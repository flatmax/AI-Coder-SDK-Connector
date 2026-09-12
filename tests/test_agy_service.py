"""Tests for the ``agy`` transport's adapter.

Two claims carry the file.

**It mounts.** ``build_router(…, engine=ANTIGRAVITY)`` refuses an adapter
that cannot serve the core surface, and this one inherits two-thirds of
that surface from the SDK transport. The test that matters is the one
that fails when the inheritance stops covering it.

**It will not run ungated.** `agy` is launched with
``--dangerously-skip-permissions``, because its own headless layer
auto-denies rather than asking. That is safe *only* while our hook is
installed in the user's global configuration. A session that started
without it would be an agent editing the tree with nothing in the way, so
``connect_engine`` refuses — and refusing is the behaviour under test,
because the alternative fails silently and looks like everything working.

Offline. No ``agy``; the one lifecycle test uses a fake subprocess.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path

import pytest

from aic_dc.agy import install, roots
from aic_dc.agy.service import AgyService
from aic_dc.antigravity.service import AntigravityService
from aic_dc.capabilities import ANTIGRAVITY
from aic_dc.config import ConfigManager
from aic_dc.engine_router import build_router


def config(tmp_path):
    return types.SimpleNamespace(
        repo_root=str(tmp_path), config_dir=str(tmp_path / "cfg")
    )


def _write_gate_entry(hooks, command):
    """A hooks file on disk, without going through ``install``.

    For a state ``install`` now refuses to create — it probes the command
    first, and the ones these tests want are the ones that do not run.
    """
    hooks.write_text(
        json.dumps(
            {
                install.HOOK_NAME: {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def service(tmp_path, **kw):
    from aic_dc.antigravity.credentials import GEMINI_API, Credentials

    kw.setdefault(
        "credentials", Credentials(mode=GEMINI_API, api_key="k", source="test")
    )
    return AgyService(config(tmp_path), **kw)


def gated_service(tmp_path, **kw):
    """A service whose executable is certainly on ``PATH``.

    The gate refusals below are *after* the missing-binary refusal, so on
    a machine with no ``agy`` they never reached the check they are about
    and asserted the wrong error — the same machine-dependence the
    ``GLOBAL_HOOKS`` comment warns of, one branch earlier. `connect_engine`
    only asks whether the name resolves, and refuses long before anything
    is launched, so a name that always resolves is enough.
    """
    kw.setdefault("executable", "sh")
    return service(tmp_path, **kw)


class TestItMounts:
    def test_the_router_accepts_it(self, tmp_path):
        assert build_router(service(tmp_path), engine=ANTIGRAVITY) is not None

    def test_it_serves_the_same_surface_as_the_sdk_transport(self, tmp_path):
        """One engine, two transports — so one surface.

        A method the SDK transport answers and this one does not would be a
        capability that appears and disappears depending on how the user
        reached Antigravity, which is exactly the confusion AG-3's single
        RPC namespace exists to prevent.
        """
        exposed = {
            name
            for name, _ in inspect.getmembers(AntigravityService, inspect.isfunction)
            if not name.startswith("_")
        }
        mine = {
            name
            for name, _ in inspect.getmembers(AgyService, inspect.isfunction)
            if not name.startswith("_")
        }
        assert exposed <= mine

    def test_async_ness_is_unchanged_by_the_override(self, tmp_path):
        """An overridden method that changed sync-ness breaks the RPC contract."""
        for name in ("chat_streaming", "cancel_streaming", "connect_engine"):
            assert inspect.iscoroutinefunction(getattr(AgyService, name))
            assert inspect.iscoroutinefunction(getattr(AntigravityService, name))


class TestItWillNotRunUngated:
    """The refusal is the feature. Running anyway fails silently."""

    def test_a_turn_is_refused_when_the_gate_cannot_be_installed(
        self, tmp_path, monkeypatch
    ):
        """AG-21 changed *who* has to have installed it, not whether.

        The gate used to be a standing entry in the user's own
        ``~/.gemini/config/hooks.json``, so "not installed" was a state a
        session could find and had to refuse. It is now written into a
        config root this app owns, on the way to starting the session — so
        the only way to arrive ungated is for the install to fail, and the
        refusal has to survive that.

        ``install`` refuses to write a command it cannot run, which is the
        failure being staged here, and the one that actually shipped: a
        PyInstaller build whose hook command exited 2 on every call.
        """
        monkeypatch.setattr(
            install, "hook_runs", lambda *_a, **_k: "exit 127: no such file"
        )
        svc = gated_service(tmp_path)
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "gate_not_installed"
        assert result["reason"] == "unrunnable"
        # And it says *why*. "The gate is not installed" sends a reader to
        # a Settings button; "the command does not run here" sends them to
        # the thing that is wrong.
        assert "exit 127" in result["message"]

    def test_nothing_is_written_to_the_users_own_hooks_file(
        self, tmp_path, monkeypatch
    ):
        """The permission AG-21 gave back.

        Pointed at a temp path rather than the real one, because a test
        that got this wrong would install a hook into the developer's
        ``~/.gemini`` and pass.
        """
        theirs = tmp_path / "their-hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)
        svc = gated_service(tmp_path)
        asyncio.run(svc.connect_engine())
        assert not theirs.exists()
        installed = (
            tmp_path / "cfg" / "agy-roots" / "master" / ".gemini" / "config"
            / "hooks.json"
        )
        assert installed.is_file()

    def test_connecting_retires_the_pre_ag21_entry(self, tmp_path, monkeypatch):
        """Not writing there is half of it; the other half is upgrading.

        A machine that ran an older build already has our hook in the
        user's own file, where it fires for `agy` sessions this app knows
        nothing about. Leaving it until somebody finds the Settings button
        would make AG-21's claim true of new installs and quietly false of
        every existing one.
        """
        theirs = tmp_path / "their-hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", theirs)
        install.install(tmp_path / "old-cfg", path=theirs)
        assert theirs.is_file()

        asyncio.run(gated_service(tmp_path).connect_engine())

        assert not theirs.exists()

    def test_a_stale_entry_in_our_own_root_is_reinstalled_rather_than_refused(
        self, tmp_path
    ):
        """The state that used to be a dead end is now just a write.

        ``stale`` meant "another checkout owns the user's file", and the
        only honest answer was to refuse and ask them to reinstall. In a
        root this app owns there is nobody to ask: whatever is in the file
        is ours to correct, so `connect` corrects it.
        """
        hooks = (
            tmp_path / "cfg" / "agy-roots" / "master" / ".gemini" / "config"
            / "hooks.json"
        )
        hooks.parent.mkdir(parents=True)
        _write_gate_entry(
            hooks, install.hook_command(tmp_path / "cfg", "/other/python")
        )
        svc = gated_service(tmp_path)
        result = asyncio.run(svc.connect_engine())
        assert result.get("error") != "gate_not_installed"
        assert svc.gate_status()["state"] == "current"

    def test_a_missing_binary_is_a_named_refusal(self, tmp_path):
        svc = service(tmp_path, executable="agy-does-not-exist")
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "not_installed"
        assert "not on PATH" in result["message"]

    def test_a_resume_becomes_agys_own_conversation_flag(self, tmp_path):
        """**Was**: resume is declined, because phase 5 had not built it.

        ``--conversation <id>`` is the flag; the id is the one `agy`'s own
        ``init`` frame gave us, which is the same id the mirror filed the
        transcript under. Asserted on the argv rather than by spawning,
        because what could go wrong here is the flag name and the shape of
        its argument.
        """
        from aic_dc.agy.session import AgySession

        session = AgySession(tmp_path, gate=object(), resume="a-conversation-id")
        argv = session._argv()
        assert "--conversation" in argv
        assert argv[argv.index("--conversation") + 1] == "a-conversation-id"

    def test_a_session_that_is_not_resuming_passes_no_conversation(self, tmp_path):
        from aic_dc.agy.session import AgySession

        assert "--conversation" not in AgySession(tmp_path, gate=object())._argv()

    def test_gate_status_is_answerable_without_starting_anything(self, tmp_path):
        """The settings surface's whole question."""
        report = service(tmp_path).gate_status()
        assert report["state"] in ("absent", "current", "stale", "unreadable")
        assert "agy_present" in report


