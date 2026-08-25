# Packaging

How config defaults and per-repo working state are distributed with the application and managed across
upgrades. The bundle embeds sensible defaults; the user config directory persists customizations across
releases. A per-repo working directory holds the transcript mirror, session files, and caches.

The word "prompts" left this sentence. Six of the eight bundled config files were prompt text AIC⚡DC
assembled into requests, and none of them has a consumer any more — the agent's instructions come from
`CLAUDE.md` and `.claude/`, which live in the repository and are not ours to package. What remains is
three config files plus one request template, and the interesting part of this spec is now the *upgrade
path off* the old set rather than the maintenance of it. Canonical file list, whitelist, and retirement
policy: [`../1-foundation/configuration.md`](../1-foundation/configuration.md).

## Config Directory Resolution

### Development Mode

- Config directory is relative to the application source
- Used when running from source during development (not from a packaged build or pip-installed package)

### Packaged Builds

- Bundled configs are embedded in the executable (PyInstaller) or installed package data (pip)
- On first run, configs are copied to a persistent user directory
- All reads go to the user directory so edits persist across runs

### Platform-Specific User Directory

| Platform | Path |
|---|---|
| Linux | `~/.config/aic-dc/` |
| Windows | `%APPDATA%/aic-dc/` |
| macOS | `~/Library/Application Support/aic-dc/` |

Resolution uses the platform's standard user config location. Created on first run if missing.

## Version Marker

- A marker file records which release populated the user config directory
- Name indicates this is the bundled-version marker
- Compared against the current bundled version at startup
- Controls the upgrade flow

## File Categories

Two constant sets in the config module control upgrade behavior:

- **Managed files** — safe to overwrite on upgrade (prompts, default settings)
- **User files** — expected to be user-edited, never overwritten

| Category | Files | Upgrade behavior |
|---|---|---|
| Managed | `app.json`, `commit.md` | Overwritten on upgrade; old version backed up with a timestamp/version suffix |
| User | `engine.json` | Never overwritten; only created if missing |
| Retired | `llm.json`, `snippets.json`, `system.md`, `system_doc.md`, `system_extra.md`, `compaction.md`, `review.md`, `system_reminder.md` | **Left on disk untouched.** Never read, never backed up, never deleted |

Files not in any set (e.g., the version marker, directory entries with a leading dot) are skipped during iteration.

### The Retired Set Is a Third Category, Not an Absence

The upgrade pass needs an explicit list of retired names, not just a shorter managed list. With only two
sets, a retired file falls through to "not in either set" and is skipped — which is the right outcome by
accident, and stops being right the moment someone adds a directory-iteration cleanup step or a
"remove files not in the bundle" tidy-up. Naming them keeps the skip deliberate.

`system_extra.md` is the file this matters most for. It was the designated user-customization slot, it was
in the **user** category so it was never overwritten, and a user who wrote three hundred lines of house
style into it over a year has that text and nothing else. It is not migrated: its content was written to be
appended to a system prompt that no longer exists, and pasting it into `CLAUDE.md` unread could change the
agent's behaviour in ways the user did not choose at a moment they were not consulted. Instead, startup
reports once that the file exists and is ignored, with its path, so the user can move what they want into
`CLAUDE.md` themselves.

`llm.json` gets the same treatment for a different reason: only its model name has a successor, and its
`env` block is actively dangerous to honour (see
[`../1-foundation/configuration.md`](../1-foundation/configuration.md) § The environment must not be
written).

### Exempt Managed Files

`commit.md` is loaded internally by the config manager but not exposed to the settings RPC whitelist — it cannot be edited via the Settings tab, only by direct filesystem access.

## Version-Aware Upgrade

On each packaged startup:

1. Read bundled version from the version file inside the executable/package
2. Read installed version from the marker in the user config directory
3. If versions match — no config changes (fast startup)
4. If versions differ (upgrade or first install):
   - **New files** (not yet in user dir) — copied from bundle
   - **Managed files** (already exist) — old file backed up, then overwritten
   - **User files** (already exist) — never touched
   - Version marker updated to the current version

## Backup Naming

When managed files are overwritten during upgrade, the previous version is saved:

- With known version — file plus dot plus timestamp plus dash plus version short SHA
- Without version marker (pre-tracking installs) — file plus dot plus UTC timestamp only

Users who customized managed files directly (instead of using the extra prompt) can diff backups to recover their changes.

## Default Config Values

### Engine Config (`engine.json`, user file)

- Model — a plain alias the CLI resolves, not a provider-prefixed identifier. There is no provider to name
- Commit model — absent by default, which falls back to the session's model. A shipped default would have to be a full model id, and a full model id is provider-specific
- Permission mode — `"default"`
- Effort and thinking display — absent by default, so the CLI's own defaults apply
- Budget — absent by default. A shipped default here would be a spending cap the user did not choose
- CLI path — absent by default; set only to override discovery
- **No environment block.** The field does not exist, and a file carrying one is reported as ignored rather than honoured

