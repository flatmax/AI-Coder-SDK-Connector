# What Is Next To Implement

**Status:** the implementation queue. Current as of **2026-08-28**, HEAD `1986d45`.

This file adds no design. Every item below is already specified somewhere in this suite; what it
records is *that nothing implements it yet*, what "done" looks like, and which file holds the
reasoning. An item leaves this list when it is built or when it is explicitly declined — and the
record of either belongs where the reasoning already lives, not here:
[`plan/delivery.md`](plan/delivery.md) for conversion-phase work,
[`impl-history/work-log.md`](impl-history/work-log.md) § *Landed since* for everything after phase 6.

Three rolling records feed this queue, and none of them is a queue on its own:

| Source | What it holds | Why it is not the queue |
|---|---|---|
| [`plan/README.md`](plan/README.md) § *Open items carried forward* | items 1–11, each with the interlude that found it | dated 2026-08-18; reasoning, not sequence, and some entries have closed since |
| [`impl-history/work-log.md`](impl-history/work-log.md) § *Specified but not yet built* | the Settings-tab drift, item by item | scoped to one tab |
| [`known-issues.md`](known-issues.md) | the raw inbox — defects as they are noticed | unsorted by design |

Claims marked **(verified 2026-08-27)** were checked against the tree at HEAD, not carried forward
from a dated paragraph.

---

## Start here

**Phase 7 — packaging, to make a release possible.** Chosen 2026-08-27, because a release is what is
wanted from this tree next and **no part of the release path works today**: the workflow will not fire for
this branch, the build command it would run describes an engine deleted three phases ago, and it never
mentions the CLI the SDK ships even though its own spec requires collecting it. § A2 is the checklist, in
the order the steps unblock each other.

**Where it stands after 2026-08-27 (late):** phase 7 is built. The workflow is rewritten, R-7 is decided
(collect the bundle — § A2 (c)), `master` is merged, **deployment on GitHub is reported working**, and
§ A2 (d)'s runtime half landed: `aic-dc --check-engine`, a fresh-container step in the Linux leg, and a
wheel that carries the webapp. Verified locally against a real 237 MiB artefact, including in a container
with no `claude`, `node`, `npm` or `python3` — see [`6-deployment/build.md`](6-deployment/build.md) and
work-log § *Landed since*.

**Read from the Actions tab 2026-08-28**, closing the two claims the paragraph above could only report:
`gh` is authenticated on this machine after all, so the run was read rather than relayed. Pull request #1
merged into `master` (`a8be5fb`), the gate fired, **all three legs went green and a release was
published** — `2026.08.27-04.22-a8be5fb2`, marked latest, carrying `aic-dc-linux` (247.8 MB),
`aic-dc-macos` (223.3 MB) and `aic-dc-windows.exe` (236.5 MB). Those sizes are the load-bearing part:
each is engine-scale, so `--collect-all=claude_agent_sdk` resolved a platform-tagged wheel on every
runner and **R-7's option 2 is proven on the matrix, not just inherited from `uv.lock`**.

**What that run does not cover is the commit that verifies it.** It built `0e46991` at 04:21 UTC;
`1986d45` — `--check-engine`, the `ubuntu:24.04` clean-container step, the wheel-carries-webapp assertion
— landed at 06:40 UTC and is the one commit `master` does not have. So the published binary was built
*before* the tripwire existed, and phase 7's stated exit criterion has still never run on a runner.
**Nothing in this file is finished by a green matrix**, and this matrix was green without the check that
would make it mean what § A2 (d) wants it to mean.

That gap closes on the next real pull request into `master` and by no other route: **manual runs are
declined by decision** (2026-08-28), and `workflow_dispatch` has been removed from the workflow so the
policy is structural rather than remembered. See § E.

This is not a reversal of **"packaging last but not never"**
([`plan/README.md`](plan/README.md) § *Ordering constraints that are not obvious*). That constraint is
satisfied — the conversion phases are shipped — and its stated reason was that packaging is "the one
most likely to block a release". It is, and the block is now the thing in the way.

