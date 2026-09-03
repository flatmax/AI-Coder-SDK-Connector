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
| `scripts/probe_edit_args.py` | The phase-2 spike. Seeds a file, requests an edit, logs every `ToolCall` at `pre_tool_call_decide`, denies all mutating tools, then asserts the file's bytes are unchanged and prints a verdict. |

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

`scripts/probe_edit_args.py` needs a real key and hits the network, so it is a spike and not a test. Its
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
| `scripts/probe_consultant.py` | The live spike. Three checks, runnable, costs money |

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

**The code and the probe are built; the live run has not been made.** `scripts/probe_session.py` exists,
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
| `scripts/probe_session.py` | The live spike. Read-only, costs money, not yet run |

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

`scripts/probe_session.py` is read-only by construction — no decide hook, so no mutating tool is enabled —
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
4. **No live turn has been run through the gate.** `scripts/probe_edit_args.py` measured the raw hook in
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

---

## Phase 4 — The Antigravity adapter (2026-09-01)

`src/aic_dc/antigravity/service.py`. The thing the router refused to mount, and now mounts.

| File | Role |
|---|---|
| `src/aic_dc/antigravity/service.py` | `AntigravityService` — 31 methods behind the shared RPC surface |
| `tests/test_antigravity_service.py` | 42 tests, offline. Nothing starts a harness |

### Thirty-one, not forty-eight, and the two that moved

Writing the adapter forced two methods to be classified that the first pass had left as core:

- **`rewind_files`** has no Antigravity counterpart at any layer — it is the Claude SDK's own
  checkpointing, and there is no checkpoint, no restore and nothing to build one from. A new
  `file_checkpointing` surface, `ABSENT`, with the note that git already covers most of what it would
  undo.
- **`stop_task`** kills one subagent, so it belongs to `subagent_tabs`, which is already `UNBUILT`
  here.

Both were found by asking what an honest implementation would look like and finding the answer was
"a stub that lies". Adding them to the descriptor is the alternative the descriptor exists to
provide, and it took the required surface from 33 to 31.

**No method here returns an empty dict to satisfy the router.**
`test_it_implements_no_method_the_descriptor_hides` asserts the converse too: a method the descriptor
hides must *not* be implemented, so the two tables cannot drift into a state where the panel is
hidden while the method works.

### Sharing, done by holding the same objects

A third of the surface is repository and index work that is not engine-specific, and the failure mode
named in the previous entry — the second engine growing its own file tree — is avoided by reusing the
same modules rather than by extracting a mixin out of a 148 KB `service.py`:

- `symbol_index` is **injected**: the one instance `main.py` built, not a second index over the same
  tree. The four `lsp_*` methods return `None`/`[]` before it exists, which Monaco reads as "no
  answer here" — the same contract the Claude adapter has.
- `review` is a real `ReviewMode`, constructed with *this* engine's `set_permission_mode`. Its
  collaborators are injectable precisely so a second engine can own its posture while sharing the git
  arrangement, which is what made this a one-line reuse rather than a refactor.
- `commit_all` and `reset_to_head` call `claude_code.commit` directly. That module takes the service
  as its argument and reads `_check_localhost_only`, `_repo`, `review`, `_committing`, `_turn_tasks`
  and `_broadcast` off it — so this class provides exactly that contract, and a test names the six
  attributes so a rename there fails here rather than at the first commit somebody tries.

Importing `claude_code.review` and `claude_code.commit` from the Antigravity package reads oddly. It
is the same trade AG-3 makes about the class name: those modules are engine-agnostic in everything
but their package, moving them is mechanical and can happen later, and copying them is what drifts.

### Declining rather than pretending, in the two places a stub was easiest

- **`connect_engine(resume=…)` refuses.** Resume by `conversation_id` is phase 5. Starting a *fresh*
  session when the caller asked to resume one is the wrong kind of success — it returns a working
  session that has lost the context that was the point of the request.
- **`chat_streaming(images=…)` refuses.** The SDK accepts image input, so this is unbuilt rather than
  impossible; a turn that silently dropped an attached screenshot would answer the wrong question
  convincingly.

### The postures it does not offer

`PERMISSION_MODES` is `("default", "plan")`. `acceptEdits` and `bypassPermissions` are **absent by
construction**, and that is AG-5 rather than an omission: the dialog is a requirement of this engine,
not a feature of it, and a posture that skips it is the blanket bypass that decision says must never
reach a shipped path. Asking for either returns an `unsupported` error naming the reason.

`resolve_permission` forwards to the gate's **shared** broker, so it answers the same queue, with the
same first-one-wins rule, that the Claude adapter's does. One ask path across two engines.

### A test that would have caught a whole class of mistake

`test_async_ness_matches_the_claude_adapter` walks every method both adapters define and asserts they
agree about being coroutines. A method that was `async` on one engine and not the other would satisfy
the router's surface check, register fine, and then break the browser's call in a way that looks like
a transport bug.

### What is still not done

- **Nothing constructs it.** `main.py` builds the Claude adapter and routes to it. Choosing the
  master per session (AG-1) needs a setting and a startup path, and neither exists.
- **No live turn.** Every test here is offline; the harness has never been started through this
  class.
- **Engine errors are in memory.** The Claude engine writes `engine-errors.jsonl`; this holds a list,
  because a second on-disk log wants a path convention and there is no reader for one. Recorded as a
  gap rather than pretended away.
- **`get_current_state` carries no cost key at all**, which is AG-6 working as intended and will look
  like a missing field until the webapp reads the descriptor.

---

## The consultant, wired (2026-09-01)

Phase 1 built `ConsultantBridge` and ended with *"nothing constructs one in `service.py`"*. This is
that line. It is one method, and it is the point at which the second engine becomes **usable rather
than merely present**: with a Gemini key on the machine, a Claude turn can now call
`second_opinion` and `generate_image`.

That is AG-1's user story arriving in the order AG-7 chose for it — Claude stays master, and reaches
Google's model for a capability Anthropic does not have, without either engine giving up its own
strengths. It needs no engine selector, no per-session master choice and no UI work, which is exactly
why phase 1 put it first.

### Where it mounts, and why not one line earlier

`ClaudeCodeService._add_consultant`, called from `_build_bridge_wiring` beside the index tools. It
mounts under **`aic-dc-antigravity`**, not `aic-dc`, and that is AG-5 rather than tidiness:
`permissions.can_use_tool` early-returns an allow — no dialog, no broadcast — for anything matching
`mcp__aic-dc__*`, because the index tools are read-only. `generate_image` writes a file. Mounting it
on the ungated server would have routed a file write around the permission dialog *silently*: the
tool would work, the file would appear, and nothing would look wrong.

`tests/test_consultant_wiring.py` asserts the name against `permissions.AIC_DC_MCP_SERVER` directly
rather than against a literal, so a rename on either side fails there instead of quietly re-opening
the hole. That check already existed for the bridge; the mount point is a second place to get it
wrong, so it is made twice.

### Absent, not broken, without a credential

No Gemini key means the server is **not registered at all** — AG-9's "hidden rather than stubbed"
applied to a tool definition, since two tools that always answer "no credentials" cost context on
every turn and buy nothing. AG-R-8 makes that the most likely first experience of this engine, so it
is the ordinary path rather than the edge case.

It is deliberately **not** recorded in `_degradations`. That banner is for things *this* engine lost;
listing a second engine nobody configured would make every default install look broken.

A consultant that fails to construct for any other reason is caught and logged, never fatal: the
consultant is an addition to this engine, not a part of it, and a session that starts without it is
strictly better than one that refuses to start with it.

### Two existing tests that had to change, and the reason is worth keeping

`TestBridgeWiring` pinned `list(session._mcp_servers) == ["aic-dc"]`. Both it and its sibling started
failing — **because this machine has a key**. That is a worse problem than the failure: those
assertions had become dependent on whether the machine running the suite happened to have a Gemini
credential, which is a test that passes or fails for reasons unrelated to the code.

Fixed by pinning the consultant *off* for that class with a fixture, since those tests are about the
index bridge. The consultant's own behaviour is covered in its own file, where the credential state
is controlled rather than inherited.

### Verified live

A real `aic-dc --no-browser` startup logs:

```
Antigravity consultant mounted as aic-dc-antigravity
(credential from Gemini API key from ~/.config/aic-dc/gemini-api-key)
```