class TestItDoesNotInheritTheSdksModel:
    """The bug that made every live session fail, found by running one.

    The two Antigravity surfaces disagree about model names: the SDK takes
    ``gemini-3.7-flash`` plus a separate ``ThinkingLevel``, while ``agy``
    bakes the effort into the name and rejects the bare form —

        --model gemini-3.7-flash requires --effort (available: low, medium, high)

    Inheriting ``options.DEFAULT_MODEL`` therefore made `agy` exit before
    its init frame on **every** session. ``sdk-surface.md`` § *What `agy`
    models returns* recorded the disagreement on 2026-08-30 and the code
    did it anyway, which is why this is an assertion and not a third
    paragraph of prose.
    """

    def test_no_model_is_passed_by_default(self, tmp_path):
        from aic_dc.agy.session import AgySession

        svc = service(tmp_path)
        assert svc._model is None
        argv = AgySession(tmp_path, gate=None, model=svc._model)._argv()
        assert "--model" not in argv

    def test_the_sdk_default_is_never_what_we_send(self, tmp_path):
        from aic_dc.antigravity import options

        assert service(tmp_path)._model != options.DEFAULT_MODEL

    def test_an_explicit_model_is_still_passed(self, tmp_path):
        """A user choosing an agy model name must reach agy."""
        from aic_dc.agy.session import AgySession

        svc = service(tmp_path, model="gemini-3.7-flash-low")
        argv = AgySession(tmp_path, gate=None, model=svc._model)._argv()
        assert argv[argv.index("--model") + 1] == "gemini-3.7-flash-low"


