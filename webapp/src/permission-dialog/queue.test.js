// Tests for the permission dialog's pure functions.
//
// Each one encodes an invariant from
// specs5/5-webapp/permission-dialog.md that is cheaper to pin down
// without a DOM than through the rendered component: queue ordering,
// where default focus lands, what the countdown says, and what the
// "always allow" button is allowed to claim.

import { describe, expect, it } from 'vitest';

import {
  answersComplete,
  arrivalAnnouncement,
  countdownUrgency,
  defaultDenyReason,
  defaultFocusTarget,
  describeMode,
  describeRule,
  expiryMs,
  formatCountdown,
  headerTarget,
  interactQuestions,
  offersAlwaysAllow,
  orderQueue,
  secondsRemaining,
} from './queue.js';

// ---------------------------------------------------------------------------
// expiryMs
// ---------------------------------------------------------------------------

describe('expiryMs', () => {
  it('passes a finite number straight through', () => {
    expect(expiryMs(1_700_000_000_000)).toBe(1_700_000_000_000);
  });

  it('parses an ISO timestamp', () => {
    expect(expiryMs('2026-08-14T00:00:00.000Z')).toBe(
      Date.parse('2026-08-14T00:00:00.000Z'),
    );
  });

  it('treats an unparseable string as infinitely far away', () => {
    // Infinity rather than 0 or NaN so a malformed timestamp sorts LAST.
    // Sorting it first would let a broken payload jump the queue ahead of
    // a request that really is about to expire.
    expect(expiryMs('not a date')).toBe(Infinity);
  });

  it('treats missing, empty, and non-finite values as infinitely far away', () => {
    expect(expiryMs(null)).toBe(Infinity);
    expect(expiryMs(undefined)).toBe(Infinity);
    expect(expiryMs('')).toBe(Infinity);
    expect(expiryMs(NaN)).toBe(Infinity);
    expect(expiryMs(Infinity)).toBe(Infinity);
  });
});

// ---------------------------------------------------------------------------
// orderQueue
// ---------------------------------------------------------------------------

describe('orderQueue', () => {
  const entry = (id, expires, arrivedAt) => ({
    payload: { permission_id: id, expires_at: expires },
    arrivedAt,
  });

  it('answers the request closest to timing out first', () => {
    const ordered = orderQueue([
      entry('late', '2026-08-14T00:05:00Z', 1),
      entry('soon', '2026-08-14T00:01:00Z', 2),
      entry('middle', '2026-08-14T00:03:00Z', 3),
    ]);
    expect(ordered.map((e) => e.payload.permission_id)).toEqual([
      'soon', 'middle', 'late',
    ]);
  });

  it('breaks ties on arrival order, not on id', () => {
    const same = '2026-08-14T00:01:00Z';
    const ordered = orderQueue([
      entry('c', same, 3),
      entry('a', same, 1),
      entry('b', same, 2),
    ]);
    expect(ordered.map((e) => e.payload.permission_id)).toEqual(['a', 'b', 'c']);
  });

  it('sorts a request with no parseable expiry last', () => {
    const ordered = orderQueue([
      entry('no-expiry', null, 1),
      entry('has-expiry', '2026-08-14T00:09:00Z', 2),
    ]);
    expect(ordered.map((e) => e.payload.permission_id)).toEqual([
      'has-expiry', 'no-expiry',
    ]);
  });

  it('tolerates an entry with no payload at all', () => {
    expect(() => orderQueue([{ arrivedAt: 1 }, entry('a', null, 2)])).not.toThrow();
  });

  it('returns a new array rather than sorting in place', () => {
    const input = [
      entry('late', '2026-08-14T00:05:00Z', 1),
      entry('soon', '2026-08-14T00:01:00Z', 2),
    ];
    const ordered = orderQueue(input);
    expect(ordered).not.toBe(input);
    expect(input[0].payload.permission_id).toBe('late');
  });
});

// ---------------------------------------------------------------------------
// defaultFocusTarget
// ---------------------------------------------------------------------------

describe('defaultFocusTarget', () => {
  it('lands on Deny for an MCP tool', () => {
    // Third-party code: the class alone is enough to move focus.
    expect(defaultFocusTarget({ tool_class: 'mcp' })).toBe('deny');
  });

  it('lands on Deny for a command flagged as deleting or networked', () => {
    expect(defaultFocusTarget({
      tool_class: 'exec',
      command: { flags: ['deletes'] },
    })).toBe('deny');
    expect(defaultFocusTarget({
      tool_class: 'exec',
      command: { flags: ['writes', 'network'] },
    })).toBe('deny');
  });

  it('lands on Allow for a command whose only flag is advisory-benign', () => {
    expect(defaultFocusTarget({
      tool_class: 'exec',
      command: { flags: ['writes'] },
    })).toBe('allow');
  });

  it('lands on Allow for an edit', () => {
    expect(defaultFocusTarget({ tool_class: 'write' })).toBe('allow');
  });

  it('lands on Allow for a question even when flags are present', () => {
    // A question is not a risk; there is nothing to be cautious about in
    // answering one, and moving focus off Answer would just be friction.
    expect(defaultFocusTarget({
      tool_class: 'interact',
      command: { flags: ['deletes'] },
    })).toBe('allow');
  });

  it('fails safe with no payload', () => {
    expect(defaultFocusTarget(null)).toBe('deny');
  });
});

