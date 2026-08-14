# Reference: Permissions

**Supplements:** `specs5/3-engine/permissions.md`

The `can_use_tool` signature, the awaitable RPC pair's payload shapes, the tool classification map, and
the SDK result types. The behavioural contracts — ask-path-only, localhost-only authority, the diff
requirement — are in the parent spec.

Verified against `claude-agent-sdk` **0.2.137**.

## Byte-level formats

### Permission ID format

Server-generated, one per `can_use_tool` invocation:

```
perm-{epoch_ms}-{6-char-alphanumeric}
```

Example: `perm-1736956800000-a1b2c3`. Distinct namespace from request IDs so a mis-routed
`resolve_permission` cannot resolve a turn, and never reused within a process lifetime.

### Rule content syntax

`PermissionRuleValue.rule_content` uses Claude Code's own rule syntax, not a format of ours:

```
Bash(npm test:*)          — command prefix match, `:*` is the wildcard suffix
Bash(git status)          — exact command
Edit(src/**)              — path glob
Read(secrets/**)          — path glob, used for deny rules
WebFetch(domain:*.example.com)
```

A rule with `rule_content: None` is a bare tool grant. AC⚡DC never writes one — see the parent spec.

## Numeric constants

| Constant | Value | Notes |
|---|---|---|
| Decision timeout | 300 s | Generous enough to read a diff carefully. On expiry: deny with the timeout reason. |
| No-localhost-client timeout | 30 s | Shorter path when no localhost client is connected at request time, since a fast deny is more useful than a five-minute stall. A localhost client that connects inside the window can still answer. |
| Diff render ceiling | 2 MiB per side | Above it, the dialog shows a summary and the raw input with an explicit "too large to diff" label rather than hanging Monaco. |
| Command display cap | 4 000 chars | Above it the command is shown truncated with a full-text expander. Never truncated silently. |
| Prompts-per-turn metric | counted per turn, reported in `StreamCompleteResult.permission_prompts` | Feeds the click-through tripwire in `specs5/plan/risks.md` § R-12. |

## Schemas

### The callback signature

```python
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResult],
]
```

Arguments: tool name, tool input, context.

`ToolPermissionContext` fields:

| Field | Type | Notes |
|---|---|---|
| `tool_use_id` | `str \| None` | Always a non-empty string in practice; the `Optional` is dataclass field-ordering only. Correlates the dialog with the tool card already on screen. |
| `suggestions` | `list[PermissionUpdate]` | Rules the CLI itself proposes for "always allow". Prefer these over a synthesised rule — see § Dependency quirks. |
| `agent_id` | `str \| None` | Non-null when the call originates inside a subagent. The dialog must say which subagent is asking. |
| `blocked_path` | `str \| None` | The path that triggered the request, e.g. a `Bash` command reaching outside allowed directories. |
| `decision_reason` | `str \| None` | Why the request was raised — carries a `PreToolUse` hook's `permissionDecisionReason` when one asked. |
| `title` | `str \| None` | The CLI's own full prompt sentence, e.g. `Claude wants to read foo.txt`. **Prefer this over a summary of our own** where present: it is what the terminal would show, so the two front ends agree. |
| `display_name` | `str \| None` | Short noun phrase for the action, e.g. `Read file`. Suitable for a button label or a compact row. |
| `description` | `str \| None` | Human-readable subtitle for the dialog. |
| `signal` | `Any \| None` | Reserved for abort-signal support; unused. |

The last three were absent from an earlier draft of this table. They are the CLI's own copy for the
call, they are exactly what a permission dialog needs, and reconstructing a sentence from tool name and
input when the CLI already sent one is how the browser and the terminal end up describing the same call
differently. All three are carried through to `permissionRequest` verbatim, `None` included; the
dialog falls back to its own summary only when they are absent.

`PermissionRuleValue`, `PermissionUpdate`, `ToolPermissionContext`, `PermissionResultAllow` and
`PermissionResultDeny` are importable from `claude_agent_sdk.types`. Only some are re-exported from the
package root — `PermissionRuleValue` is **not** (`"PermissionRuleValue" in dir(claude_agent_sdk)` is
`False` in 0.2.137) — so import the permission types from `claude_agent_sdk.types` as a set rather than
discovering the split one `ImportError` at a time.

### Return types

```python
PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
PermissionResultDeny(behavior="deny", message="", interrupt=False)
```

Mapping from user decision:

| Decision | Returned |
|---|---|
| Allow once | `PermissionResultAllow()` |
| Allow with edited input | `PermissionResultAllow(updated_input=<edited dict>)` — offered for `Bash`, `Write` and `NotebookEdit` only, see below |
| Always allow | `PermissionResultAllow(updated_permissions=[PermissionUpdate(...)])` |
| Deny | `PermissionResultDeny(message=<reason>, interrupt=False)` |
| Deny and stop the turn | `PermissionResultDeny(message=<reason>, interrupt=True)` |
| Timeout | `PermissionResultDeny(message=<timeout reason>, interrupt=False)` |