class TestTheModelSurface:
    """`agy` has its own model vocabulary, and it must be offered.

    Setting the model to ``None`` stopped the SDK's default reaching `agy`
    and killing every session — and left the picker showing one blank
    entry, because ``get_model`` answered ``{"model": None, "models":
    [None]}``. The fix for a crash is not allowed to be a hole in the UI.
    """

    def fake_agy_models(self, tmp_path, out, rc=0):
        """A stand-in `agy` that prints a model list, like the real one."""
        script = tmp_path / "agy"
        script.write_text(
            f"#!/bin/sh\n[ \"$1\" = models ] && printf '{out}' && exit {rc}\nexit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return service(tmp_path, executable=str(script))

    LIST = "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\nclaude-sonnet-4-6\tClaude Sonnet 4.6\n"

    def test_the_ids_and_their_labels_are_offered_as_objects(self, tmp_path):
        """`get_model`'s contract is a list of **objects** on every engine.

        This assertion previously demanded bare strings, on the stated
        grounds that "the shape is a list of names". It is not: the Claude
        adapter returns the CLI's `{value, displayName, ...}` dicts and the
        browser's `modelEntries` skips anything else — so the fourteen
        names this transport sent rendered as an empty picker. The test was
        green throughout, which is why it is written against the shape the
        browser consumes rather than against the ids alone.
        """
        svc = self.fake_agy_models(tmp_path, self.LIST)
        assert asyncio.run(svc.get_model())["models"] == [
            {"value": "gemini-3.8-flash-low", "displayName": "Gemini 3.8 Flash (Low)"},
            {"value": "claude-sonnet-4-6", "displayName": "Claude Sonnet 4.6"},
        ]

    def test_every_entry_carries_the_key_the_browser_selects_on(self, tmp_path):
        """`modelEntries` reads `value`, and drops an entry without one.

        Pinned separately from the exact-list assertion above because this
        is the property that failed in the browser, and a later change that
        enriched the entries would keep that one honest by accident.
        """
        svc = self.fake_agy_models(tmp_path, self.LIST)
        offered = asyncio.run(svc.get_model())["models"]
        assert offered
        assert all(isinstance(e, dict) and e.get("value") for e in offered)

    def test_a_model_with_no_label_still_has_a_name_to_show(self, tmp_path):
        """`agy` prints `id<TAB>Label`; a bare id is not a reason to blank.

        `displayName` falls back to the id rather than to an empty string,
        because an option rendering as nothing is the failure this whole
        surface has already had once.
        """
        svc = self.fake_agy_models(tmp_path, "gemini-3.8-flash-low\n")
        assert asyncio.run(svc.get_model())["models"] == [
            {"value": "gemini-3.8-flash-low", "displayName": "gemini-3.8-flash-low"},
        ]

    def test_claude_models_routed_through_google_are_offered_too(self, tmp_path):
        """They are on the account and hiding them would be the bigger lie.

        `sdk-surface.md` notes that surfacing these naively makes "which
        engine am I talking to" hard — which the engine label now answers,
        since it says *antigravity (subscription)* beside them.
        """
        svc = self.fake_agy_models(tmp_path, self.LIST)
        offered = asyncio.run(svc.get_model())["models"]
        assert "claude-sonnet-4-6" in {entry["value"] for entry in offered}

    def test_an_unknown_name_is_refused_at_selection(self, tmp_path):
        """Rather than at session start, where it reads as a broken engine.

        This is not hypothetical: ``options.DEFAULT_MODEL`` is exactly such
        a name, and it cost a day as a bare "Error: engine".
        """
        svc = self.fake_agy_models(tmp_path, self.LIST)
        result = asyncio.run(svc.set_model("gemini-3.7-flash"))
        assert result["error"] == "unknown_model"
        assert "exiting" in result["message"]
        # And it did not take.
        assert svc._model is None

    def test_a_known_name_is_accepted(self, tmp_path):
        svc = self.fake_agy_models(tmp_path, self.LIST)
        assert asyncio.run(svc.set_model("claude-sonnet-4-6"))["model"] == (
            "claude-sonnet-4-6"
        )

    def test_an_unreadable_list_does_not_block_a_choice(self, tmp_path):
        """Empty means *unknown*, never *none*.

        A picker that went blank because a subprocess timed out would look
        exactly like this transport having no models — and refusing every
        name on that basis would strand the user.
        """
        svc = service(tmp_path, executable="agy-does-not-exist")
        assert asyncio.run(svc.get_model())["models"] == []
        assert asyncio.run(svc.set_model("anything"))["model"] == "anything"

    def test_the_list_is_read_once(self, tmp_path):
        """It is a subprocess, and the answer belongs to the account."""
        counter = tmp_path / "runs"
        script = tmp_path / "agy"
        script.write_text(
            f"#!/bin/sh\necho x >> {counter}\nprintf 'a\tA\n'\n", encoding="utf-8"
        )
        script.chmod(0o755)
        svc = service(tmp_path, executable=str(script))

        async def go():
            await svc.get_model()
            await svc.get_model()

        asyncio.run(go())
        assert counter.read_text().count("x") == 1


@pytest.fixture
def wired(tmp_path, monkeypatch):
    conv = "b1d377c5-ef66-4d58-a7ca-5aee75acc853"
    fake = tmp_path / "fake_agy.py"
    fake.write_text(
        "import json,sys,os\n"
        f'conv = "{conv}"\n'
        "def emit(o):\n"
        "    sys.stdout.write(json.dumps(o)+'\\n'); sys.stdout.flush()\n"
        'emit({"event":"init","conversation_id":conv,'
        '"init":{"cwd":os.getcwd(),"tools":[]}})\n'
        "for line in sys.stdin:\n"
        "    if not line.strip():\n"
        "        continue\n"
        '    emit({"event":"step_update","step_update":{"step_index":1,'
        '"state":"DONE","step_type":"agent_response","text_delta":"done."}})\n'
        '    emit({"event":"result","result":{"status":"SUCCESS",'
        '"response":"done.","usage":{"total_tokens":7}}})\n',
        encoding="utf-8",
    )
    launcher = tmp_path / "agy"
    launcher.write_text(
        f"#!/bin/sh\nexec {sys.executable} {fake}\n", encoding="utf-8"
    )
    launcher.chmod(0o755)

    # Nothing global: AG-21 put the entry in the root the service
    # spawns against, and the service writes it itself on connect.
    # Pinned anyway so a regression that reaches for the user's own
    # file lands in a temp path rather than in their home directory.
    monkeypatch.setattr(install, "GLOBAL_HOOKS", tmp_path / "their-hooks.json")

    events: list = []

    async def callback(name, *args):
        events.append((name, args))

    svc = service(tmp_path, executable=str(launcher), event_callback=callback)
    return svc, events


class TestATurn:
    """One turn end to end against a fake ``agy``, with the gate installed."""

    def test_the_reply_arrives_before_the_turn_finishes(self, wired):
        """The contract the SDK transport learned the hard way.

        A reply that waits for the turn is one the browser's 75s deadline
        kills, after which the agent keeps working and the transcript says
        it failed.
        """
        svc, _events = wired

        async def go():
            reply = await svc.chat_streaming("r1", "hello")
            for task in list(svc._turn_tasks):
                await task
            await svc.shutdown()
            return reply

        assert asyncio.run(go()) == {"status": "started"}

    def test_the_turn_reaches_the_browser_as_events(self, wired):
        svc, events = wired

        async def go():
            await svc.chat_streaming("r1", "hello")
            for task in list(svc._turn_tasks):
                await task
            await svc.shutdown()

        asyncio.run(go())
        names = [n for n, _ in events]
        assert "streamChunk" in names
        assert "streamComplete" in names

    def test_a_second_turn_is_refused_synchronously(self, wired):
        svc, _events = wired

        async def go():
            await svc.chat_streaming("r1", "hello")
            second = await svc.chat_streaming("r2", "again")
            for task in list(svc._turn_tasks):
                await task
            await svc.shutdown()
            return second

        assert asyncio.run(go())["reason"] == "turn_in_progress"

    def test_cancelling_a_turn_that_is_not_running_says_so(self, wired):
        svc, _events = wired
        assert asyncio.run(svc.cancel_streaming("nope"))["status"] == "not_running"

    def test_shutdown_releases_the_conversation(self, wired, tmp_path):
        """So the hook goes back to treating it as a stranger's."""
        from aic_dc.agy import registry

        svc, _events = wired

        async def go():
            await svc.chat_streaming("r1", "hello")
            for task in list(svc._turn_tasks):
                await task
            claimed = svc._session.conversation_id
            await svc.shutdown()
            return claimed

        conv = asyncio.run(go())
        assert registry.lookup(conv, config_dir=tmp_path / "cfg") is None

    def test_the_installed_hook_names_this_interpreter(self, wired, tmp_path):
        """A gate pointing elsewhere gates a different build.

        Read out of the master root rather than out of ``~/.gemini``, and
        read *after* a connect rather than before one, because AG-21 made
        the install part of starting the session instead of a precondition
        of it.
        """
        svc, _events = wired

        async def go():
            await svc.connect_engine()
            await svc.shutdown()

        asyncio.run(go())
        assert svc.gate_status()["state"] == "current"
        hooks = (
            tmp_path / "cfg" / "agy-roots" / "master" / ".gemini" / "config"
            / "hooks.json"
        )
        data = json.loads(hooks.read_text(encoding="utf-8"))
        command = data[install.HOOK_NAME]["PreToolUse"][0]["hooks"][0]["command"]
        assert sys.executable in command
        assert not (tmp_path / "their-hooks.json").exists()


class TestTheSessionContractTheServiceReadsThrough:
    """`AgyService` inherits methods that reach into the *session*.

    The adapter test above pins the RPC surface — which methods exist.
    That is not the same contract as the one broken here three times, and
    the difference is why a green suite shipped all three: inheriting a
    method also inherits every attribute that method reads off objects the
    subclass supplies, and nothing enumerates those.

    The live failures, in order:

    - `translator.stats` — every permission dialog raised `AttributeError`
      in `_note_permission_prompt`; caught and logged, so only the turn's
      prompt count was lost.
    - `session.read_only` — `get_current_state` and `get_engine_status`
      both read it, and neither catches, so the **whole app-state load
      failed** and the browser could not render the engine at all.

    So these assert on the attributes rather than on the methods. Adding a
    `session.foo` to an inherited method still needs a test, but this makes
    the common case fail here rather than in somebody's browser.
    """

    #: Read off `self._session` by `AntigravityService.get_current_state`
    #: and `get_engine_status`, which `AgyService` inherits unchanged.
    SESSION_ATTRS = ("conversation_id", "started", "read_only")

    def _session(self, tmp_path):
        from aic_dc.agy.gate_server import AgyGateServer
        from aic_dc.agy.session import AgySession
        from aic_dc.antigravity.permissions import AntigravityPermissionGate

        async def broadcast(_event):
            return None

        gate = AntigravityPermissionGate(
            tmp_path, broadcast=broadcast, config_dir=tmp_path / 'cfg'
        )
        return AgySession(
            tmp_path,
            gate=AgyGateServer(tmp_path / "g.sock", gate=gate),
        )

    @pytest.mark.parametrize("attr", SESSION_ATTRS)
    def test_the_session_answers_what_the_inherited_methods_read(
        self, tmp_path, attr
    ):
        assert hasattr(self._session(tmp_path), attr)

    def test_a_gated_session_is_not_read_only(self, tmp_path):
        # The gate is this transport's decide hook: agy runs with
        # --dangerously-skip-permissions, so it is the only thing between
        # the model and the working tree.
        assert self._session(tmp_path).read_only is False

    def test_the_sdk_transports_session_answers_the_same_names(self):
        # If the SDK session grows an attribute the shared methods read,
        # this is where the agy one is noticed to be missing it.
        from aic_dc.antigravity.session import AntigravitySession

        for attr in self.SESSION_ATTRS:
            assert hasattr(AntigravitySession, attr), attr

    @pytest.mark.asyncio
    async def test_get_current_state_does_not_raise_with_a_session_attached(
        self, tmp_path
    ):
        """The user's actual symptom, reproduced.

        `'AgySession' object has no attribute 'read_only'` surfaced as
        `RPC ClaudeCodeService.get_current_state() failed`, three times in
        one page load.
        """
        svc = service(tmp_path)
        svc._session = self._session(tmp_path)
        state = await svc.get_current_state()
        assert state["read_only"] is False
        assert state["connected"] is False


class TestTheWriteGuidance:
    """Why every `agy` prompt carries a framing block.

    `agy` declares `write_to_file` as *"Use this tool to create new
    files"*, with `ArtifactMetadata` documented as *"Required when
    creating an artifact file"* — optional, by its own schema, for
    anything else. The *presence* of that field is nonetheless what makes
    it enforce `artifacts must be in <appDataDir>/brain/<conversation-id>`,
    so a model that fills it in for an ordinary source file gets its write
    refused. Measured twice against a real session on 2026-09-05, and
    confirmed by `agy` itself when asked.

    The refusal is not recoverable in-flight: it happens inside `agy`
    while *declaring permissions*, which is before any hook runs, so the
    gate never sees the call and cannot amend it. The model then routes
    around the broken tool with a `run_command` heredoc — and a write that
    arrives as a shell command has no diff to render, no attributable
    file, and no rule "always allow" could ever match twice.
    """

    def test_every_prompt_carries_it(self, tmp_path, monkeypatch):
        from aic_dc.agy import tools as agy_tools

        sent = {}

        async def fake_run(self, session, translator, request_id, message):
            sent["message"] = message

        monkeypatch.setattr(AgyService, "_run_agy_turn", fake_run)

        async def fake_ensure(self):
            # Not `object()`: the turn reads the session's conversation id
            # to build the translator, since that is what names the
            # directory `agy` writes a generated image into.
            return types.SimpleNamespace(conversation_id="b1d377c5")

        monkeypatch.setattr(AgyService, "_ensure_session", fake_ensure)
        svc = service(tmp_path)
        asyncio.run(svc.chat_streaming("r1", "please create a hello world script"))
        asyncio.run(asyncio.sleep(0))
        assert sent["message"].startswith(agy_tools.WRITE_GUIDANCE)
        assert sent["message"].endswith("please create a hello world script")

    def test_it_names_the_field_that_causes_the_failure(self):
        """The guidance has to be specific to work.

        "Prefer write_to_file" alone does not help — the model was already
        preferring it. What it could not know is that one optional field
        makes the call unrecoverable.
        """
        from aic_dc.agy import tools as agy_tools

        assert "ArtifactMetadata" in agy_tools.WRITE_GUIDANCE
        assert "write_to_file" in agy_tools.WRITE_GUIDANCE

    def test_it_is_wrapped_in_the_framing_the_reader_strips(self):
        """It is for the model, not for the user.

        `history.strip_framing` removes this block at read time, so a
        browsed transcript shows what the user typed. Storing the framed
        text and stripping it on the way out is deliberate — the
        transcript's job is to say what the model was actually sent.
        """
        from aic_dc.agy import tools as agy_tools
        from aic_dc.claude_code.history import strip_framing

        framed = agy_tools.WRITE_GUIDANCE + "do the thing"
        assert strip_framing(framed) == "do the thing"


class TestModelPersistence:
    """AG-R-15's other half: a picker that forgot what you told it.

    `set_model` assigned an instance attribute and nothing else, so a model
    chosen in Settings came back as the account's default at the next
    server start — a control that appears to work, for one session.
    """

    def test_a_chosen_model_survives_a_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(tmp_path / "cfg"))
        config = ConfigManager()
        service = AgyService(config=config)
        monkeypatch.setattr(
            type(service),
            "_list_models",
            lambda _self: _completed([{"value": "gemini-3.8-flash-high"}]),
        )
        result = asyncio.run(service.set_model("gemini-3.8-flash-high"))
        assert result["persisted"] is True
        assert AgyService(config=ConfigManager())._model == "gemini-3.8-flash-high"

    def test_a_failed_write_keeps_the_session_choice(self, tmp_path, monkeypatch):
        """The selection is what the user asked for; persistence is a bonus
        that must not cost it."""
        monkeypatch.setenv("AIC_DC_CONFIG_HOME", str(tmp_path / "cfg"))
        config = ConfigManager()
        service = AgyService(config=config)
        monkeypatch.setattr(
            type(service),
            "_list_models",
            lambda _self: _completed([{"value": "gemini-3.8-flash-high"}]),
        )
        monkeypatch.setattr(
            type(config),
            "set_engine_option",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only")),
        )
        result = asyncio.run(service.set_model("gemini-3.8-flash-high"))
        assert result["model"] == "gemini-3.8-flash-high"
        assert result["persisted"] is False


