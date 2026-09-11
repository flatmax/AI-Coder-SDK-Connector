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
// § B1's residue is the fourth, and it is the shape at its plainest: the
// usage HUD's collapse behaviour is asserted from the DOM and jsdom
// answers it honestly, because presence is not layout — while whether
// five sections and their heads fit 300px on a real screen has only ever
// been read by eye.
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
import './usage-hud.js';
import './settings-tab.js';
import { renderBlock } from './chat-panel/block-render.js';
import { STYLES } from './chat-panel/styles.js';
import { setRepoRoot } from './repo-path.js';
import { resetCapabilities } from './engine-capabilities.js';
import { SharedRpc } from './rpc.js';

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

// The HUD's three payloads, at the widest each one can honestly be. The
// question this scene answers is whether five sections fit 300px, so a
// fixture at a comfortable width would answer it for a case nobody worries
// about. Every value below is one a real session produces.

/**
 * A `ContextUsageResponse` at the widest headline the HUD can draw.
 *
 * The headline is `NN% · total/max`, so the widest is a full 1M window:
 * `100% · 1.00M/1.00M`, three digits of percentage and two `M` figures.
 * Sonnet's 1M context makes that a real reading rather than a contrived
 * one, and 200K would leave the check passing on four fewer characters
 * than the app can be asked to draw.
 *
 * The content rows sum to `totalTokens` exactly, because
 * `partitionCategories` only reports `verified` when they do — and an
 * unverified payload falls back to one solid bar segment, which would
 * quietly measure a simpler render than the app's own.
 */
function contextPayload() {
  return {
    categories: [
      { name: 'System prompt', tokens: 3200, color: 'promptBorder' },
      { name: 'System tools', tokens: 14800, color: 'claude' },
      { name: 'MCP tools', tokens: 21400, color: 'purple_FOR_SUBAGENTS_ONLY' },
      { name: 'Memory files', tokens: 6600, color: 'promptBorder' },
      { name: 'Messages', tokens: 954000, color: 'claude' },
      { name: 'Deferred tools', tokens: 9000, color: 'inactive', isDeferred: true },
      { name: 'Free space', tokens: 0, color: 'promptBorder' },
    ],
    totalTokens: 1000000,
    maxTokens: 1000000,
    rawMaxTokens: 1000000,
    autoCompactThreshold: 840000,
    percentage: 100,
    model: 'claude-sonnet-4-5-20250929',
    isAutoCompactEnabled: true,
  };
}

/**
 * The repo root, so the file chips are labelled the way the app labels
 * them.
 *
 * In the app this arrives from the backend and `toRepoPath` reads it.
 * Without it every chip renders its absolute path, which is a wider chip
 * than the app ever draws — so the scene would be measuring a label the
 * house rule forbids (specs5/next.md § C4) and reporting it as a pass.
 */
const REPO_ROOT = '/home/you/repo';

/**
 * A `streamComplete` result, in the shape `cost.py` builds.
 *
 * Three models, and the third is the point. "The model name gives way
 * before the numbers do" is a claim about what happens when a row
 * *cannot* fit, so a fixture whose rows all fit demonstrates nothing —
 * and the row turns out to have real headroom at 300px. Both a dated id
 * (`claude-opus-4-6-20260514`) and the `us.anthropic.claude-opus-5-v1:0`
 * form `modelUsageLines`' docstring names for Bedrock and Vertex were
 * measured here and **neither clips**, which is worth knowing on its own.
 *
 * What does is the same case one step further out: on Bedrock the model
 * id may be a cross-region inference-profile ARN, passed through as
 * `ANTHROPIC_MODEL` and reported back as the key of `turn_model_usage`
 * unchanged, because there is no `canonicalModel` beside it to prefer.
 * That is the widest a model name gets and it is where the ellipsis has
 * to land on the name rather than on the count.
 *
 * `turn_cost_basis` is `measured` with a figure, so the cost chip
 * renders a price. Its other two renderings are § D4's item and are not
 * layout questions.
 */
