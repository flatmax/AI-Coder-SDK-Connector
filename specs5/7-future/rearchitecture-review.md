# Rearchitecture review — "if we started again"

**Speculative, per this layer's rule: nothing here is a commitment and no other layer may depend on
it.** What makes it worth keeping is that it is *measured* speculation. Every number below was taken
from the tree or from a live probe on 2026-09-11, and the two rounds where a number arrived late are
recorded as reversals rather than quietly corrected.

**Provenance.** Three rounds through `mcp__aic-dc-antigravity__second_opinion` on
`gemini-3.8-flash-high`, driven by [`.claude/skills/consult-agy`](../../.claude/skills/consult-agy/SKILL.md)'s
convergence protocol — each round carrying the prior exchange forward as third-party evidence, each
claim checked against the tree before being accepted. The reviewer declared the position converged at
the end of round 3, with one disagreement left standing. The transport measurements it produced are
recorded separately, as [AG-20](../plan-ag/decisions.md#ag-20), because they change a live decision
rather than a future one.

## The question, and the answer to it

*If we re-architected from scratch today, knowing what the system does, what would change — and is a
from-scratch rearchitecture even the right move against targeted refactors?*

**No rewrite.** A ~135k-line production system (63.5k Python, 71.5k webapp, and a near-1:1 test body
behind both) in this problem space is mostly accumulated edge cases: CLI wire-protocol quirks,
streaming boundaries, cancellation races, reverse-engineered vendor behaviour. The core bet is also
already the right one and survives unchanged:

- a capability-descriptor-driven router ([`capabilities.py`](../../src/aic_dc/capabilities.py),
  [`engine_router.py`](../../src/aic_dc/engine_router.py));
- per-engine adapters behind it;
- a webapp forbidden from branching on engine identity, receiving descriptors instead
  ([AG-R-4](../plan-ag/risks.md#ag-r-4)).

That last rule is what makes the rest survivable, and it is the thing a rewrite would most likely
lose.

## The ranking

Ordered by cost of leaving it alone over two years, not by size of the change. Departures from the
reviewer's own final list are marked, because the disagreements are the useful part.

| | Item | Verdict |
|---|---|---|
| **1** | **Containment placement** — confine `agy` in a mount namespace, drop the global hook | The one item with a realized failure and a validated replacement. Full measurement in [AG-20](../plan-ag/decisions.md#ag-20) |
| **2** | **Consultant plumbing** — consultations as child sessions on the master pipeline | *Departure.* The reviewer ranked this MEDIUM in round 2 and then omitted it from its round-3 list without saying why. Promoted here instead, because a defect landed in exactly this seam within the hour — [`known-issues.md`](../known-issues.md) § *An empty consultation tab* |
| **3** | **Static engine contract** — `typing.Protocol` validated at registration, replacing reflection over a reference implementation | Cheap enough (hours) that the rank barely matters; do it whenever the file is open. Both reviewers called the present scheme a smell with near-zero blast radius, and that is right: three engines, a mismatch caught by the first local run |
| **4** | **Surgical extraction from `claude_code/`** — `review.py` (402), `turn_hud.py` (366), the decoupled part of `commit.py` (514) | ~1.3k lines move as-is. *Not* the 6.3k first claimed — see the reversal below |
| **5** | **`chat-panel/`** — leave it alone | Ordinary feature maintenance. Diagnosis of structural rot was raised and withdrawn under measurement |

The structural shape the reviewer would build toward, kept because it names the seam rather than the
files: a **session supervisor** owning turn lifecycle, event multiplexing and history as
engine-agnostic core; adapters demoted to I/O **drivers** translating vendor frames to internal
events; consultations as **child sessions** of that supervisor, so the `second_opinion` MCP tool
becomes a thin bridge over `spawn_child_session(...)` rather than a parallel pipeline.

## Two reversals, recorded because they were reversals

**The trapped-core claim was wrong, and it was mine.** Round 2 argued that ~6,273 lines inside
`claude_code/` are engine-agnostic product features wrongly held there, and ranked it the top
two-year cost. Checking imports refuted it:

| file | lines | verdict |
|---|---|---|
| `history.py` | 1432 | `claude_agent_sdk`: `SessionMessage`, `SessionStore`, `get_session_messages_from_store`, `project_key_for_directory` — **SDK-coupled** |
| `session_store.py` | 753 | `claude_agent_sdk`: `SessionKey`, `SessionStoreEntry`, `SessionSummaryEntry` — **SDK-coupled** |
| `health.py` | 676 | `claude_agent_sdk`, including `_internal.transport.subprocess_cli` — **coupled to internals** |
| `history_index.py` | 575 | `claude_code.history` + `claude_agent_sdk` — **SDK-coupled** |
| `commit.py` | 514 | intra-package only — partially extractable |
| `review.py` | 402 | `messages.Event` only — **clean** |
| `turn_hud.py` | 366 | `.cost` only — **clean** |

3,436 of those lines exist *to read the vendor's own transcript store and install*, so the work is not
a move but a new engine-agnostic transcript/session-store/health abstraction with a driver per engine
— for a benefit (a cheap second master) that traffic says is rarely exercised: `claude` is master in
essentially every real session, `agy`-as-master is rare, and `second_opinion` appears in 25 of this
project's 51 session transcripts. Hence rank 4, not rank 1.

**The `chat-panel/` collapse claim was wrong, and it was the reviewer's.** Round 1 read 34,407 lines
and concluded *"in Lit, when a single panel hits 34k lines, state management has collapsed."* The
split:

- 18,112 test lines, 16,295 production;
- of the production, 3,035 is `styles.js` — CSS-in-JS, no behaviour;
- leaving ~13.3k behavioural across **17** single-responsibility modules, root element `index.js` at
  811 lines, a dedicated `state.js`, and a sibling test file per module.

Withdrawn in full on round 2. The generalisable part is that a directory total is not an architecture
measurement, and neither reviewer noticed until the totals were split.

## The disagreement left standing

On whether the global hook survives as a fallback once mount-namespace containment exists.

**The reviewer:** delete it. Keeping it preserves the fail-open shell line, the cgroup classifier and
static-string attestation, and buys a bifurcated QA matrix — so make `bwrap` a hard prerequisite for
the `agy` engine and fail at the health check when it is missing.

**Held here:** the recommendation is right and its premise is only mostly right. `bubblewrap` is
`Priority: optional` and arrives as an automatic dependency of `libwebkit2gtk-4.1-0`,
`xdg-desktop-portal` and `libgnome-desktop-*` — near-universal on a developer workstation, absent on
a stripped headless box, which is where CI runs. Failing closed at the health check with an
actionable install message is the honest form of its advice, and it is what this repo's standing
instruction about missing commands already requires.

## The recurrence worth more than the ranking

Three subsystems have now failed the same way: **the thing was broken and nothing said so.**

| where | the silence |
|---|---|
| The `agy` gate on a PyInstaller build | Every tool call auto-approved; `status()` reported the gate `current`, because it compares command strings and the strings matched ([`risks.md`](../plan-ag/risks.md) § *The second way it failed open*) |
| The Settings config editor | Rendered two pixels tall; every unit test passed and could not have failed, because jsdom computes no layout ([`known-issues.md`](../known-issues.md)) |
| A consultation tab | Empty while the answer was complete; both emit guards `return` without logging ([`known-issues.md`](../known-issues.md) § *An empty consultation tab*) |

`risks.md` already states the lesson — *assert on the artefact, not on the mechanism* — and records
that it had been learned twice. This is the third and fourth times, in subsystems that share no code.
That pattern is a better argument for the reviewer's attestation reasoning than anything in the three
rounds, because it shows the failure mode is not specific to the gate: **any component that reports
its own health by inspecting its own configuration will eventually report health it does not have.**

## Convention

Same as the rest of this layer: an entry graduates by being specified in a numbered layer with a
behavioural contract and invariants. An entry refuted by measurement stays, marked as refuted, because
the refutation is the finding.
