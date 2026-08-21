# Subagent Browser

Subagents surface as additional read-only tabs in the existing chat panel. There is no separate browser
view and no dedicated UI protocol. Each subagent has a transcript; the chat panel already renders
transcripts; adding tabs that cannot be typed into is the entire user-facing change.

This file replaces the old `agent-browser.md`, and the difference is the whole point of it. That design
described a **writable** agent team: one interactive chat tab per spawned agent, each with its own
`ContextManager`, its own file selection, its own input box, retaskable by id across turns, closable
individually. None of that survives, because AIC⚡DC no longer spawns agents.

A subagent here is created by the agent's own `Task` tool, inside a turn, for its own reasons. It has no
seam a third party can speak into. What is left is observation plus a kill switch — which, in practice,
is what the old design was actually used for.

## What the Browser Can and Cannot Do

| | Old agent tabs | Subagent tabs |
|---|---|---|
| Who spawns | AIC⚡DC, from a parsed `🟧🟧🟧 AGENT` block | The agent, via its `Task` tool |
| Send a message to it | Yes | **No** — there is no channel |
| Grant it files | Yes, per-tab selection | **No** — it inherits the agent's tool access |
| Change its mode | Yes | **No** — there are no modes |
| Stop it | Close tab (killed the scope) | Yes, `stop_task(task_id)` |
| Read its transcript | Yes | Yes |
| Lifetime | Session-scoped, retaskable | Turn-internal; terminal status ends it |
| Identity | LLM-chosen id from the spawn block | SDK agent ID |

Everything in the "No" column is a real capability loss, and it is worth being plain about it: a
subagent that goes off course cannot be redirected, only stopped. The compensating change is that a
subagent cannot go off course *quietly* — its tool calls are gated by the same permission dialog as the
main scope, tagged with its `agent_id` (see [permission-dialog.md](permission-dialog.md)).

## Tab Strip

The chat panel keeps a tab strip along its top edge:

- **Main** — always present. The user↔agent conversation, with the full input surface.
- **One tab per subagent** — appears when a `subagentEvent` of type `started` arrives. Labelled with an ordinal and one keyword — `1 headings`, `2 test-files`.

This spec originally said the label was the `task_type` and a truncated `description` — "explore: find
auth call sites". The first live fan-out retired that: the real `task_type` is `local_agent` for every
subagent, spending 13 identical characters to distinguish nothing, and the event's `description` is the
SDK's *live activity* string, so a tab ended up named after whatever its subagent was doing when its last
event landed. Four such labels filled the strip and none of them said what the user had asked for.

So the label is an **ordinal plus one keyword**, and the sentence lives in the tooltip and in the feed's
opening line, which are the places with room for it. The ordinal is assigned when the tab is created and
never recomputed: numbering by strip position would rename tabs under the user's cursor as earlier ones
close. The words come from the spawning `Task` call's `input.description` and `input.subagent_type` — what
the delegation actually asked for — with the live activity string as the fallback, and the upgrade to the
real description latches so a later activity string cannot undo it. The keyword itself is a heuristic:
the last word of the description, since English puts the object last, reaching back past stopwords when
that word identifies nothing on its own ("check the tests" → `check-tests`), paths reduced to their
basename, and 14 characters at most — the whole point being the visibility of the *other* tabs.

When the strip exceeds the viewport width it scrolls horizontally, with a menu affordance listing all tabs
by description for direct access.

Each tab carries two inline affordances, invisible by default and fading in on hover / active / focus:

- **Live indicator** — a small pulsing dot while the subagent's status is non-terminal, visible on every tab regardless of active state, so a user sees work happening on tabs they are not viewing.
- **⏹ Stop** — on live tabs only. Calls `stop_task(task_id)` after a confirmation. This is the only write affordance in this entire spec.

There is no per-tab close affordance and no 📊 context icon. A subagent has no context breakdown of its
own to show: `get_context_usage()` reports the session's context, and the subagent's token consumption
appears in its own row and in the Context tab's subagent inventory
(see [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md)).

## Tab Lifetime

A subagent tab's life is bounded by the subagent, not by the session:

- **Terminal status** — the tab stops pulsing and becomes an archived transcript in place. It stays in the strip for the remainder of the turn so a user can read what happened. A terminal status may arrive via `updated` with **no** `notification`; the tab must settle on either (see `specs-reference/3-engine/session.md` § A task can finish without a notification).
- **Turn end** — on `streamComplete`, live tabs that have not reported terminal status are marked *status unknown* rather than silently completed. The turn is over; a subagent that never reported is a fact worth showing, not a spinner to leave running.
- **New turn** — the previous turn's subagent tabs leave the strip. Their transcripts remain on disk under the session's `subagents/` directory and stay reachable through the history browser.
- **New session / session resume** — the strip drops to Main alone. Nothing is torn down on the server, because AIC⚡DC owns no subagent state to tear down.
- **Server shutdown** — in-memory tab state is lost; transcripts on disk survive.

The old spec's `new_session`-dismisses-the-team rule, its asymmetry argument about main's
`ContextManager` surviving while agents' were freed, and the whole `close_agent_context` /
`agentClosed` protocol are gone with the registry they managed.

### Refresh and Reconnect

A refresh rebuilds the chat panel without disturbing the engine. Rehydration is a single read:

- `get_current_state` reports the active turn's subagents in its `active_streams` entry — `{agent_id, task_id, description, task_type, status, last_tool_name, usage}` per subagent. The panel recreates the tabs from that list. Tab creation is idempotent, so a later `subagentEvent` for the same `agent_id` updates rather than duplicates. Each entry also carries `terminal` (the engine's own verdict that the task ended, so the browser need not track the SDK's growing status vocabulary) and `tool_use_id` (the spawning `Task` call, which the replayed blocks name as their `agent_id` — without it a rebuilt tab cannot claim its own feed).
- A **live** tab needs no read at all: the snapshot's block list is the whole turn, subagent blocks included, so the same mirroring that feeds the tab live refills it on replay. Reading the transcript would fetch a second copy of what is already on screen.
- A tab read from **disk** fetches with `get_subagent_transcript(agent_id)` when the user opens it, not eagerly. A turn that fanned out to twelve subagents should not cost twelve transcript reads on reconnect.
- Live status resumes from subsequent `subagentEvent` messages. A subagent that reached a terminal status while the browser was away shows as terminal on reconnect, because status comes from the snapshot rather than from having watched the event go by.

What is lost across refresh: per-tab scroll position, and the in-flight tail of any subagent whose
events were emitted while the socket was down. The transcript on disk is authoritative and the tab
reads from it, so the loss is cosmetic rather than a gap in the record.

There is no `list_live_agents()` and no `get_agent_history()`. Both read a registry that does not exist.

## Status LEDs

The main tab's header carries a compact row of status LEDs — one dot for the main conversation plus one
per subagent tab currently in the strip. The LEDs are a derived view of state, giving ambient awareness
without forcing a scan of tab labels.

The main-tab LED is always present and follows the same colour rules. Its presence regardless of whether
subagents exist makes the LED row the canonical place to look for "which conversation is active right
now" — clicking the main LED switches focus back to Main the same way a subagent LED activates its tab.

Each LED has four states:

- **Flashing cyan** — non-terminal status. The subagent is working.
- **Solid green** — terminal status `completed`.
- **Solid red** — terminal status `failed`, or the parent turn ended in an error while this subagent was live.
- **Solid amber** — terminal status `stopped` / `killed` (the user stopped it), or *status unknown* at turn end.

Amber is new, and it exists because a stopped subagent is neither a success nor a failure and rendering
it as either is a lie. The old spec's green rule — "every `EditResult` in the result reports success" —
has no successor: edit outcomes are the agent's business now, reported as tool results inside the
transcript rather than as a structured array we can total up.

LED lifetime tracks its tab's lifetime exactly. There is no acknowledgement gesture, no auto-fade, no
"seen" state; the LED reflects current state until the tab leaves the strip.

### Click and hover

- Clicking a LED activates that subagent's tab — the same effect as clicking the tab itself, but the LED row is more compact and sits where the user's eyes already are. The tab strip also scrolls to reveal the target tab's button if it was offscreen, so the LED row works as a navigation primitive when many subagents have pushed the active tab beyond the visible window. Already-visible tabs do not jiggle — the scroll is a no-op when the button is on-screen.
- Hovering shows a tooltip carrying the description, status, and the state-specific detail:
  - Cyan: `<description>: running — <last_tool_name>`
  - Green: `<description>: completed (<tool_uses> tools, <total_tokens> tokens)`
  - Red: `<description>: failed`
  - Amber: `<description>: stopped` or `<description>: status unknown at turn end`

