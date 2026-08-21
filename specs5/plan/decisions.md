# Conversion Decisions

Binding choices for the Claude Code conversion. Each has an ID (`CC-n`), the decision, and why.
The specs in `specs5/` assume every decision here. Where a decision contradicts
[`origin-brief.md`](origin-brief.md), this file wins.

Decisions marked **(user)** were made by the project owner directly and are not open for
re-litigation during implementation.

---

## CC-1 — Total replacement, not a dual-engine mode **(user)**

Claude Code replaces the native engine. There is no engine-selection switch, no
`engine: "native" | "claude_code"` config key, and no fallback path that reconstructs a tiered
prompt.

The origin brief proposed a mode ("the native engine remains the default; this is a mode"). That
is explicitly overridden: *"can you rip the guts out of the old system to replace it"*.

**Why it matters:** a dual-engine build has to keep prompt assembly, the stability tracker, the
token counter, and the edit protocol alive and tested for a path most users would never take. The
cost of the second engine is not the engine — it is that every surface downstream of it (chat
rendering, context tab, HUD, history, settings) has to support two shapes forever. Deleting one
engine is cheaper than maintaining the seam.

**Consequence:** `litellm` leaves the dependency set. So do the four-tier cache, the membrane /
flux controller, the prompt assembler, the context manager, the history compactor, the token
counter, and the emoji edit protocol.

---

## CC-2 — The symbol index and document index survive **(user)**

Both tree-sitter indexes are kept, including keyword enrichment on the document side. What
changes is who consumes them.

| Consumer | Before | After |
|---|---|---|
| Prompt assembly | Primary consumer — the aggregate map was the bulk of L0 | **Gone** |
| Monaco LSP (hover, go-to-definition, references, completions) | Secondary | **Primary** |
| File picker outlines, doc navigation, SVG structure | Secondary | Retained |
| Claude Code | — | **New** — via MCP tools (see CC-6) |

**Why it matters:** the indexes are the one piece of the engine that is genuinely AC⚡DC's own
intelligence rather than plumbing around a provider. They give Monaco real language features
without an LSP server per language, and they let the agent buy a whole-repo structural picture
for a few hundred tokens instead of a directory walk plus twenty `Read` calls.

**Consequence:** `2-indexing/` stays in the spec tree essentially intact; its integration
sections are repointed from cache tiering to the MCP bridge and the browser.

---

## CC-3 — Transcript mirrored to `.ac-dc4/`, context continuity via the SDK **(user)**

