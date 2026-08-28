# What Is Next To Implement

**Status:** the implementation queue. Current as of **2026-08-28**, HEAD `f089416`. B1 is committed
(`fbce225`), and the two commits above it are the tool-card header's left rail.

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

**Phase 8 — index freshness after `Bash` — is built (2026-08-28).** It was the last phase-shaped
*correctness* item, and the one whose absence the agent itself tripped over: a `sed -i`, a `git
checkout` or a `mv` through `Bash` changed files that no index heard about until the next full build,
so `symbol_map`, `file_symbols`, `find_references` and `doc_outline` answered confidently from a stale
picture with no marker on it.

**None of CC-18's four options was taken.** The choice as framed assumed freshness needed a new source
of truth — a watcher, or paths parsed out of a command line. The index already had one:
`BaseCache.get(path, mtime)` has always returned `None` on a stale entry, and nothing had ever asked it
as a question of its own. So the `Bash` hook sets a boolean and does no work, and `Reindexer.flush()` —
which every index-reading MCP tool already awaits — stats the known files and re-indexes what
disagrees. An `ls` costs nothing, because no sweep runs until an index is *read*; an unchanged repo
never reaches `reindex_files`, so its two whole-index passes are not paid for a sweep that found
nothing. There is still **no filesystem watcher in the tree**, and now there is no reason to want one.

