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

### The session, and a narrowing that nearly went missing for a third time (same day)

`agy/session.py` holds one `agy` process open across turns — `--print= --input-format stream-json`,
prompts on stdin, frames on stdout — and does the forced handshake: spawn, read `init`, claim, then
prompt. Fifteen tests drive it through a **fake `agy` that is a real subprocess** speaking the real
protocol, because a fake answering in-process would exercise neither the handshake's order nor the
pipes.

**Stop has no halt frame on this transport.** The input protocol accepts one event, `user`; the
binary answers anything else with *"unsupported stream input message event"*. The SDK has
`conversation.cancel()` and there is no counterpart. What there is instead is the gate: ⏹ **starves**
the turn by refusing every subsequent tool call with a reason naming the user's stop, which is the
mechanism the Claude adapter already leans on — `cancel_streaming` releases the turn's open
permissions *before* interrupting, because a released dialog is what makes an interrupt actionable.

The limit is stated rather than discovered: **a turn producing only prose cannot be starved**,
because it asks permission for nothing. Killing the process would stop it and end the session, so
`cancel()` starves and `close()` is the separate, explicit act.

**Two bugs the tests caught, and both were repeats.**

The first is the one worth the entry. `gate_server.decide` called `broker.can_use_tool` **directly**,
bypassing the read-class narrowing that lives in `AntigravityPermissionGate.run` — so every read on
this transport would have raised a dialog. That is precisely the phase-4 defect fixed this morning
on the SDK path, four dialogs for a turn whose only mutation was one edit, **reintroduced by a third
transport within hours of being fixed on the second.** The narrowing is now
`AntigravityPermissionGate.pre_verdict`, called by both, so a fourth transport cannot reintroduce it
by writing plausible code.

The second: a prompt that could not be written to a dead process `return`ed, skipping
`stream_complete` and leaving the browser spinning — the *same* mistake as the SDK adapter's error
path, fixed this morning, made again this afternoon in a different file. Both now close the turn out
whatever happens.

Neither was caught by review. Both were caught by a test asserting on the outcome the user sees.

### Installing into somebody else's configuration (same day)

The gate must live in `~/.gemini/config/hooks.json` — outside this repository, in a file belonging to
Google's CLI that the user may already be using — because workspace-local hooks are not loaded
headlessly on 1.1.25. That is the most invasive thing this project does, and `agy`'s own docs say
global hooks *"fire unconditionally"*, so it is handed every tool call from every `agy` session on the
machine including the user's own.

**What that costs a session that has nothing to do with us, measured.** The answer was nearly
unacceptable and the measurement is why:

| | Per tool call, on the user's own sessions |
|---|---|
| As first written | **~500 ms** |
| After moving the package | **~30 ms** |

The hook lived at `aic_dc/antigravity/agy/`, and **importing `aic_dc.antigravity` alone costs 500 ms
and pulls in the Claude SDK.** Every tool call in the user's unrelated `agy` work would have paid that,
to be told "not mine". So the package moved to `aic_dc/agy/` — `import aic_dc` is as cheap as starting
Python — and the remainder is interpreter startup, which is unavoidable when the caller spawns a
process per call. It also reads better: this transport does not touch the SDK, and
`antigravity/surface.py` globs its own directory for the `handled` bucket, so being outside that glob
was always the point.

Nothing else changes for standalone `agy`: a call from an unclaimed conversation is answered
`{"decision": "allow"}` and never reaches a dialog, a socket or a queue.

**And one failure would have been unacceptable.** `agy` **blocks** a tool whose hook command cannot be
run — exit 127, measured. A stale entry left by a crash, pointing at a virtualenv since deleted, would
stop the user's own `agy` working *entirely*, with an error naming a program they may not recognise.
The installed command is wrapped:

```
<python> -m aic_dc.agy.hook <config_dir> || printf '{"decision":"allow"}'
```

The fallback is sound rather than convenient: `hook.main` exits 0 on every path it controls, including
a denial and including an unexpected exception, so a **non-zero exit means the interpreter never
started** — which means this host is not running, owns no conversations, and allow is the correct
answer. The one case it does not cover is a transient fork failure while a turn is genuinely being
gated; recorded rather than hidden, and the reason `status()` reports a stale install loudly instead of
silently repairing it.

`install.py` therefore answers four states rather than a boolean — `absent`, `current`, `stale`,
`unreadable` — because "installed" and "installed and usable" are different and a settings surface has
to explain the difference. It **merges** rather than writes, preserves every other key, removes only
its own entry, deletes the file only if ours was the last thing in it, and refuses to touch a file it
cannot parse rather than replacing it. 16 tests, most of them about restraint.

### The adapter, and the one place inheritance is right (same day)

`agy/service.py` mounts everything above behind the engine router. It **inherits**
`AntigravityService`, which is the opposite of what this project does everywhere else — the
Antigravity adapter holds a real `ReviewMode` and calls `commit.py` rather than subclassing the Claude
adapter, deliberately, because those are two engines and a shared base invites each into the other's
lifecycle.

Here it is one engine reached two ways. AG-14 calls `agy` a *transport*, not an engine, and the class
says so: two-thirds of the surface is repository, index and review work that is not transport-specific,
and a parallel class would duplicate 31 method bodies whose only content is `return
self._repo.something()`. The copy is what drifts — which is the argument this file would otherwise be
making against itself. What is overridden is exactly what differs: how a session starts, how a turn is
pumped, and what ⏹ does.

**It refuses to start a session without the gate installed, and the refusal is the feature.** `agy`
launches with `--dangerously-skip-permissions`, which is safe *only* while our hook is in the user's
global configuration. A session started without it is not a degraded experience — it is an agent
editing the tree with nothing in the way. So `connect_engine` answers `gate_not_installed`, names the
file, and says the gate is removed again on shutdown. `stale` is refused as well as `absent`: a hook
pointing at another checkout gates *that* build, not this one.

Two smaller declarations: `resume` is declined rather than silently starting a fresh conversation, and
`gate_status()` is public so the settings surface can ask its whole question without starting anything.

### The settings surface, and where it turned out to belong (same day)

The panel that asks. It is the only control in the app that writes **outside the repository**, so the
wording is the feature and the tests assert on what it *says*, not only on what it does — it names the
file, says it is outside the project, counts the user's own hooks it will leave alone, states the
~0.2 s per tool call that **every** `agy` session on the machine pays including ones started in a
terminal, and says it is removed on shutdown.

The four states `status()` reports are rendered as four, not as a checkbox: `absent`, `current`,
`stale`, `unreadable`. A stale gate is explained rather than silently taken over — it usually means a
second checkout is also installed, and seizing the hook would break whichever one the user was using.
An unreadable file gets no button at all, because offering an action that will refuse is worse than
not offering it.

**And a cross-cutting test moved the methods.** `test_the_browser_calls_nothing_the_server_does_not_expose`
went red: the panel called `ClaudeCodeService.gate_status`, which exists only when the `agy` transport
is mounted, and nothing mounts it yet. The guard was right, and the fix it forced is better than the
design it rejected.

**Installing the gate is a machine setting, not an engine capability.** What it changes is the user's
own `agy` configuration, not a property of a running session — so it must be answerable and reversible
with **no engine running at all**, and on an engine they are not currently using. It lives on
`Settings` now. `AgyService` keeps only the half that genuinely is the session's: removing the gate on
`shutdown`, so the cost is paid while it buys something.

That is a distinction the panel would have got wrong on its own, and the test found it by asking a
question about the wire rather than about the design.

**Phase 8's transport is complete**: registry, hook, vocabulary, host socket, stream reader, session,
installer, adapter and settings surface, at 4,299 Python and 4,407 webapp tests. What has *not*
happened is a live conversation through it end to end — the gate is proved against the real binary,
and the turn path is proved only against a fake. That run is phase 8's exit criterion and it is the
next thing to do. The handshake it needs is proved and written down in
the probe: spawn, read `init`, claim, then prompt. That order is forced, because the id is unknown
before `init` and a tool call cannot precede the first prompt.


---

## The 200 ms that was 30 ms (2026-09-04)

A measurement error worth its own entry, because a design decision was made against it.

`install.py` recorded the gate as costing **~200 ms per tool call** on every `agy` session on the
machine. It costs **~30 ms**, against ~10 ms for starting Python at all. The figure was taken through
`uv run`, which adds ~170 ms of its own startup — and `uv run` is **not what gets installed**:
`hook_command` writes `sys.executable`, the virtualenv's interpreter. The convenience wrapper was
measured instead of the command under test, and it overstated the cost sevenfold.

**What it bought was the wrong design.** Uninstall-on-shutdown existed *because* 200 ms per tool call
seemed too much to leave running — the tax should be paid only while it bought something. At 30 ms
that argument does not hold, and what it cost instead was a Settings toggle that silently un-set
itself and a next session refusing to start because the thing the user switched on had been taken
away behind them.

**So the gate is sticky (user, 2026-09-04).** `uninstall` is reached only from Settings, by the person
who put it there. The drawbacks are real and smaller than the one being traded away: the 30 ms is
permanent rather than session-scoped, and an entry left pointing at a deleted virtualenv persists —
harmlessly, because the `||` fallback answers *allow*, and visibly, because `status()` reports
`stale` rather than repairing it.

