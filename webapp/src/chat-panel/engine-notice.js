// Which engine is running, and what it has not finished.
//
// Governing spec: specs5/5-webapp/chat.md § Engine Indicator and Notice;
// specs5/plan-ag/decisions.md AG-1, AG-3, AG-9, AG-R-4.
//
// Why this exists
// ---------------
// AG-9 hides a surface the running engine cannot feed rather than drawing
// an empty one, and that is right: a Context tab showing a 0% bar for an
// engine with no context window has replaced an absence with a
// measurement. What it did not account for is *twelve* of them at once.
//
// Found 2026-09-03, by a user reporting that "past work on plan-ag has
// made the UI unusable" — the history browser gone, the always-allow
// button gone, no slash commands, no cost. Every one of those was AG-9
// working exactly as designed: `app.json` named Antigravity as master, and
// the descriptor hid every surface phase 5 has not built. Nothing was
// broken. Nothing said so either. The only place in the application that
// named the running engine was a selector inside the Settings tab, and
// finding it required already suspecting the engine — which is precisely
// what a user cannot do, because the symptom of a half-built master is
// indistinguishable from the symptom of a broken build.
//
// So hiding stays, and it gains the thing that makes it legible: the
// application says which engine it is on, and says what that engine has
// not built yet.
//
// The distinction that decides whether to speak
// ---------------------------------------------
// `absent` and `unbuilt` are hidden identically and are not the same fact,
// and this is the one reader that cares (see `surfacesWithStatus`).
//
//   - `absent` — the engine has no source data and never will. A UI
//     without it is complete. Antigravity has no USD figure; saying so
//     would be an apology in the shape of a panel, which is the failure
//     AG-9 is written against.
//   - `unbuilt` — this project built the feature and has not reached this
//     engine with it. A UI without it is *unfinished*, and a user staring
//     at the gap is owed the difference between "this engine does not work
//     that way" and "nobody has written this yet".
//
// Only `unbuilt` opens the notice. On Claude that count is zero, so the
// shipped engine never sees this strip — which is the test that it is
// keyed to the right fact.
//
// What this does not do (AG-R-4)
// ------------------------------
// It does not branch on an engine name. *Whether* to speak is decided
// entirely from the descriptor, which carries no engine identity; the name
// is read from `list_engines`, rendered as text, and passed back to
// `switch_engine` as a choice. That is the carve-out `list_engines`
// documents — a human choosing an engine is not a component deciding
// whether to draw a bar — and the two are kept apart here on purpose: swap
// Antigravity for a third engine tomorrow and this file needs no edit.

import { html, nothing } from 'lit';

import { surfacesWithStatus } from '../engine-capabilities.js';

/**
 * The engines this session has, or null before `list_engines` answers.
 *
 * A helper rather than an inline guard because three renderers ask, and a
 * malformed answer must read as "not known yet" rather than as one engine.
 */
function engines(panel) {
  const value = panel._engines;
  if (!value || typeof value !== 'object') return null;
  if (!Array.isArray(value.mountable)) return null;
  return value;
}

/**
 * The engine names this session could switch to, active one excluded.
 *
 * Sorted by `list_engines` already; kept in that order rather than
 * re-sorted, so the server owns the ordering the same way it owns the
 * names.
 */
export function alternateEngines(panel) {
  const value = engines(panel);
  if (!value) return [];
  return value.mountable.filter((name) => name !== value.active);
}

/**
 * Surfaces this project has built and this engine cannot yet reach.
 *
 * @returns {Array<{key: string, title: string, note: string}>}
 */
export function unbuiltSurfaces() {
  return surfacesWithStatus('unbuilt');
}

/**
 * Whether the running engine is missing features rather than differing.
 *
 * The whole condition, in one place, so the chip's emphasis and the
 * notice's existence cannot disagree about it.
 */
export function engineIsUnfinished() {
  return unbuiltSurfaces().length > 0;
}

/**
 * The identity of what the notice currently says.
 *
 * Dismissal is per engine *and* per gap set, matching `healthKey`: a user
 * who has read what Antigravity has not built should not be told again
 * every render, and should be told again after switching to an engine with
 * a different set of gaps.
 */
export function engineNoticeKey(panel) {
  const value = engines(panel);
  return JSON.stringify([
    value?.active || '',
    unbuiltSurfaces().map((s) => s.key),
  ]);
}

/** Open the notice, whatever it has to say. Called by the chip. */
export function revealEngineNotice(panel) {
  panel._engineNoticeDismissed = null;
  panel._engineNoticeForced = true;
}

/** Hide it until the engine or its gaps change. */
export function dismissEngineNotice(panel) {
  panel._engineNoticeForced = false;
  panel._engineNoticeDismissed = engineNoticeKey(panel);
}

