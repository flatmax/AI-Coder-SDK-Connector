# Open Work — Background-Subagent Fix List

**Status:** paused mid-list on 2026-08-24. Items 1–4 are committed; 5 and 6 are not started. This file
is the handoff note for resuming, and should be deleted when the list is finished.

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

Each has specs: `specs5/3-engine/session.md` § *Every result the drain reads is emitted*,
`specs5/3-engine/history.md` § *A background agent's wake-up*, `specs5/5-webapp/chat.md`
§ *A Turn Can End More Than Once* and § *Markdown Rendering*.

## 5. The inline subagent block repeats itself

**Symptom, from the live run:** one assistant bubble said "single commit" three times.

**Where to look.** `renderSubagentRow` in `webapp/src/chat-panel/block-render.js:882` draws, in order:
`row.description` (or `task_type`) as the head, then `row.summary` as a line under it, then the
subagent's own blocks indented beneath. The main turn *also* carries a `Task` tool card whose input
summary is that same description, and the assistant's own text usually restates it a fourth time. So
the repetition is several sources agreeing, not one renderer looping — which means the fix is a decision
about which of them is the one worth showing, not a bug hunt.

**Intended shape:** collapse the block by default. The row is evidence the subagent ran and the way into
its transcript; its blocks are a second copy of a transcript that already has a tab. What was not yet
decided is whether `row.summary` survives the collapse — it is the only line that says what the subagent
*concluded*, and it is also the line most likely to be identical to the description.

**Where the investigation stopped:** I was about to trace where `row.summary` is set
(`webapp/src/chat-panel/streaming.js`, around the freeze at line 654) to find out whether it is the
notification's `<summary>` — in which case it duplicates the description by construction — or the
subagent's own closing text. Resume there.

**No live data survives for this.** A restored session does not rebuild the `subagents` map, so the dev
frontend's current transcript has `rows: []` on every turn — verified. Reproducing needs a fresh
delegated turn on the dev backend, not a reload.

## 6. Post-refresh and boot state

Three separate faults, all about state that does not survive a page load:

- Subagent tabs are lost on refresh.
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

- **Python:** `pytest tests/ -q` — **3337 passed**, nothing failing or skipped, measured with items 1–4
  committed.
- **Frontend:** `npx vitest run src/` in `webapp/` reports **63 failures that predate this list**, all
  in `deriveAgentTabLabel`, `parseAgentTabId` and the chat-panel tab suites — leftovers from `a0cb83b`
  removing the agent-spawn protocol. Measure a baseline with `git stash` before believing a new failure
  is yours; the whole list so far has introduced none.
