# Antigravity Risk Register

Each risk has an ID, a description, a mitigation, and a **tripwire** — the observable that tells us
the risk has fired. A risk without a tripwire is a worry, not a managed risk.

`AG-R-1`, `AG-R-3` and `AG-R-11` were found by measurement on 2026-08-30 rather than by reasoning; the
captures are in [`sdk-surface.md`](sdk-surface.md). The rest follow from the surface read.

**`AG-R-1` is retired** — it was measured on 2026-08-30 and did not materialise. It is kept rather
than deleted because its tripwire is still wanted as a regression test, and because the same
measurement raised **`AG-R-11`**, which is live, critical, and was not predicted by anyone.

---

<a id="ag-r-1"></a>

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

<a id="ag-r-2"></a>

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

**The `agy` binary is the same risk and had no tripwire until 2026-09-10**, which is the more
consequential half: it is the transport that reaches the paid subscription, it self-updates, and its
tool set is not something a `pyproject.toml` pin can hold still. It had moved to 1.2.0 and was
advertising **57 tools where `agy/tools.py` classified 14** — found not by an audit but by a live turn
raising a dialog for `schedule`. `scripts/probe_agy_tool_inventory.py` now reads the inventory off the
`init` frame, which costs no model turn, and gives this transport the same *empty by declaration*
bucket: `TOOL_CLASSES` for what is classified, `SEEN_UNCLASSIFIED` for what has been looked at and
deliberately left to the default gate. It reports a second bucket the wheel's probe does not need —
names in our table the binary no longer offers, which is where a **rename** would show up as a silently
emptied write seam. See
[`delivery.md` § Three findings, closed](delivery.md#three-findings-closed--and-the-one-that-was-wrong-before-it-shipped-2026-09-10-latest).

---

<a id="ag-r-3"></a>

## AG-R-3 — A write can be silently diverted out of the repo

> ## **CAUSE FOUND AND FIXED, 2026-09-05. It was ours.**
>
> **`agy` was never diverting anything.** The agent was writing exactly where it intended, in the only
> directory it had ever been in: `agy`'s own scratch directory. AIC⚡DC spawned the subprocess with
> `cwd=repo_root` and never told `agy` that the repository was its **workspace**, so every tool call
> on this transport ran outside the user's tree.
>
> Measured with everything else held constant — same parent directory, same `git init`, same seed
> file, same process cwd, one flag different:
>
> | | tool `pwd` | `git rev-parse --show-toplevel` |
> |---|---|---|
> | `--add-dir <repo>` | `/tmp/temp/wstest` | `/tmp/temp/wstest` |
> | *(control, no flag)* | `~/.gemini/antigravity-cli/scratch` | `fatal: not a git repository` |
>
> And `agy`'s system prompt closes the loop — it instructs the model that when it needs somewhere to
> write it may use *"the default project directory at `~/.gemini/antigravity-cli/scratch`"* and should
> *"recommend the user set that subdirectory as the active workspace."* Which is what it did, every
> time, correctly, given where it was standing.
>
> **The fix is one flag**: `AgySession._argv()` passes `--add-dir <repo_root>`. The same prompt that
> produced `scratch/hello_world/hello.py` now produces `hello_world.py` in the repository, with
> nothing written to scratch — and it goes through `write_to_file` and the permission dialog rather
> than the shell heredoc the model previously fell back to when `write_to_file` was refused.
>
> **How it was found is the lesson.** Not by a probe. A user asked *"create a helloworld script"* in a
> new repository, then *"create it in this repo"*, and the model's own second turn ran `pwd` —
> answering `~/.gemini/antigravity-cli/scratch` — then found the `agy` pid, read `/proc/<pid>/cwd`,
> and wrote the file where it had been asked. **The agent diagnosed this, in its transcript, and the
> transcript was readable because phase 5 had shipped the mirror a few hours earlier.**
>
> Four causes were guessed at over six days and each disproven by measurement: `trustedWorkspaces`,
> git-ness, workspace emptiness, concurrency. The fifth was never guessed because it was not a
> property of `agy` at all. The correlation the register had settled on — *"a create lands when the
> turn also touches an existing file, and diverts when creating is all the turn does"* — is explained
> exactly: a turn that edits an existing file is given a path and writes there; a turn that only
> creates picks its own location, which was the scratch directory.
>
> **What stays.** The detection in `agy/steps.py` and the sentinel-write tripwire both stay: they
> assert an outcome and cost nothing, and a future release of somebody else's CLI can reintroduce this
> shape by another route. What is retired is the *theory* — this entry no longer needs one.
>
> The original assessment and the four dead ends are kept below, because the way they were wrong is
> the reusable part: every one of them was a hypothesis about the other product, and the variable
> nobody controlled for was in our own argv.

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
> **Excluded 2026-09-05, and the run that excluded it found something better.**
> `scripts/probe_agy_concurrent_write.py` ran the same create twice in one workspace — once alone,
> once beside a second working `agy` session — and **both diverted**, the solo one included. So
> concurrency is not the trigger, and the probe reported itself INCONCLUSIVE rather than claiming a
> result, because a comparison whose control also fails proves nothing.
>
> What it does establish is sharper than what it set out to test. The *solo* create diverted in a
> freshly-initialised **empty** git repository, while `probe_agy_write_target.py` — which seeded
> `existing.txt` and asked for an edit *and* a create in one turn — had **both** land in a workspace
> under the same root. The two differ in whether the workspace **contained a file at all**.
>
> **That hypothesis was tested immediately and is also wrong.** The probe was re-run with a file
> seeded in the workspace and the create diverted again, solo and concurrent alike. Emptiness is not
> the trigger.
>
> **What the four runs do line up on is the shape of the *turn*, not of the workspace.** Every
> observation on record, with the workspace root held constant at `/tmp/temp`:
>
> | Turn | Outcome |
> |---|---|
> | edit an existing file **and** create a new one (`probe_agy_write_target`) | **both landed** |
> | edit an existing file (browser demo; isolation probe's stranger) | **landed** |
> | create only, empty workspace (`probe_agy_concurrent_write`, ×2) | **diverted** |
> | create only, seeded workspace (same probe, re-run, ×2) | **diverted** |
> | create only, fresh directory (the original 2026-08-30 capture) | **diverted** |
>
> So the sharpest statement the evidence supports is: **a create lands when the turn also touches an
> existing file, and diverts when creating is all the turn does.** That is a correlation across five
> runs and still not a mechanism — the third explanation offered here, after two that were confidently
> wrong, so it is written as an observation rather than a cause.
>
> It does not change the mitigation, which is the useful part: the detection below fires on the
> outcome and needs no theory about what produced it.
>
> **The consequence for the mitigation is the point, and it survives not knowing the cause.** AG-10's
> health check asserts *"the repo root is a workspace the engine will write to"*. A check phrased
> against `trustedWorkspaces` would pass on a machine where writes divert anyway — measured here. It
> must assert the **outcome**: write a sentinel, then `stat` it at the path it was asked for. That is
> the one form of the check that does not depend on a mechanism nobody has pinned down. See
> [`delivery.md` § The trusted workspace was not the whole story](delivery.md#the-trusted-workspace-was-not-the-whole-story-and-the-first-two-explanations-were-wrong).

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

<a id="ag-r-4"></a>

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

<a id="ag-r-5"></a>

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

<a id="ag-r-6"></a>

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

<a id="ag-r-7"></a>

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

<a id="ag-r-8"></a>

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

<a id="ag-r-9"></a>

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

<a id="ag-r-10"></a>

## AG-R-10 — A second bundled binary

> **Mitigated 2026-09-05 (phase 7).** `google-antigravity` is now the `antigravity` extra. The
> numbers below are measured rather than estimated, and the risk is closed to the extent a
> dependency-set risk can be: it is now one `pyproject.toml` edit away from returning, and there is a
> test and a build-time assertion in the way of that edit.

**Severity: medium. Likelihood: certain if the SDK is a hard dependency.**

`localharness` is **129,065,896 bytes** inside the 0.1.16 wheel — it was 119,721,512 in 0.1.15, so it
grew by ~9 MB in the four days between the two measurements, which is its own small argument. The
bundled `claude` CLI is already ~295 MB and is the reason packaging is its own phase on the Claude
side.

**Why it bites:** it compounds a problem that is already the most likely thing to block a release,
and it does so for a capability many users will not enable. An install that grows by 120 MB to ship a
second engine nobody in that install has credentials for is a bad trade made silently.

**Mitigation:** `google-antigravity` is an **optional dependency**, in an extra rather than the base
install. The engine reports its own absence through the capability descriptor
([AG-9](decisions.md#ag-9)) exactly as it reports a missing surface, so a base install is a
one-engine install with no broken UI rather than an error.

**What it costs, measured on 2026-09-05** with `uv pip install` into two clean Python 3.14 venvs,
`claude-agent-sdk` 0.2.152:

| Install | `site-packages` |
|---|---|
| `aic-dc` | **273.1 MiB** |
| `aic-dc[antigravity]` | 408.3 MiB |

So the extra is **135.2 MiB**, of which `localharness` alone is 123.1 MiB (129,065,896 bytes).

**Measure a *fresh* install, before its first run.** The first attempt at this table reported 285.8 MiB
for the base, because that venv had already started a server and `__pycache__` had added ~9 MiB of
bytecode to `site-packages` — while the comparison venv had not been run. The absolute number moved,
the difference moved with it, and nothing looked wrong. Sum apparent file sizes rather than `du`
blocks, too: `uv` hardlinks from its cache, so block counting answers a different question depending
on what else is installed.

**And the extra is narrower than its name.** It buys the *metered* route, not the engine: the `agy`
transport ([AG-14](decisions.md#ag-14)) reaches the same Antigravity product over a pipe on the
owner's own subscription, imports nothing from `google.antigravity`, and mounts on the CLI being on
PATH — so a base install is still a two-engine install for anyone who has it. What a base install
genuinely loses is the API-key session and the **consultant**, since `second_opinion` and
`generate_image` are the SDK's and `agy` has no one-shot consultation mode.

**Tripwire, in three places** — because the leak is a `pyproject.toml` edit nobody reviews as a size
change, and a number in a release note is something a human has to notice:

1. `tests/test_antigravity_packaging.py` reads `pyproject.toml` and fails if `google-antigravity` is
   back in `[project.dependencies]`, or if the extra loses its version floor.
2. The release workflow's verify step fails if `localharness` appears in the PyInstaller archive.
3. Base-install size, measured per release against the table above.

The first two are what make this a caught regression rather than a noticed one.

---

<a id="ag-r-11"></a>

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

<a id="ag-r-12"></a>

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

### The second way it failed open, found 2026-09-05

Read the table again and notice what it says about *`agy`*: four of five failures **block**. `agy`
fails closed. The one fail-open row is the one we write.

**Our installed command converts all four into fail-open**, deliberately:

```
<interpreter> -m aic_dc.agy.hook <config_dir> || printf '{"decision":"allow"}'
```

That is sound on one premise, stated in `agy/install.py`: *a non-zero exit means the interpreter
could not start, which means this host is not running, which means it owns no conversations, so allow
is correct.* The premise buys something real — a stale entry must not stop a stranger's `agy` — and it
holds exactly while the only way the command can fail is that a Python has gone missing.

**It was false on a PyInstaller build.** There `sys.executable` is the frozen binary, not a Python, so
`<binary> -m aic_dc.agy.hook …` exits 2 with *"unrecognized arguments"* — on every call, of a session
this host **was** running and **did** own. Every tool call auto-approved. And `status()` reported the
gate `current`, because it decides by comparing the installed command against the one it would write,
and the string matched perfectly.

So: an ungated agent, reporting itself gated, on the transport where the gate *is* the product
([AG-5](decisions.md#ag-5)). It was invisible from a source checkout, where the `-m` form is correct
and every test passes, and no test could have caught it — the suite runs where `sys.executable` is a
Python.

**What closes it, at two layers:**

- `hook_command` emits `<binary> --agy-hook <config_dir>` on a frozen build — a suppressed CLI flag
  whose only caller is that string.
- **`install` probes the command before writing it** (`hook_runs`) and refuses one that does not
  answer with a JSON decision. That is the general fix, and the frozen binary was only its first
  instance: a moved virtualenv or an uninstalled package reach the same place. It runs the left side
  only, without the fallback — running the whole command would print a perfect decision and mask
  precisely the failure being looked for.

Failing closed here costs the user an error message at the moment they asked for a gate, which is the
cheapest place in the system to spend one.

**Added tripwire:** `install` returns `unrunnable` rather than writing, and the Settings panel renders
that state with its reason. The deeper lesson is a third instance of this entry's own: **a string that
is correct is not a mechanism that works**, and `status` comparing strings was measuring the first
while claiming the second.

### What a socket-level test double can and cannot attest — noted 2026-09-11

Raised by the reviewer in [AG-22](decisions.md#ag-22)'s consultation, which asked how gating could be
attested in CI without live auth or a vendor binary. Its proposal: a test double that connects to the
gate socket and replays canned payloads — symlinked paths, unicode, relative paths — so normalisation,
classification and timeout handling are covered deterministically in milliseconds.

**That is worth building, and it is not attestation of interception.** The failure above happened
*upstream of the socket*: the hook process never started, so a double that speaks to the socket replays
straight past the break and reports healthy for the same reason `status` did. The distinction is this
entry's own lesson in a new place — a payload a socket answers correctly is not evidence that the vendor
ever reaches that socket.

`hook_runs` is the part that already attests interception, and it is stronger than the proposal because
it executes **the command string actually written to `hooks.json`**, left side only, and requires a JSON
decision back. So the split to keep is: `hook_runs` attests that the vendor's invocation reaches us; a
socket double attests what we decide once it has. Neither substitutes for the other, and only the second
is cheap enough to run per-case.

---

<a id="ag-r-13"></a>

## AG-R-13 — A Claude-only deployment acquires a second provider by accident

**Raised 2026-09-06**, by a user asking whether the second engine could be turned off and finding that
it could not. [AG-17](decisions.md#ag-17) is the answer; this is the register entry, because the risk
is not the missing switch but the *shape of the mount conditions* that made one necessary.

**Every Antigravity surface mounted on evidence that something was possible, never on anything saying
it was wanted.** The `agy` adapter mounted on `shutil.which("agy")`. The SDK engine mounted on a
resolvable credential and an installed wheel. [AG-16](decisions.md#ag-16) then mounted the consultant
on the `agy` binary plus a gate, where it had previously needed a Gemini key. Each of those is a
reasonable condition read alone, and together they mean **an install acquires a second provider by
having a file on disk** — which is invisible to the person who did not put it there for this purpose.

**The exposure is the advertisement, not the traffic.** `second_opinion` and `generate_image` reach
the permission dialog like any other `mcp` tool ([AG-5](decisions.md#ag-5)), so no code leaves without
a click. But they are described to the model on every turn, the model reaches for them when they fit,
and the dialog then asks a user — who is not the person who wrote the workplace's policy — to approve
something the policy forbids. **A control that resolves to "the human said yes" is not a control an
organisation can rely on**, which is the same reason [AG-5](decisions.md#ag-5) refuses a blanket-bypass
posture from the other direction.

**Why it took a question from outside to see it.** This directory reasons throughout from the position
of an owner who *wants* a second engine — the whole of "Why a second engine" is an argument for having
one. Nothing in it had a place to hold the reader who is not allowed one, so a default that widened on
2026-09-06 read as a feature and was one, for everybody the plan had in mind. **This is the class of
defect that is only visible from outside the plan**, the same class as the routed `/usage` command
that named a tab which did not hold what it promised — found by a user comparing the app against the
CLI it wraps, and unreachable by any audit of this suite against itself.

**Mitigation, and it is a requirement rather than advice:** every mount point for an Antigravity
surface consults `engines.enabled` — the engine adapters, the consultant, the selector, and
`switch_engine` — with a test asserting that the set of such mount points is exactly the set that
consults it. A surface added beside them that forgets is the recurrence, and it would look exactly
like today's condition did: locally correct, and wrong for a deployment nobody in the room had.

**Tripwire that says it has fired:** a Claude-only install where `list_engines().mountable` names more
than `claude`, or where a Claude session's MCP server list contains `aic-dc-antigravity`.

**Both halves of that tripwire were run against a fresh server on 2026-09-07 and are clean — and doing
so found a second way this risk recurs, which the entry above does not cover.** `app.json` is a
*managed* config file, so the first start after **any** version change backs the user's copy up and
replaces it from the bundle: `engines.enabled` disappears and `list_engines().enabled` is
`['claude', 'antigravity', 'agy']` again, with the consultant's tools back inside every Claude turn
and no warning that a policy was dropped. The mount conditions are all correct; the policy is simply
not there any more. So the mitigation above is necessary and not sufficient — **a policy that lives in
a file the upgrader rewrites is a policy with an expiry date**, and an audit of the mount points cannot
see it.

Shipping `app.json` read-only, which [AG-17](decisions.md#ag-17) already recommends, does hold: the
overwrite fails, the `OSError` is caught, startup continues and the policy stays in force. It is
therefore the enforcement path to document rather than an equivalent alternative to the writable one.
It has a cost of its own — the aborted pass never advances the version marker, so it repeats on every
start, leaving one timestamped backup per launch and never upgrading `commit.md`. Measurements and the
reasoning are in [`delivery.md` § Phase 11](delivery.md#phase-11--the-engine-policy-and-what-a-fresh-server-said-about-it-2026-09-07).

**Closed in the config layer the same day.** `app.json` is now merged key by key against a pristine
copy of the bundle rather than overwritten, so an upgrade keeps a value the user or their configuration
management set and still delivers a default they never touched
([`../1-foundation/configuration.md` § `app.json` is merged key by key](../1-foundation/configuration.md#appjson-is-merged-key-by-key-commitmd-is-overwritten)).
Re-measured on two fresh servers: the policy survives a version change, `list_engines().enabled` stays
`['claude']`, and the read-only case now writes no backup at all because the merge finds nothing to
change. The read-only recommendation stands but is no longer load-bearing — and its cost is gone too,
since one unwritable file no longer aborts the pass or blocks the marker.

**The tripwire gains a third arm, and it is a test rather than a run:** an upgrade across two versions
that leaves `engines.enabled` on disk. It lives in `tests/test_config.py`
(`test_an_upgrade_keeps_the_engine_policy`) rather than here, because what recurs is not the mount
conditions but the *file category* — a future maintainer moving `app.json` back to a plain overwrite,
or a second policy key landing in a file with the same problem, is the recurrence. Note what this
implies for any policy added later: **the file it lives in is part of the decision**, and a policy in a
managed file needs the merge or it needs a file of its own.

---

<a id="ag-r-14"></a>

## AG-R-14 — A subagent's tool calls do not reach the gate

**Severity: critical. Likelihood: MEASURED — it happened on 2026-09-09, the main case was fixed the
same day, and all three residues were closed on 2026-09-10.** Two of them by changing what identity
*is*: on a systemd platform the gate no longer depends on anyone registering a conversation before it
speaks — `agy` runs in a cgroup of ours and the hook asks the kernel ([AG-18](decisions.md#ag-18)).
Where that is unavailable the original mechanism and those two residues stand, which is a per-platform
gap rather than a design one. The third was closed on every platform and **not in the form this entry
recommended**: reading the one pid it named would have turned an *orphaned agent's* claim into a way
past the gate, so liveness is asked of two pids and only a corpse is released.

> **Outcome, 2026-09-10: the registry was switched off and the gate still held.**
> `probe_agy_subagent_gate.py`, re-run with `AgyGateServer.claim` replaced by a no-op for **both**
> the parent and the subagent, still put all eight tool calls to the dialog — the subagent's seven
> included — and the deny still left the target file byte-identical. Conversation claims were inert
> and cgroup identity carried the whole load. `run_command` and `find_by_name` appear in that list,
> which is [AG-R-11](#ag-r-11) looking for another way out and being gated on each attempt.

> **Outcome: the gate was bypassed entirely.** `scripts/probe_agy_subagent_gate.py` denied every tool
> call except the delegation itself and watched the subagent's edit land anyway. The gate was asked
> about one conversation and one tool — `invoke_subagent` — and about nothing the subagent then did.
> Fixed the same day by claiming the subagent's conversation; the probe now reports the deny holding
> with the target file byte-identical. See [`delivery.md` § The subagent that was never
> gated](delivery.md#the-subagent-that-was-never-gated-2026-09-09).

The mechanism is [AG-14](decisions.md#ag-14)'s own, working exactly as designed for a case nobody had
enumerated. `agy` runs under `--dangerously-skip-permissions`, so this host's gate is the only thing
between the model and the tree, and the hook routes to it by **conversation id**: a conversation
nobody has claimed is passed through ungated. That passthrough is load-bearing — it is what keeps a
second `agy` session of the user's own out of our dialog ([AG-R-12](#ag-r-12),
`probe_agy_isolation.py`). A subagent is given a conversation of its own, and nothing claimed it.

**[AG-5](decisions.md#ag-5)'s table is what this contradicts.** It puts the spawners in the *still
asks* column with the reason "a subagent inherits the tool set". The subagent does inherit the tool
set; what it did not inherit was the gate — so gating the spawn bought a dialog on the word
"delegate" and no review of anything done under it.

### The three residues — two closed on systemd, the third closed everywhere

**Read the two closures as conditional.** [AG-18](decisions.md#ag-18) removes the premise both of the
first two residues rest on, and it removes it *where `systemd-run --user --scope` works*. Where it does
not, `scope.available()` answers `False`, the gate falls back to the conversation-id routing described
below, and these bullets describe the live behaviour rather than the history. They are kept in the
present tense for that reason. Residue 3 is closed on every platform, because it was never about
routing.

- **The spawn-to-announce race. Closed on systemd (2026-09-10).** The scope entry is published before
  `agy` is spawned, so a child's first call arrives inside a group that was registered before the
  child could exist. The measurement below stands as the record of what was open, and the paragraph
  after it — that the window was never quantified — is now moot rather than answered: nothing is
  waiting on a claim.

  `agy` starts the child before the frame announcing it arrives, so a
  tool call made in that window reaches the hook before the claim is on disk. This side cannot close
  it as built: `invoke_subagent`'s arguments carry only `Prompt`, `Role`, `TypeName`, `Model` and
  `Workspace` — **no conversation id**, because the child does not exist when the dialog for the spawn
  is answered. A *tentative hold* is the candidate that needs no vendor change: on approving a spawn,
  mark a short pending window, and have the hook briefly poll for a claim on an unclaimed
  conversation rather than passing it straight through, falling open on timeout so AG-R-12's property
  survives.

  **How wide this window is, is measured on one side and argued on the other.** The consultant's
  case for ranking it *above* nested delegation is that it fires on every spawn, where nesting
  needs the model to choose it — and that a subagent exists to act immediately, so its first call
  should lose the race routinely. **Both tiers of the consultant made this argument independently**
  and both put residue 2 first. The frequency half is right. The second half is contradicted by
  the run in [`delivery.md`](delivery.md#the-subagent-that-was-never-gated-2026-09-09): the gate
  decided the subagent's `view_file`, which *is* its first call, so on that run the claim won. One
  run is not a distribution, and the honest reading is that the window is real and unquantified
  rather than that it is narrow.

  **The same missing id has a second consequence, found 2026-09-10 while building ⏹.** This residue
  is about a claim arriving late; the other half is that *nothing else can be keyed on the id either*.
  `agy` emits the `subagent` step carrying `conversation_id` **once, and at `DONE`** — the live
  `invoke_subagent` frame carries `Role`, `TypeName`, `Model`, `Workspace` and `Prompt`, exactly as
  this bullet says, and no id. So a subagent's row arrives already terminal, and a Stop button, which
  needs a handle the user's click can carry, has no window in which to exist. `subagent_stop` is
  therefore blocked on an **identity** rather than on a mechanism: the aimed refusal was built and
  measured working (`scripts/probe_agy_subagent_stop.py`), and
  `scripts/probe_agy_subagent_stop_ui.py` found no live row to press it on. AG-18 closed the *gating*
  consequence of this residue by asking the kernel which group a call came from; there is no
  equivalent move for a button, because the kernel has no name for the user to click either. See
  [`delivery.md` § ⏹ on one subagent](delivery.md#-on-one-subagent-the-mechanism-works-and-the-handle-arrives-too-late-2026-09-10).

  Two distinctions the consultant's argument elides and this entry should not. **A window that
  opens on every spawn is not a bypass on every spawn** — the bypass needs the child's first call to
  land inside it, which is the unmeasured quantity. And the figures offered for how often that
  happens were invented rather than measured, on both tiers. What the argument does establish, and
  what stands, is the *shape* of the worst case: a user approves a benign-sounding delegation, and
  the child's first call inside the window is the one that matters. **The measurement this needs is
  a distribution, not another opinion** — the interval between a subagent's first tool call reaching
  the hook and the claim landing on disk, over many spawns, which the existing probe is most of the
  way to being able to report.
- **Nested delegation is a blind spot at full width. Closed on systemd (2026-09-10).** The kernel puts
  a grandchild in the same group as its parent whether or not anyone announced it, so the hook
  recognises it with nothing written down. This is the residue that could not be closed by claiming
  faster, and it is why the whole premise went instead — see [AG-18](decisions.md#ag-18). The
  deny-the-nested-spawn candidate below is **no longer the recommendation on this platform**, and
  stays recorded as the standing option for the platforms that have no scope.

  A subagent's own frames never reach this host —
  its work appears only in its transcript on disk — so a subagent that spawns a *further* subagent
  announces the grandchild to nobody, and the grandchild runs exactly as the child did before this
  fix. Watching the child's `transcript.jsonl` is the obvious answer and is not a sound one: the file
  is flushed by a process we do not control, so the grandchild's first calls land before the line
  does. **Denying a spawn that arrives from an already-claimed subagent conversation** is the
  candidate — refusing what cannot be gated, rather than shipping a gate that is not one. It takes a
  capability away from the agent, so it is an amendment to AG-5 rather than a fix.

  **The real candidate is neither the deny nor the two userspace schemes that were tried first, and
  finding that took three rounds with the consultant on 2026-09-09.** All of them attack the same
  premise — that identity is routed by a `conversationId` somebody has to write down in advance —
  and the first two attack it with a signal the gated process controls.

  Two dead ends, recorded because the way they die is the argument for what replaced them:

  - **An inherited environment marker** (launch `agy` with a secret in its environment; children,
    grandchildren and the hook process inherit it; the hook asks *"was I born inside this host's
    tree?"*). Its corollary is the good idea that survives into the answer: the claim becomes
    **dynamic**, because the first call carrying the marker *is* the announcement, so there is no
    footrace to lose. But the marker is mutable data belonging to the process it identifies, and
    [AG-R-11](#ag-r-11) already measured this agent reaching for `run_command` when a write was
    refused. It is worse than that adversarial case: `env -i`, build runners, `sudo` and
    `subprocess(env={})` scrub environments as ordinary practice, so it fails open on benign
    tooling too. A pid-ancestry walk is the same idea one step better and still loses to a
    double-fork.
  - **A private config root** (`--config-dir` / `XDG_CONFIG_HOME`), so the hook exists only for our
    sessions and can then **fail closed**. This one is self-contradictory, which is the part worth
    keeping: refusing an unknown conversation refuses *exactly the two cases this row is about* —
    the grandchild and the early call — so it converts an unsupervised action into a denial of
    service against our own delegation. And the override has to travel by environment (which
    reduces it to the first dead end) or by argv (which a shell-spawned process does not inherit).

  **One of this row's own premises was wrong and the consultant caught it.** The argument for
  keeping the global hook was that an `agy` started indirectly through `run_command` is "at least
  visible" to it. It is not: that instance gets a fresh conversation id, `registry.lookup` misses,
  and the hook passes it through. Today's design already supervises indirect instances not at all,
  so that was never a coverage advantage to protect.

  **The candidate to measure is a kernel-enforced group rather than a userspace marker.** Launch
  `agy` inside a cgroup of our own — `systemd-run --user --scope --unit=aic-dc-<session>` — and have
  the hook read `/proc/self/cgroup`. cgroup v2 binds every descendant irrevocably: an unprivileged
  process cannot move itself out, and `fork`, `env -i`, a subshell and a double-fork all stay inside
  it. That gives the identity answer at the instant the hook runs, with no registry write to race,
  which closes **both** residues and lets the claim be adopted dynamically on first contact. The
  isolation property ([AG-R-12](#ag-r-12)) survives on a stronger footing than it has now, since the
  user's own session sits in their terminal's scope and simply does not match. Verified available on
  this machine 2026-09-09: `systemd-run --user --scope` succeeds, and this shell reports
  `…/app.slice/ptyxis-spawn-<uuid>.scope` — a terminal spawn scope, which is exactly the shape a
  user's own session would present. `bwrap` is present too, which is the second synthesis: shadow
  the config directory through a mount namespace so the private-root scheme needs no environment
  variable and no flag.

  **The containment claim is measured rather than quoted, and it was free to measure** — a scope,
  four descendants, no `agy` involved. Each printed its own `/proc/self/cgroup`:

  | descendant | in the scope? |
  |---|---|
  | direct child | yes |
  | subshell | yes |
  | `env -i` (environment fully stripped) | **yes** |
  | `setsid` double-fork | **yes** |

  The last two are the point: they are precisely the escapes that kill the inherited-marker design
  and its pid-ancestry variant, and the kernel one survives both. That is the whole argument for
  preferring it, and it cost one command — which is the argument for measuring the cheap half of a
  design before writing the probe for the expensive half.

  **Measured on 2026-09-09 and it holds.** `scripts/probe_agy_cgroup_identity.py` ran two real turns
  and answered all four questions:

      scope unit: aic-dc-probe-e937141e
      hook invocations inside the scope:  4
        matched our unit: 4  ['aic-dc-install-probe', 'invoke_subagent',
                              'list_dir', 'send_message']
      hook invocations outside the scope: 2  → ptyxis-spawn-…, not ours
      a subagent's calls carried our unit too: ['list_dir', 'send_message']

  `agy` runs normally inside a scope; the hook subprocess — which `agy` spawns, not us — inherits
  it; **a subagent's own calls carry it**, which is residue 1's case covered with nothing announced
  and nothing claimed; and a turn taken outside the scope reported the terminal's own
  `ptyxis-spawn-…` cgroup, so [AG-R-12](#ag-r-12)'s discriminator discriminates. The control is not
  decoration: a matcher that answered yes to everything would have passed every other check.

  So **the deny is no longer the recommendation**. It stays recorded as the fallback for one reason
  only, which the probe reports rather than hides: this is a Linux-and-systemd mechanism and the
  packaging is not. Where `systemd-run --user --scope` is unavailable the choice is the capability
  reduction or an unclosed residue, and that is a per-platform answer rather than a design one.

  **Built 2026-09-10** — `src/aic_dc/agy/scope.py`, `registry.claim_scope` / `scope_owner`, a
  fallback branch in `hook.decide`, and 17 tests. The probe above measured that the mechanism
  *works*; what it could not measure is whether it is **load-bearing**, because the conversation
  registry was still running underneath and a claim that landed in time prints the same result. So
  `probe_agy_subagent_gate.py` was re-run with `AgyGateServer.claim` stubbed to a no-op on both the
  parent and the subagent: all eight calls still reached the dialog and the deny still held. That is
  the outcome quoted at the top of this entry, and it is the reason these two residues are marked
  closed rather than mitigated. See [AG-18](decisions.md#ag-18).

  **One incidental finding, from the probe's own setup — fixed 2026-09-10.** It reported the installed
  gate as `stale` before the run, on a machine where it was working: `install.status` compared command
  *strings*, and the same interpreter spelled `.venv/bin/python3` in the file and `.venv/bin/python` by
  `sys.executable` is two strings for one program. **The consequence was larger than "a wrong label":**
  `AgyService.connect` answers `gate_not_installed` on a stale state and `AgyConsultant.available` goes
  false, so the whole engine refuses to start and `second_opinion` disappears — after nothing more than
  a different entry point resolving the interpreter by its other name.

  `_same_command` now compares the arguments exactly and the interpreter as a file: the command is cut
  on the invocation `hook_command` itself writes, so the two halves are separated without guessing at
  word boundaries. **Not by the interpreter's resolved target, which is the trap the fix nearly walked
  into:** `.venv-a/bin/python` and `.venv-b/bin/python` are typically both symlinks to one system
  interpreter and are *not* interchangeable, because a virtualenv is selected by the path used to invoke
  it — so following the link would report another checkout's gate as ours, which is the case `stale`
  exists to report. The test is same directory (through `realpath`, so a checkout reached by a symlink
  is still itself) and the same file within it, which is exactly "one interpreter spelled two ways" and
  nothing wider. Measured on this machine with the pair that caused it (`.venv/bin/python3` and
  `.venv/bin/python` → `current`, `/usr/bin/python3` → still `stale`), and pinned by tests: the other
  spelling reads `current`, a second checkout is still `stale`, a different `config_dir` with the same
  interpreter is still `stale` — only the interpreter is forgiven, never the arguments — an interpreter
  that has been deleted is `stale` rather than an exception thrown at a Settings caller, and an
  unbalanced quote in a hand-edited entry is `stale` rather than a raise.
- ~~**A stale claim now costs more.**~~ **Fixed 2026-09-10.** `registry.claim` recorded a `pid` and
  `registry.lookup` had never read it, so a killed session's entries were indistinguishable from live
  ones and the hook denied on them — it refuses whatever it cannot reach. Before the subagent fix that
  orphaned one entry per session; after it, one per subagent as well. The recorded `pid` was the unused
  handle.

  **It was a prerequisite of the race fix rather than merely the cheap one.** A tentative hold makes
  the hook *poll* an unclaimed conversation instead of passing it through, so a dead host's leftover
  entry stops being an annoyance and becomes a block: the poll finds a claim, the socket behind it
  answers nothing, and the hook denies. Whatever order the three are taken in, this one lands before
  the hold does. Named by the consultant on 2026-09-09.

  **Its original argument expired on 2026-09-10.** The tentative hold was never built, because
  [AG-18](decisions.md#ag-18) closed the race a different way — so this is no longer a prerequisite of
  anything. `registry.claim_scope` wrote a `pid` that `scope_owner` did not read, exactly as `claim`
  wrote one that `lookup` did not, so AG-18 doubled the number of entry kinds with an unread handle
  without changing what the handle was for.

  **Fixed 2026-09-10, and the recommendation this entry carried for a day was wrong as written.** It
  said to *"treat an entry whose process is gone as absent rather than as ours"* — which is a change to
  what the gate **answers**, and on one pid it would have opened a hole this directory has argued
  against from the start. `registry.py`'s own header states the trade: *a dead host makes our sessions
  un-runnable rather than un-gated*. A host killed mid-turn can leave an `agy` still running inside a
  scope of ours, and an absent entry passes its calls through — an agent under
  `--dangerously-skip-permissions` with no gate at all, which is strictly worse than the denial being
  complained about.

  **So reading the pid was not the fix; reading *two* was.** `agy` is a child of this host and a child
  outlives a killed parent, so a dead host with a live `agy` is an **orphaned agent**. `claim` now
  records the conversation's `agy_pid` beside the host `pid` and `entry_is_live` is an **or**: either
  alive and the entry stands and the hook goes on denying what it cannot reach, both gone and the file
  is a *corpse*. Only a corpse reads as not ours — safe precisely because no process from that session
  is left to un-gate — so `lookup` returns `None` for one, `owns_anything` stops counting one, and
  `reap_stale` deletes one from `AgyGateServer.start`, beside the stale-socket unlink that has the same
  cause. The sweep is keyed on liveness rather than on ownership, deliberately: "remove entries that are
  not mine" would delete the live claims of a second host sharing the directory and un-gate its session.

  **The residue was written as tidiness and both of its real costs land on the user**, which is the
  correction worth keeping. `owns_anything` counted *files* and is the tie-breaker for an unparseable
  payload, so one unclean exit of ours made every junk payload from the *user's own* `agy` deny from
  then on — a function whose docstring says it "fails toward the user's work" doing the opposite,
  permanently. And `release`'s docstring had already named the second: a leftover claim intercepts a
  later session of the user's that resumes that conversation ([AG-R-12](#ag-r-12)), which a killed host
  reaches by a route no care in `stop()` can close.

  **One claim made here on 2026-09-10 was overstated and is withdrawn — but the entry it was about
  needed a change of its own.** This said a stale scope entry is worse than a stale conversation entry
  because *"a unit name is something systemd can hand to a later process"*. `scope.unit_name()` is
  `aic-dc-` plus twelve hex characters of `uuid4`, so a recurrence is not a case worth designing
  against. The scope entry's real exposure is the orphaned `agy` above — still inside the unit, still
  matching `scope_owner` — and that is an argument *for* the denial. Which is exactly why the two-pid
  rule had to reach a scope entry too, and it cannot the way a conversation entry does: a scope claim is
  published *before* `agy` exists, so its first write cannot name the process that would act.
  `AgyGateServer.claim` rewrites it with the pid once the child is up, and until then the scope reads as
  alive rather than as a corpse.

  **Three limits are stated rather than solved**, in
  [`delivery.md`](delivery.md#the-pid-that-was-written-and-never-read-2026-09-10): no liveness probe on
  Windows (`os.kill(pid, 0)` is `TerminateProcess` there, so a probe would kill what it asks about —
  the answer is "alive" and that platform keeps the old behaviour), a recycled pid reading as alive,
  and an entry with no `agy_pid` never being a corpse. ~~**And one thing unmeasured:** whether a real
  `agy` outlives a SIGKILLed host is cited from `main.py`'s measurement of the *other* engine's CLI,
  not measured here — `agy` on this machine is installed but not authenticated.~~

  **Measured 2026-09-10, later.** `scripts/probe_agy_orphan.py` kills a host it spawned and watches.
  `agy` **does** outlive it — all three runs, reparented to the user's `systemd`, still there 200 ms
  later — and it is gone in **0.60 s, 0.80 s, 0.80 s**. The mechanism is stdin EOF, isolated
  separately: with the host *alive*, closing the pipe alone ended `agy` in **0.30 s**. So the orphan is
  real and its window is sub-second, where the citation borrowed from the Claude CLI was 38 seconds —
  the *direction* transfers between the two engines and the magnitude does not. The decision table ran
  end to end on pids that were really dead: live → orphan → corpse, then `reap_stale` collecting the
  conversation entry and its scope entry together. (The same run also settled that the recorded
  `agy_pid` is `agy`'s own: `systemd-run --user --scope` execs, so the pid is preserved and the
  process is inside `…/app.slice/aic-dc-<hex>.scope`.)

  **The third limit is what that run found sitting on this machine: eight entries, every host dead,
  every one reading `live`, and `owns_anything()` permanently `True`.** All eight predate the two-pid
  fix, so none carries an `agy_pid`, and `process_alive(None)` answers `True` because the question
  cannot be asked. This is the grandfather clause working exactly as written and reproducing the exact
  defect the fix was for — the paragraph above, *"one unclean exit made every junk payload from the
  user's own `agy` deny from then on"* — for every entry written before it, which on a machine that
  ran the older version is all of them. The fix ended the defect for the population that does not have
  it yet.

  **Fixed the same day, and the argument is the measurement above.** The clause existed so as not to
  guess about an `agy` that might still be running under a host we can no longer ask about. That
  agent's lifetime is known now: under a second past its host. So a **conversation** entry with a dead
  host and no `agy_pid` was written by a version older than this rule and names a process that ended
  long ago by any clock this system has, and `entry_is_live` treats it as a corpse — the same claim the
  two-pid rule already makes, with elapsed time doing the work the missing pid would have done.

  **A scope entry is deliberately not covered, and that asymmetry is the change.** It is published
  before `agy` exists, so an absent `agy_pid` there is the normal state of a session that is *starting*
  rather than the mark of an old file; reaping one on the host pid alone would release the unit an
  orphaned agent is still sitting in. The age argument is available only for the entry kind that cannot
  legitimately lack a pid, and generalising it would undo the paragraph above.

  **Measured on the machine that had it:** the eight entries now read as corpses and `owns_anything()`
  is `False`. Three tests replace the one that pinned the old behaviour, and they are the table rather
  than the change — a legacy entry with a dead host is reaped, one whose host is **alive** is untouched,
  and a scope entry with no `agy_pid` is never a corpse. **Tripwire:** `owns_anything()` answering true
  on a machine with no live `agy` session.

**Two of the three were named by the consultant** (`second_opinion`, 2026-09-09) rather than by the
author of the fix, which is the first time this feature has been used on this repository's own work
and is worth recording as evidence for [AG-13](decisions.md#ag-13).

<a id="ag-r-15"></a>

## AG-R-15 — The second opinion may not be a second model

**Severity: moderate. Likelihood: LIVE — not fired, and nothing prevents it.**

[AG-13](decisions.md#ag-13)'s premise is a sentence in the README: *"Two independent agents
disagreeing about a diff is information. One agent asked twice is not."* On the `agy` transport
nothing enforces that premise, and nothing records whether it held.

`AgyConsultant` takes `model=None` deliberately (`src/aic_dc/agy/consultant.py`), so a consultation
runs on whatever the account holder selected in `agy`'s own settings — inheriting a decision rather
than an accident, which is the right default for a plan the user pays for. **What was not
considered is what that menu contains.** `agy models` on 1.1.27 offers fifteen entries, and three
of them are not Google's:

    claude-sonnet-4-6          Claude Sonnet 4.6 (Thinking)
    claude-opus-4-6-thinking   Claude Opus 4.6 (Thinking)
    gpt-oss-120b-medium        GPT-OSS 120B (Medium)

So a user who sets `agy`'s model to `claude-opus-4-6-thinking` turns `second_opinion` into Claude
reviewing Claude's own work, with the tool description still promising *"Google's Gemini, running as
an independent agent"* and *"a separate Google account"*. The feature does not degrade visibly: it
returns fluent, plausible review either way. This is the failure AG-13 exists to prevent, reachable
through a settings file this app does not own and does not read.

**Measured on this machine, 2026-09-09**, prompted by the question *"can you tell which model you
are calling?"* — which the code could not answer. `~/.gemini/antigravity-cli/settings.json` holds
`"model": "Gemini 3.8 Flash (Low)"`, so the consultations cited in [AG-R-14](#ag-r-14) were
genuinely cross-family and that evidence stands. It stands **by luck of a setting**, not by design,
and the run that produced two of three residues plus the process-lineage candidate was the *Low*
effort tier — which is the more interesting half of the measurement.

**Mitigated the same day, by pinning — which is the opposite of what this entry first recommended.**
`AgyConsultant.DEFAULT_MODEL` is `gemini-3.8-flash-high` as of 2026-09-09, at the user's decision.
The first draft of this entry argued *"do not pin a model to fix this — the defect is silence, not
inheritance"*, and that was wrong in a way worth keeping: it treated the vendor question and the
depth question as separate, when on this surface **effort is baked into the model name**, so the one
choice settles both. Pinning makes AG-13's premise true by construction rather than by a settings
file this app does not own, and it takes the consultation off the account's *Low* tier — the README
already argued the second half about the SDK transport (*"the point of a second opinion is a capable
independent one"*) and this entry did not notice it applied here too.

The parameter is kept, and `None` still means "agy's own default", so the inherited behaviour is one
argument away rather than deleted. Two tests: the pin is a `gemini-` name (asserting the *vendor*,
since the pin will be raised as models are released and should not be raised out of Google's range),
and `model=None` still reaches the account's own choice.

**And then made a control, the same day**, at the user's suggestion — *"perhaps there should be a
way to change that in settings?"* — which is the right instinct for a value that is a preference
with a *warning* attached rather than a constant. `engines.consultant_model` in `app.json`, a
`ConfigManager` accessor, `Settings.get_consultant_model` / `set_consultant_model` validated against
`agy models`, and a picker in the Settings tab beside the master engine's.

Four things about its shape are decisions rather than details:

- **The vendor flag is on the server.** Each entry carries `second_vendor`, computed from a
  `gemini-` prefix in `settings.py`, so no webapp branch keys off a vendor string
  ([AG-R-4](#ag-r-4)). A prefix rather than an allowlist because the list moves — `gemini-3.8-*` did
  not exist when this feature was built, and a stale allowlist would flag a *new Gemini model* as a
  foreign vendor, which is how a warning gets trained out of a user.
- **Marked, not removed.** A Claude entry stays selectable and says what it costs, in the list (`⚠`)
  and again in prose once chosen. Refusing it would be this app overruling a user who may want
  exactly that comparison; hiding it would be pretending the menu is smaller than it is.
- **It is live, not app-restart.** `app.json` is in `settings.py`'s reloadable set and
  `AgyConsultant` resolves the model *per consultation*, so a save reaches the next second opinion.
  Capturing it at construction would have silently made this the grid's third disposition, which is
  the one users have to be told about.
- **`"auto"` is a value, not `null`.** `_changed_fields` treats absent and explicitly-null as the
  same, deliberately, so "let agy choose" needed a word of its own — and `consultant_transport`
  already had that vocabulary.

**A second defect fell out of building it**, in the master engine's equivalent: `AgyService.set_model`
assigned `self._model` and never wrote anything, so a model chosen in the picker was forgotten at the
next server start and the account's default came back. A control that works for one session and then
quietly stops is worse than one that was never offered, because nobody re-checks it. It now writes
`engines.agy_model`, and a write failure costs the persistence rather than the selection. The engine
stays *unpinned* by default, and that asymmetry with the consultant is deliberate: a master engine is
the one the user drives all day and whose cost they feel, so their own tier choice stands; a
consultation is bought by the answer it gives and has a vendor requirement the master does not.

**The pin is verified live, and the tier bought something measurable.** The argv was captured from
the running process on 2026-09-09 — `agy … --model gemini-3.8-flash-high` — which is the check the
missing self-report (below) would otherwise have made impossible. The same AG-R-14 question was then
put to both tiers *verbatim*, so the difference is the tier and not the prompt: `-low` produced the
process-lineage idea and the ranking counter; `-high` produced those plus the inherited-token
refinement that dissolves the race rather than narrowing it, plus the isolated-config-directory
option that would let the gate fail closed, plus the reason tailing a transcript cannot bootstrap at
all (the host would have to discover the grandchild's *own* directory from the child's log while the
grandchild is already running). Neither tier's ranking argument survived contact with the
measurement, in the same way, which is its own evidence about what a second opinion is for. One
comparison is not a trend; it is the first entry in the ledger [AG-13](decisions.md#ag-13) has never
had.

**What is still open:**

- **Report the model with the answer.** The consultation still does not name the model that produced
  it, so a citation in this directory remains an assumption rather than a record. Worth doing
  anyway: a pin is an argument on a command line, and the thing that proves it took effect is on the
  `init` frame.
- **Gemini's own filters refuse this feature's best use case.** A follow-up round on
  [AG-R-14](#ag-r-14), phrased in the ordinary vocabulary of an attack on our own gate — "escape the
  gate", a literal `env -u MARKER agy …` command, "adversarial, prompt-injected" — came back
  *"blocked by Gemini's filters"* rather than answered. The identical question, reframed as a
  supervision-design review with the command strings described rather than written, was answered in
  full and produced the best round of the three. So the tool is refusable exactly where it is most
  valuable, which is adversarial review of this app's own permission boundary, and the workaround is
  a phrasing convention rather than a setting. Worth stating in the consultant's own prompt
  guidance; a user who hits this sees a blocked consultation and no reason to suspect the wording.

- **A pin can go stale.** `gemini-3.8-flash-high` is a name read from `agy models` at 1.1.27 on one
  account. On a plan without that model, or after a rename, the consultation fails rather than
  silently downgrading — which is the right direction, and is the reason this is a note rather than
  a fallback.

---

<a id="ag-r-16"></a>

## AG-R-16 — A `Stop` hook this app does not own can revive a turn it stopped

**Severity: raised to high on 2026-09-12. Likelihood: latent — the mechanism is documented and
shipped; nothing on this machine uses it today.**

**Raised because the harm was measured and is larger than "spend and prose".** ⏹ can be defeated
outright by a `Stop` hook this app does not own, for as long as `--print-timeout` allows — and
`AgySession` sets that to **12h**, with nothing else in this app bounding a turn's wall clock. Worse,
[AG-19](decisions.md#ag-19)'s terminate makes the scenario *cost more* rather than less: see
§ *The two mechanisms fight, and the loop is what spins* below.

`agy`'s shipped `hooks.md` (read 2026-09-10; see
[`sdk-surface.md` § The hook contract is shipped](sdk-surface.md#the-hook-contract-is-shipped-not-inferred--read-2026-09-10))
documents a `Stop` event that fires when the execution loop terminates, and a handler answering

```json
{"decision": "continue", "reason": "…"}
```

**blocks the stop and re-enters the loop**, with `reason` injected as a system message. The same doc
states that multiple *named* hooks for one event *"are merged and executed sequentially"* — and
`~/.gemini/config/hooks.json` is a file this app writes one entry into and does not own. A user's own
`Stop` hook, or one arriving inside a plugin, sits beside `aic-dc-gate` and fires on **this app's**
conversations too, exactly as `aic-dc-gate` fires on the user's.

The consequence is specific to how ⏹ works here. This transport has no halt frame, so a stop
*starves* the turn: the gate refuses every subsequent tool call and the agent is expected to wind
down. A `Stop` hook returning `continue` re-enters the loop the moment the agent tries to finish —
so a turn the user stopped, and the UI has settled, keeps invoking the model. The gate still refuses
every tool call, which bounds the damage to spend and to prose rather than to the working tree, and
that is the reason this is moderate rather than critical.

**Mitigation.** Two were proposed, and **the first one does not work.** It was: register an
`aic-dc-gate` `Stop` handler returning `{}` — the doc says any decision other than `"continue"`
allows the stop — so at least one voice in the merge is always for stopping. That shipped on
2026-09-11 with [AG-19](decisions.md#ag-19), carrying this entry's own warning that whether it *wins*
against a concurrent `"continue"` was **unverified and worth a probe before relying on it**.

**Probed 2026-09-12, and it loses.** `scripts/probe_agy_stop_merge.py`, two hooks in one isolated
`hooks.json`, the same turn, both key orders:

| | Ours ran? | Turn | |
|---|---|---|---|
| rival `continue` alone | — | **held open** (17.6s against ~1.6s) | the harm, reproduced |
| ours `{}` **first**, rival second | yes, 1× | **held open** (19.5s) | our vote is cast and ignored |
| rival first, ours `{}` second | **no, 0×** | **held open** (18.6s) | our handler is never asked |

So `continue` wins irrespective of order, and a `continue` **short-circuits the handlers after it** —
being registered is not the same as being asked. The shipped `Stop` handler keeps its place for a
different reason: the payload carries `terminationReason`, which is how this host tells a loop it
ended from one a stranger's hook ended, and there is nowhere else to read it. It is a report, not a
veto, and the docstrings that called it a vote have been corrected.

**The second mitigation is therefore the load-bearing one, and it was already the behaviour:** keep
the `PreToolUse` refusal armed after the stop rather than clearing it at turn end, so a revived loop
reaches the tree through a gate that is still saying no. `AgyGateServer.resume` is called "when a new
turn starts, never mid-turn" (`src/aic_dc/agy/gate_server.py`) — the right shape, and now pinned by
`TestTheRefusalOutlivesTheTurnItStopped` rather than left as a property nobody asserted. This is why
the risk stays **moderate**: a revived loop costs spend and prose, not the working tree.

### The two mechanisms fight, and the loop is what spins

The question the merge probe left open — *whether `PostInvocation`'s `terminate` beats a concurrent
`Stop` `continue`* — was measured the same day by
`scripts/probe_agy_terminate_vs_continue.py`. **It does not.** Four runs, one prompt, both controls
holding:

| Run | Held open | Invocations | |
|---|---|---|---|
| `terminate` alone | no, 2.2s | **1** | the whole of AG-19, working |
| `continue` alone | **yes**, 18.9s | 0 | the turn hangs |
| both, ours first | **yes**, 16.2s | **8** | |
| both, rival first | **yes**, 19.1s | **8** | |

The contested runs are a **ping-pong**, and the logs show it exactly: `invocationNum` 0→7, every one
ended by us and revived by the rival, all eight `Stop` payloads reading `TERMINAL_CUSTOM_HOOK`. So
`continue` wins — and the interaction is worse than either mechanism alone. Without AG-19's
terminate the turn simply **hangs** (one `Stop`, then nothing). With it, the turn **cycles**: eight
model invocations in sixteen seconds, spending on every one, bounded only by `--print-timeout`.

**This is not an argument against the terminate.** Unopposed it ends a loop in a single invocation,
which is AG-19 met. It is an argument that a stop the user pressed can be overridden by a
configuration they may not know they have, and that the app has no ceiling underneath it.

**Mitigated only by being made legible, for now.** `AgyGateServer.decide_invocation` counts its
terminations per conversation and warns on the second — *"the loop was revived after AIC-DC ended
it, so the user's stop is being overridden"* — once per turn rather than once per cycle. It does not
act: ending the process is an explicit user escalation ([AG-19](decisions.md#ag-19)), and bounding a
turn's wall clock is a decision above that class. It keeps answering `terminate` every cycle, because
answering anything else hands the revived loop what it wants.

**Answered the same day, and the answer is not a ceiling.** A turn on this transport still has no
wall-clock limit, deliberately: `--print-timeout 12h` exists so a permission dialog can outlast a
human reading a diff, and a cap that fired on its own would eventually fire on a legitimate turn
waiting in one. What shipped instead is the shape AG-19 had already named — *"offer a separate
force-reset for a genuinely runaway generation"*:

- `AgySession` records when ⏹ was pressed and, if the turn is still producing frames
  `STOP_OVERDUE_SECONDS` later, emits **one** `systemEvent` of subtype `stop_ignored` carrying the
  elapsed time and whether the gate's loop was *revived*. Ten seconds, because both things that get a
  turn there are far faster when they work — an armed stop ends the loop in one invocation at 2.2s,
  and a revived loop cycles at about two invocations a second.
- The browser renders it as a durable card, and **only the revived case offers the button**. A turn
  held open by prose finishes on its own and costs words; restarting the engine to save a few seconds
  of text would end a session the user is holding, which is the trade AG-19 refuses to make for them.
- Nothing is ended automatically. The report is per turn rather than per frame — a turn being
  overridden emits continuously, and one card per frame would bury the message it is delivering.

The limit is stated rather than discovered: this is checked **per frame**, so a turn emitting nothing
at all cannot be reported on. Both conditions it exists for emit continuously, so what it misses is a
turn that is idle — a different problem, whose name is `--print-timeout`.

**One thing still not settled.** Why `status` says `SUCCESS` for a turn that never finished: a
held-open turn returns `status: "SUCCESS"` with partial output once `--print-timeout` expires, which
is what made the first two runs of the merge probe disagree with each other. A host reading `status`
alone cannot tell a completed turn from an abandoned one.

**Three instrument defects came first, and they are the reason the measurement is trustworthy.** The
rival hook's answer was built into a shell command where a backslash inside single quotes is literal,
so it printed `{\"decision\":…}` and `agy` read no decision from it — the probe reported that
`continue` does nothing, about a hook that had never said `continue`. Then an unhandled
`TimeoutExpired` killed the run that first reproduced the harm. Then `status` was read as the signal.
The probe now self-tests its own stimulus before trusting anything downstream of it, which is the
check that would have caught the first and cost nothing.

**Measured 2026-09-10, which sharpens both the risk and the tripwire.** The `Stop` payload carries a
`terminationReason`, and the values seen are `NO_TOOL_CALL` for an ordinary end and
**`TERMINAL_CUSTOM_HOOK`** for a loop ended by a hook — so a host can tell *which* hook ended a turn,
and [AG-19](decisions.md#ag-19) now depends on that distinction to render a user's stop honestly. It
follows that the `Stop` handler AG-19 installs is the mitigation this risk asked for, arriving for a
different reason: this app will be a voice in the merge on every turn rather than only when defending.

**Tripwire.** A conversation whose `result` frame has been delivered and whose tab has settled emits
a further `step_update`. The stream reader knows both facts and today does not compare them; a
warning naming the conversation is enough, because the case is a user's hook fighting the stop
button and the useful thing is to say so rather than to fix it silently.

**A `Stop` handler shipped 2026-09-11** with [AG-19](decisions.md#ag-19), on the same registration,
and `hook.report_stop` returns a literal `{}` rather than anything derived from the socket — so no
host bug and no failure can reach the `"continue"` this risk is about.

*It was written here, that day, as this app being "a voice for stopping on every turn". **That is
false and the correction is above**: measured 2026-09-12, a rival `continue` wins in either order,
and when it runs first this app's handler is not run at all. The handler earns its place as the
only reader of `terminationReason`, not as a vote. The sentence is replaced rather than deleted
because it is the claim the next reader would otherwise have reasoned from — and because believing
the shipped documentation for a day, on a mechanism this entry had itself flagged as unverified, is
the mistake worth leaving visible.*

What is now built is the detection this asked for, keyed on the field measured the same day:
`AgyGateServer.note_stop` compares the `Stop` payload's `terminationReason` against its own record of
having answered `terminate`, and a `TERMINAL_CUSTOM_HOOK` on a conversation this app never terminated
logs a warning naming the conversation and this risk. That is a user's hook ending our loop, detected
without waiting for the late `step_update` the paragraph above describes — and it is logged rather
than acted on, because it is a fact about the user's own configuration.

---

<a id="ag-r-17"></a>

## AG-R-17 — An engine may invent its own spelling of a shared field, and nothing says so

**Severity: moderate. Likelihood: realised — five fields, on both Antigravity transports, from the
day each was written. Three closed 2026-09-11, a fourth (`toolResult.content`) on 2026-09-12, the
fifth (`stop_reason`) the same day.**

[AG-R-4](#ag-r-4) says the browser must not learn engine names, and the tripwire built for it —
`TestVocabulary` in `tests/test_antigravity_steps.py` — asserts that every **event name** an
Antigravity pump emits is one the Claude pump also emits. It passed throughout. The drift was one
level down, in the **fields** on `streamComplete`:

| Antigravity pumps said | The Claude pump says, and the browser reads | What was lost |
|---|---|---|
| `num_tool_calls` | `tool_calls` | The turn footer's *"N tool calls"* — `chat-panel/block-render.js` `renderTurnFooter`, and the HUD's turn row |
| *(absent)* | `permission_prompts` | *"M asked"* on the same line. **Counted and never sent** — the `stats` object was shared precisely so this could be attributed to a turn |
| `response_text` | `response` | Every settled assistant message's `content` (`chat-panel/streaming.js`), which took `''` on both transports |

**Nothing failed, and that is the risk rather than an aside.** Each consumer guards its read —
`Number.isFinite`, `typeof … === 'string'` — so a missing key renders as an absent stat or empty
string, never as an error. An event name that drifts breaks a call site loudly; a field name that
drifts goes missing in silence, on a payload no test cross-checked. This is the same shape as the
`read_only` defect (`AgySession` § *read_only*) and the `stats` `AttributeError` before it: a
contract between two modules that neither module states.

**Mitigation, shipped 2026-09-11.** `TestTheFooterSpellsFieldsTheClaudePumpsWay` asserts that every
key on either Antigravity pump's `streamComplete` is a key the Claude pump's source also emits,
checked by quoted literal — the same weaker-and-honest match `TestVocabulary` uses. Divergences must
be added to `THEIRS_ALONE` **with the reason they may differ**, so the next one has to argue for
itself rather than appear.

**Two entries went on that list, and the second was an open question rather than a settled
difference:**

- `request_id` — ours, not the browser's. It reads the request id from the RPC callback argument.
- `stop_reason` — the Claude pump spells this **`terminal_reason`**, which `computeTurnOutcome`
  reads and turns into a red LED for anything neither empty nor `completed`. So renaming it would
  change what colour a failed `agy` turn draws, and `agy`'s own status words (`ERROR`, `CANCELED`)
  are not that vocabulary. **It is a mapping to be designed, not a spelling to be fixed.**

### The fifth divergence was the expensive one — mapped 2026-09-12

The second entry is now closed, and closing it showed the entry had **understated what was lost**.
The question was recorded as one of presentation — *what colour does a failed `agy` turn draw* — and
the answer turned out to be that it drew the colour of a turn that worked.

**Neither Antigravity footer carries `is_error`.** The Claude pump sends it and the browser checks it
first; these two pumps do not, and never did. So `terminal_reason` is not *a* route to a red LED on
these transports, it is the **only** one — and it was arriving under a name with no reader. Every
failure either transport could report rendered identically to a success:

| The engine said | The panel drew |
|---|---|
| `agy`: `status: "ERROR"` — the turn failed | green LED, no badge |
| `agy`: `status: "CANCELED"` — stopped, or a headless permission denial | green LED, no badge, prose settled as a completed answer |
| SDK: `MAX_TOTAL_TOKENS_EXCEEDED` and the rest of the budget family | green LED, no badge |
| SDK: `QUOTA_EXHAUSTED` — out of quota | green LED, no badge |

The three fields before it cost stats. This one cost the verdict, which is why the severity of the
*family* is right even though each instance looked minor on its own.

**The mapping, in `antigravity.steps.terminal_reason_for`** — shared by both pumps, three rules:

1. **`""`, `SUCCESS` and `UNSPECIFIED` become the empty string.** All three are one engine's way of
   saying nothing happened worth naming, and the browser draws no badge for an empty reason.
2. **`ERROR` -> `engine_error`, `CANCELED`/`CANCELLED` -> `aborted_streaming`.** The only two places
   the vocabularies genuinely name the same thing.
3. **Everything else passes through lower-cased.** The badge table already renders an unmapped
   reason legibly — header, red, underscores to spaces — so the words are kept and only the case is
   normalised, because `SHOUTY_CASE` is a signature the browser could learn an engine by
   ([AG-R-4](#ag-r-4)).

Two decisions inside that are worth naming, because both were tempting the other way:

- **`SUCCESS` is not promoted to `completed`.** It is the obvious symmetry and it is wrong.
  `completed` draws a green check, and `agy` reports `SUCCESS` for a turn held open until
  `--print-timeout` expired — [AG-R-16](#ag-r-16), and the open question below. The tick would land
  on precisely the turn that did not finish, which is what `block-render.js` means by *"a badge that
  claims a clean finish is worse than no badge at all"*.
- **The `MAX_*_EXCEEDED` family is not collapsed into `max_turns`.** `MAX_MODEL_CALLS_EXCEEDED` is
  close enough to be tempting, and folding it in would throw away *which cap fired* — the one thing
  [AG-6](decisions.md#ag-6) wants from `BudgetConfig`, which it chose over a dollar cap precisely
  because a token cap names itself.

**`CANCELED` maps to one word for two different events, on purpose.** It is what `agy` reports for a
turn the user stopped *and* for a headless permission denial — measured in phase 0, exit 0, no error
key anywhere in the stream ([`sdk-surface.md`](sdk-surface.md) § *There is no permission channel*).
`aborted_streaming` is true of both: stopped short rather than failed. It is in
`CANCELLED_TERMINAL_REASONS`, so the `agy` pump now derives `cancelled` from the reason the same
one-line way the Claude pump does, against the same shared set. Before this, a turn `agy` cancelled
on its own — as opposed to one ⏹ cancelled, which the session latches — reported `cancelled: false`
and rendered as a completed answer that happened to say nothing.

**The tripwire gained a second half.** The existing one asks whether a key is spelled the Claude
pump's way; it cannot ask whether a *value* is one the browser has a label for, and a mapping onto a
word the browser has since renamed would look like it worked.
`TestTheTerminalReasonLandsInAVocabularyTheBrowserHas` reads `block-render.js` and asserts every word
`TERMINAL_REASONS` maps to appears in the badge table. **It failed on its first run** — matching only
quoted literals, it reported `engine_error` missing from a table that has a label for it, because the
reason *sets* quote their members and `REASON_LABELS` writes them as bare object keys. The fix was to
accept both spellings; the lesson is that a source-literal tripwire is only as good as its idea of
how the other side writes things down, which is the same weakness `TestVocabulary` records for
`streamChunk`.

`THEIRS_ALONE` is back to one entry — `request_id`, a genuine difference — on both tripwires.

---

<a id="ag-r-18"></a>

## AG-R-18 — `BRAIN_DIR` is pinned to this server's own `HOME`, which [AG-21](decisions.md#ag-21) moves the tree out from under **(discharged 2026-09-11)**

**Severity: moderate. Likelihood: certain on the day AG-21 is built, and silent when it happens.**
**Raised 2026-09-11**, while checking a reviewer's advice against the tree during
[AG-22](decisions.md#ag-22)'s consultation — not by the consultation, which never saw this file.

`agy/steps.py` holds the path to the vendor's artefact tree as a module constant, resolved at **import
time** from the *server's* environment:

```python
BRAIN_DIR = Path.home() / ".gemini" / "antigravity-cli" / "brain"
```

[AG-21](decisions.md#ag-21) spawns `agy` under a different `HOME`, and measured that the whole tree
follows it: `artifactDirectoryPath` came back as `<private>/.gemini/antigravity-cli/brain/<conversationId>`.
So on the day AG-21 ships, the vendor writes into the private root while both readers of this constant
look in the user's real one. AG-21's own *Still unmeasured* list does not name it, and it is not a
consequence of AG-21 being wrong — the constant was correct until the root moved.

**Two readers, and neither raises.**

| reader | what it does when the tree is not where it looked |
|---|---|
| `locate_generated_image`, called from `consultant.py` | Reports *"could not be found under `<BRAIN_DIR>/<id>`"* — a sentence that blames the model for a path this side got wrong. Phase 10's exit criterion is a picture verified inside the repository ([AG-R-3](#ag-r-3)), and it would fail as *"the model did not write one"* |
| `subagents.py`, scanning every `transcript_full.jsonl` under the tree | Finds **zero** files where it found 121, and a subagent transcript surface with no transcripts renders as a subagent that did nothing |

Both are this register's recurrence again: **the thing was broken and nothing said so.** Neither raises,
neither logs at `error`, and both read as the *engine's* failure rather than as ours — which is the
[AG-R-12](#ag-r-12) shape, one layer out.

**Mitigation.** The constant becomes a function of the active config root, derived from the `HOME` the
invocation was spawned with, which is the only thing that knows. The seam already exists on the harder
of the two readers: `locate_generated_image(brain_dir, conversation_id, image_name, …)` takes the
directory as its **first parameter**, so it is the callers passing a module constant that are wrong, not
the function. The transcript scan needs the same argument. This is small; it is invisible until looked
for, which is why it is recorded before AG-21 is written rather than after.

**Tripwire.** A test that spawns nothing: set a private `HOME`, resolve the brain directory the way the
code under test resolves it, and assert it lands under that root and not under `Path.home()`. The
assertion must be on the **resolved path for a given root**, never on the constant's spelling — comparing
strings is precisely how [AG-R-12](#ag-r-12) § *The second way it failed open* reported a gate `current`
while every call was being auto-approved.

**Discharged 2026-09-11, with AG-21 and before it.** The constant is gone; `steps.py` takes a
`config_root` per translator and `subagents.py` takes `brain_dir` on all three of its readers, both
resolved through `roots.brain_dir(root)`. The tripwire was built as written — three tests in
`test_agy_steps.py` that assert on the resolved path for a given root — and one of them goes further
than this entry asked: given **no** root, the translator must collect nothing *and log at `warning`
naming AG-R-18. Silence was the entire risk, so a version of the fix that fails quietly would be the
same defect in a new place.

---

<a id="ag-r-19"></a>

## AG-R-19 — The master turn awaits its browsers, and the slowest one sets the pace

**Severity: moderate. Likelihood: certain with a wedged or paused client, and it reads as a hung engine.**

Found while enforcing *no sink may be load-bearing* on the consultation path (migration step 1, and
[`delivery.md` § The sink stops deciding what the answer is](delivery.md#the-sink-stops-deciding-what-the-answer-is-2026-09-11)).
The bridge is fixed and tested; **the master pump is the same fault one layer out**, and it is measured
rather than reasoned:

| link in the chain | what it does with a slow consumer |
|---|---|
| `AcSession._emit` | `await emit(event)`, with only the *exception* absorbed. A consumer that never returns is not an exception |
| `service._dispatch` | `await self._event_callback(...)`, then — deliberately, per its own comment — runs engine state: `_sweep_ended_subagent`, and the `_turn_footer` assignment plus `_post_response_for_background` on `streamComplete` |
| `main._make_real_callback` | `await`s the jrpc-oo call proxy when what it returns is awaitable |
| `JRPCCommon.call_all_remotes` | `asyncio.gather` over **every** connected remote, so with collab clients the slowest browser paces all of them |
| `JRPC2.call` | Bounded, and this is the good news: the send is `create_task`'d and a `timeout_handler` fails the request after `remote_timeout` — `rpc.DEFAULT_REMOTE_TIMEOUT`, 120s here |

So the stall is **bounded at 120s per event, not unbounded** — the difference between a bug and a
deadlock, and the reason this is moderate rather than severe. It is also 120s of a turn that has already
produced its answer, which no user will read as anything but a hang.

**What the turn is actually waiting for, and it is not the network.** Measured a round into the
consultation below. `JRPC2.call` is a *synchronous* `def`: it `create_task`s the transmit and a timeout
handler and returns immediately, so **the socket write is already fire-and-forget**. The await comes from
`JRPCCommon.setup_fns`'s `remote_call` wrapper, which creates a future, calls that synchronous `call`, and
awaits the future — and the future resolves only when the *browser's JSON-RPC reply* arrives. Every client
handler is a thin re-dispatcher ending `return true`, whose value `call_all_remotes` packs into a
`{uuid: result}` dict that neither `_make_real_callback` nor `_dispatch` ever reads.

So the master turn blocks for up to 120s **waiting for an acknowledgement nobody consumes**, and the bound
is a browser-JS-thread bound rather than a network one.

The sting is in the second half. **Today's in-order delivery is an accident of that round-trip**: because
`remote_call` awaits the reply, only one `transmit` is ever in flight per remote, so `await ws.send(msg)`
is serialised by the waiting rather than by design. Dropping the await without putting a serialiser in its
place gives N concurrent `create_task(_transmit_message(...))` racing on one websocket, which breaks the
exact contract the migration order names as a must-not-break. *The acknowledgement is useless, and it is
also the only thing currently holding the ordering up.*

**The part worth more than the latency.** Engine state is sequenced *behind* the browser. `_dispatch`
puts `_sweep_ended_subagent` and the footer after the broadcast on purpose, and the reason given is
sound — a subagent that ended with a dialog open needs the terminal status to explain the denial that
follows. But it makes a *rendering* consumer a precondition for *engine bookkeeping*, which is the
conflation named in [`../7-future/blank-sheet-architecture.md`](../7-future/blank-sheet-architecture.md#the-conflation-underneath-the-shipped-defect):
one mechanism serving a presentation consumer and a data consumer, with the presentation consumer's
condition in front. The empty consultation tab is what that shape looks like when it fails.

**Correction, 2026-09-11: engine state must NOT move ahead of the enqueue.** This entry said it should,
"where it never needed a browser at all". That sentence was wrong, and a consultation caught it before it
was built — see [`delivery.md` § A consultation that refuted a line already committed](delivery.md#a-consultation-that-refuted-a-line-already-committed-2026-09-11).
`_dispatch` is **re-entrant**: dispatching one event synchronously causes the dispatch of another. Traced
end to end, `_dispatch(subagentEvent, terminal)` → `_sweep_ended_subagent` → `permissions.cancel_for_agent`
→ `_deny_unanswered` → `_announce` → `_broadcast(Event("permissionResolved", …))`. Bookkeeping is an event
*producer*, so running it before the enqueue would put the denial in the queue **ahead of the termination
that explains it** — a browser showing a dialog denied for a subagent that still appears to be running,
which is the precise failure the after-the-broadcast placement exists to prevent.

The resolution is better than the reordering would have been: **leave the statement order exactly as it
is.** Once `_dispatch` enqueues instead of awaiting, bookkeeping no longer waits on a browser *because
nothing does*. The conflation named above dissolves without moving a line, and causal order is preserved
for free, since the cause is enqueued before the bookkeeping that produces the consequence runs.

**Mitigation, and it is deliberately not the one used on the bridge.** The bridge could
schedule-and-briefly-wait because a consultation's frames have no ordering contract beyond *terminal
last*. The master path has one — in-order websocket delivery, which the migration order names as a
must-not-break — so fire-and-forget per event is wrong here. What fits is **one sender task per client,
over a bounded shared ring buffer read by a per-client cursor**:

- One sender per socket is what preserves order, and it must `await ws.send` **only** — never the RPC
  reply, which is the discarded acknowledgement measured above.
- The backlog lives once in a `deque(maxlen=…)` rather than once per client, so memory is O(buffer)
  rather than O(clients × buffer).
- A client whose cursor falls off the tail is **told to rehydrate**, not dropped. `get_current_state`
  exists on both services and already carries `pending_permissions`, so re-baselining is a supported
  operation rather than a loss. Overflow becomes cursor arithmetic instead of an emergency disconnect
  issued during an active send — which matters, because a wedged `ws.send` cannot be cancelled safely and
  `ws.close()` on a wedged socket hangs for the same reason the send does.
- Bound the buffer by **bytes as well as frames**. A `turnUsage` payload is a few hundred bytes and a tool
  result can be megabytes, so a frame count is not a memory bound.
- The rehydrate path needs [AG-R-20](#ag-r-20) first, or a routine rehydrate walks into a race that is
  rare today.

**What is settled by measurement, and what is only settled by argument.** Everything above the mitigation
is measured. The ring buffer is not: it came from a reviewer arguing against the per-client queues this
entry originally specified, and it won on memory and on overflow-as-arithmetic. The shape above is a
synthesis of both positions that the consultation has **not** attacked, because it stopped at two rounds.
That is where to look first if this proves awkward to build.

**Tripwire.** Not a unit test on `_dispatch` — an event-loop test on the artefact: a turn against a sink
that never returns must complete, and **a second client must receive its events at full speed while the
first is stalled**. The second half is the one that would have caught `gather`, and nothing in the suite
asserts it today.

A third tripwire, added by the correction above: a `subagentEvent(terminal)` that sweeps an open dialog
must reach a client **before** the `permissionResolved` it causes. That is the assertion which fails if
anyone moves engine bookkeeping ahead of the enqueue again.

**Correction, 2026-09-11 (second consultation): ordering is not what the sender buys, and the shared ring
is wrong.** The entry above justified a serialised per-client sender by saying the master path has an
in-order delivery contract that fire-and-forget would break. **That is false, and it is now measured.**
The mitigation survives; its stated reason does not.

In websockets 16.1.1, `await ws.send(str)` runs from task start through `transport.write()` **without a
single suspension point**. The `send_in_progress` guard is set only in the fragmented-iterable branches,
never for a `str`; `send_context()` reaches its `yield` with no await; and the one await —
`await self.drain()` — sits *after* `send_data()` and suspends only when `self.paused`. Since
`create_task` is FIFO, fire-and-forget preserves wire order even against a peer that never reads a byte.

    Probe A — 2000 concurrent sends of ~4 KB to a deaf peer, limits high=4096/low=2048:
        frames written before the stall bit:  2000/2000
        writes issued while transport paused: 1996
        peak transport write-buffer bytes:    8,040,846   (high-water 4096)
        write order == create_task order:     True

**What the sender actually buys is backpressure propagation, and the same probe shows why that matters
more than ordering did.** The high-water mark does not bound anything — it only flips a flag;
`transport.write()` never refuses — so a stalled browser costs unbounded memory *below* the application
layer, where no application-level buffer can see it. A single sender awaiting each send cannot issue the
next write while suspended in `drain()`, which forces the backlog up into a queue that can have a policy:

    Probe B — identical conditions, one task awaiting each send before the next:
        sends completed before the peer stalled the sender: 19/2000
        peak transport write-buffer bytes:                  4,022
        (concurrent shape, same conditions:             8,040,846)

A factor of two thousand, and it is the whole argument for the design. **The in-order contract is
preserved by the sender for free, but it is not the reason to build it.**

**The shared ring buffer was a false economy.** The entry above specified one `deque(maxlen=…)` read by
per-client cursors, on the grounds that the payloads are identical across clients and storing them once is
cheaper. Python already stores them once: a deque holds a *reference*, so N queues cost N pointers, not N
payloads.

    Probe C — 1000 frames of 10 KB, tracemalloc:
        payload actually allocated:                  10,000,000 bytes
        1 shared deque of 1000 refs:                      9,864 bytes
        +5 per-client deques on top:                     46,856 bytes
        ratio of 5-deque overhead to payload:            0.0047x
        identity check — same object in every deque:      True

0.47% for complete lifecycle isolation. **Per-client deques, then** — a stalled client's queue fills,
overflows and is evicted without any cursor shared with anyone, which removes in memory the coupling this
entry removes in time. A shared ring would have pinned every entry until the slowest cursor passed it.

**The overflow policy moved out of this entry entirely.** "Tell the client to rehydrate" is not a policy
that degrades badly; it is incoherent, because overflow is *defined* as the client not draining its
socket, so the channel the snapshot would travel down has zero throughput at the moment it is needed. It
is recorded as [AG-23](decisions.md#ag-23) — evict, do not rehydrate — together with the finding that
this policy and [AG-R-20](#ag-r-20)'s gate would have defeated each other, and the consequence that the
queue must be a strict FIFO with no coalescing, no prioritisation and no shedding.

**And the acknowledgement is deleted rather than bounded.** Events become JSON-RPC notifications: no
`id`, no future, no 120 s `timeout_handler` task per event. Liveness comes from the websocket's ping/pong
and from write-buffer depth, which Probe B shows flags a stalled peer within about nineteen frames.

**A tripwire this correction adds.** A test that issues two sends fire-and-forget and asserts wire order
would pass today and prove nothing, because ordering holds for reasons unrelated to the design. The test
that bites is on **buffer depth**: stall a peer, push more than the queue bound, and assert that
`transport.get_write_buffer_size()` stays within one frame of the low-water mark and that the client is
evicted with the documented close code — not that the frames arrived in order.

**Built 2026-09-11.** `src/aic_dc/broadcast.py` holds a `ClientSender` — one websocket, one bounded
`deque`, one task that awaits `ws.send` before taking the next frame — and a `Broadcaster` that owns one
per connected client. `main._make_real_callback` no longer resolves jrpc-oo's `call` proxy; it calls
`server.broadcaster.broadcast(event_name, args)`, which is a plain `def` and therefore cannot be paced by
anything. Events go out as JSON-RPC notifications: no `id`, no future, no 120 s `timeout_handler` task per
event. `service._dispatch` is **unchanged**, which was the point of the correction above.

Four departures from the mitigation as specified, all found by building it:

**The seam is `create_remote`, not `_run_admitted_connection`.** The plan named `collab.py`'s admitted
path because that is where `remote.uuid` and the websocket are both in scope. They are also both in scope
one level down, in `JRPCCommon.create_remote(ws)` — which *every* connection passes through, the collab
server's and the solo server's alike. Overriding it on `MaxSizeJRPCServer` covers both with one edit, and
`rm_remote` is the matching teardown on every disconnect path.

**The acknowledgement was carrying one signal worth keeping.** Deleting the reply also deletes the
`KeyError` that used to warn when no remote exposed `AcApp.<event>`, and the browser's JSON-RPC library
drops an unknown method *silently* when there is no `id` to answer with — measured in
`webapp/node_modules/jrpc/jrpc.js:491`, which returns without emitting anything. So a renamed handler
would have stopped events arriving with nothing anywhere saying so, which is the failure class this
register keeps hitting. `Broadcaster._warn_if_unexposed` puts it back, checked against the remote's `rpcs`
and warned once per client per method.

**And it fixed a drop nobody had noticed.** The old path resolved `self.call["AcApp.<event>"]`, and that
key does not exist until the client's `system.listComponents` reply has been processed — so every event
dispatched during the handshake window was dropped with a warning. The sender queues them instead: they
wait behind the handshake and arrive after it. `test_the_handshake_window_does_not_warn_or_drop`.

**Eviction cannot simply `await ws.close()`.** This entry already noted that a wedged socket's close hangs
for the same reason its send does, and that is exactly what happens: a peer not reading its socket does
not answer a close handshake either. `_close_socket` sends the close frame under
`wait_for(CLOSE_HANDSHAKE_TIMEOUT)` and aborts the transport when that expires, so evicting a stalled
client cannot acquire a second stuck task. Relatedly, `close()` **cancels** the sender task — which the
[consultation bridge forbids itself](../../src/aic_dc/antigravity/bridge.py) — and the difference is
stated there: cancelling a suspended send cannot corrupt the stream but cannot retract the frame either,
and on the bridge the stream continues. Here it does not, because this only runs while the socket is
being torn down.

**What the tripwire caught, and it is the one this entry specified.** `tests/test_broadcast.py` runs a
real `websockets` server against a peer that completes the handshake by hand and then never reads a byte,
with the transport's water marks pinned at 4096/2048 and both socket buffers at 2048. Mutation-tested
rather than assumed:

| mutation | what fails |
|---|---|
| `await ws.send(frame)` → `create_task(ws.send(frame))` | the clamp: `assert 1547187 <= (2048 + 4098)`. Probe B's factor of two thousand, reproduced as an assertion |
| overflow drops instead of evicting | the eviction and close-code assertions, and the byte-bound test |
| `_sweep_ended_subagent` moved ahead of the enqueue | the causal-order test — `permissionResolved` reaches the wire before the `subagentEvent` explaining it |

The frame-order test this entry warned against was not written. What was written instead is a **control**
for the tripwire — `test_a_deaf_peer_really_does_stall_the_sender` — because a "deaf" peer whose backlog
the kernel quietly absorbs is not deaf, and every assertion built on one would pass while measuring
nothing. That is the same trap two of [AG-R-20](#ag-r-20)'s tests fell into the same morning, caught there
by mutation and here by anticipating it.

**What this did not change, and should be looked at next.** `Collab._push_event` is a second, independent
route to `AcApp.*` — `clientJoined`, `clientLeft`, `roleChanged`, `admissionRequest`, `admissionResult` —
and it still resolves the `call` proxy and awaits it, with the same `gather` and the same 120 s bound.
It was left alone deliberately: those events fire on connect and disconnect rather than inside a turn, and
`_handle_disconnect` awaits them for a *recorded* reason — an earlier fire-and-forget version left the
broadcast tasks unstarted when `handle_connection` returned, so `clientLeft` never arrived. Moving it is a
separate change with its own tripwire. The consequence meanwhile is that collab events and engine events
are no longer ordered with respect to each other, which nothing depends on today — the roster and the turn
are unrelated — but which is now true rather than merely likely.

<a id="ag-r-20"></a>

## AG-R-20 — A reconnecting browser applies live events against a baseline it has not received yet

**Severity: moderate. Likelihood: every reconnect that overlaps a running turn, and it corrupts quietly.**
**Raised 2026-09-11**, by the [AG-R-19](#ag-r-19) consultation rather than by the risk it was consulted
about. This is shipped behaviour and is independent of anything AG-R-19 changes — which is why it is here
and not in layer 7.

`setupDone` runs on every connect, first or subsequent, and ends by calling `_fetchCurrentState()`. That
is an RPC round-trip, and the websocket is already live while it is in flight. So a reconnect that lands
mid-turn takes events **before** the snapshot they are relative to:

1. the socket connects and the client's handlers are installed;
2. the client asks for `get_current_state`;
3. the server, still mid-turn, emits events 101 and 102, and the client's reducers apply them at once;
4. the snapshot resolves — describing the world as of event 100 — and is applied over the top.

There is no gate. `state-fetch.js` does `const raw = await fn()` and applies the result, with nothing
suppressing or buffering inbound events while the request is outstanding. Whether the overwrite corrupts
anything depends on each component's reducer — one keyed by id will probably survive it, one that appends
will not — and **that half is untraced**, which is the honest limit of this entry.

**Why it is worth recording now rather than when it bites.** It is latent today because reconnects are
rare: a restarted server, or a refresh. [AG-R-19](#ag-r-19)'s mitigation makes a rehydrate the *routine*
answer to a slow client, turning a rare race into a regular one. **So the gate is a prerequisite of
AG-R-19's overflow policy rather than a follow-up to it** — the same relation [AG-R-18](#ag-r-18) has to
[AG-21](decisions.md#ag-21), and the second time on this plan that a prerequisite was found by consulting
about something else.

**Mitigation.** Buffer inbound events in the shell from socket-open until `_fetchCurrentState()` resolves,
then apply the snapshot and replay the buffer. Once AG-R-19's cursor exists there is a cheaper version
that does not need buffering at all: the snapshot carries the sequence number it was taken at, and the
client drops anything at or below it.

**Tripwire.** A webapp test that connects, holds the `get_current_state` response open, delivers two
events, then releases the response — and asserts both events survive in the rendered state rather than
being overwritten by the baseline.

**Built 2026-09-11**, in `webapp/src/app-shell/event-gate.js` and four lines of wiring in
`app-shell/index.js`. See
[§ The gate that had to have four ways out](delivery.md#the-gate-that-had-to-have-four-ways-out-2026-09-11).
Three things differ from the mitigation as written above, and each is a correction rather than a shortcut:

- **The gate holds from the snapshot *request*, not from socket-open.** "Buffer from socket-open" was
  tried first and was wrong: until a request has been made there is no baseline for anything to race, and
  a gate armed at construction holds — and then warns — on a page that never manages to connect at all. It
  also broke thirteen existing tests that call handlers directly, which is the tree saying the same thing.
- **The seam is own-property wrappers over the prototype methods**, one per server-pushed handler. jrpc-oo's
  `ExposeClass` resolves `classToExpose[name]` per call, so an own property wins every dispatch, while it
  enumerates the *prototype* when deciding what to expose — so all 37 handlers are gated in one place and
  the RPC surface is unchanged. `event-gate.test.js` asserts both halves of that, because the gate silently
  stops working if either changes.
- **There are four ways out, not one.** The snapshot landing is the intended one; a snapshot that *fails*,
  a snapshot that never arrives (5 s), and a flood that outruns the buffer (500 events) are the other
  three. Overflow **releases rather than drops**: releasing early is no worse than the behaviour before the
  gate existed, whereas a dropped event is a permanently missing row that nothing reports — which is the
  failure class this whole register keeps hitting.

**What the tripwire caught that reading would not have.** Each of the four exits was mutation-tested by
breaking the implementation and confirming the right test failed. Two did not bite on the first attempt
and both were the test's fault:

- The throwing-handler test made a **DOM listener** throw, and jsdom reports that as an unhandled error
  without it ever reaching the gate's `try`. It passed while asserting nothing. Rewritten against a plain
  host whose handler body throws.
- The failed-snapshot test passed with `.then` as well as `.finally`, because `fetchCurrentState` catches
  its own errors — so the gate was leaning on a property of a function three files away. The wiring now
  carries a trailing `.catch(() => {})` and a second test stubs `_fetchCurrentState` to reject outright,
  which is what makes `finally` load-bearing and checkable.

The remaining unbuilt half is the cheap version named above: once AG-R-19's cursor exists, the snapshot can
carry the sequence number it was taken at and the client can drop anything at or below it, with no
buffering. The gate stays either way — it is what makes the cursor's *first* snapshot safe.

**A condition on the escape hatches, added 2026-09-11 after the second [AG-R-19](#ag-r-19) consultation.**
The 5 s deadline and 500-event ceiling are safe *because* [AG-23](decisions.md#ag-23) evicts a stalled
client rather than rehydrating it. Under the rehydrate-on-overflow policy this entry was originally
written to support, they would have stopped being emergency exits and become the routine path: the
deadline expires while a 24.7 MB snapshot crawls down a socket nobody is reading, the gate releases, 500
buffered events apply to the **pre-snapshot** baseline, and the snapshot then lands on top of them. That
is strictly worse than having no gate, because it interleaves two inconsistent views of the world instead
of overwriting one with the other — and the sentence recorded above, *"releasing early is no worse than
the behaviour before this gate existed"*, would have been false. It is true for the ordinary reconnect the
gate was built for, and it stays true only while overflow means eviction.

**So the two are coupled in both directions, and the README's ordering note now understates it.** AG-R-20
is a prerequisite of AG-R-19's overflow policy, as recorded; but AG-R-19's overflow policy is also a
*constraint on AG-R-20*, and reintroducing rehydration without revisiting these two constants would
silently convert this gate into a state scrambler. Anyone raising the deadline to accommodate a slow
snapshot should read [AG-23](decisions.md#ag-23) first: the answer is not a longer deadline, because the
throughput available in that case is zero at any deadline.

**Measured, so the constants are not guesses.** `JSON.parse` of a real 24.7 MB transcript is **43 ms** in
V8 and `JSON.stringify` is **71 ms**, so a reviewer's prediction that main-thread parse cost would starve
socket reads is refuted — the gate's budget is spent entirely on transfer, and on healthy loopback a
full-size snapshot clears well inside 5 s.

<a id="ag-r-21"></a>

## AG-R-21 — Moving the master root orphans 189 conversations, and the cheap migration corrupts the originals

**Severity: moderate. Likelihood: certain, the first time [AG-21](decisions.md#ag-21) ships.**
**Raised 2026-09-11**, by the consultation about AG-21 rather than by AG-21 itself: neither AG-21 nor
[AG-R-18](#ag-r-18) names the existing tree, and both are written as though the private root were the
first root there had ever been. It is not. The user's real `~/.gemini/antigravity-cli` holds **189
conversation databases (98 MB)** and a 34 MB brain tree, accumulated by this app and by the user's own
interactive `agy`. The moment the master root moves, `--conversation <id>` on any of them fails, because
the vendor looks for the database under the `HOME` it was given.

**What actually breaks is narrower than it first looks, and that is measured.** This app keeps its own
transcript mirror, so history remains readable in the UI with the vendor tree untouched — the browser
does not read the vendor's databases. Two things do:

- **vendor-side resume**, which is the feature `--conversation` exists for; and
- **the subagent transcript scan** in `subagents.py`, which reads the vendor's files directly and is
  already the subject of [AG-R-18](#ag-r-18).

So the loss is "old conversations cannot be continued", not "old conversations are gone".

**The obvious migration is worse than the problem.** Hardlinking the tree into the new root — `cp -al`,
which is instant and costs no disk — was proposed during review and is **unsafe here**, measured:
all 189 databases are `journal_mode=wal` with live `-wal` and `-shm` sidecars, and every one has a link
count of 1. SQLite in WAL mode writes **in place, through the inode**. A hardlinked database is the same
database, so the first checkpoint taken by an `agy` running under the private root would write into the
user's real history — which is precisely the isolation AG-21 exists to create, inverted. The cheap
mechanism silently reconnects the two roots it was asked to separate.

**So the decision is clean slate, with the old tree left where it is.** New conversations live in the
private master root; the existing 189 stay under `~/.gemini`, are never written by this app again, and
remain readable in the UI through the mirror. If vendor-side resume of a specific old conversation is
ever wanted, it is a **one-time real copy** of that conversation's database and brain directory — never
a hardlink, and never lazily at resume time, because lazy migration means permanent runtime code that
has to understand the vendor's `conversation_summaries.db` to keep the index consistent.

**The generalisable part.** AG-21 was measured thoroughly against a *fresh* root and not at all against
the *populated* one that exists, which is the same shape as this directory's recurring finding one layer
out: the mechanism was probed, the artefact was not. A containment change is also a data-migration
change whenever the thing being contained already has data.

<a id="ag-r-22"></a>

## AG-R-22 — A denied tool leaves the model writing fiction, and nothing in the transcript says it was denied

**Severity: moderate, and it reads as high. Likelihood: every consultation that tries to reach the
network.** **Raised 2026-09-11**, from a probe that was nearly written up as a gate failure.

The consultant gate denies every tool. When the denied tool is `read_url_content` or `search_web`, `agy`
does not report the denial to the user and does not decline — **it answers anyway**, in fluent prose,
with invented page contents and invented search results, presented exactly as retrieved material would
be. A reader of the consultation tab sees a confident, well-formed answer and has no signal that the
model never left the process.

**This was nearly reported as a containment bypass, and the artefact is what stopped it.** The model
produced `example.com`'s HTML and plausible search results while the hook log recorded a `deny`, which
looks like a gate that leaked. The check was a nonce: a local HTTP server serving a random token, with
its access log as the evidence. Control run, hook absent — the model reported the token. Denied run —
the model said the tool was blocked, the token never appeared, and **the server logged zero GET
requests**. The gate held perfectly; the model was confabulating from memory. *Assert on the artefact,
not on the mechanism* cuts both ways — it is also what keeps a working mechanism from being condemned.

**It does not block [AG-21](decisions.md#ag-21), and it is not a containment defect.** Where the config
root lives cannot change how a remote model narrates a tool error; the perimeter did its job. It is
recorded here because the *product* consequence lands in this app's UI, not the vendor's: AIC⚡DC owns
the gate, so it is the one component in the system that knows for certain that a tool was denied, and it
currently keeps that to itself.

### Resolved 2026-09-12, and it was a data-loss bug rather than a missing feature

The entry above specifies a *presentational* fix — "a denial the consultation tab renders" — on the
premise that the denial was not on screen because nothing had been written to put it there. That premise
was wrong, and the reason matters more than the fix. **`agy` reports a failure in `tool_info.error` and
never in `tool_info.output`, and `AgyTranslator` read only `output`.** The gate's own sentence arrived in
the frame, in this app's words, and was discarded one line from the browser. Every denial and every tool
error drew the literal **"No output."** on its card — measured on *both* surfaces, a consultation denied
`read_url_content` and the master engine denied `view_file`, so this was never only a consultation
problem.

Four things were built, and each answers a question the first cut got wrong.

**The card says why.** `_error_message` reads `tool_info.error` as either a mapping or a bare string, and
a failed call with no output renders it. That alone closes the master-engine half.

**A refusal is told from a failure by two conditions, not one.** The pump is handed the consultation's
allowlist *and* `StaticPolicy.MARK`, and claims containment only when the tool is off the list **and**
the error carries our mark. The allowlist alone would report a hallucinated tool name, or a gate that
failed open and then failed on its own, as a gate that held — which is the one case where saying so is
worst. The mark is a fixed short token prepended by `StaticPolicy.of`, not a sentence sliced out of the
reason: the first cut matched `reason.partition(".")[0]`, which made a wire format a property of
copy-editing, and a "e.g." in the prose would have broken detection with nothing to notice. It is also
what a person now reads on the card — *"AIC-DC refused this tool call."* — instead of the vendor's
*"denied by pre-tool hook"* and nothing else.

**The notice fires at the first refusal, not at the end of the turn.** The first cut raised it when the
`result` frame was absorbed. A consultation that is stopped, times out or crashes never sends that frame,
so the prose it had already streamed stayed on screen with nothing against it — and a notice underneath a
thousand words the reader has already believed arrives after the damage. Once per consultation, because
the sentence is about the answer rather than about the call, and worded so it cannot go stale as later
refusals arrive: it says *anything else it reaches for will be refused too*, which is a property of the
policy rather than a count of what has happened.

**The model that asked is told too, above the answer and outside it.** The tab tells a human who may not
be watching; the agent that asked is blocked on the string and about to act. `ConsultantBridge` prefixes
a header naming every refused tool, and quotes the consultant's answer between markers carrying a
per-call nonce the consultant never sees. Reading order alone was theatre — the answer passes through
verbatim, so nothing stopped the consultant from writing the header itself and claiming it had been
refused nothing. With a nonce it cannot guess, a forged marker is just more quoted text.

**Measured live, three times, against the real binary.** P21: the card preview went from `""` to 298
bytes and one notice was raised. P22: two refusals in one consultation — `search_web` then
`read_url_content` — with the notice once, immediately after the first card and before any prose, the
second card carrying its own reason, and the header naming both. P23, after the rework: the mark in the
preview at 329 bytes, the notice scoped to the consultation's `agent_id`, and the fenced answer. In all
three the model declined honestly; the point of the mechanism is that this app no longer depends on that.

**One question the conjunction exists for cannot be provoked from the model side.** A review round argued
that the "hallucinated tool name" case is not closed by construction: if `agy` ran our hook *before*
validating the name, an invented tool would be refused with our mark and reported as containment that
held. P23's second arm asked a consultation to call `frobnicate_repository` and quote the error. It never
became a tool step — `agy`'s tool calls are constrained to its declared set, so the model reached for
`list_dir` instead, was refused, and then said in prose that the invented name "is not a declared or
available tool in this environment". So that arm of the hazard is unreachable through the model, and the
conjunction earns its keep on the remaining ones: arguments rejected before the hook runs, and a gate
that fails open and then fails for its own reasons. Incidentally the same run is the clearest evidence
the mark is legible to a reader: the model quoted it back verbatim, *"AIC-DC refused this tool call."*,
as the error it had been given.

**And then the mark turned out to be answering the wrong question.** A third round found that keying the
*grounding notice* on it made the warning conditional on this app having been the component that refused
the call. **P24 arm C** measured the cost: told to call `read_url_content` with `{"invalid_parameter":
42}`, `agy` rejected the call against the tool's own schema in 203ms and never ran our hook, returning
*"invalid arguments: missing properties 'Url', 'toolSummary', 'toolAction'"* — no mark in it. Under that
rule the tab said nothing, the header said nothing, and the consultation answered a question whose fetch
had failed. AG-R-22 again by another road.

The two questions are now separate. `_is_ungrounded` — a non-allowed tool whose call *failed*, whoever
stopped it — raises the notice and fills `ungrounded_tools`, because what a reader needs is that the
answer is missing a retrieval, not which component refused it. `_is_refusal` keeps the mark and the
narrower job it is actually good for: drawing the card finished on this app's authority when `agy`'s
state word would leave it pending. Attribution survives on the card, which carries the gate's marked
sentence whenever the gate is what answered.

**The posture is stated whether or not anything was refused.** The bridge header used to be empty when a
consultation reached for nothing, which is most of them. That reasoning had the risk backwards: the
consultation that tries and is stopped is the safe one — it learns it has no tools and in every live run
so far it then said so. The dangerous one never tries. Asked what a config file sets a timeout to, it
answers from its weights, calls nothing, is refused nothing, and the caller has no way to distinguish
"it did not try" from "it had nothing to try with". **P24 arm D**, 45 events and not one tool call,
is that shape. The sentence about what was reached for is now an addition to a posture that is always
said.

**The notice names no tool.** Naming the one that provoked it made it an inventory frozen at one entry
while further refused cards rendered beneath it, however carefully the rest was worded; the payload's
`tools` list went with it, replaced by the single `tool` that provoked it. The names are on the cards,
and the sentence is about the answer.

**One correction to the record above**: the mark is not *first*. `agy` prefixes its own framing — 35
characters on a tool step, 71 in prose about one — so a claim that truncation would have to cut inside
the first 29 characters was wrong. The property the rule actually needs is that detection reads the
error whole, before this app's own preview limit cuts the copy bound for the card, and it does.

### Reworked again 2026-09-12, after a fourth round: the mark stops being a detector

Round three left the mark doing one narrow job — settling a card on this app's authority when `agy`
sends no terminal state for a call it refused. A fourth round found that job leaking the same failure
the round before had just closed. **A barred call that fails without our mark never settles.** The
schema rejection of **P24 arm C** is exactly that shape: `agy` rejected the arguments before the hook
ran, so the frame carried an error and no terminal state, and the card kept its pending spinner for the
rest of the session. The tab therefore showed a call still in flight next to a completed consultation —
a reader who waits for it to resolve is waiting on a fetch that already failed, which is the AG-R-22
belief by another route again.

**One predicate now answers both questions, and it is not the mark.** `_is_barred` asks only whether
the tool is absent from the consultation's allowlist. *Is the call over* is `_is_barred` and an error
message; *did the answer lose a retrieval* is `_is_barred` and a failure. `_is_refusal` is deleted.
Neither question turns on who said no, so neither reads a string the vendor is free to reframe: the
mark is now only the sentence a person reads on the card, which is the one thing it is unambiguously
good at.

That also makes the truncation correction above moot rather than merely acknowledged. The reviewer's
arithmetic was right — a buffer that truncates a tail keeps `agy`'s prefix and cuts the mark, so a
detector keyed on the mark fails open in the one direction that matters. Nothing is keyed on it, so
there is nothing left to fail.

**The tab says the posture too, not just the header.** Round three made the bridge header state the
no-tools posture unconditionally, and left the tab still speaking only when something was refused — so
a human watching the tab of a consultation that never called a tool saw an ordinary agent working. A
`consultation_posture` system event is now pushed as the tab opens, before any output, carrying the
same statement in the tab's own voice. It raises no toast: it is a standing condition of every
consultation, and a notification per second opinion is wallpaper. The event-driven
`consultation_ungrounded` notice is unchanged and still lands at the first failed reach.

**Measured live, P25.** Arm E asked for two different barred tools in one consultation: the posture row
came first, both cards opened *and settled* (`opened=['view_file','read_url_content']`,
`settled=[…]`, `pending=0`), the notice fired once after the first, and the header read *"This one
reached for view_file and read_url_content, and got nothing back."* That last measurement also refutes
the round's first finding, which held that the bridge singulates and would name only one tool. Arm F
made no tool call at all: the posture row is present, `pending=0`, and 2748 output tokens of confident
review arrive under a header that says where they came from.

### Reworked a third time 2026-09-12, after a fifth round: the category that was not empty

The record above says every hole found so far was **silence** — the app failing to speak in a case
nobody had imagined — and the position taken into the fifth round was that silence and event are the
only two categories there are. That was wrong, and the counter-example is one line of arithmetic.

**A barred call was `failed` only if `agy` said `ERROR`.** `failed = state == "ERROR" or stopped`, where
`stopped` required an error message. So a release that reported a hook-denied step as `DONE` with the
refusal in `output` rather than `error` would have drawn a **green success card** for a call that
retrieved nothing — and, because `_is_ungrounded` takes `failed`, would have left the tab's notice and
the bridge's header silent as well. Not a gap in what the app says: a green tick under a call that was
refused is the app saying the opposite of what happened. A barred call that settles now fails whatever
word the vendor put on it.

**And `_is_barred` required a name.** `self._allowed is not None and name and name not in self._allowed`
— so a tool step this pump could not read the name of was *permitted*. The names come from the vendor's
own keys, `tool_name` and `tool_info.name`; a release that renamed either would empty every one of them,
and this predicate would then have called every tool in every consultation allowed, settled each card
green and told the asking model nothing had been reached for. An allowlist that cannot identify what it
is being asked about must answer no. The `and name` is gone, and a nameless call is listed to the asking
model as *"a tool it did not name"* — counted, and honestly not named.

**The escape is reported rather than normalised.** The reviewer's closing advice was "never let a barred
tool succeed": force `failed` true whenever the tool is off the allowlist. Taken literally that files a
containment failure as a refusal. A barred tool that comes back with *output and no error* did not fail
— it **ran**, which is the one observation that makes the bridge's unconditional *"nothing below was
read"* a false statement rather than a cautious one. So `breached` is carved out of the invariant: the
card keeps saying what `agy` said, a `consultation_breach` row fires beside it, and the bridge
**withdraws** the posture instead of appending to it. It has never fired; the point is that it can.

`consultation_breach` rather than the `engine_error` the first cut reused. That subtype renders as *"the
engine reported an error"* — the engine reported nothing, this app is reporting on the engine — and it
carries `collapse`, so the next error of its kind in the turn would have replaced the one row saying
containment failed.

**The stranded card, measured.** A card is drawn when `agy` names the call and settled when the call
reports; nothing closed the gap when the call never reported. **P26 arm H** provoked it for real —
`agy` killed the instant the first card was pushed, which is what a timeout, a crash or a token ceiling
looks like from here — and before the fix the tab kept a spinner under a finished consultation, which
reads as a retrieval still running. `settle_pending` closes every open card with *"the turn ended before
this call reported a result"* and adds the barred ones to `ungrounded_tools`. It is called from **two**
places, and the second is the one that mattered: a consultation never reaches `stream_complete` —
`AgyConsultant._run` iterates the frames itself and `ConsultantBridge._tab` ends the tab — so a flush
that lived only in the pump's footer would have fixed the master engine and left the surface the round
was about untouched.

**One claim of this app's withdrawn.** "Keying on the allowlist rather than on any vendor string" was an
overstatement: `TERMINAL_STATES` and `state == "ERROR"` are the vendor's words and the pump still reads
them. What the barred-implies-failed rule buys is that they are no longer load-bearing in the direction
that matters — a vocabulary change can now cost a card its promptness, not its truth.

**Noted and not built** — and built on 2026-09-14, in
[§ The tool the allowlist exists to permit](#ag-r-22-the-permitted-entry) below. The allowlist's one
*permitted* entry has the opposite failure direction. If `agy` renamed `finish`, the gate would deny the
consultant its own turn-ending tool and every consultation would run to the bridge's timeout. Barring an
unknown tool is safe; failing to recognise the control tool is not, and nothing detects it today.

**P26 arm G** re-ran the P25 cascade under all of this: posture row first, both cards opened and
settled, `pending=0`, the header naming `view_file` and `read_url_content`, and no breach row. The
rework changed what happens in the cases nobody has seen and nothing in the case everybody has.

### Reworked a fourth time 2026-09-12, after a sixth round: the third thing that can be true

The fifth round's fix for *the app being wrong* was itself wrong in three places, and the sixth round
found all three. Two of them are the carve-out eating its own invariant; the third is a category nobody
had named.

**The breach carve-out re-opened the hole it was carved out of.** It read
`breached = barred and not message and bool(output)`, and then dropped `failed` for that call on the
reasoning that a tool which *ran* did not *fail*.

- *Output beside an error was filed as a refusal.* One byte in `tool_info.error` — a command that writes
  to stdout and exits non-zero, a fetch that returns a page and warns that it truncated — flipped
  `breached` back to false, and the app then told the asking model that **nothing had been read** about
  material sitting in that model's context. The exact statement the breach alarm exists to prevent,
  reachable by *adding* an error message.
- *A refusal in `output` was filed as an escape.* The frame shape the whole invariant was added for — a
  release reporting a hook denial as `DONE` with the refusal text in `output` rather than `error` —
  produced `breached` true, `failed` false: a **green success card**, a containment alarm, and a
  withdrawn assurance, for a gate that had held perfectly.

The cut that settles it is the gate's own mark, used in the one direction it is sound. `StaticPolicy.MARK`
is this app's string; nothing else on the wire says *this app refused this call*. Its presence in either
field means refused. Output without it means the bytes came from somewhere this app did not write, which
is a breach. Neither means this app cannot say. Two earlier rounds removed the mark from the *lifecycle*
test and from the *ungrounded* test and were right both times — a card that waits for the mark spins
forever when it never comes, and a warning that requires it is silent exactly when the gate failed — and
neither of those is this: here the mark's absence is never read as containment, so it can only ever
strengthen what is said. A release that stopped carrying it would cost this app its certainty, not its
honesty. **P27 arm I** measured it present on the live wire, in `tool_info.error`, on both denied calls.

And a breach is now `failed` like everything else the allowlist bars. The card's word is binary, a green
one for an escaped call is indefensible, and what makes an escape different is said in its own row above
the card — where it cannot be mistaken for the vendor's opinion of the call.

**`failed` no longer asks the vendor about a call that reported an error.** `stopped` was barred-only, so
for every tool on the master engine, and for the consultation's one *permitted* tool, `failed` reduced to
`state == "ERROR"` alone: an error reported under a `DONE` state drew a green card and threw the reason
away one line from the browser. That is AG-R-22's original shape, surviving inside the fix for it.

**The fourth category: an indeterminate outcome resolved into a certification of safety.** Silence is an
omission; a false statement is a commission; a stale card is a clock that stopped. This is the app being
*certain* — and it is worse than the other three, because the header's whole value is that a model acts
on it. A barred call can end carrying neither the mark nor any output: rejected upstream against the
tool's schema, or killed between the request and the result. `settle_pending` wrote those into
`ungrounded_tools` and the header said **"got nothing back"**, certifying a clean policy refusal for a
`SIGKILL`. The app does not know that. It knows a subprocess stopped while a call was in flight, which is
a statement about its own view and not about what reached the model inside it.

So there are three lists, not two, and three sentences rather than one with an exception. `ungrounded_tools`
— refused, and *nothing below was read* is a fact. `breached_tools` — it ran, and the posture is
withdrawn outright. `unverified_tools` — *treat anything that looks retrieved as unverified rather than
as absent*, weaker than the first and stronger than the second, and the only one of the three that is
true of a subprocess this app watched die. The tab gets `consultation_unverified` beside the other two,
a warning rather than an error, because nothing is known to have gone wrong: what is missing is the
certainty, not the containment.

**P27 arm J** is the first live measurement of that state. `agy` killed the instant the `view_file` card
opened, on a question written to produce prose first — and unlike P26 arm H, an answer came back, so the
header was actually composed. It read *"It reached for view_file, and ended without reporting what came
back from that — so treat anything in the answer that looks retrieved as unverified rather than as
absent."* The card settled with *"Whether anything came back from it is not known"*, `pending=0`, and the
`consultation_unverified` row fired. Under the previous version the same run would have told the asking
model that a call nobody watched finish had been refused.

### Reworked a fifth time 2026-09-12, after a seventh round: the test was forgeable, and the paragraph contradicted itself

Two defects, and the first is the more serious because the mechanism was *working* — it was the evidence
it worked from that could be manufactured.

**A refusal was recognised by a string this repository publishes.** `StaticPolicy.MARK` —
*"AIC-DC refused this tool call."* — is written in `gate_server.py`, quoted in these specs, and asserted
in three test files. The pump tested for it in `error.message` or `output` and, finding it, filed the
call as `REFUSED`: nothing was read, say so in this app's own voice, to a model that will act on it.
Now take the one case the whole mechanism exists for. The gate fails open; a barred `view_file` runs;
it reads `src/aic_dc/agy/gate_server.py`; the output carries the mark, because the mark is *in that
file*. The escaped bytes are filed as a denial, the containment alarm never fires, and the asking model
is told affirmatively that nothing was retrieved — with the retrieved material sitting in its context.
The test was a **content** test wearing the grammar of a provenance test, and the content was public.

`StaticPolicy.stamped()` fixes it by minting `secrets.token_hex(8)` per consultation and writing
`[ref <nonce>]` into the reason immediately after the mark. `AgyConsultant._run` stamps the policy before
it starts the pump and hands the nonce to `note_consultation`, and `_is_our_refusal` matches on the nonce
alone. The nonce is never written down, never logged, and reaches a model only by the gate genuinely
refusing a call and the vendor genuinely carrying our sentence back — so matching it is a statement about
the wire rather than about a string's contents.

**A ledger was considered first and rejected, and the reason generalises.** The obvious alternative is to
record each denial the gate issues and reconcile it against the frames. The reviewer named it the
*authority trap*: a ledger records what the gate **ordered**, not what the engine **did**. An engine that
ignored a deny and ran the tool anyway would reconcile perfectly and be certified clean — the ledger
would answer *we refused it*, which is exactly what a breach looks like from the gate's side. The nonce
keeps the test on the wire, where the observation is, and only makes it unforgeable.

**And when nobody stamps, the app claims nothing.** An unstamped policy leaves the nonce empty and
`_is_our_refusal` returns `False` for everything, so every refusal classifies `UNVERIFIED`. That is the
correct fail-safe: an unauthenticated refusal is precisely the state `UNVERIFIED` names, and defaulting
it to `REFUSED` would restore the forgeable test by accident, in the configuration where nobody is
watching for it.

**The second defect was in the paragraph, not the pump.** P27 arm J's header, composed live, read:

> …a second opinion runs with no tools and no repository access, **so nothing below was read, fetched or
> looked up**. Treat any file contents, page contents, search results or live values in it as the
> consultant's own rather than a source's. It reached for `view_file`, and ended without reporting what
> came back from that — so treat anything in the answer that looks retrieved as **unverified rather than
> as absent**.

The first sentence asserts a fact and the third withdraws it, in one paragraph, in this app's own voice,
to a model that resolves the contradiction by picking a side — and the flatter grammar wins. The sixth
round's fix had qualified only the *added* sentence, on the reasoning that the posture still stood. It
does not: the posture does not merely state how the consultant was launched, it *concludes* from that
launch that nothing below was retrieved, and that conclusion is the one thing this call cannot support.
So `_grounding` now composes from two forms. The launch fact is unchanged and unconditional; the
conclusion is dropped whenever `unverified_tools` is non-empty, and the weaker claim in the third
sentence is left to carry the paragraph alone.

**The fixture was lying too, and quietly.** `tests/test_agy_consultant.py`'s fake `agy` interpolated
`SECOND_OPINION_POLICY.reason` at generation time — defensible while the reason was a constant, and
untenable once it carried a per-consultation nonce, because a fixture that spelled the reason would have
had to be handed the nonce, which is testing the pump against a string the test gave to both sides. The
fake now does what `agy` does: reads `hooks.json` from the config root it was spawned against, runs the
`PreToolUse` command, and reports what came back on its stdout. The refusal therefore travels the real
path — stamped policy → gate server → socket → hook → wire → pump — and the suite asserts a
`[ref <16 hex>]` tag in the rendered card that nothing in the test file wrote. That change also exposed a
test passing vacuously: with the fake asking the real gate, `denied="finish"` is *allowed*, and the
allowed-tool test asserted an absence of notices against a consultation that had drawn no card at all.
It now asserts the card first.

---

<a id="ag-r-23"></a>

### Reworked a sixth time 2026-09-12, after an eighth round: a refusal is not a secret

An eighth round found the defect in the seventh's own repair, and it is the sharpest finding this risk
has produced. The token was minted once per **consultation** and written into every refusal — and a
refusal is not a secret, because the entire purpose of the reason is that the model reads it. One denial
therefore hands the consultant a consultation-wide credential in plain text.

The attack that follows is short. The consultant is denied `view_file` and reads `[ref 168b04…]` in the
error. Later the gate fails on a different call — a bug, a socket that went away, a release that changed
the hook contract — and the tool that escapes returns anything quoting the conversation: a `run_command`
echoing the transcript, a subagent summarising the turn, a file the consultant itself wrote the sentence
into. The token comes home in the `output` of a call that really ran. `_is_our_refusal` runs **before**
the output test, so it matches, and the breach is filed as a denial. That is worse than the forgery the
token replaced: the gate's loudest alarm is disarmed by the gate's own words, and only in the case where
the gate has already failed.

Closed by making the token single-use and binding it to a tool. `Refusals.mint` issues a fresh
`secrets.token_hex(8)` per **denial** and remembers the tool it was issued for; `Refusals.spend` matches
only a live token whose tool matches, and consumes it. A token the consultant has already seen is worth
nothing the second time, and a token minted for `view_file` cannot vouch for a `run_command` that
escaped. The set is bounded at `Refusals.LIMIT = 64`, evicting oldest-first, so a consultation that
spends its whole turn being refused cannot grow it without limit. The spend is safe from double-counting
because `_tool` returns early on an already-settled call id *before* `_outcome` runs, so at most one
token is spent per call.

**The residue, which is stated rather than fixed.** A denial frame that is *dropped* — never delivered,
or delivered in a shape this pump does not read — leaves one live token outstanding for that tool. An
escape of the *same* tool could still reflect it. Shrinking that further means expiring tokens on a clock
or on the next frame, both of which trade a rare false-REFUSED for a common false-UNVERIFIED, which is
the wrong direction for a mechanism whose job is to be believed when it says *refused*.

Two corrections the round made to the reasoning here, both accepted. Per-denial tokens were argued against
on the grounds that a desynchronised counter would produce a false `BREACHED`; under this app's own code
it produces `UNVERIFIED`, because an unmatched token falls through to the output test and a refusal
carries no output. And the scheme needs only a **set** of live tokens, not a counter — there is nothing
to keep in step.

### What the wire actually looks like, measured (P28, 2026-09-12)

The eighth round asked for three raw frames to test a proposed discriminator between a vendor schema
rejection and a post-gate execution failure. P24's are not recoverable — that log records translated
events — so P28 produced fresh ones of each shape, dumping `tool_info` verbatim. Three live
consultations, `/tmp/agyprobe/p28.log`.

| shape | `state` | `duration_seconds` | `tool_info.parameters` | `tool_info.output` | error text |
|---|---|---|---|---|---|
| denied by this app's gate (`view_file`) | `ERROR` | 0.048 | echoed | **absent** | `tool call denied by pre-tool hook: AIC-DC refused this tool call. [ref 168b041b6b5eb600] …` |
| denied by this app's gate (`read_url_content`) | `ERROR` | 0.050 | echoed | **absent** | same sentence, `[ref ab7f67a9b349b06e]` — a *different* token |
| schema rejection, before the gate | **`DONE`** | 0.008 | **absent** | **absent** | `invalid arguments:\n- missing properties 'Url', 'toolSummary', 'toolAction'\n- additional properties 'url' not allowed` |
| ran and failed (`view_file`, no such file) | `ERROR` | 0.051 | echoed | **absent** | `declaring permissions: cortex tool view_file: … invalid tool call error (invalid_args) failed to read file: stat …: no such file or directory` |
| ran and failed (`read_url_content`, dead port) | `ERROR` | 0.921 | echoed | **absent** | `Failed to fetch document content at http://127.0.0.1:9/: … connection refused` |

Four things follow, and they matter more than the discriminator that was asked for.

**`output` is absent on every failing shape — not empty, absent.** The eighth round's Finding B assumed a
silent successful breach would arrive as `output: ""`, and proposed `output is not None` in place of
`if output`. The measurement says that repair is *safe* (it would misfire on nothing observed) and also
*useless*: this pump's own header already records `tool_info.output` missing from a **completed**
`find_by_name`, so a call that ran and printed nothing arrives with no `output` key either. `output` does
not separate ran from did-not-run in either direction, so no test built on it can close the hole.

**The signal that does separate them is `state` paired with the presence of an error.** A call that ran
and produced nothing is `DONE` with no `error`. Every call that did not run carries an `error`. That is
the discriminator worth having, and it is the one the fix should use.

**The proposed discriminator is refuted.** The round suggested schema rejections would be recognisable by
parser terminology. `invalid_args` appears verbatim in the *execution* failure of `view_file`, which
really did stat the file. What actually separates them is that a schema rejection echoes **no
`parameters`** — the arguments never validated, so they are never carried back — and returns in 8 ms
against 48–921 ms for anything that reached the gate or the tool.

**A rejected call reports `DONE`.** The frame shape the fifth round hypothesised — a failure wearing a
terminal success word — is not hypothetical; it is what a malformed call does on this transport today.
The invariant that a barred call which settles has failed *whatever word `agy` put on it* is load-bearing
in production, not defensive.

The round's regression also passed on the live wire: two denials in one consultation carried two
**different** `[ref …]` tokens, both were spent, and the tab said `consultation_ungrounded` with the
strong sentence — *"This one reached for view_file and read_url_content, and got nothing back."*

### Reworked a seventh time 2026-09-12, after a ninth and a tenth round: what the wire cannot tell you

The ninth round attacked the `output` test and the tenth resolved a contradiction the ninth left behind.
Both are settled and the design is frozen.

**`output` is not the test for whether a call ran, and was the only one.** `if output` is false for `""`,
so a barred call that escaped and printed nothing would have been filed `UNVERIFIED` — a warning where
the alarm belongs. The proposed repair, `output is not None`, is dead on the P28 measurement: `output` is
not *empty* on a call that retrieved nothing, it is **absent**, and this pump's own module header records
it absent from a **completed** `find_by_name`. It does not separate ran from did-not-run in either
direction.

What does separate them is `state` paired with *did the frame report a failure at all*. Every shape P28
measured that did not run carries an `error`; a call that ran to completion and produced nothing is `DONE`
with none. So:

```python
if output:
    return BREACHED
if state == "DONE" and not has_error:
    return BREACHED
return UNVERIFIED
```

`has_error` is **`"error" in info`**, not `bool(message)`, and that distinction is the ninth round's best
catch. `agy` is Go and its JSON omits empty fields, so an error whose message is the empty string arrives
with the message gone and the `type` still there. Reading the message would have called such a frame a
clean completion and manufactured a containment alarm out of a vendor serialising a default — the one
mistake in this method that invents a breach rather than missing one. The key survives an empty message,
an error spelled as a bare string, and an explicit `null`.

A third failure mode was raised and rejected on evidence: a sibling tool in a multi-call turn emitted as
`DONE` with an empty `tool_info` to drain the queue. The tenth round's own reading of the Cortex step
machine says no — a step reaches `DONE` only when the runner dispatched it — and P28 arm K is the
counter-example on the wire: two tools in one consultation were each put to the hook independently, each
minted its own token, and each came back `ERROR`. Not probed further.

### The blind spot, accepted and priced

A barred call that escaped the gate and *then* failed in the host — `stat: no such file`, `dial tcp:
connection refused` — really did execute, and lands `UNVERIFIED`. The ninth round called this a defect and
was right about the principle (**a breach requires execution, not success**) while being wrong about its
evidence: it cited P28 rows 4 and 5 as escapes, and those ran under a probe-only policy that *permitted*
those two tools precisely so the "ran and failed" shape could be captured.

The tenth round then found the ninth contradicting itself. The ninth had also written, of the signals that
would identify a pre-gate schema rejection, that *"treating unauthenticated vendor wire quirks as
equivalent to REFUSED re-opens the very hole that was closed; leaving unauthenticated pre-gate rejections
as UNVERIFIED is the only classification consistent with your threat model."* Both cannot hold. On the wire
a pre-gate rejection and an escaped-then-failed call are the same frame: a barred tool, a terminal state,
an error this app did not write, no token. Promoting one without the other means parsing the vendor's error
prose, counting milliseconds, or testing whether `parameters` came back — the exact heuristics the same
answer forbade.

Both stay `UNVERIFIED`, and the cost is stated rather than waved away. The tenth round's framing is adopted
in preference to the one this repository first wrote: this is **not** "the system working", it is an
accepted blind spot of an unauthenticated wire. The trade is detection soundness for alarm precision, and
the price is real — a catastrophic gate failure whose calls all fail in the host is reported as unverified
noise rather than as a breach, and a failed syscall is itself an oracle (`connection refused` versus a
timeout is a port scan; `no such file` versus `permission denied` is filesystem discovery), so the model
learns host state through the error channel whatever `output` says. What the app never does is *assert*
containment: `UNVERIFIED` already withdraws the header's conclusion. The blind spot costs the alarm, never
the honesty.

### Two smaller holes closed on the same rounds

**An empty answer used to swallow the containment event.** P28's malformed-call arm spent the whole
consultation on a rejected tool call and wrote no prose, so `second_opinion` raised *"Antigravity returned
an empty answer"* — a sentence that reads as a transport hiccup and invites a retry — while the tab
correctly showed `consultation_unverified`. The surface an agent is actually *blocked on* was the one that
lost the event, which inverts this mechanism's founding invariant. `_empty_answer_reason` now names the
unverified or breached tools in the failure. It matters beyond tidiness because a call with no output is
not a call with no effect: an unverified `read_url_content` carries data out in a URL whether or not a page
comes back, and an unverified `run_command` writes to disk without printing.

**The no-tools posture is now checked at the launch path.** `_no_tools_or_fail` refuses to start a second
opinion under a policy permitting anything outside `CONTROL_TOOLS`, because the header's sentence is true
of `SECOND_OPINION_POLICY` only by virtue of `finish` retrieving nothing, and a future edit adding a read
tool would leave the app asserting something false with every test still green — a *permitted* call raises
no refusal and no breach, so there is nothing to notice. A raised `ConsultationError` rather than an
`assert`, on the ninth round's catch that `assert` is stripped from the bytecode under `python -O`, which
is exactly the build that runs unattended.

The breach sentence in the header lost the words *"and returned output"* on the same change, since a breach
recognised by a clean `DONE` has none, and promising a reader output that is not there sends them looking
for it.

<a id="ag-r-22-the-permitted-entry"></a>

### The tool the allowlist exists to permit — built 2026-09-14

Everything above is about tools this mechanism *refuses*. The note left standing on the fifth rework was
about the one it **permits**: `CONTROL_TOOLS = frozenset({"finish"})`, whose failure direction is the
mirror image. Bar an unknown tool and a consultation loses a capability it should not have had. Fail to
recognise the control tool and the consultation cannot end its own turn — it runs to the bridge's
timeout, having spent the tokens, and the user is told Antigravity did not answer in time.

The note stopped there for a good reason, and the reason is the whole difficulty: permitting an unknown
control tool *because the name has the right shape* is exactly the reasoning this allowlist exists to
refuse. Any repair that widened the frozenset would be the defect rather than the fix.

**The answer is *report, never permit*, and it is affordable because of where the evidence comes from.**
Not the gate seeing a name it does not hold — by then the consultation has already been paid for. `agy`'s
**`init` frame** carries the binary's entire tool inventory by name, and it arrives during the handshake,
*before any prompt is sent*. No model turn, no subscription spend. So the check runs on every
consultation instead of living in a probe somebody has to remember to re-run after an upgrade — which is
the one moment a rename would ever arrive.

`AgySession` keeps the advertisement as `advertised_tools`; `AgyConsultant._run` subtracts it from the
policy's allowlist immediately after `session.start()`; anything missing is logged at `warning` and
handed to the translator as **`unadvertised_tools`** — a fourth list beside `ungrounded`, `breached` and
`unverified`, and the odd one out in that group, which its docstring says out loud: the other three are
things a tool call *did*, this one is a thing that was true before the first prompt was sent. It lives
there rather than in a local so that both surfaces obliged to explain a bad outcome read one source and
cannot disagree. The timeout now says *"This `agy` build does not advertise finish, which this
consultation was permitted and needs — most likely the CLI renamed it, so the consultation had no way to
end its turn"*, and `_empty_answer_reason` says the same, with a breach still leading when there is one
because a withdrawn posture is the more important sentence.

Three things it deliberately does not do.

- **It permits nothing.** A name the policy does not hold stays refused whether the binary advertises it
  or not — the objection above honoured rather than routed around, and asserted from the other side:
  a consultation offered an *advertised* `read_url_content` still lands it in `ungrounded_tools`.
- **It refuses no launch**, and that is measurement rather than nerve. **P24 arm D** answered without
  making a single tool call, so a missing `finish` makes failure *likely* and not certain; refusing to
  start would trade a diagnosable failure for a guaranteed one.
- **An empty inventory means *no claim*, never *no tools***. An `init` frame carrying no list — a binary
  older than the one measured here — must not make every consultation announce that everything it
  permits has gone missing. An alarm that fires on a working setup teaches a reader to ignore the next
  one.

**Measured while building it.** `agy` on this machine is **1.2.2** (this directory's captures say 1.2.0);
it advertises **57** tools with `finish` among them; `scripts/probe_agy_tool_inventory.py` reports
`unclassified: []`; and there is **no** tool-listing subcommand, so the `init` frame is the only free
oracle for the vendor's vocabulary that exists. Nine tests on the consultant and three on the session,
run against the shipped source first, where the timeout said only *"Antigravity did not answer within 2s.
The consultation was abandoned and its process stopped."* Written up in
[`delivery.md` § The two silences at the gate](delivery.md#the-two-silences-at-the-gate-2026-09-14).

**Tripwire.** `unadvertised_tools` is empty on every consultation against a build that still calls it
`finish`. If it is ever non-empty, the version this app was measured against and the version installed
have parted company, and the warning names the tool before the timeout does.

## AG-R-23 — A failed consultation deletes its own evidence

**Severity: low for correctness, moderate for diagnosability. Likelihood: certain, on every failure.**
**Raised 2026-09-11**, while checking what [AG-21](decisions.md#ag-21) did to `scripts/` — not by
building it, and not by a test, which is the point.

[AG-21](decisions.md#ag-21)'s ephemeral root is removed in a `finally`, and a `finally` does not know
whether the consultation worked. So the one case where somebody wants to read the vendor's tree — an
image the collector did not find, a transcript that is not where it should be, a turn that failed in a
way the frames do not explain — is exactly the case where the tree has already been deleted.

Before AG-21 this cost nothing, because the tree was under the user's own `HOME` and simply stayed
there. `scripts/probe_agy_consultant.py` still carried the sentence that assumed it: *"if the image is
missing, look in `<BRAIN_DIR>/<conversation_id>/`"*. That advice is now unfollowable, and the probe was
the only place in the repository that noticed — the test suite cannot, because every test that would
care builds its own root and keeps it.

**This is a trade, not a defect.** Keeping every consultation root would reinstate the litter
`sweep_consultations` exists to remove, and 228 KB per consultation is small but unbounded. The
question is only whether *failure* should be treated differently from success, and the answer is not
obvious enough to build unasked.

**Candidate mitigations**, cheapest first:

- **Keep the root when the consultation raised**, and log the path at `warning`. One condition in
  `ephemeral`'s `finally`. Bounded by the failure rate, and swept on the next start by
  `sweep_consultations`, which already removes anything it finds before this process opens one — so the
  evidence survives exactly until the next run, which is roughly the window in which anyone would look.
- **Copy the brain subtree out before removal**, which keeps the sweep simple at the cost of a second
  policy about where copies live and when they expire.
- **A debug flag**, which is the honest answer if failures turn out to be rare enough that nobody ever
  wants this — and the dishonest one if they are not, because a flag is only reachable by somebody who
  already knew to set it before the failure they wanted to investigate.

**Tripwire.** A test that fails a consultation deliberately and asserts something readable survives,
with the path named in the log. Assert on the **artefact** — that a named directory exists and holds the
conversation's files — never that a branch was taken.

## AG-R-24 — Every turn-boundary signal `agy` offers is the wrong one, and the plausible wrong choice fails open

[AG-24](decisions.md#ag-24) settles that a consultation from an `agy` master arrives as `call_mcp_tool`
and must be counted by its arguments. This is the other half: **what a "turn" is**, for a quota of one
consultation per turn. Every candidate signal in `agy`'s hook payloads is misleading, and the one a
careful reviewer picked is the one that silently permits unlimited consultations.

**Measured 2026-09-12**, two turns in one conversation, dumping handlers on all three events:

```
PreToolUse      stepIdx=2                  view_file      (schema read)
PostInvocation            invocationNum=0
PreToolUse      stepIdx=4                  call_mcp_tool  msg=TURN-ONE
PostInvocation            invocationNum=1
PostInvocation            invocationNum=2
Stop                                       executionNum=0
PreToolUse      stepIdx=9                  call_mcp_tool  msg=TURN-TWO
PostInvocation            invocationNum=0
PostInvocation            invocationNum=1
Stop                                       executionNum=0
```

- **`invocationNum` is not a turn counter and keying on it fails open.** It restarts at 0 each turn *and
  increments repeatedly within one* — three times in turn one, twice in turn two, once after each model
  step. A counter reset on `PostInvocation` is therefore reset several times per turn, and the defeating
  sequence is the ordinary one: consult, `PostInvocation` arrives, counter resets, consult again. "One
  per turn" becomes "one per model step", enforced by code that looks like it is enforcing something.
- **`stepIdx` never rolls back, so a rollback fallback is dead code.** It is monotonic across the whole
  *conversation*: turn one used 2 and 4, turn two opened at **9**. A fallback conditioned on
  `stepIdx <= last_seen` can never fire, so the lock-out it exists to prevent is exactly what happens if
  the primary signal is ever dropped — a quota that refuses every consultation for the life of the
  conversation, and a feature that stops working after one use.
- **`Stop` is the only one-per-turn signal**, firing once at the end of each turn with `executionNum: 0`.

**The mitigation is to stop inferring.** The gate is this process's own in-process server, inside the
session that dispatches the turn, so the turn identity is ours already and does not need to be
reconstructed from vendor telemetry. Reset the counter where the prompt is dispatched. Every
hook-derived key makes a security control depend on a vendor event arriving, and the failure of that
delivery is invisible in both directions. `Stop` is worth keeping as a *corroborating* signal only if a
disagreement is an assertion rather than a log line nobody reads: `dispatch_count == stop_count` in the
integration suite, and a recorded desync — a `Stop` outside a turn, or a second `Stop` within one —
surfaced in session diagnostics, because a second `Stop` before the next dispatch would mean `agy` had
completed a turn this host never dispatched.

Three further conditions, all from the reviewer and all accepted. The check-and-set must be **atomic**,
because a model may emit parallel `call_mcp_tool` calls in one step and a non-atomic `if count < 1`
approves both. **Subagents must not spend the master's quota** — `invoke_subagent` children get their own
`conversationId` and can outlive the turn, so a consultation belongs to the master conversation or it is
denied. *(Closed 2026-09-12, after first being recorded as unbuildable: see the measurement below.)* And a reactive wakeup — a background `run_command` completing and waking the agent with no new
prompt — shares the dispatching turn's single consultation, which is the intended reading of a *user*-turn
quota but should be a stated one rather than an accident.

**Tripwire, and it must be a positive artefact.** `mock_server.call_count == 1` is an assertion on
absence dressed as a count: it passes if the second call was never attempted, if routing broke, or if the
process died. Prompt the master to consult twice in one turn and assert three things that only exist when
the quota actually fired — a structured `QUOTA_DENIAL` record from the handler, exactly one request at the
consultant server, and the denial string present in the *model's* transcript at the second
`call_mcp_tool` step. `agy` returns a refused hook's reason into the transcript as
`tool call denied by pre-tool hook: <reason>`, so that third assertion fails loudly whether the quota
failed open or the model never made the second call.

**Concurrency, measured rather than assumed (2026-09-12).** The quota is an atomic check-and-set under a
per-grant lock, which counts correctly under concurrency but does not *serialise execution*: two calls
arriving together would both pass and run two consultations side by side. Whether that is reachable is a
property of `agy`'s planner, so it was measured instead of argued. Prompted explicitly to issue two
`second_opinion` calls at once and not to wait for the first, `agy` **serialised them** — the second
`tools/call` arrived 2.7 seconds *after* the first returned, and peak concurrency inside the handler was
**1**. So the execution lock is not needed today. The limit is worth stating: one prompt, one model, one
run, and nothing on this side depends on that ordering — the lock protects the counter, which is the part
that would be wrong if a later `agy` did fan out.

---

## AG-R-25 — The MCP session table is reclaimed in exactly one place, and it is not the one that runs

**Measured 2026-09-12 against `mcp` 1.29.0.** Ten spawns of `agy`'s observed handshake — the rejected
`server/discover`, then `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, and a clean
`DELETE /mcp` — left **twenty** transports resident in `StreamableHTTPSessionManager._server_instances`
and reclaimed none of them:

```
spawn  1: discover_sid=yes after_discover=1  after_init=2  DELETE=200 after_delete=2
spawn  2: discover_sid=yes after_discover=3  after_init=4  DELETE=200 after_delete=4
     ...
spawn 10: discover_sid=yes after_discover=19 after_init=20 DELETE=200 after_delete=20
```

Two separate leaks, and they need two separate fixes because **one is fixed by the idle timeout and the
other is made permanent by it**:

- **The rejected `server/discover` registers a transport.** `agy` opens one on every spawn, the SDK
  answers 400, and no `DELETE` ever follows it — nothing is left that would ever close it.
- **A cleanly terminated session is never removed.** The SDK pops a session in exactly one place, the
  idle-scope path, and its other cleanup is guarded by `not http_transport.is_terminated`. An explicit
  `DELETE` sets that flag, so the guard skips the very sessions that were closed properly. Measured
  directly: after a TTL fired, `discover(400) transports still resident: 0/3`, `DELETEd sessions still
  resident: 3/3`, with `is_terminated=True` on each. **The tidy path is the leaking one.**

**The mitigation is both halves.** `session_idle_timeout` is set on the session manager — FastMCP has no
settings key for it, so it is assigned after `streamable_http_app()` and before any traffic, since the
deadline is read when a session is created — and `sweep_terminated()` drops terminated entries at the
spawn boundaries, which is where the leak is generated, so the table stays flat across a long-lived host
without anything having to wake up.

### The reversal, recorded because the correction came from measurement on both sides

A reviewer prescribed an idle TTL. It was then **dropped entirely** on the strength of one measurement —
`agy` never rebuilds a session the server has forgotten, reporting `failed to connect (session ID: …):
session not found` and giving up — with the reasoning that any TTL shorter than a conversation is a bug
generator and any TTL longer is inert. The reviewer called that an over-correction, on the grounds that
eviction being fatal had been shown while sessions surviving long idleness had not.

Both halves were then measured. **P1**: one `agy` process, a turn, a 900-second gap with no traffic
whatsoever, then a second turn — the same session id was reused, with no re-initialize and both TCP
connections held open throughout. So idleness is survived, and a TTL of minutes would be a bug generator.
**P8**, above: with no TTL nothing is ever reclaimed, so a TTL is not inert.

The over-correction was real, and the specific error is worth naming: the TTL was being judged as a
*correctness* mechanism, which the eviction measurement does refute, when it is a *garbage collection*
mechanism, which only the leak measurement could settle. Four hours is past any gap a live conversation
plausibly has and still reclaims an abandoned one the same day.

**The residual cost is stated rather than hidden.** A conversation left idle longer than the TTL loses its
consultant for the rest of its life, because of the eviction measurement above — `agy` will not rebuild
the session. The failure is loud (`session not found` reaches the user), and the remedy is restarting the
engine, but it is a real degradation and not a theoretical one.

**The subagent clause, closed on a measurement that reversed a ruling (2026-09-12).** When the listener
was built, this clause was recorded as unbuildable and the quota shipped at ◑: the gate sees one bearer
per `agy` spawn, and a bearer names a session, not which agent inside it is asking. Measured on a real
`start_subagent` delegation, that is false in the only place it matters. The delegate's `tools/call`
reached the **same** MCP session id, over the **same** bearer, with the **same** HTTP headers — nothing at
the transport told them apart — but `params._meta` did:

```
master's own call:  {"antigravity.google/artifacts_dir":            ".../brain/51e16a25-…",
                     "antigravity.google/conversation_id":          "51e16a25-…",
                     "progressToken":                               "d95f27e9-…:4"}
subagent's call:    {"antigravity.google/artifacts_dir":            ".../brain/dfa77e15-…",
                     "antigravity.google/conversation_id":          "dfa77e15-…",
                     "antigravity.google/parent_conversation_id":   "51e16a25-…",
                     "progressToken":                               "146bb0ad-…:2"}
```

`parent_conversation_id` appears on the delegate's call and nowhere else, and
`ctx.request_context.meta.model_extra` exposes exactly those namespaced keys. The listener refuses a
delegate **before** the budget check, so it spends neither an answer nor an attempt — which matters more
than tidiness, because `agy` can dispatch delegates concurrently, and delegates that could spend would
exhaust the master's two before the master reached the synthesis the budget was reserved for. The refusal
is instructional: report upward that no second opinion was available, so the agent that delegated can seek
one. Absent metadata means *not* a subagent, deliberately — every non-`agy` MCP client sends no `_meta`,
and failing closed would refuse the master.

---

**A second reclamation path, and what the sweep structurally cannot reach (2026-09-12).** Review held that
`sweep_terminated()` has a blind spot: a child that dies without closing its session leaves
`is_terminated=False`, and the sweep only collects `True`. Measured across six sessions — three closed
with `DELETE`, three simply walked away from, which is what `SIGKILL` looks like on the wire:

```
immediately:   resident 6;  DELETEd -> True,True,True ; walked-away -> False,False,False
after sweep:   swept 3;     DELETEd -> gone           ; walked-away -> False,False,False   (resident 3)
after the TTL: DELETEd gone ; walked-away gone ; resident 0 ; _session_owners 0
```

The blind spot is real in the narrow sense, and the two halves are **exactly complementary**: the sweep
collects precisely the set the TTL never will, and the TTL collects precisely the set the sweep never
will. Nothing leaks permanently, so what the proposed fix buys is up to four hours of latency rather than
correctness — the reviewer's diagnosis was right and their reason was not. It was built regardless:
`revoke()` runs on every child exit, clean or unclean, and now terminates and forgets every session id the
gate recorded under that bearer, whatever the flags say. `terminate()` is idempotent, so the two halves
may overlap.

Both tables it reclaims from — `_server_instances` and `_session_owners` — are private to the SDK, so a
rename in a later release would turn eviction into a silent no-op. The listener checks for both at startup
and logs by name if either is missing, because the failure this file keeps recording is not a leak, it is
a leak that says nothing.

## AG-R-26 — The delegate refusal is policy against `agy`'s dispatcher, not a boundary against local code

**Stated 2026-09-12, accepted rather than mitigated.**

[AG-26](decisions.md#ag-26) refuses a subagent's consultation by reading
`antigravity.google/parent_conversation_id` out of the `tools/call` `params._meta`. That works, it is
measured, and it discharges [AG-R-24](#ag-r-24)'s clause. What it is **not** is a security boundary, and
this entry exists because the difference is easy to lose: `_meta` is a field the caller fills in. Any
process holding the bearer token can send a `tools/call` with `_meta` omitted and be treated as the master.
The refusal is therefore *policy against a cooperating dispatcher* — it stops `agy` from spending the
master's quota through a delegate, because `agy` labels its own delegates honestly — and nothing more.

**The disposition is to leave it.** Not because the hole is small, but because there is no version of this
that closes it: the only party who could attest to a call's origin is `agy`, `agy` is closed source, and a
credential handed to a subprocess is a credential that subprocess's whole process tree holds. Every
defence that could be built here — nonces, per-call signing, a second channel — is defeated by the same
fact, so building one would buy the *appearance* of a boundary, which is worse than the documented
absence.

### The counter-argument that was offered for this, and the three ways it fails

The first draft justified the disposition by claiming that a bearer holder gains strictly less than the
capability they would have anyway, since anyone who can read `mcp_config.json` can already read
`~/.claude` and run `claude -p` directly. Review dismantled that, and the corrections are recorded because
each one is a fact about this system rather than a debating point.

1. **The bearer reaches something `claude -p` cannot: the live master turn's budget.** A holder can spend
   the two consultations belonging to a turn in flight, so the master's next call is refused and it
   proceeds on its own reasoning without its second opinion. That is a sabotage channel into a *running*
   conversation, and no amount of local `claude -p` access provides it.
2. **In the `agy` child, the bearer is a net-new capability rather than a lesser one.** `roots.PASSTHROUGH`
   is an allowlist and carries no Anthropic or Claude names, and `HOME` is set to the private root, so the
   child does not inherit credentials for the thing the token reaches. It is the *only* route to Claude
   that child has. (The claim is narrower than it looks: the gate auto-allows reads, so the child can still
   `view_file` the user's real `~/.claude` by absolute path. That is a separate matter from the token, and
   it is the read posture's to answer, not this one's.)
3. **On an egress-filtered host the listener is an egress oracle.** A loopback port answering
   authenticated requests, whose handler reaches the public internet, is a hole through a network policy
   that was configured to have none — and a deployment that filters egress is exactly the deployment that
   would care.

None of the three changes the disposition. All three change what the disposition is *based on*: it stands
because the attestation is unbuildable against a closed-source client, not because the capability granted
is trivial. The honest summary is that anyone who can read the token file can consult Claude on the user's
subscription and interfere with a running turn, and that the file's `0600` inside a `0700` directory is
the whole of the protection.

## AG-R-27 — A stop cannot un-send a request that is already on the wire

**Accepted, bounded, and not worth building against.** [AG-27](decisions.md#ag-27) establishes that ⏹
tears a consultation down: the task unwinds, the scratch directory goes, and the CLI subprocess is gone
from `/proc` within six seconds. What that cannot do is recall the HTTP request the CLI already sent.

The mechanism, agreed by both sides of the review: cancellation severs the TLS connection, and the API
notices on its next write. During *prefill* — prompt ingestion, before the first output token — the
server is computing and not writing, so a reset arriving in that window may not be observed until the
turn is done, and the turn may be billed. There is no out-of-band cancel on `/v1/messages`; dropping the
socket **is** the cancellation primitive, and no client-side machinery changes remote scheduling.

The blast radius is one turn, because `max_turns=1` means there is no follow-up to dispatch even if the
first one runs to completion unobserved. So the honest statement is that a stop is prompt against the
*local* consultation — which is what a user pressing ⏹ is asking for, and what the subscription-drain
worry was actually about — and best-effort against a single already-transmitted turn upstream. Recorded
rather than mitigated, because the only thing that would shrink the window is not sending the request.

## AG-R-28 — Two byte-identical consultations in one turn nest under neither call

**Accepted, bounded, and irreducible without a change to the CLI.** [AG-28](decisions.md#ag-28) anchors a
consultation's row to its spawning tool call by staging the real `toolu_…` in a `PreToolUse` hook and
letting the handler claim it back by matching every identifying argument. The join is on content, because
content is the only thing both sides hold: an in-process MCP handler receives its own `args` dict and
nothing else.

So two calls whose anchor arguments are identical — the same question over the same context, or the same
prompt at the same aspect ratio with no filename — are indistinguishable to the mechanism. `_claim` claims
only when exactly one staged entry matches, so both fall back to the minted identity and render as
free-floating rows at the turn level. That is the pre-AG-28 behaviour, logged at debug, and it is the
deliberate choice: the alternative is a coin toss whose lost half renders a live card under a *different*
call, beside that call's own result, disagreeing with it in the one place a reader is looking.

Two narrow paths could still mis-anchor rather than decline, and both require a staged entry to be missing
when its own handler claims. The first is an SDK that dispatches a handler without having run its hook,
which is outside this mechanism to defend against — the hook is awaited before dispatch. The second is
buffer eviction: `STAGE_LIMIT` is 16, so sixteen intervening unclaimed consultations in a single turn could
push one half of an ambiguous pair out and leave the other claimable. Consultations are user-facing calls
that run once or twice a turn, so this is recorded rather than defended against; raising the cap trades a
bound nobody has approached for a bound nobody would.

There is one adjacent path that is *not* new here. `observe()` reads the turn id live on each step, so a
consultation outliving its turn emits blocks tagged with the new turn, which the renderer's fallback would
spill into the main transcript. That was raised during AG-28's review as a regression and is not one: it
behaves identically with the minted scope and predates the anchor entirely.

## AG-R-29 — A breached consultation reads as successful to someone skimming a cold transcript **(closed 2026-09-12)**

[AG-29](decisions.md#ag-29) established that a containment breach *is* durable across a
reload, against a review that argued it was not: `_grounding` composes the retraction inside
the value `second_opinion` returns, so it is in the transcript on disk rather than only in a
notice row that cold reload never rebuilds. The model that asked is therefore told, every
time, in the one artefact that survives.

Two narrower versions of the argument survive the measurement, and both are about the
**human** reading a restored session rather than the model reading the result.

**The retraction is the second paragraph.** The returned value is assembled as
`"A second opinion from Google Antigravity (a different model, reasoning independently — treat
it as evidence, not as a verdict).\n\n" + _grounding(observer) + _fence(answer)`. A surface
that previews a tool result by its opening line — a collapsed card, a search hit, a snippet —
shows the reassuring boilerplate and puts the withdrawal of the assurance below the fold.
The ordering is defensible for the model, which receives the whole string; it is exactly
backwards for anything that truncates.

**A breach returns a successful tool result.** `_text` returns `{"content": [...]}` and sets
no `isError`, so the card for a consultation whose gate failed renders with the same neutral
completed styling as one that held. `_grounding`'s own docstring says the row in the tab
"has to be louder than the card" precisely because "the card beside it renders `agy`'s own
success, truthfully" — and on a restored turn that row is gone, leaving only the card it was
meant to be louder than.

Neither is a containment failure and neither misleads the asking model. Both are the same
shape of defect: the app knows something went wrong and says so in prose, in a place a
skimming reader does not have to look. The fix is small and is listed as unbuilt work —
hoist the retraction above the boilerplate when `escaped` or `unknown` is non-empty, and set
`isError` on a breach so the collapsed card carries a warning badge without an expand click.

Not a risk, though it was raised as one: that the live banner would proclaim containment over
a breach row. That is closed by construction in AG-29 — the banner reads a severity-monotonic
store rather than the container's identity, so a retraction cannot be overwritten by the
assurance it retracted.

### Closed, 2026-09-12

Both halves are built. **Ordering is priority, and priority moves when something is wrong**: the
grounding paragraph is hoisted above the "A second opinion from Google Antigravity …"
attribution when `escaped` or `unknown` is non-empty, so a truncating surface leads with the
withdrawal. It needed no rewording to do the job — it already opens *"Before the answer, from
AIC⚡DC and not from the consultant"*, which names its speaker and its position. The attribution
moves rather than goes away: the model still has to be told this is a second opinion and not a
verdict.

`reached` alone deliberately does **not** hoist, and that is the line worth holding. A
consultation that reached for a tool and was refused is one where the gate *worked* — it is the
ordinary shape and it happens constantly — so the paragraph still opens with an assurance that
holds. Promoting it would spend the lede on the common case and leave nothing for the one that
matters.

`is_error` is set on a breach and on nothing else. Not because the tool failed to answer — it
answered, and the answer is still passed through verbatim below — but because it failed to *be
what it promised*, which is the one thing the card beside it cannot show, since `agy`'s own
success renders truthfully. The browser turns that into the card's `error` status, and
`blockExpanded` returns `true` for an error card, so a breached consultation on a restored turn
now opens showing the retraction rather than waiting to be clicked.

**The accepted cost:** an `is_error` result may make the asking model retry the consultation.
That is bounded by the per-turn quota ([AG-26](decisions.md#ag-26)), and it is on a path the
notice's own comment calls "the only notice in this file that should never fire". A prior
measurement (E1, `consult_listener.py`) found that it is the *prose* and not the flag that
decides whether `agy` retries; that was about this app serving `agy`, not about Claude reading
this result, so it constrains the analogy rather than settling it.

## AG-R-30 — A background subagent's tab shows the tail of its work and calls it the whole

Found while building [AG-30](decisions.md#ag-30), in the code that change routes *around*
rather than through. It is a defect in the delegated-subagent path and a consultation never
reaches it, so this is a record rather than a fix.

`loadSubagentFeedIfEmpty` (`webapp/src/chat-panel/tabs.js`) fills a settled subagent tab from
its on-disk transcript, and declines when the tab already mirrored blocks:

```js
if ((tab.turnBlocks?.blocks?.length || 0) > 0) return false;
```

Its stated reason is sound and its gate is not. The doc comment says *"A tab with mirrored
blocks already has the better view: live records the transcript on disk has not caught up
with"* — a claim about **staleness**, and a true one, because disk lags a running subagent.
But the function opens with `if (!sub.settled) return false;`, so it only ever executes
against tabs where the subagent has finished and the lag is over. The rationale describes a
state the guard never runs in.

**The reachable sequence**, from three properties that are each individually deliberate:

1. A background subagent still running at turn end is **not settled** — `settleLiveSubagentTabs`
   skips any task the engine names in `background_tasks`, and the doc calls settling them
   "this bug's most visible face". So the fallback, which requires `settled`, cannot fire in
   the turn that spawned it.
2. Subagent tabs are **dropped at every send** (`clearSubagentTabs`), so that turn's tab and
   its blocks do not survive into the next turn.
3. Tab creation is **not limited to `started`**: *"Called for every `subagentEvent`, not just
   `started` … If that first event is already terminal the tab is created and settled in the
   same call."*

So the subagent's terminal event arrives during a later turn, creating a **fresh** tab and
settling it in the same call. That tab mirrored only the blocks that landed in the later
turn — the earlier ones went to a tab that no longer exists. It is settled, its block count is
non-zero, the guard aborts the read, and the user is shown the end of the subagent's work with
the beginning missing.

**Head-truncation, not tail.** This is the more dangerous half. A record cut off at the end
leaves visible seams — an unclosed thought, a tool call with no result — and reads as
"something stopped". A record missing its beginning starts mid-thought under a completed
status, so the user sees actions taken with no sight of the instructions or reasoning that
produced them, and nothing on the surface says anything is absent.

**Why it is not fixed here.** The honest fix is not deleting the guard: re-enabling the disk
read for settled tabs that hold partial blocks means reconciling two orderings of the same
work, and it runs straight into the specification's own open *Known gap*
(`specs5/5-webapp/subagent-browser.md` § Tab Lifetime), which says the rest of a background
subagent's output "lands mis-attributed rather than dropped" and that fixing it properly
"means routing each message to the translator that owns it rather than to whichever turn is
current, which is a change to the turn model rather than an addition to it". That is the work
this belongs to, and folding it into a consultation-rendering change would make one commit out
of two, with the harder half the one carrying no tests.

Listed as unbuilt work in [README.md](README.md).

## AG-R-31 — A consultation's tab has no way to be closed, and one opened inside a delegation opens blank

Two consequences of [AG-31](decisions.md#ag-31), recorded together because they are the two
places that change's reasoning stops rather than defects in what it built.

### There is no close affordance, and now there is a tab that wants one

A consultation's tab survives every send and is swept only by a session change. That is the
point — it exists so the second opinion can sit beside the composer while the reply to it is
written — but it makes this the first tab in the strip whose lifetime the user cannot end.
`tabs.js:264` records that `onTabClose` was **deliberately removed**, on the grounds that "both
kinds of tab that remain sweep themselves"; that is no longer true of all three kinds.

The exposure is bounded and slow. A tab appears only when the user clicks a consultation's name
button, there is at most one per consultation, and a session change clears the lot. A long
session in which many second opinions were each opened by hand ends with a strip the user can
neither thin nor close, and the only way out is to change session.

It is not fixed here because the two available fixes are each larger than they look. Restoring
`onTabClose` re-adds a primitive the project removed on purpose, for one kind, and the question
of whether *every* tab should close by hand is a tab-model question rather than a consultation
one. Sweeping at the `clearHistoricalTabs` cadence instead — which review floated — would close
the first consultation the moment a second is opened, and comparing two second opinions is a
reasonable thing to want.

### A consultation asked by a delegation opens with its seed line and nothing else

`openConsultationTab` takes its blocks from the owning tab, and both routes into it refuse an
owner that is itself a subagent:

```js
if (!ownerTab || ownerTab.subagent) return [];   // findSubagentBlocks
if (!ownerTab || ownerTab.subagent) return false; // mirrorSubagentBlocks
```

For the mirror that guard is correct and its comment says why — "a subagent tab is a mirror
target, never a source", since mirroring from one would re-mirror its own blocks straight back
into itself. `findSubagentBlocks` inherited it, and a read cannot self-feed, so there the guard
looks unnecessary.

**Reachability, as far as it was measured.** Nothing restricts a delegated subagent from
calling `second_opinion`: `allowed_tools` is never set (`claude_code/options.py` says so
deliberately), and the agent types that ship with the workspace carry `Tools: *`. So a Task
subagent can ask a consultation, its card renders in that subagent's tab, and clicking its row
passes a subagent tab as the owner.

What was **not** measured is whether the lookup would then find anything. A delegation's feed
is read from its on-disk transcript, and whether those reconstructed blocks carry the
`agent_id` stamp that `findSubagentBlocks` matches on is an open question — if they do not,
dropping the guard changes a blank tab into a blank tab. That is why the one-line fix review
endorsed is not applied here: it is cheap, but shipping it blind would mean claiming a fix that
has never been seen to work.

---

<a id="ag-r-32"></a>

## AG-R-32 — Two readers still derive the vendor's product directory, and getting it wrong reports nothing rather than failing

Left by [AG-32](decisions.md#ag-32), which fixed this for the transcript mirror and could not fix it
for the rest.

`roots.PRODUCT_DIR` is the string `"antigravity-cli"`, hard-coded, and `hooks.md` names two other
values for the same slot: `antigravity/` for Antigravity 2.0 and `antigravity-ide/` for the IDE. Every
path this package computes from `roots.vendor_dir` therefore rests on one constant matching the binary
actually installed. AG-32 removed that dependency for the one reader that had somewhere better to
look — the hook payload announces `<brain_dir>/<conversation_id>` on every event, so
`roots.announced_brain_dir` reads the directory back out of a path the running vendor process itself
sent, and `AgyService._brain_dir_for` prefers it.

**Two readers have no payload to learn from, and both fail the same quiet way.**

- **`steps.locate_generated_image`** resolves `brain_dir(root)/<conversation_id>/<name>_<epoch>.jpg`.
  Its `brain_dir` comes from the caller, and on the consultant path
  (`agy/consultant.py`) that caller has no gate and no hook, so there is no announcement to prefer.
  A wrong directory does not raise: the file is simply not found and the image is not collected.
- **`steps.scratch_dir`** resolves `vendor_dir(root)/scratch`, and it is the diverted-write detector
  from [AG-R-3](#ag-r-3). Its own docstring already records the twist: this is a diagnostic that fires
  only when something has gone wrong, so a wrong directory does not misreport — **it reports
  nothing**. A detector that has quietly stopped detecting is the exact failure the directory exists
  to catch.

Neither is derivable from the announcement, and that is the honest position rather than an oversight.
The payload names the *brain* directory; `scratch` is its sibling and nothing sends it. Inferring
`scratch` by taking the announced brain dir's parent would work on today's layout and is a second
hard-coded assumption wearing a measurement's clothes — the geometry
`vendor_dir/brain` beside `vendor_dir/scratch` is no better attested than `PRODUCT_DIR` itself.

**The tripwire is the resolved path for a given root**, which is where [AG-R-18](#ag-r-18) put its own
and for the same reason: a test that asserts on the constant asserts that the constant is the
constant. What would make this real is a machine running one of the other two products, which is not
this machine — `agy` here is `antigravity-cli` 1.2.2. So this is recorded as a tripwire for a change
that has not happened, not as a fault with a symptom anyone has seen.

**What raises the odds rather than lowering them.** AG-32 makes the brain directory come from the
payload, so on a mismatched build the transcript mirror would now work while image collection and the
diverted-write check silently would not — the three used to be wrong together, which at least made one
of them a witness for the others. `AgyGateServer.note_paths` warns, once, when the announced directory
and the derived one disagree, and that warning is the only signal this condition has: it names
`roots.PRODUCT_DIR` explicitly and says every path computed from it is suspect, because by the time it
fires the mirror has already routed around the problem and will not complain again.

---

<a id="ag-r-33"></a>

## AG-R-33 — Neither Antigravity transport can be asked what the user is looking at *now* **(closed 2026-09-15)**

Left by [AG-33](decisions.md#ag-33), which closed the larger half of this: the open file now reaches
the model on all three engines, in identical words, on the turn's own prompt.

What did not come with it is the refresh. On the Claude engine the viewer state feeds **two** readers —
the turn framing and the `ui_state` MCP tool (`claude_code/mcp_server.py`) — so a model that suspects
its context has gone stale mid-turn can ask for the live answer. On both Antigravity transports the
framing is the only report. Measured rather than assumed: the sole MCP server `agy` is given is the
consultation listener, written by `AgySession` from `ConsultListener.config_entry`, which declares
`second_opinion` and nothing else. There is no `ui_state` for that model to call.

**Why this is a residue and not the same bug again.** The framed fact is turn-scoped and true when
sent, so nothing in front of the model is wrong. The gap is narrower than it sounds and shows up in one
shape: a long turn during which the user navigates. The agent finishes work on the file the turn opened
with while the user is looking at something else, and has no way to notice.

**Why the fix is not `PreInvocation`.** That is the tempting shape, since [AG-32](decisions.md#ag-32)
already re-injects a message at every invocation on the `agy` transport, and it is wrong here for the
reason AG-33 records: a re-asserted viewer either repeats a stale claim as though it were live, or
contradicts the turn's own opening framing. A **tool the model chooses to call** is the right shape
because it carries its own freshness — the answer is dated by the asking.

**What it would cost.** More than it looks. Claude's `ui_state` arrives through an MCP server this app
already runs for that engine and mounts in-process; giving the Antigravity transports an equivalent
means an MCP server exposing repository/UI surface to a model whose gate is `AgyGateServer`, and
[AG-24](decisions.md#ag-24) records that every `agy` MCP call is two hook events with the server and
tool named as a pair rather than as `mcp__server__tool`. So the tool would need gate policy of its own,
and *that* is the work — not the snapshot, which is three fields the adapter already holds.

**The tripwire.** `test_claude_code_service.py::TestSetViewerState::
test_it_reaches_the_ui_state_tool_as_well_as_the_prompt` pins the Claude side, and the framing module's
docstring names this asymmetry at the point where a reader would otherwise assume parity. There is no
test on the Antigravity side, because the thing to assert is the absence of a tool, and an assertion
that `agy` is given exactly one MCP server would fail the day a second one is added for any reason.

> **Widened 2026-09-15 by [AG-34](decisions.md#ag-34): the missing tool is the smallest part of it.**
> This entry was written as though `ui_state` were the one absence, because AG-33 had just been looking
> at viewer state and found the reader that was missing. Measured properly the next day: **none of the
> six** `aic-dc` tools reaches either Antigravity transport. `claude_code/mcp_server.py` is imported by
> `claude_code/service.py` alone, so `symbol_map`, `file_symbols`, `find_references`, `doc_outline` and
> `review_state` are absent by the same mechanism and were absent the whole time. Two sentences above
> are therefore too kind to the shipped state — *"the gap is narrower than it sounds"* and *"nothing in
> front of the model is wrong"* — and both are true only of the viewer half this entry happened to be
> looking at.
>
> The paragraph that survives is **What it would cost**, which guessed the shape right: the snapshot was
> never the work, the gate policy was. AG-34 measures exactly that — `agy/tools.py:114` classes
> `call_mcp_tool` as `exec`, so every one of the six would open a permission dialog where the Claude
> engine ungates them by server name at `claude_code/permissions.py:173`. What that paragraph did not
> anticipate is that the *other* transport needs no server at all: [AG-4](decisions.md#ag-4) had already
> decided callables for the SDK path, and its plumbing has been sitting unfed at
> `antigravity/options.py:240` since it was written.
>
> **The reasoning that stays correct is the rejection of `PreInvocation`**, and it now covers more than
> it was written for. A repository whose shape is re-asserted every invocation is a worse version of the
> same mistake than a re-asserted viewer, because it is larger and goes stale on the agent's own writes.
>
> Superseded as a whole by AG-34; closed when that build lands on both transports. The last paragraph's
> reason for having no Antigravity-side tripwire also expires with it — once a second MCP server is
> deliberately given to `agy`, "exactly one server" stops being the invariant it declined to assert and
> becomes a fact worth pinning by name.

### Closed, 2026-09-15

All six tools reach both transports, and the two costs this entry guessed at are the two that were
actually paid.

**The gate policy was the work, as predicted — and it was two narrowings, not one.** On `agy` the call
arrives as `call_mcp_tool` with `{"ServerName", "ToolName"}` beside it, so
`antigravity/permissions.is_index_read` checks the **pair** and `pre_verdict` returns an allow before the
class table or `ALWAYS_ASK` is consulted. First, and that ordering is the whole of it: `call_mcp_tool`
is classed `exec` and sits in `ALWAYS_ASK` — correctly, since it dispatches an open set — so a check
placed after either would never have fired and every `symbol_map` would have opened a dialog. On the SDK
transport there is no server name because there is no server, so the gate is told which bare names *this
session registered* (`own_read_tools`, derived from the callables actually passed). Deliberately not
defaulted to `index_tools.TOOL_NAMES`: on `agy` a bare `symbol_map` would be a tool of the binary's that
happened to share the spelling, and defaulting would be the gate deciding it recognises something on
evidence it does not have.

**What the cost paragraph did not anticipate, and it was the expensive half.** AG-4's callable channel
existed and using it needed a measurement of somebody else's code. A plain callable has its declaration
derived from its *signature*
(`FunctionDeclaration.from_callable_with_api_option`, `connections/local/local_connection.py:243`), and
all six of our handlers share one `**arguments` signature — so the obvious build would have advertised
six tools with one empty schema between them and no per-argument prose. `ToolWithSchema` is the one
branch that takes a schema verbatim (`:222`), where `__name__` is the name, **`__doc__` is the
description** — there is no description argument anywhere on that path — and `normalize_schema` was
measured to return all six of our schemas unchanged. That measurement is what lets the claim be
*identical* words rather than equivalent ones.

**And a trap neither this entry nor AG-34's first draft saw.** `policies=[deny_all(), *allows]` names
`BuiltinTools` members only, and a custom Python tool is not one — so the six would have been declared
to the model and then refused by the wildcard deny, in the Go harness, with nothing in Python to show
it. That failure costs a turn and reads to the user as the tool being broken rather than as a policy.
`build_config` now emits one specific allow per custom tool, which beats a wildcard deny by the SDK's
own documented precedence.

**The structural blocker was on the `agy` side and had nothing to do with tools.** `_offer_consultant`
started the loopback listener only when a Claude CLI was installed, and cleared the config otherwise —
so an `agy`-only install, which is the likeliest shape of a subscription user's machine, would have lost
the repo intelligence along with the second opinion. The two halves are now offered independently: a
consultant factory when `claude` is there, an index server whenever there is a bridge, both on one
socket under one bearer with two mount points.

**The tripwire this entry declined to write now exists**, and it is the fact rather than the count:
`test_agy_service.py::TestTheIndexToolsAgyCanReach` asserts both servers by name in the config document
`agy` actually reads, on one port with one token — over the wire, not in-process, because the defect
being closed was a tool that existed in Python and did not exist to a model. `test_antigravity_options.py`
drives the SDK's own `ToolRunner` for the same reason.

**And a live one, because no offline test can see the far end of a pipe.**
`scripts/probe_agy_index_tools.py` spawns the real binary against the real config and confirmed on
2026-09-15 that `agy` opens a transport on *both* servers and calls two of the six with no dialog. Its
first run failed on correct code by looking for our names in `agy`'s `init` frame: that frame carries 57
builtins and no MCP tool at all — not `second_opinion` either, which has worked since AG-22 — because
every MCP call on this transport arrives as `call_mcp_tool` with the target in its arguments. Which is
the same fact `is_index_read` above is built on, arrived at from the other direction.

The one thing left unfixed is the `ui_state` description's promise of *"files ticked in the picker"*,
which no snapshot has ever carried. Moved verbatim so the extraction was provably byte-exact, and
recorded separately as [AG-R-34](#ag-r-34).

---

<a id="ag-r-34"></a>

## AG-R-34 — `ui_state` promises the model a file list that no snapshot has ever carried

Left by [AG-34](decisions.md#ag-34), which moved the tool descriptions into `index_tools.SPECS` and
declined to edit one of them on the way.

`ui_state`'s description opens *"What the user is looking at right now: **files ticked in the picker**,
the file open in the viewer pane and the selected line range."* The first of those three is not in the
answer. `ClaudeCodeService._ui_state_snapshot` returns `viewer`, `review_state` and `permission_mode`,
and the Antigravity adapter's counterpart returns the same three keys — there is no set of ticked files
in either, and there never was. The tool has said this since phase 4.

**The snapshot is right and the sentence is wrong**, which is the part worth being clear about. The
absence is deliberate and reasoned: `specs5/plan/decisions.md` CC-21 records that pointing at a file is
something the user does *in the prompt*, where the agent already sees it, so there is no browser-side
selection to report. `_ui_state_snapshot`'s own docstring makes that argument at the point where a
reader would otherwise assume a field had been forgotten. So the fix is a reword, not a feature.

**Why it was not fixed in the same change.** AG-34's whole guarantee is that the prose the model reads
did not change when it moved out of the `@tool` calls and into a spec table — `tests/test_index_tools.py`
pins every description as a **literal**, written from the shipped source, so the extraction is provably
byte-exact. Editing a description inside that change would have made one of the six unverifiable against
the state it came from, in the one commit whose claim is sameness. And a tool description is product
text: it is now read by three engines, so rewording it is a change to what every engine tells a model,
which is a decision rather than a tidy-up.

**What it costs while it stands.** A model that wants the picker's selection calls `ui_state`, gets three
keys that do not include it, and has to infer the difference between "nothing is ticked" and "this tool
does not report that". The likeliest reading is the first, which is a false negative rather than a
wrong answer — the model concludes the user has selected nothing. Small, and one-directional: nothing
here puts a wrong fact in front of the model, and the two fields that *are* promised are both delivered.

**The tripwire is `tests/test_index_tools.py`'s literal**, which is what makes the reword deliberate: the
sentence cannot be changed without changing the test that quotes it, in a file whose whole purpose is to
make a description edit visible. The fix is to drop the clause, at which point this entry closes.