A shell fast-path was measured and **declined**: testing the registry directory before starting Python
answers an unowned call in ~0 ms rather than 30 ms. It was not adopted because it puts a third branch
of globbing shell into the one command whose correctness is the whole gate, and 30 ms does not buy
that risk.

**And one test was reading the developer's machine.** `test_a_turn_is_refused_when_the_gate_is_not_installed`
did not patch `GLOBAL_HOOKS`, so it asserted against the real `~/.gemini/config/hooks.json` — green for
a day because that file was absent, red the moment a gate was installed for real. Now pointed at a temp
path, like its neighbours.


---

## The `agy` transport, driven from the browser (2026-09-04)

**It works.** Claude, then a switch to *antigravity (subscription)*, then a turn on the paid account —
reported by the user after the two defects below were fixed. That is the transport end to end through
the UI rather than through a probe.

Three things had to be fixed first, and all three were found by running it rather than by 4,300
passing tests.

**The SDK's model name.** `AgyService` inherited `options.DEFAULT_MODEL` and passed it as `--model`.
`agy` bakes reasoning effort into the name and rejects the bare form, so it exited before its init
frame on *every* session. `sdk-surface.md` had recorded that disagreement on 2026-08-30.

**The discarded message.** `_record_error` answers `{error: "engine", message: …}` and `input.js`
rendered the *code*, so the whole failure read as **"Error: engine"** while the sentence sat unread in
the same payload.

**A selector that lied.** After a refused switch the `<select>` kept showing the engine the user picked
— Lit re-applies `.value` only when the bound value changes, and `active` had not — while the session
stayed on the old engine and the refusal sat inside a dismissible notice. `live()` and a toast.

That last one is the third instance in two days of one rule, now an invariant in
[`chat.md`](../5-webapp/chat.md): **a control that reports its own success must read its state back
from the thing it changed**, not from the input the user gave it.

### The model surface (same day)

Setting the model to `None` was the right fix for the crash and left a hole: `get_model` answered
`{"model": None, "models": [None]}`, so the picker showed one blank entry on the subscription engine.

`agy models` is read once — it is a subprocess, and the answer belongs to the account rather than the
session — and its 14 ids are served through the existing contract. The labels it also returns are
dropped: `get_model`'s shape is a list of names on every engine, and a second shape for one transport
would make the picker engine-aware, which is AG-R-4.

**`set_model` validates against that list**, and that is the substance rather than a nicety. An
unrecognised name does not fail at selection; it fails at the *next session start*, as `agy` exiting
before its init frame — which is precisely the failure above, and `options.DEFAULT_MODEL` is exactly
such a name. Refusing it here turns a day of diagnosis into a sentence.

An unreadable list means **unknown**, never *none*: a picker blank because a subprocess timed out
looks identical to a transport with no models, and refusing every name on that basis would strand the
user. So an empty list validates nothing.

The Claude models Google routes to — `claude-sonnet-4-6`, `claude-opus-4-6-thinking` — are offered
rather than hidden. `sdk-surface.md` warns that surfacing them naively makes "which engine am I
talking to" unanswerable; the engine selector now answers it, reading *antigravity (subscription)*
beside them.

### Closed: the model picker was empty because `models` was the wrong shape (2026-09-04)

**The cause was in neither the transport nor the event chain, and the section below excluded every
place it was not.** `get_model`'s `models` is a list of **objects** — the Claude adapter returns the
CLI's own `{value, displayName, resolvedModel, description}` dicts — and `AgyService` returned a list
of bare **id strings**. `settings-tab.js`'s `modelEntries` opens with `if (!m || typeof m !==
'object') continue`, so all fourteen names were dropped one at a time, `entries` came out empty, and
`offline` — which is literally `entries.length === 0` — disabled the select and printed *"The engine
has not connected yet…"*.

So every observation in the note below was true and none of them was the fault. The backend did
answer with the fourteen ids; the event did reach the window; `_loadModel` did run and did assign
them to `this._models`. The list was thrown away one layer further on, at render.

**The wrong belief is quotable, which is why it survived a fix.** `1b48ba5`'s message states that
*"`get_model`'s shape is a list of names on every engine"* and drops the display labels `agy models`
prints in order to honour it, citing [AG-R-4](risks.md#ag-r-4). The shape is a list of objects; the
labels were always welcome, under a `displayName` key the Claude CLI already fills in. Dropping them
was harmless — returning strings was fatal — and the two came from one misreading.

**It was invisible to the tests because they asserted on the wrong side of the boundary.** The Python
test was named `test_the_ids_are_offered_and_the_labels_dropped` and pinned the string list as the
contract; the webapp tests added in `9eb4a89` asserted on `el._models`, the raw array, and defined a
`modelSelect` helper they never called. Both suites were green for the whole of the two reports. The
tests now assert on the rendered `<option>` set, and the name-only case fails on the old renderer
with `expected [] to deeply equal …` — the reported symptom, reproduced.

The fix is in three places, and the third is the one worth arguing about:

- `AgyService._list_models` returns `{value, displayName}` per model, keeping the label.
- `AntigravityService.get_model` had the same defect one line long — `models: [self._model]`, a bare
  string — so the SDK transport's one-entry menu rendered empty too. It returns an object, and an
  empty list rather than `[None]` when no model is set.
- `modelEntries` **normalises a bare string** to `{value}` rather than skipping it. This is defence
  in depth and not a second contract: a transport answering with names is wrong, but the honest
  failure is an option with no label, never a picker that blames the engine for not connecting.
  Silently dropping an entire populated list is what made this cost two sessions.

**The general lesson, because this surface has now had it twice.** `offline` conflates *"the engine
has told us nothing"* with *"we could not read what it told us"*, and it renders the first. A caption
that names a cause is an assertion, and this one was wrong while being the most prominent text on the
panel — it is what sent both diagnoses at the transport. The note below is kept whole because its
three hypotheses were reasonable, all three were wrong, and the shape of that error is the finding:
every one of them was about *whether the data arrived*, and none about *what was done with it once it
had*.

### The note as it stood, before the cause was found (2026-09-04)

**Reported twice, and the second report is after the fix that was supposed to close it.** With
`agy` master — the chip reading *⚠ antigravity (subscription)* — Settings shows an **empty model
select**, disabled, above the note *"The engine has not connected yet, so it has not said which models
it offers — the list arrives with the first turn."* That note is the `offline` branch, and `offline` is
literally `entries.length === 0`, so the browser has no list. (The ⚠ on the chip is the unfinished-engine
marker and is expected; it is not this.)

The first report was diagnosed as `settings-tab`'s `engine-changed` handler updating the active
engine's *name* and nothing else, and fixed in `9eb4a89` by clearing and re-reading. The symptom
survived that, so the diagnosis was incomplete rather than wrong — the clear-and-re-read is still
correct, it just is not the whole path.

What is excluded by measurement rather than by argument:

- **The backend answers.** `AgyService.get_model()`, probed in-process, returns the 14 ids with
  `model: None`. `agy models` exits 0 and prints `id<TAB>Label`; its *"Fetching available models…"*
  banner goes to stderr, which is already discarded, so it is not being parsed as a fourteenth-and-a-half
  model name.
- **The router does not refuse it.** Neither `get_model` nor `set_model` is in `RPC_SURFACES`, so
  both delegate to whichever adapter is master; there is no `UnsupportedOnThisEngine` path here.
- **The event chain exists end to end**: `_announce_engine` → `engineChanged` → `app-shell`
  re-dispatches `engine-changed` on `window` → `settings-tab._onEngineChanged` clears and calls
  `_loadModel`.

So the failure is between the browser making that call and the list arriving — which leaves these,
in the order worth probing:

1. **The window was running code older than the fix.** `--dev` runs Vite, so a reload picks the edit
   up, but a window left open across the commit does not. The cheapest thing to check first, and the
   one that would make this a non-defect.
2. **`_loadModel` returned early on `!this.rpcConnected`**, or `get_model` came back with an
   `error` — both paths `console.warn('[settings] get_model failed', …)` and leave the list alone.
   **The browser console is the next artefact needed**; nothing else distinguishes them.
3. **`engine-changed` never reached this window.** The chat panel listens on the same event and
   clears its transcript, so *did the conversation clear on the switch* is a free observation that
   separates "the event did not arrive" from "the reload ran and got nothing".

Worth stating plainly because it shaped the wrong first diagnosis: with **Claude** master and no turn
yet run, this panel is *also* empty, for the honest reason the note gives. Empty-before and
empty-after look identical on screen, which is why the switch appeared to be the thing that broke it.

### What phase 8 still owes

> **Superseded 2026-09-05 for the first half — see
> [§ The approved write](#phase-8--the-approved-write-and-what-running-it-found-2026-09-05).**

Its exit criterion, both halves, neither demonstrated:

- a conversation on the subscription **including an approved write**, so the dialog renders a real
  diff from `replace_file_content` and the edit lands;
- a **second `agy` session of the user's own, running concurrently and never intercepted** — the half
  that has had no test at all, and the one that matters for trusting a hook installed globally.

---

## Phase 8 — the approved write, and what running it found (2026-09-05)

**The first half of the exit criterion is met, in a browser, on the paid subscription.** Not through a
probe: a real dialog, answered by a person clicking **Allow once**.

```
10:23:55  run_command  pwd && ls -la                        → allow
10:24:03  run_command  find /home/flatmax -name target.txt  → deny, with a reason
10:25:16  replace_file_content  target.txt                  → dialog, +1 −1
10:25:37  …                                                 resolved as allow by localhost
          target.txt on disk: MODIFIED_TEXT   git diff: 1 insertion(+), 1 deletion(-)
