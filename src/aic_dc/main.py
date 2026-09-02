"""AIC-DC startup orchestrator — Layer 6.

Two-phase startup:

Phase 1 (fast, < 1 second):
  - Validate git repo
  - Find available ports
  - Initialize lightweight services (ConfigManager, Repo, Settings, DocConvert)
  - Create ClaudeCodeService — the engine adapter, cold: no CLI process
    is spawned until the first turn
  - Register services with JRPCServer, start it
  - Open browser

Phase 2 (background, non-blocking):
  - Initialize SymbolIndex via run_in_executor
  - Hand the index to ClaudeCodeService
  - Index repository in batches
  - Build reference index
  - Schedule the doc-index build
  - Signal ready

Phase 2 is shorter than it was. Two steps went with the native engine:
the deferred-init call that wired a token counter and context manager
around the symbol index, and the stability-tracker pass that turned the
repository into four cache tiers. The CLI reads what it needs when it
needs it, so the indexes are built here purely for AIC⚡DC's own use —
Monaco's hovers and go-to-definition, the file tree, and (from phase 4)
the MCP tools the agent can call.

Governing spec: specs4/6-deployment/startup.md
"""

from __future__ import annotations

# os is imported first so the env-var setdefaults below
# fire before any import that might transitively load
# numpy / scipy / sklearn / torch and bake in their BLAS
# thread-pool sizes.
import os

# Constrain native math libraries to a single thread BEFORE
# any import that might pull numpy/scipy/sklearn/torch.
# Field incident: segfault inside OpenBLAS's threaded SGEMM
# kernel (sgemm_oncopy_SKYLAKEX) during KeyBERT's MMR
# cosine-similarity computation. The crash reproduces on
# Python 3.14 + OpenBLAS in threaded mode and disappears
# when OpenBLAS is restricted to one thread.
#
# These env vars only take effect if set BEFORE the library
# is loaded — once OpenBLAS or MKL has initialised its
# thread pool the count is baked in for the lifetime of
# the process. Setting them here, at the top of main.py,
# ensures the safety net is in place before any aic_dc
# import runs.
#
# setdefault preserves explicit user overrides (e.g. for
# benchmarking). The cost of single-threaded BLAS for
# keyword enrichment is negligible — bottleneck is the
# sentence-transformer forward pass, not the matmul.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("BLIS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")

import asyncio
import atexit
import logging
import signal
import sys
import time
import traceback
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Windows has no SIGKILL; there ``os.kill`` with SIGTERM calls
# TerminateProcess, which is already the unignorable form — so
# the escalation below has nothing to escalate to, and nothing
# to wait for either (no ``os.WNOHANG``).
_HARD_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_WNOHANG = getattr(os, "WNOHANG", None)


def _kill_cli_children(grace: float = 0.25) -> None:
    """Kill every live Claude Code CLI subprocess, synchronously.

    Called from the signal handler immediately before
    ``os._exit``. The SDK tracks its children in a module-level
    set and SIGTERMs them from an ``atexit`` hook — and
    ``os._exit`` never runs ``atexit``, so that guard never fires
    for us. This is the same job, done by hand.

    Measured with no kill at all: SIGINT during a streaming turn
    left the ``claude`` process running for a further ~38
    seconds, reparented to init, still holding the repo as its
    cwd. Measured with SIGTERM: a mid-stream child was gone in
    under 0.3s. So SIGTERM is the whole mechanism in the normal
    case, and the escalation below exists for a child that has
    wedged — the case where doing what the SDK does (signal and
    go) would leave exactly the orphan this function is for.

    A blocking wait inside a signal handler is only acceptable
    because the next statement is ``os._exit``: nothing else is
    waiting on this thread, and in the normal case the wait ends
    within one poll interval.
    """
    try:
        from claude_agent_sdk._internal.transport import subprocess_cli

        children = list(subprocess_cli._ACTIVE_CHILDREN)
    except Exception as exc:
        # Private SDK surface. An SDK that moves it leaves us
        # with the old behaviour — a child that lingers until it
        # notices stdin closed — rather than a server that will
        # not exit.
        logger.debug("Cannot reach the SDK child registry: %s", exc)
        return

    pending: set[int] = set()
    for child in children:
        try:
            pid = child.pid
        except Exception:
            continue
        pending.add(pid)
        with suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    if not pending or _WNOHANG is None:
        return

    deadline = time.monotonic() + grace
    while True:
        pending = {pid for pid in pending if not _child_exited(pid)}
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    for pid in pending:
        with suppress(OSError):
            os.kill(pid, _HARD_KILL)


def _kill_vite(vite_process: Any) -> None:
    """Kill the Vite dev/preview server and everything ``npx`` put under it.

    ``terminate()`` was not enough, and the comment that used to sit in
    the signal handler — "vite shuts itself down once the parent dies" —
    was wrong. We launch ``npx vite``, and ``npx`` becomes a chain:
    ``npm exec vite`` → ``sh -c "vite"`` → ``node .../vite``. ``Popen``
    holds the pid of the *wrapper*, so ``terminate()`` signalled the top
    of that chain and the node process holding the port survived,
    reparented to init.

    Observed on one machine before the fix: orphan Vite servers of 22h40m
    and 50m still bound to ports 19000 and 19001, and a third orphaned
    on demand by sending SIGTERM to a running server. Ctrl-C at a
    terminal often hid it, because the shell signals the whole foreground
    group and reaches ``node`` that way — so the leak only showed up when
    the server was ended any other way.

    ``start_new_session=True`` at launch puts the chain in its own
    process group precisely so this can address all of it at once. That
    also takes Vite out of the terminal's foreground group, which is why
    the kill here is now the *only* thing that stops it — hence the
    fallback to signalling the wrapper directly if the group lookup
    fails.
    """
    if vite_process is None:
        return
    try:
        os.killpg(os.getpgid(vite_process.pid), signal.SIGTERM)
        return
    except Exception as exc:
        # No process group to speak of (Windows, or a vite that already
        # exited and was reaped). Fall back to the old behaviour rather
        # than leaving it entirely unsignalled.
        logger.debug("Could not signal the Vite process group: %s", exc)
    with suppress(Exception):
        vite_process.terminate()
        return
    with suppress(Exception):
        vite_process.kill()


