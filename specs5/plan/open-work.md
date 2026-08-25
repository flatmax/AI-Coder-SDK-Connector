# Open Work — Background-Subagent Fix List

**Status:** resumed 2026-08-25. Items 1–6 are done bar one fault that would not reproduce (6c — worth
one check against the live session), the frontend test baseline is green again, and `spare_subagents`
has finally been exercised live. This file is the handoff note for resuming, and should be deleted once
6c is settled.

The list came out of one live run on the dev backend: a turn that delegated to a background subagent,
watched from the browser rather than from tests. Everything below is something that run surfaced, in the
order it was worth fixing.

## Done

| # | What | Commit |
|---|---|---|
| 1–2 | Subagent LED status at turn end; empty subagent tab | `c4ccd95` |
| 3(a) | The drain emits every result; the browser revises the settled message | `e3d0e9c` |
| 3(b) | A background agent's wake-up renders as a system note on replay | `fa572ce` |
| 4 | Raw HTML in a message renders as the text it was written as | `1d40022` |
| 5 | The inline subagent block collapses; the type chip names the agent | `5ca28b7` |
| 6a–6b | A settled turn keeps its subagent rows on refresh; the boot LED reads idle | *uncommitted* |

Each has specs: `specs5/3-engine/session.md` § *Every result the drain reads is emitted*,
`specs5/3-engine/history.md` § *A background agent's wake-up*, `specs5/5-webapp/chat.md`
§ *A Turn Can End More Than Once*, § *Markdown Rendering* and § *What a Row Shows, and What It Drops*.

### What item 5 turned out to be

The open question was whether `row.summary` duplicates the description by construction. It does not.
A headless CLI capture (`claude -p … --output-format stream-json`, 2026-08-25) settled it: a task
described as "Find magic word in README" reported `summary: "The magic word is **ORCHID**."` — the
subagent's own closing answer. So the summary stayed, and now renders as the markdown it arrives as.
What was dropped instead is the nested transcript, behind a disclosure that counts the calls it would
draw, and the `task_type` chip.

The same capture turned up a second fault the live run had not isolated: **`task_type` is the transport
kind** (`local_agent`, `local_bash`), not the agent's. The agent's own kind rides as `subagent_type`,
which no SDK dataclass hoists, so the engine never forwarded it and every subagent chip and label
fallback read "local_agent". The engine now forwards it and everything user-facing labels with it.

### Left over from item 5: the row head followed the activity string — **fixed**

The row's head showed the *last* description an event carried, because the CLI overwrites the field as
the task runs: `task_started` gave "Find magic word in README", then `task_progress` replaced it with
"Reading README.md", and that is what the settled row was left headed with. The first non-empty
description now latches, in both folds — `applySubagentEvent` and `_record_subagent` — so a row rebuilt
from a reconnect snapshot and one folded from live events agree. The activity is not lost: it is the
`last_tool_name` chip beside it. The tab strip had already made this decision for its labels
(`resolveLabel`: "the SDK's live description is an *activity* string, not the task"), so the row now
agrees with its own label instead of drifting away from it.

The latch is "first non-empty wins", not "only `task_started` may set it" — a row whose start event was
missed must still be able to name itself. Both sides have a test for that case.

## 6. Post-refresh and boot state

Three separate faults, all about state that does not survive a page load. Two are fixed and verified
live; the third did not reproduce.

### 6a. A settled turn's subagents are lost on refresh — **fixed**

The row, its summary and the "View subagents" affordance all vanished from a turn that had shown them a
moment earlier, because `render_messages` reported an empty `subagents` list for every turn read off
disk. The belief behind that — "the transcript does not attribute a subagent to the turn that spawned
it" — turned out to be false. The spawn call is a tool block in the turn (`Agent`, or `Task` in older
transcripts) carrying `description` and `subagent_type`, and its result names the subagent in prose:
`agentId: a9f5687c0b6a0904f`. `_note_subagent` in `src/aic_dc/claude_code/history.py` rebuilds a row per
spawn call from those; `restoreMessage` already passed `subagents` through, so nothing changed in the
browser. Verified on the dev backend: four restored turns came back with their rows, and a row's button
opened the subagent's real transcript from disk.

