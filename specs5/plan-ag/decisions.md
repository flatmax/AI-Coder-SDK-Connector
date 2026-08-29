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
