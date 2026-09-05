"""The ``agy`` transport — Antigravity reached through the CLI, not the SDK.

[AG-14](../../../specs5/plan-ag/decisions.md) adds this beside
:mod:`aic_dc.antigravity.service`'s SDK engine rather than in place of it.
The two reach *different backends*: the SDK speaks to Gemini API or Vertex
on a metered key, and ``agy`` speaks to Code Assist on the owner's OAuth —
which is the only route to a paid Google AI Pro subscription, and the
reason this transport exists at all. The SDK's key refuses at 20 requests
per model per day, which is not enough to verify an engine, let alone use
one.

Why this is a sub-package when the rest of ``antigravity/`` is flat
------------------------------------------------------------------
Two reasons, and the second is load-bearing.

It is a whole transport rather than one more collaborator: a gate, a
session, a step reader and an ownership registry, none of which the SDK
path has an equivalent for. And :mod:`aic_dc.antigravity.surface` derives
its ``handled`` bucket by globbing ``*.py`` **beside itself** — so code
here is deliberately outside that glob. Nothing in this directory touches
the SDK, and a module that named an SDK symbol from in here would
otherwise report that symbol as covered when it is not.

Governing spec: ``specs5/plan-ag/`` — AG-14, AG-5, AG-R-12.
"""
