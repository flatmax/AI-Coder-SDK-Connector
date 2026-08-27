# Conversion Plan — Native Engine → Claude Code Frontend

**Status:** Active. This directory is the plan of record for converting AIC⚡DC from its own
LiteLLM-based context engine to a frontend for Claude Code (via the Claude Agent SDK).

The rest of `specs5/` describes the **target** state. This directory describes **how we get
there** and **why the shape is what it is**. When the conversion is finished, `plan/` becomes
history and moves under `specs5/impl-history/`.

## Where we are (2026-08-18)

> **The queue is [`../next.md`](../next.md), not this section.** What follows is the record of how the
> conversion got here — dated, and deliberately left as written. The open items below are the reasoning
> behind their entries in `next.md`; that file carries the sequence, the state of each item at HEAD, and
> the ones that have closed since this section was written.

**Phases 0 through 6 are done, and phase 6's last clause closed on 2026-08-17.** The Context tab had its
live run: the visualisation and the `aic-dc` tool inventory were read off a live CLI — the app was
hosting the session doing the verifying — and the run found four things no test could have, all of them
about what a reader is *told* rather than what is computed. The clause that was outstanding — a live
turn's cost chip — was closed by leaving a `window.__phase6` recorder behind and reading it on the two
following turns, since **a turn cannot observe its own completion**. It verified the HUD appearing and
auto-hiding at the specified 8.8 s, and, more to the point, that the chip prints a *difference*: two
turns of `1.51561` and `1.210191` against a cumulative `2.725801`, with `$1.21` on screen where the
pre-fix code would have shown `$2.73`. **A single turn could not have shown that** — a session's first
turn has difference equal to total, so the bug and the fix look identical. The chip's "nothing extra" and
"cost unknown" renderings are still unobserved, because no ordinary turn causes them. Details in the
[phase-6 entry](delivery.md#phase-6--context-and-cost-visualisation-2026-08-17) under *Live
verification*. A conversation now outlives the process: `RepoSessionStore` mirrors the CLI's transcript
under `.aic-dc/sessions/`,
`run_session_store_conformance` passes with nothing waived, and a server that comes back up reattaches
to the session it was in. **Resumption is the SDK's rebuild, never our replay** — we render a *record*
of a session for the human and hand the same session ID to the CLI, and the two views cannot drift
because neither is derived from the other.

**Phase 5 was also where three specified-but-undelivered warnings got a reader**: the mirror gap, at
both of its scales — a marker on the turn that was not written and a running count for the session —
the 1 GiB disk warning, and the health banner that four specs routed to and nothing rendered. The
`history` section of `app.json` exists now too, so the two thresholds the mirror is judged by are the
user's rather than a module constant's. What phase 6 inherits is under *What phase 6 will find already
there*.

**Phase 5 shipped with no live CLI run, and both interludes after it are what that cost.** The first
found an engine that could not connect at all in any repo. The second ran the exit criterion properly —
a restarted server, driven from outside by a second agent, because nothing can verify its own shutdown —
and it passes: `--resume` on the child, the earlier conversation answered with no tool calls, no fork in
the transcript, a clean SIGINT. It also found that **every row in the session list read as the same 100
characters** of our own framing prose, which 2 915 green tests had agreed with. Three phases running,
green numbers have met a live CLI and lost.

**Phase 6 makes it four, and the shape of the loss changed.** Its live run found nothing wrong with the
arithmetic — the interlude had already spent that budget — and four things wrong with what the tab
*said*: a credential source predicting a login prompt for an authenticated session, a hook log promising
traffic the CLI does not emit while the hook it named was working, and two column labels naming counts
over cells holding tokens. **None of the four was reachable by any test**, because each is a claim in
prose about a mechanism, and a test asserting the prose would have asserted the wrong prose. That is the
argument for the live run stated more sharply than phases 3–5 could state it: green numbers were not the
problem this time; unread sentences were.

**And a fifth loss came from a screenshot rather than a run.** A permission request had been denied by a
300-second timer while nobody was at the machine, and the question *why does it time out at all?* had no
good answer: nothing is consumed while a request waits, so the timer was not protecting a resource — it
was answering for the user. What it *was* protecting was a missing call: nothing released a pending
request when a user hit Stop, so the expiry was the only thing that ever cleared it. The interlude after
the phase-6 entry has the chain. The lesson is the phase-6 one again from the other side: the deadline
was in the specs, tested, green, and load-bearing for a reason none of that recorded.

**And the plan now knows what it has not built, without anyone re-reading the wheel.**
`sdk_surface.py` asks the installed SDK what it offers and this repo's own syntax trees what it uses, and
puts every name in one of three buckets — used, declined with a reason, or pending with an argument. The
gate fails on a name in **none** of them, which is the only state that means the SDK moved and nobody
looked; the 22 still-pending options sit green by design, because a gate that fails on unbuilt surface
earns an ignore-list in a week. It found this list's own
[open item 1](#open-items-carried-forward-as-of-2026-08-18) from the wheel rather than from the list — and
then closed it, along with the other two options and the one hook it had argued were worth doing, inside
the day. It also corrected a belief held in this repo an hour earlier: the
`Message` union and the client surface are **fully** consumed, 7 of 7 and 14 of 15. `sdk-surface.md` is
still where the reasoning lives — reflection reads shape, and every correction in that file was a
type-satisfied, behaviour-wrong case. What it no longer has to carry alone is the inventory.

Read [`delivery.md`](delivery.md) before touching anything: it records what each finished phase
landed, what it deliberately left out, and what the next phase has to do first. The phase-6 entry and the
interlude after it are what matter for picking this up cold; the phase-5 entry and the two interludes
after it are the background they assume.

### Picking phase 7 up cold (as of 2026-08-17)

**Both suites are green**, and the totals are deliberately not written here — run them: `pytest -q` and
`npm test` in `webapp/`. A pass count in a rolling paragraph is stale by the next commit, and a stale
one invites the reader to treat a difference as a finding. What is worth knowing is the part that does
*not* move:

**Pytest skips 75, and will keep skipping 75.** They are a standing feature of this venv, not a
regression and not the tree-sitter extractor tests — those grammars are installed and those tests run.
The skips are three absent optional document-conversion dependencies: PyMuPDF (29), python-pptx (25),
openpyxl (21). Install those three and nothing is skipped. Nothing was deleted and nothing was waived,
and the count has been 75 on both sides of every phase since 5.

That last sentence is the whole reason this paragraph is worded the way it is. It used to claim "3131
passed, 0 skipped" and explain the zero with the venv gaining the grammars — an explanation for a change
that had never happened, built on a figure that was the *collected* total quoted as the passing one. See
the phase-6 [*Tests*](delivery.md#tests-7) section for the retraction and the rule it leaves behind:
quote both halves of the pytest summary line, or quote neither. Dated per-commit measurements in
[`delivery.md`](delivery.md) are a different thing and stay — a number attached to a commit does not go
stale, it becomes history.

**Nothing is uncommitted.** The live subagent tabs, the `local_bash` filter and the ordinal-plus-keyword
strip labels under item 9 below landed on 2026-08-17 as the head of this branch. The table below stops one
commit short of the head, as it always does — a row is written by the commit that follows the one it
names. Read
[*Interlude — the timer that answered for the user*](delivery.md#interlude--the-timer-that-answered-for-the-user-2026-08-17)
before touching the permission path: a request now waits indefinitely while a host client is connected,
and Stop is what closes a dialog nobody wants to answer. The commits on this branch, oldest first:

| Commit | What it landed |
|---|---|
| `8f92eee` | The autocompact mark on the HUD's context bar, and the note that stands in for it when autocompact is off |
| `fd3963a` | The spec reconciliation: § *Verified field shapes — the result footer*, the cost-is-cumulative correction, the Debug section specified ahead of being built |
| `02373a2` | The Debug section — Engine, initialize reply, hook traffic, MCP status, `gridRows` — and the segmented control appearing on an error, which used to hide Debug exactly when it was worth the most |
| `551e169` | `EngineHealth.degradations` and the health banner that reads it, so a bridge or hook that did not start reports the loss instead of writing a log line nobody reads |
| `97910f5` | The phase-6 entry written before its live run, naming what was built-and-unverified and the three findings to pick up |
| `4efc0f9` | Phase 6's seven fixes — the credential source that predicted a login prompt for an authenticated session, the hook log that reported working machinery as broken, two wrong labels, the late fetch that could overwrite a push, the initialize reply's own heading, the clipped autocompact mark in both readers |
| `5fc6fa4` | The permission interlude: Stop denies before it interrupts, the decision timeout is gone, and the one deadline left is armed by absence rather than by a clock |
| `834aff2` | The delivery record catching up with the interlude it had stopped short of |
| `092ea58` | The forensic probes moved out of `/tmp` and into `scripts/`, where they run again next time the CLI ships a new build |
| `aee7b2b` | Option previews: the compare pane, and the correction of what the format env var actually buys |
| `51ea77f` | The per-answer note (`annotations`), and the retraction of the claim that the format env var is what makes a preview possible |
| `fa66b99` | The dialog's Monaco given the head styles a shadow root cannot see, edit cards that open themselves, `Bash` summaries that wrap |
| `218f89d` | `repo_root` in the state snapshot, and the one conversion that stops a file chip asking the repo API for an absolute path |
| `0d80758` | The open items written down, so none of them has to be rediscovered from a log |

`51ea77f` is where the retraction lives: **setting `CLAUDE_CODE_QUESTION_PREVIEW_FORMAT` is not what
makes a preview possible**, and this record said otherwise in four places before a live A/B disproved it.
See [*Interlude — the examples the question was asking about*](delivery.md#interlude--the-examples-the-question-was-asking-about-2026-08-17).

**The live run is done, the phase-6 entry is written, and the cost chip is verified.** The three review
findings below are all fixed. The live run then found four more, also fixed. The exit criterion's last
clause — the live cost chip — **is now closed**: the `window.__phase6` recorder was reinstalled during a
turn that wrote nothing under `webapp/` (which is why it survived) and read on the two following turns.
**Phase 7 starts with no verification debt.** Two rules that came out of it and generalise:

- **Two turns minimum when checking anything per-turn.** A session's first turn has `turn_cost_usd`
  equal to `total_cost_usd`, so it cannot tell a differenced figure from a cumulative one.
- **Install any browser-side recorder after the last webapp write of the sitting.** Vite HMR does a full
  page reload and takes it. Doc-only edits under `specs5/` do not, which is what made this sitting work.

The three review findings, all fixed in this phase:

- ~~**A fetched health record can overwrite a fresher pushed one**~~ — `_ensureDebug` now captures a
  `_healthSeq` push counter before the `await` and applies the fetch only if it has not moved. The rule
  that section is built on is "a push wins over a fetch, because `mirror_gaps` moves during a turn", and
  the guard had only covered a fetch that answered *nothing*.
- ~~**The initialize reply renders inside the Engine section**~~ — it has its own `<h3>` now, so Debug is
  five sections. The distinction is provenance: the binary resolution is ours, the reply is the engine's,
  and two tables under one heading worked against it.
- ~~**The autocompact mark's overhang is clipped**~~ — a `.bar-wrap` with `position: relative` holds the
  mark as a sibling of the `overflow: hidden` bar rather than a child, in `usage-hud.js` and
  `context-usage-tab.js` together. It was pre-existing in the tab and had been copied faithfully into the
  HUD, so fixing one would have left the other looking correct by accident.

What the live run found, all fixed, and all four about what a reader is *told*: `detect_credentials()`
predicted "the CLI will prompt for login" against a fully authenticated session (a resumed session's CLI
child runs under a materialised `CLAUDE_CONFIG_DIR` this process never reads — the branch now reports
where it looked and predicts nothing, and the function went from **zero tests to 41**); the hook log's
empty state promised traffic the CLI never sends, because an SDK-callback hook is answered over the
control channel and never enters the message stream — **the hook and the re-index were both working, and
the copy told a diagnosing user they had failed**; and two labels were wrong rather than merely unclear —
Tool traffic's `Calls`/`Results` columns render tokens, and the 📊 tooltip still named the panel CC-17
replaced.

Still deliberately unbuilt, so their absence is not a discovery: ~~`ClaudeCodeService.reconnect_mcp_server`
exists and no browser surface calls it~~ *(built 2026-08-26, with `toggle_mcp_server`, on the Context tab's
server rows)*, and the HUD's Rate limits and Files modified sections and its collapse persistence are
unwritten — `viewers-hud.md` § *Sections* specifies all three.

### Open items carried forward (as of 2026-08-18)

Known, decided-against-for-now, or awaiting a number. Each one's reasoning is in the `delivery.md`
interlude that found it; this list exists so that none of them has to be rediscovered from a log.

1. ~~**`max_buffer_size` is still the SDK's 1 MB default**~~ — **set on 2026-08-18, to 16 MiB.** One
   stdout line over the SDK's own 1 MiB raised `CLIJSONDecodeError` inside the transport's reader and
   ended the message pump for that session permanently. The failure path did work as designed —
   `_is_connection_failure` matches `CLIJSONDecodeError`, so the turn was failed, the session marked lost
   and `engineHealth` broadcast — but the conversation was over, and **one oversized tool result was
   enough to do it**: an inline screenshot did exactly that on 2026-08-17. It was blocked on a number
   rather than on design, and the number fell out of the asymmetry: the ceiling exists to bound memory,
   the overflow ends a conversation, and one session's pending buffer at 16 MiB is nothing on a
   single-user localhost host while covering a ~12 MB raw image. `engine.json` can raise it further, and
   is the one field there where null does not mean "let the CLI decide" — see
   [`../1-foundation/configuration.md`](../1-foundation/configuration.md). **The SDK probe reported this
   one independently** (item 10), having found it from the wheel rather than from this list, which is
   also how the other two items closed that day.
2. ~~**A lost session keeps being polled.**~~ — **fixed on 2026-08-28.** After that pump died, the usage
   HUD went on calling `get_context_usage` — each attempt a control request that ends in a 60-second
   `Control request timeout` traceback, four of them in one log. `get_context_usage` caught
   `EngineNotReadyError` and `SessionLostError`, but nothing gated the *poll* on engine health, and the
   HUD had no "the engine is gone" state to sit in. The tracebacks were noise about a thing already
   reported by the banner. **The gate is now on the pushed `engineHealth` record, and the interesting
   part was deciding what it reads:** `connected: false` is also true of a freshly loaded page, so the
   discriminator is a non-empty `last_error` — the same rule the health banner uses, kept single-owner
   deliberately. A second signal was needed for the turn that *causes* the loss, whose fetch is already
   out before the health push lands: the reply's `reason: 'no-engine'`. Writing the spec section also
   caught § *Data Flow* claiming the HUD makes no follow-up RPC, which has been false since phase 3.
   [`../5-webapp/viewers-hud.md`](../5-webapp/viewers-hud.md) § *When the Engine Is Gone*.
3. **Two mechanisms now answer "absolute engine path → repo path".** `_mark_openable_memory_files` adds
   a `relPath` field server-side so the Context tab knows which memory files are openable; `toRepoPath`
   (`218f89d`) converts client-side at the shell's `navigate-file` choke point. Both are correct and
   neither is wrong to exist, but the next payload carrying an absolute path will pick one of them by
   accident. They should converge — most likely on the client-side one, which needs no per-payload
   enrichment.
4. **An RPC that fails behind a viewer open shows the user nothing.** That is what kept the chip bug
   unreported for as long as it was: the viewer painted an empty diff and the only evidence anywhere was
   a bare `Failed: Absolute paths not accepted:` line on the server's stderr. Two independent halves —
   jrpc-oo prints raw exception text with no request context, and the diff viewer treats a failed fetch
   as empty content — and **fixing either one alone would have made the bug reportable**.
5. **Tool-card file chips still display the absolute path.** Only the navigation was converted.
   Shortening the label is a display decision with its own question (basename, root-relative,
   middle-elided) and it is the one place a multi-root future would want the root visible.
6. **Two rendering behaviours have no test and cannot get one from jsdom.** The `Bash` summary's
   three-row clamp is layout, and jsdom has none; the dialog's Monaco style-clone tests assert that the
   rules *arrive*, not that the editor lays out. Both were verified by driving a live tab and probing the
   DOM. A screenshot-based regression harness is the only thing that would catch a re-break, and it should
   still write files rather than return images inline — item 1 raised the ceiling that made one inline
   screenshot fatal, but a harness returns many per run and a ceiling is not a budget.
7. **The permission dialog re-clones the whole document head per editor creation.** The same cost the
   diff viewer has always paid, for the same reason (Monaco's constructor adds rules synchronously), and a
   request builds at most one editor — so it is unmeasured rather than known-cheap. Incremental cloning is
   the optimisation if that ever stops being true.
8. **The question-preview `--without` A/B is not automated.** `scripts/question_preview_smoke.py`
   supports it and the specs record its result, but nothing re-runs it when the CLI ships a new build —
   and what it measures is exactly the kind of detail a version bump moves.
9. ~~**Live subagent tabs have never watched a real fan-out.**~~ **Watched on 2026-08-17**, and it found
   what 33 green tests could not: **the CLI reports a slow `Bash` command as a task**
   (`task_type="local_bash"`, one per command past its backgrounding threshold) through the same four
   `Task*` messages a subagent uses, so a turn that delegated four subagents opened **twenty tabs, sixteen
   of them empty**. The engine had been folding those into subagent rows since the rows existed — a
   pre-existing bug that only became visible when each row also became a tab. Fixed in
   `messages.py` by filtering them where the one rule serves every surface at once, and **re-verified live
   after a restart**: three long commands in the main scope and two subagents' own sleeps opened nothing,
   while the two subagents opened exactly two tabs and settled green with their real counters. The run
   left three things open; two are now closed:
   - **⏹ Stop and the amber LED path are still unverified** — the one still open, and now for a better
     reason. The first reason was wrong and the docstring carrying it is corrected: `stop_task` is its own
     control subtype (`SDKControlStopTaskRequest`, a sibling of the interrupt request), and `client.py:454`
     says the CLI answers it with a `task_notification` of status `stopped` **in the message stream** — so
     it does not end the host turn, and `service.py`'s claim that it did had nothing behind it.
     The cheap substitute was then tried and **failed to reach amber**: an agent's own `TaskStop` against
     a live background subagent (watched live 2026-08-17) killed it without the CLI emitting any terminal
     task message at all — engine and browser both still read `status: null, terminal: false`, the LED
     stayed cyan and the tab kept offering ⏹ for a task that no longer existed. The harness reported
     `killed`; the SDK message stream said nothing. So the two paths are **not** interchangeable, the
     webapp's ⏹ (a real `stop_task` control request) is the only thing that can verify itself, and a task
     that dies quietly is left to the turn-end sweep that shows a still-live subagent as status-unknown.
   - ~~**The strip labels read as noise.**~~ **Fixed.** A tab is now `1 headings`, `2 test-files`: an
     ordinal assigned at creation (it never renumbers under the cursor) plus one keyword, with the whole
     sentence in the tooltip and in the feed's opening line. The keyword and the sentence come from the
     parent `Task` card's `input.description`/`input.subagent_type` when it can be found, which is what
     the user asked for rather than what the subagent happened to be doing when its last event landed;
     the live activity string is only the fallback. `subagentKeyword` is the heuristic: last word,
     reaching back past stopwords when that word identifies nothing (`check the tests` → `check-tests`),
     paths collapsed to their basename, 14 characters.
   - ~~**One unreproduced focus anomaly.**~~ **Guarded rather than explained.** After a mid-fan-out
     reload the active tab was once a live subagent feed rather than Main. It never reproduced, so
     `rehydrateSubagentTabs` now refuses to leave focus on a tab it just created: a rebuilt tab is one
     nobody chose. The cause is still unknown, which is why this is a guard and not a fix.

   What *was* confirmed live: the tab per `Task`, keyed on `task_id` because **`agent_id` was null on
   every single event** (the fallback the spec calls a last resort is the normal case); `readOnly: true`
   with no textarea; `currentRequestId` never set; cyan→green LEDs with real counters from `TaskUsage`;
   the flat feed; and the reconnect rebuild — a hard reload mid-fan-out came back with all three tabs, two
   green from the snapshot and one live with its ⏹.
10. **22 SDK options are known, argued for, and unbuilt** — the pending list the probe maintains
    ([`sdk-surface.md` § The probe](sdk-surface.md#the-probe), Alt+5 in the app). This is a *findings* list,
    not a defect list: the gate goes red only when the SDK grows a name nobody has triaged, so these 22 sit
    green by design. It said 24 and three of them were worth doing; **two are now built, on the day the
    probe first reported them** — `max_buffer_size` (item 1, reached independently) and `stderr` (the CLI's
    diagnostics were log-only; the last 20 lines now ride on `EngineHealth` into the health banner). The
    third stays open and is the one that is a feature rather than a constructor argument:
    `resume_session_at` / `resume_drops_turn`, resume from a chosen point, the SDK-side half of the undo
    story [CC-20](decisions.md) gave up. `sandbox` is on the list as a trap: it reads like a free security
    win and it changes what the agent may do to the machine, which is the permission dialog's question,
    not an option's. The **hook** column is now empty too: `PreCompact` was the one pending event, and it
    is registered — nothing else announces a compaction *before* it happens, since the stream's own
    `compact_boundary` arrives once compaction has finished.

    Closing the first two also found a gap in the probe itself: a `PENDING_OPTIONS` note left behind after
    the option is implemented is *worse* than absent, because it reads as an argument to undo the work, and
    the `stale` bucket structurally cannot report it (`stale` is names the SDK removed; these names still
    exist). Hence a `resolved` bucket, `(declined | pending) ∩ assigned`, rendered beside `stale` in the
    tab and asserted empty by the suite.

    Two things the probe cannot do, stated here because a green gate invites the wrong inference.
    **It reads shape, never semantics** — every row in `sdk-surface.md`'s correction tables was a
    type-satisfied, behaviour-wrong case that no reflection would have caught. And **nothing runs it on a
    schedule**: it fires with the suite, so a `pip install --upgrade` with no commits after it leaves a
    window where the report is stale and does not say so.
11. **Six `LLMService.*` method names survive in `webapp/src` across seven call sites, and one of them
    fires on every connect.** Found
    while diagnosing something else — a stale cached bundle whose failures *looked* exactly like this
    ([`delivery.md`](delivery.md#interlude--the-empty-repo-that-wasnt-and-an-app-served-from-last-week-2026-08-18)),
    which is the reason this item is worth having: the next person to see `method not found on proxy` will
    have two candidate causes and no way to tell them apart. `LLMService` was deleted in phase 3 and
    `ClaudeCodeService` has none of these names.

    They are **not one group**, and that is the whole point:
    - **Deliberately inert, do not touch.** `switch_mode` in `app-shell/mode.js:61` and
      `chat-panel/events.js:896` is the code/doc mode toggle, already recorded in the paragraph below on
      the surface that is mounted and inert by decision (CC-12). Both guard properly — `mode.js` checks
      `typeof fn !== 'function'` and returns; nothing calls the `events.js` one at all.
    - **Dead, and calling every connect.** `rehydrateLiveAgents` (`events.js:1204`) runs from
      `chat-panel/index.js:515` on every `onRpcReady` and fires `LLMService.list_live_agents`, which
      cannot succeed; `events.js:1213` swallows the failure by matching on `'method not found'`. That
      filter is what has kept it quiet. It cites `specs4/5-webapp/agent-browser.md`, a spec from before
      the conversion, and its `onAgentsRehydrated` trigger (`events.js:1162`) waits on an
      `agents-rehydrated` broadcast that `load_session_into_context` used to send and nothing sends now.
      Downstream of it, `rehydrateAgentTabs` (`tabs.js:675`) and `loadAgentHistory` →
      `LLMService.get_agent_history` are dead with it.
    - **Dead, on paths a user can still reach.** `close_agent_context` (`tabs.js:328`, on tab close),
      `set_agent_selected_files` (the agent-tab branch of the file picker) and `switch_agent_mode`
      (`events.js:928`). The middle one is closed: `files-tab/selection.js` was deleted whole by
      [CC-21](decisions.md#cc-21) along with the checkbox it served.

    What makes this a cleanup rather than a fix is that **CC-8's live half already landed with its own
    mechanism** on 2026-08-17 (item 9): `rehydrateSubagentTabs` (`subagent-tabs.js:576`) driven off the
    stream's own `entry.subagents`, plus `ClaudeCodeService.list_subagent_transcripts` and
    `get_subagent_transcript`. So there are now **two agent-tab rehydration paths, and the older one can
    never work** — the same "two mechanisms answer one question" shape as item 3, except here one half is
    already unreachable. The replacement having shipped is what makes deletion the answer; the risk is
    only that a reader deletes the CC-12 pair in the same sweep.

**The native engine is gone.** `llm_service.py`, `src/aic_dc/llm/`, the four-tier cache and its
membrane, the context manager, the stability tracker, the token counter, the edit protocol and its
pipeline, the history compactor, URL fetching and the `🟧🟧🟧 AGENT` factory: 37 modules, 25,371
lines, plus 52 test files and five dependencies (`litellm`, `tiktoken`, `boto3`, `tenacity`,
`trafilatura`). `grep -rn -i litellm src/` and the same over `webapp/src/` both return nothing.

**The indexes are back, as tools rather than as prompt text** (CC-6). An in-process MCP server named
`aic-dc` exposes six read-only tools — `symbol_map`, `file_symbols`, `find_references`, `doc_outline`,
`review_state`, `ui_state` — sharing the browser's own index objects. A `PostToolUse` hook re-indexes
what the agent writes, and every index-reading tool flushes that queue before it answers, so a file
written this turn is a file the map describes. Verified live: the agent answered a "which module holds
the permission gate" question from `symbol_map` alone, summarised `specs5/plan/` from `doc_outline`
without opening a file, and read back a function it had just written.

The state phase 6 inherits:

- **Suites are green:** python **2945 passed, 75 skipped**; webapp **92 files / 3526 passed**.
- **`Reindexer` is the only thing that knows what the agent wrote.** `take_reindexed()` is
  repo-relative and filtered to files an index cares about; `result['files_modified']` is absolute and
  everything. If the transcript wants a durable "files changed this turn", those are the two sources,
  and they disagree by design.
- **Our own MCP tools are ungated in `can_use_tool`**, by an early return before any dialog is built.
  `classify_tool` returning `"read"` was never enough — it shapes a dialog, it does not skip one — and
  in `acceptEdits` the agent stalled on a prompt for every `symbol_map` call.
- **Four features moved out of the engine rather than dying with it**: commit
  (`claude_code/commit.py`), review (`claude_code/review.py`), the post-write doc-index builder
  (`doc_index/background.py`), and the LSP / snippets / git RPC surface, which folded into
  `claude_code/service.py`.
- **Two panels were replaced, not vacated** — [`decisions.md#cc-17`](decisions.md).
  `context-usage-tab.js` and `usage-hud.js` read `ClaudeCodeService.get_context_usage`, a pass-through
  of the breakdown the CLI's own `/context` prints, and the shell's capacity bar is a third reader of
  the same RPC. **All three now share one derivation** in `context-usage.js` (242 lines): each had
  derived the arithmetic independently and each was wrong on its own terms. The identities that make it
  checkable — categories sum to `totalTokens`, `maxTokens` is *not* pre-reduced by the autocompact
  buffer — are recorded in the interlude entry, and `partitionCategories` verifies the sum rather than
  trusting its own name matching.
- **A guarded fetch that loses its reply stays locked out for the session.** The `if (inFlight)
  return;` idiom nearly every fetch uses clears its flag in a `finally`, and a jrpc-oo call dropped
  during socket replacement never settles. `withRpcTimeout` in `rpc.js` is opt-in — some calls
  legitimately run for minutes — so **any new fetch has to opt in**, with the deadline set *above* the
  backend method's own, never under it. The three `get_context_usage` callers and the history browser's
  fetches are bounded; the rest of the webapp's guarded fetches are not. `get_context_usage` measured
  3–5 s warm and 14 s cold, which is the scale to expect from a control request to the CLI.
- **A revealed tab is told it is on screen.** `_switchTab` notifies the arriving tab's
  `onTabVisible`; the panels refuse to refetch while hidden and mark themselves stale instead. A
  session load is the second staleness path and reuses this contract rather than inventing one.
- **The file picker's deny-read gesture writes a real `Read` deny rule** to
  `.claude/settings.local.json` (CC-14), and says "deny agent read" rather than "exclude from
  index". The L0-invalidation dialog is gone with the cache it asked about; its one honest job —
  the change is not instant — is a once-per-session toast built from the RPC's own `takes_effect`.
  It was the checkbox's third state until [CC-21](decisions.md#cc-21) dropped the checkbox and moved
  the gesture onto the row (shift+click) and into the context menu.
- **The two things that waited on the post-tool-call hook are closed.** The file tree refreshes after
  the agent writes (`filesModified`, session-wide), and the doc index learns about those writes:
  `DocIndexBuilder.note_file_written` now has two callers, `Repo.write_file` for the user's edits and
  the `PostToolUse` re-index for the agent's. **What still escapes is `Bash`** — a `sed -i` or a
  `git checkout` changes files no index hears about until the next full build. Phase 4's largest known
  hole; see [`delivery.md`](delivery.md#deviations-from-inventorymd-1). It is now phase 8 with the
  choice left open ([`decisions.md#cc-18`](decisions.md)). The naming half is settled and on disk: the
  persisted event is `files_written_by_file_tools`, and no field may call itself `files_changed`.
- **One surface is mounted and inert, deliberately**: the code/doc mode toggle, which has no emitter for
  the pushes that drive it and whose replacement is the preset selector (CC-12), deferred by decision. It
  is annotated where it sits rather than half-deleted, because removing a receiver while leaving its
  consumer mounted moves the break instead of fixing it. **Its two `LLMService.switch_mode` call sites are
  the pair open item 11 exempts** — that item deletes the other five names, and this is the one that stays.
  Two former members of this list have left it.
  Phase 5 re-pointed `<aic-history-browser>` at the seven `history_*` RPCs and gave it a way in from the
  chat panel; 2026-08-17 gave the tab strip a producer — a running `Task` opens its own read-only tab
  (CC-8's live half, see [`delivery.md`](delivery.md#interlude--the-tab-a-subagent-never-got-2026-08-17)).
- **17 RPCs are localhost-gated, and four do not look it.** `commit_all`, `reset_to_head`,
  `start_review` and `end_review` delegate, so their `_check_localhost_only()` lives in
  `claude_code/commit.py` and `claude_code/review.py`, not in `service.py`.
- **`collab.py`'s `ContextVar` fix survived the deletion**, as phase 2 required.
  `TestGateUnderRealDispatch` in `test_collab_restrictions.py` is what pins it; that file lost half
  its cases with `LLMService`, and those five tests are the ones that must not go.
- **Nothing in the config layer writes `os.environ`.** The `claude` CLI resolves its own
  credentials; injecting a key or a region would silently change which account a turn bills to.

## What phase 6 will find already there

Phase 6 is the *designed* visualisation, and the correctness rescue is already done — the interlude
found and fixed five wrong readings on screen before phase 5 could inherit the blame for them. What is
in the tree:

- **One derivation, checkable against the engine's own identities.** `context-usage.js` is the single
  owner of the arithmetic all three views share, and `partitionCategories` verifies the sum identity
  rather than trusting its name matching. An unverified payload degrades to an unsegmented bar.
  **No part of the payload's shape rests on a guess any more.** Phase 6 read the wire schema out of the
  bundled `claude` binary, which settled `agents[]` (the live capture returned it empty),
  `messageBreakdown`, the reserve category's second name — "Compact buffer", when autocompact is off —
  and the theme tokens for the two rows the capture could not show. Recorded in
  `3-engine/context-visibility.md`.
- **The same read settled the result footer, and found the HUD pricing the wrong thing.**
  `total_cost_usd` and `modelUsage` are **cumulative across a streaming-input session** — each result
  carries the running total — while `usage` is per-turn but main-agent-loop only. The HUD and the turn
  footer were both printing the session's running total under a heading that said "This turn", and
  naming every model the session had ever used. A turn's cost is a *difference*, its baseline is session
  state, so the engine takes it (`aic_dc/claude_code/cost.py`) and ships `turn_cost_usd`,
  `turn_cost_basis` and `turn_model_usage` beside the cumulative fields rather than replacing them.
  **One name per scope**, so no reader can confuse the two again. The same read also killed the "null
  cost means subscription billing" belief three specs carried: the field has no null branch, so every
  null in the tree is a footer AIC⚡DC wrote itself, and `EngineHealth.credential_source` is the only
  billing-mode signal. Shapes and lifetimes are in `3-engine/context-visibility.md`
  § *Verified field shapes — the result footer*; the browser's single owner of the derivation is
  `webapp/src/turn-cost.js`, which exists for the reason `context-usage.js` does.
- **The staleness contract is `onTabVisible`.** The panels refuse to refetch while hidden — a breakdown
  costs a control request — and mark themselves stale instead. Both a turn completing and a session
  loading go through it.
- **A session load must not pop the HUD.** `session-changed` refreshes the numbers and shows nothing,
  because a HUD that appears on resume reports a turn nobody took. Auto-resume means this fires on
  every server start now, not just on a click.
- **`EngineHealth.mcp` was a field with no writer. Deleted 2026-08-28.** This bullet used to say the
  health payload already carried what a per-server status view needs. It did not: `mcp` was declared on
  `EngineHealth`, serialised by `to_dict()`, and assigned by nothing in `src/`, so every consumer read
  `[]`. **The choice was write it or delete it, and deleting won** — the question it looked like it
  answered already had a better answer, and an empty list does not say "no servers", it says "no
  answer". Nothing broke when it went, which is the clearest evidence available that it was dead. The
  Context tab's Session section therefore calls `ClaudeCodeService.get_mcp_status()` alongside the
  breakdown and tolerates it failing — health is a decoration on the numbers, so its error must not
  replace them. Anything else wanting per-server status has the same two options, and the empty list is
  not one of them. `degradations` is *not* a second attempt at `mcp`: it is the record of capabilities
  the session started **without**, one sentence per loss, written where the loss happens
  (`_build_bridge_wiring`) and read by the chat panel's health banner. A running server's status and a
  server that never started are different questions, and the field with a writer answers the second.
- **A browsed turn and a live turn are the same objects.** `render_messages` returns the block shapes a
  live turn produces and the browser restores them through `restoreMessage`. Anything phase 6 adds to a
  live turn's rendering has to survive arriving that way, or a resumed conversation loses the
  visualisation phase 6 exists to add.
- **One surface is still mounted and inert by decision**: the code/doc mode toggle, whose replacement is
  the preset selector (CC-12). The tab strip is no longer among them — as of 2026-08-17 a running `Task`
  opens its own read-only tab, mirroring the parent turn's blocks for that scope by reference, and the
  subagent row inside Main stays as the evidence the delegation happened. What remains of CC-8 is the
  *listing* over past runs, which the transcript reader already serves: `list_subagents` /
  `load_subagent`, "View subagents (N)" on a settled turn, and read-only `historical:<agent_id>` tabs.

**The mirror's own health is a verdict, not a threshold.** `mirror_gaps_escalated` is computed on
`EngineHealth` against `app.json`'s `history.mirror_gap_tolerance`. A second view of mirror health reads
that flag; it does not re-derive it from a count and a config value.

Three App Config sections `configuration.md` specifies still have no implementation: `Indexing`
(phase 4's), `Permissions` (phase 2's), and `Presets` (deferred by CC-12). The `history` section phase 5
added is the pattern — a callable provider so `reload_app_config()` takes, and a floor per key.

## The one-paragraph version

AIC⚡DC keeps its skin and loses its brain. The browser UI, the jrpc-oo transport, the git
repository layer, the file picker, the Monaco diff viewer, the SVG editor, the document
converter, collaboration admission, and the tree-sitter indexes all survive. Everything that
existed to *assemble a prompt and pay for it efficiently* is deleted: prompt assembly, the
four-tier stability cache and its membrane/flux controller, the token counter, the emoji
edit protocol, the LLM-driven history compactor, URL fetching, and the
`🟧🟧🟧 AGENT` spawn protocol. In their place sits one `ClaudeSDKClient` per repo, and
AIC⚡DC's job becomes *rendering* an agent session rather than *constructing* one.

## Why

Three reasons, in order of weight:

1. **Cost.** The native engine's whole reason for existing is to make a full-repo prompt cheap
   by caching it in tiers. Claude Code sidesteps the problem instead of optimising it: it reads
   what it needs, when it needs it, with its own cache discipline, and — under a Claude
   subscription — the marginal cost of a turn is not a per-token invoice.
2. **Capability.** Claude Code ships agentic behaviour AIC⚡DC does not have and would have to
   build: real tool use, bash execution, web fetch and search, subagents, skills, plugins,
   MCP clients, file checkpointing, self-compaction, and its own edit application with
   checkpoint/rewind.
3. **Maintenance.** ~28k lines of engine become a small adapter. Measured at the end of phase 3:
   **25,371 lines of engine deleted** against **6273 lines in `src/aic_dc/claude_code/`** — three
   times the "~2k" this estimate guessed, because the permission gate (1548 lines) and the message
   pump (979) are real work the estimate did not foresee. Still a 4:1 reduction, and the deleted
   code is the part of the system that was most expensive to reason about and most sensitive to
   provider behaviour changes.

## What AIC⚡DC still contributes

The point of the conversion is not "be a terminal in a browser". Claude Code already has a
terminal. AIC⚡DC contributes what a terminal cannot:

- **Spatial code navigation.** A Monaco diff viewer over every file the agent touches, a
  git-status file tree, an SVG editor, a TeX preview, a 2-D file-navigation grid.
- **Repo intelligence as tools.** The tree-sitter symbol index and document index survive and
  are exposed to Claude Code as MCP tools, so the agent can ask for a whole-repo structural map
  or a document outline in one cheap call instead of grepping for it. See
  [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md).
- **Permission UX with a diff in it.** `can_use_tool` becomes a browser dialog that shows the
  actual proposed edit as a rendered diff, not a `y/n` on a text hunk.
- **Context transparency.** `get_context_usage()` gives us `/context` as a live, clickable
  visualisation instead of a slash command that prints once.
- **Multi-client collaboration.** Two people watching one agent session, with admission control.
- **Documents.** Doc convert, doc mode outlines, SVG-as-document indexing — none of which
  Claude Code knows about.

## Phases

Each phase is independently shippable and leaves the tree working. Phase 0 is this pass.

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Plan and specs** ✅ | This directory + the specs5 rewrite. No code changes. | specs5 describes the target state; `plan/inventory.md` names every file to keep, delete, or add. |
| **1. Engine spike** ✅ | `src/aic_dc/claude_code/` — session, options, message pump. Registered as a second service alongside `LLMService`; not yet wired to the UI. | A CLI-side smoke test can send a prompt and print the streamed message taxonomy. |
| **2. Chat on the new engine** ✅ | Frontend chat panel renders the Claude Code message stream (text, thinking, tool-use cards, tool results, result summary). Permission dialog lands. `LLMService` still constructed but no longer reachable from the chat path. | A user can hold a full working conversation, including edits, entirely through Claude Code. |
| **3. Rip-out** ✅ | Delete `src/aic_dc/llm_service.py`, `src/aic_dc/llm/`, the cache/context/edit/compaction modules, and the frontend surfaces that fed them. Replace the HUD and Context tab with minimal panels over the SDK's own numbers rather than vacating them ([`decisions.md#cc-17`](decisions.md)). | `grep -r litellm src/` is empty; test suite green. |
| **4. Restore the indexes as tools** ✅ | In-process MCP server exposing the symbol map, doc outlines, and reference graph. Monaco LSP paths re-pointed at the surviving index. | Claude Code can call `symbol_map` / `doc_outline`; hover and go-to-definition still work in Monaco. |
| **5. History and sessions** ✅ | A fresh `SessionStore` over `.aic-dc/` — all six protocol methods, entries verbatim, `history_store.py` retired ([`decisions.md#cc-19`](decisions.md)) — plus resume/fork, and the history browser and full-text search re-pointed at the mirrored transcript. Also the readers four specified-but-undelivered warnings never had: the mirror gap at both scales, the disk warning, and the health banner. **Shipped without a live CLI run**; the two interludes after it are what that cost, and the criterion was verified live in the second — see the [phase-5 entry](delivery.md#phase-5--history-and-sessions-2026-08-16). | Restarting the server resumes the previous conversation with context intact, and `session_store_conformance` passes. **Met live**: `--resume` on the CLI child, the earlier conversation answered with no tool calls, no fork in the transcript. |
| **6. Context and cost visualisation** ✅ | Both panels exist as of phase 3 (CC-17); their tests and first live run landed ahead of phase 5, and that run spent this phase's correctness budget on the *context* numbers, which now match `/context`. The **cost** numbers turned out to owe a correctness pass of their own: the row below expected "a turn that fails late" to be the unpriced case, and reading the CLI's wire schema found the opposite — every turn was mispriced, because `total_cost_usd` and `modelUsage` are cumulative and both readers printed them as one turn's. Done: the difference is taken in the engine, `turn_cost_basis` names why a figure is absent when it is, the autocompact point is marked on the context bar, the sections are built — Usage over the engine's own categories, Session over what the session was started with including MCP server health and the `aic-dc` tool inventory with its token cost, Debug over the engine rather than the code — and a bridge or hook that did not start now reports the loss in the health banner instead of the log line nobody read. The live run then found four things no test could have, all fixed, and all four about what a reader is *told*: see the [phase-6 entry](delivery.md#phase-6--context-and-cost-visualisation-2026-08-17). | The Context tab shows the designed visualisation over those numbers, names the `aic-dc` tools it is paying for, and distinguishes a turn that cost nothing extra from one whose cost is unknown. **All three met**; the first two live on 2026-08-17, the third live later that day via the `window.__phase6` recorder — two consecutive turns, `1.51561 + 1.210191 = 2.725801`, chip reading **$1.21 not $2.73**, so the figure is a difference and not the running total. Its "nothing extra" and "cost unknown" renderings hold in 60 tests but remain unobserved, because no ordinary turn causes them. |
| **7. Packaging** | Platform-specific wheels or an explicit external-CLI mode; the bundled CLI is ~295 MB. | A fresh machine can install and run without a manual `npm i -g @anthropic-ai/claude-code`. |
| **8. Index freshness after `Bash`** | Close phase 4's largest known hole per [`decisions.md#cc-18`](decisions.md): a filesystem watcher, or an explicit spec statement that `Bash`-driven writes are not tracked. The choice is open; "nothing, documented" is a legitimate exit. | A file changed by `sed -i` or `git checkout` is either reflected in the indexes, or its absence is stated in `2-indexing/` and surfaced to the user rather than silent. |

Phases 1–3 were the risky ones and were not interleaved: the native engine stayed intact and
reachable until phase 2's exit criterion was genuinely met, and the deletion then landed in one
commit of 189 files, **+6228 / −69527**.

Phase 3's footprint was wider than its row implied, and the grep is why we knew. `litellm` was
reachable from ten files at the end of phase 2 — `llm_service.py`, `llm/_commit.py`,
`llm/_helpers.py`, `config.py`, `main.py`, `settings.py`, `token_counter.py`, `context_manager.py`,
`history_compactor.py` and `logging_setup.py` — four of which are not obviously "engine" files. An
exit criterion written as a file list would have missed them; written as a grep, it did not.

A phase is recorded in [`delivery.md`](delivery.md) when its exit criterion is met — what landed,
what was deliberately left out, and what the next phase has to do first.

## Ordering constraints that are not obvious

- **Permissions before edits.** Do not ship phase 2 with `permission_mode:
  "bypassPermissions"` as a shortcut. The permission dialog is the feature; a build that writes
  files without asking will train users to distrust the tool, and retrofitting the dialog after
  people have muscle memory for silent edits is worse than building it first.
- **Indexes after the rip-out, not before.** *Satisfied in phase 4.* The indexes had two consumers
  (prompt assembly and the browser); deleting the prompt-assembly one first meant the MCP bridge was
  written against one clear consumer instead of two competing ones. It paid off in a way worth
  recording: the bridge takes provider *callables* rather than index objects, which only reads as
  obviously right once the browser is the sole other reader of the same objects.
- **`SessionStore` before history-browser work.** The store determines the on-disk shape; the
  browser reads it. Building the browser first bakes in assumptions about a format we have not
  chosen yet.
- **Packaging last but not never.** It is the least interesting phase and the one most likely to
  block a release. See [`risks.md`](risks.md#r-7--bundled-cli-size-and-platform-specific-wheels).

## Reading order for this directory

1. [`decisions.md`](decisions.md) — the binding choices, each with its rationale. Read this first;
   the specs assume it.
2. [`inventory.md`](inventory.md) — keep / delete / add, file by file.
3. [`sdk-surface.md`](sdk-surface.md) — verified Agent SDK API surface, the corrections it
   forces on the origin brief, and [§ The probe](sdk-surface.md#the-probe): the module and test that keep
   its inventory honest as the wheel moves, and what they still cannot see.
4. [`risks.md`](risks.md) — the register, with mitigations and the tripwires that tell us a risk
   has fired.
5. [`origin-brief.md`](origin-brief.md) — the document that started this, preserved as written.
   Superseded by the above where they disagree; `sdk-surface.md` lists where.
