// Tests for the reconnect gate — specs5/plan-ag/risks.md § AG-R-20.
//
// The recorded tripwire is the first test here: connect, hold the
// `get_current_state` response open, deliver two events, then release the
// response — and assert both events survive in the rendered state rather than
// being overwritten by the baseline they arrived ahead of.
//
// The rest of the file is about the gate not becoming a fault of its own. A
// gate that can wedge is worse than the race it closes, so every way out is
// asserted: a snapshot that fails, a snapshot that never arrives, a flood that
// outruns the buffer, and a handler that throws on replay.

import {
  afterEach, describe, expect, it, vi,
} from 'vitest';
import {
  installAppShellTestSetup, mountShell, tick,
} from './test-helpers.js';
import { AppShell } from './index.js';
import {
  GATED_EVENTS, UNGATED_METHODS, GATE_MAX_EVENTS, GATE_MAX_MS,
  heldCount, holdEvents, installEventGate, isHolding, releaseEvents,
} from './event-gate.js';

/** Listen for the window events the tests below assert on, in order. */
function recordOrder(names) {
  const order = [];
  const listeners = names.map(([domEvent, label]) => {
    const fn = () => order.push(label);
    window.addEventListener(domEvent, fn);
    return [domEvent, fn];
  });
  return {
    order,
    stop() {
      for (const [domEvent, fn] of listeners) {
        window.removeEventListener(domEvent, fn);
      }
    },
  };
}

