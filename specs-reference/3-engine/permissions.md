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

#### Path rules are gitignore patterns, and only two tool names are consulted

Verified against CLI 2.1.229 and the published permission reference. Four properties, each of
which AC⚡DC got wrong before phase 2 closed:

- **Only `Edit(path)` and `Read(path)` rules are checked.** A path rule written for `Write`,
  `MultiEdit`, `NotebookEdit` or `Glob` is *accepted, never consulted*, and warned about at
  startup (CLI v2.1.210+). Write `Edit(docs/**)` in place of `Write(docs/**)`, and
  `Read(docs/**)` in place of `Glob(docs/**)`. A derived rule named for the requesting tool is
  therefore a silent no-op: the user grants it and is asked again on the next call.
- **`*` matches within one path segment; `**` matches across directories.** So `<dir>/**` is the
  whole subtree, not "this directory", and at the repo root it degenerates to `**` — every file
  in the repository.
- **Anchors.** `//path` is absolute from the filesystem root; `/path` is relative to the
  *settings source*; a bare relative pattern anchors at the working directory. A rule for a file
  outside the repo therefore needs `//`, or it resolves under the project root and never matches.
- **Generated rules are escaped.** When the CLI turns an approved path into a rule it escapes
  gitignore metacharacters so the rule "matches only the literal path you approved". Without it a
  directory named `[2024-06] Reports` yields a rule that fails to match its own path.

#### What the CLI actually suggests

Observed against CLI 2.1.229 by denying every request and logging `context.suggestions` verbatim:

| Request | Suggestion |
|---|---|
| `Edit` on an in-repo file | `setMode` → `acceptEdits`, `destination: "session"`. **No rule at all.** |
| `Read` on a file outside the working directory | `addRules` → `Read(//home/<user>/**)`, `destination: "session"` |
| `Bash` on a compound command | `addRules` → `Bash(<the literal sub-command needing approval>)`, `destination: "localSettings"` |

Three consequences worth stating, because each contradicts a reasonable guess:

- **A file-modification always-allow is a mode switch, not a rule**, and it is not persisted —
  the published tier table gives its lifetime as "until session end" and says outright that the
  approval "isn't saved to the file". So a dialog that only renders `addRules` suggestions shows
  the user nothing the CLI offered for a write.
- **`destination: "session"` is normal, not an edge case.** Any dialog copy that denies the
  existence of session-scoped grants is false.
- **Persisted rules go to `localSettings`**, `.claude/settings.local.json` at the git root — not
  to `projectSettings`. AC⚡DC's derived rules follow the CLI here (CC-16): `localSettings` is the
  default, and `projectSettings` is reachable only from the extra, `shared`-tagged menu row. A
  destination the CLI names is used exactly as named, `session` included.

#### How AC⚡DC answers a mode suggestion

A `setMode` suggestion is **offered on its own control**, never folded into "always allow". The
reason is the size of the grant: `acceptEdits` stops the dialog opening for every later edit in
the session, so a user who clicks a button labelled for *this call* would silence the gate for
calls they never saw. The mode is applied by returning a `setMode` `PermissionUpdate` on the
allow — not by a separate `set_permission_mode` control request, which would deadlock: the CLI is
waiting on the permission response and will not service another control request until it lands.

Two constraints on which modes may be offered:

- Only modes AC⚡DC holds copy for, so the control can state what stops being asked. An
  unrecognised mode is logged and dropped rather than rendered with a generic label.
- **Never `bypassPermissions`**, at either end — not in the offer table, and re-checked when the
  update is built. A dialog button is precisely the accident the no-bypass rule exists to prevent.

The mode a decision applies is read from the request the broker built, never from the decision
that comes back over the wire. `resolve_permission` is localhost-only, but a mode is a
session-wide grant, and the offered mode is the only trustworthy statement of which one was on
screen.

The CLI applies the update **without announcing it on the message stream** — `permissionMode`
appears only in the `init` system message. Anything caching the session's mode must therefore be
told separately, or the mode selector goes on reporting the mode the session started in.

#### Derived rules for shell commands

When the CLI offers no suggestion of its own, AC⚡DC derives two, **narrowest first**:

| Order | For `git push origin main` | Grants |
|---|---|---|
| 1 (default) | `Bash(git push origin main)` → `localSettings` | exactly what the dialog showed, on this machine |
| 2 (menu) | `Bash(git push:*)` → `localSettings` | every `git push`, including `--force` |
| 3 (menu, tagged `shared`) | `Bash(git push origin main)` → `projectSettings` | what the dialog showed, for everyone who pulls the commit |

Row 3 exists once per dialog, not once per rule, and it always carries the *narrowest* content: the
wider grant and the wider audience must not arrive on the same click. It is built only for rules
AC⚡DC derived — a CLI suggestion keeps the destination the CLI chose, since promoting its
`session` suggestion to a committed rule would invent a persisted grant it declined to ask for.