/**
 * A capitalised engine name, for prose.
 *
 * The server sends `claude` and `antigravity` as identifiers, which is
 * right on the wire and wrong in a sentence. Capitalising here rather than
 * shipping a display-name map keeps the browser from owning a second name
 * for something the server already named — a map would have to be edited
 * in two places to add an engine, and the one that was forgotten would
 * render a blank.
 */
function engineLabel(name) {
  if (typeof name !== 'string' || !name) return 'an unknown engine';
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/**
 * The persistent indicator: which engine is answering.
 *
 * Rendered **only when more than one engine is mountable**, and that rule
 * is the point rather than tidiness. With one engine there is no question
 * to answer and a permanent "Claude" chip is furniture; with two, "which
 * engine am I talking to" becomes a real question that the application
 * previously answered nowhere the user would look.
 *
 * A button rather than a label, because the indicator and the explanation
 * should not be two separate things to find: clicking it opens the notice,
 * the same way the turn footer's mirror-gap marker forces the health
 * banner open. Emphasised when the engine has unbuilt surfaces, so the
 * chip carries the warning even after the notice has been dismissed —
 * which is what makes dismissing it safe.
 */
export function renderEngineChip(panel) {
  const value = engines(panel);
  if (!value || value.mountable.length < 2) return nothing;
  const unfinished = engineIsUnfinished();
  const label = engineLabel(value.active);
  const title = unfinished
    ? `Running on ${label}, which has features this build has not finished. `
      + 'Click for what is missing.'
    : `Running on ${label}. Click to change engine.`;
  return html`
    <button
      class="action-button engine-chip ${unfinished ? 'engine-unfinished' : ''}"
      @click=${() => revealEngineNotice(panel)}
      aria-label=${title}
      title=${title}
    >
      ${unfinished ? '⚠' : '⚙'} ${label}
    </button>
  `;
}

/**
 * The notice: what this engine has not built, and the way back.
 *
 * Sits beside the health banner, between the transcript and the input, for
 * the reason that banner gives — both are standing conditions about the
 * channel rather than events in the conversation.
 *
 * Lists the surfaces by their descriptor titles rather than summarising
 * them. A count ("5 features unavailable") would be the same non-answer as
 * the empty panels it is explaining; the user's question is *which* ones,
 * because that is what tells them whether the thing they were reaching for
 * is coming back.
 *
 * The switch is offered only when exactly one alternate is mountable.
 * With more than one this is a choice rather than a recovery, and the
 * Settings tab's selector is where a choice belongs — offering the first
 * of three here would be picking for the user and calling it a fix.
 */
export function renderEngineNotice(panel) {
  const value = engines(panel);
  if (!value) return nothing;
  const forced = !!panel._engineNoticeForced;
  const unfinished = engineIsUnfinished();
  if (!forced && !unfinished) return nothing;
  if (!forced && engineNoticeKey(panel) === panel._engineNoticeDismissed) {
    return nothing;
  }

  const label = engineLabel(value.active);
  const missing = unbuiltSurfaces();
  const alternates = alternateEngines(panel);
  const pending = !!panel._engineSwitchPending;

  return html`
    <div class="health-banner ${unfinished ? '' : 'health-banner-ok'}"
         role="status">
      <div class="health-lines">
        <div class="health-line">
          <span class="health-label">Engine</span>
          This session is running on ${label}.
        </div>
        ${missing.length
          ? html`
              <div class="health-line">
                These are built in AIC⚡DC and not yet wired to ${label}, so
                their controls are hidden rather than broken:
                <ul class="engine-missing">
                  ${missing.map(
                    (surface) => html`<li>${surface.title}</li>`,
                  )}
                </ul>
              </div>
            `
          : html`
              <div class="health-line health-ok">
                Nothing this build offers is missing on ${label}.
              </div>
            `}
        ${alternates.length === 1
          ? html`
              <div class="health-line">
                <button
                  class="engine-switch"
                  ?disabled=${pending}
                  @click=${() => panel._onSwitchEngine(alternates[0])}
                >
                  ${pending
                    ? 'Switching…'
                    : `Switch to ${engineLabel(alternates[0])}`}
                </button>
                <span class="engine-switch-note">
                  Starts a new session — the two engines' transcripts do not
                  translate. Nothing is deleted, and this conversation stays
                  loadable.
                </span>
              </div>
            `
          : nothing}
        ${panel._engineSwitchError
          ? html`
              <div class="health-line health-degraded">
                <span class="health-label">Switch failed</span>
                ${panel._engineSwitchError}
              </div>
            `
          : nothing}
        <div class="health-line health-engine">
          Which engine a session starts on is app.json's
          <code>engines.master</code>, and the Settings tab can change it
          mid-run.
        </div>
      </div>
      <button
        class="health-dismiss"
        @click=${() => dismissEngineNotice(panel)}
        aria-label="Dismiss engine notice"
        title="Dismiss — the engine chip stays in the action bar"
      >
        ✕
      </button>
    </div>
  `;
}