`interrupt=True` aborts the whole turn, not just the call. It is offered as a distinct button, never
as the default deny — a denial the agent can adapt to is more useful than a stopped turn.

`message` is never empty: the agent receives it and a blank denial produces a blind retry.

**`updated_input` is not offered for `Edit` or `MultiEdit`.** Their input is a list of
`old_string` → `new_string` replacements, and the dialog's editor works on the file's proposed content;
turning an edited full-file result back into replacements means guessing which replacement the user
meant. A call that then ran something other than what the dialog showed is a worse outcome than having
no edit affordance at all, so those tools get allow-or-deny and the reason box. The tools whose input
carries the whole file — `Write` (`content`) and `NotebookEdit` (`new_source`) — accept an edit
faithfully, and `Bash` accepts an edited `command` string.

### `PermissionUpdate` for "always allow"

```python
PermissionUpdate(
    type="addRules",
    rules=[PermissionRuleValue(tool_name="Bash", rule_content="npm test:*")],
    behavior="allow",
    destination="projectSettings",
)
```

- `type` ∈ `addRules`, `replaceRules`, `removeRules`, `setMode`, `addDirectories`, `removeDirectories`.
  AC⚡DC writes `addRules` only.
- `behavior` ∈ `allow`, `deny`, `ask`.
- `destination` ∈ `userSettings`, `projectSettings`, `localSettings`, `session`. Default for "always
  allow" is `projectSettings` (`.claude/settings.json`, committed and shared with the CLI).
  `session` is never used for "always allow" — an invisible in-memory grant is exactly what the parent
  spec forbids.
- `to_dict()` emits camelCase (`toolName`, `ruleContent`); the dataclass is what we pass, the dict is
  what goes on the wire.

### There is no runtime rule API

A `PermissionUpdate` reaches the CLI **only** as `PermissionResultAllow.updated_permissions`, returned
from inside a `can_use_tool` callback. `ClaudeSDKClient` in 0.2.137 exposes `connect`, `disconnect`,
`query`, `receive_messages`, `receive_response`, `interrupt`, `set_model`, `set_permission_mode`,
`stop_task`, `rewind_files`, `get_context_usage`, `get_server_info`, `get_mcp_status`,
`reconnect_mcp_server` and `toggle_mcp_server` — and nothing that adds, removes or lists permission
rules. The consequences are load-bearing and an earlier draft of this file assumed otherwise:

- **The file picker's deny-read gesture has no `session` path.** It happens outside any tool call, so
  there is no callback return value to attach a `session`-scoped rule to. The only mechanism that
  reaches the CLI is writing the rule into `.claude/settings.local.json` ourselves, which the CLI reads
  on its own. `set_denied_read_files` is therefore a file writer, not an SDK call, and the "this session
  only" option in `specs5/5-webapp/file-picker.md` § Denial Scope Prompt has to be honest about what it
  means: AC⚡DC drops the rule from its own list at session end and rewrites the file, rather than the
  engine forgetting anything.
- **A rule written mid-session is not retroactive** to a call already in flight, and nothing can query
  the CLI for the rules currently in force. The dialog's "always allow" therefore reports what it
  *wrote*, never what the engine now believes.

The file picker's third checkbox state writes:

```python
PermissionUpdate(type="addRules",
                 rules=[PermissionRuleValue(tool_name="Read", rule_content="<path or glob>")],
                 behavior="deny",
                 destination="localSettings")
```

`localSettings` (`.claude/settings.local.json`, git-ignored) because a per-user exclusion is not a
project policy.

### `permissionRequest(data)` — server → browser (broadcast)

