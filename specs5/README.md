# AIC⚡DC Specification Suite (specs5)

**Status:** Active — target specification for AIC⚡DC as a browser frontend for Claude Code.

This suite describes the system **after** the engine conversion. AIC⚡DC no longer assembles prompts,
counts tokens, or caches context in tiers; it drives a Claude Code session through the Claude Agent
SDK and renders it. The conversion itself — what is kept, deleted, and added, and why — is in
[`plan/`](plan/README.md). The specs assume every decision in
[`plan/decisions.md`](plan/decisions.md).

## Companion Tree: `specs-reference/`

A companion directory at the repo top level holds implementation detail that specs5 deliberately
leaves unspecified — byte-level formats, numeric constants, persistent storage schemas, RPC argument
shapes, dependency quirks. The two trees mirror each other: when implementing from
`specs5/{path}/{name}.md`, also load `specs-reference/{path}/{name}.md` if it exists. Missing twins
mean the specs5 spec is self-sufficient. See
[`specs-reference/README.md`](../specs-reference/README.md) for the full convention.

## What AIC⚡DC Is

A browser frontend for Claude Code. It runs as a local terminal process, serves a Lit single-page app,
and drives one Claude Code session per repository. Claude Code does the thinking, the tool use, and
the file editing; AIC⚡DC provides the surfaces a terminal cannot — a Monaco diff viewer over
everything the agent touches, a git-status file tree, an SVG editor, a TeX preview, a 2-D file
navigation grid, permission prompts with a real diff in them, live context and cost visualisation,
and multi-client collaboration.

It also contributes intelligence of its own: tree-sitter symbol indexing and document-outline
indexing, exposed to the agent as MCP tools and to the editor as language features.

## Architecture at a Glance

```
Browser (Lit SPA) ←─ WebSocket / JSON-RPC 2.0 ─→ Python process ─→ claude CLI subprocess
                                                  (git + indexes + MCP bridge)
```

The Python process owns a `ClaudeSDKClient`, a repository layer over git, both indexes, an in-process
MCP server exposing them, and optional collaboration and document-conversion services. The browser
hosts a draggable dialog (chat, context, settings, doc convert) over a full-viewport diff/SVG viewer.

See [`0-overview/architecture.md`](0-overview/architecture.md) for the complete picture.

## Reading Order

Specs are numbered in dependency order — bottom-up. Each layer depends only on layers below it.

| Layer | Contents |
|-------|----------|
| **0 Overview** | Architecture, glossary, implementation guide — start here |
| **1 Foundation** | jrpc-oo, RPC transport, RPC inventory, configuration, repository |
| **2 Indexing** | Symbol index, document index, keyword enrichment, reference graph |
| **3 Engine** | Claude Code session, permissions, tool surface and hooks, history and sessions, context visibility, MCP bridge |
| **4 Features** | Images, code review, collaboration, document convert |
| **5 Webapp** | Shell, chat, viewers and HUD, file picker, search, settings, subagent browser, specialised components |
| **6 Deployment** | Build, startup, packaging |
| **7 Future** | Speculative designs and the record of what the platform implemented for us — **not for implementation** |

Two directories sit outside the layer numbering:

| Directory | Contents |
|---|---|
| [`plan/`](plan/README.md) | The conversion plan of record: decisions, keep/delete/add inventory, verified SDK surface, risk register, origin brief. Becomes history when the conversion lands. |
| [`impl-history/`](impl-history/README.md) | Delivery record of the pre-conversion system. Historical; references retired specs and older suites intentionally. |

## Conventions

- **Behavioral contracts, not implementation details.** Specs describe what must happen and what
  invariants must hold, not specific method names or field shapes (except at module boundaries like
  RPC, config files, persistent storage).
- **Single source of truth.** Each concept has one authoritative spec; others reference it without
  re-deriving.
- **Terminology is defined once** in [`0-overview/glossary.md`](0-overview/glossary.md).
- **The SDK is a dependency, not a contract we own.** Where a spec depends on Claude Agent SDK
  behaviour, [`plan/sdk-surface.md`](plan/sdk-surface.md) records what was verified and when. Re-read
  the installed wheel when implementing; the SDK moves faster than this suite.
- **Test invariants are inline** at the end of each spec — properties that must hold, not specific
  test functions.
