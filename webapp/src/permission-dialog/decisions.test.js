import { alwaysAllowTooltip } from './constants.js';

describe('the always-allow tooltip tells the truth about where the rule goes', () => {
  // Caught in a browser on 2026-09-05, not by a test. The Antigravity
  // dialog rendered the Claude tooltip — "It applies to the claude CLI in
  // this repository too" — for a rule that goes into a file the `claude`
  // CLI has never heard of. A misleading sentence on a permission control
  // is worse than a missing one, because the user acts on it.
  //
  // The call site had `rule.session ? A : B`, so a third destination fell
  // through to B and asserted something untrue. These pin all three.

  it('says AIC-DC keeps it, for an Antigravity rule', () => {
    const tip = alwaysAllowTooltip({ destination: 'aicDcRules' });
    expect(tip).toContain('AIC-DC keeps');
    expect(tip).toContain('not to the claude CLI');
  });

  it('still says the CLI reads it, for a Claude settings rule', () => {
    // The control: a fix that told everyone "not the claude CLI" would be
    // wrong in the other direction, on the engine that ships.
    expect(alwaysAllowTooltip({ destination: 'localSettings' }))
      .toContain('claude CLI');
  });

  it('still says session-only for a session rule', () => {
    expect(alwaysAllowTooltip({ session: true, destination: 'session' }))
      .toContain('rest of this session only');
  });

  it('a rule with no destination reads as the settings-file case', () => {
    // Unchanged behaviour, asserted so the fallback is a decision.
    expect(alwaysAllowTooltip({})).toContain('claude CLI');
    expect(alwaysAllowTooltip(null)).toContain('claude CLI');
  });
});
