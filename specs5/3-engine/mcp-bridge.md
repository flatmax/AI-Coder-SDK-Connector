# MCP Bridge

AC⚡DC exposes its repo intelligence to Claude Code as an in-process MCP server named `ac-dc`. This
is how the tree-sitter symbol index, the document index, and the reference graph survive the
conversion with a purpose beyond serving Monaco.

The server is in-process — an SDK MCP server, not a subprocess — so it shares the indexes the browser
already queries. No separate build, no IPC, no second copy of the index in memory.

## Why Tools, Not Prompt Injection

The native engine pre-injected a whole-repo structural map into every prompt and then built a
four-tier cache to make that affordable. The map was in the prompt whether or not the turn needed it.

Tools invert the economics: the agent pays for structure only when it asks for structure, and Claude
Code's own cache handles repeats. Three further advantages, each of which independently justifies the
choice:

- **Auditability.** A tool call appears in the transcript. Injected context does not, so the user
  cannot tell why the model believed something.
- **Compaction compatibility.** Injected context the model never requested is the first thing
  compaction discards — so injection quietly stops working exactly when the conversation gets long
  enough to need it.
- **Honesty about the contribution.** If the map is worth having, an agent will call the tool. If no
  agent ever calls it, we have learned something we could not learn while forcing it into every
  prompt.

The rejected alternative — a `UserPromptSubmit` hook injecting the map on the first turn — was cheaper
to build and fails on all three counts.

## Tool Budget

Every tool definition costs context on every turn, and appears in the Context tab's tool inventory
alongside its cost (see [context-visibility.md](context-visibility.md)). The bridge is therefore
deliberately small: **six tools**, each answering a question Claude Code's built-ins answer badly or
not at all.

The test for adding a tool is not "would this be useful?" but "is this cheaper or better than the
agent doing it with `Glob`, `Grep`, `Read`, and `Bash`?" A tool that wraps `grep` fails that test. A
tool that returns the structure of 900 files in one response passes it.

## Tools

### `symbol_map`

Whole-repo or subtree structural map in the compact format: per-file classes, functions, methods,
imports, with the legend needed to decode it.

*Answers:* "what is the shape of this codebase?" — in one call, for a few hundred to a few thousand
tokens, instead of a directory walk plus dozens of reads.

Arguments: an optional path prefix to scope to a subtree, and an optional language filter. Chunked
output with a continuation token when the map exceeds a reasonable response size, because a 900-file
monorepo map is not a single response.

This is the bridge's flagship tool and the clearest case where AC⚡DC knows something the agent cannot
cheaply discover.

### `file_symbols`

The structural block for specific files: symbols with line numbers, imports, and incoming reference
counts.

*Answers:* "what is in this file?" without reading it. Materially cheaper than `Read` for a large file
when the agent needs orientation rather than content, and it is the natural follow-up to a
`symbol_map` call.

### `find_references`

Where a symbol is used, from the reference graph: definition site plus call and import sites, with
paths and line numbers.

*Answers:* "what breaks if I change this?" `Grep` approximates this with text matching, which
over-matches on common names and under-matches on aliased imports. The reference graph resolves
imports properly. This is the same data that powers Monaco's find-references, which is a useful
consistency property: the agent and the user see the same answer.

### `doc_outline`

Document structure for markdown and SVG: headings with line numbers, extracted keywords, content-type
markers, cross-references, and — for SVG — the containment hierarchy and box labels.

*Answers:* "what is in this document set, and how do the documents link to each other?" There is no
built-in equivalent at all. SVG in particular is otherwise opaque to an agent: `Read` on an SVG
returns coordinate soup, while the index returns a labelled nesting structure. See
[`../2-indexing/document-index.md`](../2-indexing/document-index.md).

### `review_state`

Active review facts: reviewed branch, target branch, merge-base, changed files with status.

*Answers:* "what am I reviewing?" The agent can derive most of this with `git`, but not the fact that
the repository is in AC⚡DC's soft-reset review state — which changes what `git status` means. Without
this tool the agent will misinterpret the staged-changes-as-branch-changes arrangement. Returns an
explicit not-in-review result when review is inactive. See
[`../4-features/code-review.md`](../4-features/code-review.md).

### `ui_state`

