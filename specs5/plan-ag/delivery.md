# Delivery log — second engine

One entry per phase from [`README.md`](README.md#phases), written when the phase's exit criterion is
met. Each entry records what landed, what was deliberately left out, and what the next phase has to
do first — so a phase can be picked up cold without re-deriving the previous one's state.

Corrections to the specs found while implementing belong in the spec, not here. This file points at
them; it does not restate them.

---

## Phase 0 — Assessment (2026-08-30)

**Exit criterion:** *"`sdk-surface.md` records the verified surface with file:line citations and raw
captures; `decisions.md` records the choices it forces; unknowns are stated as unknowns."* Met.

### What landed

This directory. No code changes; `src/` and `webapp/` are untouched.

| File | Role |
|---|---|
| `README.md` | Purpose, phases, ordering constraints, reading order |
| `sdk-surface.md` | Both Antigravity products read first-hand, with raw protocol captures |
| `decisions.md` | `AG-1` … `AG-12` |
| `risks.md` | `AG-R-1` … `AG-R-10`, each with a tripwire |
| `delivery.md` | This file |

### Live verification

Three `agy` turns on 2026-08-30, `gemini-3.7-flash-low`, in a throwaway git repo under `/tmp`, at a
total of ~42k input tokens. They were run because the question that decided the whole transport
choice — does the stream carry an interactive permission channel? — is not answerable by reading a
stripped binary, and because the input frame schema is undocumented.

They answered that question and then answered a second one nobody had thought to ask. **The
permission finding demoted `agy`; the missing tool-result payload disqualified it.** A
`write_to_file` frame carries `TargetFile` and neither the bytes nor a result, so the diff viewer and
the permission dialog — the two things the product exists for — have no data. Details and captures in
[`sdk-surface.md` § Why `agy` is nonetheless not the engine](sdk-surface.md#why-agy-is-nonetheless-not-the-engine).

Two incidental findings from the same runs, both recorded because neither was reachable by
inspection:

- **An untrusted workspace diverts writes silently.** A file requested in the working directory was
  written to a scratch directory under `~/.gemini/`, and the agent reported success with a `file://`
  link. Cause: `trustedWorkspaces` in the CLI's own `settings.json`. Now
  [AG-R-3](risks.md#ag-r-3), with a tripwire that refuses to trust a tool's own success report.
- **The input frame schema is undocumented and non-obvious.** `{"event":"user","message":{…}}`, found
  from an error string after four wrong guesses. Recorded so nobody spends that time again, along
  with the `--print` flag-order quirk that eats `--input-format` as its prompt.

### What was deliberately left out

- **No code.** The probe, the consultant and the engine are phases 1–3. Writing any of them during an
  assessment would have produced an adapter shaped by whichever approach was being explored when the
  keyboard got warm.
- **No Python-SDK turn was run.** Every SDK claim here is read from source, because running one needs
  a Gemini API key that does not exist yet — which is itself the finding
  ([AG-R-8](risks.md#ag-r-8)). The credential wall is the reason phase 1 comes first.
- **No decision about strong symmetry** — both engines live in one session, sharing a working tree.
  It is scoped out by [AG-1](decisions.md#ag-1) and its blocker is recorded as
  [AG-R-7](risks.md#ag-r-7), but it was not designed against.

### What phase 1 has to do first

1. **Get a Gemini API key or a Vertex project.** Everything past phase 1 is gated on it, and it is
   procurement rather than engineering. `validate_endpoint()` raises on the connect path, so an
   engine without one fails at session start, not lazily.
2. **Build the probe before the consultant, not after.** The SDK is alpha; the consultant written
   first is the consultant written against a snapshot. Reflection targets are listed in
   [`sdk-surface.md` § The probe](sdk-surface.md#the-probe) and differ from the Claude probe's —
   pydantic fields rather than dataclass fields, enum members rather than `Literal` unions.
3. **Wire `agy`'s `init` frame as the CLI half of the probe.** It is free, it is the only
   machine-readable capability inventory either product offers, and it is the analogue of
   `diff_server_info`. `agy` is not the engine and is still the best inventory available.
4. **Assume nothing about the SDK's workspace containment.** Whether `workspaces` honours the CLI's
   `trustedWorkspaces` is unknown, and the sentinel-write check in phase 1's exit criterion exists to
   settle it rather than to assume it.

### The unknowns this phase did not close

Carried forward verbatim so none of them has to be rediscovered:

- Whether a `PreToolCallDecideHook` receives file **content** for `create_file`/`edit_file`. This is
  the phase-2 gate ([AG-R-1](risks.md#ag-r-1)) and the single most consequential open question in
  this directory.
- Whether the SDK's `workspaces` is subject to the CLI's `trustedWorkspaces` list.
- Whether `agy` has any supported programmatic contract, or whether `stream-json` is best-effort.
- How stable `Step` is across 0.1.x.
- ~~Whether `localharness` can be pointed at OAuth credentials by some path not exposed through the
  Python SDK.~~ **Closed in phase 1: no.** The harness's wire protocol has no OAuth field in any of
  its four endpoint shapes. [AG-R-8](risks.md#ag-r-8) stands, with its shape made precise — the wall
  is minting a token, not presenting one, since `base_url` + `http_headers` bypass the key check
  entirely. See [`sdk-surface.md` § Verified, inferred, unknown](sdk-surface.md#verified-inferred-unknown).

---

## Phase 2 — Permission gate (2026-08-30)

**Exit criterion:** *"Go/no-go, recorded either way. Either the hook carries file content and the
dialog can render a diff, or it does not and [AG-R-1](risks.md#ag-r-1)'s fallbacks are chosen from
explicitly before phase 3 begins."* Met — **go**.

Run out of order, ahead of phase 1, because it is the gate the rest of the plan is contingent on and
it turned out to be cheap. `src/` and `webapp/` remain untouched.

### What landed

| File | Role |
|---|---|
| `probe_edit_args.py` | The phase-2 spike. Seeds a file, requests an edit, logs every `ToolCall` at `pre_tool_call_decide`, denies all mutating tools, then asserts the file's bytes are unchanged and prints a verdict. |

`sdk-surface.md`, `decisions.md` and `risks.md` were amended with the measurements; the amendments
are marked as corrections rather than silently applied.

### The result

**The gate passed, with more margin than expected.** `edit_file` hands the host a complete diff
hunk — `TargetContent`, `ReplacementContent`, `StartLine`/`EndLine` — so the dialog does not even
need to read the file from disk to render one. `create_file` hands over `CodeContent`, the whole new
file. `HookResult(allow=False)` left the seeded file byte-identical, and the denial reached the model
as a message it read and adapted to. None of AG-R-1's fallbacks were needed.

### The finding nobody predicted

**Denying the file tools does not prevent the edit.** On both runs, `gemini-3.6-flash` responded to a
refused `edit_file` by going after the same change through `run_command` — `sed -i` on the first run,
inline `python3 -c` on the second, neither suggested by the prompt. The probe's first version gated
only the file tools and the file was modified anyway, by a tool card that looked unrelated.

This is now [AG-R-11](risks.md#ag-r-11) — critical, live, and mitigated in
[AG-5](decisions.md#ag-5) by defining the permission seam as *all mutating tools*. The tripwire
asserts on the **file's bytes after the turn completes**, not on the hook having fired, because a
hook-level assertion passes while `sed` is rewriting the file. That distinction is the whole risk.

### Corrections to phase 0

Re-measuring `agy` at 1.1.22 falsified part of what phase 0 recorded. Both are fixed in place and
flagged as corrections:

- **`agy` *does* carry tool results.** `tool_info.output` exists — `run_command` returns full stdout.
  Phase 0's "no result payload, no output, and no file content" was too broad. Only the file content
  is missing, and that alone is what disqualifies the transport. [AG-2](decisions.md#ag-2) stands on
  the narrower reason.
- **The frame shape is nested**, `{"event":"…","…":{…}}`, not the flat frames phase 0 quoted. A parser
  written against the old capture would have read `None` for every field without erroring.
- **A headless denial is no longer visible in the stream at all** — `DONE`, no `error` key,
  `CANCELED`, exit 0. Worse than phase 0 recorded, and stderr-only.

### Cost

Two `agy` turns (`gemini-3.7-flash-low`) against the owner's Antigravity OAuth quota, and three SDK
runs (`gemini-3.6-flash`) against a free-tier Gemini API key. The free tier throttles at 5 RPM and
both later SDK runs hit 429s *mid-turn* — an agent turn is many model calls, so the free tier is
adequate for probes and not for development. Tier 1 requires linking a billing account to the same
project; the key does not change, since *"rate limits are applied per project, not per API key."*

### What phase 1 has to do first

Nothing is blocked. Phase 1 proceeds as written, with two amendments now settled rather than open:
the permission hook's shape is known, and `run_command` must be gated alongside the file tools from
the first line of the engine adapter rather than retrofitted.

`probe_edit_args.py` needs a real key and hits the network, so it is a spike and not a test. Its
assertions should be lifted into the phase-1 probe as the AG-R-1 and AG-R-11 tripwires.

---

## Phase 1 — Consultant and probe (2026-08-31)

**Exit criterion:** *"The agent generates an image from a Claude Code turn and it lands in the repo
where the file tree and viewer find it. The probe's `unclassified` bucket is empty by declaration. A
sentinel write lands at its expected absolute path ([AG-R-3](risks.md#ag-r-3))."*

**Met except the image, which is blocked on billing rather than on code.** Every Gemini image model
returns `limit: 0` on a free-tier key — see § *The image wall* below. The two criteria that ask
questions about *this* build are both met and were measured live.

### What landed

| File | Role |
|---|---|
| `src/aic_dc/antigravity/surface.py` | The AG-8 probe. Six sections, 96 names, `diff_agy_init` as the CLI half |
| `src/aic_dc/antigravity/credentials.py` | AG-R-8 resolution, reporting its source; secret redacted in `repr` and absent from `report()` |
| `src/aic_dc/antigravity/consultant.py` | AG-7's one-shot `async with Agent(...)`, with AG-R-3 verification |
| `src/aic_dc/antigravity/bridge.py` | The two tools, on their **own** MCP server |
| `tests/test_antigravity_{surface,credentials,consultant,bridge}.py` | 145 tests, offline — no key, no harness, no network |
| `probe_consultant.py` | The live spike. Three checks, runnable, costs money |

### Live verification

`gemini-3.7-flash` on a free-tier key, 2026-08-31.

- **`second_opinion` answered.** AG-R-8 turned from a wall into a bill. One 503 "high demand" arrived
  mid-run and the SDK retried through it without the caller seeing anything, which is worth knowing
  before `retry_config` is tuned: the default already retries.
- **AG-R-3 is settled — the SDK honours `workspaces`.** Measured on the same machine whose
  `trustedWorkspaces` diverted `agy` in phase 0. Downgraded rather than retired; the reasoning is in
  [`risks.md`](risks.md#ag-r-3).
- **The probe's `unclassified` bucket is empty**, and `tests/test_antigravity_surface.py` is what
  keeps it that way.

### The image wall

`generate_image` could not be verified. All six image models the API lists for this key —
`gemini-2.5-flash-image`, `gemini-3-pro-image{,-preview}`, `gemini-3.1-flash-image{,-preview}`,
`gemini-3.1-flash-lite-image` — return HTTP 429 with **`limit: 0`** on the free tier. That is not a
throttle: the plan's allowance is zero and no wait changes it. Image generation needs a billing
account linked to the project.

Recorded as a phase-1 result rather than a defect because it sharpens
[AG-R-8](risks.md#ag-r-8): the credential wall is not one wall. A free key buys the text consultant;
**the capability [AG-1](decisions.md#ag-1) names as the reason for a second engine costs money.**
Google's own 429 says *"Please retry in 57s"* while also saying `limit: 0`, so an agent trusting that
message retries forever — which is why `_explain` distinguishes the two and says so.

### Three bugs the gates caught, and one they did not

- **The probe was too generous and said so.** It collected keyword names from every call and reported
  the `tools` config field — AG-4's route for the symbol index — as built, because `bridge.py` passes
  `tools=` to *Claude's* `create_sdk_mcp_server`. Two SDKs in one package means a bare keyword is not
  evidence about either; the readers are now scoped to the config constructors.
- **`policies` unset is not "no policy".** `LocalAgentConfig` defaults it to blanket approval. See
  [AG-5](decisions.md#ag-5), which is amended, and the test that pins the SDK's default so a release
  fixing it goes red.
- **The consultant tools do not go on the `aic-dc` MCP server.** `can_use_tool` early-returns an
  allow for `mcp__aic-dc__*`. A file-writing `generate_image` there would have routed a write around
  the permission dialog silently.
- **The one the tests missed: a lazy stream drained after teardown.** `agent.chat()` returns a cursor
  over a stream nothing has pulled, so `await response.text()` outside the `async with` read a dead
  connection and hung until killed — and the `asyncio.timeout` did not fire, because it wrapped
  *starting* the agent rather than the model work. Every mocked test passed, because the fake
  response answered whenever asked. The fake now dies with its agent; that, plus a timeout test that
  puts the slow part in `text()`, is the regression.

### What was deliberately left out

- **No engine, no session, no step pump.** Phase 3. The AG-R-9 boundary is enforced as a test that
  reads this module's own syntax tree for `receive_steps`, `cancel`, `conversation_id` and hook
  registrations, so the tripwire fails the build rather than being noticed in review.
- **No RPC surface and no UI.** The probe's report is shaped like the Claude one so a tab can render
  both (AG-3), but nothing serves it yet.
- **No `ConsultantBridge` wired into the running engine.** The tools are built and tested; nothing
  constructs one in `service.py`.

### What phase 3 has to do first

1. **Decide whether image generation is bought.** [AG-1](decisions.md#ag-1)'s worked example is
   behind a billing account. If it is not bought, the disjoint-capability argument for a second
   engine rests on the other two reasons and the README should say so.
2. **Move `NEVER_SET_CONFIG` into `options.py`** when one exists. `surface._declined_config` already
   prefers that module, so the move is a cut-and-paste and the refusal ends up beside the code doing
   the refusing.
3. **Gate `run_command` from the adapter's first line.** Unchanged from phase 2, and now with
   AG-5's amended reading: the static allowlist is for one-shot calls with no user in the loop, and
   the master engine's gate is still the raw `PreToolCallDecideHook`.
4. **Do not extend the consultant.** `_chat` is one function on purpose. Phase 3 writes against
   `Conversation` directly.

---

## Phase 3 — Engine spike (2026-09-01)

**Exit criterion:** *"A CLI-side smoke test sends a prompt and prints the streamed step taxonomy,
including a tool call and its result."*

**The code and the probe are built; the live run has not been made.** `probe_session.py` exists,
asserts the criterion and is runnable, and it has not been pointed at the network — the owner has not
yet decided whether to spend free-tier quota on it. Everything that does not need a key is done and
measured: the pump's behaviour is pinned by 46 offline tests, and the three SDK facts it is built on
were read off the wheel rather than inferred. Nothing here is blocked on the live run; it is the
demonstration, not the evidence.

### What landed

| File | Role |
|---|---|
| `src/aic_dc/antigravity/options.py` | Config assembly and the AG-5 write seam. `NEVER_SET` moved here from `surface.py` |
| `src/aic_dc/antigravity/steps.py` | The `Step` → `Event` pump. Emits only names the Claude pump emits |
| `src/aic_dc/antigravity/session.py` | Lifecycle: `Agent` for the lifetime, `agent.conversation` for the turn |
| `tests/test_antigravity_{options,steps,session}.py` | 94 tests, offline — no key, no harness, no network |
| `specs5/plan-ag/probe_session.py` | The live spike. Read-only, costs money, not yet run |

`sdk-surface.md` gained [§ The step stream](sdk-surface.md#the-step-stream--read-in-phase-3-and-it-is-not-shaped-like-claudes)
with the three measurements below. `surface.py`'s tables moved where phase 3 made them stale.

### The three findings the pump is shaped by

All three came from reading `localharness_pb2.StepUpdate` while building, and none was visible in the
type stubs the earlier phases read.

1. **A builtin tool's arguments and its result are the same sub-message.** `run_command` goes out as
   `{command_line, working_dir}` and comes back as the same dict with `exit_code` and
   `combined_output` added; the SDK copies the whole thing into `ToolCall.args` on both frames. A
   pump that forwarded `args` would render a card whose *input* grew a command's entire stdout on
   completion and would never emit a result. `TOOL_RESULT_FIELDS` is the split, and a test checks it
   against the proto field by field so a release that adds an output field goes red.
2. **The step stream and the permission hook have different shapes for the same call.** The stream
   carries the typed `{file_path, diff_block}`; the hook carries free-form JSON with `TargetContent`
   and `ReplacementContent`, which is what phase 2 measured. `view_file` on the stream carries no
   content at all. This does not re-open AG-2 — it says where phase 4 reads the diff from, and it is
   not the stream.
3. **`BuiltinTools.nondestructive()` is not a write boundary.** It excludes only `run_command`,
   classifying `create_file`, `edit_file` and `generate_image` as nondestructive. An adapter adopting
   it as its seam would enable exactly the tools AG-5 exists for. `MUTATING_TOOLS` is ours; the SDK's
   `read_only()` *is* adopted, because a new read-only tool arriving free is safe and a new write
   tool arriving free is not.

### The posture, which is enforced rather than documented

A session with **no decide hook enables no mutating tool at all** — not gated-then-denied, absent
from `enabled_tools`. There is no flag that overrides it, because a flag is what somebody sets while
debugging and forgets. Asking for a write tool without a hook is a `ValueError` at config assembly,
ahead of the SDK's own refusal (`agent.py:93-103`), which fires on *policy absent* and would be
satisfied by a stray `policy.allow_all()`.

`start_subagent` joined the seam during the work: a subagent inherits the tool set, so a gate that
stopped at the top-level trajectory is bypassed by asking a child to do the write. Same hole as
AG-R-11's `run_command`, one level down, and it was not in any earlier phase's list.

### Four bugs the gates caught

Three of the four were caught by tests written in the same sitting as the code they broke, which is
the argument for writing the gate first rather than the feature first.

- **A read-only session raised instead of running.** `write_tools` defaulted to all of
  `MUTATING_TOOLS` and *then* rejected the absence of a hook, so the ordinary phase-3 posture — no
  hook, no writes — was a `ValueError`. The default is now "everything the hook can gate", which
  with no hook is nothing.
- **`StepSource.UNKNOWN` was being dropped.** The pump filtered on `source != "MODEL"`, which
  discards an unrecognised source; `surface.STEP_MEMBERS` says *"render, do not drop"*, and on an
  alpha SDK that member is how a source this wheel does not know arrives. Now `PROSE_SOURCES`
  includes it, written as a positive set so every member is accounted for by name.
- **Every turn would have ended with a card reading "finish".** `finish` is a real `BuiltinTool` and
  arrives as a real tool call. It is the loop's terminator, not work the agent did, and it is now
  suppressed.
- **The vocabulary gate passed for the wrong reason at first.** Its first draft matched
  `Event("name")` by regex against the Claude pump, which misses `streamChunk` and `thinkingChunk` —
  that pump builds those two in a conditional and passes the result as a variable, so the two events
  most likely to drift were the two the check could not see.

### What was deliberately left out

- **No RPC registration and no UI.** The phase says "registered but not wired"; on reading, there is
  no engine registry to register *with* — `service.py` constructs the Claude session directly. Adding
  one is phase 4's problem, when there is a second thing to route to, and inventing it here would be
  a seam designed against no second caller.
- **No permission hook.** Phase 4. `options.py` wires the `hooks` config field and the session takes
  a `decide_hook`; what does not exist is an implementation.
- **No resume, no mirror, no `conversation_id`.** Phase 5. The session deliberately does not read it,
  so the field stays pending in the probe rather than reading as handled on the strength of a
  property nobody calls.
- **No `StopReason` rendering.** The pump forwards the reason verbatim onto `streamComplete` and
  nothing renders the difference between a budget cap and an ordinary stop. Those rows stay pending
  in `STEP_MEMBERS` for that reason.

### A note on the drift gate

The step section is the one place in `surface.py` that **declares** coverage rather than deriving it.
`referenced_enum_members` finds `StepType.TOOL_CALL` written as an attribute, and the pump does not
write it that way: it compares on `.name` against string literals, because `Step`'s enums are
`str`-valued and a release turning one into a plain string would otherwise mis-dispatch silently.
That defensiveness is right and it costs the derived signal, so the rows are set by hand with
`test_every_step_member_is_named_in_the_pump` reading `steps.py` for each member's name as the
cross-check the syntax tree cannot give. Worth knowing before trusting that section the way the other
five can be trusted.

### What phase 4 has to do first

1. **Read the diff from the hook, not the stream.** Finding 2 above. The stream's `edit_file` carries
   `diff_block` and no old text; the hook carries `TargetContent` + `ReplacementContent` + a line
   range. A dialog built against the stream cannot render what phase 2 proved was available.
2. **Decide where the engine seam lives.** There is no registry today. Whatever phase 4 builds is the
   thing AG-3's capability descriptor hangs off in phase 6, so it is worth designing once rather than
   growing.
3. **Own "always allow" locally.** `updated_permissions` has no counterpart at any layer (AG-5). A
   rule store consulted by the hook before it opens a dialog is AIC⚡DC's to build.
4. **Fill in the `tools` section of the probe.** It is the one section still entirely pending, and it
   becomes derivable the moment the gate has a per-tool table to read.

### The live run, and what happens when the free tier says no

`probe_session.py` is read-only by construction — no decide hook, so no mutating tool is enabled —
which makes it safe to point at a real repository. It **does not retry**: the SDK's own
`retry_config` already retries a 429 or a 503 invisibly (measured in phase 1), so a failure that
reaches the probe has been retried through already, and a loop on top would burn the same quota to no
effect. It distinguishes the two refusals instead, because Google's message does not: `limit: 0`
means the plan's allowance is zero and no wait changes it, while a plain 429 means wait a minute. The
free tier's 5 RPM against a turn that is many model calls makes the second outcome ordinary rather
than exceptional.

---

## Phase 4 — Chat on the second engine (in progress, 2026-09-01)

**Exit criterion:** *"A user holds a full working conversation, including edits, entirely through
Antigravity, with every write approved through the dialog."* Not met — this entry records the first
tranche, the permission gate, which is the half AG-5 calls non-negotiable.

### What landed

| File | Role |
|---|---|
| `src/aic_dc/antigravity/permissions.py` | The AG-5 gate. A `PreToolCallDecideHook` driving the **shared** `PermissionBroker` |
| `tests/test_antigravity_permissions.py` | 32 tests, offline — no key, no harness, no network |

### The design decision, which was the whole of the work

**There is one ask path, and this module does not own it.** `permissions.md`'s three load-bearing
properties — one ask path, every request resolves exactly once, localhost-only — are engine-agnostic,
so the gate calls `PermissionBroker.can_use_tool`, the same method the Claude engine calls, and
converts the answer. It owns no queue, no countdown, no presence check and no broadcast, and
`test_this_module_owns_no_second_broker` reads its own source to keep it that way.

A second broker would have been the easier thing to write and would have produced two places a
request could be lost, two countdowns for one dialog to render, and two implementations of the
localhost check that decides whether a remote collaborator can approve a write.

Exactly two facts are per-engine, and they are why `_AntigravityBroker` overrides one method:

- **Classification.** `classify_tool` knows Claude's names; asked about `edit_file` it returns its
  most-cautious fallback, `exec` — right as a default, wrong as an answer, because the dialog would
  show a shell-command card for a file edit and no diff at all.
- **Argument spelling.** The hook's arguments arrive as CamelCase JSON from the Go side
  (`TargetFile`, `TargetContent`, `ReplacementContent`) while the existing payload builders read
  Claude's `file_path` / `old_string` / `new_string`. `normalise_args` is that translation, and it
  **adds** aliases rather than substituting them, so the dialog still shows the engine's own words
  beside the diff and a stale alias degrades to "no diff, full input shown" rather than to an empty
  dialog.

It is a *field* rename and not a *tool* rename: the payload keeps saying `edit_file`. Telling the
user their agent called `Edit` would be a lie about which engine is running, and the kind of
engine-name leak [AG-R-4](risks.md#ag-r-4) exists to prevent.

### Three things the work turned up

- **The gate has to subclass, and the package must not import the SDK at module scope.**
  `HookRunner.register_hook` registers by `isinstance` and raises `ValueError` on anything else
  (`hooks/hook_runner.py:148-153`), so the object handed to the config must be a real
  `PreToolCallDecideHook`. Defining that subclass needs the SDK imported, which the package refuses
  to do at import time (AG-R-10). Resolved with a factory: the logic lives in an SDK-free class and
  `as_hook()` builds the subclass lazily, so every test runs offline and only registration needs a
  wheel.
- **"Always allow" is closed at the right end.** Antigravity has no `updated_permissions` at any
  layer, so a rule cannot be persisted. Rather than accepting an always-allow and dropping the rule,
  the payload offers **no** `suggested_rules` at all, so the broker's own normalisation has nothing
  to bind one to and degrades it to allow-once, saying so. An offer the engine cannot keep is worse
  than no offer, because the user believes they will not be asked again. A warning guards the path
  that should now be unreachable.
- **`start_subagent` would have been ungated.** Its natural class is `delegate`, matching Claude's
  `Task`, and `GATED_BY_DEFAULT["delegate"]` is `False` — correct there, because the child's own
  calls are gated individually as they happen. The flag is forced true for everything in
  `MUTATING_TOOLS` anyway: the class shapes the dialog's *wording*, this decides whether a dialog can
  be presented as routine, and the two must not be able to disagree. `ALWAYS_ASK` is
  `options.MUTATING_TOOLS` itself rather than a copy, so the module that turns the tools on and the
  module that gates them cannot drift.

### A bug the tests caught, and one they caused

- **The test helper hung the suite.** `PermissionBroker.resolve` is `async` and takes `resolved_by=`;
  the first draft called it synchronously with `caller=`. Nothing resolved, and because a connected
  localhost client means *no deadline* by design, the request waited forever rather than failing.
  The helper now wraps the call in `asyncio.timeout` — the failure mode without one is a hang, which
  is right in production and useless in a test.
- **An assertion claimed the wrong mechanism.** The always-allow test asserted a warning fired, and
  it did not: the degradation happens earlier and more robustly, in the broker, because no rule is
  offered. The test now asserts the offer is absent, which is the property that makes the
  degradation honest rather than a silent discard.

### What is still missing before the exit criterion is met

1. **Nothing constructs the gate.** It takes the same callbacks the Claude session gives its broker,
   and no engine router exists to supply them. That is the next piece, and the architecture diagram
   settles its shape: a router mounting both adapters under one RPC namespace with a capability
   descriptor ([AG-3](decisions.md#ag-3), [AG-9](decisions.md#ag-9)).
2. **The chat panel has not been pointed at the second taxonomy.** The pump emits only event names
   the Claude pump already emits, so the browser needs no new handlers — but that claim is asserted
   against the Claude pump's source, not against a rendered conversation.
3. **`PostToolCallHook` is unwired.** It is the trigger for broadcasting a write and queueing the
   re-index, both engine-agnostic jobs. Still pending in the probe.
4. **No live turn has been run through the gate.** `probe_edit_args.py` measured the raw hook in
   phase 2; nothing has yet driven a real dialog from a real Antigravity turn.

---

## Phase 6 — Capability descriptor (data landed early, 2026-09-01)

**Exit criterion:** *"No surface renders an empty or synthesised value for a fact its engine cannot
report; no webapp branch keys off an engine name string."* Not met — nothing renders from it yet.
What landed is the descriptor itself, ahead of its phase and deliberately.

### Why it is here and not at the end

The README's own ordering constraint asks for exactly this: *"The capability descriptor late, but
specified early. It cannot be built until there are two engines to describe, but every phase from 3
onward must record which surfaces it could not serve — otherwise phase 6 is an archaeology
exercise."* There are now two engines to describe, and phases 3 and 4 have just finished producing
the list of things the second one cannot do. Writing it down now is the difference between recording
a decision and reconstructing one.

| File | Role |
|---|---|
| `src/aic_dc/capabilities.py` | The descriptor. Thirteen surfaces, both engines, engine-agnostic module |
| `tests/test_capabilities.py` | 23 tests, including one that reads `sdk-surface.md` as the source of truth |

### The distinction the table is built around

**`ABSENT` is not `UNBUILT`.** Both hide the surface and the browser cannot tell them apart — why
there is no data is not the browser's business. The difference is for us:

- `ABSENT` — no source data exists and none will. USD cost on Antigravity, because there is no dollar
  figure anywhere on the SDK or on `agy`'s wire (AG-6). A decision.
- `UNBUILT` — the data exists and nothing reads it. Antigravity's transcript history: `Step` carries
  everything needed and no renderer has been written. A to-do.

Collapsing them would have made the table cheaper to write and thrown away the only thing that makes
phase 6 tractable. `unbuilt_surfaces()` is the resulting to-do list, and it is data rather than
memory: `agent_questions`, `mcp_server_inventory`, `session_mirror`, `subagent_tabs`,
`transcript_history`.

### Two rules about what is *not* in the table

- **A surface neither engine serves has no row.** AG-9: dead code should be deleted rather than
  described. That cuts five real SDK capabilities — structured output, audio and video input, daemon
  commands, `triggers`, multi-model routing — which have no AIC⚡DC surface at either end. A row for
  them would make the table read as coverage of a UI that does not exist.
- **A surface both engines serve has no row either.** An entry that is always `supported` is a
  browser branch never taken, and a table of them rots unnoticed because nothing reads it. The one
  exception is `amend_tool_input`, which earns its row by carrying an argument rather than a
  decision: it is the capability AG-5 chose the raw hook over `policy.ask_user` to keep, and deleting
  the row would delete the reason. A test pins the exception at exactly one.

The permission dialog deliberately has **no** row, and a test asserts it never gains one: AG-5 makes
it a requirement of the second engine rather than a feature of it, and a descriptor entry would imply
an engine could ship without one.

### An unanswered question is loud

`supports()` raises `UnknownSurfaceError` on a key it does not know, rather than returning `False`.
This is AG-9's own lesson one layer up: returning `False` for a typo hides a panel and looks exactly
like a deliberate hiding. The near-miss is realistic — the real key is `context_window_usage` and
`context_usage` is what the existing Claude method is called — so the test uses that one.

### AG-R-4, enforced structurally

The descriptor carries no engine identity: no `engine`, `name` or `adapter` field at any level, and
both engines' payloads are asserted to have identical shape so a call site cannot be written against
one of them. The check is on the *fields*, not on the prose — a first draft grepped the payload for
the word "antigravity" and failed on its own explanatory notes, which is forbidding the documentation
rather than the branch.

### What phase 6 still has to do

The router publishes it as of the entry below. The Context tab and HUD have to hide on it, and
`test_capabilities.py` should grow a webapp-side counterpart when there is one.

---

## The engine router (2026-09-01)

Not a numbered phase. [AG-3](decisions.md#ag-3)'s seam, which phases 3 and 4 both ended by naming as
the thing they were blocked on: the permission gate had nothing to construct it and the capability
descriptor had nobody to publish it.

| File | Role |
|---|---|
| `src/aic_dc/engine_router.py` | The router. Generates its delegates; owns the descriptor |
| `tests/test_engine_router.py` | 22 tests, including registration against the real `RpcServer` |
| `src/aic_dc/main.py` | Registers the router instead of the service, and reads the call proxy off it |

### The method list is generated, and that is not a flourish

jrpc-oo finds a service's methods with `inspect.getmembers(cls, predicate=inspect.isfunction)` on the
**class** (`jrpc_oo/ExposeClass.py:37-41`). A `__getattr__` that forwarded everything would therefore
expose *nothing*: the handshake sends a method list, and a name that is not on the class is not in
it. That ruled out the obvious implementation before it was written.

The alternative to generation is 48 hand-written delegates, and its failure mode is specific: a
method added to the adapter and forgotten here **works in Python and 404s over RPC**, so nothing
catches it until somebody clicks the button. Generating them from `_public_methods(master)` — the
same call jrpc-oo makes, deliberately, rather than a `dir()` filter that happens to agree today —
makes the router's surface unable to drift from the adapter's.

Two details that are load-bearing rather than tidy:

- **Async and sync delegates are generated separately.** The adapter has both — `shutdown` is a
  coroutine, `get_server_info` is not — and one wrapper that returned a coroutine for a synchronous
  method would change its contract for every in-process caller, not only the RPC one.
- **`functools.wraps` copies the adapter's signature and docstring.** jrpc-oo inspects the exposed
  callables, and a wall of identical `(*args, **kwargs)` stubs would erase the surface's
  self-description for anything that reads it.

### It routes to one engine, and says so

The Antigravity adapter does not implement the 48-method surface — it has a session, a step pump and
a permission gate, and no `chat_streaming`, `history_list` or `get_model` — so there is nothing to
switch to yet. `list_engines()` reports `mountable: ["claude"]` rather than implying otherwise.

That is the reason this lands now rather than with a second master: the router is
**behaviour-preserving**, and the claim is cheap to test. It exposes exactly the method names the
adapter exposes, every call reaches the same object it reached before, and the switch — when the
adapter is ready — is a change to one constructor rather than to a working system.

### The bug this would have shipped with

**jrpc-oo injects `get_call()` onto the *registered* instance.** That is now the router, not the
service behind it. `main.py` read the call proxy off `claude_code_service`, and left unchanged it
would have found nothing and dropped **every server-push event** — every streamed chunk, every
permission dialog — behind one `logger.warning`. The chat would have looked hung rather than broken.

Caught by reading the wiring rather than by a test, and then confirmed live: a real startup logs
`engine_router attributes: get_call=True call=False`. `test_the_call_proxy_is_read_off_the_router`
reads `main.py` to hold it, because the failure is a one-line mistake with a silent symptom and a
startup test would need a git repo, ports and a browser.

### Verified live

A real `aic-dc --no-browser` startup in a throwaway repo: server up on its port, `Event callback
wired (service=ClaudeCodeService)` — the generated class carries the namespace name — no tracebacks,
initialization complete. The only warnings are `no remote method AcApp.startupProgress`, which is the
absent browser and is what `--no-browser` means.

Separately, registration through the real `RpcServer`: **50 methods, all under
`ClaudeCodeService.*`**, the adapter's 48 plus `get_engine_capabilities` and `list_engines`. Asserted
in the suite rather than only observed, because the generated-class trick is exactly the kind of
thing that satisfies `inspect` and then does not survive contact with the transport.

### The router is the authority, not the engine

`get_engine_capabilities` is answered from `aic_dc.capabilities`, never by asking an adapter, and
`build_router` **raises** if the adapter has a method of that name rather than letting the delegate
win. An engine cannot be the authority on what it cannot do: that is the question the descriptor
exists to answer, and asking the engine would reintroduce exactly the "no answer looks like no data"
failure AG-9 is written against.

### What is still not done

- **No webapp reads the descriptor.** `get_engine_capabilities` is reachable over RPC and nothing
  calls it. Per-engine hiding across the Context tab, HUD and settings is the rest of phase 6.
- **No engine selector.** `list_engines` exists for one, and there is no UI.
- **Antigravity is not mountable.** Resolved in the entry below.

---

## Partial-surface routing (2026-09-01)

The open question the router entry ended on — whether a method with no counterpart should fail as
"unsupported on this engine" or whether the adapter must grow the full 48-method surface — turned out
not to need a new decision. [AG-9](decisions.md#ag-9) already answers it: *a surface with no
counterpart is absent from the UI, driven by the capability descriptor.* So the descriptor decides
which methods are meaningful, and the router refuses the rest.

### The mapping, and which way its default points

`RPC_SURFACES` maps 15 RPC methods onto the six hideable surfaces they serve — `get_context_usage` to
`context_window_usage`, the five `history_*` methods to `transcript_history`, and so on. It is the
bridge between `capabilities.py`, which is about panels, and the RPC surface, which is about methods.

**A method absent from that table is core, and every engine must implement it.** That default is
deliberate and it is the safe direction: forgetting to map a new method makes it *required*, which
fails loudly at startup on an engine that lacks it. The opposite default would silently make it
optional and let an engine mount with a hole in it.

### Refused, not missing

An unsupported method is still **generated and still on the wire** — it raises
`UnsupportedOnThisEngine` when called. Omitting it would take the name out of jrpc-oo's handshake and
the browser would get a transport-level "no such method", which is indistinguishable from a version
mismatch or a broken build. The method is there; it declines, and it names the surface and points at
`get_engine_capabilities()`.

That is AG-9 restated one layer down. An empty list does not say "no servers", it says "no answer" —
and a *missing* method does not say "this engine has no such thing" either.

### The mounting guard

`build_router(..., require_full_surface=True)` — the default, and what `main.py` uses — refuses to
build if the adapter cannot serve the core surface, and **the error message is the to-do list**:
every missing method by name. Without it, a half-mounted engine fails at *click* time, one button at
a time, with an `AttributeError` that reads as a crash rather than as an engine that was never ready.

The required set is computed per engine, so an adapter that omits `history_list` mounts on
Antigravity — where transcript history is unbuilt and the panel is hidden — and does **not** mount on
Claude, where the panel is real. A test asserts exactly that asymmetry, and another asserts
`main.py` never waives the requirement.

### Behaviour-preserving, still

Every surface in `RPC_SURFACES` is supported on Claude, so the router generates **no refusals at
all** for the shipped engine. That is asserted as a property rather than left as an observation: the
change must not take a working surface away from the engine that has one to accommodate an engine
that does not. Verified live again — a real `--no-browser` startup with the guard active reaches
`Initialization complete` with no tracebacks and nothing refused.

### One source of truth

The refusal set is derived from the descriptor at build time rather than kept as a second list. A
hand-kept list is the thing that drifts from `capabilities.py`, and the failure is silent in both
directions: a panel hidden while its method works, or a method refused while its panel renders.
`test_the_refusal_set_matches_the_descriptor` walks the mapping and checks each one against
`capabilities.supports`.

### What is still not done

- **No webapp reads the descriptor.** Unchanged; this is the rest of phase 6.
- **There is still no Antigravity adapter.** What exists now is the shape of the hole:
  `build_router(adapter, engine=ANTIGRAVITY)` names every method it must implement. That list is
  phase 4's remaining work, and it is now data rather than a design question.

  **It is 33 methods**, down from 48 — the descriptor makes 15 of them optional on this engine. Not
  evenly weighted: `chat_streaming`, `cancel_streaming`, `resolve_permission`, `get_current_state`,
  `new_session` and `connect_engine` are the conversation, and phases 3 and 4 already built what sits
  behind them. Several others are engine-agnostic work the adapter only has to forward — the four
  `lsp_*` methods, `navigate_file`, `set_viewer_state`, `commit_all`, `get_commit_graph`,
  `reset_to_head` and the four `*_review` methods touch the repo and the indexes rather than the
  engine, and a second adapter should delegate to the same objects rather than reimplement them.
  That split is worth making explicitly before writing the adapter, because doing it method by method
  is how the second engine ends up with its own copy of the file tree.
