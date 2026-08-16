# Conversion Plan — Native Engine → Claude Code Frontend

**Status:** Active. This directory is the plan of record for converting AC⚡DC from its own
LiteLLM-based context engine to a frontend for Claude Code (via the Claude Agent SDK).

The rest of `specs5/` describes the **target** state. This directory describes **how we get
there** and **why the shape is what it is**. When the conversion is finished, `plan/` becomes
history and moves under `specs5/impl-history/`.

## Where we are (2026-08-16)

**Phases 0 through 5 are done. Phase 6 — the designed visualisation — is next.** A conversation now
outlives the process: `RepoSessionStore` mirrors the CLI's transcript under `.ac-dc4/sessions/`,
`run_session_store_conformance` passes with nothing waived, and a server that comes back up reattaches
to the session it was in. **Resumption is the SDK's rebuild, never our replay** — we render a *record*
of a session for the human and hand the same session ID to the CLI, and the two views cannot drift
because neither is derived from the other.

**Phase 5 was also where three specified-but-undelivered warnings got a reader**: the mirror gap, at
both of its scales — a marker on the turn that was not written and a running count for the session —
the 1 GiB disk warning, and the health banner that four specs routed to and nothing rendered. The
`history` section of `app.json` exists now too, so the two thresholds the mirror is judged by are the
user's rather than a module constant's. What phase 6 inherits is under *What phase 6 will find already
there*.

**Phase 5 shipped with no live CLI run, and both interludes after it are what that cost.** The first
found an engine that could not connect at all in any repo. The second ran the exit criterion properly —
a restarted server, driven from outside by a second agent, because nothing can verify its own shutdown —
and it passes: `--resume` on the child, the earlier conversation answered with no tool calls, no fork in
the transcript, a clean SIGINT. It also found that **every row in the session list read as the same 100
characters** of our own framing prose, which 2 915 green tests had agreed with. Three phases running,
green numbers have met a live CLI and lost.

Read [`delivery.md`](delivery.md) before touching anything: it records what each finished phase
landed, what it deliberately left out, and what the next phase has to do first. The phase-5 entry and
the two interludes after it are what matter for picking this up cold.

**The native engine is gone.** `llm_service.py`, `src/ac_dc/llm/`, the four-tier cache and its
membrane, the context manager, the stability tracker, the token counter, the edit protocol and its
pipeline, the history compactor, URL fetching and the `🟧🟧🟧 AGENT` factory: 37 modules, 25,371
lines, plus 52 test files and five dependencies (`litellm`, `tiktoken`, `boto3`, `tenacity`,
`trafilatura`). `grep -rn -i litellm src/` and the same over `webapp/src/` both return nothing.

**The indexes are back, as tools rather than as prompt text** (CC-6). An in-process MCP server named
`ac-dc` exposes six read-only tools — `symbol_map`, `file_symbols`, `find_references`, `doc_outline`,
`review_state`, `ui_state` — sharing the browser's own index objects. A `PostToolUse` hook re-indexes
what the agent writes, and every index-reading tool flushes that queue before it answers, so a file
written this turn is a file the map describes. Verified live: the agent answered a "which module holds
the permission gate" question from `symbol_map` alone, summarised `specs5/plan/` from `doc_outline`
without opening a file, and read back a function it had just written.

The state phase 6 inherits:

- **Suites are green:** python **2922 passed, 75 skipped**; webapp **92 files / 3526 passed**.
- **`Reindexer` is the only thing that knows what the agent wrote.** `take_reindexed()` is
  repo-relative and filtered to files an index cares about; `result['files_modified']` is absolute and
  everything. If the transcript wants a durable "files changed this turn", those are the two sources,
  and they disagree by design.
- **Our own MCP tools are ungated in `can_use_tool`**, by an early return before any dialog is built.
  `classify_tool` returning `"read"` was never enough — it shapes a dialog, it does not skip one — and
  in `acceptEdits` the agent stalled on a prompt for every `symbol_map` call.
