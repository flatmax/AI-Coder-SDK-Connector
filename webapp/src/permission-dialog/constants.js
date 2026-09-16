// Permission dialog constants.
//
// Governing spec: specs5/5-webapp/permission-dialog.md.
// Payload shapes and the server-side numbers are in
// specs-reference/3-engine/permissions.md and mirrored by
// src/aic_dc/claude_code/permissions.py.

/**
 * How long after a dialog appears before any decision control may
 * hold focus, and during which Enter/Space are swallowed.
 *
 * This is the anti-click-through mitigation that costs the attentive
 * user the least: a keystroke already in flight when the dialog opened
 * cannot approve anything, and after 700ms an intentional Enter works
 * normally. Long enough to outlast a key repeat, short enough that a
 * user who is actually reading never notices it
 * (permission-dialog.md § Anti-Click-Through).
 */
export const SETTLING_MS = 700;

/** Countdown re-render cadence. One second; the label shows seconds. */
export const COUNTDOWN_TICK_MS = 1000;

/**
 * The narrowest a docked question **that compares examples** may be, in px.
 *
 * A question is docked beside the chat rather than modal over it, because
 * its options and their examples are written *about* the transcript
 * (permission-dialog.md § Placement). The floor is the
 * `.question-compare` breakpoint in styles.js: below it the options and
 * the example they are being compared against stack, and § interact
 * requires both on screen at once. Narrower than this and docking would
 * buy the transcript at the cost of the comparison, so the centred modal
 * is the better answer there.
 *
 * Read by the shell, which decides whether there is room
 * (app-shell/dialog.js § questionDockLeft). The two numbers have to agree,
 * so the breakpoint in styles.js names this constant.
 */
export const QUESTION_DOCK_MIN_WIDTH = 720;

/**
 * The narrowest a docked question with **no examples to compare** may be.
 *
 * The floor above was applied to every question, and for most of them it was
 * enforcing a layout they never use: `.question-compare` only renders when an
 * option carries a `preview`, and a plain list of labels and descriptions is
 * perfectly legible in half that. The cost of the one number was not
 * theoretical — 720px plus gutters needs a viewport of about 1480 before the
 * *default* half-width panel leaves room, so on any panel the user had widened
 * the dock never engaged at all and the feature was invisible in practice.
 *
 * Which floor applies is the *dialog's* answer, not the shell's, because it is
 * a fact about the pending request: see `dockMinWidth` in index.js. The shell
 * asks, for the same reason it asks about modality — a copy of the rule in the
 * shell is a copy that can disagree with the layout actually rendered.
 */
export const QUESTION_DOCK_MIN_PLAIN = 420;

/**
 * Breathing room between a docked question panel and the viewport edges.
 *
 * Written into the shadow root as `--question-dock-gutter` rather than
 * duplicated in the stylesheet: the shell adds it to the `left` it sends
 * and the stylesheet uses it for the other three sides, and a gutter that
 * disagreed with itself would sit the panel off-centre in its own region.
 */
export const QUESTION_DOCK_GUTTER = 10;

/**
 * The collapse / expand toggle on a question that could not be docked.
 *
 * The dock exists because a question's options are written *about* the
 * transcript, and below the width floor above there is nowhere to put it — so
 * the fallback is the centred modal, covering the one thing the user needs to
 * read to answer. Collapsing is the modal's version of the same answer: park
 * the question at the top of the screen as its header, hand the transcript
 * back, expand when ready.
 *
 * `▾` collapses and `▴` expands, matching the chat panel's own minimize pair
 * (chat-panel/tabs.js § tab-strip-minimize, app-shell/styles.js § expand-fab)
 * — a second collapse gesture in the same app that pointed the other way
 * would be a worse control than no gesture at all.
 *
 * The labels say the request is still pending, because that is the thing a
 * collapse gesture on a *permission* dialog could plausibly be mistaken for.
 * Collapsing decides nothing, sends nothing, and stops no clock.
 */
export const COLLAPSE_GLYPH = '▾';
export const EXPAND_GLYPH = '▴';
export const COLLAPSE_QUESTION_LABEL =
  'Collapse to the header and read the chat — the question stays pending';
export const EXPAND_QUESTION_LABEL = 'Expand the question';

/** Under this many seconds remaining the countdown turns amber. */
export const AMBER_SECONDS = 60;

/** Under this many seconds remaining it turns red. */
export const RED_SECONDS = 10;