async def _completed(value):
    return value


class _StubStore:
    """Only the one method ``_mirrors_session`` calls.

    Deliberately not a ``RepoSessionStore``: the question under test is
    whether the service *asks*, and a real store would answer it out of a
    tree these tests would then have to build.
    """

    def __init__(self, session_ids=(), error=None):
        self._ids = list(session_ids)
        self._error = error
        self.keys: list[str] = []

    async def list_sessions(self, project_key):
        self.keys.append(project_key)
        if self._error is not None:
            raise self._error
        return [{"session_id": sid} for sid in self._ids]


class _Reader:
    """A stand-in for :mod:`aic_dc.agy.subagents`, recording every call.

    The reader has its own test file, exercised against files on disk. What
    these tests are about is the two gates *in front of* it, so the thing
    that matters here is which of these functions was reached and with
    what — and, for a refusal, that none of them was.
    """

    def __init__(self, *, rows=(), owned=(), messages=(), error=None):
        self._rows = list(rows)
        self._owned = set(owned)
        self._messages = list(messages)
        self._error = error
        self.rows_for: list[str] = []
        self.descendants_for: list[str] = []
        self.loaded: list[str] = []
        #: Every ``brain_dir`` the service handed over. Recorded because
        #: AG-R-18 is precisely the argument being wrong: the functions
        #: used to default it to a constant derived from the server's own
        #: ``HOME`` at import time, which is the one tree no session's
        #: ``agy`` ever writes to once AG-21 gives it a private root.
        self.brains: list = []

    def rows(self, conversation_id, *, brain_dir):
        self.rows_for.append(conversation_id)
        self.brains.append(brain_dir)
        if self._error is not None:
            raise self._error
        return self._rows

    def descendants(self, conversation_id, *, brain_dir):
        self.descendants_for.append(conversation_id)
        self.brains.append(brain_dir)
        if self._error is not None:
            raise self._error
        return self._owned

    def load(self, conversation_id, *, brain_dir):
        self.loaded.append(conversation_id)
        self.brains.append(brain_dir)
        return self._messages