```

The dialog rendered `replace_file_content` **as a write with a side-by-side diff** — `−ORIGINAL_TEXT`
against `+MODIFIED_TEXT`, counted `+1 −1` — which is the thing
[§ The tool *names* differ](sdk-surface.md#the-tool-names-differ-and-only-the-tool-names--measured-2026-09-03)
warned would degrade silently if the per-transport name map had a hole. It does not.

**The deny path was exercised without being planned, and it is the better half of the record.** The
agent's second move was `find /home/flatmax -name "target.txt"` — a search of the whole home
directory for a file that was in its own cwd. Denied with a reason naming the working directory, it
adapted and edited the right file. That is [AG-R-11](risks.md#ag-r-11)'s route-around instinct being
*steered* rather than escaping, and it is the first time the reason-carrying deny has been watched
changing an agent's course on this transport.

### Set up the way the previous entries said to, and that mattered twice

Driven from a **second, freshly started** server — `--server-port 18081 --webapp-port 19000` — because
the instance hosting the session is both the software under test and the thing whose child process the
session is. And pointed by `--repo-path` at a **throwaway git repo**, `/tmp/temp/agy-write-test`,
rather than at this working tree: the demonstration has an agent editing a file, and the working tree
is where the work is.

### AG-R-3, live, and the tripwire it had already been quietly holding up

The isolation probe was written at the same time and **failed on its first two runs — for a reason
that had nothing to do with the gate.** Its assertion "the stranger never reached our gate" passed
immediately and every time: 9 calls on the first run, 15 on the second, **every one of them ours**.
What failed was the *other* assertion, that the stranger's own work completed:

```
[probe] the gate decided 15 call(s), for conversation(s) {'ad55c68b-…'}   ← ours only
[probe] stranger's run status: SUCCESS
[probe] FAIL: the stranger's file was never written
```

`SUCCESS` with no file is [AG-R-3](risks.md#ag-r-3): the probe's workspaces were plain `/tmp`
temporary directories, `/tmp` is not in `trustedWorkspaces` (`/tmp/temp` is, one path component
away), and `agy` had written `stranger.txt` into `~/.gemini/antigravity-cli/scratch/` while reporting
success. Confirmed by finding the file there, timestamped to the run.

**That is a hole in `probe_agy_gate.py`, and it is the finding worth keeping.** The deny tripwire's
entire assertion is *the target file is unchanged*. Under diversion that is true **whether the gate
denied the write or waved it through** — so its recorded PASS rested on an assumption nobody had
checked, and would have kept reading green through a gate that had stopped working. It is the same
"passed for the wrong reason" failure the probe was rewritten once to avoid, arriving by a second
road: the first time the model never proposed a write, this time the write could not land anyway.

`scripts/_agy_probe_support.py` now owns the setup for all three probes and **raises rather than
warns** when it cannot find a trusted workspace, because the failure it prevents is a green test that
means nothing.

**A correction to that helper, worth its own paragraph.** Its first version took the first trusted
root it found — which on this machine is `culvertHouse`, **a real project** — so a helper written to
make write probes safe had arranged for an agent to run loose in the user's own repository. It now
refuses anything outside the system temp directory.

### The trusted workspace was not the whole story, and the first two explanations were wrong

Being under a trusted root turned out to be necessary and **not sufficient**, and the two hypotheses
tried before the evidence was read properly are recorded because each was reasonable and each cost a
subscription turn.

1. *"`/tmp` is untrusted."* True, and it was not the cause: moved to `/tmp/temp`, which **is**
   trusted, the write diverted again.
2. *"The working demonstration was a git repo and the probe was not."* Also true, also not the cause:
   `git init`-ing the probe's workspaces changed nothing.

What every diverted file ever recorded has in common is that it was **newly created** — `probe.txt`
(2026-08-30), `hello.txt` and `test_hello_world.py` (2026-09-04), `stranger.txt` on all three runs
here. Against that, the browser demonstration *edited an existing* file and the edit landed. So the
working reading is that a bare filename handed to `write_to_file` is not resolved against the
session's cwd, and the trusted-workspace story was the most visible correlate rather than the
mechanism.

**That is a reading of the evidence and not a measured rule.** It has not been isolated with a
controlled probe, and "creation and modification are trusted differently" is not excluded. It does
change what a probe must do: seed the file and ask for an *edit*, which both live probes now do.

It also puts a question against [AG-R-3](risks.md#ag-r-3) as currently written, which attributes the
diversion to `trustedWorkspaces` alone. The risk is real either way — a write reported as successful
that is not where the user thinks — but its stated trigger may be wrong, and a mitigation aimed at the
trust list would then miss.

### A shipped bug, found by reading the log rather than by a test

`AgyTranslator` had no `stats` attribute. `AntigravityService._note_permission_prompt` — **inherited**
by `AgyService` — does `translator.stats.permission_prompts += 1`, so **every permission dialog on
this transport** raised `AttributeError` there:

```
ERROR aic_dc.claude_code.permissions: Could not record the permission prompt on the turn
  File ".../antigravity/service.py", line 466, in _note_permission_prompt
    translator.stats.permission_prompts += 1
AttributeError: 'AgyTranslator' object has no attribute 'stats'
```

It was caught and logged, so the gate kept working, the dialog kept rendering, the tool card kept its
`gated` badge, and only the turn's prompt count was lost. **4,317 tests stayed green through it**, and
so did two live browser runs, because nothing asserted on a count that nothing displayed prominently.

Fixed by giving the translator the *same* `TurnStats` the SDK transport's translator carries rather
than a second one, and folding `_tool_calls` onto it — one counter, so a HUD and a `streamComplete`
payload cannot disagree about one turn. Three tests pin it, written as the *caller* writes it, and
they fail against the old code.

The near-miss worth naming: this is the second defect in phase 8 that inheritance produced and tests
did not see. The first was `denormalise_args` preferring the last alias; both are shared code meeting
a second transport whose shape nobody re-checked.

### And a third, an hour later, which was not survivable

The user restarted onto the fixed build and the browser could not render the engine at all:

```
ERROR aic_dc.rpc: RPC ClaudeCodeService.get_current_state() failed:
      'AgySession' object has no attribute 'read_only'          ← three times in one page load
```

`AntigravityService.get_current_state` and `get_engine_status` both do `session.read_only`,
`AgyService` inherits both, and **neither catches** — so where the `stats` bug cost a prompt count,
this cost the whole app-state load.

`AntigravitySession.read_only` is `self._decide_hook is None`. The `agy` counterpart is the gate: this
transport runs with `--dangerously-skip-permissions`, so `AgyGateServer` is the only thing between the
model and the tree, and no gate would mean no way to review a write. `AgySession.read_only` is
therefore `self._gate is None`.

**Three instances of one pattern is a pattern, so the test is written against the pattern.** Inheriting
a method also inherits every attribute that method reads off objects the subclass supplies, and
nothing enumerated those: `tests/test_agy_service.py` now pins the *session contract*
(`conversation_id`, `started`, `read_only`) as a list, asserts the SDK session answers the same names
so a new one is noticed here, and calls `get_current_state` to reproduce the reported symptom exactly.
The adapter test above it pins which **methods** exist, which is a different contract and is why it
stayed green through all three.

### The second half, met the same day

```
[probe] ours     : c7090b73-a6c8-4161-aced-8199294f6fec
[probe] stranger : be99f0b7-7113-44bb-b55f-b30052d6364b
[probe] the gate decided 9 call(s), for conversation(s) {'c7090b73-…'}
[probe] PASS: 9 call(s) of ours gated, 0 of the stranger's, its work completed, 13.2s of overlap
```

**Phase 8's exit criterion is met in both halves.** A second `agy` session belonging to the user, in
its own workspace, ran concurrently with one this host owned and was **never intercepted** — while a
gate installed in the user's *global* `hooks.json` was firing on every tool call on the machine. That
is what `conversationId` isolation was designed for and the first time it has been shown working
against a real second session rather than in a unit test.

Four assertions, and three of them exist because the fourth is easy to pass by accident:

- the stranger's `conversationId` never appears in the gate's record — the one that matters;
- the stranger's own work **completed**, so it was not stalled on
  `hook.SOCKET_TIMEOUT_SECONDS` or denied;
- **our own** calls did reach the gate in the same window — the control, without which the whole
  thing passes trivially against a hook that is not installed at all;
- the two turns **overlapped in time**, measured at 13.2s, since sequential sessions would not test
  concurrency and the registry is keyed per conversation precisely so simultaneous ones can disagree
  about ownership.

The only change between the three failing runs and this one was **seeding the stranger's file and
asking for an edit** instead of asking it to create one, which is the strongest evidence for the
reading in the section above: nothing about the gate, the workspace or the trust list moved.

### The probes, as they now stand

`scripts/probe_agy_write.py` was also run end to end, and it is the regression harness the browser
demonstration cannot be:

```
[probe] dialog: run_command ×11 [exec] → allow
[probe] dialog: replace_file_content [write] → allow
[probe] tools the gate decided: run_command ×11, view_file, replace_file_content, view_file
[probe] diff: target.txt  +1 −1  new_file=False
[probe] PASS: replace_file_content was presented as a write with a real diff (+1 −1), approved,
        and the edit landed