The smaller model has one caller left and no default: commit messages come from a stateless one-shot,
which `commit_model` may point at a small model but only by full id. Topic detection went with the
compactor.

### App Config (`app.json`, managed file)

Default sections:

- Document conversion — enabled, supported extensions, max source size
- Document index — keyword model, enabled, top-N, n-gram range, min section chars, min score, diversity, TF-IDF fallback chars, max document frequency
- Indexing — debounce interval for the re-index hook
- Permissions — no-localhost timeout, presence poll interval, diff ceiling, command display cap. No decision timeout; see [configuration.md](../1-foundation/configuration.md#app-config) for why one must not be added back as a default
- History — mirror policy, session-directory size warning threshold
- Presets — the named prompt presets offered in the chat panel

Gone: the URL cache section (URL fetching is deleted) and the history-compaction section (compaction is
the engine's, and its threshold is the engine's `autoCompactThreshold` to report, not ours to set).

### Request Templates

One file, `commit.md`: the system prompt for the commit-message one-shot, which is a fresh CLI process
with nothing in context but this file and the diff. It briefs a model on its role, because that model
has no other briefing — and possibly a small model, so the guidance carries its own examples rather
than assuming judgement. Conventional-commit style is the substance.

## Per-Repository Working Directory

A per-repo working directory at the repo root, hidden (leading dot). Created on first run by the config manager and added to the repo's `.gitignore` file.

### Contents

The directory is `.aic-dc/` — a new name, so a rollback to the previous release finds its own `.aic-dc3/`
state intact rather than a directory of records it cannot parse.

| Entry | Purpose | Lifecycle |
|---|---|---|
| `sessions/` | Engine transcripts written through our `SessionStore`, plus per-session `subagents/`. The browsable archive and the resume source, one file per session | Append-only; a failed append surfaces as a health banner, never a silent gap. Deleted per session from the history browser — which deletes that session's images with it |
| `events.jsonl` | AIC⚡DC's own operational events, keyed by session and request ID | Append-only; never rewritten |
| `index/` | Derived search and summary index | Built from `sessions/`; deletable, rebuilt on next start |
| `doc_cache/` | Disk-persisted document outline cache (keyword-enriched) | Auto-managed by the doc index cache |
| `tex_preview/` | Transient working dir for TeX compilation | Cleaned up on next compilation and on startup |

Gone: the symbol map snapshot (the map is rebuilt in memory and served through the MCP bridge, so a
stale on-disk copy has no reader), the URL cache, the `agents/` directory from the parallel-agent
design, and `snippets.json` ([CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user))
— a leftover copy is ignored rather than deleted, like any retired file.

`sessions/` is the one entry whose growth is worth watching: it holds every turn of every session
including subagent transcripts, and it is the engine's primary record rather than a cache. Layout and
key derivation: [`../../specs-reference/3-engine/history.md`](../../specs-reference/3-engine/history.md)
§ Working-directory layout.

### Creation and Gitignore

- Working directory created on first run (idempotent)
- Subdirectories (`sessions/`, `index/`, `doc_cache/`) created by their respective subsystems with exist-ok semantics; `events.jsonl` is created by its first append
- Gitignore entry added — if the working directory is not already ignored, an entry is appended to the repo's gitignore; duplicate entries avoided
- All operations are idempotent — safe to re-run on subsequent startups

### Cleanup

- No automatic cleanup of old data
- Users can delete the working directory to reclaim space or reset state without affecting application functionality — but the loss is now larger than it was, and the spec should say so plainly: `sessions/` is the engine's transcript, so deleting it removes the ability to resume any past session. Caches rebuild; conversations do not
- A one-shot warning appears when the working directory passes a size threshold, pointing at the history browser where deletion happens next to what is being deleted
- TeX preview directory is the exception — cleaned on every compilation and on server startup since it holds only transient data

## Packaging and Distribution

- PyInstaller builds produce single-file binaries per platform (see [build.md](build.md))
- Pip install distributes the Python package with bundled config defaults as package data
- GitHub Pages deployment serves the built webapp for pip installs that skip the local webapp build

## Invariants

- User files are never modified during an upgrade
- Retired files are never read, never overwritten, and never deleted; their presence is reported once, with their path
- No config file is ever migrated automatically into `CLAUDE.md` or `.claude/`
- Managed files are always backed up before being overwritten
- Version marker is always updated after a successful upgrade pass
- First run always copies all files and writes the version marker
- Same-version restart never modifies any files
- Gitignore entry for the per-repo working directory is never duplicated
- Per-repo working directory creation is idempotent
- All reads go to the user config directory, not the bundle
- Files outside the managed, user, or retired sets are never copied or overwritten
- No packaged config file contains a credential, and no packaging step writes the process environment
- The per-repo working directory name is version-distinct, so a rollback never reads the newer release's records