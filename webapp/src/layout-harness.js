// Layout harness — the scenes only a real layout engine can answer for.
//
// specs5/next.md § D2 carries three rendering behaviours the unit suite
// cannot reach, and they are all the same shape: the CSS rule *arrives*,
// which is what a jsdom test can see, and nothing measures what it lays
// out, which is the only thing anybody cares about. The permission
// dialog's Monaco style clone is asserted by "the rules are in the shadow
// root"; the tool-card header's two-column grid was read by hand at 520,
// 400 and 300px, twice, and neither reading is repeatable by anything in
// the suite; and the dialog rendered ~570px of empty editor for a
// one-line diff in front of 60 passing dialog tests.
//
// So this is not a screenshot dump. A picture nobody looks at is not a
// regression test — it is a file. What makes a browser worth launching is
// that it *measures*: every scene here returns numbers read out of a real
// layout, and `scripts/layout_probe.py` asserts on those. The screenshots
// it writes beside them are evidence for the human who has to understand
// a failure, not the assertion itself.
//
// Why the components are mounted directly rather than driven through a
// live turn: these are layout questions. A live engine would add
// credentials, a turn's worth of latency and a non-deterministic payload
// to answer a question about pixels, and the guide's argument for driving
// the real app (specs5/0-overview/implementation-guide.md § Verifying UI
// Work Against a Running Engine) is about claims in prose and mechanisms
// in our own code — not about what the layout engine does with a grid.
// What is load-bearing is that the CSS, the component and the browser are
// the real ones, and they are.
//
// Served by Vite dev off `webapp/layout-harness.html`, deliberately: a
// harness pointed at `webapp/dist` reports on whatever was last built,
// and a regression harness that can pass against stale bytes is worse
// than none. Every style here is either a Lit `css` literal on the
// component (identical in both modes) or emitted by Monaco's own
// JavaScript at runtime, so the serving-mode caveat in that same section
// does not bite: there is no build-time CSS transform for the modes to
// disagree about.

import { render } from 'lit';

import './permission-dialog/index.js';
import { renderBlock } from './chat-panel/block-render.js';
import { STYLES } from './chat-panel/styles.js';

// ---------------------------------------------------------------------------
// Measurement
// ---------------------------------------------------------------------------

const round = (n) => Math.round(n * 10) / 10;

/** A bounding box, rounded — sub-pixel noise is not a regression. */
function box(el) {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: round(r.x), y: round(r.y), w: round(r.width), h: round(r.height) };
}

/** Query through shadow roots. Depth-first, and it stops at the first hit. */
function deep(root, selector, depth = 0) {
  if (!root || depth > 8) return null;
  const scope = root.shadowRoot || root;
  if (scope.querySelector) {
    const hit = scope.querySelector(selector);
    if (hit) return hit;
    for (const el of scope.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const found = deep(el, selector, depth + 1);
        if (found) return found;
      }
    }
  }
  return null;
}

/** Every match through the shadow roots, in document order. */
function deepAll(root, selector, depth = 0, out = []) {
  if (!root || depth > 8) return out;
  const scope = root.shadowRoot || root;
  if (!scope.querySelectorAll) return out;
  out.push(...scope.querySelectorAll(selector));
  for (const el of scope.querySelectorAll('*')) {
    if (el.shadowRoot) deepAll(el, selector, depth + 1, out);
  }
  return out;
}

const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));

/** Let the browser paint `n` times. Monaco settles over several frames. */
async function settle(n = 3) {
  for (let i = 0; i < n; i += 1) await frame();
}

/**
 * Poll `predicate` until it holds.
 *
 * Rejects rather than resolving false: a scene that measures a thing that
 * never appeared reports zeros, and zeros read as a finding.
 */
async function waitFor(predicate, what, timeout = 20000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      if (predicate()) return;
    } catch (_) {
      // Not there yet — a half-built scene throws on the way up.
    }
    await frame();
  }
  throw new Error(`timed out waiting for ${what}`);
}

