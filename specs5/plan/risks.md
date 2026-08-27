# Risk Register

Each risk has an ID, a description, a mitigation, and a **tripwire** — the observable that tells us
the risk has fired. A risk without a tripwire is a worry, not a managed risk.

R-1 through R-4 are the hazards identified in [`origin-brief.md`](origin-brief.md), carried forward
with corrections from [`sdk-surface.md`](sdk-surface.md). R-5 onward are new.

---

## R-1 — `can_use_tool` is not a universal gate

**Severity: high. Likelihood: certain — this is documented behaviour.**

The callback fires only when the permission flow falls through to a prompt. Calls approved by
`allowed_tools`, by settings allow rules, or by `permission_mode` (`acceptEdits`,
`bypassPermissions`, `dontAsk`) never reach it. `sdk-surface.md` adds: a `PreToolUse` hook returning
`"allow"` also skips it, and the SDK emits `CanUseToolShadowedWarning` when that happens.

**Why it bites:** the obvious design — "render every tool call from `can_use_tool`" — produces a UI
that silently stops showing tool activity the moment a user switches to `acceptEdits`, or adds an
allow rule to `.claude/settings.json`. The failure is invisible: nothing errors, the transcript just
goes quiet about file writes.

**Mitigation:** split the two jobs at the architectural level, enforced by spec.

- **Display** comes from the message stream (`ToolUseBlock` / `ToolResultBlock`) and the
  `PreToolUse` hook. Always fires, regardless of permission mode.
- **Asking** comes from `can_use_tool`, and only from there.
- Our hooks are strictly observational: they never return `permissionDecision`. This is an invariant
  in [`../3-engine/tool-surface.md`](../3-engine/tool-surface.md).
- Do not put gated tools in `allowed_tools`.

**Tripwire:** a test that runs the same scripted turn under `default` and `acceptEdits` and asserts
the rendered tool-card count is identical. If the counts diverge, display has leaked into the ask
path.

---

## R-2 — `interrupt()` leaves messages in the buffer

**Severity: medium. Likelihood: high — every user-cancelled turn.**

After `interrupt()`, the interrupted turn's remaining messages, including its `ResultMessage`, must
be drained before the next query's response is read. `terminal_reason` will be
`"aborted_streaming"` or `"aborted_tools"`.

**Why it bites:** the visible symptom is a reply appearing in the wrong chat bubble, or a stale
`ResultMessage` closing out a turn that just started — which looks like a cost-accounting bug and
gets diagnosed as one.

**Mitigation:** the message pump owns a drain-to-`ResultMessage` step in its cancel path, and the
request-ID map keeps the interrupted request ID alive until its `ResultMessage` arrives. Late
messages for a retired request ID are dropped with a log line, never re-routed to the current
request.

**Tripwire:** log at WARNING whenever a message arrives for a request ID that has already been
finalised. In steady state this log is empty; a non-empty log after a cancel means the drain is
wrong.

---

## R-3 — Do not `break` out of the message iterator

**Severity: medium. Likelihood: high — a browser disconnecting mid-turn is normal, not exceptional.**

The SDK warns that breaking out of iteration causes asyncio cleanup problems. AIC⚡DC's normal case
is exactly the provoking case: the user refreshes the tab, the WebSocket drops, and the naive
handler stops reading.

**Mitigation:** the message pump's lifetime is tied to the **work**, not to any client connection.
It runs to `ResultMessage` regardless of who is listening — and past it while background tasks are
still in flight, because a result frame ends a turn rather than the run
([`../3-engine/session.md`](../3-engine/session.md) § A result message ends a turn, not the run;
"tied to the turn" is what this originally said, and a background subagent is what disproved it) —
accumulating into a server-side buffer;
disconnected clients re-attach and replay from the buffer. This is the same shape as the native
engine's existing stream-resumption behaviour, so the reconnect logic in `app-shell/reconnect.js`
carries over rather than being rewritten. Cancellation is a flag plus `interrupt()`, never a
`break`.

**Tripwire:** an integration test that disconnects the client mid-turn, reconnects, and asserts the
full turn transcript is present and the process has no pending-task warnings at teardown.

