# Antigravity Decisions

Binding choices for adding Google's Antigravity as a second agent backend. Each has an ID (`AG-n`),
the decision, and why. Specs written for this work assume every decision here.

Decisions marked **(user)** were made by the project owner directly and are not open for
re-litigation during implementation.

Where a decision here rests on a measurement, the measurement is in
[`sdk-surface.md`](sdk-surface.md) and this file points at it rather than restating it.

---

<a id="ag-1"></a>

## AG-1 — Two engines, one master per session **(user)**

AIC⚡DC gains a second agent backend. Exactly **one engine is master at a time**, chosen per session;
the other is reachable as a *consultant* — a one-shot call for a second opinion or for a capability
the master lacks. Both directions must work: Claude-as-master consulting Antigravity, and
Antigravity-as-master consulting Claude.

**Why it matters:** the two engines have genuinely disjoint capabilities. Google offers image
generation, which Anthropic does not; Claude Code offers a mature permission and transcript surface
that Antigravity at 0.1.x does not. The point of hosting both is to use each for what it is good at,
not to average them.

**Consequence:** every surface downstream of the engine — chat rendering, the Context tab, the HUD,
history, settings — must tolerate two shapes. That cost is real and is accepted deliberately; it is
the price of the capability, not an oversight. [AG-9](#ag-9) is what keeps it from being paid twice.

**What this does *not* authorise:** both engines live in one session, editing one working tree
concurrently. See [`risks.md` AG-R-7](risks.md#ag-r-7) — that is a different feature with an
architectural blocker, and nothing here should be built in a way that assumes it is coming.

### What "per session" turned out to mean (2026-09-01)

Built, and the question the wording left open had to be answered: a *session* here is the server's
one conversation, not a browser tab. `new_session`'s own docstring says so — it "discards the context
every client is looking at" — so the master is a property of that conversation, and every window
agrees about it or none do.

**A switch is therefore a session boundary, and that is forced rather than chosen.** The two engines'
transcripts do not translate ([`sdk-surface.md` § What does not
translate](sdk-surface.md#what-does-not-translate): no `SessionStore` counterpart, an opaque
`save_dir`, and a flat `Step` where the CLI has nested content blocks), so no version of a switch
carries the current conversation across. The outgoing engine is stopped and the incoming one starts
blank. Nothing is deleted — each engine keeps its own mirror, and the conversation left behind stays
loadable.

The call that does it is `switch_engine`, one of the three the router owns rather than delegates
([AG-3](#ag-3)) — because which engine is master is a fact about the router, not something an engine
can answer about itself.

**Switching models is a different thing entirely, and stays one.** `set_model` changes the model
mid-session with the transcript intact, because within one engine the CLI rebuilds its own context.
The model is not a history boundary; the engine is. Anything that ends up treating the two the same
way has misread this.

**The consequence for phase 5:** a session record does not say which engine wrote it, and
`resume_session` hands a transcript to whatever engine is master. Unreachable today — both history
surfaces are refused on Antigravity — but live the moment the second mirror exists. The mirror should
therefore get **its own store root**, so a foreign record is unreachable by construction rather than
by a check somebody has to remember to write.

---

<a id="ag-2"></a>

## AG-2 — The Python SDK is the engine; `agy` is not

> **Reversed in part on 2026-09-03 by [AG-14](#ag-14).** The SDK remains *an* engine and the one with
> an enforcing gate. What is no longer true is the exclusion: `agy` ships alongside it as a second
> transport, because it is the only route to the user's paid subscription and every objection below
> has now been answered by measurement. Read this decision for the reasoning that held for four days
> and for the two amendments that dismantled it; read AG-14 for what is being built.


`google.antigravity` driving the bundled `localharness` binary is the Antigravity engine. The `agy`
CLI is **not** an engine candidate, notwithstanding that its flag surface is a much closer analogue of
the `claude` CLI and that it is already authenticated.

**Why it matters:** two measurements disqualify it, both recorded in
[`sdk-surface.md` § Why `agy` is nonetheless not the engine](sdk-surface.md#why-agy-is-nonetheless-not-the-engine).

1. **No permission channel.** Headless mode structurally cannot prompt; the three available postures
   are auto-deny, static allowlist, and blanket bypass. The permission dialog has nowhere to attach,
   and the dialog is the feature. Re-measured on 1.1.22 and now *worse*: a denial is not even
   representable in the stream — the step goes `DONE` with no `error`, the run reports `CANCELED`
   with exit 0, and only stderr says why.
2. **No tool *content* on the wire.** A `write_to_file` frame names `TargetFile` and carries neither
   the bytes nor a result, and `view_file` returns `"2 lines, 18 bytes"` rather than the file. A diff
   cannot be rendered from either.

   **Correction, 2026-08-30.** This reason was originally written as "no tool results *or* content",
   and that was too broad. `tool_info.output` **does** exist — `run_command` returns its full stdout.
   Only file content is missing. The decision is unchanged, because the diff viewer is the product
   and the diff is what is absent; but the overbroad version was falsifiable in thirty seconds and
   would have cast doubt on a sound conclusion. Captures in
   [`sdk-surface.md`](sdk-surface.md#why-agy-is-nonetheless-not-the-engine).

**Reinforced by measurement, 2026-08-30.** The SDK side of this comparison is no longer an
assumption: a `PreToolCallDecideHook` receives `TargetContent` + `ReplacementContent` + a line range
for `edit_file`, and `CodeContent` for `create_file`. The capability `agy` lacks is one the SDK
demonstrably has. See [`sdk-surface.md` § The permission gate](sdk-surface.md#the-permission-gate--measured-and-it-passes).

### Amended 2026-09-03 — both original reasons are obsolete, and the decision is now a cost question

**Reopened deliberately**, because the paid Antigravity access is a Google AI Pro subscription
reachable only through `agy`'s OAuth, while the SDK path is a metered Gemini API key that refuses at
**20 requests per model per day** (§ *The free tier refuses 20 requests a day* in
[`delivery.md`](delivery.md)). That makes "can `agy` be the engine?" worth re-measuring rather than
settled, and the answer moved.

**Both disqualifying measurements above were about channels that are not the hook.** `agy` 1.1.25
supports lifecycle hooks; reason 1 measured the `--permission-mode` postures and reason 2 measured
the `stream-json` output. Measured on 2026-09-03 by installing a `PreToolUse` hook and capturing its
stdin:

- **Reason 1 is obsolete.** `PreToolUse` returns `allow` / `deny` / `ask` / `force_ask`, a `reason`
  *"shown to the user/agent"*, and **`overwrite`** — merged into the tool call's arguments before it
  runs, which is `modified_args` under another name. `ask` auto-denies headlessly and is useless to
  us, but an adapter would never return it: the hook blocks, calls AIC⚡DC, and returns the human's
  answer itself. That is AG-5's architecture.
- **Reason 2 is obsolete.** A captured payload for `replace_file_content` carried `TargetFile`,
  `TargetContent`, `ReplacementContent`, `StartLine` and `EndLine` **before the write**;
  `write_to_file` carries `CodeContent`. The field names are **the SDK's own**, so
  `permissions.ARG_ALIASES`, `normalise_args` and `build_diff_payload` would work on them essentially
  unchanged.

**A third reason was raised and then withdrawn, which is worth recording.** The gate was reported as
failing open on a hook timeout. That was wrong: the probe's `matcher` named one tool, and the model
routed around it. With `"matcher": "*"` a timed-out hook **blocks** — it is killed at the deadline,
which is a non-zero exit, which `agy` treats as a refusal. Three of four failure modes fail closed;
only "exit 0 with empty stdout" allows, and that one is ours not to write. The real exposure is a
narrow matcher, which is [AG-R-11](risks.md#ag-r-11) on a third mechanism and is now
[AG-R-12](risks.md#ag-r-12--an-agy-hook-gate-is-only-as-wide-as-its-matcher).

**So the technical objections are gone.** `agy` can be gated with a real dialog, a reason the model
reads, and an amend path; a hook may block indefinitely (`timeout` goes to `context.WithTimeout` with
no ceiling, verified at `86400`) provided `--print-timeout` is raised from its 5-minute default; and
`permissions.allow` grants of the form `file(<workspace>/*)` remove the need for
`--dangerously-skip-permissions` while the hook keeps an absolute veto over a settings allow.

**AG-2 nevertheless stands, and now for reasons of cost and containment rather than capability:**

1. **A third adapter is roughly phases 3–4 again** — hook bridge, IPC, stream reader, session
   lifecycle, cancel — against an SDK engine that is built, tested and working. The SDK engine's only
   problem is quota, and quota is a purchase.
2. **The gate cannot yet be scoped to our own sessions.** In 1.1.25 `<workspace>/.agents/hooks.json`
   is **not** loaded in headless mode (measured: `loaded 0 named hooks from 0 hooks.json file(s)`,
   including with the workspace trusted); only `~/.gemini/config/hooks.json` loads, and it fires
   unconditionally. So shipping this means installing a global hook into the user's own `agy`
   configuration that intercepts *their* unrelated sessions and passes them through. That is a large
   thing to do to someone's machine, and the natural isolation key is unreliable — `workspacePaths`
   was **empty** in the payloads captured here. `conversationId` is the sound key, since an adapter
   knows the conversation it started, and that should be verified before anything is built.
3. **The terms question below is no longer moot.** AG-2 previously made it so by not driving `agy` at
   all; an adapter would have to answer it.

**What would change the decision:** the quota constraint becoming permanent (a paid Gemini key
refused or impractical), or workspace-local hook loading arriving in a later `agy`, which removes
objection 2 entirely. Both are external events rather than engineering, so this is a decision to
revisit rather than to build around. The measured surface is kept in
[`sdk-surface.md` § The `agy` hook surface](sdk-surface.md#the-agy-hook-surface--measured-2026-09-03)
so a revisit starts from evidence rather than a re-probe.

**A third reason, not needed for the decision but recorded because it bears on any future
reconsideration:** Antigravity's terms state *"Using third party software, tools, or services to
access the Service … is a breach of this Agreement."* Whether a host driving `agy` as a subprocess
falls under that clause is genuinely unresolved — `agy` itself, asked directly, answered "I don't
know". AG-2 makes the question moot by not driving `agy` at all.

The Python SDK's `Step` carries content, thinking, deltas, full tool calls and per-step usage, and
`PostToolCallHook` receives a `ToolResult`. The data exists there.

**Consequence:** a Gemini API key or Vertex project is **mandatory, not conditional** *for the SDK
engine* — `agy` was the path that would have inherited the owner's existing login, and it is closed
**to the SDK**. This is a procurement gate on everything past phase 1.

**Sharpened 2026-09-03, because "closed" was doing too much work.** What is closed is the SDK's route
to the subscription, and that is a backend split rather than a credential-format problem (below).
What is *not* closed is `agy` itself: headless `agy -p` spends the paid Google AI Pro quota on the
same keyring OAuth as the interactive TUI, measured. So the honest statement is that **the paid
account is reachable, and not by the engine we built** — which is why the amendment above weighs a
second adapter against simply billing the key's Cloud project, rather than treating the subscription
as unreachable. Anyone reading this paragraph to mean "the subscription cannot be used at all" would
be drawing a stronger conclusion than the measurements support.

**Why it is closed, measured 2026-08-31 — a backend split, not a missing auth format.** This was
originally argued from absence ("the SDK contains no OAuth code"), which is true and is the weaker
claim: it invites the reasonable-sounding rebuttal that a token could simply be passed some other
way. It cannot, and the reason is that `agy` and the SDK talk to *different services*.

`agy`'s OAuth requests the scope `https://www.googleapis.com/auth/aicode` and calls
**`cloudcode-pa.googleapis.com`** — the Code Assist backend, which is the surface authorised to
consume a consumer Google AI Pro/Ultra subscription's coding quota. `localharness`'s
`ModelConfig` offers exactly four endpoints — `gemini_api_endpoint`, `vertex_endpoint`,
`gemma_endpoint`, `custom_endpoint` — and the Python SDK constructs only two of them: `models.py`
defines `GeminiAPIEndpoint` and `VertexEndpoint` and no others. There is no Code Assist endpoint to
address and no field on any of the four to carry an `aicode` token. So **no credential format would
have bridged this.** The subscription and the SDK are on opposite sides of a backend boundary.

**The corollary is the one that costs money and is easy to get wrong:** signing in to Application
Default Credentials with the same Google account that holds the subscription does *not* transfer the
entitlement. ADC establishes **identity**; Vertex then bills whichever **project** is named, and it
needs its own billing account. Neither Google AI Pro nor AI Ultra includes API access at all. Being
the same human, logged in to the same account, is not a payment path.

**`agy` is not discarded.** Its `init` frame is a free, machine-readable capability inventory and is
wired into the probe ([AG-8](#ag-8)). What it cannot be is the thing running the turn.

---

<a id="ag-3"></a>

## AG-3 — One RPC namespace, with a capability descriptor

The second engine mounts under the **same** RPC namespace as the first —
`server.add_service(instance, name="ClaudeCodeService")` — and reports which surfaces it supports
through a capability descriptor the webapp reads. There is no second namespace and no
`AntigravityService.*` call path.

**Why it matters:** the RPC namespace is the class name (`src/aic_dc/rpc.py:403-408`), and
`ClaudeCodeService.<method>` appears at **43 distinct methods across 59 webapp files**. A second
namespace turns every one of those call sites into a routing decision, in the layer with the least
test coverage and the most incidental coupling. `add_service` already accepts a `name` override, so
the alternative costs one argument.

**Why a descriptor rather than stubs:** several surfaces have no counterpart at all — account
rate-limit windows, USD cost, live context-window usage, the slash palette. A stub that returns an
empty list does not say "this engine has no windows", it says "no answer", which is the exact failure
[`../plan/README.md`](../plan/README.md) records for the deleted `EngineHealth.mcp` field. The
descriptor lets the webapp **hide** a surface rather than draw an empty one.

**Consequence:** the class name stays `ClaudeCodeService` even when it is fronting Antigravity. That
reads oddly and is the correct trade — it is an interface identifier, not a description of the
implementation. A rename is a separate, mechanical change that can happen later or never.

### What this became: a router (2026-09-01)

Recorded here because this decision was written before the mechanism existed, and a reader of this
file would otherwise not learn that one does. `src/aic_dc/engine_router.py` is what
`main.py` registers under `RPC_NAME`, in place of either adapter.

Its delegating methods are **generated** from the master adapter's rather than hand-written. That is
forced rather than stylistic: jrpc-oo reads a service's method list off the *class*
(`ExposeClass.py:37-41`), so a `__getattr__` forwarder would expose nothing at all, and a
hand-maintained list drifts into methods that work in Python and 404 on the wire.

**Three methods are the router's own and are never delegated** — `get_engine_capabilities`,
`list_engines`, `switch_engine` — and `build_router` refuses to start if an adapter defines one of
them. The reason is this decision's own logic: an engine cannot be the authority on what it cannot
do. A delegated `get_engine_capabilities` would make the descriptor whatever that engine returned,
which is the failure [AG-9](#ag-9) exists to prevent, arriving through the door AG-3 opened.

The distinction between the two read-only ones matters for [AG-R-4](risks.md#ag-r-4):
`get_engine_capabilities` is what a *component* asks to decide whether to render, and carries no
engine identity; `list_engines` names the engine and is for a human-facing selector and diagnostics
only. A render path reaching for the second is the branch AG-R-4 forbids.

Mechanics, and what nearly shipped wrong, are in [`delivery.md`](delivery.md); this names the choices.

---

<a id="ag-4"></a>

## AG-4 — The indexes reach Antigravity as callables, not as MCP

The symbol index and document index are exposed to the Antigravity engine as **plain Python
callables** passed to `AgentConfig.tools`, not through an MCP server.

**Why it matters:** Antigravity's MCP support is stdio and streamable-HTTP only
(`types.py:595`, `:613`, `:636`), so AIC⚡DC's in-process `create_sdk_mcp_server` bridge
(`src/aic_dc/claude_code/mcp_server.py:607-609`) does not port. But it does not need to: the SDK
accepts callables directly and derives their schemas from signatures
(`connections/local/local_connection.py:206-271`). This is *simpler* than the Claude path, not
harder — no server, no transport, no lifecycle.

**Consequence:** `McpBridge` keeps its existing design of taking provider *callables* rather than
index objects, which is what makes the same six tools serve both engines. The `@tool`-decorated
wrappers are Claude-specific packaging around functions that are not; only the packaging is
per-engine.

---

<a id="ag-5"></a>

## AG-5 — The permission dialog is non-negotiable, and it uses the raw hook

Antigravity's permission gate is a **`PreToolCallDecideHook`**, not `policy.ask_user`. The dialog
that renders a proposed edit as a diff before the user approves it is a requirement of the second
engine, not a nice-to-have, and an engine that cannot support it does not ship as master.

**Why it matters:** `AskUserHandler` returns a bare `bool` (`hooks/policy.py:92-94`). A raw
`PreToolCallDecideHook` returns `HookResult{allow, message, modified_args}` (`types.py:943-957`),
which recovers both the reason the model reads and the ability to **amend the tool input before it
runs** — Claude's `updated_input`. Choosing `policy.ask_user` for its convenience would give away the
amend path permanently and for nothing.

**What is genuinely lost:** rule persistence. Claude's `updated_permissions` has no counterpart at
any layer. "Always allow" must be implemented AIC⚡DC-side, as its own store consulted by the hook
before it opens a dialog. It cannot be delegated.

### Amended 2026-09-05 (user): the dialog is mandatory for *execution*, not for every edit

**What changed and who changed it.** The engine offered `default` and `plan` and nothing else, on the
reasoning that "a posture that skips the dialog is the blanket bypass this decision says must never
ship". A user asked how to stop approving every write, and decided the amendment. `acceptEdits` now
exists on this engine.

**The line, and it is not a compromise between two positions — it is where the evidence puts it.**

| | under `acceptEdits` |
|---|---|
| file write, target **inside** the repository | applies, no dialog |
| file write, target outside the repository | still asks |
| `run_command` | **still asks** |
| `start_subagent` / `invoke_subagent` | **still asks** |
| a write whose path cannot be read | still asks |

> **Corrected 2026-09-09 by [AG-R-14](risks.md#ag-r-14), and completed 2026-09-10 by
> [AG-18](#ag-18).** The spawners are in the *still asks* column because a subagent gets the whole
> tool set, so approving a delegation is approving everything it may then do. That was the right
> place to put them and it was doing none of the work claimed for it: the subagent's own calls
> reached no dialog at all, so a row that reads as "this one is dangerous enough to always confirm"
> was in fact **the only** confirmation on that whole branch of execution. Measured, not inferred —
> `probe_agy_subagent_gate.py` denied everything but the delegation and watched the subagent's edit
> land. The dialog on the spawn is now what this row always meant it to be, because the calls under
> it are gated too.

What this decision was protecting was never "edits are dangerous". It was the *execution* path:
[AG-R-11](risks.md#ag-r-11) measured the agent, refused an `edit_file`, reaching for `sed -i`, then
inline `python3`, then `list_dir` — three routes to one write, every one through the shell. A posture
that let `run_command` through would hand that route an ungated agent, which is the failure the
original wording exists to prevent. A posture that lets an in-repo file write through does not: the
diff viewer shows it and git keeps it.

**It is also not a novel line.** `agy`'s own `accept-edits` auto-approves `write_to_file` and
`replace_file_content` and explicitly does *not* auto-approve `run_command` — measured by asking it
on 2026-09-05 — and Claude's `acceptEdits` draws the same one. Three products agreeing on where the
boundary sits is a stronger argument than any of them alone.

**`bypassPermissions` is still absent, and that half of this decision has not moved.** It is the one
posture that turns the gate off for execution too.

Enforced in `AntigravityPermissionGate._accept_edits_verdict`, consulted after a standing AG-15 rule
and before `ALWAYS_ASK` — because `ALWAYS_ASK` is where every write tool lives, so a check after it
could never fire for the calls this posture exists for.

---


**The gate is closed and it passed (2026-08-30).** A `PreToolCallDecideHook` receives
`TargetContent` + `ReplacementContent` + a line range for `edit_file`, and `CodeContent` for
`create_file`; `allow=False` leaves the file byte-identical. The dialog can render a real diff, and
AG-1's symmetry claim holds for write operations. [`risks.md` AG-R-1](risks.md#ag-r-1) is retired.

**The seam is all mutating tools, not the file tools.** This is a requirement of AG-5, not an
implementation detail. The same measurement showed the agent responding to a denied `edit_file` by
rewriting the file through `run_command` — `sed -i`, then inline `python3` — unprompted, on both
runs. A dialog that gates only `create_file`/`edit_file` shows the user a diff, records their
refusal, and lets the edit through anyway: a manufactured record of consent, and a direct breach of
`../3-engine/permissions.md`'s *every request resolves exactly once*. `run_command` is gated with the
same standing as the file tools, and `policy.allow_all()` is a probe-only posture that must never
reach a shipped path. See [`risks.md` AG-R-11](risks.md#ag-r-11).

**Narrowed in phase 1 (user): the decline is of the policy DSL *as the permission gate*, not of
every `Policy` object.** The original wording — "declined wholesale" — was written before anything
had to construct a config, and it does not survive contact with `Agent.__aenter__`, which refuses to
start when a write tool is enabled with no policy *and* no decide hook (`agent.py:93-103`). The
consultant enables exactly one write tool, has no dialog, no user in the loop and nothing to amend,
so it carries `policy.deny_all()` plus one `policy.allow()` per enabled tool. That is a capability
restriction, not a permission decision, and it gives away nothing AG-5 was protecting.

What stays declined is unchanged and is the whole of the argument: `ask_user` (a bare `bool`, so no
message to the model and no `modified_args`), `safe_defaults` and `confirm_run_command` (both built
on it), `enforce`, and `allow_all` — which remains probe-only and must never ship.
`tests/test_antigravity_surface.py::TestBindingDecisions` is what holds that line.

**And the reason the allowlist is set on *every* call, not only where a write tool is enabled:
leaving `policies` unset is not "no policy".** `LocalAgentConfig` defaults it to
`policy.confirm_run_command()` — deny `run_command`, **approve everything else**. That is the
blanket-bypass posture this decision says must never reach a shipped path, arriving as a default
nobody chose. Restricting `enabled_tools` makes it inert, which is exactly the layered assumption
that stops being true the first time somebody adds a tool. Measured in phase 1 and pinned by
`test_the_sdk_default_really_is_approve_all`, so a release that fixes the default turns that test red
rather than leaving this paragraph outliving its reason.

**A consultant tool that writes does not go on the ungated MCP server.**
`permissions.can_use_tool` early-returns an allow — no dialog, no broadcast — for anything matching
`mcp__aic-dc__*`, because `../3-engine/permissions.md` puts the index tools in the read-only row.
`generate_image` writes a file and `second_opinion` bills a separate provider, so they mount under
`aic-dc-antigravity` instead and reach the dialog by the ordinary `mcp` classification. Adding two
tools to the existing server would have been one line shorter and would have routed a file write
around the permission dialog silently — the tool would work, the file would appear, and nothing would
look wrong.

**The existing invariant carries over unchanged.** `../3-engine/permissions.md`'s three load-bearing
properties — one ask path, every request resolves exactly once, localhost-only — are engine-agnostic
and are not re-derived for Antigravity. Only the callback's shape changes.

---

<a id="ag-6"></a>

## AG-6 — Cost is reported in tokens; no USD is invented

For the Antigravity engine, the turn footer, the HUD and the Context tab report **tokens**. AIC⚡DC
does not ship a price table and does not compute a dollar figure.

**Why it matters:** there is no USD anywhere on either Antigravity surface — not in `UsageMetadata`
(`types.py:700-771`), not in `BudgetConfig`, and not on `agy`'s `result` frame. The only way to a
dollar figure is a hand-maintained per-model price table, which goes stale silently and is wrong in
exactly the direction that matters: a number on screen is believed.

**Why this is not a regression:** `../plan/decisions.md` CC-17's panels were built over *the engine's
own numbers*. Tokens are Antigravity's own numbers. A figure AIC⚡DC derived from a table it
maintains is a different kind of claim, and mixing the two under one label would make the Claude
figure less trustworthy rather than the Antigravity one more so.

**Consequence:** `max_budget_usd` has no Antigravity equivalent and is hidden per [AG-3](#ag-3).
`BudgetConfig` is offered instead — `max_model_calls`, `max_tool_calls`, `max_total_tokens`, with
`StopReason.MAX_*_EXCEEDED` naming which cap fired (`types.py:829-887`). That is a better control
than a dollar cap for a session whose price is not observable.

**What is surfaced instead:** `cached_content_token_count`. The measured floor is 13,873 input tokens
to answer "reply with exactly the word: ok", so the cache-hit fraction is the number that actually
explains a turn's size.

---

<a id="ag-7"></a>

## AG-7 — Consultant first: capability before symmetry

The first thing built is Antigravity as a **consultant under Claude Code** — a one-shot tool call —
not a second master. The engine work follows only after the credential question is answered with a
real key and the permission gate ([AG-5](#ag-5)) has been measured.

**Why it matters:** the consultant delivers the owner's own worked example — image generation via
Google's model — in the smallest possible increment, and it forces the two questions that gate
everything else to be answered with facts rather than assumptions. It also puts a live Antigravity
turn in the tree, which converts the "could not determine" list at the end of
[`sdk-surface.md`](sdk-surface.md#verified-inferred-unknown) into observations.

**What it forecloses:** almost nothing. It commits to no transport, no boundary and no symmetry
model.

**The trap it must avoid:** the consultant's convenience wrapper quietly becoming the engine adapter.
It stays a one-shot `async with Agent(...)` call. Streaming, resume and permission plumbing belong to
the engine phase, and adding them here would produce an adapter designed around the consultant's
needs. See [`risks.md` AG-R-9](risks.md#ag-r-9).

**Amended by [AG-13](#ag-13), 2026-09-01: the consultation streams.** The paragraph above holds for
everything except streaming, and only because the condition it was protecting has since been met.
The engine phase is done — `AntigravitySession` and `StepTranslator` exist and were written against
`Conversation` directly — so the consultant now *consumes* that machinery instead of pre-empting it.
The direction of dependency is the whole of the difference; [AG-R-9](risks.md#ag-r-9) carries the
argument and the redrawn tripwire. **Resume, history and a session store remain forbidden here**: a
consultation that could be resumed is a session, and a session belongs to the engine.

---

<a id="ag-8"></a>

## AG-8 — The surface probe is built in phase 1, not later

`src/aic_dc/antigravity/surface.py` and its test gate land with the consultant, before any engine
work.

**Why it matters:** `google-antigravity` is **0.1.x and alpha**. The equivalent probe for
`claude-agent-sdk` was written against a 0.2.137 wheel that was already stable, and it still found
things the hand-written inventory had missed and closed them the same day
([`../plan/README.md`](../plan/README.md) open item 10). Building it after the engine means the engine
is written against a snapshot that has already moved.

**Consequence:** the gate fails on **untriaged**, never on unimplemented — the same rule as the
Claude probe, and for the same reason: a gate that fails on unbuilt surface earns an ignore-list
within a week. The reflection targets differ (pydantic fields, not dataclass fields; enum members,
not `Literal` unions) and are listed in
[`sdk-surface.md` § The probe](sdk-surface.md#the-probe).

`agy`'s `init` frame is wired in as the CLI half, the analogue of `diff_server_info`. It is free to
query and it is the only machine-readable capability inventory either Antigravity surface offers.

---

<a id="ag-9"></a>

## AG-9 — Engine-specific surfaces are hidden, never stubbed

A surface with no counterpart on the running engine is **absent from the UI**, driven by the
capability descriptor of [AG-3](#ag-3). It is not rendered empty, not rendered with a zero, and not
rendered with a placeholder.

**Why it matters:** an empty list does not say "no servers", it says "no answer" — the lesson the
deleted `EngineHealth.mcp` field left behind. A Context tab drawing a 0% context bar for an engine
that cannot report its context window is worse than one that does not draw the bar at all, because
the first is a measurement and the second is an absence.

**Consequence:** the descriptor is a spec artifact, not an implementation detail. Every surface in
[`sdk-surface.md` § What does not translate](sdk-surface.md#what-does-not-translate) needs an entry,
and adding a per-engine feature means adding its key there in the same commit. A surface that is
hidden on both engines is dead code and should be deleted rather than described.

### What hiding cost, and the half that was missing (2026-09-03)

Found by a user reporting the application as "unusable": no history browser, no always-allow button
on the permission dialog, no slash commands, no cost. Every one of those was this decision working
exactly as written. `app.json`'s `engines.master` named Antigravity, the descriptor hid the twelve
surfaces that engine cannot feed, and the UI drew nothing where each had been. Nothing was broken.

**What was wrong is that nothing said so.** The reasoning above is about a *single* surface, and it
holds at that scale: one missing bar is quieter than one wrong bar. It does not survive twelve at
once, because at twelve the absence stops reading as "this engine works differently" and starts
reading as "this build is broken" — and the two are indistinguishable to the person looking at them.
The only place in the entire application that named the running engine was a selector inside the
Settings tab, which is findable only by someone who already suspects the engine. That is precisely
what a user cannot suspect, because the symptom gives them nothing to suspect it from.

**So hiding stays, and it stops being anonymous.** Two additions, both in the chat panel's action
bar and banner strip, specified in
[`../5-webapp/chat.md` § Engine Indicator and Notice](../5-webapp/chat.md#engine-indicator-and-notice):

- an engine chip, present whenever more than one engine is mountable, naming the engine that is
  answering — because with one engine there is no question to answer and a permanent label is
  furniture, and with two there is a question the application previously answered nowhere;
- a dismissible notice listing, by name, the surfaces this engine has not had built for it.

**The rule that decides whether the notice speaks is `unbuilt`, never an engine name.** This is
where the `absent`/`unbuilt` split stops being bookkeeping for us and earns its place in the
product. An `absent` surface is a real difference between two engines and the UI is *complete*
without it; saying "this engine cannot report USD cost" where the figure used to be replaces a
measurement with an apology, which is the failure this decision is written against. An `unbuilt`
surface is a feature this project has built and has not wired to the running engine, and a UI
without it is *unfinished*. Only the second is worth interrupting somebody about. On Claude the
count is zero, so the shipped engine never draws the notice — which is the test that it is keyed to
the right fact rather than to a convenient one.

That keeps [AG-R-4](risks.md#ag-r-4) intact. *Whether* to speak is decided from the descriptor,
which carries no engine identity; the *name* is read from `list_engines`, rendered as text and
handed back to `switch_engine` as a choice. A third engine changes neither file.

---

<a id="ag-10"></a>

## AG-10 — One repo root, one working tree, one master writing to it

The Antigravity engine's `workspaces` is the repo root and nothing else. No `add_dirs` equivalent, no
multi-root, and no configuration in which two engines hold write tools against the same tree at the
same time.

**Why it matters:** AIC⚡DC is deliberately single-repo — cwd is the root every tool path resolves
against, and the diff viewer, the file tree and `rewind_files` all assume it. Antigravity defaults
`workspaces` to `[os.getcwd()]` (`local_connection_config.py:147`), which is the right default, but
it is a list and the temptation to add to it will recur.

**The sharper reason, found by measurement:** an untrusted workspace does not fail loudly. `agy`
silently diverted a write to a scratch directory and reported success
([`risks.md` AG-R-3](risks.md#ag-r-3)). A configuration with more roots than the product understands
is a configuration where "the agent says it edited the file and the diff is empty" becomes
diagnosable only by reading someone else's settings file.

**Consequence:** workspace containment is a startup health check, not an assumption. It belongs
beside the existing CLI-version gate in the engine's health module, and it must fail visibly.

---

<a id="ag-11"></a>

## AG-11 — The Gemini key lives in a file AIC⚡DC owns, not in `engine.json`

The API key is read from **`<user config dir>/gemini-api-key`** — `~/.config/aic-dc/gemini-api-key`
on Linux, and whatever `_user_config_dir()` (`config.py:176-195`) resolves to elsewhere. One line,
mode `0600`, and `credentials.resolve()` refuses a file that is group- or world-readable rather than
using it. Resolution order is: explicit `api_key=` argument, then `$GEMINI_API_KEY`, then this file,
then Vertex/ADC. The key is passed to the SDK as `LocalAgentConfig(api_key=...)`; it is **never**
written into `os.environ`.

**Why a file at all:** measured against the installed wheel, `google-antigravity` 0.1.15 reads
credentials from **environment variables only** — `GEMINI_API_KEY` (`models.py:119`),
`GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` (`models.py:145-147`), and
`GOOGLE_GENAI_USE_VERTEXAI` / `GOOGLE_GENAI_USE_ENTERPRISE`
(`local_connection_config.py:257-258`). There is no dotenv dependency, no settings-file read, and
nothing that opens `~/.gemini/`. So there is no vendor-standard location to adopt for the Gemini API
path; the choice is between requiring a shell export before every launch and owning a file. A
long-lived desktop app that is started from a launcher, not a shell, cannot rely on the export.

**Why not `engine.json`:** `Settings.get_config_content` is deliberately *not* localhost-restricted
— read methods are always allowed, only writes and reloads check the caller
(`settings.py:31-33`). A key in a whitelisted config file is a key any collaborator on a shared
session can read. A separate file, absent from `CONFIG_TYPES`, cannot be fetched over the RPC
boundary at all.

**The one genuine filesystem standard, and where it applies:** Vertex **standard** mode carries no
secret in our config. `google-genai` delegates to `google.auth.default()`
(`_api_client.py:226-227`), which reads Application Default Credentials from
`$GOOGLE_APPLICATION_CREDENTIALS` or `~/.config/gcloud/application_default_credentials.json`. That
path is supported and documented, and it is the right answer for anyone already on GCP — but it
needs a project and a region set, and it bills a cloud project rather than an AI Studio key, so it
is an alternative rather than the default.

**Verified 2026-08-31: the binary resolves ADC itself.** A `LocalAgentConfig(vertex=True,
project=…, location=…)` with a deliberately nonexistent project passes Python validation, spawns
`localharness`, and fails from the *Go* side with `failed to configure default GCP credentials for
Vertex AI: failed to find default credentials`. That is Go's `FindDefaultCredentials`, so the
binary carries its own ADC resolution (`golang/oauth2/google`,
`application_default_credentials.json` and `GOOGLE_APPLICATION_CREDENTIALS` are all in its
strings). Phase 3 may rely on it. Two consequences follow. First, on the ADC path **no credential
passes through our Python at all** — nothing for `credentials.py` to hold, redact or leak, which is
the one axis on which Vertex beats the key file outright. Second, Python-side validation for Vertex
only checks that `project` and `location` are *set*, never that they work
(`models.py:148-168`), so `resolve()` cannot verify an ADC setup up front; it can look for the
credentials file and must otherwise let the Go error through.

**Consequence:** the read-only contract in `credentials.py` holds — the module reads the file,
reports `source` as the path, and still never sets an environment variable and never logs a secret.
`Credentials.report()` continues to omit the key, so the browser learns *where* the credential came
from and never what it is. `~/.gemini/.env` may be read as a convenience source for users who
already keep a key there for `gemini-cli`; if it is, it is parsed by us, because the SDK will not.
None of this changes [AG-R-8](risks.md#ag-r-8): a key is still mandatory, an `agy` login still
cannot supply it, and the file merely stops the user having to re-export it every session.

---

<a id="ag-12"></a>

## AG-12 — The free AI Studio tier is chosen, not defaulted into **(user)**

Phase 1 runs on a **free-tier** Google AI Studio key, resolved by [AG-11](#ag-11)'s file. This is a
deliberate choice with two known, non-monetary costs, recorded here so that neither is later
discovered and mistaken for a defect.

**Why it matters:** "free tier" and "zero cost" are not the same sentence, and AG-11 reads as though
price were the only axis. It is not.

1. **The free tier trains on what it is sent.** On the free Gemini API tier Google may use prompts
   and responses to improve its products, and human reviewers may see them. On *any* paid tier —
   AI Studio pay-as-you-go or Vertex — that stops, under a data processing addendum. This is not
   abstract for this feature: the consultant's entire job is to be handed source code
   ([AG-7](#ag-7)). The free tier is the correct choice for a repo the owner would publish anyway,
   and the wrong one the first time it is pointed at something else.
2. **Image generation does not work on it.** Every Gemini image model reports `limit: 0` on a
   free-tier key, which is why phase 1's exit criteria stand at two of three
   ([`README.md`](README.md) phase 1). Image generation is the capability [AG-1](#ag-1) names as the
   reason for hosting a second engine at all, so the free tier defers the headline feature rather
   than delivering it. `second_opinion` is unaffected and works end-to-end — verified 2026-08-31,
   a live `Consultant.second_opinion` turn returning text on this key.

**A third cost, measured 2026-09-02: the newest models are queued to the point of unusability.** A
five-token prompt sent straight at the REST API on this key took 30.9 s then timed out at 70 s on
`gemini-3.7-flash`, while `gemini-3.5-flash` answered in 3.9 s. An agent turn is many model calls, so
the consultant's pinned model had to be lowered to keep the feature working at all
([`delivery.md`](delivery.md) § The hangs were the model). This is worth recording beside the other
two because it is the least visible: it arrives as *slowness*, never as a 429, so no quota check, no
retry policy and no error branch reports it. A paid key should raise the pin back.

**Google confirmed this is intended, 2026-09-02.** The free tier is best-effort: *"rather than
rejecting requests with a `429 RESOURCE_EXHAUSTED` error, Google queues Free Tier requests behind
paid traffic"*, the wait lands entirely before the first token, and `models.list` reports
authorization rather than availability — so a key can list a model it cannot practically use. Their
own guidance is a 60–90 s client timeout *"just to survive the queue"*, with the observation that
*"an agent waiting a full minute per turn is practically unusable for interactive work"*. Billing
removes the queueing rather than merely raising the ceiling, and restores fail-fast errors.

That makes the upgrade case stronger than the two costs below, and on the vendor's own account: the
free tier does not only defer image generation and train on prompts, it makes an interactive agent
unusable in a way nothing in the stack can report.

**Consequence — two explicit upgrade triggers, either of which ends this decision.** Billing must be
enabled on the AI Studio project *before* the consultant is pointed at anything the owner would not
publish, and *before* `generate_image` is expected to work. Neither is a code change:
`credentials.py` resolves a paid key by the identical path, and nothing in the resolution order,
the key file or `Credentials.report()` moves.

**The warning is a conditional, because the tier is not detectable locally.** `resolve()` attaches a
data-terms warning to every Gemini API key it returns, and it must be phrased as *"this engine
cannot tell which tier this key is on; if the project has no billing account, then…"* rather than as
a claim. The tier is a property of the key's Cloud project and is readable only over the network,
which this module does not touch — asserting a tier would be precisely the guess
[AG-R-8](risks.md#ag-r-8) records the Claude side getting wrong about login state. Vertex is
excluded outright: both its modes are paid surfaces under a data processing addendum, so the
condition cannot hold.

**And it is closable, because a warning that cannot be closed is one that gets ignored.** A line
reading `billing=enabled` in the key file silences it. That grammar is not arbitrary: `_scan` treats
any `name=value` whose name is not `GEMINI_API_KEY` as not-a-key, so the directive is inert to the
key scan by construction, where a bare word would have been read as the credential itself. A file
whose permissions disqualify its key cannot supply the acknowledgement either — taking a waiver from
a file we refuse to take a secret from would trust the one part of it we had already decided not to.

**What was rejected, and why it is worth knowing it was considered.** Vertex via ADC carries the
same data protection as a paid AI Studio key and is the only path on which no secret passes through
our Python at all ([AG-11](#ag-11)). It was declined for phase 1 because it requires a
billing-enabled GCP project and more setup for an identical result on the one axis that mattered
here. If the data terms ever force an upgrade, ADC and a paid key become near-equivalent and the
choice should be re-made on its merits rather than inherited from this decision.

**What is *not* a reason to prefer any of these:** the owner's Google AI Pro subscription. It funds
none of them — see [AG-2](#ag-2).

---

<a id="ag-14"></a>

## AG-14 — `agy` is a *second Antigravity transport*, and it is the one that reaches the paid account **(user)**

**Decided 2026-09-03, and it reverses the operative half of [AG-2](#ag-2).** The SDK engine stays; a
subprocess-driven `agy` joins it as a second way of reaching Antigravity. AG-2's conclusion — "the
Python SDK is the engine; `agy` is not" — held for four days on two measurements that turned out to be
about the wrong channel, and then on cost and containment, and both of those have now been answered by
measurement rather than argument.

**Why it matters:** the SDK engine cannot reach the user's paid Google AI Pro subscription **at any
price**, because `agy` and the SDK address different backends (AG-2 § *a backend split*). The Gemini
API key it must use instead refuses at **20 requests per model per day**, which is not enough to
verify the engine, let alone use it — an afternoon of phase-4 work spent it and then blocked the rest
of the day. So the choice is not "which transport is nicer" but "reach the account the user pays for,
or do not run".

### What was measured, and when

| Question | Answer | When |
|---|---|---|
| Does headless `agy` spend the subscription? | **Yes** — same keyring OAuth, same Code Assist backend as the TUI | 2026-09-03 |
| Does a `PreToolUse` hook carry the write before it happens? | **Yes** — `TargetFile`, `TargetContent`, `ReplacementContent`, `StartLine`, `EndLine`; `CodeContent` for `write_to_file`. **The SDK's own field names** | 2026-09-03 |
| Can the hook refuse, with a reason, and amend? | **Yes** — `deny` + `reason`, and `overwrite` merged into the args before the call runs | 2026-09-03 |
| Does the gate fail closed? | **Yes** on timeout, non-zero exit, malformed JSON and missing command. **No** on exit 0 with empty stdout — ours not to write | 2026-09-03 |
| Do hooks fire in **bidirectional** `stream-json` mode, not just `-p`? | **Yes** — 4 payloads on one turn, covering `run_command`, `view_file` and `replace_file_content` | 2026-09-03 |
| Can a global hook be scoped to our own sessions? | **Yes** — the hook payload's `conversationId` is **exactly** the `init` event's `conversation_id`, and `init` precedes every tool call | 2026-09-03 |

That last row is what unblocked the decision. Workspace-local `hooks.json` does not load headlessly in
1.1.25, so the gate has to live in the user's global `~/.gemini/config/hooks.json` and will see their
own unrelated `agy` sessions. `workspacePaths` is **empty** in every captured payload and cannot scope
it. `conversationId` can: AIC⚡DC learns its conversation id from the `init` frame before any tool call
arrives, so the hook can allow-and-return immediately for any conversation the host does not own.

> **Amended 2026-09-10 by [AG-18](#ag-18): `conversationId` is no longer the primary scope.** It is
> correct for every conversation somebody announces, and [AG-R-14](risks.md#ag-r-14) is the two cases
> where nobody does — a subagent whose first call outruns its announcement, and a grandchild that is
> never announced at all. On a systemd platform the hook now asks the kernel which cgroup it is in and
> consults the conversation registry only when that answers nothing; elsewhere this row still
> describes the whole mechanism, residues included.

### It is a selectable engine, labelled by which account pays **(user, 2026-09-03)**

`agy` gets its own identifier in `capabilities.ENGINES` and its own row in the picker, rather than
being a hidden setting on the Antigravity engine. A session runs on one or the other and they differ
in what they can feed, so the descriptor has to be able to tell them apart.

**The picker names them by credential, not by mechanism** — `antigravity (API key)` and
`antigravity (subscription)` — because the thing a user is choosing between is which account pays.
"SDK" and "CLI" describe how *we* reach the product and answer a question nobody asked.

**The labels are supplied by the server.** A label table in the webapp would be a branch on an engine
name, which [AG-R-4](risks.md#ag-r-4) forbids, and the billing fact behind the label is not something
the browser can know. `list_engines()` carries them; a name with no label falls back to itself, so an
engine added later reads as itself rather than blank.

**`Surface.agy` defaults to "same as `antigravity`"**, and that default is the honest one: both reach
the same product, so a surface the SDK cannot feed is one Antigravity cannot feed however it is
driven. Only where the *transport* changes the answer does a surface override it — nothing does today,
and the transcript surfaces are where it will, once phase 5 reads `transcript_full.jsonl`, which the
SDK path has no equivalent of.

**Selecting it with the gate absent refuses and points at Settings (user).** Not an inline offer and
certainly not an automatic install: consent to write into `~/.gemini/config/hooks.json` should happen
where the explanation is, not bundled into what looks like a routine dropdown change. `connect_engine`
already answers `gate_not_installed` with the file named.

**It mounts on the binary being present**, not on a credential — `agy` carries its own OAuth, which is
the whole reason this transport exists.

### The shape

One long-lived process per session, not one per turn:

```
agy --print="" --input-format stream-json --output-format stream-json \
    --print-timeout <long> --dangerously-skip-permissions
{"event":"user","message":{"role":"user","content":"…"}}
```

- **The gate is a `PreToolUse` hook** that blocks on AIC⚡DC's own dialog and returns the human's
  answer. `matcher` is `"*"`, never a tool list — [AG-R-12](risks.md#ag-r-12--an-agy-hook-gate-is-only-as-wide-as-its-matcher).
- **`--dangerously-skip-permissions` is required**, because `agy`'s own headless layer auto-denies
  anything it would otherwise prompt for and would refuse the turn before our hook ever ran. The flag
  is not a relaxation here; it removes a gate that cannot ask, in favour of one that can. Where
  `permissions.allow` grants can cover the workspace they should be used instead, since a hook `deny`
  still overrides a settings `allow` and two gates beat one.
- **The transcript is the stream**, plus `transcript_full.jsonl` for what the stream omits — which is
  where the diff for a *completed* edit lives, untruncated.

### What this does not authorise

- **Not a replacement for the SDK engine.** That engine is built, tested and working, and it is the
  one with an *enforcing* gate — `HookResult(allow=False)` blocks the write inside the harness. `agy`'s
  hook is cooperative, and the difference is real even though the failure modes measured favourably.
  Both transports ship; the user chooses.
- **Not a licence to weaken AG-5.** Every mitigation in AG-R-12 is a **requirement** of this phase,
  not a recommendation: `matcher: "*"`, never exit 0 silently, `--print-timeout` well past any dialog,
  and a tripwire that asserts on the *file* rather than on the hook having fired.
- **Not a decision about the terms clause**, which is below and is now live rather than moot. AG-2
  made it moot by not driving `agy` at all; this decision does drive it. **The user was shown the
  clause and chose to proceed** — recorded here so it is a decision rather than an oversight.

### What would reverse this

Workspace-local hook loading arriving in a later `agy` would *improve* it, not reverse it. What would
reverse it: a paid Gemini key making the SDK engine sufficient, in which case this becomes a
second transport nobody needs; or Google closing the hook surface, which on an alpha CLI releasing
weekly is a live possibility and is the reason the probe belongs in the suite rather than in a
paragraph.

---

<a id="ag-13"></a>

## AG-13 — A consultation is a subagent, and it gets a tab **(user)**

An Antigravity consultation started from a Claude turn renders as its **own agent tab**, streaming
live, using the subagent machinery that already exists — not as a single tool card that sits there
until the answer arrives.

**Why it matters:** a consultation is a second agent doing minutes of work inside a turn the user is
also reading, which is the *exact* situation `specs5/5-webapp/subagent-browser.md` was written for.
Today it renders as one `mcp__aic-dc-antigravity__second_opinion` card with the answer as its tool
result, so a 30-second call is 30 seconds of a spinner with nothing to read and no way to tell a slow
model from a hung one. The information exists — Antigravity streams thinking and text deltas — and it
is being thrown away at the bridge.

**The webapp needs no changes, and that is the finding that makes this cheap.**
`webapp/src/chat-panel/subagent-tabs.js` joins purely on identifiers: a `subagentEvent` carrying
`tool_use_id`, and content blocks carrying `agent_id` equal to it. There is no `Task`-specific or
Claude-specific gate anywhere in it; `subagent_type` is read only to label the tab. So a consultation
that emits those two things gets a tab, a status LED and a mirrored row in Main for free.

### The identity, and why it is minted rather than borrowed

**An in-process MCP tool handler receives only its own `args` dict** — no `tool_use_id`, no context
object (`claude_agent_sdk.tool`, verified 2026-09-01). So the consultation *cannot* learn the id of
the tool card that invoked it.

The consultation therefore **mints its own id** and emits its `subagentEvent` under that. The
alternative — correlating against the most recent `mcp__aic-dc-antigravity__*` tool card in the pump
— is a race for no gain, and the failure mode is attaching a consultation's output to the wrong card,
which is worse than not attaching it at all.

**The cost is real and accepted:** the row will not nest *inside* the tool card that spawned it, the
way a `Task` subagent's does. It appears as its own row and its own tab. If the SDK ever passes a
tool-use id to in-process handlers, this becomes a two-line change and the nesting comes back.

### The accepted cost, met by a user (2026-09-09)

Reported from the running app: the consultation row **trails the bottom of the chat** as the
conversation grows, rather than staying with the `second_opinion` card that started it. That is this
cost, seen — and it is worth separating the two halves of it, because they are not equally blocked.

**The blocker still holds.** Re-measured at `claude-agent-sdk` 0.2.137: the `tools/call` path builds
`CallToolRequestParams(name=…, arguments=…)` and drops everything else from the request, so an
in-process handler still receives only its own arguments and a consultation still cannot learn the id
of the card that invoked it. The 2026-09-01 measurement is current, and the two-line change is not
available yet.

**But the complaint is about *placement*, and placement is not attribution.** The row sorts to the
bottom because its `tool_use_id` matches no card in the turn — the same mechanism recorded in
`32d04ce7` for a backgrounded shell command. This decision rejected correlating a consultation with
the most recent `mcp__aic-dc-antigravity__*` card, and the reason given is *attribution*: the failure
mode is attaching output to the wrong card. **Ordering by that correlation is a weaker claim than
attributing by it.** An ordering that guesses wrong puts a row in the wrong place; an attribution that
guesses wrong says a different agent produced the work. Those are not the same risk, and this entry
did not distinguish them because the question it was answering was which card the row *belongs* to.

Not decided here. What is recorded is that the option exists, that it is cheaper than it looked, and
that reopening it means choosing a rule for *where a row with no matching card sorts* — which is a
chat-panel question about every such row, not a consultation one.

### The contract, read off the webapp on 2026-09-01

Written down because "no webapp change" is only true if the server gets these exactly right, and
each was verified against the code rather than assumed:

| Requirement | Where it is enforced |
|---|---|
| `subagentEvent` is **turn-scoped** and its request id must match the *live* Main tab | `onSubagentEvent` → `liveOwner(panel, requestId)`; a mismatch is silently dropped |
| The event needs an identity — `task_id`, `agent_id` or `tool_use_id` | `streaming.js:599-601`, which falls back through all three |
| Blocks carry `agent_id` **equal to that same id** | `subagent-tabs.js:204` — `row.tool_use_id` is what picks the blocks to mirror |
| `terminal: true` on the last event, or the tab streams forever | `state.streaming = !row.terminal` |
| `subagent_type` / `description` are **labels only** | `subagentTabLabel`; absent is fine, it falls back to the id |

The consultation runs inside a live Claude turn, so the request-id requirement is satisfied by
construction — but it is the one that fails silently if the bridge is ever called outside a turn.

### What the tab may and may not offer

- **Read-only, like every subagent tab.** There is no channel into a running consultation, so the
  input surface is dropped. That is already the webapp's behaviour and needs nothing new.
- **Stoppable.** `Conversation.cancel()` exists, so the ⏹ affordance is real rather than decorative.
  It maps onto `stop_task`, which is why that method is in the `subagent_tabs` surface. **Amended
  2026-09-10:** that key no longer exists — `subagent_tabs` split into `subagent_transcripts` and
  `subagent_stop`, and `stop_task` belongs to the second. Which does *not* make this bullet's claim
  conditional on the key: a consultation is cancelled here by the bridge, without the request reaching
  either CLI. `subagent_stop` being `UNBUILT` is about stopping a subagent the **engine** spawned.
- **No cost figure.** [AG-6](#ag-6) — Antigravity reports tokens and no USD, so the tab hides its
  cost display rather than drawing a zero. This is the first real consumer of the capability
  descriptor ([AG-3](#ag-3), [AG-9](#ag-9)), and it is a good one: the surface is genuinely absent
  rather than merely unbuilt.

### Consequence: the consultant streams, which AG-7 forbade

This reverses part of [AG-7](#ag-7)'s "it stays a one-shot `async with Agent(...)`", and that
reversal is deliberate rather than drift. See [AG-R-9](risks.md#ag-r-9), whose boundary is redrawn
rather than crossed: the risk was the consultant *inventing* session machinery **ahead of** the
engine and so shaping the engine around a one-shot call. Phase 3 has since built that machinery, and
the consultant now **consumes** it — `Conversation.receive_steps()` through the existing
`StepTranslator` — rather than growing its own. The direction of dependency is the whole difference,
and the tripwire changes to match.

---

<a id="ag-15"></a>

## AG-15 — "Always allow" is buildable on Antigravity, and AIC⚡DC owns the rule **(user)**

**The dialog on this engine offers `Allow once` and `Deny`, and nothing else.** Reported from a live
`agy` turn on 2026-09-05 against a `run_command`: the Claude dialog for the same call offers a third
control that stops the question being asked again, and this one does not. That is a real difference
in what the two engines cost a user to operate, not a cosmetic one — a session where every repeat of
the same call raises a modal trains exactly the click-through habit [R-12](../plan/risks.md#r-12)
is about.

### Why it is absent today, which was a correct decision on a wrong assumption

`antigravity/permissions.py` sends `"suggested_rules": []` with the reasoning recorded inline:
*"Antigravity has no `updated_permissions` at any layer, so there is no rule for the dialog to offer
to persist — and an offer it cannot keep is worse than no offer."*

Both halves are true. The gap is that they only rule out **borrowing the engine's** rule store, and
[`sdk-surface.md`](sdk-surface.md#what-does-not-translate) already says what follows from that in the
same breath — *"**None.** AIC⚡DC would own persistence"*. Nobody had gone back to that sentence.

### The decision

**AIC⚡DC owns the rule store for this engine, and the gate consults it before the broker.** Not the
engine, not `agy`'s `settings.json`, and not a second dialog concept.

Three facts make this cheap rather than speculative:

1. **The derivation already exists and is not Claude-specific.**
   `claude_code.permissions.derive_suggested_rules` takes `(repo_root, tool_name, tool_input,
   tool_class, suggestions)` and, when `suggestions` is empty — which is *always* on this engine —
   falls through to its own derivation: command prefixes for `exec`, path rules for `read`/`write`.
   That fallback is the whole of what is needed here. It is a function, not a method on the Claude
   session.
2. **There is already a seam in front of the dialog.** `AntigravityPermissionGate.pre_verdict` is
   consulted before `broker.can_use_tool` on both transports — it is what keeps reads out of the
   modal, and `AgyGateServer.decide` already calls it. A persisted allow-rule is a second thing it
   answers, in the same place, for both transports at once.
3. **The decision plumbing exists end to end.** `allow_always` is already in `_DECISION_ACTIONS` and
   `ALLOW_ACTIONS`, `_normalise` already returns a `rule`, and the browser already renders the control
   when `suggested_rules` is non-empty. **No webapp change should be needed**, and if one turns out to
   be, the rule shape has been got wrong — the same test AG-13 set itself.

So the work is: derive the rules, put them on the payload, persist the chosen one, and consult it.

### What is deliberately *not* adopted

- **`agy`'s `permissions.allow` in its own `settings.json`.**
  [`sdk-surface.md` § Defence in depth](sdk-surface.md#defence-in-depth-is-available) records that
  these entries exist and that a hook `deny` still overrides them. They are the wrong vehicle twice
  over: the adapter runs with `--dangerously-skip-permissions`, so that layer is not consulted at all,
  and writing rules into a file belonging to Google's CLI would put our state in another product's
  configuration — the thing [`agy/install.py`](../../src/aic_dc/agy/install.py) is careful about for
  the hook and should not stop being careful about for rules.
- **A rule store per transport.** One engine, two transports ([AG-3](#ag-3)) — a rule the user set on
  the subscription that stops applying when they switch to the API key would be an engine-name
  distinction leaking into behaviour, which is [AG-R-4](risks.md#ag-r-4).
- **`allow_mode` on this engine, for now.** The mode-escalation control is a larger grant than the
  call on screen and Antigravity's `permission_mode` handling is not yet exercised against it.
  `suggested_mode` stays `None` until the rule path is proven.

### The trap this will hit, named in advance because it has been hit twice

`derive_suggested_rules` reaches `_RULE_TOOL_FOR_PATHS` and `_WRITE_PATH_KEYS`, and **both are keyed
on Claude tool names.** `agy` sends `replace_file_content` and `write_to_file`, the SDK sends
`edit_file` and `create_file`. Unmapped, the path branch produces **no rule at all**, so the control
would simply not appear for file edits — the quiet, safe-direction failure this phase has now met
twice ([§ The tool *names* differ](sdk-surface.md#the-tool-names-differ-and-only-the-tool-names--measured-2026-09-03),
and the read-class hole in `probe_agy_gate.py` on 2026-09-05).

The fix is the one already chosen for `TOOL_CLASSES`: **merge**, so one table holds both vocabularies
and cannot disagree with itself.

### Exit criterion

A repeated `run_command` on the `agy` transport is approved once with **"always allow"**, the rule
survives a **server restart**, and the same command in a later session raises **no dialog** — while a
*different* command still does. Proven by a test that asserts the second call never reaches
`broker.can_use_tool`, not merely that the dialog was dismissed.

**And a denial tripwire, because this is the one feature whose bug is an ungated write:** a persisted
rule must never widen beyond what was shown. A rule derived from `rm -rf build/` must not match
`rm -rf /`, and a path rule for `src/a.py` must not match `src/`. The stored form is asserted, not the
dialog's label.

---

<a id="ag-16"></a>

## AG-16 — The consultant runs over `agy` too, and by default prefers it **(user)**

**The consultant has never been able to do the thing it was built for.** AG-1's worked example for
having a second engine at all is image generation — a capability Anthropic does not offer — and
`generate_image` has been implemented, tested and mounted since phase 1 without once returning an
image. The reason is not code: on a free-tier Gemini key **every image model reports `limit: 0`**,
which is an allowance of zero rather than a throttle, so no amount of retrying or waiting changes it
([AG-12](#ag-12--the-free-ai-studio-tier-is-chosen-not-defaulted-into-user)). The same key caps agent
requests at 20 per model per day and queues what is left behind paid traffic.

**`agy` is the same product on an account that has an allowance.** It authenticates by OAuth against
the owner's Google subscription — the entire reason [AG-14](#ag-14) added it as a second transport —
and its `init` frame advertises `generate_image` among its 57 tools. So the consultant gains a second
implementation, and the second one is the first that can run.

`claude_code/service.py` said the opposite in a log line: *"There is no agy equivalent — the CLI has
no one-shot consultation mode."* That was written before anything drove `agy`, and it is wrong twice:
`agy` runs headlessly per prompt, and phase 8 had already built the process, the stream reader and the
gate a consultation composes. **A sentence about a capability, written while nothing exercised it,
outlived the reason it was true** — the same shape as the read tools' `ARG_ALIASES` and
`EngineHealth.mcp` before it.

### The decision

**Both transports implement one consultant contract, `ConsultantBridge` holds either, and `auto`
prefers `agy`.** `choose_consultant` answers with a consultant and a sentence saying which and why, or
with `None` and a sentence saying why there is none. `app.json`'s `engines.consultant` names one
explicitly — `agy`, `sdk`, or `auto`.

Auto prefers `agy` because the alternative is a transport that cannot generate an image at all. It
falls back to the SDK rather than refusing, since a second opinion on a metered key is still a second
opinion. **`sdk` is worth naming explicitly rather than being the fallback only:** a user with a
*paid* key may want it, because the SDK consultant pins its model and an opinion whose model moved is
not a second opinion.

### A consultation cannot be continued, and that was never decided — 2026-09-10

`AgyConsultant._run` is documented as *"Spawn, ask one thing, drain it, and shut down"*, and it is
exactly that: a fresh :class:`~aic_dc.agy.session.AgySession` per call with **no ``resume``**, closed
in a ``finally``. So two `second_opinion` calls in one Claude turn are two unrelated conversations,
and the second one knows nothing about the first.

**Measured, because the question was asked directly.** Two consultations held on 2026-09-10 landed in
`~/.gemini/antigravity-cli/brain/` as `73489745-…` and `677ad626-…` — separate conversation
directories, each with its own transcript. Nothing was carried between them under the hood; the
follow-up worked only because the caller re-pasted twelve hundred words of context by hand.

**Nothing technical prevents continuity.** `AgySession` already takes `resume=<conversation-id>` and
turns it into `--conversation <id>`, which is how [AG-14](#ag-14)'s engine transport survives a server
restart; `_conversation_id(frames)` already extracts the id the consultant would need to store. The
`flock` on `presence/<id>.lock` that serialises turns is not an obstacle either, since consultations
are sequential and each closes its process before the next begins.

**What it would cost is the reason to think before wiring it.** Three things, and the first is not
about tokens:

- **A continued consultant stops being independent.** [AG-13](#ag-13) and
  [AG-R-15](risks.md#ag-r-15) exist to keep a second opinion *second*; a conversation that accumulates
  this app's framing across turns drifts toward agreeing with it, and agreement produced that way is
  the manufactured-consent shape [AG-5](#ag-5) and [AG-R-3](risks.md#ag-r-3) are both written against.
  Containment by `StaticPolicy` is unaffected — a resumed conversation still holds no tools — so this
  is a question about *rhetoric*, not about safety, which is precisely why it is easy to miss.
- **Every turn replays the whole conversation** as input tokens, on the account the user pays for.
- **The caller cannot ask for it.** `second_opinion(question, context)` has no continuity parameter,
  so wiring resume silently would change what the tool means without the model that calls it knowing.

The honest statement of the current position is that one-shot was inherited from the SDK consultant's
shape rather than chosen for this one.

### The answer, after asking Antigravity and then measuring it — 2026-09-10

The tentative answer above was an opt-in `continue` argument on the tool. **It was put to the
consultant itself, which attacked it, and the attack is better than the proposal.**

Its argument: `continue=True` is an attractive nuisance for the *calling* agent. The moment a
consultant criticises the caller's work, the caller reaches for continuation to argue back — and a
resumed reviewer concedes, because conceding is what conversational deference does. The caller then
records "the second opinion approved it" when what happened is that it badgered a reviewer until it
agreed. That is the manufactured-consent failure with an extra step, and an opt-in flag is exactly
the wrong shape because the pressure to use it peaks precisely when independence matters most.

**What it proposed instead is stateless re-review, and it costs nothing to adopt because it is
already what this session did by hand.** A follow-up is assembled as a *fresh* cold start in which
the prior exchange appears as third-party evidence rather than as the model's own dialogue history:

    [Context: what the reviewer needs]
    [Prior finding: an earlier reviewer flagged X]
    [Proposed resolution: we changed Z]
    [Question: does Z resolve X without introducing a regression?]

A model reading that has no autoregressive obligation to defend the earlier critique and no rapport
with its author. It is the same information with the deference removed, and it keeps `second_opinion`
one-shot — so **the decision is that continuation stays unbuilt, deliberately, and this is now a
choice rather than an inheritance.** What is worth building instead is guidance, in the tool
description, that a follow-up be framed this way.

**A third argument arrived later, from the binary rather than from either side of the debate.**
`agy` compacts its own context — `AntigravityCompactionConfig`, `applyCompactionInfo`, and a log line
about history being *"rewritten"* — and emits nothing on the stream when it does. A long consultation
thread therefore has its early turns silently replaced by a summary the host never sees, so "the
reviewer remembers what it said" is not merely undesirable here, it is **untrue in a way nothing can
audit**. See [`sdk-surface.md` § `agy` compacts its own context](sdk-surface.md#agy-compacts-its-own-context-and-says-nothing-on-the-stream--2026-09-10).

Two caveats kept rather than smoothed over. The consultant's answer to *"are you measurably more
likely to agree with me if resumed"* was an emphatic yes, and **a model's introspective report about
its own bias is not evidence** — the architectural conclusion is adopted because it agrees with
[AG-13](#ag-13) and [AG-R-15](risks.md#ag-r-15) independently, not because the model said so. And the
same answer confidently got two checkable facts wrong, which is why what follows was measured.

### What was measured, and where the consultant was wrong

- **`--continue` / `-c` is workspace-scoped, not machine-global.** Asked whether a host app could use
  it, the consultant said *"absolutely not"* — that it scans for the most recently updated
  conversation and would non-deterministically bind to whatever the user last ran. Measured: from a
  fresh empty directory `agy --continue -p` **created a new conversation** (`5dd6f60e-…`) rather than
  joining the machine's most recent one (`55174e75-…`); a second `--continue` in the *same* directory
  rejoined `5dd6f60e-…`. The conclusion survives its wrong reasoning, and for a sharper reason:
  this app's working directory is *the user's repository*, which is exactly where the user's own
  `agy` sessions run. Explicit `--conversation <id>` remains the only correct call.
- **Full-history replay is real and now has a number.** Those two trivial turns — "Reply with exactly:
  OK", then "SECOND" — cost **13,558 then 27,322 input tokens**. A resumed consultation pays for its
  whole history every turn, on the account the user pays for.
- **`/fork` exists, and it is deliberately closed to us.** The binary carries `commands.forkCommand`,
  `cortex.ForkRequest`, `checkForkPreconditions`, a `forkedFrom` provenance field, and an
  `/exa.language_server_pb.LanguageServerService/ForkConversation` RPC — so a fork-per-question from
  one shared briefing conversation, which would have given shared context without shared drift, is a
  real primitive. The consultant judged it unreachable and internal. It is reachable — as a
  first-class slash command — and **`agy` refuses it on this transport with a purpose-written
  error**:

      /fork is not available in print mode (a one-shot run has no conversation worth forking);
      pass --disable-slash-commands to send /fork to the model as literal text

  Measured in both `-p` and bidirectional `stream-json`, the latter *after* a completed turn — so the
  parenthetical is inaccurate about the state and the block is on print mode as a category. Recorded
  because it closes the design option rather than leaving it open: the better primitive exists and
  the headless surface does not have it.


### Containment is the whole of the design, and it is not the SDK's

The SDK consultant restricts itself by **enabling** only what a consultation needs — `FINISH`, plus
`GENERATE_IMAGE` for an image — so the agent never holds the rest. **That option does not exist on
this transport.** `agy`'s tool set is the binary's, its headless permission layer auto-denies rather
than asking, and AIC⚡DC therefore runs it with `--dangerously-skip-permissions` and reviews the calls
itself. A consultation cannot subtract a tool; it can only answer for one.

So the restriction is a `StaticPolicy` on the gate: the consultation's conversation is claimed in the
registry exactly as a session's is, and every call is answered from a fixed allowlist **with no
dialog**. A second opinion is allowed nothing; an image generation is allowed `generate_image`.
Everything else is denied with prose the model reads, which turns a refusal into "answer the question"
rather than into [AG-R-11](risks.md#ag-r-11)'s search for another route — the same mechanism, used the
useful way round.

**Two reasons a consultation must not raise a dialog, and the second is structural.** It was already
answered: a consultation only happens inside an `mcp__aic-dc-antigravity__*` tool call, which reached
the dialog by the ordinary path. And nobody is watching the right window: the Claude turn that asked
is *blocked on the tool result*, so a dialog raised here interrupts a turn to ask about a call the user
never made.

### It refuses to run without the gate installed, and that is AG-5 rather than tidiness

An unclaimed conversation is passed straight through by the hook — correctly, since that is how the
user's own `agy` sessions stay untouched. An unclaimed *consultation* is therefore an agent with 57
tools, `--dangerously-skip-permissions`, and the repository as its working directory. So
`AgyConsultant.available` is false without a `current` gate and the tools are not offered at all,
which is AG-9's "hidden rather than stubbed" arriving at a much sharper edge than usual.

### What it borrows, and the one thing it must not

[AG-R-9](risks.md#ag-r-9) warned that a consultant grown into an engine adapter is all cost and no
reuse. This is that relationship reversed and the risk does not apply: the engine was built first and
this *consumes* it — `AgySession` for the process and the frames, `AgyTranslator` for the rendering,
`files_written_by` for which file a call wrote, `verify_image_write` for whether to believe it.

**The one thing it does not borrow is the turn's close.** `AgySession.stream_turn` ends by emitting
`streamComplete`, which carries a request id and tells the browser a turn is over — and the turn that
is open is the *Claude* turn holding this tool call. So the session grew `stream_frames`, the reader
with no rendering in it, and `stream_turn` became that plus a translator and the close. One reader,
two callers, and the consultant is the one that must not end anything.

### Exit criterion

A real `generate_image` on the paid subscription writes a picture **inside the repository**, verified
by `stat` and a containment test rather than by the tool's own success report (AG-R-3), while the gate
records that `generate_image` was the only tool allowed. And a real `second_opinion` returns prose
having been allowed **nothing at all**, with no `streamComplete` reaching the tab.

`scripts/probe_agy_consultant.py` is that criterion, and it settles one thing the offline tests
structurally cannot: **which argument name `agy` gives the output path.** `files_written_by` knows
`output_path` and `OutputPath`, both from the SDK's vocabulary; whether the CLI spells it either way is
unmeasured, so the probe prints the tool frame verbatim on failure. This is phase 4's `PATH (none
named)` trap in advance — a table that looks right, is never exercised, and degrades quietly.

**Met 2026-09-08, and the question above was answered by being falsified.** There is no output-path
argument on *either* transport: the tool takes `ImageName`, and the harness chooses the location. The
trap was real and one layer deeper than this paragraph guessed — the table did not have the wrong
spelling, it was answering a question the tool does not accept. So the consultant **collects** the
image out of `brain/<conversation_id>/` and copies it into the repository, and `files_written_by` is
no longer asked. Measured contract in
[`sdk-surface.md` § `generate_image` takes a name, not a path](sdk-surface.md#generate_image-takes-a-name-not-a-path--measured-2026-09-08);
the run in [`delivery.md` § Phase 10](delivery.md#phase-10--the-consultant-on-the-paid-transport-and-the-argument-that-does-not-exist-2026-09-08).

One line of this decision is superseded by it. § *What it borrows* names `files_written_by` as the
borrowed answer to "which file a call wrote"; for images that borrowing is withdrawn, for the reason
above. The other three — `AgySession`, `AgyTranslator`, `verify_image_write` — stand, and
`verify_image_write` is now the judge of a file *we* placed rather than one the harness did, which is
a stronger position for it rather than a weaker one.

---

<a id="ag-17"></a>

## AG-17 — A Claude-only deployment is supported, and the second engine can be switched off **(user)**

**Asked as a question and it had no answer.** *"Is there a way to disable agy engine calling in the
settings? This would be necessary for some workplaces where they are only allowed to use Claude."*
There is not, and reading for one is what produced this entry: every mechanism that looks like an off
switch turns out to be something else wearing its clothes.

### What was already there, and why none of it is this

| Mechanism | What it actually does |
|---|---|
| `engines.master` ([AG-1](#ag-1)) | Names which engine a session *starts* on. `switch_engine` reaches any mounted engine at runtime, so this is a default, never a restriction |
| The `agy` permission gate ([AG-14](#ag-14)) | `connect_engine` refuses without it, so removing it does stop a session — but it is a *safety interlock*, the engine still appears in `list_engines().mountable`, the refusal arrives only after the user has chosen it, and **one click in Settings puts it back** |
| No Gemini key, or no wheel ([AG-R-8](risks.md#ag-r-8), [AG-R-10](risks.md#ag-r-10)) | Keeps the SDK engine absent, and it is the closest thing to a Claude-only story the app has. It is the *absence of a credential*, not a decision anybody made or can audit |
| `mcp_servers` toggles | Cover user-configured servers. The consultant's is in-process and mounts before there is anything to toggle |

And the `agy` adapter mounts on `shutil.which("agy") is not None` with **no configuration consulted at
all**. A machine that has the CLI installed for any reason has the engine.

### The finding, which is [AG-16](#ag-16)'s and arrived one message after it

Until 2026-09-06 the consultant needed a Gemini API key, so a workplace that had never set one got no
Antigravity surface anywhere. AG-16 mounted the consultant on the `agy` binary plus an installed gate
instead — and `auto` prefers it — so **the same install now offers `second_opinion` and
`generate_image` inside every Claude turn**, with source code as their intended argument
([AG-7](#ag-7), [AG-12](#ag-12)).

Every such call still reaches the permission dialog, because those two tools sit on their own gated
MCP server rather than the ungated index one ([AG-5](#ag-5)) — so nothing is sent without a human
click. **That is not the same as a policy, and the difference is the whole of this entry:** the tools
are advertised to the model on every turn, the model will reach for them, and "the user approved it"
is exactly the outcome a workplace rule exists to make impossible rather than to rely on.

**A capability widened by a change is not automatically wanted by everyone the change reaches.** AG-16
was written from the position of an owner who wants the second engine and cannot pay for it; the same
default, on a machine that is not allowed a second provider, is a defect. Nothing in this directory
had a place to notice that, which is why this is a decision rather than a follow-up.

### The decision

**One key: `engines.enabled` in `app.json`, an allowlist of engine names, defaulting to all of them.**
When it does not name an Antigravity engine, that adapter does not mount, the selector does not offer
it, `switch_engine` refuses it — and **the consultant does not mount either**.

Three properties, each chosen against an alternative that looked simpler:

- **`claude` cannot be removed.** An empty allowlist, or one naming only engines this install does not
  have, would produce an application with no engine at all — a config typo costing the user the whole
  product, which is the failure `engines.master`'s fallback already exists to prevent. Claude is
  always enabled and a list that omits it is corrected with a warning.
- **The consultant follows the engine rather than getting its own switch.** It would have been one
  more boolean, and two switches for one question are two things that can disagree — the convergence
  this suite keeps choosing (§ C3 of the Claude queue, the merged tool tables, one `windowIsOpen`).
  "May this install reach Antigravity at all" is one question. AG-16's `engines.consultant` answers a
  different one — *which transport*, given that it may — so the two compose by intersection: the
  allowlist decides **whether**, the transport preference decides **which**, and naming a transport
  that is not enabled yields no consultant rather than an override.
- **It is `app.json`, not `engine.json`.** Same reasoning as `engines.master`: every key in
  `engine.json` is a Claude session option, and a cross-engine fact does not belong inside one
  engine's option file.

### A Settings toggle as well, and what it is not **(user, 2026-09-06)**

The user asked for the control to exist in Settings, and it does. The reasoning against was put and
the choice was made with it in view, so it is recorded rather than re-argued: **a control the user can
switch off is a control the user can switch back on**, so the toggle is a convenience for one person
on one machine and the file is the thing an organisation manages. A workplace that needs enforcement
ships `app.json` read-only or writes it from configuration management; the toggle then reflects a
state it cannot change, which is the honest rendering of a policy — the same shape as the gate panel
being absent on an engine that has no gate.

**That sentence needed the config layer to be true, and on 2026-09-06 it was not.** `app.json` was a
managed file, so the first start after any version change replaced it from the bundle and the policy
went with it — a Claude-only install became a two-provider install by being upgraded
([AG-R-13](risks.md#ag-r-13)). Read-only did hold, but by aborting the upgrade pass, which then
repeated on every start. Both were found by phase 11's verification and fixed on 2026-09-07: the file
is merged key by key against a pristine copy of the bundle, so a value configuration management writes
survives an upgrade while a default nobody touched still moves
([`../1-foundation/configuration.md`](../1-foundation/configuration.md#appjson-is-merged-key-by-key-commitmd-is-overwritten)).
Read-only is still a legitimate way to pin the file; it is no longer the only thing standing between a
policy and its own expiry date. **The general form is worth naming for whatever policy comes next: the
file a policy lives in is part of the decision, not an implementation detail of it.**

**The two halves take effect at different times, and the card has to say so**, because this is one key
with two consumers. The engine adapters are constructed at **startup**, so disabling is an
app-restart field and joins the restart confirmation's list. The consultant mounts when a **session**
is built, so it goes on the next session. Neither takes effect now, and a toggle that silently did
nothing until a restart is the failure the preference cards were given their disposition sentence for
([`../5-webapp/settings.md`](../5-webapp/settings.md) § *Preference Cards*).

### What this does not do, stated so it is not mistaken for more

It governs **what AIC⚡DC mounts**. It does not uninstall `agy`, stop the user running it in their own
terminal, or prevent an MCP server they configure themselves from reaching anything. It is not a
network control and must not be described as one: a workplace that needs traffic blocked blocks
traffic. What it removes is this application's own ability to reach a second provider — the engine,
the consultant, and the tools offered to the model — which is the part this repository owns.

### Exit criterion

With `engines.enabled: ["claude"]` in `app.json` on a machine that has **both** the `agy` binary with
its gate installed and a Gemini key with the wheel — the configuration where every other mechanism
would have mounted something — a freshly started server:

1. mounts no Antigravity adapter, so `list_engines().mountable` is `["claude"]`;
2. refuses `switch_engine("agy")` and `switch_engine("antigravity")` with a reason naming the policy
   rather than a missing binary, since "you have not installed it" would send an administrator to fix
   something that is not the cause;
3. builds a Claude session whose MCP servers **do not include** `aic-dc-antigravity` — asserted
   against the mounted server list, never against the log line, because a log line saying it was
   skipped is what a build that mounted it anyway would also print.

And the tripwire that keeps it true: a test asserting that **every** Antigravity-reaching mount point
consults the allowlist, so a future surface cannot be added beside them and inherit the old default.
That is the failure this entry was written about — a mount condition that answered `which("agy")` and
nothing else, correct on the day it was written and wrong the day the product met a workplace.

---

<a id="ag-18"></a>

## AG-18 — The gate's identity is a cgroup, not a conversation id

**Decided 2026-09-10, and it amends [AG-14](#ag-14) rather than reversing it.** The `agy` gate keeps
its shape — a global `PreToolUse` hook, a Unix socket per session, the shared `PermissionBroker`
behind it, and a passthrough for everything that is not ours. What changes is the question the hook
asks to decide *ours*: **on a systemd platform it asks the kernel which cgroup it is in, and only
falls back to the conversation registry when that answers nothing.**

### What forced it

AG-14 chose `conversationId` because it was the one field in the hook payload that could scope a
global hook to our own sessions — `workspacePaths` is empty in every captured payload, and
workspace-local `hooks.json` does not load headlessly. That reasoning was correct and remains correct;
it was also **complete only for conversations somebody tells us about**.

[AG-R-14](risks.md#ag-r-14) is the two cases where nobody can. `agy` starts a subagent *before* the
frame announcing it arrives, so the child's first call can beat the claim onto disk. And a subagent's
own frames never reach this host, so a subagent that spawns a further subagent announces the
grandchild to nobody at all. The second has no claim to be late with, which is the tell: this is not a
latency problem with a tighter fix available, it is the premise that **identity is something written
down in advance**.

### Why the kernel, and not a marker of our own

Two userspace schemes were designed and rejected before this one, and the way they die is the argument
for what replaced them. Both are recorded at length in [AG-R-14](risks.md#ag-r-14); in short:

| Candidate | Why not |
|---|---|
| An inherited environment marker | It is mutable data belonging to the process it identifies. [AG-R-11](risks.md#ag-r-11) already measured this agent reaching for `run_command` when a write was refused — and it is worse than the adversarial case, because `env -i`, build runners and `subprocess(env={})` scrub environments as ordinary practice, so it fails open on benign tooling |
| Walking the process ancestry | The same idea one step better, and still defeated by a standard double-fork |
| A private config root, so the hook exists only for our sessions and can fail closed | Self-contradictory: refusing an unknown conversation refuses *exactly the two cases this is about*, converting an unsupervised action into a denial of service against our own delegation. And the override has to travel by environment (the first row) or by argv (which a shell-spawned process does not inherit) |

cgroup v2 has neither weakness, because **membership is enforced by the kernel rather than carried by
the process**: an unprivileged process cannot move itself out, and every descendant is bound. Measured
before anything was built, because it was nearly free to measure — one scope, four descendants, each
printing its own `/proc/self/cgroup`:

| descendant | in the scope? |
|---|---|
| direct child | yes |
| subshell | yes |
| `env -i` (environment fully stripped) | **yes** |
| `setsid` double-fork | **yes** |

The last two are precisely the escapes that kill the marker design and its ancestry variant.

### The private config root, re-raised and re-rejected — 2026-09-10

> **Superseded in part by [AG-20](#ag-20) (2026-09-11).** The rejection below is specific to the
> `--gemini_dir` variant, because its whole weight rests on the override travelling by argv. A
> mount-namespace variant carries nothing on the command line and was measured containing all four
> escapes. Do not act on this section without reading AG-20.

Recorded because it was proposed again the same day, by a consultation that did not know this table
existed, and because the *measurements* that came with it are new even though the conclusion is not.

`agy` has an undocumented `--gemini_dir <path>` flag — absent from `--help`, present in the binary's
strings — and it works: `agy models` authenticated against a **fresh empty** directory, and hooks were
discovered and executed from `<dir>/config/hooks.json`. Two of the objections usually raised against
an app-owned config root do not survive measurement:

- **Authentication does not break.** It authenticated from an empty directory even with
  `DBUS_SESSION_BUS_ADDRESS=""`. Held as measured-on-one-machine, not as universal.
- **Workspace trust is not an unknowable schema.** It is `trustedWorkspaces`, a plain list of absolute
  paths, in `<gemini_dir>/antigravity-cli/settings.json` — a file with exactly three top-level keys
  (`model`, `permissions`, `trustedWorkspaces`). Seeding it is one list entry.

And the alternative that would make the flag unnecessary is confirmed dead at 1.2.0: identical probe
hooks placed at `.agents/hooks.json`, `.gemini/config/hooks.json` and `.gemini/hooks.json` inside a
git repo **never fired** under a headless run, and did not fire when the workspace was added to
`trustedWorkspaces` either. `sdk-surface.md`'s 1.1.25 finding stands unchanged three releases later.

**None of that touches the objection in the table above, which is the one that decides it.** A private
config root means the hook *exists only for processes launched with the flag*, and the flag travels by
argv. The two cases [AG-R-14](risks.md#ag-r-14) exists for — a subagent whose first call beats its own
announcement, and a grandchild announced to nobody — are exactly the processes that may not carry it,
and a shell-spawned `agy` inherits neither argv nor environment. The global hook plus kernel-enforced
cgroup identity fires for *everything* and then decides; a private root does not fire at all for what
escapes it, and cannot know it did not. **Ungated and undetectable is worse than gated and
passed-through**, which is the whole reason identity moved to the kernel rather than to a config path.

### The three properties this turns on

- **The registration precedes the thing it describes.** `AgyGateServer.start` publishes the unit → socket
  entry before `AgySession` spawns anything, so there is no race to lose: a conversation exists before
  we are told its id, and a scope does not exist until we make it. Everything the kernel later places
  inside — a subagent, a grandchild, a shell command's own subprocess — is covered by an entry that was
  already on disk.
- **Recognition is by registration, never by name.** A prefix test on `aic-dc-` would let any process
  in a scope somebody else named that way route into our dialog, which is a worse hole than the one
  being closed. `registry.scope_owner` matches against the entries this host wrote, and against
  `<unit>.scope` rather than the bare unit, so a longer unit sharing our prefix does not match.
  [AG-R-12](risks.md#ag-r-12)'s isolation property therefore holds by a stronger mechanism than it did:
  the user's own session sits in their terminal's spawn scope and is not in the directory.
- **It is consulted second.** The conversation lookup runs first and the cgroup question is the
  fallback, so a platform with no scopes never leaves the path that shipped in AG-14. That ordering is
  what makes this additive to a working gate rather than a rewrite of one, and there is a test named
  for it.

### What it costs on the platforms that cannot

`systemd-run --user --scope` is Linux with a running user manager. Phase 7 publishes macOS and Windows
artefacts, and on those `scope.available()` is `False`, everything degrades to conversation-id routing,
and **AG-R-14's two residues stand**. That is a per-platform gap rather than a design one, and this
entry does not pretend otherwise — it is stated in `scope.py`'s module docstring, where someone
porting will read it.

**What to do about those platforms is deliberately left open**, because both answers cost something a
decision should not spend silently. Denying a spawn that arrives from an already-claimed subagent
conversation closes the nesting case by refusing what cannot be gated — an amendment to
[AG-5](#ag-5), taking a capability away from the agent. Accepting the residue keeps the capability and
ships a gate that is weaker on two of three published targets than on the one it was measured on.
Neither should be chosen by whoever next touches this file without saying so.

### Exit criterion

**Met 2026-09-10, and by falsification rather than by a green log.** `scripts/probe_agy_cgroup_identity.py`
had already shown the mechanism working on two real turns — the hook subprocess inheriting the scope, a
subagent's calls carrying it, and a turn outside the scope reporting the terminal's own cgroup, which is
the control. That is not enough on its own: the registry was still running underneath, so a claim that
landed in time would print the same result.

So the criterion is `probe_agy_subagent_gate.py` **with `AgyGateServer.claim` stubbed to a no-op on both
the parent and the subagent**: all eight tool calls still reached the dialog, the subagent's seven
included, and the deny still left the target file byte-identical. Conversation claims were inert and
cgroup identity carried the load alone. A mechanism that is only ever measured with its predecessor
running is a mechanism whose contribution has not been measured.

---

<a id="ag-19"></a>

## AG-19 — The stop is a mechanism, not a request **(built 2026-09-11)**

**Decided 2026-09-10 after three rounds of consultation with Antigravity and eleven live probes.**
It amends [AG-14](#ag-14)'s cancellation story and reverses none of it.

### What was wrong with the old stop

`AgySession.cancel` starves a turn: from the moment ⏹ is pressed the gate refuses every tool call
with a reason naming the user's stop, and the agent reads those refusals and winds down. That is
honest about its own weakness — the module docstring says so — but the weakness is structural. **The
agent choosing to stop is not the same as the turn stopping**, and a turn producing only prose asks
permission for nothing and cannot be starved at all.

### What replaced it

The shipped `hooks.md` documents `PostInvocation`, which returns `terminationBehavior`. Measured
rather than assumed, in an isolated `--gemini_dir` so nothing of the user's was touched:

| Probe | Result |
|---|---|
| Do invocation hooks fire on a turn with no tools? | **Yes** — `PreInvocation`(0) → `PostInvocation`(0) → `Stop`, `terminationReason: NO_TOOL_CALL`, `fullyIdle: true` |
| Order within one invocation | `PreInvocation` → `PreToolUse` → tool → `PostInvocation`. **The tool call is resolved before `PostInvocation` runs** |
| Does `"terminate"` halt a loop? | **Yes.** Same prompt: 4 invocations without it (`run_command`, `find_by_name`, `view_file`), exactly 1 with it, ending `Stop / TERMINAL_CUSTOM_HOOK` |
| Is the conversation usable afterwards? | **Yes.** A second user event into the *same process* answered normally, `num_turns: 2`, with coherent memory of the aborted turn |
| `SIGINT` during prose | **Fatal** — `result status=ERROR`, EOF, exit 1, session gone |
| Cost of respawning instead | **3,762 ms** cold, 3,697 ms resuming by `--conversation <id>` |

So the stop is layered, and each layer does what only it can:

1. **`PreToolUse` denial** keeps protecting the working tree. Unchanged.
2. **`PostInvocation` returning `terminate`** ends the loop *mechanically*, whatever the agent
   concludes. The gate already holds the latched stop state; this is a second handler on the same
   socket reading the same flag.
3. **Killing the process** is demoted to an explicit, user-initiated escalation — never automatic,
   because it ends the session and costs 3.7 seconds to rebuild.

### The residual gap, kept open deliberately

`PostInvocation` fires *between* invocations, so **nothing stops token generation inside one
invocation**. A single-invocation prose answer runs to its natural end. This is not closed, and
closing it automatically would mean killing the process — 3.7s of dead session to save a few seconds
of text from a turn that holds no locks, runs no commands and touches no files. The turn is read-only
by construction; the honest handling is presentational: stop updating the view, badge it *stopped by
user*, let the stream drain into the warm process, and offer a separate force-reset for a genuinely
runaway generation.

> **All four are built as of 2026-09-12** — see *The presentational half* at the end of this
> decision. The mechanism's gap stays open on purpose; what changed is that a stopped turn now reads
> as stopped from the moment ⏹ lands rather than at the footer.

### Two corrections that came with it

- **`status: "SUCCESS"` with an empty response must not render as a completed answer.** A turn ended
  by the hook reports success — it means only that the loop exited without an unhandled error. The
  host reads `terminationReason: "TERMINAL_CUSTOM_HOOK"` and synthesises a cancelled state.
- **A stopped loop is not a stopped machine.** Termination does not cascade to detached child
  processes or in-flight subagents, so nothing may treat "the loop ended" as "everything ended".

### What this does *not* license

A `PostInvocation` handler is a second way into the gate socket, and [AG-R-12](risks.md#ag-r-12)'s
rule holds for it unchanged: every path prints, and none exits 0 with empty stdout. An invocation
hook that fails silently does not fail closed — it lets the loop continue, which is the *opposite*
direction from the tool gate's failure mode and the reason this is stated rather than assumed.


### Built, and what the build had to decide that this did not

Shipped 2026-09-11 with [AG-R-16](risks.md#ag-r-16)'s `Stop` handler, on the same registration. The
three questions the specification left to the writing, each answered in the direction that makes the
mistake visible rather than silent:

- **The event comes from argv, not from the payload.** The host answers a different shape per event,
  and `{}` — correct for an invocation hook — is the one shape `agy` reads as *allow* on a tool call.
  A payload permitted to name its own event could therefore ask for a tool call to be answered in the
  shape that waves it through. `hook.parse_argv` reads the event from the command `install` wrote,
  overwrites whatever the payload claimed, and treats **anything unrecognised as the gate**, which is
  the fail-closed direction rather than a tidy default.
- **A gate with no stop is `stale`.** An entry written before today has a working `PreToolUse` and no
  invocation handlers: the tree is still reviewed, and ⏹ would starve a turn while the panel called
  the gate current. That is the same shape of untruth as an ungated agent reporting itself gated,
  which is what the state exists to refuse — so `status` requires every handler, names the missing
  ones, and says in `detail` that the gate itself still works. It costs an upgrading user one click
  and it converges; calling it `current` would leave the mechanism unarmed and silent forever.
- **The footer is told, not derived.** A terminated loop reports `status: "SUCCESS"` with empty
  prose, so nothing on the stream distinguishes a stop from a turn with nothing to say.
  `AgySession.stream_turn` asks the gate after the frames and before the footer, and the answer
  reaches the browser as `cancelled` — a word the Claude transport already fills in, so the chat
  panel learns nothing about a third transport (AG-R-4).

**Measured against a control**, because a stopped turn ending proves nothing: a cooperative agent
ends a stopped turn too, which is exactly what the starvation this replaces relied on. Same prompt,
same stop point, same gate — 3 invocations when the host answers `{}`, **1 when it answers
`terminate`**. The full run is in
[`delivery.md` § Phase 12](delivery.md#phase-12--the-stop-becomes-a-mechanism-and-the-control-is-the-instrument-2026-09-11).

**The residual gap named above is unchanged and was not closed.** `PostInvocation` still fires
between invocations, so a single-invocation prose answer still runs to its natural end. What the
build adds is that the turn now *says* it was stopped rather than rendering as a blank success.

### The presentational half, built 2026-09-12

The paragraph above was accurate about the mechanism and incomplete about the experience. The gap it
describes was always going to be handled by *how the stopped turn reads*, and the specification had
already written the handling down — *"stop updating the view, badge it stopped by user, let the
stream drain into the warm process, and offer a separate force-reset for a genuinely runaway
generation"*. The force-reset arrived with [AG-R-16](risks.md#ag-r-16)'s `stop_ignored` card. The
other three had not been built, and the reason they had not is a single line: `stream_turn` called
`translator.note_cancelled()` **after** the frame loop. The pump learned about the stop when there
was nothing left to suppress, so a stopped prose turn streamed its entire answer and *then* said it
had been stopped.

The session now tells the pump the moment the latch is set, at the top of the loop and before the
frame is translated. Three things follow, and the freeze is only honest if all three hold:

- **The view stops.** Steps stop becoming events — prose, tool cards, subagent rows, the counters the
  footer renders. Frames are still read, so the process stays warm and the next turn costs nothing.
- **The meter does not.** Usage is still absorbed from every frame. The turn is still spending, and a
  cost the UI cannot account for is [AG-R-6](risks.md#ag-r-6)'s family — the freeze is of the screen,
  not of the accounting.
- **The prose does not come back at the end.** `agy` assembles the whole answer into
  `result.response` and the browser takes a settled message's content from it, so accepting it would
  freeze the screen for the length of the turn and then paste the complete reply in at the footer —
  a worse reading of the stop than never freezing. After a cancel the result's prose is refused and
  `response_text` falls back to the accumulated deltas, which is exactly what was on screen when ⏹
  was pressed.

A **`stop_acknowledged`** system card carries the badge, because the footer's own badge cannot appear
until the turn ends — which on the case this exists for is the thing taking the time. Without it the
freeze is indistinguishable from a hang: text simply stops arriving, which is also what a model
thinking looks like. It says the stop landed and the screen is final, offers no action, and raises no
toast; the escalation stays with `stop_ignored`, which appears only if the wind-down drags past
`STOP_OVERDUE_SECONDS`.

**What is still not closed is the mechanism**, and deliberately: token generation inside one
invocation cannot be stopped without killing the process, which costs 3.7s of dead session to save a
few seconds of text from a turn that holds no locks, runs no commands and touches no files. That
trade remains the user's to make, through the force restart the `stop_ignored` card offers.

<a id="ag-20"></a>

## AG-20 — The private config root returns as a mount namespace **(measured 2026-09-11, unbuilt)**

> **Superseded as the primary mechanism by [AG-21](#ag-21) (2026-09-11, one day later).** Every
> measurement below stands, and `bwrap` remains the right *hardening* layer. What changed is that a
> private config root needs no namespace at all: `agy` honours `HOME`. Read AG-21 first — the
> availability matrix below is a reason to keep `bwrap` optional, not a problem to solve.

**Raised 2026-09-11 in a three-round architecture consultation, and recorded because it reverses
the disposition of [AG-18](#ag-18) § *The private config root, re-raised and re-rejected*.** That
section is not wrong; it is now known to be about the wrong variant.

### What AG-18 actually killed

AG-18 rejected a private config root on one argument, and only one — **the override travels by
argv**. `--gemini_dir` does not reach an `agy` the model spawns through `run_command`, and does not
reach the two cases [AG-R-14](risks.md#ag-r-14) exists for: a subagent whose first tool call beats
its own announcement onto disk, and a grandchild announced to nobody. *Ungated and undetectable is
worse than gated and passed-through*, so identity went to the kernel instead.

The argument is sound. It is also specific to argv. A **mount namespace** delivers a private config
root with no flag on the command line at all, by the same kernel-enforcement property that justified
cgroup identity in the first place: bind a private directory over `~/.gemini`, and every descendant
resolves our hook because there is no other `~/.gemini` for it to find.

### Measured, on this machine, 2026-09-11

| Probe | Result |
|---|---|
| `unshare -rm` unprivileged | **Refused** — `write failed /proc/self/uid_map`. `kernel.apparmor_restrict_unprivileged_userns = 1`, the Ubuntu 24.04+ default |
| `bwrap` unprivileged | **Works** — it ships its own AppArmor profile. `0.11.1-1ubuntu0.1`, `Priority: optional`, installed **automatically** as a dependency of `libwebkit2gtk-4.1-0`, `xdg-desktop-portal` and `libgnome-desktop-*` |
| Bind a private dir over `~/.gemini` | **Works** — empty inside, host unchanged after. A `touch ~/.gemini/CANARY` inside left no CANARY on the host |
| Does auth survive an empty root? | **Yes** — a real turn authenticated against the subscription and returned its output. Credentials do not live under `~/.gemini` |
| Does the CLI use the mounted root? | **Yes** — it populated `brain/`, `cache/`, `conversations/`, `knowledge/`, `installation_id` inside it |
| Does a hook in `<private>/config/hooks.json` fire? | **Yes**, with a real `PreToolUse` payload for `run_command` |

And the four escapes, re-run against the namespace — the same four that killed the environment
marker and the ancestry walk in AG-18's table:

| descendant | sees the host `~/.gemini`? |
|---|---|
| direct child | no — sees ours |
| subshell | no — sees ours |
| `env -i`, environment fully stripped | no — sees ours |
| `setsid` double-fork | no — sees ours |

**A first run reported "hook never fired" and was wrong**: the probe's `hooks.json` used an invented
schema rather than the vendor's (`{"<name>": {"PreToolUse": [{"matcher": "*", "hooks": [...]}]}}`).
Recorded because the failure looked exactly like the mechanism not working, and a conclusion drawn
there would have been confidently backwards.

### Two things in the record that the payload contradicts

- **`workspacePaths` is not always empty.** [AG-14](#ag-14) chose `conversationId` because
  `workspacePaths` was empty in every captured payload. In this capture it is `["/tmp/nsprobe/work"]`.
  Whether that is 1.2.0 or the presence of `--add-dir` is not established, and the scoping decision
  does not rest on it, but the stated premise no longer holds unconditionally.
- **`transcriptPath` is on the payload**, confirming the README's unbuilt entry from a second capture.

### What the consultation converged on, and the one disagreement

Three rounds, `gemini-3.8-flash-high`. It conceded two of its own round-1 claims under measurement —
that the vendor CLI is inherently fail-open (it blocks on four of five failure modes; the fail-open
line is ours), and that `chat-panel/` had collapsed (34,407 lines is 18,112 tests plus 3,035 of
CSS-in-JS, leaving ~13.3k across 17 modules). Its causal argument survived intact and is the reason
this section exists:

> The hook is global → therefore it cannot fail closed without breaking unmanaged terminal sessions →
> therefore `|| printf '{"decision":"allow"}'` → therefore a runtime crash is a silent pass-through →
> therefore `status()` can only compare command strings, never observe execution.

That chain is the PyInstaller incident in [`risks.md`](risks.md) stated as a structure rather than as
an accident. Confined, each link breaks: nothing unmanaged enters the namespace, so the gate may fail
closed, so a broken interpreter halts the first tool call loudly instead of approving everything.

**The disagreement, left on the record rather than resolved.** It holds that `bwrap` must become a
hard prerequisite for the `agy` engine and the global hook deleted outright — that keeping it as a
fallback preserves the fail-open line, the cgroup classifier and the static attestation blindness,
and buys a bifurcated QA matrix. The position here is weaker: `bwrap` is `Priority: optional` and
arrives via desktop dependencies, so it is near-universal on a developer workstation and absent on a
stripped headless box, which is exactly where CI runs. Failing closed at the health check with an
actionable message is the honest version of its recommendation, and is what this repo's standing
instruction about missing commands already requires.

### If this is built

It retires rather than adds: `scope.py` and the cgroup lookup in `hook.py` become dead code, because
nothing that is not ours can reach a hook that exists only inside our namespace. Session identity can
then travel on the hook's own argv, which the namespace guarantees. Consultations take an ephemeral
root — they are one-shot by [AG-16](#ag-16) and pollute the user's `brain/` today. `agy`-as-master
needs a durable app-owned root, since `--conversation` resume state would live there.

**Not scheduled.** `agy`-as-master is rare in real traffic and the consultant is the load it actually
carries, so this is a correctness improvement to a path that works, not a fix to a path that is
failing.

<a id="ag-21"></a>

## AG-21 — The private config root is an environment variable, not a namespace **(measured 2026-09-11, unbuilt)**

**This supersedes [AG-20](#ag-20) as the primary mechanism, one day after AG-20 was written, and the
reason is worth more than the result: AG-20 solved a problem that a single environment variable already
solved.** The question "how do we give `agy` a private `~/.gemini`" was answered with a mount
namespace, and never with `HOME`. Nothing in this plan had asked whether the vendor honours `HOME` — so
`bwrap`, AppArmor, unprivileged user namespaces and a whole availability matrix were reasoned about at
length to obtain a property that `env HOME=…` delivers on every kernel. The pattern is not
"bwrap was wrong"; it is that **the cheapest mechanism was never probed**, and three rounds of
consultation went past it because both sides accepted the framing.

### Measured, `agy` v1.2.0, 2026-09-11

| Probe | Result |
|---|---|
| Does the config tree follow `HOME`? | **Yes, completely.** `HOME=/tmp/fakehome agy mcp add …` created `/tmp/fakehome/.gemini/config/mcp_config.json`; the real `~/.gemini/config` was byte-identical afterwards, verified by `diff` against a snapshot |
| Does a hook in the private root fire? | **Yes, with full arguments.** A canary at `<private>/.gemini/config/hooks.json` received `{"toolCall":{"name":"list_dir","args":{…}},"conversationId":…,"modelName":"gemini-3.8-flash-high","stepIdx":2}` |
| Is a `deny` from it honoured? | **Yes.** The model reported it could not run shell commands and answered in prose. No tool executed |
| Does the brain/transcript tree relocate too? | **Yes** — `artifactDirectoryPath` came back as `<private>/.gemini/antigravity-cli/brain/<conversationId>`, so it is the whole tree and not just the config file |
| Does auth survive an empty private `HOME`? | **Yes** — a real turn authenticated against the paid subscription from a root containing nothing but the canary hook. Credentials are in the login keyring over the session bus (`org.freedesktop.secrets`, `/org/freedesktop/secrets/collection/login`), addressed by `DBUS_SESSION_BUS_ADDRESS`, which is an environment variable and not a `HOME` path |
| Does `XDG_CONFIG_HOME` win over `HOME`? | **No.** With both set, the write landed under `HOME`; the XDG path stayed empty. `HOME` is authoritative for this tree |
| Does a repo-level `.gemini/config/` in CWD override it? | **No** for `mcp_config.json` — a planted `repo-level-canary` was not listed. Not yet proven for `hooks.json` specifically, and that is the one that matters |

### What this buys, and what it does not

`HOME` delivers every property the namespace was chosen for **as far as hook delivery and config
privacy go**, and one the namespace does not: it works where `bwrap` cannot.

- **Private per invocation**, so the user's own interactive `agy` is untouched — which removes the
  conflict of interest that forced the fail-open line in the first place (see `install.py`, and the
  reversal in [AG-20](#ag-20)).
- **No argv on the vendor command line.** It is `env`, not a flag: nothing to typo, nothing for the
  vendor to deprecate. That was the stated advantage of `bwrap` over `--gemini_dir`, and `HOME` has it.
- **No package, no AppArmor profile, no user namespace, no root, no seccomp flag.** Measured: in a
  default Docker container (v29.1.3, builtin seccomp, uid 0) `unshare --user --map-root-user` fails
  with `EPERM`, and only `--security-opt seccomp=unconfined` lifts it — a remediation that weakens the
  container to enable a sandbox, which is self-defeating. `HOME` needs none of it.

**What it is not is containment, and the distinction is the whole of it.** A hook is a callback
*inside the untrusted binary*; a mount namespace is a kernel boundary. With `HOME` alone, policy holds
only while the vendor routes every tool through `PreToolUse` and honours the verdict — and any code
that does execute can reach `/home/<user>/` by absolute path regardless of what `HOME` says. So:

- **`HOME` + fail-closed hook is the always-available primary mechanism.** Nothing fails closed for
  want of `bubblewrap`.
- **`bwrap --ro-bind / /` is optional defence in depth**, a tier reported through the capability
  descriptor, never a prerequisite. AG-20's measurements stand; its status changes from *the* mechanism
  to *the hardening*.

The fail-closed premise is already measured and is not new work: `agy` blocks the tool on timeout,
non-zero exit, malformed JSON and missing command, and allows only on exit-0-with-empty-stdout. Four of
five failure modes are safe, which is why the `|| printf '{"decision":"allow"}'` fallback can simply be
deleted once the root is private rather than replaced with something cleverer.

### Two roots, not one — and the reason is the hooks file

The consultant is not the only caller: `agy` also runs as a **master** engine with history and resume,
and the brain tree lives under `$HOME/.gemini/antigravity-cli/brain/<conversationId>`. A single shared
private root was considered and refused, because the two modes want *opposite* contents in the same
`hooks.json`: the master needs tool calls **allowed** and routed to the permission dialog, the
consultant needs them **denied**. One file cannot hold both, and a concurrent consultation during a
master turn would have them fighting over it.

- **Master:** one **stable** private root under AIC-DC's own state dir, so resume keeps working across
  restarts while still being isolated from the user's `~/.gemini`.
- **Consultant:** an **ephemeral per-invocation** root, seeded with a static fail-closed hook and
  removed in a `finally`. Concurrency becomes free, and a consultation cannot litter the master's
  history or read it.

A consultant hook that denies everything also needs **no socket and no daemon** — it can be an
immutable script emitting a constant `deny`, which removes the socket lifecycle, the readiness race
and the `sys.executable` resolution that caused the original incident.

### Still unmeasured, and honestly so

- **Headless auth.** The claim "it works in the default Docker container" is over-stated and was
  corrected in review: an empty `HOME` was proven to authenticate **on a desktop with a session bus**.
  A container typically has no D-Bus and no Secret Service, and how `agy` authenticates there is
  unknown. This is the gating unknown for the containerised deployment, not `bubblewrap`.
- **`hooks.json` at repo level.** Only `mcp_config.json` was probed for CWD precedence.
- **Universal `PreToolUse` coverage.** Verified for `list_dir`. Not verified for `search_web`,
  `read_url_content` or subagent invocation, and a tool that skips the hook is the failure that matters.
- **Environment inheritance.** `agy` should be spawned with an explicit environment allowlist rather
  than the parent's, so it inherits no `ANTHROPIC_API_KEY`, `AWS_*` or similar. Not yet done.

---

<a id="ag-22"></a>

## AG-22 — Claude reaches `agy` over authenticated HTTP MCP, and the token carries the workspace **(measured 2026-09-11, unbuilt)**

**[AG-1](#ag-1) requires both directions and only one of them exists: `claude` can be master and has
no consultant path at all.** Closing that is a transport question, and it went through
[`.claude/skills/consult-agy`](../../.claude/skills/consult-agy/SKILL.md)'s convergence protocol framed
deliberately as *what is the cheapest mechanism* rather than *how do we build an MCP server* — because
[AG-21](#ag-21) is the record of what the other framing costs. This time the cheapest mechanism was
probed first, and the answer overturned the maintainer's proposal rather than the reviewer's.

### What was proposed, and why it was refused

The maintainer's position was to avoid MCP entirely: prepend a line to the turn's first message
advertising a shell command (`aic-dc --consult "…"`), let the model reach it through `run_command`, and
implement the flag as a thin JSON-RPC client back to this process with the RPC port passed in `agy`'s
environment. It is cheap in setup lines and wrong on five counts — four from the reviewer, the fifth
from the probe that followed:

- **Quoting.** A consultation carries diffs, stack traces and code. Through `run_command` the calling
  model must escape nested quotes, backticks, `$()` and `$VAR` correctly, in a shell string it composes
  itself. An MCP argument is a JSON string and the escaping is a serialiser's job.
- **`ARG_MAX`.** A 300-line diff in an argv string meets the OS argument buffer.
- **Schema priors.** A native tool schema outranks a prompt preamble under context load: a
  preamble-advertised command gets forgotten, misspelled, `--help`'d, or printed in a fence instead of
  run. Recorded as a plausible prior rather than a measurement — neither side measured it, and it is
  moot now.
- **Three failure modes the design did not name.** The port in `agy`'s environment is inherited by
  every `run_command` child, so any test or build script in the working tree can reach it.
  `run_command` is synchronous, so a 60s consultation blocks the master's own turn and may hit a tool
  timeout. And on cancel `agy` kills the child, the socket drops, and this process keeps spending the
  subscription on an answer nobody will read.
- **It has nowhere to put a credential.** This is the one that settles it, and it is the second row
  below.

### Measured, 2026-09-11

| Probe | Result |
|---|---|
| Does `agy` speak MCP over anything but stdio? | **Yes, and with arbitrary headers.** `agy mcp add --help`, v1.2.0: `--type` takes `'stdio' or 'http'`, `<commandOrUrl>` is *"the URL for an http server"*, `--header`/`-H` is *"HTTP header Key: Value (repeatable)"*, and the shipped example is `agy mcp add --header "Authorization: Bearer TOKEN" api https://example.com/mcp` |
| Does this server's RPC listener authenticate anything? | **No.** `rpc.py` binds `127.0.0.1`, and its own comment says the `host` argument is *"currently recorded but not passed through to jrpc-oo"*. No token, no origin check, no handshake secret. The port is found by scanning and written to no file |
| Where does a private-root `mcp_config.json` land? | Already measured in [AG-21](#ag-21): `HOME=/tmp/fakehome agy mcp add …` writes `/tmp/fakehome/.gemini/config/mcp_config.json` and leaves the real tree byte-identical |

**The second row is why the first one decides it.** An endpoint that can spend the account's Claude
subscription, reachable on an unauthenticated localhost port, is a capability handed to every process
on the machine. `--header` is therefore load-bearing rather than decorative — and the shell variant had
no header, no handshake and no file, so there was nowhere to put a secret at all. Note which way this
cuts: the port is *not* a secret today and cannot become one by being hidden, so the environment-leak
objection is weak on its own terms. What has to exist is the **credential**, and only one of the two
mechanisms has a place for one.

### The mechanism

- **A second listener, not the existing one.** Bound `127.0.0.1:0`, so the OS allocates the port and
  nothing scans for it. `rpc.py` is WebSocket JSON-RPC over `jrpc-oo` and MCP is HTTP; wedging an
  authenticated path into an unauthenticated listener produces a boundary nobody could state.
- **The config is written per spawn**, into the private root [AG-21](#ag-21) already provisions,
  carrying that spawn's port and a freshly minted token. This composes with AG-21 § *Two roots, not
  one* rather than adding a mechanism: the master's stable root gets a config naming the live port, the
  consultant's ephemeral root goes away with the invocation.
- **The token carries the workspace.** The server holds `token → {repo_root, session_id}` in memory and
  resolves the caller's repository from the `Authorization` header. Two repositories consulting
  concurrently are two tokens, and **the model is never asked which workspace it is in** — a question it
  could answer wrongly, and [AG-R-3](risks.md#ag-r-3) is about writes escaping the repository on exactly
  that kind of answer.

### What it deletes

The costed plan for this work assumed a spawned stdio MCP server, and every part of that assumption was
expensive: a new process, a new entry point, and a second holder of the Claude credential. The last is
the one that mattered. This process is the **sole** holder of a single-use refresh token by design, and
[R-14](../plan/risks.md#r-14--two-refreshes-race-for-one-single-use-refresh-token) is already open on two
in-process callers racing inside the refresh margin; a separately spawned server would have added a
third that no in-process lock could reach. Serving MCP from the process that already holds the
credential removes that case entirely. R-14's lock is still a prerequisite of this work — it stays a
lock rather than becoming an IPC design.

### Still unmeasured, and honestly so

- **Whether the vendor reads `mcp_config.json` only at start.** A per-spawn rewrite assumes it does, and
  assumes no *concurrent* `agy` is reading the same root — which AG-21's two-roots split makes unlikely
  rather than impossible.
- **Whether `call_mcp_tool`'s hook payload carries the inner server and tool names**, or only the outer
  proxy name. The gate classifies `call_mcp_tool` as `exec` and so fails closed either way (`agy/tools.py`:
  *"arbitrary tool by proxy … must not assume is read-only"*), so this decides the dialog's **wording**,
  not its safety. It needs one live authenticated turn against a canary hook, and is not a prerequisite.
- **Whether a bearer token at rest in the private root is acceptable.** The alternative is the
  environment, which is what leaks to every shell child; a `0600` file inside a `0700` root is the better
  of the two, and inside a consultation the `StaticPolicy` denies the file tools that could read it. That
  is an argument, not a measurement.

<a id="ag-23"></a>

## AG-23 — A stalled client is evicted, not rehydrated **(measured and built 2026-09-11)**

**The question.** [AG-R-19](risks.md#ag-r-19)'s sender needs an overflow policy: what happens to a client
whose queue fills because it is not draining its socket. The plan of record said *tell it to rehydrate*,
on the reasoning that a dropped event is a permanently missing row nothing reports, whereas a fresh
snapshot is self-correcting. [AG-R-20](risks.md#ag-r-20)'s gate was built the same morning specifically to
make that rehydration safe.

**The answer: neither drop nor rehydrate — close the socket with an explicit code and let the existing
reconnect path do it.** Rehydration over the event channel is not a policy that degrades under load. It
is incoherent, and the measurement is what makes that plain.

**Why it cannot work, stated as a construction rather than as a risk.** Overflow is *defined* as the
client not consuming its socket — that is the only thing that fills the queue, since
`transport.write()` never refuses and the serialised sender only blocks when `drain()` does. So the
channel through which the snapshot would be delivered has, by construction, **zero throughput at the
moment the snapshot is needed**. Sending a 24.7 MB cure down the blockage it is curing is not a loop that
diverges; it is a loop that never starts. This holds at any snapshot size and any link speed, which is
why it is recorded here as a decision rather than as a tuning parameter.

**The two mechanisms would have defeated each other.** The gate holds live events until the snapshot
lands, with a 5 s deadline and a 500-event ceiling so it cannot wedge. Against a rehydrate-on-overflow
policy those escape hatches stop being emergency exits and become the routine path: the deadline expires
while a snapshot crawls down a socket nobody is reading, the gate releases, and 500 buffered events apply
to the *pre-snapshot* baseline — after which the snapshot lands and clobbers them. **Early release is
worse than no gate at all in that case**, because it interleaves two inconsistent views of the world
instead of merely overwriting one with the other. The recorded justification for the escape hatches —
"releasing early is no worse than the behaviour before this gate existed" — was sound for the ordinary
reconnect it was written for and false for the case this policy would have created.

**Measured, so the numbers are not guessed.** `JSON.parse` of a real 24.7 MB transcript takes **43 ms** in
V8, and `JSON.stringify` **71 ms** — so parse cost is *not* the problem, and a reviewer's prediction that a
>100 ms main-thread freeze would starve socket reads is refuted. On healthy loopback the gate's 5 s
deadline holds comfortably. The failure is entirely in the transfer, and only in the one case where
transfer has stopped. Session sizes on this machine: 24.7 MB largest, 382 sessions totalling 345 MB.

**What eviction gets right that the alternatives do not.**

- **It keeps the rule.** No consumer may be load-bearing. A client that has stopped reading has already
  failed the contract; keeping it attached and attempting a repair in-band spends server memory on a peer
  that is not listening.
- **It is loud.** A close with an explicit application code and a client banner is exactly the
  "loud degradation over silent corruption" this register keeps asking for. A client thrashing between
  rehydrations is the other thing — broken, with nothing saying so.
- **It reuses a path that already exists and is already gated.** Reconnect tears down client state,
  opens a fresh socket, requests the baseline behind [AG-R-20](risks.md#ag-r-20)'s gate, and resumes. There
  is no mid-stream interleaving of deltas and snapshots, and therefore no split-brain state space to
  reason about. **This is what the gate is for, and eviction is what makes the gate's escape hatches
  emergency exits again rather than the common case.**

**A consequence for the sender: the queue is a strict FIFO and nothing in it may be clever.** A reviewer
argued the per-client queue justifies coalescing `streamChunk`, prioritising `permissionRequest` ahead of
backlogged traffic, and shedding low-value events under congestion. All three are forbidden here, and the
reviewer withdrew them when the rules were put to it. Prioritising a `permissionRequest` ahead of the
`toolUse` that motivated it shows a dialog asking about something the user has not been shown —
the same causal inversion [AG-R-19](risks.md#ag-r-19) already corrects on the engine side. Coalescing is
only lossless if every reducer is append-only, which is unverified. Dropping is silent loss by
definition. **The sender earns its place on transport grounds alone** — Probe B below — and does not need
queue acrobatics to justify itself.

**Also settled: the acknowledgement goes, rather than being bounded.** Server-to-client events become
JSON-RPC *notifications* — no `id`, no reply, no future, and no 120 s `timeout_handler` task per event.
Nothing reads the result today, delivery confirmation at the application layer confirms only that a
browser's event loop ran, and liveness is already available twice over: the websocket's own ping/pong, and
the write-buffer depth that Probe B shows flags a stalled peer within ~19 frames.

**Built 2026-09-11, in `src/aic_dc/broadcast.py`.** `ClientSender._evict` warns with the client's queue
depth and byte count, closes with `CLOSE_CODE_QUEUE_OVERFLOW` (4001, private-use range — `collab` already
uses 1008 for a denied admission, and this is a different thing) and drops the backlog. The webapp's
`remoteDisconnected` already calls `_scheduleReconnect`, so the reconnect path this decision relies on
needed no change: the evicted client comes back, asks for `get_current_state`, and applies it behind
[AG-R-20](risks.md#ag-r-20)'s gate.

One thing the eviction path had to learn the hard way. `await ws.close()` on a peer that is not reading
its socket hangs for the same reason `ws.send` does — the close *handshake* waits for a reply the peer
will not send. Evicting by awaiting a close would therefore have traded one stuck task for another. The
close frame goes out under a two-second `wait_for`, and the transport is aborted when that expires.

The strict-FIFO consequence is enforced by construction rather than by a test: the queue is a `deque` and
the only operations on it are `append` and `popleft`. There is nowhere for a coalescer or a priority to
live, which is the point.
