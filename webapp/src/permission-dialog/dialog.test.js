// Tests for <aic-permission-dialog>.
//
// The invariants asserted here are the ones from
// specs5/5-webapp/permission-dialog.md that cost the most to get wrong,
// in roughly that order:
//
//   1. A request leaves the queue only via a decision, a broadcast, or
//      expiry — never by being dismissed.
//   2. Escape denies with a reason; the scrim does nothing.
//   3. Nothing holds focus during the settling interval and Enter/Space
//      are swallowed for its duration, on arrival and on reconnect alike.
//   4. Only localhost gets decision controls; every client gets the body.
//
// Timers are faked for the whole file: the settling interval, the
// one-second countdown tick, and expiry are all clock-driven, and a real
// clock would make each of those a sleep. Per D15 in
// specs5/impl-history/decisions.md the `settle()` helper here drains
// microtasks only — no rAF, no real setTimeout — so it works either side
// of the fake-timer boundary.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ---------------------------------------------------------------------------
// Monaco mock — registered before ./index.js is imported
// ---------------------------------------------------------------------------

// `vi.mock` factories hoist above top-level declarations, so the shared
// state has to come from `vi.hoisted` to be in scope for both. Same
// approach as diff-viewer/test-helpers.js.
const { monacoState, makeModel, makeEditor } = vi.hoisted(() => {
  const state = { editors: [], models: [], languages: new Set() };

  function _makeModel(content, language) {
    const model = {
      _content: content,
      _language: language,
      _disposed: false,
      getValue: () => model._content,
      setValue: (value) => { model._content = value; },
      dispose: () => { model._disposed = true; },
    };
    state.models.push(model);
    return model;
  }

  function _makeEditor(container) {
    let models = null;
    const editor = {
      _container: container,
      _disposed: false,
      _options: { readOnly: true },
      _revealed: [],
      _contentListeners: [],
      _diffListeners: [],
      _lineChanges: [],
      setModel: (pair) => { models = pair; },
      getModel: () => models,
      getModifiedEditor: () => ({
        onDidChangeModelContent: (cb) => {
          editor._contentListeners.push(cb);
          return { dispose: () => {} };
        },
        updateOptions: (opts) => { Object.assign(editor._options, opts); },
        revealLineInCenter: (line) => { editor._revealed.push(line); },
        focus: () => {},
        getValue: () => models?.modified?._content ?? '',
        getModel: () => models?.modified ?? null,
      }),
      onDidUpdateDiff: (cb) => {
        editor._diffListeners.push(cb);
        return { dispose: () => {} };
      },
      getLineChanges: () => editor._lineChanges,
      layout: () => {},
      dispose: () => { editor._disposed = true; },
      /** Pretend the user typed in the right-hand pane. */
      _simulateEdit(value) {
        if (models?.modified) models.modified._content = value;
        for (const cb of editor._contentListeners) cb();
      },
      /** Pretend Monaco finished computing the diff. */
      _simulateDiffComputed(lineChanges) {
        editor._lineChanges = lineChanges;
        for (const cb of [...editor._diffListeners]) cb();
      },
    };
    state.editors.push(editor);
    return editor;
  }

  return { monacoState: state, makeModel: _makeModel, makeEditor: _makeEditor };
});

vi.mock('monaco-editor/esm/vs/editor/edcore.main.js', () => {
  const monaco = {
    editor: {
      createDiffEditor: (container, options) => {
        const editor = makeEditor(container);
        editor._constructionOptions = options || {};
        return editor;
      },
      createModel: (content, language) => makeModel(content, language),
    },
    languages: {
      register: (info) => { monacoState.languages.add(info.id); },
      setMonarchTokensProvider: () => {},
      getLanguages: () => [...monacoState.languages].map((id) => ({ id })),
    },
  };
  return { default: monaco, ...monaco };
});

// ---------------------------------------------------------------------------
// Now the module under test
// ---------------------------------------------------------------------------

import { SharedRpc } from '../rpc.js';
import { ESCAPE_DENY_REASON, SETTLING_MS, TITLE_MARKER } from './constants.js';
import './index.js';

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const NOW = Date.parse('2026-08-14T10:00:00.000Z');
const ORIGINAL_TITLE = 'AIC-DC — test';

const _mounted = [];
let rpcCalls;
let toasts;
let onToast;

/** An ISO `expires_at` that many seconds from the faked clock. */
function expiresIn(seconds) {
  return new Date(Date.now() + seconds * 1000).toISOString();
}

/**
 * Publish a fake RPC proxy. Handlers are looked up by `Class.method`
 * and their return value is wrapped in a one-key envelope, which is what
 * `rpcExtract` unwraps.
 */
function publishRpc(overrides = {}) {
  const handlers = {
    'Collab.get_collab_role': async () => ({
      role: 'host', is_localhost: true, client_id: 'localhost',
    }),
    'ClaudeCodeService.get_current_state': async () => ({
      pending_permissions: [],
    }),
    'ClaudeCodeService.resolve_permission': async () => ({ resolved: true }),
    ...overrides,
  };
  const proxy = {};
  for (const [method, impl] of Object.entries(handlers)) {
    proxy[method] = async (...args) => {
      rpcCalls.push({ method, args });
      return { fake: await impl(...args) };
    };
  }
  SharedRpc.set(proxy);
  return proxy;
}

function mount() {
  const el = document.createElement('aic-permission-dialog');
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

/**
 * Drain microtasks and Lit's update queue.
 *
 * Microtasks only — see the file header. Lit schedules its updates on a
 * microtask, so this is sufficient and it does not need the clock.
 */
async function settle(el, rounds = 12) {
  for (let i = 0; i < rounds; i += 1) {
    await Promise.resolve();
    if (el?.updateComplete) await el.updateComplete;
  }
}

/** Advance the clock and let everything it triggered land. */
async function tick(el, ms) {
  await vi.advanceTimersByTimeAsync(ms);
  await settle(el);
}

function broadcast(payload) {
  window.dispatchEvent(new CustomEvent('permission-request', { detail: payload }));
}

function resolveBroadcast(detail) {
  window.dispatchEvent(new CustomEvent('permission-resolved', { detail }));
}

/** Enqueue a request and let the settling interval elapse. */
async function ask(el, payload) {
  broadcast(payload);
  await settle(el);
  await tick(el, SETTLING_MS + 40);
  return el;
}

function key(el, k, extra = {}) {
  const event = new KeyboardEvent('keydown', {
    key: k, bubbles: true, cancelable: true, ...extra,
  });
  window.dispatchEvent(event);
  return event;
}

function decision(el, which) {
  return el.shadowRoot.querySelector(`button.decision[data-decision="${which}"]`);
}

/** A stylesheet in document.head, standing in for one of Monaco's. */
function headStyle(cssText) {
  const style = document.createElement('style');
  style.textContent = cssText;
  document.head.appendChild(style);
  return style;
}

/** Everything the dialog cloned out of document.head, concatenated. */
function clonedStyleText(el) {
  return [...el.shadowRoot.querySelectorAll('[data-aic-dc-monaco-clone]')]
    .map((node) => node.textContent)
    .join('\n');
}

function lastResolve() {
  const calls = rpcCalls.filter(
    (c) => c.method === 'ClaudeCodeService.resolve_permission',
  );
  return calls[calls.length - 1];
}

// ---------------------------------------------------------------------------
// Payload factories — shapes from src/aic_dc/claude_code/permissions.py
// ---------------------------------------------------------------------------

function execPayload(over = {}) {
  return {
    permission_id: 'perm_exec',
    tool_use_id: 'toolu_exec',
    request_id: 'req_1',
    tool_name: 'Bash',
    display_name: 'Bash',
    tool_class: 'exec',
    gated_by_default: true,
    summary: 'ls -la',
    input: { command: 'ls -la', description: 'list the files' },
    command: {
      command: 'ls -la',
      cwd: '/repo',
      description: 'list the files',
      flags: [],
      truncated: false,
    },
    suggested_rules: [{
      tool_name: 'Bash',
      rule_content: 'ls:*',
      behavior: 'allow',
      destination: 'localSettings',
      origin: 'cli',
    }],
    // The normal case: a host client is connected, so nothing is counting
    // down. Tests that want a clock pass `expires_at` explicitly.
    expires_at: null,
    localhost_available: true,
    ...over,
  };
}

function writePayload(over = {}) {
  return {
    permission_id: 'perm_write',
    tool_use_id: 'toolu_write',
    request_id: 'req_1',
    tool_name: 'Write',
    display_name: 'Write',
    tool_class: 'write',
    gated_by_default: true,
    summary: 'write src/main.py',
    input: { file_path: 'src/main.py', content: 'print(2)\n' },
    diff: {
      path: 'src/main.py',
      original: 'print(1)\n',
      proposed: 'print(2)\n',
      additions: 1,
      deletions: 1,
      is_new_file: false,
      is_binary: false,
      too_large: false,
    },
    suggested_rules: [{
      tool_name: 'Write',
      rule_content: 'src/**',
      behavior: 'allow',
      destination: 'localSettings',
      origin: 'derived',
    }],
    // The normal case: a host client is connected, so nothing is counting
    // down. Tests that want a clock pass `expires_at` explicitly.
    expires_at: null,
    localhost_available: true,
    ...over,
  };
}

const PLAN_TEXT = [
  '## Add the widget',
  '',
  '- move `src/old.js` aside',
  '- write the new one',
  '',
  '```js',
  'const widget = 1;',
  '```',
].join('\n');

function planPayload(over = {}) {
  return {
    permission_id: 'perm_plan',
    tool_use_id: 'toolu_plan',
    request_id: 'req_1',
    tool_name: 'ExitPlanMode',
    display_name: 'ExitPlanMode',
    tool_class: 'plan',
    gated_by_default: true,
    summary: 'ExitPlanMode: Add the widget',
    input: { plan: PLAN_TEXT },
    plan: {
      plan: PLAN_TEXT,
      headline: 'Add the widget',
      file_path: null,
    },
    suggested_rules: [],
    // The normal case: a host client is connected, so nothing is counting
    // down. Tests that want a clock pass `expires_at` explicitly.
    expires_at: null,
    localhost_available: true,
    ...over,
  };
}

function interactPayload(over = {}) {
  return {
    permission_id: 'perm_ask',
    tool_use_id: 'toolu_ask',
    request_id: 'req_1',
    tool_name: 'AskUserQuestion',
    display_name: 'AskUserQuestion',
    tool_class: 'interact',
    gated_by_default: true,
    summary: 'Which branch?',
    input: { questions: [{ question: 'Which branch?' }] },
    question: {
      question: 'Which branch?',
      multi_select: false,
      options: [
        { label: 'main', description: 'the default' },
        { label: 'dev5', description: 'the working branch' },
      ],
      questions: [{
        question: 'Which branch?',
        header: 'Branch',
        multi_select: false,
        options: [
          { label: 'main', description: 'the default' },
          { label: 'dev5', description: 'the working branch' },
        ],
      }],
    },
    suggested_rules: [],
    // The normal case: a host client is connected, so nothing is counting
    // down. Tests that want a clock pass `expires_at` explicitly.
    expires_at: null,
    localhost_available: true,
    ...over,
  };
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  rpcCalls = [];
  toasts = [];
  onToast = (event) => toasts.push(event.detail);
  window.addEventListener('aic-toast', onToast);
  document.title = ORIGINAL_TITLE;
  monacoState.editors = [];
  monacoState.models = [];
});

