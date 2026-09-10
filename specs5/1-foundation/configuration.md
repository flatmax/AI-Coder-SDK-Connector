# Configuration

Configuration is split across a small set of files, each with a distinct purpose. A settings service
provides RPC methods for reading, editing, and reloading configs. Packaged builds copy configs to a
persistent user directory on first run.

Two things shrank this layer dramatically. AIC⚡DC no longer has prompts, so the prompt files are gone;
and AIC⚡DC no longer talks to a provider, so the credential and cache-tuning machinery is gone with
them. What remains is thin, and deliberately so: **the engine's own configuration is not ours to
own**. `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, and `.claude/commands/` belong to the
user and reach the session through `setting_sources` (see
[decisions § CC-11](../plan/decisions.md#cc-11--setting_sources-includes-the-project-so-claudemd-is-live)),
not through anything in this spec.

## Config File Set

| File | Kind | Purpose |
|---|---|---|
| `engine.json` | User | Model, commit-message model, default permission posture, reasoning depth, thinking display, optional budget, CLI discovery override, stdout line ceiling |
| `app.json` | Managed, **merged** | Document conversion, document index, indexing debounce, permission timeouts, mirror and session-directory policy, engine master and allowlist, consultant transport, presets |
| `commit.md` | Managed, overwritten | The commit-message request text |

Deleted by the conversion: `llm.json` (superseded by `engine.json`), `system.md`, `system_doc.md`,
`system_extra.md`, `system_agentic_appendix.md`, `review.md`, `compaction.md`, `system_reminder.md`.
Every one of them existed to shape a prompt AIC⚡DC no longer assembles.

`commit.md` survives as the system prompt for the one auxiliary model call the conversion did not
remove: a **stateless one-shot** — its own short-lived CLI process, no tools, no settings sources, no
thinking, `low` effort, one turn — that takes the staged diff and returns a commit message. It is not
a user turn on the live session, and deliberately so: routing a whole staged diff through the
conversation would put it in the transcript the user is reading and would queue behind a turn in
flight. See [`../3-engine/session.md`](../3-engine/session.md).

### The one-shot states its own effort

The one-shot names an effort of `low` rather than letting one be inherited, and the reason is that
"no thinking" and "no stated effort" are not a combination the API accepts. `settings.json` is passed
to this session as a file — it is the only place a machine says which provider to talk to, and
`setting_sources=[]` would otherwise withhold it — so whatever `effortLevel` the user chose for their
conversations arrives with it. The top two rungs of that setting are refused outright alongside
disabled thinking: a user whose conversations run at `xhigh` clicked Commit (September 2026) and got
`400 output_config.effort 'xhigh' is not supported when thinking is disabled on this model` in place
of a message, every time, with no way to commit from the UI at all until the effort was stated here.

`low` is both the floor and the honest description of the work — a diff in, a paragraph out, nothing
to weigh — and it keeps the two halves of the option set from contradicting each other whatever the
machine prefers. It is not configurable: a conversational reasoning depth is a preference, and this
call is not a conversation.

## Engine Config

`engine.json`, a user file, never overwritten on upgrade.

- **Model** — an alias or full model name. Null means the CLI's default, which is the right default for us: the CLI tracks model availability more closely than our config does.
- **Commit model** — the model for the commit-message one-shot, when it should not be the session's. That call is one turn with no tools: a diff in, a paragraph out, nothing to weigh. A small model does it for a fraction of the cost and a little less of the wait, and the conversation keeps the model it was chosen with. Null falls back to **Model** rather than to a model of our choosing, because a default here could only be a full model id and a full model id is provider-specific — a first-party id is a `400` from Bedrock, and the CLI's tier aliases resolve against per-tier defaults a third-party provider may not have. The dialect is the user's to write.
- **Default permission mode** — the posture a new session starts in. `default` unless the user changes it. Live changes go through `set_permission_mode()` and do not write this file unless the user asks to make them the default.
- **Effort** and **thinking display** — reasoning depth, and whether thinking is shown, summarised, or hidden.
- **Budget** — an optional `max_budget_usd` hard stop. Null under subscription billing, where cost is unreported and a budget would be meaningless. See [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap).
- **CLI path override** — an explicit `claude` binary, for installations where discovery picks the wrong one or the bundled CLI is deliberately not used.
- **`max_buffer_size`** — the ceiling on one line of CLI stdout, and **the one field where null does not mean "let the CLI decide"**. The SDK's default is 1 MiB and a single line over it raises inside the reader, which ends the session's message pump; deferring to the dependency is the broken option, so null means 16 MiB rather than "unset". A value *below* the SDK's own default is dropped with a warning, because it could only make that failure arrive sooner. The field exists so a pathological payload the chosen number does not cover is a config edit rather than a release.

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
- **Indexing** — the debounce interval for post-tool-call re-indexing, and the ceiling on how long an `aic-dc` tool call may wait for a pending flush
- **Permissions** — `no_client_timeout_s`, the deadline armed when the *last* localhost client leaves and cancelled when one returns, and `presence_poll_s`, how often presence is re-sampled for the life of a waiting request. **There is no decision timeout, and adding one back is a decision to be argued rather than a default to be restored**: nothing accrues while a request waits — one blocked SDK control request is the whole cost — so a wall-clock limit protects no resource and does the one thing a permission dialog must not do, which is answer for the user. Stop is the escape hatch from a dialog nobody wants to answer. Whatever reads these keys must also keep the screen-reader milestones inside the window that exists; the coarse `[300, 60, 10]` list told a user they had five minutes to answer something expiring in thirty seconds. See [delivery § the timer that answered for the user](../plan/delivery.md#interlude--the-timer-that-answered-for-the-user-2026-08-17)
- **History** — session-directory size warning threshold, and how many mirror-append failures are tolerated before the health banner escalates. The transcript now carries pasted images inline, so the threshold is reached sooner than the native engine's history did. Both thresholds are compared engine-side and the browser is told the verdict — the banner receives "this has escalated", not the number to compare against, for the same reason the disk warning arrives as a sentence: two owners of one rule can only disagree. Both are read on use rather than at construction, so an edit to `app.json` takes effect on the next turn
- **Engines** — `engines.master`, which engine a session starts on ([plan-ag AG-1](../plan-ag/decisions.md#ag-1)). It lives here rather than in `engine.json` despite that file's name: every key in `engine.json` is a *Claude session option* read by `claude_code.engine_config`, and putting a cross-engine fact in one engine's option file would make the second engine's existence conditional on the first's config. An unrecognised name falls back to Claude with a warning — a typo should cost the user the second engine, not the ability to start the application. It is read at startup and at an explicit `switch_engine`, never mid-session, so it does not breach the rule below that nothing in `app.json` reaches a running engine's session options
- **Enabled engines** — `engines.enabled`, an allowlist of engine names defaulting to all of them ([plan-ag AG-17](../plan-ag/decisions.md#ag-17)). A **policy** rather than a preference: some workplaces are only permitted to use Claude, and until this key existed nothing could express that — the `agy` adapter mounted on the binary being on `PATH` with no configuration consulted at all, and `engines.master` names which engine *starts* rather than which may run. Naming Claude alone removes the Antigravity adapters, removes them from the selector, makes `switch_engine` refuse with a reason naming the policy rather than a missing credential, and **removes the consultant** — `second_opinion` and `generate_image` follow the engine rather than carrying a switch of their own, because reaching Antigravity from inside a Claude turn is the same question as running it as master and two switches for one question are two things that can disagree. `claude` cannot be removed: a list omitting it has it added back with a warning, since an install with no engine is not a configuration this application can run. **A malformed value keeps every engine**, which is the deliberate direction for that mistake — a policy that will not parse says nothing about what is permitted, and reading it as a restriction would let a typo silently remove a feature, where reading it as "no policy" leaves the user where they were before they wrote it. Read once, at startup, because the adapters are constructed there; the Settings card that writes it says so rather than promising a session restart would help
- **Consultant transport** — `engines.consultant`, which of Antigravity's two transports answers `second_opinion` and `generate_image` from inside a Claude turn ([plan-ag AG-16](../plan-ag/decisions.md#ag-16)). `auto` by default, and **auto prefers `agy`**: the SDK path authenticates with a metered Gemini key whose free tier allows twenty agent requests a day and *zero* image generations, while `agy` reaches the account holder's own subscription. A default that chose the transport which cannot generate an image would leave the second engine's own worked example broken for the reason it has always been broken. `sdk` and `agy` name one explicitly, and `sdk` is worth having rather than theoretical — a user with a *paid* key may prefer it, because the SDK consultant pins its model and an opinion whose model moved is not a second opinion. An unrecognised value falls back to `auto` with a warning, for the same reason `engines.master` falls back to Claude
- **Presets** — the named bundles that replaced modes: a default tool hint and optionally a Claude Code skill or agent name. The snippet set that was a preset's third component went with snippets themselves — see [decisions § CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user) and [§ CC-12](../plan/decisions.md#cc-12--modes-become-prompt-presets-not-engine-states)

Deleted keys: `url_cache`, `history_compaction`, `cache_tiering` (including every membrane and flux
parameter), and `agents`. The first two describe subsystems the engine now owns; the third describes a
cache that no longer exists; the last gated a spawn protocol replaced by the `Task` tool.

## Config Directory Resolution

- Development mode — config directory relative to source tree
- Packaged builds — bundled configs embedded in the executable, copied to a platform-specific user directory on first run
- Platform paths — Linux, Windows, macOS conventions
- Version marker file tracks which release populated the directory
- All reads go to the user directory so edits persist

## Managed vs User Files

- Managed files — the bundle owns their content (`app.json`, `commit.md`)
- User files — never overwritten (`engine.json`)
- Upgrade creates backup copies of overwritten managed files with a version suffix
- Files outside either set are skipped during iteration

### `app.json` is merged key by key, `commit.md` is overwritten

Both are managed, and the word means something different for each. `commit.md` is prose the bundle
owns: a user who edits it is patching a prompt, an upgrade replaces it, and the backup is how they get
their text back. `app.json` is the file **our own Settings tab writes** — `CONFIG_TYPES` exposes `app`
for editing, `engines.master` moves when a session switches engines, and `engines.enabled` is an
organisation's *policy*. Overwriting it reverted all of that on a version bump, which meant a
Claude-only deployment came back two-provider without anybody being told
([plan-ag AG-R-13](../plan-ag/risks.md#ag-r-13)).

So it is merged, and the merge needs three legs rather than two: a user's copy of this file *is* a copy
of the bundle, taken at install, so comparing it against the new bundle cannot tell "I chose this" from
"this was the default". A hidden `.pristine/` directory inside the user config dir holds each merged
managed file as the bundle last shipped it, and the pass is the familiar three-way one, per key:

| On disk | Ancestor | Bundle | Result |
|---|---|---|---|
| absent | — | present | the bundled value — this is how a key a release *adds* arrives |
| equals the ancestor | present | changed | the bundled value — nobody touched it, so a changed default lands |
| differs from the ancestor | present | anything | the value on disk — an upgrade does not overrule an edit |
| anything | **no record** | anything | the value on disk — with no ancestor there is no evidence the value came from us |
| present | — | absent | the value on disk, left unread, exactly as a retired *file* is |

Nested objects recurse, so `engines.enabled` survives a release that changes `engines.master`'s
default. Lists compare whole: a user who edits `doc_convert.extensions` owns the list, and there is no
per-element ancestry to merge against. The pristine copy is refreshed on every upgrade, so release *n+1*
compares against what release *n* shipped rather than against the original install. The merged file is
re-serialised — key order follows the file on disk with bundled additions appended, and the bundle's
hand-wrapped arrays come back expanded; JSON carries no comments, so there is nothing else to lose. A
file that will not parse is **left for the user to fix** rather than replaced: overwriting swaps text we
cannot read for text they did not write, and every accessor already falls back to its own default, so
the application starts either way.

The merge is also why the accessors' in-code defaults matter. Every key in this file has its default
in Python as well (`doc_convert_config`, `doc_index_config`, `_DISK_WARNING_BYTES`, `master` → Claude,
`enabled` → every engine), which is what makes the file *documentation of what can be set* rather than
the source of the values. Promoting it to a user file instead would have been one line, and would have
frozen every install-time literal in place forever, unreachable by any later default.

### One unwritable file does not cost the others their upgrade

Each file is upgraded inside its own error handling, and the version marker is written even when one of
them could not be. A workplace that pins the policy by shipping `app.json` read-only used to get the
opposite: the `OSError` aborted the whole pass, the marker was never written, so **every subsequent
start retried it** — a fresh backup each time, and `commit.md` never upgraded at all. The pass now
reports the file it left as found, names the marker to delete to retry, and moves on. A backup taken
for a write that then failed is removed again, because a copy of a file we did not change is litter.

The wrapper around the whole pass stays as a backstop, for the case where the user config *directory*
cannot be created at all — there the fallback is reading the bundle directly.

### Retired files are ignored, not deleted

The conversion removes eight files from the managed set. The upgrade pass must **leave them on disk**
rather than deleting them. They may contain a user's customised prompt text, that text represents real
work, and an upgrade that silently deletes it is hostile — the more so because the deletion would be
irreversible and the file would never be read again either way. Ignoring them costs a few kilobytes;
deleting them costs trust.

A repo-local `.aic-dc/snippets.json` is left alone on the same reasoning, and is simply never read
([CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user)). Prose a user
still wants belongs in `.claude/commands/`, where the CLI reads it and the `/` palette lists it.

For the same reason, a leftover `llm.json` is not migrated automatically. Its model name is the only
field with a successor, and the rest of it (`env`, cache tuning, timeouts) maps to nothing. Startup
notices the file, reports it once in the health banner as ignored, and does not touch it.

## Version-Aware Upgrade

- On startup, compare the bundled version against the installed version marker
- Matching versions — no action (fast path)
- Differing versions — new files copied, `app.json` merged, `commit.md` backed up and overwritten, user files preserved
- Version marker updated to current, including when a file had to be left as found

## Backup Naming

- Timestamped with UTC
- Version SHA appended when known
- Allows users to recover customizations made directly to managed files
- Taken only when the upgrade actually replaces something: a merge that changes nothing leaves no
  backup, since a directory of identical copies is noise rather than a recovery path

## Loading and Caching

- App config loaded once and cached; hot-reload available
- Downstream consumers read config values through accessor methods, not snapshot dicts, so hot-reloaded values take effect immediately
- Engine config read on init, and re-read by `restart_session()` — there is no engine reload RPC, because
  a reload that did not replace the subprocess would report applying values the subprocess never saw

### What a config change can and cannot do live

Session options are assembled once, at connect time. That makes the reload story sharper than it was,
and the UI must be honest about it:

| Change | Effect |
|---|---|
| Model | Live, via `set_model()` — but not from this file. Editing `engine.json`'s `model` changes what the *next* session requests; the running session's model is the Settings tab's model panel, which calls `set_model()` directly ([`../5-webapp/settings.md` § Model Panel](../5-webapp/settings.md#model-panel)) |
| Permission mode | Live, via `set_permission_mode()` — and, as with the model, not from this file. The file's value is what the *next* session starts in; the running posture is the selector beside the composer |
| Effort, thinking display, budget, CLI path, buffer size, commit model | Requires a new session. The Settings tab says so per field after a save, and offers the action — `restart_session()`, which re-reads this file and resumes the conversation ([`../3-engine/session.md` § Restart is the only thing that applies an option](../3-engine/session.md#restart-is-the-only-thing-that-applies-an-option)) — rather than appearing to apply and quietly not |
| App config (indexing, doc index, presets, timeouts) | Live on the next use — nothing in it reaches the engine's options |

There is no equivalent of the old `refresh_system_prompt`, and no "prompt composition depends on app
config" problem, because there is no prompt. A config change never invalidates the engine's context.

## Settings Service

- Whitelisted config types can be read and written; only `app` can be reloaded
- A save answers with a per-field disposition — what changed, and what that means for the running session
  — so the UI can qualify its own success message instead of asserting one
- Arbitrary file paths rejected
- The whitelist is now two entries — `engine` and `app` — down from eight; the five prompt entries went with the prompt files, and `snippets` with snippets ([CC-22](../plan/decisions.md#cc-22--snippets-are-deleted-the--palette-replaces-them-user))
- `commit.md` is loaded internally but not exposed via the whitelist, as before

## Per-Repository Working Directory

`.aic-dc/` under the repository root, created on first run, hidden, auto-added to `.gitignore`. It
holds:

| Entry | Contents |
|---|---|
| `sessions/` | Engine transcripts written through our `SessionStore`, plus subagent transcripts. The only transcript there is |
| `events.jsonl` | AIC⚡DC's own operational events — commit, reset, review entry and exit, preset and permission-mode changes |
| `index/` | Derived search, summary and request-ID index. Rebuildable from `sessions/`; safe to delete |
| `doc_cache/` | Document outline cache |
| `tex_preview/` | Generated TeX preview output |

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
- The commit-message one-shot inherits the provider from `settings.json` and nothing else from it: every option that shapes the turn — thinking, effort, permission mode, tools, turn count — is stated outright, so a preference set for conversations can neither reshape nor refuse that call
- Which engine is master is read from configuration, never inferred from what happens to be installed. An engine that is configured but not mountable falls back with a warning rather than starting silently on something else