The literal command is the default because it is what the CLI derives and because the prefix
pattern grants more than the user looked at. The prefix stays available as a deliberate choice
from the rule menu: it is legitimate syntax and the right thing to write by hand, and the wrong
thing to put behind the button someone reaches for without reading it.

The command is stripped but not otherwise normalised. Collapsing its internal whitespace would
produce a rule that does not match the command it came from — the same silent no-op as naming the
wrong tool in a path rule.

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
    destination="localSettings",
)
```

- `type` ∈ `addRules`, `replaceRules`, `removeRules`, `setMode`, `addDirectories`, `removeDirectories`.
  AC⚡DC writes `addRules` only.
- `behavior` ∈ `allow`, `deny`, `ask`.
- `destination` ∈ `userSettings`, `projectSettings`, `localSettings`, `session`. Default for "always
  allow" is `localSettings` (`.claude/settings.local.json`, git-ignored) — the file the CLI persists
  its own approvals to (CC-16). `projectSettings` is offered as one extra menu row, tagged `shared`,
  because a git-tracked grant reaches every checkout that pulls it. AC⚡DC never *chooses* `session`
  for a derived rule — an invisible in-memory grant is what the parent spec forbids — but it does
  pass one back unchanged when the CLI suggested it, which for reads outside the cwd is the common
  case.
- **No derived rule may name a path under `.claude/`.** `Edit(.claude/settings.json)` is a permission
  to grant permissions: with it the agent can write `"Bash(*)": "allow"` into its own gate. The check
  is on a path component, so `.claude-notes.md` is unaffected, and it holds for absolute paths too —
  `~/.claude/settings.json` is the same escalation one directory further out. Such a call is still
  approvable once; it simply never becomes standing.
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
    tool_class: "read" | "write" | "exec" | "delegate" | "interact" | "plan" | "mcp"
    gated_by_default: bool             // whether this class is gated in `default` mode
    input: object                      // full tool input, verbatim
    summary: string                    // one-line human summary, ours
    blocked_path: string | null
    decision_reason: string | null
    title: string | null               // the CLI's own prompt sentence; preferred over `summary`
    display_name: string | null        // the CLI's short noun phrase for the action
    description: string | null         // the CLI's subtitle
    suggested_rules: list[SuggestedRule]
    suggested_mode: SuggestedMode | null   // the CLI's setMode suggestion, if it made one
    diff: DiffPayload | null           // present for tool_class == "write"
    command: CommandPayload | null     // present for tool_class == "exec"
    question: QuestionPayload | null   // present for tool_class == "interact"
    plan: PlanPayload | null           // present for tool_class == "plan"
    expires_at: string                 // ISO 8601 UTC; drives the dialog countdown
    localhost_available: bool          // false ⇒ dialog explains the short timeout

SuggestedRule:
    label: string                      // rendered on the "always allow" control
    tool_name: string
    rule_content: string | null
    behavior: "allow" | "deny" | "ask"
    destination: string
    origin: "cli" | "derived"          // cli ⇒ came from context.suggestions
    shared: bool                       // true ⇒ writes the git-tracked file; the row is tagged

SuggestedMode:
    mode: string                       // only modes the engine holds copy for; never bypassPermissions
    destination: string                // "session" — a persisted mode would outlive the session
    label: string                      // rendered on the mode control
    detail: string                     // what stops being asked, for the control's tooltip

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

PlanPayload:
    plan: string                       // the whole plan, markdown, never truncated
    headline: string                   // its first line of prose, `#` stripped, capped at 120
    file_path: string | null           // planFilePath, when the CLI names the file it read
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
    action: "allow" | "allow_always" | "allow_mode" | "deny" | "deny_interrupt"
    reason: string | null              // required for deny actions
    rule_index: integer | null         // index into suggested_rules, for allow_always
    updated_input: object | null       // when the user edited the input; Bash/Write/NotebookEdit only
    answers: list[Answer] | null       // interact only: one entry per question, in order

Answer:
    options: list[integer]             // chosen option indices
    text: string                       // the freeform reply, "" when the user typed none
