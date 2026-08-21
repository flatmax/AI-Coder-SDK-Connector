import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import './settings-tab.js';
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

function publishFakeRpc(methods) {
  const proxy = {};
  for (const [name, impl] of Object.entries(methods)) {
    proxy[name] = async (...args) => {
      const value = await impl(...args);
      return { fake: value };
    };
  }
  SharedRpc.set(proxy);
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
// credentials. Three remain. These tests pin the set because a
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

  it('offers engine, app and snippets — and nothing else', async () => {
    const el = mountTab();
    await settle(el);
    expect(cardLabels(el).sort()).toEqual([
      'App Config',
      'Engine Config',
      'Snippets',
    ]);
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