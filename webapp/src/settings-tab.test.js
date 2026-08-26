import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import './settings-tab.js';
import { fieldLineRange } from './settings-tab.js';
import { SharedRpc } from './rpc.js';

// -----------------------------------------------------------
// Test harness
// -----------------------------------------------------------
//
// Follows the pattern from chat-panel.test.js: install a
// flat object as the SharedRpc proxy, keyed by
// "Service.method" names. Each handler returns a
// single-key envelope `{ fake: value }` matching
// jrpc-oo's multi-remote shape — rpcExtract unwraps
// the single key automatically.

const _mounted = [];

function mountTab() {
  const el = document.createElement('aic-settings-tab');
  document.body.appendChild(el);
  _mounted.push(el);
  return el;
}

// Answered on every mount, so every describe gets them whether or
// not it cares: `onRpcReady` reads the model panel's state
// unconditionally, and a proxy without these logs a "method not
// found" warning per test for a call the test never asked about.
// Overridable — a later key of the same name wins.
const _MODEL_DEFAULTS = {
  'ClaudeCodeService.get_model': () => ({
    model: null,
    resolved: null,
    models: [],
  }),
  'Collab.get_collab_role': () => ({ is_localhost: true }),
};

function publishFakeRpc(methods) {
  const proxy = {};
  for (const [name, impl] of Object.entries({ ..._MODEL_DEFAULTS, ...methods })) {
    proxy[name] = async (...args) => {
      const value = await impl(...args);
      return { fake: value };
    };
  }
  SharedRpc.set(proxy);
}

/** The seven-ish entries the live CLI advertises, trimmed to three. */
const MODELS = [
  {
    value: 'default',
    resolvedModel: 'au.anthropic.claude-opus-5',
    displayName: 'Default',
    description: 'Use the default model (currently Opus 5)',
  },
  {
    value: 'opus',
    resolvedModel: 'au.anthropic.claude-opus-5',
    displayName: 'Opus (custom)',
    description: 'Custom Opus model',
  },
  {
    value: 'haiku',
    resolvedModel: 'au.anthropic.claude-haiku-4-5-20251001-v1:0',
    displayName: 'Haiku',
    description: 'Haiku 4.5 - Fastest for quick answers',
  },
];

function modelSelect(el) {
  return el.shadowRoot.querySelector('.model-select');
}

function modelPanel(el) {
  return el.shadowRoot.querySelector('.model-panel');
}

/** A selection the way a user makes one: gesture, then change. */
function pickModel(el, value) {
  const select = modelSelect(el);
  select.dispatchEvent(new Event('pointerdown'));
  select.value = value;
  select.dispatchEvent(new Event('change'));
  return select;
}

async function settle(el) {
  // Three round-trips to let async RPC handlers complete.
  // _loadToggles does: await rpcExtract → update _toggles
  //   → Lit re-render. Each await yields to the
  //   microtask queue once; we need at least two passes
  //   to let the chain settle, plus a final updateComplete.
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
}

afterEach(() => {
  while (_mounted.length) {
    const el = _mounted.pop();
    el.remove();
  }
  SharedRpc.reset();
});

// -----------------------------------------------------------
// Helpers to reach into the shadow tree
// -----------------------------------------------------------

// -----------------------------------------------------------
// Tests
// -----------------------------------------------------------

// -----------------------------------------------------------
// The card the settings tab no longer has
// -----------------------------------------------------------
//
// Eighteen tests used to live here: a switch bound to
// `app.json` -> `agents.enabled`, plus a lock suite pinning
// that clicks no-op while `locked: true` held and the launcher
// had not passed `--experimental`.
//
// The toggle gated whether AIC(zap)DC told the model about its
// `AGENT` spawn protocol. There is no protocol left to gate:
// the agent's `Task` tool is part of the platform and is not
// ours to switch off, and a user who wants to constrain
// delegation writes a `Task` deny rule in project settings.
// The switch, its config key and the `--experimental` flag
// that unlocked it are all gone.
//
// This test replaces them. The removal has to be provable from
// the tab's own rendering, not from the absence of a card
// definition — a re-added CONFIG_CARDS entry with a stale
// `renderer: 'toggle'` would render a card with no switch
// machinery behind it and fail silently.