afterEach(() => {
  window.removeEventListener('aic-toast', onToast);
  while (_mounted.length) {
    const el = _mounted.pop();
    if (el.parentNode) el.parentNode.removeChild(el);
  }
  SharedRpc.reset();
  vi.useRealTimers();
  document.title = ORIGINAL_TITLE;
});

// ---------------------------------------------------------------------------
// Nothing pending
// ---------------------------------------------------------------------------

describe('with an empty queue', () => {
  it('renders nothing at all', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    expect(el.shadowRoot.querySelector('.dialog')).toBeNull();
    expect(el.shadowRoot.querySelector('.scrim')).toBeNull();
  });

  it('leaves the page title alone', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    expect(document.title).toBe(ORIGINAL_TITLE);
  });
});

// ---------------------------------------------------------------------------
// Arrival
// ---------------------------------------------------------------------------

describe('when a request arrives', () => {
  it('shows the tool and the target', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);

    const header = el.shadowRoot.querySelector('header');
    expect(header.textContent).toContain('Bash');
    expect(header.textContent).toContain('ls -la');
  });

  it('shows no countdown, because a request with a host waits', async () => {
    // The regression the old 300-second deadline was: a user who walked
    // away came back to a request denied on their behalf by a timer. An
    // em dash beside a stopwatch would read as a countdown that failed to
    // load, so the chip is absent rather than blank.
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    expect(el.shadowRoot.querySelector('.countdown')).toBeNull();

    await tick(el, 10 * 60 * 1000);
    expect(el.queue).toHaveLength(1);
    expect(toasts.map((t) => t.message).join(' ')).not.toContain('expired');
  });

  it('shows a countdown when the payload carries a deadline', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(300) }));
    await settle(el);
    expect(el.shadowRoot.querySelector('.countdown').textContent)
      .toContain('5:00');
  });

  it('shows the command verbatim and its working directory', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);

    // What is shown is exactly what will run — never re-formatted.
    expect(el.shadowRoot.querySelector('pre.command').textContent).toBe('ls -la');
    expect(el.shadowRoot.querySelector('.cwd').textContent).toContain('/repo');
  });

  it('marks the page title so a backgrounded tab still says so', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    expect(document.title).toContain(TITLE_MARKER);
    expect(document.title).toContain(ORIGINAL_TITLE);
  });

  it('announces the class and the target for a screen reader', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(writePayload());
    await settle(el);
    const live = el.shadowRoot.querySelector('[aria-live="polite"]');
    expect(live.textContent).toContain('permission request: edit');
    expect(live.textContent).toContain('src/main.py');
  });

  it('says so when a subagent asked, naming the only id it has', async () => {
    // `title` is the CLI's prompt sentence ("Claude wants to run npm test"),
    // not the subagent's name — putting it here would read as
    // `requested by subagent "Claude wants to run npm test"`. The payload
    // carries no subagent description, so the id is what there is to show.
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ agent_id: 'agent_3', title: 'Claude wants to run npm test' }));
    await settle(el);
    const attribution = el.shadowRoot.querySelector('.attribution');
    expect(attribution.textContent).toContain('requested by subagent');
    expect(attribution.textContent).toContain('agent_3');
    expect(attribution.textContent).not.toContain('npm test');
  });

  it('explains the countdown when no host is connected', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ localhost_available: false, expires_at: expiresIn(30) }));
    await settle(el);
    const note = el.shadowRoot.querySelector('.no-localhost');
    expect(note).not.toBeNull();
    expect(note.textContent).toContain('counting down');
  });

  it('shows why a normally-ungated class is being asked about', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({
      permission_id: 'perm_read',
      tool_name: 'Read',
      tool_class: 'read',
      command: null,
      input: { file_path: '/etc/shadow' },
      blocked_path: '/etc/shadow',
      decision_reason: { reason: 'a deny rule matched' },
    }));
    await settle(el);
    const body = el.shadowRoot.querySelector('.body');
    expect(body.textContent).toContain('not normally gated');
    expect(body.textContent).toContain('a deny rule matched');
  });

  it('never queues the same request twice', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    broadcast(execPayload());
    await settle(el);
    expect(el.queue).toHaveLength(1);
    expect(el.shadowRoot.querySelector('.queue-position')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Settling interval
// ---------------------------------------------------------------------------

describe('during the settling interval', () => {
  it('gives focus to nothing', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    expect(el.shadowRoot.activeElement).toBeNull();
  });

  it('disables every decision control', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    const buttons = [...el.shadowRoot.querySelectorAll('button.decision')];
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.every((b) => b.disabled)).toBe(true);
  });

  it('swallows Enter and Space', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    // A keystroke already in flight when the dialog opened must not be
    // able to approve anything.
    expect(key(el, 'Enter').defaultPrevented).toBe(true);
    expect(key(el, ' ').defaultPrevented).toBe(true);
    expect(lastResolve()).toBeUndefined();
  });

  it('refuses a decision even if one is somehow triggered', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    await el._decide('allow');
    await settle(el);
    expect(lastResolve()).toBeUndefined();
    expect(el.current).not.toBeNull();
  });

  it('applies to a request restored on reconnect too', async () => {
    // A page load must not be able to approve anything either.
    publishRpc({
      'ClaudeCodeService.get_current_state': async () => ({
        pending_permissions: [execPayload()],
      }),
    });
    const el = mount();
    await settle(el);
    expect(el.current?.permission_id).toBe('perm_exec');
    expect(el._settling).toBe(true);
    expect(el.shadowRoot.activeElement).toBeNull();
  });
});

