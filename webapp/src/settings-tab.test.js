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
  const el = document.createElement('ac-settings-tab');
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

function getToggleCard(el) {
  return el.shadowRoot.querySelector('.card.toggle-card');
}

function getToggleSwitch(el) {
  return el.shadowRoot.querySelector('.toggle-switch');
}

function getToggleDescription(el) {
  return el.shadowRoot.querySelector('.card-description');
}

// -----------------------------------------------------------
// Tests
// -----------------------------------------------------------

describe('ac-settings-tab agentic toggle', () => {
  describe('rendering', () => {
    beforeEach(() => {
      publishFakeRpc({
        'Settings.get_config_info': () => ({
          model: 'anthropic/sonnet',
          config_dir: '/tmp/config',
        }),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return {
              type: 'app',
              content: JSON.stringify({ agents: { enabled: false } }),
            };
          }
          return { type: key, content: '' };
        },
      });
    });

    it('renders a toggle card for the agents field', async () => {
      const el = mountTab();
      await settle(el);
      const card = getToggleCard(el);
      expect(card).toBeTruthy();
      expect(card.textContent).toContain('Agentic coding');
    });

    it('renders the description text', async () => {
      const el = mountTab();
      await settle(el);
      const desc = getToggleDescription(el);
      expect(desc).toBeTruthy();
      expect(desc.textContent.toLowerCase()).toContain(
        'decompose',
      );
    });

    it('reflects the backend state as OFF initially', async () => {
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('false');
      expect(sw.classList.contains('on')).toBe(false);
    });

    it('reflects the backend state as ON when enabled', async () => {
      publishFakeRpc({
        'Settings.get_config_info': () => ({}),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return {
              type: 'app',
              content: JSON.stringify({ agents: { enabled: true } }),
            };
          }
          return { type: key, content: '' };
        },
      });
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('true');
      expect(sw.classList.contains('on')).toBe(true);
    });

    it('defaults to OFF when the agents section is missing', async () => {
      publishFakeRpc({
        'Settings.get_config_info': () => ({}),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return { type: 'app', content: JSON.stringify({}) };
          }
          return { type: key, content: '' };
        },
      });
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('false');
    });

    it('defaults to OFF when app.json has malformed JSON', async () => {
      publishFakeRpc({
        'Settings.get_config_info': () => ({}),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return { type: 'app', content: '{not valid json' };
          }
          return { type: key, content: '' };
        },
      });
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('false');
    });

    it('defaults to OFF when app.json is empty', async () => {
      publishFakeRpc({
        'Settings.get_config_info': () => ({}),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return { type: 'app', content: '' };
          }
          return { type: key, content: '' };
        },
      });
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('false');
    });
  });

  // The agentic-coding card is locked off during early
  // development — `card.locked = true` in CONFIG_CARDS
  // and `_EXPERIMENTAL_ENABLED` is false unless the
  // launcher passed `--experimental` (which sets the
  // `?experimental=1` URL param read at module load).
  // In the test environment the param is absent, so the
  // lock is active and clicks no-op at the handler level.
  // These tests verify the lock is enforced; when the
  // feature unlocks, this suite gets inverted to test
  // the unlocked toggle mechanism.
  describe('lock enforcement (locked=true, experimental off)', () => {
    let saves;
    let state;

    beforeEach(() => {
      saves = [];
      state = { agents: { enabled: false } };
      publishFakeRpc({
        'Settings.get_config_info': () => ({}),
        'Settings.get_config_content': (key) => {
          if (key === 'app') {
            return {
              type: 'app',
              content: JSON.stringify(state),
            };
          }
          return { type: key, content: '' };
        },
        'Settings.save_config_content': (key, content) => {
          saves.push({ key, content });
          if (key === 'app') {
            try {
              state = JSON.parse(content);
            } catch (err) {
              // Preserve state; test only asserts on saves.
            }
          }
          return { status: 'ok' };
        },
      });
    });

    it('switch is rendered disabled', async () => {
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.disabled).toBe(true);
    });

    it('click does not flip the switch', async () => {
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      expect(sw.getAttribute('aria-checked')).toBe('false');
      sw.click();
      await settle(el);
      // Still off — the lock guard returned early.
      expect(sw.getAttribute('aria-checked')).toBe('false');
      expect(sw.classList.contains('on')).toBe(false);
    });

    it('click does not write to app.json', async () => {
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      sw.click();
      await settle(el);
      expect(saves.length).toBe(0);
    });

    it('renders a locked-state note', async () => {
      const el = mountTab();
      await settle(el);
      const note = el.shadowRoot.querySelector(
        '.toggle-readonly-note',
      );
      expect(note).toBeTruthy();
      // The CONFIG_CARDS entry sets `lockedNote: 'Locked
      // — feature in early development'`. The exact
      // wording is the source of truth; the test just
      // confirms a note renders rather than pinning the
      // string verbatim (so a copy-edit of the lockedNote
      // doesn't break this test).
      expect(note.textContent.trim().length).toBeGreaterThan(0);
    });

    it('repeated clicks remain inert', async () => {
      const el = mountTab();
      await settle(el);
      const sw = getToggleSwitch(el);
      sw.click();
      sw.click();
      sw.click();
      await settle(el);
      expect(saves.length).toBe(0);
      expect(sw.getAttribute('aria-checked')).toBe('false');
    });
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

describe('ac-settings-tab config cards', () => {
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
      'Agentic coding',
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

describe('ac-settings-tab info banner', () => {
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