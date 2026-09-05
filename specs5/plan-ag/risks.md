# Antigravity Risk Register

Each risk has an ID, a description, a mitigation, and a **tripwire** — the observable that tells us
the risk has fired. A risk without a tripwire is a worry, not a managed risk.

`AG-R-1`, `AG-R-3` and `AG-R-11` were found by measurement on 2026-08-30 rather than by reasoning; the
captures are in [`sdk-surface.md`](sdk-surface.md). The rest follow from the surface read.

**`AG-R-1` is retired** — it was measured on 2026-08-30 and did not materialise. It is kept rather
than deleted because its tripwire is still wanted as a regression test, and because the same
measurement raised **`AG-R-11`**, which is live, critical, and was not predicted by anyone.

---

## AG-R-1 — The permission dialog may not be able to render a diff

**Severity: critical. Likelihood: RETIRED — measured 2026-08-30, the risk did not materialise.**

> **Outcome: the gate passed.** A `PreToolCallDecideHook` receives the full proposed content —
> `edit_file` carries `TargetContent`, `ReplacementContent` and a line range; `create_file` carries
> `CodeContent`. `allow=False` leaves the file byte-identical on disk. Captures and the probe command
> are in [`sdk-surface.md` § The permission gate — measured, and it passes](sdk-surface.md#the-permission-gate--measured-and-it-passes);
> the probe is `scripts/probe_edit_args.py`. **None of the fallbacks below were needed.** The tripwire stands
> and should be kept — it is now a regression test, not a gate.
>
> The measurement did expose a different failure of the same mechanism, which is live and unmitigated:
> [AG-R-11](#ag-r-11).

The original assessment is kept below because the reasoning is still what justifies the tripwire.

The permission dialog's value is that it shows the *proposed edit* as a rendered diff before the user
approves it. That requires the file content to be present in the tool call at decision time.
`ToolCall.args` is untyped (`types.py:642-663`), so reflection cannot answer whether
`create_file`/`edit_file` carry content or only a path.

**Why it bites:** `agy`'s stream omits exactly this — a `write_to_file` frame names `TargetFile` and
nothing else ([`sdk-surface.md`](sdk-surface.md#why-agy-is-nonetheless-not-the-engine)). If the SDK's
hook has the same shape, the dialog degrades to "approve a write to this path, sight unseen", which
is a `y/n` on a filename. That is not the product's permission UX; it is the thing the product exists
to replace. And the failure is *late* — it surfaces only when someone tries to build the dialog,
after the engine adapter is written.

**Mitigation:** measure it in phase 2, before any engine work, as an explicit gate with a
go/no-go. It is free to measure — a `PreToolCallDecideHook` fires **before** the model call, so a
hook that logs its `ToolCall` and denies costs no tokens and no quota. If content is absent, the
fallbacks in descending order are: read the target file from disk and diff against the proposed
`modified_args`; render a path-and-tool-name dialog with an explicit "content not available from this
engine" banner; or decline to ship Antigravity as master for write operations.

**Tripwire:** a phase-2 test asserting the hook's `ToolCall.args` contains a content-bearing key for
each write tool. It runs against the installed SDK with no network. If it goes red after an SDK bump,
the dialog has silently lost its diff — which otherwise presents as users approving faster.

---

## AG-R-2 — `google-antigravity` is 0.1.x and alpha

**Severity: high. Likelihood: certain — the classifier says so.**

`Development Status :: 3 - Alpha`. `claude-agent-sdk` was at 0.2.137 and stable when the first engine
was written, and its surface still moved enough to justify a machine-checked probe.

**Why it bites:** an alpha SDK breaks by renaming, not by erroring. A `Step` field that becomes
optional, a `HookResult` that grows a required key, a policy builder that changes its default — none
of these arrive as a build break. They arrive as a feature that silently stops working, which is the
failure mode the Claude probe was built to close and the reason it is not optional here.

**Mitigation:** [`decisions.md` AG-8](decisions.md#ag-8) — the probe lands in phase 1, before the
engine. Pin the version in `pyproject.toml` rather than floating it, and treat a bump as a task with
a red gate attached rather than a dependency refresh.

**Tripwire:** the probe's `unclassified` bucket non-empty. A name the SDK exposes that appears in
none of `handled` / `declined` / `pending` is the only state that means the package moved and nobody
looked.

---

## AG-R-3 — A write can be silently diverted out of the repo

**Severity: high. Likelihood: certain on an unconfigured machine — observed 2026-08-30.**

Measured, not hypothesised: `agy` was asked to create a file in the current directory and wrote it to
`~/.gemini/antigravity-cli/scratch/` instead, reporting success with a `file://` link. The cause was
`trustedWorkspaces` in the CLI's `settings.json` not listing the working directory.

> **Amended 2026-09-05 — the stated cause is wrong, the risk is not, and the real trigger is
> unknown.** Three phase-8 probe runs diverted a write from **inside** `/tmp/temp`, which *is* in
> `trustedWorkspaces`, on `agy` 1.1.26. So the trust list is not a sufficient explanation.
>
> Two replacement explanations were then tried and **both failed**, which is why this amendment offers
> none:
>
> 1. *"The workspace must be a git repository."* `git init`-ing the probe workspace changed nothing.
> 2. *"Newly-created files divert; edits land."* This fitted every observation available — every
>    diverted file on record was a creation (`probe.txt`, `hello.txt`, `test_hello_world.py`,
>    `stranger.txt`), and the write that landed was an edit. `scripts/probe_agy_write_target.py` was
>    written to isolate exactly that: **one session, one workspace, one turn**, asked to edit an
>    existing file *and* create a new one. **Both landed.** Disproven.
>
> The largest untested difference between the diverting runs and the clean ones is that the diverting
> ones had **two `agy` processes running concurrently**. That is a candidate and nothing more.
>
> **The consequence for the mitigation is the point, and it survives not knowing the cause.** AG-10's
> health check asserts *"the repo root is a workspace the engine will write to"*. A check phrased
> against `trustedWorkspaces` would pass on a machine where writes divert anyway — measured here. It
> must assert the **outcome**: write a sentinel, then `stat` it at the path it was asked for. That is
> the one form of the check that does not depend on a mechanism nobody has pinned down. See
> [`delivery.md` § The trusted workspace was not the whole story](delivery.md#the-trusted-workspace-was-not-the-whole-story-and-two-explanations-that-were-wrong).

**Mitigated 2026-09-05, at detection rather than at startup — and the reason is that the specified
mitigation cannot be built honestly.** The startup check above has to assert an *outcome*, and the
only thing that produces an outcome is a real write, which costs a turn on the user's subscription
every time the app starts. So the check was moved to where it is free: `agy/steps.py` inspects every
completed call in the write seam, and a target that is **missing here** while a file of that name sits
in `~/.gemini/antigravity-cli/scratch/` is reported as a `systemEvent` naming both paths.

Deliberately narrow. "The file is missing" alone has innocent explanations — the model naming a path
it never created, a tool that failed for an unrelated reason — and a false alarm about a write that
did land would be worse than the silence it replaces. The pair has no innocent reading.

The notice says the edit is **not lost** and names the file holding it, because a user told only "the
file is not there" would redo work that has already been done. And it sits *beside* the tool card
rather than rewriting it: `agy` reported success, the card says so, and the two disagreeing is exactly
the information the user needs.

This does not close the risk — a diverted write still happens, and nothing prevents it. What changes is
that it stops being undiagnosable, which was the whole of the severity.

**Why it bites:** it does not error. The agent believes it succeeded, the transcript says it
succeeded, and the file tree and diff viewer — both rooted at the repo — show nothing. The user's
reading is "the agent lied about editing my file", and the actual cause is in a settings file in
another product's config directory. There is no diagnostic path from the symptom to the cause.

**Mitigation:** workspace containment becomes a **startup health check** with a visible failure, beside
the existing CLI-version gate ([`decisions.md` AG-10](decisions.md#ag-10)). The check asserts the repo
root is a workspace the engine will actually write to, and reports a degradation into the health
banner if it is not.

**Settled in phase 1 (2026-08-31): the SDK is not subject to the trust list.** The tripwire below was
run — `scripts/probe_consultant.py` § *AG-R-3: workspace containment* — on the same machine whose
`trustedWorkspaces` diverted `agy`. A `create_file` turn with `workspaces=[repo_root]` wrote
`aic-dc-sentinel.txt` and it was found by `stat` **at the expected absolute path** in this repository,
not under `~/.gemini/`. The two mechanisms are separate, as the `hooks/policy.py` reading suggested
but did not confirm.

**The risk is downgraded, not retired. Severity: medium. Likelihood: low.** What was measured is that
the SDK honours `workspaces` on *one* machine, on 0.1.15, for `create_file`. The failure mode — a
write reported as successful that the file tree cannot see — remains undiagnosable from the symptom,
and the product it was measured in ships roughly daily. AG-10's startup health check stands for that
reason: it costs one `stat` and it is the difference between a bug report saying "the agent lied"
and one naming a settings file.

**Note for whoever reads this next:** the check deliberately does not use `generate_image`. Every
Gemini image model returns `limit: 0` on a free-tier key, so folding containment into the image call
made a settled question look blocked on billing. Containment is a question about `workspaces`, and
the cheapest tool that asks it is the right one.

**Tripwire:** the sentinel write, at the expected absolute path, asserted by `stat` rather than by
the tool's own report. A test that trusts the report cannot catch this.

---

## AG-R-4 — Two engines double every downstream surface

**Severity: high. Likelihood: certain — it is the shape of the work.**

Chat rendering, the Context tab, the HUD, history, settings and the permission dialog all currently
have exactly one shape to support. After [AG-1](decisions.md#ag-1) they have two, permanently.

**Why it bites:** the cost is not in the engine adapter, which is bounded and writable. It is that
every future feature costs twice, and that the second cost is invisible at design time — a change
lands, works on the engine the author was using, and is discovered broken on the other one by a user.
The asymmetry makes it worse: the Claude path has thousands of tests and years of live use, the
Antigravity path will have neither for a long time.

**Mitigation:** [AG-3](decisions.md#ag-3) and [AG-9](decisions.md#ag-9) together — one namespace so
call sites do not fork, and a capability descriptor so a missing surface is *declared* rather than
discovered. The descriptor is the mechanism that turns "does this work on the other engine?" from a
question requiring a live session into a lookup.

**Tripwire:** a surface that reads engine state without consulting the descriptor. Enforceable as a
test: every capability key in the descriptor has at least one reader, and every per-engine branch in
the webapp keys off the descriptor rather than off an engine name string. A branch on
`engine === 'claude'` is the observable that the seam has leaked.

---

## AG-R-5 — The Context tab has nothing to draw

**Severity: medium. Likelihood: certain — verified absent.**

Antigravity exposes no context-window read-back. `compaction_threshold` is a value you set
(`types.py:436`), not a window you can query. Claude's `get_context_usage` — a pass-through of what
`/context` prints, shared by three webapp readers through `webapp/src/context-usage.js` — has no
counterpart.

**Why it bites:** the Context tab is one of the most-looked-at surfaces in the product and its bar is
the thing that explains a compaction pause. The temptation is to synthesise a bar from
`prompt_token_count` against a model's published window size. That would be a **guess rendered as a
measurement**, and the published window is not necessarily the window the harness is using.

**Mitigation:** hide the bar per [AG-9](decisions.md#ag-9). Surface what *is* real —
`cached_content_token_count` as a cache-hit fraction, and `StepType.COMPACTION` steps from the
transcript, which say a compaction *happened* even though nothing predicts one. `OnCompactionHook`
(`hooks/hooks.py:228`) gives the same before-the-pause signal that `PreCompact` gives on the Claude
side.

**Tripwire:** any code path computing a percentage from a hard-coded or model-derived window size for
this engine. Grep-able, and worth a test: no context percentage may be produced without a
window figure that came from the engine.

---

## AG-R-6 — A hand-maintained price table goes stale silently

**Severity: medium. Likelihood: high, if one is ever written.**

There is no USD figure on either Antigravity surface. The only route to one is a per-model price
table maintained in this repo.

**Why it bites:** a stale price table is worse than no price at all, because a number on screen is
believed and a missing number prompts a question. It fails in the direction where nobody
investigates: prices move down more often than up, so the displayed figure is quietly high and reads
as conservative rather than wrong.

**Mitigation:** [AG-6](decisions.md#ag-6) — do not write one. Report tokens, and offer `BudgetConfig`'s
token and call caps as the control instead of a dollar cap.

**Tripwire:** any constant in `src/` mapping a Gemini model name to a price. Its existence is the
tripwire; there is no correct value.

---

## AG-R-7 — Two masters on one working tree

**Severity: high if attempted. Likelihood: low — explicitly out of scope.**

Both engines ship independent file-writing tools with independent checkpoint state, and AIC⚡DC has
one working tree and one `rewind_files` (`src/aic_dc/claude_code/service.py:1703`).

**Why it bites:** interleaved edits from two agents cannot be unwound by either engine's
checkpointing, and the diff viewer cannot attribute a change. It also breaks the session invariant
that `../3-engine/session.md` treats as load-bearing — one client, never silently re-created. The
danger is not that someone builds it deliberately; it is that an "it would be easy to also…" change
makes it reachable, because nothing structurally prevents constructing two sessions.

**Mitigation:** [AG-1](decisions.md#ag-1) scopes it out and [AG-10](decisions.md#ag-10) states the
invariant positively. The consultant pattern gives most of the perceived benefit — ask the other
engine — with none of the concurrency.

**Tripwire:** more than one engine session holding write tools alive at once. Assertable at
construction: a second master session cannot be created while one is live, and the consultant's
config is built from `BuiltinTools.read_only()` so it structurally cannot write.

---

## AG-R-8 — The credential path is separate, and separately billed

**Severity: high. Likelihood: certain — verified.**

The Python SDK accepts only `GeminiAPIEndpoint` or `VertexEndpoint`
(`connections/local/local_connection.py:200-201`). It cannot reuse the `agy` login, which is what
the owner already has.

**Scope, clarified 2026-09-03:** this risk is about the **SDK engine**, not about the account. `agy`
itself does reach the subscription — headless `agy -p` authenticates from the same keyring OAuth and
calls the same Code Assist backend as the interactive TUI, measured. So the exposure is precise: the
engine AIC⚡DC actually runs is billed separately from the subscription the user already pays for, and
the free tier of that separate billing refuses at 20 requests per model per day. The mitigation is a
purchase (billing on the key's Cloud project), not an architecture — see
[`decisions.md` AG-2](decisions.md#ag-2) § *Amended 2026-09-03* for why the alternative was measured
and declined.

**The reason is a backend split, established by measurement on 2026-08-31 and recorded in full at
[`decisions.md` AG-2](decisions.md#ag-2).** `agy` authenticates with the `auth/aicode` scope against
`cloudcode-pa.googleapis.com`, the Code Assist surface where a consumer AI Pro subscription's coding
quota lives; the SDK can only address the AI Studio and Vertex endpoints. This risk was previously
stated as "the SDK contains no OAuth code", which is true but is an argument from absence and
understates it — there is also no *endpoint* to point a token at. Two corollaries worth stating here
because they are what people actually try: signing in to ADC with the subscription's Google account
proves identity and transfers no entitlement, and a bare `LocalAgentConfig()` is not "auth-less" —
it raises `AntigravityValidationError` before the binary is spawned.

**Why it bites:** this looks like an engineering question and is a procurement one. It gates
everything past phase 1, and discovering it late means an engine adapter that cannot be run. It also
changes the cost conversation entirely: the Claude path is a subscription where the marginal cost of
a turn is not a per-token invoice, and the Antigravity path is metered API usage with a measured
floor of ~13,900 input tokens per turn.

**Mitigation:** [AG-7](decisions.md#ag-7) puts the consultant first precisely because it forces this
question to be answered with a real key and a real bill before any engine work is committed to.
Credential resolution reports its *source* in engine health, the way `detect_credentials()` does for
Claude, and predicts nothing it has not looked at.

Once a key exists, [AG-11](decisions.md#ag-11) is where it lives — a `0600` file in the user config
directory, because the SDK reads credentials from environment variables only and an app started from
a desktop launcher has no export to inherit. That does not lower this risk: a key is still mandatory
and still separately billed. It removes the second-order failure only — being authenticated in one
shell and not in the next — which would otherwise look exactly like this risk and be diagnosed as it.

**Tripwire:** an engine start that fails at `validate_endpoint()`. It already raises on the connect
path (`local_connection.py:1241`), so the requirement is that it is caught and reported as a
credential degradation in the health banner rather than as an engine crash.

---

## AG-R-9 — The consultant becomes the engine adapter by accident

**Severity: medium. Likelihood: moderate — this is how phase boundaries usually erode.**

Phase 1 builds a one-shot Antigravity call. Phase 3 builds a streaming engine session. The two share
config construction and credential resolution, and the natural move when phase 3 starts is to extend
the thing that already works.

**Why it bites:** an adapter grown out of a consultant is shaped by the consultant's needs — one
turn, no resume, no permissions, no history. Those are exactly the four things the engine is mostly
made of, so the extension is all cost and no reuse, while *looking* like reuse. The result is an
engine designed around a call pattern it does not have.

**Mitigation:** [AG-7](decisions.md#ag-7) states the boundary. The consultant stays a one-shot
`async with Agent(...)`; streaming, `receive_steps`, resume and the permission hook belong to phase 3
and are written against `Conversation` directly. What legitimately survives is narrow and worth
naming: config construction, credential resolution, and the probe.

**Tripwire:** `receive_steps`, `cancel`, `conversation_id` or a hook registration appearing in the
consultant module. Any of them means the boundary has moved.

### Amended 2026-09-01 — the boundary is redrawn, not crossed

[AG-13](decisions.md#ag-13) makes the consultant stream, which the mitigation above forbids. That is
a deliberate reversal and it needs the reasoning restated, because "the risk register said no and we
did it anyway" is exactly how a register stops being read.

**The risk was never "the consultant streams".** It was the consultant *inventing* session machinery
**ahead of** the engine, and the engine then inheriting a shape built for one turn with no resume, no
permissions and no history. The danger was entirely in the **direction of dependency**: a phase-1
convenience wrapper growing into the thing phase 3 was supposed to design properly.

Phase 3 has since designed it properly. `AntigravitySession`, `StepTranslator` and the permission
gate exist, are tested offline, and were written against `Conversation` directly exactly as this
entry demanded. The consultant now **consumes** them. Nothing about the engine's shape is being
decided by the consultant's needs, because the engine's shape was already decided — which is the
condition this risk was protecting, and it has been met rather than waived.

**The new tripwire, replacing the old one:** a *second implementation* of session machinery in
`consultant.py` or `bridge.py` — its own step loop, its own event vocabulary, its own translator, its
own permission handling. Importing and calling `StepTranslator` is reuse and is the point; writing a
second one beside it means the boundary has moved after all.

The old tripwire is kept as a test in `tests/test_antigravity_consultant.py`, narrowed to the names
that still stand for re-implementation rather than for use. **`cancel` is now expected** — AG-13's
tab offers ⏹ Stop, and `Conversation.cancel()` is what makes it real rather than decorative.

**What is still forbidden, and is the part of AG-7 that does not change:** the consultant does not
grow resume, history, a session store, or a master's RPC surface. It is one question and one answer,
streamed. A consultation that could be resumed is a session, and a session belongs to the engine.

---

## AG-R-10 — A second bundled binary

**Severity: medium. Likelihood: certain if the SDK is a hard dependency.**

`localharness` is 119,721,512 bytes inside the wheel. The bundled `claude` CLI is already ~295 MB and
is the reason packaging is its own phase on the Claude side.

**Why it bites:** it compounds a problem that is already the most likely thing to block a release,
and it does so for a capability many users will not enable. An install that grows by 120 MB to ship a
second engine nobody in that install has credentials for is a bad trade made silently.

**Mitigation:** `google-antigravity` is an **optional dependency**, in an extra rather than the base
install. The engine reports its own absence through the capability descriptor
([AG-9](decisions.md#ag-9)) exactly as it reports a missing surface, so a base install is a
one-engine install with no broken UI rather than an error.

**Tripwire:** base-install size, measured per release. A jump means the extra has leaked into the
default dependency set — which is a `pyproject.toml` edit nobody reviews as a size change.

---

## AG-R-11 — A denied edit is re-attempted through the shell

**Severity: critical. Likelihood: observed — it happened on both probe runs, unprompted.**

Gating `create_file` and `edit_file` does not stop the agent from making the edit. It stops it from
making the edit *that way*. When [`scripts/probe_edit_args.py`](../../scripts/probe_edit_args.py) denied both file tools,
`gemini-3.6-flash` immediately reached for `run_command` with the same intent — `sed -i` on the first
run, an inline `python3 -c "…content.replace(…)…"` on the second. Neither was suggested by the
prompt. Only once `run_command` was denied on the same seam did the seeded file survive the turn.

**Why it bites:** it defeats the permission dialog while appearing to honour it. The user is shown a
diff, clicks *deny*, sees the edit rejected — and the file changes anyway, through a tool card that
looks like an unrelated shell command. That is worse than having no dialog, because it manufactures a
false record of consent. It also breaks the invariant `../3-engine/permissions.md` calls
*every request resolves exactly once*: the request the user answered is not the operation that ran.

This is not an Antigravity defect. It is what a capable agent does when told no, and the same
behaviour should be assumed of Claude Code. It is recorded here because the Antigravity probe is
where it was actually observed.

**Mitigation:** the permission seam is **the set of all mutating tools, not the file tools**. The
hook of [AG-5](decisions.md#ag-5) must gate `run_command` with the same standing as `create_file` and
`edit_file`, and the dialog must be able to say *"this command writes to a file you already declined"*
rather than presenting it as a fresh, unrelated request. Deny-by-default on the shell, as
`policy.confirm_run_command()` already does, is the correct posture and must not be relaxed to
`allow_all()` outside probes.

**Tripwire:** a test that denies an `edit_file` against a seeded file, lets the turn run to
completion, and asserts the file's bytes are unchanged — asserting on the *file*, not on the hook
having fired. A hook-level assertion passes while the file is being rewritten by `sed`, which is
precisely the hole. It goes red if a future engine adapter gates file tools only.

## AG-R-12 — An `agy` hook gate is only as wide as its matcher

**Raised 2026-09-03 as "the hook gate fails open", and corrected the same day when the original
measurement turned out to be wrong.** The wrong version is kept in outline because the way it was
wrong is the risk.

**What was measured first.** A `PreToolUse` hook sleeping 10s against `"timeout": 3` was invoked, the
deadline passed, and the edit landed. Read as: a timeout does not block, so the gate fails open.

**What was wrong with it.** That probe's `matcher` was `replace_file_content` alone. Re-run with
`"matcher": "*"`, the identical timeout **blocked the write** — the file was untouched and the agent
reported *"the configured `PreToolUse` lifecycle hook intercepted and blocked the tool execution."*
A hook killed at its deadline exits non-zero, and `agy` treats that as a refusal.

So the timeout is not the hole. **The hole is a narrow matcher**: a blocked tool is an error the model
can see, and it will reach for a different tool to accomplish the same thing. Anything the matcher
does not cover is ungated. That is [AG-R-11](#ag-r-11) exactly — a denied edit re-attempted by
another route — on a third mechanism, after `sed -i` and inline `python3`.

**The failure modes, measured.** Only one of four fails open, and it is the one under our control:

| Hook behaviour | Tool | Why |
|---|---|---|
| exceeds `timeout` | **blocked** | killed at the deadline, which is a non-zero exit |
| exits non-zero | **blocked** | `command failed: exit status 1` |
| prints malformed JSON | **blocked** | unmarshal failure |
| command missing / not executable | **blocked** | `exit status 127` |
| **prints nothing, exits 0** | **allowed** | parsed as `{}`, empty decision defaults to allow |

**Mitigations — and as of [AG-14](decisions.md#ag-14) these are phase-8 requirements, not advice.**
A gate is the product on this transport, so each of these is a thing the phase does not ship without:

- **`"matcher": "*"`, never a tool list.** The seam is every tool, for the same reason
  [AG-5](decisions.md#ag-5) makes it every *mutating* tool on the SDK: a gate the model can walk
  around is a manufactured record of consent. A per-tool matcher is the mistake this entry exists to
  prevent.
- **Never exit 0 with empty stdout.** Every path through the hook prints a decision. The one
  fail-open case is ours to not write, and it should carry a test.
- **Raise `--print-timeout`** (default **5m**), which bounds the whole turn and therefore the dialog.
  A hook may block as long as it likes — `timeout` is passed straight to `context.WithTimeout` with no
  ceiling, verified at `86400` — but the turn around it will not.
- **`permissions.allow` instead of `--dangerously-skip-permissions`.** Grants of the form
  `file(<workspace>/*)` stop the headless layer soft-denying, while the hook keeps an absolute veto —
  a hook `deny` overrides a settings `allow`. Defence in depth rather than one gate.

**Tripwire, and it must assert on the file rather than on the hook.** A probe that denies an edit,
lets the turn run to completion, and asserts the target's bytes are unchanged — with the hook
recording *every* tool it was asked about, so a route-around shows up as a second tool name rather
than as a silent pass. Asserting that the hook fired is what produced the wrong answer the first
time: it fired, and the file changed anyway.

**Standing caveat on the correction.** The first reading was published in this file and in
[AG-2](decisions.md#ag-2) before it was checked against a second matcher. It survived one probe and
one commit. The lesson is the one AG-R-11's own tripwire already states — assert on the artefact, not
on the mechanism — and it is recorded twice because it has now been learned twice.
