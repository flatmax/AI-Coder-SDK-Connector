# Known issues

The raw inbox — defects as they are noticed, unsorted. Triaged entries are carried into
[`next.md`](next.md) § C and leave here when they are fixed.

*Was empty as of 2026-08-29.* The prior entry — `specs-reference/5-webapp/viewers-hud.md` § *Cost
rendering* describing the pre-phase-6 HUD — was fixed the same day, and fixing it found two adjacent
sections stale from the same cause and one specified surface with nothing building it. See
[`next.md`](next.md) § B6.

*Empty again as of 2026-09-10.* The entry it held for one day — the token-refresh ladder reporting a
failure for a token that was fine — was fixed the same day it was reported, without passing through
§ C. It never needed triage: the report already carried the measurement, and the measurement was the
whole diagnosis. The reasoning, the two failures it separated and the third defect found underneath
it are in [`impl-history/work-log.md`](impl-history/work-log.md) § *Landed since*.

---

**The Settings tab's config editor laid out two pixels tall.** Reported 2026-09-11 from a live session:
*"in settings when I click on engine config and app config buttons, there is no way to edit the json
files."* ✅ *Fixed the same day — one CSS declaration in
[`settings-tab.js`](../webapp/src/settings-tab.js); the reasoning is in the comment above `.editor-area`
rather than repeated here.* Left in the inbox until the next reader confirms it in their own browser,
since the fix was verified by measurement rather than by looking at it.

`.editor-area` was `flex: 1; min-height: 0` inside a `:host` that is a column flex container with
`height: 100%` and `overflow-y: auto`. Everything above it — the retired note, the agy gate, permission
rules, model, consultant, the preference cards, the card grid — already overflowed the host without it:
**989px of content in a 473px box.** `flex: 1` is `flex-basis: 0%`, so the editor asked for no height and
there was no free space to give it; `min-height: 0` removed the minimum that would otherwise have let its
own 200px textarea hold it open, and `overflow: hidden` clipped what was left. It rendered at 2px — both
borders and nothing between them. `flex: 0 0 auto` measures 237px in the same place.

**Nothing was broken about the behaviour**, which is why this survived: `_openCard` fetched the content,
`_editorContent` held it, the card took its `active` ring, Ctrl+S would have saved. The editor was
present, populated and two pixels tall. **Every settings-tab unit test passed throughout**, and could not
have done otherwise — jsdom computes no layout, so "the rule is in the shadow root" is the most any of
them can assert. That is [`next.md`](next.md) § D's argument for the layout harness arriving from a
direction D2 did not list, and the harness
([`layout-harness.js`](../webapp/src/layout-harness.js)) had no settings scene. **It has one now** —
built the same day rather than held for a recurrence, because the tab is a column of panels that grows by
one every time the Settings spec does, and the next thing given `flex: 1` down there would have collapsed
the same way. `settings-editor` mounts the tab in a host short enough that the column overflows it, opens
a card by clicking it, and [`layout_probe.py`](../scripts/layout_probe.py) checks [8]–[10] assert the
overflow is present *before* asserting the editor clears its toolbar plus the textarea's declared 200px
floor. **The checks were shown to fail**: with `flex: 1; min-height: 0` put back, four of the eight report
the defect exactly as it shipped — editor 2px, 0px of a 200px textarea visible, and the roomy control
595px against the cramped 2px. The reasoning is in
[`5-webapp/settings.md`](5-webapp/settings.md) § *Step 3 Is Measured Now*.

---

