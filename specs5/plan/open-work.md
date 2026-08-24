# Open Work — Background-Subagent Fix List

**Status:** resumed 2026-08-25. Items 1–5 are done; 6 is not started. This file is the handoff note for
resuming, and should be deleted when the list is finished.

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
| 5 | The inline subagent block collapses; the type chip names the agent | *this change* |

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

### Left over from item 5: the row head follows the activity string

The row's head shows the *last* description an event carried, and the CLI overwrites it as the task
runs: `task_started` gave "Find magic word in README", `task_progress` replaced it with "Reading
README.md", which is what the settled row is still headed with. The tab strip already solved this —
`resolveLabel` in `subagent-tabs.js` latches the `Task` card's description and comments that "the SDK's
live description is an *activity* string, not the task" — but the inline row has no such latch, and its
live activity is already carried by the `last_tool_name` chip beside it. Not fixed here: it changes what
`description` means in the fold (`applySubagentEvent` and `_record_subagent` both patch cumulatively, and
the spec says so), which is a decision of its own rather than part of the collapse.

## 6. Post-refresh and boot state

Three separate faults, all about state that does not survive a page load:

- Subagent tabs are lost on refresh. Re-confirmed on 2026-08-25 by accident: a Vite HMR reload during
  item 5's verification took the settled turn's subagent row and tab with it, mid-inspection.
- A stale `Main: running` LED at boot.
- The `Bypass permissions?` confirm replays on page load, with the mode selector still reading "Ask" —
  so the dialog asks about a change that is not in effect.

## Undecided / watch-list

- **Option B — session-lifetime pump, or per-translator routing.** The alternative to the drain that
  3(a) built on. Left open deliberately: the agreement was to watch for A's residual mis-attribution
  first and only take B if it shows up. Nothing has shown up yet.
- **A subagent permission ask landing inside the turn window.** `spare_subagents` in
  `src/aic_dc/claude_code/permissions.py` still has not been exercised by any live run — every run so
  far had the subagent's asks land outside the window. Worth engineering a run that hits it.

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

## Test baselines

- **Python:** `pytest tests/ -q` — **3343 passed**, nothing failing or skipped, measured with item 5
  applied.
- **Frontend:** `npx vitest run src/` in `webapp/` reports **63 failures that predate this list**, all
  in `deriveAgentTabLabel`, `parseAgentTabId` and the chat-panel tab suites — leftovers from `a0cb83b`
  removing the agent-spawn protocol. Measure a baseline with `git stash` before believing a new failure
  is yours; the whole list so far has introduced none. Item 5 was checked exactly that way — 68 with the
  change, 63 stashed, and the five were its own fixtures asserting the old `task_type` vocabulary.
