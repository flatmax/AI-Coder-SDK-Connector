# Antigravity Decisions

Binding choices for adding Google's Antigravity as a second agent backend. Each has an ID (`AG-n`),
the decision, and why. Specs written for this work assume every decision here.

Decisions marked **(user)** were made by the project owner directly and are not open for
re-litigation during implementation.

Where a decision here rests on a measurement, the measurement is in
[`sdk-surface.md`](sdk-surface.md) and this file points at it rather than restating it.

---

## AG-1 — Two engines, one master per session **(user)**

AIC⚡DC gains a second agent backend. Exactly **one engine is master at a time**, chosen per session;
the other is reachable as a *consultant* — a one-shot call for a second opinion or for a capability
the master lacks. Both directions must work: Claude-as-master consulting Antigravity, and
Antigravity-as-master consulting Claude.

**Why it matters:** the two engines have genuinely disjoint capabilities. Google offers image
generation, which Anthropic does not; Claude Code offers a mature permission and transcript surface
that Antigravity at 0.1.15 does not. The point of hosting both is to use each for what it is good at,
not to average them.

**Consequence:** every surface downstream of the engine — chat rendering, the Context tab, the HUD,
history, settings — must tolerate two shapes. That cost is real and is accepted deliberately; it is
the price of the capability, not an oversight. [AG-9](#ag-9) is what keeps it from being paid twice.

**What this does *not* authorise:** both engines live in one session, editing one working tree
concurrently. See [`risks.md` AG-R-7](risks.md#ag-r-7) — that is a different feature with an
architectural blocker, and nothing here should be built in a way that assumes it is coming.

---

## AG-2 — The Python SDK is the engine; `agy` is not

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

**A third reason, not needed for the decision but recorded because it bears on any future
reconsideration:** Antigravity's terms state *"Using third party software, tools, or services to
access the Service … is a breach of this Agreement."* Whether a host driving `agy` as a subprocess
falls under that clause is genuinely unresolved — `agy` itself, asked directly, answered "I don't
know". AG-2 makes the question moot by not driving `agy` at all.

The Python SDK's `Step` carries content, thinking, deltas, full tool calls and per-step usage, and
`PostToolCallHook` receives a `ToolResult`. The data exists there.

**Consequence:** a Gemini API key or Vertex project is **mandatory, not conditional** — `agy` was the
path that would have inherited the owner's existing login, and it is closed. This is a procurement
gate on everything past phase 1.

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

---

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

## AG-8 — The surface probe is built in phase 1, not later

`src/aic_dc/antigravity/surface.py` and its test gate land with the consultant, before any engine
work.

**Why it matters:** `google-antigravity` is **0.1.15 and alpha**. The equivalent probe for
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

---

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
  It maps onto `stop_task`, which is why that method is in the `subagent_tabs` surface.
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