def _purge_resume_dirs() -> None:
    """Remove the temp config dirs a resumed session materialised.

    Called from the signal handler *after* ``_kill_cli_children``,
    because those directories are the live ``CLAUDE_CONFIG_DIR`` of the
    children it kills. Same gap as the kill above: the SDK cleans up in
    ``disconnect()``, and ``os._exit`` never gets there.

    Wrapped because an import failure at shutdown must not be the reason
    the server will not exit. See
    :mod:`aic_dc.claude_code.resume_cleanup` for what accumulates without
    this.
    """
    try:
        from aic_dc.claude_code import resume_cleanup

        resume_cleanup.purge()
    except Exception as exc:
        logger.debug("Could not purge resume temp dirs: %s", exc)


# How long the graceful step below gets before the exit stops waiting for
# it. The useful work is milliseconds — denying a pending permission and
# getting the notice onto the websocket — and the slow part is the SDK's
# own disconnect against a CLI that may be wedged. Ctrl-C has to work on
# a wedged engine, so it is bounded rather than awaited.
_SHUTDOWN_GRACE = 2.0


async def _shut_the_engine_down(service: Any, timeout: float = _SHUTDOWN_GRACE) -> None:
    """Run the service's graceful teardown, bounded, before the hard exit.

    Everything ``ClaudeCodeService.shutdown`` does that survives process
    death is already done by hand below — ``_kill_cli_children`` and
    ``_purge_resume_dirs`` exist precisely because ``disconnect()`` is
    unreachable from here. What it adds is the one effect that leaves this
    process: **a pending permission request gets denied, and the denial is
    announced to the browser**, which survives the server. Without this a
    user who stops the server with a dialog open keeps a live-looking
    dialog that will never resolve, and
    ``5-webapp/permission-dialog.md``'s ``shutdown`` cause — one of the
    four it says a denial must name — was unreachable.

    Failures are logged at debug and swallowed. Nothing here may be the
    reason the server will not exit; a second Ctrl-C skips it entirely.
    """
    try:
        answer = await asyncio.wait_for(service.shutdown(), timeout)
    except Exception as exc:
        # Includes the timeout. A wedged CLI is the expected case.
        logger.debug("Graceful engine shutdown did not finish: %r", exc)
        return
    if isinstance(answer, dict) and answer.get("error") == "restricted":
        # The localhost gate reads the *current RPC caller*, and a remote
        # participant's call can be mid-dispatch when the signal lands.
        # Logged rather than worked around: the window is microseconds
        # wide, and a silent skip would look like the teardown ran.
        logger.warning("Graceful engine shutdown was refused by the localhost gate")


def _install_exit_handlers(
    loop: asyncio.AbstractEventLoop,
    service: Any,
    teardown: Callable[..., None],
    others: tuple[Any, ...] = (),
) -> None:
    """Route SIGINT/SIGTERM through the grace period, then ``teardown``.

    Installed on the *loop* on POSIX, and that is the whole reason this
    function exists rather than a pair of ``signal.signal`` calls: a
    C-level handler cannot await a coroutine, and the one part of teardown
    whose effect leaves this process is a coroutine
    (:func:`_shut_the_engine_down`).

    ``teardown`` ends in ``os._exit``, so nothing here returns twice in the
    normal case. The second signal is the exception and it is deliberate:
    the grace period is a courtesy, and a user pressing Ctrl-C again has
    just withdrawn it.

    On Windows the proactor loop owns no signals, ``add_signal_handler``
    raises :exc:`NotImplementedError`, and the immediate exit that was
    here before ``next.md`` § C8 stands. Stated rather than worked around
    — the same platform split as :func:`_kill_vite`'s process group.
    """
    exiting = False

    async def graceful_then_exit() -> None:
        # The master first, then every other mounted engine. `others` is
        # normally cold — an engine nobody switched to never connected —
        # and shutting a cold adapter down is a no-op, so the cost of
        # being thorough here is nil and the cost of not being is a
        # pending permission dialog on the engine the user switched away
        # from, left live forever.
        await _shut_the_engine_down(service)
        for other in others:
            await _shut_the_engine_down(other)
        teardown()

    def on_signal() -> None:
        nonlocal exiting
        if exiting:
            teardown()
            # ``teardown`` ends in ``os._exit`` so this never runs in
            # production. It is here so the behaviour does not *depend* on
            # that: without it a second Ctrl-C also queues a second
            # graceful step, against an engine already being torn down.
            return
        exiting = True
        loop.create_task(graceful_then_exit())

    try:
        loop.add_signal_handler(signal.SIGINT, on_signal)
        loop.add_signal_handler(signal.SIGTERM, on_signal)
    except NotImplementedError:
        logger.debug("No loop signal handlers here; exiting without the grace period")
        signal.signal(signal.SIGINT, teardown)
        signal.signal(signal.SIGTERM, teardown)


