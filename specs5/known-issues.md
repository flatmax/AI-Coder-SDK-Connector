# Known issues

The raw inbox — defects as they are noticed, unsorted. Triaged entries are carried into
[`next.md`](next.md) § C and leave here when they are fixed.

- **`specs-reference/5-webapp/viewers-hud.md` § Cost rendering still describes the pre-phase-6 HUD.**
  Its table is driven by `total_cost_usd` and says the field "is `null` under subscription billing",
  with a row rendering a `subscription` label and the tooltip *"This turn is billed under your plan,
  not per token."* [`5-webapp/viewers-hud.md`](5-webapp/viewers-hud.md) § *Cost Is Cumulative, and the
  HUD Reports One Turn* corrected all of that against the CLI's own wire schema — the field has no null
  branch, it is the session's running total rather than the turn's, and the HUD reads
  `turn_cost_usd` / `turn_cost_basis` instead — but the twin was not corrected with it, so the two
  files now say opposite things about the same row. Documentation only; `turn-cost.js` implements the
  corrected version and 60 tests hold it. Noticed 2026-08-28 while building § B1, from the same reading
  of the twin that found the HUD's z-index and its missing `max-height` (both fixed; see the work-log's
  § *Landed since*). The mechanical-twin rule means an implementer loads both files, so the stale one
  is not harmless.