A restored row has no status, usage or summary — those are `Task*` event fields with no home on disk,
and are left absent rather than defaulted. Spec: `specs5/3-engine/history.md` § *A rendered turn reports
the subagents it spawned*.

### 6b. A stale `Main: running` LED at boot — **fixed**

`getLedState` fell through to cyan for a tab with no outcome that was not streaming, on the reasoning
that a tab only existed because `agentsSpawned` had created it for a stream in flight. `a0cb83b` removed
that protocol, so the branch was only ever reached by Main on a freshly loaded page. Every finished turn
writes an outcome — `computeTurnOutcome` returns `clean` even for a cancelled turn — so "neither" now
means only "nothing has run here yet", which is a fifth LED state: grey, `idle`. Verified live: a
reloaded page reads `Main: idle` and does not pulse. One test had been pinning the bug, asserting a
freshly mounted panel's tooltip was `Main: running`.

### 6c. The `Bypass permissions?` confirm on page load — **not reproduced**

This did not reproduce on the dev backend. Instrumenting `onPermissionModeSelect` to record every
`change` it received showed **nothing at all** on the loads where a dialog appeared — the app was not
raising it. What was: the CDP harness re-surfacing a native `confirm` it had already handled, once per
navigation after one had been raised. With `window.confirm` stubbed in-page so no native dialog ever
existed, reloads were clean.

Note that the "selector still reads Ask" half needs no page-load story at all: `onPermissionModeSelect`
resets the control to the engine's mode *before* drawing the confirmation, so the mismatch is on screen
the first time too — and on a backend not launched with `--dangerously-skip-permissions` the engine
refuses the change, so the mode never moves.

Guards went in anyway, since a `change` here authorises a destructive confirmation and a browser
restoring form state can raise one: `autocomplete="off"` on the control, plus a gesture latch — a
pointer or key on the `<select>` must precede the `change`, because `isTrusted` cannot separate a
restored event from a click. **Worth checking against the live session**, where it was originally seen;
if it still happens there with these in place, the mechanism is something else and this is the place to
start over.

## Undecided / watch-list

- **Option B — session-lifetime pump, or per-translator routing.** The alternative to the drain that
  3(a) built on. Left open deliberately: the agreement was to watch for A's residual mis-attribution
  first and only take B if it shows up. Nothing has shown up yet.
- ~~**A subagent permission ask landing inside the turn window.**~~ **Done — exercised live on
  2026-08-25, and it works.** See below for what it took and what it showed.

## `spare_subagents`, finally exercised

It works end to end. The run: a turn spawns a background `general-purpose` subagent whose first act is a
gated `Bash`, **and** then runs a gated `Bash` of its own. Both asks land against the same turn. Answer
only main's; the turn ends, and the engine logs

```
Permission perm-…-2vpe98 for Bash left open past the end of turn 1787620595247-76z4se:
subagent aa28597609d094a45 is still waiting on it
```

The subagent's dialog survived the turn, and answering it afterwards let the subagent run — its
`touch` landed after its parent turn was over.

**Why every earlier run missed it, precisely.** `note_permission_prompt` attributes a request to
`session._active_turn`, and that is cleared when the turn's **result** arrives, not when the drain ends.
A background subagent normally reaches its first gated call a second or two *after* the parent has
finished replying, so the request carries no `request_id` at all — the logs read `turn None` — and
`cancel_for_turn` skips it on the `request_id` mismatch before `spare_subagents` is ever consulted.
Two attempts missed by about one second each.

So the branch only matters in the narrow case where the subagent is *already blocked* when the parent's
result arrives; the thing that protects a background subagent's dialog the rest of the time is
`cancel_for_agent`, when the subagent reaches a terminal status. Making main block on its own permission
is what holds the turn open long enough to engineer the narrow case, and is the recipe to reuse.

## Noticed, deliberately not acted on

- **`postResponseComplete` fires only after a turn's *first* result**, so the Context tab does not
  refresh after background work finishes; it picks the change up on the next turn. Recorded as an
  invariant caveat in `specs5/3-engine/session.md`.