// ---------------------------------------------------------------------------
// Countdown
// ---------------------------------------------------------------------------

describe('secondsRemaining', () => {
  const now = Date.parse('2026-08-14T00:00:00Z');

  it('counts down from expires_at, not from arrival', () => {
    expect(secondsRemaining(
      { expires_at: '2026-08-14T00:05:00Z' }, now,
    )).toBe(300);
  });

  it('rounds up, so a dialog never claims less time than it has', () => {
    expect(secondsRemaining({ expires_at: now + 1200 }, now)).toBe(2);
  });

  it('floors at zero rather than going negative', () => {
    expect(secondsRemaining({ expires_at: now - 60_000 }, now)).toBe(0);
  });

  it('is null when there is nothing to count down from', () => {
    expect(secondsRemaining({}, now)).toBeNull();
    expect(secondsRemaining(null, now)).toBeNull();
  });
});

describe('formatCountdown', () => {
  it('formats minutes and zero-padded seconds', () => {
    expect(formatCountdown(300)).toBe('5:00');
    expect(formatCountdown(65)).toBe('1:05');
    expect(formatCountdown(9)).toBe('0:09');
    expect(formatCountdown(0)).toBe('0:00');
  });

  it('has a placeholder rather than rendering nothing', () => {
    expect(formatCountdown(null)).toBe('—');
  });
});

describe('countdownUrgency', () => {
  it('is quiet with plenty of time', () => {
    expect(countdownUrgency(300)).toBe('');
    expect(countdownUrgency(60)).toBe('');
  });

  it('turns amber under a minute and red under ten seconds', () => {
    expect(countdownUrgency(59)).toBe('amber');
    expect(countdownUrgency(10)).toBe('amber');
    expect(countdownUrgency(9)).toBe('red');
    expect(countdownUrgency(0)).toBe('red');
  });

  it('has no urgency to report without a countdown', () => {
    expect(countdownUrgency(null)).toBe('');
  });
});

// ---------------------------------------------------------------------------
// arrivalAnnouncement
// ---------------------------------------------------------------------------

describe('arrivalAnnouncement', () => {
  it('says the class, the file, and the diff size', () => {
    // Stats, not the diff itself: walking a screen reader through a
    // hundred-line hunk is not an announcement (§ Accessibility).
    const text = arrivalAnnouncement({
      tool_class: 'write',
      diff: { path: 'src/main.py', additions: 12, deletions: 3 },
    });
    expect(text).toBe('permission request: edit, src/main.py, 12 added 3 removed');
  });

  it('names each case where no diff can be shown', () => {
    const base = { tool_class: 'write' };
    expect(arrivalAnnouncement({
      ...base, diff: { path: 'a.py', is_new_file: true },
    })).toContain('new file');
    expect(arrivalAnnouncement({
      ...base, diff: { path: 'a.png', is_binary: true },
    })).toContain('binary, cannot be shown');
    expect(arrivalAnnouncement({
      ...base, diff: { path: 'big.js', too_large: true },
    })).toContain('too large to diff');
  });

  it('reads the command for a shell call, bounded', () => {
    const long = 'x'.repeat(500);
    const text = arrivalAnnouncement({
      tool_class: 'exec',
      command: { command: long },
    });
    expect(text).toBe(`permission request: shell command, ${'x'.repeat(200)}`);
  });

  it('reads the question for an interactive call', () => {
    expect(arrivalAnnouncement({
      tool_class: 'interact',
      question: { question: 'Which branch?' },
    })).toBe('permission request: question, Which branch?');
  });

  it('falls back to the summary when nothing more specific is present', () => {
    expect(arrivalAnnouncement({
      tool_class: 'mcp',
      summary: 'ac-dc › search',
    })).toBe('permission request: MCP tool, ac-dc › search');
  });

  it('says when a subagent asked, because that changes the answer', () => {
    expect(arrivalAnnouncement({
      tool_class: 'exec',
      command: { command: 'ls' },
      agent_id: 'agent_7',
    })).toContain('requested by a subagent');
  });

  it('is empty with no payload', () => {
    expect(arrivalAnnouncement(null)).toBe('');
  });
});

// ---------------------------------------------------------------------------
// headerTarget
// ---------------------------------------------------------------------------

