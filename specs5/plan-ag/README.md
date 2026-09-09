# Second Engine — Google Antigravity alongside Claude Code

**Status (2026-09-10):** **the gate stopped asking who claimed a conversation and started asking the
kernel** ([AG-18](decisions.md#ag-18)). Yesterday's fix closed the main case of
[AG-R-14](risks.md#ag-r-14) by claiming a subagent's conversation as its announcement arrived, and left
three residues — two of which were the same premise wearing two faces: *identity is something somebody
writes down in advance*. A child's first call can beat the claim onto disk, and a **grandchild is never
announced at all**, so it has no claim to be late with. `agy` now runs inside a systemd user scope and
the hook reads `/proc/self/cgroup`; cgroup v2 binds every descendant irrevocably, so `env -i`, a
subshell and a `setsid` double-fork all stay inside, and the scope entry is published *before the
process exists* — there is nothing left to be late for. **The criterion was falsification, not a green
log:** `probe_agy_subagent_gate.py` re-run with `AgyGateServer.claim` stubbed to a no-op on both the
parent and the subagent still put all eight tool calls to the dialog and the deny still left the target
byte-identical, so conversation claims were inert and cgroup identity carried the load alone. Matching
is against the units this host registered, never the name's shape, so [AG-R-12](risks.md#ag-r-12)'s
isolation holds on a stronger footing than before. **Two things are open and both are stated rather
than hidden:** this is Linux-and-systemd, so on the macOS and Windows artefacts phase 7 publishes
everything degrades to conversation-id routing with both residues intact — and the third residue, a
stale claim nothing reaps, now orphans a *unit name* as well as a conversation id. **The probe's own
setup found one more, and it is fixed:** `install.status` compared command strings, so one venv's
`python3` and `python` read as two installs and a live gate reported itself `stale` — which makes
`AgySession` refuse to start. Only the interpreter's spelling is forgiven now, and not by resolving
both paths, which would have called two different checkouts the same install. 4,701 tests green. See
[`delivery.md` § Identity by cgroup](delivery.md#identity-by-cgroup-not-by-claim-2026-09-10).

**Status (2026-09-09):** **a subagent's tool calls were never reaching the permission gate, and now
they are** ([AG-R-14](risks.md#ag-r-14)). The hook routes by conversation id and passes through
whatever nobody has claimed — the property that keeps a stranger's `agy` session out of our dialog —
and a subagent runs in a conversation of its own, so a delegation was a route around the dialog: the
user approved the *spawn*, and every call made under it ran unreviewed.
`scripts/probe_agy_subagent_gate.py` denied everything but the delegation and watched the subagent's
edit land anyway; after the fix the same run has the gate deciding the subagent's `view_file`,
`replace_file_content` and `send_message`, the deny holding, and the target file byte-identical. This
contradicts [AG-5](decisions.md#ag-5)'s stated reason for gating the spawners — the subagent inherits
the tool set, and did not inherit the gate. **Three residues are open and none is mitigated**: the
spawn-to-announce race, nested delegation announcing a grandchild to nobody, and a stale claim now
orphaning one registry entry per subagent. Found while building `subagent_rows`, which is the other
half of the day: `agy` announces a delegation with a `conversation_id` of its own, so the live strip
is fed on this transport and `subagent_tabs` split into two keys. See
[`delivery.md` § The subagent that was never gated](delivery.md#the-subagent-that-was-never-gated-2026-09-09).

**Status (2026-09-08):** **phase 10 is met, and with it every phase in this directory is closed.**
`scripts/probe_agy_consultant.py` ran two real turns on the account holder's subscription: a second
opinion that was allowed **nothing at all** — not one tool, on an agent running with 57 of them
available — and **an image, which is the first this feature has ever produced.** `generate_image` was
specified in phase 1 and has been unverifiable since, because a free-tier Gemini key reports
`limit: 0` for every image model; the subscription has an allowance, and AG-1's worked example finally
landed as a 1024×1024 JPEG.

**The first run failed, and that is what the probe was for.** The image was generated and then written
to `~/.gemini/antigravity-cli/brain/<conversation_id>/`, outside the repository, because
**`generate_image` takes no output path on either transport** — its schema declares `ImageName`,
*"Short descriptive name for the saved file"*, and the harness chooses the location. AG-16 had left
open *"which name does `agy` give that path"*; the answer is that the question was wrong.
`sdk-surface.md` had it right in its columns all along — `output_path` is listed among that tool's
**outputs** — and our code read it as an argument, which works on the SDK transport only because that
stream merges results back into `args` at `DONE` (phase 3, finding 1). So the consultant now
**collects** the image out of the conversation's own directory and copies it into the repository, the
produced extension wins over the requested one, and the prompt no longer asks the model to place the
file — an instruction that had made it reach for `run_command`, which the policy denied. The closing
run is `PASS` with `denied along the way: []`, which is that change measuring itself. 4,608 tests
green. See [`delivery.md` § Phase 10](delivery.md#phase-10--the-consultant-on-the-paid-transport-and-the-argument-that-does-not-exist-2026-09-08)
and [`sdk-surface.md` § `generate_image` takes a name, not a path](sdk-surface.md#generate_image-takes-a-name-not-a-path--measured-2026-09-08).

**Status (2026-09-07):** **phase 11's criterion is met on a fresh server**, so the only open item left
in this directory is phase 10's — two real turns of the consultant on the paid subscription.
`scripts/probe_engine_policy.py` starts **two** servers on this machine, and the first one is why the
second means anything: this machine has no `agy` binary and no Gemini key, so `mountable ==
["claude"]` was already true before any policy existed and a single-server check would have gone green
proving nothing. With the mount conditions supplied (a key in the child's environment, an executable
named `agy` on its `PATH`) the control run mounts both Antigravity engines and puts
`aic-dc-antigravity` in the Claude session's own MCP server list; the Claude-only run, same machine and
same environment, reports `mountable == ["claude"]`, refuses both `switch_engine` calls with
`engine_disabled` naming `app.json` rather than a missing binary, and the session's server list — read
from the CLI, not from our log — holds `aic-dc` and not `aic-dc-antigravity`. 18 checks green.
**Two findings came with it, both in the config layer and both about AG-17's premise, and both are now
fixed:** `app.json` was a *managed* file, so the first start after any version change silently reverted
`engines.enabled` and a Claude-only install became a two-provider install again — and because that file
is also the one our own Settings tab writes, so did every other value a user had set through it. It is
now **merged key by key** against a pristine copy of the bundle: a value the user set survives, a
default they never touched still upgrades, and a key a release adds still arrives
([`../1-foundation/configuration.md`](../1-foundation/configuration.md#appjson-is-merged-key-by-key-commitmd-is-overwritten)).
The second finding was the read-only enforcement path AG-17 recommends: it did hold the policy, but the
`OSError` aborted the whole upgrade pass, so it repeated on every start, dropped a backup each time and
never upgraded `commit.md`. Each file is now upgraded inside its own error handling and the marker is
written either way. Re-measured after both: the policy survives a version change on a fresh server,
the read-only case leaves no backup at all, 15 new tests including one named for each finding, the
suite at 4,581 green, and the 18-check probe still passing end to end. See
[`delivery.md` § Phase 11](delivery.md#phase-11--the-engine-policy-and-what-a-fresh-server-said-about-it-2026-09-07)
and its § The two findings, fixed.

**Status (2026-09-06, later still):** **the second engine can be switched off**
([AG-17](decisions.md#ag-17), [AG-R-13](risks.md#ag-r-13)). A user asked whether a workplace that is
only permitted to use Claude could disable it, and nothing in this directory could: the `agy` adapter
mounted on `shutil.which("agy")` with no configuration consulted, `engines.master` is a default rather
than a restriction, and the permission gate — the nearest thing to an off switch — is a safety
interlock that one click in Settings puts back. **The question landed one message after AG-16 widened
the exposure**: the consultant used to need a Gemini key, and now mounts on the `agy` binary, so a
Claude-only install with the CLI present acquires `second_opinion` and `generate_image` inside every
Claude turn, with source code as their intended argument. Every call still reaches the dialog (AG-5),
and *that is the point* — a control which resolves to "the human said yes" is not one an organisation
can rely on. `engines.enabled` in `app.json` is the answer, with the consultant following the engine
rather than carrying a second switch, plus a Settings toggle the user asked for and whose limits are
recorded rather than re-argued. **Built the same day** — 20 tests including the tripwire that says every
Antigravity mount point consults the policy, and a webapp card with the grid's third disposition
(app-restart, which this tab had said it did not have). 4,567 Python and 4,454 webapp tests green. **What
is not done is the live check**: a fresh server on a Claude-only `app.json`, which is the one thing that
exercises `main.py`'s mount block rather than the pieces under it.

**Status (2026-09-06, later):** **the consultant now runs over `agy` as well as the SDK**
([AG-16](decisions.md#ag-16)), which removes the one blocker this directory had been carrying as "not
a code problem" since phase 1. `generate_image` had never returned an image and no successful
consultation had been watched streaming, both because a free-tier Gemini key reports `limit: 0` for
every image model and caps agent requests at twenty a day. `agy` reaches the owner's subscription and
advertises `generate_image` among its 57 tools, so the second implementation of the consultant is the
first one that can run. Containment moves from *enabling* tools to a static allowlist on the gate,
because this transport's tool set is the binary's — and the consultation refuses to run at all
without the hook installed, since an unclaimed one would be an unreviewed agent in the repository.
Built and green (4,547 tests); **the exit criterion is `scripts/probe_agy_consultant.py`**, two real
turns on the subscription, and until that runs this is a build rather than a result. See
[`delivery.md` § AG-16](delivery.md#ag-16--the-consultant-learns-the-transport-that-can-pay-for-it-2026-09-06).

**Status (2026-09-06):** every phase is closed and **the 2026-09-05 work has now been watched
running.** A verification sitting drove a browser against a throwaway repository and confirmed four
claims that until then were only green: phase 9's own exit criterion (*always allow* on a live
`run_command`, then the same command allowed by the standing rule in a later turn, with
`broker.can_use_tool` never reached), both halves of the `acceptEdits` boundary, and the posture
selector offering only what the engine accepts. It also found two defects, both invisible to 4,485
passing tests and both visible within minutes — the always-allow tooltip wrong for the *second* time
in two days, on a fix that was correct and unreachable from its call site, and a posture change that
applied on the engine while the selector sat disabled reading the old mode, because this engine
broadcast an event name the browser has no handler for. One small build went in with it: the second
engine now feeds the shared symbol index, which it never had. See
[`delivery.md` § The verification sitting](delivery.md#the-verification-sitting-and-the-two-defects-it-cost-to-buy-2026-09-06).

**Status (2026-09-05):** Phases 0–6, 8 and 9 done or substantially done. **Phase 5 landed on
2026-09-05** — Antigravity conversations are mirrored per transport, the history browser renders
them, and a server restart resumes the previous conversation with the model's context intact,
proven across two processes on the paid subscription. **Phase 7 landed the same day**, making
`google-antigravity` an optional extra — so **every phase in this directory is now closed**. What
remains open is not a phase: phase 4's end-to-end criterion on the *SDK* transport, unmet for the
reason it always was — the free tier's daily ceiling, not the code — and three surfaces the
descriptor still marks `unbuilt`. The consultant
ships and streams into its own tab, the engine router is wired into startup, and Antigravity can be
chosen as master. **Turns have now been driven through it as master (2026-09-03)** — the permission
dialog, the diff and the step stream all work — and doing so found six defects, five of them fixed
the same day. Phase 4's criterion is still unmet: no conversation has yet run end to end *including
an approved write*, because the free tier's 20-requests-per-day ceiling was reached first.
**That ceiling is what [AG-14](decisions.md#ag-14) answers**, reversing AG-2's exclusion of `agy` and
adding phase 8 — a subprocess-driven `agy` as a second transport, and the only one that reaches the
user's paid subscription. This directory is
the plan of record for adding Google's Antigravity SDK as a second agent backend beside the existing
Claude Code engine.

[`../plan/`](../plan/) is the finished conversion that produced the current single-engine build. This
directory is a *different piece of work* and does not extend it: its decisions are `AG-n`, its risks
are `AG-R-n`, and where it needs a fact about the existing engine it cites the code rather than
restating that directory's reasoning.

## Where we are (2026-09-05)

**Every phase is closed.** Phases 0–9, with phase 5 (history and sessions) and phase 7 (packaging)
both landing on 2026-09-05. Two things remain open and neither is a phase: phase 4's criterion on the
SDK transport — a full conversation including an approved write, blocked by the free tier's daily
ceiling rather than by code, and *met* on the `agy` transport in phase 8 — and the three surfaces
`capabilities.py` still marks `unbuilt`: subagent tabs, agent-initiated questions, and the MCP server
inventory.

**Phase 5 landed on 2026-09-05** and cost about what its groundwork entry predicted: one observer
module and seven delegations, with no `SessionStore` implementation and — contrary to what this
directory said for a week — no sibling of `history.py`. A conversation is mirrored under the
engine's own conversation id, each transport into its own store root ([AG-1](decisions.md#ag-1)),
and a restart resumes it through the engine's own mechanism rather than by replaying our transcript
into a prompt. Verified across two processes against the paid subscription, and then in a browser,
which found two defects nothing else could: the router's refusal of a *different* surface rendered
as red text inside the newly-visible history panel, and a Fork button on an engine that refuses
forking. Both are the same shape — a surface newly enabled exposing an unguarded call to another —
and both are now descriptor-guarded. See
[`delivery.md` § Phase 5](delivery.md#phase-5--history-and-sessions-and-the-two-things-a-browser-found-2026-09-05).

The paragraphs below are the 2026-09-03 picture and are kept for the reasoning they record.
Phase 3's engine spike
and phase 6b's streaming consultation have both been **run against the network** and passed, and on
2026-09-03 **so has the permission gate**: a browser answered a real `edit_file` dialog against a
live Antigravity turn, the diff rendered at +5 −0 with the correct hunk, and the write landed. That
run and its six findings are in [`delivery.md` § Phase 4](delivery.md#phase-4--the-live-run-and-the-four-things-it-found-2026-09-03). The engine router is
wired into startup, the master engine is chosen per session, and the webapp hides surfaces the
running engine cannot feed. The surface of both Antigravity products was read first-hand — the
installed `google-antigravity` 0.1.15 wheel (0.1.16 since 2026-09-03), and the `agy` 1.1.22 binary (re-probed at 1.1.25). **Phase 2 was taken out of
order**, ahead of phase 1, because it was the gate everything else was contingent on and it turned
out to be cheap. The results are in [`sdk-surface.md`](sdk-surface.md); the choices they force are in
[`decisions.md`](decisions.md); what each phase actually did is in [`delivery.md`](delivery.md).

**What you can use today.** An ordinary Claude turn can call
`mcp__aic-dc-antigravity__second_opinion` and `mcp__aic-dc-antigravity__generate_image`. Ask Claude
for a second opinion and it reaches Google's model; both calls go through the permission dialog,
because they mount on their own server rather than the ungated index one. Since 6b the consultation
**streams into its own agent tab** as Google produces it, rather than sitting behind one tool card
until the answer lands. When neither transport can run the tools are absent rather than broken.

**Since [AG-16](decisions.md#ag-16) (2026-09-06) there are two ways to have them**, and the default
prefers the second: a Gemini key in `~/.config/aic-dc/gemini-api-key` *plus* the
`google-antigravity` extra, or the `agy` CLI with the permission gate installed from Settings. The
`agy` route needs no key and no wheel, and it is the only one where `generate_image` has an
allowance at all — a free-tier key reports `limit: 0` for every image model. `engines.consultant` in
`app.json` names one explicitly; `auto` takes `agy` when it can run.

Antigravity can now also be selected as **master** for a session, and that path has been driven from
the browser as of 2026-09-03 — but it is still far less exercised than the consultant. The first turn
through it found that `chat_streaming` held the browser's RPC open for the whole turn, that every
read-only call raised a dialog, and that a turn killed by a rate limit rendered nothing at all. Those
are fixed; what has not been demonstrated is one conversation running end to end *including an
approved write*, because the verification turn hit the free tier's request ceiling first.

**The free tier is slower than it looks, and that is now the main practical constraint.** Measured
2026-09-02 and confirmed by Google: a free-tier key's requests are **queued behind paid traffic
rather than refused**, so *per-minute* capacity rationing arrives as latency and never as a `429`.

**Corrected 2026-09-03: that is the per-minute story, and there is a second, harder one.** Alongside
the queueing there is a **daily** ceiling that does refuse — `quotaId:
GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`, arriving as a real `429` with
`RESOURCE_EXHAUSTED` and a `retryDelay`. Twenty agent *requests* per model per day is a handful of
turns, not a day's work: it was reached in an afternoon of phase-4 verification and then blocked the
rest of it. So the free tier fails two different ways — slowly within a minute, and flatly within a
day — and only the second announces itself. A five-token
prompt took 30.9 s then timed out at 70 s on `gemini-3.7-flash` while `gemini-3.5-flash` answered in
3.9 s. The consultant's pinned model was lowered to `gemini-3.5-flash` to keep the feature working;
a paid key should raise it back, because the point of a second opinion is a *capable* independent one.
Google's own summary of an agent on the free tier is *"practically unusable for interactive
work"* — which is the strongest of AG-12's upgrade arguments and the least visible, since
`models.list` reports authorization rather than availability and nothing else reports it at all.

Two things were outstanding here and neither was a code problem. **Image generation is funded as of
2026-09-08** — not by buying the key's tier, but by the transport: `agy` reaches the subscription and
generated one, where every Gemini image model still reports `limit: 0` on a free-tier key
([AG-12](decisions.md#ag-12) stands for the SDK route). And **a *successful*
consultation has not been watched streaming into its tab**: the tab itself was confirmed in the
browser on 2026-09-02 — it appears, labels, settles and mirrors a row into Main — but both live
attempts that day stalled provider-side, so the chunk-by-chunk rendering is verified by
`scripts/probe_consultation_tab.py` and not yet by eye.

**Probes live in `scripts/`, not here.** This directory is the plan of record and holds Markdown
only. The four live-verification spikes — `probe_edit_args.py`, `probe_consultant.py`,
`probe_session.py`, `probe_consultation_tab.py` — sit beside the project's other smoke tests, and
each phase's entry in [`delivery.md`](delivery.md) cites the one that settled it.

**`agy` was re-examined on 2026-09-03 and the exclusion was reversed — see
[AG-14](decisions.md#ag-14), which adds it as a *second transport* beside the SDK engine rather than
in place of it.** The question was reopened because the paid access is a Google AI Pro subscription
reachable only through `agy`'s OAuth, while the SDK's key refuses at 20 requests/day — so this is
"reach the account the user pays for, or do not run" rather than a preference between transports.

Both of [AG-2](decisions.md#ag-2)'s original disqualifications turned out to be about the wrong
channel. `agy` 1.1.25 has lifecycle hooks: a `PreToolUse` hook receives `TargetFile` /
`TargetContent` / `ReplacementContent` **before the write**, answers `deny` with a reason the model
reads, and can `overwrite` the arguments — the whole of AG-5's architecture, in the SDK's own field
names. The gate **fails closed** on timeout, non-zero exit, malformed JSON and missing command; the
single fail-open case is exit 0 with empty stdout, which is ours not to write.

Two further measurements closed the objections that survived those: hooks **do** fire in
bidirectional `stream-json` mode and not only under `-p`, and the hook payload's `conversationId` is
**exactly** the `init` frame's `conversation_id` — so a gate living in the user's global
`hooks.json` can be scoped to the host's own sessions and pass every other one straight through.
That last point was the blocker, because workspace-local `hooks.json` does not load headlessly in
1.1.25 and `workspacePaths` is empty in every captured payload.

**The terms clause is now live rather than moot** — AG-2 made it moot by not driving `agy` at all.
It is recorded in AG-2 and the user chose to proceed knowing it.

**One correction is recorded rather than quietly fixed.** The first pass reported the hook gate as
*failing open* on a timeout and that reached both `decisions.md` and `risks.md` before it was checked.
It was wrong: the probe's `matcher` named a single tool and the model routed around it. Re-run with
`"matcher": "*"` the write was blocked. The lesson is AG-R-11's own — assert on the file, not on the
hook having fired — and it is now recorded twice because it has been learned twice.

Eight findings determine the shape of everything below: five from the assessment and the permission
gate, one from building the router, and two from building the step pump.

**There are two Antigravitys and they are not the same program.** The Python SDK bundles and spawns
its own 119 MB Go binary; the `agy` CLI is a separate 208 MB product that shares no symbols with it.
They have separate state directories and, decisively, **separate authentication**: `agy` is
OAuth-authenticated against the owner's account, while the SDK accepts only a Gemini API key or a
Vertex project and contains no OAuth code at all. The engine cannot borrow the login that already
exists.

**`agy` looked like the better transport and is disqualified.** Its flag surface is a startlingly
close analogue of the `claude` CLI — `--continue`, `--conversation`, `--output-format stream-json`,
`--input-format stream-json`, `mcp`, `plugin`, `agents` — and its NDJSON protocol is real and typed.
Two measurements ruled it out. Headless mode **structurally cannot prompt for permission**: the three
postures are auto-deny, static allowlist, and blanket bypass. And **tool content is not on the wire**:
a `write_to_file` frame names the target path and carries neither the bytes nor a result, so no diff
can be rendered from it. (Re-measured at 1.1.22, this is narrower than first recorded — tool
*results* are present in `tool_info.output`; only file content is missing. The correction is marked
in `sdk-surface.md`. It does not change the decision: the diff is the product, and the diff is what
is absent.)

**The seam is a router, and it mounts under the existing namespace.**
`src/aic_dc/engine_router.py` is registered in place of `ClaudeCodeService` as of 2026-09-01, under
the *same* RPC name, with its 48 delegating methods **generated** from the adapter's rather than
hand-written — jrpc-oo reads a service's method list off the class, so a `__getattr__` forwarder
would have exposed nothing at all, and a hand-written list drifts silently into methods that work in
Python and 404 on the wire. It routes to one engine today and says so through `list_engines`. The
change it nearly shipped with is recorded in [`delivery.md`](delivery.md): jrpc-oo injects the
server-push call proxy onto the *registered* instance, so reading it off the service behind the
router would have dropped every streamed chunk and every permission dialog behind one warning.

The router also answers a question that looked like an open design fork and was not: a method whose
surface the running engine cannot feed is **refused, not missing**. It stays on the wire and raises
`UnsupportedOnThisEngine`, because omitting it would give the browser a transport-level "no such
method" — indistinguishable from a broken build. That is [AG-9](decisions.md#ag-9) one layer down,
and `RPC_SURFACES` derives the refusals from the descriptor rather than from a second list that could
disagree with it. The practical effect: the Antigravity adapter needs **31** of the 48 methods, not
all of them, and `build_router(adapter, engine=ANTIGRAVITY)` prints exactly which.

**The Python SDK's core is a genuinely good fit** — better than expected. Streaming deltas, mid-turn
cancel, resume by `conversation_id`, an async permission hook that receives the full tool call,
post-tool-call hooks, compaction steps, subagents with per-trajectory usage, and — the pleasant
surprise — **plain Python callables as tools**, which makes the symbol-index bridge simpler here than
it is under Claude Code. The gaps are real but bounded, and they are gaps in *reporting*, not in the
loop.

**The permission gate passed, and it raised a harder problem than the one it closed.** The hook
receives the full proposed edit — `edit_file` hands over old text, new text and a line range, so the
dialog can render a diff without even reading the file — and denying it leaves the file untouched.
That was the contingency the whole plan hung on and it is now measured rather than hoped for. But the
same probe showed the agent, refused an `edit_file`, going straight for `run_command` to make the
same change by other means: `sed -i`, then inline `python3`, unprompted, on both runs. **A dialog
that gates only the file tools shows the user a diff, records their refusal, and lets the edit
through anyway.** The permission seam is therefore *all mutating tools*, `run_command` included —
[AG-5](decisions.md#ag-5), [AG-R-11](risks.md#ag-r-11).

**What does not translate is mostly accounting.** There is no USD figure anywhere on either
Antigravity surface, and no context-window read-back at all. The turn footer, the session cost, the
`max_budget_usd` cap and the Context tab's bar have no source data. In the other direction,
Antigravity offers image generation, agent-initiated structured questions, audio and video input,
daemon commands and out-of-band triggers, none of which has a home in the current UI.

**A tool's arguments and its result are the same object, and the stream is not the hook.** Claude
sends a `tool_use` block and later a separate `tool_result`. Antigravity sends the *same* typed
sub-message twice — at `ACTIVE` with the inputs, at `DONE` with the outputs filled in beside them —
so a pump that forwards `ToolCall.args` renders a card whose "input" grows a command's entire stdout
on completion, and emits no result at all. Worse for phase 4: the step stream's `edit_file` carries
`{file_path, diff_block}` while the *hook* carries `TargetContent` + `ReplacementContent` + a line
range, and `view_file` on the stream carries no content at all. **The diff the dialog renders comes
from the hook.** Details and the full field table in
[`sdk-surface.md` § The step stream](sdk-surface.md#the-step-stream--read-in-phase-3-and-it-is-not-shaped-like-claudes).

**The SDK's own `nondestructive()` is not a write boundary.** It excludes only `run_command`,
classifying `create_file`, `edit_file` and `generate_image` as nondestructive — defensible for "will
this hurt the machine", exactly backwards for "will this change the working tree". An adapter
adopting it would enable the two tools AG-5 exists for. `options.MUTATING_TOOLS` is therefore ours,
and it gained `start_subagent` during phase 3: a subagent inherits the tool set, so a gate that stops
at the top-level trajectory is bypassed by asking a child to do the write. Same hole as AG-R-11's
`run_command`, one level down.

Read [`sdk-surface.md`](sdk-surface.md) before touching anything. Read
[`decisions.md`](decisions.md) before reading the specs; they assume it.

## The one-paragraph version

AIC⚡DC gains a second agent backend. Exactly one engine is master per session and the other is
reachable as a consultant — a one-shot call for a second opinion, or for a capability the master
lacks, of which image generation is the worked example. The second engine is
`google.antigravity.Agent` driving its bundled `localharness`, **not** the `agy` CLI. It mounts under
the *same* RPC namespace as the first, so the browser's 43 call sites across 59 files do not fork,
and it reports which surfaces it supports through a capability descriptor so the ones with no
counterpart are **hidden rather than stubbed**. The first thing built is the consultant, because it
delivers the owner's own example in the smallest increment and forces the two questions that gate
everything else — a real API key, and whether the permission hook can render a diff — to be answered
with facts.

## Why a second engine

Three reasons, in order of weight:

1. **Disjoint capability.** Google offers image generation; Anthropic does not. That is not a
   preference between models, it is a thing one engine can do and the other cannot, and the
   consultant pattern makes it reachable without either engine giving up its own strengths.

   **Delivered 2026-09-08, on the subscription rather than on a key.** `generate_image` returned a
   picture for the first time — a 1024×1024 JPEG, collected into the repository and verified there —
   through the `agy` transport ([AG-16](decisions.md#ag-16)). Getting it took one correction the
   probe existed to find: the tool takes a *name*, not a path, so the harness writes into its own
   per-conversation directory and the host has to collect the file rather than ask for it
   ([`sdk-surface.md`](sdk-surface.md#generate_image-takes-a-name-not-a-path--measured-2026-09-08)).

   **On the SDK transport it remains unfunded**, and that is unchanged: every Gemini image model
   reports `limit: 0` on the free-tier key, which is not a throttle — the plan's allowance is zero,
   and no wait changes it. The fix is not a different key: the tier is a property of the key's Cloud
   project, so enabling billing on that project moves the *same key* to a paid tier and nothing in
   `credentials.py`, the resolution order or the key file moves ([AG-12](decisions.md#ag-12)). What
   changed is that this reason no longer waits on that, because there are two transports and only one
   of them is metered.
2. **A second opinion is worth something.** Two independent agents disagreeing about a diff is
   information. One agent asked twice is not.
3. **Not being one vendor deep.** The engine layer's whole design is that AIC⚡DC renders an agent
   session rather than constructing one. That claim had never been tested against a second SDK, and
   an abstraction validated by one implementation is a naming convention.

   **Tested now, and it mostly held — with the exceptions being the valuable part.** The permission
   broker, `ReviewMode` and `commit.py` all took a second engine with no change, because their
   collaborators were already injectable. What did *not* survive contact: the RPC surface needed a
   router with generated delegates rather than a shared base class; the event vocabulary needed a
   capability descriptor to say what a second engine cannot feed; and the transcript, history and
   cost layers turned out to be shaped around the Claude CLI's message types rather than around
   "an agent session". Two of those three are now built. The third is phase 5, and it is the honest
   measure of how far the abstraction actually reached.

## What AIC⚡DC still contributes

Unchanged from the single-engine build, and the reason [AG-5](decisions.md#ag-5) treats the
permission dialog as non-negotiable rather than as a feature to port:

- **Spatial code navigation** — a Monaco diff viewer over every file the agent touches, a git-status
  file tree, an SVG editor, a TeX preview.
- **Repo intelligence as tools** — the tree-sitter symbol and document indexes, reaching Antigravity
  as plain callables ([AG-4](decisions.md#ag-4)) rather than through MCP.
- **Permission UX with a diff in it** — the thing `agy` structurally cannot support, and the reason
  it is not the engine.
- **Multi-client collaboration**, and **documents** — both engine-agnostic already.

## Phases

Each phase is independently shippable and leaves the tree working. Phase 0 is this directory.

| Phase | Scope | Exit criterion |
|---|---|---|
| **0. Assessment** ✅ | This directory. Both surfaces read first-hand; three live `agy` turns. No code changes. | `sdk-surface.md` records the verified surface with file:line citations and raw captures; `decisions.md` records the choices it forces; unknowns are stated as unknowns. |
| **1. Consultant and probe** ◑ **(live)** | Antigravity as a one-shot tool under Claude Code — `generate_image` and `second_opinion`, on their **own** MCP server rather than the ungated `aic-dc` one ([AG-5](decisions.md#ag-5)). Credential resolution reporting its source, from the environment or the stored key file ([AG-11](decisions.md#ag-11)). `src/aic_dc/antigravity/surface.py` and its gate ([AG-8](decisions.md#ag-8)). | The agent generates an image from a Claude Code turn and it lands in the repo where the file tree and viewer find it. The probe's `unclassified` bucket is empty by declaration. A sentinel write lands at its expected absolute path ([AG-R-3](risks.md#ag-r-3)). **Two of three met (2026-08-31); the image is blocked on billing, not on code — every Gemini image model is `limit: 0` on a free-tier key, which is a known and accepted cost of [AG-12](decisions.md#ag-12) rather than an open defect. AG-R-3 settled: `workspaces` is honoured.** |
| **2. Permission gate** ✅ | `scripts/probe_edit_args.py` — a `PreToolCallDecideHook` logging its `ToolCall` and denying. Run out of order, ahead of phase 1, because everything downstream is contingent on it. | **Met — go.** The hook carries full edit content and `allow=False` blocks the write. [AG-R-1](risks.md#ag-r-1) retired; [AG-R-11](risks.md#ag-r-11) raised. `trustedWorkspaces` remains unsettled and moves to phase 1. |
| **3. Engine spike** ✅ | `src/aic_dc/antigravity/{options,steps,session}.py` — session lifecycle, options assembly, and the `Step` → `Event` pump. **Not registered:** there is no engine registry to register with, and inventing one against no second caller is phase 4's problem. | `scripts/probe_session.py` sends a prompt and prints the streamed step taxonomy, failing if no tool call and result arrive. **Met, 2026-09-02.** A real `localharness` session, a `list_directory` call and its result, `PASS` on the first run. The offline half is 94 tests, and the live run found **three bugs none of them could see** — an empty `turnUsage`, a stop reason that was always blank, and (once that was fixed) an `UNSPECIFIED` that would have put a red badge on every clean turn. All three traced to one cause: `FakeConversation` answered to `stop_reason`, a name the real SDK does not have. See [`delivery.md`](delivery.md#phase-3--the-live-run-and-the-three-bugs-it-found-2026-09-02). Three earlier findings in [`sdk-surface.md` § The step stream](sdk-surface.md#the-step-stream--read-in-phase-3-and-it-is-not-shaped-like-claudes); the write seam gained `start_subagent`. |
| **4. Chat on the second engine** ◑ | The chat panel renders the Antigravity stream — text, thinking, tool cards, results. The permission dialog lands against the phase-2 mechanism. | A user holds a full working conversation, including edits, entirely through Antigravity, with every write approved through the dialog. **The gate and the adapter both landed (2026-09-01):** `permissions.py` drives the *shared* `PermissionBroker` — one ask path, one queue, one localhost rule across both engines — and `service.py` implements the 31 methods the router mounts, sharing the symbol index, `ReviewMode` and `commit.py` rather than copying them. **AG-1's per-session choice landed the same day** — `main.py` constructs both adapters, `app.json`'s `engines.master` names the one that starts, and `switch_engine` changes it mid-run; see [`delivery.md` § AG-1](delivery.md#ag-1--one-master-per-session-chosen-per-session-2026-09-01). **A live turn ran 2026-09-03 and the criterion is NOT met** — see [`delivery.md` § Phase 4](delivery.md#phase-4--the-live-run-and-the-four-things-it-found-2026-09-03). The switch, the notice, the step stream and the `edit_file` diff (+5 −0, correct hunk) all work, which settles AG-5 in the browser. Four defects, in the order they must be fixed: (1) **`chat_streaming` awaits the whole turn** where Claude's returns on acceptance, so the browser's 75s JRPC deadline renders `Error: Timed out waiting for response`, discards the rendered transcript, and the agent then edits the file anyway — a permission dialog guarantees the overrun, since the user's thinking time is inside the budget; (2) **every read-only call raises a modal** — `ALWAYS_ASK`/`GATED_BY_DEFAULT` shape the dialog's wording but nothing consults them before `broker.can_use_tool`; (3) the **read tools' `ARG_ALIASES` name fields the SDK does not send** (`AbsolutePath`, `Pattern`/`SearchDirectory`), so the dialog shows `PATH (none named)`; the mutating aliases are correct, which is why nobody noticed; (4) the hook and the step stream use **two vocabularies for one call**. **(1)–(3) fixed the same day** and verified on a second live conversation — zero dialogs for two reads, both tool cards rendered and kept, the turn settled cleanly. Fixing (2) meant connecting `denied_read_files`, which had never had a reader. **Still open:** the end-to-end criterion (a full conversation *including an approved write*) — the verification turn died on a free-tier `429` before reaching the edit — **(5) fixed the same day** — a turn killed by a rate limit rendered nothing, because `onSystemEvent` handled only `conversation_reset` and dropped every `engine_error`, including the one naming the retry delay in seconds; the three user-facing subtypes now land as durable transcript cards. Building it surfaced a **sixth**: a `{role: 'system'}` row renders under an *"Assistant"* heading, since `renderMessage` labels on `system_event` rather than on the role — so `handleUnsupportedSlash` had been putting the engine's refusals in the assistant's voice. Both are shared chat-panel defects that any engine met; only their frequency was Antigravity's. |
| **5. History and sessions** ✅ | Resume by `conversation_id`; a repo-local mirror rebuilt as a step observer rather than as a store implementation, since there is no `SessionStore` protocol to implement. **Its own store root**, so that a record written by one engine cannot be handed to the other ([AG-1](decisions.md#ag-1)). | Restarting the server resumes the previous Antigravity conversation with context intact, and the history browser renders it. **Met, 2026-09-05**, and proven twice: `scripts/probe_agy_resume.py` holds a conversation in one process, restarts into a **second process**, and the model still returns a passphrase only the resumed context holds — the assertion the mirror alone cannot make, since a transcript on disk looks identical for a resume that silently opened a blank conversation. Then the same path was driven from a browser across two fresh servers. The build is one new module (`antigravity/mirror.py`, an observer of the events both translators already emit) plus seven delegations to `claude_code.history`; **there is no `history.py` sibling** — that module takes the store as an argument and was engine-agnostic already, which is the one thing the pre-phase estimate had backwards. `Step` being flat turned out to be a fact about the *pump*, which had absorbed it in phase 3. Three store roots rather than two: `agy` and the SDK reach the same product through different harnesses and neither can resume the other's ids. The groundwork entry's contract needed one correction — `parentUuid` is droppable for a *single* entry and load-bearing for a conversation, since the reader walks back from one terminal, so a mirror written to its letter would have rendered every session as its last message. Running it found four things: **two only a browser could see** — the router's `subagent_tabs` refusal rendered as red text in every preview, and a Fork button on an engine that refuses forking (now the `session_fork` descriptor row) — one wrong number on screen (a closing usage entry counted as a message; the counters are no longer mirrored, because this engine has no per-message usage to place), and one shipped table divergence: `files_written_by` and the pump's own path-field map, merged so a browsed turn attributes the files a live one did. See [`delivery.md` § Phase 5](delivery.md#phase-5--history-and-sessions-and-the-two-things-a-browser-found-2026-09-05). |
| **6. Capability descriptor** ✅ | The descriptor of [AG-3](decisions.md#ag-3) made real, and every surface in § *What does not translate* given an entry. Per-engine hiding across the Context tab, HUD and settings. | No surface renders an empty or synthesised value for a fact its engine cannot report; no webapp branch keys off an engine name string ([AG-R-4](risks.md#ag-r-4)). **Met, 2026-09-02.** `src/aic_dc/capabilities.py` holds 14 surfaces, distinguishing `absent` (no source data, ever) from `unbuilt` (a to-do); the router publishes it, `engine-capabilities.js` answers `supports()` synchronously, and seven surfaces hide on it across the HUD, the Context tab, the settings tab and the chat panel's action bar. Cost hides at the *figure*, not the row ([AG-6](decisions.md#ag-6)). Six surfaces still have no consumer, because they have no UI on either engine yet — see [`delivery.md`](delivery.md#phase-6--the-rest-of-the-consumers-2026-09-02). **Amended 2026-09-03:** hiding twelve surfaces at once reads as a broken build rather than as a different engine, so the chat panel now names the engine that is answering and lists the surfaces it has not had built for it — keyed on `unbuilt`, never on an engine name. See [AG-9 § What hiding cost](decisions.md#ag-9--engine-specific-surfaces-are-hidden-never-stubbed). |
| **6b. The consultation as an agent tab** ◑ | [AG-13](decisions.md#ag-13). The consultant streams `Conversation.receive_steps()` through the existing `StepTranslator`, tagging every event with a minted consultation id, and emits `subagentEvent` so the tab strip picks it up. Stop wired to `Conversation.cancel()`. Cost hidden via the descriptor. | A `second_opinion` call from a Claude turn opens its own tab, fills with thinking and text as Google produces them, can be stopped mid-flight, and shows tokens with no USD figure. **No webapp change** — if one is needed, the id contract has been got wrong. **Met live 2026-09-02**: `scripts/probe_consultation_tab.py` drives a real Gemini consultation and all six contract checks pass — 13 chunks streamed progressively, each carrying the consultation id, terminal event last. No webapp file changed. **Confirmed in the browser 2026-09-02**: the dialog gates it, the tab appears, settles and mirrors a row into Main, with no webapp change. It also found a real bug — a failed consultation settled as a green `completed` — now fixed and re-verified. Outstanding: both live browser attempts timed out provider-side, so a *successful* stream has not been watched rendering; ⏹ Stop is now routed end to end (`stop_task` → `ConsultantBridge.cancel()`), and a stalled consultation reports a heartbeat every 20s and explains itself with the harness's own stderr when it gives up. |
| **8. `agy` as a second transport** ✅ | [AG-14](decisions.md#ag-14). One long-lived `agy --print="" --input-format stream-json` process per session; a `PreToolUse` hook in the user's global `hooks.json` that blocks on AIC⚡DC's dialog and returns the human's answer, scoped by `conversationId`; the step stream for the transcript and `transcript_full.jsonl` for what it omits. Reuses the shared `PermissionBroker`, the dialog, and the payload builders — the hook's argument names are the SDK's own. | A user holds a full conversation on the **paid subscription**, including an approved write, with the gate proven by a tripwire that asserts the *file* is unchanged after a deny — not that the hook fired. Plus: a second `agy` session belonging to the user runs concurrently and is **never** intercepted. **The approved write is met (2026-09-05)** — driven from a browser against a freshly started server on a throwaway repo, `replace_file_content` rendered as a write with a real `+1 −1` diff, was allowed by a click, and the edit landed on disk. The reason-carrying deny was watched steering the agent off an over-broad `find` and onto the right file — [AG-R-11](risks.md#ag-r-11) contained rather than escaping. **The isolation half is met the same day**: a second `agy` session of the user's own ran concurrently with one this host owned — 13.2s of measured overlap — and was **never intercepted**, while the gate decided 9 calls, all ours. Getting there took three failed runs, all defeated by [AG-R-3](risks.md#ag-r-3) rather than by the gate, and that exposed the real finding: `probe_agy_gate.py`'s deny tripwire had the same hole, since under write-diversion "the file is unchanged" is true whether or not the gate works, so its recorded PASS rested on an unchecked assumption. It also put AG-R-3's *stated cause* in doubt — writes diverted from inside a trusted root, and what the diverted files have in common is being newly created rather than edited. Setup is now shared in `scripts/_agy_probe_support.py`, which refuses to run outside a trusted workspace. Running it also found a shipped bug 4,317 green tests could not see: `AgyTranslator` had no `stats`, so every dialog on this transport raised `AttributeError` in the inherited `_note_permission_prompt`, losing the turn's prompt count. See [`delivery.md` § The approved write](delivery.md#phase-8--the-approved-write-and-what-running-it-found-2026-09-05). |
| **9. "Always allow" on Antigravity** ✅ | [AG-15](decisions.md#ag-15). The dialog offers `Allow once` and `Deny` only, because `suggested_rules` is hardcoded empty — correct while the reasoning was "the engine has no `updated_permissions`", and incomplete because AIC⚡DC was always going to own persistence. Derive rules with the existing `derive_suggested_rules` (its no-suggestions fallback is engine-agnostic), persist them in a store of ours, and consult them in `AntigravityPermissionGate.pre_verdict` — the seam that already keeps reads out of the modal on **both** transports. Merge the path-rule tool tables rather than keeping a second copy, or the control silently never appears for file edits. | A repeated `run_command` approved once with "always allow" raises no dialog in a later session *after a server restart*, while a different command still does — asserted by the second call never reaching `broker.can_use_tool`. Plus a widening tripwire: a rule from `rm -rf build/` must not match `rm -rf /`. **No webapp change** — `allow_always` and the rule control already exist; if one is needed, the rule shape is wrong. **Built 2026-09-05**, and that prediction was almost exactly right: one line, a `DESTINATION_FILES` label for the new destination, because the chip renders where the rule went and every existing entry names a `.claude/` file. The shape needed nothing. Matching is exact — `rm -rf build/` does not match `rm -rf /`, `git push:*` does not match `git pushover`, and a path rule is one file for one tool, since matching by *class* would let a grant made by reading a diff also permit a whole-file overwrite. `pre_verdict` consults the store **before** `ALWAYS_ASK`, or the control would have no effect on the writes it was pressed for. **Met at the seam** — 17 new tests, the call never reaches `broker.can_use_tool` — and **met in a browser on 2026-09-06**: *always allow* clicked on a live `run_command`, the rule written, and the same command in a later turn allowed by it, asserted from the log line rather than from the absence of a dialog on screen (an absent dialog is also what a turn that never got that far looks like). The sitting that closed it found the always-allow *tooltip* wrong for the second time in two days — see [§ The verification sitting](delivery.md#the-verification-sitting-and-the-two-defects-it-cost-to-buy-2026-09-06). See [`delivery.md` § Phase 9](delivery.md#phase-9--always-allow-on-antigravity-2026-09-05). **Reviewing and revoking landed the same day** ([§ Phase 9b](delivery.md#phase-9b--the-half-ag-15-shipped-without-seeing-and-revoking-2026-09-05)), because the first cut gave the user a way to grant a standing permission and no way to take it back — one click to give, a text editor to undo. Two `Settings` RPCs and a panel listing the rules with a Forget button; ids derived from what a rule *grants* rather than its position or its label, so a list refreshed between render and click cannot revoke the wrong one. It went on `Settings` and not the engine adapter because the router holds that Claude refuses nothing and Antigravity exposes nothing Claude does not — `get_agy_gate` was the precedent, for the stronger reason that a standing permission outlives the session that granted it. |
| **7. Packaging** ✅ | `google-antigravity` as an optional extra, not a base dependency — a second bundled binary on top of the ~295 MB CLI ([AG-R-10](risks.md#ag-r-10)). | A base install is a one-engine install with no broken UI, and its size has not moved. **Met, 2026-09-05**, measured against two clean venvs: `aic-dc` is **273.1 MiB** and `aic-dc[antigravity]` is 408.3 MiB, so the extra is 135.2 MiB of which `localharness` alone is 123.1 MiB. (The first cut of that table was wrong by ~9 MiB in the base column and the difference moved with it: the venv had been *run*, so `__pycache__` counted. Measure before first run.) **It is a *two*-engine base install, not one** — that is what made the extra affordable rather than a loss: the `agy` transport reaches the same product over a pipe and imports nothing from the SDK, so it mounts with no wheel. The extra buys the API-key route and, at the time, the consultant — which this row said *"has no `agy` equivalent"* and which [AG-16](decisions.md#ag-16) reversed on 2026-09-06: the CLI runs headlessly per prompt, so a base install now has a consultant too, on the account the user pays for. The size claim is unaffected; what changed is that the extra buys the API-key route and a *pinned model*, rather than the feature. **The work was not the `pyproject.toml` edit.** Every `from google.antigravity` in the package is already function-local, so a base install imported cleanly, constructed every adapter and offered the engine in the selector — the absence surfaced only as an `ImportError` on the first turn, which is exactly the "broken UI" this criterion forbids and is invisible to a suite running where the wheel exists. `surface.sdk_installed()` is now the one authority, the engine and the consultant mount on it as they already mounted on a credential, and the diagnostic names which of the two is missing — the first cut told a user with a key to go set one. Verified by running both installs; tripwires in `tests/test_antigravity_packaging.py` and in the release workflow. See [`delivery.md` § Phase 7](delivery.md#phase-7--the-sdk-becomes-an-extra-and-what-that-exposed-2026-09-05). |
| **10. The consultant on the paid transport** ✅ | [AG-16](decisions.md#ag-16). `second_opinion` and `generate_image` over `agy` as well as the SDK, behind the one `ConsultantBridge`, with `engines.consultant` naming a transport and `auto` preferring `agy`. Containment is a `StaticPolicy` on the gate rather than a restricted `enabled_tools`, because this transport's tool set is the binary's — and the consultation is refused outright without the hook installed, since an unclaimed one is an unreviewed agent in the repository. | A real `generate_image` writes a picture **inside the repository** on the subscription, verified by `stat` and containment rather than by the tool's own report (AG-R-3), with the gate's log showing `generate_image` as the only allow; and a real `second_opinion` returns prose having been allowed nothing at all, with no `streamComplete` reaching the tab. **Built 2026-09-06** — one new module, a second gate posture, a reader split out of `stream_turn`, and 32 tests; 4,547 green. **Met 2026-09-08**, on two real turns: a second opinion allowed **nothing** (`allowed []`, on an agent with 57 tools available) and an image verified inside the repository with `generate_image` the only allow — the first picture this feature has ever produced, since a free-tier key reports `limit: 0` for every image model. The one thing the offline tests structurally could not settle — which argument name `agy` gives the output path — was settled by being **falsified**: there is none, on either transport. The tool takes `ImageName`, *"Short descriptive name for the saved file"*, and the harness writes to `brain/<conversation_id>/<ImageName>_<epoch_ms>.jpg`; `output_path` is a **result** field that reads as an argument on the SDK transport only because that stream merges results into `args` at `DONE`. The first run therefore generated a real image outside the repository and the probe refused it, correctly. The consultant now collects it: located from the conversation id and the model's chosen name rather than from prose (AG-R-3), copied in, produced extension winning over the requested one, and the prompt no longer asking the model to place the file — which had made it reach for `run_command`. Closing run `PASS`, `denied along the way: []`; 4,608 tests. See [`delivery.md` § Phase 10](delivery.md#phase-10--the-consultant-on-the-paid-transport-and-the-argument-that-does-not-exist-2026-09-08) and [`delivery.md` § AG-16](delivery.md#ag-16--the-consultant-learns-the-transport-that-can-pay-for-it-2026-09-06). |
| **11. Claude-only deployments** ✅ | [AG-17](decisions.md#ag-17). `engines.enabled` in `app.json`, an allowlist defaulting to every engine, consulted by the two adapters' mount points, the consultant, the selector and `switch_engine`. `claude` cannot be removed — a typo should not leave the application with no engine. The consultant follows the engine rather than getting its own boolean, and AG-16's `engines.consultant` composes by intersection: the allowlist decides *whether*, the transport preference decides *which*. A Settings toggle as well, at the user's decision, with its limit stated in the entry: a control a user can switch off is one they can switch back on, so the file is what an organisation manages. | With `engines.enabled: ["claude"]` on a machine that has **both** the `agy` binary with its gate and a Gemini key with the wheel — the configuration where everything else would have mounted — a fresh server reports `mountable` as `["claude"]`, refuses `switch_engine` for either Antigravity engine *with a reason naming the policy rather than a missing binary*, and builds a Claude session whose MCP servers do not include `aic-dc-antigravity` — asserted against the mounted server list, never the log line. Plus the tripwire that keeps it true: a test asserting the set of Antigravity mount points is exactly the set that consults the allowlist. **Built 2026-09-06** — `ConfigManager.enabled_engines` is the one reader (asserted), both adapters and the consultant consult it, `list_engines` filters `available` and carries `enabled`, `switch_engine` answers `engine_disabled`, and the Settings card writes it with an app-restart disposition. 20 tests. **Met on a fresh server, 2026-09-07** — `scripts/probe_engine_policy.py`, 18 checks across **two** servers, because on a machine with no `agy` binary and no Gemini key `mountable == ["claude"]` is already true and a single-server check proves nothing. The control run supplies the two mount conditions (a key in the child's environment, an executable named `agy` on `PATH` — substitutes for a *mount condition*, never for a working transport, and no turn is taken through either) and mounts everything; the Claude-only run, same machine and same environment, gives all three assertions: `mountable == ["claude"]`, both switches refused with `engine_disabled` naming `app.json`, and the session's MCP server list — read from the CLI by `get_mcp_status`, not from our log — holding `aic-dc` and not `aic-dc-antigravity`. An answer with no `aic-dc` in it counts as uninterpretable rather than as a pass. The static tripwire runs from the same command. **Two instrument defects came first and the first is a finding:** the probe's scratch config dir had no `.bundled_version`, so `_run_upgrade` overwrote its `app.json` from the bundle before anything read it — which is also what a real **upgrade** does, silently reverting the policy and making a Claude-only install two-provider again ([AG-R-13](risks.md#ag-r-13)); read-only `app.json` held it, at the cost of an aborted upgrade pass repeating every start. **Both fixed the same day** — `app.json` is merged key by key against a pristine copy of the bundle, and each file upgrades inside its own error handling — with a regression test named for each finding, and the criterion re-measured through the changed config layer. See [`delivery.md` § Phase 11](delivery.md#phase-11--the-engine-policy-and-what-a-fresh-server-said-about-it-2026-09-07). |

## Ordering constraints that are not obvious

- **The permission gate before the engine, not after.** Phase 2 is a gate rather than a task, and it
  is placed before phase 3 because it is nearly free to run and because the answer changes whether
  Antigravity can be master at all for write operations. Discovering it while building the dialog in
  phase 4 means an engine adapter written against an assumption.
- **The probe before the engine.** The SDK is 0.1.x and alpha. Writing the adapter first means
  writing it against a snapshot that has already moved, which is the failure the equivalent Claude
  probe exists to close.
- **The consultant before anything.** It is the only phase that delivers user-visible value before
  the second-engine decision is committed to, and it forces the credential question to be answered
  with a real bill. What survives into phase 3 is narrow and known: config construction, credential
  resolution, the probe. [AG-R-9](risks.md#ag-r-9) is the boundary it must not cross.
- **Phase 6b after the descriptor, not before it.** The consultation tab is the descriptor's first
  real consumer: it must hide its cost panel because [AG-6](decisions.md#ag-6) says this engine
  reports no USD, and doing that by an engine-name check in the webapp is precisely what
  [AG-R-4](risks.md#ag-r-4) forbids. Built before the descriptor was readable from the browser, it
  would have grown exactly that branch.

- **The capability descriptor late, but specified early.** It cannot be built until there are two
  engines to describe, but every phase from 3 onward must record which surfaces it could not serve —
  otherwise phase 6 is an archaeology exercise. **Discharged on 2026-09-01**, immediately after
  phases 3 and 4 produced the list: `src/aic_dc/capabilities.py` holds it, and its `unbuilt` bucket
  is the to-do list as data rather than as memory.

## Reading order for this directory

1. [`decisions.md`](decisions.md) — the binding choices, each with its rationale. Read this first.
2. [`sdk-surface.md`](sdk-surface.md) — the verified surface of both Antigravity products, the raw
   protocol captures, what does not translate in either direction, and
   [§ The probe](sdk-surface.md#the-probe): the gate that keeps the inventory honest as an alpha
   package moves.
3. [`risks.md`](risks.md) — the register, with mitigations and the tripwires that say a risk has
   fired.
4. [`delivery.md`](delivery.md) — one entry per phase, written when its exit criterion is met, plus
   an entry for each piece of work that was not phase-shaped. Newest last, so the tail is the
   current picture.
