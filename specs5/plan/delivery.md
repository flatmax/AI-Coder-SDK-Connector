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

Whole-suite state at the close of the phase: python **3897 passed** with nothing failing, webapp
**89 files, 3215 passed**. The one failure that stood here is fixed — below.

**Phase 1's absence assertions are now partly deleted**, as phase 1's entry said they should be:
`resolve_permission` and `set_denied_read_files` exist and are tested. `history_list` and
`get_denied_read_files` are still asserted absent — phase 5 and the file-picker work respectively.

**One pre-existing failure, unrelated to this phase, now fixed** — and the first diagnosis of it
was wrong, which is the interesting part.
`test_doc_convert/test_libreoffice_pipeline.py::TestLibreOfficeDispatch::test_odp_routes_to_libreoffice_when_available`
was recorded here as "needs PyMuPDF, which is not installed", implying an `import fitz` escaping
somewhere. It was not. The failure was `assert 0 == 1` on the call log: PyMuPDF is an **optional**
extra (`docs-convert` in `pyproject.toml`), `convert_pptx_via_libreoffice` pre-flight-checks it at
`pdf_pipeline.py:94`, and without it an `.odp` falls back to markitdown **without ever spawning
soffice**. That fallback is the documented, intended behaviour; the test was asserting the
LibreOffice route was taken without declaring that it needs both deps. Its `.pptx` sibling twenty
lines above guards itself with `_require_pymupdf()`; the `.odp` case had been written without it.
Adding the same guard turns a false failure into an honest skip, so a base install now runs the
suite green: `tests/test_doc_convert/` is **113 passed, 75 skipped**, and the whole python suite has
no failures. No production code was touched — `doc_convert/` is on the inventory's keep-unchanged
list and stays there.

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

> **Closed at the start of phase 3** by [`decisions.md`](decisions.md) CC-16: `localSettings` is the
> default, `projectSettings` survives as one `shared`-tagged menu row, and no derived rule may name a
> path under `.claude/` — the second defect the first one was hiding.

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

> **Two of those came forward into phase 3**, and its entry says why. The Context tab and HUD were
> replaced rather than vacated (CC-17), because a three-phase gap in "how full is the context" is not
> a neutral wait. CC-14 landed as wiring *and* labels together, because a read-denial behind a label
> saying "exclude from index" is a dishonest control — worse than either half of the job alone.

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

> **17 as of phase 3**, and the four new ones do not look gated from `service.py`: `commit_all`,
> `reset_to_head`, `start_review` and `end_review` delegate, so their `_check_localhost_only()` sits
> in `claude_code/commit.py` and `claude_code/review.py`.

---

## Phase 3 — The rip-out (2026-08-15)

**Exit criterion:** *"`grep -r litellm src/` is empty; test suite green."* Met on both counts:
`grep -rn -i litellm src/` and the same over `webapp/src/` return nothing, and both suites pass —
python **2550 passed, 75 skipped**; webapp **88 files, 3163 passed**.

One commit, per the plan's no-interleaving rule: 189 files, **+6228 / −69527**.

**The suite is green and smaller.** Python went 3897 → 2550 tests, because 52 test files and 33,350
lines of them tested code that no longer exists. That is a shrinking denominator, not improving
coverage, and it is the honest reading of the number.

### What went

37 Python modules, 25,371 lines:

| Deleted | Lines | What it was |
|---|---|---|
| `llm_service.py` | 2043 | The RPC face of the native engine |
| `llm/` (20 modules) | 13,704 | Streaming, prompt assembly, the cache warmer, breakdown, agents, the RPC mixins |
| `stability_tracker.py` + `cache_membrane.py` | 1977 | Four-tier cache assignment and its flux controller |
| `context_manager.py` | 1393 | The central session state holder |
| `edit_protocol.py` + `edit_pipeline.py` | 1640 | The emoji edit protocol and its applier |
| `url_service/` (7 modules) | 2840 | URL detection, fetching, extraction, summarising, cache |
| `history_compactor.py` | 658 | LLM-driven compaction |
| `token_counter.py` | 578 | The `tiktoken` wrapper |
| `file_context.py` | 279 | Per-file context assembly |
| `agent_factory.py` | 259 | The `🟧🟧🟧 AGENT` spawn protocol |

Seven config files, 569 lines: `system.md`, `system_doc.md`, `system_agentic_appendix.md`,
`system_reminder.md`, `compaction.md`, `review.md` and `llm.json`. Five were prompts this app composed
and sent; there is no longer a prompt to compose. **`review.md` has no successor at all** (CC-13) —
the review no longer describes itself to a model. A review is an *arrangement of the repository* (disk at
the branch tip, HEAD at the merge-base, everything staged), and the agent reaches the pre-change state
the way a human reviewer does, with `git show` / `git diff` / `git log`. `commit.md` is the one prompt
file that stayed (+12/−10): it is a message *format*, handed to the agent per commit rather than
installed as a system prompt, and `config.py` keeps it out of the editable-file whitelist for that
reason.

`config.py` lost 896 lines and gained 55: the provider table, the model catalogue, the tier budgets,
the cache-warmup knobs and the prompt-file loaders all described the deleted engine. **Nothing in the
config layer writes `os.environ`.** The `claude` CLI resolves its own credentials, and injecting a
key or a region silently changes which account a turn bills to — that is left as a deliberate
absence, not an oversight.

Five dependencies left `pyproject.toml` with it: `litellm`, `tiktoken`, `boto3`, `tenacity` and
`trafilatura`. The reason each one was there is recorded in the comment that replaced them.

`src/ac_dc/__init__.py` now re-exports nothing but `__version__`. It used to hoist the four engine
types that were constructed everywhere; all four are gone, and the surviving subsystems are reached
by module path, which keeps `import ac_dc` free of transitive cost.

### What survived by moving

Four pieces of `llm/` were not engine machinery — they were features that happened to live inside the
engine, and each is re-pointed at the CLI:

| New home | Lines | Was |
|---|---|---|
| `claude_code/commit.py` | 321 | `llm/_commit.py` — generate a commit message, stage, commit, reset |
| `claude_code/review.py` | 410 | `llm/_review.py` — branch review: checkout, diff, posture, teardown |
| `doc_index/background.py` | 464 | `llm/_doc_index_background.py` — the post-write doc-index builder |
| `claude_code/service.py` (+294) | — | The LSP RPC surface, `get_snippets`, `navigate_file`, and the git/review RPCs from `llm_service.py` and `llm/_rpc_lifecycle.py` |

Two things about the review move are load-bearing. Review entry switches the engine's permission
posture to `plan`, which is a control request — so `start_review` is now async where its predecessor
was not. And a review can be entered on a **cold** engine, before the CLI process exists, where
there is no client to send a control request to. `EngineSession.prefer_permission_mode` handles that
case: it sets the posture a *future* `connect` starts in, and `build_option_kwargs` grew a
`permission_mode` parameter so the session passes its **current** mode rather than `engine.json`'s.
Without it, connecting would have silently reverted a posture the user had already asked for.

`get_snippets` returns two sets now, `review` and `code`. The `doc` set went with the modes: there is
no longer a state in which documents are the only thing the agent can see, so a document-specific
snippet list has nothing to key off.

### The panels were replaced, not vacated

[`decisions.md#cc-17`](decisions.md), rebuilding what CC-4 specified — the HUD and the Context tab
were the two surfaces most completely made of deleted numbers, and the plan's phase table originally
left them for phase 6. `inventory.md:155-156` had already named both replacement files.
Deleting them and shipping a gap for three phases would have removed the app's only answer to *how
full is the context* at exactly the moment the CLI started making that decision on the user's behalf.

| Deleted | Lines | Replacement | Lines |
|---|---|---|---|
| `context-tab.js` | 2360 | `context-usage-tab.js` | 629 |
| `token-hud.js` | 1245 | `usage-hud.js` | 586 |

Both read `ClaudeCodeService.get_context_usage`, which is a pass-through of the breakdown the CLI's
own `/context` prints — so the tab and that command cannot disagree. The category colours come from
the engine, so a user running both does not have to learn two colour languages; `categories[].color`
carries the CLI's *theme token names* (`claude`, `promptBorder`, `inactive`, `warning`) rather than
CSS, and `context-usage.js` maps them.

`maxTokens` is the model's raw window — it equals `rawMaxTokens`, and the autocompact buffer is
*not* subtracted from it. The compaction point is `autoCompactThreshold`, a separate field (167,000
against a 200,000 window on Sonnet). The bar therefore fills toward the threshold, not `maxTokens`,
so 100% is the real trigger point; `compactionLimit()` and `warningPercent()` own that arithmetic
for all three views that render this payload. The payload was written against the opposite belief,
and the live run corrected it: the ratios are provable from three identities the engine maintains —
the content categories sum to `totalTokens`, `Free space` is `autoCompactThreshold − totalTokens`,
and `Autocompact buffer` is `maxTokens − autoCompactThreshold`. The structural rows are room left
rather than content, so the bar excludes them and checks the sum before segmenting.

Two absences in them are deliberate. There is **no refresh loop** — the breakdown only moves when a
turn runs or a session loads, so it refreshes on those events plus a button; polling would spend
control requests watching a number that cannot change on its own. And there is **no "rebuild cache"
button**, because the cache is the CLI's and there is no request to rebuild it; a button that quietly
did nothing would be worse than no button.

**Cost renders as "included", never as `$0.00`.** `total_cost_usd` is null under subscription
billing. A turn on a Max plan did not cost nothing — it cost nothing *extra*, and the two are not the
same claim. The model is read from the turn rather than from a config default, because `set_model`
can change it mid-session and a subagent may have used a different one.

The dialog's capacity bar was re-based from `get_history_status` (gone) onto `get_context_usage` for
the same reason.

### The file picker's third state now means something (CC-14)

The three-state checkbox's third position used to mean *keep this file out of the structural index*.
There is no index in the prompt to keep it out of, so per [`decisions.md`](decisions.md) § CC-14 it
now writes a real `Read` deny rule to `.claude/settings.local.json` via
`ClaudeCodeService.set_denied_read_files`.

**This was listed under phase 2's "Deliberately not built" as deferred by decision**, framed as
labelling work. It is done here instead, and the reading is worth stating because a later reader will
find the two in conflict: wiring a read-denial *behind a label that says "exclude from index"* is a
dishonest control — worse than either half of the job alone. `inventory.md:144` puts CC-14 in the
phase-3 ADAPT table and `specs5/5-webapp/file-picker.md:5` states the end state outright, so the
deferral is read as a record of what phase 2 didn't do rather than a prohibition on phase 3. Wiring
and labels landed together.

- **The picker's user-facing words are "deny agent read"**; the internal vocabulary stays `excluded` /
  `_excludedFiles` / `exclusion-changed`. That is the name of a *tree state* — the third position of
  a checkbox — shared with the picker, its event contract and a dozen tests. What changed is what the
  state means to the backend, and that lives in one function.
- **One repo-wide RPC, no per-tab dispatch.** The agent-tab branch (`set_agent_excluded_index_files`)
  was dropped rather than re-pointed: a deny rule lands in settings sources that every SDK subagent
  inherits, so a per-agent variant would be a promise the permission layer cannot keep.
- **The list is authoritative, not additive**, which is what makes un-denying work without a second
  method.
- **The L0-invalidation dialog is gone** — with its three-way localStorage preference, its CSS and
  its 20 tests. Excluding a file used to rewrite a ~100K-token cache prefix, so a dialog asking the
  user whether to pay for that now was honest work. Both halves of the trade are gone: this app
  builds no aggregate map, and the CLI's prompt cache is the CLI's. Its one surviving job — telling
  the user the change is not instant — is now a `takes_effect` toast shown **once per session**, plus
  the checkbox tooltip. The RPC returns that string; the frontend does not assume it.
- **A remote collaborator's tick is refused and says so.** `set_denied_read_files` is localhost-only
  (CC-15) and the `error: 'restricted'` path surfaces a toast, because otherwise the checkbox lies.

### Frontend surfaces deleted

Beyond the two replaced panels:

| Deleted | Lines | Why |
|---|---|---|
| `compaction-progress.js` + test | 758 | The CLI compacts itself and reports one `compact_boundary`. A progress bar over someone else's compaction would be an animation, not a measurement — the transcript divider is the replacement |
| `cache-warmup-progress.js` | 322 | There is no cache to warm, and the four `cacheWarmup*` receivers went with it |

