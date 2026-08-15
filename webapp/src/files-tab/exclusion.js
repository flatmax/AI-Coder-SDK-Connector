// Read-denial state — apply / send / event handlers.
//
// The third checkbox state used to mean "keep this file
// out of the structural index". It now means the agent
// is denied `Read` on that path, per
// specs5/plan/decisions.md § CC-14: there is no prompt
// to exclude a file from, so the closest honest
// equivalent is a real permission rule that stops the
// agent reading the file. The rule lands in
// `.claude/settings.local.json` — git-ignored,
// inspectable, revocable by editing a file.
//
// The internal vocabulary stays `excluded` /
// `_excludedFiles` / `exclusion-changed`. That is the
// name of a *tree state* — the third position of a
// three-state checkbox — and it is shared with the
// picker, its event contract and a dozen tests. What
// changed is what the state means to the backend, and
// that lives in `sendExclusionToServer` alone. User-
// facing wording is the picker's job and says "deny".
//
// One RPC, no per-tab dispatch. The agent-tab branch went
// with the native engine's parallel agents: a per-agent
// exclusion set was a filter on a per-agent prompt, and
// SDK subagents share the session's settings sources.
// A deny rule is repo-wide by construction.
//
// The L0-invalidation dialog that used to gate every
// exclusion is gone — see the tombstone below
// `sendExclusionToServer`.

/**
 * Apply a new exclusion set. Single entry point so
 * the set-equality short-circuit and the
 * direct-update push to the picker are uniform.
 */
export function applyExclusion(host, newExcluded, notifyServer) {
  // Fast-path no-op when the set hasn't actually
  // changed. Prevents loopback from the server
  // broadcast (when collab mode lands this for real)
  // doing another round-trip for our own update.
  if (host._setsEqual(host._excludedFiles, newExcluded)) return;
  host._excludedFiles = newExcluded;
  // Direct-update pattern. Assign to picker prop then
  // requestUpdate.
  const picker = host._picker();
  if (picker) {
    picker.excludedFiles = new Set(newExcluded);
    picker.requestUpdate();
  }
  if (notifyServer) {
    sendExclusionToServer(host, Array.from(newExcluded));
  }
}

/**
 * Write the deny-read rules for `files` to the CLI's
 * local settings.
 *
 * The list is authoritative, not additive — the RPC
 * replaces every rule it owns, which is what makes
 * un-excluding work without a second method.
 *
 * Two things are worth surfacing and both come from the
 * return value rather than from an assumption here:
 *
 *   - `error: 'restricted'` — the RPC is localhost-only
 *     (CC-15), so a remote collaborator's tick is
 *     refused. It has to say so, or the checkbox lies.
 *   - `takes_effect` — the CLI reads its settings
 *     sources itself, so a rule written mid-session
 *     applies from its next read of them. Shown once per
 *     session, on the first denial: it is the kind of
 *     caveat a user needs to hear once, not on every
 *     checkbox tick.
 */
export async function sendExclusionToServer(host, files) {
  try {
    const result = await host.rpcExtract(
      'ClaudeCodeService.set_denied_read_files',
      files,
    );
    if (
      !result ||
      typeof result !== 'object' ||
      Array.isArray(result)
    ) {
      return;
    }
    if (result.error === 'restricted') {
      host._showToast(
        result.reason || 'Restricted operation',
        'warning',
      );
      return;
    }
    if (result.error) {
      host._showToast(
        `Failed to deny agent read: ${result.error}`,
        'error',
      );
      return;
    }
    if (
      files.length > 0 &&
      result.takes_effect &&
      !host._readDenyCaveatShown
    ) {
      host._readDenyCaveatShown = true;
      host._showToast(
        `Agent denied read on ${files.length} `
        + `file${files.length === 1 ? '' : 's'} — `
        + `${result.takes_effect}.`,
        'info',
      );
    }
  } catch (err) {
    console.error(
      '[files-tab] set_denied_read_files failed', err,
    );
    host._showToast(
      `Failed to deny agent read: ${err?.message || err}`,
      'error',
    );
  }
}

// The L0-invalidation dialog lived here until conversion
// phase 3, along with its three-way preference
// (`always` / `never` / `ask`) in localStorage. Excluding
// a file used to rewrite the L0 cache prefix — typically
// 100,000+ tokens of aggregate structural map — so the
// dialog existed to let the user decide whether to pay
// that now or leave the map stale until the next
// invalidating event. Both halves of that trade are gone:
// this app builds no aggregate map, and the CLI's prompt
// cache is the CLI's business. A dialog asking the user
// to authorise a cost we no longer incur, about a cache
// we no longer own, would be theatre. What replaced its
// one honest job — telling the user the change is not
// instant — is the `takes_effect` toast above.

/**
 * Picker emits `exclusion-changed` when the user
 * shift+clicks a file checkbox or a directory
 * checkbox (the latter applies to every descendant
 * file). The event carries an array of excluded
 * paths; we update our authoritative state, push to
 * the picker via direct-update, and notify the
 * server.
 */
export function onExclusionChanged(host, event) {
  const incoming = event.detail?.excludedFiles;
  if (!Array.isArray(incoming)) return;
  applyExclusion(host, new Set(incoming), /* notifyServer */ true);
}
