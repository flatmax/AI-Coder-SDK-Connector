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
the engine and are used verbatim, so a user running both does not have to learn two colour
languages. `maxTokens` arrives already reduced by the autocompact buffer, so the bar reaching 100%
is the real trigger point rather than the model's raw window.

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
- **No unit tests for `usage-hud.js` / `context-usage-tab.js`.**
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