- **Four features moved out of the engine rather than dying with it**: commit
  (`claude_code/commit.py`), review (`claude_code/review.py`), the post-write doc-index builder
  (`doc_index/background.py`), and the LSP / snippets / git RPC surface, which folded into
  `claude_code/service.py`.
- **Two panels were replaced, not vacated** — [`decisions.md#cc-17`](decisions.md).
  `context-usage-tab.js` and `usage-hud.js` read `ClaudeCodeService.get_context_usage`, a pass-through
  of the breakdown the CLI's own `/context` prints, and the shell's capacity bar is a third reader of
  the same RPC. **All three now share one derivation** in `context-usage.js` (242 lines): each had
  derived the arithmetic independently and each was wrong on its own terms. The identities that make it
  checkable — categories sum to `totalTokens`, `maxTokens` is *not* pre-reduced by the autocompact
  buffer — are recorded in the interlude entry, and `partitionCategories` verifies the sum rather than
  trusting its own name matching.
- **A guarded fetch that loses its reply stays locked out for the session.** The `if (inFlight)
  return;` idiom nearly every fetch uses clears its flag in a `finally`, and a jrpc-oo call dropped
  during socket replacement never settles. `withRpcTimeout` in `rpc.js` is opt-in — some calls
  legitimately run for minutes — so **any new fetch has to opt in**, with the deadline set *above* the
  backend method's own, never under it. The three `get_context_usage` callers and the history browser's
  fetches are bounded; the rest of the webapp's guarded fetches are not. `get_context_usage` measured
  3–5 s warm and 14 s cold, which is the scale to expect from a control request to the CLI.
- **A revealed tab is told it is on screen.** `_switchTab` notifies the arriving tab's
  `onTabVisible`; the panels refuse to refetch while hidden and mark themselves stale instead. A
  session load is the second staleness path and reuses this contract rather than inventing one.
- **The file picker's third checkbox state writes a real `Read` deny rule** to
  `.claude/settings.local.json` (CC-14), and says "deny agent read" rather than "exclude from
  index". The L0-invalidation dialog is gone with the cache it asked about; its one honest job —
  the change is not instant — is a once-per-session toast built from the RPC's own `takes_effect`.