> **Partly superseded by [CC-19](#cc-19) (2026-08-16).** The split below stands. The second writer
> does not: there is one store, `HistoryStore` is retired, and the browser reads the mirrored
> transcript through the SDK's parsers.

History is split cleanly in two:

- **Display and search** — AC⚡DC persists the rendered message stream to `.ac-dc4/` as JSONL and
  keeps `HistoryStore`, the history browser, and full-text search. This is what the user browses.
- **Context continuity** — owned entirely by the SDK. Resumption is `resume=<session_id>` (plus
  `fork_session` to branch); the conversation state the model sees is Claude Code's, not ours.

AC⚡DC additionally implements the SDK's `SessionStore` protocol against `.ac-dc4/` so the
authoritative transcript lives inside the repo working directory rather than only under
`~/.claude/projects/`.

**Why it matters:** the two jobs have different requirements and conflating them is what made the
old history layer complicated. A browser wants stable records with metadata, thumbnails, and
search. A model wants a correct, compactable conversation. The SDK is better at the second job
than we are, and it is the only thing that can be — it owns compaction. Keeping our own store for
the first job costs one append per message and keeps every existing history surface working.

**Consequence:** the LLM-driven compactor (topic-boundary detection on a smaller model, verbatim
windows, summarize-vs-truncate) is deleted. Claude Code compacts itself; we render the
`SystemMessage(subtype="compact_boundary")` it emits so the user can see it happened. There is no
`CompactBoundary` class — the subtype falls through the SDK's parser to a generic `SystemMessage`,
so its payload is untyped and must be read defensively out of `.data`.

---

## CC-4 — Claude Code's context is visualised, not guessed **(user)**

The Context tab is not deleted. It is rebuilt on `ClaudeSDKClient.get_context_usage()`, which
returns the same data the CLI's `/context` command renders: per-category token counts with
colours, total / max / raw-max, percentage, model, auto-compact state and threshold, `CLAUDE.md`
and memory files with per-file token counts, MCP tools, agents, system-prompt sections, system and
deferred built-in tools, slash commands, skills, a message breakdown, and cumulative API usage.

The user asked whether this was possible. It is, and it is strictly better than what the tab
showed before: the old tab displayed *our model of* the prompt, which could drift from what the
provider actually received. `get_context_usage()` is the engine's own accounting.

**Why it matters:** losing context visibility was the main thing the conversion threatened to
cost. Without it the tool becomes opaque — the user cannot tell why a turn was expensive, whether
auto-compact is about to fire, or what the agent currently knows. This decision turns the
conversion's biggest regression into an upgrade.

**Consequence:** `5-webapp/viewers-hud.md` is rewritten rather than retired. Tier bars, N-values,
stability bars, promotion/demotion logs, and the rebuild button all go; category bars, memory-file
rows, tool inventories, and an auto-compact gauge take their place. Per-turn cost and rate-limit
state come from `ResultMessage.model_usage` / `total_cost_usd` and `RateLimitEvent`.

---

## CC-5 — Scope of the first pass: plan and specs only **(user)**

This pass writes `specs5/plan/` and rewrites `specs5/`. Nothing under `src/` or `webapp/` is
touched.

**Why it matters:** the specs are the contract the code will be written against, and the rip-out
touches ~28k lines across two languages. Agreeing the target shape on paper is the cheap half.

---

## CC-6 — The indexes reach Claude Code as MCP tools, not as prompt text

AC⚡DC registers an in-process SDK MCP server (`ac-dc`) exposing its repo intelligence as tools
the agent may call. It does **not** pre-inject the symbol map into the system prompt or via a
`UserPromptSubmit` hook's `additionalContext`.

**Why it matters:** pre-injection is the native engine's model, and it is the model that made
caching necessary in the first place — every turn carried the whole map whether the turn needed it
or not. On-demand tool calls invert that: the agent pays for structure only when it asks for
structure, and Claude Code's own cache handles the repeat case. It also keeps AC⚡DC honest about
what it adds: a tool that returns a whole-repo map in one call is a real capability, visible in
`/context`'s MCP-tools section, and attributable in the transcript.

**Rejected alternative:** `UserPromptSubmit` hook injecting the map on the first turn. Cheaper to
build, but it re-creates the un-auditable "the prompt contains things the user did not ask for"
property, and it fights auto-compact — injected context that the model did not request is exactly
what compaction throws away first.

---

## CC-7 — Edits are Claude Code's, applied by Claude Code

The `🟧🟧🟧 EDIT` / `🟨🟨🟨 REPL` / `🟩🟩🟩 END` protocol, the anchored-match pipeline, its five
failure classifications, and the three auto-populated retry prompts are all deleted. Claude Code's
own `Edit` / `Write` / `NotebookEdit` tools apply changes.

AC⚡DC's role at edit time becomes:

1. **Before** — `can_use_tool` intercepts the call and renders the proposed change as a Monaco
   diff in the permission dialog. Allow / deny / allow-and-remember.
2. **After** — a `PostToolUse` hook observes the write, broadcasts `filesModified`, re-indexes the
   touched files, and refreshes the diff viewer. (This line named `filesChanged` alongside it until
   [CC-21](#cc-21); that was the selection broadcast, a different signal that no longer exists.)

**Why it matters:** anchored-match edits fail in ways that need retry prompts *because* the model
was working from a structural map rather than the file. An agent that just read the file does not
need an ambiguous-anchor recovery protocol. Keeping our pipeline would mean re-implementing it as
an MCP tool and asking the model to prefer it over its native tools — fighting the grain for no
user-visible gain.

**Consequence:** `3-llm/edit-protocol.md` is deleted. Its user-visible replacement is the
permission diff, specified in [`../3-engine/permissions.md`](../3-engine/permissions.md).

**Kept from the old design:** file checkpointing. `enable_file_checkpointing=True` plus
`rewind_files()` gives an undo that the anchored pipeline never had. ~~Kept~~ — **withdrawn by
[CC-20](#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)**: the SDK will not enable
checkpointing in a session that mirrors its transcript, and the mirror is the one phase 5 needs.

---

## CC-8 — Subagents are Claude Code's `Task` tool, not AC⚡DC's spawn blocks

The agent-mode design (`🟧🟧🟧 AGENT` blocks, `filter_dispatchable_agents`, per-agent
`ContextManager`s, `agent_idx` archive routing, cross-turn reconstruction, the `agents.enabled`
config gate) is deleted. Subagents come from Claude Code's `Task` tool and are surfaced through
`TaskStartedMessage` / `TaskProgressMessage` / `TaskUpdatedMessage` / `TaskNotificationMessage` —
all four are `SystemMessage` **subclasses**, dispatched on `subtype` — plus the module-level
`list_subagents(session_id, directory=None)`, which is not a client method.

**Why it matters:** the entire `agent_idx`-vs-`id` two-namespace problem, the cross-turn
reconstruction algorithm, and the mode-replay-from-archive machinery exist to solve a problem the
SDK solves with a stable `agent_id` and a transcript per subagent. The tab UI survives; only its
data source changes.

**Consequence:** `5-webapp/agent-browser.md` becomes `subagent-browser.md`, keyed on SDK agent
IDs. `7-future/parallel-agents.md` is retired as implemented-by-the-platform.

---

## CC-9 — URL fetching is retired

URL detection, the fetch pipeline, the URL cache with TTL, the LiteLLM-backed summariser, and the
frontend URL chips are all deleted. Claude Code has `WebFetch` and `WebSearch`.

**Why it matters:** the URL chip UI existed to let the user curate which fetched pages entered the
prompt, because every page cost tokens on every turn. Under an agent that fetches on demand, the
curation problem disappears.

**Consequence:** `4-features/url-content.md` is deleted. The one behaviour worth preserving —
"paste a URL, get a fetch" — is now the agent's default response to a pasted URL, and needs no
UI.

---

## CC-10 — One SDK client per repo process, browser request IDs retained

The process holds a single connected `ClaudeSDKClient` with `cwd` at the repo root. Browser-side
request IDs stay as the correlation primitive for server-push events, mapped onto SDK
`session_id` and message UUIDs.

**Why it matters:** the multi-client broadcast model (collaboration, passive stream adoption,
reconnect-and-reattach) is built on request IDs and works. Replacing it with SDK session IDs would
mean rewriting the transport layer for no benefit — SDK session IDs are not per-request, so they
cannot demultiplex two concurrent turns on their own.

**Consequence:** the single-user-initiated-stream guard survives. `client.interrupt()` replaces
the cancellation flag.

---

## CC-11 — `setting_sources` includes the project, so `CLAUDE.md` is live

The session is configured with `setting_sources=["user", "project", "local"]`. The repo's
`CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`, `.claude/skills/`, and slash commands all
apply.

**Why it matters:** the alternative is an AC⚡DC session that behaves differently from the CLI in
the same repo, which makes every "does it work in Claude Code?" question ambiguous. Honouring
project settings means the user's existing configuration transfers, and it makes AC⚡DC a viewer
onto a session they could also have opened in a terminal.

**Consequence:** `1-foundation/configuration.md` shrinks dramatically. AC⚡DC's own prompt files
(`system.md`, `system_doc.md`, `review.md`, `system_reminder.md`, `compaction.md`, `commit.md`,
`system_agentic_appendix.md`) are deleted; what survives is a thin engine config plus the UI's own
settings. Prompt customisation moves to `CLAUDE.md`, where users already expect it.

---

## CC-12 — Modes become prompt presets, not engine states

Code mode / document mode / cross-reference mode currently switch which index feeds assembly,
which system prompt is installed, and which stability tracker is attached. None of those
mechanisms survive.

What survives is the user-visible intent: "I am working on documents, not code." It is
re-expressed as a **preset** — a named bundle of a snippet set, a default MCP tool hint, and
optionally a Claude Code skill or agent definition. Switching a preset does not reset context,
does not swap a system prompt, and does not invalidate a cache.

**Why it matters:** users have muscle memory for the doc-mode toggle and the doc snippets are
genuinely useful. But the toggle's implementation cost was almost entirely in the engine, and
under Claude Code a "mode" that changed the system prompt mid-session would be a context
invalidation for no reason.

**Consequence:** `3-llm/modes.md` is deleted; the surviving behaviour is a section in
`5-webapp/chat.md` plus a line in `1-foundation/configuration.md`.

---

## CC-13 — Review mode keeps its git machinery, loses its prompt swap

Code review's git state machine (clean-tree gate, merge-base computation, soft reset, branch-tip
checkout, exit sequence), the commit graph selector, and the review diff surfaces all survive
untouched — they are repository-layer behaviour, not engine behaviour.

What goes: the review system-prompt swap, the re-injected review context block, the pre-change
symbol map in the prompt, and the reverse-diff-for-selected-files section.

What replaces it: entering review mode sends one framing message to the agent describing the
review (branch, merge-base, changed files) and makes the review facts available through the MCP
bridge (`review_state`, `review_diff`). The agent reads the diffs it wants.

**Why it matters:** the review context block was a pre-computed answer to "what changed?" — a
question an agent with `Bash` and `git` can answer better, at the granularity it needs, without a
token budget cap on how many diffs fit.

---

## CC-14 — File selection becomes a hint, not a context contract

> **Partly superseded by [CC-21](#cc-21) (2026-08-20).** The hint went, and the checkbox with it —
> the paragraph below describes a channel that no longer exists. What stands is the last paragraph's
> repurposing of the third state into `Read(path)` deny rules, which is now the whole of the
> picker's per-path meaning.

The file picker survives. Its meaning changes from *"these files' full text is in the prompt"* to
*"these files are what I am pointing at"*. Selection is communicated to the agent as a list of
paths in the turn's framing, and it continues to drive the diff viewer, the navigation grid, and
the picker's own UI.

**Why it matters:** the picker is the most-used surface in the app and the three-state checkbox
(select / neutral / exclude) is load-bearing muscle memory. But "exclude from index" no longer has
a meaning — there is no prompt to exclude a file from. The third state is repurposed to
`.claude/settings.json`-style deny rules (`Read(path)` permission denial), which is the closest
honest equivalent: it stops the agent reading the file rather than stopping us from describing it.

---

## CC-15 — Permission prompts are localhost-only

`can_use_tool` resolves against the **host** browser only. Non-localhost collaborators see the
permission request and its outcome, but cannot answer it.

**Why it matters:** every mutating RPC in the system is already localhost-gated. A permission
dialog is the most consequential mutation surface in the new design — it authorises arbitrary
`Bash`. If a remote participant could answer it, collaboration mode would become a remote-code-
execution grant, which is precisely what the existing restriction policy exists to prevent.

**Consequence:** if no localhost client is connected when a permission request arrives, the
request is denied after a timeout with a reason the transcript records. A headless AC⚡DC cannot
be driven by a remote collaborator into running commands.

---

## CC-16 — Always-allow grants persist to `localSettings`, and never to `.claude/**` **(user)**

A rule derived from an "always allow" click names `destination: "localSettings"`
(`.claude/settings.local.json`, git-ignored). `projectSettings` is not a default; it is available
only as an explicitly-labelled second entry in the rule menu that says the grant will be committed.
Separately, no derived rule may ever name a path under `.claude/`.

**Why it matters:** three reasons, in order of weight.

1. **It is where the CLI puts its own grants.** Observed against CLI 2.1.229 and recorded in
   [`../../specs-reference/3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md):
   a persisted rule goes to `localSettings`. With AC⚡DC defaulting to `projectSettings`, the same
   approval landed in a different file depending on which front end the user happened to be in, so
   "what have I allowed in this repo?" had two answers and only one of them was the CLI's.
2. **A click must not become a commit.** `.claude/settings.json` is git-tracked. AC⚡DC is a
   multi-client system (CC-15, [`../4-features/collaboration.md`](../4-features/collaboration.md)):
   one participant's trust decision would have become the team's checked-in policy, shared by the
   next `git push`. A shared allowlist is a policy, and a policy deserves a reviewed edit rather
   than a button. The dialog copy never said "and share this with your team", and a grant wider
   than its label is the defect class the six always-allow fixes were about.
3. **`.claude/**` is a permission to grant permissions.** Approving one write to
   `.claude/settings.json` would otherwise derive `Edit(.claude/settings.json)`, after which the
   agent can write `Bash(*): allow` into its own gate and the dialog never opens again. The
   exclusion is unconditional and does not depend on which destination is chosen, because the
   escalation is in the *path*, not the file the rule lives in.

**Consequence:** `derive_suggested_rules` returns no rule at all for a write under `.claude/`, so
such a call is approvable once but never permanently. `permissions.py` continues to honour an
explicit `destination` on a CLI suggestion; the change is to what AC⚡DC's own derivation defaults.

## CC-17 — The HUD and Context tab are replaced in phase 3, not vacated **(user)**

`token-hud.js` and `context-tab.js` are deleted by the rip-out and immediately replaced with
minimal panels over Claude Code's own numbers. The designed visualisation remains phase 6.

**Why it matters:** both files are fed by `get_context_breakdown` and are written in tier
vocabulary — the RPC and the concept both go with the engine. Leaving them mounted past phase 3
would leave two panels that render nothing, or worse, a last-known number that never updates
again; and a permanently-empty panel is indistinguishable to a user from a broken one. Deleting
them without replacement instead leaves the product with no answer to "how full is the context and
what did that turn cost" for a whole phase.

The replacement is small because phase 2 already shipped the data path: the result event carries
`num_turns`, `usage`, `model_usage` and `total_cost_usd`, the service refreshes `context_usage`
after every turn, and `get_context_usage` exists as an RPC. What is missing is only rendering.

**Consequence:** phase 3 adds a HUD strip (context percentage, per-turn cost, active model) and a
Context tab that lists what `get_context_usage()` reports. Phase 6 upgrades them into the
visualisation [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md) describes,
starting from a working panel and a gate that can already be checked against `/context`.

---

## CC-18 — Index freshness after `Bash` is an open choice, and the record must not pre-empt it

Phase 4's `PostToolUse` re-index watches `Write`, `Edit`, `MultiEdit` and `NotebookEdit`. A `sed -i`,
a `git checkout`, a formatter or an `npm install` run through `Bash` changes files no index hears
about until the next full build ([`delivery.md`](delivery.md#deviations-from-inventorymd-1) calls this
phase 4's largest known hole). **How that is closed is not decided.** The candidates:

| Option | Cost |
|---|---|
| Filesystem watcher | A new subsystem: cross-platform behaviour, debouncing, gitignore-awareness, and a thrash risk on build directories. |
| Parse `Bash` input for touched paths | Rejected at implementation time — tool input is not reliably parseable into "which files did this touch". |
| Re-index after every `Bash` call | Re-indexes after every `ls`. |
| Nothing, stated in the spec | Honest and free; leaves the agent's own `Read` as the only fresh view. |

"Nothing, documented" is a legitimate outcome, which is precisely why it must be chosen rather than
inherited. The implementation is its own phase (phase 8) and is sequenced *after* phase 5: it does not
block history, and phase 5 is what reveals whether the durable record wants a watcher at all.

**What is binding now is the naming.** Both candidate sources for a "what changed this turn" record
share the `Bash` blind spot — `take_reindexed()` because the hook never fires, and
`result['files_modified']` because `messages.py:62` gates it on the same four-tool
`_FILE_WRITING_TOOLS` map, its own docstring calling input-attribution "a stopgap". So any persisted
field **must be named for what it actually contains** (`files_written_by_file_tools` or equivalent),
never `files_changed`.

**Why it matters:** a wrong live broadcast dies at reload. A wrong field written into `.ac-dc4/` is
what the history browser and full-text search then show, permanently, and correcting it means
migrating transcripts users have already accumulated. The cheap moment to be accurate about scope is
before the first one is written — phase 5, before CC-18's implementation exists.

---

## CC-19 — One store, entries verbatim; `history_store.py` retires **(user)**

Phase 5 implements the SDK's `SessionStore` protocol fresh, under `.ac-dc4/`, against
`claude_agent_sdk.testing.session_store_conformance`. **`history_store.py` and
`test_history_store.py` are deleted with it.** There is no second writer: the history browser and
full-text search become readers of the mirrored transcript, through the SDK's `*_from_store`
parsers (`get_session_messages_from_store` and friends), not of records of our own shape.

Entries are stored **verbatim** — the pass-through contract read literally, base64 image payloads
included.

**This supersedes the first half of [CC-3](#cc-3).**
CC-3's split of the two jobs stands, and is still the right split: *display and search* are ours,
*context continuity* is the SDK's, because the SDK owns compaction. What does not stand is CC-3's
sentence that AC⚡DC "keeps `HistoryStore`" alongside its `SessionStore` implementation. That was
written before the protocol was read.

**Why:** `SessionStoreEntry` is documented in the installed SDK (0.2.137) as *"a minimal structural
supertype — adapters should treat entries as pass-through blobs; round-tripping
`json.dumps`/`json.loads` is the only required invariant"*. The concrete shape is the CLI's own
transcript union, which is internal. Three things follow:

1. **A store cannot impose a record shape**, so `history_store.py`'s schema is not a head start on
   one. Of its `append` fields, `system_event` and `turn_id` are what the phase-3 deviations need,
   while `edit_results` and `agent_blocks` describe an edit protocol and a spawn protocol that were
   deleted in phase 3.
2. **The browser must not read raw entries.** An internal discriminated union is exactly the kind of
   thing that changes under us; the SDK ships parsers for it and they are the supported reader.
3. **Two writers means drift.** One append per message sounded cheap in CC-3. The real cost is two
   sources that can disagree about what a session contains, and a reload is where the disagreement
   surfaces.

Nothing is being migrated: no `.ac-dc4/history.jsonl` exists in practice, and
`import_session_to_store()` is the SDK's own path for replaying a local transcript if one ever needs
adopting.

### What replaces the second store's three jobs **(user)**

The spec `3-engine/history.md` gave the rendered mirror three jobs: summarised browse records,
request-ID correlation, and full-text search that excludes tool results. They are re-homed rather than
dropped, and neither home is a second transcript:

| Job | Home | Property that makes it safe |
|---|---|---|
| Search, session list, request-ID ↔ session correlation | A **derived index** under `.ac-dc4/`, built from the store | Deletable at any time and rebuilt from the transcript. It can go stale; it cannot disagree, because it has no independent content. |
| Browse rendering, tool-call summaries | Produced at read time from parsed `SessionMessage`s | Summarising at write time is denormalisation that drifts from the transcript it summarises. |
| AC⚡DC's own system events — commit, reset, review entry and exit, preset switch, permission-mode change | `.ac-dc4/events.jsonl`: ours, append-only, keyed by session ID and request ID, carrying no message content | Not derivable from the transcript, so it cannot live in the index; not the CLI's, so it must not live in the store. |

**The store is never given an entry the CLI did not write.** This is the invariant that keeps the
single-store design safe. Injecting our own records — namespaced `type` or not — puts them in front of
the CLI during resume materialisation, where `load()` hands the store's contents back to a subprocess
that parses its own union. A resume that fails because of a record we invented would present as context
loss, arbitrarily later, in a session the user cares about.

**Consequences:**

- **Implement all six protocol methods**, not the two that are required. The SDK probes for
  `list_sessions`, `list_session_summaries`, `delete` and `list_subkeys` by attribute presence, so a
  missing one degrades a feature *silently* — the failure mode is a browser that lists nothing and
  reports no error.
- **Pasted-image extraction to `.ac-dc4/images/` retires with the store.** Images are live — the
  browser pastes data URIs and `session.py:212` turns them into content blocks — so they arrive as
  base64 inside opaque entries and stay there. A session with several pasted screenshots produces a
  multi-MB JSONL. **The revisit trigger is measured size, not distaste**; if it fires, the mechanism
  is extraction on `append` with rehydration on `load`, which preserves the round-trip invariant, and
  it is a change to the store alone.
- **The history browser loses the metadata it renders today.** `files_modified`, `edit_results` and
  the "show agents" affordance had `HistoryStore` records behind them. What the transcript can support
  is re-derived from it; what it cannot is dropped rather than faked — and anything persisted about
  files written obeys CC-18's naming rule.
- **Pre-ack durability narrows, and the spec must say so.** `3-engine/history.md` asserted that every
  message is durably appended *before* the turn that produced it is acknowledged. With one store that
  becomes: appended during the turn, at the eager flush's ~100 ms cadence, after the CLI's own local
  write. The window where a crash loses the user's typed text is small but real, and the browser's
  `input-history.js` is what covers it — not the store.
- **Fifteen spec files describe the two-store design** and are corrected with this decision. Eight were
  obvious: `3-engine/history.md` (the design itself), `0-overview/glossary.md`,
  `0-overview/implementation-guide.md`, `1-foundation/configuration.md`, `3-engine/tool-surface.md`,
  `4-features/images.md`, `6-deployment/packaging.md`, and the schema twin in
  `specs-reference/3-engine/history.md`. Seven more turned up in a grep sweep for the phrase *mirrored
  store*, which had come to mean two different files in two different documents:
  `0-overview/architecture.md` (the `.ac-dc4/` table, and a turn-flow step claiming the server persists
  the user message itself), `3-engine/session.md` (the same step), `1-foundation/rpc-inventory.md`,
  `5-webapp/chat.md`, `plan/inventory.md` (`history_store.py` was still filed under ADAPT),
  `specs-reference/1-foundation/rpc-inventory.md` and `specs-reference/5-webapp/chat.md` (both pointing
  at a section this decision deletes). The lesson is in the phrase: "mirrored store" was ambiguous
  *before* this decision, and only reads as one thing now.
- **`session_store_flush: "eager"`** is already set when a store is present
  (`claude_code/options.py:183`), which is what makes the mirror near-real-time rather than a turn
  behind.

### What one store does to the RPC surface

Three consequences fell out of writing the schema twin, none of them decided above. Recorded here rather
than left implicit in a table:

- **`list_engine_sessions` and `delete_engine_session` are deleted.** They listed and deleted the store;
  `history_list_sessions` and a browser-side delete covered the browsable records. With one store each
  pair is two names for one operation, and two listings that can disagree about which sessions exist is
  the exact failure this decision removes.
- **The surviving set is `history_list`, `history_load`, `history_search`, `history_delete`.** The first
  three of those names are not new: phase 1 chose them, the delivery log has carried them since, and
  `test_phase_five_methods_are_absent` asserts them absent to this day. `history_search` keeps its
  native name because there was nothing wrong with it. Renaming rather than reusing the native names is
  the loud option and the right one — every payload changed (`engine_session` is gone, `message_id`
  becomes `entry_uuid`), so a browser still calling `history_list_sessions` should fail at the call site
  rather than parse a shape it no longer understands.
- **`history_image(session_id, entry_uuid, block)` is new**, and is required rather than convenient:
  `4-features/images.md` has the `userMessage` broadcast carry a pointer instead of bytes, which is only
  viable if something can turn a pointer back into bytes on demand.

---

## CC-20 — The mirror wins over file checkpointing; undo is git's job

`session_store` and `enable_file_checkpointing` cannot both be set. The SDK validates the pair in
`ClaudeSDKClient.connect()` *and* in `query()`, and raises:

> `session_store cannot be combined with enable_file_checkpointing (checkpoints are local-disk only and
> would diverge from the mirrored transcript)`

A session that asks for both therefore does not start at all — the user sees "Could not start a Claude
Code session" and has no engine, no history, and no way to ask for either. AC⚡DC set both from phase 1
onward and got away with it because nothing *constructed* a store until CC-19's implementation landed;
the constraint fired on the first run that had one. `claude_code/options.py` now sets checkpointing and
its `--replay-user-messages` partner only when there is no store, which in practice means only a
repoless run — every run with a repo mirrors.

**Why the mirror rather than the undo:** the store carries `.ac-dc4/` history, resume after a restart,
the session browser and the derived index — it *is* CC-19 and most of phase 5. Without it a session
outlives only the CLI's own retention window, so the loss surfaces days later looking like data loss.
Checkpointing carries one control that has never had a caller (`delivery.md` § No rewind UI), over
changes git already tracks and a working tree the user can already diff.

**Consequences:**

- **`rewind_files()` is refused at the RPC**, not attempted and translated. The SDK's own answer is a
  `ValueError` about local-disk divergence, which tells a user nothing about what to do instead; the
  refusal names git.
- **[CC-7](#cc-7--edits-are-claude-codes-applied-by-claude-code)'s closing line is withdrawn.** The undo
  the anchored pipeline never had is not this one either.
- **`5-webapp/chat.md`'s "Undo file changes" action is off the table while the mirror is on**, which is
  every run with a repo. It was never built, so nothing is removed — but the spec must stop promising it.
- **The revisit trigger is the SDK, not our taste.** One test asserts the constraint still exists
  (`test_the_sdk_still_refuses_the_pair`). When the SDK learns to checkpoint alongside a store, that test
  fails, and undo comes back by deleting a branch. Recorded in `7-future/README.md`.

---

## CC-21 — The selection hint goes, the checkbox with it **(user)**

The picker's checkbox column is deleted, and with it every channel the selection travelled down:
the frontend's `_selectedFiles` state and its per-tab Map, the `set_selected_files` /
`get_selected_files` / `set_agent_selected_files` RPCs, the `filesChanged` broadcast, `Turn.files`,
the framing branch that listed selected paths, the `selected_files` key in the `ui_state` snapshot
and in `mcp__ac-dc__ui_state`'s rendered block, the `files` key on the user-message history entry,
and review entry's `on_selection_cleared` callback. `chat_streaming` loses its `files` parameter and
takes four arguments.

This supersedes the first half of [CC-14](#cc-14--file-selection-becomes-a-hint-not-a-context-contract).
The second half — the third state repurposed as `Read(path)` deny rules — is kept and is now the
whole of what the picker's per-path state means.

**Why it matters:** the app had two ways to point at a file and only one of them did anything. `@path`
in the prompt is the CLI's own mechanism: the CLI expands it, the agent reads the file, and the user
can see in their own message what they asked for. The checkbox produced a paragraph of framing that
said "here is a hint" and then relied on the model to act on it — so a user who ticked three boxes and
asked "fix the bug" had no way to tell whether the files had been read, and the honest answer was
usually not. Muscle memory was the argument for keeping it (CC-14), but muscle memory for a control
that mostly does nothing is a liability, not an asset: it teaches users that pointing is free and
silent when the real mechanism is neither.

**What survives, deliberately:**

- **Deny-read**, moved off the checkbox onto the row: `shift`+click toggles the `Read` rule, and the
  context menu carries both verbs. This is a real permission written to
  `.claude/settings.local.json`, which the CLI reads — the one per-path control that changes agent
  behaviour rather than suggesting it.
- **Viewer framing** in `build_framing()`. What the user is *looking at* is a fact they could not
  reasonably type, and it changes without them acting. That is the test framing now has to pass:
  facts the user could not have typed themselves.
- **The `@`-filter bridge** in the chat input, which is how a typed `@` finds a path at all.
- **Path insertion**, promoted rather than merely kept, because it becomes the only picker→prompt
  route. Middle-click inserts the bare path; `ctrl`+middle-click inserts `@path`; both are also
  context-menu items ("Insert path in prompt", "Insert @path — agent reads it"). **(user)** chose
  both forms on distinct gestures, and chose the context-menu item as the discovery path. The
  mention modifier was `shift` as first shipped; see the amendment below.

**Consequences:**

- **`shift` meant two things on one row** as first shipped, split by mouse button: with the left
  button it denied, with the middle button it inserted a mention. Accepted with a mitigation rather
  than resolved, on the reasoning that the alternative was a second modifier and `ctrl`/`cmd`+click
  belongs to the browser. **(user)** amended this on 2026-08-21: the mention modifier is `ctrl`+middle-click.
  The browser's claim on `ctrl` is on the *primary* button, so the middle button was free all along, and
  `shift` now means deny-read and only that whichever button it is held with. `shift`+middle-click
  inserts the bare path — it is not a distinct gesture. All four verbs stay context-menu items, so no
  gesture is the sole route to anything.
- **The insertion is additive, and padded on both sides.** It lands at the cursor (standing in for an
  active selection) with a space either side unless whitespace is already there, and never replaces
  what the user has written. Same amendment: a path arriving mid-sentence should read as a word in the
  prompt, and a half-composed prompt is not the picker's to discard.
- **Nothing about the picker crosses the collaboration boundary any more.** There is no shared
  selection to keep in agreement, so `4-features/collaboration.md`'s File Selection Sync section
  describes a channel that no longer exists. Deny rules are host-written and localhost-gated
  (CC-15); collaborators read them from the state snapshot's `denied_read_files`.
- **Review entry no longer clears anything.** It cleared the selection because the selection
  described the branch you had just left; the deny list is about paths, which the checkout does not
  invalidate.
- **The `@`-expansion is the CLI's, so we cannot report on it.** No UI can say "these three files
  were read" — the read shows up as `Read` tool calls in the transcript like any other. That is a
  loss of a promise we were never keeping.
