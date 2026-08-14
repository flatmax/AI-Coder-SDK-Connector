# Conversion Plan — Native Engine → Claude Code Frontend

**Status:** Active. This directory is the plan of record for converting AC⚡DC from its own
LiteLLM-based context engine to a frontend for Claude Code (via the Claude Agent SDK).

The rest of `specs5/` describes the **target** state. This directory describes **how we get
there** and **why the shape is what it is**. When the conversion is finished, `plan/` becomes
history and moves under `specs5/impl-history/`.

## Where we are (2026-08-14)

**Phases 0, 1 and 2 are done. Phase 3 — the rip-out — is next and is unblocked.**

Read [`delivery.md`](delivery.md) before touching anything: it records what each finished phase
landed, what it deliberately left out, and what the next phase has to do first. The phase-2 entry
is the one that matters for picking this up cold.

The state phase 3 inherits:

- **The chat path runs entirely on Claude Code.** A full working conversation, including a
  permission-gated write, was verified live against the bundled CLI 2.1.229.
- **`LLMService` and `src/ac_dc/llm/` are intact and still registered, and nothing in the chat path
  reaches them.** That is deliberate, per the no-interleaving rule below. Phase 3 deletes them.
- **Suites are green:** python 3897 passed, nothing failing; webapp 89 files / 3215 passed. The one
  long-standing failure — `test_odp_routes_to_libreoffice_when_available` — was a test bug, not a
  missing install: PyMuPDF is an optional extra, and without it an `.odp` correctly falls back
  without spawning soffice, so the test's dispatch assertion failed for an unrelated reason. It now
  carries the `_require_pymupdf()` guard its `.pptx` sibling already had and skips honestly. No
  production code touched; `doc_convert/` stays on the keep-unchanged list.

**The permission gate was wrong in six ways and is fixed.** Two were recorded as open findings when
phase 2 first closed; probing the live CLI for what it actually suggests turned them into six
confirmed defects, all now corrected — see the phase-2 entry in [`delivery.md`](delivery.md). The
observed suggestion shapes are recorded in
[`../../specs-reference/3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md).
The last two of the six were a shell rule that granted more than the dialog showed (a click on
`git push origin main` authorised `git push --force origin main`, because the derived rule was the
prefix `git push:*`; the literal command is now the default and the prefix is a second entry in the
rule menu) and a transcript that rendered an approved call as denied whenever the approval came from
"always allow".

**The mode control that was open is now built.** For an in-repo edit the CLI's only suggestion is a
mode switch to `acceptEdits`, which we still refuse to put behind a button labelled "always allow
this call" — it now has its own amber control, "accept all edits for the rest of this session", that
says what it costs including the diffs it stops showing. The mode rides back to the CLI on the
permission result rather than as a separate `set_permission_mode` control request, because the CLI is
blocked waiting on that result and a second control request would deadlock on a slow user. The CLI
applies such a mode silently — `permissionMode` appears only in the `init` message — so the broker
reports the switch back through the service, which broadcasts `permissionModeChanged` and keeps the
panel's mode pill truthful.

**The open decision is closed:** derived rules named `destination: "projectSettings"`
(`.claude/settings.json`, git-tracked) where the CLI persists its own approvals to `localSettings`
(`.claude/settings.local.json`, gitignored), so an "always allow" could land a permission grant in a
committed file. `localSettings` is now the default and `projectSettings` is offered only as an
explicitly-labelled menu entry — [`decisions.md#cc-16`](decisions.md). Closing it surfaced a second
defect of the same kind: nothing stopped a derived rule from naming a path under `.claude/`, which
would have turned one approved write into `Edit(.claude/settings.json)` — a permission to grant
permissions. Derived rules now refuse that path outright.

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
3. **Maintenance.** ~28k lines of engine become ~2k lines of adapter. The deleted code is the
   part of the system that was most expensive to reason about and most sensitive to provider
   behaviour changes.

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
| **3. Rip-out** | Delete `src/ac_dc/llm_service.py`, `src/ac_dc/llm/`, the cache/context/edit/compaction modules, and the frontend surfaces that fed them. Replace the HUD and Context tab with minimal panels over the SDK's own numbers rather than vacating them ([`decisions.md#cc-17`](decisions.md)). | `grep -r litellm src/` is empty; test suite green. |
| **4. Restore the indexes as tools** | In-process MCP server exposing the symbol map, doc outlines, and reference graph. Monaco LSP paths re-pointed at the surviving index. | Claude Code can call `symbol_map` / `doc_outline`; hover and go-to-definition still work in Monaco. |
| **5. History and sessions** | `SessionStore` implementation over `.ac-dc4/`, resume/fork, history browser and full-text search re-pointed at the mirrored transcript. | Restarting the server resumes the previous conversation with context intact. |
| **6. Context and cost visualisation** | Context tab rebuilt on `get_context_usage()`; HUD rebuilt on `ResultMessage.model_usage` and `RateLimitEvent`. | The Context tab shows the same numbers as `/context` in the CLI, live. |
| **7. Packaging** | Platform-specific wheels or an explicit external-CLI mode; the bundled CLI is ~295 MB. | A fresh machine can install and run without a manual `npm i -g @anthropic-ai/claude-code`. |

Phases 1–3 are the risky ones and should not be interleaved: keep the native engine intact and
reachable until phase 2's exit criterion is genuinely met, then delete in one commit. Phase 2's
criterion is now met, so the deletion is unblocked.

Phase 3's footprint is wider than its row implies. As of the end of phase 2, `litellm` is reachable
from ten files: `llm_service.py`, `llm/_commit.py`, `llm/_helpers.py`, `config.py`, `main.py`,
`settings.py`, `token_counter.py`, `context_manager.py`, `history_compactor.py` and
`logging_setup.py`. The last four are not obviously "engine" files, which is exactly why the exit
criterion is a grep rather than a file list.

A phase is recorded in [`delivery.md`](delivery.md) when its exit criterion is met — what landed,
what was deliberately left out, and what the next phase has to do first.

## Ordering constraints that are not obvious

- **Permissions before edits.** Do not ship phase 2 with `permission_mode:
  "bypassPermissions"` as a shortcut. The permission dialog is the feature; a build that writes
  files without asking will train users to distrust the tool, and retrofitting the dialog after
  people have muscle memory for silent edits is worse than building it first.
- **Indexes after the rip-out, not before.** The indexes currently have two consumers (prompt
  assembly and the browser). Deleting the prompt-assembly consumer first means the MCP bridge is
  written against one clear consumer instead of two competing ones.
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
