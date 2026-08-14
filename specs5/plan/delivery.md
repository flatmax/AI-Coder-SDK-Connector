# Delivery log

One entry per conversion phase from [`README.md`](README.md#phases), written when the phase's exit
criterion is met. Each entry records what landed, what was deliberately left out, and what the next
phase has to do first — so a phase can be picked up cold without re-deriving the previous one's
state.

Corrections to the specs found while implementing belong in the spec, not here. This file points at
them; it does not restate them.

---

## Phase 1 — Engine spike (2026-08-14)

**Exit criterion:** *"A CLI-side smoke test can send a prompt and print the streamed message
taxonomy."* Met — see [Live verification](#live-verification).

### What landed

`src/ac_dc/claude_code/`, a self-contained package with no import edge to `ac_dc.llm`:

| Module | Lines | Role |
|---|---|---|
| `engine_config.py` | 224 | `EngineConfig` over `engine.json`; every field nullable, null means "let the CLI decide" |
| `health.py` | 464 | `resolve_cli()` version gate, `EngineHealth`, `EngineStartupError`, failure classification |
| `options.py` | 194 | The single place `ClaudeAgentOptions` is constructed; `NEVER_SET` prohibitions with reasons |
| `messages.py` | 960 | `TurnTranslator` — SDK message objects → AC⚡DC event payloads |
| `session.py` | 791 | One `ClaudeSDKClient` per process: connect, admission, message pump, interrupt drain, live controls |
| `service.py` | 566 | The `ClaudeCodeService` RPC facade |
| `__init__.py` | 86 | Public surface |

Supporting changes:

- `scripts/engine_smoke.py` (255 lines) — the CLI-side smoke test the exit criterion names. Sends
  one prompt, prints every event payload with its channel, and can drive an interrupt mid-turn.
- `src/ac_dc/config/engine.json` — the default (all-null) engine config, registered in
  `config.py`'s `_USER_FILES` so it is user-editable and not overwritten on upgrade.
- `src/ac_dc/main.py` — registers `ClaudeCodeService` alongside `LLMService`. The chat path is
  untouched: nothing in the frontend calls the new namespace yet.

**The engine connects lazily**, on the first turn or an explicit `connect_engine()`. Connecting at
startup would add a second `claude` subprocess (~295 MB resident) to every launch for a service
nothing is calling. Phase 2 keeps this: the eager connect belongs with the UI that shows the
connecting state.

### Tests

302 tests across five files, all offline — no CLI is spawned, no credentials are read:

| File | Tests | What it pins |
|---|---|---|
| `test_claude_code_engine_config.py` | 22 | Nullable-field semantics, validation, precedence |
| `test_claude_code_options.py` | 34 | **The SDK-drift tripwire** — every key we emit must exist on the installed `ClaudeAgentOptions` dataclass |
| `test_claude_code_messages.py` | 74 | Subclass-before-superclass dispatch, cumulative chunks, block identity, nothing dropped, no double render |
| `test_claude_code_session.py` | 81 | Framing, connect failure modes, pump resilience, interrupt drain, admission |
| `test_claude_code_service.py` | 91 | The RPC surface, lazy connect, slash equivalents, event arity, cancel |

Two are contract tests against the wheel rather than against our logic, and are the ones that will
fail first on an SDK upgrade: `test_every_key_exists_on_the_installed_dataclass` (options) and the
`SystemMessage`-subclass assertions plus the SDK-error-name tripwire (messages, session).

`test_claude_code_service.py` also asserts the phase-2/3 surface is **absent** —
`resolve_permission`, `get_denied_read_files`, `set_denied_read_files`, `history_list`. Those
assertions are meant to be deleted by the phase that implements each method; a green run of them is
a statement about scope, not about correctness.

### Live verification

Two runs against the real CLI (bundled 2.1.229, Bedrock), both with `--permission-mode plan` so no
write was possible:

- The full message taxonomy printed, including the framing events, cumulative text and thinking
  deltas, tool-use cards correlated to their results with durations, and the `ResultMessage`
  footer with a populated `total_cost_usd`.
- The interrupt path drained to a real `ResultMessage` rather than truncating the stream.

Divergences between the installed wheel and the specs were found and written back into the specs,
per the plan's rule that the wheel wins: see
[`sdk-surface.md` § Corrections found while implementing phase 1](sdk-surface.md) and
`specs-reference/3-engine/session.md`. The substantive ones were the `thinking` option's shape (a
TypedDict union, not a `ThinkingConfig` constructor), the SDK's real CLI discovery order (bundled
beats `PATH`), the correct partial-message block key, and `rewind_files`'s `restored` list always
being empty.

One behaviour is in no spec and should be treated as real: `SystemMessage(subtype="status")` with
`{"status": "requesting"}`, which arrived four times during a three-tool-call turn. It currently
falls through to the generic `systemEvent` channel, which is correct but silent — phase 2 may want
it as a "thinking…" affordance.

### Deliberately not built

Not oversights; each is a later phase's scope:

- **No `can_use_tool` gate.** The session accepts one as a constructor argument and the service
  passes nothing. There is therefore no path to a browser permission prompt, which is exactly why
  the live runs used `plan` mode. What `permission_mode: "default"` does with no callback attached
  was not exercised — **phase 2 must land the gate before any run uses `default` or `acceptEdits`
  for real work.** This is the plan's *permissions before edits* constraint, and it is the reason
  phase 1 stopped where it did.
- **No hooks, no MCP servers, no `SessionStore`.** All three are constructor arguments that are
  currently `None`. Hooks must stay strictly observational when they arrive: a `PreToolUse` hook
  that returns a decision shadows `can_use_tool` and would silently bypass the dialog phase 2
  builds.
- **No frontend.** No Lit component calls `ClaudeCodeService`.
- **No `live` pytest marker.** Phase 1 produced no credentialed pytest tests — the credentialed
  path is `scripts/engine_smoke.py`, a script run by hand — so declaring a marker nothing uses
  would be speculative. Add it in the phase that first writes a credentialed test.

### For whoever picks up phase 2

- **The RPC namespace is the class name.** `add_service(instance)` derives the namespace from
  `type(instance).__name__`, so renaming `ClaudeCodeService` breaks every frontend call site at
  once. `test_claude_code_service.py::TestRpcSurface` pins the name for this reason.
- **`request_id`, not `session_id`, is the multiplexing primitive.** Turn-scoped events carry
  `(request_id, payload)`; session-wide events carry `(payload,)`. The event-arity tests encode
  the split.
- **Never `break` out of the message iterator.** Cancel means interrupt then drain to
  `ResultMessage`; breaking early leaves the CLI mid-turn and the next turn inherits the mess.
