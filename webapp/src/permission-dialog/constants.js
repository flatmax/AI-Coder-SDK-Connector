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
 */
export function alwaysAllowTooltip(rule) {
  if (rule?.session) return ALWAYS_ALLOW_SESSION_TOOLTIP;
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
