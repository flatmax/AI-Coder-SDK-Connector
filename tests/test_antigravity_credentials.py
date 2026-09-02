"""Tests for aic_dc.antigravity.credentials — AG-R-8's failure, explained.

The credential path is the one thing in ``specs5/plan-ag/`` that is pure
procurement rather than engineering, and the one most likely to be met
with "but I *am* logged in". The Python SDK has no OAuth code; the ``agy``
CLI's login is unreachable from it (AG-2). So the interesting tests here
are not "does a key get found" but:

- does the *source* we report match what the SDK will actually use, and
- does the no-credential message explain the thing the user cannot deduce.

Offline and hermetic: every lookup is injected — environment, home
directory, explicit arguments — so nothing reads the developer's own
shell and no test can pass because a real key happened to be exported.
"""

from __future__ import annotations

import dataclasses

import pytest

from aic_dc.antigravity import credentials as creds
from aic_dc.antigravity.credentials import (
    API_KEY_VAR,
    GEMINI_API,
    LOCATION_VAR,
    NONE,
    PROJECT_VAR,
    VERTEX,
    VERTEX_FLAG_VARS,
    Credentials,
    MissingCredentialsError,
    resolve,
)

KEY = "AIzaSy-not-a-real-key"


@pytest.fixture
def bare_home(tmp_path):
    """A home with no ``agy`` state, so the AG-R-8 branch stays off."""
    return tmp_path


@pytest.fixture
def agy_home(tmp_path):
    """A home where the ``agy`` CLI has been used — the AG-R-8 case."""
    (tmp_path / creds.AGY_STATE_DIR).mkdir(parents=True)
    return tmp_path


