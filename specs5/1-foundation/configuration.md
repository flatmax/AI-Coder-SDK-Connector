# Configuration

Configuration is split across a small set of files, each with a distinct purpose. A settings service
provides RPC methods for reading, editing, and reloading configs. Packaged builds copy configs to a
persistent user directory on first run.

Two things shrank this layer dramatically. AC⚡DC no longer has prompts, so the prompt files are gone;
and AC⚡DC no longer talks to a provider, so the credential and cache-tuning machinery is gone with
them. What remains is thin, and deliberately so: **the engine's own configuration is not ours to
own**. `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, and `.claude/commands/` belong to the
user and reach the session through `setting_sources` (see
[decisions § CC-11](../plan/decisions.md#cc-11--setting_sources-includes-the-project-so-claudemd-is-live)),
not through anything in this spec.

## Config File Set

| File | Kind | Purpose |
|---|---|---|
| `engine.json` | User | Model, default permission posture, reasoning depth, thinking display, optional budget, CLI discovery override |
| `app.json` | Managed | Document conversion, document index, indexing debounce, permission timeouts, mirror and session-directory policy, presets |
| `snippets.json` | Managed | Quick-insert chat buttons, keyed by preset |
| `commit.md` | Managed | The commit-message request text |

Deleted by the conversion: `llm.json` (superseded by `engine.json`), `system.md`, `system_doc.md`,
`system_extra.md`, `system_agentic_appendix.md`, `review.md`, `compaction.md`, `system_reminder.md`.
Every one of them existed to shape a prompt AC⚡DC no longer assembles.

`commit.md` survives with a changed nature: it is no longer a system prompt for an auxiliary model
call, it is the text of a **user turn** sent to the session, and its result appears in the transcript
like any other turn. It is a template, not an instruction to a hidden model.

## Engine Config

`engine.json`, a user file, never overwritten on upgrade.

- **Model** — an alias or full model name. Null means the CLI's default, which is the right default for us: the CLI tracks model availability more closely than our config does.
- **Default permission mode** — the posture a new session starts in. `default` unless the user changes it. Live changes go through `set_permission_mode()` and do not write this file unless the user asks to make them the default.
- **Effort** and **thinking display** — reasoning depth, and whether thinking is shown, summarised, or hidden.
- **Budget** — an optional `max_budget_usd` hard stop. Null under subscription billing, where cost is unreported and a budget would be meaningless. See [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap).
- **CLI path override** — an explicit `claude` binary, for installations where discovery picks the wrong one or the bundled CLI is deliberately not used.

### No credentials, and no environment export

`engine.json` has **no `env` block, and configuration exports nothing into the process
environment.**

This is the single most important reversal in this spec. The old `llm.json` carried an `env` dict that
`apply_llm_env` exported at startup, because provider SDKs read credentials from the environment at
client-construction time. Under Claude Code the `claude` CLI resolves its own credentials — a
subscription login, `ANTHROPIC_API_KEY`, or a cloud provider configuration — and injecting anything
into the environment changes which account a turn bills to, silently and invisibly.

The consequences to preserve:

- Nothing in the config layer writes `os.environ`. There is no equivalent of `apply_llm_env`, and no ordering constraint about calling it before constructing a service.
- The **resolved** credential source is read back and reported in engine health, so the user can see which account is in use without us managing it. See [`../3-engine/session.md`](../3-engine/session.md).
- A surprising credential source (an API key present when the user expects a subscription) is an engine-health banner, not a silent fact.

## App Config

`app.json`, a managed file with bundled defaults.

- **Document conversion** — enabled flag, supported extensions, max source size
- **Document index** — keyword model name, enabled flag, top-N, n-gram range, min section chars, min score, diversity, TF-IDF fallback threshold, max document frequency
- **Indexing** — the debounce interval for post-tool-call re-indexing, and the ceiling on how long an `ac-dc` tool call may wait for a pending flush
- **Permissions** — the decision timeout and the shorter no-localhost-client timeout
- **History** — session-directory size warning threshold, and how many mirror-append failures are tolerated before the health banner escalates. The transcript now carries pasted images inline, so the threshold is reached sooner than the native engine's history did
- **Presets** — the named bundles that replaced modes: a snippet set, a default tool hint, and optionally a Claude Code skill or agent name. See [decisions § CC-12](../plan/decisions.md#cc-12--modes-become-prompt-presets-not-engine-states)

Deleted keys: `url_cache`, `history_compaction`, `cache_tiering` (including every membrane and flux
parameter), `cache_warmup`, and `agents`. The first two describe subsystems the engine now owns; the
third and fourth describe a cache that no longer exists; the last gated a spawn protocol replaced by
the `Task` tool.

## Snippets

- Single file with nested structure keyed by preset (code, review, doc)
- Each snippet has an icon, tooltip, and message text
- Legacy flat format supported for backwards compatibility
- Repo-local override first, then the app config directory

The snippet *content* changes with the conversion even though the mechanism does not: snippets that
recited the edit protocol or asked for a cache rebuild are meaningless, and are replaced by ones that
are useful against an agent (ask it to plan first, to run the tests, to review its own diff).

## Config Directory Resolution

- Development mode — config directory relative to source tree
- Packaged builds — bundled configs embedded in the executable, copied to a platform-specific user directory on first run
- Platform paths — Linux, Windows, macOS conventions
- Version marker file tracks which release populated the directory
- All reads go to the user directory so edits persist

## Managed vs User Files

- Managed files — safe to overwrite on upgrade (`app.json`, `snippets.json`, `commit.md`)
- User files — never overwritten (`engine.json`)
- Upgrade creates backup copies of overwritten managed files with a version suffix
- Files outside either set are skipped during iteration

### Retired files are ignored, not deleted

The conversion removes eight files from the managed set. The upgrade pass must **leave them on disk**
rather than deleting them. They may contain a user's customised prompt text, that text represents real
work, and an upgrade that silently deletes it is hostile — the more so because the deletion would be
irreversible and the file would never be read again either way. Ignoring them costs a few kilobytes;
deleting them costs trust.

For the same reason, a leftover `llm.json` is not migrated automatically. Its model name is the only
field with a successor, and the rest of it (`env`, cache tuning, timeouts) maps to nothing. Startup
notices the file, reports it once in the health banner as ignored, and does not touch it.

## Version-Aware Upgrade

- On startup, compare the bundled version against the installed version marker
- Matching versions — no action (fast path)
- Differing versions — new files copied, managed files backed up and overwritten, user files preserved
- Version marker updated to current

## Backup Naming

- Timestamped with UTC
- Version SHA appended when known
- Allows users to recover customizations made directly to managed files

## Loading and Caching

- App config loaded once and cached; hot-reload available
- Downstream consumers read config values through accessor methods, not snapshot dicts, so hot-reloaded values take effect immediately
- Snippets loaded on request with two-location fallback: repo-local first, then app config directory
- Engine config read on init and on explicit reload

### What a config change can and cannot do live

Session options are assembled once, at connect time. That makes the reload story sharper than it was,
and the UI must be honest about it:

| Change | Effect |
|---|---|
| Model | Live, via `set_model()` |
| Permission mode | Live, via `set_permission_mode()` |
| Effort, thinking display, budget, CLI path | Requires a new session. The Settings tab says so, and offers the action, rather than appearing to apply and quietly not |
| App config (indexing, doc index, presets, timeouts) | Live on the next use — nothing in it reaches the engine's options |

There is no equivalent of the old `refresh_system_prompt`, and no "prompt composition depends on app
config" problem, because there is no prompt. A config change never invalidates the engine's context.

## Settings Service

- Whitelisted config types can be read, written, and reloaded
- Arbitrary file paths rejected
- The whitelist is now three entries — `engine`, `app`, `snippets` — down from eight; the five prompt entries went with the prompt files
- `commit.md` is loaded internally but not exposed via the whitelist, as before

## Per-Repository Working Directory

`.ac-dc4/` under the repository root, created on first run, hidden, auto-added to `.gitignore`. It
holds:

| Entry | Contents |
|---|---|
| `sessions/` | Engine transcripts written through our `SessionStore`, plus subagent transcripts. The only transcript there is |
| `events.jsonl` | AC⚡DC's own operational events — commit, reset, review entry and exit, preset and permission-mode changes |
| `index/` | Derived search, summary and request-ID index. Rebuildable from `sessions/`; safe to delete |
| `doc_cache/` | Document outline cache |
| `tex_preview/` | Generated TeX preview output |
| `snippets.json` | Optional per-repo snippet override |

Gone: the symbol map snapshot (the map is rebuilt in memory and served as a tool), the URL cache,
`agents/` from the parallel-agent design, and — per [CC-19](../plan/decisions.md#cc-19) — `history.jsonl`
and `images/`. Pasted images live in the transcript entries that carried them; a `history.jsonl` left by
the native engine is ignored rather than read.

## Invariants

- User files are never modified during upgrade
- Retired managed files are never deleted
- All reads go to the user config directory, not the bundle
- **Nothing in this layer writes to the process environment**
- No configuration file contains a provider credential
- App-config hot-reload takes effect without a server restart and without disturbing the engine session
- The whitelist rejects unknown config type names
- A setting that cannot take effect until a new session is labelled as such in the UI
