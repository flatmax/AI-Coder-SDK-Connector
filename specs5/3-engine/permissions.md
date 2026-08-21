# Permissions

When Claude Code wants to use a tool that needs approval, the request surfaces as a dialog in the
browser with the actual consequence rendered — a Monaco diff for an edit, the command and working
directory for a shell call. This is the single most important surface the conversion adds, and the
main reason a browser frontend beats a terminal for this workflow.

## The Mechanism

The session is configured with an async `can_use_tool` callback. When it fires, AIC⚡DC:

1. Assigns the request an ID and broadcasts `permissionRequest` to all clients.
2. Awaits a decision from a **localhost** client.
3. Returns an allow or deny result to the SDK.

The RPC pair is the first genuinely awaitable server→browser call in the system: every other
server-push event is fire-and-forget. The browser answers with a matching
`resolve_permission(request_id, decision)` call rather than an RPC return value, so the transport
stays fire-and-forget in both directions and the server side is a future keyed by request ID. See
[`../../specs-reference/3-engine/permissions.md`](../../specs-reference/3-engine/permissions.md) for
the payload shapes.

## `can_use_tool` Is Not a Display Channel

The callback fires **only when the permission flow falls through to a prompt**. Calls approved by
settings allow rules, by `permission_mode` (`acceptEdits`, `bypassPermissions`, `dontAsk`), or by a
`PreToolUse` hook returning `allow` never reach it.

This means the callback cannot be used to show the user what the agent is doing. Tool *display*
comes from the message stream and the `PreToolUse` hook, which always fire; the callback is strictly
the ask path. Conflating the two produces a UI that silently stops showing file writes the moment a
user switches to `acceptEdits` — nothing errors, the transcript just goes quiet.

Corollaries:

- AIC⚡DC does not set `allowed_tools`. Allow rules approve before the callback runs.
- AIC⚡DC's hooks never return a `permissionDecision`. The SDK emits `CanUseToolShadowedWarning` when
  a hook pre-empts the callback; if that warning appears, a hook has overstepped.
- Some tools always reach the callback regardless of mode: `AskUserQuestion`, MCP tools marked as
  requiring user interaction, and organisation-`ask` connector tools. In `dontAsk` mode these are
  denied without invoking the callback.

## Dialog Content by Tool Class

The dialog is tiered by consequence, not by tool. A dialog that appears for every `Read` teaches
users to click Allow without reading, at which point it costs attention and grants authority — the
documented failure mode of every permission UI.