describe('after the settling interval', () => {
  it('focuses Allow for an ordinary call', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(el.shadowRoot.activeElement).toBe(decision(el, 'allow'));
  });

  it('focuses Deny for a command flagged as deleting', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload({
      command: { command: 'rm -rf build', cwd: '/repo', flags: ['deletes'] },
    }));
    // Controls stay put; it is the focus that moves with risk, so muscle
    // memory alone cannot approve this.
    expect(el.shadowRoot.activeElement).toBe(decision(el, 'deny'));
    expect(el.shadowRoot.querySelector('.dialog').classList.contains('risky'))
      .toBe(true);
  });

  it('focuses Deny for an MCP tool', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload({
      permission_id: 'perm_mcp',
      tool_name: 'mcp__aic-dc__search',
      tool_class: 'mcp',
      server: 'aic-dc',
      command: null,
      input: { query: 'x' },
    }));
    expect(el.shadowRoot.activeElement).toBe(decision(el, 'deny'));
  });

  it('lets Enter through again', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(key(el, 'Enter').defaultPrevented).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Escape and the scrim
// ---------------------------------------------------------------------------

describe('dismissal', () => {
  it('denies on Escape, with the reason the spec names', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    const event = key(el, 'Escape');
    await settle(el);

    expect(event.defaultPrevented).toBe(true);
    expect(lastResolve().args).toEqual([
      'perm_exec', { action: 'deny', reason: ESCAPE_DENY_REASON },
    ]);
    expect(el.current).toBeNull();
  });

  it('closes the open menu on Escape before denying anything', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    el._toggleDenyMenu();
    await settle(el);

    key(el, 'Escape');
    await settle(el);

    expect(el._denyMenuOpen).toBe(false);
    expect(lastResolve()).toBeUndefined();
    expect(el.current).not.toBeNull();
  });

  it('does nothing at all when the scrim is clicked', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    el.shadowRoot.querySelector('.scrim').click();
    await settle(el);

    // A stray click on a modal over a UI the user was mid-gesture in is
    // not recoverable the way a stray Escape is.
    expect(lastResolve()).toBeUndefined();
    expect(el.current?.permission_id).toBe('perm_exec');
  });
});

// ---------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------

describe('deciding', () => {
  it('allows once', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow').click();
    await settle(el);

    expect(lastResolve().args).toEqual(['perm_exec', { action: 'allow' }]);
    expect(el.current).toBeNull();
  });

  it('labels always-allow with the rule text and the file it writes to', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    const button = decision(el, 'allow-always');
    expect(button.textContent).toContain('Always allow Bash(ls:*)');
    expect(button.textContent).toContain('.claude/settings.local.json');
    // Both consequences of the grant have to be in reach, not discovered.
    expect(button.title).toContain('settings file you can read and revoke');
    expect(button.title).toContain('claude CLI');
  });

  it('tells the truth about a session grant, which writes to no file', async () => {
    // The CLI suggests destination 'session' for a read outside the working
    // directory. The old single tooltip asserted "there is no invisible
    // session-only grant behind this button", which was false for exactly
    // this case — and it is the CLI's suggestion, not one of ours.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload({
      suggested_rules: [{
        tool_name: 'Read',
        rule_content: '//home/someone/**',
        behavior: 'allow',
        destination: 'session',
        origin: 'cli',
      }],
    }));

    const button = decision(el, 'allow-always');
    expect(button.textContent).toContain('(this session only)');
    expect(button.title).toContain('rest of this session only');
    expect(button.title).not.toContain('settings file you can read and revoke');
  });

  it('marks a rule AIC-DC guessed rather than one the CLI suggested', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());
    expect(decision(el, 'allow-always').textContent).toContain('derived');
  });

  // -------------------------------------------------------------------
  // The session mode switch
  // -------------------------------------------------------------------

  const acceptEditsMode = {
    mode: 'acceptEdits',
    destination: 'session',
    label: 'Accept all edits for the rest of this session',
    detail: 'Every later file edit is applied without asking — you will not '
      + 'see a diff for it. Shell commands still ask.',
  };

  it('offers the mode switch on its own control, not on always-allow', async () => {
    // `acceptEdits` stops this dialog opening for every later edit in the
    // session. That is a different and much larger thing than a rule
    // granting one path, so it cannot share a button whose label speaks
    // about this call.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({ suggested_mode: acceptEditsMode }));

    const button = decision(el, 'allow-mode');
    expect(button).toBeTruthy();
    expect(button.textContent).toContain('rest of this session');
    expect(decision(el, 'allow-always').textContent)
      .not.toContain('rest of this session');
  });

  it('says what the mode switch costs, including the lost diff', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({ suggested_mode: acceptEditsMode }));

    const button = decision(el, 'allow-mode');
    expect(button.title).toContain('diff');
    expect(button.textContent).toContain('(this session only)');
  });

  it('sends allow_mode, and never the mode name, when it is clicked', async () => {
    // The mode comes from the request the engine built. A client able to
    // name one could name `bypassPermissions`.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({ suggested_mode: acceptEditsMode }));

    decision(el, 'allow-mode').click();
    await settle(el);

    expect(lastResolve().args).toEqual(['perm_write', { action: 'allow_mode' }]);
  });

  it('has no mode control when the CLI offered no mode', async () => {
    // AIC-DC never invents one: a rule grants one path and can be read back
    // out of a settings file, whereas a mode silences the gate wholesale.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(decision(el, 'allow-mode')).toBeNull();
  });

  it('never offers a mode switch on a question', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({ suggested_mode: acceptEditsMode }));
    expect(decision(el, 'allow-mode')).toBeNull();
  });

  it('sends the chosen rule index with an always-allow', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow-always').click();
    await settle(el);

    expect(lastResolve().args).toEqual([
      'perm_exec', { action: 'allow_always', rule_index: 0 },
    ]);
  });

  it('prefills a deny reason and sends it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'deny').click();
    await settle(el);
    const field = el.shadowRoot.querySelector('input.deny-reason');
    // A blank denial produces an agent that retries the same call.
    expect(field.value).toBe('Do not run this command.');

    field.value = 'not on this branch';
    field.dispatchEvent(new Event('input'));
    await settle(el);
    el.shadowRoot.querySelector('.reason-row button.decision').click();
    await settle(el);

    expect(lastResolve().args).toEqual([
      'perm_exec', { action: 'deny', reason: 'not on this branch' },
    ]);
  });

  it('offers a deny that stops the turn, separately from a plain deny', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    el.shadowRoot.querySelector('button.decision.danger.caret').click();
    await settle(el);
    const item = [...el.shadowRoot.querySelectorAll('[role="menuitem"]')]
      .find((node) => node.textContent.includes('stop the turn'));
    expect(item).toBeDefined();
    item.click();
    await settle(el);
    el.shadowRoot.querySelector('.reason-row button.decision').click();
    await settle(el);

    expect(lastResolve().args[1].action).toBe('deny_interrupt');
  });

  it('never sends an empty reason even if the field is cleared', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'deny').click();
    await settle(el);
    const field = el.shadowRoot.querySelector('input.deny-reason');
    field.value = '   ';
    field.dispatchEvent(new Event('input'));
    await settle(el);
    field.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', bubbles: true, cancelable: true,
    }));
    await settle(el);

    expect(lastResolve().args[1].reason).toBe('Do not run this command.');
  });

  it('cannot send a second decision for the same request', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    const allow = decision(el, 'allow');
    allow.click();
    allow.click();
    await settle(el);

    const sent = rpcCalls.filter(
      (c) => c.method === 'ClaudeCodeService.resolve_permission',
    );
    expect(sent).toHaveLength(1);
  });

  it('says so when another window won the race', async () => {
    publishRpc({
      'ClaudeCodeService.resolve_permission': async () => ({
        error: 'already_resolved', resolved_by: 'c2', action: 'allow',
      }),
    });
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow').click();
    await settle(el);

    expect(toasts.map((t) => t.message).join(' ')).toContain('Already answered by c2');
  });

  it('says so, rather than re-opening, when the call itself fails', async () => {
    publishRpc({
      'ClaudeCodeService.resolve_permission': async () => {
        throw new Error('socket closed');
      },
    });
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow').click();
    await settle(el);

    expect(el.current).toBeNull();
    expect(toasts.map((t) => t.message).join(' ')).toContain('will time out');
  });
});

// ---------------------------------------------------------------------------
// The queue
// ---------------------------------------------------------------------------

describe('the queue', () => {
  it('shows exactly one dialog, with its position', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    broadcast(writePayload());
    await settle(el);

    expect(el.shadowRoot.querySelectorAll('.dialog')).toHaveLength(1);
    expect(el.shadowRoot.querySelector('.queue-position').textContent.trim())
      .toBe('1 of 2');
  });

  it('answers the request closest to timing out first', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(300) }));
    broadcast(writePayload({ expires_at: expiresIn(30) }));
    await settle(el);
    expect(el.current.permission_id).toBe('perm_write');
  });

  it('moves to the next request after a decision', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(60) }));
    broadcast(writePayload({ expires_at: expiresIn(300) }));
    await settle(el);
    await tick(el, SETTLING_MS + 40);

    decision(el, 'allow').click();
    await settle(el);

    expect(el.current.permission_id).toBe('perm_write');
    // The next request gets its own settling interval.
    expect(el._settling).toBe(true);
  });

  it('discards a half-typed deny reason when the request changes', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(60) }));
    broadcast(writePayload({ expires_at: expiresIn(300) }));
    await settle(el);
    await tick(el, SETTLING_MS + 40);

    decision(el, 'deny').click();
    await settle(el);
    const field = el.shadowRoot.querySelector('input.deny-reason');
    field.value = 'meant for the first one';
    field.dispatchEvent(new Event('input'));
    await settle(el);
    el.shadowRoot.querySelector('.reason-row button.decision').click();
    await settle(el);

    // The text belonged to the request that is now answered; it must not
    // be resubmitted against the next one.
    expect(el.current.permission_id).toBe('perm_write');
    expect(el._denyOpen).toBe(false);
    expect(el._denyReason).toBe('');
  });

  it('keeps the title count in step with the queue', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(60) }));
    broadcast(writePayload({ expires_at: expiresIn(300) }));
    await settle(el);
    expect(document.title).toContain(`${TITLE_MARKER} 2`);

    await tick(el, SETTLING_MS + 40);
    decision(el, 'allow').click();
    await settle(el);
    expect(document.title).toContain(`${TITLE_MARKER} 1`);
  });

  it('restores the title once nothing is pending', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    decision(el, 'allow').click();
    await settle(el);
    expect(document.title).toBe(ORIGINAL_TITLE);
  });
});