/**
 * Coarse announcement thresholds for the polite live region, in
 * seconds. A per-second live region is unusable with a screen reader
 * (permission-dialog.md § Accessibility).
 */
export const ANNOUNCE_AT_SECONDS = [300, 60, 10];

/** Prefix for the page title while requests are pending. */
export const TITLE_MARKER = '⚡';

/** Settings key for the arrival chime. Default on. */
export const CHIME_SETTING_KEY = 'aic-dc.permission-chime';

/** Tool-class glyphs for the header. Text, never colour alone. */
export const CLASS_GLYPHS = {
  write: '✎',
  exec: '›_',
  read: '◎',
  delegate: '⚇',
  interact: '?',
  plan: '☰',
  mcp: '⚙',
};

/** Human labels for the tool classes, for the header and announcements. */
export const CLASS_LABELS = {
  write: 'edit',
  exec: 'shell command',
  read: 'read',
  delegate: 'subagent',
  interact: 'question',
  plan: 'plan',
  mcp: 'MCP tool',
};

/**
 * The reason a deny field is prefilled with, by class. The user can
 * replace it; what matters is that a deny is never reasonless, because
 * a blank denial produces an agent that retries the same call.
 */
export const DEFAULT_DENY_REASONS = {
  write: 'Do not change this file.',
  exec: 'Do not run this command.',
  read: 'Do not read this path.',
  delegate: 'Do not start this subagent.',
  interact: 'Do not ask; carry on with what you have.',
  // Denying a plan is "keep planning", not "stop": the tool the user is
  // refusing is the *exit* from plan mode, so a reason that reads as a
  // refusal of the work would send the agent the wrong correction.
  plan: 'Keep planning — do not start on this yet.',
  mcp: 'Do not use this tool.',
};

/**
 * Placeholder for the freeform reply on a question.
 *
 * The terminal always offers an "Other" row alongside the options — the
 * tool's own schema tells the model "There should be no 'Other' option,
 * that will be provided automatically" — so a dialog without one is
 * missing a control the agent is counting on
 * (permission-dialog.md § interact).
 */
export const OTHER_ANSWER_PLACEHOLDER = 'Other — type your own answer';

/**
 * Placeholder for the note on an answer.
 *
 * Worded to distinguish it from the field above it, which is the harder
 * part of putting two text inputs on one question: that one is an answer
 * *instead of* the options, this one is a remark *about* whichever answer
 * was given. It reaches the model through `annotations`, not `answers`
 * (permission-dialog.md § interact).
 */
export const ANSWER_NOTE_PLACEHOLDER = 'Add a note about this choice (optional)';

/** The reason Escape sends. Named in the spec, so it is a constant. */
export const ESCAPE_DENY_REASON = 'dismissed by the user';

/**
 * The `action` values on a `permissionResolved` broadcast that mean the
 * call went ahead. Mirrors `ALLOW_ACTIONS` in
 * src/aic_dc/claude_code/permissions.py.
 *
 * Named once and imported by everything that has to tell an approval from a
 * denial, because the alternative is what it replaced: `action === 'allow'`
 * written out in two places, which rendered a tool card the user had
 * approved with "always allow" as *denied* — the amber lock and the denial
 * body, on a call that ran. Anything added to the engine's set has to be
 * added here, and that is easier to remember about one list than three
 * comparisons.
 */
export const ALLOW_ACTIONS = ['allow', 'allow_always', 'allow_mode'];

/**
 * Advisory `flags` that move default focus from Allow to Deny. The
 * flags themselves gate nothing — they are heuristics — but where the
 * *default focus* lands is exactly the right weight for a heuristic
 * (permission-dialog.md § Anti-Click-Through, point 3).
 */
export const RISKY_FLAGS = ['deletes', 'network'];

/** Tooltip copy for the advisory chips, so they cannot read as facts. */
export const FLAG_TOOLTIPS = {
  deletes: 'Heuristic: this command looks like it removes something. '
    + 'Read the command — the guess can be wrong either way.',
  writes: 'Heuristic: this command looks like it writes to disk.',
  network: 'Heuristic: this command looks like it reaches the network.',
  sudo: 'Heuristic: this command looks like it escalates privileges.',
};

