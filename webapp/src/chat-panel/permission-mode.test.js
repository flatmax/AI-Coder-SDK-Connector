// Tests for permission-mode.js — the control that decides what the agent may
// do without asking.
//
// action-bar.test.js pins that the selector is *present* in every tab and
// search state. This file pins that what it shows is true, which is the harder
// half. Four properties, each of which fails quietly and expensively:
//
//   1. **Never optimistic.** The selector flips on the engine's broadcast, not
//      on click. A control reading `acceptEdits` while the engine sits in
//      `default` is a lie about what the next edit will do — and the reverse is
//      worse, because the user only finds out from the diff.
//   2. **Read-only for participants.** The engine rejects a non-localhost
//      caller, so an enabled control would be an invitation to a guaranteed
//      failure.
//   3. **An unknown mode is shown, not dropped.** A `<select>` whose value is
//      not among its options renders the *first* one, which would report `Plan`
//      for a session running in something else entirely.
//   4. **Bypass costs a confirmation** and is never where the panel starts.

import { describe, expect, it, vi } from 'vitest';

import {
  INITIAL_PERMISSION_MODE,
  PERMISSION_MODES,
  needsConfirmation,
  permissionModeLabel,
  permissionModeOptions,
  probeModeAuthority,
  onRoleChanged,
  setPermissionMode,
} from './permission-mode.js';
import { mountPanel, publishFakeRpc, settle } from './test-helpers.js';

const select = (panel) =>
  panel.shadowRoot.querySelector('select.permission-mode-select');

/** Mount with a stub `set_permission_mode` and return both. */
async function mountWithSetter(impl, props = {}) {
  const setMode = vi.fn(impl ?? (async () => ({ mode: 'acceptEdits' })));
  publishFakeRpc({ 'ClaudeCodeService.set_permission_mode': setMode });
  const panel = mountPanel(props);
  await settle(panel);
  return { panel, setMode };
}

/** Drive a real selection through the native element. */
/**
 * Pick a mode the way a user does: a press on the control, then the change it
 * produces. The pointer half is not decoration — the handler ignores a
 * `change` with no gesture in front of it, because a browser restoring form
 * state can raise one and a bypass confirmation must not answer to that.
 */
async function choose(panel, value) {
  const el = select(panel);
  el.dispatchEvent(new Event('pointerdown'));
  el.value = value;
  el.dispatchEvent(new Event('change'));
  await settle(panel);
}

/** A `change` with no gesture behind it — what a restored control raises. */
async function chooseWithoutGesture(panel, value) {
  const el = select(panel);
  el.value = value;
  el.dispatchEvent(new Event('change'));
  await settle(panel);
}

function captureToasts() {
  const toasts = vi.fn();
  window.addEventListener('aic-toast', toasts);
  return {
    toasts,
    done: () => window.removeEventListener('aic-toast', toasts),
  };
}

// ---------------------------------------------------------------------------
// The mode table
// ---------------------------------------------------------------------------

describe('PERMISSION_MODES', () => {
  it('starts the panel in the mode that asks about everything', () => {
    // The one guess that cannot mislead. Being wrong this way means the user
    // expects a prompt and gets one they did not need.
    expect(INITIAL_PERMISSION_MODE).toBe('default');
    expect(
      PERMISSION_MODES.some((m) => m.value === INITIAL_PERMISSION_MODE),
    ).toBe(true);
  });

  it('never starts in a mode that turns the gate off', () => {
    expect(needsConfirmation(INITIAL_PERMISSION_MODE)).toBe(false);
  });

  it('is ordered by how much each mode lets through', () => {
    // The order is the whole information design of the control: reading down
    // the list is reading down the safety gradient.
    expect(PERMISSION_MODES.map((m) => m.value)).toEqual([
      'plan',
      'default',
      'acceptEdits',
      'dontAsk',
      'auto',
      'bypassPermissions',
    ]);
  });

  it('gives every mode a label and an explanation', () => {
    for (const mode of PERMISSION_MODES) {
      expect(mode.value).toBeTruthy();
      expect(mode.label).toBeTruthy();
      expect(mode.detail).toBeTruthy();
    }
  });
});