- **`response_text()` joins blocks with no separator** (`"I'll read the file.SPAWNED"`). Harmless
  because the UI renders blocks rather than that string, but it is a trap for the next caller.
- **The notification system-event card still carries the message toolbar** (📋 ↩ 🔊), consistent with
  the other `system_event` cards (commit, reset, permission-mode) and unlike the compaction divider,
  which has a dedicated renderer and no toolbar.

## Resuming the dev backend

UI work on this list is verified against a **dedicated dev backend**, never the live session:

```
.venv/bin/aic-dc --repo-path /tmp/aicdc-uitest --server-port 18090 --webapp-port 19010 \
    --no-browser --dev --verbose
```

Read the real ports off the startup log rather than trusting the numbers above. To stop it, iterate
`pgrep -f aicdc-uitest` and check `/proc/$p/cmdline` — a `pkill -f` whose pattern appears in its own
command line kills the shell that ran it.

Frontend DOM notes for driving it: `aic-chat-panel` is at shadow-DOM depth 2 (walk the shadow roots
recursively), `panel._tabs` is a `Map`, and `panel.messages` is the active tab's view. RPC goes through
`document.querySelector('aic-app-shell').call['ClaudeCodeService.<method>'](...)`.

Three traps that cost time on 2026-08-25:

- **Send through the input box, not `chat_streaming` directly.** The panel routes chunks by request id,
  so a turn it did not start lands in no tab: the transcript stays empty and no subagent row appears.
  Set `.input-textarea`'s value, fire `input`, then a `keydown` of Enter.
- **A native `confirm` handled through CDP re-appears on the next navigation.** It looks exactly like
  the dialog replaying on page load, and it is what 6c chased. Stub `window.confirm` in the page when
  testing anything that raises one, and trust in-page instrumentation over what the harness reports.
- **Editing frontend source while the page is open triggers a Vite reload**, which drops live subagent
  tabs mid-inspection. Backend edits need a full restart — Python is not hot-reloaded, so a
  `history.py` change is invisible until you stop and relaunch.

## Test baselines

**Both suites are green.** A new failure is now yours, and no longer has to be diffed against a list of
expected ones.

- **Python:** `pytest tests/ -q` — **3353 passed**, nothing failing or skipped.
- **Frontend:** `npx vitest run src/` in `webapp/` — **3974 passed across 99 files**, none failing.

The frontend was carrying **63 failures predating this list**, all left by `a0cb83b` removing the
agent-spawn protocol. Cleared on 2026-08-25, by deletion where the subject was gone and by rewriting
where the behaviour survived:

- `chat-panel-agent-labels.test.js` — deleted whole (30). Every test drove `deriveAgentTabLabel`, which
  went with the protocol. Its one surviving concern, truncation at `_AGENT_LABEL_MAX_LENGTH`, moved to
  `view-subagents-load.test.js`, which covers the `📜` transcript labels that still read the cap.
- `tabs.test.js` — three suites deleted (26). "tab close — behavior" and "— guards" drove `_onTabClose`;
  "close-tab backend wiring" asserted it fired `close_agent_context`. `tabs.js` records that the
  primitive is gone, that no UI gesture ever bound to it, and that both surviving kinds of tab sweep
  themselves. The "tab close — rendering" group stays: that the button is *absent* is still worth
  pinning. "tab mode storage" went too — see the `_tabModes` note below.
- `helpers.test.js` — the `parseAgentTabId` group deleted (6); the function is gone.
- `streaming.test.js` — one test rewritten (1). It asserted `agents-spawned` *did* spawn tabs, directly
  contradicting the test above it. It now pins that a well-formed payload spawns nothing, beside the
  malformed case — "spawns nothing" only means something if a valid payload also does.

**Found while clearing them: `panel._tabModes` has no writer.** It is read by the tab chips and the LED
tooltip and is filled only by `test-helpers.js`, so in production it is permanently empty and the mode
segment those two render can never appear. Retiring the map (and the `mode` parameter threaded through
`renderLedRow` and `formatLedTooltip`) is a small, separate cleanup — not done here because it is a
production change and clearing the baseline was meant to be a test-only one.