def _child_exited(pid: int) -> bool:
    """Whether child ``pid`` has exited, reaping it if it has.

    ``os.kill(pid, 0)`` cannot answer this. A child we have
    signalled but not waited for is a zombie, and a zombie still
    answers signal 0 — so a liveness probe would report every
    successfully killed child as alive and burn the whole grace
    period before hard-killing a corpse.

    The event loop is gone by the time this runs, so nothing is
    reaping for us and ``Process.returncode`` never updates.
    ``waitpid`` is the only thing left that can tell "exited"
    from "still streaming".
    """
    try:
        reaped, _status = os.waitpid(pid, _WNOHANG)
    except ChildProcessError:
        # Already reaped, or never ours to wait on.
        return True
    except OSError:
        # Nothing further we could usefully do to it.
        return True
    return reaped == pid


# ---------------------------------------------------------------------------
# Exit-trigger diagnostics
# ---------------------------------------------------------------------------
#
# Field incident: AIC-DC processes have been exiting with no
# log line preceding the post-exit ``resource_tracker``
# semaphore warning. The existing signal handler only logs
# SIGINT/SIGTERM, so any other exit path (uncaught exception
# in a background task, sys.exit from unexpected code,
# event-loop crash, OOM kill) leaves no breadcrumbs and we
# can't distinguish "user closed browser" from "doc-index
# task crashed silently" from "main coroutine returned
# normally".
#
# Three hooks installed below catch every Python-visible
# exit path:
#
# 1. ``atexit`` — fires on every clean Python interpreter
#    shutdown. Logs the moment the process starts its
#    final unwind, regardless of who triggered it.
# 2. ``sys.excepthook`` — fires on unhandled exceptions in
#    the main thread. Captures the traceback before
#    Python's default handler prints to stderr and exits.
# 3. ``asyncio`` loop exception handler — fires on
#    unhandled exceptions in background tasks (doc-index
#    build, enrichment loop, post-write hooks). Without
#    this, a bug in a task's own error-handling path
#    silently kills the task; the loop keeps running
#    but the work it represented is lost.
#
# The first two are installed at module load (before any
# aic_dc import that might transitively spawn threads or
# tasks). The asyncio handler is installed at the top of
# ``run()`` once we have a loop reference.


def _on_atexit() -> None:
    """Mark the start of process unwind.

    Fires on any clean exit — signal handler, sys.exit,
    main coroutine return, exception in main thread that
    propagates to the top. Pairs with the
    ``resource_tracker`` warning that fires later in the
    same unwind: if we see this log line, the unwind was
    Python-driven; if not, the process died abruptly
    (segfault, OOM kill, kill -9) and kernel dmesg is the
    next thing to check.

    Uses ``os.write`` directly rather than the logger
    because by the time atexit handlers run the logging
    module's stream handlers may already be torn down by
    pytest's capture machinery (or any harness that closes
    stderr early). The logging module catches the
    resulting ``ValueError`` internally and prints a
    "--- Logging error ---" diagnostic to stderr, which is
    noisy and unhelpful at this stage. The raw fd write is
    both more reliable and quieter.
    """
    try:
        os.write(2, b"[aic-dc] atexit: process unwinding...\n")
    except Exception:
        pass


atexit.register(_on_atexit)