describe('permissionModeLabel', () => {
  it('labels the modes we know', () => {
    expect(permissionModeLabel('plan')).toBe('Plan');
    expect(permissionModeLabel('default')).toBe('Ask');
    expect(permissionModeLabel('acceptEdits')).toBe('Accept edits');
    expect(permissionModeLabel('bypassPermissions')).toBe('Bypass (unsafe)');
  });

  it('reads an unmapped mode as itself', () => {
    // The engine's list can grow without this file changing.
    expect(permissionModeLabel('microplan')).toBe('microplan');
  });

  it('is empty rather than "undefined" for no mode', () => {
    expect(permissionModeLabel(null)).toBe('');
    expect(permissionModeLabel(undefined)).toBe('');
    expect(permissionModeLabel('')).toBe('');
  });
});

describe('needsConfirmation', () => {
  it('only bypass costs a confirmation', () => {
    expect(needsConfirmation('bypassPermissions')).toBe(true);
    for (const mode of PERMISSION_MODES) {
      if (mode.value === 'bypassPermissions') continue;
      expect(needsConfirmation(mode.value)).toBe(false);
    }
    expect(needsConfirmation('something-new')).toBe(false);
  });
});

describe('permissionModeOptions', () => {
  it('uses the table as-is for a mode it knows', () => {
    expect(permissionModeOptions('acceptEdits')).toBe(PERMISSION_MODES);
    expect(permissionModeOptions(null)).toBe(PERMISSION_MODES);
    expect(permissionModeOptions('')).toBe(PERMISSION_MODES);
  });

  it('appends a mode the engine reports but this build has not heard of', () => {
    const options = permissionModeOptions('microcompactOnly');
    expect(options).toHaveLength(PERMISSION_MODES.length + 1);
    expect(options.at(-1)).toEqual({
      value: 'microcompactOnly',
      label: 'microcompactOnly',
      detail: 'Reported by the engine.',
    });
    // And leaves the shared table alone.
    expect(PERMISSION_MODES).toHaveLength(6);
  });
});

// ---------------------------------------------------------------------------
// What the selector shows
// ---------------------------------------------------------------------------

describe('the rendered selector', () => {
  it('shows the mode the panel holds', async () => {
    const panel = mountPanel();
    await settle(panel);
    expect(select(panel).value).toBe('default');
    panel._permissionMode = 'plan';
    await settle(panel);
    expect(select(panel).value).toBe('plan');
  });

  it('selects an unknown engine mode rather than falling back to the first', async () => {
    // The failure this prevents: a resumed session running in a mode a newer
    // CLI invented, reported to the user as `Plan`.
    const panel = mountPanel();
    await settle(panel);
    panel._permissionMode = 'somethingNew';
    await settle(panel);
    expect(select(panel).value).toBe('somethingNew');
    const labels = [...select(panel).options].map((o) => o.value);
    expect(labels).toContain('somethingNew');
  });

  it('warns in the markup when the gate is off', async () => {
    const panel = mountPanel();
    await settle(panel);
    expect(
      panel.shadowRoot.querySelector('.permission-mode').classList
        .contains('unsafe'),
    ).toBe(false);
    expect(
      panel.shadowRoot.querySelector('.permission-mode-glyph').textContent,
    ).toContain('🔒');
    panel._permissionMode = 'bypassPermissions';
    await settle(panel);
    expect(
      panel.shadowRoot.querySelector('.permission-mode').classList
        .contains('unsafe'),
    ).toBe(true);
    expect(
      panel.shadowRoot.querySelector('.permission-mode-glyph').textContent,
    ).toContain('⚠️');
  });

  it('is disabled with no engine to ask', async () => {
    const panel = mountPanel();
    await settle(panel);
    expect(panel.rpcConnected).toBeFalsy();
    expect(select(panel).disabled).toBe(true);
  });

  it('is disabled for a participant, and says why', async () => {
    const { panel } = await mountWithSetter();
    panel._canSetPermissionMode = false;
    await settle(panel);
    expect(select(panel).disabled).toBe(true);
    expect(select(panel).getAttribute('title')).toContain('only the host');
  });

  it('is disabled while a change is in flight', async () => {
    const { panel } = await mountWithSetter();
    panel._permissionModePending = true;
    await settle(panel);
    expect(select(panel).disabled).toBe(true);
    expect(
      panel.shadowRoot.querySelector('.permission-mode-pending'),
    ).toBeTruthy();
    expect(select(panel).getAttribute('title')).toMatch(/waiting/i);
  });

  it('carries each mode’s explanation as its tooltip', async () => {
    const panel = mountPanel();
    await settle(panel);
    const byValue = new Map(
      [...select(panel).options].map((o) => [o.value, o.title]),
    );
    for (const mode of PERMISSION_MODES) {
      expect(byValue.get(mode.value)).toBe(mode.detail);
    }
  });
});

