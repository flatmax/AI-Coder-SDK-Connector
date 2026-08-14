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

---

## Phase 2 — Chat on the new engine (2026-08-14)

**Exit criterion:** *"A user can hold a full working conversation, including edits, entirely through
Claude Code."* Met — see [Live verification](#live-verification-1).

The plan's *permissions before edits* constraint held: no run at any point used
`permission_mode: "bypassPermissions"`, and the write that satisfies "including edits" went through
the browser dialog.

### What landed

**Backend.** One new module, plus the gate wired through the session:

| Module | Lines | Role |
|---|---|---|
| `claude_code/permissions.py` | 1490 | `can_use_tool` gate: request classification, the pending-request registry, `derive_suggested_rules`, `build_answer_input`, deny/allow/always-allow resolution |
| `claude_code/service.py` | 881 (+367) | `resolve_permission`, `set_denied_read_files`, permission events, `doc_convert_available` on `EngineState` |
| `claude_code/session.py` | 824 (+33) | Passes the gate to `build_options`; permission-mode changes without a reconnect |
| `claude_code/options.py` | 214 (+30) | The `system_prompt` preset fix (below) |
| `claude_code/messages.py` | 979 (+31) | `compact_boundary`, subagent framing |

`src/ac_dc/collab.py` (+51): the async permission gate runs on the SDK's task, not on an RPC task,
so the caller-identity `ContextVar` was unset there and every async gate read as "not localhost".
Fixed by propagating the context. **This retroactively enables every async gate on `LLMService` and
`Repo`** — they had the same latent hole and were failing open. `test_collab_restrictions.py` grew
from its previous size to 68 tests to pin it.

