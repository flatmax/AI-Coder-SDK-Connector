// Tests for the engine-health banner.
//
// `hasHealthProblem` and `healthKey` are pure and tested in
// isolation. The banner itself goes through a mounted
// ChatPanel, because what it renders is a function of three
// pieces of panel state (`_engineHealth`, `_healthDismissed`,
// `_healthForced`) and the `engine-health` event that writes
// the first of them.

import { describe, expect, it } from 'vitest';

import { hasHealthProblem, healthKey, revealHealth } from './health-banner.js';
import { mountPanel, pushEvent, settle } from './test-helpers.js';

/** A clean payload, in the shape `EngineHealth.to_dict()` sends. */
function health(overrides = {}) {
  return {
    connected: true,
    cli_path: '/usr/bin/claude',
    cli_version: '2.0.31',
    cli_source: 'path',
    sdk_version: '0.2.137',
    sdk_cli_pin: '2.0.30',
    version_warning: null,
    credential_source: 'subscription',
    auth_warning: null,
    mcp: [],
    mirror_gaps: 0,
    mirror_gaps_escalated: false,
    last_error: null,
    ...overrides,
  };
}

function banner(panel) {
  return panel.shadowRoot.querySelector('.health-banner');
}

async function report(panel, payload) {
  pushEvent('engine-health', payload);
  await settle(panel);
}

// ---------------------------------------------------------------
// hasHealthProblem — pure
// ---------------------------------------------------------------

describe('hasHealthProblem', () => {
  it('a clean payload has nothing to say', () => {
    expect(hasHealthProblem(health())).toBe(false);
  });

  it('each of the four fields is enough on its own', () => {
    expect(hasHealthProblem(health({ mirror_gaps: 1 }))).toBe(true);
    expect(hasHealthProblem(health({ last_error: 'boom' }))).toBe(true);
    expect(hasHealthProblem(health({ version_warning: 'skew' }))).toBe(true);
    expect(hasHealthProblem(health({ auth_warning: 'api key' }))).toBe(true);
  });

  it('a disconnected engine with no error is not a fault', () => {
    // `connected: false` is the normal state of a freshly loaded page:
    // the CLI starts on the first prompt. A session that *loses* its
    // engine sets `last_error` on the way out, and that case is above.
    expect(hasHealthProblem(health({ connected: false }))).toBe(false);
  });

  it('ignores counts and warnings that are not what they claim', () => {
    expect(hasHealthProblem(health({ mirror_gaps: 0 }))).toBe(false);
    expect(hasHealthProblem(health({ mirror_gaps: -2 }))).toBe(false);
    expect(hasHealthProblem(health({ mirror_gaps: 'lots' }))).toBe(false);
    expect(hasHealthProblem(health({ last_error: '' }))).toBe(false);
    expect(hasHealthProblem(health({ version_warning: {} }))).toBe(false);
    expect(hasHealthProblem(null)).toBe(false);
    expect(hasHealthProblem('health')).toBe(false);
  });
});

// ---------------------------------------------------------------
// healthKey — pure
// ---------------------------------------------------------------

describe('healthKey', () => {
  it('is the same for the same problems', () => {
    expect(healthKey(health({ version_warning: 'skew' }))).toBe(
      healthKey(health({ version_warning: 'skew', cli_version: '9.9.9' })),
    );
  });

  it('changes when a second append fails', () => {
    // The count is in the key on purpose: a second gap is new
    // information about how bad it is, so a dismissal of the first
    // does not cover it.
    expect(healthKey(health({ mirror_gaps: 1 }))).not.toBe(
      healthKey(health({ mirror_gaps: 2 })),
    );
  });

  it('changes when the count crosses the tolerance', () => {
    // Same count, different verdict: `mirror_gap_tolerance` can be
    // lowered in app.json between two reports of the same three gaps,
    // and "this repo has given up on the mirror" is news.
    expect(healthKey(health({ mirror_gaps: 3 }))).not.toBe(
      healthKey(health({ mirror_gaps: 3, mirror_gaps_escalated: true })),
    );
  });

  it('changes when a different thing goes wrong', () => {
    expect(healthKey(health({ last_error: 'boom' }))).not.toBe(
      healthKey(health({ auth_warning: 'boom' })),
    );
  });

  it('answers for a payload it never got', () => {
    expect(healthKey(null)).toBe('');
    expect(healthKey(undefined)).toBe('');
  });
});

// ---------------------------------------------------------------
// The banner
// ---------------------------------------------------------------