// ---------------------------------------------------------------------------
// Choosing a mode
// ---------------------------------------------------------------------------

describe('choosing a mode', () => {
  it('asks the engine for the mode that was picked', async () => {
    const { panel, setMode } = await mountWithSetter();
    await choose(panel, 'acceptEdits');
    expect(setMode).toHaveBeenCalledWith('acceptEdits');
  });

  it('does not flip the control before the engine confirms', async () => {
    // The property the whole module exists for. The native `<select>` flips
    // itself on change and Lit will not put it back, so the handler does.
    const { panel } = await mountWithSetter();
    await choose(panel, 'acceptEdits');
    expect(panel._permissionMode).toBe('default');
    expect(select(panel).value).toBe('default');
  });

  it('flips on the broadcast, and only then re-enables', async () => {
    const { panel } = await mountWithSetter();
    await choose(panel, 'acceptEdits');
    expect(select(panel).disabled).toBe(true);
    window.dispatchEvent(
      new CustomEvent('permission-mode-changed', {
        detail: { mode: 'acceptEdits', by: 'user' },
      }),
    );
    await settle(panel);
    expect(panel._permissionMode).toBe('acceptEdits');
    expect(panel._permissionModePending).toBe(false);
    expect(select(panel).value).toBe('acceptEdits');
    expect(select(panel).disabled).toBe(false);
  });

  it('follows a collaborator’s change without anyone touching the control', async () => {
    const { panel } = await mountWithSetter();
    window.dispatchEvent(
      new CustomEvent('permission-mode-changed', {
        detail: { mode: 'plan', by: 'someone else' },
      }),
    );
    await settle(panel);
    expect(select(panel).value).toBe('plan');
  });

  it('re-picking the current mode asks nothing', async () => {
    const { panel, setMode } = await mountWithSetter();
    await choose(panel, 'default');
    expect(setMode).not.toHaveBeenCalled();
  });

  it('bypass is confirmed before the engine hears about it', async () => {
    const confirm = vi
      .spyOn(window, 'confirm')
      .mockReturnValue(false);
    try {
      const { panel, setMode } = await mountWithSetter();
      await choose(panel, 'bypassPermissions');
      expect(confirm).toHaveBeenCalledOnce();
      expect(confirm.mock.calls[0][0]).toMatch(/without asking/i);
      expect(setMode).not.toHaveBeenCalled();
      // And the control did not move on the way through.
      expect(select(panel).value).toBe('default');
    } finally {
      confirm.mockRestore();
    }
  });

  it('bypass goes through once confirmed', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const { panel, setMode } = await mountWithSetter();
      await choose(panel, 'bypassPermissions');
      expect(setMode).toHaveBeenCalledWith('bypassPermissions');
    } finally {
      confirm.mockRestore();
    }
  });

  it('a safe mode is not confirmed', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const { panel, setMode } = await mountWithSetter();
      await choose(panel, 'plan');
      expect(confirm).not.toHaveBeenCalled();
      expect(setMode).toHaveBeenCalledWith('plan');
    } finally {
      confirm.mockRestore();
    }
  });

  it('a change with no gesture behind it neither confirms nor sets', async () => {
    // What a browser raises when it restores this control's value on load.
    // Nothing about that is a user asking to turn the permission gate off, and
    // a confirmation drawn from it asks about a change nobody made.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const { panel, setMode } = await mountWithSetter();
      await chooseWithoutGesture(panel, 'bypassPermissions');
      expect(confirm).not.toHaveBeenCalled();
      expect(setMode).not.toHaveBeenCalled();
      // And the control is left telling the truth about the session.
      expect(select(panel).value).toBe('default');
    } finally {
      confirm.mockRestore();
    }
  });

  it('one gesture authorises one change', async () => {
    // The latch is spent by the change it authorised, so a restored event
    // arriving afterwards cannot ride in on it.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    try {
      const { panel, setMode } = await mountWithSetter();
      await choose(panel, 'plan');
      expect(setMode).toHaveBeenCalledTimes(1);
      await chooseWithoutGesture(panel, 'bypassPermissions');
      expect(setMode).toHaveBeenCalledTimes(1);
    } finally {
      confirm.mockRestore();
    }
  });

  it('opts the control out of form-state restoration', async () => {
    // The declarative half of the same guard: the HTML spec exempts an
    // `autocomplete="off"` control from being restored at all.
    const panel = mountPanel();
    await settle(panel);
    expect(select(panel).getAttribute('autocomplete')).toBe('off');
  });
});