describe('headerTarget', () => {
  it('prefers the file path for an edit', () => {
    expect(headerTarget({
      diff: { path: 'src/main.py' },
      title: 'Edit main',
    })).toBe('src/main.py');
  });

  it('collapses a multi-line command onto one line', () => {
    expect(headerTarget({
      command: { command: 'git add -A\n  && git commit -m x' },
    })).toBe('git add -A && git commit -m x');
  });

  it('truncates a long command with an ellipsis', () => {
    const target = headerTarget({ command: { command: 'y'.repeat(200) } });
    expect(target).toHaveLength(90);
    expect(target.endsWith('…')).toBe(true);
  });

  it('gives the server equal billing with the tool for MCP', () => {
    expect(headerTarget({ server: 'ac-dc', tool_name: 'search' }))
      .toBe('ac-dc › search');
  });

  it('uses the CLI title when it has one', () => {
    // What the terminal would show for the same call.
    expect(headerTarget({ title: 'Run the tests', tool_name: 'Bash' }))
      .toBe('Run the tests');
  });

  it('falls back to the summary, then the tool name', () => {
    expect(headerTarget({ summary: 'a summary', tool_name: 'Weird' }))
      .toBe('a summary');
    expect(headerTarget({ tool_name: 'Weird' })).toBe('Weird');
    expect(headerTarget(null)).toBe('');
  });
});

// ---------------------------------------------------------------------------
// defaultDenyReason
// ---------------------------------------------------------------------------

