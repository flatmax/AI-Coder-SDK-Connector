// Render helpers for AppShell.
//
// Extracted from webapp/src/app-shell.js so the root component
// stays focused on lifecycle wiring. The host (AppShell instance)
// is passed in explicitly — these functions read reactive state
// off `host.*` and bind handlers to host methods.

import { html } from 'lit';

import { RESIZE_RIGHT, RESIZE_BOTTOM, RESIZE_CORNER } from './constants.js';
import {
  bandColor,
  compactionLimit,
  warningPercent,
  windowPercent,
} from '../context-usage.js';

/**
 * Context-capacity bar — a thin strip at the dialog bottom
 * showing how full the engine's context window is.
 *
 * Re-based from `get_history_status` onto the engine's own
 * `get_context_usage`. The bar means the same thing it always
 * did — how close we are to a compaction — but the threshold is
 * now the engine's rather than one AC⚡DC configured for a
 * prompt it assembled.
 *
 * The bar fills toward the autocompact threshold, not the model's
 * ceiling, so 100% here means a compact is due. It was written
 * against `maxTokens` on the belief that the engine had already
 * subtracted the reserve; it has not — see context-usage.js. The
 * bar therefore stopped short: at the moment of compaction it
 * showed 84% in green, which is the one reading it exists to
 * rule out.
 *
 * Visibility rules:
 *
 *   - Returns empty before the first successful fetch (initial
 *     paint, or an engine that has not connected yet).
 *   - Returns empty when the engine reports no usable window
 *     size — a ratio against zero says nothing.
 *   - Otherwise always rendered, including at 0% — the constant
 *     placeholder makes the bar's collapse after a compact
 *     (tokens drop sharply) less surprising.
 *
 * Colour follows the same tri-state rule used by the Context
 * tab and the usage HUD: green ≤75%, amber 75-90%, red >90%.
 * The red band is the "imminent compaction" warning — users can
 * anticipate the pause.
 */
export function renderContextBar(host) {
  const usage = host._contextUsage;
  if (!usage) return null;
  // Keys are the SDK's ContextUsageResponse, passed through
  // unmodified by ClaudeCodeService.get_context_usage:
  //   totalTokens, maxTokens, rawMaxTokens, percentage, model,
  //   isAutoCompactEnabled, categories, memoryFiles, mcpTools.
  const max = Number(usage.maxTokens) || 0;
  if (max <= 0) return null;
  const tokens = Number(usage.totalTokens) || 0;
  const limit = compactionLimit(usage);
  // Capped at 100 for display — rendering widths beyond 100% would
  // trigger horizontal overflow on the bar container. A context past
  // the threshold pins the bar rather than bursting it.
  const pct = Math.max(0, Math.min(100, warningPercent(usage)));
  const color = bandColor(pct);
  // The tooltip carries the engine's own window figure alongside,
  // because that is the number `/context` prints and the two
  // disagreeing without explanation would look like a bug.
  const parts = [
    `Context: ${tokens.toLocaleString()} / ${max.toLocaleString()} `
    + `tokens (${windowPercent(usage).toFixed(1)}% of the window)`,
  ];
  if (usage.isAutoCompactEnabled === false) {
    parts.push('autocompact off — the turn fails at the limit');
  } else if (limit > 0 && limit < max) {
    // Naming the reserve keeps the number honest: a user who knows
    // the model's window is 200K should be told why the bar is full
    // at 167K rather than left to assume a bug.
    parts.push(
      `autocompact at ${limit.toLocaleString()} (${pct.toFixed(0)}% there, `
      + `${(max - limit).toLocaleString()} reserved for the summary)`,
    );
  }
  return html`
    <div class="compaction-bar" title=${parts.join(' · ')}>
      <div
        class="compaction-bar-fill"
        style="width: ${pct}%; background: ${color};"
      ></div>
    </div>
  `;
}

