// Tab strip module for the ChatPanel component.
//
// Owns:
//   - Tab state lookup by request ID (routes
//     stream chunks/completions to the right tab)
//   - Tab activation, close, overflow menu
//   - Tab strip rendering
//   - Agent-tab spawning when the orchestrator's
//     response carries agent blocks
//   - Alt+` chat-tab keyboard shortcut
//
// Functions take the chat-panel instance as their
// first parameter rather than living on the
// prototype. The component class binds them as
// methods (or as event handlers) in its
// constructor — `installTabHandlers(panel)` is
// the entry point that does the binding.
//
// Why instance-as-parameter rather than mixin or
// subclass: the chat-panel module has too many
// concerns to share one prototype, and Lit's
// reactive-property machinery doesn't compose
// cleanly with multi-class inheritance. Functional
// modules with explicit `panel` parameters keep
// the dependencies visible and the call graph
// flat.

import { html } from 'lit';
import { withRpcTimeout } from '../rpc.js';
import { _AGENT_LABEL_MAX_LENGTH } from './helpers.js';
import { restoreMessage } from './restore.js';
import { makeTabState } from './state.js';
import { findSubagentTab, subagentTabTooltip } from './subagent-tabs.js';

// ---------------------------------------------------------------
// Request-ID → tab routing
// ---------------------------------------------------------------

/**
 * Find which tab owns ``requestId``, or null.
 *
 * Matching rules:
 *
 *   1. Exact match against any tab's
 *      ``currentRequestId`` — the tab that initiated
 *      the request is the primary owner.
 *   2. Prefix match against ``{parentId}-`` —
 *      parallel-agent mode spawns N child streams
 *      under a parent turn; each child's request ID
 *      is ``{parent}-agent-NN``. The chat panel's
 *      tab strip carries one tab per agent, each
 *      with its own request ID, so this prefix
 *      match is rarely hit in practice — but
 *      keeping it wired in means future spawn paths
 *      don't have to re-touch streaming routing.
 *
 * Returns the tab ID or null when no tab claims
 * the request. Collaboration broadcasts (a remote
 * user's stream reaching our panel) also return
 * null.
 */
export function findTabForRequest(panel, requestId) {
  if (!requestId) return null;
  // Fast path — exact match against the active tab.
  const active = panel._tabs.get(panel._activeTabId);
  if (active && active.currentRequestId === requestId) {
    return panel._activeTabId;
  }
  // General scan. Two passes so exact matches on any
  // tab win over prefix matches — a request ID that
  // exactly equals one tab's ID shouldn't be treated
  // as a child of another tab whose ID happens to be
  // a prefix.
  for (const [tabId, tab] of panel._tabs) {
    if (tab.currentRequestId === requestId) {
      return tabId;
    }
  }
  for (const [tabId, tab] of panel._tabs) {
    const parentId = tab.currentRequestId;
    if (parentId && requestId.startsWith(`${parentId}-`)) {
      return tabId;
    }
  }
  return null;
}

// ---------------------------------------------------------------
// Tab activation, close, overflow menu
// ---------------------------------------------------------------

/**
 * Handle a tab button click. Flips ``_activeTabId``,
 * which fires the ``active-tab-changed`` event so
 * the files-tab picker swaps its selection state to
 * match the newly-active tab.
 *
 * Same-tab clicks are no-ops because the setter
 * short-circuits on equal values.
 */
export function onTabClick(panel, tabId) {
  if (typeof tabId !== 'string' || !tabId) return;
  panel._activeTabId = tabId;
}

/**
 * Click on a tab's inline 📊 context icon. Two effects:
 *
 *   1. Activate the tab (so the chat panel shows that
 *      agent's transcript). Cheap when the tab is
 *      already active — the setter short-circuits on
 *      equal values.
 *   2. Switch the dialog tab to Context via a bubbling
 *      ``request-dialog-tab`` event the shell catches.
 *      The Context tab listens for ``active-tab-changed``
 *      independently, so the rescope happens through
 *      that channel without a coupling here.
 *
 * Both effects fire on every click, even when the
 * target tab is already active — re-clicking the icon
 * on an already-active tab is a "show me Context for
 * this conversation" gesture and should re-open the
 * overlay if the user previously navigated away.
 */