def _excepthook(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_tb: Any,
) -> None:
    """Log unhandled exceptions in the main thread.

    Replaces Python's default excepthook with one that
    routes through our logger first, then chains to the
    default so the user still sees the traceback on
    stderr. KeyboardInterrupt is handled specially —
    it's the user's Ctrl+C and shouldn't get a scary
    "unhandled exception" log line; the signal handler
    logs the shutdown intent properly.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        # Defer to default — signal handler logs cleanly.
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    try:
        logger.error(
            "Unhandled exception in main thread: %s",
            "".join(
                traceback.format_exception(
                    exc_type, exc_value, exc_tb
                )
            ),
        )
    except Exception:
        pass
    # Chain to default so stderr still shows the traceback
    # for users who don't have the log open.
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _excepthook


def _asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop,
    context: dict[str, Any],
) -> None:
    """Log unhandled exceptions from background asyncio tasks.

    asyncio's default handler logs to its own logger at
    ERROR level, which the user typically doesn't see in
    the steady-state output. Routing through our logger
    plus a structured prefix makes these visible in the
    same place as the rest of AIC-DC's diagnostics.

    The ``context`` dict carries the exception object
    (``"exception"``), the message (``"message"``), and
    sometimes the future / task / handle that raised
    (``"future"`` / ``"task"`` / ``"handle"``). We log
    all three so a wedged background task can be
    identified by name in the log.
    """
    message = context.get("message", "asyncio error")
    exception = context.get("exception")
    task = context.get("task") or context.get("future")
    try:
        if exception is not None:
            tb = "".join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )
            logger.error(
                "asyncio unhandled exception: %s | task=%r\n%s",
                message, task, tb,
            )
        else:
            logger.error(
                "asyncio unhandled error: %s | task=%r | context=%r",
                message, task, context,
            )
    except Exception:
        # Logger failure mid-shutdown — fall back to default.
        loop.default_exception_handler(context)


def _find_webapp_dist() -> Path | None:
    """Locate the built webapp directory.

    Priority:
    1. PyInstaller bundle (sys._MEIPASS)
    2. Source tree (project_root/webapp/dist)
    3. Installed package data (package_dir/webapp_dist)

    Entry 3's producer is ``hatch_build.py``, whose ``WHEEL_DEST`` has to
    name the same ``webapp_dist`` directory this looks for. Changing one
    without the other yields an install that serves nothing;
    ``test_wheel_destination_matches_the_runtime_lookup`` holds them
    together.
    """
    # PyInstaller bundle
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "aic_dc" / "webapp_dist"
        if candidate.is_dir():
            return candidate

    # Source tree — walk up from this file to find webapp/dist
    pkg_dir = Path(__file__).resolve().parent
    project_root = pkg_dir.parent.parent
    candidate = project_root / "webapp" / "dist"
    if candidate.is_dir():
        return candidate

    # Installed package data
    candidate = pkg_dir / "webapp_dist"
    if candidate.is_dir():
        return candidate

    return None


def _warn_if_dist_is_stale(webapp_dist: Path) -> None:
    """Warn when the bundled webapp is older than its sources.

    ``--preview`` rebuilds before serving, and the comment on that
    branch says why: a stale bundle means the user is looking at old
    code, and if the RPC contract moved since the build, the app calls
    methods the backend no longer has. The plain path has no such
    guard — it serves whatever ``npm run build`` last left behind,
    however long ago that was — so the same failure arrives with no
    hint of its cause. This does not rebuild (that would put Node on
    the critical path of every launch); it just refuses to let the
    drift be silent.

    Only meaningful against a source tree. An installed package ships
    ``webapp_dist`` with no ``webapp/src`` beside it to compare, and
    absent sources are not evidence of staleness, so that case says
    nothing.
    """
    src_dir = webapp_dist.parent / "src"
    if not src_dir.is_dir():
        return

    index = webapp_dist / "index.html"
    if not index.is_file():
        return

    try:
        built = index.stat().st_mtime
        newest = max(
            (p.stat().st_mtime for p in src_dir.rglob("*") if p.is_file()),
            default=0.0,
        )
    except OSError:
        # A file that vanished mid-walk is not worth failing startup
        # over; the warning is a convenience, not a precondition.
        return

    if newest > built:
        logger.warning(
            "webapp/dist is older than webapp/src — you are being served "
            "a stale webapp. Rebuild with `cd webapp && npm run build`, or "
            "launch with `aic-dc --preview` to rebuild automatically.",
        )


def _write_not_a_repo_page(repo_path: str) -> str:
    """Write a self-contained HTML instruction page and return path."""
    import tempfile

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AIC⚡DC</title>
<style>
body {{ background: #0d1117; color: #c9d1d9; font-family: system-ui;
       display: flex; justify-content: center; align-items: center;
       min-height: 100vh; margin: 0; }}
.box {{ text-align: center; max-width: 600px; padding: 2rem; }}
h1 {{ font-size: 4rem; opacity: 0.18; margin-bottom: 1rem; }}
.path {{ color: #58a6ff; font-family: monospace; font-size: 1.1rem; }}
pre {{ background: #161b22; padding: 1rem; border-radius: 8px;
       text-align: left; color: #7ee787; }}
</style></head><body><div class="box">
<h1>AIC⚡DC</h1>
<p>The path is not a git repository:</p>
<p class="path">{repo_path}</p>
<pre>cd {repo_path}\ngit init</pre>
<p>Or run aic-dc from inside an existing repository.</p>
</div></body></html>"""

    fd, path = tempfile.mkstemp(suffix=".html", prefix="aic-dc-")
    os.write(fd, html.encode("utf-8"))
    os.close(fd)
    return path


async def _send_progress(
    event_callback: Any,
    stage: str,
    message: str,
    percent: int,
) -> None:
    """Best-effort progress notification to the browser."""
    if event_callback is None:
        return
    try:
        await event_callback("startupProgress", stage, message, percent)
    except Exception:
        pass  # Browser may not be connected yet