// ---------------------------------------------------------------------------
// Resolution from elsewhere
// ---------------------------------------------------------------------------

describe('a resolution from elsewhere', () => {
  it('closes the dialog and says who answered', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'allow', resolved_by: 'c2',
    });
    await settle(el);

    // A dialog that vanished with no explanation reads as a bug.
    expect(el.current).toBeNull();
    expect(toasts.map((t) => t.message).join(' '))
      .toContain('Allowed by another window (c2)');
  });

  it('blames the absent host, not the user, when the clock ran out', async () => {
    // Expiry means one thing now: no host client was connected to answer.
    // "For want of an answer" would blame the user for a dialog that was
    // never on their screen.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'timeout', tool_use_id: 'toolu_exec',
    });
    await settle(el);

    expect(toasts[0].message).toContain('no host client was connected');
    expect(toasts[0].type).toBe('warning');
  });

  it('says when a stopped turn denied it', async () => {
    // The way out of a dialog nobody wants to answer is Stop, not a timer,
    // so the dialog that Stop closes has to say that is what happened —
    // which it did not, while the message was read off `action` alone.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'cancelled', cause: 'stopped',
    });
    await settle(el);

    expect(el.current).toBeNull();
    expect(toasts.map((t) => t.message).join(' ')).toContain('the turn was stopped');
  });

  it('says when the end of a turn denied it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'cancelled', cause: 'turn_ended',
    });
    await settle(el);

    expect(toasts.map((t) => t.message).join(' ')).toContain('the turn it belonged to ended');
  });

  it('blames the subagent, not the turn, when a subagent ended', async () => {
    // A background subagent's dialog outlives the turn that spawned it, so
    // pointing at the turn would send the user looking in the wrong place.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'cancelled', cause: 'agent_ended',
    });
    await settle(el);

    expect(toasts.map((t) => t.message).join(' '))
      .toContain('the subagent that asked for it ended');
  });

  it('stays vague rather than wrong when no cause is given', async () => {
    // An engine too old to send a cause must not make the dialog assert a
    // specific reason it cannot know.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({ permission_id: 'perm_exec', action: 'cancelled' });
    await settle(el);

    const said = toasts.map((t) => t.message).join(' ');
    expect(said).toContain('no longer waiting on anyone');
    expect(said).not.toContain('the turn it belonged to ended');
  });

  it('says when a shutdown denied it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'shutdown', cause: 'shutdown',
    });
    await settle(el);

    expect(toasts.map((t) => t.message).join(' ')).toContain('shut down');
  });

  it('is silent about a decision this window sent', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow').click();
    await settle(el);
    // The deciding client receives its own broadcast; it must not toast.
    resolveBroadcast({
      permission_id: 'perm_exec', action: 'allow', resolved_by: 'localhost',
    });
    await settle(el);

    expect(toasts).toHaveLength(0);
  });

  it('does not re-open a request that arrives again after resolution', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    resolveBroadcast({ permission_id: 'perm_exec', action: 'allow' });
    await settle(el);

    broadcast(execPayload());
    await settle(el);

    expect(el.current).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Countdown and expiry
// ---------------------------------------------------------------------------

describe('the countdown', () => {
  it('counts down from expires_at, not from arrival', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    // Pretend the socket was slow: the request already has 30s left.
    broadcast(execPayload({ expires_at: expiresIn(30) }));
    await settle(el);
    expect(el.shadowRoot.querySelector('.countdown').textContent)
      .toContain('0:30');

    await tick(el, 21_000);
    const countdown = el.shadowRoot.querySelector('.countdown');
    expect(countdown.textContent).toContain('0:09');
    // Colour is never the only signal — the numeral carries it too.
    expect(countdown.classList.contains('red')).toBe(true);
  });

  it('turns amber under a minute', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(61) }));
    await settle(el);
    await tick(el, 3_000);
    expect(el.shadowRoot.querySelector('.countdown').classList.contains('amber'))
      .toBe(true);
  });

  it('announces at coarse milestones only', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(70) }));
    await settle(el);
    await tick(el, 11_000);
    const live = el.shadowRoot.querySelector('[aria-live="polite"]');
    // A per-second live region is unusable with a screen reader.
    expect(live.textContent).toContain('1 minute left to answer');
  });

  it('never announces a milestone above the time the request has', async () => {
    // The only deadline left is the 30 s no-host one, and the milestone
    // list starts at five minutes. Announcing the first threshold the
    // remaining time falls under would tell a screen-reader user they have
    // five minutes to answer something that expires in thirty seconds.
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(30) }));
    await settle(el);
    const live = el.shadowRoot.querySelector('[aria-live="polite"]');
    await tick(el, 3_000);
    expect(live.textContent).not.toContain('minute');
    // The ten-second milestone is inside the window, so it still fires.
    await tick(el, 18_000);
    expect(live.textContent).toContain('10 seconds left to answer');
  });

  it('closes a request whose clock ran out, and says why', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(5) }));
    await settle(el);

    await tick(el, 6_000);

    expect(el.current).toBeNull();
    expect(toasts.map((t) => t.message).join(' '))
      .toContain('no host client was connected');
  });
});

// ---------------------------------------------------------------------------
// Presence-driven deadlines
//
// A request has no clock while a host client is connected to answer it, and
// presence changes during the wait. The server arms and disarms, and says
// so with `permissionDeadline` — its own event, because re-sending the
// request would restart the settling interval and throw away a half-typed
// deny reason (permissions.py § Deadline).
// ---------------------------------------------------------------------------

