// The permission-mode selector.
//
// The mode decides what the agent may do without asking, which makes this the
// one control in the action bar that changes the meaning of every other one.
// specs5/5-webapp/chat.md § Permission Mode Selector pins down four things, and
// each is load-bearing:
//
//   1. **Always visible.** Not behind the search-collapse, not in a menu. A
//      user who cannot see the current mode cannot know whether the next edit
//      will ask.
//
//   2. **Never optimistic.** The selector flips when the engine broadcasts
//      `permissionModeChanged`, never on click. A `set_permission_mode` call
//      can be refused — no engine, a lost session, a non-localhost caller —
//      and a selector reading `acceptEdits` while the engine sits in `default`
//      is a lie about what happens next.
//
//   3. **Read-only for participants.** The engine rejects a non-localhost
//      caller outright; showing an enabled control that always fails is worse
//      than showing a disabled one that explains why.
//
//   4. **`bypassPermissions` is warned and never preselected.** It is in the
//      list because the engine supports it and hiding a mode the CLI will
//      happily report is its own kind of dishonesty. It is not the default,
//      and picking it costs a confirmation.

import { html, nothing } from 'lit';

/**
 * The engine's modes, in ascending order of how much they let through.
 *
 * Mirrors `PERMISSION_MODES` in `src/aic_dc/claude_code/session.py`. The engine
 * validates and is authoritative; this list exists to label them. A mode the
 * engine reports that is missing here still renders — see
 * `renderPermissionModeSelector` — because a selector that silently dropped an
 * unknown mode would show the wrong one as selected.
 */
export const PERMISSION_MODES = [
  {
    value: 'plan',
    label: 'Plan',
    detail: 'Read and think only. No edits, no commands.',
  },
  {
    value: 'default',
    label: 'Ask',
    detail: 'Ask before edits and commands. The safe default.',
  },
  {
    value: 'acceptEdits',
    label: 'Accept edits',
    detail: 'Edits apply without asking. Commands still ask.',
  },
  {
    value: 'dontAsk',
    label: "Don't ask",
    detail: 'Skip prompts for calls that have a rule. Unrecognised calls still ask.',
  },
  {
    value: 'auto',
    label: 'Auto',
    detail: 'The CLI decides when to ask.',
  },
  {
    value: 'bypassPermissions',
    label: 'Bypass (unsafe)',
    detail: 'Nothing asks. Every tool call runs, including deletions.',
  },
];

/** Modes that turn the gate off entirely and need a confirmation first. */
const CONFIRM_MODES = new Set(['bypassPermissions']);

/**
 * The mode the panel should show before the engine has said anything.
 *
 * `default` rather than null: the selector renders on first paint, and the one
 * guess that cannot mislead is the mode that asks about everything. Being wrong
 * this way means a user expects a prompt and does not get one, which they
 * notice; the reverse means they expect a prompt, do not get one, and only find
 * out from the diff.
 */
export const INITIAL_PERMISSION_MODE = 'default';

/**
 * Label for a mode value, falling back to the raw string.
 *
 * The engine's mode list can grow without this file changing, so an unmapped
 * value reads as itself rather than as blank.
 */
export function permissionModeLabel(mode) {
  const known = PERMISSION_MODES.find((entry) => entry.value === mode);
  return known ? known.label : (mode || '');
}

/** Whether picking `mode` should ask the user to confirm first. */
export function needsConfirmation(mode) {
  return CONFIRM_MODES.has(mode);
}

/**
 * The options to render, guaranteed to contain the current mode.
 *
 * If the engine reports a mode this build has never heard of — a newer CLI, a
 * session resumed from one — it is appended rather than dropped. A `<select>`
 * whose value is not among its options renders as the *first* option, which
 * would show `Plan` for a session running in something else entirely.
 */
export function permissionModeOptions(current) {
  if (!current || PERMISSION_MODES.some((entry) => entry.value === current)) {
    return PERMISSION_MODES;
  }
  return [
    ...PERMISSION_MODES,
    { value: current, label: current, detail: 'Reported by the engine.' },
  ];
}

/**
 * Render the selector.
 *
 * Deliberately outside every `.search-collapsible` group: the search bar
 * expanding must not be able to hide the safety posture.
 *
 * The pointer and key handlers are a gesture latch: a `change` here authorises
 * a destructive confirmation, and a `change` alone does not prove a user chose
 * anything. A browser restoring form state on load can raise one, and
 * `isTrusted` does not separate that from a click — the restored event is the
 * browser's own, so it is trusted too. A real selection always follows a
 * pointer or a key on the control. `autocomplete="off"` asks for the same
 * thing declaratively (the HTML spec exempts such controls from form-state
 * restoration) and costs nothing where it is honoured.
 *
 * Both are guards rather than a fix for a reproduced fault. The symptom they
 * are aimed at — the bypass confirmation appearing on page load, over a
 * selector still reading "Ask" — was reported from a live run but did *not*
 * reproduce on the dev backend on 2026-08-25. Instrumenting this handler to
 * log every `change` it received showed nothing at all on the loads where a
 * dialog appeared, so on that build Chrome was not restoring the select into
 * an event: the reappearing dialog was the CDP harness re-surfacing a native
 * confirm it had already handled. Note that the "selector still reads Ask"
 * half needs no page-load story to explain it — the reset below puts the
 * control back before the confirmation is even drawn, so the mismatch is
 * visible the first time too.
 */