def _reading_service(tmp_path, monkeypatch, reader, *, mirrored=("sess",), store=None):
    from aic_dc.agy import subagents

    svc = service(tmp_path)
    svc.session_store = _StubStore(mirrored) if store is None else store
    # No session is attached, so the "which conversation is on screen?"
    # answer has to be explicit rather than a mirror lookup.
    svc._auto_resume = False
    for name in ("rows", "descendants", "load"):
        monkeypatch.setattr(subagents, name, getattr(reader, name))
    return svc


class TestSubagentTranscripts:
    """The ``subagent_transcripts`` surface, and the two gates behind it.

    The reader these methods delegate to takes a conversation id and
    returns whatever ``agy`` wrote under it, which on this machine includes
    **every conversation the user has ever had with the Antigravity IDE**.
    Nothing in the id's shape distinguishes one of ours from one of those.
    So the RPC is only as safe as its containment, and the containment is
    two independent checks — the session has to be one this repository
    mirrors, and the agent has to be reachable by announcement from it.

    Half of these tests therefore assert that the reader was *not* called.
    That is the assertion that fails when a refusal is turned into a
    warning, which is the way this kind of check usually stops working.
    """

    # -- the listing -----------------------------------------------------

    def test_the_brain_it_reads_is_the_root_the_session_runs_against(
        self, tmp_path, monkeypatch
    ):
        """AG-R-18, at the surface where it would have been invisible.

        These readers used to default ``brain_dir`` to a module constant
        built from ``Path.home()`` at import time. AG-21 gives every
        session a private root, so that constant names a tree the session's
        ``agy`` never writes to — and the failure is not an exception, it
        is a subagent list that is correctly empty for the wrong reason.
        The argument is required now, and this is what says which value it
        must carry.
        """
        reader = _Reader(rows=[{"agent_id": "child"}])
        svc = _reading_service(tmp_path, monkeypatch, reader)
        asyncio.run(svc.list_subagent_transcripts("sess"))
        expected = (
            tmp_path / "cfg" / "agy-roots" / "master" / ".gemini"
            / "antigravity-cli" / "brain"
        )
        assert reader.brains == [expected]
        assert Path.home() not in expected.parents

    def test_it_lists_the_session_it_was_given(self, tmp_path, monkeypatch):
        reader = _Reader(rows=[{"agent_id": "child"}])
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.list_subagent_transcripts("sess"))
        assert got == [{"agent_id": "child"}]
        assert reader.rows_for == ["sess"]

    def test_it_defaults_to_the_session_on_screen(self, tmp_path, monkeypatch):
        """Matching the Claude adapter, so the common call needs no argument
        — and cannot name a session other than the one being read."""
        reader = _Reader()
        svc = _reading_service(tmp_path, monkeypatch, reader)
        svc._resume_request = "sess"
        asyncio.run(svc.list_subagent_transcripts())
        assert reader.rows_for == ["sess"]

    def test_a_session_we_do_not_mirror_is_not_read(self, tmp_path, monkeypatch):
        """An empty list rather than an error, because the honest answer to
        "what did that conversation delegate?" from this service is that it
        has no such conversation. What matters is that it did not look."""
        reader = _Reader(rows=[{"agent_id": "child"}])
        svc = _reading_service(tmp_path, monkeypatch, reader, mirrored=("other",))
        assert asyncio.run(svc.list_subagent_transcripts("sess")) == []
        assert reader.rows_for == []

    def test_no_session_lists_nothing(self, tmp_path, monkeypatch):
        reader = _Reader()
        svc = _reading_service(tmp_path, monkeypatch, reader)
        assert asyncio.run(svc.list_subagent_transcripts()) == []
        assert reader.rows_for == []

    def test_a_failed_read_is_answered_rather_than_raised(self, tmp_path, monkeypatch):
        """A session that delegated nothing and a listing that could not be
        read want opposite reactions from the user, so they must not both
        be an empty list."""
        reader = _Reader(error=OSError("brain directory vanished"))
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.list_subagent_transcripts("sess"))
        assert "brain directory vanished" in got["error"]

    # -- one transcript --------------------------------------------------

    def test_it_reads_a_subagent_this_session_announced(self, tmp_path, monkeypatch):
        reader = _Reader(owned={"child"}, messages=[{"role": "user", "content": "hi"}])
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("child", "sess"))
        assert got == [{"role": "user", "content": "hi"}]
        assert reader.descendants_for == ["sess"]
        assert reader.loaded == ["child"]

    def test_a_conversation_this_session_never_announced_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The hole this method would otherwise be: an id from ``agy``'s
        store that AIC⚡DC never owned, read because it parsed."""
        reader = _Reader(owned={"child"}, messages=[{"role": "user", "content": "hi"}])
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("somebody-elses", "sess"))
        assert "not a subagent of this conversation" in got["error"]
        assert reader.loaded == []

    def test_the_session_itself_is_refused_through_this_door(
        self, tmp_path, monkeypatch
    ):
        """``descendants`` excludes the conversation it was asked about, and
        this is the consequence that makes that exclusion matter: the main
        transcript has its own RPC, with its own rules."""
        reader = _Reader(owned={"child"})
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("sess", "sess"))
        assert "error" in got
        assert reader.loaded == []

    def test_a_session_we_do_not_mirror_is_refused_before_the_id_is_used(
        self, tmp_path, monkeypatch
    ):
        """Either gate alone leaks. This one is first because it is the one
        that stops an unmirrored session's *own* announcements from
        authorising anything."""
        reader = _Reader(owned={"child"})
        svc = _reading_service(tmp_path, monkeypatch, reader, mirrored=("other",))
        got = asyncio.run(svc.get_subagent_transcript("child", "sess"))
        assert got["error"] == "sess is not a session in this repository"
        assert reader.descendants_for == []
        assert reader.loaded == []

    def test_an_empty_agent_id_is_refused(self, tmp_path, monkeypatch):
        reader = _Reader()
        svc = _reading_service(tmp_path, monkeypatch, reader)
        assert "error" in asyncio.run(svc.get_subagent_transcript("", "sess"))
        assert reader.descendants_for == []

    def test_no_session_is_refused(self, tmp_path, monkeypatch):
        reader = _Reader()
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("child"))
        assert "No session" in got["error"]
        assert reader.descendants_for == []

    def test_a_pruned_transcript_says_so_rather_than_drawing_an_empty_tab(
        self, tmp_path, monkeypatch
    ):
        """A subagent that ran wrote records, so nothing to render means the
        record is gone — not that the subagent said nothing."""
        reader = _Reader(owned={"child"}, messages=[])
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("child", "sess"))
        assert "no readable transcript" in got["error"]

    def test_a_failed_read_is_answered_here_too(self, tmp_path, monkeypatch):
        reader = _Reader(error=RuntimeError("unreadable"))
        svc = _reading_service(tmp_path, monkeypatch, reader)
        got = asyncio.run(svc.get_subagent_transcript("child", "sess"))
        assert "unreadable" in got["error"]

    # -- the ownership check itself --------------------------------------

    def test_without_a_mirror_nothing_is_owned(self, tmp_path, monkeypatch):
        """No repository, no mirror, and so no way to tell one of our
        conversations from one of the IDE's."""
        reader = _Reader(owned={"child"})
        svc = _reading_service(tmp_path, monkeypatch, reader)
        svc.session_store = None
        assert asyncio.run(svc.list_subagent_transcripts("sess")) == []
        assert reader.rows_for == []

    def test_a_check_that_fails_refuses_rather_than_allows(
        self, tmp_path, monkeypatch
    ):
        """The direction this must fail in. A store that cannot be listed is
        a reason to show nothing, never a reason to show everything."""
        reader = _Reader(owned={"child"})
        svc = _reading_service(
            tmp_path,
            monkeypatch,
            reader,
            store=_StubStore(error=OSError("store gone")),
        )
        got = asyncio.run(svc.get_subagent_transcript("child", "sess"))
        assert "not a session in this repository" in got["error"]
        assert reader.loaded == []

    def test_the_check_asks_about_this_repository(self, tmp_path, monkeypatch):
        """Not about the store as a whole: `agy`'s conversations are keyed by
        project, and the mirror for another checkout is not ours either."""
        from claude_agent_sdk import project_key_for_directory

        store = _StubStore(("sess",))
        reader = _Reader()
        svc = _reading_service(tmp_path, monkeypatch, reader, store=store)
        asyncio.run(svc.list_subagent_transcripts("sess"))
        assert store.keys == [project_key_for_directory(str(tmp_path))]

    # -- where the file work happens -------------------------------------

    def test_the_reads_are_handed_to_an_executor(self):
        """One transcript is one file, but it is on the user's disk and this
        loop is serving a live turn. Asserted structurally because the cost
        of getting it wrong is a stall nothing measures — the same reason
        the other history reads here are pinned this way.
        """
        source = inspect.getsource(AgyService.list_subagent_transcripts)
        source += inspect.getsource(AgyService.get_subagent_transcript)
        for name in ("subagents.rows", "subagents.descendants", "subagents.load"):
            index = source.index(name)
            assert "run_in_executor" in source[max(0, index - 120) : index], name