```pseudo
PermissionRequestPayload:
    permission_id: string
    request_id: string | null          // the turn this belongs to; null if outside a turn
    tool_name: string
    server: string | null              // MCP server name for mcp__* tools
    tool_use_id: string
    agent_id: string | null
    tool_class: "read" | "write" | "exec" | "delegate" | "interact" | "mcp"
    gated_by_default: bool             // whether this class is gated in `default` mode
    input: object                      // full tool input, verbatim
    summary: string                    // one-line human summary, ours
    blocked_path: string | null
    decision_reason: string | null
    title: string | null               // the CLI's own prompt sentence; preferred over `summary`
    display_name: string | null        // the CLI's short noun phrase for the action
    description: string | null         // the CLI's subtitle
    suggested_rules: list[SuggestedRule]
    diff: DiffPayload | null           // present for tool_class == "write"
    command: CommandPayload | null     // present for tool_class == "exec"
    question: QuestionPayload | null   // present for tool_class == "interact"
    expires_at: string                 // ISO 8601 UTC; drives the dialog countdown
    localhost_available: bool          // false ⇒ dialog explains the short timeout

SuggestedRule:
    label: string                      // rendered on the "always allow" control
    tool_name: string
    rule_content: string | null
    behavior: "allow" | "deny" | "ask"
    destination: string
    origin: "cli" | "derived"          // cli ⇒ came from context.suggestions

DiffPayload:
    path: string
    is_new_file: bool
    is_binary: bool
    too_large: bool
    original: string | null            // current on-disk content; null for a new file
    proposed: string | null            // null when too_large or is_binary
    additions: integer
    deletions: integer

CommandPayload:
    command: string                    // capped at 4 000 chars
    truncated: bool                    // true ⇒ `command` was cut; full text is in `input`
    cwd: string
    description: string | null         // the agent's own description of the call
    flags: list[string]                // e.g. ["writes", "network", "deletes"] — heuristic, advisory

QuestionPayload:
    question: string                   // the first question, promoted for the dialog's headline
    options: list[{label: string, description: string | null}]
    multi_select: bool
    questions: list[Question]          // every question, in order

Question:
    question: string
    header: string | null              // AskUserQuestion's short chip label
    options: list[{label: string, description: string | null}]
    multi_select: bool
```

`flags` is a display hint derived from the command text. It is explicitly advisory: it must never gate
anything, because a heuristic that gates would be either bypassable or wrong.

`truncated` is what makes the 4 000-char cap non-silent: the dialog reads it to offer a full-text
expander over the verbatim `input`, which is never truncated.

`AskUserQuestion` takes a **list** of questions (`input.questions`, each with its own `header`,
`options` and `multiSelect`), not the single question an earlier draft of this block described. The
first is promoted to the top-level fields so a dialog that renders one question is still correct, and
the whole list travels in `questions` so a dialog that renders all of them can. Option entries are
normalised: the tool permits a bare string as well as `{label, description}`, and both arrive as
`{label, description}`. A payload with no question at all is `null` rather than an empty shell.

The CLI's own bounds are 1–4 questions and 2–4 options each, question texts unique within a call and
option labels unique within a question. The payload does not enforce them — a call that violated them
would have been rejected before reaching `can_use_tool`, and a dialog that refused to render an
out-of-bounds call would fail closed on the one tool class the user cannot route around. An option's
third field, `preview`, is **not** carried: it is a block of HTML the terminal renders for comparing
mockups side by side, and forwarding untrusted model-authored HTML into the dialog's shadow DOM is not
something to do incidentally. See `specs5/5-webapp/permission-dialog.md` § `interact`.

### `resolve_permission(permission_id, decision)` — browser → server

```pseudo
PermissionDecision:
    action: "allow" | "allow_always" | "deny" | "deny_interrupt"
    reason: string | null              // required for deny actions
    rule_index: integer | null         // index into suggested_rules, for allow_always
    updated_input: object | null       // when the user edited the input; Bash/Write/NotebookEdit only
    answers: list[list[integer]] | null  // interact only: chosen option indices, one list per question
```

Returns:

| Return | When |
|---|---|
| `{status: "accepted"}` | Decision recorded and returned to the SDK |
| `{error: "restricted", reason: str}` | Caller is not localhost — the standard restricted shape, see `specs-reference/1-foundation/rpc-inventory.md` § Restricted error shape |
| `{error: "unknown", reason: str}` | No such `permission_id` |
| `{error: "already_resolved", resolved_by: str}` | Another localhost client won the race, or it timed out |

### Answering an `interact` request

Allowing an `AskUserQuestion` call is not the same as answering it. The tool reads its answers off its
own input, so the answer has to travel as an `updated_input`:

```python
PermissionResultAllow(updated_input={**tool_input, "answers": {"Which branch?": "dev5"}})
```

Verified against the bundled CLI 2.1.229, whose tool definition is:

- Input: `questions` (1–4, each `{question, header, options: 2–4 × {label, description, preview?},
  multiSelect}`), plus `answers: Record<str, str>` — described in the CLI as "User answers collected by
  the permission component" — plus `annotations` and `metadata`.
- `checkPermissions` returns `{behavior: "ask"}` unconditionally, which is why this class is gated in
  every mode.
- The tool's `call` destructures `{questions, answers = {}, annotations, response, afkTimeoutMs}` from
  that input and returns them as its result.
- The result the model sees is built from `answers` by question text. **With no `answers` key the model is
  told "The user did not answer the questions"** — so a dialog that collects a selection and then allows
  the call plainly shows the user an answered question and hands the agent silence. An answer that is not
  one of the option labels is delivered too, prefixed with an instruction to read it carefully, which is
  how the tool's auto-provided "Other" reply reaches the model.

