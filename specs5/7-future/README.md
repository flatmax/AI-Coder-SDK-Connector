# Future

Speculative designs, **not for implementation**. Nothing in this layer is a commitment, and no other
layer may depend on it.

The layer was emptied by the Claude Code conversion. Four designs lived here; three were implemented by
the platform we adopted and one was deleted along with the subsystem it optimised. That is worth
recording rather than quietly deleting, because "we planned this and then got it for free" is the
strongest available evidence for the conversion's premise — see
[`../plan/README.md`](../plan/README.md).

## Retired Because the Platform Implements It

| Design | Superseded by | Notes |
|---|---|---|
| `parallel-agents.md` | The `Task` tool and `.claude/agents/` definitions | Subagent spawning, per-agent context, and lifecycle are engine features. The design's hardest problems — cross-turn agent identity, positional-versus-named addressing, per-agent archive routing — do not exist, because SDK agent IDs are stable. See [decisions § CC-8](../plan/decisions.md#cc-8--subagents-are-claude-codes-task-tool-not-acdcs-spawn-blocks) and [`../3-engine/history.md § Subagent transcripts`](../3-engine/history.md#subagent-transcripts). |
| `reasoning.md` | `effort` and `thinking` options; `ThinkingBlock` in the stream | Reasoning depth is a session option and thinking is a first-class message block, rather than a per-provider parameter matrix we maintain. |
| `mcp-integration.md` | [`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md) | Inverted, and shipped. The speculative design had AC⚡DC *consuming* MCP servers; the conversion makes AC⚡DC *be* one, and third-party servers arrive through project settings without our involvement. See [decisions § CC-6](../plan/decisions.md#cc-6--the-indexes-reach-claude-code-as-mcp-tools-not-as-prompt-text). |
| `cache-tiering-piggyback-promotion.md` | Nothing — deleted | It optimised the four-tier stability cache, which no longer exists. Claude Code owns prompt caching. |

## Genuinely Future

Unordered, and none of it scheduled. Each entry names the concrete SDK surface it would build on, so a
future reader can tell whether the idea has become cheap.

- **Manual compaction.** Auto-compact is engine-owned and its boundaries are rendered
  ([`../3-engine/history.md § Compaction`](../3-engine/history.md#compaction)). A deliberate "compact
  now" affordance before a long task is a plausible addition; the open question is whether users can
  judge the right moment better than the engine can.
- **Undo at all, and then undo beyond files.** Two SDK limits stack here. The first is that
  checkpointing cannot coexist with a `session_store`, so a mirrored session — every session with a repo
  — has no checkpoints to rewind to and git is the undo
  ([decisions § CC-20](../plan/decisions.md#cc-20--the-mirror-wins-over-file-checkpointing-undo-is-gits-job)).
  `test_the_sdk_still_refuses_the_pair` is the watch on it: when that test fails, the SDK has learned to
  do both and the affordance can be built. The second limit outlives the first — `rewind_files()`
  restores files but does not rewind the conversation, so after an undo the model still remembers what it
  did. A true "go back to before this message" needs conversation rewind, which the SDK does not expose.
  Until it does, the affordance must keep saying *files* and nothing more.
- **Adopting terminal sessions.** `import_session_to_store()` can replay an existing
  `~/.claude/projects/…` transcript into our store, which would let the history browser show and resume
  sessions started in the CLI. Cheap to build; the design question is scoping — a repo's worth, or
  everything the user has ever run.
- **Structured turn results.** `ResultMessage.structured_output` allows a turn to return typed data.
  The commit-message generator and the document converter's classification step are the obvious
  candidates, both of which currently parse prose.
- **Skills and plugins surfaces.** The SDK exposes `skills` and `plugins` options, and the Context tab
  already reports their token cost. A UI for authoring and toggling them is a natural extension, and a
  more honest home for repo conventions than a system prompt would have been.
- **Budget governor.** `max_budget_usd` and `TaskBudget` allow hard stops. Under subscription billing
  cost is unreported, so a budget UI is mostly meaningless today; under API billing it would be the
  first thing to build. See [risks § R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap).
- **Delegated permission authority.** Permission requests resolve against localhost clients only
  ([`../3-engine/permissions.md § Collaboration and authority`](../3-engine/permissions.md#collaboration-and-authority)),
  which means a remote pair-programming participant cannot approve anything. A future design could let
  a localhost host grant a named participant time-boxed, per-tool-class authority. This is the highest
  stakes idea in this file: `can_use_tool` authorises arbitrary `Bash`, so anything less than explicit,
  revocable, per-tool delegation turns collaboration into remote code execution.
- **Multi-repo sessions.** One session per repo is the current model, and `cwd` is the repo root. Adding
  directories via `PermissionUpdate(type="addDirectories")` would allow cross-repo work; the file tree,
  the diff viewer, and the indexes all assume a single root, so the cost is in the browser, not the
  engine.

## Convention

An entry graduates out of this layer by being specified in a numbered layer with a behavioural
contract and invariants. An entry that has been overtaken by the platform moves into the table above
rather than being deleted, so the record of what we chose not to build survives.