**An empty consultation tab, for a consultation that succeeded.** Reported 2026-09-11 from a live
session, during a three-round architecture consultation: *"did the third opinion go through? when I
look at it, it is empty."* It had gone through. The answer was complete, was returned to the calling
turn, and is the source of
[`7-future/rearchitecture-review.md`](7-future/rearchitecture-review.md) and
[AG-20](plan-ag/decisions.md#ag-20). The tab rendered nothing.

**The two are not contradictory states, and that is the defect.** A consultation's text reaches the
browser and reaches the calling turn by *different paths over the same stream*:

- the **tab** is fed by `text_delta` frames, accumulated per index in `AgyTranslator`
  ([`steps.py`](../src/aic_dc/agy/steps.py) `_absorb_*`, around the `text_delta` branch) and pushed
  per step by `ConsultantBridge._observer`'s `observe`;
- the **tool result** is fed by `AgyTranslator.response_text()`, which prefers the prose assembled in
  the `result` frame and only falls back to the accumulated deltas.

So a turn whose deltas never arrive, or whose observer is closed when they do, yields a complete
answer and an empty tab, with nothing anywhere marking the difference. `bridge.py`'s docstring for
`_observer` already names this asymmetry as the reason the observer is an object rather than a
closure — *"the turn's prose arrives in a `result` frame the pump has already absorbed"* — but it
draws the conclusion for the consultant's benefit and not for the tab's.

**And it cannot be diagnosed from the record**, because both emit guards leave none:

```python
request_id = self._turn()
if self._emit is None or request_id is None:
    return          # bridge.py, in _push's caller and in _announce
```

Two candidate causes survive the evidence — no `text_delta` frames for that turn, or
`session.active_request_id` unset when the steps arrived — and no log line, counter or event
distinguishes them. Recorded that way deliberately: *which* of the two it was is unknown, and the
unknowability is the finding.

Three facts that were checked before any of the above was written, so a future reader does not repeat
them. The transcript at `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl`
records `status: DONE` with content byte-identical to what the calling turn received. The consultation
was the third of three that day and the only empty one, so it is not a permanent breakage. And rounds
1 and 3 both contain a refused `tool_calls` step ahead of the prose while round 2 does not — the
`StaticPolicy` denial working as designed, and *not* the discriminator, since round 1 rendered.

This is the fourth entry in the pattern
[`7-future/rearchitecture-review.md`](7-future/rearchitecture-review.md) § *The recurrence worth more
than the ranking* collects: a component that was broken while nothing reported it. It is also the
realized defect that promotes consultant plumbing to rank 2 in that review — a consultation
re-implements streaming, tab lifecycle and cancellation beside the master engine's equivalents, so it
has a second rendering path that the master's tests do not cover, and it diverged.

**Fixed 2026-09-11**, in a way that does not depend on knowing which of the two causes it was, because
both were reachable and either could recur:

- **Translation is no longer conditional on there being somewhere to draw.** `observe` now calls
  `translator.translate(step)` first, unconditionally, and only then decides whether to emit. This was
  the more serious half: `AgyConsultant._run` hands frames to `observer(frame)` **instead of**
  `translator.translate(frame)` whenever a browser is attached
  ([`consultant.py`](../src/aic_dc/agy/consultant.py), the `if observer is not None` branch), so the
  observer held the only pass over the stream — and a closed emit gate was silently shortening the
  answer the model received, not just the tab.
- **A turn that streamed no text seeds its tab from its own answer.** `_seed_tab` emits one
  `streamChunk` in the shape the browser already renders, read from the same `response_text()` the
  tool result comes from, so the two cannot disagree about what was said. It is a fallback, not a
  footer: a turn whose deltas did arrive is left alone.
- **The silent guard now speaks, once per consultation.** A closed gate logs `emit=` and
  `request_id=` at warning level, which is the line that would have told us which candidate cause it
  was. Once, not per step, via a `muted` flag on the progress dict.

Three tests in `tests/test_antigravity_bridge.py` § *TestTheTabAndTheAnswerAgree*. The first two were
confirmed to fail against the bridge as it shipped — the reply is shortened, and the tab is empty; the
third passes there and exists to stop the seed being appended to a tab that already has text.

What is *not* fixed is the structural cause: the observer still carries both jobs, so a future guard
added for rendering reasons can still reach the answer. That is rank 2 of the review, and this entry is
the argument for it.

---

**A nameless, permanently-pending tool card in front of every real one, after a refresh.** Reported
2026-09-11 from a live session, with screenshots either side of a browser reload: *"before and after a
browser refresh. Extra blue LEDs sneaking in on empty cards."* Every Bash card in the reloaded
transcript had gained a card above it carrying a pulsing blue status dot, a disclosure caret, and
nothing else — no tool name, no invocation time, no input summary.

**The dot was honest; the card should not have existed.** Blue-pulsing is `status-pending`, which
[`toolStatus`](../webapp/src/chat-panel/blocks.js) returns for any tool block with no result — and
these had no result because they had no *call*. With partial streaming on, the CLI announces each
content block before sending any of it, and `TurnTranslator._stream_event`'s `content_block_start`
branch opened a `{request_id}:b{n}` block for the announcement whatever its type. For a `tool_use`
announcement that block was unreachable: a tool card's block identity is the SDK's `tool_use_id`, so
`_tool_use` writes `self._blocks[tool_use_id]` when the completed assistant message arrives and the
announcement block keeps `tool: None` for the rest of the turn. Two blocks per tool call, in arrival
order — the empty one first.

**Live it is inert, which is why it survived.** A block with no card emits no chunk, so nothing is ever
pushed for it and no connected browser can learn it exists. `rendered_blocks()` replays the whole map,
so it is exactly and only a client that reconnects mid-turn that is told about it — and
`applyReplayBlocks` faithfully drew what it was sent. Every replay test fed `AssistantMessage` objects
directly; not one replayed the streaming framing that precedes them, so the map under test never held
the extra blocks.

**Fixed 2026-09-11 at the source, and refused at the sink.**

- `content_block_start` opens no block for `tool_use` / `server_tool_use`
  ([`messages.py`](../src/aic_dc/claude_code/messages.py)). The slot it was buying arrival order for
  was one the card could never claim. Two tests in `tests/test_claude_code_messages.py` §
  `TestReplay`: one asserting a streamed tool call replays as one block, one asserting the text around
  it keeps its own slot, since skipping an index must not renumber what follows.
- `applyReplayBlocks` drops a replayed tool block that carries no card at all. Nothing can be drawn
  from one — the same reasoning `applyToolResult` already applies to a headless result, and an empty
  card is worse there because the reader cannot dismiss it.

Note what the engine's own `history.py` does with the mirror-image case: a `tool_result` whose call it
never saw is dropped, *"rendered on its own: a card with no header reads as a rendering bug."* The rule
was already written down. It was applied to results read off disk and not to blocks replayed from
memory.