| Class | Tools | Default posture | Dialog shows |
|---|---|---|---|
| **Read-only** | `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `aic-dc` index tools | Displayed, not gated | — (tool card only) |
| **File mutation** | `Edit`, `Write`, `NotebookEdit`, `MultiEdit` | Gated | Path, and the proposed change as a **rendered Monaco diff** — the same viewer used everywhere else in the app |
| **Execution** | `Bash`, `BashOutput`, `KillShell` | Gated | The exact command, the working directory, and a note when it appears to write, network, or delete |
| **Delegation** | `Task` | Displayed, not gated | Subagent type and prompt summary |
| **Interaction** | `AskUserQuestion` | Always gated by the SDK | The question and its options, rendered as real choices, each with a freeform reply |
| **Plan** | `ExitPlanMode` | Gated | The proposed plan, **rendered as markdown and never truncated** |
| **MCP (third-party)** | Anything from a non-`aic-dc` MCP server | Gated | Server name, tool name, full input |

Read-only calls being ungated by default is a deliberate trade. The alternative — gating reads —
produces the click-through failure, and a read of a file inside the repo the user opened is within
the authority the user granted by opening it. Users who want reads gated set a deny or ask rule in
project settings; the file picker's deny-read gesture — shift+click on a row, or its context menu —
writes exactly such a rule (see [decisions § CC-14](../plan/decisions.md#cc-14), which repurposed the
checkbox's third state into it, and [CC-21](../plan/decisions.md#cc-21), which moved it onto the row
when the checkbox went).

**"Ungated" is two different mechanisms, and only one of them is ours.** `Read`, `Glob`, `Grep`,
`WebFetch`, `WebSearch` and `Task` are ungated because the CLI never raises a permission request for
them — `can_use_tool` is not called at all, so nothing in this app decides anything. The `aic-dc`
index tools it *does* ask about: in `plan` mode it allows them itself, but in `acceptEdits` and
`default` it asks, and an unanswered ask is a denial. So `can_use_tool` early-returns
`PermissionResultAllow()` for any `mcp__aic-dc__*` tool, before a payload is built or a request is
broadcast, and without recording a prompt against the turn.

`classify_tool` returning `"read"` for those tools is *not* what ungates them — it only shapes a
dialog's wording, and by the time it is consulted the request already exists. The distinction is
worth stating because getting it wrong is invisible in `plan` mode and produces a dialog per
`symbol_map` call everywhere else, which is the click-through failure arriving through prompts that
should never have existed. Pinned by `TestOurOwnToolsAreUngated`, including the case that keeps it
narrow: `mcp__aic-dc-plus__*` is somebody else's server and is still gated.

The allow is safe because the tools are read-only by construction — closures over index objects this
process already holds, on an in-process server with no third party who could change its tool list.
It does not extend to a third-party MCP server that happens to be read-only; those stay gated,
because their inventory is outside our control.

### The diff is the feature

For file mutations, the dialog fetches the current file content and renders the proposed result as a
diff in a Monaco instance, using the same configuration as the main diff viewer. The user is
approving a visible change, not a tool name and a JSON blob. This is what a terminal cannot do, and
it is the reason the permission surface is specified before anything else is built
([`../plan/README.md § Ordering constraints`](../plan/README.md#ordering-constraints-that-are-not-obvious)).

Where the proposed change cannot be rendered as a diff — a new file, a notebook cell, a binary — the
dialog falls back to showing the full new content with a clear "new file" or "binary" label rather
than an empty diff.

## Decisions

| Decision | Effect |
|---|---|
| **Allow once** | Approves this call only. |
| **Deny** | Rejects with a reason the agent receives, so it can adapt rather than retry blindly. The dialog offers a free-text reason. |
| **Always allow this** | Writes a scoped rule into the project's permission settings via a `PermissionUpdate`, then allows. |

"Always allow" is scoped to a tool-plus-pattern rule — `Bash(npm test:*)`, `Edit(src/**)` — never a
bare tool grant, and never an invisible in-memory grant. It lands in a file the user can read and
revoke, and it applies to the CLI in the same repo too, which is the honest consequence of
`setting_sources` including the project.

Denials carry a reason because a denial without one produces an agent that retries the same call.
The reason is the cheapest possible steering mechanism.

## Permission Mode

The mode is a first-class UI control, surfaced in the chat panel's action bar and in Settings, and
switched live via `set_permission_mode()` without reconnecting.

| Mode | Meaning as presented to the user |
|---|---|
| `default` | Ask before writing files or running commands. |
| `plan` | Read and reason only. No writes, no commands. Good for "explain this" and "what would you change?" |
| `acceptEdits` | Writes proceed without asking; commands still ask. |
| `dontAsk` | Nothing asks. Interaction-required tools are denied rather than prompted. |
| `bypassPermissions` | Nothing asks and nothing is denied. Presented with an explicit warning and never the default. |
| `auto` | Engine-chosen posture per call. |

The active mode is always visible, not buried in settings — a user who cannot tell at a glance
whether the agent can write files has lost the thread. Mode changes are broadcast to all clients and
recorded in the transcript as system events, so a collaborator sees that the posture changed.

## Collaboration and Authority

Permission requests resolve against localhost clients only. Non-localhost participants see the
request and its outcome but cannot answer it.

This follows the existing restriction policy rather than inventing a new one, and it is the highest
stakes application of it: `can_use_tool` authorises arbitrary `Bash`. A remote participant able to
answer it would make collaboration mode a remote-code-execution grant.

When no localhost client is connected, a request cannot be answered. It is denied after a short
deadline, with a reason recorded in the transcript naming the cause. A headless AIC⚡DC therefore
cannot be driven into running commands by a remote collaborator — it degrades to something like
`plan` mode rather than to something permissive.

Concurrent localhost clients race: the first decision wins and the dialog closes on the others with
a note saying who answered.

## Waiting for an Answer

**A request has no deadline while a localhost client is connected to answer it.** Gating is gating:
the CLI is holding a complete assistant message and cannot issue its next API call until a tool
result exists, so a request that waits consumes nothing — no API request is open, no cache is being
kept warm, nothing accrues. A wall-clock limit on the answer therefore buys nothing, and it costs
the case that matters most: the user walks away, comes back, and finds the request was denied on
their behalf by a timer. A request outlives a coffee break.

The escape hatch is a person, not a clock. A dialog nobody wants to answer is closed by Stop, which
denies the turn's open requests before it interrupts, and the end of a turn sweeps anything still
open. Both denials say which happened, so the agent can tell it was not refused on the merits.

**The one deadline left runs only when nobody who could answer is there.** A remote collaborator
cannot grant permissions, so a session whose only participants are remote is not thinking about it —
it is unattended, and a fast deny beats a stalled turn. Presence is therefore re-sampled for the
life of every request rather than once at the start, because the answer changes in both directions:
the last localhost client leaving arms the clock, and one connecting inside the window cancels it.
Each arm and disarm is broadcast as `permissionDeadline` — session-wide, not turn-scoped, because a
request outlives the moment it was raised — so a dialog never shows a countdown that is not running or
hides one that is.

A request raised while nobody is connected starts out counting down, which is what the payload's
`expires_at` and `localhost_available` say. Both are live for the life of the request.

## Related Hooks

`PermissionRequest` is the right hook for observing that a permission is being asked for; it carries
no shadowing risk, unlike `PreToolUse`. AIC⚡DC uses it for the debug view and for the
prompts-per-turn metric that tells us whether the tiering above is working (see
[risks § R-12](../plan/risks.md#r-12--the-permission-dialog-becomes-a-click-through)).

## Invariants

- `can_use_tool` is the only ask path; tool display never depends on it firing.
- No AIC⚡DC hook ever returns a `permissionDecision`; `CanUseToolShadowedWarning` never appears in
  our logs.
- `allowed_tools` is never set by AIC⚡DC.
- Every permission request resolves exactly once — by a localhost decision, by a stopped turn, by
  the end of its turn, by the no-localhost deadline, or by session teardown — and the SDK always
  receives a result.
- No dialog outlives its turn. Stop denies the turn's open requests before it interrupts, and a turn
  that ends any other way sweeps whatever is left.
- No request has a wall-clock deadline while a localhost client is connected to answer it, and a
  request that acquires one loses it again when a localhost client returns.
- Only localhost clients can resolve a request; a non-localhost attempt is rejected and logged.
- With no localhost client connected, no permission request is ever allowed.
- Every file-mutation dialog shows the proposed change as a diff or, where a diff is impossible, the
  full new content with an explicit label. It never shows only a tool name and raw input.
- "Always allow" always writes a visible, scoped rule; there are no invisible grants.
- A denial always carries a machine-readable reason.
- The active permission mode is visible in the UI at all times and is broadcast to every client on
  change.