describe('a deadline that arms mid-request', () => {
  function deadline(detail) {
    window.dispatchEvent(new CustomEvent('permission-deadline', { detail }));
  }

  it('starts the countdown when the last host client leaves', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(el.shadowRoot.querySelector('.countdown')).toBeNull();

    deadline({
      permission_id: 'perm_exec',
      request_id: 'req_1',
      expires_at: expiresIn(30),
      localhost_available: false,
    });
    await settle(el);

    expect(el.shadowRoot.querySelector('.countdown').textContent).toContain('0:30');
    expect(el.shadowRoot.querySelector('.no-localhost')).not.toBeNull();
  });

  it('cancels the countdown when a host client comes back', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload({
      expires_at: expiresIn(30), localhost_available: false,
    }));
    expect(el.shadowRoot.querySelector('.countdown')).not.toBeNull();

    deadline({
      permission_id: 'perm_exec',
      request_id: 'req_1',
      expires_at: null,
      localhost_available: true,
    });
    await settle(el);

    expect(el.shadowRoot.querySelector('.countdown')).toBeNull();
    expect(el.shadowRoot.querySelector('.no-localhost')).toBeNull();

    // And the clock that was armed does not fire after being cancelled.
    await tick(el, 60_000);
    expect(el.current?.permission_id).toBe('perm_exec');
  });

  it('announces the clock starting, with the time it really has', async () => {
    // A numeral appearing beside a stopwatch glyph is not something a
    // screen reader picks up, and the first milestone inside a 30 s window
    // is the ten-second one — too late to be the only notice.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    const live = el.shadowRoot.querySelector('[aria-live="polite"]');

    deadline({
      permission_id: 'perm_exec',
      request_id: 'req_1',
      expires_at: expiresIn(30),
      localhost_available: false,
    });
    await settle(el);

    expect(live.textContent).toContain('no host client is connected');
    // "30 seconds left", not the chip's `0:30` — which reads as
    // "zero colon thirty".
    expect(live.textContent).toContain('30 seconds left to answer');
  });

  it('leaves the announcement to the request it promoted', async () => {
    // When arming a clock changes which dialog is on screen, what the
    // reader needs is the new request, not its countdown.
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    broadcast(writePayload());
    await settle(el);

    deadline({
      permission_id: 'perm_write',
      request_id: 'req_1',
      expires_at: expiresIn(30),
      localhost_available: false,
    });
    await settle(el);

    const live = el.shadowRoot.querySelector('[aria-live="polite"]');
    expect(el.current.permission_id).toBe('perm_write');
    expect(live.textContent).toContain('permission request');
    expect(live.textContent).not.toContain('30 seconds left');
  });

  it('keeps the dialog the user is mid-answer on', async () => {
    // The settling interval and the deny reason belong to the request, not
    // to its clock. Re-enqueuing would restart one and discard the other.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    decision(el, 'deny').click();
    await settle(el);
    const field = el.shadowRoot.querySelector('input.deny-reason');
    field.value = 'not on main';
    field.dispatchEvent(new Event('input'));
    await settle(el);

    deadline({
      permission_id: 'perm_exec',
      request_id: 'req_1',
      expires_at: expiresIn(30),
      localhost_available: false,
    });
    await settle(el);

    expect(el._settling).toBe(false);
    expect(el._denyOpen).toBe(true);
    expect(el._denyReason).toBe('not on main');
  });

  it('promotes a counting-down request over an open-ended one', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    broadcast(writePayload());
    await settle(el);
    expect(el.current.permission_id).toBe('perm_exec');

    deadline({
      permission_id: 'perm_write',
      request_id: 'req_1',
      expires_at: expiresIn(30),
      localhost_available: false,
    });
    await settle(el);

    // The one with a clock on it is the one that needs answering first.
    expect(el.current.permission_id).toBe('perm_write');
    // And it gets its own settling interval, because a dialog that appeared
    // under the user's fingers must not be approvable by a keystroke
    // already in flight.
    expect(el._settling).toBe(true);
  });

  it('ignores a deadline for a request it does not have', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    deadline({
      permission_id: 'perm_someone_else',
      expires_at: expiresIn(1),
      localhost_available: false,
    });
    await settle(el);

    expect(el.queue).toHaveLength(1);
    expect(el.shadowRoot.querySelector('.countdown')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Reconnect
// ---------------------------------------------------------------------------

describe('on reconnect', () => {
  it('rebuilds the queue from the server', async () => {
    publishRpc({
      'ClaudeCodeService.get_current_state': async () => ({
        pending_permissions: [
          execPayload({ expires_at: expiresIn(300) }),
          writePayload({ expires_at: expiresIn(45) }),
        ],
      }),
    });
    const el = mount();
    await settle(el);

    expect(el.queue).toHaveLength(2);
    // Restored with the time it actually has left, in expiry order.
    expect(el.current.permission_id).toBe('perm_write');
    expect(el.shadowRoot.querySelector('.countdown').textContent)
      .toContain('0:45');
  });

  it('does not double-queue a request it also hears broadcast', async () => {
    publishRpc({
      'ClaudeCodeService.get_current_state': async () => ({
        pending_permissions: [execPayload()],
      }),
    });
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    expect(el.queue).toHaveLength(1);
  });

  it('stays usable when the snapshot cannot be fetched', async () => {
    publishRpc({
      'ClaudeCodeService.get_current_state': async () => {
        throw new Error('not up yet');
      },
    });
    const el = mount();
    await settle(el);
    // A dialog that cannot rebuild its queue is bad; one that throws on
    // connect and takes the shell's wiring with it is worse.
    await ask(el, execPayload());
    expect(el.current?.permission_id).toBe('perm_exec');
  });
});

// ---------------------------------------------------------------------------
// Authority
// ---------------------------------------------------------------------------

describe('a client that may not decide', () => {
  const participant = {
    'Collab.get_collab_role': async () => ({
      role: 'participant', is_localhost: false, client_id: 'c2',
    }),
  };

  it('sees the whole request and no controls', async () => {
    publishRpc(participant);
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    // The restriction is on authority, not on information: a collaborator
    // who cannot see what the agent asked for cannot review what it did.
    expect(el.shadowRoot.querySelector('pre.command').textContent).toBe('ls -la');
    expect(el.shadowRoot.querySelectorAll('button.decision')).toHaveLength(0);
    expect(el.shadowRoot.querySelector('.read-only-note').textContent)
      .toContain('Only the host can answer this');
  });

  it('cannot deny with Escape either', async () => {
    publishRpc(participant);
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    key(el, 'Escape');
    await settle(el);

    expect(lastResolve()).toBeUndefined();
    expect(el.current).not.toBeNull();
  });

  it('is offered no edit affordance', async () => {
    publishRpc(participant);
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(el.shadowRoot.querySelector('.edit-toggle')).toBeNull();
  });

  it('loses its controls if the server rejects a decision anyway', async () => {
    // The server enforces the real gate; being wrong in the browser costs
    // a rejected call, not an unauthorised one.
    publishRpc({
      'ClaudeCodeService.resolve_permission': async () => ({
        error: 'restricted',
        reason: 'Participants cannot perform this action',
      }),
    });
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    decision(el, 'allow').click();
    await settle(el);

    expect(el._canDecide).toBe(false);
    expect(toasts.map((t) => t.message).join(' '))
      .toContain('Only the host can answer');
  });

  it('takes authority from a role-changed broadcast', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    expect(decision(el, 'allow')).not.toBeNull();

    window.dispatchEvent(new CustomEvent('role-changed', {
      detail: { role: 'participant', is_localhost: false },
    }));
    await settle(el);

    expect(el.shadowRoot.querySelectorAll('button.decision')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// write — the diff is the feature
// ---------------------------------------------------------------------------

describe('an edit', () => {
  it('renders a diff editor rather than a tool name and a JSON blob', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());

    expect(el.shadowRoot.querySelector('.diff-host')).not.toBeNull();
    expect(monacoState.editors).toHaveLength(1);
    const models = monacoState.editors[0].getModel();
    expect(models.original.getValue()).toBe('print(1)\n');
    expect(models.modified.getValue()).toBe('print(2)\n');
  });

  it('opens scrolled to the first change', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());

    monacoState.editors[0]._simulateDiffComputed([
      { modifiedStartLineNumber: 42, originalStartLineNumber: 40 },
    ]);
    // A dialog that opened on line 1 of a 2,000-line file would ask the
    // user to hunt for the change they are approving.
    expect(monacoState.editors[0]._revealed).toEqual([42]);
  });

  it('shows the diff stats alongside it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());
    const stats = el.shadowRoot.querySelector('.detail-strip .stats');
    expect(stats.textContent).toContain('+1');
    expect(stats.textContent).toContain('−1');
  });

  it('is read-only until the user asks to edit it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());
    expect(monacoState.editors[0]._options.readOnly).toBe(true);

    el.shadowRoot.querySelector('.edit-toggle').click();
    await settle(el);
    expect(monacoState.editors[0]._options.readOnly).toBe(false);
  });

  it('swaps Allow once for Allow with edits once it is changed', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());
    expect(decision(el, 'allow').textContent.trim()).toBe('Allow once');

    el.shadowRoot.querySelector('.edit-toggle').click();
    await settle(el);
    monacoState.editors[0]._simulateEdit('print(3)\n');
    await settle(el);

    // The difference between approving what the agent asked for and
    // approving something else must be unmistakable.
    expect(decision(el, 'allow').textContent.trim()).toBe('Allow with edits');
  });

  it('sends the edited content as the updated input', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());

    el.shadowRoot.querySelector('.edit-toggle').click();
    await settle(el);
    monacoState.editors[0]._simulateEdit('print(3)\n');
    await settle(el);
    decision(el, 'allow').click();
    await settle(el);

    expect(lastResolve().args[1]).toEqual({
      action: 'allow',
      updated_input: { file_path: 'src/main.py', content: 'print(3)\n' },
    });
  });

  it('tells the transcript what actually ran', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());

    const edits = [];
    const listener = (event) => edits.push(event.detail);
    window.addEventListener('permission-input-edited', listener);
    try {
      el.shadowRoot.querySelector('.edit-toggle').click();
      await settle(el);
      monacoState.editors[0]._simulateEdit('print(3)\n');
      await settle(el);
      decision(el, 'allow').click();
      await settle(el);
    } finally {
      window.removeEventListener('permission-input-edited', listener);
    }

    // A transcript showing the agent's original proposal while something
    // else ran would lie about the repository's history.
    expect(edits).toHaveLength(1);
    expect(edits[0]).toMatchObject({
      permission_id: 'perm_write',
      tool_use_id: 'toolu_write',
      tool_name: 'Write',
      updated_input: { content: 'print(3)\n' },
    });
  });

  it('offers no edit affordance for a replacement-based edit', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({
      permission_id: 'perm_edit',
      tool_name: 'Edit',
      display_name: 'Edit',
      input: { file_path: 'src/main.py', old_string: 'print(1)', new_string: 'print(2)' },
    }));
    // An edited full-file pane cannot be reduced back to old_string /
    // new_string without guessing, and a call that ran something other
    // than what the dialog showed would be worse than no edit affordance.
    expect(el.shadowRoot.querySelector('.diff-host')).not.toBeNull();
    expect(el.shadowRoot.querySelector('.edit-toggle')).toBeNull();
  });

  it('names the case when a file is binary instead of showing nothing', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({
      diff: { path: 'logo.png', is_binary: true, proposed: null },
    }));
    const body = el.shadowRoot.querySelector('.body');
    expect(body.textContent).toContain('logo.png');
    expect(body.textContent).toContain('binary — cannot be shown');
    expect(el.shadowRoot.querySelector('.diff-host')).toBeNull();
  });

  it('names the case when the proposal cannot be computed', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({
      diff: { path: 'src/main.py', original: 'print(1)\n', proposed: null },
    }));
    // Showing a guessed diff would show a change the agent is not asking for.
    expect(el.shadowRoot.querySelector('.body').textContent)
      .toContain('does not match the file on disk');
    expect(monacoState.editors).toHaveLength(0);
  });

  it('shows the whole proposed file for a new one', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload({
      diff: {
        path: 'src/new.py', original: '', proposed: 'print(0)\n',
        additions: 1, deletions: 0, is_new_file: true,
      },
    }));
    expect(el.shadowRoot.querySelector('pre.new-file-pane').textContent)
      .toBe('print(0)\n');
  });

  it('gives the editor the Monaco styles that live outside its shadow root', async () => {
    // Monaco writes its rules to document.head, which a shadow root cannot
    // see. Without the clones the dialog drew a diff at the dialog's own font
    // with Monaco's line pitch: line numbers piled onto one row, every line
    // wrapped, nothing highlighted.
    const head = headStyle('.monaco-editor { color: red }');
    try {
      publishRpc();
      const el = mount();
      await settle(el);
      await ask(el, writePayload());

      expect(clonedStyleText(el)).toContain('.monaco-editor { color: red }');
    } finally {
      head.remove();
    }
  });

  it('picks up a stylesheet added after the editor was built', async () => {
    // Monaco emits its theme rules during construction and later on a theme
    // change; a one-shot clone would miss everything after the first pass.
    const first = headStyle('.first { color: red }');
    let late = null;
    try {
      publishRpc();
      const el = mount();
      await settle(el);
      await ask(el, writePayload());

      late = headStyle('.late { color: blue }');
      await settle(el);
      expect(clonedStyleText(el)).toContain('.late { color: blue }');
    } finally {
      first.remove();
      late?.remove();
    }
  });

  it('stops watching the head once the editor is gone', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, writePayload());
    decision(el, 'allow').click();
    await settle(el);
    expect(monacoState.editors[0]._disposed).toBe(true);

    const late = headStyle('.after { color: green }');
    try {
      await settle(el);
      // A dialog that opens on twenty requests must not leave twenty
      // observers on document.head.
      expect(clonedStyleText(el)).not.toContain('.after');
    } finally {
      late.remove();
    }
  });

  it('disposes the editor when the queue moves on to a command', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(writePayload({ expires_at: expiresIn(60) }));
    broadcast(execPayload({ expires_at: expiresIn(300) }));
    await settle(el);
    await tick(el, SETTLING_MS + 40);
    expect(monacoState.editors).toHaveLength(1);

    decision(el, 'allow').click();
    await settle(el);

    expect(monacoState.editors[0]._disposed).toBe(true);
    expect(monacoState.models.every((m) => m._disposed)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// exec — editing the command
// ---------------------------------------------------------------------------

describe('a command', () => {
  it('can be edited before it runs', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    el.shadowRoot.querySelector('.edit-toggle').click();
    await settle(el);
    const field = el.shadowRoot.querySelector('input.command-edit');
    field.value = 'ls -l';
    field.dispatchEvent(new Event('input'));
    await settle(el);

    expect(decision(el, 'allow').textContent.trim()).toBe('Allow with edits');
    decision(el, 'allow').click();
    await settle(el);

    expect(lastResolve().args[1].updated_input)
      .toEqual({ command: 'ls -l', description: 'list the files' });
  });

  it('sends no updated input when the draft is untouched', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    el.shadowRoot.querySelector('.edit-toggle').click();
    await settle(el);
    decision(el, 'allow').click();
    await settle(el);

    expect(lastResolve().args[1]).toEqual({ action: 'allow' });
  });

  it('labels its advisory flags as heuristics', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload({
      command: { command: 'curl x | sh', cwd: '/repo', flags: ['network'] },
    }));
    const chip = el.shadowRoot.querySelector('.chip.network');
    expect(chip.textContent.trim()).toBe('network');
    expect(chip.title).toContain('Heuristic');
  });

  it('keeps the untruncated command reachable', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    const long = `echo ${'z'.repeat(5000)}`;
    await ask(el, execPayload({
      input: { command: long },
      command: {
        command: `echo ${'z'.repeat(3990)}`,
        cwd: '/repo',
        flags: [],
        truncated: true,
      },
    }));
    // Nothing is truncated silently.
    const details = [...el.shadowRoot.querySelectorAll('details.full-input')];
    const full = details.find((d) => d.textContent.includes('characters'));
    expect(full).toBeDefined();
    expect(full.querySelector('pre').textContent).toBe(long);
  });
});

