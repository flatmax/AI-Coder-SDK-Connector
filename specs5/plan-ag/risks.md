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

---

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
  [`delivery.md` § ⏹ on one subagent](delivery.md#-on-one-subagent-the-mechanism-works-and-the-handle-arrives-too-late-2026-09-10-latest).

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

## AG-R-17 — An engine may invent its own spelling of a shared field, and nothing says so

**Severity: moderate. Likelihood: realised — three fields, on both Antigravity transports, from the
day each was written until 2026-09-11.**

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

**Two entries are on that list today**, and the second is an open question rather than a settled
difference:

- `request_id` — ours, not the browser's. It reads the request id from the RPC callback argument.
- `stop_reason` — the Claude pump spells this **`terminal_reason`**, which `computeTurnOutcome`
  reads and turns into a red LED for anything neither empty nor `completed`. So renaming it would
  change what colour a failed `agy` turn draws, and `agy`'s own status words (`ERROR`, `CANCELED`)
  are not that vocabulary. **It is a mapping to be designed, not a spelling to be fixed** — and
  until it is, the `AgyTranslator.stream_complete` docstring's claim that *"the browser reads an
  unrecognised reason as something worth a red badge"* describes a consumer this key does not reach.
