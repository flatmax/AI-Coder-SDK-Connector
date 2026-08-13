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
flux controller, the cache warmer, the prompt assembler, the context manager, the history
compactor, the token counter, and the emoji edit protocol.

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
`CompactBoundary` marker it emits so the user can see it happened.

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
2. **After** — a `PostToolUse` hook observes the write, broadcasts `filesChanged` /
   `filesModified`, re-indexes the touched files, and refreshes the diff viewer.

**Why it matters:** anchored-match edits fail in ways that need retry prompts *because* the model
was working from a structural map rather than the file. An agent that just read the file does not
need an ambiguous-anchor recovery protocol. Keeping our pipeline would mean re-implementing it as
an MCP tool and asking the model to prefer it over its native tools — fighting the grain for no
user-visible gain.

**Consequence:** `3-llm/edit-protocol.md` is deleted. Its user-visible replacement is the
permission diff, specified in [`../3-engine/permissions.md`](../3-engine/permissions.md).

**Kept from the old design:** file checkpointing. `enable_file_checkpointing=True` plus
`rewind_files()` gives an undo that the anchored pipeline never had.

---

## CC-8 — Subagents are Claude Code's `Task` tool, not AC⚡DC's spawn blocks

The agent-mode design (`🟧🟧🟧 AGENT` blocks, `filter_dispatchable_agents`, per-agent
`ContextManager`s, `agent_idx` archive routing, cross-turn reconstruction, the `agents.enabled`
config gate) is deleted. Subagents come from Claude Code's `Task` tool and are surfaced through
`TaskStarted` / `TaskProgress` / `TaskUpdated` / `TaskNotification` messages and
`list_subagents()`.

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