export function onTabContextClick(panel, tabId) {
  if (typeof tabId !== 'string' || !tabId) return;
  panel._activeTabId = tabId;
  panel.dispatchEvent(
    new CustomEvent('request-dialog-tab', {
      detail: { tab: 'context' },
      bubbles: true,
      composed: true,
    }),
  );
}

/**
 * Toggle the overflow menu open/closed. Attaches
 * capture-phase document listeners for outside-click
 * and Escape dismissal when opening; detaches them
 * when closing.
 */
export function toggleOverflowMenu(panel) {
  if (panel._tabStripOverflowOpen) {
    closeOverflowMenu(panel);
  } else {
    openOverflowMenu(panel);
  }
}

export function openOverflowMenu(panel) {
  if (panel._tabStripOverflowOpen) return;
  panel._tabStripOverflowOpen = true;
  // Capture phase so we see the event before any
  // child handler stops propagation.
  document.addEventListener(
    'click',
    panel._onOverflowOutsideClick,
    true,
  );
  document.addEventListener(
    'keydown',
    panel._onOverflowKeyDown,
    true,
  );
}

export function closeOverflowMenu(panel) {
  if (!panel._tabStripOverflowOpen) return;
  panel._tabStripOverflowOpen = false;
  document.removeEventListener(
    'click',
    panel._onOverflowOutsideClick,
    true,
  );
  document.removeEventListener(
    'keydown',
    panel._onOverflowKeyDown,
    true,
  );
}

/**
 * Document-level click listener (capture phase)
 * installed while the overflow menu is open. Walks
 * ``composedPath()`` to see if the click originated
 * inside the menu or its toggle button — if yes,
 * let it through. Otherwise close the menu.
 */
export function onOverflowOutsideClick(panel, event) {
  const path = event.composedPath ? event.composedPath() : [];
  const hit = path.some(
    (el) =>
      el instanceof Element &&
      (el.classList?.contains('tab-strip-overflow') ||
        el.classList?.contains('tab-strip-overflow-menu')),
  );
  if (!hit) closeOverflowMenu(panel);
}

/**
 * Document-level keydown listener (capture phase).
 * Escape closes the menu and stops propagation so
 * the textarea's own Escape handler doesn't also
 * fire.
 */
export function onOverflowKeyDown(panel, event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    closeOverflowMenu(panel);
  }
}

/**
 * Handle an overflow menu item click — jumps to the
 * target tab and closes the menu.
 */
export function onOverflowItemClick(panel, tabId) {
  closeOverflowMenu(panel);
  onTabClick(panel, tabId);
}

/**
 * Document-level keyboard handler for chat-tab
 * cycling. Alt+` moves to the next tab; Alt+Shift+`
 * moves to the previous. Wraps at both ends.
 *
 * Gated on:
 *   - ``event.altKey`` without Ctrl/Meta
 *   - ``event.key === '\`'``
 *   - ``_tabs.size > 1`` — single-tab mode has nothing
 *     to cycle through.
 *
 * Does NOT fire on Alt+1..9 — those belong to
 * app-shell's dialog-tab shortcuts.
 */
export function onChatTabShortcut(panel, event) {
  if (!event.altKey) return;
  if (event.ctrlKey || event.metaKey) return;
  if (event.key !== '`') return;
  if (panel._tabs.size <= 1) return;
  event.preventDefault();
  const tabIds = Array.from(panel._tabs.keys());
  const currentIdx = tabIds.indexOf(panel._activeTabId);
  if (currentIdx < 0) return;
  const delta = event.shiftKey ? -1 : 1;
  // Modular arithmetic with positive modulo — JS's
  // ``%`` returns negative values for negative
  // dividends, so add the length before taking the
  // modulo to force a positive result.
  const nextIdx =
    (currentIdx + delta + tabIds.length) % tabIds.length;
  panel._activeTabId = tabIds[nextIdx];
}