function turnPayload({ files = 3 } = {}) {
  return {
    session_id: 'sess-layout',
    response: 'done',
    subtype: 'success',
    terminal_reason: null,
    is_error: false,
    num_turns: 1,
    duration_ms: 128400,
    usage: { input_tokens: 100, output_tokens: 50 },
    model_usage: null,
    total_cost_usd: 4.82,
    turn_model_usage: {
      'claude-opus-4-6-20260514': {
        inputTokens: 18400,
        outputTokens: 128000,
        cacheReadInputTokens: 860000,
        cacheCreationInputTokens: 24000,
        costUSD: 1.482,
      },
      ['arn:aws:bedrock:us-east-1:123456789012:inference-profile/'
        + 'us.anthropic.claude-opus-5-v1:0']: {
        inputTokens: 11200,
        outputTokens: 30000,
        cacheReadInputTokens: 408800,
        costUSD: 0.71,
      },
      'claude-haiku-4-5-20251001': {
        inputTokens: 9100,
        outputTokens: 2400,
        costUSD: 0.014,
      },
    },
    turn_cost_usd: 1.496,
    turn_cost_basis: 'measured',
    tool_calls: 42,
    permission_prompts: 0,
    // Absolute, as every path the engine reports is. `toRepoPath` is what
    // the chip does with them, and a path outside the root stays whole —
    // which is the widest a chip gets.
    files_modified: Array.from(
      { length: files },
      (_, i) => `${REPO_ROOT}/src/aic_dc/claude_code/module_${i + 1}.py`,
    ),
    cancelled: false,
    mirror_gap: false,
    user_message_id: 'msg-layout',
  };
}

/**
 * A `RateLimitEvent`'s record.
 *
 * `seven_day_sonnet` on purpose: it is the longest label in
 * `rate-limit.js`'s table, so the section's name is "7-day Sonnet limit"
 * — the widest name any HUD section head is asked to hold, and the one
 * that decides whether a name and its headline share a line at 300px.
 *
 * `resets_at` is a fixed timestamp years out, for two reasons that point
 * the same way. It keeps the window open on every future run, so this
 * scene does not expire — `hasSomethingToSay` drops a closed one and the
 * section would vanish. And a reset on another date renders
 * `formatResetTime`'s long form, time and weekday and date, which is the
 * widest that line gets; a relative offset would have drawn the short
 * form on any run that happened to land on the same day.
 */