// ---------------------------------------------------------------------------
// Payloads — shapes from src/aic_dc/claude_code/permissions.py, the same
// ones webapp/src/permission-dialog/dialog.test.js builds. Kept here rather
// than imported from the test file because that file imports vitest.
// ---------------------------------------------------------------------------

function writePayload({ lines = 1, newFile = false } = {}) {
  const original = newFile
    ? ''
    : Array.from({ length: lines }, (_, i) => `line ${i + 1}`).join('\n') + '\n';
  const proposed = Array.from(
    { length: lines },
    (_, i) => (i === 0 ? 'line 1 — changed' : `line ${i + 1}`),
  ).join('\n') + '\n';
  return {
    permission_id: `perm_write_${lines}${newFile ? '_new' : ''}`,
    tool_use_id: 'toolu_write',
    request_id: 'req_1',
    tool_name: 'Write',
    display_name: 'Write',
    tool_class: 'write',
    gated_by_default: true,
    summary: 'write src/main.py',
    input: { file_path: 'src/main.py', content: proposed },
    diff: {
      path: 'src/main.py',
      original,
      proposed,
      additions: newFile ? lines : 1,
      deletions: newFile ? 0 : 1,
      is_new_file: newFile,
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
    expires_at: null,
    localhost_available: true,
  };
}

function toolBlock({ toolName = 'Bash', input, summary } = {}) {
  return {
    kind: 'tool',
    block_id: 'toolu_01',
    tool: {
      name: toolName,
      input: input ?? { command: summary ?? 'ls -la' },
    },
  };
}

/**
 * The parts of a ChatPanel the block renderers touch, and nothing else —
 * the same stub `block-render.test.js` uses, for the same reason: a
 * template that grows a dependency on tab state should fail here.
 */
function stubPanel() {
  return {
    _blockExpansion: new Map(),
    repoFiles: [],
    requestUpdate: () => {},
    _stopSubagent: () => {},
  };
}

// ---------------------------------------------------------------------------
// Scenes
// ---------------------------------------------------------------------------

const mounted = [];

function clear() {
  while (mounted.length) {
    const el = mounted.pop();
    try { el.remove(); } catch (_) { /* already gone */ }
  }
}

/**
 * The permission dialog on a `write` request.
 *
 * Measures the two things 60 dialog tests cannot: how tall the editor is
 * against how tall its content is, and whether Monaco's own metrics
 * survived the crossing into the shadow root.
 */
async function dialogWrite(opts = {}) {
  const dialog = document.createElement('aic-permission-dialog');
  document.body.appendChild(dialog);
  mounted.push(dialog);
  await dialog.updateComplete;

  window.dispatchEvent(new CustomEvent('permission-request', {
    detail: writePayload(opts),
  }));
  await dialog.updateComplete;

  await waitFor(() => deep(dialog, '.diff-host'), 'the diff host');
  await waitFor(() => dialog._diffEditor, 'the diff editor');
  await waitFor(
    () => deepAll(dialog, '.view-line').length > 0,
    'Monaco to render a line',
  );
  // The diff is computed asynchronously, and the alignment view zones it
  // inserts are part of the content height. Waiting on a decoration is
  // waiting on that pass having finished.
  await waitFor(
    () => deepAll(dialog, '.line-insert, .char-insert, .line-delete, .char-delete').length > 0,
    'a diff decoration',
  );
  await settle(4);

  const modified = dialog._diffEditor.getModifiedEditor?.();
  const original = dialog._diffEditor.getOriginalEditor?.();
  const contentHeight = Math.max(
    modified?.getContentHeight?.() ?? 0,
    original?.getContentHeight?.() ?? 0,
  );

  // Line pitch, read off the elements rather than off Monaco's options:
  // the whole failure mode this catches is Monaco believing one number
  // while the browser paints another.
  const viewLines = deepAll(dialog, '.view-line');
  const lineTops = [...new Set(
    deepAll(dialog, '.margin-view-overlays > div')
      .map((el) => round(el.getBoundingClientRect().top)),
  )];
  const lineNumbers = deepAll(dialog, '.line-numbers');
  const scrollable = deep(dialog, '.monaco-scrollable-element');

  // The bounds the *app* declares, so the probe asserts against one
  // source of truth rather than against a number copied into Python.
  // Absent means the height is not content-driven at all, which is the
  // defect this scene was written for.
  const host = deep(dialog, '.diff-host');
  const declared = host ? getComputedStyle(host) : null;
  const prop = (name) => {
    const raw = declared?.getPropertyValue(name)?.trim();
    return raw ? raw : null;
  };

  return {
    window: { w: window.innerWidth, h: window.innerHeight },
    dialog: box(deep(dialog, '.dialog')),
    body: box(deep(dialog, '.body')),
    diffHost: box(host),
    declaredMinHeight: prop('--diff-min-height'),
    declaredMaxHeight: prop('--diff-max-height'),
    declaredContentHeight: prop('--diff-content-height'),
    contentHeight: round(contentHeight),
    viewLineCount: viewLines.length,
    viewLineHeight: viewLines.length ? round(viewLines[0].getBoundingClientRect().height) : 0,
    // One distinct top per rendered line. The style-clone failure piled
    // every line number onto one row, which collapses this to 1.
    distinctMarginRows: lineTops.length,
    lineNumberCount: lineNumbers.length,
    decorations: deepAll(dialog, '.line-insert, .char-insert, .line-delete, .char-delete').length,
    monacoFont: viewLines.length
      ? getComputedStyle(viewLines[0]).fontFamily
      : null,
    dialogFont: getComputedStyle(deep(dialog, '.dialog')).fontFamily,
    scrolls: scrollable
      ? scrollable.scrollHeight > scrollable.clientHeight + 1
      : null,
  };
}

/**
 * One tool card at one card width.
 *
 * The wrapper stands in for `aic-chat-panel`: same shadow root, same
 * stylesheet, so `:host` and the `@container` query on `.tool-card` are
 * answered by the same rules the app uses. The width is the card's own,
 * which is what the container query reads — not the viewport's.
 */
async function toolCard(opts = {}) {
  const width = opts.width ?? 520;
  const wrapper = document.createElement('div');
  wrapper.style.width = `${width}px`;
  wrapper.style.height = 'auto';
  document.body.appendChild(wrapper);
  mounted.push(wrapper);

  const root = wrapper.attachShadow({ mode: 'open' });
  const style = document.createElement('style');
  style.textContent = STYLES.cssText;
  root.appendChild(style);
  const mount = document.createElement('div');
  root.appendChild(mount);

  render(renderBlock(stubPanel(), toolBlock(opts)), mount);
  await settle(3);

  const card = root.querySelector('.tool-card');
  const header = root.querySelector('.tool-header');
  const caret = root.querySelector('.tool-caret');
  const name = root.querySelector('.tool-name');
  const summary = root.querySelector('.tool-summary');

  return {
    width,
    toolName: opts.toolName ?? 'Bash',
    gridTemplateColumns: header ? getComputedStyle(header).gridTemplateColumns : null,
    card: box(card),
    header: box(header),
    caret: box(caret),
    name: box(name),
    summary: box(summary),
    meta: box(root.querySelector('.tool-meta')),
    // The one thing in this header that must not wrap: a name whose line
    // number depends on its character count leaves a caret and a dot
    // alone above it, and a name is what the eye scans for down the rail.
    nameOnCaretsLine: !!(caret && name)
      && Math.abs(caret.getBoundingClientRect().top - name.getBoundingClientRect().top) <= 2,
    overflows: card ? card.scrollWidth > card.clientWidth + 1 : null,
  };
}

const SCENES = {
  'dialog-write': dialogWrite,
  'tool-card': toolCard,
};

window.__layout = {
  scenes: Object.keys(SCENES),
  async build(name, opts = {}) {
    clear();
    const scene = SCENES[name];
    if (!scene) throw new Error(`no such scene: ${name}`);
    return scene(opts);
  },
  clear,
};

// The driver polls for this rather than for `load`: the module graph here
// pulls in Monaco, and a page that has fired `load` may still be
// evaluating it.
window.__layoutReady = true;