Three rules the CLI enforces that the map has to respect:

- **The key is the question text**, exactly as the tool was called with it — not a normalisation of ours.
  `build_question_payload` fills a missing question text from `header`; `build_answer_input` therefore
  keys off the verbatim `tool_input["questions"][i]["question"]` and only falls back to the normalised
  text.
- **Multi-select is one string joined with `", "`.** The CLI splits on exactly that separator to check the
  parts back against the option labels.
- **A question with no key is a question the user declined**, which is a legitimate state — an empty
  answer counts as covered. So a partial answer set is deliverable, and the browser's rule that "Answer"
  waits for every question is a UI choice, not a protocol requirement.

The mapping from indices to labels lives on the engine side for two reasons. The engine already holds the
verbatim tool input, so the key it writes cannot drift from what the tool was called with; and
`updated_input` being present on a decision is what marks a call as user-modified in the transcript.
Answering a question the agent asked is not modifying the call it made, so the browser sends
`answers` — option indices — and never builds the patch itself.

### `permissionResolved(data)` — server → browser (broadcast)

```pseudo
PermissionResolvedPayload:
    permission_id: string
    request_id: string | null
    action: string                     // as above, plus "timeout"
    reason: string | null
    resolved_by: string                // client id, or "timeout"
    rule_written: SuggestedRule | null
```

Closes the dialog on every other client, including non-localhost observers, and gives them the
attribution note.

### Tool classification map

The `tool_class` values and their default posture. This is a constant in the permissions module, not a
config surface — a user who wants a different posture writes a rule.

| Class | Tools | Gated by default |
|---|---|---|
| `read` | `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `NotebookRead`, `TodoWrite`, `mcp__ac-dc__*` | No |
| `write` | `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | Yes |
| `exec` | `Bash`, `BashOutput`, `KillShell` | Yes |
| `delegate` | `Task` | No |
| `interact` | `AskUserQuestion` | Always, by the SDK |
| `mcp` | any `mcp__*` tool from a server other than `ac-dc` | Yes |

An unrecognised tool name classifies as `mcp` when it matches `mcp__*` and as `exec` otherwise —
unknown-and-gated is the safe default, and a new built-in tool appearing in a CLI upgrade must not
arrive ungated.

## Dependency quirks

### Prefer the CLI's own suggestions

`ToolPermissionContext.suggestions` carries `PermissionUpdate` objects the CLI has already computed for
this call. They match the CLI's own rule semantics exactly, including how it normalises a `Bash`
command into a prefix pattern. Deriving our own rule risks writing one that is either broader than the
user expects or that never matches again. Use `origin: "cli"` suggestions first and mark derived rules
as such in the payload so the UI can label a fallback.

### A hook that returns a decision silently disables the dialog

The SDK emits `CanUseToolShadowedWarning` when a `PreToolUse` hook returns a `permissionDecision` that
pre-empts `can_use_tool`. `"allow"` shadows it just as `"deny"` does, and `"defer"` stops the run and
surfaces the call in `ResultMessage.deferred_tool_use`. AC⚡DC's hooks return no decision at all;
treat the warning appearing in logs as a regression, and `deferred_tool_use` being non-null likewise.

### `PermissionMode` has six values

`Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]`. In `dontAsk`,
interaction-required tools (`AskUserQuestion`, MCP tools flagged as requiring interaction,
organisation-`ask` connector tools) are **denied without invoking the callback** — so a UI that only
learns about those tools through the callback will show nothing at all in that mode.

### The callback does **not** run on the SDK's read loop

An earlier draft of this file said a slow `can_use_tool` blocks message delivery for the session. It
does not. `Query._read_messages` dispatches a `control_request` through
`_spawn_control_request_handler`, which spawns each handler as its own detached child task
(`spawn_task` → `spawn_detached`, tracked in `_inflight_requests` so `close()` can cancel it). The read
loop returns to reading immediately, so a five-minute permission decision does not stall the turn's
remaining messages, and two permission requests raised by parallel tool calls are genuinely concurrent
rather than serialised behind each other.

What still matters, and what the implementation still does:

- **Blocking I/O inline blocks the whole event loop**, not just the read loop — one thread serves the
  SDK, the WebSocket, and every other session. The diff's file read goes through
  `run_in_executor` for that reason: a 2 MiB synchronous read would stall the very socket that has to
  deliver the dialog asking about it.
- **The wait for the browser is a plain `asyncio` future** keyed by `permission_id`, never a poll.
- **Cancellation is real.** The handler task is cancelled on `close()`, so a pending permission does not
  outlive its session; the future must be resolved (deny) on that path rather than left pending.