describe('aic-settings-tab has no preference switches yet', () => {
  beforeEach(() => {
    publishFakeRpc({
      'Settings.get_config_info': () => ({ config_dir: '/tmp/config' }),
      'Settings.get_config_content': (key) => ({
        type: key,
        content: '{}',
      }),
    });
  });

  it('renders no toggle card, switch or agentic label', async () => {
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.card.toggle-card')).toBeNull();
    expect(el.shadowRoot.querySelector('.toggle-switch')).toBeNull();
    expect(el.shadowRoot.querySelector('[role="switch"]')).toBeNull();
    expect(el.shadowRoot.textContent).not.toContain('Agentic');
  });

  it('never reads app.json to hydrate a switch', async () => {
    // `_loadToggles` fetched every toggle card's backing file on
    // `onRpcReady`. Nothing should read a config file until the
    // user opens a card.
    const reads = [];
    publishFakeRpc({
      'Settings.get_config_info': () => ({}),
      'Settings.get_config_content': (key) => {
        reads.push(key);
        return { type: key, content: '{}' };
      },
    });
    const el = mountTab();
    await settle(el);
    expect(reads).toEqual([]);
  });
});

// -----------------------------------------------------------
// The card set after the conversion
// -----------------------------------------------------------
//
// The grid used to offer eight cards, five of them prompt files
// the app assembled itself and one of them provider
// credentials. Two remain. These tests pin the set because a
// card for a config type the backend no longer whitelists opens
// an editor that errors on load — and a card for a *retired*
// prompt would be worse than broken: it would tell the user the
// app still sends a system prompt it composes.

describe('aic-settings-tab config cards', () => {
  beforeEach(() => {
    publishFakeRpc({
      'Settings.get_config_info': () => ({ config_dir: '/tmp/config' }),
      'Settings.get_config_content': (key) => {
        if (key === 'app') {
          return {
            type: 'app',
            content: JSON.stringify({ agents: { enabled: false } }),
          };
        }
        return { type: key, content: '{}' };
      },
    });
  });

  function cardLabels(el) {
    return [...el.shadowRoot.querySelectorAll('.card-label')]
      .map((n) => n.textContent.trim());
  }

  it('offers engine and app — and nothing else', async () => {
    const el = mountTab();
    await settle(el);
    expect(cardLabels(el).sort()).toEqual(['App Config', 'Engine Config']);
  });

  it('offers no card for a retired prompt or provider file', async () => {
    const el = mountTab();
    await settle(el);
    const labels = cardLabels(el).join(' ');
    for (const gone of [
      'LLM Config',
      'System Prompt',
      'System Extra',
      'Compaction Skill',
      'Review Prompt',
      'Doc Prompt',
    ]) {
      expect(labels).not.toContain(gone);
    }
  });

  it('never offers to reload the engine config', async () => {
    // engine.json's values are read when the CLI subprocess
    // starts. A reload would report success while the running
    // engine kept its original model and permission mode, so
    // the card is editable but not reloadable — save writes the
    // file and says so, and the change lands on next launch.
    const reload = vi.fn(() => ({ status: 'ok' }));
    publishFakeRpc({
      'Settings.get_config_info': () => ({}),
      'Settings.get_config_content': (key) => ({
        type: key,
        content: '{}',
      }),
      'Settings.save_config_content': () => ({ status: 'ok' }),
      'Settings.reload_app_config': reload,
    });
    const el = mountTab();
    await settle(el);
    el._activeKey = 'engine';
    el._editorContent = '{"model": null}';
    await el._save();
    await settle(el);
    expect(reload).not.toHaveBeenCalled();
  });

  it('reloads app.json on save, because that one takes', async () => {
    const reload = vi.fn(() => ({ status: 'ok' }));
    publishFakeRpc({
      'Settings.get_config_info': () => ({}),
      'Settings.get_config_content': (key) => ({
        type: key,
        content: '{}',
      }),
      'Settings.save_config_content': () => ({ status: 'ok' }),
      'Settings.reload_app_config': reload,
    });
    const el = mountTab();
    await settle(el);
    el._activeKey = 'app';
    el._editorContent = '{}';
    await el._save();
    await settle(el);
    expect(reload).toHaveBeenCalled();
  });
});