`second_opinion` was verified end-to-end against a live key in phase 1. `generate_image` still
cannot be, because every Gemini image model reports `limit: 0` on a free-tier key — the tool will
mount, the agent can call it, and Google will refuse it with the message `_explain` turns into
"retrying will not help; it needs a billing account" ([AG-12](decisions.md#ag-12)).

---

## Phase 6 — the webapp reads the descriptor (2026-09-01)

The half phase 6 was missing: the data landed early, and nothing read it. Now something does, and
[6b](README.md#phases) is unblocked.

| File | Role |
|---|---|
| `webapp/src/engine-capabilities.js` | The browser-side store. Fetch once, answer `supports(key)` synchronously |
| `webapp/src/engine-capabilities.test.js` | 17 tests |
| `webapp/src/usage-hud.js` | First consumer: the Context section hides when the engine has no window |

### The default before the answer arrives, which is the whole design

`supports()` answers **true** while the descriptor is loading. That is chosen rather than fallen
into, and it is the decision the rest of the module hangs off:

- Answering `false` would hide every panel for the width of one RPC round trip **on the shipped
  engine** — a visible regression in the common case, bought for tidiness in the rare one.
- Answering `true` renders a panel that may then hide. Every reader already tolerates absent data,
  because they were written for an engine that had not connected yet, so the cost is a panel that
  empties rather than one that breaks.

The same reasoning makes a failed fetch a no-op: the descriptor is *how a panel learns to hide*, so
failing to read it must not hide anything. And it is safe in the direction that matters, because the
router raises `UnsupportedOnThisEngine` rather than returning a plausible empty value — a fetch that
slips through during load fails loudly instead of drawing a synthesised zero.

An unknown key also reads as supported, which is the **opposite** of the server's rule and
deliberately so. There, an unknown key is a programming error worth raising on. Here it is most
likely a webapp built against a newer server, and hiding a panel over version skew is worse than
showing one whose data may be empty.

### AG-R-4, enforced by having nothing to branch on

Surface keys are a frozen `SURFACE` constant rather than free strings, so a typo is a broken import
at build time instead of a silently hidden panel at run time — a misspelled free string would read as
"unknown key" and therefore as supported, which is the silent failure.

The tests assert the payload carries no `engine`/`name`/`adapter` field at any level. The rule is
only as good as the shape that enforces it, and the shape is checked.

### The first consumer, and what "hidden" means in practice

`usage-hud.js` returns `nothing` from `_renderContext()` when `context_window_usage` is unsupported —
not a 0% bar, not a "no data" note. A placeholder where a bar used to be still reads as a reading,
and a number on screen is believed. Because `_section()` is called at the *end* of that method, the
collapsible **head** disappears with the body, so there is no empty "Context" row left behind. A test
asserts exactly that, because it is the kind of thing an early return gets subtly wrong.

It also stops the poll: without the guard the HUD would retry a method the router is guaranteed to
refuse, once per tick.

### Two tests that caught real drift

- **`test_rpc_surface.py` failed the moment the browser called
  `get_engine_capabilities`.** It audits browser call sites against the mounted server surface, and
  it was still describing `ClaudeCodeService` — but `main.py` mounts the *router*. Fixed by reading
  `ROUTER_OWNED`, so a method added to the router joins the audit in the same commit. The router's
  *delegates* need nothing: they carry the adapter's names, which is what makes the wrapping
  invisible to 43 existing call sites.
- **`list_engines` had no caller**, and the same file refuses an RPC that is in neither table. It is
  now `DORMANT` with the reason: AG-1's engine selector does not exist. Worth distinguishing from its
  sibling — `get_engine_capabilities` is what a component asks to decide whether to render;
  `list_engines` names the engine, which is the thing AG-R-4 forbids a render path from branching on.
  Its only legitimate readers are a human-facing selector and diagnostics.

### What phase 6 still has to do

`usd_cost` and `account_rate_limits` have descriptor entries and no consumer yet — the turn footer,
the session cost and the rate-limit panel still render unconditionally. They are the same shape of
change as the Context section and were left out of this tranche deliberately, to keep the first
consumer small enough to be obviously correct.

---

## AG-1 — one master per session, chosen per session (2026-09-01)

The thing three phases were blocked on. Phase 4's exit criterion, the adapter's *What is still not
done* and the router's all named the same missing piece in the same words — **nothing constructs it** —
and it was one constructor argument away, as the router's own docstring predicted.

| File | Role |
|---|---|
| `src/aic_dc/engine_router.py` | Mutable master; call-time refusals; `switch_engine` |
| `src/aic_dc/main.py` | Constructs both adapters, mounts the one `app.json` names |
| `src/aic_dc/config.py` | `master_engine`, from `app.json`'s `engines.master` |
| `src/aic_dc/antigravity/service.py` | `_attach_symbol_index`, so the shared index reaches both |
| `webapp/src/settings-tab.js` | The selector — `list_engines`, `switch_engine` |
| `webapp/src/app-shell/index.js` | `engineChanged`: descriptor first, then dispatch |
| `tests/test_engine_router.py` | 50 tests, 16 of them about switching |
| `webapp/src/settings-tab.test.js` | 7 tests on the panel |

### The measurement the design rests on

Before writing anything, both adapters were mounted and their surfaces compared. The result is what
made this small, and it is a coincidence worth keeping under test rather than a property that was
designed for:

* Claude exposes **48** public methods, Antigravity **31**, and Antigravity exposes **nothing Claude
  does not**.
* The 17-method difference is **exactly** `RPC_SURFACES`. Not approximately — every method one engine
  has and the other lacks was already mapped to a hideable surface.
* `_missing_core_methods(AntigravityService, ANTIGRAVITY)` was already `[]`. The adapter mounted the
  day it landed; nothing had asked it to.

So the set of names on the wire is the same whichever engine is master: 48 delegates plus the
router's own. That matters more than it looks. **jrpc-oo sends its method list once, at the
handshake** (`ExposeClass.py:37-41`), and cannot renegotiate it. A router whose surface moved with the
master would have to re-register the service and reconnect every browser in order to switch. This one
does not, so a switch is a field assignment. `test_the_wire_surface_does_not_move` and
`test_the_real_adapters_both_mount` are what stop that identity from quietly breaking.

### The one structural change: refusals moved to call time

The first cut baked the decision into the generated method — a name was either a delegate or a
refusal, decided when the router was built. With a master that can change, a delegate generated for
the engine mounted at startup would go on answering for it after the swap: the descriptor would say
the panel is hidden and the method would still return data. So `_delegate` now reads `self._engine`
on every call.

The visible cost is that a refused *async* method refuses on the `await` rather than on the call.
Over the wire that is the same thing — `ExposeClass` inspects the **result** and awaits a coroutine —
and it buys the property that matters more: the shape of the surface no longer changes under a
switch. Two tests were updated to await, with the reason recorded in them.

### A switch is a session boundary, and it cannot be anything else

Not a policy choice. [`sdk-surface.md` § What does not translate](sdk-surface.md#what-does-not-translate)
settles it: the mirror has **no protocol counterpart** — Antigravity owns an opaque `save_dir` — and
history rendering **needs a full sibling**, because `Step` is flat with `trajectory_id`/`depth` rather
than nested content blocks. These are two transcripts, not one in two dialects, so no version of this
carries a conversation across.

What follows from that, and is implemented:

- The outgoing engine is **stopped**; the incoming one connects lazily on the next turn, with no
  resume, which is what makes it a new session. Switching back is a new session too.
- **Nothing on disk is touched.** Each engine keeps its own mirror, and the conversation you leave
  stays listed and loadable.
- The clear is broadcast as **`sessionChanged` with an empty message list** — the event every client
  already resets on, the one `new_session` sends. Teaching the chat panel a second way to be reset
  would be two clearing paths that can disagree.
- It is refused mid-turn, matching `new_session`: the user can cancel first, and pulling the engine
  out from under a live turn loses its tail. The busy check reads `streaming_active` *and*
  `_turn_tasks`, because the adapters answer in different vocabularies and a background commit is not
  a chat turn but would still lose its tail.

### Ordering, in the one place it is load-bearing

`engineChanged` carries the **descriptor**, not just the name, and the browser installs it *before*
re-dispatching. Every listener downstream decides what to render by asking `supports()`, so a panel
that re-rendered while the store still held the outgoing engine's answers would draw exactly the
surface the switch was meant to hide. `sessionChanged` follows, for the same reason.
`engineChanged installs the descriptor before it dispatches` asserts it by reading the store from
inside the listener — a check after the fact cannot tell the two orders apart.

A failed announcement does **not** undo the switch. The engine has changed; a window that missed the
event is stale, which is recoverable, where raising would leave the router switched and the caller
told it failed.

### Where the choice lives, and why not in `engine.json`

`app.json`'s new `engines.master`, despite the other file's name. Every key in `engine.json` is a
*Claude session option* — model, effort, permission mode — read by `claude_code.engine_config`. Which
engine is master is a fact about the application, and putting it in one engine's option file would
make the second engine's existence conditional on the first's config.

It does not break `reload_app_config`'s promise that nothing in `app.json` reaches the engine's
session options: it is read at startup and at an explicit switch, never mid-session. An unknown name
falls back to Claude with a warning — a typo should cost the user the second engine, not the ability
to start the application.

### Absent, not broken, without a credential

Both adapters are constructed cold at startup, which is free: neither connects until asked, so the
second engine costs no subprocess and no harness. Without a Gemini key Antigravity is simply not in
`list_engines().mountable`, and `switch_engine` refuses it **by that reason** — a missing credential
said as a missing credential, distinguishable from a typo, because only one of the two is the user's
to fix. `mountable` was already in `list_engines`'s payload and hardcoded to `[self._engine]`; it now
answers honestly.

Every mounted adapter is validated at **build** time, not at switch time. A switch that discovered a
half-implemented adapter would already have torn down the working one, leaving the user on nothing.

### Three single-service assumptions in `main.py`, found by looking

None of these would have failed a test, and all three fail only *after* a switch — the worst time to
find out:

- **The symbol index** went to one adapter. The other would have answered every hover with "no
  answer", silently, and `AntigravityService` gained an `_attach_symbol_index` with the Claude
  adapter's exact name so startup can hand the *one* index to every adapter in a loop.
- **`_collab`** was set on one adapter. The localhost gate reads it, so the other would have failed
  open or closed depending on its default.
- **Teardown** shut one engine down. The other's pending permission dialog would have been left live
  forever — which is the one effect of `shutdown` that survives process death and the reason
  `_shut_the_engine_down` exists at all.

### What is still not done

- **No live turn.** Unchanged, and still the thing phases 1, 3 and 4 are waiting on. What has changed
  is that there is now a way to *reach* the second engine from the UI, which is what a live turn
  needs.
- **The descriptor has four unwired consumers, and the switch makes them reachable.**
  `transcript_history`, `session_mirror`, `slash_commands` and `mcp_server_inventory` are all
  `UNBUILT` on Antigravity, and nothing in the browser gates on them — so the history browser, the
  session-storage card, the slash-command menu and the MCP panel will call methods the router
  refuses. It is loud rather than wrong (`UnsupportedOnThisEngine`, never a synthesised empty), which
  is the safe direction, but it is four panels that should be hidden. Same shape as the Context
  section; this is the rest of phase 6.
- **Resume across engines is unguarded, and unreachable.** `resume_session` hands a transcript to
  whatever engine is master, and nothing records which engine wrote a session. It cannot be reached
  today — `session_mirror` and `transcript_history` are both refused on Antigravity — but it becomes
  live the moment phase 5 lands, and the failure would be Claude-format JSONL handed to an SDK that
  cannot read it. The cheapest fix is a store root per engine, which makes a foreign record
  unreachable by construction rather than by a check; it belongs with phase 5's mirror, where the
  storage layout is being decided anyway.

---

## Phase 6 — the rest of the consumers (2026-09-02)

The list the entry above left, closed. Everything here was reachable the moment `switch_engine`
landed: five surfaces the descriptor describes and nothing in the browser asked about, on an engine
where all five are `UNBUILT` or `ABSENT`.

| Surface | Where it now hides |
|---|---|
| `session_mirror` | `settings-tab.js` — the session-storage card, and the read behind it |
| `transcript_history` | `chat-panel/rendering.js` — the 📜 button; `history-browser.js` guards the load |
| `slash_commands` | `chat-panel/input.js` — `ensureSlashCommands` returns `[]` |
| `mcp_server_inventory` | `context-usage-tab.js` — `_fetchMcpStatus` returns `null` |
| `account_rate_limits` + `rate_limit_events` | `context-usage-tab.js` — the whole Rate limits section |
| `usd_cost` | `usage-hud.js` turn footer, `context-usage-tab.js` session cost |

### Three granularities, and choosing between them is the work

The Context section set the precedent — hide the whole thing — but applying that everywhere would
have hidden measurements the engine does take:

- **The whole section**, for Rate limits. It has two sources and nothing left when both are absent,
  and a "Rate limits" heading over nothing reads as *you have none* — a claim, where the truth is an
  absence.
- **The entry point**, for history. The 📜 button goes rather than the dialog being taught to explain
  itself: a browser that opens to say it has nothing is a click that can only disappoint. The
  browser's own load is guarded too, because a slash command, a link, or a switch made while the
  dialog was already open all reach it another way.
- **The figure, not its row**, for cost. This is [AG-6](decisions.md#ag-6) at the granularity that
  matters: usage is reported in tokens and no USD is invented, so the turn's tool-call count and
  duration — and the session's per-model token rows — are as true as ever. Hiding them alongside the
  price would take three measurements away to hide the one that was never taken. `turn-cost.js`'s
  "cost unknown" rendering is the wrong instrument here too: *unknown* is a failure to establish a
  price, and this engine quotes none by design.

### The load side matters as much as the render side

Every gate is in two places, and the second is not tidiness. The router **raises**
`UnsupportedOnThisEngine` rather than answering emptily, so an ungated fetch is a guaranteed error —
once per refresh for the Context tab, once per *keystroke* for the slash palette, which retries on
every `/`. `ensureSlashCommands` returning `[]` early also keeps the palette's own "nothing matched"
rendering in charge, which was already the right answer.

### One thing that had to be walked back

The Context tab's first cut **awaited** `loadCapabilities` before its refresh, to avoid spending two
round trips on calls that would be refused. It broke 187 tests, and the reason is the reason not to
do it: the descriptor was now in front of the breakdown, which is the thing that tab exists for.
Reverted to the HUD's shape — fire it, re-render when it lands — so the first refresh may spend those
two calls once and the panels go away afterwards. The loading default is "supported" precisely so
this trade is available.

### What is still not done

- **No live turn**, unchanged, and now the only thing between phases 1, 3, 4 and their exit criteria.
- **`agent_questions`, `subagent_tabs`, `persisted_permission_rules`, `amend_tool_input`,
  `file_checkpointing` and `image_generation` have no consumer yet.** Not an oversight and not the
  same shape as the six above: each is either a surface with no UI on *either* engine
  (`agent_questions`, `image_generation` — see `sdk-surface.md` § *Antigravity capabilities with no
  home in the current UI*) or one whose only caller is already behind a control the descriptor does
  not reach. They need a home before they need a gate.
- **Resume across engines** — unchanged, still unreachable, still phase 5's to close with a per-engine
  store root.

---

## Phase 3 — the live run, and the three bugs it found (2026-09-02)

`scripts/probe_session.py`, built 2026-09-01 with 94 offline tests and never executed. **Exit criterion met on
the first run**: a real `localharness` session, a `list_directory` tool call, its result in the same
sub-message as the arguments, and a `PASS`. The taxonomy printed exactly as phase 3 predicted —
`TEXT_RESPONSE`/`USER` echo, four `TOOL_CALL` frames going `ACTIVE`→`DONE`, then `TEXT_RESPONSE`
frames to `DONE`.

**And then three bugs, none of which any offline test could see.** All three were invisible for the
same reason, which is the finding worth keeping: the fakes described a friendlier SDK than the real
one.

### 1. The turn reported no tokens at all

`turnUsage` came back `{'turn_model_usage': {}}` on a turn that had really billed 8,435 tokens. Under
[AG-6](decisions.md#ag-6) tokens are the whole of what this engine reports **in place of** a cost, so
the descriptor was promising a figure the engine never sent — and the browser, correctly, would have
hidden it forever.

The pump read `Step.usage_metadata`. That field exists and is documented — *"token usage for this
specific step's model invocation, or None"* (`types.py:914`) — and was **`None` on all ten steps**.
The figure lives on `Conversation.last_turn_usage`, which the SDK computes as
`cumulative_usage - turn_start_usage` (`conversation.py:311-319`) and which no step carries. Fixed by
having the session hand it to the translator at turn close, symmetrically with `note_stop_reason`,
because both live on the conversation and neither is reachable from inside `translate`.

Note the near-miss: `sdk-surface.md` **cited the right field all along** — *"`Conversation.last_turn_usage`
— a difference against turn-start"* — and the pump reached for a plausible-looking one on the object it
already had.

### 2. The stop reason was always empty

`_stop_reason()` looked for a public `stop_reason` on the conversation and on its `_connection`.
Neither has one. The SDK spells it `_last_turn_stop_reason` — a property on `Conversation` delegating
to the connection (`conversation.py:326-328`) — and the SDK's own `Response.stop_reason` reads it
through that private path (`types.py:1262`). The underscored names now come first, with the public
one kept after them so a later SDK promoting it is a non-event.

### 3. Fixing #2 exposed a third: `UNSPECIFIED` would have been a red badge

With the reason read correctly, a clean turn reports `UNSPECIFIED` — the SDK's *"default value; normal
completion or unspecified stop reason"* (`types.py:866`). Forwarded as-is it would have been worse
than the empty string it replaced: the browser sends an **unmapped** reason to the card *header* with
`severity: 'error'` (`block-render.js:87-91`), deliberately, because an unrecognised reason is more
likely to matter than not. Every normal turn would have carried a red badge reading "UNSPECIFIED" — a
label that says nothing, in the place reserved for labels that say something is wrong.

Translated to `""` in `note_stop_reason`, which the browser already reads as "the engine named no
reason". A filter with a whitelist would have been the wrong shape; this is one named constant with
the SDK's own docstring as its justification, and `MAX_*_EXCEEDED` and `QUOTA_EXHAUSTED` still get
through — which is the point of AG-6 offering `BudgetConfig` in place of a dollar cap.

### The thing that made all three invisible

`FakeConversation` set `self.stop_reason = None` — **a name the real `Conversation` does not have**.
Every offline test passed against a double that answered to an attribute the SDK never exposed, and a
double that cannot fail the way the real object fails is not standing in for it.

So the fake now carries the SDK's spellings, and
`test_the_fake_matches_the_sdks_shape` asserts the two agree: it reads `Conversation` off the
installed SDK and requires both names on the real class *and* on the fake, skipping where the SDK is
absent so the offline suite stays runnable without it. That is the same instinct as
[§ The probe](sdk-surface.md#the-probe) applied one layer down — the inventory keeps the *surface*
honest, and this keeps the *doubles* honest.

Both new figures verified live on a second run: `prompt_token_count: 8229`,
`candidates_token_count: 106`, `thoughts_token_count: 100`, `total_token_count: 8435`, and a stop
reason that is now correctly silent.

### One thing checked and found already correct

`streamChunk` carries the **whole accumulated block**, not a delta — visible in the probe's output as
each `seq` printing a longer prefix of the same sentence. That is right: `blocks.js:107` documents
"content is cumulative" and the browser replaces by `block_id`. Checked because the probe made it
look like duplication, and worth recording so the next reader does not re-open it.

---

## Phase 6b — the consultation as an agent tab (2026-09-02)

[AG-13](decisions.md#ag-13), and the tier-2 shape rather than the cheap one: the consultation
**streams** into its own tab instead of filling in at the end.

**No webapp change**, which was the exit criterion and is the thing worth checking first. The tab
strip joins on identifiers alone, so the whole feature is server-side.

### What the consultant became, and why that is not AG-R-9 firing

`_chat` no longer calls `agent.chat()`. It drives `conversation.send()` + `receive_steps()` and hands
each step to an optional observer, which the bridge feeds to the **existing** `StepTranslator`.

That reverses AG-7's "it stays a one-shot `async with Agent(...)`", and the amendment argues it out:
the risk was the consultant *inventing* session machinery **ahead of** the engine, so the engine
inherits a shape built for one turn. Phase 3 built that machinery properly, against `Conversation`
directly, and the consultant now consumes it. The direction of dependency was the whole of the risk.

`chat()` is still not called, and that is the other half: it returns a lazy cursor whose read after
`Agent.__aexit__` hung until killed in phase 1.

**The tripwire in `tests/test_antigravity_consultant.py` was rewritten to match the redrawn risk**,
not deleted. `receive_steps` and `cancel` are now expected; what is forbidden is a *second
implementation* — the test asserts `consultant.py` defines no `StepTranslator`, `Event`, `_Block` or
`translate` of its own, and that it reaches the stop reason and usage through the shared readers
rather than the SDK's private attributes.

To make that reuse real, `stop_reason_of` and `turn_usage_of` were lifted out of
`AntigravitySession` into module-level functions taking a conversation. Both were spelled wrongly
once already (phase 3's live run); a second copy would be a second place to get them wrong, found the
same way — live, months later.

### The identity, minted rather than borrowed

An in-process MCP tool handler receives **only its own `args` dict** — no `tool_use_id`, no context
object. So a consultation cannot learn the id of the tool card that invoked it, and the bridge mints
its own. Correlating against the most recent `mcp__aic-dc-antigravity__*` card in the pump would be a
race whose failure mode is attaching output to the *wrong* card.

Accepted cost: the row does not nest inside its spawning card the way a `Task` subagent's does.

### Settling, which is the part that would have been forgotten

`_tab` is an async context manager, and the terminal `subagentEvent` is in its `finally`. The webapp
sets `state.streaming = !row.terminal`, so a consultation that raised without one leaves a tab
spinning for the rest of the session — and a refusal, a timeout and a cancel all take that path. It
also drains the observer's scheduled pushes first, or the terminal event can overtake the text it is
meant to be terminating.

Two tests cover the failure directly: one for the ordinary end, one where the consultation raises.

### A latent bug found while writing it

`_first_output_path` read `chunk.result.output_path` — the `ChatResponse.resolve()` shape. It now
receives `Step` objects, where the path is a *tool-call argument*. Every test passed either way,
because the fakes carried the old shape; only a real image would have shown it. Both shapes are now
tried, which is the same class of near-miss phase 3's live run found three of.

### And one real bug in committed code, found by running the app

`_heavy_init` referenced `capabilities` without importing it into its own scope — the name is bound
in `main()`, a different function. **The whole deferred initialisation died on a `NameError`**, so no
adapter ever received the symbol index and every hover, definition and reference answered "no answer"
for the life of the session.

It was invisible in the way that matters: one traceback at startup, and thereafter it reads as a slow
or empty index rather than as a crash. No unit test touched the attachment loop, because it is
startup plumbing. `tests/test_main_symbol_index_attach.py` is the regression — checked structurally,
since reproducing it needs the real deferred path — and it fails on the pre-fix tree, which was
verified rather than assumed.

### The live run (2026-09-02, same day)

`scripts/probe_consultation_tab.py` drives a real `second_opinion` through the real bridge with a recording
emit, and checks the five-point contract read off `subagent-tabs.js`. **All six checks pass**: 13
chunks arrived progressively, every one carrying the consultation id, turn-scoped to the live
request, with the terminal event last.

Two things the run taught that the offline tests could not.

**The first attempt 503'd, and that was the more useful result.** Google returned *"this model is
currently experiencing high demand"* mid-turn. The consultation failed — and the **terminal event
still fired**, because it is in a `finally`. That is the tab-spins-forever failure mode, exercised
live and against a real provider fault rather than a mocked raise. It is the single thing about 6b
most likely to have been got wrong, and it was right.

**The probe's own check was too narrow**, and the 503 is what exposed it: it counted only
`streamChunk` and reported "the answer did not stream" on a turn that had streamed two
*thinking* chunks before the provider gave up. Thinking renders in the tab exactly as prose does, so
the check now counts both. A green run would never have shown this — it took a turn that produced
thinking and no answer, which is exactly what a transient provider error produces.

### The browser run (2026-09-02) — the tab draws, and it found a real bug

Driven through Chrome against a live `--preview` server in a scratch repo. Three things were
confirmed and one was wrong.

**Confirmed.** The permission dialog fires for `mcp__aic-dc-antigravity__second_opinion` — AG-5's
whole reason for the second server name, seen working rather than argued. The tab appears in the
strip labelled *"Antigravity — Second opinion"*, carries the ⏹ Stop affordance and the read-only
note, mirrors a row into Main, and settles. No webapp file was changed to make any of that happen,
which was 6b's exit criterion.

**Wrong: a failed consultation settled as a green `completed`.** `_tab` announced the terminal event
from a `finally` with a hard-coded status, so a consultation that timed out after 180s reported
success. The webapp maps `completed` straight to a green LED (`subagent-tabs.js` `_TERMINAL_LED`), so
the row read as a clean result for a call that returned nothing. That is the manufactured-success
shape AG-5 and AG-R-3 are both written against, arriving in the one place nobody had looked.

Fixed with `try/except/else`: `completed` is now *earned* by the body not raising, and the handlers
catch **outside** the `async with` so the failure propagates through the manager. `failed` was then
observed live, red LED and all.

**Note what the existing tests did not catch.** All 26 bridge tests passed against the broken
version, because every one of them asserted `terminal` was true and none asserted *what the status
said*. The three new tests close that, and the fix was verified by reverting it alone — with the
tests kept — and watching the new one fail.

### The finding that has nothing to do with our code

**Both browser consultations timed out at 180s, while the standalone probe answered in seconds.** The
harness stderr says why:

```
received model response error: doRequest: error sending request:
Post ".../gemini-3.7-flash:streamGenerateContent?alt=sse": context canceled
```

`context canceled` is our own timeout cutting a request that had been in flight the whole 180s. The
harness started, authenticated and sent; Google never answered. The same model returned 503 *"this
model is currently experiencing high demand"* twice during the probe runs an hour earlier, so the
likeliest reading is provider-side unavailability rather than anything about the query — which is
also what the master engine concluded unprompted after the second failure.

Three consequences worth carrying:

- **A hung provider is indistinguishable from a hung engine, for 180s.** The tab shows a spinner and
  nothing else for three minutes. `DEFAULT_TIMEOUT_SECONDS = 180` was chosen when a consultation was
  a blocking tool call with no UI; now that it has a visible tab, that is a long time to say nothing.
- **The harness's stderr is where the diagnosis was**, and it reaches only the server log. Routing it
  into the tab as a `systemEvent` would have made this self-diagnosing.
- **Neither probe could have found it.** Both run the consultant on a bare event loop; the failure
  needs a real provider having a bad afternoon. That is not a gap in the probes so much as a reminder
  of what they are for.

### What is left

- **A successful consultation has not been watched streaming into the tab.** Both live attempts
  failed provider-side, so the chunk-by-chunk rendering is verified only by
  `scripts/probe_consultation_tab.py` (13 chunks, all carrying the consultation id) and not by eye.
  The tab was *correct* to be empty both times — there was nothing to draw.
- **The browser has not drawn the tab.** The contract is verified end to end on the server; that the
  webapp renders it needs a real session with a browser attached and a Claude turn calling the tool.
  It is the one part of 6b a script cannot stand in for.
- **⏹ Stop is wired to the bridge but not to `stop_task`.** `ConsultantBridge.cancel()` exists and
  reaches `Conversation.cancel()`; nothing routes the RPC to it yet.
- **`usage` rides on the terminal event** and nothing renders it. Tokens only, per AG-6.

---

## Making a stalled consultation legible (2026-09-02)

Three changes, all of them consequences of the browser run above rather than of the plan. The
consultation worked; what failed was every part of *saying so* when it did not.

### 1. The failure carries the harness's own words

The diagnosis on 2026-09-02 — `Post ".../streamGenerateContent": context canceled`, meaning the
request had been in flight the whole timeout and Google never answered — was in the harness's stderr,
which the SDK logs at INFO on the **root** logger and nowhere else. Finding it took a `grep` of the
server log. Nobody using the app could have.

`Consultant._stderr_tail()` now appends the last six lines to a timeout or an SDK error. Read off
`Conversation.connection._stderr_lines`, the bounded deque the SDK already fills, rather than
captured with a logging handler: a handler would be global, would fire on a thread that is not the
event loop, and would pick up every other engine's lines.

**The ordering here was a bug in the first draft and is the reason there is a test for it.**
`_chat`'s except handlers run *after* `_drive`'s `finally` has cleared the live conversation, so a
tail read from that attribute would always have been empty — the feature would have been a silent
no-op, which is the exact failure it exists to prevent, one layer up. The connection is now retained
past teardown for diagnostics.

### 2. Silence is reported while it is happening

A heartbeat `systemEvent` every 20s into the consultation's tab, saying only how long the wait has
been. Deliberately **not** dressed up as progress: the harness is blocked on a socket and there is no
progress to report, and a bar that moves while nothing happens is worse than a number that grows.
Cancelled in the `finally`, with a test that it stops — a background task outliving the thing it
reports on is how "harmless" tasks accumulate.

The failure's reason now also goes **into the tab**, attributed to the consultation. Until now it
went to the *model*, as the tool's text result, which the person watching the tab does not read: the
row went red and said nothing.

### 3. `DEFAULT_TIMEOUT_SECONDS`: 180 → 120

Two live runs sat at the full 180 and returned nothing. The number is not the real fix and does not
pretend to be — silence was the problem rather than duration — but the cost is asymmetric: a
consultation needing more than two minutes is already a bad second opinion, while every extra second
of a hung one blocks the Claude turn that asked.

### 4. ⏹ Stop is no longer decorative

A consultation is a subagent row, so its Stop click arrives at `stop_task` — where the CLI has never
heard of the id. `ClaudeCodeService.stop_task` now routes ids prefixed `consultation-` to
`ConsultantBridge.cancel()`, which reaches `Conversation.cancel()`.

Routed by the id's shape rather than by asking the CLI and falling back on its error, because an
unknown id is not a failure the CLI reports cleanly. A test pins the minting site and the routing
site against each other, since they are two constants that must agree and live in different packages.

### What this does not fix

The provider hanging. If Google accepts a request and never answers, the consultation still waits out
its timeout — it just now says so every 20s, ends 60s sooner, and explains itself with the harness's
own stderr when it gives up.

---

## The hangs were the model, not the code (2026-09-02)

The two 180s timeouts in the browser run were blamed, reasonably, on "the Antigravity side isn't
responding". That was right and not specific enough. Measuring it took three minutes and changes a
pinned default.

### The measurement

A trivial five-token prompt — *"Reply with the single word: ok"* — sent **straight at
`generativelanguage.googleapis.com`** with this free-tier key, bypassing the harness, the SDK and our
code entirely:

| model | run 1 | run 2 |
|---|---|---|
| `gemini-3.7-flash` | 30.9 s | **timed out at 70 s** |
| `gemini-3.6-flash` | 3.1 s | 22.7 s |
| `gemini-3.5-flash` | — | 3.9 s |

A latency ladder by model recency. `gemini-3.7-flash` — what `DEFAULT_TEXT_MODEL` was pinned to since
phase 1 — is effectively unusable on a free key, and an agent turn is many model calls, so *any*
timeout would have been exceeded.

**It arrives as slowness, not as a 429**, which is why nothing in the stack reported it. The SDK's
`retry_config` has nothing to retry; the harness's stderr says only `context canceled`, which is our
own timeout; and `_explain`'s quota branch never fires. A provider rationing capacity by queueing is
invisible to every mechanism built to notice rationing.

### What changed

`DEFAULT_TEXT_MODEL`: `gemini-3.7-flash` → `gemini-3.5-flash`, with the table above in the docstring
so the next reader gets the evidence rather than a bare constant. `scripts/probe_consultation_tab.py`
then passed all six contract checks on the first run, streaming 11 chunks — the same probe that had
been passing intermittently for days.

**This is a free-tier default, not a judgement about the models.** The point of a second opinion is an
independent and *capable* one, and pinning an older model to make it respond is a real cost. It joins
[AG-12](decisions.md#ag-12)'s list of things a paid key should revisit, and it is a constructor
argument so that revisiting is one line.

### Why this took a browser to find

Every probe run before today either passed or failed for a reason that looked like weather — a 503, a
"high demand" notice. The pattern only became visible with two consecutive 180s timeouts on different
questions, which is what a human watching a UI noticed and a passing test suite could not. The
measurement that settled it deliberately used **neither** our code nor the SDK: when the question is
"is it us or them", the answer has to come from a path that contains neither.

### Confirmed by Google (2026-09-02)

The measurement above was put to Google and the behaviour is intended, which turns a guess into a
constraint the design can rest on. Their answer, in the parts that change something:

- **Free tier is best-effort and queues rather than refuses.** *"When backend capacity is highly
  utilized, rather than rejecting requests with a `429 RESOURCE_EXHAUSTED` error, Google queues Free
  Tier requests behind paid traffic."* Newer models carry heavier traffic, which is the ladder
  exactly.
- **There is no availability signal to query.** *"The `models.list` endpoint only verifies
  authorization, not current network load."* Worth recording because it is the endpoint anyone would
  reach for: this key lists `gemini-3.7-flash` and cannot practically use it.
- **60–90 s is their own free-tier guidance**, *"just to survive the queue"* — and they add that *"an
  agent waiting a full minute per turn is practically unusable for interactive work."* Our 120 s sits
  deliberately above their figure: that number is what a request needs to *clear* the queue, so
  timing out at it would abandon calls that were about to succeed.
- **The whole wait is time-to-first-token.** *"The capacity queueing occurs at the routing layer
  before a model is allocated … Once your request finally clears the queue, the tokens will stream out
  at their normal rate."*
- **Billing removes the queueing**, not merely the rate ceiling, and restores fail-fast `429`s.

### The one that changed code

The TTFT detail is the actionable one. A consultation with **no step yet** is queued; one that has
started is merely thinking. Same spinner, opposite meanings — and only the first is something a
reader can act on. So the heartbeat now says which:

> Waiting for Google to start — 40s so far, and nothing has arrived yet. On a free-tier key requests
> are queued behind paid traffic rather than refused, and the whole wait lands before the first token.

and switches to a plain *"Antigravity is working"* once a step arrives. Saying "queued" after the
first token would be the wrong kind of wrong: it would blame the provider for a model taking its time.

### What it means for AG-12

The billing case is now stronger than the two costs [AG-12](decisions.md#ag-12) records, and stronger
on the vendor's own account: the free tier does not merely defer image generation and train on
prompts, it makes an interactive agent *"practically unusable"* — Google's phrase — and it does so
invisibly, because queueing looks like nothing at all. Lowering the model pin buys usability today; it
does not buy back the capability, and a paid key should raise it again.

---

## Phase 4 — the live run, and the four things it found (2026-09-03)

The first conversation ever held through the Antigravity engine as master. The adapter, the gate and
the per-session switch all landed on 2026-09-01 and had never been driven; the phase-4 row said *"no
live turn has run through it, which is now the whole of the gap"*. One has now, and **the exit
criterion is not met**.

Setup: a fresh repo (`calc.py`, two functions), `switch_engine` to Antigravity through the chat
panel's own notice, permission mode **Ask**, one prompt — *"Read calc.py, then add a multiply(a, b)
function that returns a * b. Make only that one edit."*

**What worked.** The engine notice and the amber chip both read correctly, and `switch_engine` did
what AG-1 says it does. The `edit_file` dialog rendered a real side-by-side diff at **+5 −0** with the
correct hunk, so [AG-5](decisions.md#ag-5)'s central claim — that the raw `PreToolCallDecideHook`
carries enough to render a diff — holds in the browser and not only in the phase-2 probe. The write
landed correctly. The step stream drove tool cards as they arrived.

**What did not.** The turn was declared dead in the UI while it was still running, and then edited the
file anyway.

### 1. `chat_streaming` awaits the whole turn, and the browser gives up at 75s

The transcript ended at a single line —

> **ASSISTANT** — **Error:** Timed out waiting for response

— with every tool card that had already rendered *replaced* by it: no diff, no answer, no footer, tab
reading `Main: idle`. Meanwhile the server log ran on to `STATE_FULLY_IDLE` three minutes later and
`calc.py` gained its function.

The two engines implement the same router method with opposite lifetime contracts, and the docstrings
say so plainly:

| | Returns when | Survives a disconnect |
|---|---|---|
| `claude_code/service.py:1232` | *"as soon as the engine has accepted it"* — the turn runs in a background task | yes, *"a client that disconnects mid-turn re-attaches to a turn that kept running"* |
| `antigravity/service.py:318` | after `async for event in session.stream_turn(...)` drains — i.e. the whole turn | no |

The browser's JRPC deadline is 75s (`webapp/src/app-shell/index.js:230`). So **any Antigravity turn
longer than 75 seconds renders a fatal error while continuing to run**, and a permission dialog makes
that a certainty rather than a risk, because the user's own thinking time is inside the budget. Timed
from the log: prompt at 12:30:47, the deadline fired at ~12:32:02 while the `view_file` dialog was
open, the turn finished at 12:35:32.

**This is the worst available failure mode**, and worth naming as such: the app tells the user the
turn failed, destroys the record of what it did, and *then* writes to their file. A user who read that
error and walked away would have an edited working tree and no idea. Everything else on this list is
cosmetic beside it, and nothing downstream is trustworthy until it is fixed. The fix is a port rather
than a design — Claude's implementation is the template.

The same defect costs the reload case: an Antigravity turn does not survive a refresh, and a Claude
one does.

### 2. Every read-only tool call raises a modal

Four dialogs for one edit — `find_file`, `view_file`, `edit_file`, `view_file` — of which exactly one
is a mutation.

`ALWAYS_ASK` and `GATED_BY_DEFAULT` are both computed correctly at
`antigravity/permissions.py:271`, but they only populate the payload's `gated_by_default` field, which
shapes the dialog's *wording*. Nothing consults them before `broker.can_use_tool`, so the gate
forwards every call the `PreToolCallDecideHook` sees, and in Ask mode the broker asks about all of
them. On Claude the CLI decides which calls need `can_use_tool` and auto-allows reads, so the shared
broker never sees them — which is why one shared broker across two engines does not by itself give one
behaviour.

The dialog then states something false. It says:

> read calls are not normally gated. This one is: A deny or ask rule matched, or a hook asked for
> confirmation.

No rule matched. On this engine every read is gated, so the sentence explaining why *this* one is
unusual is the sentence that is wrong. **The harness agrees with Claude, not with us** — its own log
reads `permission_manager.go:917] permissions: skipping check for step 2: handler *handlers.FindHandler
does not declare permissions`, so Antigravity's own permission manager considers `find_file`
permission-free while AIC⚡DC asks about it.

The fix is to consult the existing classification before the broker, honouring deny rules and
`denied_read_files` so the narrowing cannot become a hole.

### 3. The read tools' argument aliases are against names the SDK does not send

The dialog rendered `PATH (none named)` directly above an input block reading
`{"AbsolutePath": "/tmp/ag-repo/calc.py"}`.

`ARG_ALIASES["view_file"]` maps `TargetFile`; the hook is handed **`AbsolutePath`**. `find_file` has
no entry at all and is handed `Pattern` / `SearchDirectory`. The **mutating** aliases are all correct
— `edit_file` really does send `TargetFile`, `TargetContent`, `ReplacementContent`, `Instruction` —
which is exactly why the diff rendered and nobody noticed the read half had drifted.

**The surface probe structurally cannot catch this**, and its own docstring says why: reflection sees
*shape*, and an argument name inside a JSON string is not shape. This is the third finding in this
directory of that kind, after `agy` frames with no content and `policy.ask_user`'s bare bool.

### 4. The hook and the step stream use different names for the same call

Not a defect yet, and the thing most likely to become one:

| Call | Hook `argumentsJson` | Step stream |
|---|---|---|
| `find_file` | `Pattern`, `SearchDirectory` | `findFile.query`, `findFile.directoryPath` |
| `view_file` | `AbsolutePath` | `viewFile.filePath` |
| `edit_file` | `TargetFile`, `TargetContent`, `ReplacementContent` | `editFile.filePath`, `editFile.diffBlock` |

Two vocabularies for one call, and the step stream's paths are `file://` URIs where the hook's are
bare. Recorded in [`sdk-surface.md`](sdk-surface.md) so the next module to read a path picks
deliberately rather than by whichever it met first.

### What this says about the phase

Phase 3's entry ended on *"the fakes described a friendlier SDK than the real one"*. This run is the
same lesson one layer up: **the offline suite described a friendlier engine than the real one**, and
every one of these four is a thing no unit test was ever going to fail on — a lifetime contract, a
call that is asked about rather than allowed, an alias against a name nobody sent, and two spellings
that agree until they do not. The gate for phase 4 is a conversation, and it has to be held.

### The first three fixed, and a fifth found while verifying them (2026-09-03)

**1. The turn lifetime.** `chat_streaming` now admits, spawns
`_run_turn` as a background task, and returns `{"status": "started"}` — the Claude
adapter's shape to the letter. No webapp change went with it, because `input.js`
already reads only `error` / `routed` / `unsupported` from the reply and takes
everything else off the event stream. Two things came with it rather than after:
the turn-in-progress refusal moved *ahead* of the spawn (`stream_turn` raises, but
it is a generator, so its `TurnInProgressError` now lands where no synchronous
refusal can be made of it), and the failure branch emits the translator's own
closing events. With no RPC reply left to carry a failure, the event stream is the
only channel there is, so a path that emits no terminal event is a spinner that
never stops — survivable before only because the browser ignored the status anyway.

**The regression test hangs against the old code rather than failing**, which was
discovered by running it: checking the new tests against the pre-fix
implementation timed out a five-minute command with nothing to show. Every one of
them now goes through a `_start` helper that wraps the call in
`asyncio.wait_for`, so the six fail in 31s with a `TimeoutError` naming the test.
A guard that hangs is worse than one that fails, so the deadline is part of the
assertion rather than a convenience.

**2. The read class.** The gate answers `read` itself and never reaches the
broker with it — `ALWAYS_ASK` is still checked first, so `start_subagent` (class
`delegate`, ungated by default) cannot slip through the narrowing.

**This required wiring a control that had never been connected.** `denied_read_files`
had *no reader at all*: the service stored the list, answered `get_denied_read_files`
with it, and nothing consulted it. That was invisible while every read raised a
dialog the user could refuse by hand — and allowing reads without asking is exactly
what would have turned it into a silent read of a file the user had marked. So the
gate now takes a `denied_reads` callable (a callable, not a snapshot: the list is
toggled from the file tree mid-session) and **denies** a matching read with a reason
the model can act on, rather than asking about it. Directory prefixes match, which
is what shift-clicking a folder means.

**3. The read aliases.** `view_file` gains `AbsolutePath` and `find_file` gains
`SearchDirectory`; `Pattern` is deliberately *not* aliased to a path, because the
dialog promises a file where it says PATH and a glob is not one. `list_directory`'s
inherited `DirectoryPath` turned out to be correct — confirmed on the verification
run's live frame — and `search_directory` remains unmeasured and is labelled so.

### Verified live, and the honest gaps

A second conversation on the fixed build (2026-09-03): `list_directory` and
`view_file` both ran with **zero dialogs**, both tool cards rendered and **stayed**,
and the turn settled with the composer re-enabled. The first run's single
`Error: Timed out waiting for response` replacing the whole transcript did not
recur.

Two things this run did **not** establish, stated rather than implied:

- **The deny path was not exercised live.** The turn died before it reached
  `secrets.env`, so the refusal is covered by unit tests only.
- **The edit dialog was not re-reached**, so the end-to-end criterion — a full
  conversation including an approved write — is still unmet. It failed for a
  reason that has nothing to do with any of this: `429`, *"Quota exceeded for
  metric: generativelanguage.googleapis.com/generate_content_free_tier_requests,
  limit: 20"*. The same free-tier ceiling § *The hangs were the model* and AG-12
  are about.

### 5. A turn killed by a rate limit says nothing at all

Found while verifying the above, and the reason the run's failure was confusing:
the engine explained itself perfectly and the browser dropped it.

The step arrived as `STATE_ERROR` / `SOURCE_SYSTEM` / **`TARGET_USER`** — addressed
to the human — carrying the whole story: the 429, the quota metric, the limit of
20, and *"Please retry in 29.957436016s."* `steps.py` translated it correctly into
a `systemEvent` with `subtype: "engine_error"`. Then
`chat-panel/streaming.js`'s `onSystemEvent` **handles exactly one subtype,
`conversation_reset`, and silently returns for every other**.

So a turn that died of a rate limit renders as two tool cards and a stop, with no
badge, no message and no retry advice — while the payload naming the wait in
seconds sits unread on the window channel. This is not Antigravity-specific in
its mechanism: the handler is the shared chat panel's, and any engine's
`engine_error` meets the same silence. It is Antigravity-specific in how often it
fires, because the free tier's ceiling is 20 requests.

**Fixed the same day.** `onSystemEvent` now renders the three subtypes that carry
something a *user* must read — `engine_error`, `turn_timeout` and `engine_notice` —
as durable transcript cards, with a toast only as the glance. The reasoning lives in
[`chat.md` § Engine Event Routing](../5-webapp/chat.md#engine-event-routing); the
short version is that a toast expires in about three seconds and neither the rate
limit nor the reader's absence does. Repeats are dropped, because one error arrives
as several `stepUpdate` frames for one step — three, in the run that found this.

The forward-compat diagnostics (`unknown_step`, `unknown_message`, `step_unreadable`)
deliberately stay out of the transcript: they are about our reader rather than the
user's turn, and `engine-errors.jsonl` and the Debug section are their home.

**And a sixth thing fell out of building it.** The first attempt appended
`{role: 'system'}` and the card rendered under an **"Assistant"** heading, because
`renderMessage` reads `system_event` for the label and treats every non-`user` role
as the assistant. That is precisely the attribution `steps.py` routes
`SYSTEM_MESSAGE` away from a text block to avoid, arrived at from the other
direction. Five producers already had the flag right; `handleUnsupportedSlash` did
not, and had been telling users the engine's refusal of a slash command in the
assistant's voice. Corrected with the same change.

Verified end to end against a **real live 429** later the same day: the card rendered
under a SYSTEM heading carrying the engine's own words, the HTTP code, the quota
metric and the retry delay, where before the fix the same failure rendered nothing.

**And that verification immediately found the opposite failure.** One rate limit
produced **four** cards totalling ~4,500 characters, because the engine retries and
each attempt reports the same failure with a little more gRPC detail than the last —
four walls of `map[@type:type.googleapis.com/google.rpc.QuotaFailure…]`. A transcript
nobody can read is not an improvement on a transcript that says nothing. So a notice
marked `collapse` now **replaces** the previous card of its own subtype within the
same turn rather than stacking, and the last telling wins because it is the most
complete; the toast fires once per distinct report rather than once per attempt. The
trade is stated rather than hidden — two genuinely different errors in one turn leave
only the second on screen — and `engine_notice` is deliberately *not* collapsible,
because two harness notices are two facts.

The collapse itself is covered by tests and **not** re-verified live, for the reason
in the next section.


### The free tier refuses 20 requests a day, and that is what stopped phase 4

*Measured 2026-09-03, and it corrects a claim in this directory.* § *The hangs were
the model* records Google confirming that free-tier requests are **queued rather than
refused**, so rationing arrives as latency and never as a `429`. That is true of the
*per-minute* limit and it is not the whole story. There is a second ceiling that
refuses outright:

```
quotaId:    GenerateRequestsPerDayPerProjectPerModel-FreeTier
quotaValue: 20
status:     RESOURCE_EXHAUSTED   httpCode: 429
```

Twenty agent *requests* per model per day — not turns, and an agent turn is several
requests. An afternoon of phase-4 verification spent it, and every Antigravity turn
after that failed instantly for the rest of the day.

**This is what leaves phase 4's exit criterion unmet.** One conversation running end
to end *including an approved write* has still not been demonstrated: the write path
was proved on the first run (a real `edit_file` dialog, a correct +5 −0 diff, the
write landing), and the fixed build has been shown to allow reads without asking and
to keep its transcript — but no single conversation has yet done all of it, because
the quota ran out between the two. The deny path is likewise unit-tested only.

It also sharpens [AG-12](decisions.md#ag-12) further than § *What it means for AG-12*
already did. The billing case is no longer just that the free tier is slow enough to
be *"practically unusable for interactive work"* in Google's words; it is that
**twenty requests a day is not enough to test the engine, let alone use it**. Every
remaining phase-4 and phase-5 verification is gated on a paid key or on waiting a day
per handful of turns.

---

## Phase 8 — the gate, before the adapter (2026-09-03)

[AG-14](decisions.md#ag-14) landed and the first thing built under it is the permission gate, for the
reason phase 2 was taken out of order: everything downstream is contingent on it, and on this
transport it is **the only gate there is**. `agy`'s own headless layer auto-denies rather than asking,
so the adapter runs with `--dangerously-skip-permissions` and nothing stands behind the hook.

**What landed.** `src/aic_dc/antigravity/agy/` — a sub-package rather than more files beside
`service.py`, deliberately: `surface.py` derives its `handled` bucket by globbing `*.py` next to
itself, and code in here touches no SDK symbol, so it must stay outside that glob or it would report
SDK surface as covered that is not.

- `registry.py` — which conversations this host owns, as files on disk.
- `hook.py` — the process `agy` runs before every tool call.
- `scripts/probe_agy_gate.py` — the live tripwire.
- `tests/test_agy_gate.py` — 35 tests, nearly all of them failure paths.

**The design decision worth recording is the split.** The obvious shape is to ask the running app and
let it answer "not mine". That is wrong, and the failure case says why: with the host down, *every*
question goes unanswered, and one channel cannot tell "not ours" from "ours but unreachable". Reading
silence as the first ungates our own sessions; as the second, it breaks the user's. So ownership is a
fact on disk and the decision is a question over a socket, and each fails safely on its own terms:

| Situation | Answer | Why |
|---|---|---|
| No registry entry | **allow** | Somebody else's `agy` session. The common case — the hook is global. |
| Entry present, host answers | the human's answer | The dialog did its job. |
| Entry present, host unreachable | **deny** | Ours and unreviewable. A dead host makes our sessions un-runnable rather than un-gated. |
| Payload unparseable, we own nothing | **allow** | Cannot be ours, and refusing would break a stranger's session on a bug of ours. |
| Payload unparseable, we own something | **deny** | Might be ours. |

**The live tripwire passed, and its first version passed for the wrong reason.** The gate denied
*everything*, so the model gave up before proposing a write — leaving "the file is unchanged" true and
meaningless. That is the same shape as the AG-R-12 error two entries up, caught this time before it
was committed. The sharpened version allows the read class so the model can actually reach an edit,
and asserts that a write was refused as well as that the file is intact:

```
tools the gate was asked about: run_command, find_by_name ×8, list_dir, view_file ×6,
                                replace_file_content, grep_search ×2
of those, refused:              run_command, list_dir, replace_file_content
file after the turn:            'ORIGINAL_TEXT'
PASS: 3 write attempt(s) refused across 3 distinct route(s), file unchanged
```

**Three distinct routes.** Denied the edit, the model tried `run_command`, and `list_dir`. That is
[AG-R-11](risks.md#ag-r-11) live on this transport — the same behaviour as `sed -i` and inline
`python3` on the SDK — and it is exactly what the `"*"` matcher is for. A per-tool matcher would have
shipped a gate the model walks around.

**And it surfaced the trap for reusing `permissions.py`:** `agy` and the SDK agree on *argument*
names and disagree on *tool* names — `replace_file_content` not `edit_file`, `write_to_file` not
`create_file`, `find_by_name` not `find_file`, `list_dir` not `list_directory`. The argument names
transferring is the real convenience; the tool names look like they transfer and do not, and the
failure is quiet: an unknown name classifies as `exec`, so the call is still gated, but the dialog
calls a file edit a command and `_diff_tool_for` renders no diff. A gate that holds while the
product's central feature silently degrades. Recorded in
[`sdk-surface.md`](sdk-surface.md#the-tool-names-differ-and-only-the-tool-names--measured-2026-09-03);
a per-transport name map is a requirement of the adapter rather than a refinement.

### The vocabulary, merged rather than kept beside (same day)

The trap above is closed. `agy/tools.py` holds this transport's tool classes, write seam, argument
aliases and diff shapes, and `permissions.py` **merges** them into the tables it already has rather
than consulting a second set. The names do not collide — no SDK tool is called
`replace_file_content` — so one table can hold both vocabularies, and one table cannot disagree with
itself. Two would be the copy that drifts, and the direction it drifts is a mutating tool nobody
gates.

`ALWAYS_ASK` widens from `MUTATING_TOOLS` to the union of both transports' write seams, which broke
`test_the_seam_is_read_from_options_not_restated` — a test asserting `ALWAYS_ASK is MUTATING_TOOLS`.
Restated rather than deleted: identity no longer holds and the property it stood for does, so it now
asserts the seam is *derived from* both modules and equal to their union. A literal set in
`permissions.py` would be exactly the drift it was written to prevent.

Four new assertions come with it, and they check the *class* rather than merely that a dialog appears
— because an unrecognised name is already gated, so the omission this guards against does not ungate
anything. It renders a file edit as a shell command with no diff.

### The host end, and a shipped bug it exposed (same day)

`agy/gate_server.py` is what `hook.py` connects to: one unix socket per session, one connection per
tool call. It owns almost nothing — the queue, the countdown, the localhost rule, the dialog payload
and the diff are all the *shared* `PermissionBroker`'s, reached through the existing
`AntigravityPermissionGate`. So a request raised by `agy` lands in the same `pending()` list and
renders in the same dialog as one raised by any other engine, which is
[`permissions.md`](../3-engine/permissions.md)'s *one ask path* holding across a third transport.

Three things are genuinely this module's, and each is a small surprise:

- **There is no call id.** The hook's JSON carries `conversationId`, `stepIdx`, `toolCall`,
  `transcriptPath`, `workspacePaths` and `artifactDirectoryPath` — the raw protobuf has a `callId`
  and the hook's payload does not. One is composed from the conversation and the step index: unique
  within a conversation, stable across a retry of the same step.
- **`stop()` releases before it closes**, and the order is load-bearing. While the registry entry
  stands the hook *denies* anything it cannot get an answer for, so closing the socket first would
  refuse a tool call racing the shutdown. Releasing first makes it pass through as unowned, which is
  what it is.
- **A stale socket file is removed on `start()`**, because a killed process leaves one and `bind`
  would fail on it — at session start, where it reads as "the engine will not run" rather than as
  stale state.

**And writing the amend test found a bug in shipped code.** `denormalise_args` built its reverse map
with a dict comprehension, so where two source names share a target the *last* won: `CommandLine` and
`Command` both mean `command`, so an amended command went back as `Command` while the engine sends
and reads `CommandLine`.

That failure is silent and worse than an error, because `overwrite`/`modified_args` is a **merge**:
the unrecognised key lands *beside* the real one rather than replacing it, so the original argument
survives. **The user watches themselves edit a dangerous command, allows it, and the command they
edited away runs.** A manufactured record of consent — the same family as
[AG-R-11](risks.md#ag-r-11), reached by a third road, and present on the SDK path since the aliases
were written.

Fixed by preferring the *first* alias, which is why the tables are ordered with the engine's own
spelling first. Four regression tests, and the near-miss is worth naming: nothing on the SDK path
exercised an amend, so it took building a second transport to notice.

### The stream reader, and the capture that had to come first (same day)

`agy/steps.py` translates the CLI's NDJSON into the *same* events the SDK pump emits —
`streamChunk`, `toolUse`, `toolResult`, `systemEvent`, `turnUsage`, `streamComplete` — so the chat
panel needs no branch for a third transport (AG-R-4).

**It could not be written until the stream was captured**, and the capture is the entry's real
content. `sdk-surface.md` recorded the vocabulary from `-p` runs, and against a bidirectional turn
that record was incomplete in three ways, each of which would have produced a pump that was plausible
and wrong:

- **Frames are nested** under their own event name, not flat. Read flat, every field is `None` and the
  turn renders empty *without raising* — the failure `diff_agy_init` was corrected for at 1.1.22, on a
  different frame. `unwrap` is a named function with five tests rather than an inline `.get`.
- **`text_delta` is a real delta, and the SDK's `streamChunk` is cumulative.** The browser replaces by
  `block_id`, so forwarding `agy`'s fragment would render only the last few words of every message —
  and accumulating the SDK's would repeat every prefix. The two transports need *opposite* handling
  and neither mistake raises anything. This pump accumulates, so the browser keeps one rule.
- **`step_type` is not the closed vocabulary it was recorded as.** Three members documented; a plain
  read-a-file turn produced a fourth, `system_message`. An unknown member renders as a notice rather
  than being dropped — the rule `StepType.UNKNOWN` earns on the SDK side, for the same reason.

One absence is load-bearing: `tool_info.output` was **not** present on a completed `find_by_name`,
where the 1.1.22 correction found it for `run_command`. So it is per-tool, and a completed call with
no output is reported complete with none rather than left pending, which would spin forever.

The fixtures are transcribed from the capture rather than invented, because phase 3's lesson was that
a fake describing a friendlier engine than the real one passes every test while the pump is wrong.
28 tests.

**What is not built:** the session itself — spawn, the `init` read, the claim, and cancel. Its
handshake is already proved in `scripts/probe_agy_gate.py`, which does exactly that sequence. The handshake it needs is proved and written down in
the probe: spawn, read `init`, claim, then prompt. That order is forced, because the id is unknown
before `init` and a tool call cannot precede the first prompt.