- **The two things that waited on the post-tool-call hook are closed.** The file tree refreshes after
  the agent writes (`filesModified`, session-wide), and the doc index learns about those writes:
  `DocIndexBuilder.note_file_written` now has two callers, `Repo.write_file` for the user's edits and
  the `PostToolUse` re-index for the agent's. **What still escapes is `Bash`** — a `sed -i` or a
  `git checkout` changes files no index hears about until the next full build. Phase 4's largest known
  hole; see [`delivery.md`](delivery.md#deviations-from-inventorymd-1). It is now phase 8 with the
  choice left open ([`decisions.md#cc-18`](decisions.md)). The naming half is settled and on disk: the
  persisted event is `files_written_by_file_tools`, and no field may call itself `files_changed`.
- **Some surfaces are mounted and inert, deliberately.** The code/doc mode toggle and the agent tab
  strip have no emitter for the pushes that drive them; their replacements are the preset selector
  (CC-12) and the subagent browser (CC-8), both deferred by decision. They are annotated where they
  sit rather than half-deleted, because removing a receiver while leaving its consumer mounted moves
  the break instead of fixing it. `<ac-history-browser>` is no longer among them: phase 5 re-pointed it
  at the seven `history_*` RPCs and gave it a way in from the chat panel.
- **17 RPCs are localhost-gated, and four do not look it.** `commit_all`, `reset_to_head`,
  `start_review` and `end_review` delegate, so their `_check_localhost_only()` lives in
  `claude_code/commit.py` and `claude_code/review.py`, not in `service.py`.
- **`collab.py`'s `ContextVar` fix survived the deletion**, as phase 2 required.
  `TestGateUnderRealDispatch` in `test_collab_restrictions.py` is what pins it; that file lost half
  its cases with `LLMService`, and those five tests are the ones that must not go.
- **Nothing in the config layer writes `os.environ`.** The `claude` CLI resolves its own
  credentials; injecting a key or a region would silently change which account a turn bills to.

## What phase 6 will find already there

Phase 6 is the *designed* visualisation, and the correctness rescue is already done — the interlude
found and fixed five wrong readings on screen before phase 5 could inherit the blame for them. What is
in the tree:

- **One derivation, checkable against the engine's own identities.** `context-usage.js` (242 lines) is
  the single owner of the arithmetic all three views share, and `partitionCategories` verifies the sum
  identity rather than trusting its name matching. An unverified payload degrades to an unsegmented bar.
  **`agents[]`'s element shape is still unverified** — the live capture returned it empty, and it is the
  only part of the payload still resting on a guess.
- **The staleness contract is `onTabVisible`.** The panels refuse to refetch while hidden — a breakdown
  costs a control request — and mark themselves stale instead. Both a turn completing and a session
  loading go through it.
- **A session load must not pop the HUD.** `session-changed` refreshes the numbers and shows nothing,
  because a HUD that appears on resume reports a turn nobody took. Auto-resume means this fires on
  every server start now, not just on a click.
- **The health payload already carries what the bridge-failure banner needs.** `EngineHealth.to_dict()`
  includes the per-server `mcp` list; the chat panel's banner deliberately leaves it out because a
  per-server status wants the Context tab's room. Read it there rather than adding a second source.
- **A browsed turn and a live turn are the same objects.** `render_messages` returns the block shapes a
  live turn produces and the browser restores them through `restoreMessage`. Anything phase 6 adds to a
  live turn's rendering has to survive arriving that way, or a resumed conversation loses the
  visualisation phase 6 exists to add.
- **Two surfaces are still mounted and inert by decision**: the code/doc mode toggle and the agent tab
  strip, whose replacements are the preset selector (CC-12) and the subagent browser (CC-8). The
  subagent *transcript* reader exists — `list_subagents` / `load_subagent` and read-only
  `historical:<agent_id>` tabs — so CC-8's browser is a listing over data that is already there.

**The mirror's own health is a verdict, not a threshold.** `mirror_gaps_escalated` is computed on
`EngineHealth` against `app.json`'s `history.mirror_gap_tolerance`. A second view of mirror health reads
that flag; it does not re-derive it from a count and a config value.

Three App Config sections `configuration.md` specifies still have no implementation: `Indexing`
(phase 4's), `Permissions` (phase 2's), and `Presets` (deferred by CC-12). The `history` section phase 5
added is the pattern — a callable provider so `reload_app_config()` takes, and a floor per key.

## The one-paragraph version

AC⚡DC keeps its skin and loses its brain. The browser UI, the jrpc-oo transport, the git
repository layer, the file picker, the Monaco diff viewer, the SVG editor, the document
converter, collaboration admission, and the tree-sitter indexes all survive. Everything that
existed to *assemble a prompt and pay for it efficiently* is deleted: prompt assembly, the
four-tier stability cache and its membrane/flux controller, the cache warmer, the token
counter, the emoji edit protocol, the LLM-driven history compactor, URL fetching, and the
`🟧🟧🟧 AGENT` spawn protocol. In their place sits one `ClaudeSDKClient` per repo, and
AC⚡DC's job becomes *rendering* an agent session rather than *constructing* one.

## Why

Three reasons, in order of weight:

1. **Cost.** The native engine's whole reason for existing is to make a full-repo prompt cheap
   by caching it in tiers. Claude Code sidesteps the problem instead of optimising it: it reads
   what it needs, when it needs it, with its own cache discipline, and — under a Claude
   subscription — the marginal cost of a turn is not a per-token invoice.
2. **Capability.** Claude Code ships agentic behaviour AC⚡DC does not have and would have to
   build: real tool use, bash execution, web fetch and search, subagents, skills, plugins,
   MCP clients, file checkpointing, self-compaction, and its own edit application with
   checkpoint/rewind.
3. **Maintenance.** ~28k lines of engine become a small adapter. Measured at the end of phase 3:
   **25,371 lines of engine deleted** against **6273 lines in `src/ac_dc/claude_code/`** — three
   times the "~2k" this estimate guessed, because the permission gate (1548 lines) and the message
   pump (979) are real work the estimate did not foresee. Still a 4:1 reduction, and the deleted
   code is the part of the system that was most expensive to reason about and most sensitive to
   provider behaviour changes.

## What AC⚡DC still contributes

The point of the conversion is not "be a terminal in a browser". Claude Code already has a
terminal. AC⚡DC contributes what a terminal cannot:

- **Spatial code navigation.** A Monaco diff viewer over every file the agent touches, a
  git-status file tree, an SVG editor, a TeX preview, a 2-D file-navigation grid.
- **Repo intelligence as tools.** The tree-sitter symbol index and document index survive and
  are exposed to Claude Code as MCP tools, so the agent can ask for a whole-repo structural map
  or a document outline in one cheap call instead of grepping for it. See
  [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md).
- **Permission UX with a diff in it.** `can_use_tool` becomes a browser dialog that shows the
  actual proposed edit as a rendered diff, not a `y/n` on a text hunk.
- **Context transparency.** `get_context_usage()` gives us `/context` as a live, clickable
  visualisation instead of a slash command that prints once.
- **Multi-client collaboration.** Two people watching one agent session, with admission control.
- **Documents.** Doc convert, doc mode outlines, SVG-as-document indexing — none of which
  Claude Code knows about.

## Phases

Each phase is independently shippable and leaves the tree working. Phase 0 is this pass.

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Plan and specs** ✅ | This directory + the specs5 rewrite. No code changes. | specs5 describes the target state; `plan/inventory.md` names every file to keep, delete, or add. |
| **1. Engine spike** ✅ | `src/ac_dc/claude_code/` — session, options, message pump. Registered as a second service alongside `LLMService`; not yet wired to the UI. | A CLI-side smoke test can send a prompt and print the streamed message taxonomy. |
| **2. Chat on the new engine** ✅ | Frontend chat panel renders the Claude Code message stream (text, thinking, tool-use cards, tool results, result summary). Permission dialog lands. `LLMService` still constructed but no longer reachable from the chat path. | A user can hold a full working conversation, including edits, entirely through Claude Code. |
| **3. Rip-out** ✅ | Delete `src/ac_dc/llm_service.py`, `src/ac_dc/llm/`, the cache/context/edit/compaction modules, and the frontend surfaces that fed them. Replace the HUD and Context tab with minimal panels over the SDK's own numbers rather than vacating them ([`decisions.md#cc-17`](decisions.md)). | `grep -r litellm src/` is empty; test suite green. |
| **4. Restore the indexes as tools** ✅ | In-process MCP server exposing the symbol map, doc outlines, and reference graph. Monaco LSP paths re-pointed at the surviving index. | Claude Code can call `symbol_map` / `doc_outline`; hover and go-to-definition still work in Monaco. |
| **5. History and sessions** ✅ | A fresh `SessionStore` over `.ac-dc4/` — all six protocol methods, entries verbatim, `history_store.py` retired ([`decisions.md#cc-19`](decisions.md)) — plus resume/fork, and the history browser and full-text search re-pointed at the mirrored transcript. Also the readers four specified-but-undelivered warnings never had: the mirror gap at both scales, the disk warning, and the health banner. **Shipped without a live CLI run**; the two interludes after it are what that cost, and the criterion was verified live in the second — see the [phase-5 entry](delivery.md#phase-5--history-and-sessions-2026-08-16). | Restarting the server resumes the previous conversation with context intact, and `session_store_conformance` passes. **Met live**: `--resume` on the CLI child, the earlier conversation answered with no tool calls, no fork in the transcript. |
| **6. Context and cost visualisation** ← next | Both panels exist as of phase 3 (CC-17); their tests and first live run landed ahead of phase 5, and that run spent this phase's correctness budget — the numbers now match `/context`. What is left is *upgrade*: the designed visualisation, MCP server health and the `ac-dc` tool inventory with its token cost, the bridge-failure banner (the same missing surface seen from the other side), and a "cost unknown" rendering for a turn that fails late carrying real usage, which the HUD currently declines to price rather than mispricing. | The Context tab shows the designed visualisation over those numbers, names the `ac-dc` tools it is paying for, and distinguishes a turn that cost nothing extra from one whose cost is unknown. |
| **7. Packaging** | Platform-specific wheels or an explicit external-CLI mode; the bundled CLI is ~295 MB. | A fresh machine can install and run without a manual `npm i -g @anthropic-ai/claude-code`. |
| **8. Index freshness after `Bash`** | Close phase 4's largest known hole per [`decisions.md#cc-18`](decisions.md): a filesystem watcher, or an explicit spec statement that `Bash`-driven writes are not tracked. The choice is open; "nothing, documented" is a legitimate exit. | A file changed by `sed -i` or `git checkout` is either reflected in the indexes, or its absence is stated in `2-indexing/` and surfaced to the user rather than silent. |

Phases 1–3 were the risky ones and were not interleaved: the native engine stayed intact and
reachable until phase 2's exit criterion was genuinely met, and the deletion then landed in one
commit of 189 files, **+6228 / −69527**.

Phase 3's footprint was wider than its row implied, and the grep is why we knew. `litellm` was
reachable from ten files at the end of phase 2 — `llm_service.py`, `llm/_commit.py`,
`llm/_helpers.py`, `config.py`, `main.py`, `settings.py`, `token_counter.py`, `context_manager.py`,
`history_compactor.py` and `logging_setup.py` — four of which are not obviously "engine" files. An
exit criterion written as a file list would have missed them; written as a grep, it did not.

A phase is recorded in [`delivery.md`](delivery.md) when its exit criterion is met — what landed,
what was deliberately left out, and what the next phase has to do first.

## Ordering constraints that are not obvious

- **Permissions before edits.** Do not ship phase 2 with `permission_mode:
  "bypassPermissions"` as a shortcut. The permission dialog is the feature; a build that writes
  files without asking will train users to distrust the tool, and retrofitting the dialog after
  people have muscle memory for silent edits is worse than building it first.
- **Indexes after the rip-out, not before.** *Satisfied in phase 4.* The indexes had two consumers
  (prompt assembly and the browser); deleting the prompt-assembly one first meant the MCP bridge was
  written against one clear consumer instead of two competing ones. It paid off in a way worth
  recording: the bridge takes provider *callables* rather than index objects, which only reads as
  obviously right once the browser is the sole other reader of the same objects.
- **`SessionStore` before history-browser work.** The store determines the on-disk shape; the
  browser reads it. Building the browser first bakes in assumptions about a format we have not
  chosen yet.
- **Packaging last but not never.** It is the least interesting phase and the one most likely to
  block a release. See [`risks.md`](risks.md#r-7--bundled-cli-size-and-platform-specific-wheels).

## Reading order for this directory

1. [`decisions.md`](decisions.md) — the binding choices, each with its rationale. Read this first;
   the specs assume it.
2. [`inventory.md`](inventory.md) — keep / delete / add, file by file.
3. [`sdk-surface.md`](sdk-surface.md) — verified Agent SDK API surface, and the corrections it
   forces on the origin brief.
4. [`risks.md`](risks.md) — the register, with mitigations and the tripwires that tell us a risk
   has fired.
5. [`origin-brief.md`](origin-brief.md) — the document that started this, preserved as written.
   Superseded by the above where they disagree; `sdk-surface.md` lists where.