describe('aic-settings-tab info banner', () => {
  it('shows the config dir', async () => {
    publishFakeRpc({
      'Settings.get_config_info': () => ({ config_dir: '/tmp/cfg' }),
      'Settings.get_config_content': (key) => ({
        type: key,
        content: '{}',
      }),
    });
    const el = mountTab();
    await settle(el);
    const banner = el.shadowRoot.querySelector('.info-banner');
    expect(banner).toBeTruthy();
    expect(banner.textContent).toContain('/tmp/cfg');
  });

  it('still names no model in the banner once the panel has one', async () => {
    // The panel below reads the *session's* model from the engine. The
    // banner's job did not change: engine.json's value is a request, and
    // the field is gone from `get_config_info` for that reason. A stale
    // backend volunteering it must still not put it in the banner.
    publishFakeRpc({
      'Settings.get_config_info': () => ({
        model: 'anthropic/sonnet',
        config_dir: '/tmp/cfg',
      }),
      'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
      'ClaudeCodeService.get_model': () => ({
        model: 'haiku',
        resolved: 'au.anthropic.claude-haiku-4-5-20251001-v1:0',
        models: MODELS,
      }),
    });
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.info-banner').textContent)
      .not.toContain('sonnet');
    expect(modelPanel(el).textContent).toContain('haiku');
  });

  it('names no model, even if the RPC volunteers one', async () => {
    // `get_config_info` stopped reporting a model with the
    // engine that had one to report. A stale backend sending
    // the field anyway must not put a model name in the
    // banner: engine.json's value is a request, and the engine
    // can answer on a different model (a rate-limit fallback,
    // a mid-session set_model). The model actually used is
    // reported per turn by the usage HUD.
    publishFakeRpc({
      'Settings.get_config_info': () => ({
        model: 'anthropic/sonnet',
        smaller_model: 'anthropic/haiku',
        config_dir: '/tmp/cfg',
      }),
      'Settings.get_config_content': (key) => ({
        type: key,
        content: '{}',
      }),
    });
    const el = mountTab();
    await settle(el);
    const banner = el.shadowRoot.querySelector('.info-banner');
    expect(banner.textContent).not.toContain('sonnet');
    expect(banner.textContent).not.toContain('haiku');
    expect(banner.textContent).not.toContain('Model:');
  });
});

// -----------------------------------------------------------
// The model panel
// -----------------------------------------------------------
//
// The one control on this tab that reaches the running session.
// `set_model` is an SDK control request, so it applies now rather
// than next launch — which is the opposite of every config card
// below it, and the reason the panel sits outside the grid.
//
// Two facts, deliberately both shown: the *alias* is what was
// asked for and the *resolved model* is what will answer, and
// they are allowed to differ (specs5/5-webapp/settings.md
// § Info Banner). Nothing here derives the resolution — it comes
// from the CLI's own `models` list.

function publishModelRpc(model, extra = {}) {
  publishFakeRpc({
    'Settings.get_config_info': () => ({ config_dir: '/tmp/cfg' }),
    'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
    'ClaudeCodeService.get_model': () => model,
    ...extra,
  });
}

