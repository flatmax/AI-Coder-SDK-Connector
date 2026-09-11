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
([`layout-harness.js`](../webapp/src/layout-harness.js)) has no settings scene — **worth one if this
class recurs**, because the tab is still a column of panels that grows by one every time the Settings
spec does, and the next thing given `flex: 1` down there will collapse the same way.
