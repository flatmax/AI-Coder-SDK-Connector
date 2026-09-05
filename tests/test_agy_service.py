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

import pytest

from aic_dc.agy import install
from aic_dc.agy.service import AgyService
from aic_dc.antigravity.service import AntigravityService
from aic_dc.capabilities import ANTIGRAVITY
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

    def test_a_turn_is_refused_when_the_gate_is_not_installed(
        self, tmp_path, monkeypatch
    ):
        # Pointed at a temp file rather than the real one. Without this the
        # test reads the developer's own ~/.gemini/config/hooks.json and
        # passes or fails on whether *they* have the gate installed — which
        # is how it went green for a day and then red the moment one was.
        monkeypatch.setattr(install, "GLOBAL_HOOKS", tmp_path / "hooks.json")
        svc = service(tmp_path)
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "gate_not_installed"
        assert result["reason"] == "absent"
        # The message says where to fix it, and that fixing it sticks —
        # the gate is not removed on shutdown, so a session started later
        # finds it ready.
        assert "Settings" in result["message"]
        assert "until you remove it" in result["message"]

    def test_a_stale_gate_is_refused_too(self, tmp_path, monkeypatch):
        """A gate pointing at another checkout gates *that* one, not this.

        Written rather than installed: since 2026-09-05 ``install`` probes
        the command and refuses one that does not run, and
        ``/other/python`` does not. This describes a file an installation
        that has since moved left behind, which is a state that arrives on
        disk rather than through the installer.
        """
        hooks = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", hooks)
        _write_gate_entry(
            hooks, install.hook_command(tmp_path / "cfg", "/other/python")
        )
        svc = service(tmp_path)
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "gate_not_installed"
        assert result["reason"] == "stale"

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


class TestATurn:
    """One turn end to end against a fake ``agy``, with the gate installed."""

    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
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

        hooks = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", hooks)
        install.install(tmp_path / "cfg", path=hooks)

        events: list = []

        async def callback(name, *args):
            events.append((name, args))

        svc = service(tmp_path, executable=str(launcher), event_callback=callback)
        return svc, events

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
        """A gate pointing elsewhere gates a different build."""
        svc, _events = wired
        assert svc.gate_status()["state"] == "current"
        data = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        command = data[install.HOOK_NAME]["PreToolUse"][0]["hooks"][0]["command"]
        assert sys.executable in command


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
            return object()

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
        from aic_dc.claude_code.history import strip_framing

        from aic_dc.agy import tools as agy_tools

        framed = agy_tools.WRITE_GUIDANCE + "do the thing"
        assert strip_framing(framed) == "do the thing"
