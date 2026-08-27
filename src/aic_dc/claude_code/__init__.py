"""Claude Code engine — the session, its options, and the message pump.

AIC⚡DC drives one Claude Code session per repository through the Claude
Agent SDK. Claude Code owns the agent loop, tool execution, permissions,
compaction, and session persistence; this package's job is to start a
session, feed it user intent, and translate what comes back into events
the browser can render.

The engine is deliberately **not** a provider behind an abstraction layer.
There is no prompt assembly, no context manager, no token budget, and no
cache tiering here (``specs5/plan/decisions.md`` CC-1).

Governing spec: ``specs5/3-engine/session.md``.
Reference: ``specs-reference/3-engine/session.md`` — exact option values
and event payload shapes, verified against ``claude-agent-sdk`` 0.2.137.

Public surface:

- :class:`EngineConfig` — parsed ``engine.json``
- :class:`EngineHealth`, :func:`resolve_cli`, :class:`EngineStartupError` —
  which ``claude`` binary, which version, which credentials
- :func:`build_options` — the ``ClaudeAgentOptions`` assembly
- :class:`Event`, :class:`TurnTranslator` — SDK messages → AIC⚡DC events
- :class:`CostLedger` — the engine's cumulative cost and per-model usage,
  differenced into what *this* turn cost
- :class:`EngineSession`, :class:`Turn`, :class:`ViewerFraming` — the
  session lifecycle and one user turn's inputs
- :class:`ClaudeCodeService` — the browser-facing RPC surface. The class
  name is the RPC namespace; renaming it breaks every frontend call site.

Import design note: nothing here imports ``claude_agent_sdk`` at module
scope. The SDK spawns a ~295 MB CLI subprocess and its import cost is
real, so every reference is inside the function that needs it. That also
keeps the modules importable — and unit-testable — on a machine where the
CLI is not installed.
"""

from __future__ import annotations

from aic_dc.claude_code.cost import MEASURED, RESET, UNPRICED, CostLedger
from aic_dc.claude_code.engine_config import (
    EFFORT_LEVELS,
    PERMISSION_MODES,
    THINKING_DISPLAYS,
    EngineConfig,
)
from aic_dc.claude_code.health import (
    EngineHealth,
    EngineStartupError,
    detect_credentials,
    resolve_cli,
    sdk_cli_pin,
    sdk_version,
)
from aic_dc.claude_code.messages import Event, TurnTranslator
from aic_dc.claude_code.options import build_option_kwargs, build_options
from aic_dc.claude_code.service import ClaudeCodeService
from aic_dc.claude_code.session import (
    EngineNotReadyError,
    EngineSession,
    SessionLostError,
    Turn,
    TurnInProgressError,
    ViewerFraming,
)

__all__ = [
    "EFFORT_LEVELS",
    "MEASURED",
    "PERMISSION_MODES",
    "RESET",
    "THINKING_DISPLAYS",
    "UNPRICED",
    "ClaudeCodeService",
    "CostLedger",
    "EngineConfig",
    "EngineHealth",
    "EngineNotReadyError",
    "EngineSession",
    "EngineStartupError",
    "Event",
    "SessionLostError",
    "Turn",
    "TurnInProgressError",
    "TurnTranslator",
    "ViewerFraming",
    "build_option_kwargs",
    "build_options",
    "detect_credentials",
    "resolve_cli",
    "sdk_cli_pin",
    "sdk_version",
]
