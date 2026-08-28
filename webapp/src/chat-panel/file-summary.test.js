// Tests for the "Files Referenced" summary in rendering.js —
// `collectMessageFiles` and `renderFileSummary`.
//
// This pair serves archived prose turns only: a Claude Code turn gets
// `renderTurnFooter`, which reads the files the agent reported. Here the list
// is *inferred* from the message — edit block headers plus prose mentions
// matched against the picker's file list — which is why the two halves do not
// have to agree on how a path is spelled.
//
// The properties worth failing over:
//
//   1. **One file is one chip.** The two sources spell paths differently: a
//      prose mention is matched against `repoFiles` and so is repo-relative,
//      while an edit block header carries whatever the LLM wrote, which is
//      absolute when it echoed a tool result. Deduplicating on the raw string
//      would list that file twice, and since § C4 gave both chips the same
//      label, as two chips that look identical.
//   2. **The label is repo-relative; the path as found stays on the tooltip
//      and in the click.** The house rule for naming files in this app
//      (`context-usage-tab.js`), and `onNavigateFile` remains the one
//      normaliser — a label must not become the navigation contract.

import { render } from 'lit';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EDIT_MARK, END_MARK, REPL_MARK } from '../edit-blocks.js';
import { resetRepoRoot, setRepoRoot } from '../repo-path.js';
import { collectMessageFiles, renderFileSummary } from './rendering.js';

const ROOT = '/home/dev/my-repo';

const hosts = [];

function draw(template) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  hosts.push(host);
  render(template, host);
  return host;
}

afterEach(() => {
  while (hosts.length) hosts.pop().remove();
  resetRepoRoot();
});

/** An edit block naming `path`, as the parser expects to read it. */
const editBlock = (path) =>
  [path, EDIT_MARK, 'old line', REPL_MARK, 'new line', END_MARK].join('\n');

const assistant = (content) => ({ role: 'assistant', content });

const panelWith = (repoFiles = []) => ({ repoFiles });

const chips = (host) =>
  [...host.querySelectorAll('.file-chip')].map((chip) => ({
    label: chip.querySelector('.file-chip-path').textContent.trim(),
    title: chip.getAttribute('title'),
    aria: chip.getAttribute('aria-label'),
  }));

describe('collectMessageFiles', () => {
  it('collects an edit block target and a prose mention', () => {
    const msg = assistant(
      `Updating src/a.js.\n\n${editBlock('src/b.js')}`,
    );
    expect(collectMessageFiles(panelWith(['src/a.js']), msg))
      .toEqual([{ path: 'src/b.js' }, { path: 'src/a.js' }]);
  });

  it('lists a file once when the two sources spell it differently', () => {
    // The edit block echoed the engine's absolute path; the prose named the
    // same file the way the picker knows it. One file, one chip.
    setRepoRoot(ROOT);
    const msg = assistant(
      `Changing src/a.js now.\n\n${editBlock(`${ROOT}/src/a.js`)}`,
    );
    expect(collectMessageFiles(panelWith(['src/a.js']), msg))
      .toEqual([{ path: `${ROOT}/src/a.js` }]);
  });

  it('keeps the path as it was found, not the relative form', () => {
    // What the entry carries is what the chip's tooltip can show. The
    // conversion belongs to the dedup key and the label, not to the record.
    setRepoRoot(ROOT);
    const msg = assistant(editBlock(`${ROOT}/src/a.js`));
    expect(collectMessageFiles(panelWith([]), msg))
      .toEqual([{ path: `${ROOT}/src/a.js` }]);
  });

  it('still lists two genuinely different files', () => {
    // The dedup key must not collapse distinct files that happen to share a
    // basename — it is a whole repo-relative path, not a name.
    setRepoRoot(ROOT);
    const msg = assistant(
      `${editBlock(`${ROOT}/src/a.js`)}\n\n${editBlock(`${ROOT}/tests/a.js`)}`,
    );
    expect(collectMessageFiles(panelWith([]), msg)).toEqual([
      { path: `${ROOT}/src/a.js` },
      { path: `${ROOT}/tests/a.js` },
    ]);
  });

  it('keeps a file outside the repo separate from one inside it', () => {
    // `/etc/a.js` has no repo-relative name, so the key stays absolute and
    // cannot collide with the repo's own `a.js`.
    setRepoRoot(ROOT);
    const msg = assistant(
      `${editBlock('/etc/a.js')}\n\n${editBlock(`${ROOT}/a.js`)}`,
    );
    expect(collectMessageFiles(panelWith([]), msg)).toEqual([
      { path: '/etc/a.js' },
      { path: `${ROOT}/a.js` },
    ]);
  });

  it('ignores anything that is not an assistant message', () => {
    const content = editBlock('src/a.js');
    expect(collectMessageFiles(panelWith([]), { role: 'user', content }))
      .toEqual([]);
    expect(collectMessageFiles(panelWith([]), null)).toEqual([]);
  });
});

describe('renderFileSummary', () => {
  it('renders nothing without files', () => {
    expect(draw(renderFileSummary(panelWith(), [])).querySelector('.file-chip'))
      .toBeNull();
    expect(
      draw(renderFileSummary(panelWith(), null)).querySelector('.file-chip'),
    ).toBeNull();
  });

  it('labels an absolute path relatively and keeps it on the tooltip', () => {
    // § C4 — the same rule the tool cards' chips follow, from the same
    // module, so the two lists cannot name one file two ways.
    setRepoRoot(ROOT);
    const abs = `${ROOT}/src/chat-panel/rendering.js`;
    const host = draw(renderFileSummary(panelWith(), [{ path: abs }]));
    expect(chips(host)).toEqual([{
      label: 'src/chat-panel/rendering.js',
      title: `${abs} — click to open`,
      aria: `Open ${abs}`,
    }]);
  });

  it('leaves a path outside the repo at its absolute name', () => {
    setRepoRoot(ROOT);
    const host = draw(renderFileSummary(panelWith(), [{ path: '/etc/hosts' }]));
    expect(chips(host)[0].label).toBe('/etc/hosts');
  });

  it('shows an already-relative path unchanged', () => {
    setRepoRoot(ROOT);
    const host = draw(renderFileSummary(panelWith(), [{ path: 'src/a.js' }]));
    expect(chips(host)[0].label).toBe('src/a.js');
  });

  it('opens the path as found, not the label', () => {
    // `onNavigateFile` normalises; a chip that announced its shortened label
    // would be a second converter to keep true.
    setRepoRoot(ROOT);
    const abs = `${ROOT}/src/a.js`;
    const panel = { repoFiles: [], dispatchEvent: vi.fn() };
    const host = draw(renderFileSummary(panel, [{ path: abs }]));
    host.querySelector('.file-chip')
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(panel.dispatchEvent).toHaveBeenCalledOnce();
    const announced = panel.dispatchEvent.mock.calls[0][0];
    expect(announced.type).toBe('file-chip-click');
    expect(announced.detail.path).toBe(abs);
  });
});
