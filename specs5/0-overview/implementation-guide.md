# Implementation Guide

How to use `specs5/` and `specs-reference/` together when implementing AC⚡DC.

## Context: Why Two Suites Exist

AC⚡DC's specification is split across two peer directories:

- **`specs5/`** — behavioural contracts, invariants, module decomposition, data flow. Written at the level a capable reimplementer actually needs. Deliberately omits byte-level detail that a fresh implementation would legitimately handle differently.
- **`specs-reference/`** — implementation detail that specs5 deliberately leaves unspecified but an implementation must reproduce for interop. Byte-level formats, numeric constants, persistent storage schemas, RPC argument shapes, dependency quirks. Mirrors specs5's path structure; each twin supplements its specs5 counterpart.

The goal is equivalent user-visible behaviour and interop compatibility, not line-by-line reproduction. Internal structures (module boundaries, class hierarchies, internal APIs, framework patterns) are the implementer's choice. External structures (wire formats, file formats, numeric thresholds that affect observable behaviour) are contracts, because existing data, existing user configs, and other AC⚡DC instances depend on them.

There is a third input, and it is not a spec: **the installed Claude Agent SDK**. See [Reading the SDK](#reading-the-sdk) below.

## The Two-Suite Relationship

**specs5 is the primary reference.** It describes behavioural contracts, invariants, module decomposition, and data flow at a level suitable for clean-room implementation. Design from specs5, structure modules per specs5, write tests against specs5's invariants.

**specs-reference is the detail reference.** Byte-exact formats, numeric thresholds, storage schemas, dependency quirks. When specs5 leaves a format or threshold unspecified, consult the mirrored file at the same path and name. Twins exist only where specs5 needs supplementing — a missing twin means the specs5 spec is self-sufficient.

The conversion added a third kind of twin content: **SDK-behaviour findings**. Where the SDK does something surprising — a protocol method probed by attribute presence, a mirror that is asynchronous despite an eager flag, a bare-flag `extra_args` convention — the finding lives in the twin's *Dependency quirks* section, because that is exactly what it is.

## When to Use Which

### Use specs5 for:

- Architecture and module decomposition
- What components exist and how they relate
- Behavioural contracts (what must happen)
- Invariants (properties that must hold)
- Event flows and lifecycle descriptions
- Test design — invariants become test properties
- Whether a capability is ours at all, or the engine's

### Use `specs-reference/` for:

- **Byte-level formats** — the symbol map compact format, doc outline annotation syntax (`←N`, `→target#Section`, `~Nln`, content-type markers), request-ID and block-identity formats, permission-rule syntax
- **Numeric constants** — debounce intervals, timeouts, retry and backoff schedules, truncation limits, port ranges
- **Persistent storage schemas** — engine-transcript line format and session-key path mapping, the events-log record shape, the derived index's layout, docuvert provenance headers, doc-cache sidecars
- **Config file schemas** — exact field names, nesting, whitelists, legacy-format fallbacks
- **Dependency quirks** — tree-sitter TypeScript function name, Vite `optimizeDeps` exclusions, PyInstaller hidden imports, Monaco worker configuration, `mcp` version floor, CLI discovery and version skew
- **RPC wire formats** — exact argument shapes, return shapes, event payload structures

## Reading the SDK

Layer 3 is written against the Claude Agent SDK, which is a dependency rather than a contract we own. Three rules follow:

1. **[`../plan/sdk-surface.md`](../plan/sdk-surface.md) records what was verified, and when.** It is a snapshot, not a guarantee. Read the installed package before implementing anything in layer 3.
2. **The installed wheel wins over every document in this repo, including this one.** If the SDK's behaviour contradicts a spec, the spec is stale — fix the spec rather than working around it silently.
3. **Never assume a class or option exists because a spec names it.** Several plausible-sounding names in the origin brief turned out not to exist; the same will happen again as the SDK moves. Attribute-probe rather than import-and-hope where the spec says the surface is optional.

Version skew between the SDK and the `claude` CLI is its own failure class, surfaced as an engine-health banner rather than an exception. See [`specs-reference/3-engine/session.md` § Dependency quirks](../../specs-reference/3-engine/session.md#dependency-quirks).

## Conflict Resolution

**When specs5 and a twin conflict, specs5 wins.** specs5 owns behavioural contracts. If a twin's detail describes behaviour specs5 contradicts, the twin is out of date and needs updating.

**When specs5 is silent on a byte-level or numeric detail, the twin is authoritative.** Do not invent alternative wire formats, file formats, or thresholds that affect observable behaviour. Changing them silently breaks compatibility with existing data or existing user configs.

**When both are silent, the implementer chooses.** Class hierarchies, method names, module organisation, framework patterns, and internal APIs are not contracts. A cleaner design is welcome.

**When a twin's detail looks like a workaround for something that no longer exists, raise it.** Some twin detail captures compromises made against the native engine or against library bugs. Flagging "the twin says X but it reads as a workaround for a subsystem we deleted" is more valuable than silent preservation.

**When you have a better approach than a twin prescribes, propose it before implementing.** Describe the twin's approach, the alternative, and the trade-offs. A short discussion beforehand is cheaper than a refactor afterwards.

## Freedom the Implementer Has

- Module organisation — restructure files, merge or split modules, pick better names
- Internal APIs — method signatures, class hierarchies, dependency-injection patterns
- Framework choices — different async primitives, different data structure types
- Error handling — a unified approach rather than case-by-case
- Code style — naming, type hinting, docstring conventions
- Test structure — organise around specs5's invariants
- Performance work — specs5's invariants do not prescribe caching strategies beyond what correctness requires

## Freedom the Implementer Does Not Have

Changes that cross a boundary visible to users, git, the engine, or other AC⚡DC instances are fixed by interop:

- The persistent storage formats — engine transcript and session-key layout under `.ac-dc4/`, the events-log schema, docuvert headers, `.bundled_version` marker, doc-cache sidecars. The derived index is *not* in this list: it is rebuildable, so its format may change freely
- The RPC surface the webapp expects, including server-push event names and payload shapes
- The config file schemas users edit, and the whitelist of what the Settings tab may write
- The `ac-dc` MCP tool names, argument shapes, and output formats — the agent's prompt cache and any project-level tool-permission rules are keyed to them
- Permission rule syntax written into project settings, which the CLI reads too

There is one entry that used to be on this list and is now absent: **LLM-facing prompt text**. AC⚡DC no longer has any. The engine's system prompt is the engine's, and repo conventions belong in `CLAUDE.md`, which the user owns.

## Subtle Cases

Some specs5 descriptions can be satisfied in several valid ways, but a twin pins a sequencing or timing detail that downstream behaviour depends on. The load-bearing examples:

- **Index flush before a tool answer.** Re-indexing is debounced, but a pending flush must complete before any `ac-dc` tool returns. Get the ordering wrong and the agent silently reads a stale map — no error, just a wrong answer.
- **Drain before the next turn.** Cancellation must run the pump to `ResultMessage`. Breaking out of iteration routes the interrupted turn's tail into the next turn's UI and produces asyncio cleanup failures.
- **Hooks are not a decision channel.** A `PreToolUse` hook that returns a permission decision silently disables the dialog. Observe in hooks; decide in `can_use_tool`.
- **Credential resolution.** Nothing may export provider credentials into the process environment. The CLI resolves its own; polluting the environment changes which account a turn bills to.

When in doubt, read the twin for sequencing and ordering constraints, not just formats.

## Where specs5 Is Incomplete Without specs-reference

| Area | specs-reference location |
|---|---|
| Symbol map compact format | [`2-indexing/symbol-index.md`](../../specs-reference/2-indexing/symbol-index.md) (legend, abbreviations, ditto marks, path aliases, test collapsing) |
| Doc outline annotation syntax | [`2-indexing/document-index.md`](../../specs-reference/2-indexing/document-index.md) (keyword parentheses, content-type markers, section size, ref counts, outgoing refs) |
| Request ID, block identity, timestamp formats | [`3-engine/session.md` § Byte-level formats](../../specs-reference/3-engine/session.md#byte-level-formats) |
| SDK options assembly | [`3-engine/session.md` § Schemas](../../specs-reference/3-engine/session.md#schemas) (every `ClaudeAgentOptions` field we set, and why) |
| Streaming and lifecycle event payloads | [`3-engine/session.md` § Service: AcApp](../../specs-reference/3-engine/session.md#service-acapp--server--browser) — the authoritative server-push event set |
| Permission rule syntax and `PermissionUpdate` shape | [`3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md) (rule content syntax, callback signature, return types, tool classification map) |
| Permission timeout and ID format | [`3-engine/permissions.md` § Numeric constants](../../specs-reference/3-engine/permissions.md#numeric-constants) |
| `.ac-dc4/` layout, session keys, engine transcript lines | [`3-engine/history.md` § Byte-level formats](../../specs-reference/3-engine/history.md#byte-level-formats) |
| Mirrored-store JSONL schema | [`3-engine/history.md` § Schemas](../../specs-reference/3-engine/history.md#schemas) |
| `SessionStore` conformance harness | [`3-engine/history.md` § Dependency quirks](../../specs-reference/3-engine/history.md#dependency-quirks) |
| RPC method signatures | [`1-foundation/rpc-inventory.md`](../../specs-reference/1-foundation/rpc-inventory.md) (full inventory with argument and return shapes) |
| Config file schemas | [`1-foundation/configuration.md`](../../specs-reference/1-foundation/configuration.md) |
| Docuvert provenance header | [`4-features/doc-convert.md`](../../specs-reference/4-features/doc-convert.md) |
| Collaboration admission messages | [`4-features/collaboration.md`](../../specs-reference/4-features/collaboration.md) (message types, 120 s timeout, close code 1008, share-info payload) |
| Startup progress stages | [`6-deployment/startup.md`](../../specs-reference/6-deployment/startup.md) (stage name strings, reconnect backoff, port probe range) |
| Dependency quirks | [`2-indexing/symbol-index.md`](../../specs-reference/2-indexing/symbol-index.md) (tree-sitter TypeScript), [`5-webapp/diff-viewer.md`](../../specs-reference/5-webapp/diff-viewer.md) (Monaco workers), [`6-deployment/build.md`](../../specs-reference/6-deployment/build.md) (Vite `optimizeDeps`, PyInstaller imports), [`3-engine/session.md`](../../specs-reference/3-engine/session.md) (CLI discovery, `mcp` floor) |

Two rows that earlier suites carried are gone rather than moved: cache-tier thresholds and model-specific cache minimums (no tiering), and system prompt text (no prompt).

## Architectural Position Changes

This suite changes what AC⚡DC *is*, not merely how it is built. The binding decisions, each with its rationale, are in [`../plan/decisions.md`](../plan/decisions.md); the file-by-file disposition is in [`../plan/inventory.md`](../plan/inventory.md). Read `decisions.md` before writing code in any layer — several specs read as under-specified until you know that the corresponding capability is deliberately the engine's.

The four that most change how a layer is built:

| Decision | Consequence for the implementer |
|---|---|
| [CC-1](../plan/decisions.md#cc-1--total-replacement-not-a-dual-engine-mode-user) Total replacement | No dual-engine abstraction layer. Do not write an interface that both a native engine and the SDK could implement; there is one engine. |
| [CC-6](../plan/decisions.md#cc-6--the-indexes-reach-claude-code-as-mcp-tools-not-as-prompt-text) Indexes as tools | The indexes have one consumer shape — request/response — instead of two. No assembly path, no per-turn seeding. |
| [CC-3](../plan/decisions.md#cc-3) + [CC-19](../plan/decisions.md#cc-19) Mirrored history | **One** transcript, two roles. Never read it back to build context — continuity is `resume`/`fork_session`. Never give the store an entry the CLI did not write. Anything else under `.ac-dc4/` is derived and rebuildable, or holds only what the transcript never had. |
| [CC-15](../plan/decisions.md#cc-15--permission-prompts-are-localhost-only) Localhost-only permissions | The authority check belongs in the resolution path, not in the UI. A hidden button is not a security boundary. |

Contracts inherited from earlier suites that remain live and are not obvious from any single spec: the repository layer's **per-path write mutex**; the **one user-initiated turn at a time** guard, which counts user intent and therefore does not gate subagents; **streaming state keyed by request ID** rather than a singleton passive-stream flag; and the **single bespoke SVG editor** on both panes, with the left pane constructed read-only.

## Build Order Suggestion

Bottom-up, matching the layer numbering:

1. **Foundation** — RPC transport, configuration, repository (git operations, file I/O)
2. **Indexing** — symbol index, document index, reference graph, keyword enrichment
3. **Engine** — session and message pump, permissions, tool surface and hooks, `SessionStore` and history, MCP bridge, context visibility
4. **Features** — images, code review, collaboration, document convert
5. **Webapp** — shell, chat, viewers, file picker, search, settings, specialised components
6. **Deployment** — build, startup, packaging

Each layer depends only on layers below it. Within layer 3 the order matters more than usual, and it is not the order the specs are listed in: **session and pump first, then permissions, then hooks and the tool surface, then history, then the MCP bridge, then context visibility**. Permissions before the tool surface because a tool surface built under `bypassPermissions` bakes in the assumption that tools never block. The MCP bridge after history because the bridge is the first component with two masters — the agent and the browser — and it is easier to get right once the storage boundaries are settled.

For converting an existing installation rather than building fresh, the phase table in [`../plan/README.md`](../plan/README.md) supersedes this ordering: it keeps the tree shippable at every step, which a bottom-up rebuild does not.

## Testing Strategy

- Every spec ends with an invariants section; treat these as test property sources.
- **Unit tests** verify component-level invariants — "the pump routes an unknown block kind to the generic path", "a permission rule is always tool-plus-pattern, never a bare tool grant".
- **Integration tests** verify cross-layer invariants — "a `PostToolUse` write is reflected in the next `symbol_map` result", "a cancelled turn's transcript ends with a `ResultMessage`".
- **End-to-end tests** verify user-facing contracts — "file selection change broadcasts to all connected clients", "a restart resumes the previous session".
- **Contract tests against the SDK** are their own category and the only defence against version skew: the `SessionStore` conformance harness, the message-taxonomy coverage check, and an options-assembly test that fails when a field we set disappears from `ClaudeAgentOptions`. These are cheap, and they fail loudly on an SDK upgrade instead of quietly at runtime.

Anything that requires a real model call belongs behind a marker and out of the default run. The engine is a subprocess with credentials; a test suite that needs it is a test suite nobody runs.
