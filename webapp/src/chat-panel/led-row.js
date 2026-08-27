// LED row for the chat-panel "main tab header".
//
// Renders one dot per live agent tab between the tab
// strip and the message list. Each dot reflects that
// agent's current state derived from the tab object
// (`streaming` flag plus `lastEditOutcome` written by
// the streaming pipeline) and carries a hover tooltip
// per ``specs4/5-webapp/agent-browser.md`` § Status
// LEDs.
//
// This module is purely presentational. State writes
// live in ``streaming.js`` (`onStreamComplete` and
// `onStreamChunk`); the LED row reads that state
// directly off `panel._tabs` on each render.
//
// A per-tab *mode* used to be read here too, from
// `panel._tabModes`, and appended to every tooltip as
// `(code)` / `(doc+xref)`. Those were the native
// engine's per-agent modes, written by the spawn
// protocol `a0cb83b` removed; nothing has filled the
// map since, so the segment could never render.
//
// Three exports:
//
//   - `getLedState(tab)` — pure function returning
//     'cyan' | 'green' | 'red' | 'amber' | 'idle'. Streaming wins
//     over completion state (a tab that just started a new
//     stream after a previous failure should flash
//     cyan, not stay red). Amber belongs to subagent
//     tabs alone — a stopped subagent, or one whose
//     outcome the turn never reported. Pinned in tests.
//
//   - `formatLedTooltip(agentId, state, outcome)` —
//     pure tooltip-string builder, one form per state.
//
//   - `renderLedRow(panel)` — Lit template returning
//     the row, or an empty fragment when no agent tabs
//     exist. Click handler is `onTabClick` from
//     ``tabs.js``.

import { html } from 'lit';
import { subagentLedState, subagentLedTooltip } from './subagent-tabs.js';
import { onTabClick } from './tabs.js';

/**
 * Scroll the tab strip so the button for `tabId` is
 * visible. No-op when the strip isn't rendered (single-
 * tab mode) or the button isn't found yet — the next
 * render will lay it out, but we only need to scroll
 * when the user actively jumps to a tab via its LED.
 *
 * Defers through `updateComplete` so any pending
 * activation render (which may move the active class
 * around) commits before we measure positions. The
 * tab-strip-scroll element is the horizontal scroll
 * container; `scrollIntoView` with inline:'nearest'
 * scrolls the minimum amount needed, leaving the
 * user's existing scroll position alone when the
 * target is already visible.
 */
function scrollTabIntoView(panel, tabId) {
  panel.updateComplete.then(() => {
    const root = panel.shadowRoot;
    if (!root) return;
    const btn = root.querySelector(
      `.tab-strip-scroll .tab-strip-tab[data-tab-id="${tabId}"]`,
    );
    if (!btn) return;
    // Test environments (happy-dom / older jsdom) may
    // not implement scrollIntoView. Guard so the LED
    // click handler doesn't throw an unhandled
    // rejection in those harnesses; real browsers
    // always have it.
    if (typeof btn.scrollIntoView !== 'function') return;
    btn.scrollIntoView({ inline: 'nearest', block: 'nearest' });
  });
}

/**
 * Derive the LED state for one agent tab.
 *
 * Streaming → cyan (flashing). Resting state reads
 * `lastEditOutcome.status`: clean → green, error →
 * red. A tab that has neither streamed nor completed
 * is **idle**, and draws the dot's own grey.
 *
 * That fallback used to be cyan, on the reasoning that a
 * tab only existed because `agentsSpawned` had just
 * created it for a stream already in flight. `a0cb83b`
 * removed that protocol, and what is left hitting this
 * branch is Main on a freshly loaded page — which had
 * never streamed, so every refresh showed a flashing
 * "Main: running" over an idle engine until the first
 * turn of the new page finished. Every finished turn
 * writes an outcome (`computeTurnOutcome` returns
 * `clean` even for a cancelled one), so "no outcome and
 * not streaming" now means only "nothing has run here
 * yet", which is neither running nor finished.
 *
 * A live subagent's tab is read from its own row instead: it has no
 * `lastEditOutcome` (nothing here completes *its* turn) and it needs a fourth
 * state, amber, for the outcomes that are neither success nor fault — stopped,
 * killed, or a subagent whose outcome the turn never reported. See
 * `subagentLedState` in subagent-tabs.js, and specs5/5-webapp/subagent-
 * browser.md § Status LEDs.
 *
 * Pure function. Caller is responsible for handling
 * `null` / `undefined` tab argument.
 */