describe('defaultDenyReason', () => {
  it('is specific to the class', () => {
    expect(defaultDenyReason({ tool_class: 'write' }))
      .toBe('Do not change this file.');
    expect(defaultDenyReason({ tool_class: 'exec' }))
      .toBe('Do not run this command.');
    expect(defaultDenyReason({ tool_class: 'interact' }))
      .toBe('Do not ask; carry on with what you have.');
  });

  it('is never empty, so a denial is never reasonless', () => {
    // A blank denial produces an agent that retries the same call.
    expect(defaultDenyReason({ tool_class: 'something-new' })).toBeTruthy();
    expect(defaultDenyReason(null)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// describeRule / offersAlwaysAllow
// ---------------------------------------------------------------------------

describe('describeRule', () => {
  it('puts the rule text and its destination file on the button', () => {
    // "Always allow" with no rule text is the promise the engine spec
    // forbids (§ Always allow shows the rule, not a promise).
    expect(describeRule({
      tool_name: 'Bash',
      rule_content: 'npm test:*',
      behavior: 'allow',
      destination: 'localSettings',
      origin: 'cli',
    })).toEqual({
      label: 'Always allow Bash(npm test:*)',
      destination: '.claude/settings.local.json',
      derived: false,
      session: false,
      shared: false,
    });
  });

  it('names the verb for deny and ask rules too', () => {
    expect(describeRule({
      tool_name: 'Bash', behavior: 'deny', origin: 'cli',
    }).label).toBe('Always deny Bash');
    expect(describeRule({
      tool_name: 'Bash', behavior: 'ask', origin: 'cli',
    }).label).toBe('Always ask about Bash');
  });

  it('marks a rule AC-DC guessed rather than one the CLI suggested', () => {
    expect(describeRule({
      tool_name: 'Write', destination: 'projectSettings', origin: 'derived',
    })).toEqual({
      label: 'Always allow Write',
      destination: '.claude/settings.json',
      derived: true,
      session: false,
      shared: false,
    });
  });

  it('flags a session grant, which is written to no file at all', () => {
    // The CLI suggests this destination for a read outside the working
    // directory (observed: Read(//home/<user>/**), destination 'session'),
    // so the button's tooltip has to promise something different from the
    // settings-file case.
    expect(describeRule({
      tool_name: 'Read',
      rule_content: '//home/someone/**',
      destination: 'session',
      origin: 'cli',
    })).toEqual({
      label: 'Always allow Read(//home/someone/**)',
      destination: '(this session only)',
      derived: false,
      session: true,
      shared: false,
    });
  });

  it('flags the row that writes to the git-tracked file', () => {
    // Two rows differing only by `.local` in a filename is a distinction a
    // person reading quickly will not make, and the consequence — the grant
    // reaching everyone who pulls — is not undone by unclicking (CC-16).
    expect(describeRule({
      tool_name: 'Edit',
      rule_content: 'src/x.py',
      destination: 'projectSettings',
      origin: 'derived',
      shared: true,
    }).shared).toBe(true);
  });

  it('passes an unmapped destination through verbatim', () => {
    expect(describeRule({
      tool_name: 'Bash', destination: 'somethingNew', origin: 'cli',
    }).destination).toBe('somethingNew');
  });

  it('is null for anything it cannot label honestly', () => {
    expect(describeRule(null)).toBeNull();
    expect(describeRule({})).toBeNull();
    expect(describeRule({ rule_content: 'x' })).toBeNull();
  });
});

describe('offersAlwaysAllow', () => {
  it('is offered when the request carries rules', () => {
    expect(offersAlwaysAllow({
      tool_class: 'exec',
      suggested_rules: [{ tool_name: 'Bash' }],
    })).toBe(true);
  });

  it('is never offered for a question', () => {
    // There is no rule that can answer a future question (§ interact).
    expect(offersAlwaysAllow({
      tool_class: 'interact',
      suggested_rules: [{ tool_name: 'AskUserQuestion' }],
    })).toBe(false);
  });

  it('is not offered when there is no rule to show', () => {
    expect(offersAlwaysAllow({ tool_class: 'exec', suggested_rules: [] }))
      .toBe(false);
    expect(offersAlwaysAllow({ tool_class: 'exec' })).toBe(false);
    expect(offersAlwaysAllow(null)).toBe(false);
  });
});

describe('describeMode', () => {
  const offer = {
    mode: 'acceptEdits',
    destination: 'session',
    label: 'Accept all edits for the rest of this session',
    detail: 'Every later file edit is applied without asking.',
  };

  it('carries the engine\'s own copy through to the button', () => {
    // The engine is the side that knows which modes it will offer and what
    // each one costs. Splitting that between the two sides would let a
    // button describe a consequence the engine does not apply.
    expect(describeMode({ tool_class: 'write', suggested_mode: offer })).toEqual({
      mode: 'acceptEdits',
      label: 'Accept all edits for the rest of this session',
      detail: 'Every later file edit is applied without asking.',
      destination: '(this session only)',
    });
  });

  it('is null when the CLI offered no mode', () => {
    expect(describeMode({ tool_class: 'write' })).toBeNull();
    expect(describeMode({ tool_class: 'write', suggested_mode: null })).toBeNull();
    expect(describeMode(null)).toBeNull();
  });

  it('is null for a question, which no standing grant can answer', () => {
    expect(describeMode({ tool_class: 'interact', suggested_mode: offer }))
      .toBeNull();
  });

  it('renders nothing it cannot label honestly', () => {
    // A button that names a mode but not its consequence is the promise the
    // spec forbids, one level up from a rule with no rule text.
    expect(describeMode({
      tool_class: 'write', suggested_mode: { mode: 'acceptEdits' },
    })).toBeNull();
    expect(describeMode({
      tool_class: 'write', suggested_mode: { label: 'do a thing' },
    })).toBeNull();
  });

  it('tolerates a missing detail rather than dropping the control', () => {
    const described = describeMode({
      tool_class: 'write',
      suggested_mode: { mode: 'acceptEdits', label: 'Accept edits' },
    });
    expect(described.detail).toBe('');
    expect(described.destination).toBe('');
  });
});

// ---------------------------------------------------------------------------
// interactQuestions / answersComplete
// ---------------------------------------------------------------------------

describe('interactQuestions', () => {
  it('reads the whole list a call asked', () => {
    const questions = interactQuestions({
      question: {
        question: 'first?',
        questions: [{ question: 'first?' }, { question: 'second?' }],
      },
    });
    expect(questions.map((entry) => entry.question)).toEqual(['first?', 'second?']);
  });

  it('falls back to the promoted question when there is no list', () => {
    // The engine always sends `questions`; a payload from a reconnect
    // snapshot written by an older server, or a hand-built one, still has
    // one question in it and should still render.
    const questions = interactQuestions({
      question: { question: 'only?', options: [{ label: 'yes' }] },
    });
    expect(questions).toHaveLength(1);
    expect(questions[0].options[0].label).toBe('yes');
  });

  it('has nothing to render without a question', () => {
    expect(interactQuestions({ question: null })).toEqual([]);
    expect(interactQuestions({ question: { options: [] } })).toEqual([]);
    expect(interactQuestions(null)).toEqual([]);
  });
});

describe('answersComplete', () => {
  const payload = {
    question: { questions: [{ question: 'a?' }, { question: 'b?' }] },
  };

  it('needs an answer to every question', () => {
    expect(answersComplete(payload, new Map())).toBe(false);
    expect(answersComplete(payload, new Map([[0, new Set([1])]]))).toBe(false);
    expect(answersComplete(
      payload,
      new Map([[0, new Set([1])], [1, new Set([0])]]),
    )).toBe(true);
  });

  it('does not count a question that was selected and then cleared', () => {
    expect(answersComplete(
      payload,
      new Map([[0, new Set([1])], [1, new Set()]]),
    )).toBe(false);
  });

  it('is never complete when there is nothing to answer', () => {
    // Guards the Answer button on a malformed `interact` payload: with no
    // questions there is no answer to send, so it stays disabled.
    expect(answersComplete({ question: null }, new Map())).toBe(false);
  });
});
