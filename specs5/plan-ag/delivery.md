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
| `decisions.md` | `AG-1` … `AG-10` |
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