// ---------------------------------------------------------------------------
// setPermissionMode failure paths
// ---------------------------------------------------------------------------

describe('setPermissionMode', () => {
  it('says so rather than pretending, with no engine to ask', async () => {
    const panel = mountPanel();
    await settle(panel);
    const { toasts, done } = captureToasts();
    try {
      setPermissionMode(panel, 'acceptEdits');
      await settle(panel);
      expect(toasts).toHaveBeenCalledOnce();
      expect(toasts.mock.calls[0][0].detail.message).toMatch(/not connected/i);
      expect(panel._permissionModePending).toBeFalsy();
    } finally {
      done();
    }
  });

  it('reports the engine’s reason when the change is restricted', async () => {
    const { panel } = await mountWithSetter(async () => ({
      error: 'restricted',
      reason: 'set_permission_mode is localhost-only',
    }));
    const { toasts, done } = captureToasts();
    try {
      setPermissionMode(panel, 'acceptEdits');
      await settle(panel);
      const detail = toasts.mock.calls[0][0].detail;
      expect(detail.type).toBe('warning');
      expect(detail.message).toBe('set_permission_mode is localhost-only');
      // The control comes back so the user can see the mode is unchanged.
      expect(panel._permissionModePending).toBe(false);
      expect(select(panel).disabled).toBe(false);
    } finally {
      done();
    }
  });

  it('falls back to a sentence of its own when restricted with no reason', async () => {
    const { panel } = await mountWithSetter(async () => ({
      error: 'restricted',
    }));
    const { toasts, done } = captureToasts();
    try {
      setPermissionMode(panel, 'acceptEdits');
      await settle(panel);
      expect(toasts.mock.calls[0][0].detail.message).toMatch(
        /only the host/i,
      );
    } finally {
      done();
    }
  });

  it('surfaces any other error envelope verbatim', async () => {
    const { panel } = await mountWithSetter(async () => ({
      error: 'unknown permission mode: turbo',
    }));
    const { toasts, done } = captureToasts();
    try {
      setPermissionMode(panel, 'turbo');
      await settle(panel);
      expect(toasts.mock.calls[0][0].detail.message).toBe(
        'unknown permission mode: turbo',
      );
      expect(panel._permissionModePending).toBe(false);
    } finally {
      done();
    }
  });

  it('a failed call is an error, not a warning', async () => {
    const { panel } = await mountWithSetter(async () => {
      throw new Error('socket closed');
    });
    const { toasts, done } = captureToasts();
    try {
      setPermissionMode(panel, 'acceptEdits');
      await settle(panel);
      const detail = toasts.mock.calls[0][0].detail;
      expect(detail.type).toBe('error');
      expect(detail.message).toContain('socket closed');
      expect(panel._permissionModePending).toBe(false);
    } finally {
      done();
    }
  });

  it('a success leaves the control waiting for the broadcast', async () => {
    // Clearing the flag on the reply would re-enable the control a frame
    // before the mode it displays caught up.
    const { panel } = await mountWithSetter(async () => ({ mode: 'plan' }));
    setPermissionMode(panel, 'plan');
    await settle(panel);
    expect(panel._permissionModePending).toBe(true);
    expect(select(panel).disabled).toBe(true);
    expect(panel._permissionMode).toBe('default');
  });
});

