# Permission Dialog

The browser surface for `can_use_tool`. When the agent wants to do something that needs approval, this
is what the user sees — and what they see is the *consequence*, not a tool name: a Monaco diff for an
edit, the exact command and working directory for a shell call, the question and its options for an
interactive tool.

This is the component that justifies a browser frontend over a terminal, and it is specified before the
rest of the webapp layer for that reason
([`../plan/README.md § Ordering constraints`](../plan/README.md#ordering-constraints-that-are-not-obvious)).
The engine-side contract is in [`../3-engine/permissions.md`](../3-engine/permissions.md); payload
shapes and numeric constants are in
[`../../specs-reference/3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md). This
file specifies the UI.

## Placement

Viewport-scoped modal, above everything: above the draggable dialog panel, above every progress overlay,
above the toast layer. It is **not** a tab and not hosted inside the dialog body.

The reason is that the dialog panel can be minimized, docked, dragged mostly offscreen, or showing the
Settings tab, and a permission request has stalled the turn — it cannot be allowed to render somewhere
the user is not looking. It is the only modal in the application that takes precedence over the shell's
own overlays, including the startup overlay (a permission request during startup is possible when a
session resumes into a pending call).

While a dialog is open the rest of the UI stays readable but inert: a scrim dims the background, and
clicks outside the dialog do nothing. Clicking the scrim is **not** a dismiss — see
[§ Escape and the scrim](#escape-and-the-scrim).

## Anatomy

```
┌──────────────────────────────────────────────────────────┐
│ ✎ Edit  src/auth/session.py            2 of 3   4:52 ⏱  │  header
│ requested by subagent "explore: auth call sites"         │
├──────────────────────────────────────────────────────────┤
│                                                          │
│   [ Monaco diff — current on disk │ proposed ]           │  body
│                                                          │
├──────────────────────────────────────────────────────────┤
│ +12 −3   ▸ full input                                    │  detail strip
├──────────────────────────────────────────────────────────┤
│ Always allow Edit(src/**) ▾ │ Deny ▾ │      Allow once   │  decisions
└──────────────────────────────────────────────────────────┘
```

**Header** — tool class glyph, tool name, and the single most identifying piece of the input (the path,
the first words of the command, the MCP server name). Then the queue position when more than one request
is pending, then — only when something is actually counting down — the countdown to expiry.

The CLI sends its own copy for the call and it is preferred over anything we would compose:
`display_name` ("Read file") is the tool label, and `title` ("Claude wants to read foo.txt") is the
identifying line when the input has no path, command or server to name. Both are new to
`ToolPermissionContext` in 0.2.137 and both may be null; our own `summary` is the fallback, not the
default. The reason for the preference is that the terminal shows the same strings, and a user who runs
both front ends should not see the same call described two ways.

**Subagent attribution line** — rendered only when `agent_id` is non-null. It names the subagent by
`agent_id`, which is the only identifier `permissionRequest` carries. Naming it by *description* — which
an earlier draft of this section asked for, and which is what a user actually wants — needs a description
in the payload, and neither `ToolPermissionContext` nor the tool input has one; the descriptions live in
the `Task*Message` events the chat panel consumes to build its subagent rows. Closing that gap means the
engine correlating `agent_id` against those rows and adding a field, which is subagent-browser work
(phase 6), not dialog work. Until then the line says which subagent by id.

What the line must not do is borrow `title`: `title` is the prompt sentence for *this call*, so using it
here renders `requested by subagent "Claude wants to run npm test"`. A user must never have to guess
whether the main agent or one of twelve subagents is asking, because "the agent wants to edit this" and
"one of the parallel explorers wants to edit this" warrant different answers — but an id answers that
question and a mislabelled sentence does not.

**Body** — varies by `tool_class`; see below.

**Detail strip** — diff stats or command length, plus a collapsed `▸ full input` disclosure carrying the
verbatim tool input as formatted JSON. Always present, for every class. The rendered view is an
interpretation; the disclosure is the ground truth, and a user who suspects the rendering is misleading
must be able to check.

**Decision row** — see [§ Decisions](#decisions).

`decision_reason` and `blocked_path`, when present, render as a short note above the body: "a hook asked
for confirmation", "this path is outside the allowed directories". Why the dialog appeared is part of
what the user is deciding about.

## Body by Tool Class

### `write` — the diff is the feature

A Monaco diff editor, same configuration as the main viewer (see
[diff-viewer.md](diff-viewer.md)): left is `original` (current on-disk content), right is `proposed`.
Read-only until the user asks to edit.

- **New file** (`is_new_file`) — single pane showing the full proposed content with a "new file" label. Not an empty diff against nothing.
- **Binary** (`is_binary`) — no editor. Path, size, and an explicit "binary — cannot be shown" label.
- **Too large** (`too_large`) — diff stats plus the raw input behind the disclosure, labelled "too large to diff". Never a hung Monaco instance and never a silent truncation.
- **Notebook edits** — the target cell rendered as a diff of that cell's source, with the cell index and type in the header.

The initial scroll position is the first changed hunk, not the top of the file. A dialog that opens on
line 1 of a 2 000-line file asks the user to hunt for the change they are approving.

Multi-edit calls (`MultiEdit`, or an `Edit` whose input carries several replacements) render as one diff
per target with a count in the header — "3 changes in 2 files". Each is individually scrollable; the
decision covers the call as a whole, because the tool call is atomic.

### `exec` — the command, verbatim

- The command in monospace, unwrapped, horizontally scrollable, never re-formatted or pretty-printed. What is shown is exactly what will run.
- Working directory, always, on its own line — a command that is fine in the repo root and destructive two levels up is the case this line exists for.
- The agent's own `description` when it supplied one, clearly attributed to the agent rather than presented as fact.
- `flags` (`writes`, `network`, `deletes`) as advisory chips with a tooltip explaining they are heuristics. They shift the default focus (see below) and nothing else. A chip that gated anything would be either bypassable or wrong.
- Commands over the display cap are truncated with a full-text expander, never silently.

### `plan` — the artefact being approved

`ExitPlanMode` asks the user to approve a plan written as markdown, so the body renders it as markdown:
headings, lists, code blocks, tables. The same trust call the chat panel makes — the content is the
model's, `marked` escapes HTML by default — and the alternative is a wall of `##` and `-` for the one
artefact in the whole dialog the user has to read carefully.

- **The plan is never truncated.** Unlike a command, the plan *is* the thing being approved; a plan elided at a display cap is a plan approved unread. It scrolls within the body instead.
- **The header shows the plan's own first line**, hashes stripped, in preference to the CLI's `title` — which says the mode is being left, and says it identically for every such call.
- **The primary button reads "Approve plan"**, not "Allow once". What is being approved is a proposal, and what happens next is the agent starting on it.
- **Default focus is Allow**, unconditionally. A plan is a proposal and every edit it leads to is gated on its own. The `exec` dialog this replaced ran the command flag heuristics over the plan's *prose*, so a plan containing the word "delete" moved focus to Deny.
- **No "always allow".** A standing grant for `ExitPlanMode` would approve every later plan sight-unseen, which is the one thing this dialog is for. The engine declines to derive the rule (§ Suggested rules) and the control is therefore absent.
- **Deny means "keep planning"**, and the prefilled reason says so, because that is where the agent already was.
- `plan` is optional in the CLI's own schema — it is injected from disk by `normalizeToolInput`, with `planFilePath` naming the file — so a call carrying no plan text is a real case. The body says the plan could not be read and points at the verbatim input, rather than rendering a blank pane above an Approve button. Where `planFilePath` is present it is shown, so the user can go and read the file.

This class was added late: `classify_tool` had no entry for `ExitPlanMode`, so it fell through the
unknown-name path to `exec` and the plan arrived as a summarised blob truncated at 4 000 characters,
inside a body captioned "command". A dialog asking for approval of something it is not showing is the
same failure the `write` renderer's fallback rule forbids.

One known gap: when a plan is approved the CLI switches its own permission mode to `prePlanMode ?? default`
**without announcing it on the stream**, so AC⚡DC's mode selector goes on claiming `plan`. Same class of
lie the `note_mode` callback fixed for the other mode transitions; it needs the engine to learn the target
mode rather than guess it, so it is recorded here and not patched.

### `interact` — real choices

`AskUserQuestion` renders its question as prose and its options as actual selectable controls —
radio-style for single select, checkbox-style for multi. The decision row collapses to a single "Answer"
button plus Deny; "always allow" is not offered, because there is no rule that can answer a future
question.

The tool takes a **list** of questions (`input.questions`, each with its own `header`, `options` and
`multiSelect`), not the single question an earlier draft assumed. The payload promotes the first to the
top level and carries the whole list, so the body renders each question with its `header` as a section
label and one control group per question, and "Answer" is disabled until every question has a selection.
A dialog that rendered only the first would silently drop the rest of the agent's ask and answer a
different question than the one it was given.

**Allowing the call is not answering it.** `AskUserQuestion` reads its answers off its own input: the
permission decision has to allow the call *with* an `answers` map merged into the input, keyed by
question text. Allow it plainly and the tool result the agent receives is "The user did not answer the
questions" — the user would see an answered question and the agent would hear silence, which is the
worst of the available outcomes because nothing on either side looks broken. The dialog sends the
selections as `PermissionDecision.answers`, one entry per question — `{options: [<index>, …], text:
"<typed reply>"}` — and the engine builds the map; see
`specs-reference/3-engine/permissions.md` § Answering an `interact` request for why the mapping lives
there and not in the browser.

**The freeform reply is one of the answers, not a separate field.** Each question gets a plain text field
below its options, because the terminal always offers one: the tool's own schema tells the model *not* to
write an "Other" option, on the grounds that the front end provides it. Without one, a user whose answer
is none of the offered options has to deny the call and start again in prose. A typed reply is a complete
answer on its own, so it satisfies the "every question answered" rule above.

It is a field rather than an "Other" radio because typing and picking are mutually exclusive for a
single-select question: typing clears the selection, picking clears the text, and the field's note says
which way round it will be sent. For a multi-select the reply is one more item in the joined list.

An earlier draft of this section said the reply travels as `input.response`. It must not. The CLI's own
result mapping reads `response` *instead of* the answers map — `else if (response?.trim())` precedes the
answers branch — so a call that set both would report the typed reply and silently discard every option
the user also picked. The reply goes into `answers[<question text>]` like any other answer, and
`test_no_response_key_is_ever_written` pins it.

Two affordances the tool supports remain **not built**: the per-option `preview` its terminal UI renders
for comparing mockups, and the per-answer notes (`input.annotations`). Both are additive — a call
answered by option label or by prose is answered correctly without them — and they belong with the rest
of the dialog's polish in phase 6.

This class is always gated by the SDK, so it is the one dialog a user cannot make go away with a rule or
a permissive mode. In `dontAsk` it is denied without ever reaching us, which the dialog therefore cannot
show — the transcript records the denial instead.

### `mcp` — third-party tools

Server name and tool name given equal prominence, since "which server is this?" is the security-relevant
question. Full input rendered as formatted JSON in the body rather than behind the disclosure; there is
no meaningful summary view for an arbitrary third-party tool, and inventing one would hide the payload.

### `read`, `delegate` — normally absent

These classes are ungated, so a dialog for one means something specific happened: a deny or ask rule
matched (often the file picker's deny-read rule), or a hook asked. The body says so, names the rule that
matched when the SDK tells us, and shows the path or the subagent prompt. It never looks like a routine
prompt, because a routine `Read` prompt is exactly the click-through trainer the tiering exists to
avoid.

## Decisions

| Control | Sends | Notes |
|---|---|---|
| **Allow once** | `{action: "allow"}` | Primary. Approves this call only |
| **Allow with edits** | `{action: "allow", updated_input}` | Appears only after the user edits the body; replaces Allow once |
| **Always allow ▾** | `{action: "allow_always", rule_index}` | The button label is the **rule text itself**, e.g. `Always allow Edit(src/**)`. The ▾ opens the other suggested rules |
| **Deny** | `{action: "deny", reason}` | Opens a one-line reason field, prefilled with a sensible default the user can replace |
| **Deny and stop the turn ▾** | `{action: "deny_interrupt", reason}` | Behind the Deny control's ▾. Never the default — a denial the agent can adapt to beats a stopped turn |

### Always allow shows the rule, not a promise

The control is labelled with the rule that will be written and its destination is stated next to it
(`.claude/settings.local.json` for the default, local grant; `.claude/settings.json` for the shared
one). Rules suggested by the CLI (`origin: "cli"`) are offered first and unlabelled; a rule AC⚡DC
derived itself is marked "derived" so the user knows the pattern is our guess at their intent rather
than the CLI's own normalisation.

A grant is **local by default** and reaches the git-tracked file only through the last menu row,
which carries a `shared` tag as well as the filename (CC-16). Two rows differing only by `.local` in
a path is not a distinction someone clicking quickly will make, and this one cannot be undone by
clicking again: the grant travels to every checkout that pulls the commit, where nobody clicked
anything. Nothing in the menu ever names a path under `.claude/` — a rule over the settings file is
a permission to grant permissions.

Two consequences are stated in the control's tooltip rather than left to be discovered:

- The rule applies to the `claude` CLI in this repository too, not just to AC⚡DC. That is the honest consequence of `setting_sources` including the project.
- It is a file the user can read and revoke. There is no invisible session grant behind this button — an in-memory "always" is exactly what the engine spec forbids.

### Editing the input

For `Write` and `NotebookEdit`, the right-hand pane becomes editable on an explicit "edit proposed
content" affordance; for `exec`, the command becomes an editable single-line field. Editing swaps Allow
once for **Allow with edits** so the difference is unmistakable.

**`Edit` and `MultiEdit` get no edit affordance.** Their input is a list of `old_string` → `new_string`
replacements while the pane shows the resulting file, and there is no way back from an edited file to a
set of replacements without guessing which one the user meant. A call that ran something other than what
the dialog displayed would be a worse failure than the missing affordance — and it would break the
"transcript records what actually ran" promise below, since we would not know what actually ran. For
those two tools the answer is allow, or deny with a reason and let the agent redo it. This is narrower
than an earlier draft of this section, which offered editing to all of `write`.

An edited input is recorded in the transcript as the input that actually ran, alongside a marker that
the user modified it. The agent's own record of the call is the SDK's, and it sees `updated_input` — but
a transcript that showed the agent's original proposal while a different command ran would be a
transcript that lies about the repository's history.

Editing is deliberately shallow: it is a scalpel for "almost right, wrong path" and "drop the `-f`", not
an authoring surface. For anything larger, deny with a reason and let the agent redo it.

### Escape and the scrim

**Escape is a deny**, with reason "dismissed by the user", and the dialog says so on the Deny control's
tooltip. Clicking the scrim does nothing at all.

The asymmetry is deliberate. A stray Escape producing a deny is recoverable — the agent gets a reason
and can ask again. A stray click producing anything at all is not acceptable when the dialog is modal
over a UI the user was mid-gesture in. And a dismiss that resolved nothing would leave the turn stalled
behind a dialog the user believes they closed, which is the worst of the three outcomes.

Escape takes priority over every other Escape binding in the application — the chat panel's @-filter,
the snippet drawer, the input-clear chain, the lightbox (see
[chat.md § Escape Priority Chain](chat.md#escape-priority-chain)).

## Anti-Click-Through

The documented failure mode of every permission UI is that users learn to hit the primary button without
reading ([risks § R-12](../plan/risks.md#r-12--the-permission-dialog-becomes-a-click-through)). Four
mitigations, all of them cheap:

1. **Tiering.** Read-only tools are displayed, not gated. This is the big one: it is what keeps prompt volume low enough that a prompt still means something.
2. **No default focus on arrival.** For a short settling interval after the dialog appears, no decision control holds focus and Enter/Space are swallowed. A keystroke already in flight when the dialog opened cannot approve anything. After the interval, focus lands on Allow once — except for `exec` calls flagged `deletes` or `network`, and for `mcp` calls, where it lands on Deny.
3. **Position stability.** Decision controls occupy fixed positions regardless of class, so muscle memory targets the same button — but the *default focus* moves with risk, so muscle memory alone cannot approve a risky call.
4. **Visible prompt count.** The turn footer reports how many prompts a turn produced ([chat.md § Turn Footer](chat.md#turn-footer)), and a per-session count feeds the tripwire in the risk register. If prompts per turn climbs, the tiering is wrong and the number is how we find out.

There is no timed lockout of the Allow button beyond the settling interval and no
type-to-confirm. Both punish the attentive user to protect against the inattentive one, and both get
routed around by habit within a day.

## Queue

`can_use_tool` can fire concurrently — the agent runs tool calls in parallel.

- Exactly one dialog is visible. Additional requests queue.
- Ordering is by `expires_at` ascending, so the request closest to timing out is answered first. A `null` `expires_at` — the normal case, nothing counting down — sorts last, because that request will still be there afterwards. Ties break by arrival.
- A deadline that arms mid-request therefore promotes it, and one that is cancelled demotes it again. The queue reorders live rather than being fixed at arrival.
- The header shows `n of m`. The count is live: a request that expires, is swept by the end of its turn, or is answered on another client leaves the queue and the count drops.
- Nothing is ever auto-answered to clear the queue, and there is no "allow all pending" control. A bulk-approve button is a click-through generator with extra steps.
- A queued `interact` request does not jump the queue. It is gated by the SDK regardless of mode, so it will still be there.

When the queue drains, the scrim releases and focus returns to whatever held it before the first
request — usually the chat input, mid-sentence.

## Countdown

**Most dialogs show no countdown, because most requests have no deadline.** A request waits
indefinitely while a host client is connected to answer it — nothing is consumed while it waits — so
`expires_at` is `null` and there is nothing to render. A dialog that showed a clock anyway would be
inventing a pressure that does not exist, and a coffee break is not a denial
([`../3-engine/permissions.md` § Waiting for an Answer](../3-engine/permissions.md#waiting-for-an-answer)).

When a deadline does exist — no host client is connected, so nobody can answer:

- The countdown renders from `expires_at`, not from a client-side timer started on arrival, so a slow socket does not produce a dialog that claims more time than it has.
- Under a minute remaining it turns amber; under ten seconds, red. While it exists it is never hidden.
- On expiry the dialog closes itself and shows a toast naming what expired and that it was denied because no host client was connected to answer it.
- The dialog says why in place of the ordinary countdown label: no host is connected, this one is counting down, and requests wait indefinitely while a host is here. Without that sentence a countdown appearing on some requests and not others reads as a bug.

**A deadline can arrive or leave while the dialog is open.** `permissionDeadline` arms it when the last
host client disconnects and cancels it when one returns, and the dialog updates in place: the countdown
appears or disappears, the queue reorders, the coarse announcements reset. What it does *not* do is
rebuild the dialog — a half-typed deny reason survives, and the settling interval is not restarted by a
clock the user did not touch. A promoted request that becomes current does get a settling interval,
because it is newly on screen.

The countdown is not decoration where it appears: it means nobody who could answer is here, and a user
who returns should be able to see that a decision was taken for them and what it was.

## Multiple Clients

- The request is broadcast to every client. Only localhost clients get decision controls.
- **Non-localhost clients** see the same body — the diff, the command, the question — with the decision row replaced by a note: "only the host can answer this". Read-only, not hidden: a collaborator who cannot see what the agent asked for cannot review what it did, and the restriction is on authority, not on information (see [`../4-features/collaboration.md`](../4-features/collaboration.md)).
- **Racing localhost clients** — the first decision wins. `permissionResolved` closes the dialog everywhere with an attribution note ("allowed by another window"), including on the client that was mid-typing a deny reason. The losing client's in-progress reason text is discarded, not resubmitted against the next request in the queue.
- A `resolve_permission` that arrives late gets `already_resolved` with the winner's identity, and the UI shows the attribution rather than an error.

## Reconnect

`get_current_state` carries `pending_permissions`, each a full request payload with its original
`expires_at`. The dialog reconstructs from that list on connect:

- A refresh mid-request re-opens the dialog with the remaining time, not a fresh countdown — and with no countdown at all when the request has no deadline, which is what a refresh during an ordinary wait looks like.
- A request resolved while the browser was away — expired, swept by the end of its turn, or answered elsewhere — is simply absent from the snapshot; the transcript carries the denial and its cause.
- `permissionDeadline` is session-wide rather than turn-scoped for this reason: a request outlives the moment it was raised, so a client that reloaded still has to be told when its clock starts.
- The settling interval applies to a reconstructed dialog too — a page load should not be able to approve anything either.

## Attention

A blocked turn behind a dialog in a background tab is a stall the user cannot see. When the document is
hidden on arrival:

- The page title is prefixed with a marker and the pending count.
- An optional short chime, default on, muted by a Settings toggle. It fires once per queue transition from empty to non-empty, not once per request — a fan-out of nine gated calls must not produce nine chimes.

Nothing about attention changes the decision path, and no notification carries an action. The user comes
back to the tab and reads the dialog.

## Accessibility

- `role="dialog"`, `aria-modal="true"`, labelled by the header; focus is trapped for the dialog's lifetime.
- On arrival, screen readers are given the class, the tool, and the target — "permission request: edit, src/auth/session.py, 12 added 3 removed" — rather than being walked through the diff. The diff itself is navigable but is not the announcement.
- Where a countdown exists it is announced at coarse intervals (five minutes, one minute, ten seconds) via a polite live region. A per-second live region is unusable. A milestone above the time the request actually has is never announced — over a thirty-second window, "five minutes left" is worse than silence — so a clock arming announces itself with the real remaining instead, which is the one thing a reader cannot pick up from a numeral that just appeared.
- Announcements say "30 seconds left", not the chip's `0:30`, which reads as "zero colon thirty".
- Every decision control has a keyboard path; the ▾ menus are proper menus, not hover-only.
- Colour is never the only carrier of risk: the advisory chips carry text, and the countdown's urgency states carry the numeral.

## Invariants

- The dialog renders above every other surface in the application, including the startup overlay and the toast layer.
- The dialog resolves exactly once, through `resolve_permission`, a broadcast `permissionResolved`, or expiry. It never closes without one of those.
- A `permissionDeadline` never closes a dialog, never restarts its settling interval, and never discards a half-typed deny reason. It changes only whether a clock is running.
- Escape denies with a reason. The scrim does nothing. Neither ever dismisses a request unresolved.
- Every `write` request shows a diff, the full new content for a new file, or an explicit binary/too-large label — never a tool name and a JSON blob alone.
- Every request, of every class, offers the verbatim tool input behind a disclosure.
- The rendered command for an `exec` request is byte-identical to what will run, modulo an explicit truncation with an expander.
- No decision control holds focus during the settling interval, and Enter/Space are swallowed for its duration — on first appearance and on reconnect alike.
- Default focus is Deny for `mcp` requests and for `exec` requests flagged `deletes` or `network`.
- The "always allow" control is labelled with the rule text and its destination file. It never writes a bare tool grant and never a session-only grant.
- A deny always carries a non-empty reason.
- An edited input is recorded in the transcript as the input that ran, marked as user-modified.
- `agent_id` is always surfaced when non-null; a subagent request is never presented as coming from the main agent.
- Exactly one dialog is visible at a time; queued requests are never auto-answered and there is no bulk approve.
- Non-localhost clients see the full request body and no decision controls.
- The countdown derives from `expires_at`. It is absent exactly when `expires_at` is null, and never hidden when it is not — a dialog never invents a deadline, and never conceals one.