**What was accepted rather than solved:** a file a shell command *creates* holds no cached mtime to
disagree with, so the sweep is blind to it until the next full build. Catching it means re-walking the
repo per sweep — the cost the approach exists to avoid. Modification and deletion are covered. That
residue is stated in [`2-indexing/symbol-index.md`](2-indexing/symbol-index.md) § *Freshness After a
Shell Command* and pinned by a test, which is CC-18's own exit criterion: the absence is stated rather
than silent. Reasoning in [`plan/decisions.md#cc-18`](plan/decisions.md); phase 4's entry had called
this its largest known hole ([`plan/delivery.md`](plan/delivery.md#deviations-from-inventorymd-1)).

**Both numbered phases are now done**, phase 7 modulo a runner. Everything left is in §§ B–D, all of it
smaller than either phase. B1, B2, B5, C1, C4, C5, C6, C7 and C8 have since closed, and **B3's first two
rows closed 2026-08-28** — with a finding that is the point of that entry now: one of the two values was
*not* already configurable. `app.json`'s `keywords_enabled` was parsed and read by nothing, so the item's
own framing — "discoverability, not capability" — was true of one card and false of the other, and the
entry had recorded `config.py` as *honouring* a key it only parses. **"Read the value" and "find its
reader" are different checks, and only the second one closes this class of item.**

§ B is down to B3's remaining rows and B4, and B4 is a design decision rather than a build. The cheapest
remaining item is now **§ C2**, which is correctness rather than surface.

**§ C4 closed by finding its own answer already written down** (2026-08-28). It had been deferred as a
display decision with three plausible options; the house rule for naming a file on screen was already in
the Context tab's code — a day older than the commit that deferred the question — and it is what
`toRepoPath` already did. **An item can be blocked by its own framing**
— the question "which of these three" had no answer, and "what does this app already do" had one.

**§ C5's audit findings are all closed as of 2026-08-28**, and the pair it produced left two pieces of
residue that are *not* scheduled, both stated in their own entries. § C7 sends the viewer's **file** and
never its **selection range**. § C8 gives the engine a graceful teardown on POSIX only — Windows keeps
the immediate exit, because `add_signal_handler` is not available there to run a coroutine before
`os._exit`. Neither is a defect being deferred quietly; each says what it costs.

---

## A. The two phases the plan has not shipped

**A1 — Phase 8, index freshness after `Bash`.** ✅ *Built 2026-08-28 — leaves this queue.* Above for
what shipped and what was accepted; [`plan/decisions.md#cc-18`](plan/decisions.md) holds the reasoning
and the fifth option that closed it. The work-log's § *Landed since* carries the record.

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

**B1 — The HUD's last three sections.** ✅ *Built 2026-08-28 — leaves this queue.* Rate limits, Files
modified and collapse persistence all render;
[`5-webapp/viewers-hud.md`](5-webapp/viewers-hud.md) holds the reasoning in three new sections and the
concrete shapes are in its [reference twin](../specs-reference/5-webapp/viewers-hud.md).

**The section with weight behind it turned out to have a backend half.** § R-6 makes `RateLimitEvent`
the subscription-mode equivalent of a cost signal, and the SDK's own docstring says the CLI emits one
when the status *transitions* — not per turn. So a live-only reading would have shown nothing to any
browser that reloaded between transitions, which on a seven-day window is days, and possibly right up
to the rejection the figure exists to warn about. `EngineSession` holds the record and
`get_current_state` carries it, exactly as § C6 did for compaction a day earlier: **a broadcast is not
a record**, and this is the same lesson over a much longer gap.

**Two things the framing had backwards.** "When a rate-limit event is in play" reads as *an event just
fired*; it has to mean *the window has not reset yet*, or the section would be a second copy of the
toast. And expiry had a natural-looking home on the server that would have been the wrong one — a
pushed record must be aged client-side regardless, so a server-side test could only have become a
second definition of "still open" (§ C3). The server serves the record raw.

**One spec sentence was corrected rather than implemented.** § *Sections* said *all* sections collapse;
"This turn" is its own headline, so a disclosure control there hides the figure the HUD exists to show
and spends a caret doing it. Four sections collapse, the head keeps the headline, and the stored key is
deliberately not the displayed label — the rate-limit section's name moves from "5-hour limit" to
"7-day limit" with the account's binding window, and keying on it would re-open a section the user
closed on the day the label changed.

**What this did not build, and it is not residue but scope:** § *Cost Is Cumulative* also specifies a
spend-against-budget bar when `max_budget_usd` is configured. It is a different reading of a different
field — `total_cost_usd` deliberately as the cumulative figure, which is the one place in the app where
that is correct — and it is stated in both spec files rather than left to be noticed. Not queued;
nobody has asked for a budget.

**B2 — The retired-files note.** ✅ *Built 2026-08-28 — leaves this queue.*
[`5-webapp/settings.md`](5-webapp/settings.md) § *Deleted cards* had argued for it at length and
nothing rendered it for three phases. The note now lists the retired files **this install actually
has**, says they will not be deleted, and points at `CLAUDE.md` and `.claude/`; `get_config_info`
grew a `retired_files` key rather than the work growing an RPC. Dismissal is keyed on the file list
rather than a boolean, so a later upgrade that retires something new is owed the note again. Details
in that section and in the work-log's § *Landed since*.

**B3 — Three preference cards and the session-storage figure.** The remainder of
[`impl-history/work-log.md`](impl-history/work-log.md) § *Specified but not yet built* item (c), after
the save-disposition/restart pair and the MCP controls landed:

| Item | State at HEAD (verified 2026-08-27, amended 2026-08-28) |
|---|---|
| Thinking display toggle | ✅ *Built 2026-08-28.* Three-state select over `engine.json`'s `thinking_display`; the field joins the restart list. |
| Doc enrichment toggle | ✅ *Built 2026-08-28* — **and its premise was false**, see below. |
| Deny-read scope reset | Nothing anywhere reads or writes `aic-dc-deny-read-scope`. Depends on B4 below being decided first. |
| Session storage size | **Nothing to call.** The backend measures the session directory only as a turn-time warning (`_disk_warning`), never as a readable RPC. |

**The premise this item was written on was half wrong, and the wrong half was the whole job.** "The value
is already configurable, so what is being built is discoverability, not capability" held for
`thinking_display`, which `options.py:273` genuinely reads. It did not hold for `keywords_enabled`:
`config.py` *parses* it — which is what the entry above checked, and parsing is not honouring —
and **nothing read it**. `EnrichmentConfig` never carried an `enabled` field, so enrichment ran whatever
`app.json` said, and a card over it would have been a switch wired to nothing. Same shape as § B5 and
§ C7, a third time: **a documented field with no reader on one side of it.** The choice was § B5's — write
it or delete it — and writing won, because the spec names the effect and the thing being switched off
costs about a gigabyte of resident model.

**What the cards are actually made of is the sentence under the control**, because both fields stay
editable in the textarea below them. The two take effect at different times and **neither takes effect
now**: `thinking_display` is next-session and joins `_pendingFields` so the restart confirmation names it;
`keywords_enabled` is next-*pass* — the reload is called and is real, but the consumer is a background
build, so switching off does not un-enrich and switching on does not start a pass. The gate is a callable
rather than a captured boolean for exactly this reason: a builder constructed at startup would otherwise
have made it an app-restart field, and this tab has no third disposition to name.

**A switch also had to stop being able to rewrite the file it edits.** `webapp/src/settings-preferences.js`
replaces a value on its own line; a `JSON.stringify` round trip of this app's own `app.json` explodes two
array lines into twenty-four, which is an unrequested rewrite performed by a control that promised to move
one boolean. And the write bases itself on the **textarea** when that file is open, because a stale base
would silently discard the user's unsaved edits — the one fault this control could cause that the textarea
alone never could. Reasoning in [`5-webapp/settings.md`](5-webapp/settings.md) § *Preference Cards* and
[`2-indexing/keyword-enrichment.md`](2-indexing/keyword-enrichment.md) § *Switching Enrichment Off*.

**Not built, and it is B4's not this item's:** the deny-read scope reset. Session storage still has nothing
to call.

**B4 — The denial-scope prompt.** [`5-webapp/file-picker.md`](5-webapp/file-picker.md) specifies a
modal asking whether a deny-read rule is session-scoped or written to `.claude/settings.local.json`,
with an `aic-dc-deny-read-scope` localStorage key. Marked **Not built** there rather than deleted,
because [`specs-reference/3-engine/permissions.md`](../specs-reference/3-engine/permissions.md)
§ *There is no runtime rule API* already explains what a `session` option would have to mean — and
that constraint is the reason this is a design decision and not a form. Every denial currently goes to
`settings.local.json` unconditionally.

**B5 — `EngineHealth.mcp` is a field with no writer.** ✅ *Deleted 2026-08-28 — leaves this queue.*
The choice was write it or delete it, and deleting won: the Context tab already called
`get_mcp_status()` alongside the breakdown and that is the better answer, because a status call can
*fail visibly* and a field that always answers `[]` cannot. An empty list does not say "no servers", it
says "no answer" — the shape that made the Context tab's own MCP claim wrong for a week. The full test
suite passed unchanged when the field went, which is the clearest evidence available that nothing had
been reading it. Three tests now pin the absence, including one asserting that every serialised key
maps to a real field, so a key cannot again be serialised out of nothing.

---

## C. Found while working — correctness and honesty

**C1 — A lost session keeps being polled.** ✅ *Fixed 2026-08-28 — leaves this queue.* `usage-hud.js`
now listens for `engineHealth`, skips the fetch while the engine is gone, and renders a state instead of
the last good breakdown. **The definition of "gone" was the whole of the work:** `connected: false` is
also the state of a freshly loaded page, so gating on it alone would have stopped the HUD ever fetching
— the discriminator is a non-empty `last_error`, which is the rule `health-banner.js` already uses, so
there is one definition and two readers rather than two that can drift.

**Two signals, because neither covers the other's case.** The pushed health record stops every later
turn but arrives *after* the `streamComplete` of the turn that died, so the reply's `reason: 'no-engine'`
closes the gate on that first one; a reply cannot pre-empt the request it rides on, and a push cannot
overtake the event ahead of it. Both halves are pinned by tests that were checked to fail without them,
as was the `last_error` discriminator.

**The server-side half followed, and its interest is what it declines to do.** The gate needs health to
say the engine is gone, and one failure mode never says it: the SDK routes control responses on a
*detached reader task*, so when that reader dies every control request waits out 60s forever while
`connected` stays true. Treating a timeout as evidence of death would kill working sessions — the specs
already measure this call past 60s on healthy engines — and detecting the dead reader honestly means
reading the SDK's private `_read_task`. **So the residue is stated rather than guessed at** and only the
symptom is treated: a control-request timeout is logged as a sentence instead of a traceback, keyed on the
chained `TimeoutError` rather than the SDK's re-wordable message, across all eight control-request
handlers rather than the polled one.

**Found while writing it, and fixed:** [`5-webapp/viewers-hud.md`](5-webapp/viewers-hud.md) § *Data
Flow* said the HUD "renders entirely from the `streamComplete` payload", with an invariant that it
"renders without a follow-up RPC" — describing a design where the percentage rode in on
`postResponseComplete`. It never did; the RPC has been there since phase 3. The section documenting the
gate could not sit next to one denying the call exists, so both are corrected. Reasoning lives in that
file § *When the Engine Is Gone*; [`plan/README.md`](plan/README.md) open item 2 is struck.

**C2 — An RPC that fails behind a viewer open shows the user nothing.** Two independent halves, and
**fixing either one alone would have made the bug that found this reportable**: jrpc-oo prints raw
exception text with no request context, and the diff viewer treats a failed fetch as empty content, so
a real error painted an empty diff and left one bare line on the server's stderr.
[`plan/README.md`](plan/README.md) open item 4.

**C3 — Two mechanisms answer "absolute engine path → repo path".** `_mark_openable_memory_files`
enriches server-side with a `relPath`; `toRepoPath` converts client-side at the shell's
`navigate-file` choke point. Both are correct; neither is wrong to exist; the next payload carrying an
absolute path will pick one of them by accident. They should converge, most likely on the client-side
one, which needs no per-payload enrichment. [`plan/README.md`](plan/README.md) open item 3. *(§ C4 made
the client-side one strictly more capable — it now holds the root and serves labels as well as requests —
so the convergence target is settled even though the convergence is not done. `relPath` still has one
job nothing else does: it is what marks a row **openable**, which is a fact about the repo, not a
rendering.)* **The 2026-08-28 header change raised the price of leaving this open:** a tool card's input
summary wraps now instead of being elided, so the absolute prefix that used to be clipped off the end is
fully rendered, and a nested path spends two or three rows of the card on a string identical for every
file in the repo. The second change that day raised it again — the summary is a fixed column beside a
metadata rail, so it is narrower than the widest it used to be and the same width on every card, which is
another row of prefix on a deep path. Worth doing, still not worth a third mechanism.

**C4 — Tool-card file chips still display the absolute path.** ✅ *Built 2026-08-28 — leaves this queue.*
The label is now the repo-relative name with the engine's path on the tooltip, on the tool-card footer
chips, the turn footer's "files modified" list and the "Files Referenced" chips alike. Reasoning in
[`5-webapp/shell.md`](5-webapp/shell.md) § *The Same Rule Names Files On Screen*;
[`plan/README.md`](plan/README.md) open item 5 is struck and holds the account of why it sat open.

**The three-way question was not open.** Basename / root-relative / middle-elided read as a decision to
make; the house rule was in the Context tab's own code a day before the commit that deferred it, and it
*is* `toRepoPath`'s existing behaviour. So there is no second helper for display — one function answers both
"what should this ask for" and "what should this be called", which is the shape § C3 wants.

**Three things the work found.** The chip had **no width budget at all** — no `max-width`, no ellipsis,
unlike the file-summary chip it sits beside — so a long path stretched the footer row; that is fixed and
pinned the way the slash palette pins its own layout rules, from the stylesheet source, since jsdom does
no layout. `host._repoRoot` is **gone rather than duplicated**: the root moved into the module holding the
rule, because the renderers take a path and no host, and the cheap alternative would have been a third
holder of the same string. And the "Files Referenced" list **deduplicated on the raw path**, so a file an
edit-block header named absolutely and prose named relatively was already two entries — invisible until
both got the same label.

**One path is deliberately left absolute**, and it is § C3's to close rather than this item's: the tool
card *header*'s input summary still reads `file_path=/home/you/repo/…`. It is a server-built `key=value`
join that does not know which keys are paths, so shortening it needs a per-tool table of path keys — a
third mechanism answering "absolute → relative". Stated in
[`5-webapp/chat.md`](5-webapp/chat.md) § *Card Anatomy* rather than left to be noticed.

**C5 — `test_every_rpc_has_a_caller_or_is_listed_as_dormant`.** ✅ *Built 2026-08-28 — leaves this
queue.* `tests/test_rpc_surface.py` partitions all five registered services three ways and asserts the
partition: browser-called (derived by scanning `webapp/src`, never listed), `INTERNAL_ONLY` (a Python
caller exists, and the named file is checked to still contain the call), `DORMANT` (nothing calls it,
with the reason). The reasoning is in [`1-foundation/rpc-inventory.md`](1-foundation/rpc-inventory.md)
§ *Who Calls These*.

**The audit was the task and it paid.** Of 100 exposed methods, **66 have a browser caller, 22 are
internal-only, 12 are dormant** — so a third of the surface is reachable from a browser by accident,
which is what `add_service` publishing every public method costs. The work-log guessed there were more
cases like `reconnect_mcp_server`; there were ten more, and three were findings rather than
confirmations: § C7 and § C8 below, plus `get_review_file_diff` being dead on *both* services it exists
on, which needed no item and is recorded in the inventory instead. *(The dormant count is **ten** from
2026-08-28, and the internal-only count 23. § C7 gave `set_viewer_state` the caller it was waiting for
and the removal of its entry was prompted by the test failing, not by remembering to; § C8 gave
`shutdown` a Python caller, which moved it to `INTERNAL_ONLY` by hand, because that direction is the one
the test does not check — see § C8.)*

**A fourth list runs the audit backwards**, and it independently reproduced § E's CC-12 claim without
being told it: `LLMService.switch_mode` at `app-shell/mode.js:61` and `chat-panel/events.js:854` is the
only call in `webapp/src` naming a namespace no service registers. A second one would be a new defect,
and it would present as a dropped connection rather than as a missing method.

**Two docstrings had already noticed and stopped at prose**, which is the argument for a test rather
than more reading. `Settings.is_reloadable` works the problem out mid-sentence — "Underscore-prefixed so
it's not auto-exposed by jrpc-oo's `add_class` introspection — actually wait, jrpc-oo exposes everything
non-underscored. This IS a public method" — and concludes correctly that it is harmless. `shutdown`'s
reasons about its gate not obstructing "the real caller", which § C8 is about.

**C7 — The agent is never told what the user is looking at.** ✅ *Built 2026-08-28 — leaves this queue.*
`ViewerFraming` had two arrival paths and a writer on neither, so `Turn.viewer` was always `None` and the
`ui_state` tool's `viewer` key always `null`. The per-turn argument was hardcoded
(`chat-panel/input.js`: `null`, with a comment saying "wiring that gesture is phase 6's" — phase 6
shipped). `set_viewer_state` was the other path, and the service's own comment called it "the one that
keeps working when the turn comes from somewhere else"; nothing in `webapp/src` called it. Found by
§ C5's audit. **Same shape as § B5** — a field with no writer — except that the field is an advertised
input to the prompt, so the cost was the agent reading a file the user was not looking at when it could
have been told.

**Wired, as one writer, not two.** `webapp/src/app-shell/viewer-framing.js` pushes
`set_viewer_state` from the shell's `active-file-changed` handler; `chat_streaming`'s `viewer` argument
stays `null` and its comment now says why rather than promising a phase. The service's existing fallback
feeds both readers from the single write, which
`test_the_last_push_frames_a_turn_that_sends_no_viewer` had already been asserting against a writer that
did not exist. Answering in both places would have given one field two sources that can disagree — the
shape § C3 keeps finding — and the per-turn argument would have been the worse of the two, since it only
knows about turns that start in this browser.

`active-file-changed` was chosen over `navigate-file` because it reports what a viewer *has* open rather
than what it was asked to open (a fetch can fail, and routing diverges SVG→diff on a scroll hint), both
viewers emit it, and it already carries `null` for the close. Three cases the event's own shape forces:
repeats for one path are deduped (the SVG viewer re-dispatches on a same-file `openFile` on purpose, so
the shell re-runs its visibility routing); a `null` from the *hidden* viewer is ignored, because
something is still on screen; and the SVG viewer's synthesised `virtual://svg-compare/…` path is
reported as nothing open, because it is on screen but is not a file anything can read. A reconnect
re-pushes, since `_viewer_state` is in memory and a reconnect usually means a restarted server.

**The selection range is not wired, and this is the residue.** `set_viewer_state` accepts
`start_line` / `end_line` and `build_framing` renders "(lines X-Y selected)", but nothing sends them.
No selection plumbing exists in either viewer: it would take a debounced Monaco
`onDidChangeCursorSelection` chain per editor, which is new surface rather than a wire-up, and a range
that lags the cursor points the agent at lines the user is not on — the failure
`chat-panel/input.js`'s own comment refused to risk. **The file is the part worth having and the range
is a separate item**, not a thing to claim as done. Nothing schedules it.

The other half of what this item named stays dormant: `navigate_file` broadcasts navigation to all
clients, and with the collaboration admission UI on pause there is one client. It is § E's, not this
item's — `tests/test_rpc_surface.py` re-attributes it.

**C8 — Nothing ever shuts the engine down, and the docstring says otherwise.** ✅ *Decided 2026-08-28
— wired, not deleted; leaves this queue.* `ClaudeCodeService.shutdown` had no caller for its whole life
while its docstring reasoned about one. **The decision turns on a single effect**: three of its four
steps are meaningless before `os._exit`, but denying pending permissions announces `permissionResolved`
with cause `shutdown` to the *browser*, which outlives the server — so a dialog open at Ctrl-C stopped
hanging forever. That cause is enumerated in
[`5-webapp/permission-dialog.md`](5-webapp/permission-dialog.md) § *Multiple Clients* and nothing could
produce it. The full argument, and the three findings the mutation pass turned up, are in the work-log
§ *Landed since*; the mechanism is [`6-deployment/startup.md`](6-deployment/startup.md) § *Graceful
Shutdown*.

**Two residues, each recorded where it bites.** Windows gets no graceful step at all, because
`add_signal_handler` raises `NotImplementedError` on the proactor loop — startup.md's invariants say so.
And `is_caller_localhost` reads the *current* RPC caller, so a remote participant's call caught
mid-dispatch by the signal can refuse the host's own teardown; logged, not bypassed, per
[`4-features/collaboration.md`](4-features/collaboration.md).

**§ C5's own list did not catch this closure**, which is a finding about the test. `DORMANT` is asserted
against browser callers both ways, but the Python direction only for `INTERNAL_ONLY` — so this entry
moved by hand. The asymmetry, and why a repo-wide `.method(` scan is not the fix, are written above
`DORMANT` in `tests/test_rpc_surface.py`.

**C6 — [`known-issues.md`](known-issues.md).** ✅ *Its one entry fixed 2026-08-28; the inbox is now
empty.* A "compacting conversation" indicator did not survive a browser refresh — same class as the
compaction divider phase 2 shipped client-side only, and the same fix: a broadcast is not a record.
`get_current_state` now carries a `compaction` key and `state-loaded` restores the indicator, with the
elapsed seconds computed server-side so no two clocks have to agree. Details in
[`5-webapp/shell.md`](5-webapp/shell.md) § *Layout*. The inbox is still where new defects land, so read
it before planning a sitting rather than only when it is cited.

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

**B1's own residue is D2's, not this section's.** The HUD's collapse behaviour is asserted from the
DOM — a collapsed section's body is absent and its headline is not — which jsdom answers honestly
because it is presence, not layout. What jsdom cannot answer is whether five sections and their heads
fit 300px on a real screen, which is the same distance from a pixel that § D2 already records.

**D2 — Two rendering behaviours cannot be tested from jsdom.** ~~The `Bash` summary's three-row clamp is
layout,~~ and the permission dialog's Monaco style-clone tests assert that the rules *arrive*, not that
the editor lays out. A screenshot-based regression harness is the only thing that would catch a
re-break, and it **must write files rather than return images inline** — raising the buffer ceiling
made one inline screenshot survivable, and a ceiling is not a budget.
[`plan/README.md`](plan/README.md) open item 6.

**The clamp is gone and this item grew rather than shrank (2026-08-28).** What replaced it is *more*
layout, not less: the header is a two-column grid, its left rail is a fixed `7rem` chosen because a
narrower one stranded a caret and a dot above a wrapped tool name, and a container query lies the whole
thing down under 360px. Three of the four defects in that change were visible only in a browser, and the
guards now in `block-render.test.js` say the rules exist — which is the same distance from a pixel that
the Monaco clone tests are. Widths were read at 520, 400 and 300px by hand, twice, and neither reading
is repeatable by anything in the suite.

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

  **§ C5's audit added a third callerless piece to this pause (2026-08-28):** `navigate_file` — the RPC
  that makes "broadcast when *any* client navigates to a file" true — is never called either. The
  browser navigates through its own `navigate-file` window event and does not tell the server, so file
  navigation is per-client today. Recorded here rather than queued because it is dominated by the same
  pause: broadcasting to all clients is worth nothing while nobody can join, and wiring it means the
  two-real-clients workflow this bullet already says is the precondition. `Collab.admit_client` and
  `Collab.deny_client` are callerless for the same reason and are listed as such in
  `tests/test_rpc_surface.py`, so the absence now fails a test if anybody deletes the explanation.
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
