import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import './settings-tab.js';
import { fieldLineRange, fieldList, joinFields } from './settings-tab.js';
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
// The suite below replaces them. The removal has to be provable
// from the tab's own rendering, not from the absence of a card
// definition — a re-added CONFIG_CARDS entry with a stale
// `renderer: 'toggle'` would render a card with no switch
// machinery behind it and fail silently.

// -----------------------------------------------------------
// Preference cards
// -----------------------------------------------------------
//
// `specs5/5-webapp/settings.md` § Preference Cards. Two switches
// over fields that were already editable in the textarea below
// them, which is what makes the *notes* the thing under test:
// the card adds discoverability, and the only thing it can get
// wrong that the textarea could not is claiming a value is in
// force when it is not.
//
// The two dispositions are deliberately different and neither is
// "now": engine.json is read when the CLI starts, and app.json's
// enrichment flag is read by the next background pass.

const ENGINE_CONTENT = `{
  "model": null,
  "permission_mode": null,
  "thinking_display": null,
  "max_buffer_size": null
}
`;

const APP_CONTENT = `{
  "doc_index": {
    "keyword_model": "BAAI/bge-small-en-v1.5",
    "keywords_enabled": true,
    "keywords_ngram_range": [1, 2]
  }
}
`;

function prefCards(el) {
  return [...el.shadowRoot.querySelectorAll('.pref-card')];
}

function thinkingSelect(el) {
  return el.shadowRoot.querySelector('.pref-select');
}

function enrichmentCheckbox(el) {
  return el.shadowRoot.querySelector('.pref-card input[type="checkbox"]');
}

/** A fake backend that holds both config files and records saves. */
function publishConfigFiles(overrides = {}) {
  const files = {
    engine: ENGINE_CONTENT,
    app: APP_CONTENT,
    ...(overrides.files || {}),
  };
  const saves = [];
  const reloads = [];
  publishFakeRpc({
    'Settings.get_config_info': () => ({ config_dir: '/tmp/config' }),
    'Settings.get_config_content': (key) => ({ type: key, content: files[key] ?? '' }),
    'Settings.save_config_content': (key, content) => {
      saves.push({ key, content });
      files[key] = content;
      return {
        status: 'ok',
        type: key,
        disposition: overrides.disposition || {
          compared: true,
          changed: [key === 'engine' ? 'thinking_display' : 'doc_index'],
          live: key === 'app' ? ['doc_index'] : [],
          next_session: key === 'engine' ? ['thinking_display'] : [],
          live_control: {},
        },
      };
    },
    'Settings.reload_app_config': () => {
      reloads.push(true);
      return overrides.reloadFails ? { error: 'boom' } : { status: 'ok' };
    },
    ...(overrides.methods || {}),
  });
  return { files, saves, reloads };
}

function toastSpy() {
  const seen = [];
  const handler = (e) => seen.push(e.detail);
  window.addEventListener('aic-toast', handler);
  _toastCleanups.push(() => window.removeEventListener('aic-toast', handler));
  return seen;
}

const _toastCleanups = [];
afterEach(() => {
  while (_toastCleanups.length) _toastCleanups.pop()();
});

