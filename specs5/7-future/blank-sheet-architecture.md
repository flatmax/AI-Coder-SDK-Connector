# Blank-sheet architecture — the concepts, if nothing were already built

**Speculative, per this layer.** Three rounds through
`mcp__aic-dc-antigravity__second_opinion`, driven by the same convergence protocol as its companion
([`.claude/skills/consult-agy`](../../.claude/skills/consult-agy/SKILL.md)): each round carries the
prior exchange forward as third-party evidence, and no claim is accepted until it is checked against
the tree. The reviewer declared the position converged at the end of round 3 with no open
disagreements, which is one fewer than last time.

**Revisited on 2026-09-11**, two rounds further, on the three things the migration order below had left
unanswered: how a Claude consultant becomes reachable from `agy` at all, whether the two consultation
features earn two code paths, and whether step 5 was worth doing. Those rounds cost the maintainer the
first question outright, cost step 5 four-fifths of its estimate, and closed two of the four items under
[§ Still open](#still-open-honestly). Changes are marked in place; the narrative and what each side got
wrong is in [`../plan-ag/delivery.md`](../plan-ag/delivery.md#the-consultation-that-cost-a-step-and-a-half-and-one-it-moved-into-this-directory-2026-09-11).

This is the companion to [`rearchitecture-review.md`](rearchitecture-review.md) and asks a different
question. That review asked *what would we rebuild*, and answered *no rewrite*. This one strips the
design back to no classes at all and asks what concepts have to exist — with one constraint made
explicit up front, which is the reason the exercise was worth running:

> Both `agy` and `antigravity` can be the **master** engine and can be the **consultant**. `claude` can
> only be the master, and there is no consultant path for it at all. Design as if that asymmetry were
> not a fact about the code, because it is not a fact about the domain.

Two of the five changes at the end are ordinary refactors of shipped code and stand on their own
merits; the concept set is what this layer is for, and is not a commitment.

## What the exercise deleted

Three concepts that exist today did not survive the blank sheet, and neither side defended them:

| deleted | why |
|---|---|
| **Consultant** | Not a kind of thing. A consultation is an ordinary invocation whose approvals resolve headlessly. Modelling it as a type is what made a Claude consultant look like an extraction project instead of a configuration. |
| **Child session** | A consultation has no history, no resumption and no second turn. Giving it a session gave it a tab, a lifecycle and a write lock it had no use for — and the tab lifecycle is what the shipped defect was entangled with. |
| **Master** | A role, held by whichever invocation currently owns the session write lock. Not a class, and not a property of an engine. |

## The conflation underneath the shipped defect

The consultant's best contribution, and it is structural rather than stylistic: **model-to-model and
user-to-model are two domain features that share one execution primitive**, and the code had them
sharing one *mechanism*. The two have different consumers — a user consultation's output is destined
for a human eye, a model consultation's output is destined for a tool result — and when one mechanism
served both, the presentation consumer's condition ("is there anywhere to draw?") truncated the data
consumer's payload. That is the empty consultation tab in
[`../known-issues.md`](../known-issues.md) § *An empty consultation tab*, stated as a design fault rather
than as a bug: the fix that shipped repairs the symptom, and this is the argument for the seam.

## The five concepts

### Workspace
Defines the spatial boundary: repository files, project configuration, persistence roots, and the
translation from relative to absolute paths.

> **Invariant.** A Workspace is spatial context only. It never holds execution locks, event streams or
> driver processes.

### Session
The linear, user-visible history of dialogue turns, and the owner of the turn serialisation lock.

> **Invariant.** At most one Invocation holds a Session's write lock at a time, and session state
> transitions are serial.

### Invocation
The atomic unit of model execution. Carries attribution — `id`, nullable `session_id`, `causation_id`
naming the parent invocation, and `initiator: User | Agent` — a declarative `IsolationRequirement`
(tool policy, credential scope, network policy), and lifecycle state.

> **Invariant.** An Invocation's lifecycle and its result are independent of the presence, count,
> latency, failure or absence of any attached EventSink.

`session_id` is nullable because a consultation has no session; `causation_id` is what makes it
attributable anyway. **The private config root does not belong here** — that was a maintainer proposal
and it was wrong, because it leaks a process-layer mechanism into a domain concept. The Invocation
declares *what isolation it requires*; the driver translates that into its own native mechanism, which
is `HOME` for `agy` ([AG-21](../plan-ag/decisions.md#ag-21)), `CLAUDE_CONFIG_DIR` for the Claude CLI,
and in-process options for the SDK engines.

### ApprovalResolver
Authorises privileged operations — tool execution, credential use, network access — either
interactively against a user surface or headlessly against a static policy.

> **Invariant.** Execution fails closed. No driver executes a tool without an explicit grant.

This is the concept that makes "consultant" disappear, and it is also the general form of the `agy`
gate: a hook that denies everything is a headless resolver that happens to live in another process.
That the same concept covers a permission dialog, a deny-all consultation and a `PreToolUse` hook is
the strongest evidence the concept is real.

### EventSink
Fire-and-forget projection of read-only frames — text, thinking, status, heartbeat — to any number of
listeners: a websocket, a tab, an inline card, a metrics counter, a log.

> **Invariant.** A sink is read-only and downstream. Backpressure, lag, disconnection or an exception
> in a sink can never block, mutate or abort the Invocation feeding it.

## The rule that replaced "never render a tab"

The consultant's round-2 position was that a model-to-model consultation should *never* render a UI
tab — tabs imply session semantics, the tab lifecycle caused the defect, and popping tabs for
sub-queries thrashes the UI. The third of those is a good argument and an inline tool card in the
master timeline is probably the better default surface. The first two do not hold, and the consultant
withdrew them:

- **The tab was not the defect; the load-bearing sink was.** Once the sink is a pure observer and the
  result resolves from an independent future, the number and kind of attached sinks is architecturally
  irrelevant. "Never a tab" is then a preference, not a requirement.
- **"Tabs imply session semantics" is a convention claim, and false here.** The consultation tab has no
  input box, accepts no second turn, and offers only Stop. It is a view.
- **The human watching is load-bearing epistemically.** The consultant itself named *premise capture*
  as the residual risk of a text-only oracle: a blind reviewer reasons only over the framing it is
  handed, and neither model can see the framing from inside — which is exactly the failure recorded as
  the third reversal in [`rearchitecture-review.md`](rearchitecture-review.md#a-third-reversal-one-day-later).
  The mitigation is a human reading the stream. Concretely, the empty-tab defect was found because the
  maintainer was watching that tab; no test, log line or health check caught it, and the health check
  reported healthy.

So the rule is not about tabs:

> **No sink may be load-bearing. Every sink is an observer, and the result never depends on one.**

### The one failure mode that survives it

Sink detachment re-coupled to execution cancellation. In most UI conventions closing a view tears down
the work behind it, so a tab close handler that calls `abort()` reintroduces the whole defect class
through the front door.

> **Invariant.** Detaching a sink is non-destructive. Closing, hiding or navigating away from a
> consultation surface unsubscribes an observer and never aborts the Invocation. Halting requires an
> explicit cancellation routed to the invocation's cancellation token.

## Measurements, and the refutations they produced

Nothing below was taken on either side's authority, and that is the point of the table rather than a
caveat on it. Across these rounds measurement overturned four of the consultant's claims and three of
mine — plus one, in the risk section below, that we had *agreed* on. Agreement between two models is not
evidence; it was the strongest-held claim here and it was still wrong.

| claim | verdict |
|---|---|
| Reach Claude as a consultant through the raw `anthropic.messages.create()` wire API | **Refuted, on credentials.** Claude runs here on a consumer OAuth subscription through the vendor CLI — the account panel reads the CLI's own credential file and calls `api.anthropic.com/api/oauth/usage`. An API key would bill a different account, or not exist. |
| A Claude consultant means extracting from an 18.2k-line module | **Refuted.** `claude_agent_sdk.query(*, prompt, options, transport)` is a one-shot exported function over the same CLI transport, therefore the same subscription credential. `disallowed_tools`, `can_use_tool`, `permission_mode`, `max_turns`, `system_prompt`, `model`, `setting_sources` are all present on `ClaudeAgentOptions`. |
| `can_use_tool=lambda *_: False` denies tools | **Refuted, three ways.** `CanUseTool = Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResult]]` — it is async, takes three arguments, and must return `PermissionResultAllow | PermissionResultDeny`. A returned bool raises `TypeError` in `_internal/query.py`. |
| `disallowed_tools=["*"]` blocks everything | **Unverified and unlikely.** The field is a plain `list[str]` with no documented globbing; `"*"` most plausibly matches a literal tool of that name. Not a substitute for a resolver. |
| `behavior` must be passed to `PermissionResultDeny` | **Refuted.** It defaults to `"deny"`; `PermissionResultDeny(message=…)` constructs. |
| `interrupt=True` is right for a one-shot consultant | **Refuted — this was the maintainer's claim, and the consultant caught it.** `interrupt` is forwarded to the CLI as a control field, and `terminal_reason` documents `"aborted_tools"` as *the turn was cancelled via an `interrupt` control request*. A consultant denied with `interrupt=True` plausibly ends the turn before it can answer in prose. Use `interrupt=False` and let the denial arrive as an ordinary tool rejection. |
| `max_turns=1` suffices for a one-shot consultation | **Rejected as a risk.** A turn is a user message plus an assistant response, and it is forwarded verbatim as `--max-turns`. A denied tool call consumes a turn, so `max_turns=1` can terminate with `terminal_reason="max_turns"` and no prose. Leave it unset or above 2. |

The correct deny shape, matching [`permissions.py:1375`](../../src/aic_dc/claude_code/permissions.py):

```python
async def deny_all(tool_name, args, ctx):
    return PermissionResultDeny(
        message="This is a one-shot consultation. Answer in prose; do not call tools.",
        interrupt=False,
    )
```

`max_budget_usd` is also on `ClaudeAgentOptions` and is the honest guard for a delegated call, with the
caveat already recorded in [R-6](../plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap) that
subscription billing reports no cost.

## The largest remaining risk

**Two concurrent refreshes spending one single-use refresh token.** The consultant raised this
unprompted and it is real — but its framing was wrong, and so was my acceptance of it. Both of us
described three processes each refreshing the credential on a 401. That is not what this code does, and
the difference matters because it turns an architectural problem into a small lock.

What is already true, and was built deliberately:

| consumer | what it does with the credential |
|---|---|
| The account panel | **Reads only.** `account_usage.py` states it outright — *"Nothing is written, and the refresh token is never used"* — because refreshing from there "would rotate the token out from under the CLI copy and could lock the user out of their own editor". An expired token is reported as `REASON_EXPIRED`, not repaired. |
| The CLI child | **Cannot refresh.** The SDK materialises a temporary `CLAUDE_CONFIG_DIR` with `claudeAiOauth.refreshToken` removed, and `token_refresh.propagate()` copies in an access token only. The parent is the sole holder of a refresh token, by design. |
| The parent | Refreshes, via `token_refresh.ensure_fresh()` — and not by redeeming the token itself. It spawns `claude` against the real config dir and lets the CLI do it. |

So the reviewer's proposed mitigation — make one writer authoritative — is not new work. It shipped in
[`token_refresh.py`](../../src/aic_dc/claude_code/token_refresh.py), whose opening paragraph names the
hazard in the same terms: *"a single-use refresh token spent under a redirected config dir would rotate
the parent's stored credentials out from under it and lock the user out."* That also settles what I had
recorded here as unverified: **the refresh token is single-use for this provider.** It is not an
assumption from the OAuth standard; it is why the SDK redacts it.

What is genuinely unguarded is narrower. `ensure_fresh()` has no lock — no `asyncio.Lock`, no
`fcntl.flock`. It short-circuits on `needs_refresh()`, so it only acts inside the 15-minute
`REFRESH_MARGIN_SECONDS` window, but two callers arriving in that window both see the same answer and
both spawn a rung. The second redeems a token the first has already replaced. Callers are not
hypothetical: there is a connect-time pre-flight *and* a watchdog **per session**, sessions are
per-repo, so *n* open repos give 2*n* callers on one file — and a Claude consultant adds one more per
consultation, which is what makes this the risk that gates step 2 rather than a pre-existing curiosity.

Two things follow, in order. **Serialise `ensure_fresh()`** — an in-process lock is most of the value
for a few lines, and it is correct whatever the server does on replay. Then accept the part no lock can
reach: **the user's own terminal `claude` is a consumer we cannot coordinate with**, so the failure has
to be survivable rather than merely unlikely. `LOGIN_REQUIRED_DETAIL` already exists as the honest
terminal state, and the tripwire belongs on the observable the user would otherwise meet as a mystery.

The one claim here still inferred rather than measured is the *severity*. Single-use is established; whether
redeeming a superseded token merely fails or trips replay detection and revokes the whole grant is not.
The mitigation does not depend on knowing.

**This one does not stay in this layer.** It is a hazard in shipped code, not in a speculative design,
and nothing may depend on layer 7 — so it has graduated to
[R-14](../plan/risks.md#r-14--two-refreshes-race-for-one-single-use-refresh-token), with a tripwire. What
stays here is only the part that is about the *architecture*: a consultation adds a credential consumer,
which is why the lock is a prerequisite of step 2 rather than a tidy-up after it.

## Migration order

Not scheduled. Ordered so that each step is provable before the next depends on it, against this
repo's ~1.1:1 test-to-source ratio.

**Costed on 2026-09-11**, after a two-round consultation on the three questions this order left open —
see [`../plan-ag/delivery.md` § The consultation that cost a step and a half](../plan-ag/delivery.md#the-consultation-that-cost-a-step-and-a-half-and-one-it-moved-into-this-directory-2026-09-11).
The figures are days of work against the 15,475 source lines of the two engine packages, with tests at
the repo's measured 1.114:1. **Two of the five moved, and both moved down**; the ordering did not
change.

| step | size | what the costing turned on |
|---|---|---|
| 1 — every sink an observer | ~~2–3 d~~ 4–6 d | The shipped bridge fix is two-thirds of the test set already. **Consultation half done 2026-09-11**; the master-turn half is [AG-R-19](../plan-ag/risks.md#ag-r-19), and **revised upward the same day** when consulting it added [AG-R-20](../plan-ag/risks.md#ag-r-20) as a prerequisite |
| 2 — a Claude consultant through `query()` | 3–5 d | Includes [R-14](../plan/risks.md#r-14--two-refreshes-race-for-one-single-use-refresh-token)'s lock, which it must not ship without |
| 3 — inline rendering as the default surface | 3–5 d | Webapp work, and the read-only tab it replaces already exists |
| 4 — reach the consultant from `agy` | 4–6 d | **Down.** A spawned stdio server and a third credential holder were both deleted by measurement — see below |
| 5 — formalise `ApprovalResolver` | 1–2 d | **Down hard, from 1–2 weeks.** Its ~3,000-line premise was refuted by the import graph |

≈3–5k lines including tests, ~2–3 weeks in total, where **step 5 alone had been informally carrying
roughly 40% of the estimate**. A costing exercise that only ever adds is not measuring.

1. **Make every sink an observer.** Separate result resolution from frame dispatch: the driver feeds an
   accumulator that fulfils the result, and dispatch to sinks is non-blocking and exception-isolated.
   *Must not break:* in-order websocket delivery; explicit cancellation still terminating the driver and
   emitting `INTERRUPTED`; driver errors still failing the result regardless of sink health.
   *New tests:* a sink that raises on every frame, and a sink that sleeps — the result completes in both.
   A zero-sink invocation returns its full text. The shipped fix
   ([`test_antigravity_bridge.py`](../../tests/test_antigravity_bridge.py) § `TestTheTabAndTheAnswerAgree`)
   is the first two-thirds of this test set already.

   **Built on the consultation path, 2026-09-11** —
   [`../plan-ag/delivery.md` § The sink stops deciding what the answer is](../plan-ag/delivery.md#the-sink-stops-deciding-what-the-answer-is-2026-09-11).
   `_tab` no longer yields `None` with no browser attached, so `AgyConsultant._run`'s per-frame choice
   between `observer(frame)` and `translator.translate(frame)` is gone; every emit is scheduled and every
   wait is bounded. The three named tests exist, plus two more, and four of the five fail against the
   shipped source.

   **Not built on the master turn**, where the same fault is one layer out: `_dispatch` awaits every
   connected browser through an `asyncio.gather` and sequences engine bookkeeping behind the broadcast.
   Measured and graduated to [AG-R-19](../plan-ag/risks.md#ag-r-19), because it is a hazard in shipped
   code and nothing may depend on this layer. **The bridge's fix is the wrong fix there** — the master
   path has the in-order contract this step names as a must-not-break, so it wants a serialised sender
   per client rather than a bounded wait. The mechanism is specified in AG-R-19 and is deliberately not
   restated here; **it changed on 2026-09-11** after a consultation refuted part of it, which is the
   argument for keeping mechanism out of this layer.

   **The estimate above no longer covers it.** Step 1 was costed at 2–3 d when this half looked like a
   queue; it has since acquired a prerequisite ([AG-R-20](../plan-ag/risks.md#ag-r-20), a webapp change
   rather than a server one) and a shape with a cursor in it. Call the remainder 3–4 d and the step 4–6 d
   in total. Recorded rather than quietly absorbed: *a costing exercise that only ever adds is not
   measuring*, and one that never adds is not either.
2. **A Claude consultant through `query()`.** Headless resolver, `max_turns` above 2, and
   `token_refresh.ensure_fresh()` serialised first — a consultation adds a credential consumer, so the
   lock is a prerequisite rather than a follow-up. *Must not break:* the master engine, and no mutation
   of shared CLI environment state. This is the step that ends the asymmetry the exercise was framed
   around.
3. **Inline rendering as the default consultation surface**, with a full streaming view available and
   detachment non-destructive.
4. **Provision `agy`'s MCP root as a build step.** It has no MCP servers configured today, so this is
   construction, not configuration — the correction that turned a claimed half-day into a build task.
   **The mechanism has since left this layer**: `agy mcp add` documents `--type http` with repeatable
   `--header`, so this process exposes a second authenticated listener and writes a per-spawn config
   into [AG-21](../plan-ag/decisions.md#ag-21)'s private root, and the bearer token carries the
   workspace so the model is never asked which repository it is in. Specified as
   [AG-22](../plan-ag/decisions.md#ag-22). What is refuted, and stays here as the finding: the
   maintainer's design of a shell command advertised in a preamble, reached through `run_command`, with
   the port passed in `agy`'s environment — five objections, of which the fatal one is that it has
   nowhere to put a credential.
5. **Formalise ApprovalResolver and the policy tiers**, folding the `agy` hook in as a headless resolver
   and modelling containment-unavailable as `absent` with a reason, per the existing three-way
   descriptor. **Its size was refuted.** The claim that this unifies ~4k lines of duplicated
   authorisation was, in the reviewer's own words once the import graph was put in front of it, *"an
   illusion created by summing the total line counts of an import spine"*: there is one spine —
   `agy/gate_server.py` → `antigravity/permissions.py` → `claude_code/permissions.py` — and
   `PermissionBroker.can_use_tool` evaluates no policy at all beyond one early return for this app's own
   read-only tools. The vendor CLI decides what to ask about; the broker renders, broadcasts, awaits and
   times out. Two pure functions are worth isolating, and the concept above is still real — the
   *savings* were not.

## Still open, honestly

**Two of these four closed on 2026-09-11** and are struck through rather than deleted, because in both
cases the resolution says something the open question did not: one was right for a better reason than
the one offered, and the other was posed in a vocabulary that had the answer excluded. Both that closed
were settled by reading the tree; **both that remain need a live call to settle**, and they differ only
in what the call costs — one is a single consultation, the other risks the login.

- ~~**Whether a user can type into a consultation tab.**~~ **Closed by measurement, 2026-09-11: they
  cannot, and the assertion was right for a better reason than the one given here.** It is not an
  emergent property of the UI — it is set deliberately in three places.
  [`subagent-tabs.js:318`](../../webapp/src/chat-panel/subagent-tabs.js) marks the tab
  `readOnly` on creation; [`rendering.js:222`](../../webapp/src/chat-panel/rendering.js) reads that off
  the active tab and line 286 renders a read-only note **instead of** the input surface, so there is no
  element to type into; and [`input.js:155`](../../webapp/src/chat-panel/input.js) still guards the send
  path and toasts if it is ever reached. The support for rejecting "tabs imply session semantics"
  therefore holds: the consultation surface is a view because it was built as one.
- **What the auth server does on a replayed refresh token.** That the token is single-use is
  established from this repo's own prior work; whether a superseded redemption fails softly or revokes
  the grant is not, and it is the difference between a retry and an interactive re-login. Not worth a
  deliberate experiment — losing the login to find out is the whole failure — so it stays open until it
  is observed or documented.
- **`interrupt=True` and `max_turns=1` behaviour** are reasoned from the SDK source and the CLI flag,
  not observed. Both are cheap to settle with one live consultation each, and step 2 should settle them
  before shipping rather than after.
- ~~**Whether the two-features split earns two code paths or one path with two configurations.**~~
  **Closed, 2026-09-11 — and the question's own vocabulary was the wrong half of it.** The answer is
  *one execution pump with two coordinators*, which is neither of the two options as posed; "one path
  with two configurations" invites the `if not config.is_consultation:` branches that the reviewer
  predicted would accumulate. Those branches are absent, because `agy/consultant.py` already drives
  `AgySession` as a separate coordinator and installs containment as a `StaticPolicy` object rather than
  testing for a mode inline. **So step 1 is an invariant to enforce, not a structure to build** — which
  is why it costs 2–3 days above and not a week. What separate coordinators do *not* prevent, and what
  step 1's tests are therefore for: cancellation propagating from a detaching sink into the pump, the
  accumulator resolving on abnormal engine exit, and shared conversation-id or brain-tree state bleeding
  master history into a consultation. The third of those is now
  [AG-R-18](../plan-ag/risks.md#ag-r-18) and is a live defect rather than a design concern.
