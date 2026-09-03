# Chat

The chat panel renders conversation messages, handles streaming display, manages auto-scrolling, and owns the user input area. It is the primary interaction surface within the Files tab. It also hosts the history browser and session-management controls.

What it renders changed more than how it renders. A turn is no longer "a user message, then one
assistant message with edit blocks in it". A turn is a **sequence of blocks** — text, thinking, tool
calls with their results — produced by an agent that reads files, runs commands, and spawns subagents
on its own initiative. The panel's job is to make that legible without turning into a log viewer, and
to be honest about the one thing that is genuinely new: the agent acts on the repository while the user
watches.

The permission dialog is not part of this spec even though it interrupts this panel; see
[permission-dialog.md](permission-dialog.md).

## Message Display
- Scrollable list of message cards — user, assistant, system event
- Keyed rendering for DOM reuse across updates
- User cards may include image thumbnails
- Assistant cards render markdown with syntax highlighting, math, and file mentions
- Within an assistant turn, tool cards and thinking regions are rendered inline in arrival order (see [§ Tool Cards](#tool-cards) and [§ Thinking Regions](#thinking-regions))
- System event cards (commit, reset, session switch, permission-mode change, compaction boundary, a background agent's task notification on a replayed session — see [`../3-engine/history.md`](../3-engine/history.md#a-background-agents-wake-up-is-a-system-note-not-a-prompt)) use distinct styling — dashed border, muted color, "System" role label
### Terminal-Reason Badge Placement
The badge reports `terminal_reason` from `streamComplete` rather than a provider finish reason. Same
severity split, different vocabulary:
- Natural completions (`completed`) — muted green ✓ badge in the bottom-left of the card, paired with the hover toolbar; fades in on mouse-enter alongside the copy/paste icons. Positive confirmation that the turn ended cleanly without competing with role label or body content for attention
- Interruptions (`aborted_streaming`, `aborted_tools`) — neutral grey badge at the top, always visible. The user caused these, so they are information rather than warning, but they must be visible: an interrupted turn may have left a half-finished edit on disk
- Error and limit reasons (`max_turns`, refusals, API error statuses, unknown values) — badge inline with the role label at the top of the card, always visible. Users notice these before reading the body
- `terminal_reason` is null on older CLIs and on local slash-command results; no badge is drawn rather than a badge claiming success
- Card reserves extra bottom padding when a natural badge is present so the absolute-positioned badge doesn't overlap the last line of content
## Streaming Display
### Block-Keyed Rendering
- Every streaming event carries a **block identity**, a sequence number, and content that is cumulative *within that block* (see [`../3-engine/session.md` § Chunk semantics change](../3-engine/session.md#chunk-semantics-change))
- The panel keys rendered elements by block identity. A chunk updates its block in place; blocks appear in arrival order and never reorder
- A chunk whose `seq` is not greater than the highest seen for its block is discarded. Within a block, drops and reorderings are harmless; across blocks, arrival order is the contract
- Chunks coalesced per animation frame — a pending map of block id → latest content is drained by the frame callback, which then updates the rendered blocks
- The first chunk of a turn begins rendering the streaming card; subsequent chunks update blocks in place
- Streaming card uses a force-visible class so content-visibility optimizations don't hide it

The old contract — every chunk carries the entire accumulated turn — is gone, and with it the property
that any single chunk could rebuild the whole view. The replacement is weaker but bounded: any single
chunk can rebuild *its block*. This is why block identity has to be threaded through every rendering
path rather than treated as an optimisation.
### Code Block Scroll Preservation
- Markdown HTML replacement on each chunk loses horizontal scroll positions on code blocks
- Before updating, snapshot scrollLeft of every code block inside the streaming card (by index)
- After DOM rebuild completes, restore the saved scroll positions
- Skipped when no code block was scrolled (common case)
- Block-keyed rendering shrinks the blast radius: only the block being updated is rebuilt, so scroll positions in earlier blocks survive without help
### Passive Stream Adoption
- When an event arrives with a request ID the client did not initiate, adopt the stream as passive
- Sets current request ID and a passive-stream flag
- On completion of a passive stream, prepend the user message from the result (since the passive client didn't add it optimistically)
### Reconnect Replay
- On reconnect, `get_current_state` carries `active_streams`, each with the turn's blocks in order and each block's current content, and its `usage` — the turn's token counters so far, in the same shape `turnUsage` pushes
- The panel renders the block list, then resumes applying live chunks. A user who refreshed mid-turn sees the turn as it stands, not an empty card that fills in from the next token
- Replay is block state, not a chunk log: superseded thinking content and intermediate renderings are not recoverable, and the panel must not imply otherwise
- Any `pending_permissions` in the same snapshot are handed to the permission dialog immediately

### Streaming State Keyed by Request ID

Streaming state (per-block content, passive flag, streaming card DOM node) is keyed by request ID, not held as a singleton. Only one user-initiated turn is active at a time, so there is at most one active key; the singleton-like behavior is an emergent property, not a structural assumption.

Subagents produce concurrent activity *inside* one turn. Their events carry the parent turn's request ID plus an `agent_id`, so routing is two-level: request ID selects the turn, `agent_id` selects where inside it the event belongs. Main-scope events have a null `agent_id`.

The transport never assumes a singleton stream — every event carries the exact ID of the stream it belongs to (see [`../1-foundation/rpc-transport.md` § Concurrency](../1-foundation/rpc-transport.md#concurrency)). The chat panel's routing layer is the frontend counterpart to that contract.

### A Turn Can End More Than Once

`streamComplete` is where the panel *settles* a turn: it freezes the turn's blocks onto a new assistant
message, attaches the footer, and tears the streaming card down. A turn that left a background subagent
running goes on to end again — main's reply to the task notification is a further turn on the same
request, and it arrives as a second `streamComplete` flagged `continuation`
([`../3-engine/session.md` § Every result the drain reads is emitted](../3-engine/session.md#every-result-the-drain-reads-is-emitted-flagged-continuation)).

A continuation **revises the message already settled for that request** rather than appending a second
one. Appending is the wrong reading because every field on the payload is cumulative over the request,
not incremental: a second message would repeat the whole turn — its text, its tool cards, its subagent
rows — under a second footer. So the settled message carries the `requestId` it answered, which is the
only thing that reads it, and the handler replaces that message in place.

- The revised message keeps its original `durationMs`. The panel's run timer stopped when the turn's presentation finished; that reading is what the user waited for and is what the ⏱ badge means. There is no second wall-clock measurement to take. The footer's duration is a different figure and comes from the payload — the engine's working time, summed across the turn's results by the engine rather than the browser
- Everything else comes from the continuation: it is the whole turn's state, including main's closing answer, which is the content the revision exists to deliver
- The turn is torn down only when the continuation reports nothing left running. One that still has work in flight leaves the turn addressable, because more events are coming for it
- A continuation for a request with no settled message appends one. That is a turn whose first result carried nothing to render — not a case to lose the answer over

Without this the closing answer was consumed and rendered nowhere: it reached the turn's blocks, but the
freeze had already happened and nothing re-froze it. It appeared only if the user reloaded, from the
mirror.

The mirror is also where the two views diverge, permanently and correctly. Live, the same work is one
revised message; replayed, it is two turns with the task notification between them, rendered as a system
note rather than a prompt
([`../3-engine/history.md` § A background agent's wake-up](../3-engine/history.md#a-background-agents-wake-up-is-a-system-note-not-a-prompt)).
The transcript records that seam because that is where the turn on disk actually breaks, and the browser
does not edit the transcript's turn boundaries to match a live rendering decision.

## Thinking Regions

- `thinkingChunk` events render into a collapsed region above the text they precede, labelled `Thinking…` while chunks are arriving and `Thinking` once the block reports `done`
- **No token count.** An earlier draft asked for one. No payload carries per-block thinking tokens: `thinkingChunk` is `{block_id, seq, content, done, agent_id}` and the turn's `usage` is a single total covering text, tools and thinking together. The number would have to be invented — a character count dressed up as tokens is worse than no number, so the label carries none
- Expanded state is per-block and remembered for the session; a user who expands one does not get every subsequent one expanded
- The configured thinking display setting (`summarized`, `omitted`) governs what arrives at all. When it is `omitted`, no region is drawn — not an empty one
- Thinking content is never included in copy-message or read-aloud output; it is the agent's scratch space, not its answer

## Tool Cards

A tool card is the unit that carries what the agent *did*, as opposed to what it said.

### Card Anatomy
- Header — **two columns: a metadata rail, then the summary.** The rail holds everything that says what the call *is* — caret, status dot, server chip for an MCP tool (so a user can tell an AIC⚡DC tool from a built-in one), tool name, invocation time, gated marker — and the right column holds nothing but the input summary (≤ 200 chars, joined in the browser — see below), so a turn reads as one content column with its cards' chrome stacked down the left
- **Every summary wraps, and nothing on the header is elided** (2026-08-28; `Bash` was the only tool that wrapped before that date). A uniform row height reads well down a page of thirty calls, and it was the wrong thing to buy with the header's content: the header is the *only* place a collapsed card says what the call was about, it carries no tooltip, and an ellipsis eats exactly the tail that identifies the thing — which file, which flags, which test. That argument was written about `Bash` and was never about `Bash`; a `Read` whose path stops at `…/repos/softwaredesignlifec…` names no file. The break is `overflow-wrap: anywhere` rather than `break-word`, because the strings that overflow are paths and flag runs with no spaces in them, and a summary that may only break at a space cannot break
- **The 200-character cap is the only bound; there is no line clamp on top of it.** A clamp is the same ellipsis three rows lower, and the cap already bounds how tall a card can get. The cap was the engine's until 2026-08-28 and moved into the browser with the join itself (`TOOL_SUMMARY_CHARS`, `block-render.js`), which is why the stylesheet's comment about it now points one file sideways rather than at a Python constant
- **Nothing is pinned to the header's right edge** (2026-08-28, replacing the right-hand group of the same day's first attempt). Chrome on the right is subtracted from *every* line of the summary rather than only from the line it sits on, because the summary is a single box: a box that ends before the time chip ends before it all the way down. In one pane of eight cards the summaries measured 180–402px wide, each card's left edge somewhere else again; folded left they are 384px on all eight, from one x. The cost is paid in rail lines, which are free — the summary wraps to two or three lines on most cards, so the rail has lines to spend before it is the tallest thing in the row
- **One item to a rail line: name, then time, then the gated marker.** Sharing a line, the marker sat beside the clock on a card timed today and wrapped under it on one restored from another day, so where a reader looked for it depended on how old the card was — the same fault the rail removed one column over. A line is drawn only when it has something on it: a card with no `invoked_at` — every card written before the field existed — draws no line for a time, and an ungated one of those draws only its name
- **The rail is a fixed 7rem, and it lies down under 360px.** A column that only lines up when neighbouring cards happen to agree is not a column, so anything wider wraps *inside* the rail — a long MCP tool name, or a card restored from another day whose chip carries a date as well as a clock. The width is set by the tool name rather than the clock: the first attempt was sized to the clock, and the names too long for it dropped to a line of their own leaving a caret and a dot stranded above them, which is the one thing here that must not wrap, since the name is what a reader scans the rail for. Below a **card width** of 360px — a container query, because the pane is a dialog the user drags and the viewport's width is not the question — the header becomes one column with the metadata as a row above the summary. 7rem out of a 300px pane is over a third of it, and there the summary measured *worse* than before the rail existed (164px against 231px), because the layout the rail replaced could drop a squeezed summary onto a line of its own and a fixed column cannot. Where the trade turns, the rail is what goes
- These are invisible to the suite, because jsdom does no layout: the widths were read in a browser at 520, 400 and 300px (`DIALOG_MIN_WIDTH`), against a page mounting the real renderer with the real stylesheet
- Body — collapsed by default, with two exceptions. An **edit-shaped call opens itself**, because the diff *is* what the card is about: the header names the file and nothing else, so a collapsed `Edit` row hides the only part a reader is scanning for. An earlier draft kept these shut so a nine-edit turn would not open nine diffs; in practice the hunks are small, the card scrolls, and clicking nine carets to read a turn was the worse trade. A card **waiting on permission stays shut whatever its shape** — the dialog is open over the top with the same diff in it, and the card would be a second copy of a decision being made. An explicit click always wins over both
- Result — attached to its card by `tool_use_id`. A truncated result is marked as truncated and names the full byte count; there is **no "show all"**, because the engine sends only the preview and the untruncated text never leaves the server, so the button would expand to the content it already showed. Saying how much was withheld is the honest version of that affordance. Error results are **expanded by default**; the status flag is what drives that, not string sniffing
- Footer — duration, and the files the call modified (each clickable, navigating to the diff viewer)
- **A file chip is labelled with the repo-relative path and carries the engine's absolute one on its tooltip**, per [shell.md § The Same Rule Names Files On Screen](shell.md#the-same-rule-names-files-on-screen). Every path on a card is absolute — the CLI's file tools require it — and an absolute label spends the chip's width on a prefix identical for every file in the repo, putting the part that identifies the file at the end that falls off. The chip is also width-bounded and elided, because the relative name fits the common case and not a deeply nested one
- **The header's input summary names files by the same rule as everything else** (2026-08-28), so a `Read` reads `file_path=src/a.js`. It was the one place a raw absolute path still showed, and this entry used to say so and call it deliberate: the summary was a `key=value` join built server-side, it did not know which of its keys were paths, and teaching it looked like it needed a per-tool table of path keys — a *third* mechanism answering "absolute → relative" when there were already two. **There was no table.** A value that begins with the repo root is a path *by its shape*, which is the same discriminator the browser's rule already mirrors off the backend's own containment check, so every string value can be offered to `toRepoPath` and the rule declines the ones that are not paths. The join moved to the browser to be where that rule lives (`toolInputSummary`, `block-render.js`); it costs nothing on the wire, because both card builders already shipped the whole `input` dict beside the summary, and it gives live cards and cards read back off a transcript one builder where there were two. Two limits are worth naming: a path *inside* a value is left alone, because the rule renames a path and does not rewrite prose, so an `old_string` quoting an absolute path keeps it; and the cap still truncates from the front, so it falls on the tail
- The engine kept its own one-liner over a tool input, and that is not a second copy of this: `summarise_tool_input` is the *permission dialog's* headline fallback and the exec payload's fallback when a call carries no `command` key, both of which must exist before a browser is in the picture. Nothing renders both strings, so they are free to differ, and re-pointing the card at that function to save a copy would undo this entry
- A gated marker on cards whose call went through a permission prompt, so the transcript records that the user authorised it
- Subagent attribution — a card with a non-null `agent_id` renders indented under its subagent's row rather than at turn level

### Status
| Status | Rendering |
|---|---|
| Pending | Grey dot, animated. The call is in flight |
| Awaiting permission | Amber dot with a lock glyph. The dialog is open or queued |
| Ok | Green dot |
| Error | Red dot, body expanded |
| Denied | Amber dot, with the denial reason as the body. The agent saw this reason too |

### Diff Rendering for Edit and Write
`Edit` and `Write` inputs are diffs in disguise, and rendering them as raw JSON wastes the panel's most
useful affordance. An `Edit` card renders `old_string` → `new_string` as a two-level diff. A `MultiEdit`
renders one hunk per entry in `edits`, so a call that rewrites four places in a file reads as four hunks
rather than one incoherent diff. A `NotebookEdit` renders `new_source`.

A `Write` card renders its new content against an empty old side, which is an all-add diff. **Not a diff
against the file on disk.** An earlier draft asked for that; the `toolUse` payload carries only the new
content, and the panel has no read path that could fetch the old content without racing the write it is
describing — the permission dialog is open at that moment and the file may be gone by the time a reply
lands. Rendering what the payload contains is the honest rendering. A real before/after for a `Write`
belongs to the diff viewer, which reads the working tree after the call lands; the footer's file chips
are the link to it.

#### Two-Level Diff Highlighting
- Line-level diff — Myers algorithm via diff library, produces context/remove/add typed lines
- Pairing — adjacent runs of remove followed by add are paired 1:1 for character-level diffing
- Character-level diff — word-level diff on each pair, producing segment arrays with equal/delete/insert types
- Rendering — paired lines show the word-level changes highlighted within the line-level background color
- Unpaired lines show only the line-level highlight

This is the same algorithm the old edit-block renderer used, and the permission dialog uses it too (see
[permission-dialog.md](permission-dialog.md)). It is the one piece of the edit-protocol UI worth
keeping: the protocol is gone, but "show me precisely what changed on this line" is not a
protocol-specific need.

### Todo Lists
- `TodoWrite` calls render as a checklist that **replaces** the previous list rather than appending another card, so a long turn shows one live plan instead of fifteen snapshots
- The list stays visible while the turn runs and collapses to its final state afterwards

### What Tool Cards Deliberately Do Not Do
- No re-running a tool call from the transcript. The agent owns its tool loop; a UI that lets the user replay a call outside that loop produces state the agent does not know about
- No editing a tool input before it runs — that is the permission dialog's job, and only to the extent of allow/deny
- No hiding tool calls behind a global "show tools" switch. A turn where the agent silently modified nine files is exactly the turn a user needs to see

## Markdown Rendering
- Dedicated Marked instance for chat, separate from the diff-viewer preview instance
- Code renderer override — language label, copy button, syntax highlighting
- All other block elements use marked defaults (no preview-specific logic)
- Math extension — display and inline expressions rendered via KaTeX with parse-failure fallback
- Applies to user and assistant messages equally — users type markdown-literate text (matching what the agent receives), so the UI renders it the same way
- **Raw HTML renders as the text it was written as, never as markup.** `marked` does not sanitize, so anything tag-shaped reached the DOM as a tag: a prompt about `<your topic>` drew an unknown element, which is zero pixels, and the sentence lost its subject with nothing to show that it had. Angle brackets in a conversation about code are far more often literal than markup — placeholders, generics, comparisons, XML the conversation is *about* — and nothing writes HTML into message content, so escaping loses no rendering. It also closes the gap trust-the-source left open: message content is not all the user's, and a file the agent reads can ask it to echo `<img onerror=…>`, which would run with the app's own RPC access. Code fences and `<https://…>` autolinks are separate token types and are unaffected
- **Who wrote a string does not change how it renders.** One instance serves prompts, replies, system-event cards and replayed transcripts, and it trusts none of them more than another — a per-role escaping rule would be a second place for the question "is this content ours?" to be answered differently from the first
### Syntax Highlighting
- Explicit language registration for common languages (JavaScript, TypeScript, Python, JSON, Bash, CSS, HTML, YAML, C, C++, diff, markdown)
- Fenced blocks with recognized language are highlighted directly
- Fenced blocks without a language use auto-detection
- Unrecognized languages fall back to escaped plain text
## Code Block Copy Button
- Injected into every fenced code block unconditionally, including during streaming
- Default opacity zero; fades in on hover via CSS — no visual flicker during streaming
- Click handling delegated through the markdown content click handler
- Shows brief confirmation after click
## File Mentions
### Detection (Final Render Only)
- Scan assistant message HTML for known repo file paths
- Pre-filter by substring match, sort candidates by path length descending (longer paths match first)
- Build combined regex, replace matches with clickable spans
- Also collect file paths from tool cards — the tool's input paths and its `files_modified` list
- Replacement operates only on text segments between HTML tags — tag attributes never touched
- Matches inside code blocks skipped; matches inside inline code replaced normally
### Click Handling
- Inline text mentions in the message body navigate to the diff viewer
- File summary chips in the "Files Referenced" section do the same: one click, file open. They dispatch `file-chip-click`, and nothing else happens
### File Summary Section
- Below each assistant message with file references, shown for final rendered messages only, never during streaming
- One chip per referenced path, marked `↗`, every chip alike — the list is "what this message named", collected in one place
- Both sources contribute: edit-block headers (always, since the model named the file as an edit target whether or not it exists yet) and prose mentions matched against the repo file list
- The wording is never "in context". A chip says the message mentioned the file; nothing about it puts the file in the model's context
- Labelled by the same rule as the tool cards' chips (§ *Card Anatomy*), which is what makes **one file one chip** here: the two sources need not agree on spelling — a prose mention is matched against the repo file list and so is relative, while an edit-block header carries whatever the model wrote — so the deduplication is on the repo-relative name, not on the string as found. On raw strings the same file arrives twice and, now that both would be labelled identically, would read as two chips for one file

Three things stood here and went with the selection ([CC-21](../plan/decisions.md#cc-21)):

- **The ✓/+ mark on each chip**, which said whether the path was in the picker's selection. With one
  state per chip there is nothing to distinguish, and `↗` says what the click does instead
- **The "+ Add All (N)" header button**, which added every referenced file to the selection at once
- **Input accumulation on add** — clicking a chip used to append a sentence to the composer ("The file X
  added. Do you want to see more files before you continue?"), joining multiple files naturally across
  clicks. This was the closest the old design came to the current answer, and it is worth being clear
  about why it is not simply kept: it wrote prose *about* files it had also selected, so the model got
  a sentence and a hint and no path it could act on. The picker's middle-click writes the path itself,
  and ctrl+middle-click writes `@path`, which the CLI expands into a real read

## Turn Footer

Replaces the edit summary banner. Rendered at the end of the assistant turn, not the top:

- Files modified — the union of every tool result's `files_modified`, deduplicated, each clickable through to the diff viewer. This is the answer to "what did it just do to my repo?" and it is the footer's most important line. Those paths are absolute — the CLI's file tools require that, and the field reports what the tool was given — so the click relies on the shell relativising them before the viewer asks for one (see [shell.md § Viewer Background](shell.md#viewer-background)), and the chips are labelled by the same rule they are navigated by (§ *Card Anatomy*)
- Tool calls, and how many needed a permission prompt
- Duration, engine-internal turn count. This duration is the engine's working time summed over the turn, which is a different figure from the ⏱ badge beside the role label — that one is the panel's own wall clock, and the two part company for a turn that spent time waiting on a background subagent (see § A Turn Can End More Than Once)
- Usage — one chip per model that answered: the turn's total, then how it split, `N tok · N in · N cache · N out`. Cost when the engine reports it; **nothing** when it doesn't. Subscription billing reports null and the footer must not render that as `$0.00` (see [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap))
- A mirror-gap marker when the turn failed to append to the repo-local transcript, linking to the health banner. The link *forces* the banner open rather than merely un-dismissing it, because the engine may have recovered or restarted since that turn — a readout with nothing wrong in it is still an answer, and better than a link that lands on nothing

### The Token Split

`turn_model_usage` carries four counters per model, and the chip shows them as three figures — the two
cache counters share one. Reading a cache write apart from a cache read is a pricing question, and the
chip answers a coarser one: how much of this turn was prompt the model had to be sent, how much was
prompt it already had, and how much did it write back.

| Counter | Chip | Priced at |
|---|---|---|
| `inputTokens` | `in` | full rate |
| `cacheReadInputTokens` | `cache` | a fraction of it |
| `cacheCreationInputTokens` | `cache` | a premium over it |
| `outputTokens` | `out` | several times the input rate |

Two rules follow, and both have been got wrong before:

- **`in` is not the prompt.** It is the part of the prompt charged at full price; the cached part is
  prompt too. An agentic turn resends its whole context at every step, so nearly all of a long turn's
  prompt is a cache read, and a chip that mapped "input" to `inputTokens` alone would report a
  fraction of what was sent. The prompt's real size is the three input-side counters added up, which
  is what the chip's tooltip says and what the HUD's `↑` shows
- **A zero part is not printed.** A replayed transcript that recorded no cache counters would
  otherwise read `0 cache`, which claims a measurement that was never made — the same rule the cost
  chip follows for a turn with no basis

The total stays in the chip alongside the split. It is the figure that answers "how big was this
turn", and the split is what says whether that size was expensive.

### Live Token Counter

The footer's chips have a running twin: while a turn is streaming, the same chips render under the
streaming card, prefixed `SO FAR` and dimmed.

- The source is `turnUsage`, pushed by the engine as each assistant message reports what it used
  ([`../3-engine/session.md`](../3-engine/session.md#message-taxonomy--ui)). Same `turn_model_usage`
  shape as the footer's, so one renderer draws both
- **It steps, it does not tick.** The CLI reports usage per finished assistant message and nowhere
  else, so the number moves a few times on an agentic turn and once on a one-shot answer. This is the
  honest limit of the data, not a throttle of ours
- **No cost.** Pricing a turn means differencing the session's running total, which only the result
  message carries. A live dollar figure would be a guess, and this panel does not print guesses
- Subagent messages count toward it, under their own model. They are what makes a delegating turn
  expensive; a counter that skipped them would sit still through the costly part of the turn
- The counter is the turn's, so it renders on the card that owns the turn. A subagent tab shows no
  counter of its own
- A browser that reconnects mid-turn gets the counter as it stands, from the `active_streams` entry's
  `usage` — the next push is a whole assistant message away, and a reconnect during a long tool call
  would otherwise read as a turn that had spent nothing
- Still no estimate. Every number here is one the engine measured; AIC⚡DC does not count tokens, and
  a live counter is the most tempting place to start

### No Retry Prompts

The old panel auto-composed three kinds of retry prompt into the textarea — ambiguous anchor,
not-in-context, old-text mismatch — because a failed edit was a dead end that only the user could
break. None of them survive, and nothing replaces them.

The reason is structural rather than cosmetic. Those failures were failures of *our* apply pipeline
against a model that had no way to look at the file. The agent's `Edit` tool fails back to the agent,
which can read the file, look again, and retry inside the same turn. A panel that composed a retry
prompt for the user to send would be racing the agent's own recovery, and would usually lose.

## Message Action Buttons
- Hoverable toolbars at top-right and bottom-right of each message card (both ends for long messages)
- Copy raw text to clipboard
- Insert raw text into chat input
- Read aloud — speaks the message via text-to-speech; toggles to a stop control while this message is the one playing. Shown only when the browser supports speech synthesis. See [speech.md § Read Aloud](speech.md#read-aloud-text-to-speech)
- **Undo file changes** — **not built, and not buildable today.** It would call `rewind_files(user_message_id)` to restore the files as they were before that turn, but the engine keeps no checkpoints in a session that mirrors its transcript, and every session with a repo mirrors ([decisions § CC-20](../plan/decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)). The RPC refuses the call and names git. If the SDK ever allows both: user message cards only, confirmation dialog first, and the panel must say it restores *files* and not the conversation — the transcript is unchanged either way
- Not shown on streaming messages
## Scrolling
### Auto-Scroll
- During streaming — scroll-to-bottom on each update, unless user has scrolled up
- IntersectionObserver on a sentinel element at the bottom of the message container
- Scroll-up detection during streaming — a passive scroll listener tracks position and only disengages auto-scroll when the user scrolls upward by more than a threshold; pure observer-based detection would false-trigger during content reflows
- Observer only re-engages auto-scroll; never disengages during active streaming
- Double animation-frame wait pattern for scroll-to-bottom — ensures DOM has fully reflowed before setting scroll position
- Tool cards expanding (an error result, a user click) change height mid-turn. Height changes from expansion never disengage auto-scroll — only a deliberate upward scroll does
### Scroll-to-Bottom Button
- Appears when user has scrolled up
- Click scrolls to bottom
### Content-Visibility
- Off-screen messages use CSS content-visibility for performance
- Last N messages (around 15) forced to render fully — ensures accurate scroll heights near the bottom
### Scroll Preservation
- Tab switching — container stays in DOM (hidden), scroll position passively preserved
- Minimize/maximize — same, no explicit save/restore needed
- Session load — reset scroll state, scroll to bottom
### Auto-Scroll for Non-Streaming Messages
- Messages added outside streaming (commit, compaction boundary, permission-mode change) follow the same scroll-respect rule — if at bottom, scroll down; if scrolled up, leave unchanged
## Input Area
### Text Input
- Auto-resizing textarea
- Enter to send, Shift+Enter for newline
- Image paste — base64 encoded, size and count limits enforced
- Undo/redo workaround — native undo is broken in shadow DOM textareas when the framework re-renders set value programmatically; intercept Ctrl+Z and delegate via deprecated exec-command fallback
- Draft persistence — the in-progress draft is written to `localStorage` on every input event and restored on reconnect / refresh. Cleared on send. Pending images are not persisted. See `specs-reference/5-webapp/chat.md` for the storage key
### Slash Commands
- A leading `/` is not intercepted by the panel. Commands from `.claude/commands/`, skills, plugin commands and the CLI's own built-ins are the engine's and pass through untouched — including a mistyped one, which the CLI names better than the panel could
- A routed command returns `{status: "routed", target}` synchronously; the panel opens the surface `target` names and renders the reply as a system note saying where it went. A denied one returns `{status: "unsupported"}` with the reason, rendered the same way. Neither reaches the model
- A `target` may name a section within the surface after a `#`, and a tab with a segmented control must be targeted that way — the section it opens on is otherwise the reader's remembered choice, which is how `/mcp` came to open a tab with no MCP status on it. The panel splits the fragment off before it resolves the surface, so a section it does not recognize costs nothing: the surface still opens. Forwarding it is the shell's, over a hook the surface may simply not have — see [`shell.md`](shell.md)
- See [`../3-engine/session.md` § Slash Commands](../3-engine/session.md#slash-commands) for the target grammar and the routed table
### Slash Palette
- A separate component hosted inside the chat input area, on the same host/guest contract as [§ Input History](#input-history) — `show`/`hide`/`handleKey`, and the chat panel owns the lifecycle
- Opens when `/` is the first non-whitespace character in the composer and the cursor is still inside that token; closes once whitespace settles the command or the token is gone. The same rule the engine applies to decide a message is a command, so the palette and the engine never disagree about what will be sent
- **Also opens from a button** in the composer's send column, in the slot the snippet drawer's toggle used to hold. The button writes `/` into an empty composer and puts the cursor after it, which satisfies the rule above rather than bypassing it — there is one open condition, not two, and no state saying "opened by button" that the next keystroke could contradict. On a composer that already has text the button is disabled, because the engine only treats a leading `/` as a command and a button that silently did nothing would be worse than one visibly unavailable
- **A lone `/` is the one exemption to that**, and it exists because dismissing is specified not to retract it (see the last bullet in this section): with any non-empty composer counting as a draft, the button disabled itself with the very character it had just typed, so one press followed by Escape left it dead and the only way back was clearing the composer by hand. Re-pressing it types the same `/` over itself, which is a no-op, so the effect is that the overlay re-opens. `/comp` is a draft again — the exemption is the bare slash and nothing past it, and the overlay is already up for anything longer. The disabled attribute and the handler's own guard read the same predicate, since a render lags a fast keystroke and the two disagreeing would either lose a draft or dead-end the button
- **The button stays live during a turn**, and so does the palette. Some of the list is reachable mid-turn and some is not, which is a per-row fact the rows themselves carry — disabling the entry point would withhold the reachable rows to avoid explaining the unreachable ones, at the moment the reachable ones (context, cost, MCP, permissions) are most wanted. See [`../3-engine/session.md` § Mid-turn availability](../3-engine/session.md#mid-turn-availability)
- The button is what makes the list **browsable**. Typing `/` is a discovery problem: it works only for a user who already suspects the commands exist. A visible control with the full list behind it is how they find out — which is the job the snippet drawer used to do, done against the CLI's real inventory instead of a hand-maintained file (see [decisions § CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user))
- Entries come from `list_commands()`, fetched on first `/` and cached for the session. A failed fetch is not cached as an empty list, but is held off for a few seconds before re-asking, so a broken engine costs one RPC rather than one per keystroke
- A reply marked `partial` is the routed commands only, because the engine had not connected yet — the state every fresh start is in, since the engine connects on the first turn. It is cached like any other list and re-asked exactly once, when the engine health broadcast reports itself connected. Checked on the next open rather than invalidated from the broadcast, so a dropped broadcast cannot leave the short list up forever. While the refresh is in flight the stale list stays on show: it is still correct, and an overlay that grows beats one that appears late
- **A partial list says so on screen**, in its own line above the counts: only the commands AIC⚡DC routes itself are listed, and the CLI's own arrive once the engine connects on the first turn. The counts cannot carry this — "4 of 4" is also what a complete list of four commands looks like, and this overlay is the only place the user can size the inventory at all. It stays up while the query narrows, because a miss is reached by typing and the explanation is worth most on a miss. `partial` travels with the list through `show()` rather than being bound like `streaming`: the list only ever changes by a `show()` call, where a turn can start or end with the overlay already up. It defaults to false on every call, so the full list replacing the short one clears the notice without the host having to unset anything
- An empty list is not the same as a failure. When the engine genuinely advertises nothing, no overlay opens — one saying "0 of 0" tells the user nothing they cannot see
- Filter ranks exact names, then name prefixes, then alias prefixes, then substrings. Descriptions are not searched: a row whose reason for being there is invisible in the row is worse than a missing row
- Up/Down navigate with wrapping; Enter and Tab select; Escape dismisses. Wrapping where history clamps, because this is a short relevance-ordered list being scanned for one known item, not a chronology being read through
- **While a turn is streaming, a row whose `during_turn` is false is rendered disabled with the reason on it**, not filtered out. Arrow keys skip it, Enter and Tab refuse it, and a click does nothing — an overlay that focuses a row it will not act on is worse than one that never offers it. Focus lands on the first row that *is* actionable; when none is, nothing is highlighted at all — a highlight promises Enter does something — and the overlay still opens and still says what the filter matched, because "everything here is waiting on the turn" is the answer the user came for. The row carries **when it comes back** as visible text, not a tooltip: this list is navigated by keyboard, and a tooltip is unreachable from there. *Why* it is held differs between a session swap and a passthrough command, and that part is the tooltip's, being the secondary fact. A blocked routed row drops its "opens UI" badge, which warns that the row does something other than send text — a row that does nothing right now has no such warning to give. The footer counts what is waiting, so the shape of the list is legible without reading every row
- Selecting a `send` entry completes the command in the composer, replacing the whole token so mid-token completion cannot leave `/contexttext` behind. Selecting a `route` entry clears the token and opens the surface — the row carries an "opens UI" badge, because a command that quietly does something other than its description promises is worse than no palette
- With nothing matching, the overlay stays up to say so but consumes neither Enter nor the arrows: a stray `/typo` must still be sendable
- Dismissing never touches the composer, so there is nothing to restore and no cancel event. This holds for a button-opened palette too: Escape closes the overlay and leaves the `/` the button typed, and a second Escape clears the composer by the default rung of the [§ Escape Priority Chain](#escape-priority-chain). Retracting the `/` on dismiss would make the button the one path that edits the composer on the way out, and a lone `/` is both visible and sendable — the CLI names it better than a silent revert would
### Paste Suppression
- When middle-click inserts a path into the textarea, a flag on the chat panel tells the paste handler to suppress the browser's selection-buffer paste
- Flag is a one-shot — set on insert, consumed by the next paste event
### @-Filter
- Typing `@text` activates the file picker filter
- Escape removes the filter query from the textarea and clears the filter
### Escape Priority Chain
1. An open overlay in the input area — the history recall list, or the slash palette — takes it first and closes itself. Whoever is open wins; neither can be open at once, since recall needs cursor 0 and the palette needs the cursor inside a leading `/token`
2. @-filter active — remove query, clear filter
3. Default — clear textarea

The permission dialog is modal and takes Escape before any of this; Escape there is an explicit deny,
never a dismiss (see [permission-dialog.md](permission-dialog.md)).
### Stop Button
- During a turn, Send transforms into Stop
- Click calls `cancel_streaming`, which interrupts the engine. The button then shows a brief draining state until `streamComplete` arrives — the turn is not over until the engine says so, and pretending otherwise loses the tail
### Input History
- A separate component hosted inside the chat input area — the chat panel owns the interaction lifecycle
- Records every sent message
- Up-arrow at cursor position 0 opens an overlay showing recent history
- Keyboard priority — when the overlay is open, the chat panel delegates key events to it
- Substring filter, capped at a size limit, duplicates moved to the end rather than creating a second entry
- Items displayed oldest-first (top) to newest (bottom)
- Up/Down navigate; Enter selects; Escape restores original input
- Session seeding — when a session is resumed, all user messages from that session are added to the input history for up-arrow recall
- Long entries (including multi-line messages) collapse to a single ellipsis-clipped line; the full text is disclosed via native `title` tooltip on hover. No inline preview pane
- Overflow — when entries exceed the initial visible window, the list scrolls within a bounded height. Filter input is the primary discovery mechanism for older entries
### Speech
- **Dictation** — toggle button for continuous voice dictation; transcribed text inserted at cursor position (not appended) with automatic space separators
- **Read aloud** — the per-message speaker button (see [§ Message Action Buttons](#message-action-buttons)) reads a message back via text-to-speech, surfacing a floating play/pause/speed/position transport
- See [speech.md](speech.md)
### Images
- Supported formats — PNG, JPEG, GIF, WebP
- Size limits enforced (reject with visible error before encoding)
- Base64 data URI encoding
- Thumbnail previews with remove button below textarea
- Lightbox overlay on click (full-size view, Escape to close)
- Re-attach button on thumbnails and in lightbox (see [images.md](../4-features/images.md))
- **A restored prompt carries pointers where a live one carries bytes.** A message the user pasted into this session holds its data URIs; the same message reached by resuming or by reconnecting holds `image_refs`, and each is resolved through `history_image` after the transcript is on screen — never before it, so the text is readable while the bytes are still arriving. A resolved pointer becomes the same tile a pasted image gets, lightbox and re-attach included, because re-attaching from a past session is one of the two documented paths into the composer. Unresolved tiles hold their final size, and a pointer that cannot be read keeps a marked tile with the reason rather than vanishing. The fetching is shared with the history browser's — same pointers, same sequential reads, same cache-the-failures rule, one implementation
- No token estimate is shown. AIC⚡DC does not count tokens; the numbers it shows are the engine's own, reported once the turn is under way ([§ Live Token Counter](#live-token-counter)) and finally in the footer ([§ The Token Split](#the-token-split)). Nothing is shown for a prompt not yet sent, which is the only figure that would have to be guessed
- Not automatically re-sent on subsequent messages — display-only after original send
### Links and URLs
- A URL in the input is just text. There are no URL chips, no fetch button, no per-turn include/exclude checkboxes, and no URL modal
- The agent fetches what it needs with its own web tools, gated by the permission dialog like any other tool call, and the fetch appears in the transcript as a tool card
- This is a real loss of control surface and worth naming: the user can no longer curate a URL set across turns. What they get instead is a fetch they can see, deny, and read the result of
## Action Bar

Two visual groups separated by a thin vertical divider:

- Search group — search-mode toggle (message/file), search input with inline toggles (ignore case, regex, whole word), result counter, arrow navigation, and the preset / permission controls at the right end
- Session group — new session, open history browser (hidden in file search mode, and on any tab but the live conversation: a subagent transcript and a historical archive have no session of their own to restart, so ✨ there would restart the one behind the tab being read)
  - ✨ calls `new_session` and clears nothing locally. The server broadcasts `sessionChanged` with an empty message list and the panel acts on that, so the client that started the session and the clients merely watching it take one path. It is disabled while a turn is streaming because the server refuses mid-turn, and a button whose only outcome is a refusal it could have predicted is noise. Two refusals still reach the user as toasts: `turn_in_progress` (the turn started underneath the click) and `restricted` (the call is the host's — it discards the context every client is looking at)
  - 📜 stays live while a turn streams. Browsing is a pure read of the mirrored transcript; it is *resuming* from inside the browser that the engine refuses mid-turn
  - The engine chip — which engine is answering. Present only when more than one engine is mountable, and it opens the engine notice rather than acting directly. See § Engine Indicator and Notice

Git action buttons (copy diff, commit, reset) and the review toggle live in the file picker's top toolbar, alongside the sort glyphs and Settings button. They are not in the chat action bar. See [file-picker.md](file-picker.md).

### Dual-Mode Search

- 💬 / 📁 segmented control — both buttons always visible when the search bar has focus; active mode shows the accent halo. Click the inactive button to switch; clicking the active button refocuses the input
- 💬 — message search against raw message content (default)
- 📁 — file search via repo grep
- The whole search bar (segmented control, option toggles, counter, nav arrows) collapses to just the input when focus leaves; placeholder text indicates the active mode at rest
- See [search.md](search.md) for the full search behavior

### Focus-Driven Collapse

The action bar uses a dual-direction collapse pattern keyed on whether the search bar has focus:

- **Search has focus** → neighbouring action-bar controls (preset selector, session buttons, and their dividers) hide via the `.search-collapsible` CSS rule. The search bar expands to fill the row, exposing its segmented mode control, option toggles (Aa / .* / ab), match counter, and prev/next nav arrows
- **Search loses focus** → those controls return, and the search bar's own inner controls collapse via `.search-bar:not(:focus-within)` so only the text input remains visible. Placeholder text (`Search messages…` / `Search files…`) carries the active mode at rest

The symmetry means the action bar always shows either "what the user is searching for" (search expanded) or "what they can do next" (preset, sessions visible) — never both fighting for the same row. Active toggles inside the search bar and outside it share the same accent halo treatment so the user learns one "glowing therefore active" rule across every icon-only control (see [file-picker.md § Active-State Halo](file-picker.md#active-state-halo)).

The **permission-mode indicator is exempt from collapse.** It is the one control in the row that must never be hidden by a focus state: a user who cannot see whether the agent is allowed to write files has lost the plot, and a search box is not a good enough reason.

### Engine Indicator and Notice

AIC⚡DC can run on more than one engine, and a surface the running engine cannot feed is hidden
rather than drawn empty ([plan-ag AG-9](../plan-ag/decisions.md#ag-9)). Hiding one surface is
quieter than drawing one wrong number. Hiding twelve is a UI that reads as broken, and on
2026-09-03 a user reported exactly that — the history browser gone, the always-allow button gone, no
slash commands, no cost — none of which was a fault: `app.json`'s `engines.master` named the second
engine, and every one of those surfaces was correctly hidden. What was missing was any way to find
that out. The only place naming the running engine was the Settings tab's selector, which a user has
to already suspect the engine to go looking for.

**The chip.** A control in the session group naming the engine that is answering. Rendered **only
when `list_engines().mountable` holds more than one entry** — with a single engine there is no
question to answer and a permanent label is furniture. Clicking it opens the notice, the same way
the turn footer's mirror-gap marker forces the health banner open: the indicator and the explanation
should not be two separate things to find. It carries the warning appearance whenever the engine has
unbuilt surfaces, and it carries it *after* the notice is dismissed, which is what makes dismissing
the notice safe rather than a way to lose the only explanation.

**The notice.** A dismissible strip beside the health banner — both are standing conditions about
the channel rather than events in the conversation. It names the engine, lists by name the surfaces
this build has not wired to it, and offers a one-click switch when exactly one alternate is
mountable. With more than one alternate it offers none: that is a choice rather than a recovery, and
choosing the first of three would be picking for the user and calling it a fix.

It lists the surfaces rather than counting them. "5 features unavailable" is the same non-answer as
the empty panels it is explaining — the reader's question is *which* ones, because that is what
tells them whether the thing they reached for is coming back. The names are the descriptor's own
`title` fields, so the browser is not a second author of what a surface is called.

**What opens it is `unbuilt`, never an engine name.** An `absent` surface is a genuine difference
between engines and the UI is complete without it; an `unbuilt` one is a feature this project built
and has not reached this engine with, and its absence is an unfinished application. Only the second
interrupts. On the shipped engine that count is zero, so the notice never appears there. Nothing in
either control branches on which engine is running ([AG-R-4](../plan-ag/risks.md#ag-r-4)): the
descriptor decides whether to speak and carries no engine identity, and the name is read from
`list_engines` purely to render it and to hand back to `switch_engine`.

Switching is a session boundary and says so — the two engines' transcripts do not translate, so the
notice states that a switch starts a new session, and that nothing is deleted and the current
conversation stays loadable.

## Preset Selector

Replaces the mode toggle. A preset is a bundle of turn framing and optionally a Claude Code skill or
agent — see [decisions § CC-12](../plan/decisions.md#cc-12--modes-become-prompt-presets-not-engine-states).
The snippet set that was a preset's third component is gone with snippets themselves
([CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user)); a preset now
shapes only how a turn is framed, not what the composer offers to say.

- A small segmented control at the right end of the search bar, one button per configured preset (`💻` code, `📄` doc by default; the review preset activates from review state, not from this control)
- Active button shows accent-coloured background, a 1px ring, and a soft outer halo in the same accent colour — the halo is the load-bearing affordance because the icon-only buttons live on a dark background where a tint shift alone is hard to read at a glance
- Clicking an inactive button changes the preset. The change is **local and immediate**: it swaps the framing hint for the next turn. There is no RPC round-trip to wait on and no engine state to reconcile, because the engine has no idea presets exist
- No cross-reference toggle. Both indexes are always available as tools; there is nothing to switch on
- Rendered on every tab. There is no per-tab preset gating, because subagent views are read-only and have no input to frame

### What Changed and Why It Is Simpler

The mode toggle was a *backend* control: it swapped the system prompt, changed which index fed prompt
assembly, reset the cross-reference flag, broadcast `modeChanged`, and had to be synchronised across
clients so nobody sent a turn under a stale assumption. None of that applies. Preset state is browser
state, the way the draft in the composer is browser state.

The consequence for collaboration: presets are **per-client**, not global. Two collaborators can hold
different presets without conflict, because a preset only shapes the turn its holder sends. This is a
deliberate simplification, not an oversight — the old global mode existed because the prompt was global.

## Permission Mode Selector

- A labelled control in the action bar showing the current permission mode, always visible (see [`../3-engine/permissions.md` § Permission Mode](../3-engine/permissions.md#permission-mode))
- Clicking opens the mode list with the plain-language description of each. `bypassPermissions` carries an explicit warning and is never preselected
- Selecting calls `set_permission_mode()`. The UI flips on the **broadcast**, not optimistically on RPC success, so multiple clients cannot disagree about the posture
- A mode change is recorded in the transcript as a system event naming who changed it — the posture is part of the conversation's history, because it changes what the rest of the conversation could do
- Non-localhost participants see the control in a read-only form. The RPC would reject them; showing a live-looking control that always fails is worse than showing the truth
- During review the mode is read-only-by-default and the control says why, with an override path (see [`../4-features/code-review.md` § Read-Only Mode](../4-features/code-review.md#read-only-mode))
- A `change` on the control only counts as a choice when a pointer or a key on it came first. A browser restoring form state on load raises `change` too, and `isTrusted` is true for both — so without the gesture the bypass confirmation can be drawn by a page load, asking about a change nobody made. The control is also marked `autocomplete="off"`, which asks the browser not to restore it in the first place

### Review Status Bar

- When review mode is active, a slim status bar appears above the chat input
- Shows review summary — branch, commits, files changed, additions/deletions — and the read-only posture
- No diff-inclusion count; nothing about the review is injected, so there is no in-context set to count
- Commit button is disabled during review
- See [code-review.md](../4-features/code-review.md)

## Subagent Activity

The agent spawns subagents with its own `Task` tool. They are internal to the turn: AIC⚡DC does not
create them, cannot send them a message, and cannot grant them files.

- A turn that spawns subagents grows a row per subagent inside the assistant turn — description, agent type, live status, last tool name, token usage
- Tool cards from a subagent render indented under its row, keyed by `agent_id`, **collapsed behind a disclosure** that counts them
- A row is terminal when its status reaches a terminal value. A task can reach a terminal status with no notification event, so the row must not wait for one to stop spinning
- Clicking a row opens its full transcript in the subagent browser (see [subagent-browser.md](subagent-browser.md))
- A live subagent can be stopped — `stop_task(task_id)` — from its row. This is the only write affordance a subagent row has

### What a Row Shows, and What It Drops

A delegated turn describes the same subagent four times over — the `Task` card's header, the row's head,
the row's summary, and the agent's own prose introducing the delegation. Left alone this reads as though
the turn happened several times; one live run produced a bubble that said "single commit" three times.
Two of the four are dropped at the row.

- **The nested tool cards collapse.** The subagent's transcript already has a tab; drawing it inline is a
  second copy of it. The disclosure stays shut until clicked, including for a live subagent — the head
  carries the running status and the tool it is in, which is the part worth watching from here.
- **The type chip is `subagent_type`, never `task_type`.** Two types ride on the `Task*` messages:
  `task_type` is the *transport* kind (`local_agent`, `local_bash` — what decides whether a task is a
  subagent at all), and `subagent_type` is the agent's own kind (`Explore`, `general-purpose`). Only the
  second means anything to a reader; the first is identical on every subagent row. This applies wherever
  a subagent is labelled — the row's chip, its inline label, the tab strip, the Stop confirmation.
- **The head names the task, not the activity.** The CLI reuses `description` for both: `task_started`
  names the task, then each `task_progress` overwrites it with what the subagent is doing this second.
  The first non-empty one wins, so a settled row is still headed with what the subagent was *asked*
  rather than whatever it was doing last. The activity is not lost — it is the `last_tool_name` chip
  beside it. The tab strip already worked this way; the row now agrees with its own label.
- **The summary stays, and renders as markdown.** It is the notification's `summary`, which is the
  subagent's own closing answer rather than a restatement of its description — verified against a
  headless CLI capture on 2026-08-25, where a task described as "Find magic word in README" reported
  `summary: "The magic word is **ORCHID**."`. It is the only record in the main transcript of what the
  subagent *concluded*, so it survives the collapse.

### What the Old Agent Tabs Did That This Does Not

The old design gave each spawned agent a **full interactive chat tab**: its own `ContextManager`, its
own file selection, its own input box. A user could reply to an agent to resume its work.

That is not possible and will not come back. A `Task` subagent is a conversation between the agent and
itself; there is no seam for a third party to speak into it, and inventing one would mean running our
own parallel agent framework alongside the engine's — exactly the duplication this conversion exists to
remove. What is left is observation plus a stop button, which covers the case that actually mattered in
practice: watching a long fan-out and killing one that has gone wrong.

## Commit and Reset Flows

### Commit (Server-Driven)

- Commit button calls `commit_all`, which returns immediately with a started status
- Server performs the pipeline in a background task: stage all → get the staged diff → ask a **stateless one-shot** (its own short-lived CLI process, `commit.md` as its system prompt, the diff as its only input) → commit with the response
- On completion, server broadcasts the commit result to all connected clients
- All clients show a toast with the short SHA and first line, add a system event message card, and refresh the file tree
- A commit-in-progress guard on both client and server prevents concurrent commits
- Chat panel shows a progress message during the commit, replaced by the result when the broadcast arrives

The message-generation step is off to the side rather than in the conversation, which has two visible
consequences: the staged diff never appears in the transcript the user is reading, and a commit does
not occupy the single-turn slot, so it neither waits for a streaming turn nor blocks one. What the
one-shot was asked and what it answered are not invisible — the commit's own record in
`.aic-dc/events.jsonl` carries the message, and a failure carries the CLI's reason into the toast.

#### Commit-Result Error Path

Because the RPC returns a started status synchronously, the synchronous error branch only catches pre-launch rejections (no repo, already committing, non-localhost, a review in progress). Everything the background pipeline can fail at — staging, **the message-generation one-shot**, the commit itself — surfaces only through the broadcast `commitResult` event. The broadcast carries an `error` string and, for engine failures, a structured `error_info` dict matching the turn's error shape.

Multiple components listen to the broadcast for their own in-flight flag, but exactly one is responsible for surfacing the error:

- **The shell handler** clears its commit flag and, when the broadcast carries an `error`, shows the global toast (`Commit failed: <message>`) and stashes any `error_info` for richer recovery affordances. This is the single source of the error toast.
- **The chat-panel handler** clears its own flag and, on an error, returns without appending a system-event card — deliberately deferring the visible feedback to the shell's toast. It must not duplicate the toast.
- **The file-picker handler** only clears its in-flight flag.

The failure modes changed with the mechanism. There is no smaller model and no separate context window
to exceed; an oversized staged diff now consumes the session's context and may trigger compaction, and
a denied permission or an interrupted turn can leave the pipeline with no message to commit. Each needs
its own message: "commit fewer files", "the turn was interrupted", "permission denied". A regression
that drops the shell's toast makes every background commit failure invisible — the button silently
re-enables with no user-facing feedback.

### Reset to HEAD

- Click shows a confirmation dialog
- On confirm — calls reset RPC
- Server records a system event message in the mirrored history
- Client displays the system event card and refreshes the file tree
- The system event is **not** fed back to the model. The engine's context is the engine's; a reset the agent does not know about is a real hazard, and the honest mitigation is that the next turn's tool calls see the reset files, not that we narrate it

## Broadcast Handling

### User Message Broadcast

- Server broadcasts the user message to all clients before the turn begins, carrying the request ID, the framing's file list, and image refs
- Sending client ignores the broadcast if it has an active request ID that is not a passive stream — it already added the message optimistically
- Collaborator clients add the message to their list immediately so the user message appears before streaming, with the same file hints the sender saw

### Session Sync

- Session-changed event (from remote or local) replaces the entire message list
- Handler resets streaming state, enables auto-scroll, seeds input history from user messages in the loaded session
- Same event fires for a local resume and a remote collaborator's resume — convergent handler

## Toast System (Chat-Local)

- Rendered inside the chat panel, positioned near the input
- Auto-dismisses after a short interval
- Used for chat-specific feedback — copy success, commit result, turn errors, rate-limit warnings
- Does not dispatch global toast events; separate from the shell's global toast layer

### Engine Event Routing

Handler routes `compactionEvent` stages and adjacent engine events to appropriate feedback:

| Stage / event | Handling |
|---|---|
| `pre_compact` | No toast — the shell's `aic-compaction-progress` indicator reads this event directly and holds until `compact_boundary` (see [shell.md § Layout](shell.md#layout)). Carried by a `systemEvent`, driven by the engine's `PreCompact` hook, which is the only thing that fires *before* the pause: `compact_boundary` below arrives once compaction has finished, so it can only explain a stall the user has already read as a hang. A toast did carry it, and was the wrong instrument: it auto-dismissed after 3 seconds while the compaction it announced ran for tens, so the stall was unexplained for most of its duration. Not handled in the `hookEvent` branch either — a `HookEventMessage` arrives twice per hook run, once for `hook_started` and once for `hook_response`, so a branch there would announce one compaction two or three times over |
| `compact_boundary` | A divider card in the transcript with before/after token counts. **Not** a message-list replacement: the engine compacted its own context, and our mirrored transcript is unchanged |
| `reindex` | No toast. The file tree and viewers refresh on `postResponseComplete` |
| Doc enrichment queued / file done / complete / failed | Not rendered as a toast — header progress bar handles these (see [shell.md](shell.md) and [document-index.md](../2-indexing/document-index.md)) |
| `turnUsage` | No toast and no card: it patches the live turn's counter in place (see [§ Live Token Counter](#live-token-counter)). Routed to the tab that owns the request, like a chunk, but not staged through the rAF coalesce — one arrives per assistant message, so there is no burst to fold |
| `rateLimit` | Warning toast on `allowed_warning`, error toast on `rejected`, both naming the reset time and the window in the same words the HUD's section uses (`rate-limit.js`), so one limit is not named two ways. The reset time carries its date whenever it is not today's, which matters most here: a toast is read once, in three seconds, with no gauge beside it to reconsider against (see [viewers-hud.md § A Reset Time With No Date Reads As Today](viewers-hud.md#a-reset-time-with-no-date-reads-as-today--formatresettime)). `allowed` says nothing — the standing figure is the HUD's, and this is the transition |
| `engineHealth` | No toast; the health banner owns this. A mirror gap is a banner, not an interruption. The banner sits between the transcript and the input area beside the disconnected note — both are standing conditions about the channel rather than events in the conversation — and shows the count of failed appends, the engine's last error, the version and credential warnings, and one line per capability the session started without. Amber, not the note's red: the conversation works. Red only once the engine reports the gap count past the repo's `history.mirror_gap_tolerance`, at which point the banner says to read the mirror as broken rather than unlucky and where to look; the comparison is the engine's, and the browser is handed the verdict rather than the threshold. Dismissal is keyed to *which* problems are showing, including how many gaps and whether they have escalated, so a warning that has been read stays quiet and the next thing to go wrong — or the same thing getting worse — does not. `connected: false` alone is not a fault: it is the normal state before the first prompt, and a session that loses its engine says so in `last_error`. Underneath the summary lines, the payload's tail of the CLI's own stderr renders as preformatted output — the last thing the subprocess said, which on a failed connect is the only diagnosis there is. It is the one field that can *only* be read and never raise the banner: it is absent from both the problem test and the dismissal key, because the CLI writes routine chatter there and the tail grows on a healthy session, so it must not open a banner and must not undo a dismissal. Appended after the summary, so a banner forced open on a healthy engine still says the engine reports nothing wrong and then shows the output. The payload has no MCP server list to leave out: `EngineHealth.mcp` was declared, serialised and never written, and was deleted 2026-08-28 — per-server detail is the Context tab's, read from `get_mcp_status()`, which can fail visibly where a field that always answered `[]` could not |
| `disk_warning` (on the state snapshot and on `postResponseComplete`) | A system-event card in the transcript, not a toast: the sentence names a threshold, a cause and what to do about it, which is more reading than a toast's three seconds allow, and the card renders the directory it names as code. Read from *both* carriers, because the server spends one flag on whichever notices first — the snapshot for "checked at startup", a finished turn for a session that crosses the threshold while the browser is open. No request-id filter: the directory belongs to the session, so a collaborator's turn is as good a messenger as our own. The browser adds no one-shot of its own; a second owner of that rule could only disagree with the server's, and would swallow the honest second warning from a restarted server |

| `systemEvent` → `engine_error` | A system-event card in the transcript **and** an error toast. Same argument as `disk_warning` above: the engine's message is more reading than a toast's three seconds allow, and it is the only account of why the turn stopped. The card carries the engine's own words verbatim plus the HTTP code where there is one; the toast carries only enough to send the reader to the card. Repeats are dropped — one error arrives as several `stepUpdate` frames for the same step, and three copies of a 600-character message is a worse transcript than none |
| `systemEvent` → `turn_timeout` | Same treatment. The sentence names the bound that was exceeded **and** says that anything already done stands, including a file already written — which is the half a reader has to act on, and the half a bare "timed out" withholds |
| `systemEvent` → `engine_notice` | Card, no toast. The harness speaking rather than the model: `steps.py` routes `StepType.SYSTEM_MESSAGE` to a `systemEvent` instead of a text block precisely so it is not read as the assistant's prose, which only works if something renders it |
| `systemEvent` → `unknown_step`, `unknown_message`, `step_unreadable` | Nothing in the transcript. These are forward-compat diagnostics about *our* reader rather than about the user's turn, and they have a home in `.aic-dc/engine-errors.jsonl` and the Debug section. Putting them here would spend the reader's attention on our bookkeeping |

Handler accepts events for both the current streaming request ID and the most recently completed request ID, since post-turn housekeeping runs asynchronously after `streamComplete`.

#### Every `systemEvent` but one used to fall off the end of the handler

*Found in the phase-4 live run, 2026-09-03.* An Antigravity turn died on a free-tier `429`. The engine
reported it as a step addressed to **`TARGET_USER`** carrying the quota, the limit of 20 and *"Please
retry in 29.957436016s"*; `steps.py` translated it faithfully into an `engine_error`; and the chat
rendered two tool cards and a stop — no badge, no message, no advice. `onSystemEvent` handled exactly
one subtype, `conversation_reset`, and returned silently for every other.

The bridge that delivers these says the opposite in its own docstring — *"surfaced rather than
swallowed, because a silently dropped message type is how a CLI upgrade breaks the UI invisibly"*
(`app-shell/index.js`). That was the intent; the handler is where it stopped being true, and the gap
was invisible because the one handled subtype is rare and the dropped ones are rarer still on Claude.

**The mechanism is not engine-specific even though the run that found it was.** This is the shared chat
panel, so any engine's `engine_error` met the same silence. Only the *frequency* belongs to
Antigravity, whose free tier caps at 20 requests.

**A system row is `system_event: true`, not `role: 'system'`.** `renderMessage` reads the flag for both
the label and the styling and treats every non-`user` role as the assistant, so a `{role: 'system'}`
message renders under an **"Assistant"** heading — attributing the harness's words to the model, which
is the exact confusion `steps.py` routes `SYSTEM_MESSAGE` away from a text block to avoid. Five
producers already had this right (`events.js`, `tabs.js`, `subagent-tabs.js`); the unsupported-slash
notice did not, and was corrected with this change.

The `compacted` stage of the old pipeline — which replaced the whole message list with a
model-generated summary — has no successor. Compaction is the engine's, it happens to the engine's
context, and the transcript the user is reading is not affected by it. What the divider communicates is
"the agent's memory of everything above this line is now a summary", which is a fact about the model,
not about the page.

## History Browser

- Modal overlay hosted inside the chat panel
- Left panel — session list or search results; preview text, relative timestamp, message count badge
- Right panel — messages for selected session with simplified markdown rendering and image thumbnails, with the session's subagent transcripts listed above them
- Header — title, search input, close button
- All reads (list, messages, search, image bytes) come from the repo-local mirror of the engine transcript, read through the SDK's `*_from_store` parsers. Browsing history never touches the engine — no subprocess, no turn, no context. It is the *same* transcript the engine resumes from, read rather than replayed

### Interactions

- Search — debounced full-text via the search RPC; switches left panel to search results mode; Escape clears or closes
- Session selection — click loads messages via the session messages RPC; preserves selection on close/reopen
- Message actions — hover reveals copy and paste-to-prompt buttons
- Context menu — right-click a message shows options to load in left or right panel of diff viewer, copy, paste to prompt
- **Resume session** — calls `resume_session(session_id)`, which reconnects the engine with that session's context. Dispatches the session-changed event and closes the browser
- **Resume as a fork** — `resume_session(session_id, fork=True)`, leaving the original session untouched. The secondary button beside Resume, and offered **whenever Resume is**, not only on a session already resumed once. Two reasons: the engine spec makes it an invariant ([`../3-engine/history.md`](../3-engine/history.md) § Resume, Fork, and New) because a fork is the choice that cannot damage the original and the native engine had no equivalent; and "already resumed once" is not knowable from a listing row, which carries no resume count. A fork's new id is minted by the CLI on its first turn, so the reply names the origin in `forked_from` and the browser reports the session the user clicked
- **Subagent transcripts.** The selected session's subagents are listed from `list_subagent_transcripts(session_id)`, in a strip above the messages rather than inside the scroller: they belong to the session rather than to any one message in it. Opening one dispatches `view-subagents-requested` with that agent id and **the browsed session's id**, and closes the browser, because the tab it just asked for is behind it. The session id travels explicitly for the same reason the whole listing exists — the transcript being read is usually not the live one, and the panel's default would read the wrong session's subagent directory. This is the only way into a past session's subagents: a turn read back off disk carries no subagent rows to click, because the transcript records each subagent under its own id without attributing it to the turn that spawned it (see [subagent-browser.md](subagent-browser.md#historical-transcripts))
- A listing row is labelled with its `description` and `agent_type`, falling back to the opening words of the prompt the subagent was given and then to the bare agent id. The fallbacks are not defensive: `description` and `agent_type` reach the store as a synthetic `agent_metadata` entry the CLI sends to a *live* mirror, so a session AIC⚡DC watched has them and a session imported from disk does not ([`../3-engine/history.md`](../3-engine/history.md#subagent-transcripts)). A session that delegated nothing draws no strip at all — no header for something that does not exist — and a listing that could not be read says why in the strip's place, the same distinction the return union is for. The listing is its own read, not awaited alongside the messages: it is a directory walk plus a parse per subagent, and a session's conversation must not wait on a feature most sessions do not use
- **Images resolve by pointer.** A prompt's image blocks come back from `history_load` as `image_refs` — session, entry uuid, block index, media type — and each is fetched separately through `history_image`. Sequentially: a session that pasted twenty screenshots would otherwise open twenty concurrent RPCs at a backend whose reads are disk-bound anyway. The tile is drawn at its final size before the bytes arrive, so the preview does not reflow image by image, and a pointer that cannot be resolved keeps a marked tile with the reason — an image silently absent from a prompt reads as a prompt that never had one. Resolved pointers, failures included, are cached for the life of the modal; a missing image is missing every time it is looked at
- Read failures are shown where the list would have been. `history_list`, `history_load` and `history_search` each answer a bare list or `{error}`, and the two halves are drawn differently on purpose: "could not read your history" and "you have no history" want opposite reactions, and the browser used to render the first as the second
- **Delete session** — `history_delete(session_id)`, in the footer beside Resume. It takes two clicks: the first arms the button (`Delete permanently?`), the second sends. The confirmation is a second click rather than a dialog because the act is small and the modal already owns the screen, but it is confirmed at all because the delete is irreversible and spans three files — transcript, images, events log, and the index rows that point at them. An armed button belongs to the session it was armed on: changing the selection or closing the modal disarms it, so a click can never delete a session the user did not arm
- The live session is refused, not deleted: `{error, reason: "session_live"}` becomes a toast ("That is the current conversation. Start a new session first.") and the row stays. Deleting the session the engine is mirroring into would only have it written straight back
- A deleted row leaves the list on the **`sessionDeleted` broadcast**, not on the reply to the delete. The client that asked is not the only one holding a stale list, and a row whose transcript is gone is a click that can only fail; taking the broadcast route means every open browser converges by the same account rather than the acting client converging early and the rest lagging. A failed delete therefore leaves the row exactly where it was
- Close — backdrop click, close button, or Escape

### Resume Is Not Load

The old browser "loaded a session into context" — it read our records and rebuilt a prompt from them.
Resume hands the engine a session id and lets it restore its own context. The distinction shows up in
the UI in two places that must be got right:

- Resuming **replaces the live session**. It is not a preview. The browser confirms before resuming if the current session has messages the user has not seen the end of. "Not seen the end of" is two things: a turn still running, so the end is not written yet, or a reader scrolled up away from the bottom — the same flag that decides whether a new message scrolls into view. The confirmation is the arming step Delete uses, not a dialog: the first click labels the button `Resume anyway?` and the second acts. Nothing is lost either way — the session being left is on disk and in this very list — so the question is worth one click and no more. It applies to Fork as well, because a fork spares the session being *opened*, not the one being left. The chat panel is the one that answers it: this modal covers the transcript it is asking about
- A session in our store that the engine cannot resume (its transcript is gone, or it was written by a different CLI major) is shown as **browsable but not resumable**, with the reason. The alternative — a resume button that fails — trains users to distrust the list. It arrives as `resumable: false` on the listing row, badges the row, disables both footer buttons, and the reason itself comes from the `{error}` that `history_load` answers for the same session. A row that does not carry the field at all is treated as resumable: an unknown must not cost the user a session they could have opened

### Events

| Event | Direction | Purpose |
|---|---|---|
| `session-changed` | Outward (bubbles) | Carries the resumed session's messages to the chat panel |
| `paste-to-prompt` | Outward (bubbles) | Carries message text to insert into chat input |
| `load-diff-panel` | Outward (bubbles) | Load content in diff viewer left or right panel |
| `view-subagents-requested` | Outward (bubbles, composed) | Carries `{agents: [{agent_id, label}], session_id}` to the chat panel's tab strip, which reads the transcript and opens a read-only tab |
| `session-deleted` | Inward (`window`) | Re-dispatched by the shell from the `sessionDeleted` broadcast; drops the row, its search hits, its subagent listing, and the preview if that session was the one selected |
| `open-history` | Inward (`window`) | Opens the browser on behalf of another surface. The only inward window event on the panel that does not come from the server: the Settings tab's session-storage figure argues for deleting sessions and links here, because deletion belongs beside the transcript being deleted rather than behind a settings button ([settings.md § Session Controls](settings.md)). It opens; it does not toggle — a second request from a surface that cannot see the browser must not close it |

## Invariants

- Every rendered streaming element is keyed by block identity; a chunk with a stale `seq` for its block is discarded rather than applied
- Content is cumulative within a block and never assumed cumulative across a turn
- A request has at most one settled assistant message. A second `streamComplete` for a request revises that message rather than adding another, because the payload is the whole turn and not the part since the last one
- Only final-rendered messages detect file mentions — streaming messages never process mentions
- Auto-scroll never disengages during active streaming without a deliberate upward scroll beyond a threshold; height changes from tool-card expansion never disengage it
- The chat-local toast never dispatches global toast events
- Commit, reset, permission-mode changes, and compaction boundaries persist to the mirrored history as system events via the server, not client-side. The first three are appended to the session's events log by the engine as they happen; a compaction boundary needs no writer of ours, because the CLI's own transcript records the boundary and `history_load` reconstructs the divider from that entry. Live, the divider is still appended from the broadcast on every connected client and never optimistically by the one that triggered it — so what the clients show each other and what a reload shows later all come from one account
- One restore path, two routes into it. A transcript reached by resuming (`session-changed`) and the same transcript reached by reconnecting (`state-loaded`) are normalized by the same function, so they cannot render differently. An assistant turn keeps its `blocks`, files, and footer through a restore rather than collapsing to prose, and nothing absent from the record is defaulted — a turn the transcript could supply no usage for draws no footer, and one with no terminal reason draws no badge
- Up-arrow recall is seeded from the restored list, not the raw one, so it skips exactly what the renderer labels as a system event. A compact summary is a user-role record the CLI wrote about the context it dropped, and it is never offered back as something the user typed
- No system event is fed back to the model
- Anything the engine addresses to the *user* reaches the transcript. A turn that stopped for a reason the engine explained never renders as a turn that merely stopped: the explanation is a durable card, not only a toast, because a toast outlives neither the condition nor the reader's absence
- A row the engine authored is marked `system_event`, never merely given a non-`user` role. The label and the styling both read that flag, so an unmarked row is attributed to the assistant — which is the one attribution these rows exist to avoid
- Session-changed handler resets streaming state before replacing the message list
- Passive stream completion always prepends the user message from the result if present
- A turn's tool cards are never hidden by a global preference; the transcript always shows what the agent did
- The permission-mode indicator is visible in every layout state of the action bar
- A surface hidden because the running engine cannot feed it is never hidden anonymously: whenever more than one engine is mountable the action bar names the engine that is answering, and an engine with `unbuilt` surfaces names them. What decides both is the capability descriptor, never an engine-name comparison
- A slash command the engine answers itself — routed or denied — is never sent to the model as prose. Every other one, including an unrecognized one, reaches the CLI unchanged; the palette never rewrites what the user typed except when they pick an entry from it
- The panel never re-runs a tool call, never edits a tool input, and never speaks into a subagent
- A file chip's label and the path its click carries are separate: the label is repo-relative, what is dispatched is the path the engine reported