function limitPayload() {
  return {
    status: 'allowed_warning',
    rate_limit_type: 'seven_day_sonnet',
    utilization: 0.87,
    resets_at: 1893456000,
    overage_status: 'rejected',
    overage_resets_at: null,
    overage_disabled_reason: 'org_level_disabled',
    raw: {},
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
  // The RPC proxy is module state, not scene state. Only the settings
  // scene publishes one, and a leftover proxy would let a later scene's
  // component fetch something — so the next build starts disconnected
  // whatever the last one did.
  SharedRpc.reset();
  resetCapabilities();
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

/**
 * The usage HUD with every section in it, at its own 300px.
 *
 * specs5/next.md § B1 left one clause behind when the last three sections
 * landed, and § D2 named it as this harness's: the collapse behaviour is
 * asserted from the DOM, which jsdom answers honestly because it is
 * *presence* — a collapsed section's body is absent and its headline is
 * not — and nothing measures whether five sections and their heads fit
 * 300px on a real screen. Two claims in `usage-hud.js` are pure layout
 * and have never been read by anything:
 *
 *   - "closing one costs no height and hides no answer" (§ Sections). The
 *     head is the row that would have been there anyway, so a collapsed
 *     section's head must be exactly as tall as an open one's.
 *   - "the model name gives way before the numbers do" (`.token-model`).
 *     A clipped count is a different number, so the ellipsis has to land
 *     on the name and the `↑ … · ↓ …` value has to survive whole.
 *
 * Driven through the component's own events, not by assignment: a
 * `stream-complete` is what makes the HUD visible and builds `_turn`, and
 * a `rate-limit` push is how the fifth section arrives. `_context` is the
 * one exception and it has to be — the real path is a control request to
 * a CLI subprocess over an RPC socket, and there is neither here.
 *
 * The auto-hide is stopped by a real `pointerenter`, which is the app's
 * own documented way to hold the HUD open. A scene that measured a
 * fading overlay would read opacity as layout, and one that ran long
 * enough for the 8s timer would measure an empty page.
 */
async function usageHud(opts = {}) {
  const collapse = Array.isArray(opts.collapse) ? opts.collapse : [];

  // The collapse set persists in localStorage, so a previous build of this
  // scene would otherwise decide what this one measures. Cleared through
  // the storage API rather than by naming the HUD's key, which is private
  // to the component and would be a second copy of it here.
  try { window.localStorage.clear(); } catch (_) { /* storage unavailable */ }

  setRepoRoot(REPO_ROOT);

  const hud = document.createElement('aic-usage-hud');
  document.body.appendChild(hud);
  mounted.push(hud);
  await hud.updateComplete;

  // The breakdown, in the state a successful fetch leaves behind.
  hud._context = contextPayload();
  hud._contextError = '';
  hud._engineGone = false;

  window.dispatchEvent(new CustomEvent('rate-limit', {
    detail: { requestId: 'req-layout', data: limitPayload() },
  }));
  window.dispatchEvent(new CustomEvent('stream-complete', {
    detail: { requestId: 'req-layout', result: turnPayload(opts) },
  }));
  await hud.updateComplete;

  // Hover, so the 8-second auto-hide does not take the scene away
  // mid-measurement. The HUD's own handler, reached by its own event.
  hud.dispatchEvent(new PointerEvent('pointerenter'));

  await waitFor(() => deep(hud, '.hud'), 'the HUD');
  await waitFor(
    () => deepAll(hud, '.sec-head').length >= 4,
    'all four collapsible section heads',
  );
  await settle(3);

  // Collapsed by clicking the head, which is the gesture and the only
  // thing that also exercises the re-render. Matched on the displayed
  // name, because the stored key is deliberately not the label and the
  // label is what a reader would name.
  for (const name of collapse) {
    const head = deepAll(hud, '.sec-head').find(
      (el) => el.querySelector('.sec-name')?.textContent.trim() === name,
    );
    if (!head) throw new Error(`no section head named ${name}`);
    head.click();
  }
  if (collapse.length) {
    await hud.updateComplete;
    await settle(2);
  }

  const shell = deep(hud, '.hud');
  const shellStyle = getComputedStyle(shell);
  const sections = deepAll(hud, '.sec').map((sec) => {
    const head = sec.querySelector('.sec-head');
    const label = sec.querySelector('.sec-label');
    const name = sec.querySelector('.sec-name');
    const body = sec.querySelector('.sec-body');
    // The headline is whatever the section put on the head beside its
    // label — a `.value` for the two gauges, a `.label` for the counts.
    const headline = [...(head?.children || [])].find((el) => el !== label);
    return {
      name: name ? name.textContent.trim() : null,
      collapsed: head?.getAttribute('aria-expanded') === 'false',
      head: box(head),
      label: box(label),
      nameBox: box(name),
      headline: box(headline),
      headlineText: headline ? headline.textContent.replace(/\s+/g, ' ').trim() : '',
      // A headline that dropped to a second line is a head that did not
      // fit, and it is the failure a 300px overlay has room for.
      headlineOnNameRow: !!(name && headline)
        && Math.abs(name.getBoundingClientRect().top
          - headline.getBoundingClientRect().top) <= 2,
      overflows: head ? head.scrollWidth > head.clientWidth + 1 : null,
      bodyHeight: body ? round(body.getBoundingClientRect().height) : 0,
    };
  });

  // The per-model rows, for the rule that decides which half of a token
  // row is allowed to be clipped.
  const tokenRows = deepAll(hud, '.row').filter(
    (row) => row.querySelector('.token-model'),
  ).map((row) => {
    const model = row.querySelector('.token-model');
    const value = row.querySelector('.token-value');
    return {
      model: model.textContent.trim(),
      modelClipped: model.scrollWidth > model.clientWidth + 1,
      valueClipped: value.scrollWidth > value.clientWidth + 1,
      valueText: value.textContent.replace(/\s+/g, ' ').trim(),
      sameRow: Math.abs(model.getBoundingClientRect().top
        - value.getBoundingClientRect().top) <= 2,
    };
  });

  const turnRow = deepAll(hud, '.row').find(
    (row) => row.querySelector('.label')?.textContent.includes('This turn'),
  );
  const dismiss = deep(hud, '.dismiss');
  const dismissBox = box(dismiss);
  const shellBox = box(shell);

  return {
    window: { w: window.innerWidth, h: window.innerHeight },
    hud: shellBox,
    declaredWidth: shellStyle.width,
    declaredMaxHeight: shellStyle.maxHeight,
    // Both boxes, because `.hud` sets `width: 300px` under the default
    // `box-sizing: content-box` and carries a 1px border — so its
    // footprint is 302px and its content box is the 300 the spec names.
    // The probe asserts on the content box and prints the other, which is
    // the only way a reader of a passing run learns the overlay is two
    // pixels wider than the number in the spec.
    contentWidth: round(shell.clientWidth),
    borderBoxWidth: shellBox ? shellBox.w : null,
    // `.hud` declares `overflow-y: auto`, and a visible x-axis beside a
    // clipped y computes to auto — so this is the whole "fits 300px"
    // question in one number, and a true here is a horizontal scrollbar
    // in a 300px overlay.
    overflowsX: shell.scrollWidth > shell.clientWidth + 1,
    scrollsY: shell.scrollHeight > shell.clientHeight + 1,
    contentHeight: round(shell.scrollHeight),
    // Whether the whole overlay is on screen. The ceiling exists so the
    // HUD does not run off the bottom of the viewport, taking the part
    // that holds the dismiss button out of reach of the part that does not.
    bottom: shellBox ? round(shellBox.y + shellBox.h) : null,
    dismiss: dismissBox,
    dismissInside: !!(dismissBox && shellBox)
      && dismissBox.x >= shellBox.x - 1
      && dismissBox.x + dismissBox.w <= shellBox.x + shellBox.w + 1,
    modelLabel: box(deep(hud, '.model')),
    sectionCount: sections.length,
    sections,
    tokenRows,
    turnRow: box(turnRow),
    turnRowOverflows: turnRow
      ? turnRow.scrollWidth > turnRow.clientWidth + 1
      : null,
    turnRowText: turnRow
      ? turnRow.textContent.replace(/\s+/g, ' ').trim()
      : '',
  };
}

// ---------------------------------------------------------------------------
// Scene: the Settings tab with a config card open
// ---------------------------------------------------------------------------

/**
 * The engine's answers to everything this tab reads on mount.
 *
 * Shaped from the tab's own unit tests (`settings-tab.test.js`) and
 * wrapped the way jrpc-oo's multi-remote replies arrive — one key per
 * remote, which `rpcExtract` unwraps. Every method `onRpcReady` calls is
 * here, including the ones this scene does not measure: a missing method
 * throws through `rpcCall`, and a panel that failed to load is a panel
 * that is not on the page, which would quietly shorten the very column
 * whose length is the point.
 */
function settingsRpc() {
  const files = {
    engine: JSON.stringify({
      model: 'claude-opus-5',
      commit_model: 'claude-haiku-4-5-20251001',
      permission_mode: 'default',
      effort: 'high',
      thinking_display: 'summarized',
      max_budget_usd: null,
      cli_path: null,
      max_buffer_size: 33554432,
    }, null, 2),
    app: JSON.stringify({
      doc_index: { enabled: true, max_files: 2000 },
      agents: { enabled: true },
    }, null, 2),
  };
  const methods = {
    'ClaudeCodeService.get_engine_capabilities': () => ({}),
    'Settings.get_config_info': () => ({ config_dir: `${REPO_ROOT}/.aic-dc` }),
    'Settings.get_config_content': (type) => ({
      type,
      content: files[type] ?? '{}',
    }),
    'ClaudeCodeService.get_model': () => ({
      model: 'claude-opus-5',
      resolved: 'au.anthropic.claude-opus-5',
      models: [
        { value: 'default', resolvedModel: 'au.anthropic.claude-opus-5',
          displayName: 'Default', description: 'Use the default model' },
        { value: 'haiku', resolvedModel: 'au.anthropic.claude-haiku-4-5',
          displayName: 'Haiku', description: 'Fastest for quick answers' },
      ],
    }),
    'ClaudeCodeService.list_engines': () => ({
      active: 'claude',
      available: ['claude', 'antigravity'],
      mountable: ['claude', 'antigravity'],
    }),
    'ClaudeCodeService.get_session_storage': () => ({
      bytes: 4096, over_warning: false,
    }),
    'Settings.get_consultant_model': () => ({
      model: 'gemini-3.8-flash-high',
      inherit_value: 'auto',
      models: [
        { value: 'auto', label: 'Inherit' },
        { value: 'gemini-3.8-flash-high', label: 'Gemini 3.8 Flash' },
      ],
    }),
    'Settings.get_agy_gate': () => ({ state: 'current' }),
    'Settings.get_permission_rules': () => ([
      { id: 'rule-1', label: 'Bash(npm run test:*)' },
      { id: 'rule-2', label: 'Read(//home/you/repo/**)' },
    ]),
    'Collab.get_collab_role': () => ({ is_localhost: true }),
  };
  const proxy = {};
  for (const [name, impl] of Object.entries(methods)) {
    proxy[name] = async (...args) => ({ layout: await impl(...args) });
  }
  return proxy;
}

/** Which card to click, by the label the user reads off it. */
const CONFIG_CARD_LABEL = { engine: 'Engine Config', app: 'App Config' };

/**
 * The Settings tab with one config card open, measured.
 *
 * The failure this exists for was reported from a live session as "there
 * is no way to edit the json files": `.editor-area` was `flex: 1;
 * min-height: 0` inside a host that is a scrolling column flex container,
 * so the editor asked for a share of free space there was none of, and the
 * `min-height: 0` took away the floor its own 200px textarea would
 * otherwise have set. It laid out at 2px — the two borders — and
 * `overflow: hidden` clipped the rest. Nothing about the behaviour was
 * broken, which is why it survived: the file was fetched, the textarea
 * held it, Ctrl+S would have saved. 124 settings-tab unit tests passed
 * throughout and could not have done otherwise, because jsdom computes no
 * layout.
 *
 * So the scene has to reproduce the *condition*, not just the component.
 * The collapse only happens once the panels above have overflowed the
 * host — the measured case was 989px of content in a 473px box — and the
 * probe therefore asserts the host scrolls before it asserts anything
 * about the editor. An editor measured on a page with room to spare
 * would have passed under the broken rule too.
 *
 * The height is an option for exactly that reason: the short build is the
 * regression, and a tall one is its control.
 */
async function settingsEditor(opts = {}) {
  const key = opts.card || 'engine';
  const label = CONFIG_CARD_LABEL[key];
  if (!label) throw new Error(`no such config card: ${key}`);
  const height = typeof opts.height === 'number' ? opts.height : 470;
  const width = typeof opts.width === 'number' ? opts.width : 760;

  // Published before the element is created: the mixin flips
  // `rpcConnected` and runs `onRpcReady` off an already-ready proxy, which
  // is the order the tab's own tests use and the order the app produces.
  SharedRpc.set(settingsRpc());

  // The tab is `height: 100%` and expects a sized parent — in the app that
  // is the tab body. A holder with no height would let the host grow to
  // its content, which is the one thing that cannot happen on screen.
  const holder = document.createElement('div');
  holder.style.cssText = `position: fixed; top: 0; left: 0; width: ${width}px; `
    + `height: ${height}px; display: flex; flex-direction: column;`;
  document.body.appendChild(holder);
  mounted.push(holder);

  const tab = document.createElement('aic-settings-tab');
  holder.appendChild(tab);
  await tab.updateComplete;

  await waitFor(() => deepAll(tab, '.card').length >= 2, 'the config card grid');
  // Every panel the column is made of, because the column's height is the
  // condition under test and a scene that measured before the model panel
  // arrived would be measuring a shorter page than the app has.
  await waitFor(() => deep(tab, '.model-panel'), 'the panels above the grid');
  await settle(2);

  const collapsedHostContent = round(tab.scrollHeight);

  // Clicked, not called: `_openCard` is reachable from here, but the click
  // is the gesture and it is also what proves the card's own handler is
  // what put the editor on the page.
  const card = deepAll(tab, '.card').find(
    (el) => el.querySelector('.card-label')?.textContent.trim() === label,
  );
  if (!card) throw new Error(`no config card labelled ${label}`);
  card.click();

  await waitFor(() => deep(tab, '.editor-textarea'), 'the open editor');
  await tab.updateComplete;
  await settle(3);

  const area = deep(tab, '.editor-area');
  const toolbar = deep(tab, '.editor-toolbar');
  const textarea = deep(tab, '.editor-textarea');
  const areaStyle = getComputedStyle(area);
  const textStyle = getComputedStyle(textarea);
  const areaBox = box(area);
  const textBox = box(textarea);

  // How much of the textarea is inside its container's box. `.editor-area`
  // is `overflow: hidden`, so a textarea that extends past it is a textarea
  // the user cannot reach the bottom of — and in the failure it was the
  // whole of it.
  const visibleText = areaBox && textBox
    ? round(Math.max(0, Math.min(areaBox.y + areaBox.h, textBox.y + textBox.h)
      - Math.max(areaBox.y, textBox.y)))
    : 0;

  return {
    window: { w: window.innerWidth, h: window.innerHeight },
    card: label,
    holder: box(holder),
    host: box(tab),
    // The host is the scroller — `:host` declares `overflow-y: auto` — so
    // these two numbers are the condition the failure needed, and the
    // first is taken before the editor is opened so it is the column's own
    // length rather than the column plus what we are measuring.
    hostContentBeforeOpen: collapsedHostContent,
    hostContentHeight: round(tab.scrollHeight),
    hostVisibleHeight: round(tab.clientHeight),
    hostScrolls: tab.scrollHeight > tab.clientHeight + 1,
    editor: areaBox,
    // Printed rather than asserted on: the fix is one declaration, and a
    // reader of a failing run wants to see which one is in force.
    editorFlex: `${areaStyle.flexGrow} ${areaStyle.flexShrink} ${areaStyle.flexBasis}`,
    editorClips: area.scrollHeight > area.clientHeight + 1,
    toolbar: box(toolbar),
    textarea: textBox,
    textareaDeclaredMinHeight: textStyle.minHeight,
    textareaVisibleHeight: visibleText,
    // The half of the failure that always worked, kept so a run can tell
    // "the editor is missing" from "the editor is empty".
    contentChars: textarea.value.length,
    cardActive: card.classList.contains('active'),
  };
}

const SCENES = {
  'dialog-write': dialogWrite,
  'tool-card': toolCard,
  'usage-hud': usageHud,
  'settings-editor': settingsEditor,
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
