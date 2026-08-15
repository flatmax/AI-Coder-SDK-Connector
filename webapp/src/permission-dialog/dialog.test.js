// Tests for <ac-permission-dialog>.
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
const ORIGINAL_TITLE = 'AC-DC — test';

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
  const el = document.createElement('ac-permission-dialog');
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

function lastResolve() {
  const calls = rpcCalls.filter(
    (c) => c.method === 'ClaudeCodeService.resolve_permission',
  );
  return calls[calls.length - 1];
}

// ---------------------------------------------------------------------------
// Payload factories — shapes from src/ac_dc/claude_code/permissions.py
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
    expires_at: expiresIn(300),
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
    expires_at: expiresIn(300),
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
    expires_at: expiresIn(300),
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
    expires_at: expiresIn(300),
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
  window.addEventListener('ac-toast', onToast);
  document.title = ORIGINAL_TITLE;
  monacoState.editors = [];
  monacoState.models = [];
});

afterEach(() => {
  window.removeEventListener('ac-toast', onToast);
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
  it('shows the tool, the target, and a countdown', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload());
    await settle(el);

    const header = el.shadowRoot.querySelector('header');
    expect(header.textContent).toContain('Bash');
    expect(header.textContent).toContain('ls -la');
    expect(header.querySelector('.countdown').textContent).toContain('5:00');
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

  it('says so when no host was connected, because the deadline is shorter', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ localhost_available: false, expires_at: expiresIn(30) }));
    await settle(el);
    expect(el.shadowRoot.querySelector('.no-localhost')).not.toBeNull();
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
      tool_name: 'mcp__ac-dc__search',
      tool_class: 'mcp',
      server: 'ac-dc',
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

  it('marks a rule AC-DC guessed rather than one the CLI suggested', async () => {
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
    // AC-DC never invents one: a rule grants one path and can be read back
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

  it('says when the clock ran out rather than a person answering', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({
      permission_id: 'perm_exec', action: 'timeout', tool_use_id: 'toolu_exec',
    });
    await settle(el);

    expect(toasts[0].message).toContain('expired');
    expect(toasts[0].type).toBe('warning');
  });

  it('says when a shutdown denied it', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    await ask(el, execPayload());

    resolveBroadcast({ permission_id: 'perm_exec', action: 'shutdown' });
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

  it('closes a request whose clock ran out, and says why', async () => {
    publishRpc();
    const el = mount();
    await settle(el);
    broadcast(execPayload({ expires_at: expiresIn(5) }));
    await settle(el);

    await tick(el, 6_000);

    expect(el.current).toBeNull();
    expect(toasts.map((t) => t.message).join(' '))
      .toContain('expired — denied for want of an answer');
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
      action: 'allow', answers: [{ options: [1], text: '' }],
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
    expect(lastResolve().args[1].answers).toEqual([{ options: [1], text: '' }]);
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
    expect(lastResolve().args[1].answers).toEqual([{ options: [0, 2], text: '' }]);
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
      { options: [1], text: '' },
      { options: [0], text: '' },
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
      { options: [], text: 'a branch you have not listed' },
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
      { options: [], text: 'neither' },
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
      { options: [0], text: '' },
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
      { options: [0], text: '' },
      { options: [2], text: 'and d' },
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