// `onTabClose` stood here, with the `close-tab` event it dispatched and
// the `LLMService.close_agent_context` call it fired to free the agent's
// server-side scope. It was the primitive the `agent-closed` receiver
// invoked once per dismissed agent when `new_session` fanned out, and no
// UI gesture ever bound to it — the writable agent tabs it closed were
// session-scoped.
//
// Both kinds of tab that remain sweep themselves: `clearHistoricalTabs`
// below drops browsed transcripts on a fresh load or session change, and
// `subagent-tabs.js` retires a live subagent's tab with the turn that
// spawned it. Neither has a backend scope to release — a subagent tab is
// a view onto block records the parent turn already streamed.

// ---------------------------------------------------------------
// Tab strip rendering
// ---------------------------------------------------------------

/**
 * Render the tab strip. Always rendered — even with
 * just the Main tab — because the strip carries the
 * per-tab 📊 Context icon, which is the only path
 * to the Context overlay now that the dialog-level
 * icon has been removed. With the strip projected
 * into the dialog header (via negative-top
 * positioning), the single-Main-tab case takes no
 * extra vertical real estate.
 */
export function renderTabStrip(panel) {
  // Iteration order of a Map is insertion order, so
  // 'main' comes first and agent tabs follow in
  // spawn order.
  const tabs = Array.from(panel._tabs.keys());
  return html`
    <div class="tab-strip" data-drag-handle="true">
      <div class="tab-strip-scroll" role="tablist">
        ${tabs.map((tabId) => {
          const label = panel._tabLabels.get(tabId) || tabId;
          const active = tabId === panel._activeTabId;
          // Streaming indicator — read the tab's own
          // streaming flag directly from the Map
          // rather than through the active-tab
          // getters. Shown regardless of active state
          // so users see work happening on tabs they
          // aren't currently looking at.
          const tab = panel._tabs.get(tabId);
          const streaming = !!(tab && tab.streaming);
          const readOnly = !!(tab && tab.readOnly);
          // A live subagent's feed, as opposed to an archived transcript
          // read off disk. Both are read-only; only this one can still be
          // stopped, and only this one is a stream rather than a file.
          const subagent = tab ? tab.subagent : null;
          // The tooltip is the label, plus a hint on a subagent
          // transcript that it is read-only. It used to carry the agent's
          // *mode* as well — `(code)`, `(doc+xref)` — so that two agents
          // tasked with similar prose could be told apart. Those were the
          // spawn protocol's per-agent modes; `a0cb83b` took the writer
          // with the protocol, so `_tabModes` was permanently empty and the
          // segment never drew.
          const baseTooltip = label;
          let tooltip = baseTooltip;
          if (subagent) {
            // Not built from the label: this label is an ordinal and a keyword
            // by design, and the sentence it dropped is what a tooltip is for
            // (subagent-tabs.js § Labels).
            tooltip = subagentTabTooltip(tab);
          } else if (readOnly) {
            tooltip = `${baseTooltip} — subagent transcript (read-only)`;
          }
          const cls = [
            'tab-strip-tab',
            active ? 'active' : '',
            readOnly ? 'read-only' : '',
          ].filter(Boolean).join(' ');
          // No per-tab ✕ close affordance. A subagent tab is retired
          // with the turn that spawned it and a browsed transcript by
          // the next load or session change — neither is the user's to
          // dismiss one at a time, and a ✕ on a still-streaming
          // subagent would read as "stop it", which the ⏹ below is
          // for. See specs5/5-webapp/subagent-browser.md § Tab
          // Lifetime.
          return html`
            <button
              class=${cls}
              role="tab"
              aria-selected=${active}
              aria-busy=${streaming}
              data-tab-id=${tabId}
              @click=${() => onTabClick(panel, tabId)}
              title=${tooltip}
            >${subagent
              ? renderSubagentTabStop(panel, tabId, tab, streaming)
              : html`<span
              class="tab-context"
              role="button"
              tabindex="0"
              aria-label="Open Context for ${label}"
              title="View this conversation's context window and turn cost"
              @click=${(e) => {
                e.stopPropagation();
                onTabContextClick(panel, tabId);
              }}
              @keydown=${(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  e.stopPropagation();
                  onTabContextClick(panel, tabId);
                }
              }}
            >📊</span>`}${streaming
              ? html`<span
                  class="tab-streaming-indicator"
                  aria-hidden="true"
                ></span>`
              : ''}${label}</button>
          `;
        })}
      </div>
      <button
        class="tab-strip-overflow"
        aria-label="Tab list"
        aria-haspopup="menu"
        aria-expanded=${panel._tabStripOverflowOpen}
        title="Jump to tab"
        @click=${() => toggleOverflowMenu(panel)}
      >⋯</button>
      <button
        class="tab-strip-minimize"
        aria-label="Minimize dialog"
        title="Minimize dialog"
        @click=${() => onMinimizeClick(panel)}
      >▾</button>
      ${panel._tabStripOverflowOpen
        ? renderOverflowMenu(panel, tabs)
        : ''}
    </div>
  `;
}