describe('aic-settings-tab model panel', () => {
  it('names the alias, its resolution, and offers the menu', async () => {
    publishModelRpc({
      model: 'opus',
      resolved: 'au.anthropic.claude-opus-5',
      models: MODELS,
    });
    const el = mountTab();
    await settle(el);
    const panel = modelPanel(el);
    const resolved = panel.querySelector('.model-resolved')
      .textContent.replace(/\s+/g, ' ').trim();
    expect(resolved).toBe('opus → au.anthropic.claude-opus-5');
    expect(modelSelect(el).value).toBe('opus');
    expect([...modelSelect(el).options].map((o) => o.value))
      .toEqual(['default', 'opus', 'haiku']);
  });

  it('is not a config card', async () => {
    // Every card in the grid opens an editor on a file that mostly
    // applies next session. This control applies now. Filing it in
    // the grid would put it under the same promise as the rest.
    publishModelRpc({ model: 'haiku', resolved: 'x', models: MODELS });
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.card-grid .model-panel')).toBeNull();
    expect(modelPanel(el)).toBeTruthy();
  });

  it('says the engine has not spoken yet rather than showing an empty menu', async () => {
    // The list comes from the initialize handshake and the engine
    // connects on the first turn, so an empty list is the ordinary
    // state before then — not a failure, and not a choice.
    publishModelRpc({ model: null, resolved: null, models: [] });
    const el = mountTab();
    await settle(el);
    expect(modelPanel(el).textContent).toContain('has not connected yet');
    expect(modelSelect(el).disabled).toBe(true);
    expect(modelSelect(el).options.length).toBe(0);
    expect(modelPanel(el).querySelector('.model-resolved')).toBeNull();
  });

  it('says the file pins nothing when the session pins nothing', async () => {
    publishModelRpc({ model: null, resolved: null, models: MODELS });
    const el = mountTab();
    await settle(el);
    expect(modelSelect(el).value).toBe('default');
    expect(modelPanel(el).textContent).toContain('pins no model');
  });

  it('says a switch reaches the next turn, not the one running', async () => {
    // Measured, not reasoned: `set_model` fired 22.8s into a live 34s
    // turn answered in 252ms without interrupting it, and the turn went
    // on billing opus — including a usage report 124ms after the switch
    // had landed. The turn after it billed haiku.
    //
    // Pinned by a test because it is the sentence a copy edit is most
    // likely to trim as wordy, and the reader who needs it is the one
    // watching an expensive turn run away and reaching here to stop it.
    // Unconditional on purpose, so both a pinned and an unpinned session
    // are checked.
    for (const model of ['opus', null]) {
      publishModelRpc({ model, resolved: null, models: MODELS });
      const el = mountTab();
      await settle(el);
      const note = modelPanel(el).textContent.replace(/\s+/g, ' ');
      expect(note).toContain('next');
      expect(note).toContain('finishes on the model it started with');
    }
  });

  it('keeps an alias the engine does not advertise', async () => {
    // A `<select>` whose value is not among its options renders as
    // the *first* option. A private deployment name would silently
    // read as "Default", which is a different model.
    publishModelRpc({
      model: 'some-private-deployment',
      resolved: null,
      models: MODELS,
    });
    const el = mountTab();
    await settle(el);
    expect(modelSelect(el).value).toBe('some-private-deployment');
    expect(modelPanel(el).textContent).toContain('did not say what this resolves to');
  });

  it('switches on the reply, not on the click', async () => {
    const calls = [];
    publishModelRpc(
      { model: 'opus', resolved: 'au.anthropic.claude-opus-5', models: MODELS },
      {
        'ClaudeCodeService.set_model': (value) => {
          calls.push(value);
          return { model: value };
        },
      },
    );
    const el = mountTab();
    await settle(el);
    const select = pickModel(el, 'haiku');
    // Put back immediately: the engine has not answered yet, so the
    // control must still read what the session is actually on.
    expect(select.value).toBe('opus');
    await settle(el);
    expect(calls).toEqual(['haiku']);
    expect(modelSelect(el).value).toBe('haiku');
    expect(modelPanel(el).querySelector('.model-resolved').textContent)
      .toContain('claude-haiku-4-5');
  });

  it('ignores a change nobody made', async () => {
    // A browser restoring form state on load raises a trusted
    // `change` with no pointer or key in front of it. Unlike the
    // permission-mode selector there is no confirmation to catch it,
    // so the gesture latch is the only guard — and the thing it
    // guards is the host's bill.
    const calls = [];
    publishModelRpc(
      { model: 'opus', resolved: 'x', models: MODELS },
      {
        'ClaudeCodeService.set_model': (value) => {
          calls.push(value);
          return { model: value };
        },
      },
    );
    const el = mountTab();
    await settle(el);
    const select = modelSelect(el);
    select.value = 'haiku';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(calls).toEqual([]);
    expect(select.value).toBe('opus');
  });

  it('warns and holds its ground when the engine refuses', async () => {
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail);
    window.addEventListener('aic-toast', onToast);
    publishModelRpc(
      { model: 'opus', resolved: 'x', models: MODELS },
      {
        'ClaudeCodeService.set_model': () => ({
          error: 'restricted',
          reason: 'Only the host can change the model',
        }),
      },
    );
    const el = mountTab();
    await settle(el);
    pickModel(el, 'haiku');
    await settle(el);
    window.removeEventListener('aic-toast', onToast);
    expect(toasts.map((t) => t.message))
      .toContain('Only the host can change the model');
    expect(modelSelect(el).value).toBe('opus');
  });

  it('follows another window that switched', async () => {
    publishModelRpc({ model: 'opus', resolved: 'x', models: MODELS });
    const el = mountTab();
    await settle(el);
    window.dispatchEvent(new CustomEvent('model-changed', {
      detail: { model: 'haiku', by: 'user' },
    }));
    await settle(el);
    expect(modelSelect(el).value).toBe('haiku');
    expect(modelPanel(el).querySelector('.model-resolved').textContent)
      .toContain('claude-haiku-4-5');
  });

  it('offers a participant no control it cannot use', async () => {
    // `set_model` is localhost-only. An enabled select that always
    // fails is worse than a disabled one that says why.
    publishModelRpc(
      { model: 'opus', resolved: 'x', models: MODELS },
      { 'Collab.get_collab_role': () => ({ is_localhost: false }) },
    );
    const el = mountTab();
    await settle(el);
    expect(modelSelect(el).disabled).toBe(true);
    expect(modelPanel(el).textContent).toContain('Only the host');
  });

  it('marks the panel when /model routes here, and only then', async () => {
    // The route can arrive with this tab already open and already
    // scrolled to the panel, where a scroll on its own changes
    // nothing on screen and the command looks like it did nothing.
    publishModelRpc({ model: 'opus', resolved: 'x', models: MODELS });
    const el = mountTab();
    await settle(el);
    expect(modelPanel(el).classList.contains('flash')).toBe(false);
    el.showSection('model');
    await settle(el);
    expect(modelPanel(el).classList.contains('flash')).toBe(true);
  });

  it('ignores a section it does not have', async () => {
    publishModelRpc({ model: 'opus', resolved: 'x', models: MODELS });
    const el = mountTab();
    await settle(el);
    el.showSection('usage');
    await settle(el);
    expect(modelPanel(el).classList.contains('flash')).toBe(false);
  });

  it('re-reads the model when the tab comes back on screen', async () => {
    // The menu is empty until the engine connects on the first turn,
    // and nothing pushes it — so a tab opened before that turn holds
    // an empty list with no way to learn otherwise.
    let reads = 0;
    publishFakeRpc({
      'Settings.get_config_info': () => ({}),
      'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
      'ClaudeCodeService.get_model': () => {
        reads += 1;
        return reads === 1
          ? { model: null, resolved: null, models: [] }
          : { model: 'haiku', resolved: 'au.anthropic.haiku', models: MODELS };
      },
    });
    const el = mountTab();
    await settle(el);
    expect(modelSelect(el).options.length).toBe(0);
    el.onTabVisible();
    await settle(el);
    expect(modelSelect(el).value).toBe('haiku');
  });
});

