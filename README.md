# AIC⚡DC — AI-Coder-SDK-Connector

AIC⚡DC is a browser UI over AI coding-agent SDKs. It runs as a terminal application in a git repository, opens a browser, and gives the agent a workspace a terminal cannot: a Monaco diff viewer over everything it touches, a git-status file tree, an SVG editor, permission dialogs that render the actual diff before you approve it, and live visibility into what the turn cost.

**Claude Code is the engine that works end to end.** The name says *connector* because the seam is deliberate — the engine layer talks to an agent SDK, not to a model provider — and there is now a second implementation of that seam in the tree: Google Antigravity, reached two ways. The [Antigravity SDK](https://pypi.org/project/google-antigravity/) drives its own bundled binary on a Gemini API key; the `agy` CLI drives the same product over a pipe on your own Google subscription, needs no Python wheel, and is what a base install reaches. Antigravity is usable as a *consultant* from inside a Claude turn, and as **master** for a whole conversation: live turns have run through the chat panel on both transports, and on the subscription a full conversation including an approved write completed on 2026-09-05. It is still the less-exercised engine, and ten of the fifteen surfaces the capability descriptor tracks are hidden on it. See [Engines](#engines) for exactly what is landed and what is not.

**AIC⚡DC counts no tokens and prices nothing.** Every number it renders was measured by an engine and handed over; there is no token counter and no price table in this codebase. Which credentials it touches depends on the engine:

- **Claude Code** — none. The `claude` CLI owns credentials, billing, and the whole agent loop, and AIC⚡DC never writes provider credentials into the process environment. If it appeared to inject them it would silently redirect billing away from the account you authenticated.
- **Antigravity, on the SDK** — a Gemini API key, which AIC⚡DC does read (from `$GEMINI_API_KEY` or `~/.config/aic-dc/gemini-api-key`) and pass to the SDK's config object. It is never written into the process environment either, never logged, and never crosses the RPC boundary — the browser is told the key's *source*, never its value. With no key the engine and its consultant tools are absent rather than broken.
- **Antigravity, on `agy`** — none. The `agy` CLI authenticates from the OS keyring against your own Google account, which is the whole reason that transport exists: it is the route to a paid subscription, where the SDK's key is metered.

---
## Division of Labour

The single most useful thing to understand about this project is what it does *not* do.

| Owned by the engine | Owned by AIC⚡DC |
|---|---|
| The conversation and the context window | The Monaco diff viewer, with LSP, over every file the agent touched |
| Prompt caching and cache breakpoints | The git-status file tree, search, and 2-D navigation grid |
| Tool execution — read, write, edit, bash, grep, web fetch | The permission dialog, with the edit rendered as a two-level diff |
| Subagents (`Task`) and skills | Live context / cost / rate-limit visualisation |
| Compaction and session persistence | The SVG viewer and visual editor, TeX preview, markdown preview |
| Applying edits to disk | Document conversion, code review, collaboration |
| Credentials, retries, token counting | Tree-sitter symbol and document indexes — offered to the agent as MCP tools and to the editor as language features |

The last row is the one piece of genuine intelligence AIC⚡DC contributes. Everything else it owns is presentation over state the agent already produced.

---
## Engines

AIC⚡DC drives an agent SDK, and there are two of them in this tree — reached through three mountable engine identifiers, because Antigravity has two transports that differ in which account pays. Exactly one is **master** per session — the engine the chat panel talks to — and Antigravity is also reachable as a **consultant**: a one-shot call for a second opinion, or for a capability the master lacks.

| | Claude Code | Antigravity (API key) | Antigravity (subscription) |
|---|---|---|---|
| Engine selector reads | `claude` | `antigravity (API key)` | `antigravity (subscription)` |
| Driven through | [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) driving the local `claude` CLI | [google-antigravity](https://pypi.org/project/google-antigravity/) driving its own bundled `localharness` binary | the `agy` CLI, one long-lived subprocess speaking `stream-json` |
| Credentials | the CLI's, whatever you authenticated it with | a Gemini API key, or a Vertex project | `agy`'s own OAuth, against your Google account |
| In a base install | yes | no — needs the `antigravity` extra | yes, if the `agy` binary is on PATH |
| Permission dialog driven by | the SDK's `can_use_tool` | a `PreToolCallDecideHook` | a `PreToolUse` hook process talking back over a unix socket |
| As master | **complete** | live turns run, including a real `edit_file` diff and an approved write — but not yet all in one conversation, see below | **a full conversation on the paid subscription, including an approved write** (2026-09-05) |
| As consultant | not built — nothing calls back into Claude from an Antigravity turn | **works today** | not available — `agy` has no one-shot consultation mode |

All three mount under the *same* RPC namespace through `engine_router.py`, so the browser's call sites do not fork on which engine is running. A method the running engine cannot feed stays on the wire and **refuses** — it does not disappear, because a missing method is indistinguishable from a broken build. 31 of the router's 48 methods are engine-generic and every engine must implement them; the other 17 are mapped to a capability surface, and Antigravity refuses **10** of those on both transports. It serves 38 of 48; it served 31 until phase 5 built history and the session mirror.

### The `agy` transport, and why it exists

The plan originally excluded the `agy` CLI and then reversed that, because the free-tier Gemini key refuses at **20 requests per model per day** — a handful of agent turns — while the paid access is a Google AI Pro subscription reachable only through `agy`'s OAuth. So this is "reach the account the user pays for, or do not run" rather than a preference between transports. It also turned the packaging question around: the extra is not "Antigravity is optional", it is "the *metered* route to Antigravity is optional".

The thing that had disqualified `agy` was permissions: its headless mode cannot prompt, only auto-deny, allowlist or bypass. The answer is a lifecycle hook. AIC⚡DC installs a `PreToolUse` hook into `agy`'s **global** `~/.gemini/config/hooks.json`; it fires before *every* tool call — matching one tool at a time ships a gate the model walks around, which was measured, not guessed — blocks on AIC⚡DC's own permission dialog, and returns the human's answer, with the tool's arguments in hand so the dialog renders a real diff. Four things about it are worth stating, because a global hook is a strong thing to install:

- **It is scoped by conversation id.** A session `agy` is running for you that AIC⚡DC does not own passes straight through, unasked. Measured against a real second session: 13.2 s of overlap, 9 calls gated, none of them the stranger's, and its work completed.
- **It fails closed** on timeout, non-zero exit, malformed JSON and an unreachable host — but only for conversations this host owns, recorded as files on disk. Ownership is a fact on disk precisely so "not ours" cannot be confused with "ours but unreachable".
- **The engine refuses to start a session without it** rather than running one ungated, because `agy` itself runs under `--dangerously-skip-permissions` and the hook is the only gate there is.
- Install and remove it from the Settings tab. Installation **probes the command before writing it** and refuses one that does not answer with a JSON decision. That check exists because the gate once reported itself installed while allowing everything: on a frozen release binary the recorded command was correct as a *string* and unrunnable as a *command*, and its `|| allow` fallback answered every call. A moved virtualenv would have done the same.

### What Antigravity can and cannot do

`capabilities.py` holds a descriptor of 15 surfaces per engine, and the webapp asks it — never an engine name — before rendering anything. Ten surfaces are hidden on Antigravity, identically on both transports, and the descriptor distinguishes the two reasons:

- **Absent — no source data, ever.** USD cost of any kind (there is no dollar figure anywhere on the SDK, so the turn footer's cost, the session total and `max_budget_usd` all have nothing to render), live context-window usage and the auto-compact threshold, account rate-limit windows, mid-turn rate-limit notices, the slash-command palette, file checkpointing, and forking a past conversation.
- **Unbuilt — a to-do, not a wall.** Subagent tabs, agent-initiated structured questions, and the MCP server inventory. The list used to open with the session mirror and the history browser; phase 5 built both, and "always allow" left the absent list the same way in phase 9 — which is the descriptor working as a to-do list rather than as a record.

In the other direction, **image generation** is absent on Claude and supported on Antigravity. That asymmetry is the whole argument for a second engine.

### Status, honestly

Landed and usable:

- **The consultant, streaming into its own tab.** With a Gemini key and the `antigravity` extra present, an ordinary Claude turn gains two tools on their own MCP server — `mcp__aic-dc-antigravity__second_opinion` and `mcp__aic-dc-antigravity__generate_image`. They mount separately from the ungated `aic-dc` index server precisely so both go through the permission dialog. Since phase 6b (2026-09-02) a consultation opens its **own agent tab** and fills with Google's thinking and text as they arrive, rather than sitting behind one tool card until the answer lands; ⏹ Stop cancels it mid-flight, a stalled one reports a heartbeat every 20 s, and the tab shows tokens with no USD figure because the descriptor says this engine reports no dollars.
- **Antigravity as master, driven live.** The first browser conversation ran on the SDK transport on 2026-09-03: the engine switch, the notice, the step stream and an `edit_file` dialog rendering a real +5 −0 diff, with the write landing. Turns have run on the `agy` transport since 2026-09-04, and a **full conversation on the paid subscription including an approved write** on 2026-09-05 — `replace_file_content` presented as a write with a +1 −1 diff, allowed by a click, the edit on disk. In the same run a deny carrying a reason was watched steering the agent off an over-broad `find` onto the right file.
- **The engine router, the capability descriptor, and per-engine hiding** across the Usage HUD, the Context tab, the Settings tab and the chat panel's action bar.
- **The Antigravity adapter itself** — session lifecycle, the `Step` → event pump, options assembly, and a permission gate that drives the *same* `PermissionBroker` as Claude Code, so there is one ask path and one queue across all three transports. The gate covers **every mutating tool including `run_command` and `start_subagent`**, because a probe showed the agent, refused an `edit_file`, reaching for `sed -i` to make the same change by other means — and that instinct has since been watched live on `agy` too, blocked there as well.
- **History, resume and the session mirror** (phase 5, 2026-09-05). Conversations mirror into a store root per transport, the history browser renders them, and a restart resumes through the engine's own mechanism rather than by replaying a transcript into a prompt — proven across two processes by making the model return a passphrase only the resumed context held, because a mirror on disk looks identical for a resume that quietly opened a blank conversation.
- **"Always allow" on Antigravity** (phase 9, 2026-09-05). The dialog offered only *Allow once* and *Deny*, because the engine has no counterpart to Claude's `updated_permissions` at any layer — but persistence was always AIC⚡DC's to own, so the rule is kept in a per-repo store of ours and consulted before the call reaches the broker. Verified in a browser on the subscription: two identical `run_command` turns, one dialog, the second call never reaching the broker. Matching is deliberately exact — `rm -rf build/` does not match `rm -rf /`, `git push:*` does not match `git pushover`, and a path rule is one file for one tool, because matching by tool *class* would let a grant made by reading a diff also permit a whole-file overwrite. A denied-read shift-click still beats a standing allow. The Settings tab lists what you have granted with a Forget button beside each, because one click to give and a text editor to undo is the wrong shape for a permission.
- **Choosing the master per session** — `app.json`'s `engines.master` names the engine that starts, and the Settings tab can switch it mid-run. A switch is a boundary: the incoming engine starts blank rather than silently reattaching to the conversation it was last in, and nothing is deleted, so what was left behind stays listed and loadable.

Not landed:

- **No single SDK-transport conversation has yet run end to end including an approved write.** Every part of it has worked separately — the dialog, the diff, the write landing, reads passing without a modal, the transcript surviving the turn — but not all in one conversation, because the free-tier key's 20-requests-per-model-per-day ceiling was reached in between. That is a billing fact rather than a defect, and the same criterion is *met* on the `agy` transport, which is what it was added for.
- ~~**No history or resume on Antigravity.**~~ **Landed 2026-09-05 (phase 5).** Conversations are mirrored per transport into their own store root, the history browser renders them, and a server restart resumes the previous conversation with the model's context intact — proven across two processes on the paid subscription. What is still not built is *forking* one: the harness owns the conversation store, so there is nothing to copy, and the button is hidden rather than offered.
- **A *successful* consultation has never been watched streaming.** The tab appears, labels, settles and mirrors a row into Main, all confirmed in a browser — but both live attempts that day stalled provider-side, so the chunk-by-chunk rendering is verified by `scripts/probe_consultation_tab.py` and not yet by eye.
- **Three surfaces are unbuilt rather than impossible**: subagent tabs, agent-initiated structured questions, and the MCP server inventory. The data exists in each case; no renderer or settings surface does.
- **Image generation has never returned an image.** Every Gemini image model reports `limit: 0` on a free-tier key. That is not a throttle and no wait fixes it; the tier is a property of the key's Cloud project, so enabling billing on that project moves the same key to a paid tier and nothing in the credential path changes.
- **`agy` sometimes writes to the wrong place and reports success.** Some writes land in `~/.gemini/antigravity-cli/scratch/` instead of the repository. Three explanations have been offered confidently and all three were wrong — the trusted-workspace list, git-repository-ness, create-versus-edit — while a probe excluded a concurrent second session and an empty workspace as well. What is left is a correlation deliberately not called a cause: a newly created file lands when the turn also touches an existing one, and diverts when creating is all the turn does. Five runs, no mechanism. What *is* built is detection: a completed write whose target is missing here while a file of that name sits in the scratch directory raises a notice naming both paths, so the failure is diagnosable rather than silent. It is not prevented.
- ~~**`google-antigravity` is still a base dependency.**~~ **Landed 2026-09-05 (phase 7).** It is the `antigravity` extra: `pip install "aic-dc[antigravity]"`. A base install is 273.1 MiB and the extra adds 135.2 MiB, almost all of it the bundled `localharness` binary. **A base install still reaches Antigravity** — the `agy` transport drives the CLI on your own subscription and needs no wheel. What the extra buys is the API-key route and the consultant, which has no `agy` equivalent.

The plan of record, phase by phase, is [`specs5/plan-ag/`](specs5/plan-ag/) — start at [`decisions.md`](specs5/plan-ag/decisions.md). As of 2026-09-05 every phase in it is closed; what is left open is the list above, and none of it is a phase. [`delivery.md`](specs5/plan-ag/delivery.md) has one entry per phase written when its exit criterion was met, including the defects each live run found — the recurring one being shared code meeting a second transport whose case nobody re-checked, which several thousand green tests never see.

---
## Features

- **A real editor beside the agent.** Monaco side-by-side diff over the working tree, with hover, go-to-definition, references, and completions backed by the local symbol index — plus markdown preview, TeX preview, and cross-file markdown link navigation.
- **Permission dialogs that show the change.** An `Edit` or `Write` request renders as a line-and-word-level diff before you approve it, not as raw JSON. Six permission modes, from `default` through `plan` and `acceptEdits` to `bypassPermissions` (which is never the default and warns explicitly). Requests resolve against **localhost clients only**.
- **Tree-sitter symbol index** for Python, JavaScript, TypeScript/TSX, C, C++, and MATLAB, with cross-file reference graphs — exposed to the agent through an in-process MCP server and to the editor as LSP-style language features.
- **Document index** for markdown and SVG: heading outlines, containment-aware SVG structure, a cross-reference graph between documents, and optional KeyBERT keyword enrichment.
- **Six read-only MCP tools** on the `aic-dc` server — `symbol_map`, `file_symbols`, `find_references`, `doc_outline`, `review_state`, `ui_state`. The agent can ask what the repo's shape is and what the user is currently looking at. Those six are ungated, because reading cannot hurt you; the two consultant tools sit on a **separate** `aic-dc-antigravity` server for exactly that reason — they spend someone's quota and one of them writes a file, so they go through the permission dialog.
- **Tool cards** for every call the agent makes: input summary, status, duration, files modified (clickable through to the diff), and a marker on anything that went through a permission prompt. `TodoWrite` renders as one live checklist rather than fifteen snapshots.
- **Honest cost and context accounting.** A Usage HUD and Context tab render only what the engine measured — per-turn cost when it is priced, nothing when it is not (a subscription turn is never shown as `$0.00`), a context gauge with the auto-compact threshold marked on the bar, and a live token counter that steps as each assistant message lands. On an engine that reports no dollars at all, the cost *figure* disappears while the row that would hold it stays.
- **A second engine.** Google Antigravity sits behind the same RPC namespace as Claude Code, as a consultant — ask Claude for a second opinion and it reaches Google's model, through the permission dialog, streaming into its own tab — and as master for a whole conversation, on a Gemini API key or on your own Google subscription through the `agy` CLI. A capability descriptor tells the browser which surfaces each engine can feed, so nothing renders an empty or synthesised value. Ten of fifteen surfaces are hidden on Antigravity, and because hiding that many at once reads as a broken build rather than as a different engine, the chat panel names the engine that is answering and lists the surfaces it has not had built for it. See [Engines](#engines).
- **Visual SVG editor** — click-to-select, drag-to-move, resize handles, path endpoint and control-point editing, inline text edit, marquee multi-selection, copy / paste / duplicate, undo, copy-as-PNG, and a full-width presentation mode (F11).
- **File picker** with git status badges, diff stats, sort modes, context menus for every row type, inline rename / duplicate / new-file / new-directory, `@`-filter from the chat input, branch badge with detached-HEAD detection, keyboard navigation, and shift+click to **deny the agent read access** to a path.
- **Code review mode** — pick a commit in a live git graph, soft-reset the branch, and work through the change with reverse diffs in context. The current review state is visible to the agent through `review_state`.
- **Document conversion** — `.docx`, `.pdf`, `.pptx`, `.xlsx`, `.csv`, `.rtf`, `.odt`, `.odp` to markdown with extracted images and per-page SVG exports. PDFs go through [PyMuPDF](https://pymupdf.readthedocs.io/); presentations pipe through [LibreOffice](https://www.libreoffice.org/) → PDF → PyMuPDF with python-pptx as fallback; spreadsheets keep their cell colours as emoji markers.
- **Slash-command passthrough.** `/` opens a palette; most commands go straight to the CLI and are answered for zero turns and zero dollars. A few are intercepted and routed to the UI that already does the job — `/context` to the Context tab, `/clear` to a new session, `/permissions` to Settings, `/resume` to the history browser.
- **Session continuity** — restart resumes the last session, the history browser resumes any past one, and branching forks it. Each engine mirrors into a store root of its own, and resume is always the engine's own mechanism rather than a transcript replayed into a prompt; forking is Claude-only, and hidden elsewhere, because Antigravity's conversation store belongs to its harness and copying our mirror would leave two transcripts pointed at one engine conversation. There is no undo: the SDK refuses to combine a mirrored transcript with file checkpointing, and the mirror is what makes the repo-local history work, so undo is git's job.
- **Subagent tabs.** When the agent spawns a `Task`, its transcript appears as a read-only tab with a kill switch. There is no channel to speak into it — it is the agent's subagent, not yours.
- **Full-text search** — two panels, matching files on the left and highlighted line context on the right, with regex / whole-word / case-sensitive modes and bidirectional scroll sync. Plus in-chat message search.
- **2-D file navigation grid** — opened files arrange spatially; `Alt+Arrow` moves between them without a tab bar.
- **Speech** — Web Speech API dictation into the chat input, and sentence-by-sentence read-aloud of assistant messages with a draggable floating transport.
- **KaTeX math** in chat (`$$…$$` and `$…$`), and live TeX preview for `.tex` files via make4ht with bidirectional scroll sync.
- **Images** — paste a screenshot into chat and it goes into the transcript.
- **Collaboration mode** — multiple browsers on one backend over LAN. The host is auto-admitted; later clients need explicit approval within 120 s. Non-localhost participants get a read-only view and cannot answer permission prompts.
- **Symmetric bidirectional JSON-RPC** over WebSocket via [jrpc-oo](https://github.com/flatmax/jrpc-oo) — terminal and browser are peers, and either side calls the other.

---
## Philosophy

- **Do not reimplement the agent.** Every capability the CLI already has — caching, compaction, retries, tool execution, session persistence — is the CLI's. A wrapper that duplicated any of them would be a second, worse copy that drifts.
- **Structural maps beat raw files.** The agent gets compact, reference-annotated maps of the repo on request: a tree-sitter symbol map of classes, methods, imports and call sites, and a keyword-enriched outline of markdown headings and SVG containment trees. Both are always available as tools, so there is no mode to switch between them.
- **Show, do not summarise.** A permission prompt renders the diff. A tool card names the files it modified and links to them. A turn footer lists what changed in the repo. The panel's job is to make the agent's actions legible.
- **Never print a number you did not measure.** Cost with no basis renders as absent, not as zero. A cache counter that was never reported is omitted, not printed as `0`. AIC⚡DC counts no tokens of its own.
- **Git is a first-class citizen.** The file picker shows git status and diff stats natively, commit messages are generated from the diff, and code review runs through soft-reset rather than side branches.
- **Local is the default.** The backend binds to loopback; LAN access requires an explicit `--collab`. All persistent state lives in the repo's `.aic-dc/` directory. No cloud sync, no telemetry.
- **Degrade, do not fail.** Keyword enrichment, document conversion, LibreOffice, make4ht, individual tree-sitter grammars, the second engine in either of its transports — the SDK wheel, the Gemini credential, the `agy` binary — and even the `aic-dc` MCP server can all be absent, and the rest of the app carries on. The `claude` CLI is the one hard prerequisite.
- **Hide what an engine cannot report; never synthesise it.** A surface with no source data on the running engine disappears rather than rendering a blank or a plausible zero — and the browser decides by asking a capability descriptor, never by checking an engine's name, so a third engine costs a descriptor row rather than a sweep through the webapp.

---
## Architecture

A single Python process runs an asyncio loop with two listeners: a static HTTP server for the webapp and a jrpc-oo WebSocket server for RPC. One `ThreadPoolExecutor` absorbs the CPU-bound work — tree-sitter parsing, keyword enrichment, document conversion — so the event loop stays free for WebSocket frames. Both listeners bind `127.0.0.1` unless `--collab` is passed.

```
 ┌──────────────────────────────────────────────────────────────────┐
 │                        Python backend                            │
 │                                                                  │
 │   Repo ──┬── SymbolIndex ──┐                                     │
 │          ├── DocIndex ─────┤                                     │
 │          ├── DocConvert    ├──▶ MCP server "aic-dc"  (6 tools)   │
 │          ├── Settings      │            │                        │
 │          └── Collab        │            ▼                        │
 │                            └──▶  EngineRouter  ── capabilities   │
 │                                  │  one master, N mounted        │
 │                   ┌──────────────┴───────┬───────────────┐       │
 │                   ▼                      ▼               ▼       │
 │          ClaudeCodeService      AntigravityService   AgyService  │
 │           permissions ·          shared broker ·      (subclass) │
 │           hooks · history ·      step pump ·          gate over  │
 │           cost · review          mirror · rules       a socket   │
 │                   │                      │               │       │
 │                   ▼                      ▼               ▼       │
 │           ClaudeSDKClient        antigravity.Agent    agy stdio  │
 │                   │                      │               │       │
 │  ▼  jrpc-oo/WS    ▼  stdio               ▼  Go binary    ▼ pipe  │
 └──┬────────────────┬──────────────────────┬───────────────┬───────┘
    │                │                      │               │
    │         ┌──────┴─────┐         ┌──────┴───────┐  ┌────┴─────┐
    │         │ claude CLI │         │ localharness │  │ agy CLI  │
    │         └────────────┘         └──────────────┘  └──────────┘
    │           ▲ owns credentials,    ▲ Gemini key      ▲ your Google
    │             context, tool loop     or Vertex         account (OAuth)
    ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │                       Browser webapp (Lit)                       │
 │                                                                  │
 │   AppShell ──┬── FilesTab ──┬── FilePicker                       │
 │              │              └── ChatPanel ── tool cards,         │
 │              │                    subagent tabs, slash palette   │
 │              ├── ContextTab   (Usage / Session / Debug)          │
 │              ├── SettingsTab                                     │
 │              ├── DocConvertTab                                   │
 │              ├── SdkSurfaceTab                                   │
 │              │                                                   │
 │              │  Viewer layer (background)                        │
 │              ├── DiffViewer  (Monaco + LSP + MD / TeX preview)   │
 │              ├── SvgViewer + SvgEditor                           │
 │              │                                                   │
 │              └── Overlays — PermissionDialog, UsageHUD,          │
 │                  FileNavGrid, CompactionProgress, SpeechControls │
 └──────────────────────────────────────────────────────────────────┘
```

Startup is split in two so the browser gets feedback immediately. Phase 1 (under a second) validates the git repository, picks ports, **resolves the `claude` binary and reads its version and credential source**, starts the webapp and WebSocket servers, and opens the browser onto a startup overlay. No SDK client and no symbol index yet — and nothing in phase 1 writes `os.environ`. Phase 2 runs as a background task, building the symbol index and wiring it into the MCP bridge with progress pushed to the overlay.

Every mountable engine is constructed during startup, not the master alone — an engine that is not master still has to exist for the consultant to reach it. The SDK transport mounts only if **both** the `google-antigravity` wheel and a Gemini or Vertex credential resolve; the `agy` transport mounts if the Antigravity CLI is on PATH. Each absence is logged with the reason that actually applies — telling a user who has a key to go and set a key is worse than saying nothing — and the engine selector renders what genuinely mounted, so a base install offers `claude` and `agy` and nothing else. `app.json`'s `engines.master` picks which one the chat panel starts on, and a master that failed to mount falls back to Claude rather than to nothing.

There is no transcript-loading step and no eager resume. Showing the conversation is a disk read; resuming is a `claude` subprocess, and most launches are someone opening AIC⚡DC to read a diff.

See [specs5/0-overview/architecture.md](specs5/0-overview/architecture.md) for the full picture.

---
## Running

### Prerequisites

| Requirement | Notes |
|---|---|
| Python ≥ 3.10 | |
| The **`claude` CLI**, authenticated | The one hard prerequisite. Startup fails with an actionable message naming every location it searched. |
| A git repository | AIC⚡DC refuses to start outside one, and explains how to fix it in the browser. |
| Node.js ≥ 20 | Only for webapp development (`--dev` / `--preview` / `npm run build`). |
| [uv](https://docs.astral.sh/uv/) | Recommended for Python dependency management. |
| A **Gemini API key** | Optional — for Antigravity's API-key transport and its consultant tools, alongside the `antigravity` extra. Without either they are absent and nothing else changes. |
| The **`agy` CLI**, signed in | Optional — Antigravity's subscription transport. Needs no Python wheel and no Gemini key; it authenticates against your own Google account. Without it that engine is not offered. |

There are two routes to Antigravity and they are independent. **The API-key route** needs the optional wheel *and* a credential:

```
uv sync --extra antigravity          # or: pip install "aic-dc[antigravity]"
```

Then put a Gemini API key where the engine looks for it. The order is `$GEMINI_API_KEY` first, then the key file:

```
mkdir -p ~/.config/aic-dc
printf '%s\n' 'YOUR_KEY' > ~/.config/aic-dc/gemini-api-key
chmod 600 ~/.config/aic-dc/gemini-api-key
```

A Vertex AI project works too — set `GOOGLE_GENAI_USE_VERTEXAI=true` with `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, and the engine routes through Vertex instead of the key. A project set *without* the flag is inert, and looks for all the world like configuration that is in effect — so it raises a warning naming the variable that is doing nothing, rather than being silently ignored. The Context tab's Debug section names the source that was found, never the secret.

**The subscription route** needs neither the extra nor a key: install Google's `agy` CLI, sign it in, and *antigravity (subscription)* appears in the engine selector. Before it will run a turn it requires AIC⚡DC's permission gate to be installed into `agy`'s own global `~/.gemini/config/hooks.json` — one click in the Settings tab, which also removes it. The engine refuses to start a session while that gate is missing rather than running one ungated, because `agy`'s headless mode cannot prompt for permission and the hook is the only thing between the model and your files.

### From source

```
git clone https://github.com/flatmax/AI-Coder-SDK-Connector.git
cd AI-Coder-SDK-Connector

# Python dependencies (dev group included automatically)
uv sync

# Webapp dependencies
cd webapp && npm ci && cd ..
```

Run inside any git repository:

```
cd /path/to/your/project

# Development mode — Vite dev server with hot module replacement
uv run aic-dc --dev

# Preview mode — Vite production build, served locally
uv run aic-dc --preview

# Bundled mode (default) — built-in static server
uv run aic-dc
```

Standalone binaries (Linux / macOS / Windows) are a deferred deliverable — see [specs5/6-deployment/build.md](specs5/6-deployment/build.md).

### Optional extras

The document index works out of the box with heading outlines, SVG containment trees, and cross-references. Richer features pull extras, as does Antigravity's API-key transport:

```
# Document conversion (markitdown, PyMuPDF, python-pptx, openpyxl) — small
uv sync --extra docs-convert

# Keyword enrichment (KeyBERT, sentence-transformers, torch) — large (~800 MB with CUDA wheels)
uv sync --extra docs-enrich

# Both
uv sync --extra docs

# The Antigravity SDK engine and its consultant tools — adds 135.2 MiB,
# almost all of it the bundled localharness binary. Not needed for the
# `agy` subscription transport.
uv sync --extra antigravity
```

System-level optional tools:

| Tool | Purpose |
|---|---|
| [LibreOffice](https://www.libreoffice.org/) (`soffice` on PATH) | `.pptx` / `.odp` → PDF for document conversion. Without it, `.pptx` falls back to python-pptx (basic SVG export). |
| [make4ht](https://ctan.org/pkg/make4ht) (part of TeX Live) | Live TeX preview for `.tex` files. Without it, the preview pane shows installation instructions. |

---
## Configuration

On first run AIC⚡DC creates a per-repo working directory at `.aic-dc/` and a user-level config directory:

| Platform | Config path |
|---|---|
| Linux | `~/.config/aic-dc/` (or `$XDG_CONFIG_HOME/aic-dc/`) |
| macOS | `~/Library/Application Support/aic-dc/` |
| Windows | `%APPDATA%\aic-dc\` |

### There is no provider configuration

This is the biggest change from earlier versions of this project, and it is worth stating flatly: **there is no `llm.json`, no provider list, no API-key field, and no system prompt for AIC⚡DC to own.** The Antigravity key is the one narrow exception, and it is a bare file rather than a config surface: nothing in the Settings tab writes it, and no RPC ever returns it.

- Credentials belong to the `claude` CLI. Authenticate it however you normally would; AIC⚡DC reports which credential source it found (in the Context tab's Debug section) and otherwise stays out of the way.
- Agent instructions come from `CLAUDE.md` and `.claude/` in your repository, read by the CLI. Editing them is editing the agent's behaviour.
- Permission rules live in `.claude/settings.local.json`, which is also where deny-read entries are written. That is the `claude` CLI's own file, and Antigravity has no counterpart at any layer — so an "always allow" granted on either Antigravity transport is kept by AIC⚡DC, in `~/.config/aic-dc/antigravity-rules.json`, keyed by repository. The Settings tab lists those rules with a Forget button beside each: a permission you cannot see is one you cannot audit.

### Config files

| File | Purpose | Hot-reload |
|---|---|---|
| `engine.json` | Engine overrides — model, effort, permission mode, budget cap, CLI path | `model` and `permission_mode` apply to the live session; the rest take effect on the next session |
| `app.json` | Document conversion, document index, history, and which engine is master | Yes |
| `snippets.json` | Quick-insert prompt buttons, grouped `code` / `review` / `doc` | On next open |
| `commit.md` | Commit-message generation prompt | Re-read per use |

All of these are editable from the Settings tab in the browser.

#### `engine.json` fields

Every field defaults to `null`, meaning "let the CLI decide".

| Field | Values | Description |
|---|---|---|
| `model` | model id | Primary model for the session |
| `commit_model` | model id | Cheaper model for commit-message generation |
| `permission_mode` | `default`, `acceptEdits`, `plan`, `bypassPermissions`, `dontAsk`, `auto` | Starting permission posture. `bypassPermissions` is never a default and warns explicitly |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max` | Reasoning effort |
| `thinking_display` | `summarized`, `omitted` | How thinking is surfaced |
| `max_budget_usd` | number | Hard spend cap for the session |
| `cli_path` | path | Override `claude` binary resolution |
| `max_buffer_size` | bytes | Raises the SDK's stdout line-length ceiling; a value that is too *small* ends sessions, so it has a floor |

The permission-mode and effort value sets are read from the SDK's own type aliases at import time, with a built-in fallback — so a new mode added by the SDK is picked up without a code change here.

#### `app.json` sections

| Section | Key fields |
|---|---|
| `doc_convert` | `enabled`, `extensions`, `max_source_size_mb` |
| `doc_index` | `keyword_model`, `keywords_enabled`, `keywords_top_n`, `keywords_ngram_range`, `keywords_min_section_chars`, `keywords_min_score`, `keywords_diversity`, `keywords_tfidf_fallback_chars`, `keywords_max_doc_freq` |
| `history` | `session_dir_warning_bytes`, `mirror_gap_tolerance` |
| `engines` | `master` — `claude` (default), `antigravity` (the API-key transport) or `agy` (the subscription transport). Also switchable mid-run from the Settings tab; a name that did not mount falls back to `claude` with a warning |

Full field reference: [specs5/1-foundation/configuration.md](specs5/1-foundation/configuration.md).

---
## Typical Workflow

1. `cd` into a git repository and run `uv run aic-dc`. The browser opens on the Files + Chat tab.
2. Type a request. Point at files with `@` in the chat input, or middle-click a picker row to insert its path.
3. The agent works. Tool cards appear as it goes; edit-shaped calls open themselves so the diff is visible.
4. When it wants to do something your permission mode does not pre-approve, a dialog opens with the change rendered as a diff. Approve, deny with a reason the agent will see, or switch mode for the rest of the session.
5. Click a file chip in a tool card or the turn footer to open it in the diff viewer, with LSP over the symbol index.
6. Read the turn footer: files modified, tool calls, how many needed a prompt, duration, per-model token split, and cost when it is priced.
7. Commit from the chat panel's action bar — the message is generated from the diff by a cheaper model.

### Code review sub-workflow

1. Open the commit graph and pick a commit.
2. AIC⚡DC soft-resets the branch to its parent, so the change appears as uncommitted work.
3. Work through it with reverse diffs in the viewer. The agent can read the review state through `review_state`, so it knows what is under review without being told.
4. Re-commit when done.

---
## CLI Options

| Flag | Default | Description |
|---|---|---|
| `--server-port` | `18080` | RPC WebSocket starting port (probes upward if taken) |
| `--webapp-port` | `18999` | Webapp static / dev / preview starting port |
| `--no-browser` | off | Don't auto-open the browser |
| `--repo-path` | `.` | Git repository path |
| `--dev` | off | Run a local Vite dev server (hot reload) |
| `--preview` | off | Build and serve the webapp locally |
| `--verbose` | off | Debug-level logging |
| `--collab` | off | Collaboration mode — bind all interfaces, admission-gated |
| `--experimental` | off | Unlock experimental UI affordances that are otherwise locked read-only. Editing the underlying JSON config directly works without the flag |

---
## Keyboard Shortcuts

| Shortcut | Context | Action |
|---|---|---|
| `Enter` | Chat input | Send message |
| `Shift+Enter` | Chat input | Newline |
| `Up` | Chat input (cursor at start) | Open input history |
| `/…` | Chat input (leading token) | Slash-command palette, filtering as you type |
| `@text` | Chat input | Filter file picker live |
| `Alt+1` … `Alt+5` | Global | Switch tab — Files+Chat / Context / Settings / Convert / SDK Surface |
| `Alt+M` | Global | Toggle dialog minimize |
| `Alt+Left/Right/Up/Down` | Global | File navigation grid |
| `Ctrl+Shift+F` | Global | File search, seeded with the current selection |
| `Ctrl+S` | Diff viewer / Settings / SVG editor | Save active file |
| `Ctrl+F` | Diff viewer | Monaco find widget |
| `Scroll wheel` | SVG viewer | Zoom in / out (cursor-centred) |
| `Middle-drag` | SVG viewer | Pan |
| `F11` / `Escape` | SVG viewer | Enter / exit presentation mode |
| `Click` | SVG editor | Select element |
| `Drag handle` | SVG editor | Move endpoint / vertex / control point |
| `Double-click` | SVG editor (text) | Edit text inline |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+D` | SVG editor | Copy / paste / duplicate |
| `Delete` / `Backspace` | SVG editor | Delete selection |
| `Escape` | SVG editor | Deselect / cancel text edit |
| `Up` / `Down` / `Home` / `End` | File picker | Navigate tree |
| `Left` / `Right` | File picker | Collapse / expand / traverse |
| `Space` / `Enter` | File picker | Expand a directory / open a file |
| `Escape` | File picker | Cancel inline edit / close context menu |
| `F2` | File picker (file row) | Rename focused file inline |
| `Shift+Click` | File picker (any row) | Deny the agent read access to that path or subtree |
| `Middle-click` | File picker (file row) | Insert path into chat input |

Global shortcuts are all suppressed while the permission dialog is open — a keystroke should never move the app out from under a decision you are making.

---
## Per-Repo Working Directory

A `.aic-dc/` directory is created at the repo root on first run and added to `.gitignore`:

| Entry | Contents |
|---|---|
| `sessions/` | Repo-local mirror of the SDK's session transcripts (JSONL) |
| `antigravity-sessions/`, `agy-sessions/` | The same, per Antigravity transport. Three roots rather than one: a conversation id means nothing to a harness that did not write it, so a shared list would offer sessions the running transport could not resume |
| `events.jsonl` | Append-only engine event log |
| `index/` | Symbol-index cache |
| `doc_cache/` | Keyword-enriched document outline cache (mtime-keyed sidecars) |
| `tex_preview/` | Transient TeX compilation workspace (cleaned on startup) |

`.gitignore` also carries the two directory names this project used before it was called AIC⚡DC (`.ac-dc4/`, `.ac-dc/`). Nothing reads them; they are there so a checkout still holding old state does not suddenly show tens of megabytes of session JSONL as untracked, and so the file-tree walker and both indexers keep skipping it.

---
## What the SDK Conversion Removed

If you used an earlier version of this project, these are gone — the CLI does all of them, and better:

| Removed | Now |
|---|---|
| LiteLLM and 100+ provider routing, `llm.json` | The CLI resolves its own credentials and provider |
| Four-tier stability prompt cache (L0–L3), cache-breakpoint placement, cache warmer | The CLI manages prompt caching |
| `tiktoken` token counter | The CLI reports usage; AIC⚡DC counts nothing |
| Anchored `🟧🟧🟧 EDIT` block protocol and apply pipeline | The agent's `Edit` / `Write` / `MultiEdit` tools |
| LLM-driven history compactor | The CLI's compaction, with a progress overlay here |
| URL detection, fetch, cache, and summarise chips | The agent's `WebFetch` |
| `🟧🟧🟧 AGENT` spawn protocol and writable agent tabs | The agent's `Task` tool, with read-only subagent tabs |
| Per-mode and per-review system prompts, `system_extra.md` | `CLAUDE.md` and `.claude/` |
| Code / doc / cross-reference **modes** | Nothing. Both indexes are permanently available to the agent as tools, so there is nothing to switch. A lightweight preset selector (snippets and UI only) is specified but not yet shipped |
| File **selection** — choosing what the LLM sees | Deny-read: shift+click a picker row to write a `Read(path)` deny rule |
| `boto3`, `trafilatura`, `tenacity` | Dropped with their transitive trees |

Retained in full: the symbol index, the document index and its reference graph, the diff viewer and its LSP integration, the SVG viewer and editor, TeX and markdown preview, the file picker, the navigation grid, search, the history browser, document conversion, code review, collaboration, images, and speech.

---
## Development

### Setup

```
git clone https://github.com/flatmax/AI-Coder-SDK-Connector.git
cd AI-Coder-SDK-Connector
uv sync                          # Python deps (dev group auto-included)
cd webapp && npm ci && cd ..     # Webapp deps
```

### Run

```
uv run aic-dc --dev
```

Starts the Python backend plus the Vite dev server with hot module replacement.

### Tests

```
uv run pytest                    # Backend
uv run ruff check src tests      # Backend lint
cd webapp && npm test            # Frontend (vitest)
```

Smoke scripts under `scripts/` exercise the engine, MCP bridge, and history paths against a real `claude` CLI; they are not part of the pytest run.

### Build the webapp

```
cd webapp && npm run build
```

### Tech stack

**Backend**

| Package | Purpose |
|---|---|
| [claude-agent-sdk](https://github.com/anthropics/claude-agent-sdk-python) | The agent engine — session, tools, permissions, compaction, MCP |
| [google-antigravity](https://pypi.org/project/google-antigravity/) (extra) | The second agent engine on a Gemini API key, and the only route to the consultant tools; see [Engines](#engines). An extra because its wheel bundles a ~123 MiB binary; the `agy` transport reaches the same engine on your own subscription without it |
| [jrpc-oo](https://github.com/flatmax/jrpc-oo) | Bidirectional JSON-RPC 2.0 over WebSocket |
| [tree-sitter](https://tree-sitter.github.io/) | AST parsing for Python, JS, TS/TSX, C, C++, MATLAB |
| [mcp](https://modelcontextprotocol.io/) | In-process MCP server (pulled in by the SDK) |
| [markitdown](https://github.com/microsoft/markitdown) (extra) | Document-to-markdown |
| [PyMuPDF](https://pymupdf.readthedocs.io/) (extra) | PDF text + SVG extraction |
| [python-pptx](https://python-pptx.readthedocs.io/) (extra) | PowerPoint fallback |
| [openpyxl](https://openpyxl.readthedocs.io/) (extra) | Excel with colour clustering |
| [KeyBERT](https://maartengr.github.io/KeyBERT/) + [sentence-transformers](https://www.sbert.net/) (extras) | Keyword enrichment |

The `claude` CLI is a Node application and is **not** installed by any of these — it is resolved at startup, and the `agy` CLI is in the same position, resolved on PATH. The Antigravity SDK is the other way round: its `localharness` binary ships inside the wheel and is spawned from there, which is why `google-antigravity` is an optional extra rather than a base dependency. The released binary is built without it — PyInstaller never saw the SDK's deliberately function-local imports, so it has never carried a usable one; phase 7 makes that true by declaration rather than by accident, and the release workflow fails the build if `localharness` turns up in the archive.

**Frontend**

| Package | Purpose |
|---|---|
| [Lit](https://lit.dev/) | Web component framework |
| [@flatmax/jrpc-oo](https://www.npmjs.com/package/@flatmax/jrpc-oo) | Browser JSON-RPC client |
| [Monaco Editor](https://microsoft.github.io/monaco-editor/) | Diff editor with LSP |
| [marked](https://marked.js.org/) + [highlight.js](https://highlightjs.org/) | Markdown rendering and syntax highlighting |
| [KaTeX](https://katex.org/) | Math rendering |
| [diff](https://github.com/kpdecker/jsdiff) | Two-level diff computation for tool cards and permission dialogs |
| [svg-pan-zoom](https://github.com/bumbu/svg-pan-zoom) | SVG viewport control |

**Build**

| Tool | Purpose |
|---|---|
| [Vite](https://vitejs.dev/) | Webapp bundler / dev server |
| [Vitest](https://vitest.dev/) | Webapp test runner |
| [pytest](https://pytest.org/) | Backend test runner |
| [ruff](https://docs.astral.sh/ruff/) | Backend linter |
| [PyInstaller](https://pyinstaller.org/) | Standalone binaries (deferred) |

---
## Repository Layout

```
AI-Coder-SDK-Connector/
├── src/aic_dc/                       # Python backend
│   ├── __main__.py                  # python -m aic_dc entry
│   ├── cli.py                       # argparse surface
│   ├── main.py                      # two-phase startup orchestration
│   ├── config.py                    # config dirs, bundled defaults, upgrades
│   ├── rpc.py                       # jrpc-oo server transport
│   ├── settings.py                  # config read / write / reload RPC
│   ├── collab.py                    # multi-browser admission + restrictions
│   ├── engine_router.py             # one RPC namespace over N engines
│   ├── capabilities.py              # per-engine surface descriptor
│   ├── logging_setup.py             # structured stderr logging
│   ├── base_cache.py                # mtime-keyed cache base
│   ├── base_formatter.py            # compact-map formatter base
│   ├── claude_code/                 # the engine layer
│   │   ├── service.py              #   RPC surface the browser calls
│   │   ├── session.py              #   ClaudeSDKClient lifecycle, resume, fork
│   │   ├── session_store.py        #   SessionStore integration
│   │   ├── options.py              #   ClaudeAgentOptions assembly
│   │   ├── engine_config.py        #   engine.json parsing + SDK literal probing
│   │   ├── permissions.py          #   can_use_tool, mode rules, deny-read
│   │   ├── hooks.py                #   PreToolUse / PostToolUse (re-index)
│   │   ├── mcp_server.py           #   in-process "aic-dc" MCP server (6 tools)
│   │   ├── messages.py             #   SDK message → UI payload taxonomy
│   │   ├── cost.py                 #   per-turn cost differencing
│   │   ├── history.py              #   transcript mirror + reads
│   │   ├── history_index.py        #   session search index
│   │   ├── events_log.py           #   events.jsonl writer
│   │   ├── review.py               #   code-review state
│   │   ├── commit.py               #   commit-message generation
│   │   ├── health.py               #   claude binary resolution + version
│   │   ├── sdk_surface.py          #   which SDK features this build wired up
│   │   └── resume_cleanup.py       #   stale-session hygiene
│   ├── antigravity/                 # the second engine, SDK transport
│   │   ├── service.py              #   the 38 of 48 RPC methods this engine serves
│   │   ├── session.py              #   Agent / Conversation lifecycle
│   │   ├── steps.py                #   Step → UI event pump
│   │   ├── options.py              #   LocalAgentConfig assembly
│   │   ├── permissions.py          #   PreToolCallDecideHook → shared broker
│   │   ├── rules.py                #   "always allow" store, matching, derivation
│   │   ├── mirror.py               #   step observer → repo-local session mirror
│   │   ├── credentials.py          #   Gemini key / Vertex resolution
│   │   ├── consultant.py           #   second_opinion, generate_image
│   │   ├── bridge.py               #   the "aic-dc-antigravity" MCP server
│   │   └── surface.py              #   SDK inventory probe
│   ├── agy/                         # the second engine, subscription transport
│   │   ├── service.py              #   AntigravityService subclass
│   │   ├── session.py              #   the long-lived agy subprocess
│   │   ├── steps.py                #   NDJSON frames → UI events
│   │   ├── tools.py                #   agy's tool names, write seam, arg aliases
│   │   ├── gate_server.py          #   unix socket the hook calls back into
│   │   ├── hook.py                 #   the PreToolUse process agy runs
│   │   ├── install.py              #   gate install / status / probe
│   │   └── registry.py             #   which conversations this host owns
│   ├── repo/                        # git operations, file I/O, search
│   │   ├── tree.py  files.py  diffs.py  search.py  staging.py
│   │   ├── branches.py  commits.py  commit_graph.py  review.py
│   │   ├── paths.py  locks.py  errors.py  subprocess_runner.py
│   │   └── tex_preview.py
│   ├── symbol_index/                # tree-sitter code indexing
│   │   ├── index.py  parser.py  cache.py  models.py
│   │   ├── compact_format.py  reference_index.py  import_resolver.py
│   │   └── extractors/  (python, javascript, typescript, c, cpp, matlab)
│   ├── doc_index/                   # markdown + SVG indexing
│   │   ├── index.py  cache.py  models.py  formatter.py
│   │   ├── reference_index.py  keyword_enricher.py  background.py
│   │   └── extractors/  (markdown, svg, svg_geometry)
│   ├── doc_convert/                 # document → markdown pipelines
│   │   ├── service.py  constants.py  provenance.py
│   │   └── markitdown_pipeline.py  pdf_pipeline.py
│   │       pptx_pipeline.py  xlsx_pipeline.py
│   └── config/                      # bundled defaults
│       └── engine.json  app.json  snippets.json  commit.md
├── webapp/                          # Lit-based browser frontend
│   ├── index.html  package.json  vite.config.js
│   └── src/
│       ├── main.js                  # Vite entry
│       ├── rpc.js  rpc-mixin.js     # shared RPC proxy + mixin
│       ├── app-shell/               # root component, tabs, viewers, reconnect
│       ├── chat-panel/              # messages, streaming, tool cards,
│       │                           #   subagent tabs, permission-mode selector
│       ├── permission-dialog/       # queue, decisions, rendered diffs
│       ├── file-picker/             # tree with git status
│       ├── files-tab/               # picker + chat split, review, exclusion
│       ├── diff-viewer/             # Monaco, LSP, preview, scroll sync, export
│       ├── svg-editor/              # visual SVG editing
│       ├── svg-viewer.js            # pan / zoom / presentation mode
│       ├── context-usage-tab.js     # Usage / Session / Debug sections
│       ├── usage-hud.js  turn-cost.js
│       ├── sdk-surface-tab.js       # SDK feature probe
│       ├── engine-capabilities.js   # browser half of the descriptor
│       ├── settings-tab.js  doc-convert-tab.js
│       ├── slash-palette.js  slash-commands.js
│       ├── history-browser.js  input-history.js  message-search.js
│       ├── file-nav.js  viewer-routing.js  file-mentions.js
│       ├── markdown.js  markdown-preview.js  tex-preview.js
│       ├── lsp-providers.js  monaco-setup.js  markdown-link-provider.js
│       ├── speech-to-text.js  speech-synthesis.js  speech-player.js
│       ├── commit-graph.js  image-utils.js  shadow-style-sync.js
│       └── compaction-progress.js  doc-index-progress.js
├── tests/                           # Backend tests (pytest)
├── scripts/                         # Smoke scripts + prompt sync
├── specs5/                          # Behavioural spec suite (current)
├── specs-reference/                 # Byte-level reference twin
├── pyproject.toml                   # uv / pip / hatch config
└── LICENSE
```

---
## Specs

The behaviour of every feature above is specified in [`specs5/`](specs5/), which is the source of truth for this tree:

| Directory | Contents |
|---|---|
| [`0-overview/`](specs5/0-overview/) | Architecture, glossary, implementation guide |
| [`1-foundation/`](specs5/1-foundation/) | Configuration, repository layer, RPC transport and inventory |
| [`2-indexing/`](specs5/2-indexing/) | Symbol index, document index, reference graph, keyword enrichment |
| [`3-engine/`](specs5/3-engine/) | Session, permissions, MCP bridge, tool surface, history, context visibility |
| [`4-features/`](specs5/4-features/) | Code review, collaboration, document conversion, images |
| [`5-webapp/`](specs5/5-webapp/) | Every panel, viewer, dialog, and overlay |
| [`6-deployment/`](specs5/6-deployment/) | Startup, build, packaging |
| [`plan/`](specs5/plan/) | Conversion decisions, risks, delivery, SDK surface |
| [`plan-ag/`](specs5/plan-ag/) | The second engine — Antigravity's verified surface, its `AG-n` decisions, risks, and phase-by-phase delivery |
| [`impl-history/`](specs5/impl-history/) | Layer-by-layer implementation log |

---
## License

MIT — see [LICENSE](LICENSE).