describe('AppShell reconnect gate (AG-R-20)', () => {
  installAppShellTestSetup();

  let _warn;
  afterEach(() => {
    if (_warn) {
      _warn.mockRestore();
      _warn = undefined;
    }
    vi.useRealTimers();
  });

  describe('the recorded tripwire', () => {
    it('holds live events until the snapshot lands, then replays them', async () => {
      const shell = mountShell();
      await shell.updateComplete;

      let land;
      const snapshot = new Promise((resolve) => { land = resolve; });
      shell.call = { 'ClaudeCodeService.get_current_state': () => snapshot };

      const rec = recordOrder([
        ['state-loaded', 'snapshot'],
        ['tool-use', 'toolUse'],
        ['subagent-event', 'subagentEvent'],
      ]);

      // Connect. This asks for the snapshot and holds the gate behind it.
      shell.setupDone();
      expect(isHolding(shell)).toBe(true);

      // Two events overtake the request in flight, which is the race.
      expect(shell.toolUse('r1', { id: 't1' })).toBe(true);
      shell.subagentEvent('r1', { agent_id: 'a1' });
      expect(rec.order).toEqual([]);
      expect(heldCount(shell)).toBe(2);

      land({ messages: [] });
      await tick();

      // Both survive, and both land *after* the baseline rather than under it.
      expect(rec.order).toEqual(['snapshot', 'toolUse', 'subagentEvent']);
      expect(isHolding(shell)).toBe(false);
      rec.stop();
    });
  });

  describe('while held', () => {
    it('does not run the handler, and still acknowledges the push', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      holdEvents(shell);

      const rec = recordOrder([['engine-changed', 'engineChanged']]);
      expect(shell.engineChanged({ engine: 'claude' })).toBe(true);
      expect(rec.order).toEqual([]);
      rec.stop();
    });

    it('replays in arrival order, not in handler order', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      holdEvents(shell);

      const rec = recordOrder([
        ['tool-use', 'toolUse'],
        ['subagent-event', 'subagentEvent'],
      ]);
      shell.subagentEvent('r1', {});
      shell.toolUse('r1', {});
      shell.subagentEvent('r1', {});

      releaseEvents(shell);
      expect(rec.order).toEqual(['subagentEvent', 'toolUse', 'subagentEvent']);
      rec.stop();
    });
  });

  describe('every way out of the gate', () => {
    it('opens when the snapshot fails, rather than wedging', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      shell.call = {
        'ClaudeCodeService.get_current_state': () =>
          Promise.reject(new Error('socket died mid-snapshot')),
      };

      const rec = recordOrder([['tool-use', 'toolUse']]);
      shell.setupDone();
      shell.toolUse('r1', {});
      expect(rec.order).toEqual([]);

      await tick();
      // `fetchCurrentState` swallows the rejection itself, but the gate must
      // not depend on that: `finally` is what makes this hold regardless.
      expect(isHolding(shell)).toBe(false);
      expect(rec.order).toEqual(['toolUse']);
      rec.stop();
    });

    it('opens even when the snapshot promise rejects outright', async () => {
      const shell = mountShell();
      await shell.updateComplete;

      // `fetchCurrentState` swallows its own errors today, so the test above
      // would pass even if the gate released on `then`. This one takes that
      // prop away: the promise `setupDone` chains onto actually rejects.
      shell._fetchCurrentState = () => Promise.reject(new Error('no snapshot'));

      const rec = recordOrder([['tool-use', 'toolUse']]);
      shell.setupDone();
      shell.toolUse('r1', {});
      expect(rec.order).toEqual([]);

      await tick();
      expect(isHolding(shell)).toBe(false);
      expect(rec.order).toEqual(['toolUse']);
      rec.stop();
    });

    it('opens on its own deadline when the snapshot never arrives', async () => {
      _warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const shell = mountShell();
      await shell.updateComplete;
      vi.useFakeTimers();
      holdEvents(shell);

      const rec = recordOrder([['tool-use', 'toolUse']]);
      shell.toolUse('r1', {});
      expect(rec.order).toEqual([]);

      vi.advanceTimersByTime(GATE_MAX_MS + 1);
      expect(isHolding(shell)).toBe(false);
      expect(rec.order).toEqual(['toolUse']);
      expect(_warn).toHaveBeenCalled();
      rec.stop();
    });

    it('releases rather than drops when the buffer is outrun', async () => {
      _warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
      const shell = mountShell();
      await shell.updateComplete;
      holdEvents(shell);

      let seen = 0;
      const onToolUse = () => { seen += 1; };
      window.addEventListener('tool-use', onToolUse);

      // One more than the gate will hold. The overflowing event must arrive
      // too: a dropped event is a permanently missing row that nothing
      // reports, which is the failure this gate exists to avoid repeating.
      for (let i = 0; i <= GATE_MAX_EVENTS; i += 1) {
        shell.toolUse('r1', { i });
      }

      expect(isHolding(shell)).toBe(false);
      expect(seen).toBe(GATE_MAX_EVENTS + 1);
      expect(_warn).toHaveBeenCalled();
      window.removeEventListener('tool-use', onToolUse);
    });

    it('does not strand the events behind a handler that throws', () => {
      const _err = vi.spyOn(console, 'error').mockImplementation(() => {});

      // Against a plain host rather than the shell, deliberately. The shell's
      // handlers end in `dispatchEvent`, and the DOM swallows a listener that
      // throws before the gate could ever see it — so a test written that way
      // passes without exercising anything. What has to be isolated is the
      // *handler body*, and this is the smallest host that can have one fail.
      const ran = [];
      const host = {
        toolUse() { throw new Error('bad reducer'); },
        subagentEvent() { ran.push('subagentEvent'); },
      };
      installEventGate(host, ['toolUse', 'subagentEvent']);
      holdEvents(host);

      host.toolUse();
      host.subagentEvent();
      expect(ran).toEqual([]);

      releaseEvents(host);
      expect(ran).toEqual(['subagentEvent']);
      expect(_err).toHaveBeenCalledWith(
        '[event-gate] replaying toolUse failed:',
        expect.any(Error),
      );
      _err.mockRestore();
    });

    it('drops rather than replays into a shell that is going away', async () => {
      const shell = mountShell();
      await shell.updateComplete;
      holdEvents(shell);

      const rec = recordOrder([['tool-use', 'toolUse']]);
      shell.toolUse('r1', {});
      expect(heldCount(shell)).toBe(1);

      shell.remove();

      // Teardown disposes rather than releases: replaying reducers into a
      // disconnected tree corrects state nobody is looking at.
      expect(heldCount(shell)).toBe(0);
      expect(rec.order).toEqual([]);
      rec.stop();
    });
  });

  describe('the gate cannot be bypassed by adding a handler', () => {
    it('classifies every public method on the shell', () => {
      // The gated list is hand-written, because reflection cannot tell a
      // server push from `render`. This is the test that keeps it honest: a
      // new server-push handler fails here until it is classified, so the
      // bypass is loud instead of silent.
      const gated = new Set(GATED_EVENTS);
      const ungated = new Set(UNGATED_METHODS);
      const unclassified = Object.getOwnPropertyNames(AppShell.prototype)
        .filter((name) => !name.startsWith('_'))
        .filter((name) => typeof AppShell.prototype[name] === 'function')
        .filter((name) => !gated.has(name) && !ungated.has(name));

      expect(unclassified).toEqual([]);
    });

    it('leaves the prototype untouched, so the RPC surface is unchanged', async () => {
      // jrpc-oo's ExposeClass enumerates the *prototype* to decide what to
      // expose, and resolves `classToExpose[name]` per call. The gate relies
      // on both halves: own properties win the dispatch, and the prototype
      // still advertises the same methods to the server.
      const shell = mountShell();
      await shell.updateComplete;

      for (const name of GATED_EVENTS) {
        expect(Object.prototype.hasOwnProperty.call(shell, name)).toBe(true);
        expect(typeof AppShell.prototype[name]).toBe('function');
        expect(shell[name]).not.toBe(AppShell.prototype[name]);
      }
    });
  });
});