```

Worth reading the two lists against each other: `view_file` appears in what the **gate decided** and
never in what raised a **dialog**. That is `pre_verdict` narrowing reads away from the modal — the
defect fixed on the SDK path on 2026-09-03, which this transport could have reintroduced and did not.

It asserts four things rather than the obvious one, because the obvious one is weak: that a dialog was
raised at all; that it was classified `write` and not `exec`; that it carried a real diff with both
texts and a non-zero count each side; and only then that the file changed. The first three would pass
against a gate that rendered beautifully and dropped the answer; the fourth alone would pass against a
gate that never ran.

### What phase 8 still owes, as of this entry

- The `agy` version in the specs is stale: these runs were against **1.1.26**, where
  [`sdk-surface.md`](sdk-surface.md) records 1.1.22 and 1.1.25.
- ~~The webapp calls surfaces the descriptor says are hidden.~~ **Fixed the same day — see
  [§ The startup wall of ERROR](#the-startup-wall-of-error-and-the-two-reasons-for-it-2026-09-05).**
- **The dialog offers no "always allow"**, reported from a live `run_command` on 2026-09-05. Now
  [AG-15](decisions.md#ag-15) and phase 9: the reasoning that put `suggested_rules: []` there was
  sound about the *engine* and stopped one sentence short of the conclusion `sdk-surface.md` had
  already drawn — that AIC⚡DC owns persistence. `derive_suggested_rules`' no-suggestions fallback and
  `pre_verdict` are the two pieces that make it small.

---

## The startup wall of ERROR, and the two reasons for it (2026-09-05)

Every page load on the `agy` engine produced this, in the server log and the browser console:

```
ERROR aic_dc.rpc: RPC ClaudeCodeService.get_context_usage() failed: … the agy engine cannot feed …
ERROR aic_dc.rpc: RPC ClaudeCodeService.get_context_usage() failed: …
ERROR aic_dc.rpc: RPC ClaudeCodeService.get_mcp_status()     failed: …
ERROR aic_dc.rpc: RPC ClaudeCodeService.get_account_usage(False) failed: …
```

Every one of those refusals is **correct**. The router raises `UnsupportedOnThisEngine` rather than
answering with a synthesised empty value, which is [AG-9](decisions.md#ag-9--engine-specific-surfaces-are-hidden-never-stubbed)
working exactly as designed — and each message says so in its own words: *"the panel should be hidden
rather than calling this."* Something asked anyway, four times, before the user had done anything.

**Why it is worth fixing despite being cosmetic.** It is the same failure AG-9's amendment already
named once: hiding twelve surfaces at once reads as a broken build rather than as a different engine.
A wall of red `ERROR` at startup reads worse. It also trains the reader to ignore the log, on the one
transport whose gate is the only thing between a model and the working tree.

### Two causes, and the second is the interesting one

**1. One fetch had no guard at all.** `_refreshBreakdown` called `get_context_usage` with no
capability check, while `_fetchAccountUsage` and `_fetchMcpStatus` on either side of it both had one.
Neighbours that do the right thing are good camouflage for one that does not.

**2. The guards that existed were consulted but not awaited**, and that is a genuine design seam
rather than an oversight. `supports()` answers **true while the descriptor is still loading**, and
`engine-capabilities.js` argues for that default at length: answering `false` would hide every panel
for one RPC round trip on the shipped engine, and a panel that draws and then hides costs nothing
because every reader already tolerates absent data.

That reasoning is sound **for a render path and only for a render path.** A fetch is not undoable by a
later re-render: by the time the descriptor arrives, the request has gone and been refused. The same
file even anticipates the consequence — *"a fetch that slips through during load fails loudly instead
of drawing a synthesised zero"* — and treats it as acceptable. Four red lines per page load is what
"acceptable" turned out to look like.

So the rule is now explicit: **render paths consult the descriptor, fetch paths await it.**
`await loadCapabilities(host)` before the guard, which costs at most one round trip for the whole page
because the promise is cached and shared.

### A bug introduced while fixing it, caught by an existing test

The first version put the `await` between `_fetchContext`'s in-flight check and the line that sets the
flag — so two overlapping polls could both pass the guard and issue two control requests, which is
precisely what the flag exists to prevent. `collapses overlapping fetches into one control request`
failed, and the ordering rule is now stated where the flag is claimed: **claim, then await.**

### What the tests assert, and why it has to be the handler

`188` context-tab tests failed on the first run for a duller reason: the shared `settle()` helper loops
a fixed number of microtasks for "the whole chain", and the chain grew a hop. Raised from 6 to 12,
with the reason recorded there rather than left as a bumped constant.

The four new tests assert **the RPC handler was never called**, which is the only thing that separates
a guarded fetch from a refused one — both leave the panel empty, so asserting on the panel would pass
either way. Three cover the missing guard with the descriptor pre-loaded; the fourth leaves it unloaded
and lets the component fetch it over RPC, which is the only one that catches the unawaited guard, and
is what a real page load does. Both fail against the old code. A fifth is the control: with the surface
supported, the call still goes out — without it, a guard that refused everything would pass the rest
while breaking the shipped engine.

---

## Phase 9 — "Always allow" on Antigravity (2026-09-05)

[AG-15](decisions.md#ag-15) built. The dialog on this engine offered `Allow once` and `Deny`; it now
offers a standing rule, and AIC⚡DC keeps it.

**What landed.** `src/aic_dc/antigravity/rules.py` — the store, the matching, and the derivation —
plus three wiring points in `permissions.py`: `_build_payload` offers the rules, an overridden
`_to_result` persists the chosen one, and `pre_verdict` consults the store before anything else.

### The prediction AG-15 made about the webapp was almost exactly right

It said **no webapp change**, and that if one were needed the rule shape had been got wrong. One line
was needed and it is not the shape: `DESTINATION_FILES` gained a label for the new destination, because
the dialog renders "→ *where the rule went*" beside the rule and every existing entry names a
`.claude/` settings file. Reusing `localSettings` would have been a plain lie about where the grant
lives. The shape, the `allow_always` action and the control that sends it all already existed.

### Matching is exact, and that is the whole of the safety argument

The only bug this feature can have is an ungated write, so every choice is the narrow one:

| Rule | Matches | Does **not** match |
|---|---|---|
| `rm -rf build/` (literal) | that command | `rm -rf /`, `rm -rf build`, `rm -rf build/ /` |
| `git push:*` (prefix) | `git push`, `git push --force origin main` | `git pushover`, `git pull` |
| `src/a.py` (path) | that file, that tool | `src/`, `src/b.py`, `src/a.py.bak` |

**Path rules are keyed on the tool name, not the tool class**, and that is the conservative choice
rather than the convenient one. Both transports have two tools that write a file — `edit_file`/
`create_file`, `replace_file_content`/`write_to_file` — so matching by class would let a grant the user
made *by reading a diff* also permit a whole-file overwrite they never saw. The cost is one extra
prompt the first time the agent reaches for the other tool. Being too narrow costs a click; being too
wide is an unreviewed write.

**The matching data rides on the rule dict** under `aic_dc_match`, written when the rule is derived and
the resolved path and parsed command are already in hand. The alternative is re-deriving them from
`rule_content` at match time — unescaping gitignore metacharacters and re-parsing a prefix pattern in
the one code path whose failure mode is granting more than was clicked. The dialog echoes the dict back
verbatim, so the extra key survives the round trip for free.

`derive_rules` is **not** `claude_code.permissions.derive_suggested_rules`, and the reason is the trap
AG-15 named in advance: that function's path branch is keyed on `_RULE_TOOL_FOR_PATHS`, a table of
Claude tool names mapping to the tool the *Claude CLI* consults — both halves meaningless here. Fed an
Antigravity name it returns nothing, so the control would silently never appear for file edits. What
*is* reused is `_derived_command_rules` and `_derived_path_rule`: the prefix-splitting and the
gitignore escaping encode decisions that took a CLI-behaviour investigation to get right, and a second
copy would drift toward granting more.

### `pre_verdict` checks the store before `ALWAYS_ASK`, deliberately

Every write tool lives in `ALWAYS_ASK`, so checking the store after it would mean the one control the
user pressed had no effect on the calls they pressed it for. Safe **only** because matching is exact.

One ordering inside that: a **denied read still beats a standing allow**. Shift-clicking a file in the
tree is a later and more specific instruction than a rule granted earlier, and the newer one holds.

### A test restated rather than deleted

`test_always_allow_degrades_to_allow_once_by_construction` asserted `suggested_rules == []`, on the
rule that *an offer the engine cannot keep is worse than no offer*. That rule still holds; its premise
no longer does. It is now `test_always_allow_is_offered_and_kept`, and it asserts **both** halves —
offered, and written — because asserting only the first would pass on exactly the silent discard the
original existed to prevent. Same treatment as `ALWAYS_ASK is MUTATING_TOOLS` when the seam widened.

### Two mistakes made while building it, both of a kind

**The tests wrote standing permission grants into the developer's real `~/.config/aic-dc`.** Three
entries, keyed by `pytest` temp directories, in the actual store — noticed only by opening the file by
hand. `store_path` takes its config directory rather than resolving one, exactly as
`agy.registry.registry_dir` does, and the fixture now defaults it under `tmp_path`; every one of the
five gate constructions across the suite passes it.

That is the **second** time in two days a helper written to make this work safe reached into the
user's own files — the probe helper picked `culvertHouse`, a real project, as a scratch root. Both had
the same shape: a default that is correct in production and catastrophic in a test, with nothing
forcing the test to say which it wanted.

**And lifting `_config_dir` to the base class had to be done, not merely tidied.** `AgyService`
defined it as a `@property`; the SDK transport now needs the same value, so it moved to
`AntigravityService.__init__` as an attribute. A property on a subclass **shadows** a base-class
instance attribute, so leaving the override in place would have raised
`AttributeError: property has no setter` on every `AgyService` construction — not a style point.

### What is not built

`suggested_mode` stays `None`. Mode escalation grants far more than the call on screen, and AG-15 puts
it behind the rule path being proven first.

**The exit criterion is met at the seam and not yet in a browser.** `pre_verdict` returning
`(True, "")` is what "no dialog" means, and that is asserted — the call never reaches
`broker.can_use_tool`. Persistence across a restart is asserted at the store. What has not been done is
a live turn where a human clicks *always allow* and the next identical call passes silently.

---

## Phase 9b — the half AG-15 shipped without: seeing and revoking (2026-09-05)

**AG-15 gave the user a way to grant a standing permission and no way to take it back.** Granting was
one click; revoking meant hand-editing `~/.config/aic-dc/antigravity-rules.json`. That is the wrong
shape for a permission — someone who cannot see what they granted cannot audit it, and someone who
cannot revoke it has to trust they clicked the row they meant.

Claude gets this for free: its rules are lines in settings files the user already edits, and the CLI
warns about ones that will not match. Antigravity's live in a store *we* keep, so the app is the only
thing that can show them.

**What landed.** `rules.rule_id` and `RuleStore.remove`, two `Settings` RPCs, and a settings panel
listing the rules with a Forget button beside each.

### It went on `Settings`, and the first attempt was architecturally wrong

The obvious home was `AntigravityService`, and two router tests rejected it immediately:

- `test_claude_refuses_nothing` — `RPC_SURFACES` may hide a method on Antigravity and **never** on
  Claude. Claude is the reference surface and refuses nothing.
- `test_the_real_adapters_both_mount` — Antigravity exposes **nothing Claude does not**, which is what
  keeps `48 = 31 + 17` true and lets an engine switch be a field assignment rather than a
  re-registration.

Two Antigravity-only methods on the engine surface break both. The precedent was already there and had
been missed: `Settings.get_agy_gate` puts a per-engine control that is *not part of a conversation* on
the settings service, for a reason that applies here with more force — **a standing permission
outlives the session that granted it.** A user is entitled to ask what they have granted with no
engine running, and especially about the engine they are *not* on, which is exactly when they are
about to switch to it.

A capability-descriptor row was written for this and then removed. `get_agy_gate` has none either: the
panel hides by the method being absent, which is the established idiom for a settings control, and a
descriptor row would have implied a hideable *engine* surface that does not exist.

### The id is derived, and over the grant rather than the label

`rule_id` hashes the matching data and the behaviour — **not** the label or `rule_content`. Two
reasons, both about revoking the wrong thing:

- **Not an index.** A list refreshed between render and click would revoke a different rule than the
  one the user pointed at.
- **Not the label.** A future change that reworded a label would change every id, and the browser's
  "forget this one" would silently stop finding the rule it was looking at.

Derived rather than stored, so rules written before this existed have one too, and two identical grants
cannot end up with different ids.

### Three smaller decisions

- **Listing is not localhost-gated; forgetting is.** Reading what you have granted is not a privileged
  act, and a remote viewer who cannot see the rules cannot notice one they would object to. Revoking
  only ever *narrows* what the agent may do — but the authority question is the same one
  `resolve_permission` answers, and answering it differently here would make the rule about the
  direction rather than about the surface.
- **The panel does not hide when empty.** "You have granted nothing" is precisely what someone
  auditing their permissions wants to be told, and a panel that vanished would be indistinguishable
  from a bug. It hides only when the *method* is absent, which means a build too old to have it.
- **`forget` answers with the remaining rules**, and the panel renders that rather than its own
  prediction — [`chat.md`](../5-webapp/chat.md)'s invariant that a control reporting its own success
  reads its state back from the thing it changed. Recorded there after three separate instances; this
  is the fourth place it applies.

### And the descriptor was lying by then

Checking this work turned up that `capabilities.py` still had `persisted_permission_rules` as **ABSENT**
on Antigravity, with a note ending *"AIC-DC would have to own the rule store to change this"* — which
is what AG-15 had done an hour earlier. `capabilities.py` is meant to be the to-do list as data, so a
stale row defeats its purpose.

Two tests encoded the old premise and both were restated rather than deleted. The interesting one
asserted the surface *"must never drift into the unbuilt list and become somebody's sprint task"*,
reasoning from `updated_permissions` having no counterpart at any layer. True — and it only meant the
**engine** could not persist a rule, not that the **product** could not. The row's own note had said
what would change it. **A surface can be absent from an SDK and present in the app**, and that is now
the recorded lesson rather than an assumption sitting in a test.

---

## AG-R-3 becomes a sentence instead of a silence (2026-09-05)

The register's mitigation for the silent write-diversion is a **startup health check** asserting *"the
repo root is a workspace the engine will actually write to"*. It has not been built, and working on it
turned up that it cannot be built as specified.

**The specified check cannot be honest.** A check phrased against `trustedWorkspaces` passes on a
machine where writes divert anyway — measured three times that morning, from inside a trusted root. So
the check has to assert an *outcome*, and the only thing that produces an outcome is a real write,
which costs a turn on the user's paid subscription **every time the app starts**. That is a poor trade
for a check that mostly says yes.

**So it moved to where it is free.** A completed write already names its target, so one `stat` answers
the question. `agy/steps.py` inspects every completed call in the write seam, and a target that is
missing *here* while a file of that name sits in `~/.gemini/antigravity-cli/scratch/` becomes a
`systemEvent` naming both paths.

Three decisions in it, each about not making things worse:

- **Narrow on purpose.** It fires only on the *pair* — missing here, present there. "The file is
  missing" alone has innocent explanations: the model naming a path it never created, a tool that
  failed for an unrelated reason. A false alarm about a write that did land would be worse than the
  silence it replaces; the pair has no innocent reading.
- **It says the edit is not lost**, and names the file holding it. A user told only "the file is not
  there" would redo work that has already been done.
- **It sits beside the tool card, not inside it.** `agy` reported success and the card says so.
  Rewriting the card to say "failed" would be this pump asserting something the engine did not — and
  the two disagreeing is exactly the information the user needs.

**This does not close the risk.** A diverted write still happens and nothing here prevents it. What
changes is that it stops being undiagnosable, which was the whole of the severity: the failure was
never that a file went to the wrong place, it was that the symptom — *"the agent says it edited my
file and the diff is empty"* — had no path to a cause sitting in another product's settings directory.

---

## The deny tripwire's third false pass, and its fix verified (2026-09-05)

`probe_agy_gate.py` passed after the trusted-workspace fix, and **the pass was still not honest**:

```
tools the gate was asked about: ['run_command', 'find_by_name', 'list_dir']
of those, refused:              ['run_command', 'list_dir']
PASS: 2 write attempt(s) refused across 2 distinct route(s), file unchanged
```

`replace_file_content` never appears. The model never proposed the edit, so the refusal of a *write*
was never tested — and the run went green anyway because `list_dir` was counted as a refused write
attempt.

**The cause is this phase's signature failure, for the third time.** The probe's `READ_CLASS` was a
hand-written set containing `list_directory` — the **SDK's** name — where `agy` sends `list_dir`. So
the probe's own gate refused a read; the model lost the ability to look around, gave up before
proposing an edit, and the misclassified read padded the "writes refused" count.

`READ_CLASS` is now **derived** from `permissions.TOOL_CLASSES`, the merged table that already holds
both vocabularies. A second copy of a tool-name table is precisely the drift phase 8 has now been
caught by three times: the dialog calling a file edit a shell command, the read-class hole here, and
the `_RULE_TOOL_FOR_PATHS` trap AG-15 had to route around.

**Re-run and verified live:**

```
tools the gate was asked about: ['run_command', 'find_by_name', 'view_file', 'view_file', 'grep_search']
of those, refused:              ['run_command']
PASS: 1 write attempt(s) refused across 1 distinct route(s), file unchanged
```

Three read tools now pass through where one was being wrongly refused. **What this run does not
show** is worth stating: the model reached for the shell rather than the edit tool, so this is
[AG-R-11](risks.md#ag-r-11)'s route-around being blocked rather than `replace_file_content` being
refused. Both are real gate tests and the probe accepts either, but which one a given run exercises is
the model's choice, not the probe's — so a single green run does not prove the edit path specifically.
The 2026-09-03 run that refused three routes including `replace_file_content` remains the stronger
record.

---

## AG-15 verified in a browser, and the one thing only a browser found (2026-09-05)

**Phase 9's exit criterion is met end to end**, on the paid subscription, in a fresh instance on a
throwaway repo:

```
14:13:46  run_command  echo RULE_TEST_ONE   → dialog, offering "Always allow"
14:13:56  …            resolved as allow_always by localhost
14:13:56  Antigravity standing rule stored: Always allow run_command(echo RULE_TEST_ONE)
14:14:20  Antigravity run_command allowed by a standing rule   ← second identical turn
          dialogs asked, across both turns: 1
