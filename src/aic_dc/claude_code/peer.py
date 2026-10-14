"""Peer conversations: two AIC⚡DC processes letting their agents talk.

The agent in one repository (the *caller*) asks the agent in another (the
*responder*) about the responder's repository, through three in-process
tools. The responder's user accepts on a dialog, after which every message
is one turn of a fresh, read-only session that the user is locked out of
until either side ends it.

Three properties carry the design, and each is a spec invariant rather than
a preference (``specs5/4-features/peer-conversation.md``):

- **The caller always asks.** The responder never opens a connection back.
  Its questions ride its replies, so no turn is ever re-entered and no two
  processes wait on each other.
- **Ending is always explicit.** No timeout, socket drop or missing
  heartbeat ends a conversation (CC-23). Both users can see it and end it,
  and that is the release a timeout would otherwise be.
- **Nothing reaches the responder's engine before its user accepts.**

This module holds the transport and the state machine for both roles. The
engine work — starting the session, running a turn, restoring the posture —
belongs to the service, reached through the :class:`PeerHost` protocol.

Governing decision: ``specs5/plan/decisions.md`` § CC-23. Wire shapes and
fixed strings: ``specs-reference/4-features/peer-conversation.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from aic_dc.claude_code.messages import Event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The in-process MCP server the caller's tools are filed under. Not
#: ``aic-dc``: that server's invariant is that every tool is read-only, and
#: these send this repository's content to another process.
SERVER_NAME = "aic-dc-peer"

#: Under the config directory. One file per live process.
PEERS_DIR = "peers"

RECORD_VERSION = 1

HOST = "127.0.0.1"

MESSAGE_PATH = "/peer/message"
END_PATH = "/peer/end"

#: The first line of every peer message as stored in the transcript. The
#: chat view's *from <peer>* label matches this line and nothing else,
#: because a label derived from live state would be gone after a reload.
HEADER = "[aic-dc peer message from {name} ({root})]"
HEADER_RE = re.compile(r"^\[aic-dc peer message from (?P<name>.+?) \((?P<root>.+)\)\]$")

FIRST_TURN_FRAMING = (
    "You are answering another AI agent, which is working in the repository "
    "named above, about this repository. This repository is read-only for the "
    "whole conversation: no edits, no commands. Answer from what you read here "
    "and cite paths and symbols. Close your reply with a section headed "
    '"Questions for you:" listing anything you want the other agent to answer '
    'about its own repository on this topic, or "Questions for you: none".'
)

#: What ``can_use_tool`` says to the responder's agent for every request it
#: receives while a conversation is open.
DENY_REASON = (
    "This session is answering a peer conversation and the repository is "
    "read-only for its duration. Answer from what you can read. If you need "
    "something from the user, put the question to the peer instead."
)

FOOTER_OPEN = (
    "[Peer conversation with {peer} is open. Reply with ask_peer, or end it "
    "with end_peer_conversation.]"
)
FOOTER_ENDED = "[Peer conversation with {peer} was ended by {who}. Do not retry; tell the user.]"

#: How long the best-effort end request may take. Not a conversation
#: timeout — the conversation is already over on this side by then — but
#: the one request whose answer nobody waits for must not hold a shutdown.
END_REQUEST_SECONDS = 10.0

#: The refusals a responder gives without asking its user, said to the
#: caller's agent in words it can relay rather than retry.
REFUSALS = {
    "busy": (
        "{peer} is busy — a turn is running there, or its user is in a review "
        "or a commit. Nothing was sent. Tell the user; try again only if they ask."
    ),
    "conversation_open": (
        "{peer} is already in a peer conversation. Nothing was sent. Tell the user."
    ),
    "unavailable": (
        "{peer} is not running the Claude engine, so it cannot answer peer "
        "conversations. Nothing was sent. Tell the user."
    ),
    "wrong_conversation": (
        "{peer} has a different peer conversation open. Nothing was sent."
    ),
}


# ---------------------------------------------------------------------------
# Peer records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerRecord:
    """One live instance, as its record file describes it."""

    pid: int
    repo_root: str
    repo_name: str
    port: int
    token: str
    started_at: str
    version: int = RECORD_VERSION

    def public(self) -> dict[str, str]:
        """What the browser and the agent may see. Never the token."""
        return {"repo_name": self.repo_name, "repo_root": self.repo_root}


def peers_dir(config_dir: Path | str) -> Path:
    return Path(config_dir) / PEERS_DIR


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive and somebody else's. Not a peer of ours either way, but not
        # ours to delete.
        return True
    except OSError:
        return False
    return True


def write_record(config_dir: Path | str, record: PeerRecord) -> Path:
    """Write this process's record, ``0600``, atomically."""
    directory = peers_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{record.pid}.json"
    tmp = directory / f".{record.pid}.json.tmp"
    # Created 0600 rather than chmodded after: the token is the whole of
    # the authentication, and a window where it is world-readable is a
    # window.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle)
    os.replace(tmp, path)
    return path


