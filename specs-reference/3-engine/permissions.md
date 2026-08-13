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
| `signal` | `Any \| None` | Reserved for abort-signal support; unused. |

### Return types

```python
PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
PermissionResultDeny(behavior="deny", message="", interrupt=False)
```

Mapping from user decision:

| Decision | Returned |
|---|---|
| Allow once | `PermissionResultAllow()` |
| Allow with edited input | `PermissionResultAllow(updated_input=<edited dict>)` |
| Always allow | `PermissionResultAllow(updated_permissions=[PermissionUpdate(...)])` |
| Deny | `PermissionResultDeny(message=<reason>, interrupt=False)` |
| Deny and stop the turn | `PermissionResultDeny(message=<reason>, interrupt=True)` |
| Timeout | `PermissionResultDeny(message=<timeout reason>, interrupt=False)` |

`interrupt=True` aborts the whole turn, not just the call. It is offered as a distinct button, never
as the default deny — a denial the agent can adapt to is more useful than a stopped turn.

`message` is never empty: the agent receives it and a blank denial produces a blind retry.

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
  spec forbids — but is the correct destination for the file picker's deny-read rule when the user
  wants it for this session only.
- `to_dict()` emits camelCase (`toolName`, `ruleContent`); the dataclass is what we pass, the dict is
  what goes on the wire.

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
    input: object                      // full tool input, verbatim
    summary: string                    // one-line human summary
    blocked_path: string | null
    decision_reason: string | null
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
    command: string
    cwd: string
    description: string | null         // the agent's own description of the call
    flags: list[string]                // e.g. ["writes", "network", "deletes"] — heuristic, advisory

QuestionPayload:
    question: string
    options: list[{label: string, description: string | null}]
    multi_select: bool
```

`flags` is a display hint derived from the command text. It is explicitly advisory: it must never gate
anything, because a heuristic that gates would be either bypassable or wrong.

### `resolve_permission(permission_id, decision)` — browser → server

```pseudo
PermissionDecision:
    action: "allow" | "allow_always" | "deny" | "deny_interrupt"
    reason: string | null              // required for deny actions
    rule_index: integer | null         // index into suggested_rules, for allow_always
    updated_input: object | null       // when the user edited the input
```

Returns:

| Return | When |
|---|---|
| `{status: "accepted"}` | Decision recorded and returned to the SDK |
| `{error: "restricted", reason: str}` | Caller is not localhost — the standard restricted shape, see `specs-reference/1-foundation/rpc-inventory.md` § Restricted error shape |
| `{error: "unknown", reason: str}` | No such `permission_id` |
| `{error: "already_resolved", resolved_by: str}` | Another localhost client won the race, or it timed out |

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
| `read` | `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `NotebookRead`, `mcp__ac-dc__*` | No |
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

### The callback runs on the SDK's read loop

A slow `can_use_tool` blocks message delivery for the session. The implementation must not do blocking
I/O inline: the diff's file read happens in an executor, and the wait for the browser is a plain
`asyncio` future keyed by `permission_id`.