/**
 * What the "always allow" tooltip must say, because both consequences
 * are otherwise discovered rather than known
 * (permission-dialog.md § Always allow shows the rule, not a promise).
 *
 * Two tooltips, because the destination decides which is true. An earlier
 * single tooltip asserted "there is no invisible session-only grant behind
 * this button", which the CLI disproves: it suggests
 * `destination: 'session'` for reads outside the working directory, and a
 * file-modification approval is session-scoped by design rather than
 * written to a settings file. The chip already shows the destination, so
 * the tooltip must agree with it instead of denying one of its values.
 */
export const ALWAYS_ALLOW_TOOLTIP =
  'Writes a rule to a settings file you can read and revoke. '
  + 'It applies to the claude CLI in this repository too, not just '
  + 'AIC-DC.';

export const ALWAYS_ALLOW_SESSION_TOOLTIP =
  'Holds for the rest of this session only. Nothing is written to a '
  + 'settings file, and the grant is gone when the engine restarts — so '
  + 'there is nothing to revoke afterwards, and nothing to find later '
  + 'either.';

/**
 * Antigravity's rules (AG-15). A third destination, and a third truth.
 *
 * The two tooltips above were written when a rule went either to a Claude
 * settings file or nowhere. Antigravity has no `updated_permissions` at any
 * layer, so AIC-DC keeps the rule itself — which makes both of the others
 * wrong: it is not session-only, and it emphatically does **not** apply to
 * the `claude` CLI, which has never heard of this file.
 *
 * Caught in a browser on 2026-09-05, not by a test: the Antigravity dialog
 * rendered the Claude tooltip, telling the user their grant reached a CLI
 * it cannot reach. A misleading sentence on a permission control is worse
 * than a missing one, because the user acts on it.
 */
export const ALWAYS_ALLOW_AIC_DC_TOOLTIP =
  'Writes a rule to a file AIC-DC keeps, which you can review and revoke '
  + 'in Settings. It applies to this repository, on this engine, for you — '
  + 'not to the claude CLI and not to anyone else.';

/**
 * The tooltip for one rule, chosen by **where the rule goes**.
 *
 * A function rather than a boolean at the call site, because the call site
 * had one — `rule.session ? A : B` — and a third destination fell through
 * to B and asserted something untrue. Destinations are added from the
 * server; the mapping belongs where the destinations are described.
 *
 * **Takes the rule as the server sent it**, and reads only `destination`.
 * The first cut also accepted a `session` boolean, which looked like
 * tolerance and was the bug: `describeRule` computes that boolean *and*
 * overwrites `destination` with the filename the chip shows, so the two
 * checks wanted two different shapes. Whichever object you passed, one of
 * them was dead — the render passed the described rule and got the Claude
 * sentence on an Antigravity grant; the tests passed a hybrid with both
 * fields set, which is the one shape that made the function look right and
 * the one shape nothing produces. One field, one shape, one caller.
 */
export function alwaysAllowTooltip(rule) {
  if (rule?.destination === 'session') return ALWAYS_ALLOW_SESSION_TOOLTIP;
  if (rule?.destination === 'aicDcRules') return ALWAYS_ALLOW_AIC_DC_TOOLTIP;
  return ALWAYS_ALLOW_TOOLTIP;
}

/**
 * What the "shared" tag on a rule row has to say (CC-16).
 *
 * `.claude/settings.json` is git-tracked, so this grant is not personal: it
 * reaches every checkout that pulls the commit, and nobody there clicked
 * anything.
 */
export const SHARED_RULE_TOOLTIP =
  'Writes the rule to the git-tracked settings file, so it applies to '
  + 'everyone who pulls it — not just you. Commit it deliberately, or pick '
  + 'the row above to keep the grant on this machine.';

/** Destination file for each rule destination the CLI names. */
export const DESTINATION_FILES = {
  projectSettings: '.claude/settings.json',
  localSettings: '.claude/settings.local.json',
  userSettings: '~/.claude/settings.json',
  session: '(this session only)',
  // Antigravity (AG-15). Not one of the CLI's settings files: that engine
  // has no `updated_permissions` at any layer, so AIC-DC keeps the rule
  // itself. Naming a `.claude/` file here would be a plain lie about where
  // the grant went — the chip renders this label beside the rule.
  //
  // This is the *only* webapp change AG-15 needed. The rule shape, the
  // `allow_always` action and the control that sends it all already
  // existed, which was the decision's own test of whether the shape had
  // been got right.
  aicDcRules: '~/.config/aic-dc/antigravity-rules.json',
};