```

An `Answer` may also arrive as a bare `list[integer]`, which is the shape the browser sent before the
freeform reply existed. It is read as `{options: […], text: ""}`, so a client mid-upgrade still answers
correctly rather than having its selections dropped.

`allow_mode` carries **no mode name**. The engine applies the mode from `suggested_mode` on the
request it built; a decision that could name its own mode could name `bypassPermissions`.

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

**`response` must stay unset.** The result mapping tests it *before* it looks at `answers` — an
`else if (response?.trim())` branch yielding `The user responded: …` that pre-empts the answers branch
entirely — so a decision that set `response` for the freeform reply would report that reply and silently
discard every option the user picked alongside it, on every other question in the call too. The terminal
itself never sets it; the field exists for a caller that has nothing but prose. AC⚡DC therefore routes
the freeform reply through `answers[<question text>]` like any other answer, and
`test_no_response_key_is_ever_written` pins it. Nor is the reply a distinct kind of answer to the CLI:
the tool's own schema instructs the model not to write an "Other" option *because the front end provides
one*, so prose in the answers map is the intended path, and the "read this carefully" prefix above is the
CLI's own handling of it.

Three rules the CLI enforces that the map has to respect:

- **The key is the question text**, exactly as the tool was called with it — not a normalisation of ours.
  `build_question_payload` fills a missing question text from `header`; `build_answer_input` therefore
  keys off the verbatim `tool_input["questions"][i]["question"]` and only falls back to the normalised
  text.
- **Multi-select is one string joined with `", "`.** The CLI splits on exactly that separator to check the
  parts back against the option labels.
- **A freeform reply is combined by the same rules.** For a single-select question it *replaces* the label,
  because "Other" is one of the choices in a radio group rather than an addition to it; for a multi-select
  it is one more item in the joined list. Both fall out of the split-and-check above: an item that matches
  no label is passed through as prose.
- **A question with no key is a question the user declined**, which is a legitimate state — an empty
  answer counts as covered. So a partial answer set is deliverable, and the browser's rule that "Answer"
  waits for every question is a UI choice, not a protocol requirement.

The mapping from indices to labels lives on the engine side for two reasons. The engine already holds the
verbatim tool input, so the key it writes cannot drift from what the tool was called with; and
`updated_input` being present on a decision is what marks a call as user-modified in the transcript.
Answering a question the agent asked is not modifying the call it made, so the browser sends
`answers` — indices and typed text — and never builds the patch itself.

### `ExitPlanMode` — approving a plan

Verified against the bundled CLI 2.1.229:

- Input: `plan` (markdown) plus `planFilePath`. **`plan` is optional in the schema** — the CLI's own comment says it is "injected by `normalizeToolInput` from disk", `planFilePath` naming the file — so a call with no plan text is a real case rather than a malformed one, and the payload is `null` for it.
- The plan travels whole. Unlike `CommandPayload.command` there is no cap: what is being approved *is* the text, so a truncation would be an approval of something unread. The 4 000-char cap applied to this tool for exactly as long as `classify_tool` had no entry for it and it fell through to `exec`.
- `headline` is the first non-blank line with leading `#` stripped and a 120-char cap, used for the dialog header and the screen-reader announcement. It is a convenience, not a summary: the body renders the whole plan regardless.
- No `PermissionUpdate` is derived. A standing `allow` for `ExitPlanMode` would approve every later plan sight-unseen, which is the one thing the dialog exists to prevent, and the CLI does not suggest one either.

**Approving a plan changes the permission mode silently.** The CLI's own handler sets the session mode to
`prePlanMode ?? "default"` when `ExitPlanMode` is approved, and emits nothing on the stream to say so —
the same silence the `note_mode` callback exists to cover for `setMode` suggestions. An engine that does
not account for it leaves every client's mode selector claiming `plan` while writes are in fact being
gated as `default`. Not yet handled: the target mode is whatever the session was in before plan mode, and
the SDK does not expose `prePlanMode`, so the engine would be guessing rather than reporting.

### `permissionResolved(data)` — server → browser (broadcast)

```pseudo
PermissionResolvedPayload:
    permission_id: string
    request_id: string | null
    action: string                     // as above, plus "timeout"
    reason: string | null
    resolved_by: string                // client id, or "timeout"
    rule_written: SuggestedRule | null
    mode_set: string | null             // the mode the decision switched the session to
```

Closes the dialog on every other client, including non-localhost observers, and gives them the
attribution note. `mode_set` is there so a second window can say *why* its dialogs stopped
appearing rather than looking broken; a decision that sets it is also followed by a
`permissionModeChanged` broadcast, because the CLI applies the mode silently.

Every action in `ALLOW_ACTIONS` — `allow`, `allow_always`, `allow_mode` — means the call went
ahead. Anything else is a denial. Consumers must test membership rather than `== "allow"`: the
transcript renderer compared against `"allow"` alone and marked calls approved with "always allow"
as *denied*, with the amber lock and a denial body, on calls that ran.

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
| `plan` | `ExitPlanMode` | Yes |
| `mcp` | any `mcp__*` tool from a server other than `ac-dc` | Yes |

An unrecognised tool name classifies as `mcp` when it matches `mcp__*` and as `exec` otherwise —
unknown-and-gated is the safe default, and a new built-in tool appearing in a CLI upgrade must not
arrive ungated.

Safe is not the same as honest, though, and `ExitPlanMode` is the demonstration: it was missing from this
map, so it arrived gated — correctly — as an `exec` call, and the dialog asked the user to approve a
"command" that was a truncated markdown essay. The fallthrough buys time to add a class; it does not
substitute for adding one. A built-in tool whose dialog reads as a different kind of act than it is
belongs in this map before it ships.

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