```

The dialog count stayed at **one** across two identical commands. The second call never reached
`broker.can_use_tool`, which is what AG-15 asked to be asserted rather than "the dialog was dismissed".
The rule is on disk in `~/.config/aic-dc/antigravity-rules.json`, keyed by repository, with the
matching data that makes it exact.

### The tooltip was lying, and no test could have caught it

The always-allow control rendered with this title:

> *Writes a rule to a settings file you can read and revoke. **It applies to the claude CLI in this
> repository too**, not just AIC-DC.*

For an Antigravity rule that is false in both halves. It is not a settings file the CLI reads; it is a
file AIC⚡DC keeps, and `claude` has never heard of it. **A misleading sentence on a permission control
is worse than a missing one, because the user acts on it** — someone reading that would believe a
grant they made here also loosened their Claude gate.

The cause is a shape that was already documented as wrong. `constants.js` says, of these tooltips:
*"Two tooltips, because **the destination decides which is true**."* The call site then chose with
`rule.session ? A : B` — a boolean, not a destination — so `aicDcRules`, a third destination added the
same day, fell through to the Claude sentence and asserted it.

Fixed by making the choice a function of the destination, where the destinations are described:
`alwaysAllowTooltip(rule)`. Four tests pin all three cases, including the control — a fix that told
*everyone* "not the claude CLI" would be wrong in the other direction, on the engine that ships.

**Only a browser finds this.** Every unit test passed on both sides, before and after; the string was
correct for the engine it was written for and nobody had asked what the other engine renders. That is
the third defect in two days found by running the app and not by 4,400 tests — after `translator.stats`
and `session.read_only`, and the same shape as both: **shared code meeting a second transport whose
case nobody re-checked.**

### The diversion: concurrency excluded, emptiness excluded, and a pattern that is not a cause

`scripts/probe_agy_concurrent_write.py` was written to test the one candidate left in AG-R-3 — that a
second concurrent `agy` process is what causes the write diversion. It ran the same create twice in
one workspace, once alone and once beside a working second session.

**Both diverted, including the solo control.** So concurrency is excluded, and the probe reported
itself **INCONCLUSIVE** rather than claiming a result: a comparison whose control also fails proves
nothing about the variable. That is worth more than a green run would have been — the alternative was
to report "concurrency confirmed" from two failures that had a common cause neither of them was
testing.

The solo failure pointed somewhere else: it happened in an **empty** git repository, while the run
where a create *did* land had a file in the workspace. Seeded and re-run the same afternoon —
**diverted again**. Emptiness excluded too.

Three explanations offered for this behaviour so far, all confidently reasoned and all wrong: the
trust list, git-repository-ness, and create-versus-edit. This entry adds a fourth thing that is
**deliberately not called a cause**. Holding the workspace root constant, every run on record lines up
on the shape of the *turn*:

| Turn | Outcome |
|---|---|
| edit an existing file **and** create a new one | both landed |
| edit an existing file | landed |
| create only — empty workspace, seeded workspace, fresh directory | diverted, five times |

**A create lands when the turn also touches an existing file, and diverts when creating is all the
turn does.** Five runs, one correlation, no mechanism.

The reason this is fit to stop on rather than chase further: **the mitigation does not depend on it.**
The detection added earlier fires on the *outcome* — target missing here, file of that name in the
scratch directory — so it catches a diverted write whatever produced it. Every hour spent on the cause
buys a better explanation of a failure that is already caught and named.

---

## Phase 5 groundwork — the mirror's contract, measured (2026-09-05)

Phase 5 says the repo-local mirror is *"rebuilt as a step observer rather than as a store
implementation, since there is no `SessionStore` protocol to implement"*. That says what to build and
leaves open the thing that decides whether it is cheap or expensive: **what shape must an entry be for
the existing history stack to render it?**

Read out of the code rather than guessed, and then measured against the real parser.

### The stack is already engine-agnostic, and that is most of the phase

Three facts, each of which removes work:

- **`RepoSessionStore(root)` takes its root as a constructor argument.** AG-1's *"its own store root, so
  a record written by one engine cannot be handed to the other"* is therefore a parameter, not a
  second implementation.
- **The store is format-agnostic.** `_append_sync` writes dicts as JSONL and dedups on `uuid`;
  `_parse_lines` reads them back as dicts. It has no opinion about what is in them.
- **`history.load_session(store, session_id, directory, …)` takes the store as an argument**, as do the
  other helpers. Nothing in the history stack is bound to the Claude adapter — the same arrangement
  that already lets this engine import `claude_code.review` and `claude_code.commit`.

So the mirror is: write entries the parser accepts, into a second store root, and delegate all seven
RPC methods to helpers that already exist.

### What the parser requires, and the two hours it took to find out

`load_session` hands the entries to the SDK's own `get_session_messages_from_store`, so the entry
shape is the CLI's, not ours. Four guesses at it all parsed **zero** messages, including — puzzlingly
— a **verbatim entry copied out of the real mirror**.

The bisect that settled it: a real entry parses down to `{uuid, type, message}` and **nothing else is
required**. `parentUuid`, `sessionId`, `cwd`, `isSidechain`, `userType`, `version`, `gitBranch`,
`permissionMode`, `promptId`, `promptSource`, `entrypoint` and `timestamp` are all droppable.

The reason every synthetic attempt failed was in the argument, not the entry:

```
session id "sess-abc"                        → 0 messages parsed
session id "a3f1…-uuid"                      → 2 messages parsed
```

**The session id must be a UUID.** Nothing says so, nothing raises, and an empty list is the same
answer the parser gives for a session that does not exist — so a mirror keyed on `agy-1` or
`antigravity-session-3` would have written perfectly good transcripts that the history browser
reported as missing, with no error anywhere.

**This is free for us, and worth stating because it constrains a decision nobody would have thought
to make.** Both transports already produce UUIDs: `agy`'s `init` frame carries
`conversation_id: cd4edb7f-6de3-468f-9815-e76b310a920a`, and the SDK's `Conversation.conversation_id`
is the same shape. **The mirror must key on the engine's own conversation id and must never invent a
readable one** — which is also what makes `resume_session` possible, since that id is exactly what
`agy --conversation <id>` and `SessionContinuationMode.RESUME` take.

Recorded here rather than in a comment because it is a measured property of somebody else's parser on
an SDK that moves, and because the failure it causes is silent in both directions.

### What phase 5 does next, in order

Written down because the contract above was the expensive part and it should not have to be
rediscovered.

1. **`src/aic_dc/antigravity/mirror.py`** — an observer that turns the events **both** translators
   already emit (`streamChunk`, `toolUse`, `toolResult`, `systemEvent`) into `{uuid, type, message}`
   entries and appends them to a `RepoSessionStore` rooted at its own directory. One observer serves
   both transports *because* phase 8 made `AgyTranslator` and `StepTranslator` emit the same
   vocabulary — that is the payoff for a decision made for a different reason.
2. **Key on the engine's conversation id**, which is already a UUID on both transports. See the
   contract above: a readable key silently renders as "no such session".
3. **Its own store root** (AG-1), so a record written by one engine cannot be handed to the other.
   `RepoSessionStore` takes it as an argument.
4. **The seven RPCs** — `history_list`, `history_load`, `history_search`, `history_delete`,
   `history_image`, `get_session_storage`, `resume_session` — delegating to `claude_code.history`
   with that store. They are mapped in `RPC_SURFACES` already, so the router refuses them today and
   will stop refusing when `capabilities.py` flips.
5. **Flip `session_mirror` and `transcript_history`** from `UNBUILT` to `SUPPORTED`. Two of the five
   surfaces the chat panel currently lists as not built for this engine.
6. **Resume**, which is the exit criterion's other half: `agy --conversation <id>` and the SDK's
   `SessionContinuationMode.RESUME` + `save_dir`. Both take the same id the mirror is keyed on, which
   is why step 2 is not merely tidy.

**Do not** start by writing a session-store implementation. There is no protocol to implement and the
existing store already does the work; the phase is an observer plus seven delegations.

---

## Phase 5 — history and sessions, and the two things a browser found (2026-09-05)

**The exit criterion is met.** *"Restarting the server resumes the previous Antigravity conversation
with context intact, and the history browser renders it"* — proven twice: once headlessly by
`scripts/probe_agy_resume.py` against the paid subscription, and once by a human driving two fresh
servers over one throwaway repository.

The groundwork entry above was right about the shape, and the phase cost about what it predicted:
**one new module, seven delegations, and no `SessionStore` implementation.**

### What was built

`src/aic_dc/antigravity/mirror.py` — a `SessionMirror` that observes the events **both** translators
already emit and appends CLI-shaped entries to a `RepoSessionStore` rooted at this transport's own
directory. It is wired at `AntigravityService._dispatch`, which is the one point every event of both
transports passes through: `AgyService` inherits that method and overrides only what *produces* the
events. Observing in the two turn runners instead would have been two call sites for one job, which
is how one of them comes to be forgotten.

The five history RPCs, `get_session_storage` and `resume_session` are delegations to
`claude_code.history` with this engine's store. Resume is the engine's own on both transports —
`agy --conversation <id>` and `conversation_id` + `SessionContinuationMode.RESUME` — so nothing here
replays a transcript into a prompt. `save_dir` is deliberately left unset: it defaults to the store
the harness wrote the session into, and pointing it somewhere of ours would make every conversation
recorded before the change unresumable.

**Its own store root, per [AG-1](decisions.md#ag-1) — and *three* roots rather than two.**
`.aic-dc/antigravity-sessions/` and `.aic-dc/agy-sessions/` are separate from each other as well as
from Claude's. The two transports reach the same *product* and not the same conversation store: an
`agy` conversation id means nothing to `localharness` and the other way about, so one root would have
offered the user a list half of which the running transport would fail to resume.

### The fact the groundwork entry got half right

It recorded that an entry parses down to `{uuid, type, message}` and that everything else is
droppable. That is true **of one entry**, and it is why the bisect that established it did not see
the other half:

> `_build_conversation_chain` finds the terminal entry and walks *back* through `parentUuid`. With no
> links every entry is its own terminal, the walk picks the last one, and the conversation is one
> message long.

So `parentUuid` is not droppable once there are two entries, and a mirror written to the letter of
the earlier note would have rendered every conversation as its most recent message — silently, and
looking like a rendering bug rather than a storage one. The chain is also **re-seeded from disk** on
the first append after a resume: an unparented entry appended to an existing transcript starts a
second chain, and the reader walks back from one terminal only, so the older half would stop
rendering the moment a resumed session took a turn.

`tests/test_antigravity_mirror.py` asserts this by outcome rather than by field — events in one end,
`history.load_session` out the other — because that is the only assertion the four failed guesses
would not also have passed.

### The tool-name table, merged rather than copied

`history._Turn._attach_result` attributes a browsed turn's files with
`claude_code.messages.files_written_by`, and the live pump had its own
`antigravity.steps.TOOL_WRITTEN_PATH_FIELDS`. Two tables for one fact, and the failure was exactly
the one this plan has now paid for four times: a turn would list the files it touched while it
streamed and list none after a refresh. Nothing errors; the number is just smaller.

`_FILE_WRITING_TOOLS` now holds all three vocabularies — Claude's, the SDK's and `agy`'s — and
`_files_written` delegates to it. The names do not collide, so one table can hold them all and one
table cannot disagree with itself. That is `agy/tools.py`'s own argument, applied to the fourth
table it applies to. `generate_image` is why the values became tuples: it exists in both Antigravity
vocabularies under two different argument names.

### What the live probe proved that the mirror could not

`scripts/probe_agy_resume.py` runs two processes over one work directory. The first tells the model a
passphrase it could not otherwise know; the second — a **different process**, with the session
object, the conversation id and the mirror's chain all gone — asks for it back.

```
[probe] conversation 1c951b0f-f98f-45a2-abac-2768085d1c83
[probe] phase one done; mirrored under 1c951b0f-f98f-45a2-abac-2768085d1c83
[probe] --- restarting: phase two runs in a new process ---
[probe] state snapshot: 2 messages ['user', 'assistant']
[probe] reconnected to 1c951b0f-f98f-45a2-abac-2768085d1c83
[probe] the model answered: 'KESTREL-9-ORRERY'
[probe] history_load after the resume: ['user', 'assistant', 'user', 'assistant']
```

**The passphrase is the point, and it is not decoration.** A mirror looks perfect for a resume that
silently opened a blank conversation — the transcript is ours and it is on disk either way. Only the
engine can answer whether its context came back, and only a token it could not guess makes the answer
mean anything.

`AgySession.start` now **refuses** rather than warning when a resume comes back with a different
conversation id. A resume that quietly became a new conversation is the one failure worth not
starting over: the user asked to continue, the context is gone, and nothing downstream would say so —
the turn would simply behave as though the agent had forgotten everything.

### The two things only a browser found

Both were invisible to 4,412 green Python tests and 4,431 green webapp tests, and both are the same
shape: **a surface newly enabled exposes an unguarded call to a *different* surface.** Until phase 5
these travelled together — an engine with no history browser was never asked for a session's
subagents, and was never shown a Fork button — so the pairing had never had to be a decision.

**1. The router's refusal rendered as red text at the top of every preview.** Selecting a session
called `list_subagent_transcripts`, which serves `subagent_tabs`, which `agy` cannot feed; the router
refused it correctly and the browser drew the refusal. The refusal was right and asking at all was
the bug. `_loadSubagents` now checks `supports(SURFACE.SUBAGENT_TABS)` first.

**2. Fork was offered on an engine that refuses it.** `resume_session(fork=True)` returns
`unsupported`, because Claude forks by copying a transcript the CLI rebuilds its context from while
Antigravity's conversation store belongs to the harness and is opaque. Copying our mirror would fork
the *record* and leave both branches pointed at one engine conversation — two transcripts of one
session, diverging the moment either took a turn. A refusal the user can reach by clicking is a
stub with extra steps, so this became a descriptor row (`session_fork`) and the button is hidden on
it, per [AG-9](decisions.md#ag-9). No engine-name branch: the webapp reads the descriptor.

Both are pinned by tests in `webapp/src/history-browser.test.js` that set a descriptor with one
surface on and the other off — the configuration that did not exist before this phase.

### A third finding, and the design change it forced

The first cut wrote a closing assistant entry per turn to carry the engine's token counters. It
rendered nothing, and **the history browser counted it**: a two-message conversation showed as
`3 msgs`, seen in the browser and not by any test. It bought nothing either — Antigravity reports
`prompt_token_count` / `candidates_token_count`, which share no field name with the four counters
`_Turn.freeze` sums, and `turn-cost.js` skips this engine's flat usage shape on the live path too.

So the counters are not mirrored at all, and the reason is **placement rather than squeamishness**:
this engine has no per-message usage — the SDK reports a turn diff at close and `agy` a running total
on its result frame — so there is no entry either of them belongs on. Every assistant entry of a turn
now carries an *empty* `usage` under one shared `message.id`, which is the CLI's own arrangement and
is what still makes a browsed turn read as **one engine turn** rather than one per block. The footer
of the resumed conversation reads `2.0s · 1 engine turn` with no token chip, which is what the live
turn shows.

### A fourth: a switch said "blank" and meant "continue"

`switch_engine`'s docstring has said since AG-1 landed that a switch *"ends the outgoing session and
starts a new one… the incoming one connects lazily on the next turn, **with no resume**, which is what
makes it a new session."* Nothing enforced it. Each adapter's auto-resume flag survived the switch, so
the incoming engine's next connect quietly reattached to whatever conversation it was last in.

**This was inert while only one engine could resume**, and phase 5 is what makes it a contradiction:
the switch broadcasts `sessionChanged` with an empty message list, so a browser is told the panel is
blank while the server intends to reattach — and the next state load repopulates the chat with a
conversation the user was told had been left behind. Watched happening in the browser during this
phase's verification.

Both adapters now implement `_start_blank_session`, and the router calls it on the **incoming**
engine only — the outgoing one is being stopped, not restarted, and clearing its target would decide
on its behalf that it may never be switched back to. Nothing is deleted either way: the conversation
left behind stays listed and loadable, which is the whole reason a switch can afford to be a
boundary.

Worth naming that the Claude half of this was a **pre-existing** gap, not one phase 5 introduced. It
is fixed here because this is where it became observable, and because a rule that holds on one engine
and not the other is not a rule.

### What phase 5 deliberately did not build

- **No events log for this engine.** `EventsLog`'s `event` domain is closed on purpose and none of
  its members is a thing this engine reports, so a browsed Antigravity conversation carries the
  model's work and not the operational lines around it. `systemEvent` reaches the transcript only for
  `compaction`, which is the one subtype with a CLI counterpart (`compact_boundary`) that
  `history._compaction_divider` already renders.
- **No derived history index.** It caches a finished row keyed by transcript mtime; a cold index is a
  slower listing, never a wrong one, and a second one for this engine before anybody has felt the
  cost would be a file to keep in agreement for no measured gain.
- **No fork**, as above.

### One deviation from the testing recipe, recorded because it was deliberate

The house rule is to test against a new instance started with `--preview`. Three of the user's own
`--preview` servers were running at the time, and `--preview` does `rm -rf dist` on startup — which
would have pulled the static bundle out from under two live windows. `--dev` was used instead: it
serves through Vite and touches no `dist`, and the load-bearing half of the rule — *a Python process
started fresh, on a throwaway repository under a trusted root* — is unaffected either way.

---

## Phase 7 — the SDK becomes an extra, and what that exposed (2026-09-05)

**The exit criterion is met.** *"A base install is a one-engine install with no broken UI, and its
size has not moved."* Measured against two clean Python 3.14 venvs:

| Install | `site-packages` |
|---|---|
| `aic-dc` | **273.1 MiB** |
| `aic-dc[antigravity]` | 408.3 MiB |

The extra is **135.2 MiB**, of which the bundled `localharness` binary alone is 123.1 MiB
(129,065,896 bytes in 0.1.16 — it was 119,721,512 in 0.1.15, so it grew ~9 MB in four days, which is
its own small argument for this phase).

**Those are the second numbers, and the first ones were wrong.** The base column originally read
285.8 MiB, because that venv had already *run* a server and `__pycache__` had put ~9 MiB of bytecode
into `site-packages` while the comparison venv had not been run. The absolute figure was wrong, the
difference inherited the error, and nothing about the table looked suspect — it was caught only
because 285.8 MiB is smaller than the bundled `claude` binary that has to be inside it. Two rules
follow, and they are in AG-R-10 because this table is a per-release tripwire: **measure a fresh
install before its first run**, and **sum apparent file sizes rather than `du` blocks**, since `uv`
hardlinks from its cache and block counting then answers a question about the machine rather than
about the install.

### It is a *two*-engine base install, and that is what made the extra affordable

The criterion says "one-engine install" and the answer came out better than the criterion. The `agy`
transport ([AG-14](decisions.md#ag-14)) drives the Antigravity CLI over a pipe on the owner's own
subscription; it imports nothing from `google.antigravity` and mounts on the binary being on PATH. So
a base install still reaches this engine.

That reframes the extra. **It is not "Antigravity is optional"; it is "the metered route to
Antigravity is optional"** — which is a much easier trade to defend, and it is why the phase-8
decision to add `agy` paid for itself a second time. What a base install genuinely loses is the
API-key session and the **consultant**, because `second_opinion` and `generate_image` are the SDK's
and the CLI has no one-shot consultation mode. That loss is stated rather than papered over: the
startup log names it, and points at `aic-dc[antigravity]`.

### The `pyproject.toml` edit was the easy half

The interesting part is why this phase was not a one-line change, and it is the same property that
made the phase *possible*:

> Every `from google.antigravity import …` in the package is function-local by design, so these
> modules stay importable where the SDK is not installed.

That was written in phase 3 as a testability argument and it is what lets a base install exist at
all. It also means **nothing fails without the wheel** — not an import, not a construction, not a
mount. A base install imported cleanly, built `AntigravityService`, put it in the engine selector,
and reported the consultant as available. The absence surfaced only on the first turn, as an
`ImportError` from an engine the user had picked out of a menu.

That is precisely the "broken UI" the criterion forbids, and no offline test could see it: the test
environment has the wheel. So the phase's real work was making absence *visible at mount time*:

- **`surface.sdk_installed()`** is the one authority on the question. `importlib.util.find_spec`
  rather than an import, because it is asked at every startup including the runs that never touch
  this engine, and importing pydantic and gRPC to answer a yes/no is a cost on a path meant to be
  free. `surface._sdk()` — the probe's importer — now asks it first rather than deciding for itself,
  so the diagnostic and the mount cannot disagree about whether this install has an SDK.
- **The engine mounts on the wheel and the credential**, where it used to mount on the credential
  alone. Same rule, one more condition, and "not offered" is the same honest answer a missing key
  already got.
- **The consultant likewise.** `Consultant.available` was credentials-only, so a base install *with*
  a Gemini key registered both tools, spent context describing them on every turn, and answered the
  first call with an `ImportError`. AG-9's "hidden rather than stubbed", applied to a tool
  definition.

**`find_spec` raises rather than returning `None`** when the `google` namespace package is absent
entirely — which is exactly the state a base install is in, and unguarded it would have been an
uncaught exception at startup: a worse failure than the one this phase is about. Found by running the
base install rather than by reading the docs, and pinned by a test.

### The diagnostic that sent the user to fix the wrong thing

Running the base install found one more, and it is the kind of thing only running finds. With a valid
Gemini key on disk and no wheel, startup logged:

```
Antigravity consultant not mounted: no Gemini API key or Vertex project. Set one to…
```

The mount was correct and the *reason* was wrong. `available` had become two conditions and the
message still named one, so a user with a key was told to go and set a key. A diagnostic that sends
somebody to fix the wrong thing is worse than no diagnostic. Both reasons are now reported
separately, on both the engine and the consultant.

### What the release binary was already doing, now by declaration

The release workflow syncs `--extra build --extra docs-convert` and has no `--collect-all` for
`google.antigravity`. Since the SDK's imports are function-local, PyInstaller's static analysis never
saw them — so **the shipped binary has never carried a usable Antigravity SDK**, while every `uv sync`
of it paid for the wheel. Phase 7 does not change that artefact; it makes it correct on purpose, and
adds the assertion that keeps it so.

### Tripwires, because AG-R-10's is a number a human has to notice

The risk register asks for "base-install size, measured per release", and the failure it guards is
"a `pyproject.toml` edit nobody reviews as a size change". A release note is a poor place for that to
be caught, so two of the three now fail by themselves:

1. `tests/test_antigravity_packaging.py` reads `pyproject.toml` and fails if `google-antigravity`
   returns to `[project.dependencies]`, or if the extra loses its version floor.
2. The release workflow fails the build if `localharness` appears in the PyInstaller archive.
3. The measured size table, per release, for the part a test cannot see.

The same file also pins the property the whole phase rests on — that no module imports the SDK at
module scope — by walking the package's syntax trees. A single top-level import would turn a base
install into an `ImportError` at startup, in whichever module happened to be imported first.

### The floor, finally set

`pyproject.toml` carried a note saying the version floor was deliberately unset because *"this one
has not been read yet. The floor gets set in the same pass that writes the surface doc."* That pass
happened in phase 0 and the note outlived it. `>=0.1.16` — the version the surface was re-probed
against — and a floor rather than a pin, because the package is 0.1.x and alpha
([AG-R-2](risks.md#ag-r-2)) and the drift gate is what handles movement above it.

### Verified by running both

Not by reasoning about dependency metadata:

```
base:   Antigravity engine not mounted: google-antigravity is not installed…
        Antigravity consultant not mounted: google-antigravity is not installed…
        agy transport mounted (Antigravity CLI on PATH)