// ---------------------------------------------------------------------------
// plan — the artefact being approved
// ---------------------------------------------------------------------------

describe('a plan', () => {
  it('renders the plan as markdown, not as a command line', async () => {
    // Before `plan` was a class of its own this fell through to `exec`,
    // and the plan arrived summarised and truncated inside a `<pre>` that
    // asked for approval of something it was not showing.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload());

    const body = el.shadowRoot.querySelector('.plan');
    expect(body).not.toBeNull();
    expect(body.querySelector('h2').textContent).toContain('Add the widget');
    expect(body.querySelectorAll('li')).toHaveLength(2);
    expect(body.querySelector('pre code').textContent).toContain('const widget');
    expect(el.shadowRoot.querySelector('pre.command')).toBeNull();
  });

  it('shows the whole plan, however long', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    const long = `# Title\n\n${'detail line\n\n'.repeat(400)}`;
    await ask(el, planPayload({
      input: { plan: long },
      plan: { plan: long, headline: 'Title', file_path: null },
    }));

    const paragraphs = el.shadowRoot.querySelectorAll('.plan p');
    expect(paragraphs).toHaveLength(400);
  });

  it('names the file the CLI read the plan from', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload({
      plan: { plan: PLAN_TEXT, headline: 'Add the widget', file_path: '/tmp/plan-1.md' },
    }));
    expect(el.shadowRoot.textContent).toContain('/tmp/plan-1.md');
  });

  it('says so plainly when the call carries no plan', async () => {
    // `plan` is optional in the CLI's own schema — it is injected from
    // disk — so an absent one is a real case, and a blank body over an
    // Approve button asks for approval of nothing.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload({ input: {}, plan: null }));

    const shown = el.shadowRoot.querySelector('.not-shown');
    expect(shown.textContent).toContain('no plan text');
    expect(el.shadowRoot.querySelector('.plan')).toBeNull();
  });

  it('labels the primary action for what it does', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload());
    // Not "Allow once": what is approved is the plan, and what happens
    // next is the agent starting on it.
    expect(decision(el, 'allow').textContent.trim()).toBe('Approve plan');
  });

  it('identifies itself by the plan\'s own first line', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload({ title: 'Claude wants to exit plan mode' }));
    expect(el.shadowRoot.querySelector('.target').textContent)
      .toContain('Add the widget');
  });

  it('focuses Approve, because a proposal is not an action', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload());
    expect(el.shadowRoot.activeElement).toBe(decision(el, 'allow'));
  });

  it('offers no standing grant for future plans', async () => {
    // A rule allowing `ExitPlanMode` would approve every later plan
    // unread, which is the one thing this dialog is for.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, planPayload());
    expect(decision(el, 'allow-always')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// interact — real choices
// ---------------------------------------------------------------------------

describe('a question', () => {
  it('renders its options as controls', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    expect(el.shadowRoot.querySelector('.question').textContent)
      .toContain('Which branch?');
    const inputs = el.shadowRoot.querySelectorAll('.options input');
    expect(inputs).toHaveLength(2);
    expect(inputs[0].type).toBe('radio');
  });

  it('offers no always-allow, because no rule can answer a question', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({
      suggested_rules: [{ tool_name: 'AskUserQuestion', origin: 'derived' }],
    }));
    expect(decision(el, 'allow-always')).toBeNull();
  });

  it('cannot be answered until an option is chosen', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    const answer = decision(el, 'allow');
    expect(answer.textContent.trim()).toBe('Answer');
    expect(answer.disabled).toBe(true);

    const option = el.shadowRoot.querySelectorAll('.options input')[1];
    option.checked = true;
    option.dispatchEvent(new Event('change'));
    await settle(el);

    expect(decision(el, 'allow').disabled).toBe(false);
    decision(el, 'allow').click();
    await settle(el);

    // One entry per question: the options ticked, plus whatever was typed
    // into that question's own reply field.
    expect(lastResolve().args[1]).toEqual({
      action: 'allow', answers: [{ options: [1], text: '', notes: '' }],
    });
  });

  it('keeps a single-select single', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    const options = el.shadowRoot.querySelectorAll('.options input');
    options[0].checked = true;
    options[0].dispatchEvent(new Event('change'));
    await settle(el);
    options[1].checked = true;
    options[1].dispatchEvent(new Event('change'));
    await settle(el);

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([{ options: [1], text: '', notes: '' }]);
  });

  it('lets a multi-select take several', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({
      question: {
        question: 'Which files?',
        multi_select: true,
        options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
        questions: [{
          question: 'Which files?',
          multi_select: true,
          options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
        }],
      },
    }));

    const options = el.shadowRoot.querySelectorAll('.options input');
    expect(options[0].type).toBe('checkbox');
    for (const index of [0, 2]) {
      options[index].checked = true;
      options[index].dispatchEvent(new Event('change'));
      await settle(el);
    }

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([{ options: [0, 2], text: '', notes: '' }]);
  });

  it('renders every question the call asked, not just the first', async () => {
    // `AskUserQuestion` takes up to four. A dialog that shows one leaves
    // the rest unanswered without ever telling the user they existed.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({
      question: {
        question: 'Which branch?',
        multi_select: false,
        options: [{ label: 'main' }, { label: 'dev5' }],
        questions: [
          {
            question: 'Which branch?',
            header: 'Branch',
            multi_select: false,
            options: [{ label: 'main' }, { label: 'dev5' }],
          },
          {
            question: 'Run the tests after?',
            header: 'Tests',
            multi_select: false,
            options: [{ label: 'yes' }, { label: 'no' }],
          },
        ],
      },
    }));

    const groups = el.shadowRoot.querySelectorAll('.question-group');
    expect(groups).toHaveLength(2);
    expect(groups[1].querySelector('.question').textContent)
      .toContain('Run the tests after?');
    expect([...el.shadowRoot.querySelectorAll('.question-header')]
      .map((chip) => chip.textContent.trim())).toEqual(['Branch', 'Tests']);
  });

  it('waits for an answer to every question before it can be sent', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({
      question: {
        question: 'Which branch?',
        multi_select: false,
        options: [{ label: 'main' }, { label: 'dev5' }],
        questions: [
          {
            question: 'Which branch?',
            multi_select: false,
            options: [{ label: 'main' }, { label: 'dev5' }],
          },
          {
            question: 'Run the tests after?',
            multi_select: true,
            options: [{ label: 'yes' }, { label: 'no' }],
          },
        ],
      },
    }));

    const choose = async (group, index) => {
      const input = el.shadowRoot
        .querySelector(`.options[data-question="${group}"]`)
        .querySelectorAll('input')[index];
      input.checked = true;
      input.dispatchEvent(new Event('change'));
      await settle(el);
    };

    await choose(0, 1);
    // One of two answered is still not answered.
    expect(decision(el, 'allow').disabled).toBe(true);
    await choose(1, 0);
    expect(decision(el, 'allow').disabled).toBe(false);

    decision(el, 'allow').click();
    await settle(el);
    // One entry per question, in the order the call asked them.
    expect(lastResolve().args[1].answers).toEqual([
      { options: [1], text: '', notes: '' },
      { options: [0], text: '', notes: '' },
    ]);
  });

  it('keeps each radio group to its own question', async () => {
    // Shared `name` attributes would make a second question's selection
    // clear the first one's, and the browser would enforce it.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({
      question: {
        question: 'A?',
        multi_select: false,
        options: [{ label: 'a1' }, { label: 'a2' }],
        questions: [
          { question: 'A?', multi_select: false, options: [{ label: 'a1' }, { label: 'a2' }] },
          { question: 'B?', multi_select: false, options: [{ label: 'b1' }, { label: 'b2' }] },
        ],
      },
    }));

    const names = [...el.shadowRoot.querySelectorAll('.options input')]
      .map((input) => input.name);
    expect(new Set(names).size).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// interact — the freeform reply