class TestStoppingOneSubagent:
    """``stop_task`` — ⏹ on a row, built 2026-09-10.

    The RPC contract is the browser's: ``stopping``, never ``stopped``.
    ``chat-panel/index.js::_stopSubagent`` keeps the row live until the
    *stream* reports it terminal, because a row that greyed out on the
    request would claim a subagent had ended while its tools were still
    running. These tests are about what the method does to the gate and to
    the row's word; whether the refusal reaches the agent is
    ``scripts/probe_agy_subagent_stop.py``'s question and was measured on a
    live turn.
    """

    ANNOUNCED = "45f0eec3-66f6-4544-a1fd-a15dabe512bd"
    STRANGER = "01dab20c-69b0-4c09-ac35-3382a7b1c8d1"

    def _service_with_a_live_subagent(self, tmp_path):
        from aic_dc.agy.steps import AgyTranslator

        svc = service(tmp_path)
        translator = AgyTranslator("r1")
        translator._subagents[self.ANNOUNCED] = {"agent_id": self.ANNOUNCED}
        svc._turns["r1"] = translator

        class FakeGate:
            def __init__(self):
                self.refused = {}

            def refuse_conversation(self, conversation_id, reason):
                self.refused[conversation_id] = reason

        gate = FakeGate()
        svc._agy_gate = gate
        return svc, gate, translator

    def test_an_announced_subagent_is_refused_and_reported_stopping(self, tmp_path):
        svc, gate, _translator = self._service_with_a_live_subagent(tmp_path)
        answer = asyncio.run(svc.stop_task(self.ANNOUNCED))
        assert answer == {"status": "stopping", "task_id": self.ANNOUNCED}
        assert self.ANNOUNCED in gate.refused
        assert "stopped this subagent" in gate.refused[self.ANNOUNCED]

    def test_the_reason_tells_the_model_not_to_route_around_it(self, tmp_path):
        """AG-R-11's sentence, one level down.

        A refused agent has been measured reaching for another way; the
        reason a stopped subagent reads has to close that off, because
        unlike a denied *call* there is no second dialog behind it.
        """
        svc, gate, _translator = self._service_with_a_live_subagent(tmp_path)
        asyncio.run(svc.stop_task(self.ANNOUNCED))
        assert "not try another way" in gate.refused[self.ANNOUNCED]

    def test_a_conversation_this_turn_never_announced_is_refused_the_handle(
        self, tmp_path
    ):
        """The containment half, and the reason this is not a thin wrapper.

        ``task_id`` here is ``agy``'s own conversation id, so an unchecked
        one would let a caller aim a refusal at any conversation on the
        machine — the same door ``agy/subagents.py`` shuts on the reading
        side of the identifier.
        """
        svc, gate, _translator = self._service_with_a_live_subagent(tmp_path)
        answer = asyncio.run(svc.stop_task(self.STRANGER))
        assert answer["status"] == "not_running"
        assert gate.refused == {}

    def test_the_row_is_marked_so_it_settles_stopped_rather_than_completed(
        self, tmp_path
    ):
        """``agy`` reports a starved subagent DONE — measured, 2026-09-10.

        Left alone, the LED table would take that to ``completed`` and put
        green over something the user stopped, so the terminal word for a
        stopped id is this host's.
        """
        svc, _gate, translator = self._service_with_a_live_subagent(tmp_path)
        asyncio.run(svc.stop_task(self.ANNOUNCED))
        assert self.ANNOUNCED in translator._stopped

    def test_a_session_with_no_gate_says_so_rather_than_pretending(self, tmp_path):
        svc, _gate, _translator = self._service_with_a_live_subagent(tmp_path)
        svc._agy_gate = None
        answer = asyncio.run(svc.stop_task(self.ANNOUNCED))
        assert "error" in answer

    def test_an_empty_id_is_an_error_not_a_refusal_aimed_at_nothing(self, tmp_path):
        svc, gate, _translator = self._service_with_a_live_subagent(tmp_path)
        answer = asyncio.run(svc.stop_task(""))
        assert "error" in answer
        assert gate.refused == {}

    def test_a_remote_client_cannot_stop_anything(self, tmp_path):
        """The localhost rule every write-shaped RPC on this service follows.

        Through ``_collab``, which is what ``_check_localhost_only``
        actually reads — the first version of this test patched
        ``_localhost_available`` instead, which is the gate's deadline
        question, and passed the call straight through while asserting it
        had been stopped.
        """
        svc, gate, _translator = self._service_with_a_live_subagent(tmp_path)

        class Remote:
            def is_caller_localhost(self):
                return False

        svc._collab = Remote()
        answer = asyncio.run(svc.stop_task(self.ANNOUNCED))
        assert answer == {"error": "restricted", "reason": "localhost_only"}
        assert gate.refused == {}


