# Settings

The Settings tab provides access to configuration editing and hot-reload. Config files are read, written,
and reloaded via the settings RPC service with a whitelisted type set. Editing happens inline within the
tab — no separate window or modal.

The tab shrank with the config surface. Eight cards became three, because five of them edited prompt
files AIC⚡DC no longer assembles. What it gained is honesty about *when* an edit takes effect: most of
what remains cannot be applied to a running session, and a Settings tab that pretends otherwise is worse
than one that says so.

## Layout

- Info banner at top — resolved model, credential source, CLI path, config directory
- Card grid — one card per whitelisted config type, plus preference cards
- Inline editor area below the grid — shown when a card is selected
- Active card visually highlighted when its editor is open

## Config Cards

| Card | Format | Applies |
|---|---|---|
| Engine config | JSON | Partially — `model` and `permission_mode` live; everything else on the next session |
| App config | JSON | Yes — consumers read through accessors, so a saved value takes effect on next access |
| Snippets | JSON | Yes — reloaded into the chat panel on next use |

Card visual style — icon, label, optional subtitle. Clicking a card opens its content in the inline
editor.

### Deleted cards

`LLM config`, `System prompt`, `System extra`, `Compaction skill`, `Review prompt`, and
`Document system prompt`. The files they edited are retired: still on disk on upgraded installs, never
read, never migrated (see
[`../../specs-reference/1-foundation/configuration.md § Retired files`](../../specs-reference/1-foundation/configuration.md)).

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

The failure mode this exists to prevent is a success toast over an unchanged session. It is the specific
regression named in
[`../../specs-reference/1-foundation/configuration.md § Hot reload cannot reach the session`](../../specs-reference/1-foundation/configuration.md).

## Preference Cards

Cards that hold a switch rather than an editor, laid out in the same card shape with the control beneath
the icon and label. Descriptions live in the `title` attribute so the grid stays visually uniform.

| Card | Backed by | Effect |
|---|---|---|
| Permission mode | `engine.json` `permission_mode`, live via `set_permission_mode()` | The posture a session starts in, and the running session's posture. Mirrors the chat panel's control; changing it here broadcasts identically |
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
- On success the reload RPC is invoked automatically, and the response's per-field disposition drives the "applied / applies next session" summary
- Feedback toasts communicate success or failure
- Invalid content (malformed JSON) produces an error toast with the parse error message; the file is still saved, so a user can recover by re-editing rather than losing their work to a validator

## Reload Behavior

- Engine config reload — re-reads the file, applies `model` and `permission_mode` to the live session, and reports the rest as pending. It does **not** touch the environment: nothing in this application writes `os.environ`, and a config that appeared to set credentials would silently redirect the CLI's billing (see [`../../specs-reference/1-foundation/configuration.md § The environment must not be written`](../../specs-reference/1-foundation/configuration.md))
- App config reload — re-reads the file; consumers read through accessors rather than snapshot dicts, so values take effect on next access
- Snippets reload — re-reads and broadcasts, so open chat panels pick up the new set

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
- Session controls — restart, MCP reconnect, permission mode — are read-only. They are engine mutations, and the collaboration policy puts engine mutations on localhost
- Engine health remains visible, because a collaborator who cannot see why a turn failed cannot help

## Info Banner

- Resolved model — what the CLI actually resolved, which can legitimately differ from the configured alias
- Credential source, with the auth warning when the resolved source is surprising
- Resolved `claude` binary path and version
- Config directory path, clickable to open in the system file manager

The old banner showed a "smaller model" line. There is no auxiliary model call left to make, so there is
no second model to name.

## State Persistence

- Active card / open editor is not persisted across tab switches
- Closing and reopening the tab returns to the card grid

## Invariants

- Only whitelisted config types can be read or written via the settings RPC; the retired keys are rejected
- Save always writes to the user config directory, never the bundle
- Every save reports which fields applied live and which need a new session; a save never shows an unqualified success for a field that did not apply
- No settings path writes an environment variable
- The tab never edits `CLAUDE.md`, `.claude/settings.json`, or `.claude/settings.local.json`
- Editor shows current file content on open — no cached stale content
- Feedback toasts appear for every save and reload
- Non-localhost participants cannot save, reload, restart the session, or change the permission mode; those affordances are hidden or disabled
- Engine health is visible to every participant
