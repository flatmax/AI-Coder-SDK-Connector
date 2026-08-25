# Settings

The Settings tab provides access to configuration editing and hot-reload. Config files are read, written,
and reloaded via the settings RPC service with a whitelisted type set. Editing happens inline within the
tab — no separate window or modal.

The tab shrank with the config surface. Eight cards became three, because five of them edited prompt
files AIC⚡DC no longer assembles. What it gained is honesty about *when* an edit takes effect: most of
what remains cannot be applied to a running session, and a Settings tab that pretends otherwise is worse
than one that says so.

> **Parts of this file are ahead of the build.** The tab renders a toolbar, a one-row info banner, the
> model panel, a two-card grid and an inline editor. § Preference Cards, § Session Controls, the § Deleted
> cards note, and the per-field save disposition in § Save Behavior and § The Applies Column Is
> Load-Bearing are **not built** — and some of what they describe exists on a different tab rather than
> being missing. Each item is classified in
> [`../impl-history/work-log.md` § The Settings tab spec describes a tab three times its size](../impl-history/work-log.md#the-settings-tab-spec-describes-a-tab-three-times-its-size).
>
> This banner exists because the drift was not harmless: `/permissions` shipped promising "the Settings
> tab's permission-mode control plus the rules list", and neither is here. A route can only be as honest
> as the spec section it points at.

## Layout

- Info banner at top — config directory. *(Specified as also carrying credential source and CLI path;
  those are on the Context tab, from `get_engine_health` — see the banner above.)*
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
| Engine config | JSON | Partially — `model` and `permission_mode` live; everything else on the next session |
| App config | JSON | Yes — consumers read through accessors, so a saved value takes effect on next access |

Card visual style — icon, label, optional subtitle. Clicking a card opens its content in the inline
editor.

### Deleted cards

`LLM config`, `System prompt`, `System extra`, `Compaction skill`, `Review prompt`, and
`Document system prompt`. The files they edited are retired: still on disk on upgraded installs, never
read, never migrated (see
[`../../specs-reference/1-foundation/configuration.md § Retired files`](../../specs-reference/1-foundation/configuration.md)).

**The note below is not built** — no retired-file note is rendered. It is the cheapest item in this
file's backlog and the only one whose absence the spec already argues is a mistake, in the next
paragraph.

Their absence needs a sentence in the UI, not silence. A user who customised `system_extra.md` over
months and finds the card gone deserves to know why, so the grid carries a dismissible note: retired
files are listed by name, with the explanation that the agent's instructions now come from `CLAUDE.md`
and `.claude/` — which the user edits in the viewer like any other repository file, with the agent's help,
and which the Context tab prices in tokens.

There is no "system prompt" for AIC⚡DC to own any more. That is the conversion in one card.

## The Applies Column Is Load-Bearing

`ClaudeAgentOptions` is assembled once, when the session connects. Only `model` and `permission_mode`
have live setters. Saving an `effort`, `thinking_display`, `max_budget_usd`, `cli_path`, or
`max_buffer_size` change writes the file correctly and changes nothing about the running session.

So the editor labels each field it cannot apply, and after a save that touched one it offers the only
thing that would apply it: **restart the session**, with a warning that this ends the current
conversation (a resume brings the transcript back, but it is a new session either way). Accepting the
save without restarting is fine and normal — the value applies next time.

**Neither half of that paragraph is built.** No field is labelled, and there is no restart control to
offer — no RPC and no button. Worth stating plainly rather than leaving as an aspiration, because this
section exists to prevent a specific failure and currently permits it: a save that touched `effort` shows
an unqualified success toast over a session that did not change. That is the regression named below,
reintroduced by the fix for it never landing.

The failure mode this exists to prevent is a success toast over an unchanged session. It is the specific
regression named in
[`../../specs-reference/1-foundation/configuration.md § Hot reload cannot reach the session`](../../specs-reference/1-foundation/configuration.md).

## Preference Cards

**Not built.** No preference card exists; the grid holds the two config cards and nothing else. Of the
five below, "Permission chime" is live in the permission dialog, and the other four have no
implementation on either side.

Cards that hold a switch rather than an editor, laid out in the same card shape with the control beneath
the icon and label. Descriptions live in the `title` attribute so the grid stays visually uniform.

| Card | Backed by | Effect |
|---|---|---|
| Permission mode | `engine.json` `permission_mode` | The posture a session **starts** in. The *running* session's posture is the chat panel's selector beside the composer, which is always visible and is not reachable by a route — so `/permissions` names this file, not a live control here |
| Thinking display | `engine.json` `thinking_display` | Whether thinking regions arrive at all. Labelled as next-session |
| Permission chime | `localStorage` | Whether a permission request in a background tab plays a sound |
| Deny-read scope | `localStorage` `aic-dc-deny-read-scope` | The remembered answer to the file picker's denial-scope prompt, resettable to `ask` |
| Doc enrichment | `app.json` `doc_index.keywords_enabled` | Whether keyword enrichment runs |

The **Agentic coding** toggle is deleted. It gated whether AIC⚡DC would tell the model about its
`🟧🟧🟧 AGENT` spawn protocol, and there is no protocol left to gate — the agent's `Task` tool is part of
the platform and is not ours to switch off. A user who wants to constrain delegation does it where the
platform expects: a `Task` deny rule in project settings, which the permission layer honours like any
other rule.

## Session Controls

**Not built as a group on this tab.** Engine health and MCP server status are live in the Context tab
(`get_engine_health`, `get_mcp_status`), which is where `/mcp` routes — the open question for those two is
whether this spec should point there rather than duplicate them. MCP **reconnect** is the interesting
gap: `reconnect_mcp_server(name)` exists in `service.py` with no caller anywhere in the browser, so a
failed server is visible and unfixable. Restart-session and session-storage size exist on neither side.

A group the old tab had no equivalent for, because the old engine had no session to control:

- **Engine health** — the resolved `claude` binary and version, credential source, and any auth warning. Read-only, and the first place to look when a turn fails for a reason that is not about code
- **Restart session** — reconnects the SDK client, applying every pending `engine.json` change. Confirmation first, naming what will apply
- **MCP servers** — status per server from `get_mcp_status()`, with a reconnect action for a failed one. `aic-dc` appears here like any other
- **Session storage** — the size of `.aic-dc/sessions/` and a link to the history browser for deletion. Deletion happens there, next to what is being deleted, not behind a settings button

## Editing Flow

1. User clicks a config card
2. Card highlights; its content is loaded via the read-content RPC
3. Content appears in a monospace textarea within the tab (not a separate editor)
4. User edits directly
5. Ctrl+S or Save button writes via the save-content RPC
6. Save triggers the corresponding reload RPC, and surfaces which of the saved fields could not be applied live
7. A separate Reload button re-reads from disk (useful if the user edited the file directly)
8. Close button exits the editor and returns to the card grid

## Editor Toolbar

When an editor is open, a toolbar above the textarea shows the config type icon and label, the file path,
a Reload button, a Save button, and a Close button.

## Save Behavior

- Content is written via the save-content RPC
- On success the reload RPC is invoked automatically, and the response's per-field disposition drives the "applied / applies next session" summary. **Not built** — `save_config_content` returns `{"status": "ok"}` plus an optional JSON-parse warning, carries no per-field disposition, and the tab renders no summary
- Feedback toasts communicate success or failure
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
- Session controls — restart, MCP reconnect, permission mode — are read-only. They are engine mutations, and the collaboration policy puts engine mutations on localhost. *(None of the three is on this tab yet; the rule is the one to apply when they arrive, and `reconnect_mcp_server` already gates on localhost server-side.)*
- The model select is disabled, and the panel says why. `get_model` is read-only, though, so a
  participant still sees which model is answering: without it they could not tell why a turn came back
  cheaper, faster or worse than the last one
- Engine health remains visible, because a collaborator who cannot see why a turn failed cannot help

## Info Banner

- Config directory path. *(Specified as clickable to open in the system file manager; it renders as plain
  text with no handler.)*
- Credential source, with the auth warning when the resolved source is surprising. **Not on this tab** —
  in the Context tab and the chat panel's health banner, from `get_engine_health`
- Resolved `claude` binary path and version. **Not on this tab** — same place, same RPC

The banner reads `get_config_info`, which returns `{"config_dir": ...}` and nothing else. So the two
bullets above could not be rendered from the RPC this banner reads even if the markup were there; they
would need `get_engine_health`, which the Context tab already calls. Whether this tab should duplicate
that or the spec should point there is the open question — see the banner at the top of this file.

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

Two of these have nothing behind them and are marked. An invariant with no implementation is worse than
a missing feature — it reads as a guarantee somebody may rely on.

- Only whitelisted config types can be read or written via the settings RPC; the retired keys are rejected
- Save always writes to the user config directory, never the bundle
- Every save reports which fields applied live and which need a new session; a save never shows an unqualified success for a field that did not apply. **Not enforced** — there is no per-field disposition and no summary, so a save touching a next-session field does show an unqualified success
- No settings path writes an environment variable
- The tab never edits `CLAUDE.md`, `.claude/settings.json`, or `.claude/settings.local.json`
- Editor shows current file content on open — no cached stale content
- Feedback toasts appear for every save and reload
- Non-localhost participants cannot save, reload, restart the session, or change the permission mode or the model; those affordances are hidden or disabled. Enforced for save, reload and the model; **vacuous** for restart and permission mode, which this tab does not offer to anyone
- Engine health is visible to every participant
- The model panel never shows a resolution it did not read from the engine, and never shows the alias
  `default` in place of "no model pinned"
- The model select shows what is in force, not what was clicked: it moves on the RPC reply, and a
  refused switch leaves it where it was