class TestTheConsultantAgyCanReach:
    """AG-22's wiring: the listener was built, and reachable by nothing.

    What closes AG-1's asymmetry is not the listener, it is the file that
    tells `agy` where the listener is. These tests are about that file and
    about the token's lifetime, because both were chosen against failure
    modes rather than inherited from a default.
    """

    def offer(self, tmp_path, monkeypatch, *, available=True, fail=False):
        from aic_dc.claude_code import consult_listener as listener_module
        from aic_dc.claude_code.consultant import ClaudeConsultant

        monkeypatch.setattr(ClaudeConsultant, "available", lambda self: available)
        if fail:
            async def explode(self):
                raise OSError("no socket for you")

            monkeypatch.setattr(
                listener_module.ConsultationListener, "start", explode
            )
        svc = service(tmp_path)
        root = roots.master_root(svc._config_dir)
        return svc, root

    def test_the_config_names_the_listener_that_is_running(
        self, tmp_path, monkeypatch
    ):
        """The whole point, asserted on the artefact ``agy`` actually reads.

        ``serverUrl`` rather than ``url`` is measured — ``url`` is not read
        — and the bearer has to be one the listener will honour, so the
        token is checked against the grant table rather than for being
        non-empty.
        """
        svc, root = self.offer(tmp_path, monkeypatch)

        async def go():
            await svc._offer_consultant(root)
            try:
                body = json.loads(
                    roots.mcp_config_file(root).read_text(encoding="utf-8")
                )
                entry = body["mcpServers"]["aic-dc-claude"]
                assert entry["serverUrl"] == (
                    f"http://127.0.0.1:{svc._listener.port}/mcp"
                )
                token = entry["headers"]["Authorization"].removeprefix("Bearer ")
                assert svc._listener.resolve(token) is not None
                assert token == svc._consult_token
            finally:
                await svc._retire_consultant()

        asyncio.run(go())

    def test_no_claude_means_no_file_rather_than_a_dead_one(
        self, tmp_path, monkeypatch
    ):
        """An install with no Claude CLI is a supported install.

        A config file left behind would send `agy` dialling a port that
        answers nothing, and the user would read an MCP error for a
        feature that was never switched on.
        """
        svc, root = self.offer(tmp_path, monkeypatch, available=False)
        roots.write_mcp_config(root, {"mcpServers": {"stale": {}}})

        asyncio.run(svc._offer_consultant(root))
        assert not roots.mcp_config_file(root).exists()
        assert svc._listener is None

    def test_a_listener_that_will_not_start_does_not_fail_the_session(
        self, tmp_path, monkeypatch
    ):
        """An optional second opinion must not stop an engine.

        And it must not leave a config file either: every failure path
        leaves no file rather than a stale one.
        """
        svc, root = self.offer(tmp_path, monkeypatch, fail=True)
        roots.write_mcp_config(root, {"mcpServers": {"stale": {}}})

        asyncio.run(svc._offer_consultant(root))
        assert not roots.mcp_config_file(root).exists()
        assert svc._listener is None
        assert svc._consult_token is None

    def test_the_token_dies_with_the_child(self, tmp_path, monkeypatch):
        """Lifetime bound to the subprocess and to nothing else."""
        svc, root = self.offer(tmp_path, monkeypatch)

        async def go():
            await svc._offer_consultant(root)
            listener = svc._listener
            token = svc._consult_token
            await svc._retire_consultant()
            assert listener.resolve(token) is None
            assert listener.port is None
            assert svc._listener is None and svc._consult_token is None
            assert not roots.mcp_config_file(root).exists()

        asyncio.run(go())

    def test_one_spawn_holds_one_token(self, tmp_path, monkeypatch):
        """A second offer retires the first, so no token outlives its child."""
        svc, root = self.offer(tmp_path, monkeypatch)

        async def go():
            await svc._offer_consultant(root)
            first, first_token = svc._listener, svc._consult_token
            await svc._offer_consultant(root)
            try:
                assert svc._consult_token != first_token
                assert first.resolve(first_token) is None
                assert svc._listener.resolve(svc._consult_token) is not None
            finally:
                await svc._retire_consultant()

        asyncio.run(go())