`last_tool_name` is what makes a cyan LED useful rather than decorative — "running" alone says nothing,
"running — Grep" says the subagent is still looking and has not started writing.

### Layout

The LED strip sits below the chat panel's input textarea. Dots are centered horizontally and sized to be
unobtrusive (small enough that 8–10 fit on one line without wrapping; the strip wraps to a second line
when necessary). No background, no border — the strip floats over the input area's surface, costing no
extra vertical space compared to a separate container.

The strip is always visible while the chat panel is mounted; at minimum it carries the main-tab LED.
With no subagents it shows exactly one dot. Insertion order is preserved (main first, then subagents in
start order).

The old "above the compaction-capacity bar" placement no longer means anything — there is no capacity
bar, because AIC⚡DC does not model the context window as a budget it fills. Context pressure lives in
the Context tab and the usage HUD.

## Tab Content

A subagent tab renders its transcript through the same pipeline as the main chat:

- Assistant text with markdown, syntax highlighting, and KaTeX
- Thinking regions, collapsed
- Tool cards with their results, identical rendering to the main tab (see [chat.md § Tool Cards](chat.md#tool-cards))
- File mentions navigate to the diff viewer — the one interactive affordance that carries over, because navigating to a file is a read
- Message-level toolbars for copy and paste-to-prompt. Paste-to-prompt targets **Main's** input, which is the only input there is; a user who wants to follow up on something a subagent said does so by talking to the agent

There is no input box, disabled or otherwise. A greyed-out textarea implies a channel that might open
under some condition, and none exists. The whole composing surface goes with it — the recalled-input
list, the snippet drawer, the attached-image strip, the send button — replaced by one line saying why:
"Read-only transcript — there is no channel to a subagent. Switch to Main to send a message."

Two things below the transcript do stay, because they are true on every tab rather than about this one:
the action bar's permission-mode selector (what the agent is allowed to do next, which must never be
hidden — see [chat.md](chat.md)) and the LED row (which conversation is live). The session group — ✨ and
📜 — is dropped, as it is on any tab but Main: a subagent transcript has no session of its own to restart.

### Tool cards in a subagent tab

Tool cards arrive on the parent turn's request ID with a non-null `agent_id`. The panel routes them two
ways at once, and both are correct:

- Indented under the subagent's row in the Main tab, so a user watching Main sees the fan-out without switching tabs
- Into the subagent's own tab, in arrival order

The same card object drives both renderings; there is no duplication of state, only of placement.

## Historical Transcripts

Past turns' subagents are reachable without leaving the chat:

- Scrolling Main back to a previous turn surfaces a "View subagents (N)" affordance beneath that turn's assistant message. A single subagent reads "View subagent (1)"; a subagent row's own description is also a link, opening that one transcript.
- Clicking it populates the tab strip with that turn's subagent transcripts, read from disk via `get_subagent_transcript(agent_id, session_id)`. A subagent whose live tab is still in the strip is skipped — the live tab is the better view of it, and a second tab for the same subagent would be two views of one thing.
- The next such click clears the previous one's transcripts, so the strip does not accumulate them, and so does a session change: a transcript belongs to the session that spawned it, and a tab labelled with one session's task showing another's would be worse than no tab. A read still in flight when either happens is abandoned rather than allowed to land in a strip that has moved on.
- The history browser lists a session's subagents alongside its messages, from `list_subagent_transcripts(session_id)`; opening one from there does the same thing for a session that is not the live one, passing that session's id explicitly, and closes the modal so the tab it asked for is visible. A row is labelled by `description` and `agent_type` where the session was mirrored live, and by the opening words of the subagent's prompt where it was not (see [chat.md § History Browser](chat.md#history-browser)).

The affordance is a **live-run** affordance. A turn read back off disk carries no subagent rows — the
transcript records each subagent under its own id but does not attribute it to the turn that spawned it
(see [`../3-engine/history.md`](../3-engine/history.md#subagent-transcripts)) — so a resumed session's
turns offer nothing here, by omission rather than by oversight, and the history browser's session-level
listing is the way into them.

A transcript tab is labelled `📜 <description>` and drawn muted and italic, with "— subagent transcript
(read-only)" appended to its tooltip. The old spec said historical and live tabs "differ only in whether
the live indicator is drawn", which was true of a strip where the live tabs exist to compare against; a
strip of nothing but transcripts needs the label itself to say what it is holding. The deeper
simplification stands: read-only is not a *mode* a tab enters, the way it was in the old design — every
tab in this spec is read-only, so there is nothing to toggle.

## The Agent's View of Its Subagents

Not our concern, and worth stating because the old spec had a whole section on it. The agent reads its
subagents' results through its own `Task` tool return value. There is no synthesis step for AIC⚡DC to
trigger, no "Synthesise now" affordance, no system-prompt convention about wrapping up, and no
in-limbo turn state for the user to resolve. The turn ends when the agent ends it.

## Empty States

- **No subagents in this turn** — Main tab only. The strip still exists, carrying one tab and one LED.
- **Transcript unreadable** — a tab whose `get_subagent_transcript` call returns an error shows the reason in place of the messages ("transcript not found", "session pruned"), as a system note rather than as something the subagent said. A transcript that comes back empty, and a read that gets no reply at all, are the same case with a different sentence. The tab is not removed: its row in Main is evidence the subagent ran, and removing the tab would contradict that. One unreadable transcript does not stop the rest of a turn's from being read.
- **Subagent with no transcript yet** — a tab created by a `started` event before any content has been written shows the description and a working indicator, not an empty message list.

## Disk Usage

Subagent transcripts live under the session directory and count toward the one-shot session-directory
size warning; they are not measured or warned about separately
(see [`../3-engine/history.md`](../3-engine/history.md#subagent-transcripts)). Deleting a session's
main transcript deletes its subagent directory with it — a subagent transcript whose parent is gone is
unreachable through every RPC we expose, so leaving it on disk would only waste space.

The old per-turn archive list with per-turn delete buttons in Settings is not carried over. Deletion is
per session, in the history browser, where the user is already looking at what they are deleting.

## Deep Linking

`?turn=<request_id>` scrolls Main to that turn and triggers the historical-view tab population for it.
An unknown or pruned request ID scrolls to the most recent turn and shows a transient toast. The
parameter is a request ID rather than the old `turn_id` because request IDs are what the mirrored
records carry.

## Invariants

- The chat panel is one component. Subagent tabs are additional per-tab state slots, not duplicated components.
- Every subagent tab is read-only. There is no input surface, no file-selection binding, and no RPC that sends a subagent a message — such an RPC does not exist to be called by mistake.
- `stop_task` is the only write affordance reachable from a subagent tab.
- Tab identity is the SDK `agent_id`, verbatim. No positional index appears in a tab key, a transcript path, or a record.
- Tab creation is idempotent; a repeated `started` event for a known `agent_id` updates the tab rather than adding one.
- The panel never invents a tab. Live tabs come from `subagentEvent` or from the `get_current_state` snapshot; a transcript tab comes from an `agent_id` the panel was handed — a row on a turn from this run, or the history browser's listing — never from one it composed.
- A transcript tab is keyed on the `agent_id` under a prefix that marks it as read from disk, so it can never collide with the same subagent's live tab.
- The strip holds one click's worth of transcripts. A fresh click, and a session change, clear the ones before it.
- A tab settles on terminal status from either `updated` or `notification`, whichever arrives, and never waits for both.
- A live subagent at turn end is shown as status-unknown, never as completed.
- Tool cards with a non-null `agent_id` render in both the subagent's tab and its row in Main, from one card object.
- A live tab's content is the parent turn's blocks filtered to that `agent_id`, mirrored by reference — not a transcript read and not a copy. Text and thinking are attributed the same way tool cards are, so a subagent's narration lands in its tab and not in Main's.
- A subagent tab never claims a request ID. Routing selects the turn by request ID first; a second tab answering to the parent's ID would take the main conversation's chunks.
- A tab rebuilt on reconnect never takes focus. Focus is a choice, and nobody made it; rehydration restores the tab that was active, or Main if it is gone.
- The LED row always carries one LED for Main plus one per subagent tab currently in the strip; the Main LED is permanent for the panel's lifetime.
- LED state is a pure function of the latest `subagentEvent` for that subagent plus the parent turn's completion. No separate state machine, no acknowledgement gesture.
- Clicking a LED activates its tab and scrolls the strip to reveal it.
- Turns without subagents render exactly as a plain chat. The strip exists but contains only Main.
