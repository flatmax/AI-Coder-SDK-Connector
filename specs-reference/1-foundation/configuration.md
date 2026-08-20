# Reference: Configuration

**Supplements:** `specs5/1-foundation/configuration.md`

This twin shrank with its parent. The prompt files, the provider settings, the token-counter tables and
the cache-tuning schemas are gone, because the subsystems they configured are gone. What is left is the
config-directory mechanics (which did not change), the two remaining config schemas, and one quirk that
inverted: the environment must **not** be written.

## Byte-level formats

### Version marker file

`.bundled_version` — UTF-8 text file in the user config directory, containing a single version string and optional trailing newline. Written by the config manager after a successful upgrade pass. Absent on first run.

```
2025.06.15-14.32-a1b2c3d4
```

Format matches the baked VERSION string (timestamp + short SHA). See `specs-reference/6-deployment/build.md` (when written) for the release build format; dev installs use the literal string `dev`.

### Backup file naming

When a managed file is overwritten during upgrade, the previous version is renamed as a backup:

```
{filename}.{version}
```

With version known from the marker:

```
app.json.2025.06.15-14.32-a1b2c3d4
```

Without version marker (first upgrade of a pre-tracking install), substitutes a UTC timestamp:

```
app.json.2025.06.15-14.32
```

The version/timestamp suffix is separated from the filename by a literal `.`. Users can recover customizations by diffing backup against current.

## Numeric constants

### Platform-specific user config directory

| Platform | Path |
|---|---|
| Linux | `$XDG_CONFIG_HOME/ac-dc/` or `~/.config/ac-dc/` |
| macOS | `~/Library/Application Support/ac-dc/` |
| Windows | `%APPDATA%\ac-dc\` |

Linux respects `XDG_CONFIG_HOME` when set; otherwise falls back to `~/.config/`. The `ac-dc/` subdirectory name is literal (no version suffix — user customizations carry across releases).

### Managed files (overwritten on upgrade)

| File | Content |
|---|---|
| `app.json` | App config defaults |
| `snippets.json` | Quick-insert chat button defaults |
| `commit.md` | Commit-message request text, sent as a user turn |

Backups written before overwrite; diffs recoverable per backup naming above.

### User files (never overwritten)

| File | Content |
|---|---|
| `engine.json` | Model, permission posture, reasoning depth, thinking display, budget, CLI path override |

Created from bundle on first run if absent. Subsequent upgrades leave them untouched.

### Retired files (present on upgraded installs, never read)

| File | Was |
|---|---|
| `llm.json` | Provider settings, model names, `env` block, cache tuning, timeouts |
| `system.md` | Coding-agent system prompt |
| `system_doc.md` | Document-mode system prompt |
| `system_extra.md` | User-appended system prompt content |
| `system_agentic_appendix.md` | Agent-spawn capability description |
| `system_reminder.md` | Edit-format reinforcement per user prompt |
| `review.md` | Review-mode system prompt |
| `compaction.md` | Topic-boundary detection prompt |

These are **left on disk**, not deleted, not backed up, and not migrated — see the parent spec
§ Retired files are ignored, not deleted. `system_extra.md` is the one most likely to hold real user
work, which is the strongest single argument for the leave-alone rule. Startup reports a leftover
`llm.json` once, in the health banner, as ignored.

### Files loaded but not exposed to Settings RPC

| File | Rationale |
|---|---|
| `commit.md` | Rarely customized; loaded internally for the commit-message turn only |

`system_reminder.md` used to be the second entry. It is gone with the edit protocol.

### Config type whitelist

Three whitelisted identifiers accepted by `Settings.get_config_content` and `Settings.save_config_content`:

| Type key | Maps to |
|---|---|
| `engine` | `engine.json` |
| `app` | `app.json` |
| `snippets` | `snippets.json` |

Any other `type` value returns an error from both getter and setter — including the six retired keys
(`litellm`, `system`, `system_extra`, `compaction`, `review`, `system_doc`), which must not be
silently accepted against a file that no longer has a consumer. Arbitrary file paths are rejected; the
whitelist is the only legal input.

## Schemas

### `engine.json`

Session options for the one `ClaudeSDKClient` this process owns. Every field is nullable, and **null
means "omit the option and let the CLI decide"** rather than "substitute our own default" — see
`specs-reference/3-engine/session.md` § Options assembly for the call site.

```pseudo
EngineConfig:
    model: string | null                   // alias or full id, e.g. "claude-opus-5"; null → CLI default
    commit_model: string | null            // model for the commit-message one-shot; null → `model`
    permission_mode: string | null         // PermissionMode; null → "default"
    effort: string | null                  // reasoning depth; null → CLI default
    thinking_display: string | null        // "summarized" | "omitted"; null → CLI default
    max_budget_usd: float | null           // hard stop; null under subscription billing
    cli_path: string | null                // explicit `claude` binary; null → discovery order
