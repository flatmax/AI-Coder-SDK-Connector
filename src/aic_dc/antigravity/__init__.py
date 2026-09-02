"""Antigravity engine — Google's SDK behind the same engine seam.

AIC⚡DC's second agent backend. ``google.antigravity.Agent`` driving its
bundled ``localharness`` binary, **not** the ``agy`` CLI — that product is
a separate program with separate authentication, and its stream carries no
file content, so no diff can be rendered from it
(``specs5/plan-ag/decisions.md`` AG-2).

Exactly one engine is master per session; the other is reachable as a
one-shot consultant (AG-1). Both mount under the *same* RPC namespace
(AG-3), so the browser's call sites do not fork, and each reports which
surfaces it supports through a capability descriptor — a surface with no
counterpart is hidden rather than stubbed (AG-9).

Governing spec: ``specs5/plan-ag/``. Read ``decisions.md`` first; the
verified SDK surface and its raw protocol captures are in
``sdk-surface.md``.

Public surface:

- :func:`surface_report` — the SDK-drift probe (AG-8). Reflects over the
  installed wheel and buckets every name handled / declined / pending.
- :func:`resolve_credentials`, :class:`Credentials` — which Gemini API key
  or Vertex project the engine will use, and where it came from. A
  credential is mandatory: the SDK has no OAuth path and cannot borrow
  the ``agy`` login (AG-R-8).
- :func:`key_file` — where a stored key is read from (AG-11). The SDK has
  no file-based credential source of its own, so this one is ours.
- :class:`Consultant` — one-shot ``second_opinion`` and ``generate_image``
  calls (AG-7). Not a session: AG-R-9's boundary is what keeps phase 3's
  engine from being shaped by a call pattern it does not have.
- :class:`ConsultantBridge` — those two as MCP tools for a Claude Code
  turn, under their **own** server name so they reach the permission
  dialog rather than inheriting the index tools' ungated allow.
- :class:`AntigravitySession` — the phase-3 engine spike: one harness,
  one conversation, and a turn pumped from ``receive_steps()``. Not
  registered with the RPC service and not reachable from the webapp.
- :class:`StepTranslator` — the ``Step`` → ``Event`` pump. Emits the
  *same* event names as the Claude pump, because AG-3 puts both engines
  under one RPC namespace and a second vocabulary would fork 43 methods
  across 59 webapp files.
- :mod:`~aic_dc.antigravity.options` — config assembly, and the write
  seam. ``MUTATING_TOOLS`` is AG-5's boundary; a session with no decide
  hook enables none of them.
- :class:`AntigravityPermissionGate` — the AG-5 gate. A
  ``PreToolCallDecideHook`` that drives the **shared**
  ``PermissionBroker``, so there is one ask path, one queue and one
  localhost rule across both engines. Pass its ``as_hook()`` to the
  config, not the gate itself.

Import design note: nothing here imports ``google.antigravity`` at module
scope. The SDK spawns a bundled ~119 MB Go binary, and these modules must
stay importable — and unit-testable — on a machine where it is not
installed. ``google-antigravity`` is 0.1.15 and alpha, which is the whole
reason the probe exists.
"""

from __future__ import annotations

from aic_dc.antigravity.bridge import ConsultantBridge
from aic_dc.antigravity.consultant import (
    Consultant,
    ConsultationError,
    ImageResult,
)
from aic_dc.antigravity.credentials import (
    Credentials,
    MissingCredentialsError,
    key_file,
)
from aic_dc.antigravity.credentials import resolve as resolve_credentials
from aic_dc.antigravity.permissions import AntigravityPermissionGate
from aic_dc.antigravity.session import (
    AntigravitySession,
    SessionNotStartedError,
    TurnInProgressError,
)
from aic_dc.antigravity.steps import StepTranslator
from aic_dc.antigravity.surface import (
    DECLINED,
    HANDLED,
    PENDING,
    diff_agy_init,
    surface_report,
)

__all__ = [
    "DECLINED",
    "HANDLED",
    "PENDING",
    "AntigravityPermissionGate",
    "AntigravitySession",
    "Consultant",
    "ConsultantBridge",
    "ConsultationError",
    "Credentials",
    "ImageResult",
    "MissingCredentialsError",
    "SessionNotStartedError",
    "StepTranslator",
    "TurnInProgressError",
    "diff_agy_init",
    "key_file",
    "resolve_credentials",
    "surface_report",
]
