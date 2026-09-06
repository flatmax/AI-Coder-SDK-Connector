import { alwaysAllowTooltip } from './constants.js';
import { describeRule } from './queue.js';

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

describe('the tooltip the button actually renders', () => {
  // The four above passed for a day while the dialog went on showing the
  // wrong sentence, because every one of them fed `alwaysAllowTooltip` the
  // raw server rule and the render fed it a *described* one — whose
  // `destination` had already been replaced by the filename a person
  // reads. Found in a browser on 2026-09-06, on the same Antigravity
  // dialog the tests above were written for on 2026-09-05.
  //
  // So these go through `describeRule`, which is the shape the button has.
  // A test of a function is not a test of the thing on screen.

  const rule = (destination) => ({
    tool_name: 'replace_file_content',
    rule_content: 'calc.py',
    behavior: 'allow',
    origin: 'aic-dc',
    destination,
  });

  it('says AIC-DC keeps it, for an Antigravity rule', () => {
    const described = describeRule(rule('aicDcRules'));
    expect(described.tooltip).toContain('AIC-DC keeps');
    expect(described.tooltip).toContain('not to the claude CLI');
  });

  it('does not survive the filename the chip shows', () => {
    // The regression itself, stated as an assertion: the destination the
    // chip renders and the destination the tooltip is chosen by are two
    // different values of one field, and reading the second off the first
    // is what broke.
    const described = describeRule(rule('aicDcRules'));
    expect(described.destination).toBe('~/.config/aic-dc/antigravity-rules.json');
    expect(described.tooltip).not.toContain('applies to the claude CLI');
  });

  it('still says the CLI reads it, for a Claude settings rule', () => {
    expect(describeRule(rule('localSettings')).tooltip).toContain('claude CLI');
  });

  it('still says session-only for a session rule', () => {
    const described = describeRule(rule('session'));
    expect(described.session).toBe(true);
    expect(described.tooltip).toContain('rest of this session only');
  });
});