Also removed: the **retry banner** (the successor is the `rateLimit` push, which reports the CLI's own
limit rather than our retry loop); the **reasoning toggle and effort selector** — the `minimal` level
the selector offered is not in the SDK's `EffortLevel` vocabulary, so it was offering a value that
could not be sent; five native `compactionEvent` stages (`url_fetch`, `url_ready`, `compacting`,
`compacted`, `compaction_error`), whose handler now hardens its `default` because *acting* on a stale
`compacted` would replace the transcript the user is reading with a summary from an engine that is
gone; the whole `binaryFilesSkipped` receiver, since nothing assembles a prompt from ticked files any
more so a tick cannot silently fail; and `settings-tab.js`'s config-card grid, cut from eight
cards to three (`engine`, `app`, `snippets`) with the model rows removed from its banner.

`engine.json` is **editable but not reloadable**, and the card says so: session options are read when
the CLI subprocess starts, so a reload would report success while the running engine kept its
original model and posture. `app.json` does reload, because that one takes.

The banner names no model even if a stale backend volunteers one. `engine.json`'s value is a
*request*; the engine can answer on a different model (a rate-limit fallback, a mid-session
`set_model`), and the model actually used is reported per turn by the HUD.

### Dormant, annotated, not deleted

Five push receivers have no emitter after this phase — `modeChanged`, `agentModeChanged`,
`agentsSpawned`, `agentsRehydrated`, `agentClosed` — and so do the surfaces that consume them: the
code/doc mode toggle and the agent tab strip. They are **kept and commented, not tombstoned**,
because their replacements are deferred *by decision*: the preset selector is CC-12 and the subagent
browser is CC-8. Deleting a receiver while leaving its tab strip mounted moves the break rather than
fixing it.

The consequence is worth knowing when reading that code: an agent tab can only be created by an
`agentsSpawned` push, nothing emits one, so `parseAgentTabId` cannot return a tag outside the tests
and every `LLMService` call inside the strip is unreachable rather than broken. `app-shell/mode.js`'s
`switchMode` guards on the method being absent and returns.

`selection.js` carried a phase-3 promise that could not be kept — "phase 3 removes both the call and
its caller". The call site stays with the dormant strip it belongs to, and the comment now says why
instead of predicting a deletion that didn't happen. The sibling promise in `events.js` — "they stay
until phase 3 deletes `LLMService`" — was honoured.

`LLMService` call sites in still-live code were left in place with the phase that owns each: phase 4
(`set_cross_reference`, `set_agent_cross_reference`), phase 5 (`history_list_sessions`,
`history_get_session`, `history_search`, `get_turn_archive`, `load_session_into_context`,
`new_session`), CC-8 (`close_agent_context`, `set_agent_selected_files`, `list_live_agents`,
`get_agent_history`, `switch_agent_mode`) and CC-12 (`switch_mode`).

### Tests

Three new backend files, 104 tests, covering the three re-homed modules:

| File | Tests |
|---|---|
| `test_doc_index_background.py` | 43 |
| `test_claude_code_review.py` | 33 |
| `test_claude_code_commit.py` | 28 |

`test_claude_code_service.py` grew to 175 (+59/−17) for the absorbed RPC surface.
`test_config.py` (−478 net) and `test_settings.py` (−103 net) shed the provider, model-catalogue and
prompt-file cases.

**`test_collab_restrictions.py` went 68 → 34 tests, and the fix it exists to protect survived.**
Half its cases pinned `LLMService`'s gates. What matters is that `TestGateUnderRealDispatch` — the
five tests pinning phase 2's `ContextVar` fix, including "the caller survives an `await` inside the
method" — is independent of `LLMService` and is intact. Phase 2's note that the fix is load-bearing
beyond that phase was the reason to check.

Frontend: `exclusion.test.js` 18 (the 20 L0-dialog tests replaced by 11 read-denial ones),
`events.test.js` 39, `per-tab.test.js` 25, `settings-tab.test.js` 18.

**Neither new panel has a unit test.** `context-tab.js` and `token-hud.js` had none either, so this
is an inherited gap rather than a new one, but it is a gap: the two files are 1215 lines with only
the service-level tests for `get_context_usage` behind them.

### Live verification — not done for this phase

This was a deletion, verified by two suites and a grep. **The replacement panels have not been
exercised against a running CLI**, so their rendering of a real `ContextUsageResponse` — the category
list, the colours, the "included" cost path under subscription billing — is unproven outside the
service-level pass-through tests. Phase 6's exit criterion ("the Context tab shows the same numbers
as `/context` in the CLI, live") is the check that closes this, and it is worth doing sooner than
that.

Two empty package directories were removed by hand (`src/ac_dc/llm/`, `src/ac_dc/url_service/`);
both held nothing but stale `__pycache__` after their contents were deleted.

### Deviations from `inventory.md`

- **The review orchestration landed at `claude_code/review.py`, not `repo/review.py`.**
  `repo/review.py` already existed as the git-mechanics mixin, and the orchestration needs the
  engine — permission posture, prompt streaming, index rebuild. Splitting them keeps the mixin
  free of the engine; `repo/review.py`'s docstring now points at the orchestrator.
- **The specs' "recorded in the transcript as a system event" is not implemented, for any of the
  three things that claim it** — review entry and exit (`4-features/code-review.md:273`), a mode
  change during review (`:295`), and a permission-mode change (`4-features/collaboration.md:247`).
  All three broadcast live (`reviewStarted` / `reviewEnded`, `permissionModeChanged`) and vanish
  on reload, exactly as phase 2's compaction divider does. There is no transcript to write to until
  phase 5, so the deviation is one of ordering rather than of design; it is listed here so phase 5
  finds the three claims rather than one of them.
