// State-fetching helpers extracted from app-shell.js. These
// pull authoritative state snapshots from the backend and
// hydrate the host's reactive properties.

/**
 * Fetch get_current_state and dispatch the state-loaded
 * event so child components (files tab, chat panel) can
 * restore their UI.
 *
 * The snapshot comes from `ClaudeCodeService` as of phase 2.
 * It has to: `state-loaded` is where the chat panel restores
 * its messages and re-attaches to a turn still in flight, and
 * an `active_streams` entry from the native engine carries
 * `accumulated_content` where the new one carries `blocks`.
 * Two snapshot sources would mean the shell and the chat panel
 * disagreeing about whether a turn is running.
 *
 * Four fields the native snapshot carried are simply absent
 * from `EngineState`, and every read of them below is guarded
 * rather than defaulted:
 *
 *   - `mode` / `cross_ref_enabled` — the native engine's
 *     code/doc modes. CC-12 replaces the toggle with a preset
 *     selector; until then the shell keeps its defaults.
 *   - `enrichment_status` — the doc-index KeyBERT probe. Its
 *     one-shot toast stays silent rather than firing wrongly.
 *   - `excluded_index_files` — becomes `denied_read_files`
 *     under CC-14, read by the picker, not here.
 */
export async function fetchCurrentState(host) {
  if (!host.call) return;
  try {
    const fn = host.call['ClaudeCodeService.get_current_state'];
    if (typeof fn !== 'function') return;
    const raw = await fn();
    // Unwrap jrpc-oo envelope.
    let state = raw;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      const keys = Object.keys(raw);
      if (keys.length === 1) {
        const inner = raw[keys[0]];
        if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
          state = inner;
        }
      }
    }
    if (!state || typeof state !== 'object') return;
    // Update browser tab title from repo name.
    if (state.repo_name) {
      host._repoName = state.repo_name;
      document.title = state.repo_name;
    }
    host._initComplete = !!state.init_complete;
    // Hydrate mode state from the snapshot. Defaults
    // cover older backends that don't report these
    // fields yet.
    if (typeof state.mode === 'string') {
      host._mode = state.mode;
    }
    if (typeof state.cross_ref_enabled === 'boolean') {
      host._crossRefEnabled = state.cross_ref_enabled;
    }
    // Review state — the snapshot carries a `review_state`
    // object with `active: bool` (matching the
    // get_review_state RPC's return shape). Missing
    // field means no review in progress.
    if (
      state.review_state
      && typeof state.review_state === 'object'
    ) {
      host._reviewActive = !!state.review_state.active;
    } else {
      host._reviewActive = false;
    }
    // Doc Convert availability — true when markitdown is
    // importable on the server. Guarded rather than coerced:
    // `!!undefined` would hide the tab, and a snapshot that
    // merely forgot to mention the probe is not the same fact
    // as a server that can't convert.
    if (typeof state.doc_convert_available === 'boolean') {
      host._docConvertAvailable = state.doc_convert_available;
    }
    // Enrichment status — show the one-shot toast if the
    // backend reports KeyBERT is unavailable. No-op for
    // other values. Older backends omit the field; in that
    // case we pass undefined and the helper returns silently.
    host._maybeShowEnrichmentUnavailableToast(
      state.enrichment_status,
    );
    // Fallback when the persisted active tab no longer
    // applies. Happens when the user's last session was
    // in a repo with doc-convert enabled and they've
    // reconnected to one without markitdown. Without
    // this, activeTab stays 'doc-convert' but the panel
    // is excluded from the DOM — producing a blank body.
    if (
      host.activeTab === 'doc-convert'
      && !host._docConvertAvailable
    ) {
      host._switchTab('files');
    }
    // If the backend reports init is already complete,
    // dismiss the startup overlay. Handles the common
    // race where Phase 2 finishes before the browser
    // registers AcApp — startupProgress events get
    // dropped, but get_current_state arrives afterward
    // with init_complete=true and we can dismiss based
    // on that.
    if (host._initComplete && host.overlayVisible) {
      host.startupPercent = 100;
      host.startupMessage = 'Ready';
      setTimeout(() => {
        host.overlayVisible = false;
        if (host._pendingReopen) {
          const path = host._loadLastOpenFile();
          if (path) host._doReopenLastFile(path);
        }
      }, 400);
    }
    // Dispatch state-loaded so child components restore.
    window.dispatchEvent(
      new CustomEvent('state-loaded', { detail: state }),
    );
    // After state is loaded, try to reopen the last file.
    host._tryReopenLastFile();
  } catch (err) {
    console.warn('[app-shell] get_current_state failed', err);
  }
}