```

**Field semantics:**

- `model` — no provider prefix. The CLI resolves aliases, and `SessionStartedPayload.model` reports what it actually resolved, which is what the UI displays. A config value and a resolved value can legitimately differ.
- `commit_model` — read by `_one_shot_options` in `ac_dc.claude_code.commit`, not by the session. Precedence is `commit_model or model`, and both null omits `model` from the one-shot's options. Whatever the user writes goes to the CLI unaltered: on a third-party provider that means the provider's own id (`global.anthropic.claude-haiku-4-5-20251001-v1:0` on Bedrock), and a first-party id there returns `400 The provided model identifier is invalid`, which surfaces as the toast's failure reason. Tier aliases (`haiku`, `sonnet`) resolve through `ANTHROPIC_DEFAULT_*_MODEL`, which a third-party provider only has if the CLI's probe or the user wrote them — on a Bedrock config carrying only an Opus default, `haiku` resolved to `claude-sonnet-4-5`.
- `permission_mode` — the posture a *new* session starts in, one of the six values listed in `specs-reference/3-engine/permissions.md` § `PermissionMode` has six values. Runtime changes go through `set_permission_mode()` and do not write this file.
- `effort`, `thinking_display`, `max_budget_usd` — read once, at connect time. Changing them requires a new session; the Settings tab must say so rather than appear to apply.
- `cli_path` — bypasses discovery. Present for installs where `PATH` holds an unexpected `claude`, or where the bundled binary must be avoided.

There is **no `env` field**. The old `llm.json`'s `smaller_model` comes back as `commit_model` and
only that: the commit-message one-shot is the single auxiliary model call left to make, and it is the
only thing that field configures.

### `app.json`

App-wide settings organized into six sections.

```pseudo
AppConfig:
    doc_convert: DocConvertConfig
    doc_index: DocIndexConfig
    indexing: IndexingConfig
    permissions: PermissionsConfig
    history: HistoryConfig
    presets: dict[string, PresetConfig]
```

Deleted sections: `url_cache`, `history_compaction`, `cache_warmup`, `cache_tiering` (including
`flux_variant`, `flux_threshold`, and the three `membranes` entries), and `agents`.

**`doc_convert` section:**

```pseudo
DocConvertConfig:
    enabled: bool                  // Default true
    extensions: list[string]       // File extensions to offer for conversion
    max_source_size_mb: int        // Default 50
```

Default extensions list:

```json
[".docx", ".pdf", ".pptx", ".xlsx", ".csv", ".rtf", ".odt", ".odp"]
```

**`doc_index` section:**

```pseudo
DocIndexConfig:
    keyword_model: string                  // Sentence-transformer model name
    keywords_enabled: bool                 // Default true
    keywords_top_n: int                    // Default 3
    keywords_ngram_range: [int, int]       // Default [1, 2]
    keywords_min_section_chars: int        // Default 50
    keywords_min_score: float              // Default 0.3
    keywords_diversity: float              // Default 0.5
    keywords_tfidf_fallback_chars: int     // Default 150
    keywords_max_doc_freq: float           // Default 0.6
