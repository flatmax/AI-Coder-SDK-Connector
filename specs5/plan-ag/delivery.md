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

**Amended 2026-09-10 — the third row was two situations wearing one description.** "Host unreachable"
covered a host that is *running and not answering* and a host that **no longer exists**, and only the
first is ours to deny. The rows now read:

| Situation | Answer | Why |
|---|---|---|
| Entry present, host process gone, its `agy` gone too | **allow** | A corpse entry. Nothing from that session is left to gate, so the only thing it can still do is intercept a conversation the *user* resumes. Swept at the next `AgyGateServer.start`. |
| Entry present, host process gone, its `agy` alive | **deny** | An orphaned agent — our child outliving us. This is the row that makes "stale means allow" wrong. |
| Payload unparseable, every entry we hold is a corpse | **allow** | We own nothing that is running, so it cannot be ours. Before this, one unclean exit made the row below permanent. |

See [§ The pid that was written and never read](#the-pid-that-was-written-and-never-read-2026-09-10).

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

---

## AG-R-3, solved: the agent was never in the repository (2026-09-05)

Six days, four disproven causes, and the answer was one missing flag in our own argv.

### What the user saw

A new git repository, a fresh `agy` session, and *"create a helloworld script"*. The chat said the
file was created and linked it. The file was not in the repository.

### What the transcript said

**Readable because phase 5 shipped the mirror a few hours earlier** — this is its first real user
session, and the first time this failure has been inspectable after the fact rather than reconstructed:

```
[user]        create a helloworld script
[tool_use]    write_to_file {"TargetFile": ".../antigravity-cli/scratch/hello_world/hello.py"}
[tool_result] err=True                      ← "not a valid artifact path"
[tool_use]    run_command   cat << 'EOF' > .../scratch/hello_world/hello.py
[tool_result] err=False
```

The model never aimed at the repository. `write_to_file` was refused, so it fell back to a shell
heredoc — which is also why no permission dialog rendered a diff for it.

Then the second turn, which is where the model diagnosed the bug on our behalf:

```
[user]        create it in this repo
[tool_use]    run_command  pwd && git rev-parse --show-toplevel
[tool_result] /home/flatmax/.gemini/antigravity-cli/scratch
```

It went on to find the `agy` pid, read `/proc/<pid>/cwd` → `/tmp/temp`, check git there, and write the
file where it had been asked. The agent worked out where it was supposed to be by inspecting its own
process.

### The cause

`AgySession` spawned `agy` with `cwd=repo_root` and **never told it the repository was its
workspace**. `agy`'s own log even reported `workspaceDirs=[/tmp/temp]` while running its tools in the
scratch directory — which is exactly the kind of near-miss that keeps a wrong theory alive.

Its system prompt completes the picture: when the model needs somewhere to write, it is told it may
use *"the default project directory at `~/.gemini/antigravity-cli/scratch`"* and should *"recommend
the user set that subdirectory as the active workspace."* Given where it was standing, the model
behaved correctly every time.

### The measurement

Everything held constant — same parent directory, same `git init`, same seed file, same process cwd —
one flag different:

| | tool `pwd` | `git rev-parse --show-toplevel` |
|---|---|---|
| `--add-dir <repo>` | `/tmp/temp/wstest` | `/tmp/temp/wstest` |
| *(control)* | `~/.gemini/antigravity-cli/scratch` | `fatal: not a git repository` |

The control was run second and deliberately, because the treatment alone would have been a
correlation with the directory rather than with the flag.

### The fix, and the same prompt afterwards

`AgySession._argv()` passes `--add-dir <repo_root>` — one directory, never a list, because AG-10 is
one repo root and one working tree and the diff viewer resolves against a single one.

Driven live through the shipping adapter with the identical prompt that failed:

```
[probe] dialog: write_to_file -> allow
[probe] dialog: run_command  -> allow
=== what landed in the repo:  hello_world.py
=== anything new in scratch?  (nothing)
```

The file lands in the repository, nothing goes to scratch, and the write arrives as `write_to_file`
through the permission dialog instead of as a heredoc that no diff could be rendered from. That last
part is a second defect this fixes without having been aimed at it: the gate was reviewing the
model's *workaround* rather than its intent.

### Why four hypotheses missed it

`trustedWorkspaces`, git-ness, workspace emptiness, concurrency. Every one was a hypothesis about
`agy`'s behaviour, and each was disproven by a probe that changed something about the *workspace*. The
variable nobody controlled for was in our own argv, and it was invisible from inside the failing
system: `agy` reported the right `workspaceDirs`, the process had the right `cwd`, and the file was
genuinely written where the tool call said it would be.

The register's last correlation — *"a create lands when the turn also touches an existing file, and
diverts when creating is all the turn does"* — is now explained rather than replaced. A turn that
edits an existing file is handed a path and writes there. A turn that only creates picks its own
location, and its own location was the scratch directory.

**The reusable lesson**: when four hypotheses about somebody else's product have all failed, the
variable is probably in the argument list you control. And the thing that finally exposed it was a
user asking an ordinary question, in a transcript that had only been readable for a few hours.

---

## The write that could not be a write, and the field that caused it (2026-09-05)

Reported by a user immediately after `--add-dir` landed: the agent now had the right repository, targeted
the right path, *asked for permission*, was allowed — and still did not use the write tool. It fell
back to `cat`. Their three questions were the diagnosis: **why did it fail after I allowed it, why
`cat`, and why is the dialog not the one Claude shows?**

### The failure

```
declaring permissions: cortex tool write_to_file: convert tool call for permissions:
model output error: invalid tool call error (invalid_args)
/tmp/temp/hello.py is not a valid artifact path;
artifacts must be in ~/.gemini/antigravity-cli/brain/<conversation-id>/
```

`agy` declares `write_to_file` as *"Use this tool to create new files"*, with `ArtifactMetadata`
described as *"Required when creating an artifact file"* — optional, by its own schema, for anything
else. The model supplied it anyway, for an ordinary source file:

```json
{"ArtifactMetadata":{…,"UserFacing":true},"CodeContent":"…","TargetFile":"/tmp/temp/hello.py"}
```

**The presence of that field is what makes `agy` classify the write as an artifact** and enforce the
brain-directory path rule. One optional field, and the call is unrecoverable.

### Three consequences, and the user found all three

1. **"I allowed it and it still failed."** The permission they granted was not the one that failed.
   The refusal happens inside `agy` while *declaring permissions* — **before any hook runs** — so the
   gate never saw the call and could not have amended it. `HookResult.modified_args` is the SDK
   transport's; this hook protocol is allow/deny only.
2. **"It reverted to `cat`."** [AG-R-11](risks.md#ag-r-11) again, on a third mechanism: a refused write
   re-attempted through the shell. Except this refusal came from `agy` itself rather than from a
   permission decision, so nothing in the register predicted it.
3. **"The dialog seemed different."** Because it was no longer a write. A `run_command` dialog renders
   a *command*, not a diff; `files_written_by` cannot attribute the file (CC-18); and
   [AG-15](decisions.md#ag-15)'s "always allow" is structurally useless against it, because rules match
   exactly and a heredoc embeds the whole file — a rule granted for one could never match again.

So the sharp statement is: **edits already behaved like Claude on this transport** — phase 8 watched
`replace_file_content` render `+1 −1`, get allowed by a click and land — **and creates did not**,
because `agy` rejected its own create tool.

### Asked the engine, and it answered

There is no `settings.json` toggle, so `agy` itself was asked to create a file with the field omitted
and explain the rule. It did both:

> *"The presence of the `ArtifactMetadata` parameter object in the tool arguments tells the tool
> implementation that the target file is intended to be an artifact. When provided, the tool validates
> that `TargetFile` resides strictly within the session artifact directory."*

and its recommended host-side remedy was to *"clarify in system prompts that `ArtifactMetadata` must
only be supplied when writing artifact documents"*. The probe file landed in the workspace.

### The fix, and what it is not

`agy_tools.WRITE_GUIDANCE`, prepended to every `agy` prompt: name the field, say that including it
makes the write fail unrecoverably, and prefer the file tools over shell redirection.

**It is a workaround on a model behaviour, and it is recorded as one.** It is not a config setting,
because there is none; it is not a gate fix, because the failure precedes the gate. What makes it
tolerable is that the guidance is *specific* — "prefer `write_to_file`" would have changed nothing,
since the model was already choosing it. What it could not know is that one optional field made the
call impossible.

Wrapped in `<aic-dc-ui-context>`, the framing block `history.strip_framing` already removes, so the
model reads it and the user does not. The framed text is what the mirror stores, deliberately: a
transcript's job is to say what the model was actually sent.

### Verified live, on the prompt that had failed twice

```
[probe] dialog: write_to_file class=write diff=True -> allow
[probe] dialog: run_command   class=exec  diff=False -> allow   ← `python3 hello.py`, verifying it
[tool_result] is_error=False
=== repo: hello.py
```

`write_to_file` succeeded, gated as a **write** with a **real diff**, and the file landed in the
repository. Rendered back through `history.load_session`, the user's first message reads
`'please create a hello world script'` — the guidance stripped.

The remaining `run_command` is the model running the script it just wrote, which is the behaviour we
want reviewed rather than suppressed.

### Worth reporting upstream

`agy`'s own schema says the field is optional and its permission converter treats it as decisive.
Every host driving `agy` headlessly will hit this, and none of them can see why: the error is only in
the conversation database, not on the wire, and the model's fallback makes the turn *look* successful.

---

## The picker that never learned about a write (2026-09-05)

Reported straight after the write path started working: the file landed, the diff dialog was right,
and **the file tree went on showing the repository as it was before**.

### How the tree learns anything

One push and nothing else. `files-tab.js` reloads on a `files-modified` DOM event, which
`app-shell/index.js` dispatches from the `filesModified` server push. On the Claude engine that push
comes from a **`PostToolUse` hook** (`claude_code/hooks.py`): it extracts the written paths, queues a
re-index, and broadcasts them session-wide.

**Neither Antigravity transport has that hook, and neither had a substitute.** Every approved write on
this engine has been invisible to the picker since the transport shipped. It was never noticed because
the writes that had been demonstrated were probes asserting on `stat`, not humans watching a tree.

### Two gaps, one behind the other

`AgyTranslator`'s `toolResult` carried **no `files_modified` key at all** — so even a consumer that
wanted to know could not. `StepTranslator` had computed it since phase 3, which is why this looked
like one transport's bug and was really two:

1. `agy`'s pump now derives the written files from the shared table
   (`claude_code.messages.files_written_by`, which since phase 5 knows all three engines'
   vocabularies) and puts them on the result, and accumulates them into `stats.files_modified` so the
   turn footer stops reporting an empty list for every turn that wrote something.
2. `AntigravityService._dispatch` — the seam both transports funnel through, and where the phase-5
   mirror already observes — pushes `filesModified` when a result names files.

Derived from the event rather than from a second walk of the filesystem: the result already names
what it wrote, from the same table the turn footer and the browsed transcript read. One source, three
readers.

**Session-wide, never turn-scoped**, for the reason the Claude hook records: the tree is the same tree
for every watching browser, including ones that did not send the turn. A turn-scoped push carries the
request id first and would be dropped by exactly the clients most likely to be watching rather than
driving.

### Verified live

```
[probe] PUSH filesModified -> ['/tmp/temp/tree/notes.md']
[probe] toolResult files_modified=['/tmp/temp/tree/notes.md']
=== repo: notes.md
```

### The half deliberately not built

The Claude hook does two things and this does one. It also queues a **symbol re-index**, which needs a
`Reindexer` this engine has never had. Left out rather than half-done, and named here so it is a known
gap: a stale symbol index degrades autocomplete, where a stale tree hides the agent's work from the
person who approved it. The second is the one worth fixing first.

---

## A posture between "ask every time" and "ask nothing" (2026-09-05)

Two changes from one question — *"is there a way to permit all changes and writes rather than having
to accept each one?"* One was a defect; the other was a decision only the user could make.

### The defect: a control that was reachable and refused

The permission-mode dropdown is a hardcoded table of **Claude's six** postures. Antigravity accepted
two. Picking one of the other four returned:

```
{'error': 'unsupported', 'message': "'acceptEdits' is not a posture this engine offers…"}
```

Four options that answered an error when chosen — the same shape phase 5 found in the history
browser's Fork button, and found the same way: by a user reaching for something the UI offered.

Fixed the way AG-9 says: the engine reports the postures it accepts in `get_current_state`, exactly
as `get_model` reports its own menu, and the selector renders that. **Not** a table in the browser
keyed on which engine is running, which is what AG-R-4 forbids. A mode the engine reports that this
build has no label for still renders, because filtering it out would make a real posture unreachable
on a newer engine — and the current mode is always included, since a `<select>` whose value is absent
from its options silently displays the *first* one.

`_onEngineChanged` now re-reads the state too. Without that the list would be right until the first
switch and stale afterwards — the same defect, one action later.

### The decision: AG-5, amended by its owner

The reason there was nothing to select is [AG-5](decisions.md#ag-5): the dialog is *a requirement of
this engine rather than a feature*, so no posture may skip it. That is a decision, not a bug, and
changing it was the user's call. They took the narrow version:

| | under `acceptEdits` |
|---|---|
| file write, target **inside** the repository | applies, no dialog |
| file write, target outside the repository | still asks |
| `run_command` | **still asks** |
| `start_subagent` / `invoke_subagent` | **still asks** |
| a write whose path cannot be read | still asks |

**The line is where the evidence puts it.** AG-5 was never protecting "edits are dangerous" — it was
protecting the *execution* path. [AG-R-11](risks.md#ag-r-11) measured the agent, refused an
`edit_file`, reaching for `sed -i`, then inline `python3`, then `list_dir`: three routes to one
write, all through the shell. Letting `run_command` through would hand that route an ungated agent.
Letting an in-repo file write through does not — the diff viewer shows it and git keeps it.

It is also not a line we invented. `agy`'s own `accept-edits` auto-approves `write_to_file` and
`replace_file_content` and explicitly does not auto-approve `run_command` — measured by asking it the
same afternoon — and Claude's `acceptEdits` draws the same boundary. Three products agreeing is a
better argument than any one of them.

`bypassPermissions` is still absent and that half of AG-5 has not moved.

### Where it is enforced, and the ordering that matters

`AntigravityPermissionGate._accept_edits_verdict`, consulted **after** a standing AG-15 rule and
**before** `ALWAYS_ASK`. The ordering is the whole of it: `ALWAYS_ASK` holds every write tool, so a
check placed after it could never fire for the calls this posture exists for — the same trap AG-15
documents for its own rule lookup.

Three conditions, each doing work: the posture is on (read live, because the user flips it from the
action bar mid-session); the tool is classed `write` (**not** "is in `ALWAYS_ASK`", which also holds
`run_command` and the subagent spawners); and the target resolves inside the repository, resolved
first so `../` cannot walk out. A path that cannot be read falls through to the dialog — the one case
where we do not know what is being written is the case to ask about.

Ten tests pin the boundary rather than the implementation, including that `default` and `plan` are
completely inert and that a shift-clicked denied read is still refused: this posture is about writes,
and softening a denial would be a different change smuggled in with it.

### Asked the engine first

Everything above about `agy`'s own permission surface came from asking `agy`, not from guessing:
`--mode accept-edits`, `settings.json` `permissions.allow` with `write_file(<path>)`, and the
confirmation that `--dangerously-skip-permissions` still fires `PreToolUse` hooks and a hook `deny`
still blocks.

**None of it applies to what the user was seeing**, and that is worth recording because it is the
obvious wrong turn. We run `agy` with `--dangerously-skip-permissions`, so its permission layer is
out of the loop entirely — every dialog on this transport is ours. Configuring `--mode accept-edits`
would have configured a layer that is not running. What the answer *did* confirm is the property
AG-R-12 relies on: our hook keeps an absolute veto regardless of that flag.

---

## The verification sitting, and the two defects it cost to buy (2026-09-06)

Everything from 2026-09-05 was green and almost none of it had been watched. Phase 9's own entry said
so — *"not yet in a browser: no human has clicked always allow on a live turn and watched the next
call pass"* — and the `acceptEdits` posture had shipped the same day with ten tests and no live run.
So this sitting was a browser against a throwaway repository, with one small build carried into it.

**Four things were confirmed and two were broken.** The ratio is the argument for the sitting: both
defects were invisible to 4,485 passing tests and visible within minutes of opening the app, which is
the fourth time this suite has written that sentence down.

### The build: the second engine finally feeds the symbol index

The one gap [§ *The picker that never learned about a write*](#the-picker-that-never-learned-about-a-write-2026-09-05) named as deliberate. That entry gave this
engine a `filesModified` push so the file tree reloaded, and left the other half of the Claude hook
undone: **it queued no re-index**, because the queue lives on a `Reindexer` this engine had never
been given one of. So after an `agy` edit the tree was right and every index was wrong — `symbol_map`,
`file_symbols`, `find_references`, `doc_outline` and the editor's completions all answering from the
file as it was before the write, with nothing on the answer to say so.

**The fix is an injection, not a build**, and that is the whole design. `main.py` hands the second
adapter the `Reindexer` the first one constructed, exactly as it already hands over the symbol index —
one index over one tree, therefore one queue of files owed a re-parse. Two queues would have been the
easy version and a worse outcome than the bug: each right about its own engine's writes and wrong
about the other's, with `flush()` draining whichever one the caller happened to hold, so the failure
would depend on which engine wrote last.

Three smaller decisions, each doing work:

- **Queued before the browsers are told**, so the two signals mean what they say — by the time
  anything acts on "the disk changed", the re-parse is at least owed.
- **The queue does not need a browser.** The tree push returns early with no event callback; the
  re-index must not, or a headless run drifts out of date silently.
- **The end of a turn flushes and drops the tally.** Flushing is not what makes the index correct —
  the queue is, and every index-reading MCP tool flushes before it answers. What it buys is the
  moment *after* a turn, when a user reads what the agent wrote and the editor asks for completions
  on it; those LSP RPCs read the index directly with no flush of their own on either engine. The
  tally is dropped because `take_reindexed` is take-and-clear and this engine emits no
  `postResponseComplete` to carry it, so entries left behind would go to whichever Claude turn came
  next — a turn claiming to have re-indexed files a different engine wrote.

Verified twice live, on both paths into a write: a dialog-approved `replace_file_content` under
`default`, and an undialogued one under `acceptEdits`. `lsp_get_completions` on `calc.py` returned
five symbols before the turn and six after, then seven, with the new function present each time.

### Defect 1: the always-allow tooltip, wrong for the second time in two days

The Antigravity dialog told the user *"It applies to the claude CLI in this repository too"* about a
rule going into `~/.config/aic-dc/antigravity-rules.json`, which the `claude` CLI has never heard of.
That is verbatim the defect [§ Phase 9b](#phase-9b--the-half-ag-15-shipped-without-seeing-and-revoking-2026-09-05) fixed the day before, on the same control.

**The first fix was correct and inert.** It replaced a `rule.session ? A : B` at the call site with
`alwaysAllowTooltip(rule)` keyed on `destination`, and added four tests. What nobody checked was
which object the call site hands it: `describeRule` normalises a rule for display, and part of
normalising is replacing `destination` with the filename the chip renders — so by the time the button
had a rule, `destination` was `~/.config/aic-dc/antigravity-rules.json` and the `aicDcRules` branch
was unreachable. **The chip beside the tooltip named the right file while the tooltip named the wrong
CLI**, which is as close to the fix as a bug gets.

**And the function was dead on both shapes, not one.** It read `session` — a boolean only the
*described* rule carries — and `destination` — a key only the *raw* rule carries. Whichever object it
received, one of its two checks could never fire; the session case worked in production only because
the render passed the described rule, and the `aicDcRules` case failed for exactly the same reason.
The four tests all passed a hybrid with **both** fields set, which is the one shape nothing produces.
A function that accepts two shapes is a function that is dead on one of them, and a test that
constructs the union will never say so.

The fix is one shape and one owner: `alwaysAllowTooltip` reads `destination` only and takes the rule
as the server sent it, `describeRule` computes the tooltip alongside the label and the chip, and the
button renders what it is given. Four new tests go through `describeRule`, because **a test of a
function is not a test of the thing on screen**. Recorded in
[`5-webapp/permission-dialog.md`](../5-webapp/permission-dialog.md) § *Always allow shows the rule,
not a promise*, which also gains the three-destination table it had never had.

### Defect 2: a posture that applied and a selector that never heard

Picking *Accept edits* changed the engine's posture and left the control **disabled, reading "Ask",
with "Waiting for the engine to confirm the new mode…" on it** — permanently, because the selector
flips on the broadcast and on nothing else, and clears its pending flag on the same broadcast. One
use per session, on the surface `acceptEdits` had just been added to.

The cause is two vocabularies for one event: this engine emitted `permissionMode` with `{mode}`, and
`AcApp` has a method called `permissionModeChanged` taking `{mode, by}`. **The server said so out
loud** — `WARNING no remote method AcApp.permissionMode` — into a log nobody reads during a turn.
This is [AG-R-4](risks.md#ag-r-4) from the engine's side: the browser renders what the engine
reports, so the engine has to report in the vocabulary the browser has. It is also phase 4's fourth
finding — *"the hook and the step stream use two vocabularies for one call"* — recurring in a new
place, which is the argument for the tripwire rather than the one-line rename.

Every offline test passed, and they were not wrong: they assert the mode `set_permission_mode`
**returns**, which was correct throughout. Nothing asserted the name it broadcasts, because a name is
only wrong relative to a listener that lives in another language in another directory. So one of the
five new tests reads `webapp/src/app-shell/index.js` and asserts the handler exists — the same idiom
`test_rpc_surface.py` uses to derive browser callers, applied to events instead of RPCs.

### What was confirmed, and what it took

| Claim | Result |
|---|---|
| Phase 9's exit criterion, in a browser | **Met.** *Always allow* on a live `run_command`, then the same command in a later turn allowed by the standing rule — `broker.can_use_tool` never reached, asserted from the log rather than from the absence of a dialog on screen. |
| `acceptEdits` lets an in-repo write through | **Met.** `replace_file_content` on `calc.py` applied with no dialog, `allowed by acceptEdits` in the log, the diff on disk. |
| `acceptEdits` still asks about commands | **Met.** `ls -la` raised the dialog with the posture still in force. |
| The posture selector offers only what the engine accepts | **Met.** Three options on `agy` where Claude shows six — [§ *A posture between "ask every time" and "ask nothing"*](#a-posture-between-ask-every-time-and-ask-nothing-2026-09-05) verified. |
| The symbol index sees an `agy` write | **Met**, after the build above. |

One non-finding worth recording, because it looked like a defect for a minute: a synthetic `change`
on the posture selector does nothing at all. That is the **gesture latch** working — the control
requires a pointer or key event before it honours a change, so a browser restoring form state cannot
arm a destructive posture. It has to be driven with a real gesture, which is a fact about testing
this control rather than a fault in it.

---

## AG-16 — the consultant learns the transport that can pay for it (2026-09-06)

Built in one sitting after the queue review that found it. The user's framing is the whole of the
reasoning: *"using the agy subscription, there is no longer a blocker."*

**What was blocked.** Two items in this directory had sat open since phase 1 with "not a code
problem" beside them — `generate_image` had never returned an image, and no *successful* consultation
had been watched streaming into its tab. Both were the free tier: every Gemini image model reports
`limit: 0` on such a key, which is an allowance of zero rather than a throttle, and what is left is
capped at 20 agent requests per model per day and queued behind paid traffic. The recorded fix was to
enable billing on the key's Cloud project ([AG-12](decisions.md#ag-12)), and nobody had.

**What was already sitting there.** `agy` is the same product reached over a pipe, authenticated by
OAuth against the owner's *subscription*, and its `init` frame advertises `generate_image` among its
57 tools. Phase 8 had already built the process, the stream reader, the registry claim and the gate.
So the second implementation of the consultant is the first one that can run, and it is composition
rather than new machinery.

### The sentence that was true when it was written and wrong when it was read

`claude_code/service.py`'s own log line said *"There is no agy equivalent — the CLI has no one-shot
consultation mode."* It was written in phase 1, when nothing in the tree drove `agy` at all. By
2026-09-05 it was wrong twice over — `agy` runs headlessly per prompt, and phase 8 had built every
piece a consultation needs — and it was still being printed to users as the reason they had no
consultant.

**This is the fourth time this file has recorded the same shape** (`EngineHealth.mcp`, the read tools'
`ARG_ALIASES`, the terminal HUD's four specifications) and the first where the stale claim was
*addressed to the user*. A field with no reader is invisible until an audit; a **diagnostic** with no
reader is worse, because it is read exactly when somebody is trying to fix something and it sends
them to the wrong place.

### Containment, which is where the two transports genuinely differ

The SDK consultant contains itself by *enabling* only `FINISH` and, for an image, `GENERATE_IMAGE`.
That is not available here: `agy`'s tool set belongs to the binary, its headless posture auto-denies
rather than asking, and AIC⚡DC therefore runs it with `--dangerously-skip-permissions` and answers
the hook itself. A consultation cannot subtract a tool; it can only answer for one.

So `AgyGateServer` grew a second posture — `StaticPolicy`, a fixed allowlist with **no dialog** —
beside the broker it already had. Exactly one of the two must be given, checked at construction rather
than at the first tool call, because a gate server with neither would fail closed in a way that reads
as an unhelpful model rather than as a defect.

**A consultation must not raise a dialog, and the second reason is the structural one.** It was
already answered — the `mcp__aic-dc-antigravity__*` call reached the dialog by the ordinary path
before any of this ran. And the Claude turn that asked is *blocked on the tool result*, so a dialog
raised mid-consultation interrupts a turn to ask about a call the user never made and cannot
evaluate. Denials carry prose instead, which is [AG-R-11](risks.md#ag-r-11)'s mechanism used the
useful way round: a refused agent that is told *"answer from what you were given"* answers, where one
told nothing goes looking for another route.

**It refuses to run without the gate installed.** An unclaimed conversation is passed through by the
hook — correctly, that is how the user's own sessions stay untouched — so an unclaimed *consultation*
is an agent with 57 tools and the repository as its cwd. `available` is false without a `current`
gate, and a `stale` one (another checkout's hook) does not count.

### The turn it must not end

`AgySession.stream_turn` finishes by emitting `streamComplete`, which carries a request id and tells
the browser a turn is over — and the open turn is the *Claude* one holding this tool call. A
consultation that used it would settle the user's turn from inside a tool.

So the session was split: `stream_frames` is the reader with no rendering in it, and `stream_turn` is
that plus a translator and the close. One reader, two callers, and a test asserts no `streamComplete`
reaches the tab.

**Splitting it broke something a test caught immediately, and the failure is worth keeping.** An async
generator's `finally` runs when it is closed, and closing the *outer* generator does not close the
inner one — it is left suspended until garbage collection. So ⏹ on a turn (which closes the outer
generator) stopped clearing the turn latch, and the next `stream_turn` was refused with
`TurnInProgressError` on a session with no turn running. Before the split there was one generator and
no inner one to forget. `stream_turn` now closes it explicitly, in a `finally`.

### Three things reused rather than rebuilt, and one field name that is still a guess

`files_written_by` already maps `generate_image` to its path argument under *both* Antigravity
spellings, so the image path is read through the shared table rather than a fourth vocabulary.
`verify_image_write` was lifted out of the SDK consultant unchanged and is now the one rule for
believing a write: `stat` at the absolute path, containment against the repo root, non-zero bytes.
`AgyTranslator` gained the `agent_id` its SDK counterpart has carried since the consultation tab was
built — three hardcoded `None`s — which is the whole of what puts a consultation's blocks in its own
tab.

**What is not settled is which name `agy` gives that path.** Both spellings in the table come from the
SDK's vocabulary; the CLI's `generate_image` arguments have never been captured. The offline tests
feed the shape they assume, so they cannot find this — it is phase 4's `PATH (none named)` trap
exactly, a table that looks right and is never exercised. `scripts/probe_agy_consultant.py` prints the
tool frame verbatim on failure so one run settles it.

### A limit found by a test, in the place least likely to be reasoned about

The consultation's gate socket was first placed under `config_dir`, beside the engine's. A test whose
`tmp_path` was long failed with **`AF_UNIX path too long`** — `sockaddr_un` bounds a socket path at
about 107 bytes, and `config_dir` is configurable while the engine's short path is merely a
coincidence of `~/.config/aic-dc` being short. Consultation sockets now live in the system temp
directory under a short name; the registry entry carries the path to the hook, so where it lives is
immaterial.

### What is proven and what is not

Thirty-two new tests, and the suite is 4,547 green. What they prove is the shape: the policy denies
what it should, the claim is released, no `streamComplete` reaches the tab, every block is attributed
to the consultation, a diverted image fails loudly, and an explicit `engines.consultant` never
silently uses the other transport.

**What they cannot prove is that any of it generates an image**, and that is the exit criterion.
`scripts/probe_agy_consultant.py` is two real turns on the paid subscription: a second opinion allowed
*nothing*, and an image verified on disk with the gate's own decision log showing `generate_image` as
the only allow. Until it runs, this entry describes a build and not a result — which is the
distinction phase 7's two inert checks and phase 3's three live-only bugs were all about.

## Phase 11 — the engine policy, and what a fresh server said about it (2026-09-07)

[AG-17](decisions.md#ag-17) was built on 2026-09-06 with 20 tests and no delivery entry, and its exit
criterion was explicitly not met: *"not met until a fresh server has been started on a Claude-only
`app.json`, since everything above tests the pieces rather than the startup that assembles them."*
This is that sitting. `scripts/probe_engine_policy.py` is the artefact, **18 checks green across two
servers**, and the criterion is met. It also cost two findings, both in the config layer rather than
in phase 11's code, and both about whether `engines.enabled` is a policy an organisation can rely on
rather than a preference one machine happens to hold.

### The check that would have passed for the wrong reason

**This machine has no `agy` binary and no Gemini key.** So `mountable == ["claude"]` was *already
true* before any policy existed, and a probe that started one server on a Claude-only `app.json`
would have gone green while proving nothing — it cannot tell "the policy removed the second engine"
from "there was never a second engine here". That is the same shape as `probe_agy_gate.py`'s deny
tripwire resting on write diversion, and the same shape as phase 9's note that an absent dialog is
also what a turn that never got that far looks like. This suite has now learned it three times, which
is the argument for the probe carrying its own control rather than for another paragraph.

So the probe runs **two servers on the same machine in the same environment**, and the first one is
the point:

| | `app.json` | `mountable` | Claude session's MCP servers |
|---|---|---|---|
| **A. control** | no `engines.enabled` | `agy`, `antigravity`, `claude` | `aic-dc`, **`aic-dc-antigravity`**, `chrome-devtools` |
| **B. criterion** | `enabled: ["claude"]` | `claude` | `aic-dc`, `chrome-devtools` |

Run A is what makes run B a measurement. Both Antigravity engines mount, the consultant's server is
in the session, and then the *only* thing that changes is one key in one file.

**What the probe supplies and what it does not.** Nothing in the startup path authenticates either
transport — the SDK engine mounts on a credential *resolving* plus the wheel importing, and `agy`
mounts on `shutil.which("agy")` — so the probe puts a `GEMINI_API_KEY` in the child's environment and
an executable named `agy` on its `PATH`, and reports that it did. Both are substitutes for a **mount
condition**, never for a working transport, and no turn is taken through either. The stub records
every invocation of itself and there were none, so "startup only asked whether it existed" is
measured rather than assumed.

**Assertion 3 reads the engine's own answer.** `get_mcp_status()` is a control request to the `claude`
CLI asking which MCP servers *it* connected — the criterion's "asserted against the mounted server
list, never against the log line, because a log line saying it was skipped is what a build that
mounted it anyway would also print". An answer with no `aic-dc` in it is treated as **uninterpretable
rather than as a pass**, since a session that mounted nothing of ours also mounted no
`aic-dc-antigravity`.

### Two instrument defects, and the first one is the finding

The first run reported **10 of 16 checks failed**, and neither failure was in the product.

**1. The scratch config directory was a fresh install, not an edited one.** `app.json` is a *managed*
file (`config.py` § `_MANAGED_FILES`), and a directory with no `.bundled_version` is the largest
possible version mismatch, so `_run_upgrade` backed the probe's `app.json` up and overwrote it from
the bundle **before anything read it**. Run B mounted everything and its `engines.enabled` was on disk
the whole time, read by nobody. The probe now seeds the directory as an established install — bundled
config, current marker, the policy merged in — which is what a file a user has edited looks like.
That the upgrade discards the key at all is finding 1 below, and it is recorded rather than worked
around.

**2. The MCP instrument broke on the first non-empty list.** Servers are dialled asynchronously, and
the first answer was `['chrome-devtools']` — a server inherited from the user's own `~/.claude.json`,
arriving before ours, read as "the session mounted nothing of ours". It now waits for `aic-dc`, the
one server that must be present on both runs; only once it is there does the presence or absence of
`aic-dc-antigravity` beside it mean anything. **The docstring had already named this trap** ("an empty
list means *not yet* rather than *none*") and the loop below it broke on non-empty anyway, which is
worth recording because the gap between a stated principle and the code under it is invisible to a
green run.

One ordering change came out of it: assertion 3 now runs **before** assertion 2, against the
criterion's own numbering. A `switch_engine` that succeeded when it should not would change which
engine the session assertion is about, and a missing `aic-dc-antigravity` would then be explained by
the master no longer being Claude rather than by the policy.

### Finding 1: an upgrade silently reverts the policy

`app.json` is managed, so on **any** version change the user's copy is backed up and replaced from the
bundle. Measured directly, one server, marker set to a stale version:

```
app.json before      : engines={'master': 'claude', 'enabled': ['claude']}
app.json after       : engines={'master': 'claude'}
list_engines.enabled : ['claude', 'antigravity', 'agy']
backups              : ['app.json.2026.09.07-04.19-0.0.1-old']
```

The first start after an upgrade is a **two-provider install again**, with both Antigravity engines
mountable and the consultant's tools back inside every Claude turn. Nothing is lost — the value is in
the timestamped backup — but nothing is *in effect* either, and no warning says so, because from the
config layer's point of view this is a routine managed-file refresh. This is
[AG-R-13](risks.md#ag-r-13) recurring through a door that entry did not look at: the risk register
reasons about mount *conditions*, and this is the mount conditions being right and the policy being
gone.

It bears directly on AG-17's premise. That entry answers "a control the user can switch off is a
control the user can switch back on" by making the *file* the thing an organisation manages — and the
file is the one artefact in this system that an upgrade rewrites.

### Finding 2: read-only `app.json` holds, and repeats an aborted upgrade forever

AG-17's own recommendation is that "a workplace that needs enforcement ships `app.json` read-only or
writes it from configuration management". The read-only half was measured and it **works**: the policy
survives, `list_engines().enabled` is `['claude']`, and startup does not fail, because
`ConfigManager.__init__` catches `OSError` around the upgrade pass and logs a warning naming the file.

What it also does is never advance the marker, so the upgrade pass re-runs on **every start**. Two
consecutive starts on the same read-only directory:

```
start 1 enabled: ['claude']   backups: ['app.json.…-04.20-0.0.1-old']
start 2 enabled: ['claude']   backups: ['app.json.…-04.20-0.0.1-old', 'app.json.…-04.21-0.0.1-old']
```

A fresh backup per start, unbounded. And because `app.json` sorts first among the managed files, the
`PermissionError` aborts the loop before `commit.md` is reached, so the *other* managed file is never
upgraded on such an install — a second consequence of a single broad `except OSError` around the whole
pass.

**Neither finding is fixed in the phase-11 work above.** Both are decisions about the config layer
rather than about this phase: making `app.json` merge rather than overwrite changes the upgrade contract
for every key in it (`../1-foundation/configuration.md`), and per-file error handling in `_run_upgrade`
changes what a partial failure means. They were recorded with their measurements so the choice could be
made once, in daylight, by the person who owns the enforcement story — **and both were then taken, the
same day: see § The two findings, fixed below.**

### What is now true

Phase 11's criterion is met, in full and on a fresh server: `mountable` is exactly `["claude"]`, the
selector is offered nothing else, both `switch_engine` calls are refused with `engine_disabled` and a
reason naming `app.json`'s `engines.enabled` rather than a missing binary, Claude is still master
afterwards, and the Claude session's own MCP server list contains `aic-dc` and not
`aic-dc-antigravity`. The static half — the tripwire asserting that the set of Antigravity mount
points is exactly the set consulting the allowlist — is run by the same command and reported beside
the live checks, so one invocation answers the whole entry.

What is *not* proven is anything about the transports themselves, and the probe says so in its own
output: a stub binary and a placeholder key prove that the mount conditions were satisfied and then
overruled, which is precisely what the criterion asks and no more.

### The two findings, fixed — `app.json` is merged, not overwritten (2026-09-07)

Both findings above were put to the person who owns the enforcement story, with their measurements, and
both were taken. What follows is what changed and what was measured after it.

**The finding is wider than the policy, which is what settled the design.** `app.json` was in
`_MANAGED_FILES`, *and* it is the file the application's own Settings tab writes — `CONFIG_TYPES`
exposes `app` for editing, `engines.master` moves when a session switches engines, and AG-16's
`engines.consultant` lives there too. So the upgrade did not only revert AG-17's policy: it reverted
every value a user had ever set through our own UI. The policy is just the case where reverting it
changes what the software is *permitted* to do, which is why that is the case that got noticed.

**Three ways to fix it, and the cheap two are worse than they look.**

| | What it costs |
|---|---|
| Preserve the `engines` block across the overwrite | Fixes the policy and nothing else. The Settings tab's other writes keep reverting, and the rule needs a paragraph of its own explaining why one section of one file is special |
| Move `app.json` to `_USER_FILES` | One line. But a user's copy of this file is a *copy of the bundle taken at install*, so every literal in it would outlive every future change to that default — the code default becomes permanently unreachable for existing installs, and nothing tells anybody |
| **Merge it key by key** | Chosen. Needs a third leg: a pristine copy of the bundle to compare against |

The third leg is the whole design. Without an ancestor, "the user's file differs from the new bundle"
cannot distinguish *I chose this* from *this was the default in the release I installed*. A hidden
`.pristine/` directory in the user config dir holds each merged managed file exactly as the bundle last
shipped it, refreshed on every upgrade, and the pass is then the familiar three-way one: value absent
from disk → take the bundle's (this is how a key a release adds arrives); on disk and equal to the
ancestor → take the bundle's (nobody touched it, so a changed default lands); on disk and different →
keep it (an upgrade does not overrule an edit); **no ancestor recorded → keep it**, which is the safe
direction and is what every install existing today gets on its first upgrade after this change. A key
the bundle dropped stays on disk unread, for the same reason a retired *file* does. Nested objects
recurse, which is what lets `engines.enabled` survive a release that changes `engines.master`'s default
— measured, not assumed.

`commit.md` is still overwritten, and the distinction is not an inconsistency: it is prose the bundle
owns, a user who edits it is patching a prompt, and the backup is how they get their text back.

**The second finding needed no policy argument, only per-file scope.** Each file is now upgraded inside
its own error handling, and the marker is written even when one of them could not be — deliberately, and
stated in the code: the alternative is what was measured, a pass that repeats on every start, drops a
backup each time, and never upgrades the files it *could* write. The pass names the file it left as
found and the marker to delete to retry. A backup taken for a write that then failed is removed again,
because a copy of a file we did not change is exactly the litter the old behaviour produced. The
try/except around the whole pass stays as a backstop for the case it was really for: the user config
*directory* being uncreatable, where the fallback is reading the bundle directly.

**What was measured after the change**, in the order it was run:

- **57 tests** in `tests/test_config.py`, 15 of them new, including the two findings as regression
  tests by name: `test_an_upgrade_keeps_the_engine_policy` and
  `test_a_read_only_app_json_holds_and_does_not_repeat`. The rest pin the parts a merge gets wrong when
  it is written in a hurry — a changed default reaching an install that never touched it, a user edit
  outranking that same default, a key a release introduces, the nested case, a dropped key surviving, an
  install with no ancestor keeping everything, the pristine copy tracking *this* release so a third one
  lands, no backup when the merge changes nothing, a backup holding the pre-merge text when it does, and
  an unparseable file left for the user to fix rather than replaced. Two unit tests cover `_merge_json`
  directly: a recorded `null` ancestor is not the same as no ancestor (`.get()` cannot tell them apart
  and they mean opposite things), and the merge does not mutate its inputs.
- **The whole suite: 4,581 passed, 1 skipped.**
- **The durability measurement, re-run.** Same script, same two scenarios, against the new code:

  ```
  --- upgrade (stale_marker=True, read_only=False) ---
    app.json before    : engines={'master': 'claude', 'enabled': ['claude']}
    app.json after     : engines={'master': 'claude', 'enabled': ['claude']}
    list_engines.enabled: ['claude']
    backups            : []

  --- readonly (stale_marker=True, read_only=True) ---
    app.json before    : engines={'master': 'claude', 'enabled': ['claude']}
    app.json after     : engines={'master': 'claude', 'enabled': ['claude']}
    list_engines.enabled: ['claude']
    backups            : []
  ```

  Before the change the first block's `after` line read
  `engines={'master': 'claude'}` with `list_engines.enabled` back to all three. The read-only run's
  empty backup list is the second finding gone: the merge finds nothing to change, so there is no write
  to fail and nothing to back up.
- **`scripts/probe_engine_policy.py` re-run end to end: `PASS — 18 checks, both servers`.** The
  criterion still holds through a changed config layer, which is the point of having run it against a
  fresh server rather than against the pieces.

**A red test the re-run surfaced, unrelated to any of this.**
`tests/test_agy_service.py::TestItWillNotRunUngated` had two tests asserting `gate_not_installed` that
could only reach the gate check on a machine with an `agy` binary on `PATH`; on this one they hit the
*missing-binary* refusal first and asserted the wrong error. Red at `HEAD` before this work, in a file
whose own docstring says "Offline. No `agy`" — and one branch below a comment warning about exactly this
machine-dependence, "which is how it went green for a day and then red the moment one was". Fixed with a
`gated_service` helper whose executable is a name that always resolves; `connect_engine` only asks
whether the name resolves and refuses long before launching anything, so nothing is executed.

**What this does not do.** It does not make `app.json` a user file, so the bundle still owns any key
nobody has touched — which is the intent, and is why the pristine copy is refreshed rather than written
once. It does not preserve formatting: the merged file is re-serialised, so key order follows the file
on disk with bundled additions appended and the bundle's hand-wrapped arrays come back expanded. And it
does not give a user any *notice* that a merge happened beyond the log line and the backup; a Settings
surface for that would be a new decision, not a completion of this one.

---

## Phase 10 — the consultant on the paid transport, and the argument that does not exist (2026-09-08)

**Exit criterion:** *"A real `generate_image` writes a picture inside the repository on the
subscription, verified by `stat` and containment rather than by the tool's own report (AG-R-3), with
the gate's log showing `generate_image` as the only allow; and a real `second_opinion` returns prose
having been allowed nothing at all, with no `streamComplete` reaching the tab."* **Met.**

`scripts/probe_agy_consultant.py`, two real turns on the account holder's subscription. This was the
last open item in this directory, and [AG-16](decisions.md#ag-16) had said what it was for: *"until it
runs, this entry describes a build and not a result."* It ran, it failed, and the failure was the
point — the build was wrong in the exact place AG-16 had flagged as unmeasurable offline, and one
layer deeper than it had guessed.

### The half that passed first time

**A second opinion, allowed nothing.** A real answer on the subscription, every block attributed to
the consultation's own tab, and no `streamComplete` reaching the browser. The gate's log for that turn
is `allowed []` — not even `finish` was asked for. That is the whole of AG-16's containment claim,
measured: an agent running under `--dangerously-skip-permissions` with the binary's 57 tools
available, restrained to prose by a `StaticPolicy` and a registry claim.

### The half that failed, and what it was

`generate_image` **worked** — for the first time since phase 1. A 1024×1024 JPEG, 269,874 bytes, on
the subscription that has an allowance where the free-tier key reports `limit: 0` for every image
model. AG-1's worked example, four weeks after it was specified.

Then the probe refused it, correctly. The picture was at
`~/.gemini/antigravity-cli/brain/<conversation_id>/probe_icon_1788851691210.jpg` — outside the
repository, invisible to the file tree and the viewer, and unverifiable from the frame. The frame is
the finding:

```json
"parameters": {"ImageName": "probe_icon", "Prompt": "A simple flat-colour icon of a padlock…"}
```

**There is no output-path argument, and not on either transport.** AG-16 left open *"which name does
`agy` give that path"*, expecting a spelling. The answer is that the tool does not take one: its
schema declares `ImageName`, *"Short descriptive name for the saved file"*, and the harness picks the
location. The requested `.png` came back as `.jpg` for the same reason — the caller was never choosing.

**`sdk-surface.md` had recorded this correctly and nobody had read the column headings.** Its
step-stream table lists `generate_image`'s inputs as `prompt`, `image_name`, `aspect_ratio`, with
`output_path` among the **outputs**. `files_written_by` — a table of tools to their *path arguments* —
has held `("output_path", "OutputPath")` throughout, and it worked on the SDK transport for the reason
phase 3's finding 1 gives: that stream merges a tool's results back into `args` at `DONE`, so a result
field is readable as an argument there. `agy` does not merge. On this transport the path is nowhere in
the machine-readable stream at all, only in the model's prose, which is precisely what AG-R-3 forbids
believing.

So this is a **fourth** instance of the shape this log keeps recording — a table that looks right,
is never exercised, and degrades quietly — and the first where the table was not merely unexercised
but was answering a question its subject does not accept.

### The fix: collected, not requested

The image is copied out of `brain/<conversation_id>/` into the repository, and the location is derived
from what the frame *does* carry — the conversation's own id and the name the model chose — rather
than parsed out of prose. A consultation holds one conversation for the length of one call, so that
directory belongs to this call and nothing else writes into it; "the newest file in it" is either this
image or nothing.

Three details are decisions rather than mechanics:

- **The produced extension wins.** Asking for `icon.png` and receiving JPEG bytes yields `icon.jpg`. A
  `.png` holding a JPEG is a second lie told to make the first one tidy.
- **The prompt no longer asks the model to place the file.** It used to say *"write it inside `<repo>`
  and report the absolute path"*, and the first run showed exactly what a model does when asked for
  something its tools cannot do: it reached for `run_command` to move the file. The policy denied it,
  which is AG-R-11 contained — and the denial was avoidable, because we had asked for it.
- **"Could not be found" is worded apart from "generated no image".** The first means the collector or
  the write location is wrong; the second means prose claimed a picture nobody made. The old code
  said the second in both cases, so the first live failure reported *"Antigravity generated no image
  file"* about an image sitting on disk.

`files_written_by` is left alone. Adding `ImageName` to it would encode the wrong belief — a name is
not a path and no file exists at it — and `test_the_shared_table_cannot_answer_this_and_that_is_the_finding`
pins the emptiness so a future reader finds the reason rather than the gap.

### What the closing run measured

`PASS`, both turns, exit 0: a second opinion allowed nothing, and `probe-icon.jpg`, 322,638 bytes,
inside the probe's own directory, with `generate_image` the only allow. One line of it is the
instruction change reporting on itself — **`denied along the way: []`**, where the first run had
denied a `run_command`. The model no longer tries to move the file, because it is no longer asked to.

Offline: 4,608 tests, five of them new, including the fake `agy` corrected to emit the shape that was
measured rather than the shape the code assumed. That fake had been feeding `OutputPath` — AG-16
predicted this in as many words (*"the offline tests feed the shape they assume, so they cannot find
this"*), and the prediction is worth more than the bug, because it is the argument for the probe
existing at all.

### What this does not do

- ~~**The engine has the same gap.**~~ **Closed 2026-09-08, `2cbb61a1`.** A user asking the `agy`
  engine — rather than the consultant — for an image got a file in `brain/` that the file tree could
  not see: produced correctly, then invisible to the tree, the viewer and the user. The collector had
  lived in the consultant because that is where the criterion was, and "the same reasoning applies one
  level up" is what the fix is. `BRAIN_DIR` and the locator moved into `steps.py` where both callers
  reach them; the translator collects after a successful call and reports the landed path in
  `files_modified`, so the tree reloads. The service supplies the repo root and the conversation id,
  because neither appears in the turn's own frames.

  **The locator grew two options rather than one, and the reason is the difference between the
  callers.** A consultation is one process holding one conversation for one call, so *newest file in
  the directory* is safe there and stays behind `allow_newest`. An engine conversation is long-lived
  and resumable, so its directory accumulates every turn's images and a reused `ImageName` would
  collect an **earlier** picture and report it as this turn's — `min_mtime` bounds the search to the
  turn. A locator is only as safe as the lifetime of the thing it searches.

  It also dropped the `OutputPath` alias from `generate_image` in `tools.py`. It is a result field on
  the SDK transport and never an argument, so it matched no real call and its only effect was claiming
  the permission dialog could show a PATH it cannot — the same falsified-argument finding as above,
  one place it had been left behind.
- **Nothing renders the collected image.** It lands in the repository, so the file tree and the
  viewer find it by the ordinary path, but no consultation tab shows a thumbnail.
- **The `.jpg` is not converted.** What the harness produced is what lands, which is the honest thing
  to store and means a caller asking for a specific format does not get one.

---

## The subagent that was never gated (2026-09-09)

Found while building subagent tabs, and it stopped that work: the tabs are cosmetic and this is not.
[AG-R-14](risks.md#ag-r-14) carries the risk; this is what happened.

### How it was found, which was not by looking for it

`AgySession.cancel()` stops a turn by starving it — `gate.refuse_all`, because there is no halt frame
on this transport — so settling whether ⏹ could stop *one* subagent meant reading how the gate scopes
a call. It scopes by conversation id, and passes through every conversation nobody has claimed. A
subagent has a conversation of its own. The question "can we stop one subagent" turned into "have we
ever been gating one", and the answer was no.

**Measured rather than argued**, because a security claim reasoned from a code read is a hypothesis.
`scripts/probe_agy_subagent_gate.py` allows the delegation and denies everything else, so the
assertion is a file rather than a log line: gate consulted → the write is refused → the bytes are
unchanged; gate bypassed → the write runs. The first run:

    conversations the gate was asked about: ['5c1a3e72-… (parent)']
    tools the gate decided:                 ['invoke_subagent']
    target.txt after the turn:              'SUBAGENT_WAS_HERE'

The user approved *the spawn*, and the subagent then edited a file with no review of any kind.

### The fix, and the false start that is the more useful half

`AgyGateServer` holds a **set** of claims rather than one id, and `AgySession._gate_subagents` claims
each announced subagent's conversation as the frame arrives — before it is yielded and before the
translator runs, which is the earliest instant this process controls. `stop()` releases all of them,
including a subagent still running at teardown, because a registry entry is a file that outlives the
process and a claim left behind would intercept a later session of the user's own.

**The first cut read `state` the obvious way and failed live, identically to the unfixed code.**
Claim on `ACTIVE`, release on `DONE` — except that `DONE` on a `subagent` step means **the launch
finished**, not the subagent. Measured at `duration_seconds: 0.10` on a delegation whose work then ran
six seconds longer, over frames in which the parent polled it through a `manage_subagents` call. And
the run that exposed it never emitted `ACTIVE` at all, so the release fired on a claim that had never
been made. This is [§ Phase 9's](#phase-9--always-allow-on-antigravity-2026-09-05) lesson and D1's
arriving together: **a state word means what the engine does with it, not what it says.** Now it
claims on any announcement and releases only at session close — holding a claim costs one registry
file, dropping one early costs the review.

    conversations the gate was asked about: ['240bcbd2-… (subagent)', '4009ccc4-… (parent)']
    tools the gate decided: ['invoke_subagent', 'view_file', 'replace_file_content', 'send_message']
    target.txt after the turn: 'ORIGINAL_TEXT'

The consultant is covered by the same change without knowing about it, because it drives
`stream_frames` too (AG-16).

### What this does not do

The three residues are in [AG-R-14](risks.md#ag-r-14) rather than repeated here: the spawn-to-announce
race, nested delegation announcing a grandchild to nobody, and a stale claim now orphaning one entry
per subagent rather than one per session. Two of the three were named by **the consultant**, asked for
an adversarial review of this fix on the day it landed — the first time `second_opinion` has been
pointed at this repository's own work, and the argument for [AG-13](decisions.md#ag-13) arriving from
a direction that decision did not anticipate.

### The tests, and the one that is about a refactor rather than a behaviour

Twelve, offline: the gate server holding several claims at once, releasing one and keeping the rest,
`stop` releasing all, a blank id claiming nothing; the session claiming on announcement, claiming
rather than releasing on `DONE`, claiming every subagent in one step, and logging rather than dying
when a claim fails. The twelfth reads the source of `stream_frames` and asserts that
`_gate_subagents` appears before `yield frame`. That is the residue's size pinned as a test: the race
window is a frame read, and a refactor that moved the claim into the translator or after the yield
would widen it to the subagent's whole life without failing anything else.

---

## Identity by cgroup, not by claim (2026-09-10)

Two of the three residues above are closed, and not by claiming faster. [AG-18](decisions.md#ag-18)
holds the decision and [AG-R-14](risks.md#ag-r-14) the risk; this is what was built and what it was
measured against.

### The two residues were one premise

The spawn-to-announce race and the nested-delegation blind spot read as separate bugs and are the
same one. [AG-14](decisions.md#ag-14)'s gate decides ownership by looking up a **conversation id**
somebody wrote down in advance, and both residues are cases where nobody could: `agy` starts a child
before the frame naming it arrives, and a subagent's own frames never reach this host at all, so a
grandchild is announced to nobody. Every fix aimed at the claim is a fix aimed at how *fast* it lands,
and one of the two cases has no claim to be late with.

So the premise went instead. **A container can be registered before it contains anything.** `agy` is
launched inside a systemd user scope, and the hook asks the kernel which group it is in.

### What landed

`src/aic_dc/agy/scope.py` is the new module and it is four functions: `available()`, `unit_name()`,
`wrap()` and `current_cgroup()`. Around it:

- `AgyGateServer` names its unit **at construction** (`scope.unit_name() if scope.available()`) and
  publishes `registry.claim_scope(unit, socket)` inside `start()`, *before* `AgySession` spawns
  anything. That ordering is the fix rather than an implementation detail: a conversation claim can be
  late because the conversation exists before we are told its id, and a scope claim cannot, because
  the scope does not exist until we create it.
- `registry` gains `claim_scope`, `release_scope` and `scope_owner`, under a `scope-` filename prefix
  so `lookup` and `owns_anything` cannot confuse a unit we chose with an id `agy` chose.
- `hook.decide` consults `registry.scope_owner(scope.current_cgroup())` **only when the conversation
  lookup misses**. Second, not first, because it is the fallback: a machine with no systemd never
  leaves the old branch, which is what makes this additive rather than a rewrite of a shipped gate.
- `AgySession._argv` ends with `scope.wrap(argv, self._gate.scope_unit)`, and returns argv untouched
  where the unit is `None`.

**`available()` runs a scope rather than testing for the binary.** `systemd-run` exists in containers
and on machines with no running user manager, where it is present and fails — and a capability
asserted rather than exercised is how this directory has been wrong before. It is `lru_cache`d,
because it is a property of the machine and it costs a subprocess in the path of starting a session.

**Matching is against the units this host registered, never against the name's shape.** A prefix test
on `aic-dc-` would let anything in a scope a stranger happened to name that way route into our dialog.
`scope_owner` walks the entries we wrote, and matches `f"{unit}.scope" in cgroup` rather than the bare
name, so a longer unit that merely starts with ours does not match either. [AG-R-12](risks.md#ag-r-12)
survives on a stronger footing than it had: the user's own session sits in their terminal's spawn
scope and simply is not in the directory.

### The measurement, and then the falsification

`scripts/probe_agy_cgroup_identity.py` ran two real turns on 2026-09-09 and answered the four
questions the design needed — `agy` runs normally inside a scope, the hook subprocess (which `agy`
spawns, not us) inherits it, **a subagent's own calls carry it**, and a turn taken outside the scope
reported the terminal's `ptyxis-spawn-…` cgroup instead. The last row is the control: a matcher that
answered yes to everything would have passed the other three.

That establishes the mechanism works. It does not establish it is **load-bearing**, because the
registry was still running underneath it and a claim that happened to land in time would produce the
same log. So `probe_agy_subagent_gate.py` was re-run with `AgyGateServer.claim` replaced by a no-op
for *both* the parent and the subagent. All eight tool calls still reached the dialog — the subagent's
seven included — and the deny still left the target file byte-identical. Conversation claims were
inert and cgroup identity carried the whole load.

`run_command` and `find_by_name` are in that list, which is [AG-R-11](risks.md#ag-r-11) looking for
another way out of a refused write and being gated on each attempt.

### The defect the change caused, found before it shipped

Under a scope, the program `exec` resolves is `systemd-run`, which exists. So a missing `agy` stopped
raising `FileNotFoundError` and would have surfaced as *"exited before sending its init frame"* — the
opaque diagnostic that `AgyNotInstalledError` was written to replace. `start()` now checks
`shutil.which(self._executable)` before spawning. The `except FileNotFoundError` stays, for the
unscoped path and for a `systemd-run` that goes missing between the availability probe and the spawn.

This is the shape of thing a wrapper does: it moves the identity of the process being launched, and
every error keyed off that identity moves with it.

### What this does not do

**It is Linux-and-systemd, and the packaging is not.** Where `available()` says no, everything
degrades to the conversation-id routing that shipped before, with both residues intact — so on the
macOS and Windows artefacts phase 7 publishes, AG-R-14 is unfixed. That is stated in `scope.py`'s own
module docstring rather than left to be discovered, and the choice for those platforms — the
capability-reducing deny, or an unclosed residue — is open. See [AG-18](decisions.md#ag-18)
§ *What it costs on the platforms that cannot*.

**The third residue was untouched when this entry was first written** and is closed below.

### The incidental finding, which was a working gate reporting itself broken

The probe's own setup reported the installed gate as `stale` on a machine where it was live.
`install.status` compared command **strings**, and one venv ships `python`, `python3` and
`python3.13` in one directory pointing at one program — so an entry written by a process started as
`.venv/bin/python3` did not match a status call from `.venv/bin/python`. `AgySession` refuses to start
on a stale reading, so the whole transport was one entry point away from presenting as broken for a
reason that is not a fault.

Fixed the same day, and **the interesting part is the fix that was rejected**. Resolving both paths
and comparing the targets forgives the spelling — and forgives far too much, because every venv's
`python` resolves to the *system* interpreter, so two different checkouts would compare equal and
`status` would report `current` for an install belonging to someone else. That is the exact condition
`stale` was written to report. So the parent directories are compared first, through `realpath` so a
checkout reached by a symlink is still itself, and only then the interpreters. String equality still
answers first and touches no filesystem, because it is the answer almost every time.

Four tests, and the second is the one that keeps the first honest: the other spelling is `current`, a
second checkout is still `stale`, a different `config_dir` with the same interpreter is still `stale`
— only the interpreter is forgiven, never the arguments — and a deleted interpreter is `stale` rather
than an `OSError` raised at a Settings caller.

### The tests

Seventeen, offline, in `tests/test_agy_scope.py`, and the ones that matter are the negatives. A scope
we registered is ours; a terminal's own scope is not; **a unit named like ours but never registered is
not**, which is the imitation attack; a longer unit sharing our prefix is not; a released scope stops
being ours; a scope entry is not a conversation. Then the hook's four: an unclaimed conversation
inside our scope is gated (the residues' case), a stranger in their own scope is still passed through
(AG-R-12), a claimed conversation still wins, and **no scope means no change at all** — the tripwire
that says this is additive to the shipped gate rather than a replacement of it. Plus publish-before-
spawn, release-on-stop, and argv wrapped and unwrapped.

---

## What a subagent did, read off `agy`'s own disk — and the pairing rule 121 transcripts overturned (2026-09-10)

`subagent_rows` landed on 2026-09-09: the live strip now shows a delegation on this transport, because
`agy` announces one with a conversation id of its own. A row you cannot open is a worse offer than no
row, so this is the other half — clicking one, and getting the subagent's work.

### One capability key was answering two questions the transports answer differently

`subagent_tabs` bundled *"can this engine show what a subagent did"* with *"can it stop one"*, and on
`agy` those have opposite answers. **There is no halt frame on this transport.** ⏹ is starvation —
`AgyGateServer.refuse_all` denies every pending call for the rest of the turn — which is turn-wide and
unscoped by construction, so there is nothing to aim at a single subagent. Meanwhile the transcript is
sitting on disk, complete, in a format the history browser's renderer already understands.

So the key split: `subagent_transcripts` is **SUPPORTED** on `agy` and `subagent_stop` is **UNBUILT**
on both transports. Two consequences worth stating because they are the sort of thing a later reading
gets backwards:

- **`UNBUILT` on the surface does not make `ConsultantBridge.cancel` decorative.** A consultation *is*
  stopped, by the bridge, without the request ever reaching the CLI. What is unbuilt is stopping a
  subagent **`agy` itself** spawned. The docstrings on both `bridge.cancel` and
  `ClaudeCodeService.stop_task` now say which of the two keys they mean.
- **No row carries a `task_id`.** `task_id` is what `stop_task` takes, so a value there would be a
  handle onto a method that would refuse it — and this transport's transcript carries no call id to
  put in one anyway. `tests/test_agy_subagents.py` asserts the key's *absence*.

### The RPC pair, and the two gates that keep it from being a hole

`AgyService.list_subagent_transcripts` and `.get_subagent_transcript`, both synchronous file work on
the executor like every other history read here.

`agent_id` is a conversation id in `agy`'s own store, and that store holds **every conversation the
user has ever had with this CLI** — including the ones they had with the IDE, in other repositories,
that AIC⚡DC never ran and has no business showing. An unchecked read is therefore not "the wrong tab
opens"; it is *open any Antigravity conversation on this machine by id*. Two gates, because either
alone leaks:

1. **`_mirrors_session`** — the session has to be one this repository's mirror holds. It asks the
   store's own `list_sessions` rather than `history.list_sessions`, which parses every transcript to
   count its messages: a session-list page's worth of work to answer a yes/no.
2. **`subagents.descendants`** — the agent has to be reachable *by announcement* from that session.
   It walks deeper than the one-level listing needs, deliberately: a containment check that has to be
   widened later is one that gets widened wrongly. The session is never its own descendant, a
   self-announcement does not open that door either, cycles terminate, and the walk limit is logged
   rather than silently narrowing — a check that quietly stopped looking would refuse a transcript the
   user is entitled to, and it would read as *"the record is gone"*.

Under both, a refusal reads as an **unreadable transcript rather than a permission error**: the
browser renders the reason inside the tab, and "not this session's subagent" is a sentence about the
request. A failed ownership check refuses; it never allows. And `_safe_id` in the reader is the third
line, present even though `descendants` is the real containment, because the cost of being wrong there
is reading the user's home directory rather than showing an empty tab.

### The reader, and the one shape two engines owe the renderer

`src/aic_dc/agy/subagents.py`: find the file, read the records, pair a call with its result, render
messages the chat panel can draw. `agy`'s stream carries no trajectory or depth field, so a subagent's
work is invisible to the live pump — `steps.py` now says so, and says where it *is* read instead.

One renderer draws both engines' tabs, so this reader owes `claude_code.history` one message shape.
That is asserted against `history._text_block` rather than against a literal list of keys: a field
added there and not here is exactly the drift the test is for, and a literal would go stale without
failing. The card conventions follow the sibling reader too — an unanswered call is `pending` with
`result: None`, no turn claims a clean finish on no evidence, and **usage is absent rather than zero**,
because a zero renders as a turn that cost nothing.

### The defect a test caught before the measurement did

A child's reply reaches its parent as a `<SYSTEM_MESSAGE>` block, and the record **opens with a
preamble that mentions the tag** — `The following is a <SYSTEM_MESSAGE> not actually sent by the user`.
A non-greedy match therefore finds prose *about* the frame instead of the frame, and renders the
disclaimer, a stray opening tag and the body as one message. The assertion failed when it was written,
which is why it is its own test rather than a line in the one above it.

### The central inference was wrong, and 121 transcripts is what said so

Every inference in this reader came from **one** capture, and each had a plausible wrong answer
available. So before writing it up it was run against every `transcript_full.jsonl` on this machine:
**121 files, 1,895 records, 720 tool results**. The full vocabulary is in
[`sdk-surface.md` § The `agy` transcript store, read whole](sdk-surface.md#the-agy-transcript-store-read-whole--measured-2026-09-10);
what the run *overturned* is here.

No record type carries a tool result and no id links a result to its call, so the reader paired each
result with the oldest call still open. The arithmetic is the truth instead: a result's `step_index` is
its call's index, **plus one, plus the call's position within that step**. The difference only shows up
where a call has no result — and:

> **A tool call our own permission dialog refuses is written nowhere.** No result record, no error
> record, no status: the index just skips.

That is 58 calls across the 121 files — 39 mid-conversation holes and 19 at a file's last response
(a turn still running, or cut short). One hole knocks every later card in the conversation one call out
of step, and simulated over the same files the queue rule attaches **302 of the 720 results to a call
that is not theirs, in 24 conversations**. Twelve of those hand a *write* call someone else's result,
at which point `files_written_by` credits a file to a call that never ran — the worst failure
available, since that list is what the browsed turn reports as changed. Under the arithmetic all 720
attach to their own call and none is left unattributable.

**The specimen was already on disk because of last week's work.** `probe_agy_subagent_gate.py` denies
everything but the delegation, so steps 4, 10 and 14 of its subagent's conversation have no result
record. The only transcript on this machine where a denial is the *normal* case is the one AG-R-14's
probe wrote, and it is what made the hole visible.

Three more corrections came out of the same run, none of which a green test suite would have raised:

- **`KNOWN_TYPES` had 4 of the 16 real types.** Ten of them name a result after its tool
  (`VIEW_FILE`, `CODE_ACTION`, `RUN_COMMAND`, …) and are the format of the 9 conversations from
  2026-08-03 to 08-16; all 112 since 08-29 use `GENERIC` alone. Reading only the current spelling is
  correct on everything a user is likely to open and turns a whole era into unrendered blobs with
  their cards stuck pending. `sdk-surface.md`'s 2026-09-03 reading had listed the older set *as* the
  current one; that paragraph now carries a correction.
- **There is no `ERROR` status**, so `failed = status == "ERROR"` was a dead branch and every failure
  rendered as a success. Failure is the `ERROR_MESSAGE` **type**, and a current-format runtime failure
  says so in prose this deliberately does not sniff — `specs5/5-webapp/chat.md` § *Card Anatomy* makes
  the status flag the only thing a card's failure styling may read.
- **`RUNNING` is a backgrounded tool**, 43 records, never the last line of a file. Rendered `ok` it
  was a card claiming a tool had finished with whatever output the harness had printed so far; it is
  `pending`, `done: false`, no duration.

And one inference was replaced by its opposite: `read_records` sorted by `step_index`, which agrees
with append order on 120 of the 121 files — and the 121st **interleaves two concurrent turns and uses
five indices twice**, so sorting moved its records away from what the harness wrote. The index answers
*which call is this the result of*; it does not answer *what happened next*.

### Two guards in the browser, and neither names an engine

[AG-R-4](risks.md#ag-r-4) again: the browser must never branch on an engine name.
`tabs.js::_loadSubagentTranscript` checks `supports(SURFACE.SUBAGENT_TRANSCRIPTS)` **before the RPC**
and renders an unreadable-transcript notice — *"This engine cannot read back what a subagent did"* —
rather than letting the call fail at the router and reporting it as an error. `block-render.js` draws
Stop only on `live && row.task_id && supports(SURFACE.SUBAGENT_STOP)`, which is three conditions
because they fail in three different ways: a browsed row has nothing to stop, a row without a task id
has no handle, and a transport without the surface would refuse the call.

`engine-capabilities.js::supports` reads an **unknown key as supported** — the deliberate opposite of
the server's rule, so that a browser running against an older server does not hide working features.
It also means a new guard is untested unless its fixture sets the key explicitly, which is what the
new webapp tests do.

### The tests

Sixty-six, offline, in `tests/test_agy_subagents.py`, against a `tmp_path` store with no `agy` and no
network. They are written as falsifications of the plausible reader rather than as coverage: the one
that trusts the tool's name (`invoke_subagent` here, `start_subagent` on the SDK — a reader keyed to a
name is one rename away from listing nothing, silently, because a name matching nothing looks exactly
like a turn that delegated nothing); the one that reads `transcript.jsonl` because it has the obvious
name (it double-encodes every tool argument, so each card would render `"\"/tmp/x\""`); the one that
hands out any conversation whose id looked like a UUID.

Four of them exist only because of the measurement, and the sharpest is
`test_a_call_our_dialog_refused_does_not_take_the_next_calls_output` — a refused write, no result
record, then a `list_directory` whose output must land in its own card while the write stays pending
and attributes no file. Its sibling puts the same hole *inside* one step, where the queue rule gets
both cards wrong from one missing record. 4,804 Python and 4,484 webapp tests green.

### Does the pair answer in the app? The id chain, followed rather than assumed

Both methods take the session the browser is showing and hand it to `subagents.rows()` and
`subagents.descendants()` as an `agy` **conversation id**. Nothing says those are the same string, and
if they were not, **every test would still pass** — a fixture builds its store around whichever id it
hands in — while the tab stayed empty in the app for good. That is the shape of failure this plan has
already paid for four times, so the chain was followed link by link on this machine rather than read
as obvious:

- `agy/session.py` takes `_conversation_id` off the `init` frame and returns it as the session's
  identity; `service.py` hands that same value to the translator and to the mirror.
- `mirror.py::attach` files entries under it — refusing a non-UUID, because the reader validates —
  keyed by `project_key_for_directory(repo_root)`, and `RepoSessionStore._list_sessions_sync` reports
  each row's `session_id` as the file's stem. `_mirrors_session` asks for the same project key and
  compares the same field, so the ownership gate closes on one string rather than on two spellings of
  one.
- And the id `agy` mints is the directory name in its own store: all **8** entries in the gate
  registry — `~/.config/aic-dc/agy-sessions/<conversation_id>.json`, each written by a live turn — are
  directories under `~/.gemini/antigravity-cli/brain`. (Two roots share that leaf name and are never
  the same directory: `MIRROR_DIR` hangs off the repo's `.aic-dc/`, the registry off the user's config
  dir. Worth knowing before grepping for one and finding the other.)

Then the reader over the store as it stands: **127** conversations, **121** with a
`transcript_full.jsonl`, **7** holding subagent rows, 8 rows between them. `load()` on one returns five
messages in the shape `restore.js::restoreMessage` accepts — `role`, `content`, `blocks` of tool cards,
`system_event`, `timestamp` — which is the shape the Claude reader emits, and has to be, because one
normalizer serves both engines' tabs.

The containment gate was checked against the real store too, not only in a fixture: `descendants()` on
the two-subagent conversation returns exactly its two children, and **another real conversation on this
machine** — readable, its own `transcript_full.jsonl` sitting there — is refused by id. That is the
case the gate exists for, and it can only be demonstrated because the store holds conversations that
have nothing to do with this repository.

**Still owed:** nobody has opened the tab in a browser on a live `agy` turn. The guards and the gates
are asserted in tests, the reader is measured against the real store, and the id chain above is read
off disk — but the round trip has not been watched: a delegation announced, its row clicked, the
transcript arriving through the RPC and rendering. Until then this is a read that demonstrably works
and a wire that is only argued to.

---

## The pid that was written and never read (2026-09-10)

[AG-R-14](risks.md#ag-r-14) residue 3, and the one the consultant said had to land first: `claim`
recorded a `pid` since AG-14 and `lookup` had never read it, so an entry left behind by a host that no
longer exists was indistinguishable from a live claim. AG-18, earlier the same day, doubled the kinds
of entry carrying an unread handle without changing what the handle was for.

### What a stale entry actually costs, which is not what the residue said

The residue was written as a tidiness problem — "it orphans one entry per subagent". Reading the two
callers says otherwise, and both costs land on **the user's own work** rather than on ours:

- `owns_anything` counts *files*, and it is the tie-breaker for a payload the hook cannot parse. One
  unclean exit of ours therefore made "we own something" permanently true, and from then on every
  unparseable payload from *the user's own* `agy` was denied. The function's docstring says it "fails
  toward the user's work rather than toward silence"; with a corpse in the directory it did the
  opposite, for good.
- `AgyGateServer.release`'s docstring already named the other one: a claim left behind "would make this
  host intercept a *later* session of the user's own that happened to resume that conversation" — the
  interception `probe_agy_isolation.py` exists to hold. That was written about a claim we forgot to
  release. A host that is killed cannot release anything, so the same breakage arrives by a route no
  amount of care in `stop()` can close.

### The recommendation in the register was wrong as written, and finding that out was the work

The entry said to *"treat an entry whose process is gone as absent rather than as ours"*. Read that as
a change to what the **gate answers**, because that is what it is: absent means the hook passes the
call through. And `registry.py`'s own module header has argued the opposite since AG-14 — *a dead host
makes our sessions un-runnable rather than un-gated* — with the reasoning spelled out, which is why
the contradiction was visible before anything was built rather than after.

The case that settles it is the one AG-18 created. A host killed mid-turn can leave an `agy` **still
running inside a scope of ours**: same unit, so `scope_owner` still matches, and the process is under
`--dangerously-skip-permissions` with our hook as the only thing between it and the tree. Passing its
calls through because our socket stopped answering hands it an unreviewed repository.

So the residue was misdiagnosed rather than merely unfixed — and so was the diagnosis. It is not "the
hook denies on stale entries", and it is not "stale means allow" either. It is that *stale* was one
word for two states, and the fix is telling them apart.

### Liveness is two pids, because "stale" and "dead" are different questions

`agy` is a **child** of this host, and a child outlives a killed parent. A dead host whose `agy` is
still running is an **orphaned agent**, and allowing there would produce exactly the unreviewed write
this transport has no second check for.

So `claim` now records the conversation's `agy_pid` alongside the host `pid`, and `entry_is_live` is an
**or**: either process alive, the entry stands and the hook goes on denying calls it cannot get an
answer for. Only when both are gone is the file a corpse — and a corpse reads as *not ours*, which it
can do safely because no process from that session is left to be un-gated. `lookup` returns `None` for
one, `owns_anything` does not count one, and `reap_stale` deletes one.

**The sweep runs from `AgyGateServer.start`, beside the stale-socket unlink that has the same cause.**
Keyed on liveness rather than on ownership, deliberately: "remove entries that are not mine" would
delete the live claims of a second host sharing the directory and un-gate its session.

### The scope entry, which cannot name its own agent yet

The risk entry, written earlier the same day, ranked a stale scope entry above a stale conversation
entry because "a unit name is something systemd can hand to a later process". `scope.unit_name()` is
`aic-dc-` plus twelve hex characters of `uuid4`, so a recurrence is not a case worth designing
against, and that sentence is withdrawn rather than left to be inherited. **The scope entry's real
exposure is the orphaned `agy`** — still inside the unit, still matching — which makes it the entry
where reaping on the host pid alone would have cost the most, and it is also the entry that cannot
answer the second question when it is written. AG-18 publishes a scope claim *before* `agy` exists, on
purpose, so that nothing can beat it onto disk; an entry describing a container and no process has no
agent pid to record.

`AgyGateServer.claim` therefore rewrites it once, with the pid the session just spawned — the one place
that both knows the pid and runs after the child exists. Until then the entry reads as alive, which is
the same "cannot tell, so leave it standing" the conversation entries use, arrived at by design rather
than by age. Every subagent claims with the same pid, because they all run inside the session's one
`agy`, so the rewrite happens once per session and not once per delegation.

### Three things stated rather than solved

- **There is no liveness probe on Windows.** `os.kill(pid, 0)` there is `TerminateProcess` for any
  signal that is not a console event, so a probe would kill the process it asked about — worse than
  the bug. Where the question cannot be asked the answer is "alive", so that platform keeps the
  pre-2026-09-10 behaviour. Same shape as `next.md` § C8's POSIX-only teardown.
- **A recycled pid reads as alive**, so a corpse can present as an orphan until something reaps it.
  That is the direction that keeps our own tree gated. Closing it means comparing process start times,
  which is `/proc`-shaped and would put a second Linux-only mechanism in this file.
- **An entry with no `agy_pid`** — written by any earlier version, or a scope claim not yet rewritten —
  is never a corpse. The upgrade path is "keep the old behaviour", not "guess".

### What was not measured, and why

**Whether a real `agy` survives its host being SIGKILLed is still a citation, not a measurement.**
`main.py` measured the shape on the other engine — a Claude CLI reparented to init and still running 38
seconds later — and the same argument is made here for `agy`. It was not confirmed: `agy` is installed
on this machine but **not authenticated** (`Error: authentication required. Run 'agy' to log in`), so a
session never reaches its `init` frame from here and there is no child to orphan. The specific unknown
is whether `agy` exits promptly when its stdin pipe closes.

**Corrected the same day, later: this machine can run `agy` after all.** The paragraph above reads the authentication error as a standing property of the machine, and it is not one — on 1.2.0, `agy models` fetched the live model list and `agy -p` returned its answer and exited 0 from a throwaway directory. What the error *was* a property of is unknown and is not guessed at here; the binary self-updated at 17:26 local, which is a candidate and not a measurement. **The consequence is scheduling rather than correctness.** The two-pid rule still does not depend on the answer, but the distribution this section wants — how long an orphan lives, and the sibling window [AG-R-14](risks.md#ag-r-14) residue 1 wants — is no longer blocked on hardware or credentials. Nothing in that direction has been run: what changed is only that it can be.

**The two-pid rule does not depend on the answer**, which is the reason this is a residue rather than a
blocker: if `agy` exits on EOF the entry becomes a corpse and the next sweep collects it, and if it
lingers the entry stands and its calls are denied. Both branches are the intended behaviour. What an
authenticated machine would add is the *distribution* — how long an orphan lives — which is the same
measurement residue 1 wants for the race window.

### The tests

Twenty-eight offline — 24 in `tests/test_agy_gate.py`, two in `tests/test_agy_gate_server.py`, one in
`tests/test_agy_session.py`, one in `tests/test_agy_scope.py` — and the pid in them is real: a
subprocess run and reaped, so the probe is exercised rather than monkeypatched. The decision table —
corpse is not ours, orphan still denies, live host with a dead `agy` still owns it, no `agy_pid` keeps
the old behaviour — plus `owns_anything` no longer counting a corpse and still counting a half-written
entry (a claim in flight is a session starting, not a corpse), the sweep sparing another host's live
claim and an unreadable file, the same three-way table over a *scope* entry with `scope_owner` as the
observable, and the wiring: `AgyGateServer.claim` records what it is given, rewrites the scope entry
once, and `AgySession` gives it the pid it spawned.

One is about the platform rather than a behaviour: it monkeypatches `os.name` to `nt`, replaces
`os.kill` with something that raises, and asserts the answer is still "alive". A liveness check that
kills what it asks about would pass every other test in the file. 4,829 Python green.

### And the incidental finding beside it, which was not incidental

The same risk entry recorded the cgroup probe reading the installed gate as `stale` on a machine where
it worked, filed as a wrong label. It is not a label: **`AgyService.connect` returns
`gate_not_installed` on a stale state and `AgyConsultant.available` goes false**, so the whole engine
refuses to start and `second_opinion` disappears — because `.venv/bin/python3` in the file and
`.venv/bin/python` from `sys.executable` are two spellings of one interpreter.

`_same_command` compares the arguments exactly and the interpreter as a file, cutting the command on
the invocation `hook_command` itself writes rather than guessing at word boundaries. **The near-miss is
the part worth keeping:** the obvious fix is to resolve both paths and compare targets, and that is
wrong. A venv's `bin/python` is usually a symlink to the system interpreter, so `.venv-a/bin/python`
and `.venv-b/bin/python` resolve to the same file while selecting different environments — a virtualenv
is chosen by the path used to invoke it, `pyvenv.cfg` beside that path. Resolving would have reported
another checkout's gate as ours, which is the failure this whole module exists to prevent, arrived at
by fixing a cosmetic bug. The test is *same directory, same file within it*.

Measured with the pair that caused it, on this machine: `.venv/bin/python3` and `.venv/bin/python` read
`current`, `/usr/bin/python3` still reads `stale`. Eight tests, including the two-venvs-one-binary case
and an unbalanced quote in a hand-edited entry reading `stale` rather than raising.
---

## The round trip, watched — and what the browser and the kernel each corrected (2026-09-10, later)

Two things this directory was carrying as owed, taken in one sitting because both need a live `agy`
and the day's other work had just established that this machine has one. The first was named in the
entry above it: *"nobody has opened the tab in a browser on a live `agy` turn."* The second was
[§ What was not measured, and why](#what-was-not-measured-and-why) — whether a real `agy` outlives a
SIGKILLed host, which had been argued from the other engine rather than measured here.

**Both are now measured, and neither came back the way it was written.** The tab works end to end and
the round trip is watched. The orphan exists — and lives for **under a second**, where the citation
it was borrowed from was 38.

### The tab, in a browser, on a live turn

`scripts/probe_agy_subagent_tab.py`, ten checks, all passing on one delegation against the paid
subscription. It starts a real server on a throwaway repository inside a trusted workspace, switches
the engine through `switch_engine` the way the notice does, sends a prompt asking for a delegation,
answers the dialogs it raises, and then **clicks the subagent's tab** and reads what arrives.

The chain it closes is the one the entry above could not: the session id the browser holds being the
conversation id `agy` names its own store by. That was followed link by link off disk and is now
followed through the wire — the tab drew **three tool cards** (`view_file`, `view_file`,
`send_message`), each marked *gated*, over the subagent's own prompt and closing sentence, from a
store the browser never touches. Screenshot in `.aic-dc/live-probe/agy-subagent-tab.png`.

Two of the checks are the guards `bb9846a1` shipped, and this is the first time either has been seen
rendering rather than asserted:

- The descriptor **as the browser reads it** answers `subagent_transcripts: supported` and
  `subagent_stop: unbuilt` on this engine, read over the same RPC `engine-capabilities.js` uses.
- The row in Main carries **no Stop button**. `agy` has no halt frame, so a button there would send a
  call the router refuses.

**The negative control is the containment gate, over the wire.** A real conversation on this machine
that this session never announced is refused — *"01dab20c… is not a subagent of this conversation, so
there is no transcript here to read"* — while the session's own subagent answers two messages, from
the same browser, one line apart. In-process that pair was shown on the day the reader was built;
what it had not been shown through is the RPC, which is the only route a request shaped like an
attack would take.

**What it does not cover.** Headless Chrome, so this is layout and script rather than a human's
screen; one turn, one subagent, one delegation depth. The `⏹` half stays unbuilt and unmeasured, and
a *nested* delegation — a subagent's own subagent — was not asked for.

### The orphan is real, and it is sub-second

`scripts/probe_agy_orphan.py` starts a host in a child process, brings a real `AgySession` to its
`init` frame, SIGKILLs the host, and watches. Three runs, plus a fourth scenario that isolates the
mechanism. It takes no model turn: it reaches `init` and stops.

| What was asked | What was measured |
|---|---|
| Is the registry's `agy_pid` really `agy`'s? | **Yes.** `comm` is `agy` and the cgroup is `/user.slice/…/app.slice/aic-dc-<hex>.scope`, so `systemd-run --user --scope` **execs** rather than staying as a parent: the pid is preserved and the scope really applied. The two-pid rule is about `agy` because that pid *is* `agy`. |
| Does `agy` survive its host being SIGKILLed? | **Yes**, all three runs. Reparented to the user's `systemd` at ppid 6583, still alive 200 ms later, with the registry reading the entry live — which is the orphan the rule exists to keep denying. |
| For how long? | **0.60 s, 0.80 s, 0.80 s.** |
| Why does it go? | **Its stdin closes.** With the host *alive*, closing the pipe alone ended `agy` in **0.30 s** — so the kill case is the same path plus the time to notice. |
| Does the decision table hold on real dead pids? | **Yes.** live → orphan → corpse, and `reap_stale` then collected both the conversation entry and its scope entry, in each run. |

**The citation it replaces was two orders of magnitude out.** `main.py` measured a Claude CLI
reparented to init and still running 38 seconds later, and the same argument was made here. The
*direction* transfers — our child does outlive a killed parent — and the *number* does not, because
these are different programs with different reactions to EOF. An `agy` orphan is a sub-second window,
not a condition a user would ever sit in.

That is worth stating precisely, because it is the answer to "how long does a killed server leave a
stale gate behind": **on Linux with a scope, under a second**, and then the next sweep collects it.

### The finding underneath it, which is the same defect this rule was written to end

Reading the registry on this machine after the runs: **eight entries, every one naming a host that is
dead, every one reading `live`, and `owns_anything()` permanently `True`.**

They are all pre-`f598638e` entries, so none carries an `agy_pid`, and
[§ The pid that was written and never read](#the-pid-that-was-written-and-never-read-2026-09-10)
states the rule that keeps them: *"An entry with no `agy_pid` … is never a corpse. The upgrade path is
'keep the old behaviour', not 'guess'."* `process_alive(None)` answers `True` because the question
cannot be asked, so `entry_is_live` is `True` for ever.

**That is the intended behaviour producing exactly the defect the commit was written to fix.** Its own
account of the cost: *"One unclean exit of ours therefore made 'we own something' permanently true,
and from then on every unparseable payload from the user's own `agy` was denied."* The fix ended that
for entries written after it and grandfathered the entire population that already had it — which, on
the machine this was developed on, is all of them.

**And today's measurement retires the caution that made the grandfather clause look safe.** It was
written not to guess about an `agy` that might still be running under a host we can no longer ask
about. That agent's lifetime is now known: it is under a second past its host. An entry whose host is
gone, and which was written by a version that predates this file, describes a process that ended long
ago by any measure this system can take. Recorded rather than fixed here, with the recommendation, in
[`risks.md` AG-R-14](risks.md#ag-r-14) — the change touches what the gate answers, which is not a
thing to slip into a verification sitting.

### Two more, found by driving the app rather than by reading it

**`schedule` and `send_message` raise a permission dialog on an ordinary `agy` turn.** Neither is in
`agy/tools.py`'s `TOOL_CLASSES`, so `GATED_BY_DEFAULT.get(None, True)` gates them, and the user meets
a modal for a planning step that touches nothing and for a subagent reporting back to its parent.
This is phase 4's second defect — *"every read-only call raises a modal"* — arriving again through the
half of its fix that is a table: `pre_verdict` consults a classification, and a classification only
covers the names somebody wrote down. `agy` is at **1.2.0** here and the table was built against
1.1.2x, which is [AG-R-2](risks.md#ag-r-2) in the transport rather than in the wheel.

**The usage HUD titles an Antigravity session *"Claude Code"*.** `usage-hud.js::_modelLabel` falls
back to that literal string when there are no turn models and no context model, and on this engine
there are neither — both are `absent` in the descriptor, by decision. So every Antigravity session
names the wrong product in its own HUD. It is not [AG-R-4](risks.md#ag-r-4) — nothing branches on an
engine name — it is the shape underneath it: a default written when there was one engine, which now
asserts which engine is answering. Visible in one frame of the screenshot above, invisible to 4,829
green tests, and pinned by one of them (`usage-hud.test.js:840`).

### The two instrument defects the sitting cost, and both are the same lesson

**The deny button does not deny.** `answer_permission` clicked `button[data-decision="deny"]` and
moved on; that button calls `_openDeny`, which opens the reason row, and the request is only answered
by *Send denial* beside the field. So the first run clicked Deny, saw the same request still on
screen, and clicked again — **26,417 times**, which read in the log exactly like an agent retrying a
refused tool in a loop. It was not the agent. The harness now performs both halves of the gesture and
then **waits for the request's own `permission_id` to leave the dialog**, so "the click did nothing"
is a named failure rather than a spin.

**A zombie reads as alive, and it inverted the first orphan reading.** The probe SIGKILLed a host it
had spawned and never called `wait` on it, so the host stayed in `/proc` as a zombie; `os.kill(pid, 0)`
succeeds on one. The registry then reported a live claim over two dead processes, and — worse — the
30-second wait for the host to disappear ran to its ceiling, so the sample labelled *"200 ms after the
kill"* was taken **thirty seconds** after it and reported `agy` as already gone. Both conclusions were
wrong and the second was wrong in the interesting direction: it said there is no orphan, which is what
the sitting was there to find out. Reaping the host inverts it, and the numbers above are from the
reaped runs. `registry.process_alive` documents the zombie case and calls it a delay that "never
inverts" a reap — true of that function, and not true of a caller that waits on the same pid.

Both are the rule this directory keeps relearning: **assert on the thing, not on the gesture.**
AG-R-11's *"assert on the file, not on the hook having fired"* is the same sentence about a different
noun, and it is now recorded three times because it has been learned three times.
---

## Three findings, closed — and the one that was wrong before it shipped (2026-09-10, latest)

The entry above found three things and fixed none of them, deliberately: a verification sitting that
starts changing what the gate answers stops being one. Taken here in the order of what they cost a
user.

### 1. The registry's grandfather clause, closed by the measurement that made it unnecessary

`entry_is_live` now asks **which kind of entry** it is holding when there is no `agy_pid` to ask
about, because the two kinds get there for opposite reasons:

- A **conversation** entry is written when the `init` frame names the conversation, by which time the
  host has a child to name. So a missing `agy_pid` *dates* the file rather than describing it. With
  the host also gone it is a corpse — the agent it would have named outlives its host by under a
  second, and these files are hours old.
- A **scope** entry is written *before* `agy` exists, on purpose ([AG-18](decisions.md#ag-18)). An
  absent pid there is the normal state of a session that is **starting**, and reaping it would release
  the unit an orphaned agent is still sitting in under `--dangerously-skip-permissions`. It keeps the
  old answer.

That asymmetry is the whole change, and it is why "age" is an argument that must not be generalised:
it is available for the entry kind that cannot legitimately lack a pid, and unavailable for the one
that can.

**Measured on the machine it was found on.** The eight entries that read `live` for ever now read as
corpses, and `owns_anything()` is `False` — the tripwire [AG-R-14](risks.md#ag-r-14) states. They will
be swept at the next `AgyGateServer.start`, beside the stale-socket unlink that has the same cause.

Three tests replace the one that pinned the old behaviour, and they are the decision table rather than
the change: a legacy conversation entry with a dead host is reaped, one whose host is **alive** is
untouched (age is only ever the second question), and a scope entry with no `agy_pid` is never a
corpse.

### 2. The tool table was four releases behind the binary, and nobody could have known

`agy` 1.2.0 advertises **57 tools**. `agy/tools.py` classified **14** of them. The rest fell to
`GATED_BY_DEFAULT.get(None, True)`, which is why nothing was ungated — but the seam that is supposed
to enumerate *what can change the tree* did not know most of the ways to.

**`sed_file` is the one to read twice.** [AG-R-11](risks.md#ag-r-11) exists because an agent refused
an `edit_file` went after the same change with `sed -i` through `run_command`, unprompted, on both
probe runs. On 1.2.0 that is a **first-class tool**. It still asked — everything unknown asks — but it
asked as an unclassified call: no diff, no path chip, no `acceptEdits` reasoning, and no shape for the
`always allow` control to take. Ten names went into the write seam, including three more spellings of
spawning (`define_subagent`, `manage_subagents`, `browser_subagent`) on AG-5's own reasoning that a
child inherits the tool set, and `call_mcp_tool`, which is arbitrary tool use by proxy.

**Nothing was classified in the other direction, and that is the decision rather than the omission.**
The `init` frame carries bare names — no descriptions, no schemas — so the only evidence available is
the spelling. `read` is the single class that *removes* a dialog, and this directory does not spend
that on a guess. The remaining **33** are declared in `SEEN_UNCLASSIFIED`, which changes nothing about
how a call is treated and everything about whether the next reader can tell "seen and left" from "never
looked". `derive_rules` returns nothing for an unrecognised class, so a user cannot grant one of them a
standing permission either — checked rather than assumed, because an over-broad always-allow on
`sed_file` would have been the real hole.

**`scripts/probe_agy_tool_inventory.py` is the tripwire [AG-R-2](risks.md#ag-r-2) has always specified
and never had here.** That risk's mitigation is a probe whose `unclassified` bucket is empty *by
declaration*; one exists for the wheel and the binary — the transport that reaches the paid
subscription — had none. It reads the inventory off the `init` frame, so it costs no model turn, and
it reports a second bucket the wheel's probe does not need: names in our table that the binary no
longer offers. Today that is `codebase_search`, one name, left rather than pruned on the reasoning that
gating a tool nobody has costs nothing.

**What is still open, and it is the half a user feels.** Thirty-three tools raise a dialog that says
nothing — `wait`, `schedule`, `command_status`. That is phase 4's *"every read-only call raises a
modal"* for the names its fix could not know, and closing it needs the binary's own account of what
each tool does, which is not on this channel. Dialog fatigue is not cosmetic on a transport whose only
containment is a human reading dialogs.

### 3. The HUD named the wrong product

`usage-hud.js::_modelLabel` fell back to the literal `'Claude Code'`, written when there was one
engine and the only way to have no model was a HUD that had not loaded yet. This engine reports
*neither* source — per-model usage and the context read-back are both `absent` by decision — so every
Antigravity session carried the wrong vendor's name in the one place a user looks to see what is
answering. The label is now empty and the tooltip says why. **The engine's name is not the fix**:
`get_engine_capabilities` deliberately carries no engine identity ([AG-R-4](risks.md#ag-r-4)), and the
panel that does name the engine is already on screen saying so.

### The correction, which arrived from a test rather than from a reviewer

The first cut of (2) declared `finish` unclassified and said in as many words that a user meets a modal
for it. A new invariant test — *every declared name is unclassified in the table that decides* —
refuted both halves at once: `finish` is `read` in the **SDK** half of
`antigravity/permissions.py::TOOL_CLASSES`, which folds the two vocabularies into the table the gate
actually reads. The probe had been asking `agy/tools.py` alone, so it reported a name as unclassified
that has never reached a dialog.

The number moved with it — 43 unclassified became 33 declared plus ten classified, against a merged
table of 31 rather than an `agy` table of 24 — and the shape of the mistake is the one worth keeping:
**a probe that asks a narrower question than the code does will report differences that are not there,
and the false positives are the polite failure mode.** The same asymmetry could have hidden a real one,
had a name been classified in only the `agy` table while the merged read happened elsewhere.

4,840 Python and 4,485 webapp tests green.
---

## ⏹ on one subagent: the mechanism works and the handle arrives too late (2026-09-10)

`subagent_stop` was the last unbuilt surface of the subagent trio, and the only one whose design was
already written out — `capabilities.py` said what to build, named two limits in advance, and named one
unmeasured thing. It was built. It is still UNBUILT, **for a reason the row did not have**, and
finding that out is the entry.

### What was built, and it holds

`AgyGateServer.refuse_conversation` aims the starvation the gate already performs at a single
conversation id. That aiming is possible because of the fact [AG-R-14](risks.md#ag-r-14) was raised
*about*: a subagent runs in a conversation of its own, and the hook payload names it on every call.
`AgyService.stop_task` drives it, checks the id against what the turn announced — the same containment
`agy/subagents.py` states for the reading half of the same identifier — and answers `stopping`, never
`stopped`, because the row must stay live until the stream says otherwise.

`scripts/probe_agy_subagent_stop.py` measured it on a live turn, and the instrument is the asymmetry:
**the stopped subagent's later calls were all denied, and the parent's still reached the dialog.**
Same turn, same gate, two conversations, opposite outcomes — which is the whole difference from
`cancel_streaming`, and the difference that kept this surface unbuilt while the only mechanism was
turn-wide.

**The question the row ended on is answered, and the answer was the awkward one.** It asked whether
`agy` reports a starved subagent `CANCELED` — the state that maps to `stopped` and an amber LED. It
does not: it reports **`DONE`**. Defensible from the harness's side, since the agent did read the
refusal and wind down, and wrong on a row, because `DONE` maps to `completed` and puts green over work
a user stopped. So the terminal word is this host's (`AgyTranslator.mark_stopped`): we know a human
pressed the button, which the stream does not report. Narrow on purpose — it changes the word, never
the terminality, and only for an id `stop_task` recorded.

### What cannot be built, and it is in the wire

The browser probe found nothing to press. `scripts/probe_agy_subagent_stop_ui.py` drove a fourteen-file
delegation with a browser attached and **no live subagent row was ever on screen**: one
`subagentEvent` arrived, already terminal.

Reading the frames says why, and it is not a timing accident:

| Frame | State | Carries |
|---|---|---|
| `tool` / `invoke_subagent` | **ACTIVE** | `Subagents: [{Model, Prompt, Role, TypeName, Workspace}]` — **no conversation id** |
| `subagent` / `invoke_subagent` | **DONE** | `subagent_info.subagents[]` with `conversation_id`, `log_uri` |

`agy` emits the `subagent` step **once, and at `DONE`**. The identity a Stop button needs therefore
arrives **with the frame that ends the row**, and `block-render.js` draws Stop only on
`live && task_id && supports(...)`. There is no window.

**This is AG-R-14 residue 1 in a second place.** That residue is the spawn-to-announce race — a child
whose first call can beat the claim onto disk, because *"`invoke_subagent`'s arguments carry only
`Prompt`, `Role`, `TypeName`, `Model` and `Workspace` — no conversation id, because the child does not
exist when the dialog for the spawn is answered."* The same sentence, read a second time, is also the
answer to why a live row cannot carry a handle: the id does not exist yet, so nothing can be keyed on
it. AG-18 closed the *gating* consequence of that by asking the kernel instead. There is no equivalent
move here, because a **button** needs a name the user's click can carry, and the kernel does not have
one either.

So the gate half stays built, tested and unreachable, and the surface stays `UNBUILT` with its reason
replaced: **it waits on an identity, not on a mechanism.**

### The correction this forced next door

`subagent_rows` is titled *"Subagent rows and their own tabs, **live**"* and is SUPPORTED on `agy`.
The word `live` overstates it, and the measurement above is what shows that: the row arrives already
terminal, so it never spins. Worse in the other direction — the child can go on making tool calls
*after* that `DONE` frame; two did, in the run that measured the stop. So the row reports an end the
subagent has not reached.

Left SUPPORTED rather than downgraded, because the row and its tab are real and carry the subagent's
whole transcript — what is absent is the liveness, and it is now stated in the descriptor rather than
implied by the title. **A capability's title is a claim, and this one had been making a claim nobody
had checked.**

### What the sitting cost, and what it bought

Two probes, three tests files' worth of new coverage (an aimed refusal at the gate, the stopped word
at the pump, `stop_task`'s containment and localhost rule), and a capability that ends the day exactly
where it started — with a different reason and two measurements behind it. The alternative was
shipping a button that could never appear, or shipping the descriptor's guess that `CANCELED` was
coming.

One test earned its place immediately: `test_a_remote_client_cannot_stop_anything` was first written
patching `_localhost_available`, which is the *gate's* deadline question, and passed the call straight
through while asserting it had been stopped. `_check_localhost_only` reads `_collab`. A test that
patches the wrong seam is a test that reports the code it did not exercise.

4,857 Python and 4,485 webapp tests green.

---

## Asking `agy` how to drive `agy`: three consultations, eleven probes, one rejected proposal (2026-09-10, latest)

The question was open-ended — *is there a better way to operate this transport, perhaps SDK-style?* —
and the method is the finding as much as the answers are. Each round put the current design to
Antigravity over the paid transport, took its objections seriously, **ran the probes it asked for**,
and brought the measurements back. It was wrong often enough that this was not ceremony.

### What it got wrong, and why that matters

Round one advised migrating to `agy models --json`, which does not exist; a parallel instance with
tool access claimed to have read `sdk.md` and `cli.md` from the shipped docs, of which there are six
and neither is among them. Round two said `--continue` is machine-global and would bind to whatever
the user last ran — it is workspace-scoped, measured. It said conversation forking is an internal
primitive with no user-facing command — `/fork` is a real slash command, refused on this transport
with a purpose-written error. Round three called transcript corruption the most severe risk of the
proposed stop, and the measured lifecycle order refutes it.

**None of that made the exercise a waste, and the reason is worth stating.** Every wrong answer was
wrong in a *checkable* way, so the consultation's value was in generating claims worth probing rather
than in being right. Three of its objections survived and were adopted; see [AG-19](decisions.md#ag-19).

### What the probes found

The single most useful discovery was not an answer but a file: **`agy` ships its own hook
documentation**, extracted at `~/.gemini/antigravity-cli/builtin/skills/agy-customizations/docs/`,
and `hooks.md` documents five lifecycle events where this app wires one. Everything
[`sdk-surface.md`](sdk-surface.md) had recorded about hooks was measured by probing; it did not have
to be. That file made the stop of [AG-19](decisions.md#ag-19) findable, and the rest of the round is
in [`sdk-surface.md` § The hook contract is shipped](sdk-surface.md#the-hook-contract-is-shipped-not-inferred--read-2026-09-10).

### The proposal that was right to reject

Consultation converged on moving the permission gate into an app-owned `--gemini_dir`, and the
measurements behind it were sound: the flag works, authentication survives an empty config directory
even with the session bus cleared, workspace trust is one list of paths, and workspace-local hooks
still do not load headlessly at 1.2.0. **The conclusion was still wrong**, and the record is what
caught it: [AG-18](decisions.md#ag-18)'s table had already rejected a private config root, hours
earlier the same day, for a reason neither side of the consultation had in view — the flag travels by
argv, and the processes that escape it are exactly the ones [AG-R-14](risks.md#ag-r-14) exists for.

The lesson is not that consultation is unreliable. It is that **a consultation given a stale
description of the system will converge confidently on a regression**: the design was described to it
as isolating by `conversationId`, which AG-18 had amended the same day. Where the record was current,
the review sharpened it; where the description was stale, the review reproduced the staleness and
added conviction to it.


---

## Phase 12 — the stop becomes a mechanism, and the control is the instrument (2026-09-11)

[AG-19](decisions.md#ag-19) was specified on 2026-09-10 from eleven probes and three consultations,
and none of it was written. This is the writing of it, plus [AG-R-16](risks.md#ag-r-16)'s `Stop`
handler, which the same registration pays for.

### What shipped

One hooks entry now holds three handlers where it held one. The gate is unchanged, byte for byte —
deliberately, so an install written before today reads as *this* interpreter and the thing wrong with
it is the handlers it lacks rather than the one it has.

| Piece | Where |
|---|---|
| `PostInvocation` → `terminate` while ⏹ is latched | `hook.decide_invocation`, `AgyGateServer.decide_invocation` |
| `Stop` that always permits the stop | `hook.report_stop`, `AgyGateServer.note_stop` |
| The event argument, and the frozen build's translation of it | `hook.parse_argv`, `--agy-hook-event` in `cli.py` |
| Three registrations, three probes, two shapes | `install.hook_entry`, `install.hook_commands` |
| A stopped turn's footer carrying `cancelled` | `AgyTranslator.note_cancelled`, `AgySession.stream_turn` |

**The three events fail in opposite directions and it is written down rather than left to be
noticed.** The gate must never print `{}` — that is the one shape `agy` reads as *allow*. The two
invocation events must print `{}` on every failure, because ending a stranger's loop because this
host was unreachable is the worse error, and the tree is protected by the gate either way. The
`Stop` handler's `{}` is a literal rather than anything derived from the socket, so no host bug can
reach the `"continue"` that would revive a stopped turn.

**The event is stamped from argv, never read off the payload.** The host answers a different shape
per event, and `{}` is right for an invocation hook and *allow* on a tool call — so a payload able to
relabel itself could ask for a tool call to be answered in the shape that waves it through. `main`
learns the event from the command `install` wrote, and overwrites whatever the payload claimed. An
unrecognised event is the gate, which is the fail-closed direction rather than a tidy default.

### The measurement, and why the control is the whole of it

`scripts/probe_agy_stop_terminates.py`, two turns on the subscription, `agy` run against an isolated
`--gemini_dir` so nothing of the user's was read or written.

**A stopped turn ending proves nothing** — a cooperative agent ends a stopped turn too, which is
exactly what starvation relied on. So the same prompt is stopped at the same point on the same gate,
and the only difference is what the host answers at `PostInvocation`:

| | Invocations | Tool calls | `terminationReason` | Footer |
|---|---|---|---|---|
| **control** — answers `{}` | **3** | `list_dir` allow, `view_file` **deny** | `NO_TOOL_CALL` | `cancelled: true` |
| **armed** — answers `terminate` | **1** | `list_dir` allow | **`TERMINAL_CUSTOM_HOOK`** | `cancelled: true` |

Eight checks, all held. The armed run ends in one invocation where the control needs three; the
control never terminates, so it is a control; every call after the stop is denied on *both* runs, so
the termination is a second layer rather than a replacement; and the two runs are told apart by the
`terminationReason` [AG-R-16](risks.md#ag-r-16) turns on. The probe exits **2** rather than 0 when the
control also ends in one invocation — a prompt that cannot tell a termination from a cooperative
agent has measured nothing, and reporting that as a pass is the failure mode the control exists to
prevent.

**The probe failed its own control first, and the bug is worth keeping.** The recorder called
`super().decide_invocation` on the control run and then discarded the answer, which left the server's
`was_terminated` record saying the control had ended a loop it never told `agy` to end. The
instrument lied in the same direction the feature would have, and the check that caught it was the
one asserting the control *is* a control.

### An existing claim this falsified

`workspacePaths` **is populated**, on all three event types. [`sdk-surface.md`](sdk-surface.md)
§ *Two limits that remain* recorded it empty in every captured payload and reasoned from that to
`conversationId` as the sound isolation key. Measured today at `["/tmp/agyhooktest"]` on `PreToolUse`,
`PostInvocation` and `Stop` alike.

The likely cause is that the captures predate `ae23f0bd`, which added `--add-dir` to the session's
argv — the field is the workspace, and until then no workspace was named. **This changes no
decision**: routing on it would be worse, not better, since a stranger's own `agy` session in the
same repository would match a path where it cannot match a conversation id or a cgroup
([AG-R-14](risks.md#ag-r-14)). It is corrected because a false claim in the record is what a later
reader would reason from.

### What is still unbuilt on this transport

Three of the six items listed on 2026-09-10 are done. The remainder are unchanged and are still in
[`README.md`](README.md) § *Unbuilt on the `agy` transport*: skipping usage absorption on a
non-`SUCCESS` result, standing guidance via `PreInvocation` `ephemeralMessage`, and reading
`transcriptPath` off the payload instead of deriving it. The last of those is now measured rather
than documented — the payloads captured today carry it, on every event.

---

## Two numbers the footer was reading wrong, and one it was not reading at all (2026-09-11)

Two items, taken together because they are the same file and the same kind of defect: a figure the
user reads as true that is not.

### A refused turn was billed for the turn before it

[`sdk-surface.md` § A failed turn reports the previous turn's usage](sdk-surface.md#a-failed-turn-reports-the-previous-turns-usage--measured-2026-09-10)
measured it on 2026-09-10: `agy`'s `result` frame for a turn it refused **echoes the previous turn's
`usage` and `duration_seconds` verbatim**, with one output token. `_absorb_usage` takes last-wins
rather than summing, so nothing was ever doubled — which is exactly why it survived a year of
attention. It is a misreport, not a multiplication, and a cost figure wrong in a way nothing in the
UI can distinguish from right is [AG-R-6](risks.md#ag-r-6)'s family.

One condition, in `_absorb_result`. Two details in it are decisions rather than mechanics:

- **A result naming no status is still absorbed.** A frame that named nothing is not a frame that
  named a failure, and dropping its usage would lose a real measurement to a defensive default.
- **Only the *result* frame's usage is dropped.** The turn's own step frames stand, so a turn that
  did work before failing keeps the tokens it spent. Reporting zero for it would be the same class
  of untruth pointed the other way.

### Three field names the browser was never reading

Found while checking the first fix: **both** Antigravity pumps spelled three `streamComplete` fields
differently from the Claude pump, and the browser reads the Claude spelling.

| Pumps said | Browser reads | What was missing |
|---|---|---|
| `num_tool_calls` | `tool_calls` | The turn footer's *"N tool calls"*, and the HUD's turn row |
| *(absent)* | `permission_prompts` | *"M asked"* beside it |
| `response_text` | `response` | Every settled assistant message's `content` |

**`permission_prompts` is the one worth pausing on.** The shared `stats` object exists *because*
`AntigravityService._note_permission_prompt` reaches into it to attribute a dialog to the turn that
caused it — a defect fixed once already, when the attribute was missing and every dialog raised a
swallowed `AttributeError`. The count was fixed, and then never put on the wire. It has been
collected and discarded ever since.

**None of this failed.** Every consumer guards its read, so a missing key renders as an absent stat
or an empty string rather than as an error. `TestVocabulary` — the AG-R-4 tripwire — passed
throughout, because it checks event *names* and this drifted one level down, in the fields. That gap
is now [AG-R-17](risks.md#ag-r-17), with a tripwire asserting every key on either Antigravity footer
is one the Claude pump emits, and a `THEIRS_ALONE` list where a divergence must carry the reason it
is allowed.

### The one that was not fixed, and why it is not an oversight

`stop_reason` is still `stop_reason`, where the Claude pump says `terminal_reason`. It is on the
allowlist rather than renamed because `computeTurnOutcome` turns any `terminal_reason` that is
neither empty nor `completed` into a **red LED** — so renaming would change what colour a failed
`agy` turn draws, and `agy`'s status words (`ERROR`, `CANCELED`) are not that vocabulary. It is a
mapping to be designed, not a spelling to be corrected.

Until it is, `AgyTranslator.stream_complete`'s docstring is describing a consumer the key does not
reach: it says the browser reads an unrecognised reason as something worth a red badge, which is true
of `terminal_reason` and true of nothing this field is connected to. Left in place, named here, so
that whoever designs the mapping finds the claim rather than trusting it.

> **Designed and shipped the next day** — see § *The verdict that never reached the browser*, below.
> The deferral was right and the framing was not: this reads as a question about *colour*, and the
> colour a failed `agy` turn actually drew was green. Neither Antigravity footer carries `is_error`,
> so this key was the only route either had to a red LED.

**4,950 Python tests and 4,485 webapp tests green.** The webapp suite is not decoration here: these
were changes to what the server sends the browser, and the fixtures on that side already used the
Claude spelling — which is the other half of why the divergence was invisible.

---

## The tool cards that said "No output.", and a mitigation that did not mitigate (2026-09-12)

Two pieces of work, and each is the same lesson from a different side: **a claim believed on the
strength of a document, until something measured it.**

### The fourth field-name divergence, found by widening yesterday's tripwire

[AG-R-17](risks.md#ag-r-17) was raised on 2026-09-11 for three `streamComplete` fields the Antigravity
pumps spelled their own way. The tripwire written with it covered that one event, because that is
where the three were found. Widening it to every shared payload — an AST walk over the three pumps'
`Event(name, …)` calls, compared against the Claude pump's — found a fourth, and a worse one:

```
agy/steps.py      sends  "content": <the tool's output>
block-render.js   reads   result.preview,  default '',  empty state "No output."
blocks.js         applyToolResult: block.result = { ...payload }   ← no mapping
```

**Every tool card on the `agy` transport drew the literal string "No output."** over a payload
carrying the output the whole time. The pump also sent no `truncated` or `full_bytes`, so the
truncation marker never appeared either. Fixed by emitting the three fields the card reads, through
`truncate_tool_result` — the Claude pump's own helper, shared rather than re-derived, because two
answers to *"how much of a tool result does a card show"* would show a user different amounts on
different engines for the same output.

**The sweep's other half is what it ruled out.** `toolUse`, `systemEvent`, `subagentEvent` and
`turnUsage` are clean across all three pumps. `streamChunk` cannot be compared — the Claude pump
builds its two chunk names in a conditional and passes the result as a variable, so there is no
literal to compare against, the same gap `TestVocabulary` records for the event *names*. The tripwire
says so in its docstring rather than implying coverage it does not have, and a third test asserts the
comparison is still resolving the events it claims to, so a refactor cannot shrink it into passing
vacuously.

A webapp test now starts from the payload the server emits and goes through `applyToolResult` into
the renderer, which is the path the browser actually takes — every other tool-card test hands the
renderer a hand-written `result`, which is how the divergence survived on both transports.

### AG-R-16's mitigation, refuted the day after it shipped

[AG-R-16](risks.md#ag-r-16) said: register a `Stop` handler returning `{}` so at least one voice in
the merged hooks file is always for stopping — *"whether that wins against a concurrent `continue` is
unverified and worth a probe before relying on it."* It shipped on 2026-09-11 with
[AG-19](decisions.md#ag-19). Relying on it is exactly what shipping it did.

`scripts/probe_agy_stop_merge.py`, 2026-09-12: **it loses, both ways round.** A rival `continue` holds
the turn open with ours in the merge whichever order the keys are in — 18–20 seconds against a 1.6
second turn — and when the rival runs first, **this app's handler is not run at all**. A `continue`
short-circuits the handlers after it. Being registered is not the same as being asked.

What survives is the second mitigation, which was already the behaviour and had never been asserted:
the gate's refusal outlives the turn it stopped, because `resume` runs when a new turn *starts*. A
revived loop reaches the working tree through a gate still saying no, which is why the risk stays
moderate. It now has a test.

### What the probe cost to get right, and why that is in the record

Three instrument defects, in order:

1. The rival's answer was built into a shell command, where a backslash inside single quotes is
   literal — so it printed `{\"decision\":…}` and `agy` read no decision from it. **The run reported
   that `continue` does nothing, about a hook that had never said `continue`.**
2. An unhandled `TimeoutExpired` killed the run that first reproduced the harm — the timeout *was*
   the finding.
3. `status` was read as the signal. A held-open turn returns `status: "SUCCESS"` with partial output
   once `--print-timeout` expires, so two runs disagreed with each other for a reason that was in
   neither of them.

The probe now **self-tests its own stimulus** before trusting anything downstream — it runs the rival
hook's command and checks the JSON it prints is the JSON intended. That check costs nothing and would
have caught the first defect immediately. It is the same shape as the control in
[§ Phase 12](delivery.md#phase-12--the-stop-becomes-a-mechanism-and-the-control-is-the-instrument-2026-09-11):
an instrument that cannot show its own input arriving is measuring the input, not the system.

**4,959 Python tests and 4,489 webapp tests green.**

---

## ⏹ against a hostile hook: the answer, and it is not the one we shipped for (2026-09-12, later)

`probe_agy_stop_merge.py` closed one question that morning and opened a sharper one:
[AG-19](decisions.md#ag-19)'s stop is a **`PostInvocation`** handler, a different event from the
`Stop` a rival answers, so the two might plausibly ping-pong. That was left recorded as untested.

It stopped being academic on a second reading of `session.py`: `--print-timeout` is **12h** and
nothing else in this app bounds a turn's wall clock. So the untested interaction was the whole
question of whether a user with a third-party `Stop` hook can have ⏹ ignored for twelve hours.

### Measured

`scripts/probe_agy_terminate_vs_continue.py`, four runs, both controls holding — `terminate` alone
ends the loop in **1** invocation at 2.2s, `continue` alone holds the turn open at 18.9s:

| Contested run | Held open | Invocations |
|---|---|---|
| ours first, rival second | yes, 16.2s | **8** |
| rival first, ours second | yes, 19.1s | **8** |

`invocationNum` 0→7, each ended by us, each revived by the rival, all eight `Stop` payloads reading
`TERMINAL_CUSTOM_HOOK`. **`continue` wins**, and the two mechanisms together are worse than either
alone: unopposed `continue` makes a turn *hang*; `continue` against our `terminate` makes it *cycle*,
at roughly two model invocations a second, spending on every one.

**What we shipped for the stop makes this scenario cost more.** That is worth stating plainly rather
than filed under a risk, and it is not an argument against AG-19 — unopposed, the terminate is the
single-invocation stop the phase was built for. It is an argument that the app has no floor under a
turn.

### What was done about it, and what was deliberately not

`AgyGateServer.decide_invocation` now counts its terminations per conversation and warns on the
second: *"the loop was revived after AIC-DC ended it, so the user's stop is being overridden."* Once
per turn, not once per cycle — eight cycles is one fact — and it keeps answering `terminate` every
time, because answering anything else hands the revived loop exactly what it wants.

**It does not act, and that is the deliberate half.** The two remedies are ending the process, which
AG-19 demotes to an explicit user escalation because it ends a session the user is holding, and
bounding a turn's wall clock, which is a design decision with a real trade-off: the 12h exists so a
permission dialog can outlast a human reading a diff. Neither belongs to a gate server. What this
class owed was to stop the condition being invisible, and that is what it now does.

[AG-R-16](risks.md#ag-r-16) is raised to **high**, with the ceiling recorded as the open design
question. AG-19 had already named the shape of the answer — *"offer a separate force-reset for a
genuinely runaway generation"* — for the unrelated reason that prose inside one invocation cannot be
interrupted. This is a second, independent argument for the same control.

**4,964 Python tests green.**

---

## The stop that did not land, made visible and actionable (2026-09-12, latest)

[AG-R-16](risks.md#ag-r-16) went to **high** earlier the same day: ⏹ on the `agy` transport can be
outlasted, and with `--print-timeout 12h` and no other ceiling, outlasted for a very long time. The
question left open was whether to bound a turn. **The answer is no, and the reason is the one
[AG-19](decisions.md#ag-19) already gave for not killing the process automatically** — a cap that
fires on its own eventually fires on a legitimate turn sitting in a permission dialog, and the 12h
exists so a dialog can outlast a human reading a diff.

So the user is told, and the escalation is theirs.

### One condition, two causes, one report

From where the user sits there is no difference between the two ways ⏹ fails: they pressed stop and
the turn is still going. So `AgySession` reports *that*, once per turn, and names the cause only
where it can tell them apart:

| | What is happening | Offered |
|---|---|---|
| `revived: true` | A `Stop` hook this app does not own is putting the loop back each time the gate ends it — **spending on every cycle** | **Force restart the engine** |
| `revived: false` | The turn is writing prose, which asks permission for nothing and cannot be starved (AG-19's residual gap) | nothing; it finishes on its own |

The second row is the one worth defending. It would have been easy to offer the button on both and
call it consistent — but restarting ends a session the user is holding, to save a few seconds of text
from a turn that holds no locks, runs no commands and touches no files. Offering it there would push
a bad trade at someone who is already frustrated.

`STOP_OVERDUE_SECONDS` is ten, and the number is measured rather than picked: an armed stop ends the
loop in **one** invocation at 2.2s, and a revived loop cycles at roughly two invocations a second. A
stop that has not landed in ten seconds is not a slow stop.

### What it deliberately is not

- **Not a timeout.** Nothing is ended. The turn runs to its own end and the session stays usable.
- **Not per frame.** A turn being overridden emits continuously; one card per frame would bury the
  message it is trying to deliver. One report per turn, and the browser's `collapse` rule replaces
  the card rather than stacking it if a second telling ever arrives.
- **Not able to see an idle turn.** The check runs per frame, so a turn emitting *nothing* cannot be
  reported on. Both conditions it exists for emit continuously, so what it misses is a turn that is
  idle — a different problem, whose name is `--print-timeout`. Stated here rather than left to be
  found.

### The browser side, and the one thing it reuses

No new RPC. `restart_session` already existed on `AntigravityService` — `AgyService` inherits it, the
Settings panel already calls it, and it **resumes the same conversation** rather than starting blank,
so the context survives and what is lost is the turn the user had already asked to be rid of.

What is new is a system card that can carry an action at all: `systemNotice` may return one,
`onSystemEvent` puts it on the row, and `renderSystemAction` draws a button that disables itself
while the call is in flight — `restart_session` takes seconds to rebuild the harness, and a
live-looking button invites a second press that would tear down what the first is rebuilding. The
failure path toasts, because this is offered to someone whose stop has already been ignored once and
a button that silently does nothing would be the second thing that failed them without saying so.

### Seen in a browser, which the tests could not do

`scripts/stop_ignored_card_probe.py`, on the repo's own live rig — real server,
real Chrome, the event pushed onto the same window channel `app-shell/index.js`
re-dispatches every server push onto, so everything below the server is the real path.
Eight checks, all passing, screenshots in `.aic-dc/live-probe/`:

- The revived card renders and reads as intended, with the amber **Force restart the engine**
  button below it at 160×31px — and `elementFromPoint` at the button's own centre lands *on the
  button*, which is the check that separates "in the DOM" from "clickable". jsdom cannot make
  that distinction: a control behind an overlay or under `pointer-events: none` passes a
  synthetic `.click()` and fails a real one.
- The prose card renders with **no button**, which is the negative control. Without it a green
  run could not tell the deliberate asymmetry from a build that offers the button always.
- The click restarts the engine for real — the button reads *"Restarting…"* and is disabled on
  the render that follows the click, and a toast says *"The engine was restarted."*

**Two instrument defects first, and the first is the useful one.** The probe reported *"no system
card rendered at all"* — because `Backend` serves the **bundled** webapp by default, and the
bundle predates every line of this. The card was fine; the server was serving last week's
JavaScript, and the failure looked exactly like the feature being broken. Anything asserting on
webapp *source* needs `dev=True`, and the probe now says so where the next reader will find it.
The second was a 200ms sleep that read the button already re-enabled: the restart had finished,
the disabled state had existed, and the probe was reporting its own clock. `await
panel.updateComplete` makes it deterministic.

**What the run does not cover, so the pass is not read as more than it is:** the server end.
Nothing here makes a turn overrun its stop — that needs a hostile `Stop` hook in the operator's
own `~/.gemini/config/hooks.json`, which is not a thing a probe should install. The emit is
covered by `tests/test_agy_session.py`.

**4,972 Python tests and 4,501 webapp tests green; 8 live checks passed.**

---

## The verdict that never reached the browser (2026-09-12)

[AG-R-17](risks.md#ag-r-17)'s last open entry, closed. It had been on the allowlist since the risk was
raised, with a note saying it was *"a mapping to be designed, not a spelling to be fixed"* — which was
the right call and the wrong reason. The entry framed it as a question of presentation: renaming
`stop_reason` to `terminal_reason` would change **what colour a failed `agy` turn draws**, so the
mapping had to be designed before the rename.

Designing it turned up what the framing had hidden. **What colour did a failed `agy` turn draw?
Green.**

### Why this one was different from the four before it

`computeTurnOutcome` checks `is_error` before it reads `terminal_reason`, and **neither Antigravity
footer carries `is_error` at all** — not the `agy` pump's, not the SDK pump's, and neither ever did.
On the Claude transport `terminal_reason` is a second opinion. On these two it is the only one, and
it was going out under a name with no reader:

| The engine said | The panel drew |
|---|---|
| `agy`: `status: "ERROR"` | green LED, no badge |
| `agy`: `status: "CANCELED"` — a stop, or a headless permission denial | green LED, no badge, prose settled as a completed answer |
| SDK: `MAX_*_EXCEEDED` — whichever budget cap fired | green LED, no badge |
| SDK: `QUOTA_EXHAUSTED` | green LED, no badge |

The first three divergences cost a stat line and an empty settled message. This one cost the turn's
verdict, on every failure either transport can report.

### The mapping

`antigravity.steps.terminal_reason_for`, shared by both pumps — the agy pump already imports
`TurnStats` from that module, so this follows the grain rather than adding a vocabulary module for
one function. Three rules: the no-reason words (`""`, `SUCCESS`, `UNSPECIFIED`) become the empty
string; `ERROR` and `CANCELED`/`CANCELLED` become `engine_error` and `aborted_streaming`; everything
else passes through lower-cased.

Two things it deliberately does **not** do, both of which were the tempting move:

- **`SUCCESS` is not `completed`.** The symmetry is obvious and it is a lie — `completed` draws a
  green check, and `agy` says `SUCCESS` about a turn held open until `--print-timeout` expired
  ([AG-R-16](risks.md#ag-r-16)). An absent badge is the honest picture and the browser's own stated
  preference: *"a badge that claims a clean finish is worse than no badge at all."*
- **`MAX_MODEL_CALLS_EXCEEDED` is not `max_turns`.** Folding the budget family into the browser's
  loop-cap word would throw away which cap fired, which is exactly what [AG-6](decisions.md#ag-6)
  wanted from `BudgetConfig` over a dollar cap. `max input tokens exceeded` on the badge beats
  `turn limit reached`.

`CANCELED` covers both a user's ⏹ and a headless permission denial — phase 0 measured the denial
path reporting `CANCELED` with exit 0 and no error key anywhere. `aborted_streaming` is true of both,
and it is in `CANCELLED_TERMINAL_REASONS`, so the `agy` pump now derives `cancelled` from the reason
the same one-line way `messages.py` does. That closes a second gap on the way past: a turn `agy`
cancelled on its own, rather than one the session latched from ⏹, used to report `cancelled: false`
and render as a completed answer that said nothing.

### The new tripwire failed on its first run, correctly

The field tripwires ask whether a *key* is spelled the Claude pump's way. They cannot ask whether a
*value* is one the browser has a label for — and a mapping onto a word the browser had since renamed
would look exactly like a mapping that worked. `TestTheTerminalReasonLandsInAVocabularyTheBrowserHas`
reads `block-render.js` and asserts every word `TERMINAL_REASONS` maps to appears in the badge table.

Its first run reported `engine_error` missing from a table that has a label for it. The match was on
quoted literals, and `block-render.js` writes reason *sets* with quoted members but `REASON_LABELS`
with bare object keys, so one of the two mapped words was invisible to it. Accepting both spellings
fixed it. The lesson is the one `TestVocabulary` already records for `streamChunk`: a source-literal
tripwire is only as good as its idea of how the other side writes things down, and the failure mode
is a false *absence*, which reads exactly like a real finding.

The consumer end is pinned too — a `streaming.test.js` block builds the footer shape these pumps
actually emit, `is_error` and all absent, and asserts an `engine_error` reddens the LED, a budget cap
names its own cap, a cancel stays green, and an empty reason draws no badge.

`THEIRS_ALONE` is back to a single entry on both tripwires: `request_id`, which is a genuine
difference.

**4,979 Python tests and 4,506 webapp tests green.** No live probe: this is a translation between two
vocabularies that are both already measured, and the reading that mattered — `agy` reporting
`CANCELED` for a permission denial with exit 0 — was taken in phase 0 and is recorded in
[`sdk-surface.md`](sdk-surface.md).

## The stop that read like a hang (2026-09-12)

[AG-19](decisions.md#ag-19) shipped its mechanism on 2026-09-11 and named its own residual gap:
`PostInvocation` fires *between* invocations, so a single-invocation prose answer runs to its own end
and cannot be starved by the gate either, because it asks permission for nothing. The decision had
already written down what to do about the experience — *"stop updating the view, badge it stopped by
user, let the stream drain into the warm process, and offer a separate force-reset."* The force-reset
arrived with [AG-R-16](risks.md#ag-r-16)'s `stop_ignored` card. The other three had not been built.

**The cause was one line in the wrong place.** `AgySession.stream_turn` called
`translator.note_cancelled()` *after* the frame loop, so the pump was told about the stop when there
was nothing left to suppress. The flag was a footer field, not a switch. A stopped prose turn
therefore streamed its entire answer to the screen and *then* landed a footer saying it had been
stopped — the one reading of a stop that is worse than no feedback, because it looks like the stop
did nothing.

The session now tells the pump at the top of the loop, before the frame is translated. The late call
stays, because a stop with no frame behind it still has to badge the turn, and `note_cancelled` is
idempotent for that reason.

### Freezing the view is three changes, not one

Each of the other two would have made the freeze a lie in a different direction:

- **The meter keeps running.** Usage is still absorbed from every suppressed frame. The turn is still
  spending, and a cost the UI cannot account for is [AG-R-6](risks.md#ag-r-6)'s family. What stops is
  the rendering, not the accounting.
- **The finished prose is refused.** `agy` assembles the whole answer into `result.response`, and
  `streaming.js` takes a settled message's content from it. Accepting it after a stop would freeze
  the screen for the length of the turn and then paste the complete reply in at the footer — strictly
  worse than never freezing. `response_text` falls back to the accumulated deltas, which is exactly
  what was on screen when ⏹ was pressed.

The suppression is of *every* step type rather than of prose alone. A tool card opening after the
stop would announce work the user has no way to watch finish, and the footer's counters would
describe a turn that kept growing behind a frozen screen.

### The card exists because stillness is ambiguous

A frozen view and a hung one look identical: text stops arriving, which is also what a model thinking
looks like. The footer's *stopped* badge cannot arrive until the turn ends, which on the case this
whole change exists for is the part taking the time. So the pump emits a **`stop_acknowledged`**
system event on the first suppressed frame and never again, and the panel renders it as a card that
says the stop landed and the screen is final.

It carries no action and no toast. The escalation belongs to `stop_ignored`, which appears only after
`STOP_OVERDUE_SECONDS` and only offers the restart when something is actively overriding the stop —
restarting ends a session the user is holding, which is the trade AG-19 leaves to them. The card
collapses, so a stop on a later turn replaces it rather than stacking a second copy of one sentence.

No webapp state needed changing: `input.js::cancel` deliberately leaves `_streaming` set on success,
so the spinner keeps running while the stream drains, which is now true rather than merely tolerated.

### What is asserted

Seven pump tests and one session test, each of which fails against the previous build: the deltas
stop becoming view, a tool card does not appear after the stop, the usage total still advances, the
card is emitted exactly once, the prose is the frozen deltas rather than `agy`'s assembly, and the
footer still closes the turn with `cancelled` and `aborted_streaming`. Two controls guard the shape —
an ordinary turn still takes `agy`'s own assembly, and an unstopped turn still renders. The session
test drives the real fake subprocess and asserts that the words written after ⏹ appear nowhere in any
payload the browser is given. Two webapp tests cover the card and its collapse.

**The mechanism's gap is still open and still deliberate.** Closing it means killing the process:
3.7s of dead session to save a few seconds of text from a turn that holds no locks, runs no commands
and touches no files.

## The consultation that cost a step and a half, and one it moved into this directory (2026-09-11)

Nothing was built here. Three questions about the *unbuilt* work were put through
[`.claude/skills/consult-agy`](../../.claude/skills/consult-agy/SKILL.md)'s convergence protocol over two
rounds, the reviewer declared all three converged, and the result is
[AG-22](decisions.md#ag-22), [AG-R-18](risks.md#ag-r-18), a note under
[AG-R-12](risks.md#ag-r-12) and a smaller programme than the one that went in. Recorded because the
*shape* of how it went is the reusable part, and because two claims died on each side.

The three questions came out of [`../7-future/blank-sheet-architecture.md`](../7-future/blank-sheet-architecture.md)'s
migration order, which had five steps and no costing. They were: how does a Claude consultant become
reachable from `agy` at all; do model-to-model and user-to-model consultation earn two code paths or one;
and is unifying ~4k lines of permission code behind an `ApprovalResolver` protocol worth doing.

### What each side got wrong

**The maintainer lost question 1 outright**, and the losing proposal is in AG-22 for the record: advertise
a shell command in a message preamble, reach it through `run_command`, pass the RPC port in `agy`'s
environment. The reviewer's four objections were good and its fifth — a mechanism with nowhere to put a
credential — only became visible after the probe. **Its own preferred variant was better than the argument
it made for it**: it proposed HTTP MCP on the guess that `agy` might support it, and `agy mcp add --help`
turned out to document `--type http` with repeatable `--header`, example included.

**The reviewer lost question 3 outright, and said so plainly** once the import graph was put in front of
it: *"The ~3,000-line figure was an illusion created by summing the total line counts of an import spine
and mistaking dialog coordination and payload normalization for duplicated authorization engines."* There
is one spine — `agy/gate_server.py` → `antigravity/permissions.py` → `claude_code/permissions.py` — and
`PermissionBroker.can_use_tool` contains no policy evaluation at all beyond one early return for this
app's own read-only tools. The vendor CLI decides what to ask about; the broker renders, broadcasts,
awaits and times out. What survives is two pure functions worth isolating and the socket double now noted
under AG-R-12.

**Question 2 split.** The reviewer's vocabulary was better than the maintainer's — one execution pump with
two coordinators, not "one path with two configurations" — and its predicted failure was absent: it
forecast `if not config.is_consultation:` branches accumulating in a shared turn function, and
`agy/consultant.py` already imports and drives `AgySession` as a separate coordinator class, with
containment installed as a `StaticPolicy` object rather than tested inline. So the residual work is an
invariant, not a restructuring. It did name three failure modes separate coordinators do not prevent, and
those are worth keeping: cancellation propagating into the pump, the accumulator resolving on abnormal
engine exit, and shared conversation-id or brain-tree state bleeding master history into a consultation.
The third is what led to AG-R-18.

### The two findings the consultation did not produce

Both came from checking its recommendations against the tree afterwards, which is protocol step 5 and is
the step that earned its keep here.

- **Its fix for the config-file race was wrong, and this directory was already right.** It proposed an
  ephemeral `HOME` per spawn, never rewritten. That breaks resume for a master session, whose brain tree
  must persist — and [AG-21](decisions.md#ag-21) § *Two roots, not one* had already decided the split for
  a different reason, the two modes wanting opposite contents in one `hooks.json`.
- **`BRAIN_DIR` is a module constant resolved from this server's own `HOME` at import time**, so AG-21
  moves the vendor's tree out from under both of its readers, silently, in the register's recurring shape.
  That is [AG-R-18](risks.md#ag-r-18), and neither reviewer saw it, because neither was looking at
  `steps.py`.

The reviewer also proposed attesting interception in CI with a socket-level test double. Useful, and
weaker than `install.hook_runs`, which already executes the command string actually written to
`hooks.json` — the PyInstaller failure was upstream of the socket, so a double would have replayed past
it. Noted under AG-R-12 rather than adopted as the mitigation.

### What it did to the size of the work

The five steps were costed against the tree before the consultation and again after. Two moved:
step 4 fell because HTTP MCP from this process beats a spawned stdio server and adds no credential
holder, and **step 5 collapsed from roughly 40% of the programme to about 5%** once its premise was
refuted. Against this repo's 1.11:1 test-to-source ratio the whole set is now ~2–3 weeks and ≈3–5k lines
including tests, where step 5 alone had been costed at 1–2 weeks.

**The generalisable part, and it is the same as [AG-21](decisions.md#ag-21)'s.** Both rounds were framed
as *what is the cheapest mechanism* rather than *how do we build the thing I have in mind*, deliberately,
because AG-21 records three rounds spent on mount namespaces to obtain what one environment variable
gives. Framing that way cost the maintainer question 1 and saved the fortnight in question 3. The
reviewer cannot see the repository, so the framing it is handed is the framing it works in — and every
claim it made that was checkable and checked came back either sharpened or refuted, never merely
confirmed.

## The sink stops deciding what the answer is (2026-09-11)

Migration step 1 from
[`../7-future/blank-sheet-architecture.md`](../7-future/blank-sheet-architecture.md#migration-order),
built on the consultation path. The invariant it establishes is one sentence — *no sink may be
load-bearing; every sink is an observer, and the result never depends on one* — and the work was almost
entirely deletion, which is what the costing predicted when it called step 1 an invariant to enforce
rather than a structure to build.

**What was load-bearing, in the order it was found.** `ConsultantBridge._tab` yielded `None` when there
was no browser attached, and `AgyConsultant._run` read that as *translate the frames yourself*:

```python
if observer is not None:
    observer(frame)        # translate and push
else:
    translator.translate(frame)   # translate only
```

Two assemblers of one answer, chosen per frame by whether a tab existed. That is the empty consultation
tab's own mechanism — `specs5/known-issues.md` § *An empty consultation tab* — with the branch left in
place after the symptom was repaired. It is gone: `_tab` always yields an observer, the observer
translates above its emit gate, and the sink is resolved once before the loop so the loop calls one
thing. The `None` case survives as *construction* — a bare `translator.translate` on the `agy` side, a
named `_ignore` on the SDK side — rather than as a condition, because a fork inside a frame loop is
where a rendering question gets to decide what the caller receives.

**Three ways a consumer can fail, and only one of them was handled.** The register's own recurrence
again: what shipped guarded the loud failure and neither of the quiet ones.

| failure | before | after |
|---|---|---|
| A sink that **raises** | Already absorbed, at every emit | Unchanged. The new test for it passes against the shipped bridge and says so in its docstring |
| A sink that **stalls** | Four emits were awaited inline and the end-of-tab drain was an unbounded `gather` over an instance-wide task set — so a paused tab held the tool result, and two concurrent consultations drained each other's frames | Every emit is scheduled through `_schedule`; every wait goes through `_drain`, bounded at `DRAIN_SECONDS` and scoped to one consultation |
| A sink that is **absent** | Changed which code assembled the reply | Changes only who sees it |

**Nothing is cancelled on expiry, and that is a decision rather than an omission.** A send interrupted
mid-`await` is a half-written frame on a socket the next consultation will use, which is worse for the
app than a late frame is for one tab. So `_drain` gives up waiting without touching the task, the task
stays anchored in `_tasks`, and expiry is logged — a tab that settled early is otherwise
indistinguishable from a model that stopped talking. The same reasoning moved the heartbeat to
`_schedule`: it is cancelled in `_tab`'s `finally`, and while it awaited its own send, that cancel could
cut a frame in half.

**Two lines that were one line, now separated by a `try`.** Inside the observer, `translator.translate`
is the accumulator and everything after it is rendering. Translation is allowed to fail a consultation;
dispatch is not, and now cannot. The gate's log was also split — no emit at all is a headless server and
logs at `debug`, an emit with no live turn is the shape that shipped the defect and logs at `warning`,
because one warning per consultation on a server with no browser trains a reader to ignore the line.

**Verified by reverting rather than by reading.** The five new tests were run against the shipped source
with the new tests kept: four fail, one passes. The four are the absence and latency cases; the one that
passes is the raising sink, and it is labelled a guard rather than a fix. `test_agy_consultant.py` gets
the artefact-level twin — the same question asked with and without a tab attached, through the real
binary, must return the same prose. Full suite: 4,995 passing.

**What this did not fix, and it is now a risk rather than a plan.** The same fault exists one layer out
on the master turn, where `_dispatch` awaits every connected browser through an `asyncio.gather` and
sequences engine bookkeeping behind the broadcast. Measured, including the 120s bound that keeps it from
being a deadlock, as [AG-R-19](risks.md#ag-r-19) — and deliberately not fixed here, because the master
path has an ordering contract the consultation path does not, so the bridge's answer to a stalled
consumer is the wrong answer there. It needs an outbound queue per client, which is its own change with
its own tests, on the riskiest path in the app.

*Superseded the same day, and left standing as the record of what was believed when this was written: a
queue is not quite the shape, and part of what this paragraph implied about ordering was wrong. The
section immediately below is the correction.*

## A consultation that refuted a line already committed (2026-09-11)

The consultation half of migration step 1 shipped in the section above, and the master-turn half was
recorded as [AG-R-19](risks.md#ag-r-19) with a mitigation written into it. Before building that
mitigation it went to a second opinion over two rounds, and **the first round refuted a sentence that
was already committed to this plan.**

The sentence was the last line of AG-R-19's mitigation: *"Engine state moves ahead of the enqueue, where
it never needed a browser at all."* It reads as obviously right — bookkeeping does not need a browser, so
why would it wait behind one. The reviewer called it a causal inversion and said the bookkeeping methods
are re-entrant event producers. It could not check that claim, having no repository access; this side
could, and it is true:

```
_dispatch(subagentEvent, terminal=True)
  → _sweep_ended_subagent(payload)
    → permissions.cancel_for_agent(agent_id)
      → _deny_unanswered(pending, "cancelled", reason)
        → _announce(...)
          → _broadcast(Event("permissionResolved", {...}))
```

Dispatching one event synchronously causes the dispatch of another, so the relative order the browser
sees is decided by whether bookkeeping runs before or after the enqueue. Ahead of it, the denial is
queued before the termination that explains it, and the browser draws a dialog denied for a subagent that
still appears to be running — which is exactly what the after-the-broadcast placement was put there to
prevent, in a comment that says so. The plan had been about to delete a guard by describing it as an
accident.

**The resolution is smaller than either the error or the fix.** Leave the statement order alone. Once
`_dispatch` enqueues rather than awaits, bookkeeping stops waiting on a browser *because nothing waits on
a browser* — the conflation AG-R-19 names dissolves without moving a line, and causality is preserved for
free.

### Two of the reviewer's objections did not survive the tree

Rounds two and three of the protocol are *run the probes and bring the measurements back*, and two came
back against it.

| the objection | what the tree says |
|---|---|
| "The Disconnect Paradox" — dropping an overflowed client cements the desync, because there is no full-state fetch to recover from | `get_current_state` exists on **both** engine services, and the shell's `setupDone` calls it on every connect, first or subsequent, with `remoteDisconnected` already scheduling the reconnect |
| A disconnect mid-turn loses an open permission dialog forever, because a pending request is an in-memory `Future` and not committed history | `service.py:1235` is `"pending_permissions": self.permissions.pending()`, with a docstring saying the field exists precisely for a client that connects while a dialog is open |

Both were good reasoning from a model that could not see the code, and both were answered by looking. The
second is the more interesting one: the objection describes a real defect that somebody on this project
had already found and fixed, and the fix is *why* AG-R-19's overflow policy is affordable.

### The measurement neither side was looking for

The reviewer asked, in passing, why a fire-and-forget broadcast uses two-way JSON-RPC with a 120s timeout
at all. The answer turned out to be worse than the question assumed, and it reframes the whole risk.

`JRPC2.call` is a **synchronous** `def` that schedules the transmit as a task and returns. The socket
write was never being awaited. The await is in `remote_call`, which blocks on the *browser's reply* — and
every client handler is `window.dispatchEvent(...); return true`, whose value is packed into a result dict
that nothing anywhere reads.

**The master turn stalls for up to two minutes waiting for an acknowledgement no code consumes.**

And the part that constrains the fix: *today's in-order delivery is a side effect of that useless
round-trip.* One reply in flight means one `ws.send` in flight. Remove the await and N transmits race on
one socket, breaking the contract migration step 1 lists as a must-not-break. The acknowledgement is
worthless and it is simultaneously the only thing holding the ordering up — which is why the mitigation
is a sender task per client rather than the one-line deletion the diagnosis first suggests.

### What changed in the plan, and one new risk

- AG-R-19's mitigation is rewritten: **one sender per client over a bounded shared ring buffer read by a
  per-client cursor**, awaiting `ws.send` and never the reply, with a fallen-off cursor told to rehydrate
  rather than dropped. The ring buffer is the reviewer's, against the per-client queues this side
  proposed; it won on memory (O(buffer), not O(clients × buffer)) and on making overflow cursor
  arithmetic instead of an emergency disconnect issued during an active send.
- A third tripwire on AG-R-19, which is the one that would have caught the refuted sentence: the terminal
  subagent event must reach a client before the `permissionResolved` it causes.
- [AG-R-20](risks.md#ag-r-20) is new and is **shipped behaviour, not a consequence of any of this**: a
  reconnecting browser applies live events against a baseline still in flight, because nothing gates the
  socket while `_fetchCurrentState()` is outstanding. Latent today because reconnects are rare; AG-R-19's
  overflow policy would make rehydration routine, so the gate is a **prerequisite** of it. That is the
  second time on this plan that consulting about one thing surfaced the prerequisite of another —
  [AG-R-18](risks.md#ag-r-18) arrived the same way, out of [AG-22](decisions.md#ag-22)'s consultation.

**Where it stopped.** Two rounds, not converged — the reviewer's last round was an attack, not an
agreement, and the synthesis above answers it but has not been put back to it. The measured parts of
AG-R-19 are settled. The ring buffer is settled by argument alone, and the entry says so.

**What this cost, and what it bought.** Two consultation calls and eight probes against the tree, against
which: one wrong line caught before it was built into the riskiest path in the app, one 120-second stall
correctly diagnosed as an unread acknowledgement rather than as a network problem, and one shipped race
found that nobody was looking for. The rule that earned it is the one in the skill — *a single round
produces a plausible essay; several rounds produce a decision* — with the amendment this session adds:
**and the probes are what make the rounds worth having.** Round one's best content was a claim it could
not check.
