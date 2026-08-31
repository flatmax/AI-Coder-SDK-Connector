"""Which credential the Antigravity engine will use, and where it came from.

``specs5/plan-ag/risks.md`` AG-R-8 is the reason this module exists and is
its own phase-1 deliverable rather than three lines inside the consultant.
The Python SDK accepts a Gemini API key or a Vertex project, and it cannot
reuse the ``agy`` login the owner already has. The reason is a *backend*
split rather than a missing credential format (AG-2): ``agy`` holds an
``auth/aicode`` token for ``cloudcode-pa.googleapis.com``, and the SDK
builds only ``GeminiAPIEndpoint`` or ``VertexEndpoint``. There is no
endpoint here to point that token at, so no amount of format-shuffling
would have bridged it. So a credential is *mandatory, not conditional*,
and the most likely first experience of this engine is a user who is
demonstrably logged in to Antigravity being told they are not
authenticated.

The SDK has no OAuth *resolution* — but do not read that as "no OAuth
anywhere", which is the overbroad version of this claim and is falsifiable
in a minute. ``ModelEndpoint`` carries ``base_url`` and ``http_headers``,
and ``validate_endpoint`` returns early the moment ``base_url`` is set
(``models.py:114-115``), so an ``Authorization: Bearer`` header can be
injected at an arbitrary base URL with **no credential check at all**.
Nothing here uses that, and it is named so that its discovery is a
documented property rather than a surprise. What is genuinely absent is a
Google endpoint that both speaks ``generateContent`` and accepts an OAuth
token; the Developer API path authenticates with ``x-goog-api-key``.

Vertex is the exception and is the one path where OAuth does work
end-to-end: ``localharness`` resolves Application Default Credentials
itself, in Go, so on that path no secret passes through this module at
all. Verified 2026-08-31; see AG-11.

That is the failure this module is built to explain rather than merely
report. :func:`resolve` looks for the ``agy`` state directory, and when it
finds one beside no usable key it says so in as many words.

Read-only by contract
---------------------
Nothing here sets an environment variable, and nothing here logs a
secret. :class:`Credentials` carries the key because the config needs it,
redacts it in ``repr``, and omits it from :meth:`Credentials.report` —
which is the dict that crosses the RPC boundary to the browser.

Resolved explicitly, on purpose
-------------------------------
The SDK reads ``$GEMINI_API_KEY`` itself, inside ``validate_endpoint``
(``models.py:115-124``), so a key in the environment works whether or not
anything passes it. Resolving it here anyway and passing it explicitly is
what makes the reported source *true*: a report about a lookup somebody
else performs is a guess, and the equivalent guess on the Claude side
("the CLI will prompt for login") was observed to be wrong against a
fully authenticated session.

A file we own (AG-11)
---------------------
The SDK reads credentials from *environment variables only* — there is no
dotenv dependency in the wheel and nothing that opens ``~/.gemini/``. An
app started from a desktop launcher rather than a shell has no export to
inherit, so :func:`resolve` also reads ``gemini-api-key`` in the user
config directory: one line, mode ``0600``, and refused outright if it is
readable beyond its owner. It is deliberately *not* in ``engine.json``,
because ``Settings.get_config_content`` is not localhost-restricted and a
key there would be readable by any collaborator on a shared session.

``~/.gemini/.env`` is read after it, for users who already keep a key
there for ``gemini-cli``. That file is not ours, so a loose mode on it
earns a warning rather than a refusal.

Failing early, on purpose
-------------------------
``validate_endpoint`` raises on the connect path, so an engine with no
credential dies at session start rather than lazily. That is the right
shape and the wrong message — it arrives from inside the SDK, after a
119 MB binary has been spawned, naming two config fields.
:exc:`MissingCredentialsError` gets there first with the same facts and
the AG-R-8 explanation attached.

A warning we cannot verify, stated as a condition (AG-12)
--------------------------------------------------------
Every Gemini API key resolved here carries a data-terms warning, because
on Google's free tier prompts and responses may be used to improve their
products and may be seen by human reviewers — and the consultant's whole
job is to be handed source code. The tier is *not detectable from here*:
it is a property of the key's Cloud project, readable only over the
network. So :func:`_data_terms_warning` states the condition instead of
asserting a tier, and a ``billing=enabled`` line in the key file closes
it. Vertex never carries it; both its modes are paid surfaces.

Governing spec: ``specs5/plan-ag/`` — AG-R-8, and AG-2 for why the
existing login is unreachable.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aic_dc.config import _user_config_dir

#: Credential modes, matching the two endpoints the SDK offers.
GEMINI_API = "gemini-api"
VERTEX = "vertex"
NONE = "none"

#: The key the Gemini Developer API path uses (``models.py:119``).
API_KEY_VAR = "GEMINI_API_KEY"

#: Either of these switches ``LocalAgentConfig`` to Vertex without anyone
#: passing ``vertex=True`` (``local_connection_config.py:256-260``), which
#: is why they are read here rather than assumed absent.
VERTEX_FLAG_VARS = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE")

#: Vertex standard mode's ambient project and region
#: (``models.py:140-148``).
PROJECT_VAR = "GOOGLE_CLOUD_PROJECT"
LOCATION_VAR = "GOOGLE_CLOUD_LOCATION"

#: The ``agy`` CLI's state directory. Its presence means the owner has an
#: Antigravity login — the one this engine cannot use.
AGY_STATE_DIR = Path(".gemini") / "antigravity-cli"

#: The key file AIC⚡DC owns, inside the user config directory (AG-11).
KEY_FILENAME = "gemini-api-key"

#: ``gemini-cli``'s dotenv, read as a convenience source. The SDK never
#: opens it; if it is used, we are the ones who parsed it.
GEMINI_ENV_FILE = Path(".gemini") / ".env"

#: The line that acknowledges billing, silencing the free-tier data-terms
#: warning (AG-12). ``billing=enabled`` already parses under :func:`_scan`'s
#: grammar — it is an assignment whose name is not ``GEMINI_API_KEY``, so
#: the key scan skips it — which is why the directive uses ``=`` rather
#: than a bare word that would be mistaken for a key.
BILLING_DIRECTIVE = "billing"

#: What counts as an acknowledgement. Anything else, including a typo, is
#: not one: the failure mode of guessing wrong here is a silenced warning
#: about a live data-sharing term.
BILLING_ENABLED_VALUES = ("enabled", "paid", "true", "1")

#: Any permission bit outside the owner's. A key file carrying one of
#: these is a key the rest of the machine can read.
_GROUP_OR_WORLD = 0o077


class MissingCredentialsError(RuntimeError):
    """No usable Gemini API key or Vertex project.

    Raised before the SDK is asked to connect, so the message can explain
    AG-R-8 rather than name two config fields from inside a wheel.
    """


@dataclasses.dataclass(frozen=True)
class Credentials:
    """What the engine will authenticate with, and how we know.

    ``source`` is prose for a human and never contains the secret.
    ``warnings`` are combinations that will surprise the user — the same
    contract the Claude engine's ``detect_credentials`` has, for the same
    reason: silently billing the wrong account is worse than refusing.
    """

    mode: str
    source: str
    api_key: str | None = None
    project: str | None = None
    location: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """Whether a turn can be started at all."""
        return self.mode != NONE

    def config_kwargs(self) -> dict[str, Any]:
        """The credential half of a ``LocalAgentConfig``.

        Only the fields the chosen mode uses. Vertex rejects an
        ``api_key`` alongside ``project``/``location`` outright
        (``models.py:157-161``), so "pass everything we found" is a
        ``ValueError`` rather than a harmless superset.
        """
        if self.mode == VERTEX:
            kwargs: dict[str, Any] = {"vertex": True}
            if self.project:
                kwargs["project"] = self.project
                if self.location:
                    kwargs["location"] = self.location
            else:
                kwargs["api_key"] = self.api_key
            return kwargs
        if self.mode == GEMINI_API:
            return {"api_key": self.api_key}
        return {}

    def report(self) -> dict[str, Any]:
        """The JSON-safe view. Crosses the RPC boundary; carries no secret."""
        return {
            "available": self.available,
            "mode": self.mode,
            "source": self.source,
            "project": self.project or "",
            "location": self.location or "",
            "warnings": list(self.warnings),
        }

    def require(self) -> Credentials:
        """Self, or :exc:`MissingCredentialsError` with the AG-R-8 story."""
        if self.available:
            return self
        raise MissingCredentialsError(self.source)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        """Redacted. This object ends up in tracebacks and debug logs."""
        held = "held" if self.api_key else "none"
        return (
            f"Credentials(mode={self.mode!r}, source={self.source!r}, "
            f"api_key=<{held}>, project={self.project!r}, "
            f"location={self.location!r}, warnings={len(self.warnings)})"
        )


def _truthy(value: str | None) -> bool:
    """The SDK's own test: ``"true"`` or ``"1"``, case-insensitively."""
    return (value or "").strip().lower() in ("true", "1")


