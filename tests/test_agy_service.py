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
        """A gate pointing at another checkout gates *that* one, not this."""
        hooks = tmp_path / "hooks.json"
        monkeypatch.setattr(install, "GLOBAL_HOOKS", hooks)
        install.install(tmp_path / "cfg", path=hooks, python="/other/python")
        svc = service(tmp_path)
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "gate_not_installed"
        assert result["reason"] == "stale"

    def test_a_missing_binary_is_a_named_refusal(self, tmp_path):
        svc = service(tmp_path, executable="agy-does-not-exist")
        result = asyncio.run(svc.connect_engine())
        assert result["error"] == "not_installed"
        assert "not on PATH" in result["message"]

    def test_resume_is_declined_rather_than_silently_ignored(self, tmp_path):
        result = asyncio.run(service(tmp_path).connect_engine(resume="abc"))
        assert result["error"] == "unsupported"
        assert "lose the context" in result["message"]

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