export function renderPermissionModeSelector(panel) {
  const current = panel._permissionMode || INITIAL_PERMISSION_MODE;
  const options = permissionModeOptions(current);
  const entry = options.find((o) => o.value === current);
  const readOnly = panel._canSetPermissionMode === false;
  const pending = !!panel._permissionModePending;
  const disabled = !panel.rpcConnected || readOnly || pending;
  const unsafe = current === 'bypassPermissions';

  const title = readOnly
    ? `Permission mode: ${entry?.label || current} — only the host can change this`
    : pending
      ? 'Waiting for the engine to confirm the new mode…'
      : `Permission mode — ${entry?.detail || current}`;

  return html`
    <div
      class="permission-mode ${unsafe ? 'unsafe' : ''}"
      role="group"
      aria-label="Permission mode"
    >
      <span class="permission-mode-glyph" aria-hidden="true"
        >${unsafe ? '⚠️' : '🔒'}</span
      >
      <select
        class="permission-mode-select"
        .value=${current}
        ?disabled=${disabled}
        autocomplete="off"
        aria-label="Permission mode"
        title=${title}
        @pointerdown=${() => notePermissionModeGesture(panel)}
        @keydown=${() => notePermissionModeGesture(panel)}
        @change=${(e) => onPermissionModeSelect(panel, e)}
      >
        ${options.map(
          (option) => html`<option
            value=${option.value}
            ?selected=${option.value === current}
            title=${option.detail}
          >
            ${option.label}
          </option>`,
        )}
      </select>
      ${pending
        ? html`<span class="permission-mode-pending" aria-hidden="true">…</span>`
        : nothing}
    </div>
  `;
}

/**
 * Record that the user is actually operating the control.
 *
 * Set by a pointer or a key on the `<select>` and spent by the next `change`.
 * A `change` the browser synthesises — restoring form state on load, say —
 * has no gesture in front of it, so it finds the latch down and changes
 * nothing. See `renderPermissionModeSelector` for why the confirmation is
 * worth guarding this way even though the reported symptom did not reproduce.
 */
export function notePermissionModeGesture(panel) {
  if (panel) panel._permissionModeGesture = true;
}

/**
 * Handle a selection.
 *
 * The `<select>`'s DOM value is reset to the engine's current mode straight
 * away, before the RPC resolves. The native element flips itself on change and
 * Lit will not put it back for us — its `.value` binding sees no reactive
 * change, since `panel._permissionMode` is exactly what it was. So the reset is
 * explicit, and the real flip arrives on the broadcast.
 *
 * That reset is also what makes ignoring a gesture-less `change` safe: a
 * restored value is put back to the engine's mode on the way out, so the
 * control still tells the truth about the session even when the event that
 * reached it was a phantom.
 */
export function onPermissionModeSelect(panel, event) {
  const select = event?.target;
  const mode = select?.value;
  const current = panel._permissionMode || INITIAL_PERMISSION_MODE;
  if (select) select.value = current;
  // Spent whether or not it was set: one gesture authorises one change, and a
  // latch left standing would arm the *next* restored event instead.
  const gesture = panel._permissionModeGesture === true;
  panel._permissionModeGesture = false;
  if (!gesture) return;
  if (typeof mode !== 'string' || !mode || mode === current) return;
  if (needsConfirmation(mode)) {
    const ok = window.confirm(
      'Bypass permissions?\n\n'
      + 'Every tool call will run without asking — edits, shell commands, '
      + 'deletions. Nothing will prompt you until you change this back.',
    );
    if (!ok) return;
  }
  setPermissionMode(panel, mode);
}

/**
 * Ask the engine to change mode.
 *
 * Sets `_permissionModePending` so the control disables while the call is in
 * flight — two clicks in flight at once would race, and whichever broadcast
 * landed last would win regardless of what the user picked last. The flag is
 * cleared by the `permissionModeChanged` handler on success, and here on
 * failure.
 */
export function setPermissionMode(panel, mode) {
  if (!panel.rpcConnected) {
    panel._emitToast('Not connected to the server', 'warning');
    return;
  }
  panel._permissionModePending = true;
  panel.requestUpdate();
  panel
    .rpcExtract('ClaudeCodeService.set_permission_mode', mode)
    .then((result) => {
      if (result && typeof result === 'object' && result.error) {
        panel._permissionModePending = false;
        panel.requestUpdate();
        const reason = result.error === 'restricted'
          ? result.reason || 'Only the host can change the permission mode'
          : result.error;
        panel._emitToast(reason, 'warning');
        return;
      }
      // Success leaves `_permissionModePending` set. The broadcast clears it,
      // and it is the broadcast — not this reply — that flips the selector.
      // Clearing it here would let the control re-enable a frame before the
      // mode it displays catches up.
    })
    .catch((err) => {
      panel._permissionModePending = false;
      panel.requestUpdate();
      panel._emitToast(
        `Could not change the permission mode: ${err?.message || err}`,
        'error',
      );
    });
}

/**
 * Find out whether this client may change the mode.
 *
 * Same probe the permission dialog uses, and the same defaults: no collab
 * service means single-user, which means we are the host. It only ever narrows
 * — the engine enforces the real gate in `set_permission_mode`, so being wrong
 * here costs a rejected call rather than an unauthorised one.
 */
export async function probeModeAuthority(panel) {
  try {
    const role = await panel.rpcExtract('Collab.get_collab_role');
    if (role && typeof role === 'object' && !role.error) {
      panel._canSetPermissionMode = role.is_localhost !== false;
      panel.requestUpdate();
      return;
    }
  } catch (_) {
    // No collab service registered — single-user, and we are the host.
  }
  panel._canSetPermissionMode = true;
  panel.requestUpdate();
}

/**
 * Handle a `role-changed` broadcast: a client's authority can change mid
 * session when collab mode is turned on or off underneath it.
 */
export function onRoleChanged(panel, event) {
  const detail = event?.detail;
  if (detail && typeof detail.is_localhost === 'boolean') {
    panel._canSetPermissionMode = detail.is_localhost;
    panel.requestUpdate();
  }
}