```

Default `keyword_model` is `"BAAI/bge-small-en-v1.5"` — a compact English sentence-transformer. Changing the model invalidates all cached keyword enrichments (the cache key includes the model name). Unchanged by the conversion; the document index survives it.

**`indexing` section:**

```pseudo
IndexingConfig:
    reindex_debounce_ms: int        // Default 250
    tool_flush_timeout_ms: int      // Default 2000
```

`reindex_debounce_ms` coalesces `PostToolUse`-triggered re-index work. `tool_flush_timeout_ms` is the
ceiling on how long an `ac-dc` index-reading tool call may block waiting for a pending flush; on expiry
the tool answers from the current index and says so in its result rather than stalling the agent. Both
constants are tabulated with their rationale in `specs-reference/3-engine/session.md` § Numeric
constants, which is authoritative if the two ever disagree.

**`permissions` section:**

```pseudo
PermissionsConfig:
    no_client_timeout_s: int            // Default 30
    presence_poll_s: int                // Default 2
```

There is no decision timeout to configure. A request waits indefinitely while a localhost client is
connected to answer it — see `specs-reference/3-engine/permissions.md` § Numeric constants — so the
only deadline is the one that runs when nobody can answer, and `presence_poll_s` is how often that
condition is re-checked. Neither may be configured to zero or negative; those values fall back to the
defaults, because a zero no-client timeout is an auto-deny that looks like a broken dialog.

**`history` section:**

```pseudo
HistoryConfig:
    session_dir_warn_gib: float         // Default 1.0
    mirror_gap_escalate_after: int      // Default 3
```

`session_dir_warn_gib` measures `.ac-dc4/sessions/`; the warning is one-shot per server lifetime.
`mirror_gap_escalate_after` is how many `MirrorErrorMessage` events in one session turn the health
indicator from a per-turn note into a persistent banner. See
`specs-reference/3-engine/history.md` § Numeric constants.

**`presets` section:**

```pseudo
PresetConfig:
    label: string                  // Shown on the preset selector
    tool_hint: string | null       // Default framing hint appended to a turn, e.g. "prefer reading before editing"
    skill: string | null           // Claude Code skill name to invoke, if the preset maps to one
    agent: string | null           // Claude Code agent name, if the preset maps to one