`src/ac_dc/main.py` (+116): registers the permission plumbing, and kills the CLI child on shutdown
(see [The orphan fix](#the-orphan-fix)).

**Frontend.** `webapp/src/permission-dialog/` is new (4970 lines incl. tests):

| Module | Lines | Role |
|---|---|---|
| `index.js` | 952 | `<ac-permission-dialog>` |
| `bodies.js` | 282 | Per-tool request bodies |
| `queue.js` | 306 | Serialises concurrent requests; one dialog at a time |
| `styles.js` | 537 | |
| `decisions.js` | 212 | Decision payload assembly |
| `diff-editor.js` | 177 | The `Write` preview |
| `constants.js` | 142 | |

`webapp/src/chat-panel/` gained `blocks.js` (557), `block-render.js` (904) and `permission-mode.js`
(276), and `streaming.js` was substantially rewritten (1582 lines changed) to consume the Claude
Code event stream instead of the native one. `styles.js` (+825 net) carries the turn-block UI.

`LLMService` and `src/ac_dc/llm/` are **untouched and still registered** — per the plan's rule that
phases 1–3 are not interleaved. Nothing in the chat path reaches them.

### Removed from the frontend

Deleted outright, because the native engine's affordances have no Claude Code equivalent yet:

| Deleted | Lines | Returns in |
|---|---|---|
| `chat-panel/urls.js` + `urls.test.js` | 283 + 722 | — |
| `url-chips.js` + `url-chips.test.js` | 552 + 595 | — |
| The three retry-prompt builders (in `helpers.js`, −88) | — | — |

Also removed from the UI: the mode toggle and the ✨ and 📜 buttons (**phase 6**), the reasoning
control (**phase 6**), and the URL chips. `<ac-history-browser>` is left mounted but **inert** —
`history_list` does not exist on `ClaudeCodeService` yet (**phase 5**). `_reasoningEnabled` and
`_reasoningEffort` are still declared on the chat panel but nothing reads them; the `viewer` framing
value arrives from the engine and is not wired to anything. The CSS families for all of the above
were deleted from `styles.js`.

`enrichment_status`, `mode` and `cross_ref_enabled` go silent: they were native-engine state with no
producer on this path. `doc_convert_available` was added to `EngineState` in their place
(`service.py:99`, `:267`, `:291`) so the Files tab can still tell whether conversion is possible.

### The `system_prompt` fix

`build_option_kwargs` now sets `system_prompt={"type": "preset", "preset": "claude_code"}`.
**Omitting it had been sending an empty prompt**, not the CLI's: the SDK emits `--system-prompt ""`
for `None`, which strips the dynamic sections carrying the working directory, the git status and the
platform. Observed: an agent asked to edit `greet.py` in a repo at `/tmp/ac-dc-live` reached for
`/home/flatmax/greet.py`, because nothing had told it where it was. This is the one exception to
`options.py`'s null-means-omit rule, and the reason is recorded next to it.

### The orphan fix

`main._signal_handler` exits via `os._exit` to avoid hanging on `_heavy_init`'s
sentence-transformer load. That skips `atexit` — and the Claude Agent SDK's *only* orphan guard is
an `atexit` hook (`subprocess_cli._kill_active_children`). Measured: **SIGINT during a streaming
turn left the `claude` process running for a further ~38 seconds**, reparented to init, still
holding the repo as its working directory.

`main._kill_cli_children()` now signals the SDK's child registry before `os._exit`: SIGTERM, a
0.25 s grace, then SIGKILL for anything left. SIGTERM alone stopped a mid-stream child in under
0.3 s, so the escalation is for a wedged child rather than the normal path. Verified live:
mid-stream SIGINT now takes server *and* child within 0.27 s, with no lingering `_bundled/claude`
process and no zombie.

Two things a reader will otherwise re-derive: liveness cannot be probed with `os.kill(pid, 0)`,
because a signalled-but-unreaped child is a zombie that still answers it — hence `_child_exited`
using `waitpid(WNOHANG)`, without which the grace period was served out in full every time. And the
registry is private SDK surface, so an SDK that moves it costs us the fix but not the ability to
exit.

### Tests

533 python tests across seven files, all offline:

| File | Tests | What it pins |
|---|---|---|
| `test_claude_code_permissions.py` | 106 | Request classification, the pending registry, suggested-rule derivation, `build_answer_input`, resolution paths |
| `test_claude_code_service.py` | 153 | The RPC surface incl. the 13 localhost gates, permission events, `doc_convert_available` |
| `test_claude_code_session.py` | 83 | Gate wiring, permission-mode change without reconnect |
| `test_claude_code_messages.py` | 77 | `compact_boundary`, subagent framing |
| `test_collab_restrictions.py` | 68 | **The `ContextVar` fix** — async gates now see the real caller |
| `test_claude_code_options.py` | 35 | The SDK-drift tripwire, plus the `system_prompt` preset |
| `test_main_shutdown.py` | 11 | The orphan fix: polite-first, escalation, and the zombie regression |

`test_main_shutdown.py` spawns real child processes rather than mocking `os.kill`, because the thing
under test *is* signal delivery — a mock would pass whether or not the signal reached anything.

Frontend, 509 tests across the modules this phase touched or created:

| File | Tests |
|---|---|
| `chat-panel/block-render.test.js` | 128 |
| `chat-panel/blocks.test.js` | 82 |
| `permission-dialog/dialog.test.js` | 88 |
| `chat-panel/streaming.test.js` | 64 |
| `permission-dialog/queue.test.js` | 59 |
| `chat-panel/events.test.js` | 50 |
| `chat-panel/permission-mode.test.js` | 38 |

Whole-suite state at the close of the phase: python **1 failed, 3897 passed**, webapp **89 files,
3215 passed**. The one failure is below.

**Phase 1's absence assertions are now partly deleted**, as phase 1's entry said they should be:
`resolve_permission` and `set_denied_read_files` exist and are tested. `history_list` and
`get_denied_read_files` are still asserted absent — phase 5 and the file-picker work respectively.

One pre-existing failure in the full python run, untouched by this phase and unrelated to it:
`test_doc_convert/test_libreoffice_pipeline.py::TestLibreOfficeDispatch::test_odp_routes_to_libreoffice_when_available`,
which needs PyMuPDF (`import fitz` fails) that is not installed. `doc_convert/` is on the
inventory's keep-unchanged list and this phase's diff touches none of it.

### Live verification

Against the real CLI (bundled 2.1.229, Bedrock) in a scratch repo, `permission_mode: "default"`
throughout:

- A full conversation streamed end to end: text, thinking, tool-use cards correlated to their
  results, and the `ResultMessage` footer with duration, engine-turn count and cost.
- **The dialog appears before the write.** *Deny* landed — no file was written, and the agent
  adapted in the same turn. *Allow once* landed — the docstring reached disk (confirmed with
  `git diff`), the log recorded `resolved as allow by localhost`, and the turn reached a full
  footer.
- Ctrl-C mid-stream leaves no orphaned `claude` process (above).
- An `API Error: Output blocked by content filtering policy` on an unrelated prompt rendered
  correctly rather than breaking the turn — `api error` badge on the assistant header, the message
  inline, and the footer still populated.

The compaction divider renders from `compact_boundary`, **client-side only**; it does not survive a
reload. Persistence is phase 5's, with session history.

Spec divergences found while implementing were written back into the specs per the plan's rule that
the wheel wins: `specs5/5-webapp/permission-dialog.md`, `specs5/5-webapp/chat.md`,
`specs5/4-features/collaboration.md`, `specs5/plan/sdk-surface.md`,
`specs-reference/3-engine/permissions.md` and `specs-reference/3-engine/session.md`.

### Always-allow: six bugs, found by asking the CLI instead of guessing

Phase 2 first shipped with two findings recorded-but-unfixed, on the grounds that changing
permission semantics on a guess was worse than documenting the doubt. The way out was to stop
guessing: a throwaway probe connected with a `can_use_tool` that denied everything and logged
`context.suggestions` verbatim. That is the authoritative source, since the plan makes the installed
wheel win over any document — and it turned two suspicions into six confirmed defects. The observed
suggestion shapes are now recorded in
[`specs-reference/3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md) § What
the CLI actually suggests.

Fixed:

- **The derived rule named the wrong tool, so it did nothing.** Rules were built from the requesting
  tool's name, giving `Write(src/ac_dc/**)` for a `Write` call. Claude Code consults path rules for
  `Edit` and `Read` only; anything else is accepted, never consulted, and warned about at startup
  (v2.1.210+). Clicking "always allow" wrote a rule to settings that had no effect, and the user was
  asked again on the next call. `_RULE_TOOL_FOR_PATHS` now maps write tools to `Edit` and read tools
  to `Read`, and the button's label names the rule that will actually be written. Tools with no
  consulted path rule — `Grep` among them — derive nothing.
- **The derived rule over-granted.** `_derived_path_rule` emitted `<dir>/**`, which reads like "this
  directory" but is a whole-subtree match in gitignore syntax, and collapsed to `**` for a file at
  the repo root: one click on a dialog naming a single file granted writes to every file in the
  repository. It now grants **the literal path approved and nothing else**, mirroring the CLI, whose
  own generated rule "matches only the literal path you approved".
- **A path outside the repo was anchored wrongly.** The rule was `/home/x/**` where `/path` means
  "relative to the settings source", so it would have resolved under the project root and never
  matched. Absolute paths now carry the CLI's `//` anchor. Gitignore metacharacters in the path are
  escaped too, so a directory like `[2024-06] Reports` produces a rule that matches itself.
- **The always-allow tooltip asserted something false.** It said "There is no invisible session-only
  grant behind this button". The CLI suggests `destination: "session"` for reads outside the working
  directory, and a file-modification approval is session-scoped by design — the published tier table
  gives its lifetime as "until session end". There are now two tooltips and the one shown agrees
  with the destination chip beside it.
- **The derived shell rule over-granted, the same way the path rule did.** `_derived_command_rule`
  emitted a prefix pattern, so `git push:*` authorised `git push --force origin main` from a click
  on a dialog that said `git push origin main`. The CLI derives the literal sub-command; the
  default is now the literal command, with the prefix kept as a second entry in the rule menu the
  dialog already had. That answers the friction objection without making the broad grant the thing
  a fast click gets. The command is stripped but not otherwise normalised — collapsing its internal
  whitespace would produce a rule that never matches, which is the first bug in this list again.
- **The transcript rendered an approved call as denied.** `applyPermissionOutcome` cleared the
  denial only for `action === 'allow'`, so a call approved with "always allow" got the amber lock
  and a denial body — while it ran. No test covered it; the two frontend comparisons are now one
  imported `ALLOW_ACTIONS`, mirroring the engine's, and a test iterates every allow action.

One correction to the earlier write-up: the `//` in `Read(//home/flatmax/**)` is not a formatting
quirk of the CLI's. It is the documented anchor for an absolute filesystem path, and our own code
was the thing getting it wrong.

### The always-allow control a write never gets — now built

For an in-repo file edit the CLI's **only** suggestion is `setMode` → `acceptEdits` with
`destination: "session"`; it offers no rule whatsoever. `derive_suggested_rules` dropped
non-`addRules` suggestions, so the CLI's actual offer was never shown.

The drop stands for the rule control — switching to `acceptEdits` stops the dialog appearing for
*every* later edit, a far larger grant than the one call on screen, so it cannot honestly share a
button labelled "always allow this call". It now has its own control: `derive_suggested_mode`
picks the suggestion up, the payload carries `suggested_mode`, and an `allow_mode` decision applies
it. Four things about it are load-bearing:

- **The mode rides back on the permission result** as a `setMode` `PermissionUpdate`, not as a
  separate `set_permission_mode` control request. It is atomic with the allow, and — the reason
  that matters — the CLI is *waiting* on this response, so issuing another control request before
  answering it is a deadlock waiting for a slow user.
- **The offered mode comes from the request the engine built, never from the decision.**
  `resolve_permission` is localhost-only, but a mode is a session-wide grant, and a client able to
  name its own could name `bypassPermissions`. `_MODE_OFFERS` holds the modes we have copy for;
  `bypassPermissions` is absent from it and re-checked when the update is built.
- **The CLI applies the mode without announcing it.** `permissionMode` appears only in the `init`
  system message, so nothing on the stream would have told the mode selector. The broker gained a
  `note_mode` callback; the service updates the session's cached mode and broadcasts the
  `permissionModeChanged` the selector already listens for, attributed to the dialog. Without it
  the selector would keep claiming `default` while the engine accepted edits silently — the exact
  class of lie the rest of this section is about.
- **The copy states what is lost, not just what is granted.** "you will not see a diff for it" is
  the consequence a user would otherwise discover. The engine owns that copy so the button cannot
  describe a consequence the engine does not apply.

Still divergent from the CLI, and left alone deliberately: our derived rules default to
`destination: "projectSettings"` (`.claude/settings.json`, git-tracked) where the CLI persists
approvals to `localSettings` (`.claude/settings.local.json`, gitignored). The button does name the
file, so it is not dishonest — but it means an always-allow can land a permission grant in a
committed file and share it with the whole team. One-word fix; not made here because it changes
which file gets written and deserves its own decision.

### Deliberately not built

- **No `Edit`/`MultiEdit` input editing in the dialog.** `Write` gets a diff preview the user can
  edit; `Edit` and `MultiEdit` are shown read-only. Editing an `old_string`/`new_string` pair in a
  textarea invites a no-longer-matching edit that fails after approval, which is a worse experience
  than approving as-proposed and asking for a change.
- **The `Write` preview is against the proposed content, not disk.** For a new file that is the same
  thing; for an overwrite the dialog does not show what is being lost. Wants a real diff against the
  file on disk.
- **No rewind UI.** `rewind_files()` is on the service and nothing calls it.
- **Subagent attribution is by id, not by name.** Nested tool calls carry the subagent's id; the UI
  shows the id. Mapping it to the agent's name needs the definition lookup.
- **Three `interact` affordances are unbuilt.** `build_answer_input` and multi-question rendering
  landed; free-text-with-suggestions, question grouping, and the "ask again" path did not.
- **The file tree does not refresh mid-turn after a write.** A written file shows `Modified +1` only
  after a reload. Post-write re-indexing is phase 4 (hooks).
- **No health-banner link target.** The banner renders and its link goes nowhere.
- **Deferred by decision, not by omission:** the preset selector (CC-12), the subagent browser, the
  Context tab and HUD (**phase 6**), the history browser and session management (**phase 5**), and
  relabelling the file picker's "deny agent read" (CC-14).

### For whoever picks up phase 3

- **Phase 3 is the deletion.** `LLMService` and `src/ac_dc/llm/` are intact and registered; the
  chat path does not reach them. The exit criterion for phase 2 is met, so the deletion is now
  unblocked — do it in one commit, per the plan.
- **`collab.py`'s `ContextVar` fix is load-bearing beyond this phase.** Every async gate on
  `LLMService` and `Repo` depends on it now. Deleting `LLMService` must not take the fix with it.
- **The permission gate runs on the SDK's task.** Anything it needs from request context has to be
  propagated explicitly; nothing about the RPC call is ambient there.
- **13 `ClaudeCodeService` methods are localhost-gated**: `connect_engine`, `shutdown`,
  `set_selected_files`, `chat_streaming`, `cancel_streaming`, `resolve_permission`,
  `set_denied_read_files`, `set_permission_mode`, `set_model`, `rewind_files`, `stop_task`,
  `reconnect_mcp_server`, `toggle_mcp_server`. A new method that mutates engine state or spends
  money belongs on that list, and `test_claude_code_service.py` should pin it.