// -----------------------------------------------------------
// Where /permissions lands
// -----------------------------------------------------------
//
// The route used to be `tab:settings` with no anchor, which
// opened the tab and left the reader in front of a card grid.
// The thing the command names — the mode the *next* session
// starts in — is a line inside engine.json, so the anchor has
// to open that card and point at that line
// (specs5/3-engine/session.md § Target grammar,
// specs5/5-webapp/settings.md § Preference Cards).
//
// The running session's mode is the composer's own selector and
// is deliberately not reachable from here. A test below pins
// that this tab grows no second control for it: two controls for
// one posture, one of them applying next launch, is the drift
// that made the old route dishonest in the first place.

const ENGINE_JSON = [
  '{',
  '  "model": "opus",',
  '  "permission_mode": "acceptEdits",',
  '  "cli_path": null',
  '}',
].join('\n');

function publishEngineRpc(content = ENGINE_JSON, extra = {}) {
  publishFakeRpc({
    'Settings.get_config_info': () => ({ config_dir: '/tmp/cfg' }),
    'Settings.get_config_content': (key) => ({
      type: key,
      content: key === 'engine' ? content : '{}',
    }),
    ...extra,
  });
}

function editorTextarea(el) {
  return el.shadowRoot.querySelector('.editor-textarea');
}

