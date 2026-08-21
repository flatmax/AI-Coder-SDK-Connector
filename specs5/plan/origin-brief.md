# Claude Code Frontend

**Status:** superseded — preserved as the origin document for the conversion. Kept verbatim
(apart from this header) because its reasoning is still the best statement of *why*, and the
specs assume the reader has met it.

Where this document and the rest of `specs5/` disagree, the specs win. The disagreements are
enumerated in [`sdk-surface.md § Corrections to the origin brief`](sdk-surface.md#corrections-to-the-origin-brief);
the two most consequential are that the conversion is a **total replacement** rather than a mode
([`decisions.md § CC-1`](decisions.md#cc-1--total-replacement-not-a-dual-engine-mode-user)), and that
the open question about context visibility has been **answered** — `get_context_usage()` gives us
`/context` as live data ([`decisions.md § CC-4`](decisions.md#cc-4--claude-codes-context-is-visualised-not-guessed-user)).

Original status line, as written: *future — not implemented. This document captures the design
thinking from an exploratory conversation. Revisit when token cost on the native engine becomes the
dominant complaint, or when a user asks for Claude Code's tool loop against an AC⚡DC repo.*

Sibling in spirit to `mcp-integration.md`: both describe adopting an external Anthropic surface. The difference in scope matters and is the central caveat of this document — MCP *extends* the native LLM engine, whereas this design *replaces* it. See [What this is not](#what-this-is-not).

## Motivation

AC⚡DC's native engine is token-expensive. The four-tier stability cache (`3-llm/cache-tiering.md`) and structural maps (`2-indexing/`) exist precisely to mitigate that, and they work — but the strategy is *pre-ingest the repo's structure, then cache it hard*. Cost scales with repo structure, not with the size of the turn.

Claude Code takes the opposite approach: ingest nothing up front, then grep and read on demand. For a large repo and a narrow edit, this is dramatically cheaper. For broad cross-file reasoning over a warm cache, AC⚡DC's approach may well win. The two are genuinely different bets, not better and worse.

Separately — and probably more significant than any context strategy — Claude Code can be billed against a Claude subscription rather than metered API tokens. That is a step change in cost rather than a percentage improvement. **Confirm against the Agent SDK documentation and terms whether subscription auth is permitted for embedded/programmatic use before treating this as available.** If it is not, the cost case rests entirely on the on-demand-read argument above, which is real but far more modest.

The goal of this design is therefore: **Claude Code behaving exactly as it does under the CLI, with every prompt, response, and question rendered in and answered from the AC⚡DC browser UI.** Not a terminal embedded in a web page — the actual agent loop driving AC⚡DC's existing widgets.

## What this is not

This is **not** an alternative provider behind `LLMService`, and framing it that way will mislead implementation.

Claude Code owns its own context management, its own prompt caching, its own tool loop, and its own edit application. Adopting it means the entire native engine — roughly 15.7k lines across `src/ac_dc/llm_service.py` and `src/ac_dc/llm/` — does not participate:

- the four-tier stability cache (`_stability.py`) — Claude Code caches on its own terms
- tree-sitter symbol maps and doc outlines — replaced by on-demand grep and read
- the deterministic anchored edit protocol (`3-llm/edit-protocol.md`) — replaced by the built-in `Edit` tool's matching
- prompt assembly and breakdown (`_assembly.py`, `_breakdown.py`)

What *is* reused is the entire browser surface, which is the expensive half to build and the half AC⚡DC is good at.

So this is a **second engine sharing the UI shell** — closer to "AC⚡DC as a Claude Code frontend" than "AC⚡DC with a new backend." That is a product decision, not a refactor, and it should be made deliberately rather than discovered partway through implementation. The native engine remains the default; this is a mode.

## Why it is feasible: the async permission callback

The design hinges on one fact.

The Agent SDK's permission hook, `can_use_tool`, is an **async callback returning an awaitable**:

```python
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]
]
```

AC⚡DC's transport (`1-foundation/rpc-transport.md`) is symmetric bidirectional JSON-RPC over WebSocket via jrpc-oo — the browser publishes its own callback interface, and the backend can call into it and await a result.

Composing the two: when Claude Code needs permission, the backend calls the browser, the browser renders a dialog, and the agent loop suspends until the user clicks. No polling, no state machine, no out-of-band queue. The primitive required for "all questions answered in the browser" already exists in AC⚡DC and needs no transport work.

## The programmatic surface

Two options exist for driving Claude Code:

| Surface | Package | Notes |
|---|---|---|
| **Claude Agent SDK** | `claude-agent-sdk` (Python) | In-process. Claude Code packaged as a library: full agent loop, built-in tools, context management, hooks, subagents, permissions, sessions. **This is the one to use** — the backend is already a single Python process. |
| **Headless CLI** | `claude -p --output-format stream-json` | Subprocess over stdio. Same harness, coarser control, smaller blast radius. Useful as a throwaway feasibility probe; not the destination. |

Both are **harness-only — AC⚡DC hosts and deploys them**, which suits the local-first posture (loopback bind, state under `.ac-dc4/`, no cloud round-trip for repo contents).

Docs: `code.claude.com/docs/en/agent-sdk`

### Not to be confused with

The Anthropic API's **Tool Runner** (`client.beta.messages.tool_runner`, part of the `anthropic` SDK) is a loop helper over tools *you* define. No built-in tools, no filesystem access. It would not produce Claude Code's behaviour and is not a substitute here. Likewise **Managed Agents**, where Anthropic hosts a per-session sandbox — incompatible with operating on the user's local repo.

### Session client

Use `ClaudeSDKClient`, not `query()`. `query()` creates a new session per call; `ClaudeSDKClient` reuses one session across turns, which is what a chat panel needs.

```python
class ClaudeSDKClient:
    async def connect(self, prompt=None) -> None
    async def query(self, prompt, session_id: str = "default") -> None
    async def receive_messages(self) -> AsyncIterator[Message]
    async def receive_response(self) -> AsyncIterator[Message]   # stops after ResultMessage
    async def interrupt(self) -> None
    async def set_permission_mode(self, mode: str) -> None
    async def set_model(self, model: str | None = None) -> None
    async def rewind_files(self, user_message_id: str) -> None
    async def stop_task(self, task_id: str) -> None
    async def disconnect(self) -> None
```

### Options

```python
options = ClaudeAgentOptions(
    system_prompt={"type": "preset", "preset": "claude_code"},   # REQUIRED for CLI parity
    tools={"type": "preset", "preset": "claude_code"},           # REQUIRED for CLI parity
    setting_sources=["user", "project", "local"],                # CLAUDE.md, settings, custom commands
    include_partial_messages=True,                               # incremental deltas for streaming
    include_hook_events=True,                                    # surfaces HookEventMessage
    can_use_tool=ask_the_browser,
    hooks={...},                                                 # PreToolUse for universal display
    enable_file_checkpointing=True,                              # enables rewind_files()
    max_budget_usd=...,                                          # hard per-session cost cap
    model=..., fallback_model=...,
    permission_mode=...,                                         # see modes below
    session_id=..., resume=..., fork_session=...,
)
```

**The first three lines are the ones that get missed.** `system_prompt` defaults to `None` and `tools` defaults to `None` — without the `claude_code` presets the result is a generic agent, not Claude Code. `setting_sources` also defaults to `None`; user/project/local are required for `CLAUDE.md`, settings files, and custom slash commands to load. These three settings are the difference between "similar to the CLI" and "actually the CLI's behaviour," which is the stated requirement.

### Permission modes

```python
PermissionMode = Literal[
    "default",            # standard prompting
    "acceptEdits",        # auto-accept file edits
    "plan",               # explore without editing
    "dontAsk",            # deny anything not pre-approved rather than prompting
    "bypassPermissions",  # bypass checks; explicit ask rules still prompt
    "auto",               # model classifier approves or denies
]
```

Switchable mid-session via `set_permission_mode()`, so the browser can expose this as a live control.

### Permission results

```python
@dataclass
class PermissionResultAllow:
    behavior: Literal["allow"] = "allow"
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[PermissionUpdate] | None = None

@dataclass
class PermissionResultDeny:
    behavior: Literal["deny"] = "deny"
    message: str = ""
    interrupt: bool = False
```

`ToolPermissionContext` carries the fields a dialog needs rendered without the frontend having to know tool semantics: `title` (e.g. `"Claude wants to read foo.txt"`), `display_name` (e.g. `"Read file"`), `description`, `suggestions: list[PermissionUpdate]`, `tool_use_id`, `agent_id`, `blocked_path`, `decision_reason`, `signal`.

Returning a `suggestions` entry in `updated_permissions` with destination `localSettings` persists the rule to `.claude/settings.local.json` — i.e. the "don't ask again" checkbox has a real backing mechanism and requires no AC⚡DC-side storage.

## Mapping to existing browser surfaces

| Claude Code behaviour | Agent SDK hook | AC⚡DC surface |
|---|---|---|
| Streaming assistant text | `include_partial_messages` → `StreamEvent` | `webapp/src/chat-panel/streaming.js` |
| Tool calls (Read/Edit/Bash) | `AssistantMessage` tool-use blocks | chat panel renderer |
| Thinking blocks | `ThinkingBlock` (only when thinking display is `"summarized"`) | chat panel, collapsed |
| Permission prompt | `can_use_tool` → await browser | `webapp/src/app-shell/dialog.js` |
| `AskUserQuestion` multi-choice | reaches `can_use_tool` **even with allow rules** | `dialog.js` + option buttons |
| Esc to interrupt | `client.interrupt()` | existing stop control |
| Permission mode switching | `set_permission_mode()` | `webapp/src/app-shell/mode.js`, settings tab |
| Plan mode and its approval gate | `permission_mode="plan"` | dialog |
| File edits shown as diffs | `PostToolUse` hook on Edit/Write | Monaco diff viewer |
| Subagents in their own view | `agents` option + Task tool | `webapp/src/chat-panel/tabs.js` |
| Session resume / fork | `resume`, `session_id`, `fork_session` | session history browser |
| Cost and token display | `ResultMessage` usage | Token HUD |
| Checkpoint / rewind | `rewind_files(user_message_id)` | new control near git actions |
| Background task notifications | `TaskNotificationMessage`, `stop_task()` | toasts (`app-shell/toasts.js`) |

The subagent row is the standout: AC⚡DC's agent-mode tab machinery (`4-features/`, `chat-panel/tabs.js`) already implements per-agent chat views with archives and re-attach across reconnects. Claude Code subagents land on a UI concept that is already built and tested.

The Token HUD row is worth noting too — combined with `max_budget_usd`, the HUD becomes a genuine cost display with an enforced ceiling, which is better instrumentation than the native engine currently offers.

## Known hazards

Four things will bite during implementation. All are documented SDK behaviours, not speculation.

### 1. `can_use_tool` is not a universal gate

It fires **only** when the permission flow falls through to a prompt. Calls approved by `allowed_tools`, by settings allow rules, or by `permission_mode` (`acceptEdits`, `bypassPermissions`) never reach it.

Consequence: `can_use_tool` cannot be used to *display* tool activity, only to *ask about* it. For the browser to see every tool call, use a **`PreToolUse` hook** with `include_hook_events=True`, and keep `can_use_tool` strictly for the ask path.

Also: do not list gated tools in `allowed_tools`, since allow rules approve before the callback runs. Exceptions that always reach the callback: `AskUserQuestion`, MCP tools marked `requiresUserInteraction`, and org-`ask` connector tools — though in `dontAsk` mode these are denied without invoking it.

### 2. `interrupt()` does not clear the buffer

After interrupting, the interrupted turn's messages — including its `ResultMessage` — must be drained before reading the next query's response, or replies render in the wrong bubble. `ResultMessage.terminal_reason` will be `"aborted_streaming"` or `"aborted_tools"`.

### 3. Do not `break` out of the message iterator

The SDK documentation explicitly warns this causes asyncio cleanup issues; use flags and let iteration complete. This matters more here than in a CLI context, because a browser client disconnecting mid-turn is the normal case, not an edge case. Reconciling this with AC⚡DC's existing reconnect logic (`app-shell/reconnect.js`) needs thought.

### 4. Built-in slash commands are CLI UI, not SDK features

Custom slash commands arrive through `setting_sources`, but `/compact`, `/clear`, `/model` and friends are terminal-side interface, not harness features. They must be mapped onto SDK calls manually — `/model` → `set_model()`, `/clear` → new session, and so on. Individually cheap; collectively this is the gap between "same harness" and "same interface," and it is the part most likely to be underestimated.

### Type-access convention

Message and block classes are `@dataclass`es (attribute access: `msg.result`). Config types such as `ThinkingConfigEnabled`, `McpStdioServerConfig`, `SyncHookJSONOutput`, `ToolsPreset`, `TaskBudget` are `TypedDict`s (key access: `config["budget_tokens"]`). Mixing these up is a common source of runtime errors.

### Authentication conflict

The Agent SDK honours the same credential resolution as Claude Code. Where both an `ant auth login` profile (under `~/.config/anthropic/`) and a Claude Code `/login` credential exist, expect an auth-conflict warning — keep one. Worth surfacing as a clear startup diagnostic rather than letting it fail opaquely.

## Implementation sketch

Deliberately small, because most of the work is already done in the webapp.

**Backend**

- `src/ac_dc/claude_code/session.py` — a `ClaudeCodeSession` wrapping `ClaudeSDKClient`: builds `ClaudeAgentOptions`, owns the receive loop, translates SDK messages into the existing streaming/progress/file-broadcast callbacks.
- `src/ac_dc/claude_code/permissions.py` — the `can_use_tool` implementation plus the `PreToolUse` display hook.
- RPC surface mirroring the existing mixin split (`src/ac_dc/llm/_rpc_*.py`) so the webapp contract is unchanged where possible.
- Engine selection in `src/ac_dc/config/llm.json`, hot-reloadable via the settings tab like every other config value.

**One genuinely new RPC pair**

The browser must publish a method the backend can call *and await* for permission and question prompts. Everything else in this design reuses existing RPC shapes; this is the one addition. Specify its argument shape in `specs-reference/` per the companion-tree convention — it carries `ToolPermissionContext` fields across the wire and returns a `PermissionResult`.

**Frontend**

- An engine-mode flag in `app-shell` selecting native vs. Claude Code presentation.
- A permission/question dialog in `app-shell/dialog.js`, including the "don't ask again" path that returns a `PermissionUpdate`.
- Reuse `chat-panel/streaming.js`, `chat-panel/tabs.js`, the Monaco diff viewer, the file picker, and the Token HUD unchanged.

## Suggested delivery order

1. **Feasibility probe** — headless CLI (`claude -p --output-format stream-json`) behind a flag, output dumped to the existing chat panel with no permission handling. Answers "does the transport shape fit?" in hours, and is disposable.
2. **Instrument the comparison** — run identical turns on both engines and compare Token HUD figures on real repos. This is what tells you whether the cost premise actually holds *for these repos*, and it should gate the decision to continue. Do not skip it; the on-demand-read advantage is repo-shaped and could go either way.
3. **Resolve the billing question** — determine whether subscription auth is permitted for this use. If yes, that dominates every other consideration and justifies the full build. If no, revisit step 2's numbers before committing.
4. **Agent SDK, in-process** — `ClaudeCodeSession`, the async permission bridge, dialog UI. This is the real implementation.
5. **Parity pass** — slash-command mapping, subagent tabs, checkpoint/rewind, interrupt-and-drain correctness, reconnect behaviour.

Steps 2 and 3 are cheap and decisive. Doing them before step 4 avoids building the interesting part on an unverified premise.

## Open questions

- Does subscription-based auth cover embedded/programmatic use? Unresolved, and the single highest-leverage unknown in this document.
- Do the two engines coexist in one session's history, or are Claude Code sessions stored separately? `HistoryStore` assumes native-engine turn structure.
- Does document mode (`3-llm/modes.md`) have any meaning under Claude Code, whose tools are code-oriented? SVG editing in particular has no counterpart.
- Should the native anchored edit protocol be offered to Claude Code as an MCP tool, preserving deterministic edits and git staging while still using its loop? This is the one plausible path to *composing* the two engines rather than choosing between them, and it deserves its own investigation.
- How does collaboration mode (`--collab`) interact with an agent that has its own permission model and writes to `.claude/settings.local.json`?