describe('aic-settings-tab preference cards', () => {
  it('renders one card per preference, and none of the deleted toggle', async () => {
    publishConfigFiles();
    const el = mountTab();
    await settle(el);
    expect(prefCards(el)).toHaveLength(2);
    expect(el.shadowRoot.textContent).toContain('Thinking display');
    expect(el.shadowRoot.textContent).toContain('Doc enrichment');
    // The Agentic-coding switch and its machinery are gone for good.
    expect(el.shadowRoot.querySelector('.card.toggle-card')).toBeNull();
    expect(el.shadowRoot.querySelector('.toggle-switch')).toBeNull();
    expect(el.shadowRoot.textContent).not.toContain('Agentic');
  });

  it('hydrates each control from its own config file', async () => {
    publishConfigFiles({
      files: {
        engine: ENGINE_CONTENT.replace('"thinking_display": null', '"thinking_display": "omitted"'),
        app: APP_CONTENT.replace('"keywords_enabled": true', '"keywords_enabled": false'),
      },
    });
    const el = mountTab();
    await settle(el);
    expect(thinkingSelect(el).value).toBe('omitted');
    expect(enrichmentCheckbox(el).checked).toBe(false);
  });

  it('offers "Engine default" as a third state, not as a synonym for one of the two', async () => {
    // `thinking_display: null` means "let the CLI decide", which is
    // not the same claim as either "summarized" or "omitted". A
    // checkbox could not have said it.
    publishConfigFiles();
    const el = mountTab();
    await settle(el);
    const values = [...thinkingSelect(el).options].map((o) => o.value);
    expect(values).toEqual(['', 'summarized', 'omitted']);
    expect(thinkingSelect(el).value).toBe('');
  });

  it('writes the engine field and leaves the rest of the file alone', async () => {
    const backend = publishConfigFiles();
    const el = mountTab();
    await settle(el);
    const select = thinkingSelect(el);
    select.value = 'omitted';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(backend.saves).toHaveLength(1);
    expect(backend.saves[0].key).toBe('engine');
    expect(backend.saves[0].content).toContain('"thinking_display": "omitted"');
    expect(backend.saves[0].content).toContain('"max_buffer_size": null');
  });

  it('sends the engine field to the restart list rather than claiming it applied', async () => {
    // The whole reason the card exists: engine.json is read when the
    // CLI starts, so the honest report is a restart, and the restart
    // confirmation has to be able to name the field.
    const backend = publishConfigFiles();
    const toasts = toastSpy();
    const el = mountTab();
    await settle(el);
    const select = thinkingSelect(el);
    select.value = 'summarized';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(backend.reloads).toEqual([]);
    expect(toasts.at(-1).message).toContain('when the session next starts');
    expect(el.restartConfirmText()).toContain('thinking_display');
    expect(el.shadowRoot.textContent).toContain('Waiting to apply');
  });

  it('reloads app.json for the enrichment flag, and says what that does not do', async () => {
    const backend = publishConfigFiles();
    const toasts = toastSpy();
    const el = mountTab();
    await settle(el);
    const box = enrichmentCheckbox(el);
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    await settle(el);
    expect(backend.saves[0].key).toBe('app');
    expect(JSON.parse(backend.saves[0].content).doc_index.keywords_enabled).toBe(false);
    expect(backend.reloads).toEqual([true]);
    // Not "applied now": switching enrichment off does not remove
    // keywords already computed, and switching it on does not start a
    // pass.
    expect(toasts.at(-1).message).toContain('next enrichment pass');
    expect(el.restartConfirmText()).not.toContain('doc_index');
  });

  it('does not claim the enrichment flag is in force when the reload failed', async () => {
    const backend = publishConfigFiles({ reloadFails: true });
    const toasts = toastSpy();
    const el = mountTab();
    await settle(el);
    const box = enrichmentCheckbox(el);
    box.checked = false;
    box.dispatchEvent(new Event('change'));
    await settle(el);
    expect(backend.saves).toHaveLength(1);
    const messages = toasts.map((t) => t.message).join(' | ');
    expect(messages).toContain('still on the old value');
  });

  it('says nothing is waiting when the value written is the one already there', async () => {
    // A save that moved nothing must not offer a restart. The
    // disposition is what knows this — the card cannot.
    publishConfigFiles({
      disposition: {
        compared: true, changed: [], live: [], next_session: [], live_control: {},
      },
    });
    const toasts = toastSpy();
    const el = mountTab();
    await settle(el);
    const select = thinkingSelect(el);
    select.value = 'omitted';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(toasts.at(-1).message).toContain('Already what the file said');
    expect(el.restartConfirmText()).not.toContain('thinking_display');
  });

  it('disables both controls and refuses to write when the file will not parse', async () => {
    const backend = publishConfigFiles({ files: { engine: '{ "model":' } });
    const el = mountTab();
    await settle(el);
    expect(thinkingSelect(el).disabled).toBe(true);
    expect(thinkingSelect(el).closest('.pref-card').title).toContain('not a JSON object');
    // The other card is unaffected: they read different files.
    expect(enrichmentCheckbox(el).disabled).toBe(false);
    expect(backend.saves).toEqual([]);
  });

  it('disables both controls for a participant', async () => {
    publishConfigFiles({
      methods: { 'Collab.get_collab_role': () => ({ is_localhost: false }) },
    });
    const el = mountTab();
    await settle(el);
    expect(thinkingSelect(el).disabled).toBe(true);
    expect(enrichmentCheckbox(el).disabled).toBe(true);
    expect(thinkingSelect(el).closest('.pref-card').title)
      .toContain('Only the host');
  });

  it('puts the control back when the save is refused', async () => {
    // A native select flips itself on the gesture and Lit will not put
    // it back — the bound value never changed. Left alone, a refused
    // write leaves the control claiming a setting the file does not
    // have, which is the unqualified-success failure one screen up.
    publishConfigFiles({
      methods: {
        'Settings.save_config_content': () => ({ error: 'restricted' }),
      },
    });
    const toasts = toastSpy();
    const el = mountTab();
    await settle(el);
    const select = thinkingSelect(el);
    select.value = 'omitted';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(toasts.at(-1).message).toBe('restricted');
    expect(thinkingSelect(el).value).toBe('');
  });

  it('writes through the open textarea rather than over it', async () => {
    // The one failure this control could cause that the textarea alone
    // never could: a switch basing its write on a stale read would
    // silently discard whatever the user had typed above it.
    const backend = publishConfigFiles();
    const el = mountTab();
    await settle(el);
    await el._openCard('engine');
    await settle(el);
    const textarea = el.shadowRoot.querySelector('.editor-textarea');
    textarea.value = ENGINE_CONTENT.replace('"model": null', '"model": "opus"');
    const select = thinkingSelect(el);
    select.value = 'omitted';
    select.dispatchEvent(new Event('change'));
    await settle(el);
    expect(backend.saves[0].content).toContain('"model": "opus"');
    expect(backend.saves[0].content).toContain('"thinking_display": "omitted"');
    // And the editor now shows what was actually written.
    expect(el.shadowRoot.querySelector('.editor-textarea').value)
      .toBe(backend.saves[0].content);
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

  // Scoped to the config grid. The preference cards above it wear the
  // same `.card` chrome on purpose, so an unscoped label sweep would
  // count them as config types and this suite would be asserting
  // something it does not mean.
  function cardLabels(el) {
    return [...el.shadowRoot.querySelectorAll('.card-grid .card-label')]
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
    // Every select on this tab is accounted for: the model panel's, and
    // the preference cards'. A fourth would be the drift.
    expect([...el.shadowRoot.querySelectorAll('select')]
      .filter((s) => s !== modelSelect(el) && !s.classList.contains('pref-select')))
      .toEqual([]);
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

// -----------------------------------------------------------
// What a save applied, and what is still waiting
// -----------------------------------------------------------
//
// The invariant this section exists for: a save never shows an
// unqualified success for a field that did not apply
// (specs5/5-webapp/settings.md § Invariants). A save calls
// neither live setter, so for engine.json that means *every*
// field — the toast has to say so, not just the panel, because
// the toast is what a reader sees and the panel is what they
// read next.
//
// The disposition comes from the backend (`Settings`, tested in
// tests/test_settings.py § TestSaveDisposition). The tab's job is
// to say it in a sentence, and to join "applied" itself: the save
// cannot know whether the reload it asks for afterwards worked.

/** A save that reports `disposition`, plus a counter per call. */
function publishSaveRpc(disposition, extra = {}) {
  const calls = { save: [], reload: 0, restart: 0 };
  publishEngineRpc(ENGINE_JSON, {
    'Settings.save_config_content': (key, content) => {
      calls.save.push([key, content]);
      return { status: 'ok', type: key, disposition };
    },
    'Settings.reload_app_config': () => {
      calls.reload += 1;
      return { status: 'ok' };
    },
    'ClaudeCodeService.restart_session': () => {
      calls.restart += 1;
      return {
        status: 'restarted',
        session_id: 'sess-1',
        permission_mode: 'acceptEdits',
        model: 'opus',
      };
    },
    ...extra,
  });
  return calls;
}

function saveSummary(el) {
  return el.shadowRoot.querySelector('.save-summary');
}

function sessionControls(el) {
  return el.shadowRoot.querySelector('.session-controls');
}

function restartButton(el) {
  return sessionControls(el).querySelector('button');
}

/** Rendered text with the template's line breaks collapsed. */
function flat(node) {
  return node ? node.textContent.replace(/\s+/g, ' ').trim() : '';
}

const _toastListeners = [];

function captureToasts() {
  const toasts = [];
  const onToast = (e) => toasts.push(e.detail);
  window.addEventListener('aic-toast', onToast);
  _toastListeners.push(() => window.removeEventListener('aic-toast', onToast));
  return toasts;
}

afterEach(() => {
  while (_toastListeners.length) _toastListeners.pop()();
});

/** Open a card and press Save, the way the toolbar button does. */
async function saveCard(el, key) {
  await el._openCard(key);
  await settle(el);
  await el._save();
  await settle(el);
}

describe('aic-settings-tab save disposition', () => {
  it('qualifies the toast for a field the save could not apply', async () => {
    // "Saved" on its own is the bug: nothing about writing engine.json
    // reaches the running CLI, so a bare success reads as "in force".
    const toasts = captureToasts();
    publishSaveRpc({
      compared: true,
      changed: ['effort'],
      live: [],
      next_session: ['effort'],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    expect(toasts.map((t) => t.message)).not.toContain('Saved');
    expect(toasts[0].message)
      .toBe('Saved. effort applies when the session next starts.');
    expect(toasts[0].type).toBe('info');
  });

  it('lists the fields waiting, and pluralises the verb with them', async () => {
    const toasts = captureToasts();
    publishSaveRpc({
      compared: true,
      changed: ['cli_path', 'effort', 'model'],
      live: [],
      next_session: ['cli_path', 'effort', 'model'],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    expect(toasts[0].message)
      .toBe('Saved. cli_path, effort and model apply when the session next'
        + ' starts.');
    expect(flat(saveSummary(el)))
      .toContain('Applies when the session next starts: cli_path, effort and'
        + ' model.');
  });

  it('points at the control that would apply a field now', async () => {
    // The two fields with live setters are the ones a reader is most likely
    // to have edited here expecting them to take. Naming the shortcut is
    // cheaper for them than a restart, and the restart button is right below.
    publishSaveRpc({
      compared: true,
      changed: ['model', 'permission_mode'],
      live: [],
      next_session: ['model', 'permission_mode'],
      live_control: {
        model: 'the model panel on the Settings tab',
        permission_mode: 'the permission-mode selector beside the composer',
      },
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    const text = flat(saveSummary(el));
    expect(text).toContain('model can also be changed now, without a restart:'
      + ' the model panel on the Settings tab.');
    expect(text).toContain('the permission-mode selector beside the composer');
  });

  it('says nothing changed rather than claiming an application', async () => {
    const toasts = captureToasts();
    publishSaveRpc({
      compared: true,
      changed: [],
      live: [],
      next_session: [],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    expect(toasts[0].message).toBe('Saved');
    expect(flat(saveSummary(el)))
      .toBe('Saved. Nothing in the file changed, so nothing is waiting.');
    expect(flat(sessionControls(el))).not.toContain('Waiting to apply');
  });

  it('claims applied only for a reload that came back', async () => {
    publishSaveRpc({
      compared: true,
      changed: ['history_limit'],
      live: ['history_limit'],
      next_session: [],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'app');
    expect(flat(saveSummary(el))).toContain('Applied now: history_limit');
    expect(flat(saveSummary(el))).not.toContain('next starts');
  });

  it('does not report a field as waiting when the reload failed', async () => {
    // The reload is what applies an app.json field, and it can fail. Neither
    // "applied" nor "waiting for a restart" is true then — a restart is not
    // what applies it — so the panel says the third thing.
    publishSaveRpc(
      {
        compared: true,
        changed: ['history_limit'],
        live: ['history_limit'],
        next_session: [],
        live_control: {},
      },
      { 'Settings.reload_app_config': () => ({ error: 'no config loaded' }) },
    );
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'app');
    const text = flat(saveSummary(el));
    expect(text).toBe('Saved to the file. The reload did not apply, so'
      + ' history_limit is not in force yet.');
    expect(text).not.toContain('Nothing in the file changed');
    expect(flat(sessionControls(el))).not.toContain('Waiting to apply');
  });

  it('says why every field is listed when the previous file was unreadable',
    async () => {
      publishSaveRpc({
        compared: false,
        changed: ['cli_path', 'effort', 'model'],
        live: [],
        next_session: ['cli_path', 'effort', 'model'],
        live_control: {},
      });
      const el = mountTab();
      await settle(el);
      await saveCard(el, 'engine');
      expect(flat(saveSummary(el)))
        .toContain('previous file could not be read');
    });

  it('reports no disposition rather than inventing one', async () => {
    // Content that does not parse has no fields to diff, and an older
    // backend sends no `disposition` at all. Both have to render.
    const toasts = captureToasts();
    publishEngineRpc(ENGINE_JSON, {
      'Settings.save_config_content': (key) => ({
        status: 'ok',
        type: key,
        disposition: null,
        warning: 'Saved, but the file is not valid JSON',
      }),
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    expect(toasts[0].message).toContain('not valid JSON');
    expect(toasts[0].type).toBe('warning');
    expect(saveSummary(el)).toBeNull();
  });

  it('keeps the waiting list across saves, and drops the panel per card',
    async () => {
      // Two saves each touching one field leave two fields waiting: a
      // restart applies the whole file, so the list is about the session,
      // not about the last press of Save. The panel is the opposite — it
      // describes the file in the textarea, and goes when that changes.
      let nth = 0;
      publishEngineRpc(ENGINE_JSON, {
        'Settings.save_config_content': (key) => {
          nth += 1;
          const field = nth === 1 ? 'effort' : 'cli_path';
          return {
            status: 'ok',
            type: key,
            disposition: {
              compared: true,
              changed: [field],
              live: [],
              next_session: [field],
              live_control: {},
            },
          };
        },
      });
      const el = mountTab();
      await settle(el);
      await saveCard(el, 'engine');
      await el._save();
      await settle(el);
      expect(flat(sessionControls(el)))
        .toContain('Waiting to apply: cli_path and effort.');

      await el._openCard('app');
      await settle(el);
      expect(saveSummary(el)).toBeNull();
      expect(flat(sessionControls(el)))
        .toContain('Waiting to apply: cli_path and effort.');
    });
});

// -----------------------------------------------------------
// Session controls
// -----------------------------------------------------------
//
// The other half of the same invariant: something has to *be* the
// thing that applies a field the save could not. `restart_session`
// replaces the CLI subprocess on the file as it is on disk and
// resumes the conversation, so the transcript survives and the
// cost totals do not.
//
// Always offered, not only after a save: a user who edited
// engine.json in another editor has the same problem and no save
// here to hang the offer off.

describe('aic-settings-tab session controls', () => {
  let confirmSpy;

  beforeEach(() => {
    // Stubbed rather than native: jsdom's `confirm` is unimplemented and
    // logs, and it returns undefined — which would read as a decline and
    // pass the tests that matter for the wrong reason.
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it('offers a restart with no save in front of it', async () => {
    publishSaveRpc(null);
    const el = mountTab();
    await settle(el);
    expect(restartButton(el).textContent.trim()).toBe('↻ Restart session');
    expect(restartButton(el).disabled).toBe(false);
    expect(flat(sessionControls(el)))
      .toContain('a restart is the only thing that applies one');
  });

  it('names the waiting fields in the confirmation', async () => {
    publishSaveRpc({
      compared: true,
      changed: ['effort'],
      live: [],
      next_session: ['effort'],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    await saveCard(el, 'engine');
    restartButton(el).click();
    await settle(el);
    expect(confirmSpy.mock.calls[0][0]).toContain('This applies effort.');
  });

  it('names the file when nothing was saved here to wait for', async () => {
    publishSaveRpc(null);
    const el = mountTab();
    await settle(el);
    expect(el.restartConfirmText())
      .toContain('This applies engine.json as it is on disk.');
  });

  it('says a hand-set model or mode goes back to the file, always', async () => {
    // Unconditional on purpose. A mid-session `set_model` this save did not
    // touch is invisible to the pending list, and the restart reverts it —
    // so the only honest place for the clause is every confirmation.
    publishSaveRpc({
      compared: true,
      changed: ['effort'],
      live: [],
      next_session: ['effort'],
      live_control: {},
    });
    const el = mountTab();
    await settle(el);
    for (const stage of ['before', 'after']) {
      if (stage === 'after') await saveCard(el, 'engine');
      const asked = el.restartConfirmText();
      expect(asked).toContain('cost totals start from zero');
      expect(asked)
        .toContain('goes back to what engine.json says');
      expect(asked).toContain('conversation is resumed');
    }
  });

  it('asks before restarting, and stops on a decline', async () => {
    confirmSpy.mockReturnValue(false);
    const calls = publishSaveRpc(null);
    const el = mountTab();
    await settle(el);
    restartButton(el).click();
    await settle(el);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(calls.restart).toBe(0);
  });

  it('clears the waiting list on a restart, because the file is in force',
    async () => {
      const toasts = captureToasts();
      const calls = publishSaveRpc({
        compared: true,
        changed: ['effort'],
        live: [],
        next_session: ['effort'],
        live_control: {},
      });
      const el = mountTab();
      await settle(el);
      await saveCard(el, 'engine');
      restartButton(el).click();
      await settle(el);
      expect(calls.restart).toBe(1);
      expect(toasts.at(-1).message)
        .toBe('Session restarted. The conversation was resumed.');
      expect(flat(sessionControls(el))).not.toContain('Waiting to apply');
      expect(saveSummary(el)).toBeNull();
    });

  it('says nothing was replaced when the engine had not started', async () => {
    // `status: "adopted"`. "Session restarted" would be a claim about a
    // subprocess that never existed, and the reader's next question — is my
    // edit in force? — has a different answer: it will be, on first use.
    const toasts = captureToasts();
    publishSaveRpc(null, {
      'ClaudeCodeService.restart_session': () => ({
        status: 'adopted',
        session_id: null,
        permission_mode: 'default',
        model: null,
      }),
    });
    const el = mountTab();
    await settle(el);
    restartButton(el).click();
    await settle(el);
    expect(toasts.at(-1).message).toContain('had not started yet');
    expect(toasts.at(-1).type).toBe('info');
  });

  it('reports a refusal in the engine\'s own words, and keeps the list',
    async () => {
      // A turn in flight and an open review are both refusals. Clearing the
      // waiting list on one would tell the reader the file had been applied.
      const toasts = captureToasts();
      publishSaveRpc(
        {
          compared: true,
          changed: ['effort'],
          live: [],
          next_session: ['effort'],
          live_control: {},
        },
        {
          'ClaudeCodeService.restart_session': () => ({
            error: 'A turn is still running',
            reason: 'turn_in_progress',
          }),
        },
      );
      const el = mountTab();
      await settle(el);
      await saveCard(el, 'engine');
      restartButton(el).click();
      await settle(el);
      expect(toasts.at(-1).message).toBe('A turn is still running');
      expect(toasts.at(-1).type).toBe('error');
      expect(flat(sessionControls(el))).toContain('Waiting to apply: effort.');
    });

  it('asks once while a restart is in flight', async () => {
    let resolve;
    const calls = { restart: 0 };
    publishEngineRpc(ENGINE_JSON, {
      'ClaudeCodeService.restart_session': () => {
        calls.restart += 1;
        return new Promise((r) => { resolve = r; });
      },
    });
    const el = mountTab();
    await settle(el);
    restartButton(el).click();
    await settle(el);
    expect(restartButton(el).textContent.trim()).toBe('Restarting…');
    expect(restartButton(el).disabled).toBe(true);
    restartButton(el).click();
    await settle(el);
    expect(calls.restart).toBe(1);
    resolve({ status: 'restarted', session_id: 's' });
    await settle(el);
    expect(restartButton(el).textContent.trim()).toBe('↻ Restart session');
  });

  it('offers a participant no restart it cannot use', async () => {
    // `restart_session` is localhost-only, like `set_model`.
    publishSaveRpc(null, {
      'Collab.get_collab_role': () => ({ is_localhost: false }),
    });
    const el = mountTab();
    await settle(el);
    expect(restartButton(el).disabled).toBe(true);
    expect(flat(sessionControls(el)))
      .toContain('Only the host can restart the session');
    await el._restartSession();
    await settle(el);
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it('re-reads the model, because the file may have taken it back', async () => {
    // The restart reverts a mid-session `set_model` to what engine.json
    // says. The tab's own panel would otherwise keep showing the override.
    let reads = 0;
    publishFakeRpc({
      'Settings.get_config_info': () => ({ config_dir: '/tmp/cfg' }),
      'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
      'ClaudeCodeService.get_model': () => {
        reads += 1;
        return reads === 1
          ? { model: 'haiku', resolved: 'x', models: MODELS }
          : { model: 'opus', resolved: 'y', models: MODELS };
      },
      'ClaudeCodeService.restart_session': () => ({
        status: 'restarted',
        session_id: 's',
        permission_mode: 'acceptEdits',
        model: 'opus',
      }),
    });
    const el = mountTab();
    await settle(el);
    expect(modelSelect(el).value).toBe('haiku');
    restartButton(el).click();
    await settle(el);
    expect(modelSelect(el).value).toBe('opus');
  });

  it('is not a config card, and grows no second permission control', async () => {
    publishSaveRpc(null);
    const el = mountTab();
    await settle(el);
    expect(el.shadowRoot.querySelector('.card-grid .session-controls'))
      .toBeNull();
    expect([...el.shadowRoot.querySelectorAll('select')]
      .filter((s) => s !== modelSelect(el) && !s.classList.contains('pref-select')))
      .toEqual([]);
  });
});

describe('aic-settings-tab session storage figure', () => {
  /** The session controls, with `get_session_storage` answering `answer`. */
  function publishStorageRpc(answer, onCall) {
    return publishSaveRpc(null, {
      'ClaudeCodeService.get_session_storage': () => {
        onCall?.();
        return answer;
      },
    });
  }

  function storageNote(el) {
    return el.shadowRoot.querySelector('.storage-note');
  }

  it('reads the size and says where it is', async () => {
    publishStorageRpc({ bytes: 2_411_724_800, over_warning: false });
    const el = mountTab();
    await settle(el);
    expect(flat(storageNote(el)))
      .toContain('Session storage: 2.2 GB in .aic-dc/sessions/.');
    expect(storageNote(el).querySelector('.storage-warn')).toBeNull();
  });

  it('repeats the engine verdict rather than comparing a threshold', async () => {
    // The reply carries no number to compare against, on purpose — the same
    // reason the health banner is handed a mirror-gap verdict. A tab that
    // grew its own limit would be a second copy of an editable setting.
    publishStorageRpc({ bytes: 1_073_741_825, over_warning: true });
    const el = mountTab();
    await settle(el);
    expect(flat(storageNote(el).querySelector('.storage-warn')))
      .toBe('Past the size this repo asks to be warned at.');
    expect(flat(storageNote(el))).toContain('Pasted images are stored');
  });

  it('renders nothing at all before the first read lands', async () => {
    // A read that has not come back yet — the state every mount is in for a
    // round trip. "0 B" here would be briefly wrong about the one thing the
    // card exists to report, and an error line would be wrong too.
    publishSaveRpc(null, {
      'ClaudeCodeService.get_session_storage': () => new Promise(() => {}),
    });
    const el = mountTab();
    await settle(el);
    expect(storageNote(el)).toBeNull();
    expect(flat(sessionControls(el))).not.toContain('Session storage');
  });

  it('says a run with no repo is not mirrored, not that it is empty', async () => {
    publishStorageRpc({
      error: 'No session history: this run has no repo directory',
      reason: 'no_repo',
    });
    const el = mountTab();
    await settle(el);
    expect(flat(storageNote(el))).toContain('not mirrored');
    expect(flat(storageNote(el))).not.toContain('0 B');
    expect(storageNote(el).querySelector('.storage-link')).toBeNull();
  });

  it('shows the reason a walk failed instead of a blank card', async () => {
    publishStorageRpc({
      error: 'Could not measure the session directory: no such file',
    });
    const el = mountTab();
    await settle(el);
    expect(flat(storageNote(el))).toContain('no such file');
  });

  it('treats a thrown read as a reason, not an absence', async () => {
    publishSaveRpc(null, {
      'ClaudeCodeService.get_session_storage': () => {
        throw new Error('the socket went away');
      },
    });
    const el = mountTab();
    await settle(el);
    expect(flat(storageNote(el))).toContain('the socket went away');
  });

  it('offers the history browser and no delete of its own', async () => {
    const events = [];
    const onOpen = () => events.push('open-history');
    window.addEventListener('open-history', onOpen);
    publishStorageRpc({ bytes: 5_000, over_warning: false });
    const el = mountTab();
    await settle(el);
    const minimized = [];
    el.addEventListener('request-dialog-minimize', (e) =>
      minimized.push(e.composed),
    );
    const link = storageNote(el).querySelector('.storage-link');
    expect(link.textContent.trim()).toBe('Browse history');
    link.click();
    await settle(el);
    window.removeEventListener('open-history', onOpen);
    // Both, and in that order: the browser opens behind the dialog, so a
    // click that revealed nothing would read as a click that did nothing.
    expect(events).toEqual(['open-history']);
    expect(minimized).toEqual([true]);
    // The route, not the deletion. Deletion stays where the transcript is.
    expect(flat(storageNote(el))).not.toContain('Delete');
  });

  it('re-reads when the tab comes back, because deleting happens elsewhere',
    async () => {
      let reads = 0;
      publishSaveRpc(null, {
        'ClaudeCodeService.get_session_storage': () => {
          reads += 1;
          return reads === 1
            ? { bytes: 2_147_483_648, over_warning: true }
            : { bytes: 4_096, over_warning: false };
        },
      });
      const el = mountTab();
      await settle(el);
      expect(flat(storageNote(el))).toContain('2.0 GB');
      el.onTabVisible();
      await settle(el);
      expect(flat(storageNote(el))).toContain('4.0 KB');
      expect(storageNote(el).querySelector('.storage-warn')).toBeNull();
    });
});

describe('fieldList and joinFields', () => {
  it('reads one list out of a disposition', () => {
    const d = { changed: ['a', 'b'], live: [], next_session: ['a'] };
    expect(fieldList(d, 'changed')).toEqual(['a', 'b']);
    expect(fieldList(d, 'live')).toEqual([]);
  });

  it('treats a missing disposition as nothing to report', () => {
    // `null` is a real answer — unparseable content has no fields to diff —
    // and an older backend sends no key at all. Neither may throw on the
    // render path.
    expect(fieldList(null, 'changed')).toEqual([]);
    expect(fieldList({}, 'changed')).toEqual([]);
    expect(fieldList('nope', 'changed')).toEqual([]);
    expect(fieldList({ changed: 'effort' }, 'changed')).toEqual([]);
    expect(fieldList({ changed: ['a', 7, null] }, 'changed')).toEqual(['a']);
  });

  it('reads as a sentence, not a table', () => {
    expect(joinFields([])).toBe('');
    expect(joinFields(['a'])).toBe('a');
    expect(joinFields(['a', 'b'])).toBe('a and b');
    expect(joinFields(['a', 'b', 'c'])).toBe('a, b and c');
  });

  it('leaves a Set in the order it was given', () => {
    expect(joinFields(new Set(['b', 'a']))).toBe('b and a');
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
// -----------------------------------------------------------
// The retired-files note
// -----------------------------------------------------------
//
// `specs5/5-webapp/settings.md` § Deleted cards argues for this
// and then nothing rendered it — for three phases. The cards
// that edited these files are gone; the files are still on
// disk, deliberately, because `system_extra.md` may hold real
// user work. Leaving them was right. Not saying so was not.
//
// The load-bearing property is relevance: the note is shown
// only to installs that actually have such a file, because a
// user who never had the cards would be reading an explanation
// of a disappearance they did not witness.

describe('aic-settings-tab retired-files note', () => {
  const withRetired = (retired) => {
    publishFakeRpc({
      'Settings.get_config_info': () => ({
        config_dir: '/tmp/config',
        retired_files: retired,
      }),
      'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
    });
  };

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  const note = (el) => el.shadowRoot.querySelector('.retired-note');

  it('names the retired files this install still has', async () => {
    withRetired(['llm.json', 'system_extra.md']);
    const el = mountTab();
    await settle(el);

    const rendered = note(el);
    expect(rendered).not.toBeNull();
    const items = [...rendered.querySelectorAll('li')].map((li) => li.textContent);
    expect(items).toEqual(['llm.json', 'system_extra.md']);
  });

  it('says where the instructions come from now', async () => {
    // The point of the note is not the list — it is that there is
    // no system prompt to own any more, and where the user should
    // look instead.
    withRetired(['system_extra.md']);
    const el = mountTab();
    await settle(el);

    const text = note(el).textContent;
    expect(text).toContain('CLAUDE.md');
    expect(text).toContain('.claude/');
  });

  it('promises the files are not deleted', async () => {
    // The reassurance is the reason the leave-alone rule exists;
    // a note that only said "these are obsolete" would read as a
    // warning that they are about to be cleaned up.
    withRetired(['system_extra.md']);
    const el = mountTab();
    await settle(el);
    expect(note(el).textContent).toContain('delete');
  });

  it('says nothing to a fresh install', async () => {
    withRetired([]);
    const el = mountTab();
    await settle(el);
    expect(note(el)).toBeNull();
  });

  it('says nothing when the backend omits the field', async () => {
    // An older engine, or a failed call that left `_info` partial.
    publishFakeRpc({
      'Settings.get_config_info': () => ({ config_dir: '/tmp/config' }),
      'Settings.get_config_content': (key) => ({ type: key, content: '{}' }),
    });
    const el = mountTab();
    await settle(el);
    expect(note(el)).toBeNull();
  });

  it('dismisses, and stays dismissed across a remount', async () => {
    withRetired(['system_extra.md']);
    const first = mountTab();
    await settle(first);
    note(first).querySelector('.retired-dismiss').click();
    await first.updateComplete;
    expect(note(first)).toBeNull();

    const second = mountTab();
    await settle(second);
    expect(note(second)).toBeNull();
  });

  it('returns when a later upgrade retires something new', async () => {
    // The dismissal is keyed on the list, not on a boolean: a name
    // the user has never had explained to them is owed the note
    // again, and a flag would swallow it.
    withRetired(['system_extra.md']);
    const first = mountTab();
    await settle(first);
    note(first).querySelector('.retired-dismiss').click();
    await first.updateComplete;

    withRetired(['system_extra.md', 'review.md']);
    const second = mountTab();
    await settle(second);
    expect(note(second)).not.toBeNull();
  });

  it('renders when localStorage throws', async () => {
    // Site data blocked. Failing to render the tab over a
    // dismissal preference would be a bad trade; showing the note
    // twice is the smaller fault.
    const getItem = vi.spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => { throw new Error('blocked'); });
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => { throw new Error('blocked'); });
    try {
      withRetired(['system_extra.md']);
      const el = mountTab();
      await settle(el);
      expect(note(el)).not.toBeNull();
      // And dismissing still works for this load.
      note(el).querySelector('.retired-dismiss').click();
      await el.updateComplete;
      expect(note(el)).toBeNull();
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });

  it('sits above the card grid, where the missing card would be', async () => {
    withRetired(['system_extra.md']);
    const el = mountTab();
    await settle(el);
    const root = el.shadowRoot;
    const position = root.querySelector('.retired-note')
      .compareDocumentPosition(root.querySelector('.card-grid'));
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