def stored(home, content, mode=0o600):
    """Write the owned key file (AG-11) under an injected home.

    ``home`` alone is enough because ``resolve`` derives the config
    directory from the same injected ``env`` and ``home`` the rest of the
    lookup uses — with an empty env, that is ``<home>/.config/aic-dc`` on
    Linux, and the platform's equivalent elsewhere.
    """
    path = creds.key_file({}, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n")
    path.chmod(mode)
    return path


def dotenv(home, content, mode=0o600):
    """Write ``~/.gemini/.env`` — gemini-cli's file, not ours."""
    path = home / creds.GEMINI_ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    return path


class TestGeminiApiPath:
    def test_key_from_the_environment(self, bare_home):
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert result.mode == GEMINI_API
        assert result.api_key == KEY
        assert f"${API_KEY_VAR}" in result.source
        assert result.available is True

    def test_an_explicit_key_wins_and_says_so(self, bare_home):
        """Precedence has to match the SDK's, and the report has to match us.

        ``validate_endpoint`` falls back to ``$GEMINI_API_KEY`` only when
        no key was passed, so a passed key is the one in use — and a
        report naming the environment variable would be false.
        """
        result = resolve({API_KEY_VAR: "from-env"}, api_key=KEY, home=bare_home)
        assert result.api_key == KEY
        assert result.source == "Gemini API key from passed directly"

    def test_config_kwargs_carry_only_the_key(self, bare_home):
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert result.config_kwargs() == {"api_key": KEY}

    def test_ambient_vertex_vars_are_reported_as_inert(self, bare_home):
        """Set-but-unused configuration looks exactly like configuration.

        ``$GOOGLE_CLOUD_PROJECT`` with no Vertex flag does nothing at all,
        which is indistinguishable from working until a bill arrives on
        the wrong path.
        """
        result = resolve(
            {API_KEY_VAR: KEY, PROJECT_VAR: "some-project"}, home=bare_home
        )
        assert result.mode == GEMINI_API
        assert any(PROJECT_VAR in w for w in result.warnings)


class TestVertexPath:
    def test_the_flag_alone_switches_modes(self, bare_home):
        """``LocalAgentConfig`` reads these itself; assuming them absent
        would mean reporting a Gemini path while Vertex is in use."""
        for flag in VERTEX_FLAG_VARS:
            result = resolve(
                {flag: "true", PROJECT_VAR: "p", LOCATION_VAR: "us-central1"},
                home=bare_home,
            )
            assert result.mode == VERTEX, flag
            assert f"${flag}" in result.source

    def test_standard_mode_never_passes_the_key_alongside_the_project(
        self, bare_home
    ):
        """Passing both is a hard ValueError in the SDK, not a superset.

        So the choice is made here, and the ignored key is *reported*
        rather than silently dropped — otherwise a user with both set
        cannot tell which account is being billed.
        """
        result = resolve(
            {
                VERTEX_FLAG_VARS[0]: "true",
                PROJECT_VAR: "p",
                LOCATION_VAR: "us-central1",
                API_KEY_VAR: KEY,
            },
            home=bare_home,
        )
        kwargs = result.config_kwargs()
        assert kwargs == {"vertex": True, "project": "p", "location": "us-central1"}
        assert "api_key" not in kwargs
        assert any("ignored" in w for w in result.warnings)

    def test_express_mode_uses_the_key(self, bare_home):
        result = resolve({VERTEX_FLAG_VARS[0]: "1", API_KEY_VAR: KEY}, home=bare_home)
        assert result.mode == VERTEX
        assert result.config_kwargs() == {"vertex": True, "api_key": KEY}
        assert "express" in result.source

    def test_half_configured_standard_mode_warns(self, bare_home):
        """A project with no location silently falls through to express.

        Which bills a different path than the variable that is set
        suggests, and is the sort of thing nobody looks for.
        """
        result = resolve(
            {VERTEX_FLAG_VARS[0]: "true", PROJECT_VAR: "p", API_KEY_VAR: KEY},
            home=bare_home,
        )
        assert result.mode == VERTEX
        assert any(LOCATION_VAR in w for w in result.warnings)

    def test_vertex_with_nothing_is_unavailable_and_says_which_two(
        self, bare_home
    ):
        result = resolve({VERTEX_FLAG_VARS[0]: "true"}, home=bare_home)
        assert result.mode == NONE
        assert PROJECT_VAR in result.source
        assert LOCATION_VAR in result.source

    def test_explicit_vertex_false_beats_the_flag(self, bare_home):
        result = resolve(
            {VERTEX_FLAG_VARS[0]: "true", API_KEY_VAR: KEY},
            vertex=False,
            home=bare_home,
        )
        assert result.mode == GEMINI_API

    def test_the_flag_test_matches_the_sdks(self, bare_home):
        """``"true"`` or ``"1"`` — not "any non-empty string".

        The Claude engine's gateway vars use a *different* truthiness test
        (anything but 0/false/no), so copying that one over would switch
        modes on a value the SDK ignores.
        """
        for value in ("yes", "0", "false", "", "TRUE "):
            result = resolve(
                {VERTEX_FLAG_VARS[0]: value, API_KEY_VAR: KEY}, home=bare_home
            )
            expected = GEMINI_API if value.strip().lower() not in ("true", "1") else VERTEX
            assert result.mode == expected, value


class TestNoCredential:
    def test_absent_is_a_state_not_an_exception(self, bare_home):
        """The UI has to render this; it cannot be raised at import."""
        result = resolve({}, home=bare_home)
        assert result.mode == NONE
        assert result.available is False
        assert result.config_kwargs() == {}

    def test_require_raises_where_a_turn_needs_one(self, bare_home):
        result = resolve({}, home=bare_home)
        with pytest.raises(MissingCredentialsError):
            result.require()

    def test_require_returns_self_when_available(self, bare_home):
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert result.require() is result

    def test_an_agy_login_gets_the_agr8_explanation(self, agy_home):
        """The whole reason this module is a phase-1 deliverable.

        A user who is signed in to Antigravity and is told they are not
        authenticated will re-authenticate the wrong program, indefinitely.
        The message has to say that the login exists, that it is a
        different program, and what to do instead.
        """
        result = resolve({}, home=agy_home)
        assert result.mode == NONE
        assert "OAuth" in result.source
        assert "separate program" in result.source
        assert result.warnings, "the actionable half belongs in a warning"
        assert "aistudio.google.com" in result.warnings[0]

    def test_no_agy_login_does_not_invent_one(self, bare_home):
        """A message about a login that does not exist is a worse message."""
        result = resolve({}, home=bare_home)
        assert "OAuth" not in result.source
        assert result.warnings == ()

    def test_the_agy_check_is_a_directory_not_a_token_read(self, agy_home):
        """What matters is that the user *believes* they are authenticated.

        Any state at all is evidence of that, and reading a credential
        file would be both more fragile and a secret this module has no
        business touching.
        """
        assert creds._agy_login_exists(agy_home) is True
        for child in (agy_home / creds.AGY_STATE_DIR).iterdir():  # pragma: no cover
            raise AssertionError(f"the check should not need contents: {child}")


class TestStoredKeyFile:
    """AG-11 — the file AIC⚡DC owns, because the SDK reads none.

    ``google-antigravity`` 0.1.15 resolves credentials from environment
    variables only. An app launched from a desktop icon has no export to
    inherit, so a stored key is the difference between "works" and
    "works if you remember to run it from the right shell".
    """

    def test_a_stored_key_authenticates(self, bare_home):
        stored(bare_home, KEY)
        result = resolve({}, home=bare_home)
        assert result.mode == GEMINI_API
        assert result.api_key == KEY
        assert result.available is True

    def test_the_source_is_the_path_not_a_vague_phrase(self, bare_home):
        """"from a file" is unactionable when two files can supply one."""
        path = stored(bare_home, KEY)
        assert str(path) in resolve({}, home=bare_home).source

    def test_the_environment_still_wins(self, bare_home):
        """An export stays the way to override a stored key for one run."""
        stored(bare_home, "stored-key")
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert result.api_key == KEY
        assert f"${API_KEY_VAR}" in result.source

    def test_an_explicit_argument_wins_over_both(self, bare_home):
        stored(bare_home, "stored-key")
        result = resolve({API_KEY_VAR: "env-key"}, api_key=KEY, home=bare_home)
        assert result.api_key == KEY
        assert result.source == "Gemini API key from passed directly"

    def test_a_key_pasted_as_an_assignment_is_accepted(self, bare_home):
        """What a user copies out of a shell profile, not what we specified."""
        stored(bare_home, f"export {API_KEY_VAR}={KEY}\n")
        assert resolve({}, home=bare_home).api_key == KEY

    def test_comments_and_blank_lines_are_skipped(self, bare_home):
        stored(bare_home, f"# aistudio key, rotated 2026-08\n\n{KEY}\n")
        assert resolve({}, home=bare_home).api_key == KEY

    def test_a_world_readable_key_file_is_refused(self, bare_home):
        """We create it 0600, so a widened mode means something happened."""
        path = stored(bare_home, KEY, mode=0o644)
        result = resolve({}, home=bare_home)
        assert result.mode == NONE
        assert any(str(path) in w and "chmod 600" in w for w in result.warnings)

    def test_a_refused_key_file_is_still_reported_when_the_env_wins(
        self, bare_home
    ):
        """The next launch may not have that environment."""
        stored(bare_home, "stored-key", mode=0o644)
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert result.api_key == KEY
        assert any("chmod 600" in w for w in result.warnings)

    def test_an_empty_key_file_says_so(self, bare_home):
        """The user believes they supplied a credential. Silence is worse."""
        path = stored(bare_home, "\n# nothing here\n")
        result = resolve({}, home=bare_home)
        assert result.mode == NONE
        assert any(str(path) in w for w in result.warnings)

    def test_no_key_file_is_silent(self, bare_home):
        assert resolve({}, home=bare_home).warnings == ()

    def test_the_missing_message_names_where_to_put_a_key(self, bare_home):
        """"You have no credential" without "put it here" is half an answer."""
        result = resolve({}, home=bare_home)
        assert str(creds.key_file({}, bare_home)) in result.source

    def test_the_config_home_override_moves_the_file(self, tmp_path):
        """The same override the rest of the config layer honours."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / creds.KEY_FILENAME).write_text(KEY)
        (elsewhere / creds.KEY_FILENAME).chmod(0o600)
        env = {"AIC_DC_CONFIG_HOME": str(elsewhere)}
        assert resolve(env, home=tmp_path).api_key == KEY


class TestGeminiCliDotEnv:
    """``~/.gemini/.env`` — someone else's file, read as a convenience."""

    def test_a_key_there_is_used(self, bare_home):
        dotenv(bare_home, f"{API_KEY_VAR}={KEY}\n")
        result = resolve({}, home=bare_home)
        assert result.api_key == KEY
        assert str(bare_home / creds.GEMINI_ENV_FILE) in result.source

    def test_the_owned_file_wins(self, bare_home):
        stored(bare_home, KEY)
        dotenv(bare_home, f"{API_KEY_VAR}=older-key\n")
        assert resolve({}, home=bare_home).api_key == KEY

    def test_other_variables_are_not_mistaken_for_a_key(self, bare_home):
        """A bare or unrelated line there belongs to another program."""
        dotenv(bare_home, "GOOGLE_CLOUD_PROJECT=some-project\nnot-a-key-line\n")
        assert resolve({}, home=bare_home).mode == NONE

    def test_loose_permissions_warn_but_do_not_refuse(self, bare_home):
        """Not our file. Refusing it leaves no credential and no clue why."""
        path = dotenv(bare_home, f"{API_KEY_VAR}={KEY}\n", mode=0o644)
        result = resolve({}, home=bare_home)
        assert result.api_key == KEY
        assert any(str(path) in w for w in result.warnings)


class TestSecretsStayOut:
    """The key crosses two boundaries it must not appear on."""

    def test_report_carries_no_secret(self, bare_home):
        report = resolve({API_KEY_VAR: KEY}, home=bare_home).report()
        assert KEY not in repr(report)
        assert set(report) == {
            "available",
            "mode",
            "source",
            "project",
            "location",
            "warnings",
        }

    def test_repr_is_redacted(self, bare_home):
        """This object lands in tracebacks and debug logs."""
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        assert KEY not in repr(result)
        assert "<held>" in repr(result)
        assert "<none>" in repr(resolve({}, home=bare_home))

    def test_the_source_string_never_quotes_the_key(self, bare_home):
        for env in (
            {API_KEY_VAR: KEY},
            {VERTEX_FLAG_VARS[0]: "true", API_KEY_VAR: KEY},
        ):
            result = resolve(env, home=bare_home)
            assert KEY not in result.source
            assert all(KEY not in w for w in result.warnings)

    def test_a_key_read_from_a_file_stays_out_too(self, bare_home):
        """The file sources report a path; the path is not the secret."""
        stored(bare_home, KEY)
        result = resolve({}, home=bare_home)
        assert KEY not in repr(result)
        assert KEY not in result.source
        assert KEY not in repr(result.report())
        assert all(KEY not in w for w in result.warnings)

    def test_a_refused_file_does_not_quote_what_it_held(self, bare_home):
        stored(bare_home, KEY, mode=0o644)
        result = resolve({}, home=bare_home)
        assert all(KEY not in w for w in result.warnings)

    def test_report_is_json_serialisable(self, bare_home):
        import json

        json.dumps(resolve({API_KEY_VAR: KEY}, home=bare_home).report())


class TestReadOnlyByContract:
    def test_resolving_never_touches_the_environment(self, monkeypatch, bare_home):
        """The Claude engine's detect_credentials has the same contract.

        A resolver that exported what it found would make the next
        resolution agree with it for the wrong reason.
        """
        def _fail(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("credentials must not write the environment")

        monkeypatch.setattr("os.environ.__setitem__", _fail, raising=False)
        for env in ({}, {API_KEY_VAR: KEY}, {VERTEX_FLAG_VARS[0]: "true"}):
            resolve(env, home=bare_home)

    def test_credentials_are_frozen(self, bare_home):
        result = resolve({API_KEY_VAR: KEY}, home=bare_home)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.api_key = "other"  # type: ignore[misc]

    def test_defaults_read_the_real_environment(self, monkeypatch, tmp_path):
        """The no-argument call is the one production uses."""
        monkeypatch.setenv(API_KEY_VAR, KEY)
        monkeypatch.delenv(VERTEX_FLAG_VARS[0], raising=False)
        monkeypatch.delenv(VERTEX_FLAG_VARS[1], raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert resolve().mode == GEMINI_API


class TestCredentialsValue:
    def test_unknown_mode_yields_no_kwargs(self):
        """Defensive: an unrecognised mode must not build a broken config."""
        assert Credentials(mode="something-new", source="?").config_kwargs() == {}


def data_terms(result):
    """The AG-12 warning, or None. Matched on a phrase no other carries."""
    return next((w for w in result.warnings if "billing tier" in w), None)


class TestFreeTierDataTerms:
    """AG-12: the free tier trains on what it is sent, and we cannot detect it.

    The tier is a property of the key's Cloud project and is only readable
    over the network, which this module does not do. So the contract under
    test is not "warn about free-tier keys" — that would be the guess
    AG-R-8 records the Claude side getting wrong — but *state the
    condition, and let the user close it*.
    """

    def test_an_unacknowledged_key_carries_the_warning(self, bare_home):
        assert data_terms(resolve({API_KEY_VAR: KEY}, home=bare_home)) is not None

    def test_the_warning_states_the_condition_rather_than_asserting_a_tier(
        self, bare_home
    ):
        """It must not claim to know something it did not look at."""
        warning = data_terms(resolve({API_KEY_VAR: KEY}, home=bare_home))
        assert "cannot tell" in warning
        assert "If the key's project has no billing account" in warning

    def test_the_warning_names_the_file_that_closes_it(self, bare_home):
        """Where to put the acknowledgement is the next question asked."""
        warning = data_terms(resolve({API_KEY_VAR: KEY}, home=bare_home))
        assert str(creds.key_file({}, bare_home)) in warning
        assert f"{creds.BILLING_DIRECTIVE}={creds.BILLING_ENABLED_VALUES[0]}" in warning

    def test_the_directive_silences_it(self, bare_home):
        stored(bare_home, f"{KEY}\n{creds.BILLING_DIRECTIVE}=enabled")
        result = resolve({}, home=bare_home)
        assert data_terms(result) is None
        assert result.api_key == KEY

    def test_the_directive_is_never_mistaken_for_a_key(self, bare_home):
        """The regression that would leak a directive into the SDK as a key.

        ``_scan`` returns the first bare line, so a directive written
        without ``=`` would be read as the credential. The grammar is
        chosen to make that impossible; this pins it.
        """
        stored(bare_home, f"{creds.BILLING_DIRECTIVE}=enabled\n{KEY}")
        assert resolve({}, home=bare_home).api_key == KEY

    def test_the_directive_alone_is_not_a_credential(self, bare_home):
        stored(bare_home, f"{creds.BILLING_DIRECTIVE}=enabled")
        result = resolve({}, home=bare_home)
        assert result.mode == NONE

    def test_an_unrecognised_value_is_not_an_acknowledgement(self, bare_home):
        stored(bare_home, f"{KEY}\n{creds.BILLING_DIRECTIVE}=probably")
        assert data_terms(resolve({}, home=bare_home)) is not None

    def test_the_acknowledgement_survives_the_environment_winning(self, bare_home):
        """It is a statement about the account, not about today's launch."""
        stored(bare_home, f"{KEY}\n{creds.BILLING_DIRECTIVE}=enabled")
        result = resolve({API_KEY_VAR: "from-env"}, home=bare_home)
        assert result.api_key == "from-env"
        assert data_terms(result) is None

    def test_a_refused_file_cannot_acknowledge(self, bare_home):
        """A file we will not take a key from is not one we take a waiver from."""
        stored(bare_home, f"{KEY}\n{creds.BILLING_DIRECTIVE}=enabled", mode=0o644)
        assert data_terms(resolve({API_KEY_VAR: "from-env"}, home=bare_home))

    def test_the_dotenv_can_acknowledge_too(self, bare_home):
        dotenv(bare_home, f"{API_KEY_VAR}={KEY}\n{creds.BILLING_DIRECTIVE}=enabled\n")
        assert data_terms(resolve({}, home=bare_home)) is None

    def test_the_argument_overrides_the_lookup_in_both_directions(self, bare_home):
        stored(bare_home, KEY)
        assert data_terms(resolve({}, home=bare_home, billing_enabled=True)) is None
        stored(bare_home, f"{KEY}\n{creds.BILLING_DIRECTIVE}=enabled")
        assert data_terms(resolve({}, home=bare_home, billing_enabled=False))

    def test_vertex_standard_mode_never_carries_it(self, bare_home):
        """Both Vertex modes are paid surfaces, so the condition cannot hold."""
        env = {
            VERTEX_FLAG_VARS[0]: "true",
            PROJECT_VAR: "a-project",
            LOCATION_VAR: "us-central1",
        }
        assert data_terms(resolve(env, home=bare_home)) is None

    def test_vertex_express_mode_never_carries_it(self, bare_home):
        env = {VERTEX_FLAG_VARS[0]: "true", API_KEY_VAR: KEY}
        result = resolve(env, home=bare_home)
        assert result.mode == VERTEX
        assert data_terms(result) is None

    def test_no_credential_does_not_warn_about_one(self, bare_home):
        """The warning is about a key in use, not about the absence of one."""
        assert data_terms(resolve({}, home=bare_home)) is None

    def test_the_warning_carries_no_secret(self, bare_home):
        warning = data_terms(resolve({API_KEY_VAR: KEY}, home=bare_home))
        assert KEY not in warning
