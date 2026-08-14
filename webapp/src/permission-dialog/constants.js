// Permission dialog constants.
//
// Governing spec: specs5/5-webapp/permission-dialog.md.
// Payload shapes and the server-side numbers are in
// specs-reference/3-engine/permissions.md and mirrored by
// src/ac_dc/claude_code/permissions.py.

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
export const CHIME_SETTING_KEY = 'ac-dc.permission-chime';

/** Tool-class glyphs for the header. Text, never colour alone. */
export const CLASS_GLYPHS = {
  write: '✎',
  exec: '›_',
  read: '◎',
  delegate: '⚇',
  interact: '?',
  mcp: '⚙',
};

/** Human labels for the tool classes, for the header and announcements. */
export const CLASS_LABELS = {
  write: 'edit',
  exec: 'shell command',
  read: 'read',
  delegate: 'subagent',
  interact: 'question',
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
  mcp: 'Do not use this tool.',
};

/** The reason Escape sends. Named in the spec, so it is a constant. */
export const ESCAPE_DENY_REASON = 'dismissed by the user';

/**
 * The `action` values on a `permissionResolved` broadcast that mean the
 * call went ahead. Mirrors `ALLOW_ACTIONS` in
 * src/ac_dc/claude_code/permissions.py.
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
  + 'AC-DC.';

export const ALWAYS_ALLOW_SESSION_TOOLTIP =
  'Holds for the rest of this session only. Nothing is written to a '
  + 'settings file, and the grant is gone when the engine restarts — so '
  + 'there is nothing to revoke afterwards, and nothing to find later '
  + 'either.';

/** Destination file for each rule destination the CLI names. */
export const DESTINATION_FILES = {
  projectSettings: '.claude/settings.json',
  localSettings: '.claude/settings.local.json',
  userSettings: '~/.claude/settings.json',
  session: '(this session only)',
};
