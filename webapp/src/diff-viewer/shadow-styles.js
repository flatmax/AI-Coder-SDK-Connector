// Shadow-DOM style synchronisation.
//
// The cloning itself lives in ../shadow-style-sync.js — the permission
// dialog hosts a Monaco diff editor in its own shadow root and needs the
// same thing, and it went without for long enough to ship a visibly
// broken editor. What stays here is the part that is the viewer's alone.
//
// KaTeX CSS is special: it isn't in document.head
// either (we import it as a raw string), so we inject
// it explicitly and tag it with a separate marker so
// the Monaco-clone sweep doesn't touch it.

import {
  applyHeadMutations,
  syncHeadStyles,
} from '../shadow-style-sync.js';

import {
  _CLONED_STYLE_MARKER,
  _KATEX_CSS_MARKER,
  katexCssText,
} from './constants.js';

/**
 * Clone all document.head styles into the shadow root.
 * Runs every editor creation; removes prior clones
 * first so the count doesn't grow across re-creations.
 */
export function syncAllStyles(host) {
  if (!host.shadowRoot) return;
  syncHeadStyles(host, _CLONED_STYLE_MARKER);
  ensureKatexCss(host);
}

/**
 * Inject the KaTeX stylesheet into the shadow root if
 * not already present. Idempotent — only one copy ever
 * lives in the shadow root regardless of how many
 * times syncAllStyles runs.
 */
export function ensureKatexCss(host) {
  if (!host.shadowRoot) return;
  const attrName = _KATEX_CSS_MARKER.replace(
    /([A-Z])/g,
    '-$1',
  ).toLowerCase();
  const existing = host.shadowRoot.querySelector(
    `[data-${attrName}]`,
  );
  if (existing) return;
  const style = document.createElement('style');
  style.dataset[_KATEX_CSS_MARKER] = 'true';
  style.textContent = katexCssText;
  host.shadowRoot.appendChild(style);
}

export function ensureStyleObserver(host) {
  if (host._styleObserver) return;
  if (typeof MutationObserver === 'undefined') return;
  try {
    host._styleObserver = new MutationObserver(
      host._onHeadMutation,
    );
    host._styleObserver.observe(document.head, {
      childList: true,
    });
  } catch (_) {
    // No MutationObserver — full re-sync on every
    // editor creation is the fallback.
  }
}

export function disposeStyleObserver(host) {
  if (host._styleObserver) {
    try {
      host._styleObserver.disconnect();
    } catch (_) {}
    host._styleObserver = null;
  }
}

export function onHeadMutation(host, mutations) {
  applyHeadMutations(host, mutations, _CLONED_STYLE_MARKER);
}