extra:  Antigravity engine mounted (credential from Gemini API key…)
        Antigravity consultant mounted as aic-dc-antigravity…
        agy transport mounted (Antigravity CLI on PATH)
```

No traceback, no error, and no engine offered that could not answer — the selector renders
`list_engines().mountable`, which is the adapters actually constructed, so a base install offers
`claude` and `agy` and nothing else. **With this, every phase in this directory is closed.**

---

## The gate that reported itself installed and allowed everything (2026-09-05)

Found by a question rather than by a test: *"what does the agy subscription mode require to be
installed?"* Answering it meant reading `hook_command` closely enough to notice that its output is
not always runnable.

### The bug

The gate `agy` runs for every tool call is a command string in the user's own
`~/.gemini/config/hooks.json`:

```
<sys.executable> -m aic_dc.agy.hook <config_dir> || printf '{"decision":"allow"}'
```

On a pip install `sys.executable` is a Python and this is correct. **On a PyInstaller release binary
it is the frozen binary**, which does not honour `-m`:

```
$ aic-dc -m aic_dc.agy.hook ~/.config/aic-dc
aic-dc: error: unrecognized arguments: -m aic_dc.agy.hook ...
exit=2
```

The `||` fallback then fires and prints `{"decision":"allow"}` — for every tool call, on a session
this host owned and was supposed to be gating. Meanwhile `gate_status()` reported `current`, because
it decides by string-comparing the installed command against the one it would write, and the string
was exactly right.

**An ungated agent that reports itself gated**, on the transport where the gate *is* the product
([AG-5](decisions.md#ag-5)). `connect_engine` refuses to start without a `current` gate, and that
check passed.

### Why nothing caught it

Every test runs where `sys.executable` is a Python, so the `-m` form is correct and the suite is
green. The pip installs used to verify phase 7 the same afternoon were green for the same reason. The
only shape that fails is the released binary, and nothing in `agy/install.py` had ever distinguished
the two — the only `_MEIPASS` handling in the tree is in `config.py` and `main.py`.

This is [AG-R-12](risks.md#ag-r-12)'s lesson for the third time, in its sharpest form yet: **a string
that is correct is not a mechanism that works.** `status` was measuring the first and reporting the
second.

### The fix, at two layers

**The specific one.** `hook_command` branches on `getattr(sys, "frozen", …)` and emits
`<binary> --agy-hook <config_dir>` — a new `argparse.SUPPRESS`-ed flag on the CLI whose only caller is
that string. It dispatches before logging and before the banner, because this process is `agy` asking
about one tool call and anything else on stdout is a parse failure at the other end.

**The general one, and the more valuable.** `install` now *probes* the command before writing it and
**refuses** one that does not answer with a JSON decision:

```
old frozen-shaped command -> exit 2: aic-dc: error: unrecognized arguments: -m aic_dc.agy.hook /tmp/x
new frozen-shaped command -> accepted
```

The frozen binary was one way to get a correct string naming an unrunnable command; a moved
virtualenv and an uninstalled package are others, and this catches all of them. It runs the **left
side only** — running the whole command would execute the fallback, print a perfectly good decision,
and mask exactly the failure it exists to find. Failing closed here costs an error message at the
moment the user asked for a gate, which is the cheapest place in the system to spend one.

`install` answers a fifth state, `unrunnable`, and the Settings panel renders it with its reason
rather than falling through to the raw word.

### Two tests had to be rewritten, and that is the fix working

`test_stale_when_it_points_at_another_interpreter` and `test_a_stale_gate_is_refused_too` both
*constructed* their stale state by calling `install(python="/somewhere/else/python")` — which `install`
now correctly refuses, because that interpreter does not exist. Both now write the entry to disk
directly, which is also more honest about what they describe: a file left behind by an installation
that has since moved, not something anybody installs on purpose.

### What is verified and what is not

Verified by running: the new entry point answers a real payload end to end through `cli.main` — the
same function a frozen binary runs — and the probe rejects the old frozen-shaped command and accepts
the new one. **Not verified on an actual PyInstaller artefact**, because building one is a
multi-minute per-platform job; the argument-parser rejection was demonstrated against the console
script, which runs the same parser. The release workflow builds the binary on every release and the
gate is installed by a click rather than at startup, so the first real artefact will exercise it.