// ---------------------------------------------------------------------------

describe('a question the options do not answer', () => {
  /** Type into one question's freeform field. */
  async function typeReply(el, group, text) {
    const field = el.shadowRoot
      .querySelector(`.other-answer[data-question="${group}"]`);
    field.value = text;
    field.dispatchEvent(new Event('input'));
    await settle(el);
    return field;
  }

  async function choose(el, group, index) {
    const input = el.shadowRoot
      .querySelector(`.options[data-question="${group}"]`)
      .querySelectorAll('input')[index];
    input.checked = true;
    input.dispatchEvent(new Event('change'));
    await settle(el);
    return input;
  }

  const twoQuestions = {
    question: 'Which branch?',
    multi_select: false,
    options: [{ label: 'main' }, { label: 'dev5' }],
    questions: [
      {
        question: 'Which branch?',
        multi_select: false,
        options: [{ label: 'main' }, { label: 'dev5' }],
      },
      {
        question: 'Which files?',
        multi_select: true,
        options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
      },
    ],
  };

  it('offers a reply field per question', async () => {
    // The terminal always offers one, because the tool tells the model not
    // to write an "Other" option — the front end is expected to provide it.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({ question: twoQuestions }));

    const fields = [...el.shadowRoot.querySelectorAll('.other-answer')];
    expect(fields).toHaveLength(2);
    expect(fields[1].getAttribute('aria-label')).toContain('Which files?');
  });

  it('is answered by the typed reply alone', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    expect(decision(el, 'allow').disabled).toBe(true);
    await typeReply(el, 0, 'a branch you have not listed');
    expect(decision(el, 'allow').disabled).toBe(false);

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([
      { options: [], text: 'a branch you have not listed', notes: '' },
    ]);
  });

  it('does not count whitespace as an answer', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    await typeReply(el, 0, '   ');
    expect(decision(el, 'allow').disabled).toBe(true);
  });

  it('replaces a single-select option when the user types instead', async () => {
    // "Other" is one of the choices in a radio group, not an addition to
    // it. Sending both would answer the question twice.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    const option = await choose(el, 0, 1);
    await typeReply(el, 0, 'neither');
    expect(option.checked).toBe(false);
    expect(el.shadowRoot.querySelector('.other-note').textContent)
      .toContain('instead of the options');

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([
      { options: [], text: 'neither', notes: '' },
    ]);
  });

  it('clears the typed reply when a single-select option is picked', async () => {
    // The exclusion has to hold both ways round, or the last control the
    // user touched is not the one that gets sent.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    const field = await typeReply(el, 0, 'neither');
    await choose(el, 0, 0);
    expect(field.value).toBe('');

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([
      { options: [0], text: '', notes: '' },
    ]);
  });

  it('adds to a multi-select rather than replacing it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({ question: twoQuestions }));

    await choose(el, 0, 0);
    const ticked = await choose(el, 1, 2);
    await typeReply(el, 1, 'and d');
    expect(ticked.checked).toBe(true);
    expect(el.shadowRoot.querySelector('.other-note').textContent)
      .toContain('alongside anything ticked');

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([
      { options: [0], text: '', notes: '' },
      { options: [2], text: 'and d', notes: '' },
    ]);
  });

  it('forgets what was typed when the next request arrives', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());
    await typeReply(el, 0, 'something');
    decision(el, 'allow').click();
    await settle(el);

    await ask(el, interactPayload({
      permission_id: 'perm_ask_2', tool_use_id: 'toolu_ask_2',
    }));
    expect(el.shadowRoot.querySelector('.other-answer').value).toBe('');
    expect(decision(el, 'allow').disabled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// interact — the note on an answer
// ---------------------------------------------------------------------------

describe('a note on an answer', () => {
  const twoQuestions = {
    question: 'Which branch?',
    multi_select: false,
    options: [{ label: 'main' }, { label: 'dev5' }],
    questions: [
      {
        question: 'Which branch?',
        multi_select: false,
        options: [{ label: 'main' }, { label: 'dev5' }],
      },
      {
        question: 'Which files?',
        multi_select: true,
        options: [{ label: 'a' }, { label: 'b' }],
      },
    ],
  };

  const note = (el, group = 0) => el.shadowRoot
    .querySelector(`.answer-note[data-question="${group}"]`);

  async function choose(el, group, index) {
    const input = el.shadowRoot
      .querySelector(`.options[data-question="${group}"]`)
      .querySelectorAll('input')[index];
    input.checked = true;
    input.dispatchEvent(new Event('change'));
    await settle(el);
    return input;
  }

  async function typeNote(el, group, text) {
    const field = note(el, group);
    field.value = text;
    field.dispatchEvent(new Event('input'));
    await settle(el);
    return field;
  }

  async function typeReply(el, group, text) {
    const field = el.shadowRoot
      .querySelector(`.other-answer[data-question="${group}"]`);
    field.value = text;
    field.dispatchEvent(new Event('input'));
    await settle(el);
    return field;
  }

  it('is not offered until there is an answer to annotate', async () => {
    // Two empty text inputs on an unanswered question invites the answer
    // to be typed into the wrong one.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    expect(note(el)).toBeNull();
    await choose(el, 0, 1);
    expect(note(el)).not.toBeNull();
  });

  it('appears for a question answered by a typed reply too', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    await typeReply(el, 0, 'some other branch');
    expect(note(el)).not.toBeNull();
  });

  it('travels beside the answer it belongs to', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({ question: twoQuestions }));

    await choose(el, 0, 1);
    await choose(el, 1, 0);
    await typeNote(el, 0, 'the release branch, not the trunk');

    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([
      { options: [1], text: '', notes: 'the release branch, not the trunk' },
      { options: [0], text: '', notes: '' },
    ]);
  });

  it('cannot answer the question on its own', async () => {
    // A note annotates an answer. A dialog holding nothing but notes has
    // still not been answered, so Answer stays disabled — and the field
    // only exists once answered, so this is reached the long way round.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    const option = await choose(el, 0, 1);
    await typeNote(el, 0, 'a thought');
    option.checked = false;
    option.dispatchEvent(new Event('change'));
    await settle(el);

    expect(decision(el, 'allow').disabled).toBe(true);
    expect(note(el)).toBeNull();
  });

  it('survives switching from one option to another', async () => {
    // Unlike the reply field, which is an alternative to the options, a
    // note is about the question — so changing your mind keeps it.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    await choose(el, 0, 0);
    await typeNote(el, 0, 'either would do');
    await choose(el, 0, 1);

    expect(note(el).value).toBe('either would do');
    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers[0]).toEqual(
      { options: [1], text: '', notes: 'either would do' },
    );
  });

  it('is labelled by the question it annotates', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload({ question: twoQuestions }));
    await choose(el, 1, 0);

    expect(note(el, 1).getAttribute('aria-label')).toContain('Which files?');
    expect(note(el, 1).getAttribute('aria-label')).toContain('note on your answer');
  });

  it('forgets the note when the next request arrives', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());
    await choose(el, 0, 1);
    await typeNote(el, 0, 'carried over would be wrong');
    decision(el, 'allow').click();
    await settle(el);

    await ask(el, interactPayload({
      permission_id: 'perm_ask_2', tool_use_id: 'toolu_ask_2',
    }));
    expect(note(el)).toBeNull();
    await choose(el, 0, 0);
    expect(note(el).value).toBe('');
  });
});