What the user is looking at: selected files, the file open in the viewer, the cursor or selection
range, the active preset.

*Answers:* "what is the user pointing at?" — unanswerable by any built-in tool, because it is browser
state. Turn framing carries a snapshot of this at turn start (see
[session.md § Turn framing](session.md#turn-framing)); the tool lets the agent re-read it mid-turn,
which matters for long turns where the user has navigated since.

## What the Bridge Deliberately Does Not Provide

- **File reading or writing.** `Read`, `Edit`, and `Write` exist, are checkpointed, and are covered by
  the permission dialog. A parallel write path would bypass both.
- **Search.** `Grep` and `Glob` are good, and are backed by ripgrep.
- **Git operations.** `Bash` with `git` is more flexible than any wrapper we would write, and the
  agent already knows git.
- **An edit tool using the old anchored protocol.** Considered and rejected: it would compete with
  the agent's native, checkpointed tools and would require prompting the model to prefer ours. See
  [decisions § CC-7](../plan/decisions.md#cc-7--edits-are-claude-codes-applied-by-claude-code).
- **URL fetching.** `WebFetch` and `WebSearch` exist. See
  [decisions § CC-9](../plan/decisions.md#cc-9--url-fetching-is-retired).

## Freshness

Index-reading tools must never report state older than the most recent completed file-mutating tool
call. An agent misled by our own tool about code it just wrote is worse off than an agent with no tool
at all — it will confidently reason from a stale map.

The mechanism: incremental re-indexing is debounced on `PostToolUse`, and a pending re-index is
flushed synchronously before any `ac-dc` index-reading tool returns. See
[tool-surface.md § Snapshot discipline](tool-surface.md#snapshot-discipline-moves-to-tool-call-boundaries).

## Availability and Degradation

- The server starts with the session. If it fails to start, the session continues without it and a
  banner reports the loss — otherwise the agent simply appears inexplicably worse at repo-wide
  questions.
- **The loss is a sentence, and the engine writes it.** `EngineHealth.degradations` carries one per
  capability the session started without — the bridge and the post-write re-index hook are separate
  losses with separate remedies — and the chat panel's health banner renders them as it renders the
  version and credential warnings. Sentences rather than flags for the reason the disk warning is a
  sentence: whoever knows what was lost also knows what the agent will do instead, and a browser
  turning a flag into prose would be a second owner of the meaning. Deduplicated on the text, because
  this is a standing condition re-sent on every health push rather than an event.
- Index build is deferred at startup. Tools called before their index is ready return an explicit
  "index still building, retry shortly" result rather than an empty one. An empty result reads as "the
  repo has no symbols", which sends the agent down a wrong path it will not revisit.
- **A build that failed says so, and does not say "retry".** Three states — absent, building, built —
  plus a failure flag, because "wait a moment" and "this will never work, use `Grep`" are
  indistinguishable through a single ready boolean, and an agent that retries a permanent failure
  spends turns on it.
- **A partially built index answers Monaco and not the map tools.** The LSP paths
  (`lsp_get_hover`, `lsp_get_definition`, `lsp_get_references`) read the index object directly, so a
  hover resolves as soon as the file it names has been walked. The map tools wait for the whole repo:
  a hover that works for half the repo is useful, a map that covers half the repo is a lie with no
  marker on it.
- Tools are read-only and are displayed but not gated — but that posture is **implemented by an
  explicit allow in `can_use_tool`**, not inherited from being read-only. The CLI asks about MCP
  tools. See [permissions.md](permissions.md) for why the distinction matters.
- Server health, and the `ac-dc` tool inventory with its token cost, appear in the Context tab like
  any third-party server.

## Invariants

- The bridge exposes exactly the tools listed here; adding one requires justifying it against the
  built-in it replaces, because every definition costs context on every turn.
- Every bridge tool is read-only; none mutates repository state, engine state, or UI state.
- An index-reading tool never returns data older than the most recently completed file-mutating tool
  call.
- A tool whose index is not yet built returns an explicit not-ready result, never an empty result.
- `review_state` returns an explicit not-in-review result rather than fabricating review facts.
- Bridge failure degrades the session's capability, never its availability.
- The bridge and the browser read the same index instances; a symbol resolvable in Monaco is
  resolvable by the agent, and vice versa.
