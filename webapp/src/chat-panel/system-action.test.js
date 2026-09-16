// The escalation a system card offers — `renderSystemAction` / `runSystemAction`.
//
// Context, because a button in a transcript is unusual enough to need it.
// `specs5/plan-ag/risks.md` AG-R-16 was raised to high on 2026-09-12 after a
// probe showed ⏹ can be outlasted on the `agy` transport: a `Stop` hook this
// app does not own answers `continue`, the loop the gate terminated is put
// back, and the two ping-pong at roughly two model invocations a second —
// bounded only by `--print-timeout`, which the session sets to 12h.
//
// The remaining lever is restarting the engine, and it is *offered* rather
// than taken because a restart ends the session the user is holding. AG-19
// demoted the process kill to an explicit, user-initiated escalation for that
// reason, and a threshold firing on its own would eventually fire on a
// legitimate turn waiting in a permission dialog.
//
// So these tests are about a control that must work the one time it is
// pressed, by someone whose stop has already been ignored once.

import { render } from 'lit';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderSystemAction, runSystemAction } from './rendering.js';

const hosts = [];

function draw(template) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  hosts.push(host);
  render(template, host);
  return host;
}

afterEach(() => {
  while (hosts.length) hosts.pop().remove();
});

function stubPanel(props = {}) {
  return {
    _systemActionPending: null,
    requestUpdate: vi.fn(),
    _emitToast: vi.fn(),
    rpcExtract: vi.fn().mockResolvedValue({ status: 'ok' }),
    ...props,
  };
}

const ACTION = {
  label: 'Force restart the engine',
  method: 'ClaudeCodeService.restart_session',
};

describe('renderSystemAction', () => {
  it('draws nothing for a card that offers no escalation', () => {
    // Which is every card but one. A button on an ordinary notice would be
    // an affordance with nothing behind it.
    for (const msg of [null, {}, { system_action: null }, { system_action: {} }]) {
      expect(renderSystemAction(stubPanel(), msg)).toBe('');
    }
  });

  it('draws the label the notice chose', () => {
    const host = draw(renderSystemAction(stubPanel(), { system_action: ACTION }));
    const button = host.querySelector('.system-action-button');
    expect(button.textContent.trim()).toBe('Force restart the engine');
    expect(button.disabled).toBe(false);
  });

  it('disables itself and says so while the restart is in flight', () => {
    // `restart_session` takes a few seconds to rebuild the harness. A button
    // that still looks live invites a second press, which would tear down the
    // session the first one is rebuilding.
    const panel = stubPanel({ _systemActionPending: ACTION.method });
    const host = draw(renderSystemAction(panel, { system_action: ACTION }));
    const button = host.querySelector('.system-action-button');
    expect(button.disabled).toBe(true);
    expect(button.textContent.trim()).toBe('Restarting…');
  });

  it('is not disabled by a different card\'s action being in flight', () => {
    const panel = stubPanel({ _systemActionPending: 'Something.else' });
    const host = draw(renderSystemAction(panel, { system_action: ACTION }));
    expect(host.querySelector('.system-action-button').disabled).toBe(false);
  });
});

describe('runSystemAction', () => {
  it('calls the method the notice named', async () => {
    const panel = stubPanel();
    await runSystemAction(panel, ACTION);
    expect(panel.rpcExtract).toHaveBeenCalledWith('ClaudeCodeService.restart_session');
    expect(panel._emitToast).toHaveBeenCalledWith('The engine was restarted', 'info');
  });

  it('clears the pending flag so the button comes back', async () => {
    const panel = stubPanel();
    await runSystemAction(panel, ACTION);
    expect(panel._systemActionPending).toBeNull();
  });

  it('reports a failure rather than doing nothing visible', async () => {
    // This is offered to someone whose stop has already been ignored once.
    // A button that silently fails would be the second thing that failed
    // them without saying so.
    const panel = stubPanel({
      rpcExtract: vi.fn().mockRejectedValue(new Error('engine is gone')),
    });
    await runSystemAction(panel, ACTION);
    expect(panel._emitToast).toHaveBeenCalledWith(
      'Could not restart the engine: engine is gone',
      'error',
    );
    expect(panel._systemActionPending).toBeNull();
  });

  it('survives a panel with no toast channel', async () => {
    // `_emitToast` is optional on the stub panels other renderers are tested
    // with, and a missing one must not turn a restart into an exception.
    const panel = stubPanel({ _emitToast: undefined });
    await expect(runSystemAction(panel, ACTION)).resolves.toBeUndefined();
    expect(panel._systemActionPending).toBeNull();
  });

  it('clicking the button is what runs it', async () => {
    // The wiring, end to end: everything above calls `runSystemAction`
    // directly, which would keep passing if the template had no handler.
    const panel = stubPanel();
    const host = draw(renderSystemAction(panel, { system_action: ACTION }));
    host.querySelector('.system-action-button').click();
    await Promise.resolve();
    await Promise.resolve();
    expect(panel.rpcExtract).toHaveBeenCalledWith('ClaudeCodeService.restart_session');
  });
});