- **The two TeX RPCs were deleted, not re-homed**, against `inventory.md:76` ("navigate, TeX,
  snippets survive"). `LLMService.is_tex_preview_available` / `compile_tex_preview` were thin
  duplicates of `repo/tex_preview.py`, and the frontend already called `Repo.*` — so re-homing them
  onto `ClaudeCodeService` would have created a second name for a working RPC. `navigate_file` and
  `get_snippets` did survive onto the service, because they have no `Repo` equivalent.
- **`context_usage.py` was not created**, against `inventory.md:103`, which lists it as a new backend
  module doing "fetch, shaping, and caching for the Context tab". `ClaudeCodeService.get_context_usage`
  has existed since phase 1 and is already a pass-through; shaping happens in the two components, and
  caching would put a stale number in front of the user for no gain, since the call is one control
  request against a value that only moves when a turn runs.
- **`Repo.get_files_by_directory` and `get_directory_mtime` were deleted**, not kept: their only
  caller was the engine's per-directory prompt assembly.
- **The empty-path post-write signal is a no-op until phase 4.** `Repo`'s post-write callback is now
  `DocIndexBuilder.note_file_written`, which covers *one* of the two write paths it used to. The
  user's edits go through `Repo.write_file`; the agent's do not — the CLI's `Write` and `Edit` write
  to disk directly, and re-indexing after those is the post-tool-call hook that lands with the MCP
  bridge in phase 4. This is stated in `main.py` at the wiring site so it isn't rediscovered as a
  bug.
- **`edit-blocks.js`, `edit-block-render.js`, `agent-block-render.js` and the prose render path were
  kept**, against the inventory's DELETE. They are the decoders for archived transcripts: a session
  saved before the conversion contains emoji edit blocks and `🟧🟧🟧 AGENT` framing, and deleting the
  renderers turns old history into garbage on screen. They are archive decoders now, not a live path,
  and they revisit with phase 5.
- **Retired config files are ignored, not deleted** — a deliberate choice the inventory did not
  specify. The upgrade iterator walks the *union* of the managed and user file sets, so a user
  upgrading in place keeps their `system.md` and `llm.json` on disk. That text may be customised
  prompt work; deleting it would be irreversible and pointless, since nothing reads the file either
  way. The cost is a few kilobytes. What was **not** built is the notice: nothing tells such a user
  the files are now inert.
- **The permission-rule destination default flipped to `localSettings` before this phase, not in
  it** — CC-16 landed as its own commit, as the note under phase 2 records.
- **`prefer_permission_mode` and `build_option_kwargs(permission_mode=…)` are new seams** the
  inventory did not anticipate; the cold-engine review-entry case above is why.
- **`engine` is a new config type** in the settings whitelist, replacing `llm`.
- **Doc-index enrichment for CLI-tool writes is deferred to phase 4** (same cause as the no-op
  signal above).

### Deliberately not built

- **No live verification of the new panels** — see above. This is the one that should be closed first.
  *Closed 2026-08-16; it found five wrong numbers. See the [interlude](#interlude--the-context-panels-meet-a-live-cli-2026-08-16).*
- **No unit tests for `usage-hud.js` / `context-usage-tab.js`.** *Closed 2026-08-16, same entry.*
- **The mode toggle and agent tab strip are still mounted and inert.** CC-12 and CC-8.
- **No upgrade notice for a stale `llm.json` or `system.md`.** They are left on disk and ignored.
- **The doc index still misses the agent's writes.** Phase 4.
- **`<ac-history-browser>` is still mounted and inert.** Phase 5, unchanged from phase 2.

### For whoever picks up phase 4

- **Phase 4 is the MCP bridge, and it inherits one clear consumer.** The plan's ordering constraint
  ("indexes after the rip-out, not before") is now satisfied: prompt assembly is gone, so the symbol
  and doc indexes have exactly one consumer — the browser — and the bridge is written against that
  instead of two competing ones.
- **Two things are waiting on the post-tool-call hook**, and they are the same thing: the file tree
  does not refresh after the agent writes, and the doc index does not learn about it. Both are the
  callback in `main.py` covering only `Repo.write_file`. Wire the hook once and both close.
- **17 RPCs are localhost-gated now, not 13, and four of them do not look it.** `commit_all`,
  `reset_to_head`, `start_review` and `end_review` delegate, so the `_check_localhost_only()` call is
  in `claude_code/commit.py` and `claude_code/review.py` rather than in `service.py`. A grep of
  `service.py` alone will read them as ungated. They are pinned by
  `test_claude_code_commit.py` and `test_claude_code_review.py`.
- **`collab.py`'s `ContextVar` fix survived the deletion**, as phase 2's note required. Its five
  tests are `TestGateUnderRealDispatch` in `test_collab_restrictions.py`; that file lost half its
  cases with `LLMService` and those five are the ones that must not go.
- **Nothing in the config layer may write `os.environ`.** The CLI resolves its own credentials, and
  an injected key or region silently changes which account a turn bills to.
- **Phase 1's absence assertions are now phase 5's alone.** `get_denied_read_files` and
  `set_denied_read_files` both exist and are tested; `history_list`, `history_load` and
  `history_delete` are still asserted absent by `test_phase_five_methods_are_absent`, on phase 1's
  reasoning that a stub reporting success would be worse than a missing method. Delete that test as
  you build them, not before.

---

## Interlude — the two dialogs the terminal has and the browser did not (2026-08-15)

Not a phase. Two gaps found while answering "can this repo build itself yet?", both in the permission
dialog, both cheap enough to close before phase 4 rather than after. The question mattered because
self-hosting makes the permission dialog the only way to work: a gap the terminal covers is a gap that
stops the session.

### `ExitPlanMode` is now a class of its own

`classify_tool` had no entry for it, so it took the unknown-name fallthrough to `exec`. The consequence
was not a missing dialog but a wrong one: the plan arrived through `CommandPayload`, summarised, capped
at 4 000 characters, under the heading "command", with focus moved to Deny whenever the prose happened
to contain the word "delete". A dialog asking for approval of something it is not showing.

- `plan` is a `tool_class`, gated by default; `PlanPayload` carries `plan`, `headline` and `file_path`.
- The plan renders as markdown, whole, scrolled rather than truncated. `plan` is optional in the CLI's schema — injected from disk — so the no-plan case has its own body, and `planFilePath` is shown when present.
- The primary button says "Approve plan"; the header shows the plan's first line rather than `ExitPlanMode`'s title, which is identical for every such call; deny prefills "Keep planning".
- No suggested rule is derived. A standing grant here approves every future plan unread.

### `AskUserQuestion` gained the reply the terminal always offers

The tool's schema tells the model *not* to write an "Other" option, on the grounds that the front end
provides one. Ours did not, so a user whose answer was none of the offered options had to deny the call
and start again in prose. Each question now has a text field: typing clears a single-select selection and
is sent instead of it, a multi-select sends it alongside what is ticked, and a typed reply counts as that
question's answer for the "Answer" button.

`PermissionDecision.answers` is now `[{options, text}]` per question. Bare index lists are still read, so
a browser mid-upgrade does not lose its selections.

### One spec correction, made in code first

`specs5/5-webapp/permission-dialog.md` said the freeform reply travels as `input.response`. Implementing
that literally would have silently discarded every option the user picked: the CLI's result mapping tests
`response` *before* it reads the answers map, and returns "The user responded: …" instead of it. The
reply goes through `answers[<question text>]` like any other answer. Both specs now record the verified
semantics; `test_no_response_key_is_ever_written` pins the behaviour.

### Tests

- `tests/test_claude_code_permissions.py` — `TestPlanPayload` (8), `TestFreeformAnswers` (7), plus the classification and summary cases. 134 in the file, all passing; 2 568 in the suite.
- `webapp/src/permission-dialog/dialog.test.js` — `describe('a plan')` (8) and `describe('a question the options do not answer')` (7). `queue.test.js` gained 6. 3 184 in the webapp suite, all passing.

### Deliberately not built

- **The permission mode goes stale after an approved plan.** The CLI sets its own mode to `prePlanMode ?? "default"` and announces nothing, so the mode selector goes on saying `plan`. Same class of lie `note_mode` fixed for `setMode` suggestions, but the target mode is whatever preceded plan mode and the SDK does not expose it — the engine would be guessing. Recorded in `specs-reference/3-engine/permissions.md` § `ExitPlanMode`.
- **The transcript tool card for `ExitPlanMode` is still generic.** The dialog was the blocking gap; the card is a read of history.
- **No "chat about this" on the dialog.** Denying with a reason is the available path, and it works — the agent reads the reason — but it costs a turn where the terminal would let the user just talk.
- **`preview` and `annotations` on `AskUserQuestion` remain unbuilt.** Phase 6, unchanged.

---

## Phase 4 — The indexes as MCP tools (2026-08-15)

**Exit criterion:** *"Claude Code can call `symbol_map` / `doc_outline`; hover and go-to-definition
still work in Monaco."* Met for the tools — see [Live verification](#live-verification-2). **The Monaco
half is proven by tests only**, for the reason below.

This is CC-6 built: the symbol and document indexes reach the agent as tools it decides to call, not as
text prepended to a prompt. The difference is not packaging. Prompt injection paid for the index on
every turn whether or not the turn was about code; a tool is paid for when asked, and the agent's choice
to call it is visible in the transcript as a tool card.

### What landed

Two new modules, both in `src/ac_dc/claude_code/`:

| File | Lines | What it is |
|---|---|---|
| `mcp_server.py` | 734 | `McpBridge` — the six tools, their schemas, and their rendering |
| `hooks.py` | 383 | `Reindexer` and `build_hook_matchers` — the `PostToolUse` re-index |

**`McpBridge` takes callables, not indexes.** Every provider is a zero-argument callable
(`symbol_index`, `symbol_index_ready`, `doc_index`, `doc_index_ready`, `review_state`, `ui_state`,
`flush`) resolved at call time. The indexes it reads are built minutes after the session connects and
are replaced wholesale on a rebuild, so a bridge holding references would answer from the object that
existed at wiring time. It also means the tools and the browser read *the same* index objects — an SDK
in-process server, not a subprocess with a second copy.

The six tools, all annotated `readOnlyHint=True`:

| Tool | Answers |
|---|---|
| `symbol_map` | The compressed repo map, optionally under a `path_prefix` |
| `file_symbols` | Symbols for named paths, with line numbers |
| `find_references` | Definition and referrers for a name |
| `doc_outline` | Headings, keywords, line counts and link targets for documents |
| `review_state` | Whether a review is in progress, and over what |
| `ui_state` | Selected files, the open viewer, permission mode |

The last two are why the bridge exists as more than an index shim: the agent can now ask what the human
is looking at. `ui_state` returns a copy, not the service's own dicts —
`test_the_snapshot_does_not_alias_the_service_state` pins that, because a tool handing out a live
reference lets a schema change in the browser mutate service state.

**Chunking is by path, not offset.** A map of this repo does not fit in one tool result, so both map
tools return a chunk plus a continuation cursor, and the cursor is a *path*. An offset would be
invalidated by the re-index that the next call may trigger; a path still names a file. `exclude_files`
for the next call is computed against the whole index rather than the chunk — the bug that version one
had, and the one the live run surfaced honestly: the agent reported "the map came back chunked,
`session.py` is in a chunk I have not seen" instead of concluding the file did not exist.

**Freshness is a flush, not a hope.** `Reindexer` debounces the `PostToolUse` writes it sees, and every
index-reading tool calls `flush()` *before* it answers. So the agent that writes a file and immediately
asks for its symbols gets the file it just wrote. `MAX_FLUSH_ROUNDS = 2` bounds a write storm; an
`asyncio.Lock` serialises drains and is the thing `flush()` joins on.

**Three-state readiness, plus a failure flag.** `absent` / `building` / `built` on the service
(`_mark_symbol_index_ready`, `_mark_symbol_index_failed`, called from `main.py`'s heavy init), and
`DocIndexBuilder.failed` for the doc half. A partially built index answers Monaco's hovers and is
withheld from the map tools, which report it unavailable and point at `Grep`. A hover that resolves for
half the repo is useful; a map that covers half the repo is a lie with no marker on it. The `failed`
flag exists because "wait and retry" and "this will never work" are indistinguishable through `ready`
alone, and an agent that retries a permanent failure spends turns on it.

**The agent's writes now reach the doc index.** `DocIndexBuilder.note_file_written` gained a `bool`
return and a second caller. Phase 3 left it wired to `Repo.write_file` only — the user's edits — with a
comment saying the agent's writes were phase 4's job. They are now the `PostToolUse` path, calling the
same method, so the decision about which extensions matter stays in one place. The return value is what
lets the re-index report *which* writes refreshed an index without the caller having to know that.

**`SymbolIndex.resolve_indexed_path` learned to take the paths an agent types.** The CLI reports writes
as absolute paths; the index is keyed relative. It now accepts absolute paths under the repo root,
`./`-prefixed and `..`-containing relative paths, and refuses anything that escapes the root —
`/etc/passwd` and `../outside/a.py` return `None`, while `.github/workflows/x.py` resolves, because a
leading dot is a real directory and only `..` leaves.

### The gate had to learn about our own tools

`specs5/3-engine/permissions.md` puts the `ac-dc` index tools in the read-only row: *displayed, not
gated*. That was implemented as `classify_tool` returning `"read"` for them — which shapes a dialog's
wording and does not skip one. `Read`, `Glob` and `Grep` are ungated because the **CLI** never asks
about them. Our MCP tools it does ask about, in `acceptEdits` and `default` though not in `plan`.

So `can_use_tool` now early-returns `PermissionResultAllow()` for `mcp__ac-dc__*`, with no dialog, no
broadcast, and no prompt recorded on the turn — a prompt nobody saw must not inflate the turn footer's
tally. Without it the agent stalls on a dialog for every `symbol_map` call, and answering those is
click-through training, which is R-12 in `risks.md` becoming true through a mechanism the risk register
did not anticipate: not fatigue from real prompts, but noise from prompts that should not exist.

`allowed_tools` was **not** used for this, which would have been the obvious fix. Setting it in options
is forbidden — it replaces the CLI's own resolution of the user's settings — so the allow lives in our
gate, where the reason for it is readable.

### Retired: the cross-reference toggle

The only frontend control deleted rather than left dormant. It chose which index fed the native
engine's prompt; both indexes are now permanently available as tools, so there is nothing left to
switch. `toggleCrossRef`, `_crossRefEnabled`, `_toggleMainCrossRef`, `_toggleAgentCrossRef`, the
`+xref` mode-string composition, and the `cross_ref_enabled` snapshot hydration are all gone, each site
carrying a one-line tombstone naming the phase.

**The mode axis stayed.** `_mode`, `_tabModes`, `onModeChanged` and `onAgentModeChanged` are still
mounted and still inert, waiting for CC-12's preset selector and CC-8's `Task` tab strip. The rule that
decided each case: remove a receiver only when its consumer is going too, because removing a receiver
while leaving the consumer mounted moves the break instead of fixing it. The cross-reference toggle had
no consumer left; the mode axis has one arriving.

Two tests pin the retirement as a *behaviour* rather than an absence: `toasts-and-events.test.js`
asserts the shell ignores a `cross_ref_enabled` field it is sent, and `tabs.test.js` asserts an
archived `+xref` mode string still renders verbatim, since old transcripts contain them.

### Tests

| File | Tests | Note |
|---|---|---|
| `test_claude_code_mcp_server.py` | 42 | new — schemas, rendering, chunking, readiness, flush ordering |
| `test_claude_code_hooks.py` | 28 | new — debounce, drain, hook shape, degradation |
| `test_claude_code_service.py` | 202 | +`TestBridgeWiring`, `TestIndexReadiness`, `TestUiStateSnapshot`, `TestReindexReporting` |
| `test_symbol_index_orchestrator.py` | 53 | +`TestReindexFiles`, `TestResolveIndexedPath`, `TestNameQueries` |
| `test_claude_code_permissions.py` | 139 | +`TestOurOwnToolsAreUngated` (6) |

**2 687 passing, 75 skipped** in the backend suite; **3 185 passing** across 88 files in the webapp.

Two of those tests exist because a red test turned out to be a real defect rather than a bad assertion:

- **`test_a_flush_does_not_abandon_the_batch_a_drain_is_holding`.** `flush()` cancelled the debounce
  timer, and the drain was awaited *inside* that timer's task — so cancelling it aborted a rebuild that
  had already taken its batch off the queue. `flush()` then returned believing the index was fresh,
  over an index missing those files, with `_pending` already cleared. Silent, and exactly the failure
  the flush exists to prevent. Fixed by spawning the drain as a task of its own (`_spawn_drain`), so
  the timer only ever sleeps.
- **`test_the_debounced_path_survives_a_broken_drain`.** Nobody awaits a debounced drain, so nobody
  would see it raise. `_drain_quietly` logs and keeps the reindexer usable.

**The bridge's fake index records what it was asked for, not just what it returned.**
`FakeSymbolIndex._render` appends every `exclude_files` set it receives, and
`test_it_excludes_against_the_whole_index_not_the_scope` asserts on that list
(`symbols.exclusions[-1] == {"src/b.py", "webapp/c.js"}`). That is what caught the scoping bug: the
rendered output of a scoped map looks correct whether the exclusion set was computed against the whole
index or against the chunk, and only the *argument* distinguishes them. Asserting on the return value
alone would have passed. The real-index coverage lives next door in `test_symbol_index_orchestrator.py`,
where `TestReindexFiles` builds a tree under `tmp_path` and re-indexes it.

### Live verification

`scripts/bridge_smoke.py` — new, alongside `engine_smoke.py`, in `scripts/` for the same reasons: it
costs tokens and needs a login. It builds the symbol index the way `main.py` does (resolver seeded
before per-file indexing, then call-site resolution, then the reference graph — get that order wrong
and every import resolves to `None`), wires the real `PermissionBroker`, the real hook matchers, and
the bridge, then runs one turn.

Four runs against CLI 2.1.229. Three pass; the fourth is the one that found the gate bug, and it ran
first as a failure:

- **`--no-docs`** (352 files indexed of 447): the model called `mcp__ac-dc__symbol_map` with
  `{'path_prefix': 'src/ac_dc/claude_code'}` and named `permissions.py` as the module holding the
  permission gate, from the map alone. It also reported the chunk boundary rather than treating an
  unseen file as absent.
- **`--tool doc_outline`**: summarised all seven documents in `specs5/plan/` — their headings,
  keywords, line counts and outbound links — without opening a file. It read the phase table well
  enough to state that three phases were logged complete and phase 4 handed off.
- **`--write`, first attempt — failed.** The hook half worked: `files_reindexed` came back
  `['scratch_bridge_smoke.py']`. But the tool call itself was refused — *"Claude requested permissions
  to use mcp__ac-dc__file_symbols, but you haven't granted it yet"* — and the agent said so plainly
  instead of guessing, which is the only reason it was legible. Two bugs behind one symptom: the gate
  did not ungate our tools, and the script passed no `can_use_tool` at all, so the *CLI* was answering
  and the script would have logged a real denial as a model choice. Both fixed; the script now wires
  the real `PermissionBroker` and fails loudly if one of our tools opens a dialog.
- **`--write`, after the fix**: `Write` → `PostToolUse` → debounced re-index → `flush()` →
  `file_symbols` reporting `f smoke_marker:1()` for a file that did not exist when the index was built.
  `files_reindexed` came back as `['scratch_bridge_smoke.py']`, resolved from the absolute path the CLI
  reported. The whole freshness chain, end to end, in one turn — and no dialog.

**The Monaco half of the exit criterion is not live-verified.** `lsp_get_hover`, `lsp_get_definition`
and `lsp_get_references` needed no re-pointing — phase 3 re-homed them onto `ClaudeCodeService` reading
`self.symbol_index` directly, and they deliberately bypass the readiness gate the map tools respect, so
a partial index still answers hovers. That is tested but not clicked. **Open the app and hover a
symbol before trusting this phase.**

Two facts from the live runs worth knowing:

- **`get_mcp_status` does not list an in-process SDK server.** It reported only the user's
  `chrome-devtools` while our six tools were being called successfully in the same turn. The smoke
  script's status line is context, not the registration check its comment used to claim; what proves
  registration is a `mcp__ac-dc__*` call happening at all.
- **The `$CLAUDE_CODE_USE_BEDROCK` warning fires on a machine with a subscription login.**
  [R-9](risks.md#r-9--authentication-conflict-silently-redirects-the-session)'s tripwire, working: the
  environment redirects the CLI to a gateway while `~/.claude/.credentials.json` exists. Worth knowing
  before reading a cost number from any run here. (This entry originally cited R-10, which is subagent
  transcript volume; corrected when phase 5's live verification found the environment unchanged.)

### Deviations from `inventory.md`

- **`hooks.py` subscribes to one event of the seven the inventory lists.** `inventory.md:100` names
  `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PreCompact`, `Stop`, `SubagentStart` and
  `SubagentStop` as "handlers that drive UI broadcasts and re-indexing". Only `PostToolUse` shipped,
  because it is the only one this phase needs and every extra subscription is a place a hook can shadow
  `can_use_tool` or slow a tool call down. `PreToolUse` is *deliberately* unsubscribed —
  `test_pretooluse_is_not_subscribed_at_all` pins that, since a `PreToolUse` handler is the one that can
  return a `permissionDecision` and silently replace the gate. The other five are UI broadcasts the
  message pump already covers or phase-5/6 work.
- **Bash-driven writes are not re-indexed.** The hook watches `Write`, `Edit`, `MultiEdit` and
  `NotebookEdit`. A `sed -i` or a `git checkout` through `Bash` changes files the index will not hear
  about until the next full build. Hooking `Bash` would mean re-indexing after every `ls`, and the tool
  input is not reliably parseable into "which files did this touch" — the alternatives are a filesystem
  watcher or nothing, and nothing is what shipped. **This is the phase's largest known hole.**
- **The frontend cross-reference toggle has no inventory row of its own.** `inventory.md:142` says only
  "`app-shell/mode.js` — mode toggle becomes a preset selector (CC-12)", and cross-reference appears
  only in the two backend DELETE rows (`llm/_rpc_state.py:41`, `llm/_stability.py:42`) that went in
  phase 3. So the frontend half was left implicit, and reading the inventory alone would have you
  adapt it alongside the mode toggle. It was deleted instead — see above.
- **`messages.py`'s per-card `files_modified` inference is unchanged.** The re-index reports
  `files_reindexed` on the turn footer as a separate fact. Two sources for "what changed" sounds like
  one too many, but they answer different questions — the card attributes a write to a tool call, the
  footer says which of those writes refreshed an index — and collapsing them would lose the
  attribution.

### Deliberately not built

- **No `Bash` write detection.** Above.
- **No banner when the bridge fails to start.** `mcp-bridge.md` § Availability says the session
  continues without it "and a banner reports the loss — otherwise the agent simply appears inexplicably
  worse at repo-wide questions", which is exactly right and is exactly what is missing. What shipped is
  degradation without announcement: if `build_server()` raises, the session connects with
  `mcp_servers=None`, the hooks stay wired, and a log line says the agent will fall back to
  Glob/Grep/Read. Nothing reaches the browser. Two service tests pin the degradation itself
  (`McpBridge.build_server` raising leaves hooks intact; `build_hook_matchers` raising leaves servers
  intact), so the failure is survivable and silent rather than fatal and silent — but a user watching
  the agent grep its way around a repo will not know why. **Closed in phase 6**: each loss is recorded
  as a sentence on `EngineHealth.degradations` where it happens, and the health banner renders it beside
  the version and credential warnings. The same two tests now assert the sentence, not just the log.
- **No token-cost display for the tool inventory.** `mcp-bridge.md` also wants server health and the
  `ac-dc` tool inventory with its token cost in the Context tab. The tools are registered and callable;
  the panel does not mention them. Phase 6 territory. The banner above is the same missing surface seen
  from the other side. It also inherited phase 3's gap that neither context panel has a unit test —
  that half has since been pulled forward to sit ahead of phase 5, because phase 5 adds *session load*
  as a second refresh trigger into panels nobody has watched work.
- **No `symbol_map` in the Context tab's cost breakdown.** Same reason.
- **The mode toggle and agent tab strip are still mounted and inert.** CC-12 and CC-8, unchanged from
  phase 3.
- **`<ac-history-browser>` is still mounted and inert.** Phase 5, unchanged since phase 2.

### For whoever picks up phase 5

- **Phase 5 is history, and `test_phase_five_methods_are_absent` is still there.** `history_list`,
  `history_load` and `history_delete` are asserted absent on phase 1's reasoning that a stub reporting
  success is worse than a missing method. Delete that test as you build them, not before.
- **The transcript is where three phase-3 deviations converge.** Review entry and exit, a mode change
  during review, and a permission-mode change all claim to be "recorded in the transcript as a system
  event" in the specs, and all three only broadcast live. Phase 3 listed them together so you would
  find all three rather than one.
- **`Reindexer` is the only thing that knows what the agent wrote.** If the transcript wants a
  "files changed this turn" record that survives a reload, `take_reindexed()` is the honest source for
  the index half and `result['files_modified']` for the CLI's half. They disagree by design: the first
  is repo-relative and filtered to files an index cares about, the second is absolute and everything.
- **Neither of those two is "files changed", and the persisted field must not say it is** —
  [`decisions.md#cc-18`](decisions.md). Where they disagree is documented above; where they *agree* is
  the trap: both miss `Bash`. `take_reindexed()` misses it because the hook never fires for `Bash`, and
  `result['files_modified']` misses it because `messages.py:62` gates `_files_modified` on the same
  four-tool `_FILE_WRITING_TOOLS` map — whose docstring already calls input-attribution "a stopgap".
  Name the field for what it holds (`files_written_by_file_tools` or equivalent). A wrong live
  broadcast dies at reload; a wrong field in `.ac-dc4/` is what the history browser and full-text
  search show until someone migrates every transcript users have accumulated. Phase 8 may later make
  the narrow name obsolete — that is a cheap problem, and the reverse is not.
- **Before you start, the two context panels need tests and one live run.** They refresh on *a turn
  runs* and *a session loads*, and you are building session loading. See the README's status section;
  this is deliberately not phase-5 scope, it is phase 3 work that phase 5 would otherwise inherit the
  blame for. **Done — 2026-08-16, in the interlude below.** Two of its findings bind phase 5: session-load
  fetches must opt into `withRpcTimeout`, and the tab-visibility contract is `onTabVisible`, not a
  guess.
- **Nothing in the config layer may write `os.environ`**, and **hooks must never return a
  `permissionDecision`.** The second is new with this phase and is the sharper of the two: a
  `PostToolUse` hook returning one shadows `can_use_tool` entirely, ungating every gated tool with no
  error anywhere. `build_hook_matchers` returns observation only, and
  `test_claude_code_hooks.py` pins that the returned dict never carries a decision.
- **`can_use_tool` now has an early return before any dialog is built.** Anything you add to the front
  of that method runs after it for `mcp__ac-dc__*` calls and before it for everything else. If a future
  server is added to the bridge, it is ungated by the same line — which is correct only as long as
  every tool on it is genuinely read-only and in-process.
- **`collab.py`'s `ContextVar` fix and its five `TestGateUnderRealDispatch` tests survive**, unchanged
  and still load-bearing.

---

## Interlude — the context panels meet a live CLI (2026-08-16)

Not a phase. The README sequenced this ahead of phase 5 for one reason: `context-usage-tab.js` and
`usage-hud.js` refresh on *a turn runs* and *a session loads*, phase 5 builds session loading, and
neither panel had a test or a live run. The bet was that phase 5 would otherwise inherit the blame for
whatever was already wrong. Five things were.

### Five wrong numbers, and why 3 371 green tests said nothing

Phase 3 shipped the two panels and the shell's capacity bar against a *guessed* model of
`ContextUsageResponse`. Each of the three views derived the arithmetic independently, so each was wrong
on its own terms — and the fixtures were written from the same guess, so the suite agreed with it. This
is the failure mode a fixture cannot catch by construction: the test asserts the guess.

What the engine actually reports, provable from three identities it maintains:

- the content categories sum to `totalTokens`
- `Free space` is `autoCompactThreshold - totalTokens`
- `Autocompact buffer` is `maxTokens - autoCompactThreshold`

So the non-deferred categories tile the whole window, `maxTokens` equals `rawMaxTokens` (200 000), and
the compaction point is a separate field (167 000). Five consequences, every one of them visible on
screen:

1. **`categories[].color` carries the CLI's theme token names** — `claude`, `promptBorder`,
   `inactive`, `warning` — not CSS. Every bar segment and legend swatch in both panels rendered
   transparent.
2. **Shares divided by `totalTokens`**, which gave `Free space — 692.0%`. The denominator is
   `maxTokens`; the column is now headed `Share of window` and the shares sum to 100 %.
3. **The bar segmented by every non-deferred category**, so it sat permanently 100 % full with 73 % of
   it labelled "Free space" — a capacity bar that cannot show capacity. Structural rows are room left,
   not content, and are excluded.
4. **`maxTokens` was believed to arrive pre-reduced by the autocompact buffer.** It does not, at seven
   sites. That made the `rawMaxTokens > maxTokens` tooltip branches dead in both panels — the one thing
   they existed to explain was the one thing they never said — and put the >90 % red band out of reach
   in all three views: at the moment of compaction the bar read **84 %, in green**, the single reading
   it exists to rule out.
5. **`MCP tools — 0 loaded` above a table of 35 tools.** All 35 are deferred, so "0 loaded" was true
   and unreadable; it now names the count, the loaded tokens and the deferred tokens.

Also cosmetic and also live-only: the engine names some rows `System tools (deferred)` *and* flags them,
which rendered as `System tools (deferred) (deferred)`.

### The arithmetic moved to one place

`context-usage.js` (242 lines) now holds the derivation the three views each got wrong.
`partitionCategories` checks the sum identity rather than trusting its own name matching, and an
unverified payload degrades to an unsegmented bar instead of a confident wrong one. `warningPercent`
puts the warning colour on the figure that predicts the pause *and* on the number being looked at — the
HUD had been printing one basis and colouring by another.

The three views' fixtures are rebased onto a verbatim live capture, with a `usageAt(totalTokens)`
helper that rebuilds the structural rows so the identities keep holding as a test moves the number.
Confirmed live afterwards: `memoryFiles` is `{path, type, tokens}` and `mcpTools` is
`{name, serverName, tokens, isLoaded}`. **`agents` came back empty, so its element shape is still
unverified** — the one shape in the payload still resting on a guess.

### A dropped reply wedges every guarded fetch

Found in the same run, and the wider bug of the two. A jrpc-oo call issued while the socket is being
replaced — a reload mid-reconnect is the reliable way to see it — is dropped without a reply, and its
promise then neither resolves nor rejects. Survivable alone. What is not is the `if (inFlight) return;`
guard nearly every fetch in the webapp uses: the flag clears in a `finally`, the `finally` never runs,
and the component stops fetching for the rest of the session. The HUD's context section went
permanently blank, and the Context tab's Refresh button — the one affordance that would retry — is
disabled in exactly that state, because `_loading` both blocks the fetch and greys the button.

`withRpcTimeout` (`rpc.js`) rejects at a deadline so the `finally` runs. **Opt-in, not folded into
`rpcCall`:** some calls legitimately run for minutes — a document conversion, an index rebuild — and a
blanket deadline would break them. Nothing cancels the underlying call, since jrpc-oo cannot, so a late
reply is ignored.

Pick the deadline *above* whatever deadline the backend method already has. Every `ClaudeCodeService`
method converts its own failures into an `{error}` return, so the backend always replies; a shorter
deadline here abandons a reply that is still coming and stacks a retry onto whatever was too slow the
first time. The remaining case — no reply at all — is the only one this helper is for. All three
`get_context_usage` callers use 90 s, and that number is not arbitrary: the call measured **3–5 s warm,
14 s on the first fetch after an idle session, and past the SDK's own 60 s control-request deadline
eight times in one half-hour run.** A control request to the CLI subprocess is not a local computation.

### A revealed tab is told it is on screen

`ContextUsageTab.onTabVisible` had no caller. Its docstring said "called by the dialog when this tab
becomes visible"; `_switchTab` set `activeTab` and told nobody. The tab refuses to refetch while hidden
— a breakdown costs a control request — and marks itself stale instead, but it cannot see the class
change that reveals it, so the badge stayed lit until someone pressed Refresh. The one affordance that
clears it was unreachable by the gesture it was written for.

`_switchTab` now notifies the newly-active tab on a microtask after the render that moves `.active`, so
the query finds the tab arriving rather than the one leaving. Deliberately generic: any tab that grows
the hook gets it, tabs without one are untouched.

### A failed turn is not an included turn

The HUD guarded on `result.error` to skip a turn with no numbers. A `streamComplete` result has no
`error` key — the engine flags failure as `is_error`, in `messages.py` `_on_result` and in the synthetic
result `service.py` emits when a turn dies outside the pump, which is what the chat panel reads. **The
guard had never fired.** So a failed turn popped a HUD reading `included · 0.0s`, where "included" is a
claim about subscription billing — the turn cost nothing *extra* — standing in for a cost the engine
never priced. Wrong in the one direction a cost display must not be wrong.

### Tests

No backend change: python stays **2 687 passed, 75 skipped**. Webapp **91 files / 3 380 passed**,
+195 over the phase-4 figure, in four files:

- `context-usage.test.js` — **41**, new. The shared derivation, including the identity check and the
  degrade-to-unsegmented path.
- `context-usage-tab.test.js` — **74**, new. The panel phase 3 shipped untested.
- `usage-hud.test.js` — **68**, new. The auto-hide timers (the HUD is gone by the time anyone looks),
  hover pause and fade-undo, cost formatting at each magnitude including the null case, the model label
  when a subagent used a second model, the bar's exclusion of deferred and empty categories, the
  headroom tooltip, and the `session-changed` path — which refreshes the numbers *without* showing the
  HUD, because popping up on a session load would report a turn that never happened.
- `rpc.test.js` — **+8** for `withRpcTimeout`: rejecting at the deadline, releasing the guard so a
  later fetch succeeds where it used to be locked out, and ignoring a late reply.
- `app-shell/toasts-and-events.test.js` — **+4**: the revealed tab is told, the tab being *hidden* is
  not, a tab with no hook is a no-op, and the stale badge clears on the way in.

The lock file was also re-solved (`chore: re-lock`): phase 3 dropped `boto3`, `litellm`, `tenacity`,
`tiktoken` and `trafilatura` from `pyproject.toml`, and `uv.lock` had pinned all five plus their
transitive closure ever since — 1 165 lines of resolution for packages nothing imports. No dependency
changed.

### Deliberately not built

- **A "cost unknown" rendering.** Hiding a failed turn's cost hides the real usage that a late failure
  carries — `error_max_turns` in particular. Reporting it honestly needs a state distinct from
  "included", which is phase 6's visualisation work and not a guard's job. Noted in the code where the
  guard sits.
- **The rest of the webapp's guarded fetches are still unbounded.** Only the three `get_context_usage`
  callers opt in. The wedge is generic to the `if (inFlight) return;` idiom; the fix is per-call by
  design, because the right deadline is per-call.
- **`agents[]`'s element shape.** Live capture returned it empty. Unverified, and the only part of the
  payload still guessed.
- **No visualisation upgrade.** This entry is correctness. Phase 6 is still phase 6.

### What binds phase 5

- **Session-load fetches must opt into `withRpcTimeout`**, with the deadline above the backend
  method's, never under it. Phase 5 adds the second path into these panels, and it is the path most
  likely to run during a reconnect — which is exactly when a reply gets dropped.
- **The staleness contract is `onTabVisible`.** A session load makes the breakdown stale; the panels
  already know how to say so and already know not to refetch while hidden. Reuse it rather than
  inventing a second mechanism.
- **The HUD must not pop on a session load.** `session-changed` refreshes the numbers and shows
  nothing, because a HUD that appears on resume reports a turn nobody took. If phase 5 introduces
  another way to load a session, it joins that path and not the turn-complete one.

---

## Phase 5 — History and sessions (2026-08-16)

The exit criterion, met: **restarting the server resumes the previous conversation with context
intact, and `session_store_conformance` passes** —
`run_session_store_conformance(make_store, skip_optional=frozenset())`, nothing waived.

Two things had to be true at once for that sentence to mean anything. The transcript on disk has to be
a *record* the browser can read, and the context the model gets has to come from the engine's own
rebuild of it. Phase 5 keeps those separate on purpose: what we render is a record of the session, and
what the model gets is the session. Neither is derived from the other, so they cannot drift — which is
exactly how the previous architecture produced conversations that looked right on screen while the
model's view had diverged.

### One store, and the one it replaced

`session_store.py` (753 lines) implements the SDK's `SessionStore` protocol as `RepoSessionStore`:
`.ac-dc4/sessions/<project>/<session>.jsonl` with a sibling `<session>.summary.json`, folded and
written atomically. Per-key `asyncio.Lock`s, so two appends to one session cannot interleave; every
key component goes through `_safe_component` / `_safe_subpath` and raises `SessionStoreKeyError`,
because the session ID reaching the path builder is minted by the CLI and not by us.

`history_store.py` is **retired, not adopted** (CC-19): 1 148 lines of store and 2 079 lines of its
tests deleted. The reason it could not be a head start is in the plan — `SessionStoreEntry` is a
pass-through blob, so a store cannot impose a record shape, and half its field names described
protocols phase 3 deleted. The file handling was worth reading; the schema was not worth inheriting.

**Images are in the transcript entries that carried them.** No `images/` directory, no content-hash
indirection. A base64 screenshot is bigger on disk than a hash would be, and that cost buys the
property that matters: a transcript is one file, and a session survives being copied out of the repo.
The size consequence is the disk warning, below.

### Reopening is a rebuild, and the append observers say when it was not

`history.py` (1 127 lines) folds raw store entries into turn-shaped messages. `_Turn` reassembles a
turn from its scattered parts — user text, tool calls and their replies interleaved by
`_interleave`, todos, the result — and `render_messages` returns the same block objects a live turn
produces, so the browser consumes a browsed turn through `restoreMessage`: one renderer, not a second
one that agrees with the first until it doesn't. Compaction dividers, elapsed times and event cards
(`_event_message`) are folded in on the same sort key.

`resume_session(session_id, fork=False)` renders the transcript for the browser and hands the *same
session ID* to `connect`, and the SDK's `resume` / `fork_session` parameters do the rest. Which
session a connect attaches to is decided **inside** the connect lock, read from a held
`_resume_request` rather than passed as an argument: a click on "resume" and a first turn arriving
together would otherwise race, and the turn would win with the auto-resume default — the user asks for
an old session and gets the newest one.

Auto-resume needs no pointer file. The store sorts by `last_modified`, so the newest session *is* the
one we were in, and a pointer would be one more thing that can disagree with the transcripts it names.
`new_session` is the only thing that turns auto-resume off, and it turns back on after the next
connect, so a session lost mid-conversation reattaches to itself instead of quietly continuing as a
blank one. The startup spec's step for pre-loading history before the WebSocket server starts is
**gone**: `get_current_state` reads the transcript on demand, which gives the same guarantee without a
startup ordering constraint to get wrong.

A mirror gap — an append the store could not write — is `add_append_observer`'s job, and it is said at
two scales. The affected turn carries a footer marker; the session carries a running count in engine
health. Both were specified in phase 1 and neither had a reader until this phase.

### The events log had one writer and six events

`events_log.py` (347 lines), append-only at `.ac-dc4/events.jsonl`, with `EVENT_TYPES` a **closed**
seven-member set: a typo'd discriminator writes a record the browser has no renderer for, which reads
as "the event never happened" rather than as a bug. `id` and `timestamp` come from one clock call so
they cannot disagree.

This closes the three phase-3 deviations that converged here — review entry and exit, a mode change
during review, and a permission-mode change, all specified as "recorded in the transcript as a system
event" and all three only broadcast live. They are records now, and they render as system-event cards
in a browsed session.

`files_written_by_file_tools` keeps CC-18's narrow name. A wrong live broadcast dies at reload; a
wrong field name in `.ac-dc4/` is what the browser shows until somebody migrates every transcript
users have accumulated.

### The browser was a reader for an engine that no longer existed

`history-browser.js` went 1 356 → 2 233 lines. Every button in the modal called `LLMService.history_*`
— a name with nothing behind it since phase 3 — and its record-shape assumptions were the deleted
engine's. Seven RPCs now stand behind it: `history_list`, `history_load`, `history_search`,
`history_delete`, `history_image`, `get_subagent_transcript`, `resume_session`.

The pieces that took more than re-pointing:

- **Search that a cold index cannot answer differently.** `history_index.py` (563 lines) is CC-19's
  derived index: token postings under `.ac-dc4/`, `INDEX_VERSION`-stamped, deletable and rebuildable
  from the transcripts. It **narrows which sessions to read and never decides a hit** — every match is
  confirmed against the transcript text — so a warm index and a cold one return the same rows, and the
  index can go stale without being able to disagree. `role` narrows to user, assistant or *tool*, the
  new third value: the searches this serves best are for a path or a command the agent used. Tool
  *results* are not searched at all; that is what `Grep` is for.
- **Delete, and the one session it refuses** — the live one. Deleting the conversation you are in
  leaves an engine attached to a transcript that is not there.
- **Screenshots in a browsed prompt.** `history_image` and `image-refs.js` (117 lines, new): the
  transcript holds the image, and a listing that dropped it showed a prompt that reads as though the
  user described a screenshot rather than pasted one.
- **Subagents from a browsed session.** `list_subagents` / `load_subagent`, opened into read-only
  tabs keyed `historical:<agent_id>` with a 📜 label marker and no input surface.
- **Resume is not load.** The confirmation arms on `liveUnread` — a resume that swaps the conversation
  out while an unread live reply is on screen is the one destructive thing in the modal, so the button
  asks a second time in amber and only then.

### The two warnings that were delivered to nobody

Both were specified, both had their plumbing built in phase 1, and neither reached a human.

**The disk warning.** The 1 GiB session-directory threshold had nobody watching it; it is now a
transcript system-event card, read from both carriers so it survives a reload.

**The health banner.** Four specs routed to it — `engineHealth`'s row in chat.md said "the health
banner owns this", and the turn footer's mirror-gap marker "links to the health banner" — and it did
not exist. `panel._engineHealth` was being stashed for a reader that was never written. Worse,
`onEngineHealth` expected a `{requestId, data}` envelope that session-wide events do not carry, so the
live event had never landed at all; only the state snapshot ever set the field, and nothing tested it.
The banner sits beside the disconnected note (both are standing conditions about the channel, not
events in the conversation), is amber while the conversation still works, and its dismissal is keyed to
*which* problems are showing so a read warning stays quiet and a new one speaks. `connected: false` is
not a fault: it is the normal state of a freshly loaded page.

> **Correction to the phase-2 entry.** "No health-banner link target. The banner renders and its link
> goes nowhere" was wrong in a way worth naming: nothing rendered. The link had no target because the
> banner did not exist. Closed now, and the marker *forces* the banner open rather than un-dismissing
> it, so the click always lands somewhere — including on "the engine reports nothing wrong", which is
> the honest answer after a restart.

### The two thresholds the mirror is judged by

`app.json` gains the `history` section configuration.md always specified: the session-directory warning
threshold and how many mirror-append failures are tolerated before the banner escalates. Both were
hardcoded; the gap count in particular was compared against nothing, so three failed appends and
thirty read as the same amber sentence.

The comparison lives on `EngineHealth` — one owner for the rule, the same placement as the disk
warning's one-shot — and reaches the browser as `mirror_gaps_escalated`, a verdict rather than a
threshold to re-compare. The tolerance arrives as a `Callable[[], int]` because `reload_app_config()`
hot-reloads the file and never notifies the service, so a value read at construction would be pinned
to whatever was on disk at startup. Floors differ per key on purpose: bytes at least 1, because zero is
a silenced warning; tolerance at least 0, because zero tolerated failures is a legitimate answer.

### Tests

Python **2 906 passed, 75 skipped** (+219 over phase 4, net of the 2 079 lines that went with
`history_store.py`). Webapp **92 files / 3 526 passed** (+146 over the interlude).

- `test_claude_code_session_store.py` — **new**. Conformance first, with `skip_optional` empty, then
  the concurrency, key-safety and atomic-write behaviour the protocol does not cover.
- `test_claude_code_history.py` — **new**, 979 lines. Turn folding, interleaving, compaction dividers,
  event cards, image refs, subagent listing and loading, and delete.
- `test_claude_code_events_log.py` — **new**. The closed discriminator, malformed-line tolerance,
  per-session delete and rewrite.
- `test_claude_code_service.py` — **350** total, of which **39** cover search and the derived index
  (including the cold-versus-warm equivalence). Also the seven history RPCs, `resume_session`'s lock
  ordering and its refusals, auto-resume and what `new_session` turns off, and the disk warning.
  `history_index.py` has **no test file of its own** — it is exercised through the RPC that uses it,
  which is thinner coverage than the store or the log got.
- `history-browser.test.js` — **139**. `chat-panel/input.test.js` — **163**.
  `chat-panel/events.test.js` — **76**. `view-subagents.test.js` — **12** and
  `view-subagents-load.test.js` — **26**, replacing the two `view-agents*` files phase 3's naming left
  behind. `health-banner.test.js` — **28**, new.

### Live verification — not done for this phase

No live CLI run. The exit criterion was verified against the conformance suite and the unit tests, not
against a restarted server with a real conversation behind it. Given what the interlude found the last
time a phase's numbers met a live engine, treat this as the open risk of the phase and not as a
formality — the shapes most worth doubting are the ones the store never chose: what the CLI actually
writes into a `SessionStoreEntry` blob across SDK versions.

### Deliberately not built

- **No rewind UI.** `rewind_files()` is still on the service and still has no caller. Unchanged from
  phase 2, and still not this phase's job.
- **Subagent attribution is still by id, not by name.** A `historical:<agent_id>` tab is labelled with
  the id. Mapping it to the agent's definition name is unchanged from phase 3.
- **No per-turn subagent affordance in a browsed session.** A browsed turn's `subagents` is empty, so
  the way into a subagent transcript is the session-level listing. The per-turn chip stays a
  live-run-only affordance.
- **Search is substring, not semantic.** It answers from the transcripts, which is what makes it
  answer the same cold as warm.
- **Three App Config sections configuration.md specifies and nothing implements.** `Indexing` — the
  re-index debounce and the pending-flush ceiling are `hooks.py`'s `DEBOUNCE_SECONDS = 0.6` and
  `MAX_FLUSH_ROUNDS = 2`, phase 4's to have wired. `Permissions` — `NO_LOCALHOST_TIMEOUT = 30.0` and
  `PRESENCE_POLL_SECONDS = 2.0` in `permissions.py`, phase 2's. `Presets` is deferred by decision
  (CC-12), the other two by omission. The `history` section is the pattern to follow: a callable
  provider so a hot reload takes, and a floor per key.
- **No Settings engine-health card.** `settings.md:83` specifies one. The banner is the surface health
  reaches now.

### For whoever picks up phase 6

- **The health payload's MCP server list is unrendered, on purpose.** The banner leaves it out because
  a per-server status wants the Context tab's room. The bridge-failure banner is phase 6's, and the
  data is already in the payload.
- **A browsed session goes through `restoreMessage`.** Anything phase 6 adds to a live turn's
  rendering has to survive arriving from `render_messages` too, or a resumed conversation loses the
  visualisation the phase exists to add.
- **`session-changed` is now a real, frequent event.** Auto-resume fires it on every server start.
  The interlude's rule holds: refresh the numbers, show no HUD.
- **Escalation is a verdict, not a threshold.** If phase 6 grows a second view of mirror health, read
  `mirror_gaps_escalated`; do not re-derive it from a count and a config value.

---

## Interlude — the store that stopped the engine (2026-08-16)

Phase 5 closed with **live verification not done**, named as the open risk of the phase. The risk paid
out on the first attempt to start the app: no session at all.

```
Could not start a Claude Code session: session_store cannot be combined with
enable_file_checkpointing (checkpoints are local-disk only and would diverge
from the mirrored transcript)
```

### 2 906 green tests and an engine that could not connect

The SDK validates that pair in `ClaudeSDKClient.connect()` and again in `query()`. AC⚡DC had set
`enable_file_checkpointing=True` unconditionally since phase 1 and it was harmless for four phases,
because **nothing constructed a store** — `build_option_kwargs` only adds `session_store` when it is
given one, and until phase 5 nobody was. The moment `_build_session_store()` started returning a
`RepoSessionStore`, every connect in every repo raised.

Why the suite said nothing is the part worth keeping. `test_claude_code_options.py` is the SDK-drift
tripwire and it asserted the right things about the wrong subject: that every key we set exists on the
installed dataclass, and that checkpointing ships with its `--replay-user-messages` partner. Both were
true. Neither is *validity* — the SDK's own opinion about combinations lives in
`_internal/session_store_validation.py`, which the tripwire never called. A contract test against a
dataclass's field names cannot see a rule about two of its fields together.

Two tests replace that gap, and they point in opposite directions on purpose:

- `test_a_mirrored_session_passes_the_sdks_own_validation` runs the SDK's validator over the options we
  actually build, with a store. This is the connect the user could not get.
- `test_the_sdk_still_refuses_the_pair` asserts the constraint *exists*. When the SDK learns to
  checkpoint alongside a store, that test fails — which is the notification that undo can come back.

Both `importorskip` the private module rather than pretending it is public API. Nine tests in total —
the two above, the options assembly's three, three on the session's `file_checkpointing`, and one on the
RPC refusal — take Python to **2 915 passed, 75 skipped**.

### Which one loses, and the answer was not close

[CC-20](decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job) records it. The
store is `.ac-dc4/` history, resume-after-restart, the session browser and the derived index —
everything phase 5 shipped, and its absence reads as data loss days later when the CLI's own retention
window expires. Checkpointing was one control that has never had a caller, over changes git already
tracks. `options.py` now sets checkpointing and the replay flag only when there is no store, which
means only a repoless run: undo is not a reason to refuse anybody a session.

The loss is not silent. Connecting with a mirror logs what went with it, `rewind_files()` is refused at
the RPC with a message that names git instead of letting the SDK raise about local disks, and `/rewind`
no longer claims an affordance that was never built.

### Live verification — done this time

`EngineSession` with a real `RepoSessionStore`, connect and disconnect against the bundled CLI 2.1.229:
session ready, `file_checkpointing` False, clean disconnect. Phase 5's own exit criterion — a restarted
server resuming the previous conversation — still has not been run live, and this is the second entry in
this log to say that a phase's green tests met a live CLI and lost.

---

## Interlude — the exit criterion, and the preview every session shared (2026-08-16)

Phase 5's exit criterion was finally run against a live CLI, and it passes. Six checks: the CLI child
carries `--resume=1d53df67-39aa-4d04-894a-14a53f7f6d2a`; a question about the *earlier* conversation is
answered with no tool calls at all, so the answer came from the model's rebuilt context and not from
anything we replayed; the resumed turns land in the same `.jsonl` with no fork; the HUD stays closed on
`session-changed`; and SIGINT leaves no `_bundled/claude` survivors and no zombies.

The restart had to be driven from **outside** the app, by a terminal `claude` CLI agent with
chrome-devtools MCP. The reason is structural and worth writing down: the process under test is the one
hosting the engine that would be doing the testing. There is no way to verify "the server restarts and
resumes" from inside the server — killing it kills the turn making the observation. Any future check of
a shutdown path needs a second agent, not a cleverer test.

### Every session row said the same thing, and 2 915 green tests agreed

The bug the live run exposed was on screen the whole time. The session list showed, for **every** row,
the same 100 characters of AC⚡DC's own `<ac-dc-ui-context>` prose — and `session-preview` is the only
field that distinguishes one row from another. A history browser where every entry is identical.

Three things had to line up, and they did:

- The CLI truncates the sidecar's `first_prompt` to **exactly 200 characters**, with an ellipsis. Our
  framing block is longer than that, so the truncation lands *inside* it and `</ac-dc-ui-context>` never
  appears in the field.
- `strip_framing` needs that closing tag. Without it, it takes its "opened and never closed" branch and
  returns the text unchanged — correct behaviour, on input it was never given a way to fix.
- `summarise_session` did `info.first_prompt or info.summary` and never called `strip_framing` at all.

The last one is the actual defect, and the live sidecar shows how avoidable it was:
`info.first_prompt` was 200 characters of framing, while `info.summary` — sitting right there as the
second operand — was `'Determine next phase after phase 5'`. The CLI had already written a good
one-line title from its `ai_title` field. We preferred the truncated boilerplate over it. And
`first_prompt_locked` is `true` in the sidecar, so the field never improves on its own.

The preview now reads the *parsed messages* first, through the same parser the rest of the module reads
through, where the whole prompt is present and the framing can be stripped; then the CLI's title; then
the truncated field last rather than never, because something session-specific beats `(empty)`.

Why the suite said nothing is the same shape as the last two interludes: **every fixture's framed prompt
was short enough for the closing tag to survive the truncation**. The tests exercised
`strip_framing`'s happy branch exclusively, so they agreed with the bug. The seven new tests in
`TestThePreviewIsWhatTheUserTyped` are built on the real 200-character truncation, copied verbatim off
the live sidecar, and one of them asserts the CLI's title wins when the transcript will not parse.

`INDEX_VERSION` goes 1 → 2 with it. The derived index caches the *finished row*, `preview` included, so
without a bump the old boilerplate would have outlived the fix on every machine that already had a
cache.

### 73 raw entries, 51 parser messages, 4 rendered turns

Read against a real CLI-written blob rather than a fixture, the fold holds. Fifteen tool calls
correlated to their results, `num_turns: 12`, per-model usage with cache-read and cache-creation tokens,
`duration_ms: 231250` — footers reconstructed because the CLI writes one entry per content block and no
result entry exists to copy. `terminalReason` and cost come back **absent rather than guessed**, which
is the behaviour `formatCost` exists to protect.

Four entry types the SDK's parser drops account for the 73 → 51: `ai-title`, `attachment`,
`queue-operation`, `last-prompt`. All four are correctly dropped — the `attachment` here was a
`deferred_tools_delta`, not user content, so nothing the human wrote or saw is lost in the gap.

### Resuming redirects the CLI's own store, and that has a consequence

This looked like a defect and is not. `subprocess_cli.py` appends `--session-mirror` whenever a store is
set, and `materialize_resume_session` copies the store's session into a temp `CLAUDE_CONFIG_DIR` laid
out like `~/.claude/` and points the subprocess there. Confirmed live: both `_bundled/claude` pids carry
`CLAUDE_CONFIG_DIR=/tmp/claude-resume-<suffix>`.

The consequence belongs in the spec, not just here. **Once AC⚡DC resumes a session,
`~/.claude/projects/…/<id>.jsonl` is frozen at the moment of resume** — permanently, and a terminal
`claude --resume` on that id sees a stale conversation ending mid-restart. This sharpens the mirror-gap
warning from a nicety into the thing it is: before a resume, a gap in our mirror was survivable because
the CLI kept its own copy. After one, our mirror is the *only* record of every turn that follows, and a
gap marker is a report of data that exists nowhere.

### The temp directory the signal handler never removes

Chasing the above surfaced a real leak, and it is one neither phase owns alone. The SDK's contract is
that `MaterializedResume.cleanup()` removes the temp config dir, reached from
`ClaudeSDKClient.disconnect()`. Our graceful path honours it — `EngineSession.disconnect()` →
`_quiet_disconnect(client)`. But phase 2's `_signal_handler` exits via `os._exit`, documented right
there in `_kill_cli_children` as the reason the SDK's `atexit` child guard never fires for us. It skips
this cleanup for exactly the same reason.

Phase 2's `os._exit` and phase 5's auto-resume compose into it: auto-resume makes **every** start after
the first a resume, so every launch materialises a directory and every Ctrl-C abandons one. Measured
after two restarts: `/tmp/claude-resume-fatfygyc`, 900 K, orphaned by the verification's own SIGINT,
alongside the live one at 1.1 M.

Each holds a full transcript copy and a `.credentials.json`. Calibrating that honestly, because the SDK
did its part: `refreshToken` is **deliberately redacted** by `_write_redacted_credentials` (a
single-use token spent under a redirected config dir would revoke the parent's own credentials), and
the directory is `0700` with the file `0600`, so this is not exposure to anyone else on the machine.
What remains is a live `accessToken`, valid until its `expiresAt` — 6.7 hours out on the orphan found
here — accumulating one copy per launch cycle and surviving until the next reboot. Hygiene, not a
breach, and ours to fix.

`resume_cleanup.py` is the fix. `remember(client)` records the path after a successful connect and
`purge()` removes it from the signal handler, after `_kill_cli_children` and never before — the
directory is the live `CLAUDE_CONFIG_DIR` of the children being killed, and pulling it out from under a
CLI still flushing its transcript would trade a disk leak for a write error on the way out.

Two choices in it are worth the ink. It is **registered, not discovered**: sweeping the temp dir for
the `claude-resume-` prefix would also match the live directory of another AC⚡DC or a plain `claude`
running alongside, and deleting that is a worse bug than the leak. And the registry is **not pruned on
a graceful disconnect** — `purge()` removes with `ignore_errors`, so an already-cleaned path costs one
failed `rmtree`, which is cheaper than keeping two sources of truth about which directories still
exist.

### The other thing `os._exit` was skipping: Vite

Chasing the temp dirs turned up two orphan Vite servers on the same machine — 22h40m on port 19001 and
50m on 19000, both reparented to systemd, both still bound. The comment in the signal handler said
"vite shuts itself down once the parent dies". It does not, and this is the fourth observation of that:
sending SIGTERM to a running server orphaned a third one on demand, in front of us.

The cause is one level of indirection. We launch `npx vite`, and `npx` is a chain —
`npm exec vite` → `sh -c "vite"` → `node …/vite`. `Popen` holds the pid of the *wrapper*, so
`terminate()` signalled the top of the chain and the `node` process holding the port survived. Ctrl-C
at a terminal usually hid it, because the shell signals the whole foreground process group and reaches
`node` that way — so the leak only appeared when the server ended by any other route, which is exactly
what a supervisor, an IDE stop button or a `kill` does.

`start_new_session=True` at launch puts the chain in its own process group and `_kill_vite` signals the
group. That also takes Vite out of the terminal's foreground group, which means this kill is now the
only thing that stops it — hence the documented fallback to signalling the wrapper when there is no
group to address.

The regression test spawns a real wrapper that ignores SIGTERM and holds a grandchild, so a fix that
only reaches the wrapper cannot pass. Two things about it cost real time and are recorded so the next
person does not re-derive them: **`SIG_IGN` is inherited across `exec`**, unlike a handler, which
resets to the default — ignoring SIGTERM before spawning the grandchild gives it the same immunity and
the test then fails for the wrong reason. And **a zombie answers `kill(pid, 0)`**, which the phase-2
tests already knew for their own children; a *grandchild* leaves nothing we are allowed to reap, so
liveness has to be read from `/proc` instead.

Fourteen tests across the two fixes take Python to **2 940 passed, 75 skipped**.

### Still open after this verification

- **Whether the HUD appears on turn-complete is unverified.** The live check ran past `_AUTO_HIDE_MS`
  plus the fade, so the observation window was gone before anybody looked — reported as inconclusive
  rather than as a pass, which is the right call, and phase 6 will see it incidentally.
- **`$CLAUDE_CODE_USE_BEDROCK` still redirects this machine to a gateway** while a subscription login
  sits in `~/.claude/.credentials.json`. Phase 4 recorded the warning firing and called it the tripwire
  working; two phases later the environment is unchanged, so it is now also a standing caveat on every
  cost number read here, and phase 6 is the phase that renders cost. It is
  [R-9](risks.md#r-9--authentication-conflict-silently-redirects-the-session), not R-10 — the phase-4
  entry cited the wrong number and it is corrected there.
- ~~**The mirror read-path verifier is still a throwaway in `/tmp`.**~~ Promoted to
  `scripts/history_smoke.py`, alongside `engine_smoke.py` and `bridge_smoke.py` and on the same
  argument those two were promoted on. Five checks, `argparse`, a `Report` that runs every check
  before exiting non-zero, and no credentials — it reads the mirror off disk, so it costs nothing to
  run and can gate a phase's sign-off instead of being read by eye. It stays in `scripts/` rather
  than the suite because it needs a real conversation to have happened in the repo, which no fixture
  supplies and no CI job will have.

### The bug the promotion found on its first real run

Moving the verifier was meant to be filing. Its own output disagreed:

```
4. The session-list preview is what the user typed
        1d53df67  'This session is being continued from a previous conversation that ran '
  ok    no framing boilerplate in the preview
```

That check passed. It was still wrong, and wrong in exactly the way it was written to catch — the same
sentence in every row, from a second source. **The SDK's parser starts a compacted session's message
list at the compact boundary**, so the first user message of every compacted session is the CLI's
compaction summary, and that summary opens with a fixed sentence. `_first_prompt` read the messages
faithfully and returned it. The parser's first three user messages, live:

```
parser messages: 329
  user msg #0 (idx   0): 'This session is being continued from a previous conversation that ran out of context. The '
  user msg #1 (idx 102): '<ac-dc-ui-context>\nFiles the user has selected in the file picker (a hint about what they '
  user msg #2 (idx 265): '<ac-dc-ui-context>\nFiles the user has selected in the file picker (a hint about what they '
```

Three things are worth keeping about this.

**Reading the messages was not the fix; reading the *human's* messages was.** The earlier interlude's
conclusion — prefer the parsed transcript over the sidecar's truncated field — was right and did not
go far enough. A transcript contains machine-written user entries too: tool results, which
`_first_prompt` already skipped, and this one, which it did not.

**The entry says so, and the parser does not carry it.** The raw entry is
`{"type": "user", "isCompactSummary": true, ...}`, but a `SessionMessage` exposes only `type`, `uuid`,
`session_id`, `message` and `parent_tool_use_id`, so the flag is gone by the time the fold sees it.
Re-reading the raw entries to recover it would add a third store read per row to a listing whose whole
design is two — so `_COMPACT_PREAMBLE` prefix-matches the CLI's wording instead. That is coupling to a
string the CLI owns, and the honest note is that it can change under us. When it does the row degrades
to `info.summary`, not to a crash: **a session long enough to have compacted has an `ai_title`**, which
is why the fallback is reliable here specifically.

**The fallbacks had the same hole.** Writing the invariant down as a test — *no preview ever opens with
boilerplate* — immediately failed on the case where the sidecar's `first_prompt` is the boilerplate,
because only the first candidate was filtered. Both sidecar fields now go through `_readable`, so the
invariant is true of the preference order rather than of its first branch. `INDEX_VERSION` → 3 for the
same reason it went to 2: the cache holds finished rows, and a stale row is the one thing a user would
still see.

Live after the fix, on the session this file is being written in — which has since compacted again, and
so is the "nothing typed since the boundary" case the tests cover:

```
4. The session-list preview is what the user typed
        1d53df67  'Determine next phase after phase 5'
  ok    no framing boilerplate in the preview
  ok    no compaction preamble in the preview
```

The second `ok` is new. Check 4 printed the bad preview and passed; a human caught it by reading the
output. That is a check doing half its job, so the preamble is asserted now — 2 945 tests, five new
ones built on the real compaction prompt rather than a shortened stand-in, and `history_smoke.py`
exiting 0.

---

## Phase 6 — Context and cost visualisation (2026-08-17)

The exit criterion: **the Context tab shows the designed visualisation over those numbers, names the
`ac-dc` tools it is paying for, and distinguishes a turn that cost nothing extra from one whose cost is
unknown.** Two of those three clauses are met and were read off a live CLI. The third is met in the code
and in 60 tests and is *not* live-verified — the reason is structural and is stated below under *Live
verification*, not buried as a caveat.

The phase divides on which half of it owed a correctness pass. The **context** numbers had already had
theirs, in the interlude: three readers of one RPC, each deriving the arithmetic independently and each
wrong on its own terms, collapsed into `context-usage.js`. The **cost** numbers turned out to owe one
nobody had budgeted for. `total_cost_usd` and `modelUsage` are *session running totals* in a
streaming-input session, which is the only kind AC⚡DC runs, and both readers printed them as one turn's
— so every turn was mispriced upward, monotonically, and the HUD's "This turn · $1.87" was the whole
session's bill. The difference is taken in the engine now (`cost.py`, 207 lines), and the wire carries
`turn_cost_usd`, `turn_cost_basis` and `turn_model_usage`.

### `turn_cost_basis` exists because a missing figure has three different meanings

A null cost is not one state. The specs said it meant a subscription, which was wrong, and `fd3963a`
corrected them. What it actually means is one of:

| Basis | Figure | What the reader is told |
|---|---|---|
| `measured`, difference > 0 | the difference | a price |
| `measured`, difference == 0 | zero | **nothing extra** |
| `reset` | none | **cost unknown** — the session's total went *down*, so this turn's share cannot be separated out |
| `unpriced` | none | **cost unknown** — the engine never priced the turn |
| unrecognised | none | no chip at all |

That last row is the one worth defending. An unknown basis renders *nothing* rather than "unknown",
because a future CLI adding a fifth basis would otherwise make every turn report a problem it does not
have. `turn-cost.js` (254 lines) is the single owner of that table, and `usage-hud.js` and
`block-render.js` both read it rather than each deciding what a null means — the same mistake in the
same shape as the three context readers, caught before it was made twice.

A browsed turn shows **no cost chip at all**. Cost is not in the CLI's transcript, so a replayed footer
has nothing to report, and "unknown" on every one of them would be noise about a thing that was never
recorded. This is a deliberate asymmetry between a live footer and a browsed one, and it is the one
place phase 5's "anything phase 6 adds has to survive `restoreMessage`" is answered with "it doesn't,
on purpose".

### The three review findings, closed

All three were named in the plan as non-blocking and cheap. They were.

**A fetched health record could overwrite a fresher pushed one.** `_ensureDebug`'s rule is that a push
wins over a fetch, because `mirror_gaps` moves during a turn and the fetch is seconds wide; the guard
only covered a fetch that answered *nothing*. A `_healthSeq` counter, captured before the `await` and
compared after, closes the case where a push lands mid-flight and the older server snapshot lands on top
of it. The test gates the fetch on a promise it releases only after pushing a fresher record, so it
fails against the old code rather than passing by timing.

**The initialize reply now has its own heading.** It rendered inside Engine, under one `<h3>`, and the
whole point of the distinction is provenance: the binary resolution is a fact *we* resolved, the reply is
what the engine says about itself. Two tables under one heading lose exactly the distinction a diagnosis
needs. Debug is five sections now, not four.

**The autocompact mark is no longer clipped.** `.mark` sets `top: -1px; bottom: -1px` and a `box-shadow`
ring; `.bar` sets `overflow: hidden`. The overhang the tick is drawn for was being cut off in both
files. A `.bar-wrap` with `position: relative` holds the mark as a sibling of the bar rather than a
child, in `usage-hud.js` and `context-usage-tab.js` together — the bug was pre-existing in the tab and
had been copied faithfully into the HUD, so fixing one would have left the other looking correct by
accident.

### What the live run found

Four things, none of which any test could have caught, because all four are about what a reader is
*told*.

**The credential source predicted a future it cannot see.** The no-credentials branch of
`detect_credentials()` said "the CLI will prompt for login". It said that against a fully authenticated
session that was never going to prompt for anything. Two facts make the prediction unsupportable: the
CLI resolves its own credentials, and a *resumed* session's CLI child runs under a materialised
`CLAUDE_CONFIG_DIR` that is not the one this process reads. So the branch now reports what was looked
for and where it looked — `unknown — no key, gateway or login file in <dir>` — and predicts nothing.
`_credential_base()` was split out to say, in one place, that the directory is the limit of what the
field can know.

`detect_credentials()` had **zero tests** — the only billing-mode signal the browser gets, and the
function R-9 lives in. It has 41 now, in a new `test_claude_code_health.py`: every source branch, the
precedence order, `CLAUDE_CODE_USE_BEDROCK=0` not counting as a gateway, both conflict warnings, the
endpoint overrides, `~` expansion in `CLAUDE_CONFIG_DIR`, and two that pin the contract the config layer
is built on — that detection leaves `os.environ` byte-identical, and that probing for a login file does
not *create* the directory it looked in.

**The hook log promised traffic the CLI never sends.** The empty state read "the PostToolUse re-index
fires on every file the agent writes, so a turn with an edit in it fills this". It never fills. Proven
both directions, live, with the panel mounted throughout: a `window` probe on `hook-event` recorded
**zero** events across two agent writes, while `file_symbols` on a file created seconds earlier came back
fully indexed and the turn footer listed it under "3 files modified". So the hook ran, the re-index ran,
and the *announcement* is what does not exist — AC⚡DC registers its `PostToolUse` hook as an SDK
callback, which the CLI answers over the control channel and does not put in the message stream, and
`HookEventMessage` is the only thing feeding this table. The copy now says an empty table is the normal
state and is not evidence the re-index did not run.

This is the phase's sharpest instance of a general problem: **a reader that cannot fill looks identical
to a reader that is broken.** The old copy pointed a diagnosing user at a working mechanism and told them
it had failed.

**Two labels were wrong rather than unclear.** The Tool traffic columns read `Calls` and `Results` while
rendering tokens, so "Calls: 4.2K" against a tool called four times is a wrong number — they name the
unit now. And the per-tab 📊 tooltip still said "View this conversation's context (Budget + Cache)",
naming the two sections of the panel CC-17 replaced.

### Tests

Python **3 108 passed, 0 skipped**. Webapp **93 files / 3 724 passed**.

The Python count needs a note, because it looks like 75 tests disappeared. Phase 5's baseline was
**2 992 passed, 75 skipped**; the skips were the tree-sitter extractor tests, and the venv has the
grammars installed now, so they run. 2 992 + 75 = 3 067, plus this phase's 41 = 3 108. **Nothing was
deleted and nothing was waived** — the suite got 75 tests wider without a line changing, worth writing
down precisely because a future reader would otherwise read the skip count going to zero as someone
having turned something off.

- `test_claude_code_health.py` — **new**, 41. Credential resolution, above.
- `test_claude_code_cost.py` — 26. The four bases, and the three cases where a turn's share cannot be
  recovered from a session total.
- `test_claude_code_mcp_server.py` — 42.
- `context-usage-tab.test.js` — **168**. `context-usage.test.js` — **93**. `usage-hud.test.js` — **84**.
  `turn-cost.test.js` — **34**.

### Live verification — the context half done, the cost half still open

Run against a live CLI, and an unusually direct one: **the running app was hosting the very session doing
the verifying**, so the numbers on screen described the conversation that was reading them. No second app
instance, no synthetic turn.

Verified by reading it: the segmented context bar and its arithmetic; all five Debug sections; the
`ac-dc` tool inventory with a token cost per tool, which is the criterion's "names the `ac-dc` tools it
is paying for"; `get_mcp_status()` answering `connected`; Debug's `Grid rows` cross-checking the Usage
section it is derived independently of; and both of the fixes above that have a visible consequence.

**The cost chip and the HUD's appearance are not verified.** Not for want of trying — the structure
forbids it from inside. A turn's cost arrives only in the `result` push (`get_current_state` has no cost
key), the HUD is visible for `_AUTO_HIDE_MS` = 8 s plus an 800 ms fade, and **a turn cannot observe its
own completion**. This is phase 5's lesson in a smaller shape: there, a process could not verify its own
shutdown and needed a second agent; here, the observation window opens exactly when the observer stops
running. The method that works is to leave a recorder behind — a `stream-complete` listener plus a
`MutationObserver` on the HUD's `visible` attribute — which turn N+1 reads to describe turn N. It was
installed once and lost to a Vite HMR full page reload caused by a later edit in this same phase, and the
reinstall after the last webapp write went unanswered. **So this stays open, with the method known and
the ordering constraint now explicit: install the recorder after the final webapp write of the sitting,
or HMR takes it.**

Two smaller things the live run settled about method:

- **Diagnose the Python side first.** A webapp edit HMR-reloads the page, which remounts
  `ac-context-usage-tab` and clears the hook log the diagnosis is reading. The hook finding above was
  only reachable because the `health.py` edits came before the `context-usage-tab.js` ones.
- **`(Budget + Cache)` survived four phases** of the panel it named being replaced. Stale copy is not
  found by grepping for what changed; it is found by reading the screen.

### Deliberately not built

- **`reconnect_mcp_server` still has no caller.** The RPC exists; no browser surface offers the
  reconnect. Unchanged from phase 4, and Debug reports the status it would act on.
- **The HUD's Rate limits and Files modified sections, and its collapse persistence.**
  `viewers-hud.md` § *Sections* specifies all three. Unchanged from what the plan recorded going in.
- **`EngineHealth.mcp` is a field with no writer.** The per-server list the banner leaves out is the same
  data Debug's MCP status fetches live, and one of the two should own it.
- **No `auth_warning` for the new "unknown" source.** `hasHealthProblem` in `health-banner.js` escalates
  on `auth_warning`, and an unknown *source* is a limit on what this process can see, not a
  misconfiguration — banner-escalating it would fire on every resumed session.

### For whoever picks up phase 7

- **Read the recorder.** If `window.__phase6` is present, it holds turn N's `turn_cost_basis`, the HUD's
  visibility transitions and the rendered chip text. If it is not, reinstall it *after* the last webapp
  write and read it on the following turn. That closes the criterion's third clause and the interlude's
  "whether the HUD appears on turn-complete is unverified", which has now been open across two phases for
  the same reason both times: the window closes before anybody looks.
- **Cost is cumulative. Every time.** `total_cost_usd` and `modelUsage` are session totals, and the only
  correct per-turn figures on the wire are `turn_cost_usd` / `turn_model_usage`. A new reader that reaches
  for the obvious field name will be wrong upward and monotonically, which is the shape of wrong that
  looks plausible for a long time.
- **A reader that cannot fill and a reader that is broken look the same.** The hook log's empty state is
  one instance; anything phase 7 adds that depends on an event the CLI may not emit needs its empty state
  to say which of the two it is.
- **`R-9` is still live on this machine.** `$CLAUDE_CODE_USE_BEDROCK` redirects to a gateway with a
  subscription login present, so every cost figure read here carries that caveat. Three phases have now
  recorded it; the tripwire works and the environment has not changed.

---

## Interlude — the timer that answered for the user (2026-08-17)

Not a phase. It started with a screenshot: an `evaluate_script` permission request the dialog had closed
itself, denied, because 300 seconds passed while nobody was at the machine. The question that followed was
the right one — *why does it time out at all?* — and the honest answer turned out to be "because of a bug
somewhere else".

### Gating consumes nothing, and that is checkable

The reason a wall-clock limit felt necessary was an assumption that something is being held open while a
request waits — an API call, a warm cache, a socket. It is not. `can_use_tool` is dispatched by
`Query._read_messages` through `_spawn_control_request_handler` as its own detached task, so the read loop
is not held either. What is outstanding is one blocked SDK **control request**, and that is the whole cost:
the CLI is holding a complete assistant message and cannot issue its next API call until a tool result
exists. Nothing accrues while the user is away.

So the timer was not protecting a resource. It was answering on the user's behalf, which is the one thing
a permission dialog must not do.

### But the timer was load-bearing, for a reason nothing said out loud

Removing it needed one check first: what clears `_pending` when a user hits Stop? Nothing did.
`cancel_all`'s only callers were shutdown, new-session and resume, so `interrupt()` left the request
suspended and **the 300 s expiry was the only thing that ever released it.** Three throwaway probes against
a real `EngineSession` and a real `PermissionBroker` with only the SDK client faked — the shape the session
tests already use — split the outcome in two: a CLI that honours the interrupt anyway leaves a stale dialog
on screen (Case A); a CLI that cannot finish the turn without a tool result loses the session to
`_watch_drain` expiring (Case B). Both are the deadline covering for a missing call.

### What landed

1. **Stop denies before it interrupts.** `cancel_streaming` calls `cancel_for_turn(request_id)` *first* —
   releasing what the CLI is blocked on is what makes the interrupt actionable — and `_run_turn`'s `finally`
   sweeps anything still open, which covers a lost session, an engine crash, a drain that timed out. `Stop`
   is now the escape hatch from a dialog nobody wants to answer, which is what lets the request itself wait
   indefinitely.
2. **The deadline is presence-driven, not wall-clock.** `DECISION_TIMEOUT` and its deny reason are deleted;
   `expires_at` is nullable and `None` is the normal case. `NO_LOCALHOST_TIMEOUT = 30 s` survives as the only
   expiry, and it is armed when the *last* localhost client leaves and cancelled when one returns —
   re-sampled every `PRESENCE_POLL_SECONDS = 2 s` for the life of the request rather than once at the start.
   The poll uses `asyncio.wait({fut}, timeout=…)`, never `wait_for`, which would cancel the future it is
   waiting on. Each arm and disarm broadcasts `permissionDeadline`.
3. **The dialog updates in place.** `permissionDeadline` is session-wide, not turn-scoped — a request
   outlives the moment it was raised — and it mutates the queue entry rather than re-enqueuing it: a
   half-typed deny reason survives and the settling interval is not restarted by a clock the user did not
   touch. No countdown renders when there is nothing counting down, and a request with no deadline sorts
   last in the queue.

### One thing the change broke, found by writing the spec rather than by a test

The coarse screen-reader milestones are `[300, 60, 10]` and the loop announced the first threshold the
remaining time fell under. Correct for a 300 s window; over the 30 s one that is now the only window, it
told a screen-reader user they had **five minutes** to answer something expiring in thirty seconds. Fixed by
retiring thresholds above the time the request actually has, and by announcing the arm itself with the real
remaining — the first milestone inside a 30 s window is the 10 s one, far too late to be the only notice.
Announcements say "30 seconds left", not the chip's `0:30`, which reads as "zero colon thirty".

Also fixed while checking the new `cancelled` action end to end: the transcript rendered machine denials as
"cancelled by cancelled" and "shutdown by shutdown". `resolved_by` repeats the cause for those, and an
attribution phrase is for a person who decided.

### Tests

- `tests/test_claude_code_permissions.py` — 148 in the file. `TestCancelForTurn` (7) is new; the old expiry
  tests became four presence tests in `TestCanUseTool`, including a client who leaves and one who comes back
  and stops the clock, and `test_pending_is_ordered_by_expiry` is now a presence flip rather than two
  synthetic deadlines. Every `decision_timeout=` kwarg is gone — 12 call sites, plus one in
  `scripts/bridge_smoke.py` that no test covers and that would have crashed the script.
- `tests/test_claude_code_service.py` — 3 new: Stop denies the dialog the turn was waiting on, the deny
  reaches the CLI *before* the interrupt, and a turn that ends any other way sweeps what is left.
- `webapp/src/permission-dialog/dialog.test.js` — `describe('a deadline that arms mid-request')` (7),
  covering arm, cancel-and-does-not-fire, the surviving deny reason, promotion, the announcements, and an
  unknown `permission_id`. `queue.test.js` gained `spokenSeconds`.
- Both suites green: python **3120 passed**; webapp **93 files / 3740 passed**.

### Deliberately not built

- **The probes are still in `/tmp`.** `probe_stop_during_permission.py`, `probe_deny_recovers.py` and
  `probe_case_b_real_teardown.py` demonstrated the bug and now pass inverted. Promoting them as regression
  tests was offered and not taken up; they will not survive a reboot.
- **Case A versus Case B is still unverified against a real CLI.** The fix makes the distinction moot — the
  request is released either way — so nothing depends on knowing, but nothing asserts it either.
- **The `permissions` App Config section is still unwired.** `configuration.md` now specifies
  `no_client_timeout_s` and `presence_poll_s` against `permissions.py`'s two constants, and there is still
  no provider reading them.
