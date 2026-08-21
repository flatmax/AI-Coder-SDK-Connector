// Engine-health banner for the chat panel.
//
// The surface `engineHealth` was always routed to and never
// had: "No toast; the health banner owns this. A mirror gap
// is a banner, not an interruption"
// (specs5/5-webapp/chat.md § Engine Event Routing), and the
// turn footer's mirror-gap marker "links to the health
// banner" (§ Turn Footer). Until now the payload was stashed
// on `panel._engineHealth` for a reader that did not exist.
//
// What it says is what the engine reports and nothing
// derived: a count of mirror-append failures, the engine's
// last error, and the version and credential warnings
// `health.py` builds at startup. Once open it also shows the
// tail of the CLI subprocess's own stderr, which is the one
// thing here that cannot open it — see `cliStderr`.
//
// The MCP server list in the
// same payload is deliberately left out — that is phase 6's
// Context tab, which has room for a per-server status detail
// this strip does not.
//
// Four exports:
//
//   - `hasHealthProblem(health)` — is there anything to say?
//     The banner is silent otherwise, including before the
//     first turn, when `connected` is honestly false and
//     means "no engine yet" rather than "the engine died".
//
//   - `healthKey(health)` — the identity of the current
//     problem set, so a dismissal covers the thing that was
//     dismissed and not the next thing to go wrong.
//
//   - `revealHealth(panel)` / `dismissHealth(panel)` — the
//     two writes. `revealHealth` is what the footer marker
//     calls, and it forces the banner open even when there
//     is nothing wrong, because a link that lands on nothing
//     is the state this module exists to end.
//
//   - `renderHealthBanner(panel)` — the strip, or an empty
//     fragment.

import { html, nothing } from 'lit';

/** The engine's own numbers, shown under the problems. */
function engineLine(health) {
  const parts = [];
  const version = health.cli_version;
  if (typeof version === 'string' && version && version !== 'unknown') {
    parts.push(`CLI ${version}`);
  }
  const source = health.credential_source;
  if (typeof source === 'string' && source && source !== 'unknown') {
    parts.push(source);
  }
  return parts.join(' · ');
}

/** A non-empty string field, or null. */
function text(value) {
  return typeof value === 'string' && value ? value : null;
}

/**
 * Capabilities the session started without, as sentences.
 *
 * The engine writes these — a bridge that would not build, a post-write hook
 * that would not start — and the words are its, not ours. A browser that
 * turned a flag into a sentence would be a second owner of what the loss
 * means, and the two could only drift.
 */
function degradations(health) {
  const list = health.degradations;
  if (!Array.isArray(list)) return [];
  return list.map((s) => text(s)).filter(Boolean);
}

/**
 * The tail of the CLI's own stderr, as lines.
 *
 * Read like `degradations`, shown unlike it: this text is not a problem
 * report, it is whatever the subprocess printed, and the CLI prints routine
 * chatter there as well as stack traces. So it is absent from
 * `hasHealthProblem` and from `healthKey` on purpose — it can neither open
 * the banner nor undo a dismissal the user has already made, and only shows
 * once something else has opened it or the footer link forced it.
 */
function cliStderr(health) {
  const list = health.cli_stderr;
  if (!Array.isArray(list)) return [];
  return list.map((s) => text(s)).filter(Boolean);
}

/** `mirror_gaps` as a count, ignoring anything that is not one. */
function gapCount(health) {
  const gaps = health.mirror_gaps;
  return Number.isFinite(gaps) && gaps > 0 ? Math.floor(gaps) : 0;
}

/**
 * Whether the engine says the gaps have stopped being bad luck.
 *
 * Read, not computed. The tolerance lives in `app.json` and the comparison
 * lives on `EngineHealth`, so the browser is told the answer for the same
 * reason it is told the disk warning's sentence rather than the threshold:
 * a second owner of the rule could only disagree with the first.
 */
function escalated(health) {
  return health.mirror_gaps_escalated === true;
}

/**
 * Whether the payload carries something worth a banner.
 *
 * Three warnings, a count, and the capabilities the session
 * started without. Not `connected`: it is false before the
 * first prompt, which is the normal state of a freshly loaded
 * page and not a fault to report. A session that *loses* its
 * engine sets `last_error` on the way out, so the case that
 * matters still shows.
 */
export function hasHealthProblem(health) {
  if (!health || typeof health !== 'object') return false;
  return !!(
    gapCount(health)
    || text(health.last_error)
    || text(health.version_warning)
    || text(health.auth_warning)
    || degradations(health).length
  );
}

/**
 * The identity of what the banner is currently saying.
 *
 * Dismissal is per problem set, not per session: a user who
 * has read "the CLI is newer than the SDK pins" should not
 * have to be told again on every turn, and should still be
 * told when a mirror append starts failing an hour later.
 * The gap *count* is part of the key on purpose — a second
 * failed append is new information about how bad it is, and
 * so is the count crossing the configured tolerance.
 */