// ---------------------------------------------------------------------------
// interact — comparing examples
// ---------------------------------------------------------------------------

describe('a question offering examples', () => {
  const MOCKUP_A = '┌──────┐\n│ left │\n└──────┘';
  const MOCKUP_B = '```js\nconst layout = "right";\n```';

  /** A single-select question whose options carry `preview` content. */
  function compared(over = {}) {
    const question = {
      question: 'Which layout?',
      header: 'Layout',
      multi_select: false,
      options: [
        { label: 'Sidebar', description: 'nav on the left', preview: MOCKUP_A },
        { label: 'Topbar', description: 'nav across the top', preview: MOCKUP_B },
      ],
      ...over,
    };
    return interactPayload({
      question: { ...question, questions: [question] },
    });
  }

  const pane = (el) => el.shadowRoot.querySelector('.option-preview');
  const paneBody = (el) => el.shadowRoot.querySelector('.option-preview-body');
  const optionLabels = (el) => [...el.shadowRoot.querySelectorAll('.option')];

  async function hover(el, index) {
    optionLabels(el)[index].dispatchEvent(new MouseEvent('mouseenter'));
    await settle(el);
  }

  it('puts the options and the example side by side', async () => {
    // The whole point of the field is a comparison, and a comparison needs
    // both things on screen at once — which is the layout the terminal
    // switches to for the same call.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    const compare = el.shadowRoot.querySelector('.question-compare');
    expect(compare).not.toBeNull();
    expect(compare.querySelector('.options')).not.toBeNull();
    expect(compare.querySelector('.option-preview')).not.toBeNull();
  });

  it('leaves the ordinary question in one column', async () => {
    // Most calls are a question and two labels. An empty pane beside those
    // reads as a pane that failed to load.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, interactPayload());

    expect(el.shadowRoot.querySelector('.question-compare')).toBeNull();
    expect(pane(el)).toBeNull();
    expect(el.shadowRoot.querySelector('.options')).not.toBeNull();
  });

  it('opens on the first example rather than an empty pane', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Sidebar');
    expect(paneBody(el).innerHTML).toContain('left');
    // The pane names the option it belongs to, and the option is marked —
    // two carriers, so neither colour nor position is doing it alone.
    expect(optionLabels(el)[0].classList.contains('showing')).toBe(true);
    expect(pane(el).getAttribute('aria-label')).toContain('Sidebar');
  });

  it('follows the pointer, so comparing costs no clicks', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    await hover(el, 1);
    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Topbar');
    expect(paneBody(el).textContent).toContain('const layout');
    expect(optionLabels(el)[1].classList.contains('showing')).toBe(true);
    expect(optionLabels(el)[0].classList.contains('showing')).toBe(false);

    // And nothing has been answered by looking at it.
    expect(decision(el, 'allow').disabled).toBe(true);
  });

  it('follows keyboard focus too', async () => {
    // Arrow-keying a radio group is how this is read without a mouse.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    const inputs = el.shadowRoot.querySelectorAll('.options input');
    inputs[1].dispatchEvent(new FocusEvent('focus'));
    await settle(el);
    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Topbar');
  });

  it('shows what was picked once an option is picked', async () => {
    // Otherwise the pane keeps the example of the option before it, beside
    // a radio that is filled on a different one.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    const input = el.shadowRoot.querySelectorAll('.options input')[1];
    input.checked = true;
    input.dispatchEvent(new Event('change'));
    await settle(el);

    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Topbar');
    decision(el, 'allow').click();
    await settle(el);
    expect(lastResolve().args[1].answers).toEqual([{ options: [1], text: '', notes: '' }]);
  });

  it('renders the example as markdown, and keeps its line breaks', async () => {
    // The engine asks the CLI for `previewFormat: "markdown"`, so a fenced
    // block is a code block — and an unfenced mockup keeps the lines its
    // author drew, because a box redrawn as one paragraph is not a mockup.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());

    expect(paneBody(el).innerHTML).toContain('<br');
    await hover(el, 1);
    expect(paneBody(el).querySelector('pre')).not.toBeNull();
  });

  it('shows an option with no example of its own without emptying the pane', async () => {
    // Three of four options carrying examples is a shape the tool permits.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared({
      options: [
        { label: 'Sidebar', preview: MOCKUP_A },
        { label: 'Neither', description: 'leave it alone' },
      ],
    }));

    await hover(el, 1);
    expect(paneBody(el).textContent).toContain('left');
    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Sidebar');
  });

  it('puts a multi-select example under its own option instead', async () => {
    // The pane cannot serve a multi-select: several options can be ticked
    // and "which example" has no answer, which is why the tool tells the
    // model previews are single-select only. A model that sends one anyway
    // has authored something the user is deciding about, so it is shown
    // rather than dropped.
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared({ multi_select: true }));

    expect(el.shadowRoot.querySelector('.question-compare')).toBeNull();
    const bodies = el.shadowRoot.querySelectorAll('.option .option-preview-body');
    expect(bodies).toHaveLength(2);
    expect(bodies[0].textContent).toContain('left');
    expect(bodies[1].querySelector('pre')).not.toBeNull();
  });

  it('forgets which example was on screen when the next request arrives', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, compared());
    await hover(el, 1);
    const input = el.shadowRoot.querySelectorAll('.options input')[1];
    input.checked = true;
    input.dispatchEvent(new Event('change'));
    await settle(el);
    decision(el, 'allow').click();
    await settle(el);

    const next = compared();
    await ask(el, {
      ...next, permission_id: 'perm_ask_2', tool_use_id: 'toolu_ask_2',
    });
    expect(el.shadowRoot.querySelector('.option-preview-label').textContent)
      .toContain('Sidebar');
  });
});

// ---------------------------------------------------------------------------
// Full input
// ---------------------------------------------------------------------------

describe('the verbatim input', () => {
  it('is always reachable, whatever the summary shows', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());
    const json = el.shadowRoot.querySelector('details.full-input pre.json');
    expect(JSON.parse(json.textContent)).toEqual({
      command: 'ls -la', description: 'list the files',
    });
  });
});

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

describe('teardown', () => {
  it('gives the page title back when the element goes away', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);
    expect(document.title).not.toBe(ORIGINAL_TITLE);

    el.remove();
    await settle(el);
    expect(document.title).toBe(ORIGINAL_TITLE);
  });

  it('stops listening once removed', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    el.remove();
    await settle(el);

    broadcast(execPayload());
    await settle(el);
    expect(el.queue).toHaveLength(0);
  });
});