/**
 * The ⏹ Stop affordance on a live subagent's tab.
 *
 * Stop is the *only* write gesture a subagent tab carries, and it takes the
 * place of the 📊 Context icon rather than sitting beside it. Two things the
 * spec asks for and one it forbids:
 *
 *   - Live tabs only. A settled feed is an archived transcript, and a Stop
 *     button on it would offer to end something already over.
 *   - Confirmation first, because there is no undo: a stopped subagent cannot
 *     be resumed and its half-finished work stays half-finished.
 *   - No 📊 and no ✕. A subagent has no context window of its own to show
 *     (its tokens are the parent turn's), and its tab is not the user's to
 *     close — it leaves when the turn does
 *     (specs5/5-webapp/subagent-browser.md § Tab Strip).
 *
 * `window.confirm` rather than a custom dialog, matching the other
 * irreversible gestures in the panel (`permission-mode.js`, `git-actions.js`).
 *
 * Rendered empty for a live subagent with no task id: `stop_task` takes the
 * task id and nothing else, so there would be nothing to send.
 */
function renderSubagentTabStop(panel, tabId, tab, streaming) {
  const taskId = tab?.subagent?.task_id;
  if (!streaming || typeof taskId !== 'string' || !taskId) return '';
  // The asked-for description, not the live activity string: a confirmation
  // naming what the subagent is doing *this second* asks the user to authorise
  // something other than what they are ending.
  const desc = tab.subagent.labelDescription
    || tab.subagent.description
    || tab.subagent.subagent_type
    || 'this subagent';
  const stop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Stop ${desc}? It cannot be resumed.`)) return;
    panel._stopSubagent?.(tab.subagent);
  };
  return html`<span
    class="tab-stop"
    role="button"
    tabindex="0"
    aria-label="Stop ${desc}"
    title="Stop this subagent"
    @click=${stop}
    @keydown=${(e) => {
      if (e.key === 'Enter' || e.key === ' ') stop(e);
    }}
  >⏹</span>`;
}

/**
 * Click handler for the tab-strip minimize button.
 * Dispatches `request-dialog-minimize` which the
 * app-shell's window listener routes through its
 * existing `_toggleMinimize` method.
 *
 * Lives in the tab strip (not on the dialog as a
 * FAB) because the strip is the always-present
 * top-of-dialog region and pairs naturally with
 * the overflow menu — both are dialog-level
 * controls that don't belong inside any single
 * tab's body.
 */
function onMinimizeClick(panel) {
  panel.dispatchEvent(
    new CustomEvent('request-dialog-minimize', {
      bubbles: true,
      composed: true,
    }),
  );
}

/**
 * Render the overflow dropdown menu contents. One
 * item per tab, labelled the same way as the strip
 * button. Clicking jumps directly.
 */
export function renderOverflowMenu(panel, tabs) {
  return html`
    <div class="tab-strip-overflow-menu" role="menu">
      ${tabs.map((tabId) => {
        const label = panel._tabLabels.get(tabId) || tabId;
        const active = tabId === panel._activeTabId;
        const tab = panel._tabs.get(tabId);
        const streaming = !!(tab && tab.streaming);
        const tooltip = label;
        return html`
          <button
            class="tab-strip-overflow-item ${active ? 'active' : ''}"
            role="menuitem"
            data-tab-id=${tabId}
            title=${tooltip}
            aria-busy=${streaming}
            @click=${() => onOverflowItemClick(panel, tabId)}
          >${streaming
            ? html`<span
                class="tab-streaming-indicator"
                aria-hidden="true"
              ></span>`
            : ''}${label}</button>
        `;
      })}
    </div>
  `;
}

// ---------------------------------------------------------------
// Agent tab spawning
// ---------------------------------------------------------------

// `spawnAgentTabs`, `rehydrateAgentTabs` and
// `deriveAgentTabLabelFromEntry` stood here — the writable agent
// tab strip, filled from a `🟧🟧🟧 AGENT` spawn block's
// `{id, task, agent_idx}` and re-filled after a reconnect from
// `list_live_agents()`.
//
// A spawned tab was writable because it addressed a sibling
// conversation with its own `ContextManager`, reachable on a
// child request id (`{parent}-agent-{NN}`). Claude Code's
// subagents are internal to a turn: the agent calls `Task`, the
// output is attributed to that call's `tool_use_id`, and there is
// nothing to send a message to. `subagent-tabs.js` builds those
// tabs by mirroring the parent's block records, which is why this
// producer was never repointed at it.

// ---------------------------------------------------------------
// Handler binding
// ---------------------------------------------------------------

/**
 * Bind tab-related handlers onto the chat-panel
 * instance. Called once at construction time.
 *
 * The component class accesses these via
 * ``panel._onOverflowOutsideClick``, etc. — they
 * need to be stable function references for
 * add/removeEventListener pairs to match.
 *
 * Bound at the panel level (not module-scoped)
 * because the listeners need ``panel`` in their
 * closure.
 */
export function installTabHandlers(panel) {
  panel._onOverflowOutsideClick = (event) =>
    onOverflowOutsideClick(panel, event);
  panel._onOverflowKeyDown = (event) =>
    onOverflowKeyDown(panel, event);
  panel._onChatTabShortcut = (event) =>
    onChatTabShortcut(panel, event);
  panel._onViewSubagentsRequested = (event) =>
    onViewSubagentsRequested(panel, event);
}

// ---------------------------------------------------------------
// Historical subagent transcripts
// ---------------------------------------------------------------

/**
 * Deadline for one ``get_subagent_transcript`` call. Same 30s as the other
 * history reads, for the same reason: this is disk I/O on the server's
 * executor, so reaching the deadline means no reply is coming rather than
 * that the read is slow. Without it a dropped reply is a tab that never
 * fills and a strip the user cannot clear.
 */
const SUBAGENT_TIMEOUT_MS = 30000;

/**
 * Tab-id prefix for historical (read-only) subagent tabs read off disk.
 *
 * Live subagent tabs are keyed by the SDK ``agent_id`` verbatim, so the
 * prefix is what keeps a transcript read from the archive from colliding
 * with the same subagent's live tab — and what
 * :func:`clearHistoricalTabs` matches on when the strip has to drop back
 * to Main. Per ``specs5/5-webapp/subagent-browser.md`` § Tab Lifetime.
 */
const _HISTORICAL_TAB_PREFIX = 'historical:';

/**
 * Build the tab ID for a subagent transcript read off disk.
 *
 * The agent id alone: SDK agent ids are unique within a session, so a turn
 * segment would only encode which turn the user clicked through from —
 * which a browsed transcript cannot supply anyway, because the transcript
 * does not attribute a subagent to a turn.
 */
function _historicalTabId(agentId) {
  return `${_HISTORICAL_TAB_PREFIX}${agentId}`;
}

/**
 * Check whether a tab id names a subagent transcript read off disk.
 *
 * The strip's read-only styling and the send gate both key off the tab
 * state's own ``readOnly`` flag instead, since that is what they have in
 * hand; this is for the id-keyed work — deciding what
 * :func:`clearHistoricalTabs` sweeps.
 */
function isHistoricalTab(tabId) {
  return (
    typeof tabId === 'string' &&
    tabId.startsWith(_HISTORICAL_TAB_PREFIX)
  );
}

/**
 * Clear every historical tab from the strip.
 *
 * Called before each fresh load, so the strip does not accumulate
 * transcripts across clicks, and on a session change, because a subagent
 * transcript belongs to the session that spawned it.
 *
 * The generation bump is what stops a load still in flight: an
 * ``await`` on a transcript read spans user actions, and the tab it was
 * going to add belongs to a strip that has since been cleared.
 *
 * If the active tab was historical, switches back to Main before deletion —
 * the active-tab setter would otherwise be left pointing at a missing key.
 */
export function clearHistoricalTabs(panel) {
  panel._historicalTabGeneration =
    (panel._historicalTabGeneration || 0) + 1;
  const historical = [];
  for (const tabId of panel._tabs.keys()) {
    if (isHistoricalTab(tabId)) {
      historical.push(tabId);
    }
  }
  if (historical.length === 0) return;
  if (isHistoricalTab(panel._activeTabId)) {
    panel._activeTabId = 'main';
  }
  for (const tabId of historical) {
    panel._tabs.delete(tabId);
    panel._tabLabels.delete(tabId);
  }
}

/**
 * The strip label for one subagent transcript.
 *
 * 📜 because the strip has no live indicator to distinguish an archived
 * transcript from a running one, and hover text is not a distinction a user
 * scanning the strip can see. The description is what the label carries —
 * an SDK agent id says nothing about what the subagent was doing — with the
 * id as the fallback for a transcript whose row supplied no description.
 */
function _subagentTabLabel(label, agentId) {
  const text = typeof label === 'string' ? label.trim() : '';
  const base = text || agentId;
  return base.length > _AGENT_LABEL_MAX_LENGTH
    ? `📜 ${base.slice(0, _AGENT_LABEL_MAX_LENGTH - 1)}…`
    : `📜 ${base}`;
}

/**
 * One entry standing in for a transcript that could not be read.
 *
 * Per ``specs5/5-webapp/subagent-browser.md`` § Empty States: the tab shows
 * the reason in place of the messages and is *not* removed. Its row in Main
 * is evidence the subagent ran, and a tab that vanished on click would
 * contradict that — the user would read it as "nothing happened" rather
 * than "the record is gone".
 *
 * Shaped as a system event, the same shape a commit notice or a compaction
 * divider takes, because this is the app speaking about the transcript
 * rather than anything the subagent said.
 */
function _unreadableTranscript(reason) {
  return [{ role: 'user', content: `⚠️ ${reason}`, system_event: true }];
}

/**
 * Read one subagent's transcript, as messages the renderer can draw.
 *
 * Never throws and never returns empty: every failure — a dropped reply, an
 * ``{error}`` from the service, a session whose subagent directory was
 * pruned — comes back as the one-entry explanation above, because the
 * caller's job is to put a tab in the strip either way.
 */
async function _loadSubagentTranscript(panel, agentId, sessionId) {
  let result;
  try {
    result = await withRpcTimeout(
      panel.rpcExtract(
        'ClaudeCodeService.get_subagent_transcript',
        agentId,
        sessionId,
      ),
      SUBAGENT_TIMEOUT_MS,
      'get_subagent_transcript',
    );
  } catch (err) {
    console.error('[chat] get_subagent_transcript failed', err);
    return _unreadableTranscript(
      err?.message || 'Could not read this subagent transcript',
    );
  }
  if (!Array.isArray(result)) {
    const reason = result && result.error ? String(result.error) : '';
    return _unreadableTranscript(
      reason || 'This subagent has no readable transcript',
    );
  }
  if (result.length === 0) {
    return _unreadableTranscript('This subagent has no readable transcript');
  }
  // Through the same normalizer as a resumed session: this is the same
  // backend renderer's output, so a subagent's tool cards must survive the
  // trip the way the main transcript's do.
  return result.map(restoreMessage);
}

/**
 * Handle the ``view-subagents-requested`` event.
 *
 * Detail is ``{agents: [{agent_id, label}], session_id?}`` — dispatched by
 * the "View subagents (N)" affordance under a settled turn with all of that
 * turn's rows, and by a single subagent row with just its own. The session
 * defaults to the one the panel is attached to, which is the session Main is
 * showing; the history browser passes an explicit one when the transcript
 * belongs to a session that is not live.
 *
 * Tabs appear one at a time as their reads land, and the first one is
 * activated as soon as it exists rather than after the last: a turn that
 * fanned out to eight subagents would otherwise leave the user on Main
 * watching nothing happen. Per
 * ``specs5/5-webapp/subagent-browser.md`` § Historical Transcripts.
 */
async function onViewSubagentsRequested(panel, event) {
  const detail = event?.detail;
  if (!detail || typeof detail !== 'object') return;
  const agents = Array.isArray(detail.agents) ? detail.agents : null;
  if (!agents || agents.length === 0) return;

  // Skip subagents whose live tab is still in the strip: that tab is the
  // running conversation, and replacing it with a snapshot of what has
  // reached disk so far would be a worse view of the same subagent.
  const wanted = [];
  const seen = new Set();
  for (const agent of agents) {
    const id =
      agent && typeof agent.agent_id === 'string' ? agent.agent_id : '';
    if (!id || seen.has(id)) continue;
    seen.add(id);
    // Its live tab, whether that tab is keyed by the agent id or by the task
    // id an earlier event arrived with — a subagent must not end up with a
    // live feed and an archived snapshot of the same work side by side.
    if (panel._tabs.has(id) || findSubagentTab(panel, id)) continue;
    wanted.push({ agentId: id, label: agent.label });
  }
  if (wanted.length === 0) {
    panel._emitToast(
      agents.length === 1
        ? 'That subagent is still active in the tab strip'
        : 'Those subagents are still active in the tab strip',
      'info',
    );
    return;
  }

  const sessionId =
    typeof detail.session_id === 'string' && detail.session_id
      ? detail.session_id
      : panel._sessionInfo?.session_id || null;

  clearHistoricalTabs(panel);
  const generation = panel._historicalTabGeneration;

  let activated = false;
  for (const { agentId, label } of wanted) {
    const messages = await _loadSubagentTranscript(
      panel,
      agentId,
      sessionId,
    );
    // The strip was cleared under us — a session resume, or another click
    // on a different turn. These messages belong to nobody now.
    if (generation !== panel._historicalTabGeneration) return;
    const tabId = _historicalTabId(agentId);
    const state = makeTabState();
    state.readOnly = true;
    state.messages = messages;
    panel._tabs.set(tabId, state);
    panel._tabLabels.set(tabId, _subagentTabLabel(label, agentId));
    // Map mutations don't trigger Lit's reactivity on their own.
    panel.requestUpdate();
    // Only the first one, and only ever once: a later read landing must not
    // yank the user out of the transcript they started reading.
    if (!activated) {
      activated = true;
      panel._activeTabId = tabId;
    }
  }
}