class _StubListener:
    """Records the host's pushes without binding a socket."""

    def __init__(self):
        self.turns: list = []
        self.cancelled: list = []

    def begin_turn(self, token, turn_id):
        self.turns.append(("begin", token, turn_id))

    def end_turn(self, token, turn_id):
        self.turns.append(("end", token, turn_id))

    async def cancel_session(self, session_id):
        self.cancelled.append(session_id)
        return True

    async def revoke(self, token):
        pass

    async def aclose(self):
        pass


class TestTheTurnDrivesTheBudget:
    """The consultation budget is per turn, and nothing on the wire says so.

    An MCP request carries no turn id, so the host's push at the moment it
    writes the prompt is the only place that identity exists. Between turns
    is a *closed* state: a consultation arriving after the host considers
    the turn over must be refused rather than quietly spending the next
    turn's budget before it opens.
    """

    @pytest.fixture
    def stubbed(self, wired, monkeypatch):
        svc, _events = wired
        stub = _StubListener()

        async def offer(config_root):
            attach()

        def attach():
            svc._listener = stub
            svc._consult_token = "tok"
            svc._consult_spawn_id = "spawn"

        # Attached now *and* re-attached on spawn: a test that starts no
        # session still has a listener to push to, and one that does still
        # goes through the real call site.
        attach()
        monkeypatch.setattr(svc, "_offer_consultant", offer)
        return svc, stub

    def test_a_turn_opens_and_closes_its_own_budget(self, stubbed):
        svc, stub = stubbed

        async def go():
            await svc.chat_streaming("r1", "hello")
            for task in list(svc._turn_tasks):
                await task
            await svc.shutdown()

        asyncio.run(go())
        assert stub.turns == [("begin", "tok", "r1"), ("end", "tok", "r1")]

    def test_the_stop_button_reaches_a_consultation(self, stubbed):
        """The gate cannot, and that is measured.

        Around an MCP call the hook timeline is ``PreToolUse`` at the start
        and nothing until ``PostInvocation`` when it returns, so the
        starvation the stop performs is queued behind the very thing it is
        stopping — for up to `agy`'s own three minutes.
        """
        svc, stub = stubbed
        # The turn is placed here rather than run, because the fake `agy`
        # finishes in milliseconds and a stop racing it would pass or fail
        # on timing rather than on the branch under test.
        svc._turns["r2"] = object()

        assert asyncio.run(svc.cancel_streaming("r2")) == {
            "status": "ok",
            "request_id": "r2",
        }
        assert stub.cancelled == ["spawn"]

    def test_a_stop_for_a_turn_that_is_over_reaches_nothing(self, stubbed):
        """No live turn, no consultation to cancel."""
        svc, stub = stubbed
        assert asyncio.run(svc.cancel_streaming("gone"))["status"] == "not_running"
        assert stub.cancelled == []
