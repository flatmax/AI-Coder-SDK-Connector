// Cloning document.head's stylesheets into a shadow root.
//
// Monaco writes its rules to document.head — the bulk at module load, and
// more (the theme's colour rules, the measured font metrics) synchronously
// during the construction of an editor. A shadow root sees none of them.
// An editor built inside one therefore lays out with Monaco's own metrics
// while rendering with the host's inherited font: absolutely-positioned
// view-lines at the wrong pitch, line numbers stacked on one row, and
// every line wrapped at the container's width. It looks less like a
// missing theme than like a corrupted editor, which is why this module
// says so here rather than in either caller.
//
// Two consumers: the diff viewer, which adds a KaTeX stylesheet of its own
// on top, and the permission dialog's write body. The dialog had no style
// sync at all until this module existed — its editor was the broken one —
// so the logic lives in one place now instead of being a thing the next
// shadow-DOM Monaco host has to remember to copy.

/**
 * Default dataset marker stamped on every clone.
 *
 * Clones carry a marker so a re-sync can sweep its own previous clones
 * without touching styles the host put in its shadow root for other
 * reasons.
 */
export const HEAD_CLONE_MARKER = 'acDcMonacoClone';

/** The attribute selector for a dataset key (`acDcFoo` → `data-ac-dc-foo`). */
function attrFor(marker) {
  return `data-${marker.replace(/([A-Z])/g, '-$1').toLowerCase()}`;
}

/** True for a head node whose styles are worth cloning. */
function isStyleNode(node) {
  if (!node || node.nodeType !== 1) return false;
  if (node.tagName === 'STYLE') return true;
  if (node.tagName !== 'LINK') return false;
  return (node.getAttribute('rel') || '').toLowerCase() === 'stylesheet';
}

/**
 * Replace the shadow root's clones with the current contents of
 * document.head.
 *
 * A full re-sync rather than an incremental one because Monaco adds styles
 * *during* editor construction, before the constructor returns — an
 * observer fires asynchronously and would miss them, leaving the first
 * editor on the page unstyled.
 *
 * @param {object} host — an element with a shadowRoot
 * @param {string} [marker] — dataset key to stamp on the clones
 */
export function syncHeadStyles(host, marker = HEAD_CLONE_MARKER) {
  if (!host?.shadowRoot) return;
  for (const el of host.shadowRoot.querySelectorAll(`[${attrFor(marker)}]`)) {
    el.remove();
  }
  for (const el of document.head.querySelectorAll('style, link')) {
    if (!isStyleNode(el)) continue;
    const clone = el.cloneNode(true);
    clone.dataset[marker] = 'true';
    host.shadowRoot.appendChild(clone);
  }
}

/**
 * Apply a batch of head mutations to the clones.
 *
 * Added nodes are cloned in; removed ones are matched to their clone by
 * text content (a `<style>`) or `href` (a `<link>`), which is the only
 * handle available once the original node is gone.
 */
export function applyHeadMutations(host, mutations, marker = HEAD_CLONE_MARKER) {
  if (!host?.shadowRoot) return;
  for (const mutation of mutations || []) {
    for (const node of mutation.addedNodes || []) {
      if (!isStyleNode(node)) continue;
      const clone = node.cloneNode(true);
      clone.dataset[marker] = 'true';
      host.shadowRoot.appendChild(clone);
    }
    for (const node of mutation.removedNodes || []) {
      if (!isStyleNode(node)) continue;
      const clones = host.shadowRoot.querySelectorAll(`[${attrFor(marker)}]`);
      for (const clone of clones) {
        if (
          (node.tagName === 'STYLE' && clone.textContent === node.textContent)
          || (node.tagName === 'LINK'
            && clone.getAttribute('href') === node.getAttribute('href'))
        ) {
          clone.remove();
        }
      }
    }
  }
}

/**
 * Watch document.head and keep the clones current.
 *
 * Returns the observer so the caller owns its lifetime, or null where
 * MutationObserver is unavailable — in which case the full re-sync on the
 * next editor creation is the fallback, which is what the diff viewer ran
 * on for its whole life before this module existed.
 */
export function observeHeadStyles(host, marker = HEAD_CLONE_MARKER) {
  if (!host?.shadowRoot) return null;
  if (typeof MutationObserver === 'undefined') return null;
  try {
    const observer = new MutationObserver(
      (mutations) => applyHeadMutations(host, mutations, marker),
    );
    observer.observe(document.head, { childList: true });
    return observer;
  } catch (_) {
    return null;
  }
}