async def _heavy_init(
    claude_code_service: Any,
    repo: Any,
    config: Any,
    event_callback: Any,
    other_engines: dict[str, Any] | None = None,
) -> None:
    """Phase 2 — heavy initialization as a background task.

    Runs via ensure_future so the event loop stays free for
    WebSocket frames (pings, RPC calls).
    """
    from aic_dc.symbol_index.index import SymbolIndex

    loop = asyncio.get_event_loop()

    # Brief pause for browser to connect
    await asyncio.sleep(0.5)

    # Step 1: Initialize symbol index
    await _send_progress(event_callback, "symbol_index",
                         "Initializing symbol parser...", 10)
    try:
        symbol_index = await loop.run_in_executor(
            None, lambda: SymbolIndex(repo.root)
        )
    except Exception as exc:
        logger.warning("Symbol index construction failed: %s", exc)
        symbol_index = None

    # Step 2: Hand the index to the engine adapter, so Monaco's
    # hovers and go-to-definition start resolving. Cheap and
    # synchronous — two attribute assignments — so no executor.
    await _send_progress(event_callback, "session_restore",
                         "Completing initialization...", 30)
    if symbol_index is not None:
        # Handed to *every* mounted adapter, and it is the one instance —
        # a second engine building its own index over the same tree is
        # the duplication the adapter was written to avoid, and an engine
        # that never received one answers every hover with "no answer"
        # after a switch, silently.
        # Imported here rather than read from `main()`'s scope: this runs
        # in `_heavy_init`, a different function, and the name was not
        # bound in it — a `NameError` that took the *whole* deferred init
        # down, so no adapter got an index and every hover answered "no
        # answer" for the life of the session. It logged one traceback at
        # startup and looked like a slow index otherwise.
        from aic_dc import capabilities

        for name, adapter in (
            (capabilities.CLAUDE, claude_code_service),
            *sorted((other_engines or {}).items()),
        ):
            try:
                adapter._attach_symbol_index(symbol_index)
            except Exception as exc:
                logger.warning(
                    "Attaching the symbol index to the %s engine failed: %s",
                    name,
                    exc,
                )

    # Step 3: Index repository in batches
    if symbol_index is not None and repo is not None:
        await _send_progress(event_callback, "indexing",
                             "Indexing repository...", 50)
        try:
            flat = await loop.run_in_executor(
                None, repo.get_flat_file_list
            )
            file_list = [f for f in flat.split("\n") if f]
            # Seed the import resolver's file set BEFORE
            # per-file indexing so _resolve_imports_for_file
            # can populate Import.resolved_target correctly.
            # Without this, every resolver lookup during
            # per-file indexing returns None (the resolver's
            # file set is empty), every import gets
            # resolved_target=None, and cross-file
            # Go-to-Definition silently fails. The full
            # index_repo path does this via set_files; the
            # batched path here has to replicate it.
            await loop.run_in_executor(
                None,
                lambda: symbol_index._resolver.set_files(file_list),
            )
            batch_size = 20
            total = len(file_list)
            for i in range(0, total, batch_size):
                batch = file_list[i:i + batch_size]
                await loop.run_in_executor(
                    None,
                    lambda b=batch: [symbol_index.index_file(f) for f in b]
                )
                await asyncio.sleep(0)  # yield for WebSocket pings
                pct = 50 + int(40 * min(i + batch_size, total) / max(total, 1))
                await _send_progress(
                    event_callback, "indexing",
                    f"Indexing repository... {min(i + batch_size, total)}/{total}",
                    pct,
                )
            # Resolve cross-file call-site targets now that every
            # file's imports are in place. index_repo does this
            # automatically; the batched path has to call it
            # explicitly. Without it, call sites keep
            # target_file=None and references / Go-to-Def on
            # function calls fall back to symbol-name lookups
            # that may miss the mark.
            await loop.run_in_executor(
                None, symbol_index._resolve_call_sites,
            )
            # Build reference index after all files
            await loop.run_in_executor(
                None,
                lambda: symbol_index._ref_index.build(
                    list(symbol_index._all_symbols.values())
                )
            )
            # The map now covers the whole repo, which is what the
            # `symbol_map` / `file_symbols` / `find_references` tools wait
            # for. Monaco has been resolving hovers since step 2 — a
            # partial index is fine for a hover and misleading as a map.
            claude_code_service._mark_symbol_index_ready()
        except Exception as exc:
            logger.warning("Repository indexing failed: %s", exc)
            # Deliberately not "still building": the partial index that is
            # sitting right there would answer a map query with a repo
            # that is missing files, and nothing in the answer would say
            # so. The tools report it unavailable and point at Grep.
            claude_code_service._mark_symbol_index_failed()

    # Step 4: Schedule the doc-index background build. It has to
    # be started from the event loop thread — the builder calls
    # asyncio.get_running_loop() and ensure_future, and in an
    # executor thread the former would hand back a fresh dead
    # loop. It reports its own progress from here on (the
    # doc_index and doc_enrichment_* stages), and the enrichment
    # pass may run for minutes after "Ready".
    try:
        claude_code_service._schedule_doc_index_build()
    except Exception as exc:
        logger.warning("Doc index build scheduling failed: %s", exc)

    # Step 5: Signal ready
    await _send_progress(event_callback, "ready", "Ready", 100)
    logger.info("Initialization complete")