export function renderTemplate(host) {
  return html`
    <div class="viewer-background">
      <ac-diff-viewer
        class=${host._activeViewer === 'diff'
          ? 'viewer-visible'
          : 'viewer-hidden'}
        @active-file-changed=${host._onActiveFileChanged}
      ></ac-diff-viewer>
      <ac-svg-viewer
        class=${host._activeViewer === 'svg'
          ? 'viewer-visible'
          : 'viewer-hidden'}
        @active-file-changed=${host._onActiveFileChanged}
      ></ac-svg-viewer>
    </div>

    <ac-file-nav
      @navigate-file=${host._onNavigateFile}
    ></ac-file-nav>

    <div
      class="dialog ${host._undockedPos ? 'floating' : ''} ${host._minimized ? 'minimized' : ''}"
      style=${host._dialogInlineStyle()}
      @pointerdown=${host._onHeaderPointerDown}
    >
      ${host.connectionState === 'disconnected' ? html`
        <div class="reconnect-banner">
          Reconnecting… (attempt ${host.reconnectAttempt})
        </div>
      ` : null}
      ${host._minimized ? html`
        <button
          class="expand-fab"
          @click=${host._toggleMinimize}
          title="Expand dialog"
          aria-label="Expand dialog"
        >▴</button>
      ` : null}
      <div class="dialog-body">
        <div class="tab-panel ${host.activeTab === 'files' ? 'active' : ''}">
          <ac-files-tab></ac-files-tab>
        </div>
        <div class="tab-panel ${host.activeTab === 'context' ? 'active' : ''}">
          <ac-context-usage-tab></ac-context-usage-tab>
        </div>
        <div class="tab-panel ${host.activeTab === 'settings' ? 'active' : ''}">
          <ac-settings-tab></ac-settings-tab>
        </div>
        ${host._docConvertAvailable ? html`
          <div class="tab-panel ${host.activeTab === 'doc-convert' ? 'active' : ''}">
            <ac-doc-convert-tab></ac-doc-convert-tab>
          </div>
        ` : null}
      </div>
      <ac-doc-index-progress></ac-doc-index-progress>
      ${renderContextBar(host)}
      <div
        class="resize-handle right"
        @pointerdown=${(e) => host._onHandlePointerDown(e, RESIZE_RIGHT)}
      ></div>
      <div
        class="resize-handle bottom"
        @pointerdown=${(e) => host._onHandlePointerDown(e, RESIZE_BOTTOM)}
      ></div>
      <div
        class="resize-handle corner"
        @pointerdown=${(e) => host._onHandlePointerDown(e, RESIZE_CORNER)}
      ></div>
    </div>

    ${host.overlayVisible ? html`
      <div class="startup-overlay ${host.startupPercent >= 100 ? 'fading' : ''}">
        <div class="startup-brand">
          <span>AC</span><span class="bolt">⚡</span><span>DC</span>
        </div>
        <div class="startup-message">${host.startupMessage}</div>
        <div class="startup-progress">
          <div
            class="startup-progress-bar"
            style="width: ${host.startupPercent}%"
          ></div>
        </div>
      </div>
    ` : null}

    <div class="toast-layer">
      ${host.toasts.map((toast) => html`
        <div class="toast ${toast.type}" data-toast-id=${toast.id}>
          ${toast.message}
        </div>
      `)}
    </div>

    <!--
      Two overlays left with the native engine. ac-compaction-progress
      covered the 10-30s blocking call AC⚡DC made to find a topic
      boundary; the engine compacts on its own and reports a
      compact_boundary after the fact, which renders as a divider in
      the transcript instead. ac-cache-warmup-progress tracked a
      warmer that pre-heated prompt tiers this app no longer builds.
    -->
    <ac-speech-controls></ac-speech-controls>

    <ac-usage-hud></ac-usage-hud>

    <!--
      Last in the template and at z-index 9000: above the dialog panel,
      above the startup overlay, above the toast layer. A permission
      request during startup is possible when a session resumes into a
      pending call, so "above the startup overlay" is not theoretical
      (specs5/5-webapp/permission-dialog.md § Placement).
    -->
    <ac-permission-dialog></ac-permission-dialog>
  `;
}