describe('aic-settings-tab permission-mode section', () => {
  it('opens the engine card and selects the permission_mode line', async () => {
    publishEngineRpc();
    const el = mountTab();
    await settle(el);
    expect(editorTextarea(el)).toBeNull();

    await el.showSection('permission-mode');
    await settle(el);

    const ta = editorTextarea(el);
    expect(ta).toBeTruthy();
    expect(el._activeKey).toBe('engine');
    expect(ta.value.slice(ta.selectionStart, ta.selectionEnd))
      .toBe('"permission_mode": "acceptEdits",');
  });

  it('marks the editor, so a route that changed nothing else shows', async () => {
    // The card may already be the open one and already scrolled to,
    // where opening it again changes nothing on screen and the
    // command looks like it did nothing. Same lesson as /model's.
    publishEngineRpc();
    const el = mountTab();
    await settle(el);
    await el.showSection('permission-mode');
    await settle(el);
    expect(el.shadowRoot.querySelector('.editor-area').classList
      .contains('flash')).toBe(true);
  });

  it('lights one mark at a time', async () => {
    // Two parts of the tab lit at once would claim two commands
    // arrived when only the second did.
    publishEngineRpc();
    const el = mountTab();
    await settle(el);
    await el.showSection('permission-mode');
    await settle(el);
    await el.showSection('model');
    await settle(el);
    expect(modelPanel(el).classList.contains('flash')).toBe(true);
    expect(el.shadowRoot.querySelector('.editor-area')?.classList
      .contains('flash')).toBe(false);
  });

  it('says so when engine.json sets no mode, rather than flashing at nothing', async () => {
    // An absent key is a real answer — the engine's own default is
    // in force — and the reader who typed the command is the one
    // who would set it.
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail);
    window.addEventListener('aic-toast', onToast);
    publishEngineRpc('{\n  "model": "opus"\n}');
    const el = mountTab();
    await settle(el);
    await el.showSection('permission-mode');
    await settle(el);
    window.removeEventListener('aic-toast', onToast);
    expect(el._activeKey).toBe('engine');
    expect(toasts.map((t) => t.message).join(' '))
      .toContain('does not set permission_mode');
  });

  it('selects the user\'s unsaved line, not the one that was loaded', async () => {
    // The textarea is where an edit lives until it is saved, so a
    // reader who adds the key and re-runs the command must land on
    // the line they just wrote.
    publishEngineRpc('{\n  "model": "opus"\n}');
    const el = mountTab();
    await settle(el);
    await el.showSection('permission-mode');
    await settle(el);
    const ta = editorTextarea(el);
    ta.value = '{\n  "model": "opus",\n  "permission_mode": "plan"\n}';
    await el.showSection('permission-mode');
    await settle(el);
    expect(ta.value.slice(ta.selectionStart, ta.selectionEnd))
      .toBe('"permission_mode": "plan"');
  });

  it('offers no permission-mode control of its own', async () => {
    // The live posture belongs to the composer's selector. A second
    // control here — one that applies next launch — is exactly the
    // drift the anchor was added to end.
    publishEngineRpc();
    const el = mountTab();
    await settle(el);
    await el.showSection('permission-mode');
    await settle(el);
    expect(el.shadowRoot.querySelector('.permission-mode-select')).toBeNull();
    expect([...el.shadowRoot.querySelectorAll('select')]
      .filter((s) => s !== modelSelect(el))).toEqual([]);
  });

  it('ignores an anchor it does not have, and stays reachable', async () => {
    publishEngineRpc();
    const el = mountTab();
    await settle(el);
    await el.showSection('rules');
    await settle(el);
    expect(el._activeKey).toBeNull();
    expect(modelPanel(el).classList.contains('flash')).toBe(false);
  });
});

describe('fieldLineRange', () => {
  it('spans the key through the end of its line', () => {
    const r = fieldLineRange(ENGINE_JSON, 'permission_mode');
    expect(ENGINE_JSON.slice(r.start, r.end))
      .toBe('"permission_mode": "acceptEdits",');
  });

  it('reads a key, not a value that contains the name', () => {
    // Without the colon check this lands the reader on cli_path.
    const doc = '{\n  "cli_path": "/opt/permission_mode/claude",\n'
      + '  "permission_mode": "plan"\n}';
    const r = fieldLineRange(doc, 'permission_mode');
    expect(doc.slice(r.start, r.end)).toBe('"permission_mode": "plan"');
  });

  it('finds the line in a document that does not parse', () => {
    // A field is worth pointing at precisely while the file is being
    // edited, which is when it is most likely to be malformed.
    const doc = '{\n  "permission_mode": "plan"\n  "model":\n';
    const r = fieldLineRange(doc, 'permission_mode');
    expect(doc.slice(r.start, r.end)).toBe('"permission_mode": "plan"');
  });

  it('returns null for an absent key and for junk input', () => {
    expect(fieldLineRange('{}', 'permission_mode')).toBeNull();
    expect(fieldLineRange(ENGINE_JSON, '')).toBeNull();
    expect(fieldLineRange(null, 'permission_mode')).toBeNull();
  });
});