describe('ChatPanel health banner', () => {
  it('says nothing until something is wrong', async () => {
    const p = mountPanel();
    await settle(p);
    expect(banner(p)).toBeNull();
    await report(p, health());
    expect(banner(p)).toBeNull();
  });

  it('names the mirror gap and what it costs', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 1 }));
    const text = banner(p).textContent;
    expect(text).toContain('One turn was not appended');
    expect(text).toContain('resumed');
  });

  it('counts the gaps', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 3 }));
    expect(banner(p).textContent).toContain('3 turns were not appended');
  });

  it('stays amber while the gaps are still within tolerance', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 2 }));
    expect(banner(p).classList.contains('health-banner-bad')).toBe(false);
    expect(banner(p).textContent).not.toContain('more than this repo tolerates');
  });

  it('escalates when the engine says the tolerance is past', async () => {
    // The comparison is the engine's — `EngineHealth._escalated()` reads
    // `app.json`'s `history.mirror_gap_tolerance`. The browser is told
    // the answer, not the threshold, so there is only one owner of it.
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 4, mirror_gaps_escalated: true }));
    expect(banner(p).classList.contains('health-banner-bad')).toBe(true);
    const text = banner(p).textContent;
    expect(text).toContain('4 turns were not appended');
    expect(text).toContain('more than this repo tolerates');
    expect(text).toContain('.ac-dc4/sessions/');
  });

  it('speaks again when a dismissed warning escalates', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 3 }));
    p.shadowRoot.querySelector('.health-dismiss').click();
    await settle(p);
    expect(banner(p)).toBeNull();
    // Same three gaps, but the repo's tolerance was lowered under it.
    await report(p, health({ mirror_gaps: 3, mirror_gaps_escalated: true }));
    expect(banner(p)).not.toBeNull();
    expect(banner(p).textContent).toContain('more than this repo tolerates');
  });

  it('an escalated flag on its own is not a problem', async () => {
    // Nothing sends this — `_escalated()` cannot be true with no gaps —
    // but the banner must not invent a red strip out of a stray flag.
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps_escalated: true }));
    expect(banner(p)).toBeNull();
  });

  it('shows the engine, version and credential warnings together', async () => {
    const p = mountPanel();
    await settle(p);
    await report(
      p,
      health({
        last_error: 'connect timed out after 60s',
        version_warning: 'CLI 2.0.31 is newer than the SDK pins',
        auth_warning: 'ANTHROPIC_API_KEY is set',
      }),
    );
    const lines = [...banner(p).querySelectorAll('.health-line')].map(
      (el) => el.textContent.trim(),
    );
    expect(lines.some((l) => l.includes('connect timed out'))).toBe(true);
    expect(lines.some((l) => l.includes('newer than the SDK pins'))).toBe(true);
    expect(lines.some((l) => l.includes('ANTHROPIC_API_KEY'))).toBe(true);
  });

  it('carries the engine readout under the warning', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ version_warning: 'skew' }));
    const engine = p.shadowRoot.querySelector('.health-engine');
    expect(engine.textContent).toContain('CLI 2.0.31');
    expect(engine.textContent).toContain('subscription');
  });

  it('leaves out a readout it does not have', async () => {
    const p = mountPanel();
    await settle(p);
    await report(
      p,
      health({
        last_error: 'boom',
        cli_version: 'unknown',
        credential_source: 'unknown',
      }),
    );
    expect(p.shadowRoot.querySelector('.health-engine')).toBeNull();
  });

  it('dismisses, and stays dismissed for the same problem', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ version_warning: 'skew' }));
    p.shadowRoot.querySelector('.health-dismiss').click();
    await settle(p);
    expect(banner(p)).toBeNull();
    // The same warning again — the engine reports health on every
    // reconnect, and a re-run of the same sentence is not news.
    await report(p, health({ version_warning: 'skew' }));
    expect(banner(p)).toBeNull();
  });

  it('comes back when something else goes wrong', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ version_warning: 'skew' }));
    p.shadowRoot.querySelector('.health-dismiss').click();
    await settle(p);
    await report(p, health({ version_warning: 'skew', mirror_gaps: 1 }));
    expect(banner(p)).not.toBeNull();
    expect(banner(p).textContent).toContain('One turn was not appended');
  });

  it('opens on request even with nothing wrong', async () => {
    // What the turn footer's mirror-gap marker does. The engine may
    // since have restarted, and a link that lands on nothing is the
    // thing this banner exists to end.
    const p = mountPanel();
    await settle(p);
    await report(p, health());
    revealHealth(p);
    await settle(p);
    expect(banner(p).textContent).toContain('nothing wrong');
    expect(banner(p).classList.contains('health-banner-ok')).toBe(true);
  });

  it('says so when the engine has not reported at all', async () => {
    const p = mountPanel();
    await settle(p);
    revealHealth(p);
    await settle(p);
    expect(banner(p).textContent).toContain('has not reported its health yet');
  });

  it('a forced banner closes on the same dismiss button', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health());
    revealHealth(p);
    await settle(p);
    p.shadowRoot.querySelector('.health-dismiss').click();
    await settle(p);
    expect(banner(p)).toBeNull();
  });

  it('a request overrides an earlier dismissal', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 1 }));
    p.shadowRoot.querySelector('.health-dismiss').click();
    await settle(p);
    expect(banner(p)).toBeNull();
    revealHealth(p);
    await settle(p);
    expect(banner(p).textContent).toContain('One turn was not appended');
  });

  it('ignores an event that carries no payload', async () => {
    const p = mountPanel();
    await settle(p);
    await report(p, health({ mirror_gaps: 1 }));
    for (const bad of [null, undefined, 'health', 42, ['gap']]) {
      await report(p, bad);
    }
    // The last good payload is still what the banner says.
    expect(banner(p).textContent).toContain('One turn was not appended');
  });

  it('reads the envelope the shell actually sends', async () => {
    // `engineHealth(data)` dispatches `detail: data` with no
    // `{requestId, data}` wrapper — engine health is session-wide, so
    // there is no request id to pair it with. A handler expecting the
    // wrapper drops every payload.
    const p = mountPanel();
    await settle(p);
    await report(p, { data: health({ mirror_gaps: 1 }) });
    expect(banner(p)).toBeNull();
    await report(p, health({ mirror_gaps: 1 }));
    expect(banner(p)).not.toBeNull();
  });

  it('stops listening on disconnect', async () => {
    const p = mountPanel();
    await settle(p);
    p.remove();
    await report(p, health({ mirror_gaps: 1 }));
    expect(p._engineHealth).toBeNull();
  });
});