def _agy_login_exists(home: Path) -> bool:
    """Whether the ``agy`` CLI has state, i.e. the owner is logged in there.

    Deliberately a directory check rather than a token read: what matters
    is that the *user believes they are authenticated*, and any state at
    all is evidence of that. Reading a credential file would be both more
    fragile and a secret this module has no business touching.
    """
    return (home / AGY_STATE_DIR).is_dir()


def key_file(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> Path:
    """Where the owned key file lives (AG-11).

    Public because the path is user-facing: the no-credential message
    names it, and any UI that offers to save a key must write the same
    file this resolves.
    """
    return _user_config_dir(env, home) / KEY_FILENAME


def _scan(text: str, *, named_only: bool) -> str:
    """The first key in ``text``, tolerating what people actually paste.

    The owned file is specified as one bare line, but ``GEMINI_API_KEY=…``
    is what a user copies out of a shell profile, so both are accepted.
    ``named_only`` is the dotenv case, where a bare line is somebody
    else's variable and must not be mistaken for a key.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if sep:
            if name.strip().upper() == API_KEY_VAR:
                return value.strip().strip("\"'")
            continue
        if not named_only:
            return line.strip("\"'")
    return ""


def _scan_billing(text: str) -> bool:
    """Whether ``text`` carries an explicit billing acknowledgement.

    Deliberately separate from :func:`_scan` rather than folded into it.
    The key scan has one job and a security-relevant failure mode, and the
    directive is read from files that may hold no key at all.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip().lower() == BILLING_DIRECTIVE:
            return value.strip().strip("\"'").lower() in BILLING_ENABLED_VALUES
    return False


def _read_key(
    path: Path, *, named_only: bool, ours: bool
) -> tuple[str, list[str], bool]:
    """``(key, warnings, billing_acknowledged)`` for one file.

    An absent file is silent. A file that exists and cannot be used is
    *not* silent: it is the case where the user believes they have
    supplied a credential, which is the same failure shape as AG-R-8 and
    deserves the same treatment.

    Loose permissions disqualify our own file — we create it ``0600``, so
    a widened mode means something happened to it. ``~/.gemini/.env``
    belongs to another program and is only warned about; refusing to
    honour a file we do not own would leave the user with no credential
    and no way to deduce why.

    A file we refuse yields no acknowledgement either. Honouring a
    ``billing=enabled`` line out of a file whose key we would not touch
    would silence a warning using the one part of a disqualified file we
    decided to trust.
    """
    try:
        if not path.is_file():
            return "", [], False
        mode = path.stat().st_mode
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", [f"{path} exists but could not be read ({exc.strerror})."], False

    warnings: list[str] = []
    # st_mode's group/world bits are not meaningful on Windows, where
    # ACLs do this job and every file would look world-readable.
    if os.name != "nt" and mode & _GROUP_OR_WORLD:
        loose = (
            f"{path} is readable beyond its owner ({stat.filemode(mode)}). "
            f"Run: chmod 600 {path}"
        )
        if ours:
            return "", [f"{loose} — it is being ignored until then."], False
        warnings.append(loose)

    billing = _scan_billing(text)
    key = _scan(text, named_only=named_only)
    if not key:
        held = f"a ${API_KEY_VAR} line" if named_only else "a key"
        return "", warnings + [f"{path} exists but does not contain {held}."], billing
    return key, warnings, billing


def _key_from_files(
    env: Mapping[str, str], home: Path
) -> tuple[str | None, str, list[str], bool]:
    """``(key, source, warnings, billing_acknowledged)``, owned file first.

    Warnings are returned whichever source ultimately wins: a key file
    that cannot be read is worth saying out loud even when the
    environment happened to carry a working key, because the next launch
    may not have that environment.

    The acknowledgement is likewise collected from every file read, not
    only the one that supplied the key. It is a statement about the
    account, and a user who wrote it down once should not have it ignored
    because an export happened to take precedence on this launch.
    """
    warnings: list[str] = []
    found = ""
    source = ""
    billing = False
    for path, named_only, ours in (
        (key_file(env, home), False, True),
        (home / GEMINI_ENV_FILE, True, False),
    ):
        key, notes, acknowledged = _read_key(path, named_only=named_only, ours=ours)
        warnings.extend(notes)
        billing = billing or acknowledged
        if key and not found:
            found, source = key, str(path)
    return (found or None), source, warnings, billing


def resolve(
    env: Mapping[str, str] | None = None,
    *,
    api_key: str | None = None,
    vertex: bool | None = None,
    project: str | None = None,
    location: str | None = None,
    billing_enabled: bool | None = None,
    home: Path | None = None,
) -> Credentials:
    """Resolve a credential, reporting which source it came from.

    Order is explicit argument, ``$GEMINI_API_KEY``, the owned key file,
    then ``~/.gemini/.env`` (AG-11). Explicit arguments win over the
    environment, which is the precedence the SDK itself uses — a passed
    ``api_key`` short-circuits the ``$GEMINI_API_KEY`` lookup in
    ``validate_endpoint`` — and the environment wins over the files,
    so an export stays the way to override a stored key for one run.
    Everything is injectable, including the directories the files are
    looked for in, so the whole path is testable without a real key and
    without touching the process environment.

    ``billing_enabled`` acknowledges AG-12's data terms and suppresses
    :func:`_data_terms_warning`. ``None`` means "look for the
    ``billing=enabled`` line in the key files"; passing a bool overrides
    that lookup in both directions, so a caller who knows can say so and
    a caller who wants the warning back can force it.

    Never raises for a missing credential: an absent one is a state the
    UI has to render, not an exception at import. Call
    :meth:`Credentials.require` at the point where a turn actually needs
    one.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home

    env_key = env.get(API_KEY_VAR) or None
    file_key, file_source, file_warnings, file_billing = _key_from_files(env, home)
    acknowledged = file_billing if billing_enabled is None else billing_enabled
    key = api_key or env_key or file_key
    key_source = (
        "passed directly"
        if api_key
        else f"${API_KEY_VAR}"
        if env_key
        else (file_source if file_key else "")
    )
    flag_var = next((var for var in VERTEX_FLAG_VARS if _truthy(env.get(var))), None)
    use_vertex = vertex if vertex is not None else flag_var is not None

    if use_vertex:
        return _resolve_vertex(
            env,
            key=key,
            key_source=key_source,
            project=project,
            location=location,
            flag_var=flag_var,
            explicit=vertex is not None,
            warnings=file_warnings,
        )

    warnings: list[str] = list(file_warnings)
    # Ambient Vertex settings with no Vertex flag are inert, and look for
    # all the world like configuration that is in effect.
    ambient = [var for var in (PROJECT_VAR, LOCATION_VAR) if env.get(var)]
    if ambient and key:
        warnings.append(
            ", ".join(f"${var}" for var in ambient)
            + " is set but the Gemini API path is in use, so it has no effect. "
            + f"Set ${VERTEX_FLAG_VARS[0]}=true to route through Vertex instead."
        )

    if key:
        if not acknowledged:
            warnings.append(_data_terms_warning(key_file(env, home)))
        return Credentials(
            mode=GEMINI_API,
            source=f"Gemini API key from {key_source}",
            api_key=key,
            warnings=tuple(warnings),
        )
    return _missing(env, home, warnings)


def _data_terms_warning(path: Path) -> str:
    """The AG-12 free-tier data-sharing warning.

    Phrased as a conditional, and it has to be: **no local signal
    distinguishes a free-tier key from a paid one.** The tier is a
    property of the key's Google Cloud project, readable only over the
    network, and this module does not make network calls. Asserting
    "this is a free-tier key" would be exactly the guess AG-R-8 records
    the Claude side getting wrong.

    So the warning states Google's terms and says plainly which of the
    two cases we cannot tell apart. That is worth showing on every
    unacknowledged API-key launch, because the surprise it prevents —
    source code sent to a consultant becoming training data, with human
    review — is silent, irreversible and invisible in any turn output.

    Vertex does not get this warning. Both of its modes are paid surfaces
    under a data processing addendum, so the condition cannot hold.
    """
    return (
        "This engine cannot tell which billing tier a Gemini API key is on. "
        "If the key's project has no billing account, it is on the free tier, "
        "where Google may use prompts and responses — including any source "
        "code sent to the consultant — to improve its products, and human "
        "reviewers may see them. Paid tiers, and Vertex, are excluded from "
        f"that. If billing is enabled, add a line reading '{BILLING_DIRECTIVE}="
        f"{BILLING_ENABLED_VALUES[0]}' to {path} to silence this (AG-12)."
    )


def _resolve_vertex(
    env: Mapping[str, str],
    *,
    key: str | None,
    key_source: str,
    project: str | None,
    location: str | None,
    flag_var: str | None,
    explicit: bool,
    warnings: list[str] | None = None,
) -> Credentials:
    """The Vertex branch: standard mode by project, or express by key.

    The two are mutually exclusive at the SDK level and the error is a
    hard ``ValueError`` naming both, so the choice is made here — standard
    mode wins, and the unused key is reported rather than passed.
    """
    project = project or env.get(PROJECT_VAR) or None
    location = location or env.get(LOCATION_VAR) or None
    chose = "vertex=True" if explicit else f"${flag_var}"
    warnings = list(warnings or [])

    if project and location:
        if key:
            warnings.append(
                f"A Gemini API key is present ({key_source}) but Vertex "
                "standard mode is in use, so the key is ignored. Passing "
                "both is a hard error in the SDK, so it is left out."
            )
        return Credentials(
            mode=VERTEX,
            source=f"Vertex project {project} in {location} (via {chose})",
            project=project,
            location=location,
            warnings=tuple(warnings),
        )

    if project or location:
        # Half-configured standard mode. The SDK would fall through to
        # express mode if a key happened to be set, silently billing a
        # different path than the half-set variables suggest.
        have, missing_var = (
            (PROJECT_VAR, LOCATION_VAR) if project else (LOCATION_VAR, PROJECT_VAR)
        )
        warnings.append(
            f"${have} is set but ${missing_var} is not, so Vertex standard "
            "mode is incomplete. Set both, or unset both and use an "
            "express-mode API key."
        )

    if key:
        return Credentials(
            mode=VERTEX,
            source=f"Vertex express mode, API key from {key_source} (via {chose})",
            api_key=key,
            warnings=tuple(warnings),
        )

    return Credentials(
        mode=NONE,
        source=(
            f"Vertex is selected (via {chose}) but neither ${PROJECT_VAR} + "
            f"${LOCATION_VAR} nor an express-mode API key is set."
        ),
        warnings=tuple(warnings),
    )


def _missing(
    env: Mapping[str, str], home: Path, warnings: list[str] | None = None
) -> Credentials:
    """No credential, and the most useful explanation of why not.

    The AG-R-8 case gets its own sentence because it is the one a user
    cannot reason their way out of: they *are* logged in, to a different
    program, and no amount of re-authenticating that program will help.

    Both branches name the key file, because "where do I put it" is the
    question that immediately follows "you have no credential", and the
    answer is not one the user can guess.
    """
    path = key_file(env, home)
    remedy = (
        f"Get a key at aistudio.google.com/apikey and save it to {path} "
        f"(then chmod 600 it), export ${API_KEY_VAR}, or set "
        f"${VERTEX_FLAG_VARS[0]}=true with ${PROJECT_VAR} and ${LOCATION_VAR}."
    )
    looked = (
        f"no ${API_KEY_VAR}, no key file at {path}, and no Vertex project "
        f"(${VERTEX_FLAG_VARS[0]} is unset)"
    )
    if _agy_login_exists(home):
        return Credentials(
            mode=NONE,
            source=(
                f"{looked}. An Antigravity login exists at "
                f"~/{AGY_STATE_DIR}, but the Python SDK has no OAuth path "
                "and cannot use it — the agy CLI is a separate program with "
                "separate authentication. A Gemini API key or a Vertex "
                "project is required in addition to it."
            ),
            warnings=tuple(warnings or [])
            + (
                "Being signed in to Antigravity does not authenticate this "
                f"engine (AG-R-8). {remedy}",
            ),
        )
    # No warning here, unlike the AG-R-8 branch: nothing about this state
    # is surprising, so the remedy is part of the explanation rather than
    # an alert about a configuration that will mislead.
    return Credentials(
        mode=NONE,
        source=f"{looked}. {remedy}",
        warnings=tuple(warnings or []),
    )