def _start_static_server(
    webapp_dir: Path,
    port: int,
    host: str = "127.0.0.1",
) -> None:
    """Start a threaded HTTP server for the bundled webapp.

    Runs in a daemon thread so it doesn't block shutdown.
    """
    import http.server
    import threading

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(webapp_dir), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            pass  # Silent

        def end_headers(self) -> None:
            # Cache policy, and it is not cosmetic. Field incident:
            # a browser served a fresh tab an `index.html` from its
            # own cache, and with it the hashed bundle that
            # index.html used to name — a bundle that no longer
            # existed on disk. The app that came up was weeks old
            # and called RPC methods this backend has since
            # deleted, so every call failed with "method not found
            # on proxy". It looked like a broken backend. It was a
            # cached frontend.
            #
            # SimpleHTTPRequestHandler sends Last-Modified and no
            # Cache-Control, which leaves the freshness lifetime to
            # the browser's heuristic. For a URL as stable as
            # `/index.html` that heuristic can reuse the cached
            # copy without ever asking us, so a rebuilt webapp
            # stays invisible.
            #
            # Two rules. Vite gives every asset a content hash in
            # its name, so `/assets/**` is immutable by
            # construction — a changed file is a changed URL, and
            # caching it for a year is free. Everything else, above
            # all the entry document that names those hashes, must
            # be revalidated on every load.
            if self.path.startswith("/assets/"):
                self.send_header(
                    "Cache-Control", "public, max-age=31536000, immutable",
                )
            else:
                self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

        def do_GET(self) -> None:
            # SPA fallback — requests without extension that don't
            # match a real file serve index.html
            path = self.translate_path(self.path)
            if not Path(path).exists() and "." not in Path(self.path).name:
                self.path = "/index.html"
            try:
                super().do_GET()
            except (BrokenPipeError, ConnectionResetError):
                pass

    class _Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request: Any, client_address: Any) -> None:
            exc = sys.exc_info()[1]
            if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
                return
            super().handle_error(request, client_address)

    server = _Server((host, port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Static file server on http://%s:%d", host, port)


async def run(
    repo_path: str | Path | None = None,
    server_port: int = 18080,
    webapp_port: int = 18999,
    no_browser: bool = False,
    dev: bool = False,
    preview: bool = False,
    verbose: bool = False,
    collab: bool = False,
) -> None:
    """Main entry point — runs the two-phase startup.

    Called from cli.py or directly for programmatic use.
    """
    from aic_dc.config import ConfigManager
    from aic_dc.doc_convert import DocConvert
    from aic_dc.logging_setup import configure
    from aic_dc.repo import Repo, RepoError
    from aic_dc.rpc import RpcServer, find_available_port
    from aic_dc.settings import Settings

    configure(verbose=verbose)

    # Install the asyncio loop exception handler now that
    # logging is configured. Must run before any
    # ``ensure_future`` / ``create_task`` call that could
    # schedule a background coroutine — placed here, at the
    # top of run(), so every background task spawned by
    # subsequent code (doc-index build, enrichment loop,
    # agent streams, post-write hooks) is covered.
    try:
        _loop = asyncio.get_running_loop()
        _loop.set_exception_handler(_asyncio_exception_handler)
        logger.info("asyncio exception handler installed")
    except RuntimeError:
        # No running loop yet — caller invoked run() outside
        # an event loop. Defensive: shouldn't happen in
        # practice (cli.py calls asyncio.run(main())), but
        # if it does the handler simply isn't installed and
        # asyncio's default error logging applies.
        logger.warning(
            "run(): no running event loop; asyncio "
            "exception handler not installed"
        )

    # Resolve repo path
    if repo_path is None:
        repo_path = Path.cwd()
    repo_path = Path(repo_path).resolve()

    # Step 1: Validate git repo
    try:
        repo = Repo(repo_path)
    except RepoError:
        page = _write_not_a_repo_page(str(repo_path))
        print(
            f"\n  AIC⚡DC — Not a git repository: {repo_path}\n"
            f"  Run: cd {repo_path} && git init\n",
            file=sys.stderr,
        )
        if not no_browser:
            webbrowser.open(f"file://{page}")
        return

    # Step 2: Find available ports. Both the WebSocket and
    # webapp ports are probed so two concurrent AIC-DC
    # instances don't silently collide on the default port.
    # Without probing the webapp port, a second instance
    # would fail to bind (OSError in the static-server
    # thread or Vite crash-loop) but still open a browser
    # pointing at the *first* instance's webapp — producing
    # the confusing "AIC-DC title bar, AIC-DC code" bug.
    try:
        server_port = find_available_port(start=server_port)
    except RuntimeError as exc:
        logger.error("Could not find server port: %s", exc)
        return
    logger.info("WebSocket server port: %d", server_port)
    try:
        webapp_port = find_available_port(start=webapp_port)
    except RuntimeError as exc:
        logger.error("Could not find webapp port: %s", exc)
        return
    logger.info("Webapp server port: %d", webapp_port)

    # Step 3: Initialize lightweight services
    config = ConfigManager(repo_root=repo_path)
    # Nothing is exported into the process environment here, and
    # that is deliberate. The old `config.apply_llm_env()` call
    # pushed llm.json's `env` block into os.environ so provider
    # SDKs would pick up the user's credentials. The `claude` CLI
    # resolves its own — a subscription login, ANTHROPIC_API_KEY,
    # or a cloud provider profile — and injecting anything here
    # would silently change which account a turn bills to. What
    # the CLI resolved is read back and reported in engine health
    # instead. See specs5/1-foundation/configuration.md § No
    # credentials, and no environment export.
    settings = Settings(config)
    # DocConvert is constructed later (after event_callback is
    # defined) so progress events can flow to the browser.

    # Step 4: Start webapp server (bundled or dev)
    bind_host = "0.0.0.0" if collab else "127.0.0.1"
    vite_process = None

    if dev or preview:
        # Vite dev/preview server
        import subprocess

        node_modules = repo_path.parent / "webapp" / "node_modules"
        # Try finding webapp relative to the package
        pkg_dir = Path(__file__).resolve().parent
        project_root = pkg_dir.parent.parent
        webapp_dir = project_root / "webapp"
        if not webapp_dir.is_dir():
            webapp_dir = repo_path / "webapp"

        if not (webapp_dir / "node_modules").is_dir():
            logger.error(
                "webapp/node_modules not found. Run: cd webapp && npm install"
            )
            return

        # Preview mode always rebuilds. Without it,
        # `vite preview` silently serves whatever stale
        # bundle was last built — users see old code and
        # the browser may fail to register as AcApp at all
        # if the backend contract changed since the last
        # build. The `prebuild` hook in webapp/package.json
        # wipes dist/ and Vite's dep cache so every
        # --preview starts from a clean slate; Vite's
        # incremental build keeps the cost small when
        # nothing changed.
        if preview:
            logger.info(
                "Running `npm run build` for --preview "
                "(clean rebuild via prebuild hook)..."
            )
            try:
                build_result = subprocess.run(
                    ["npm", "run", "build"],
                    cwd=str(webapp_dir),
                    timeout=300,
                )
            except FileNotFoundError:
                logger.error(
                    "npm not found on PATH. Install Node.js, or "
                    "build manually: cd webapp && npm run build"
                )
                return
            except subprocess.TimeoutExpired:
                logger.error(
                    "npm run build timed out after 5 minutes. "
                    "Try running it manually: "
                    "cd webapp && npm run build"
                )
                return
            if build_result.returncode != 0:
                logger.error(
                    "npm run build failed (exit %d). "
                    "See output above.",
                    build_result.returncode,
                )
                return
            if not (webapp_dir / "dist").is_dir():
                logger.error(
                    "Build completed but webapp/dist is still "
                    "missing — check webapp/vite.config.js"
                )
                return
            logger.info("Build complete.")

        cmd = ["npx"]
        if dev:
            cmd.extend(["vite", "--host", bind_host, "--port", str(webapp_port)])
        else:
            cmd.extend(["vite", "preview", "--host", bind_host, "--port", str(webapp_port)])

        # Surface Vite's stdout/stderr so build errors or port
        # binding failures are visible. Previously piped to
        # DEVNULL, which hid the "dist/ not found" message
        # and made the hang impossible to diagnose.
        try:
            vite_process = subprocess.Popen(
                cmd,
                cwd=str(webapp_dir),
                # Its own process group, so shutdown can kill the whole
                # ``npx`` → ``npm exec`` → ``sh`` → ``node`` chain.
                # Popen only knows the wrapper's pid, and signalling that
                # left the node server holding the port — see
                # ``_kill_vite``.
                start_new_session=True,
            )
            logger.info("Vite %s server started (PID %d)",
                        "dev" if dev else "preview", vite_process.pid)
        except Exception as exc:
            logger.error("Failed to start Vite: %s", exc)
            return
    else:
        # Bundled static server
        webapp_dist = _find_webapp_dist()
        if webapp_dist is None:
            logger.error(
                "No built webapp found. Either:\n"
                "  - Run: cd webapp && npm install && npm run build\n"
                "  - Use: aic-dc --dev (for development)"
            )
            return
        _warn_if_dist_is_stale(webapp_dist)
        _start_static_server(webapp_dist, webapp_port, bind_host)

    # Step 5: Create the engine adapter.
    #
    # No store is constructed here, unlike the native engine's
    # HistoryStore which this step used to build and hand to
    # LLMService. The SessionStore is derived from `config.aic_dc_dir`
    # inside ClaudeCodeService, next to `engine_config`, because it is
    # a path rather than a collaborator and a startup path that forgot
    # to pass it would lose the transcript mirror silently.

    # Event callback — will be wired after the server starts
    event_callback_ref: list[Any] = [None]

    async def event_callback(event_name: str, *args: Any) -> None:
        cb = event_callback_ref[0]
        if cb is not None:
            try:
                await cb(event_name, *args)
            except Exception:
                pass

    # DocConvert wired with the same event callback so
    # docConvertProgress events reach the browser. The
    # callback is a closure over event_callback_ref which
    # the real dispatcher replaces once the RPC server is up.
    doc_convert = DocConvert(
        config, repo=repo, event_callback=event_callback,
    )

    # Claude Code engine — the whole conversation, as of phase 3.
    # This is the only chat service now; LLMService and src/aic_dc/llm/
    # are deleted.
    #
    # The engine connects lazily — on the first turn or an explicit
    # ClaudeCodeService.connect_engine() call — deliberately. Connecting
    # here would add a `claude` subprocess (~295 MB) to every startup,
    # including the ones where the user only wanted to read a diff.
    from aic_dc.claude_code import ClaudeCodeService
    claude_code_service = ClaudeCodeService(
        config, repo=repo, event_callback=event_callback,
    )

    # The second engine, constructed cold beside the first (AG-1).
    #
    # Both adapters exist for the whole run and neither costs anything
    # until it is asked to connect: the same lazy-connect bargain above,
    # which is what makes mounting two engines free rather than doubling
    # startup. What is *not* free is a switch that discovers a broken
    # adapter after tearing down the working one, so both are validated at
    # build time by the router rather than at switch time.
    #
    # Absent rather than broken without a credential, matching how the
    # consultant mounts: no Gemini key means Antigravity is not in
    # `list_engines().mountable` and `switch_engine` refuses it with that
    # reason. It does not mean a failed startup, and it does not mean a
    # selector offering an engine that cannot answer.
    from aic_dc import capabilities

    engines: dict[str, Any] = {}
    try:
        from aic_dc.antigravity.credentials import resolve as resolve_credentials
        from aic_dc.antigravity.service import AntigravityService

        antigravity_credentials = resolve_credentials()
        if antigravity_credentials.available:
            engines[capabilities.ANTIGRAVITY] = AntigravityService(
                config,
                repo=repo,
                event_callback=event_callback,
                credentials=antigravity_credentials,
            )
            logger.info(
                "Antigravity engine mounted (credential from %s)",
                antigravity_credentials.source,
            )
        else:
            logger.info(
                "Antigravity engine not mounted: %s",
                antigravity_credentials.source or "no Gemini API key",
            )
    except ImportError as exc:
        # The SDK is an optional extra (AG-R-10). A base install is a
        # one-engine install, and saying so once at startup beats a
        # selector that offers an engine the install does not have.
        logger.info("Antigravity engine not available: %s", exc)
    except Exception:
        logger.exception(
            "The Antigravity engine did not mount; this session is "
            "Claude-only. Nothing else is affected."
        )

    # Step 6: Register services with RPC server and start
    if collab:
        from aic_dc.collab import Collab, CollabServer
        collab_instance = Collab()
        server = CollabServer(
            port=server_port,
            remote_timeout=120,
            collab=collab_instance,
        )
        server.add_service(collab_instance)
        # Wire collab to all services
        repo._collab = collab_instance
        settings._collab = collab_instance
        doc_convert._collab = collab_instance
        claude_code_service._collab = collab_instance
        # Every mounted engine, not only the one that starts as master.
        # The localhost gate reads `_collab` to decide whether a caller is
        # the host, so an adapter that missed this wiring would fail open
        # or fail closed depending on its default — and would do it only
        # after a switch, which is the worst time to find out.
        for _adapter in engines.values():
            _adapter._collab = collab_instance
    else:
        server = RpcServer(
            port=server_port,
            host=bind_host,
            remote_timeout=120,
        )

    # The engine router (AG-3). Both engines mount under one RPC
    # namespace, so the browser's 43 call sites across 59 files do not
    # fork; what tells the webapp which surfaces have data is the
    # capability descriptor the router publishes, never the engine's
    # name (AG-R-4).
    #
    # Every mountable adapter goes in; which one is master is
    # `app.json`'s `engines.master`, and it can change afterwards through
    # `switch_engine` (AG-1). The set of method names on the wire does
    # not depend on that choice, which is what lets a switch happen
    # without re-registering the service or reconnecting the browser.
    #
    # A master that is not mountable falls back to Claude rather than
    # failing to start: the user asked for an engine this install cannot
    # provide — a missing key or a missing extra — and the recoverable
    # answer is the shipped engine plus a warning.
    #
    # `name=RPC_NAME` rather than the default class-name namespace,
    # even though the generated class is *called* ClaudeCodeService: the
    # namespace is what 59 webapp files assume, and it should be stated
    # here rather than inherited from a name that could be refactored.
    from aic_dc.engine_router import RPC_NAME, build_router

    adapters = {capabilities.CLAUDE: claude_code_service, **engines}
    master_engine = config.master_engine
    if master_engine not in adapters:
        logger.warning(
            "app.json asks for the %s engine, which is not mountable here. "
            "Starting on %s; switch_engine() will say why.",
            master_engine,
            capabilities.CLAUDE,
        )
        master_engine = capabilities.CLAUDE
    engine_router = build_router(
        adapters[master_engine],
        engine=master_engine,
        alternates={
            name: adapter
            for name, adapter in adapters.items()
            if name != master_engine
        },
        event_callback=event_callback,
    )

    server.add_service(repo)
    server.add_service(settings)
    server.add_service(doc_convert)
    server.add_service(engine_router, name=RPC_NAME)

    # Wire the post-write callback — every successful file
    # write/create/rename on Repo triggers the doc builder's
    # note_file_written, which decides (on extension) whether to
    # invalidate the doc-index cache entry, re-extract the outline
    # and schedule keyword enrichment.
    #
    # This is one of the two write paths, and the only one that comes
    # through Repo: the user's edits, from the viewer and the SVG
    # editor. The agent's writes bypass this layer entirely — the
    # CLI's Write and Edit go to disk directly — and are picked up by
    # the PostToolUse re-index instead, which calls the same
    # note_file_written from claude_code/hooks.py. Two callers, one
    # decision about which extensions matter.
    repo._post_write_callback = claude_code_service.doc_builder.note_file_written

    await server.start()
    logger.info("WebSocket server started on ws://%s:%d", bind_host, server_port)

    # Wire the event callback now that the server is up.
    # It dispatches to AcApp.{event_name}(...) on all connected
    # browsers. jrpc-oo injects get_call() onto instances
    # registered via add_class, so the proxy lands on the **router**,
    # which is the object that was registered — not on the service
    # behind it. Reading it off the service would find nothing and
    # every server-push event would be dropped with a warning.
    def _make_real_callback() -> Any:
        async def _cb(event_name: str, *args: Any) -> None:
            # Try both get_call() (method form) and .call
            # (attribute form) — jrpc-oo's injection shape
            # varies by version.
            call = None
            try:
                call = engine_router.get_call()
            except AttributeError:
                call = getattr(engine_router, "call", None)
            if call is None:
                logger.warning(
                    "Event callback: no call proxy available for %s",
                    event_name,
                )
                return
            method_key = f"AcApp.{event_name}"
            try:
                method = call[method_key]
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "Event callback: no remote method %s (%s)",
                    method_key, exc,
                )
                return
            try:
                result = method(*args)
                # jrpc-oo methods may return coroutines or
                # plain values; await when awaitable.
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning(
                    "Event callback %s raised: %s",
                    event_name, exc,
                )
        return _cb

    event_callback_ref[0] = _make_real_callback()
    logger.info(
        "Event callback wired (service=%s)",
        type(engine_router).__name__,
    )
    # Log what jrpc-oo has injected so we can diagnose which
    # form of the call proxy is available.
    logger.info(
        "engine_router attributes: get_call=%s call=%s",
        hasattr(engine_router, "get_call"),
        hasattr(engine_router, "call"),
    )

    # Step 7: Open browser
    url = f"http://localhost:{webapp_port}/?port={server_port}"
    if not no_browser:
        webbrowser.open(url)
        logger.info("Browser opened: %s", url)
    else:
        logger.info("Webapp URL: %s", url)

    # Launch Phase 2 as a background task
    asyncio.ensure_future(
        _heavy_init(
            claude_code_service, repo, config, event_callback,
            other_engines=engines,
        )
    )

    # Keep the server running. On Ctrl-C / SIGTERM we exit
    # via ``os._exit`` to bypass asyncio's runner cleanup —
    # otherwise the runner re-enters the loop to cancel
    # tasks and hangs on ``_heavy_init``'s sentence-transformer
    # load running in the default executor. Vite goes with us,
    # by process group — see ``_kill_vite`` for why
    # ``terminate()`` was not enough.
    #
    # ``os._exit`` also skips ``atexit``, and the Claude Agent
    # SDK's only orphan guard is an ``atexit`` hook — so the
    # CLI child has to be killed here, explicitly. Measured
    # without this: SIGINT during a streaming turn left the
    # ``claude`` process running for a further ~38 seconds,
    # reparented to init, still holding the repo as its cwd.
    #
    # The same gap costs us the SDK's other teardown: a resumed
    # session's temp ``CLAUDE_CONFIG_DIR``, cleaned in
    # ``disconnect()``, which this path never reaches. Purged
    # after the kill, never before — see ``_purge_resume_dirs``.
    #
    # Ahead of all of that, ``_shut_the_engine_down`` gets a bounded
    # turn on the loop — see ``_install_exit_handlers``, which owns the
    # reason the handler goes on the loop rather than on the signal.
    # We still ``os._exit`` at the end; what the loop gets is one
    # bounded task, not asyncio's runner cleanup.
    #
    # This closure is what ``vite_process`` is captured for, which is why
    # the wiring around it lives in a module-level function and this does
    # not: everything below needs ``main``'s locals, and nothing above
    # does.
    def _tear_down_and_exit(*_args: Any) -> None:
        _kill_vite(vite_process)
        _kill_cli_children()
        _purge_resume_dirs()
        os._exit(0)

    _install_exit_handlers(
        asyncio.get_running_loop(),
        claude_code_service,
        _tear_down_and_exit,
        tuple(engines.values()),
    )

    await asyncio.Event().wait()