export function healthKey(health) {
  if (!health || typeof health !== 'object') return '';
  // JSON rather than a joined string. The parts are free text the engine
  // wrote, so any separator that can appear inside a part cannot tell
  // "a|b" in one field from "a" and "b" in two — and the separator this
  // used to reach for was a literal NUL, which made git read the whole
  // file as binary and stop diffing it. Quoting each part is unambiguous
  // and leaves the source printable.
  return JSON.stringify([
    gapCount(health),
    escalated(health) ? '!' : '',
    text(health.last_error) || '',
    text(health.version_warning) || '',
    text(health.auth_warning) || '',
    degradations(health),
  ]);
}

/**
 * Show the banner, whatever it has to say.
 *
 * The turn footer's mirror-gap marker calls this. It forces
 * rather than un-dismisses because the marker is on a turn
 * that failed to mirror and the engine may since have
 * recovered, restarted, or been asked for a clean snapshot —
 * in which case the honest banner is a readout with no
 * problems in it, and that is still an answer.
 */
export function revealHealth(panel) {
  panel._healthDismissed = null;
  panel._healthForced = true;
}

/** Hide it until something else goes wrong. */
export function dismissHealth(panel) {
  panel._healthForced = false;
  panel._healthDismissed = healthKey(panel._engineHealth);
}

/**
 * The banner, or nothing.
 *
 * Sits between the transcript and the input area, next to
 * the disconnected note, because both are standing
 * conditions about the channel rather than events in the
 * conversation. Dismissible and never blocking.
 */
export function renderHealthBanner(panel) {
  const health = panel._engineHealth;
  const forced = !!panel._healthForced;
  const problem = hasHealthProblem(health);
  if (!forced && !problem) return nothing;
  if (!forced && healthKey(health) === panel._healthDismissed) return nothing;

  const lines = [];
  if (health && typeof health === 'object') {
    const gaps = gapCount(health);
    if (gaps) {
      lines.push(html`
        <div class="health-line health-gap">
          ${gaps === 1
            ? 'One turn was not appended to the repo-local transcript.'
            : `${gaps} turns were not appended to the repo-local transcript.`}
          The conversation is intact in the engine; the local copy has a hole,
          so those turns will be missing when this session is browsed or
          resumed.
          ${escalated(health)
            ? 'That is more than this repo tolerates: treat the mirror as '
              + 'broken rather than unlucky, and check the free space and the '
              + 'permissions on .aic-dc/sessions/.'
            : nothing}
        </div>
      `);
    }
    for (const [field, label] of [
      ['last_error', 'Engine'],
      ['version_warning', 'Version'],
      ['auth_warning', 'Credentials'],
    ]) {
      const value = text(health[field]);
      if (value) {
        lines.push(html`
          <div class="health-line">
            <span class="health-label">${label}</span> ${value}
          </div>
        `);
      }
    }
    // One line per capability the session started without, in
    // the engine's words. The alternative was the agent simply
    // appearing inexplicably worse at repo-wide questions.
    for (const sentence of degradations(health)) {
      lines.push(html`
        <div class="health-line health-degraded">
          <span class="health-label">Degraded</span> ${sentence}
        </div>
      `);
    }
  }
  // Forced open with nothing wrong: say that, rather than an
  // empty strip that reads as a rendering fault.
  if (lines.length === 0) {
    lines.push(html`
      <div class="health-line health-ok">
        ${health
          ? 'The engine reports nothing wrong.'
          : 'The engine has not reported its health yet.'}
      </div>
    `);
  }
  // Appended after the summary rather than folded into it, so a banner
  // forced open on a healthy engine still says the engine is healthy and
  // shows the output underneath. The <pre> is what makes it worth having:
  // this is terminal output, and a stack trace reflowed as prose is
  // unreadable exactly when it is needed.
  const stderr = health ? cliStderr(health) : [];
  const engine = health ? engineLine(health) : '';

  // Three appearances for three answers: nothing wrong, something
  // wrong, and a mirror the engine has stopped making excuses for.
  let tone = 'health-banner-ok';
  if (problem) tone = escalated(health) ? 'health-banner-bad' : '';

  return html`
    <div class="health-banner ${tone}" role="status">
      <div class="health-lines">
        ${lines}
        ${stderr.length
          ? html`
              <div class="health-line health-stderr">
                <span class="health-label">CLI output</span>
                <pre class="health-stderr-text">${stderr.join('\n')}</pre>
              </div>
            `
          : nothing}
        ${engine
          ? html`<div class="health-line health-engine">${engine}</div>`
          : nothing}
      </div>
      <button
        class="health-dismiss"
        @click=${() => dismissHealth(panel)}
        aria-label="Dismiss engine health notice"
        title="Dismiss — it comes back if something else goes wrong"
      >
        ✕
      </button>
    </div>
  `;
}
