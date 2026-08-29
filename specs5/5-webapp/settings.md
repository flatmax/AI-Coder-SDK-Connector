# Settings

The Settings tab provides access to configuration editing and hot-reload. Config files are read, written,
and reloaded via the settings RPC service with a whitelisted type set. Editing happens inline within the
tab — no separate window or modal.

The tab shrank with the config surface. Eight cards became two, because five of them edited prompt files
AIC⚡DC no longer assembles and a sixth held provider credentials it no longer has (§ Deleted cards names
all six). What it gained is honesty about *when* an edit takes effect: most of
what remains cannot be applied to a running session, and a Settings tab that pretends otherwise is worse
than one that says so.

> **Parts of this file are ahead of the build.** The tab renders a toolbar, a one-row info banner, the
> model panel, a two-card grid, an inline editor, the per-field save disposition (§ Save Behavior) and
> the restart control that applies what a save could not (§ Session Controls), the session-storage
> figure beside it (§ Session Controls, built 2026-08-29), and the retired-files
> note (§ Deleted cards, built 2026-08-28). What remains
> specified-but-unbuilt is now only one preference card — Deny-read scope
> (§ Preference Cards), which waits on the prompt it would reset. It is
> classified in
> [`../impl-history/work-log.md` § The Settings tab spec describes a tab three times its size](../impl-history/work-log.md#the-settings-tab-spec-describes-a-tab-three-times-its-size)
> under *(c) Neither side exists*.
>
> **What this file no longer describes at all** is the other half of that drift: six features it filed
> under Settings that were built, and built elsewhere — engine health, credential source, the resolved
> `claude` path and version, MCP server status and its reconnect/enable controls, the permission chime,
> and the live permission mode. They are gone from here rather than annotated, because a spec that
> describes a surface it does not own is how the drift started. Their owners are
> [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md),
> [`viewers-hud.md`](viewers-hud.md), [`permission-dialog.md`](permission-dialog.md) and
> [`chat.md`](chat.md).
>
> This banner exists because the drift was not harmless: `/permissions` shipped promising "the Settings
> tab's permission-mode control plus the rules list", and neither was here. A route can only be as honest
> as the spec section it points at. That one is now settled the other way round — the route names the
> `engine.json` field it can actually reach (§ Config Cards) instead of the tab growing a control so the
> promise could be kept.

## Layout

- Info banner at top — config directory
- Model panel — the model in force and a switch for it, between the banner and the grid and
  deliberately **outside** it (see § Model Panel)
- Card grid — one card per whitelisted config type, plus preference cards
- Inline editor area below the grid — shown when a card is selected
- Active card visually highlighted when its editor is open

## Model Panel

The one control on this tab that applies **now**, which is why it is not a card. Every card here
edits a file and most of what it edits reaches the next session; a model switch is a `set_model`
control request against the running one. Putting it in the grid would have it inherit the grid's
promise and break it.

It is also where the banner's long-standing "resolved model" line finally lands. That line could not
be honoured from `get_config_info`, which stopped reporting a model, and the panel is what reads the
answer properly — see [`../3-engine/session.md` § What the model surface has to read](../3-engine/session.md#slash-commands).

Three lines, in order:

1. **The control** — a select of the models the engine advertises, showing the alias in force. An
   alias in force that the engine does not advertise is added to the list rather than dropped, marked
   as being in force but unlisted: silently showing a different alias as selected would misreport what
   is answering turns.
2. **The resolution** — `alias → model-id`, in monospace, from the handshake's `resolvedModel`. Omitted
   entirely when the engine has not connected, because there is no resolution to show and an em-dash
   would read as one.
3. **The note** — which of these is true: the engine has not connected yet and the list arrives with
   the first turn; only the host may change this; a switch here leaves `engine.json` alone and lasts as
   long as the session; or `engine.json` pins nothing and the CLI is using its own default. Then, always:
   **a switch takes effect from the next turn, and a turn already running finishes on the model it
   started with** — measured, see [`../3-engine/session.md` § A mid-turn switch lands on the next turn](../3-engine/session.md#a-mid-turn-switch-lands-on-the-next-turn).
   It closes by pointing at the usage HUD, where which model actually answered is reported per turn.

   That next-turn sentence is unconditional rather than shown only while streaming. It is true either
   way, and the reader who most needs it is the one watching an expensive turn run away and reaching
   here to make it cheaper — which it will not.

**The control flips on the RPC reply, not on the click.** This is a deliberate departure from the
permission-mode control's never-optimistic rule, and it is allowed because the reply is authoritative:
`Session.set_model` records the new alias only after the control request came back. A refusal
(`restricted`) therefore leaves the select showing what is still true.

**A pointer or key gesture is required before a `change` is honoured** — the same latch as the chat
panel's permission-mode selector, kept for a stronger reason: a model switch has no confirmation
dialog to intercept a phantom `change`, and what it changes is the host's bill.

`/model` routes here as `tab:settings#model`, which flashes the panel and scrolls it into view. A
route also re-reads `get_model()`, and so does the tab becoming visible: the model list is empty until
the engine's first-turn handshake and nothing pushes it when that arrives.

## Config Cards

| Card | Format | Applies |
|---|---|---|
| Engine config | JSON | On the next session — a save reaches the running CLI in no field at all. `model` and `permission_mode` do have live setters, but a save calls neither; it names the control that would (§ Save Behavior), and § Session Controls has the restart that applies the rest |
| App config | JSON | Yes — consumers read through accessors, so a saved value takes effect on next access |

Card visual style — icon, label, optional subtitle. Clicking a card opens its content in the inline
editor.

`/permissions` routes here as `tab:settings#permission-mode`. It opens the Engine config card and selects
the `permission_mode` line, marking the editor so a route that changed nothing else on screen still shows
that it landed — and when `engine.json` sets no such key it says so, because the engine's own default
being in force is the answer rather than a gap to mime. The selection is read from the editor, so an
unsaved line the reader just added is the one they land on. That field is the mode the *next* session
starts in; the running one is the composer's selector ([`chat.md`](chat.md) § Permission Mode Selector).

### Deleted cards

`LLM config`, `System prompt`, `System extra`, `Compaction skill`, `Review prompt`, and
`Document system prompt`. The files they edited are retired: still on disk on upgraded installs, never
read, never migrated (see
[`../../specs-reference/1-foundation/configuration.md § Retired files`](../../specs-reference/1-foundation/configuration.md)).

Their absence needs a sentence in the UI, not silence. A user who customised `system_extra.md` over
months and finds the card gone deserves to know why, so the grid carries a dismissible note: retired
files are listed by name, with the explanation that the agent's instructions now come from `CLAUDE.md`
and `.claude/` — which the user edits in the viewer like any other repository file, with the agent's help,
and which the Context tab prices in tokens. **Built 2026-08-28.** Three properties are load-bearing,
and each of them is a way the obvious implementation would have been worse:

- **It is shown only to installs that have such a file.** The note explains a disappearance, so a
  reader who never had the cards would be reading about something they did not witness. That is why
  `get_config_info` answers with the names *on this disk* rather than the constant list — the
  relevance is computed on the server, where the directory is.
- **The dismissal is keyed on the file list, not a boolean.** If a later upgrade retires something
  else, that name has never been explained to this user and the note is owed again. A flag would
  swallow it.
- **It says the files are not deleted.** That is the reassurance the leave-alone rule exists to
  provide; a note that only said "these are obsolete" would read as a warning that they are about to
  be cleaned up.

The rule the note depends on — that an upgrade never migrates or removes these files — is pinned by a
test alongside the reporting, because if an upgrade ever started cleaning them up the note would
quietly stop having anything to say.

There is no "system prompt" for AIC⚡DC to own any more. That is the conversion in one card.

## The Applies Column Is Load-Bearing

`ClaudeAgentOptions` is assembled once, when the session connects. Only `model` and `permission_mode`
have live setters. Saving an `effort`, `thinking_display`, `max_budget_usd`, `cli_path`, or
`max_buffer_size` change writes the file correctly and changes nothing about the running session.

So a save says, per field, where the value it just wrote takes effect, and the tab offers the only thing
that would apply a next-session one: **restart the session**. Accepting the save without restarting is
fine and normal — the value applies next time, and the fields still waiting are listed beside the button
until it is pressed.

**The naming happens after the save, not as a label on each line.** The disposition is a diff between
what was on disk and what was written (§ Save Behavior), so it names only the fields that actually moved
— which is what the reader wants to know, and is less than a static per-field label would claim. A
textarea also cannot carry annotations, and the Applies column above already says which fields are
next-session in general.

The warning that used to stand here said a restart "ends the current conversation". It does not: the
restart resumes the current session id without forking, so the transcript and the model's context come
back. What does not come back is the CLI's cost total for the session, and a `set_model` or mode switch
made by hand — see [`../3-engine/session.md` § Restart is the only thing that applies an option](../3-engine/session.md#restart-is-the-only-thing-that-applies-an-option).
Both are in the confirmation.

The failure mode this exists to prevent is a success toast over an unchanged session. It is the specific
regression named in
[`../../specs-reference/1-foundation/configuration.md § Hot reload cannot reach the session`](../../specs-reference/1-foundation/configuration.md).

## Preference Cards

**Two of the three are built.** Thinking display and Doc enrichment render above the config grid; Deny-read
scope waits on the prompt it would reset (§ B4 of [`../next.md`](../next.md)), because a control that
forgets a remembered answer to a question nobody is asked yet is a control over nothing.

Cards that hold a switch rather than an editor, laid out in the same card shape with the control beneath
the icon and label, in a grid of their own — a `<select>` with "Engine default" in it does not fit the
110px track the icon cards use. Long descriptions live in the `title` attribute so the grid stays visually
uniform.

| Card | Backed by | Effect |
|---|---|---|
| Thinking display | `engine.json` `thinking_display` | Whether thinking regions arrive at all. Labelled as next-session |
| ~~Deny-read scope~~ | — | **Declined 2026-08-29 — this card will never exist.** It was to reset a remembered answer to the file picker's denial-scope prompt, and that prompt is now decided against rather than deferred, so there is no preference to reset. A reset control is downstream of a choice; when the choice went, the card went with it. [`file-picker.md`](file-picker.md) § *Denial Scope Prompt — declined*, [`../next.md`](../next.md) § E |
| Doc enrichment | `app.json` `doc_index.keywords_enabled` | Whether keyword enrichment runs |

**The note under each control is the card.** Both fields were already editable in the textarea below —
`engine.json` and `app.json` are the two config cards — so a switch adds no capability whatsoever. What it
adds is discoverability plus the one thing a textarea cannot say: when the value it just wrote starts being
true. The two cards deliberately answer that differently, and **neither answers "now"**:

- **Thinking display** is next-session, like every other field in `engine.json` bar the model. The field
  joins the waiting list, which is what makes the restart confirmation name it — so the card hands the
  reader to the control that finishes the job rather than reporting a success it did not achieve
- **Doc enrichment** is next-*pass*. `app.json` is reloadable and the card calls the reload, but the
  consumer is a background build, not a value read per use. Switching enrichment off stops the next pass;
  it does not remove keywords already computed, and switching it back on does not start one. Reporting it
  as applied would be wrong in both directions

**Thinking display is a three-state select, not a switch.** `null` means "let the CLI decide", which is a
different claim from either `summarized` or `omitted` — the null rule in
[`../1-foundation/configuration.md`](../1-foundation/configuration.md) is load-bearing here, and a checkbox
could not have expressed it.

**A switch writes text, not a re-serialised file.** `webapp/src/settings-preferences.js` replaces the value
on its own line and leaves every other byte alone, falling back to parse-and-stringify only for a key the
file does not have yet. Round-tripping this app's own `app.json` through `JSON.stringify` explodes
`extensions` and `keywords_ngram_range` from one line each to twelve: not data loss, but an unrequested
rewrite of the user's file performed by a control that promised to move one boolean. When the file will not
parse at all the switch is disabled and says so, and the reader is sent to the textarea — the surface that
can actually fix it.

**A switch writes through the open textarea when there is one.** If the card's file is open for editing,
the base for the write is the textarea's current content and the result goes back into it. Basing the write
on a stale read is the one failure this control could cause that the textarea alone never could: it would
silently discard whatever the user had typed above it.

**`doc_index.keywords_enabled` had no consumer until this card was built** (found 2026-08-28). It was
parsed by `ConfigManager.doc_index_config` from the day the section existed and read by nothing —
`EnrichmentConfig` never carried an `enabled` field — so enrichment ran whatever the file said. The gate
now lives in `DocIndexBuilder.run_enrichment`, as a callable rather than a captured boolean so an
`app.json` reload reaches a builder constructed at startup. See
[`../2-indexing/keyword-enrichment.md`](../2-indexing/keyword-enrichment.md) § Switching Enrichment Off.

Two rows that stood here have been removed rather than marked, because neither is this tab's to hold. The
**permission chime** is a `localStorage` preference owned by the surface that rings it — see
[`permission-dialog.md`](permission-dialog.md). The **permission mode** is two different things and
neither is a preference card: the running session's posture is the composer's own selector
([`chat.md`](chat.md) § Permission Mode Selector), and the mode the *next* session starts in is a field
in `engine.json`, edited as text in that config card like every other field in it. `/permissions` opens
that card and marks the field.

**Deny-read scope stays**, even though it is also a `localStorage` preference read by another surface. What
separates it from the chime is what its control *does*: a mute belongs beside the thing making the noise,
while this one forgets a remembered answer to a prompt that is by definition no longer on screen. There is
no local place to put it.

The **Agentic coding** toggle is deleted. It gated whether AIC⚡DC would tell the model about its
`🟧🟧🟧 AGENT` spawn protocol, and there is no protocol left to gate — the agent's `Task` tool is part of
the platform and is not ours to switch off. A user who wants to constrain delegation does it where the
platform expects: a `Task` deny rule in project settings, which the permission layer honours like any
other rule.

## Session Controls

**Both of these are built** (restart 2026-08-26, session storage 2026-08-29). They are what is left of
this section after the
things it described that live elsewhere were removed from it: engine health and MCP server status —
including the reconnect and enable/disable controls — are the Context tab's, beside the connection state
and token cost that motivate them. See [`viewers-hud.md` § Session Section](viewers-hud.md).

These two are genuinely this tab's, because both are about the session the *config on this tab*
configures:

- **Restart session** — replaces the CLI subprocess on `engine.json` as it is on disk, applying every
  field a save could not. Confirmation first, and it names the fields waiting rather than asking "restart
  the session?" over nothing: a restart is not free, and a question with nothing named cannot be weighed.
  When no save on this tab is waiting the confirmation says it applies the file as it stands, because the
  edit may have been made in another editor — which is also why the control is always rendered and not
  only offered after a save. The block below the button lists what is waiting, and clears it when the
  restart comes back. Refusals (a turn in flight, an open review) are shown in the engine's own words and
  leave the waiting list alone, because nothing was applied. See
  [`../3-engine/session.md` § Restart is the only thing that applies an option](../3-engine/session.md#restart-is-the-only-thing-that-applies-an-option)
  for the mechanism and the two refusals
- **Session storage** — the size of `.aic-dc/sessions/` and a link to the history browser for deletion.
  Deletion happens there, next to what is being deleted, not behind a settings button: a delete on this
  tab would be a second way to destroy a transcript, sited where the thing destroyed is not on screen.
  The link asks the chat panel to open the browser and minimizes the dialog on the way, the same as the
  Context tab's file links, because the browser opens behind it. Read from
  `ClaudeCodeService.get_session_storage`, which walks the directory the turn-time warning walks and is
  deliberately **not** routed through `_disk_warning`: that one is latched to fire once per server
  lifetime, so borrowing it would mean opening this tab silently spends a warning the user has not seen.
  The reply is `{bytes, over_warning}` — the *verdict*, not the threshold, matching how the health banner
  is handed a mirror-gap verdict rather than `history.mirror_gap_tolerance`; the number behind it is
  user-editable (`history.session_dir_warning_bytes`) and a second copy of it in the browser is a second
  answer waiting to disagree. Three renderings for three answers, and two of them are not sizes: a run
  with no repo says it is not mirrored, and a failed walk says so instead of showing a zero. Nothing is
  rendered before the first read lands. Re-read when the tab is revealed, which is what closes the loop —
  the figure argues for a deletion, the deletion happens in another surface, and coming back here is when
  the new number is worth a round trip

## Editing Flow

1. User clicks a config card
2. Card highlights; its content is loaded via the read-content RPC
3. Content appears in a monospace textarea within the tab (not a separate editor)
4. User edits directly
5. Ctrl+S or Save button writes via the save-content RPC
6. Save triggers the reload RPC where one exists (app config only), and surfaces which of the saved fields could not be applied live
7. A separate Reload button re-reads from disk (useful if the user edited the file directly)
8. Close button exits the editor and returns to the card grid

## Editor Toolbar

When an editor is open, a toolbar above the textarea shows the config type icon and label, the file path,
a Reload button, a Save button, and a Close button.

## Save Behavior

- Content is written via the save-content RPC
- On success the reload RPC is invoked automatically for a reloadable type, and the response's per-field
  disposition drives the "applied / applies next session" summary rendered above the textarea
- **The disposition is computed from the file, not from the field names.** `save_config_content` reads the
  previous content before writing and answers `{compared, changed, live, next_session, live_control}`:
  `changed` is the key-by-key diff, `live` is `changed` for a reloadable type and empty otherwise,
  `next_session` is the other way round, and `live_control` maps a changed field to the control that
  *would* apply it now — `model` to the panel on this tab, `permission_mode` to the composer's selector.
  Two things it deliberately reports rather than hides: `compared: false` when the previous file could not
  be read or parsed, in which case every key is listed because "nothing changed" would be wrong in the
  direction that conceals a field; and `null` for content that is not a JSON object, which has no fields
  to diff and is distinct from an empty `changed`
- **The tab joins "applied", not the save.** A reloadable type's fields are reported as applied only after
  the reload the tab then calls came back successful. A reload that fails leaves them stated as changed on
  disk and not in force — neither applied nor waiting for a restart, because a restart is not what applies
  them
- **`live_control` is a pointer, not a receipt.** A save calls no setter. It names the shortcut so a reader
  who came here to change the model is not sent to a restart for something a select can do
- Feedback toasts communicate success or failure, and a save with fields waiting is **qualified in the
  toast** — "Saved. `effort` applies when the session next starts." rather than "Saved". The panel says the
  same at more length and outlives the toast; the toast is what the reader actually sees
- Invalid content (malformed JSON) produces an error toast with the parse error message; the file is still saved, so a user can recover by re-editing rather than losing their work to a validator

## Reload Behavior

- App config reload — re-reads the file; consumers read through accessors rather than snapshot dicts, so values take effect on next access
- **There is no engine config reload.** `reload_app_config` is the only reload RPC, and the engine card
  is marked non-reloadable so a save on it does not call one. This is the honest shape: session options
  are assembled when the CLI subprocess starts, so a "reload" that reported applying `model` and
  `permission_mode` would be describing two live setters it did not call. The live equivalents exist and
  are reached directly — the model panel on this tab, and the permission-mode selector beside the
  composer — not by re-reading a file
- No reload path touches the environment: nothing in this application writes `os.environ`, and a config that appeared to set credentials would silently redirect the CLI's billing (see [`../../specs-reference/1-foundation/configuration.md § The environment must not be written`](../../specs-reference/1-foundation/configuration.md))

There is no `refresh_system_prompt`, and no config change can invalidate the engine's context.

## Restrictions

- Only whitelisted config types can be edited via this UI; arbitrary paths are rejected by the RPC
- The six retired keys are rejected too, rather than silently accepted against a file with no consumer
- `commit.md` is loaded internally and not exposed to the whitelist — editable only by direct filesystem access
- `.claude/settings.json`, `.claude/settings.local.json`, and `CLAUDE.md` are **not** edited here. They belong to the repository, they are edited in the viewer, and the permission layer writes rules into them on the user's behalf. A second editing path for the same files would race the one the agent uses

## Non-Localhost Participants

When collaboration mode is active and the client is non-localhost:

- Save and Reload are disabled or hidden
- Editors may still be shown read-only for viewing
- Restart session is disabled, and the note beside it says only the host can do it. It is an engine
  mutation twice over — it replaces the host's CLI subprocess and can take the model and permission mode
  back to what the file says — and the collaboration policy puts engine mutations on localhost. The RPC
  gates too; the disabled button only narrows what is offered
- The model select is disabled, and the panel says why. `get_model` is read-only, though, so a
  participant still sees which model is answering: without it they could not tell why a turn came back
  cheaper, faster or worse than the last one

The rule the removed controls followed still holds where they live: a guest sees the Context tab's
connection facts with no buttons on them, and the MCP RPCs gate on localhost server-side regardless of
what any UI offers.

## Info Banner

One row, one fact:

- Config directory path. *(Specified as clickable to open in the system file manager; it renders as plain
  text with no handler.)*

That is the whole banner. `get_config_info` returns one more key than the banner renders —
`retired_files`, the retired config files this install still has on disk — and that is deliberate: it
is a second fact about the same directory, it feeds § Deleted cards rather than the banner, and a
list that is usually empty does not deserve a round trip of its own. The credential source, the auth
warning, and the resolved `claude` path and version were
specified here and are not here — they come from `get_engine_health`, and they are reported where that
RPC is already called, in the Context tab and the chat panel's health banner.

The old banner showed a "smaller model" line. There is no auxiliary model call left to make, so there is
no second model to name.

**The resolved model is not here — it is the model panel's second line.** The banner reads
`get_config_info`, which reports the environment a session was launched into and does not change while
that session runs. The model does change, and it changes from a control on this same tab, so naming it
in a banner that never re-reads would have the tab contradict itself within one screen.

## State Persistence

- Active card / open editor is not persisted across tab switches
- Closing and reopening the tab returns to the card grid

## Invariants

Every invariant below now has something behind it. The two that did not — the per-field save disposition
and a restart for a participant to be refused — are the work described in § Save Behavior and § Session
Controls.

- Only whitelisted config types can be read or written via the settings RPC; the retired keys are rejected
- Save always writes to the user config directory, never the bundle
- Every save reports which fields applied live and which need a new session; a save never shows an unqualified success for a field that did not apply — including in the toast, which is the part a reader sees
- A field is reported as applied only after the call that applied it returned. The save never claims a reload it asked for but has not seen finish
- No settings path writes an environment variable
- The tab never edits `CLAUDE.md`, `.claude/settings.json`, or `.claude/settings.local.json`
- Editor shows current file content on open — no cached stale content
- Feedback toasts appear for every save and reload
- Non-localhost participants cannot save, reload, change the model, or restart the session; those affordances are hidden or disabled. The permission mode used to be named here and is not this tab's to gate
- A restart never claims more than it did: a refusal keeps the waiting list, and an engine that had not started yet is reported as adopting the file rather than as a session restarted
- The model panel never shows a resolution it did not read from the engine, and never shows the alias
  `default` in place of "no model pinned"
- The model select shows what is in force, not what was clicked: it moves on the RPC reply, and a
  refused switch leaves it where it was