// ---------------------------------------------------------------------------
// Who may change it
// ---------------------------------------------------------------------------

describe('probeModeAuthority', () => {
  it('takes the control away from a participant', async () => {
    publishFakeRpc({
      'Collab.get_collab_role': async () => ({
        is_localhost: false,
        role: 'participant',
      }),
    });
    const panel = mountPanel();
    await settle(panel);
    await probeModeAuthority(panel);
    await settle(panel);
    expect(panel._canSetPermissionMode).toBe(false);
    expect(select(panel).disabled).toBe(true);
  });

  it('leaves it with the host', async () => {
    publishFakeRpc({
      'Collab.get_collab_role': async () => ({
        is_localhost: true,
        role: 'host',
      }),
      'ClaudeCodeService.set_permission_mode': async () => ({ mode: 'plan' }),
    });
    const panel = mountPanel();
    await settle(panel);
    await probeModeAuthority(panel);
    await settle(panel);
    expect(panel._canSetPermissionMode).toBe(true);
    expect(select(panel).disabled).toBe(false);
  });

  it('assumes host when the probe returns an error envelope', async () => {
    // The engine enforces the real gate, so guessing host costs a rejected
    // call rather than an unauthorised change.
    publishFakeRpc({
      'Collab.get_collab_role': async () => ({ error: 'no collab session' }),
    });
    const panel = mountPanel();
    await settle(panel);
    panel._canSetPermissionMode = false;
    await probeModeAuthority(panel);
    expect(panel._canSetPermissionMode).toBe(true);
  });

  it('assumes host when there is no collab service at all', async () => {
    publishFakeRpc({});
    const panel = mountPanel();
    await settle(panel);
    panel._canSetPermissionMode = false;
    await probeModeAuthority(panel);
    expect(panel._canSetPermissionMode).toBe(true);
  });
});

describe('onRoleChanged', () => {
  it('takes authority away when collab is switched on underneath us', async () => {
    const { panel } = await mountWithSetter();
    onRoleChanged(panel, { detail: { is_localhost: false } });
    await settle(panel);
    expect(panel._canSetPermissionMode).toBe(false);
    expect(select(panel).disabled).toBe(true);
  });

  it('gives it back when collab is switched off', async () => {
    const { panel } = await mountWithSetter();
    panel._canSetPermissionMode = false;
    onRoleChanged(panel, { detail: { is_localhost: true } });
    await settle(panel);
    expect(panel._canSetPermissionMode).toBe(true);
    expect(select(panel).disabled).toBe(false);
  });

  it('ignores a broadcast that does not carry the flag', async () => {
    const { panel } = await mountWithSetter();
    panel._canSetPermissionMode = false;
    for (const detail of [undefined, null, {}, { is_localhost: 'yes' }]) {
      onRoleChanged(panel, detail === undefined ? undefined : { detail });
      expect(panel._canSetPermissionMode).toBe(false);
    }
  });
});