export function getLedState(tab) {
  if (tab.subagent) return subagentLedState(tab.subagent);
  if (tab.streaming) return 'cyan';
  const outcome = tab.lastEditOutcome;
  if (outcome && outcome.status === 'error') return 'red';
  if (outcome && outcome.status === 'clean') return 'green';
  return 'idle';
}

/**
 * Build the LED hover tooltip string per spec.
 *
 * Four forms based on state:
 *
 *   cyan   → `<id>: running`
 *   green  → `<id>: completed (N edits applied)`
 *   red    → `<id>: <diagnostic>`
 *   idle   → `<id>: idle`
 *
 * `idle` is named rather than left to fall through to the
 * red branch, which would have reported a freshly loaded
 * page's Main tab as "failed".
 *
 * A `mode` argument sat between the id and the state, and
 * appended `(code)` / `(doc+xref)` to the prefix. It came
 * from the spawn protocol's per-agent modes and had no
 * source left after `a0cb83b`, so every caller passed the
 * empty string and the segment never drew.
 *
 * Pure function for testability.
 */
export function formatLedTooltip(agentId, state, outcome) {
  const prefix = `${agentId}`;
  if (state === 'cyan') {
    return `${prefix}: running`;
  }
  if (state === 'idle') {
    return `${prefix}: idle`;
  }
  if (state === 'green') {
    const n = outcome?.appliedCount ?? 0;
    const edits = n === 1 ? '1 edit applied' : `${n} edits applied`;
    return `${prefix}: completed (${edits})`;
  }
  // red
  const diag = outcome?.failureReason || 'failed';
  return `${prefix}: ${diag}`;
}

/**
 * Render the LED row.
 *
 * Always carries one dot for main plus one per agent
 * tab (in tab insertion order, mirroring the strip).
 * The row is permanent — even a fresh panel with no
 * agents shows the main-tab LED so users can see at a
 * glance whether the main thread is streaming, clean,
 * or errored. Click delegates to `onTabClick` so the
 * LED is a second entry point for tab activation.
 *
 * Wrapping: the row uses flex-wrap so 8+ agents flow
 * onto a second line rather than truncating, per spec.
 * Each dot has a fixed small footprint; no overflow
 * indicator and no cap.
 */
export function renderLedRow(panel) {
  // Always include the main tab. Agents follow in
  // tab insertion order. The row is permanent — even
  // a fresh panel with no agents shows a single dot
  // for main so users can see at a glance whether
  // the main thread is streaming, clean, or errored.
  const tabs = Array.from(panel._tabs.keys());
  if (tabs.length === 0) {
    return html``;
  }
  return html`
    <div
      class="led-strip"
      role="group"
      aria-label="Conversation status"
    >
      ${tabs.map((tabId) => {
        const tab = panel._tabs.get(tabId);
        if (!tab) return html``;
        const isMain = tabId === 'main';
        const state = getLedState(tab);
        // Tooltip uses a friendlier label for main
        // ("Main") instead of the raw tab id, since
        // the literal string "main" is an internal
        // identifier rather than user-facing copy.
        const label = isMain ? 'Main' : tabId;
        // A subagent's dot names what it was asked to do and what it did with
        // it — the running tool, the tokens it spent, or why it has no
        // outcome. An SDK agent id in a tooltip tells the reader nothing.
        const tooltip = tab.subagent
          ? subagentLedTooltip(tab.subagent, state)
          : formatLedTooltip(label, state, tab.lastEditOutcome);
        const active = tabId === panel._activeTabId;
        const classes = [
          'led-dot',
          `led-${state}`,
          isMain ? 'led-main' : '',
          active ? 'active' : '',
        ].filter(Boolean).join(' ');
        return html`
          <button
            class=${classes}
            data-led-tab-id=${tabId}
            data-led-state=${state}
            title=${tooltip}
            aria-label=${tooltip}
            @click=${() => {
              onTabClick(panel, tabId);
              scrollTabIntoView(panel, tabId);
            }}
          ></button>
        `;
      })}
    </div>
  `;
}