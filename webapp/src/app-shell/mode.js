// Mode-switching helpers extracted from app-shell.js. Manages
// primary mode (code/doc), which lived on the backend
// authoritatively and propagated via mode-changed broadcasts.
//
// The RPC went with the native engine in conversion phase 3,
// and no template calls `_switchMode` any more — the toggle
// came out of the action bar in phase 2. What survives is the
// shape: `switchMode` guards on the method being absent and
// returns, so nothing here can fire at a service that is gone.
// The mode toggle's replacement is the preset selector
// (CC-12), which is deferred by decision — see
// specs5/plan/delivery.md. `onModeChanged` below is dormant
// for the same reason: nothing broadcasts `modeChanged` any
// more.
//
// **The cross-reference overlay is gone**, retired in phase 4
// rather than kept dormant like the mode axis. It had no
// replacement to wait for: it chose which index fed the native
// engine's prompt, and both indexes are now permanently
// available to the agent as MCP tools, so there is nothing
// left to switch on or off (specs5/5-webapp/shell.md § Preset
// and Permission Controls).

/**
 * Handle mode-changed broadcasts. Fires for our own
 * switches and for any other admitted client's switches
 * — collaborators follow the server's authoritative
 * mode.
 *
 * Payload shape: ``{ mode: 'code' | 'doc' }``. The
 * ``cross_ref_enabled`` field the native engine also sent is
 * ignored — see the note at the top of this file.
 */
export function onModeChanged(host, event) {
  const detail = event.detail || {};
  if (typeof detail.mode === 'string') {
    host._mode = detail.mode;
  }
  // Enrichment status may piggyback on modeChanged — the
  // backend broadcasts when it flips to "unavailable" so
  // mid-session clients learn without polling. Route to
  // the one-shot toast helper; it no-ops for other values.
  if (typeof detail.enrichment_status === 'string') {
    host._maybeShowEnrichmentUnavailableToast(
      detail.enrichment_status,
    );
  }
}

/**
 * Switch to the given primary mode. No-op if already
 * in that mode (backend would also no-op, but saves an
 * RPC). Disabled for non-localhost callers — the button
 * is visually disabled, but we guard here too.
 */
export async function switchMode(host, mode) {
  if (mode !== 'code' && mode !== 'doc') return;
  if (mode === host._mode) return;
  if (!host.call) return;
  if (!host._isLocalhost) return;
  const fn = host.call['LLMService.switch_mode'];
  if (typeof fn !== 'function') return;
  try {
    const result = await fn(mode);
    // Unwrap single-key envelope.
    let payload = result;
    if (result && typeof result === 'object' && !Array.isArray(result)) {
      const keys = Object.keys(result);
      if (keys.length === 1) {
        const inner = result[keys[0]];
        if (inner && typeof inner === 'object') payload = inner;
      }
    }
    if (payload && payload.error) {
      const reason = payload.reason || payload.error;
      host._showToast(`Mode switch failed: ${reason}`, 'warning');
      return;
    }
    // mode-changed broadcast will update _mode; don't
    // set it optimistically or we'll race the broadcast.
    host._showToast(
      mode === 'doc' ? 'Switched to document mode'
                     : 'Switched to code mode',
      'info',
    );
  } catch (err) {
    host._showToast(
      `Mode switch failed: ${err?.message || 'RPC error'}`,
      'error',
    );
  }
}

// `toggleCrossRef` stood here until conversion phase 4.