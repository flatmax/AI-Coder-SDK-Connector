# Second Engine — Google Antigravity alongside Claude Code

**Status:** Phases 0–2 done; the consultant ships and the engine spike streams a turn. This
directory is the plan of record for adding Google's Antigravity SDK as a second agent backend beside
the existing Claude Code engine.

[`../plan/`](../plan/) is the finished conversion that produced the current single-engine build. This
directory is a *different piece of work* and does not extend it: its decisions are `AG-n`, its risks
are `AG-R-n`, and where it needs a fact about the existing engine it cites the code rather than
restating that directory's reasoning.

## Where we are (2026-09-01)

**Phases 0, 1 and 2 are done; phase 3's code is built, phase 4's permission gate has landed, and the
engine router is wired into startup.** Neither the engine spike nor the gate has been run against the
network. The
surface of both Antigravity products was read first-hand — the installed `google-antigravity` 0.1.15
wheel, and the `agy` 1.1.22 binary — and live turns were run against both to measure what reflection
could not see. **Phase 2 was taken out of order**, ahead of phase 1, because it was the gate
everything else was contingent on and it turned out to be cheap. The results are in
[`sdk-surface.md`](sdk-surface.md); the choices they force are in [`decisions.md`](decisions.md);
what each phase actually did is in [`delivery.md`](delivery.md).

**What you can use today.** The consultant is wired into the running server as of 2026-09-01: with a
Gemini key in `~/.config/aic-dc/gemini-api-key`, an ordinary Claude turn can call
`mcp__aic-dc-antigravity__second_opinion` and `mcp__aic-dc-antigravity__generate_image`. Ask Claude
for a second opinion and it reaches Google's model; both calls go through the permission dialog,
because they mount on their own server rather than the ungated index one. With no key the tools are
absent rather than broken. Everything else below is still infrastructure: the second engine cannot
yet be *master*, because nothing chooses one per session.

Two things are outstanding and neither is a code problem. **Image generation is unfunded** — every
Gemini image model reports `limit: 0` on a free-tier key, so AG-1's worked example is behind a
billing account on the key's Cloud project ([AG-12](decisions.md#ag-12)). And **phase 3's smoke test
has not been run against the network**, because doing so spends free-tier quota that a rate limit
makes non-trivial to spend well.

Seven findings determine the shape of everything below. The first five are phases 0–2; the last two
are what building the pump turned up.

**There are two Antigravitys and they are not the same program.** The Python SDK bundles and spawns
its own 119 MB Go binary; the `agy` CLI is a separate 208 MB product that shares no symbols with it.
They have separate state directories and, decisively, **separate authentication**: `agy` is
OAuth-authenticated against the owner's account, while the SDK accepts only a Gemini API key or a
Vertex project and contains no OAuth code at all. The engine cannot borrow the login that already
exists.

**`agy` looked like the better transport and is disqualified.** Its flag surface is a startlingly
close analogue of the `claude` CLI — `--continue`, `--conversation`, `--output-format stream-json`,
`--input-format stream-json`, `mcp`, `plugin`, `agents` — and its NDJSON protocol is real and typed.
Two measurements ruled it out. Headless mode **structurally cannot prompt for permission**: the three
postures are auto-deny, static allowlist, and blanket bypass. And **tool content is not on the wire**:
a `write_to_file` frame names the target path and carries neither the bytes nor a result, so no diff
can be rendered from it. (Re-measured at 1.1.22, this is narrower than first recorded — tool
*results* are present in `tool_info.output`; only file content is missing. The correction is marked
in `sdk-surface.md`. It does not change the decision: the diff is the product, and the diff is what
is absent.)

**The seam is a router, and it mounts under the existing namespace.**
`src/aic_dc/engine_router.py` is registered in place of `ClaudeCodeService` as of 2026-09-01, under
the *same* RPC name, with its 48 delegating methods **generated** from the adapter's rather than
hand-written — jrpc-oo reads a service's method list off the class, so a `__getattr__` forwarder
would have exposed nothing at all, and a hand-written list drifts silently into methods that work in
Python and 404 on the wire. It routes to one engine today and says so through `list_engines`. The
change it nearly shipped with is recorded in [`delivery.md`](delivery.md): jrpc-oo injects the
server-push call proxy onto the *registered* instance, so reading it off the service behind the
router would have dropped every streamed chunk and every permission dialog behind one warning.