def read_records(config_dir: Path | str, *, sweep: bool = False) -> list[PeerRecord]:
    """Every live record. Stale ones are skipped, and removed when ``sweep``."""
    directory = peers_dir(config_dir)
    if not directory.is_dir():
        return []
    records: list[PeerRecord] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = PeerRecord(
                pid=int(data["pid"]),
                repo_root=str(data["repo_root"]),
                repo_name=str(data["repo_name"]),
                port=int(data["port"]),
                token=str(data["token"]),
                started_at=str(data.get("started_at", "")),
                version=int(data.get("version", RECORD_VERSION)),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.debug("Ignoring unreadable peer record %s: %s", path, exc)
            continue
        if not _pid_alive(record.pid):
            if sweep:
                with contextlib.suppress(OSError):
                    path.unlink()
            continue
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------


class _EmbeddedServer(uvicorn.Server):
    """Uvicorn with its signal capture removed, for the reason
    ``consult_listener._EmbeddedServer`` gives: embedded in this loop it
    would replace the application's SIGINT and SIGTERM handlers."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


Handler = Callable[[dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]


class PeerListener:
    """Loopback HTTP, two routes, one bearer.

    Plain JSON rather than MCP: the client is our own code, not a model's MCP
    client, so there is no protocol to match. Requests block with no
    deadline — a message returns when the responder's turn completes,
    including however long its user spends on the consent dialog.
    """

    def __init__(self, *, token: str, on_message: Handler, on_end: Handler, host: str = HOST) -> None:
        self._token = token
        self._on_message = on_message
        self._on_end = on_end
        self._host = host
        self._server: Any = None
        self._serving: asyncio.Task[Any] | None = None
        self._sock: socket.socket | None = None
        self._port: int | None = None

    @property
    def port(self) -> int | None:
        return self._port

    def _authorised(self, request: Request) -> bool:
        presented = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return bool(presented) and hmac.compare_digest(presented, self._token)

    def _route(self, handler: Handler) -> Callable[[Request], Awaitable[Any]]:
        async def endpoint(request: Request) -> Any:
            if not self._authorised(request):
                return PlainTextResponse("Unauthorized.", status_code=401)
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"reason": "bad_request"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"reason": "bad_request"}, status_code=400)
            status, payload = await handler(body)
            return JSONResponse(payload, status_code=status)

        return endpoint

    async def start(self) -> int:
        """Bind, serve, and return the port. Idempotent.

        The socket is bound before anything is scheduled, so the port exists
        before the record naming it is written.
        """
        if self._port is not None:
            return self._port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, 0))
        self._sock = sock
        self._port = sock.getsockname()[1]
        app = Starlette(
            routes=[
                Route(MESSAGE_PATH, self._route(self._on_message), methods=["POST"]),
                Route(END_PATH, self._route(self._on_end), methods=["POST"]),
            ]
        )
        config = uvicorn.Config(app, log_level="error", access_log=False)
        self._server = _EmbeddedServer(config)
        self._serving = asyncio.create_task(
            self._server.serve(sockets=[sock]), name="aic-dc-peer-listener"
        )
        while not self._server.started:
            if self._serving.done():
                await self._serving
                raise RuntimeError("The peer listener stopped during startup")
            await asyncio.sleep(0.01)
        logger.info("Peer listener serving on %s:%d", self._host, self._port)
        return self._port

    async def aclose(self) -> None:
        """Stop serving. Never raises."""
        if self._server is not None:
            self._server.should_exit = True
        if self._serving is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._serving, timeout=5)
            if not self._serving.done():
                self._serving.cancel()
        self._server = None
        self._serving = None
        self._port = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class PeerUnreachable(RuntimeError):
    """The responder's process did not answer at all."""


class PeerClient:
    """The caller's half of the wire. ``timeout=None`` on messages, by decision."""

    async def message(
        self,
        record: PeerRecord,
        *,
        conversation_id: str | None,
        caller: dict[str, Any],
        message: str,
    ) -> tuple[int, dict[str, Any]]:
        body = {"conversation_id": conversation_id, "caller": caller, "message": message}
        return await self._post(record, MESSAGE_PATH, body, timeout=None)

    async def end(
        self, record: PeerRecord, *, conversation_id: str | None, caller_pid: int
    ) -> None:
        body = {"conversation_id": conversation_id, "caller_pid": caller_pid}
        with contextlib.suppress(PeerUnreachable):
            await self._post(record, END_PATH, body, timeout=END_REQUEST_SECONDS)

    async def _post(
        self, record: PeerRecord, path: str, body: dict[str, Any], *, timeout: float | None
    ) -> tuple[int, dict[str, Any]]:
        url = f"http://{HOST}:{record.port}{path}"
        headers = {"Authorization": f"Bearer {record.token}"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise PeerUnreachable(str(exc) or type(exc).__name__) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"reason": response.text}
        return response.status_code, payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# The host protocol
# ---------------------------------------------------------------------------


class PeerHost(Protocol):
    """What the responder needs from the engine service."""

    def _peer_available(self) -> bool:
        """Whether this process can answer at all (Claude is the master)."""

    def _peer_busy(self) -> bool:
        """Whether a turn, a review or a commit is in progress."""

    async def _peer_begin(self) -> dict[str, Any]:
        """Start the fresh read-only session; return what to restore."""

    async def _peer_turn(self, request_id: str, text: str) -> dict[str, Any]:
        """Run one turn; return its ``streamComplete`` payload."""

    async def _peer_cancel_turn(self, request_id: str) -> None:
        """Interrupt the turn in flight."""

    async def _peer_finish(self, previous: dict[str, Any]) -> None:
        """Restore the posture recorded by :meth:`_peer_begin`."""


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------


@dataclass
class _Responding:
    conversation_id: str
    caller: dict[str, Any]
    first_message: str
    consent: asyncio.Future[bool]
    status: str = "pending"  # pending → open
    previous: dict[str, Any] | None = None
    turn_request_id: str | None = None
    ended_by: str | None = None

    @property
    def caller_pid(self) -> int | None:
        try:
            return int(self.caller.get("pid"))
        except (TypeError, ValueError):
            return None


@dataclass
class _Calling:
    peer: PeerRecord
    conversation_id: str | None = None
    status: str = "opening"  # opening → open ↔ waiting
    in_flight: bool = False
    ended_by: str | None = None


@dataclass
class _Ended:
    role: str
    peer: dict[str, str]
    ended_by: str
    previous_session_id: str | None = None
    at: float = field(default_factory=time.time)


Broadcast = Callable[[Event], Awaitable[None]]


class PeerConversations:
    """Both roles of one process. At most one conversation, in one role."""

    def __init__(
        self,
        *,
        host: PeerHost,
        config_dir: Path | str,
        repo_root: Path | str,
        broadcast: Broadcast,
        client: PeerClient | None = None,
        pid: int | None = None,
    ) -> None:
        self._host = host
        self._config_dir = Path(config_dir)
        self._repo_root = Path(repo_root)
        self._broadcast = broadcast
        self._client = client or PeerClient()
        self._pid = pid if pid is not None else os.getpid()
        self._token = secrets.token_urlsafe(32)
        self._listener: PeerListener | None = None
        self._record_path: Path | None = None
        self._responding: _Responding | None = None
        self._calling: _Calling | None = None
        self._ended: _Ended | None = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def me(self) -> dict[str, Any]:
        return {
            "repo_name": self._repo_root.name,
            "repo_root": str(self._repo_root),
            "pid": self._pid,
        }

    @property
    def locked(self) -> bool:
        """Whether the responder's lock is on: a conversation is open here."""
        return self._responding is not None and self._responding.status == "open"

    @property
    def calling_in_flight(self) -> bool:
        return self._calling is not None and self._calling.in_flight

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Serve and advertise. Sweeps records left by dead processes."""
        read_records(self._config_dir, sweep=True)
        self._listener = PeerListener(
            token=self._token, on_message=self.handle_message, on_end=self.handle_end
        )
        port = await self._listener.start()
        record = PeerRecord(
            pid=self._pid,
            repo_root=str(self._repo_root),
            repo_name=self._repo_root.name,
            port=port,
            token=self._token,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._record_path = write_record(self._config_dir, record)

    async def aclose(self) -> None:
        """End whatever is open, withdraw the record, stop serving. Never raises."""
        with contextlib.suppress(Exception):
            await self.end_calling("caller")
        with contextlib.suppress(Exception):
            await self.end_responding("responder")
        if self._record_path is not None:
            with contextlib.suppress(OSError):
                self._record_path.unlink()
            self._record_path = None
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def state(self) -> dict[str, Any] | None:
        """The browser's view: one record, whichever role is live."""
        r = self._responding
        if r is not None:
            return {
                "role": "responder",
                "status": r.status,
                "peer": {
                    "repo_name": str(r.caller.get("repo_name", "")),
                    "repo_root": str(r.caller.get("repo_root", "")),
                },
                "conversation_id": r.conversation_id,
                "first_message": r.first_message if r.status == "pending" else None,
                "previous_session_id": (r.previous or {}).get("session_id"),
            }
        c = self._calling
        if c is not None:
            return {
                "role": "caller",
                "status": c.status,
                "peer": c.peer.public(),
                "conversation_id": c.conversation_id,
            }
        e = self._ended
        if e is not None:
            return {
                "role": e.role,
                "status": "ended",
                "peer": e.peer,
                "ended_by": e.ended_by,
                "previous_session_id": e.previous_session_id,
            }
        return None

    async def _changed(self) -> None:
        await self._broadcast(Event("peerChanged", {"state": self.state()}, turn_scoped=False))

    # ------------------------------------------------------------------
    # Responder: the wire
    # ------------------------------------------------------------------

    async def handle_message(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        message = str(body.get("message") or "").strip()
        caller = body.get("caller") if isinstance(body.get("caller"), dict) else {}
        cid = body.get("conversation_id")
        if not message or not caller.get("repo_root"):
            return 400, {"reason": "bad_request"}

        if cid is None:
            if self._responding is not None or self._calling is not None:
                return 409, {"reason": "conversation_open"}
            if not self._host._peer_available():
                return 409, {"reason": "unavailable"}
            if self._host._peer_busy():
                return 409, {"reason": "busy"}
            r = _Responding(
                conversation_id=uuid.uuid4().hex,
                caller=dict(caller),
                first_message=message,
                consent=asyncio.get_running_loop().create_future(),
            )
            self._responding = r
            self._ended = None
            await self._changed()
            logger.info("Peer request from %s is waiting for consent", caller.get("repo_root"))
            allowed = await r.consent
            if r.ended_by is not None:
                return 410, {"reason": "ended", "by": r.ended_by}
            if not allowed:
                await self._close_responding(r, "responder")
                return 403, {"reason": "denied"}
            text = self._framed(caller, message, first=True)
        else:
            r = self._responding
            if r is None:
                return 410, {"reason": "ended", "by": "responder"}
            if r.conversation_id != cid:
                return 409, {"reason": "wrong_conversation"}
            if r.status != "open" or r.turn_request_id is not None:
                return 409, {"reason": "busy"}
            text = self._framed(caller, message, first=False)
        return await self._run(r, text)

    async def handle_end(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        r = self._responding
        if r is not None:
            cid = body.get("conversation_id")
            try:
                pid = int(body.get("caller_pid"))
            except (TypeError, ValueError):
                pid = None
            ours = cid == r.conversation_id or (
                cid is None and r.status == "pending" and pid == r.caller_pid
            )
            if ours:
                await self.end_responding("caller")
        return 200, {"status": "ended"}

    def _framed(self, caller: dict[str, Any], message: str, *, first: bool) -> str:
        header = HEADER.format(name=caller.get("repo_name", ""), root=caller.get("repo_root", ""))
        parts = [header]
        if first:
            parts.append(FIRST_TURN_FRAMING)
        parts.append(message)
        return "\n\n".join(parts)

    async def _run(self, r: _Responding, text: str) -> tuple[int, dict[str, Any]]:
        request_id = f"peer-{uuid.uuid4().hex[:12]}"
        r.turn_request_id = request_id
        try:
            outcome = await self._host._peer_turn(request_id, text)
        except Exception as exc:
            logger.exception("Peer turn %s failed", request_id)
            outcome = {"is_error": True, "error": str(exc)}
        finally:
            r.turn_request_id = None

        if r.ended_by is not None:
            # Ended while the turn ran. `end_responding` left the finish to
            # here so the posture is not restored under a turn still winding
            # down.
            await self._close_responding(r, r.ended_by)
            return 410, {"reason": "ended", "by": r.ended_by}
        if outcome.get("cancelled"):
            # The responder's user pressed Stop, which ends the conversation.
            await self._close_responding(r, "responder")
            return 410, {"reason": "ended", "by": "responder"}
        if outcome.get("is_error") or outcome.get("error"):
            detail = (
                outcome.get("error")
                or outcome.get("terminal_reason")
                or outcome.get("subtype")
                or "the turn ended in error"
            )
            return 502, {"reason": "turn_error", "detail": str(detail)}
        return 200, {
            "conversation_id": r.conversation_id,
            "reply": str(outcome.get("response") or ""),
        }

    # ------------------------------------------------------------------
    # Responder: the user
    # ------------------------------------------------------------------

    async def answer_request(self, allow: bool) -> dict[str, Any]:
        """The consent dialog's Allow or Deny."""
        r = self._responding
        if r is None or r.status != "pending":
            return {"error": "No peer request is waiting", "reason": "no_request"}
        if not allow:
            if not r.consent.done():
                r.consent.set_result(False)
            return {"status": "denied"}
        if self._host._peer_busy():
            return {
                "error": "Finish or stop the current turn before allowing the conversation",
                "reason": "turn_in_progress",
            }
        # Begun here rather than by the waiting request, so the session is
        # fresh and locked by the time Allow returns: a message typed in
        # between would otherwise land in the old session.
        try:
            r.previous = await self._host._peer_begin()
        except Exception as exc:
            logger.exception("Could not start the peer session")
            return {"error": f"Could not start the peer session: {exc}"}
        r.status = "open"
        if not r.consent.done():
            r.consent.set_result(True)
        await self._changed()
        return {"status": "allowed", "conversation_id": r.conversation_id}

    async def end_responding(self, by: str) -> None:
        """End the responder's conversation, from either side.

        With a turn in flight this only asks the turn to stop. The waiting
        request finishes the close once the turn has actually ended, so the
        read-only posture is never lifted under a turn still running.
        """
        r = self._responding
        if r is None or r.ended_by is not None:
            return
        r.ended_by = by
        if r.status == "pending":
            if not r.consent.done():
                r.consent.set_result(False)
            await self._close_responding(r, by)
            return
        if r.turn_request_id is not None:
            try:
                await self._host._peer_cancel_turn(r.turn_request_id)
            except Exception:
                logger.exception("Could not stop the peer turn")
            return
        await self._close_responding(r, by)

    async def _close_responding(self, r: _Responding, by: str) -> None:
        if self._responding is not r:
            return
        self._responding = None
        if r.previous is not None:
            try:
                await self._host._peer_finish(r.previous)
            except Exception:
                logger.exception("Could not restore the posture after a peer conversation")
        self._ended = _Ended(
            role="responder",
            peer={
                "repo_name": str(r.caller.get("repo_name", "")),
                "repo_root": str(r.caller.get("repo_root", "")),
            },
            ended_by=by,
            previous_session_id=(r.previous or {}).get("session_id"),
        )
        logger.info("Peer conversation with %s ended by %s", r.caller.get("repo_root"), by)
        await self._changed()

    # ------------------------------------------------------------------
    # Caller
    # ------------------------------------------------------------------

    def peers(self) -> list[PeerRecord]:
        """Live peers, never this process or another one on the same root."""
        mine = str(self._repo_root.resolve())
        out = []
        for record in read_records(self._config_dir):
            if record.pid == self._pid:
                continue
            with contextlib.suppress(OSError):
                if str(Path(record.repo_root).resolve()) == mine:
                    continue
            out.append(record)
        return out

    def _resolve(self, name: str) -> tuple[PeerRecord | None, str | None]:
        name = (name or "").strip()
        live = self.peers()
        if not live:
            return None, "No other AIC⚡DC instance is running on this machine."
        by_root = [r for r in live if r.repo_root == name]
        if by_root:
            return by_root[0], None
        by_name = [r for r in live if r.repo_name == name]
        if len(by_name) == 1:
            return by_name[0], None
        if len(by_name) > 1:
            roots = ", ".join(r.repo_root for r in by_name)
            return None, f"More than one peer is named {name!r}; name one by its root: {roots}."
        known = ", ".join(f"{r.repo_name} ({r.repo_root})" for r in live)
        return None, f"No peer named {name!r}. Live peers: {known}."

    async def ask(self, peer: str, message: str) -> str:
        """``ask_peer``: open or continue, and return text for the model."""
        message = (message or "").strip()
        if not message:
            return "ask_peer needs a message to send."
        if self._responding is not None:
            return (
                "This repository is itself answering a peer conversation, so it "
                "cannot start one. Nothing was sent."
            )
        record, problem = self._resolve(peer)
        if record is None:
            return problem or "No such peer."
        c = self._calling
        if c is not None and c.peer.repo_root != record.repo_root:
            return (
                f"A peer conversation with {c.peer.repo_name} is open. End it with "
                "end_peer_conversation before talking to another peer."
            )
        if c is not None and c.in_flight:
            return f"A message to {c.peer.repo_name} is already waiting for its reply."
        if c is None:
            c = _Calling(peer=record)
            self._calling = c
            self._ended = None
        c.in_flight = True
        c.status = "opening" if c.conversation_id is None else "waiting"
        await self._changed()

        name = record.repo_name
        try:
            status, body = await self._client.message(
                record, conversation_id=c.conversation_id, caller=self.me, message=message
            )
        except PeerUnreachable as exc:
            c.in_flight = False
            await self._close_calling(c, "the peer's process exiting")
            return (
                f"{name} could not be reached ({exc}); its AIC⚡DC has probably exited. "
                + FOOTER_ENDED.format(peer=name, who="the peer's process exiting")
            )
        except asyncio.CancelledError:
            # Stopping the turn while this waits ends the conversation (spec
            # § The caller's tools): the responder would otherwise stay
            # locked behind a caller that has stopped listening.
            c.in_flight = False
            await self.end_calling("caller")
            raise
        c.in_flight = False

        if c.ended_by is not None or self._calling is not c:
            return FOOTER_ENDED.format(peer=name, who=self._who(c.ended_by or "caller", name))
        if status == 200:
            c.conversation_id = str(body.get("conversation_id") or c.conversation_id)
            c.status = "open"
            await self._changed()
            reply = str(body.get("reply") or "").strip() or "(The peer's reply was empty.)"
            return f"{reply}\n\n{FOOTER_OPEN.format(peer=name)}"
        if status == 410:
            who = self._who(str(body.get("by") or "responder"), name)
            await self._close_calling(c, who)
            return FOOTER_ENDED.format(peer=name, who=who)
        if status == 403:
            await self._close_calling(c, f"{name}'s user (declined)")
            return (
                f"{name}'s user declined the conversation. Do not retry; tell the user."
            )
        if status == 409:
            reason = str(body.get("reason") or "busy")
            text = REFUSALS.get(reason, REFUSALS["busy"]).format(peer=name)
            if c.conversation_id is None:
                self._calling = None
                await self._changed()
            else:
                c.status = "open"
                await self._changed()
                text += "\n\n" + FOOTER_OPEN.format(peer=name)
            return text
        if status == 502 and c.conversation_id is not None:
            c.status = "open"
            await self._changed()
            return (
                f"{name}'s turn ended in error ({body.get('detail', 'unknown')}). "
                "You may send the message again or end the conversation.\n\n"
                + FOOTER_OPEN.format(peer=name)
            )
        # Anything else — a 401 from a stale token, a 502 on the first
        # message — is a fault, not a conversation.
        await self._close_calling(c, "an error")
        return (
            f"{name} answered with HTTP {status} ({body.get('reason', 'no reason')}). "
            "The conversation is closed. Tell the user."
        )

    async def end_calling(self, by: str) -> str:
        """``end_peer_conversation``, the caller's End, or its Stop."""
        c = self._calling
        if c is None:
            return "No peer conversation is open."
        name = c.peer.repo_name
        c.ended_by = by
        await self._close_calling(c, self._who(by, name))
        await self._client.end(c.peer, conversation_id=c.conversation_id, caller_pid=self._pid)
        return f"The peer conversation with {name} is ended."

    async def _close_calling(self, c: _Calling, who: str) -> None:
        if self._calling is not c:
            return
        self._calling = None
        self._ended = _Ended(role="caller", peer=c.peer.public(), ended_by=who)
        await self._changed()

    @staticmethod
    def _who(by: str, peer_name: str) -> str:
        if by == "caller":
            return "this side"
        if by == "responder":
            return f"{peer_name}'s user"
        return by

    def dismiss(self) -> None:
        """Forget the ended record once the browser has shown it."""
        self._ended = None

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def build_server(self) -> Any:
        """The caller's three tools as an in-process SDK MCP server."""
        from claude_agent_sdk import create_sdk_mcp_server, tool

        def text(body: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": body}]}

        @tool(
            "list_peers",
            "List the other AIC⚡DC instances running on this machine, each in its "
            "own repository, whose agents can be asked questions with ask_peer.",
            {"type": "object", "properties": {}},
        )
        async def list_peers(_args: dict[str, Any]) -> dict[str, Any]:
            live = self.peers()
            if not live:
                return text("No other AIC⚡DC instance is running on this machine.")
            lines = [f"- {r.repo_name} ({r.repo_root})" for r in live]
            return text("Live peers:\n" + "\n".join(lines))

        @tool(
            "ask_peer",
            "Send a message to the AI agent working in another repository on this "
            "machine and wait for its reply. The first message opens a conversation, "
            "which the other repository's user must accept; later messages continue "
            "it. The peer answers from its own repository and may end its reply with "
            "questions for you — answer them in your next message. Use for questions "
            "about the other repository that its own agent can answer better than "
            "you can by reading it. End the conversation with end_peer_conversation "
            "when you are done.",
            {
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": "The peer's repository name or root, from list_peers.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Your question, plus answers to any questions the peer asked.",
                    },
                },
                "required": ["peer", "message"],
            },
        )
        async def ask_peer(args: dict[str, Any]) -> dict[str, Any]:
            return text(await self.ask(str(args.get("peer") or ""), str(args.get("message") or "")))

        @tool(
            "end_peer_conversation",
            "End the open peer conversation, releasing the other repository's session.",
            {"type": "object", "properties": {}},
        )
        async def end_peer_conversation(_args: dict[str, Any]) -> dict[str, Any]:
            return text(await self.end_calling("caller"))

        return create_sdk_mcp_server(
            name=SERVER_NAME, tools=[list_peers, ask_peer, end_peer_conversation]
        )


def header_of(content: str) -> dict[str, str] | None:
    """The peer a stored user message came from, or ``None``."""
    first = (content or "").split("\n", 1)[0].strip()
    match = HEADER_RE.match(first)
    if match is None:
        return None
    return {"repo_name": match.group("name"), "repo_root": match.group("root")}