**Then phase 8 — index freshness after `Bash`.** It is the last phase-shaped *correctness* item, and
the one whose absence the agent itself trips over: a `sed -i`, a `git checkout` or a `mv` through
`Bash` changes files that no index hears about until the next full build, so `symbol_map`,
`file_symbols`, `find_references` and `doc_outline` answer confidently from a stale picture with no
marker on it. The `PostToolUse` hook covers `Write`, `Edit`, `MultiEdit` and `NotebookEdit` only —
hooking `Bash` would mean re-indexing after every `ls`, and the tool input is not reliably parseable
into "which files did this touch". **There is no filesystem watcher in the tree** (verified
2026-08-27: no `watchdog`, no `inotify` use in `src/`).

[`plan/decisions.md#cc-18`](plan/decisions.md) leaves the choice open and **"nothing, documented" is a
legitimate exit** — the exit criterion is that a `Bash`-driven change is either reflected in the
indexes, or its absence is stated in [`2-indexing/`](2-indexing/) *and surfaced to the user* rather
than silent. Phase 4's own entry calls this its largest known hole
([`plan/delivery.md`](plan/delivery.md#deviations-from-inventorymd-1)).

**The phase numbers record naming, not order.** Packaging is numbered 7 and index-freshness 8, but the
constraint above was written in phase 0 and phase 8 did not exist until CC-18 was decided during phase
5. Read either way round, the two are independent; nothing in phase 7 touches an index and nothing in
phase 8 touches a build. Everything in §§ B–D below is smaller than either phase and can precede both.

---

## A. The two phases the plan has not shipped

**A1 — Phase 8, index freshness after `Bash`.** Above.

**A2 — Phase 7, packaging and the release path.** *(a)–(d) all landed 2026-08-27, and (a)–(c) are now
confirmed on a runner: a green three-platform build and a published release, read from the Actions tab
2026-08-28. What remains is (d) alone — the verification steps postdate the run that would have exercised
them, and only the next PR into `master` can close that.*

**(a) The build command described a deleted engine.** ✅ *Fixed.* `.github/workflows/release.yml` predated
the conversion, and its PyInstaller step named, as things to bundle, packages phase 3 removed from
`pyproject.toml` — `--collect-all` for `litellm`, `tiktoken`, `tiktoken_ext`, `trafilatura`, and
`--hidden-import` for `boto3`, `botocore`. **Fifteen** of its `aic_dc.*` hidden imports named modules that
no longer exist; **the entire `claude_code` package was absent** — all 19 modules, including the service
the RPC layer registers by class name — as were the `repo` and `doc_convert` submodules.

The reason nobody noticed is worth keeping: **neither mistake fails a build.** A `--collect-all` for an
uninstalled package logs `skipping data collection … as it is not a package` and returns empty lists; a
`--hidden-import` for a deleted module lands in `build/*/warn-*.txt` (verified 2026-08-27 against
PyInstaller 6.22.2). The workflow was not visibly broken — it was quietly shipping.

The fix removes the dead names and replaces the hand-maintained enumeration with
`--collect-submodules=aic_dc` and `--collect-submodules=jrpc_oo`, which resolve 82 and 8 modules against
HEAD (verified locally 2026-08-27, including all 19 `claude_code` modules and the new MATLAB extractor). A
list that must be edited in lockstep with the tree is a list that will not be. The `uv --frozen` step's
comment was stale in the same way — it justified uv over pip by citing litellm's `Requires-Python` cap —
and now gives the reason that survives, lockfile reproducibility.

**The rewritten command was then run locally with CI's exact flags** (2026-08-27, PyInstaller 6.21.0 as
`uv.lock` pins it, Python 3.14.2, an isolated `UV_PROJECT_ENVIRONMENT` so the repo's `.venv` was untouched),
followed by the workflow's verification block verbatim. It passed: `claude_agent_sdk/_bundled/claude` present
in the archive, no missing-module warnings for `aic_dc` or `jrpc_oo`, and `./dist/aic-dc-linux --version`
answering. **The artefact is 237 MiB, not the ~297 the engine occupies on disk** — PyInstaller compresses the
CArchive — so R-7's cost is smaller on the wire than the raw figure suggests and larger after extraction.
This is the Linux third of a matrix build; Windows and macOS remain unproven, and a local build cannot
exercise `actions/setup-uv`, `npm ci`, or the release job at all.

**(b) The trigger, and `master`.** ✅ *Done — gate fixed, `master` merged, and the workflow has now run
green and published. The narrative below is kept because its middle section was wrong for a day and the
way it was wrong is the useful part; the closing note carries what actually happened.* The workflow
fires on a pull request **closed into `master`**, and gated the build job on the head branch starting with
`dev4-`. That gate is gone: the condition is now simply that the PR merged, because the merge target is the
release decision and a branch-name pattern is a naming convention that outlives its generation by exactly
one. The branch situation is better than it looks — verified 2026-08-27:

- **`origin/master` exists** (tip `3585cba`, 2026-04-20, the dev3 era). There is no *local* `master`,
  which is what makes it read as missing. Nothing needs to be created from scratch.
- It is 10 commits ahead of and 752 behind `dev5-claude-code`, but **`git diff dev5-claude-code...origin/master`
  is empty** — master's tree is already an ancestor of the dev line, and its ten extra commits are merges
  carrying no content dev5 lacks. **A pull request from `dev5-claude-code` into `master` merges clean and
  loses nothing.** No history reconciliation is owed and no force-push is involved.
- ~~**`workflow_dispatch` is how the build gets proven before anything touches `master`.**~~ **Withdrawn
  2026-08-28.** It was the plan for a day, and the `publish_release` input existed to make it safe (the
  release job had published unconditionally with `make_latest: true`, so a manual run from an unmerged
  branch would have gone in front of users). Both are now gone: manual runs are declined, the trigger is
  removed, and the release job needs no publish gate because the only thing that reaches it is a merge.
  § E holds the decision.
- ~~**The default branch is `dev4-membrane`, not `master`.**~~ **Closed 2026-08-28** — the repository's
  default branch *is* `master` (`gh repo view`). The suite's idea of "main" and the repository's now agree,
  and with dispatch gone the reason this mattered — which definition supplies the inputs for a non-default
  ref — has no way to arise.
- `origin/master` also carries an **older, unrelated** `release.yml` — a push-triggered "Build and Release"
  from the dev3 era. The merge replaces it, which is correct, but expect it in the diff.

**What then happened (2026-08-27, later the same day): `master` was merged by direct push, and so nothing
ran.** `origin/master` is now `bd4f62d` ("Merge branch 'dev5-claude-code'"), and
`git diff --stat dev5-claude-code origin/master` is empty — the trees are identical, so the merge took this
branch's `release.yml` whole and there is nothing left to compare. **But a local merge pushed to `master` is
not a closed pull request**, and this workflow deliberately ignores direct pushes, so no build ran and no
release exists. That is the gate working as specified, not a fault; it is worth recording because the
symptom — a merged `master` with an empty Actions tab — looks like a broken workflow and is not one.

Two loose ends followed from it, and **both closed on their own before anything was done about them**
(read from GitHub 2026-08-28). A pull request — #1 — was opened from `dev5-claude-code` into `master` after
all and merged as `a8be5fb`, which is the trigger the workflow wants; the run built all three platforms and
published `2026.08.27-04.22-a8be5fb2`. And `master` is now the default branch. So the "first release needs a
manual dispatch" problem never had to be solved, and the default-branch question dissolved with it.

**The lesson is the one this file keeps relearning, and it cost a paragraph of planning:** the item above
reasoned from a dated local observation (`git log`, a direct push, an empty Actions tab inferred rather
than seen) to a confident claim about a remote that had moved. `gh` was authenticated on this machine the
whole time. **Check the remote before writing a plan that turns on its state.**

**(c) R-7's distribution question.** ✅ *Decided: collect the bundle* — recorded in
[`plan/risks.md`](plan/risks.md#r-7--bundled-cli-size-and-platform-specific-wheels) § R-7 with the reasoning,
implemented as `--collect-all=claude_agent_sdk` plus a build step that fails if `_bundled` is missing from
the archive. The spec had already asked for exactly this and `release.yml` never mentioned the SDK, so the
workflow had been violating its own spec's invariants while producing an engine-less artefact *by accident
rather than by decision*.

Four facts settled it, all verified 2026-08-27 against the installed wheel, and **three of them contradicted
what the specs said that morning** — so [`6-deployment/build.md`](6-deployment/build.md#the-engine-is-not-bundleable),
its [reference twin](../specs-reference/6-deployment/build.md), § R-8 and `pyproject.toml`'s dependency
comment were all corrected:

- **The bundled CLI is a native executable, not a Node script.** `claude-agent-sdk` 0.2.137 (CLI pin
  2.1.229) ships `_bundled/claude` as an ELF x86-64 binary linked against glibc, 296.76 MiB inside a 298 MB
  package. Collecting it makes the artefact genuinely self-sufficient; no Node runtime is involved.
- **The SDK prefers that copy over a `claude` on `PATH`.** `_find_cli` checks bundled first. Both the spec's
  resolution list and R-8 had the order backwards, which inverts the argument: option 1 would not have been
  "the small default" but "ship a binary with its first-choice engine deliberately removed".
- **The wheel is platform-tagged and the lock already covers the matrix.** `uv.lock` pins all five SDK wheels
  — macOS arm64/x86_64, Linux x86_64/aarch64, Windows amd64 — so **R-7's option 2 was inherited rather than
  built**, and `--frozen` has something to install on every runner.
- **The executable bit survives.** `--collect-all` files are data, and data files do not carry permissions —
  but PyInstaller's onefile bootloader extracts everything `0o700`, so the engine is spawnable. Measured, not
  assumed; the detail lives in the [reference twin](../specs-reference/6-deployment/build.md) because the
  failure mode would have been an engine that exists and cannot run.

Users who want their own CLI keep `engine.json`'s `cli_path`, which bypasses discovery entirely
(`claude_code/health.py:259`).

**(d) The exit criterion is a fresh machine, not a green build.** ✅ *Built 2026-08-27; verified locally,
not yet on a runner.* Phase 7's own wording: "a fresh machine can install and run without a manual
`npm i -g @anthropic-ai/claude-code`". The build-time half was the archive assertion and `--version`; the
runtime half is now `aic-dc --check-engine`, which resolves the binary the SDK would spawn, runs it, and
exits 1 when nothing resolves or 2 when something resolved and would not run. The Linux leg runs it inside
`ubuntu:24.04` with `claude`, `node`, `npm` and `python3` asserted absent first, because a check that could
pass by finding a system engine is not a check.

**Writing it found the thing that would have made it useless.** `src/aic_dc/__main__.py` — the script
PyInstaller builds the release binary from — called `main()` and threw away its return value, so the
process exited 0 no matter what the CLI computed. A CI step whose only output is an exit status would have
passed unconditionally. That is the second time in this phase a check has turned out to be quietly
inert (§ A2 (a) was the first), and both were found by running the artefact rather than reading the
build. Reasoning is what missed them.

Also landed: `pyproject.toml`'s note predicting that Layer 6 would "wire this up properly" is gone, and
the wheel now carries `webapp/dist` at `aic_dc/webapp_dist` — the third entry in `_find_webapp_dist`'s
priority list, which until now had no producer. The include is conditional, because the declarative form
fails a dev checkout that has not run Vite; CI asserts on the built wheel instead of trusting step order.

**What is still owed here** is a run on a runner — and as of 2026-08-28 that is the *only* thing owed in
phase 7, because (a)–(c) have one. The three verification steps were committed after the green run, so
they have executed locally and nowhere else. They will run on the next pull request merged into `master`
and, by decision, on no earlier occasion; a manual dispatch is not available and is not wanted (§ E).
Two platforms stay weaker even then: Windows and macOS get the resolve-and-run check but no
clean-environment guarantee, because a Linux container cannot speak for them.

Spec homes: [`6-deployment/packaging.md`](6-deployment/packaging.md),
[`6-deployment/build.md`](6-deployment/build.md).

---

## B. Specified, with nothing rendering it

Each of these is a spec section with no implementation behind it. They are not oversights in the
specs; they are the specs waiting.

**B1 — The HUD's last three sections.** [`5-webapp/viewers-hud.md`](5-webapp/viewers-hud.md)
§ *Sections* specifies **Rate limits** (limit type, utilisation, reset time when a rate-limit event is
in play) and **Files modified** (the turn's list, each clickable to the diff viewer), and its
§ *Invariants* requires **collapse state persisted to `localStorage`**. None of the three exists
(verified 2026-08-27: `usage-hud.js` contains no `rateLimit`, no `filesModified`, no collapse
persistence). Rate limits is the one with weight behind it —
[`plan/risks.md`](plan/risks.md#r-6--cost-becomes-invisible-instead-of-cheap) § R-6 makes
`RateLimitEvent` the subscription-mode equivalent of a cost signal, and under subscription billing it is
the only figure a user can act on.

**B2 — The retired-files note.** [`5-webapp/settings.md`](5-webapp/settings.md) § *Deleted cards*
argues for it at length — a user who customised `system_extra.md` over months and finds the card gone
"deserves to know why" — and then no note is rendered (verified 2026-08-27: nothing in
`settings-tab.js`). Phase 3 left the retired config files on disk and inert precisely so nothing
irreversible happened to them, and never told the user. **The cheapest item on this page, and the only
one whose absence its own spec already calls a mistake.**

**B3 — Three preference cards and the session-storage figure.** The remainder of
[`impl-history/work-log.md`](impl-history/work-log.md) § *Specified but not yet built* item (c), after
the save-disposition/restart pair and the MCP controls landed:

| Item | State at HEAD (verified 2026-08-27) |
|---|---|
| Thinking display toggle | The engine field exists (`engine.json`'s `thinking_display`, read by `options.py`) and is reachable only by editing the file in the config card. No control. |
| Doc enrichment toggle | Same shape: `app.json`'s `keywords_enabled` exists and is honoured by `config.py`. No control. |
| Deny-read scope reset | Nothing anywhere reads or writes `aic-dc-deny-read-scope`. Depends on B4 below being decided first. |
| Session storage size | **Nothing to call.** The backend measures the session directory only as a turn-time warning (`_disk_warning`), never as a readable RPC. |

The first two are the smaller job than they look — the value is already configurable, so what is being
built is discoverability, not capability, and a card that merely edits the same field is worth less
than the reload semantics it has to explain. Read § *Save Behavior* before adding either.

**B4 — The denial-scope prompt.** [`5-webapp/file-picker.md`](5-webapp/file-picker.md) specifies a
modal asking whether a deny-read rule is session-scoped or written to `.claude/settings.local.json`,
with an `aic-dc-deny-read-scope` localStorage key. Marked **Not built** there rather than deleted,
because [`specs-reference/3-engine/permissions.md`](../specs-reference/3-engine/permissions.md)
§ *There is no runtime rule API* already explains what a `session` option would have to mean — and
that constraint is the reason this is a design decision and not a form. Every denial currently goes to
`settings.local.json` unconditionally.

**B5 — `EngineHealth.mcp` is a field with no writer.** Declared (`health.py:496`), serialised by
`to_dict()` (`:605`), assigned by nothing in `src/`, so every consumer reads `[]` (verified
2026-08-27). The Context tab works around it by calling `get_mcp_status()` alongside the breakdown, and
that is the better answer — see [`plan/README.md`](plan/README.md) § *What phase 6 will find already
there*, which corrects an earlier belief that this field carried what a per-server view needs. So the
work is one of two lines, and the choice matters more than the size: **write it, or delete it.** A
declared-and-empty field is the shape that made the Context tab's own MCP claim wrong for a week.

---

## C. Found while working — correctness and honesty

**C1 — A lost session keeps being polled.** After a message pump dies, the usage HUD goes on calling
`get_context_usage`, each attempt a control request that ends in a 60-second `Control request timeout`
traceback — four in one log. `get_context_usage` catches `EngineNotReadyError` and `SessionLostError`,
but **nothing gates the poll on engine health, and the HUD has no "the engine is gone" state to sit
in** (verified 2026-08-27: `usage-hud.js:_fetchContext` gates on `_fetchInFlight` and `rpcConnected`
only). The tracebacks are noise about a thing the health banner has already reported.
[`plan/README.md`](plan/README.md) open item 2.

**C2 — An RPC that fails behind a viewer open shows the user nothing.** Two independent halves, and
**fixing either one alone would have made the bug that found this reportable**: jrpc-oo prints raw
exception text with no request context, and the diff viewer treats a failed fetch as empty content, so
a real error painted an empty diff and left one bare line on the server's stderr.
[`plan/README.md`](plan/README.md) open item 4.

**C3 — Two mechanisms answer "absolute engine path → repo path".** `_mark_openable_memory_files`
enriches server-side with a `relPath`; `toRepoPath` converts client-side at the shell's
`navigate-file` choke point. Both are correct; neither is wrong to exist; the next payload carrying an
absolute path will pick one of them by accident. They should converge, most likely on the client-side
one, which needs no per-payload enrichment. [`plan/README.md`](plan/README.md) open item 3.

**C4 — Tool-card file chips still display the absolute path.** Only the navigation was converted. The
display decision has its own question — basename, root-relative, or middle-elided — and this is the
one place a multi-root future would want the root visible.
[`plan/README.md`](plan/README.md) open item 5.

**C5 — `test_every_rpc_has_a_caller_or_is_listed_as_dormant`.** The mechanism
[`impl-history/work-log.md`](impl-history/work-log.md) § *How to keep this from recurring* argues for,
in the same shape as the `test_every_rpc_is_classified` that has twice refused a new RPC until it was
filed — `get_model`, then `restart_session`. Its two
motivating cases were `reconnect_mcp_server` and `toggle_mcp_server`, which sat callerless and
unnoticed; the work-log's own claim that it "would have caught these years earlier" implies there are
others. Deliberately not built as a rider on that work, because writing it means auditing every RPC
for callers and arbitrating each callerless one. **That audit is the task, and the test is its
by-product.**

**C6 — [`known-issues.md`](known-issues.md).** One open entry: a "compacting conversation" toast does
not survive a browser refresh. Same class as the compaction divider that phase 2 shipped client-side
only, and cheap; the inbox is where new defects land, so read it before planning a sitting rather than
only when it is cited.

---

## D. Verification debt

Work that is built and green but has never been watched doing its job. Four phases running, green
numbers have met a live CLI and lost — the argument for this section is
[`plan/README.md`](plan/README.md) § *Where we are*, and the recipe is
[`0-overview/implementation-guide.md`](0-overview/implementation-guide.md) § *Verifying UI Work Against
a Running Engine*.

**D1 — ⏹ Stop and the amber LED on a subagent tab.** `stop_task` is its own control subtype
(`SDKControlStopTaskRequest`) answered with a `task_notification` of status `stopped` in the message
stream. The cheap substitute was tried and **failed**: an agent's own `TaskStop` against a live
background subagent killed it with the CLI emitting no terminal task message at all, leaving engine
and browser both reading `status: null, terminal: false` and the LED cyan. So the two paths are not
interchangeable and **the webapp's own ⏹ is the only thing that can verify itself.**
[`plan/README.md`](plan/README.md) open item 9, last clause.

**D2 — Two rendering behaviours cannot be tested from jsdom.** The `Bash` summary's three-row clamp is
layout, and the permission dialog's Monaco style-clone tests assert that the rules *arrive*, not that
the editor lays out. A screenshot-based regression harness is the only thing that would catch a
re-break, and it **must write files rather than return images inline** — raising the buffer ceiling
made one inline screenshot survivable, and a ceiling is not a budget.
[`plan/README.md`](plan/README.md) open item 6.

**D3 — The question-preview `--without` A/B is not automated.** `scripts/question_preview_smoke.py`
supports it and the specs record its result, but nothing re-runs it when the CLI ships a new build —
and what it measures is exactly the kind of detail a version bump moves.
[`plan/README.md`](plan/README.md) open item 8.

**D4 — The cost chip's two exceptional renderings are unobserved.** "Nothing extra" and "cost unknown"
hold in 60 tests and no ordinary turn causes either. Phase 6's entry says so in its exit criterion;
provoking them needs a contrived turn, and the rule that came out of that phase applies — **two turns
minimum when checking anything per-turn**, since a session's first turn has `turn_cost_usd` equal to
`total_cost_usd` and cannot tell a difference from a running total.

---

## E. Deferred by decision — not queued

Listed so their absence is not rediscovered as a bug. Do not implement these without reopening the
decision that parked them.

- **Manual release runs — declined, and the trigger removed (2026-08-28).** `workflow_dispatch` and its
  `publish_release` input are gone from `.github/workflows/release.yml`; a pull request merged into
  `master` is the only thing that builds. The reason is that a manual run spends three runners on three
  ~230 MiB artefacts to answer a question the next merge answers anyway, and the release decision is the
  merge rather than a button. It was removed rather than left unpressed because a policy that depends on
  nobody pressing a button is not enforced by anything. **The accepted cost is stated in § A2 (d):** a new
  build or verification step stays unproven on a runner until a real PR carries it. When packaging work
  lands, run the build command locally with CI's exact flags, run the verification block by hand, and
  record the runner half as outstanding — that is the sanctioned substitute, not a dispatch.
- **The collaboration admission UI — on pause (2026-08-27).** The backend is complete:
  [`4-features/collaboration.md`](4-features/collaboration.md) §§ *Pending State*, *Admission Toast*,
  *Connected Users Indicator*, *Collab Popover* specify the surface, `collab.py` holds the pending queue,
  `admit_client`, and the `_broadcast_admission_request` / `_broadcast_admission_result` pushes, and the
  browser receives both pushes and re-dispatches them as window events **that nothing listens to**
  (`app-shell/index.js:1088` and `:1093`; verified 2026-08-27: no listener for `admission-request` or
  `admission-result` in `webapp/src`). Record the consequence here so it is not rediscovered as a bug,
  because the participant-restriction half *is* built and reads as a working feature: with the flag on —
  collaboration is off by default — the first connection auto-admits and **every subsequent one is
  auto-denied by the pending queue's 120-second timeout**, because the request waits on a click no
  browser offers. Nobody can join a session today, and the session owner sees no sign that anyone tried.
  The pause is the reasoning in [`impl-history/work-log.md`](impl-history/work-log.md) § *Next tasks*
  item 4 — building it on spec without a real multi-client testing workflow accumulates staleness —
  reaffirmed now. Reopening it means following
  [`0-overview/implementation-guide.md`](0-overview/implementation-guide.md) § *Verifying UI Work Against
  a Running Engine* with two real clients, and noting that the specs now describe two clients watching
  one *agent* session, which is a different claim from two watching a prompt being assembled.
- **CC-12 — the preset selector.** The code/doc mode toggle stays mounted and inert, annotated where it
  sits, because removing a receiver while leaving its consumer mounted moves the break instead of
  fixing it. **Open item 11 is down to this pair:** the five other dead `LLMService.*` names it lists
  are gone with the agent-spawn protocol (`a0cb83b`) and CC-21, and the only calls left in
  `webapp/src` are `LLMService.switch_mode` at `app-shell/mode.js:61` and `chat-panel/events.js:854`,
  both guarded and neither reachable (verified 2026-08-27). That item can be closed as an item and read
  as this bullet.
- **CC-20 — undo of file changes.** Not built and **not buildable today**: the engine keeps no
  checkpoints in a session that mirrors its transcript, every session with a repo mirrors, and
  `rewind_files` refuses the call and names git. [`5-webapp/chat.md`](5-webapp/chat.md) records what it
  would take if the SDK ever allows both.
- **`resume_session_at` / `resume_drops_turn`.** The SDK-side half of the undo story CC-20 gave up, and
  the last of the three pending options the probe argued were worth doing. A feature, not a constructor
  argument. [`plan/sdk-surface.md`](plan/sdk-surface.md) § *The probe*.
- **`sandbox`.** On the pending list **as a trap**: it reads like a free security win and it changes
  what the agent may do to the machine, which is the permission dialog's question, not an option's.
- **The remaining pending SDK options.** A findings list, not a defect list — the probe's gate goes red
  only when the SDK grows a name nobody has triaged, so these sit green by design. Two things it cannot
  do: it reads shape, never semantics, and nothing runs it on a schedule, so a `pip install --upgrade`
  with no commits after it leaves a stale report that does not say so.
- **Option B — a session-lifetime pump, or per-translator routing.** The alternative to the drain the
  background-subagent fix built on. Left open deliberately; the agreement was to watch for the chosen
  approach's residual mis-attribution first and take B only if it shows up. Nothing has shown up.

---

## Keeping this file true

- **One line per item, and the reasoning stays where it is.** A restated argument is a second copy to
  keep true; this file names the section that holds it. The suite's single-source-of-truth convention
  applies to the queue as much as to the specs.
- **Date a claim about the tree, or check it.** Every unverified assertion here started as a true
  sentence in a dated paragraph. If an item is picked up and its premise no longer holds, the finding
  is the correction — record it where the reasoning lives and amend the item.
- **An item leaves by being built or by being declined**, and a decline earns a bullet in § E with the
  decision behind it. Silence is how phase 3's replaced panels went three phases without a test.