---

## R-4 — Built-in slash commands are CLI UI, not SDK features

**Severity: low individually, medium collectively. Likelihood: certain.**

Custom slash commands arrive via `setting_sources`. `/compact`, `/clear`, `/model`, `/context`,
`/rewind` and friends are terminal interface, not harness features, and must each be mapped onto an
SDK call or a UI affordance.

**Why it bites:** it is the gap between "same harness" and "same interface", and it is the part most
likely to be under-scoped, because each individual mapping is trivial.

**Mitigation:** two exception tables in
[`../3-engine/session.md`](../3-engine/session.md#slash-commands) — routed and denied — over a
**passthrough default**. The premise above is the part that turned out to be wrong: the CLI dispatches
its own built-ins in-process, before a model turn is billed, so most of them need no mapping and get
one only where an AIC⚡DC surface answers better (`/context`, `/usage`, `/cost`, `/mcp`, `/agents`,
`/permissions`) or where passing through would desynchronise the session store (`/clear`, `/resume`).
The first version of this did map every command, by refusing seventeen of them — including several
that would have answered.

**Tripwire:** typing an unmapped `/command` produces the CLI's own answer, including
`Unknown command: /xyz` for a typo. A deployment-authored refusal for a command the CLI would have
handled is the failure this now watches for, in the direction opposite to the original.

---

## R-5 — Losing the tool's identity

**Severity: high. Likelihood: medium — this is a slow drift, not an event.**

The conversion deletes the parts of AIC⚡DC that were distinctively AIC⚡DC's, and the remaining
delta over "Claude Code in a terminal" is a browser UI. If the surviving surfaces are not actively
good, the honest conclusion becomes "just use the CLI", and the conversion will have removed the
tool's reason to exist.

**Mitigation:** the four surfaces that justify the frontend are treated as deliverables with their
own specs, not as ports:

1. The permission dialog with a real diff in it ([`../3-engine/permissions.md`](../3-engine/permissions.md)).
2. Context visualisation ([`../3-engine/context-visibility.md`](../3-engine/context-visibility.md)).
3. Repo intelligence as MCP tools ([`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)).
4. Spatial navigation — Monaco, SVG editor, nav grid — kept working throughout, which is why CC-2
   keeps the indexes.

**Tripwire:** at the end of each phase, answer in one sentence why a user would open AIC⚡DC rather
than a terminal. If the answer is only "it looks nicer", the phase did not deliver.

---

## R-6 — Cost becomes invisible instead of cheap

**Severity: medium. Likelihood: medium.**

The native engine's whole apparatus made cost legible: tier bars, cache hit rates, session totals,
a terminal HUD after every turn. An agent that reads what it wants is cheaper per useful outcome but
far less predictable per turn, and under subscription billing the token counters stop mapping to
money at all.

**Why it bites:** users who chose AIC⚡DC for cost control lose their instrumentation, and the
replacement (`total_cost_usd`) may read as zero or meaningless on a subscription — which looks like
a broken HUD.

**Mitigation:** the usage HUD reports what is actually true for the active credential: cost when
the API reports cost, and token/context/rate-limit state always. `RateLimitEvent` is the
subscription-mode equivalent of a cost signal and gets first-class display.
`max_budget_usd` is exposed in settings as a hard stop.

**Tripwire:** the HUD never displays `$0.00` as a cost for a turn that consumed tokens — it either
shows a real figure or shows "subscription" with the usage figures.

---

## R-7 — Bundled CLI size and platform-specific wheels

**Severity: high for release, zero for development. Likelihood: certain.**

The `claude-agent-sdk` wheel bundles a ~295 MB platform-specific `claude` binary, making it
`manylinux_2_17_x86_64` rather than pure Python. (Measured at 296.76 MiB — see § *Status* below.)

**Why it bites:** AIC⚡DC currently ships a modest pure-Python package and a PyInstaller bundle.
Both assumptions break. Discovered at release time, this blocks a release.

**Mitigation:** decide the distribution model during phase 0 and specify it, rather than discovering
it in phase 7. Three options, in preference order:

1. **External CLI mode as the default** — depend on the SDK but configure it to use a
   system-installed `claude`, documented as a prerequisite. Keeps our package small.
2. **Per-platform wheels** — build a matrix. Correct but multiplies release CI.
3. **Embed the bundle** — simplest for users, 295 MB per platform.

Startup logs which CLI was selected and its version, so support questions are answerable.

**Tripwire:** a fresh-container install test that fails loudly with an actionable message when no
CLI is resolvable, rather than failing at the first prompt.

**Status (2026-08-27), with phase 7 now active — this risk landed as predicted, at release time.**
Measured against the installed wheel (`claude-agent-sdk` 0.2.137, CLI pin 2.1.229): `_bundled/claude` is
**296.76 MiB** (311,175,440 bytes), and it is a **native ELF x86-64 executable**, not the Node application
the deployment specs described until today. Three consequences for the options above:

- **Option 2 is mostly inherited, not built.** The wheel is tagged `py3-none-manylinux_2_17_x86_64`, so
  each runner in the existing per-platform PyInstaller matrix resolves its own platform's engine. The work
  it reduces to is confirming `uv.lock` covers every matrix target, since `--frozen` installs no wheel that
  was never locked.
- **Option 3 works standalone.** No Node runtime is needed, so embedding produces a genuinely
  self-sufficient artefact — which makes this a straight size trade against option 1 rather than the
  half-measure it looked like.
**Decision (2026-08-27): option 3 — collect the bundle.** Taken during phase 7 and implemented in
`.github/workflows/release.yml`, which now passes `--collect-all=claude_agent_sdk` and fails the build if
`_bundled` is absent from the onefile archive. Three reasons it beat option 1 on the facts above rather than
on preference:

- **It is what the SDK already does.** Resolution prefers the bundled copy over `PATH` (see R-8's
  correction), so option 1 would mean shipping a binary whose first-choice engine had been deliberately
  removed — a smaller artefact whose failure mode is a `PATH` search the user did not ask for.
- **It costs a download, not a decision tree.** ~95 MB compressed per platform in the wheel; a
  self-sufficient artefact needs no prerequisite documented, no install instruction to get wrong, and no
  support question about which `claude` was picked.
- **Option 2 came free.** `uv.lock` already pins all five SDK wheels — macOS arm64 and x86_64, Linux x86_64
  and aarch64, Windows amd64 — so each matrix runner installs its own platform's engine under `--frozen`
  (verified 2026-08-27).

Users who want their own CLI keep it via `engine.json`'s `cli_path`, which bypasses discovery entirely.
**Still outstanding: the tripwire's runtime half** — the build-time assertion landed, the fresh-container
install test has not ([`../next.md`](../next.md) § A2).

---

## R-8 — Version skew between the SDK and the system CLI

**Severity: medium. Likelihood: high over time.**

The wheel pins `__cli_version__ = "2.1.229"` and enforces a floor of `2.0.0`, while the resolved CLI
can be any version. The local machine has 2.1.227. Skew is the normal state, not the exception.

**Correction (2026-08-27):** this paragraph said `_find_cli` prefers a CLI on `PATH`. It does not — it
returns the SDK's bundled copy first and only falls back to `shutil.which("claude")`
([`../6-deployment/build.md` § The Engine Is Not Bundleable](../6-deployment/build.md#the-engine-is-not-bundleable)).
Skew is still the normal state, but it points the other way: a build that collects the bundle pins users to
the wheel's engine regardless of what they installed themselves, and `engine.json`'s `cli_path` is the only
way to prefer their copy.

**Mitigation:** startup checks the resolved CLI version, logs it alongside the SDK's pin, and warns
(not fails) on mismatch. Where a feature we rely on has a known minimum — file checkpointing,
`get_context_usage`, `SessionStore` — the check is feature-specific and degrades that feature rather
than the session.

**Tripwire:** a startup diagnostic line naming the CLI path, its version, and the SDK pin. Absent
that line, we cannot diagnose a skew report.

---

## R-9 — Authentication conflict silently redirects the session

**Severity: high. Likelihood: medium.**

The SDK honours Claude Code's credential resolution. AIC⚡DC's `llm.json` currently exports provider
credentials into the process environment on startup (`apply_llm_env`), which can point the CLI at a
different account or a Bedrock endpoint. Separately, a coexisting `~/.config/anthropic/` profile and
a Claude Code `/login` credential produce an auth-conflict warning.

**Why it bites:** the symptom is a session that works but bills the wrong account, or fails with an
opaque region/credential error that looks like a network problem.

**Mitigation:** delete the env-export path with the rest of the LiteLLM config (CC-11). Startup
reports which credential source the CLI resolved, and surfaces the SDK's auth-conflict warning as a
visible banner rather than a log line.

**Tripwire:** the startup banner names the auth source. A session that starts without naming its
auth source is misconfigured.

---

## R-10 — Subagent transcript volume

**Severity: low. Likelihood: medium.**

The native engine's agent archive already needed a 1 GB warning. Claude Code subagents write their
own transcripts, and `Task`-heavy work produces many of them.

**Mitigation:** carry the existing disk-usage warning forward, re-pointed at the session store's
directory. The warning mechanism is unchanged; only the path it measures moves.

**Tripwire:** the existing one-shot warning, retained.

---

## R-11 — Index staleness now has no natural refresh point

**Severity: medium. Likelihood: high.**

The native engine re-indexed at request boundaries, which gave a clean snapshot discipline. Under
Claude Code, files change mid-turn — the agent might write twenty files in one turn — and there is
no request boundary to re-index at.

**Why it bites:** Monaco's go-to-definition starts resolving to stale line numbers during long
agentic turns, and the MCP `symbol_map` tool returns a map that predates edits the agent itself
made two tool calls ago. The second is worse: the agent is misled by our own tool.

**Mitigation:** re-index incrementally from the `PostToolUse` hook, per touched path, debounced.
The snapshot-discipline invariant is restated in terms of tool-call boundaries rather than request
boundaries — see [`../2-indexing/symbol-index.md`](../2-indexing/symbol-index.md).

**Tripwire:** a test that has the agent write a file and then call `symbol_map`, asserting the new
symbol is present.

---

## R-12 — The permission dialog becomes a click-through

**Severity: medium. Likelihood: high — this is the documented failure mode of every permission UI.**

A dialog that appears for every `Read` trains users to hit Allow without looking, at which point it
provides negative safety value: it costs attention and grants authority.

**Mitigation:** tier the surface by consequence rather than by tool count. Reads and searches are
displayed but not gated by default; writes show a diff; `Bash` shows the command with the
working directory. "Always allow" is scoped to a tool-plus-pattern rule written into the project's
settings where the user can see and revoke it — never an invisible in-memory grant.

**Tripwire:** count permission prompts per turn in the usage HUD. If the median is above roughly
two, the tiering is wrong and needs re-cutting.

---

## R-13 — Some `ClaudeAgentOptions` combinations are invalid, and only `connect()` knows

**Severity: high when it fires — no session at all. Likelihood: certain, and it has already happened
once.**

Individual options are checked by the dataclass. Combinations are checked by
`_internal/session_store_validation.py`, from inside `connect()` and `query()`, by raising
`ValueError`. Phase 5 hit the one that exists today: `session_store` plus
`enable_file_checkpointing` is refused, so the first run that built a store could not start the engine
at all ([CC-20](decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)).

**Why it bites:** the drift tripwire we had — every key we set exists on the installed dataclass — is a
test about *field names*, and cannot see a rule about two fields together. It stayed green through the
failure. Worse, the failing option had been set and harmless for four phases, so the change that broke
it was somewhere else entirely: the phase that finally supplied the other half.

**Mitigation:** run the SDK's own validator over the options we actually build, for the combinations we
actually ship — including the ones assembled only when a collaborator is present. And keep a second test
asserting the constraint still exists, so an SDK that relaxes it tells us rather than being discovered
by accident.

**Tripwire:** `test_a_mirrored_session_passes_the_sdks_own_validation` and
`test_the_sdk_still_refuses_the_pair`, both `importorskip`ing the private module. A new validation rule
in a future SDK will not be caught by either — the general guard is a live connect per phase, which is
what phase 5 skipped.