```

Keyed by preset id; the bundled set is `code`, `review`, `doc`, matching the snippet groups. A preset is
a bundle of *user-turn* framing plus a snippet set — it is not an engine state, and switching one never
reconnects the session. `skill` and `agent` name things the CLI discovered from
`setting_sources`; a name that does not resolve is reported once and the preset still works without it.
See `specs5/plan/decisions.md` § CC-12.

### `snippets.json`

Quick-insert message templates for the chat panel, organized by preset.

**Primary format (nested):**

```json
{
  "code": [
    {"icon": "📋", "tooltip": "Plan first", "message": "Before changing anything, tell me your plan and which files you'd touch."},
    {"icon": "🧪", "tooltip": "Run the tests", "message": "Run the test suite and fix what you broke."}
  ],
  "review": [
    {"icon": "🔍", "tooltip": "Full review", "message": "Give me a full review of this PR."}
  ],
  "doc": [
    {"icon": "📄", "tooltip": "Summarise", "message": "Summarise this document in 3-5 bullet points"}
  ]
}
```

Each snippet entry has:

| Field | Type | Notes |
|---|---|---|
| `icon` | string | Emoji or short glyph shown on the button |
| `tooltip` | string | Hover text |
| `message` | string | Inserted into the chat input on click |

The format is unchanged; the *content* of the bundled defaults is not. Snippets that recited the edit
protocol ("Your last edit was truncated, please continue") or asked for a cache rebuild have no
referent and are replaced by ones that are useful against an agent.

**Legacy flat format (backwards-compatible fallback):**

```json
{
  "snippets": [
    {"mode": "code", "icon": "📋", "tooltip": "...", "message": "..."},
    {"mode": "review", "icon": "🔍", "tooltip": "...", "message": "..."}
  ]
}
```

Reader detects the flat format by the presence of a top-level `snippets` key, groups entries by their
`mode` field (default `"code"` if absent), and surfaces the result in the same nested shape as the
primary format. The key is still spelled `mode` in the legacy format — it is a file format on disk in
users' config directories, and renaming it would break the files it exists to read. It groups by
preset id, which the old mode names happen to match.

### Per-repo snippets override

A repo-local `{repo_root}/.ac-dc4/snippets.json` takes precedence over the user-config version when present. Same format. Falls through to the user config if the repo-local file is absent or fails to parse.

## Dependency quirks

### The environment must not be written

This is the inversion of the old § Provider SDK env-var caching quirk, and it matters for the same
reason that one did: credentials are resolved once, early, and invisibly.

The native engine's `apply_llm_env` exported `llm.json`'s `env` dict into `os.environ` at startup
because litellm's provider clients (boto3 in particular) read credentials at client-construction time
and cache them. Under Claude Code the client is the `claude` CLI, and it resolves its own credentials
from its own config — a subscription login, `ANTHROPIC_API_KEY`, or a cloud provider setup. Exporting
anything into the environment therefore does not configure *our* provider client, because we have none;
it silently redirects the CLI to a different account or endpoint, and the turn bills somewhere the user
did not choose.

So: nothing in the config layer writes `os.environ`, there is no `apply_llm_env`, and there is no
ordering constraint to get right. The resolved source is read back and reported in
`EngineHealth.credential_source`, with `auth_warning` set when it is surprising. See
`specs-reference/3-engine/session.md` § Credential resolution must not be polluted.

A corollary worth stating because it is easy to get wrong while porting: the *subprocess* environment
is not a back door either. The CLI is spawned by the SDK, inherits our environment, and any variable we
add for its benefit has exactly the effect described above.

### Upgrade atomicity

The version-aware upgrade is not atomic — if the process crashes mid-upgrade, some managed files may be overwritten and others not. On next startup the version marker still reflects the OLD bundled version (it's written last), so the upgrade re-runs and catches unfinished files. Partially-written files are simply overwritten again with the new bundle content; user files are never touched either way.

Retired files are inert with respect to this: they are neither copied nor backed up nor removed, in any
pass, at any version.

### Hot reload cannot reach the session

`app.json` reloads take effect on next use, because every consumer reads through an accessor rather
than a snapshot dict. `engine.json` reloads mostly cannot take effect, because `ClaudeAgentOptions` is
assembled once at connect time. Only `model` and `permission_mode` have live setters. There is no
equivalent of the old `refresh_system_prompt` — nothing in either file participates in a prompt, so no
config change can invalidate the engine's context.

The failure mode to avoid is a Settings tab that accepts an `effort` change, writes the file, shows a
success toast, and leaves the running session on the old value. See the parent spec's
§ What a config change can and cannot do live for the required labelling.

## Cross-references

- Hot reload semantics, accessor patterns, upgrade flow narrative: `specs5/1-foundation/configuration.md`
- Options assembly, CLI discovery order, engine numeric constants: `specs-reference/3-engine/session.md`
- Permission timeout rationale and `PermissionMode` values: `specs-reference/3-engine/permissions.md`
- Session-directory sizing and mirror-gap accounting: `specs-reference/3-engine/history.md`
- Keyword enrichment behavioral detail: `specs5/2-indexing/keyword-enrichment.md`
- Settings RPC whitelist enforcement: `specs-reference/1-foundation/rpc-inventory.md` § Service: Settings
- Preset semantics and why modes were not kept: `specs5/plan/decisions.md` § CC-12