The router also answers a question that looked like an open design fork and was not: a method whose
surface the running engine cannot feed is **refused, not missing**. It stays on the wire and raises
`UnsupportedOnThisEngine`, because omitting it would give the browser a transport-level "no such
method" — indistinguishable from a broken build. That is [AG-9](decisions.md#ag-9) one layer down,
and `RPC_SURFACES` derives the refusals from the descriptor rather than from a second list that could
disagree with it. The practical effect: the Antigravity adapter needs **33** of the 48 methods, not
all of them, and `build_router(adapter, engine=ANTIGRAVITY)` prints exactly which.

**The Python SDK's core is a genuinely good fit** — better than expected. Streaming deltas, mid-turn
cancel, resume by `conversation_id`, an async permission hook that receives the full tool call,
post-tool-call hooks, compaction steps, subagents with per-trajectory usage, and — the pleasant
surprise — **plain Python callables as tools**, which makes the symbol-index bridge simpler here than
it is under Claude Code. The gaps are real but bounded, and they are gaps in *reporting*, not in the
loop.

**The permission gate passed, and it raised a harder problem than the one it closed.** The hook
receives the full proposed edit — `edit_file` hands over old text, new text and a line range, so the
dialog can render a diff without even reading the file — and denying it leaves the file untouched.
That was the contingency the whole plan hung on and it is now measured rather than hoped for. But the
same probe showed the agent, refused an `edit_file`, going straight for `run_command` to make the
same change by other means: `sed -i`, then inline `python3`, unprompted, on both runs. **A dialog
that gates only the file tools shows the user a diff, records their refusal, and lets the edit
through anyway.** The permission seam is therefore *all mutating tools*, `run_command` included —
[AG-5](decisions.md#ag-5), [AG-R-11](risks.md#ag-r-11).

**What does not translate is mostly accounting.** There is no USD figure anywhere on either
Antigravity surface, and no context-window read-back at all. The turn footer, the session cost, the
`max_budget_usd` cap and the Context tab's bar have no source data. In the other direction,
Antigravity offers image generation, agent-initiated structured questions, audio and video input,
daemon commands and out-of-band triggers, none of which has a home in the current UI.

**A tool's arguments and its result are the same object, and the stream is not the hook.** Claude
sends a `tool_use` block and later a separate `tool_result`. Antigravity sends the *same* typed
sub-message twice — at `ACTIVE` with the inputs, at `DONE` with the outputs filled in beside them —
so a pump that forwards `ToolCall.args` renders a card whose "input" grows a command's entire stdout
on completion, and emits no result at all. Worse for phase 4: the step stream's `edit_file` carries
`{file_path, diff_block}` while the *hook* carries `TargetContent` + `ReplacementContent` + a line
range, and `view_file` on the stream carries no content at all. **The diff the dialog renders comes
from the hook.** Details and the full field table in
[`sdk-surface.md` § The step stream](sdk-surface.md#the-step-stream--read-in-phase-3-and-it-is-not-shaped-like-claudes).

**The SDK's own `nondestructive()` is not a write boundary.** It excludes only `run_command`,
classifying `create_file`, `edit_file` and `generate_image` as nondestructive — defensible for "will
this hurt the machine", exactly backwards for "will this change the working tree". An adapter
adopting it would enable the two tools AG-5 exists for. `options.MUTATING_TOOLS` is therefore ours,
and it gained `start_subagent` during phase 3: a subagent inherits the tool set, so a gate that stops
at the top-level trajectory is bypassed by asking a child to do the write. Same hole as AG-R-11's
`run_command`, one level down.

Read [`sdk-surface.md`](sdk-surface.md) before touching anything. Read
[`decisions.md`](decisions.md) before reading the specs; they assume it.

## The one-paragraph version

AIC⚡DC gains a second agent backend. Exactly one engine is master per session and the other is
reachable as a consultant — a one-shot call for a second opinion, or for a capability the master
lacks, of which image generation is the worked example. The second engine is
`google.antigravity.Agent` driving its bundled `localharness`, **not** the `agy` CLI. It mounts under
the *same* RPC namespace as the first, so the browser's 43 call sites across 59 files do not fork,
and it reports which surfaces it supports through a capability descriptor so the ones with no
counterpart are **hidden rather than stubbed**. The first thing built is the consultant, because it
delivers the owner's own example in the smallest increment and forces the two questions that gate
everything else — a real API key, and whether the permission hook can render a diff — to be answered
with facts.

## Why a second engine

Three reasons, in order of weight:

1. **Disjoint capability.** Google offers image generation; Anthropic does not. That is not a
   preference between models, it is a thing one engine can do and the other cannot, and the
   consultant pattern makes it reachable without either engine giving up its own strengths.

   **Specified and built, but unverified and currently unfunded.** `generate_image` is implemented
   and tested offline; it has never returned an image, because every Gemini image model reports
   `limit: 0` on the free-tier key. That is not a throttle — the plan's allowance is zero, and no
   wait changes it. The fix is not a different key: the tier is a property of the key's Cloud
   project, so enabling billing on that project moves the *same key* to a paid tier and nothing in
   `credentials.py`, the resolution order or the key file moves ([AG-12](decisions.md#ag-12)). Until
   that happens, the argument for a second engine rests on reasons 2 and 3, which are both delivered.
2. **A second opinion is worth something.** Two independent agents disagreeing about a diff is
   information. One agent asked twice is not.
3. **Not being one vendor deep.** The engine layer's whole design is that AIC⚡DC renders an agent
   session rather than constructing one. That claim has never been tested against a second SDK, and
   an abstraction validated by one implementation is a naming convention.

## What AIC⚡DC still contributes

Unchanged from the single-engine build, and the reason [AG-5](decisions.md#ag-5) treats the
permission dialog as non-negotiable rather than as a feature to port:

- **Spatial code navigation** — a Monaco diff viewer over every file the agent touches, a git-status
  file tree, an SVG editor, a TeX preview.
- **Repo intelligence as tools** — the tree-sitter symbol and document indexes, reaching Antigravity
  as plain callables ([AG-4](decisions.md#ag-4)) rather than through MCP.
- **Permission UX with a diff in it** — the thing `agy` structurally cannot support, and the reason
  it is not the engine.
- **Multi-client collaboration**, and **documents** — both engine-agnostic already.

## Phases

Each phase is independently shippable and leaves the tree working. Phase 0 is this directory.

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Assessment** ✅ | This directory. Both surfaces read first-hand; three live `agy` turns. No code changes. | `sdk-surface.md` records the verified surface with file:line citations and raw captures; `decisions.md` records the choices it forces; unknowns are stated as unknowns. |
| **1. Consultant and probe** ◑ **(live)** | Antigravity as a one-shot tool under Claude Code — `generate_image` and `second_opinion`, on their **own** MCP server rather than the ungated `aic-dc` one ([AG-5](decisions.md#ag-5)). Credential resolution reporting its source, from the environment or the stored key file ([AG-11](decisions.md#ag-11)). `src/aic_dc/antigravity/surface.py` and its gate ([AG-8](decisions.md#ag-8)). | The agent generates an image from a Claude Code turn and it lands in the repo where the file tree and viewer find it. The probe's `unclassified` bucket is empty by declaration. A sentinel write lands at its expected absolute path ([AG-R-3](risks.md#ag-r-3)). **Two of three met (2026-08-31); the image is blocked on billing, not on code — every Gemini image model is `limit: 0` on a free-tier key, which is a known and accepted cost of [AG-12](decisions.md#ag-12) rather than an open defect. AG-R-3 settled: `workspaces` is honoured.** |
| **2. Permission gate** ✅ | `probe_edit_args.py` — a `PreToolCallDecideHook` logging its `ToolCall` and denying. Run out of order, ahead of phase 1, because everything downstream is contingent on it. | **Met — go.** The hook carries full edit content and `allow=False` blocks the write. [AG-R-1](risks.md#ag-r-1) retired; [AG-R-11](risks.md#ag-r-11) raised. `trustedWorkspaces` remains unsettled and moves to phase 1. |
| **3. Engine spike** ◑ | `src/aic_dc/antigravity/{options,steps,session}.py` — session lifecycle, options assembly, and the `Step` → `Event` pump. **Not registered:** there is no engine registry to register with, and inventing one against no second caller is phase 4's problem. | `probe_session.py` sends a prompt and prints the streamed step taxonomy, failing if no tool call and result arrive. **Built and asserted, not yet run live (2026-09-01)** — the offline half is 94 tests and the three SDK facts the pump is built on were read off the wheel. Three findings in [`sdk-surface.md` § The step stream](sdk-surface.md#the-step-stream--read-in-phase-3-and-it-is-not-shaped-like-claudes); the write seam gained `start_subagent`. |
| **4. Chat on the second engine** ◑ | The chat panel renders the Antigravity stream — text, thinking, tool cards, results. The permission dialog lands against the phase-2 mechanism. | A user holds a full working conversation, including edits, entirely through Antigravity, with every write approved through the dialog. **The gate and the adapter both landed (2026-09-01):** `permissions.py` drives the *shared* `PermissionBroker` — one ask path, one queue, one localhost rule across both engines — and `service.py` implements the 31 methods the router mounts, sharing the symbol index, `ReviewMode` and `commit.py` rather than copying them. **AG-1's per-session choice landed the same day** — `main.py` constructs both adapters, `app.json`'s `engines.master` names the one that starts, and `switch_engine` changes it mid-run; see [`delivery.md` § AG-1](delivery.md#ag-1--one-master-per-session-chosen-per-session-2026-09-01). Still missing: **no live turn has run through it**, which is now the whole of the gap. |
| **5. History and sessions** | Resume by `conversation_id`; a repo-local mirror rebuilt as a step observer rather than as a store implementation, since there is no `SessionStore` protocol to implement. **Its own store root**, so that a record written by one engine cannot be handed to the other ([AG-1](decisions.md#ag-1)). | Restarting the server resumes the previous Antigravity conversation with context intact, and the history browser renders it. |
| **6. Capability descriptor** ◑ | The descriptor of [AG-3](decisions.md#ag-3) made real, and every surface in § *What does not translate* given an entry. Per-engine hiding across the Context tab, HUD and settings. | No surface renders an empty or synthesised value for a fact its engine cannot report; no webapp branch keys off an engine name string ([AG-R-4](risks.md#ag-r-4)). **The data landed early (2026-09-01)** — `src/aic_dc/capabilities.py`, 13 surfaces, distinguishing `absent` (no source data, ever) from `unbuilt` (a to-do). Nothing reads it yet: the router must publish it and the panels must hide on it. |
| **6b. The consultation as an agent tab** | [AG-13](decisions.md#ag-13). The consultant streams `Conversation.receive_steps()` through the existing `StepTranslator`, tagging every event with a minted consultation id, and emits `subagentEvent` so the tab strip picks it up. Stop wired to `Conversation.cancel()`. Cost hidden via the descriptor. | A `second_opinion` call from a Claude turn opens its own tab, fills with thinking and text as Google produces them, can be stopped mid-flight, and shows tokens with no USD figure. **No webapp change** — if one is needed, the id contract has been got wrong. |
| **7. Packaging** | `google-antigravity` as an optional extra, not a base dependency — a second bundled binary on top of the ~295 MB CLI ([AG-R-10](risks.md#ag-r-10)). | A base install is a one-engine install with no broken UI, and its size has not moved. |

## Ordering constraints that are not obvious

- **The permission gate before the engine, not after.** Phase 2 is a gate rather than a task, and it
  is placed before phase 3 because it is nearly free to run and because the answer changes whether
  Antigravity can be master at all for write operations. Discovering it while building the dialog in
  phase 4 means an engine adapter written against an assumption.
- **The probe before the engine.** The SDK is 0.1.15 and alpha. Writing the adapter first means
  writing it against a snapshot that has already moved, which is the failure the equivalent Claude
  probe exists to close.
- **The consultant before anything.** It is the only phase that delivers user-visible value before
  the second-engine decision is committed to, and it forces the credential question to be answered
  with a real bill. What survives into phase 3 is narrow and known: config construction, credential
  resolution, the probe. [AG-R-9](risks.md#ag-r-9) is the boundary it must not cross.
- **Phase 6b after the descriptor, not before it.** The consultation tab is the descriptor's first
  real consumer: it must hide its cost panel because [AG-6](decisions.md#ag-6) says this engine
  reports no USD, and doing that by an engine-name check in the webapp is precisely what
  [AG-R-4](risks.md#ag-r-4) forbids. Built before the descriptor was readable from the browser, it
  would have grown exactly that branch.

- **The capability descriptor late, but specified early.** It cannot be built until there are two
  engines to describe, but every phase from 3 onward must record which surfaces it could not serve —
  otherwise phase 6 is an archaeology exercise. **Discharged on 2026-09-01**, immediately after
  phases 3 and 4 produced the list: `src/aic_dc/capabilities.py` holds it, and its `unbuilt` bucket
  is the to-do list as data rather than as memory.

## Reading order for this directory

1. [`decisions.md`](decisions.md) — the binding choices, each with its rationale. Read this first.
2. [`sdk-surface.md`](sdk-surface.md) — the verified surface of both Antigravity products, the raw
   protocol captures, what does not translate in either direction, and
   [§ The probe](sdk-surface.md#the-probe): the gate that keeps the inventory honest as an alpha
   package moves.
3. [`risks.md`](risks.md) — the register, with mitigations and the tripwires that say a risk has
   fired.
4. [`delivery.md`](delivery.md) — one entry per phase, written when its exit criterion is met.
   Currently empty.