/**
 * Event handler bound to stream-complete, session-changed,
 * and compaction-event. Fire-and-forget refresh — we don't
 * need to await the fetch here, the reactive property update
 * in _fetchContextUsage re-renders when the result lands.
 */
export function onContextUsageRefresh(host) {
  fetchContextUsage(host);
}

/**
 * Fetch the engine's context-window usage and update the
 * reactive property behind the dialog's capacity bar.
 *
 * Replaces the `get_history_status` fetch this used to make.
 * That method merged AC⚡DC's own token budget with its own
 * compaction threshold — two numbers this app computed about a
 * prompt it assembled. The engine now owns both, and reports
 * them together: `percentage` against `maxTokens`, where
 * `maxTokens` is already reduced by the autocompact buffer. So
 * the bar filling means a compact is imminent, which is exactly
 * what the old bar was trying to say.
 *
 * Guarded against overlapping fetches — this one crosses into
 * the CLI subprocess as a control request, so a burst of events
 * coalescing matters more than it did for a local computation.
 *
 * Non-fatal on failure: a disconnected engine, a session that
 * was lost, or a transient error all leave the prior snapshot
 * in place. The bar keeps showing the last-known state; the
 * next event triggers a retry.
 */
export async function fetchContextUsage(host) {
  if (!host.call) return;
  if (host._contextUsageFetchInFlight) return;
  host._contextUsageFetchInFlight = true;
  try {
    // Call in the same style as _fetchCurrentState — no
    // typeof check on the method reference. The jrpc-oo
    // call proxy exposes methods as Proxy-wrapped
    // callables whose typeof is not necessarily
    // 'function', so guarding on typeof was rejecting
    // valid calls and silently leaving the bar empty.
    const raw = await host.call[
      'ClaudeCodeService.get_context_usage'
    ]();
    // Unwrap single-key envelope the same way
    // _fetchCurrentState does. jrpc-oo returns
    // { ClassName: { ... } } for method calls.
    let payload = raw;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      const keys = Object.keys(raw);
      if (keys.length === 1) {
        const inner = raw[keys[0]];
        if (
          inner && typeof inner === 'object'
          && !Array.isArray(inner)
        ) {
          payload = inner;
        }
      }
    }
    // `{error: ...}` is the service's shape for "the engine
    // isn't ready" — expected before connect and after a lost
    // session, so it keeps the last snapshot rather than
    // blanking the bar.
    if (payload && typeof payload === 'object' && payload.error) {
      console.debug(
        '[app-shell] get_context_usage unavailable', payload.error,
      );
      return;
    }
    const usage = payload && typeof payload === 'object'
      ? payload.usage
      : null;
    if (usage && typeof usage === 'object') {
      host._contextUsage = usage;
    }
  } catch (err) {
    // Surface failures with console.warn — debug-level
    // messages are hidden by default in most browsers
    // and we'd lose visibility on genuine RPC errors.
    // method-not-found is still the only expected
    // non-fatal case (older backend), so filter that
    // to debug to avoid nagging.
    const msg = err?.message || '';
    if (msg.includes('method not found')) {
      console.debug(
        '[app-shell] get_context_usage not available', err,
      );
    } else {
      console.warn(
        '[app-shell] get_context_usage failed', err,
      );
    }
  } finally {
    host._contextUsageFetchInFlight = false;
  }
}