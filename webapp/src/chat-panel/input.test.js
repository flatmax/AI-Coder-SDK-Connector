// Tests for input handling, send flow, message rendering,
// edit-block rendering, file mentions, image paste/render,
// message action buttons, history browser,
// new-session, lightbox, and input history.

import { describe, expect, it, vi } from 'vitest';

import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  seedTab,
  settle,
} from './test-helpers.js';

// ---------------------------------------------------------------------------
// Initial state
// ---------------------------------------------------------------------------

describe('ChatPanel initial state', () => {
  it('renders the empty state when no messages', async () => {
    const p = mountPanel();
    await settle(p);
    const empty = p.shadowRoot.querySelector('.empty-state');
    expect(empty).toBeTruthy();
    expect(empty.textContent).toMatch(/conversation/i);
  });

  it('disables the input when RPC is not connected', async () => {
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    expect(ta.disabled).toBe(true);
    const note = p.shadowRoot.querySelector('.disconnected-note');
    expect(note).toBeTruthy();
  });

  it('enables the input when RPC is connected', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    expect(ta.disabled).toBe(false);
  });

  it('send button is disabled when input is empty', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const btn = p.shadowRoot.querySelector('.send-button');
    expect(btn.disabled).toBe(true);
  });

  it('send button enables after typing', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'hello';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    const btn = p.shadowRoot.querySelector('.send-button');
    expect(btn.disabled).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Message rendering
// ---------------------------------------------------------------------------

describe('ChatPanel message rendering', () => {
  it('renders user and assistant messages with labels', async () => {
    const p = mountPanel({
      messages: [
        { role: 'user', content: 'hello' },
        { role: 'assistant', content: 'hi there' },
      ],
    });
    await settle(p);
    const labels = Array.from(
      p.shadowRoot.querySelectorAll('.role-label'),
    ).map((el) => el.textContent.trim());
    expect(labels).toEqual(['You', 'Assistant']);
  });

  it('renders user content as markdown', async () => {
    // User input goes through the markdown renderer.
    const p = mountPanel({
      messages: [
        { role: 'user', content: 'use **bold** here' },
      ],
    });
    await settle(p);
    const html = p.shadowRoot.querySelector(
      '.role-user .md-content',
    ).innerHTML;
    expect(html).toContain('<strong>bold</strong>');
  });

  it('renders assistant content as markdown', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: 'use **bold** here' },
      ],
    });
    await settle(p);
    const html = p.shadowRoot.querySelector(
      '.role-assistant .md-content',
    ).innerHTML;
    expect(html).toContain('<strong>bold</strong>');
  });

  it('renders system event messages with distinct styling', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: 'Reset to HEAD',
          system_event: true,
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.role-system');
    expect(card).toBeTruthy();
    expect(card.querySelector('.md-content')).toBeTruthy();
  });

  it('renders code fences in assistant messages', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: '```\nsome code\n```',
        },
      ],
    });
    await settle(p);
    const pre = p.shadowRoot.querySelector(
      '.role-assistant pre',
    );
    expect(pre).toBeTruthy();
    expect(pre.textContent).toContain('some code');
  });
});

// ---------------------------------------------------------------------------
// Edit block rendering
// ---------------------------------------------------------------------------

describe('ChatPanel edit block rendering', () => {
  // Edit-block fixtures — literal marker bytes per D3.
  const EDIT_MARK = '🟧🟧🟧 EDIT';
  const REPL_MARK = '🟨🟨🟨 REPL';
  const END_MARK = '🟩🟩🟩 END';

  const simpleEditBlock = [
    'src/foo.py',
    EDIT_MARK,
    'old line',
    REPL_MARK,
    'new line',
    END_MARK,
  ].join('\n');

  const proseAndEdit = [
    'Here is the change:',
    '',
    simpleEditBlock,
    '',
    'That should fix it.',
  ].join('\n');

  it('renders prose segments through markdown', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: proseAndEdit },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector(
      '.role-assistant',
    );
    expect(card.querySelector('p')).toBeTruthy();
    expect(card.textContent).toContain('Here is the change');
    expect(card.textContent).toContain('That should fix it');
  });

  it('renders edit segment as edit-block-card', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: simpleEditBlock },
      ],
    });
    await settle(p);
    const cards = p.shadowRoot.querySelectorAll(
      '.edit-block-card',
    );
    expect(cards.length).toBe(1);
    expect(cards[0].querySelector('.edit-file-path').textContent).toBe(
      'src/foo.py',
    );
    expect(cards[0].querySelector('.edit-pane-content')).toBeTruthy();
    expect(cards[0].querySelector('.edit-pane-old')).toBeNull();
    expect(cards[0].querySelector('.edit-pane-new')).toBeNull();
    const removeLine = cards[0].querySelector(
      '.diff-line.remove',
    );
    const addLine = cards[0].querySelector('.diff-line.add');
    expect(removeLine).toBeTruthy();
    expect(addLine).toBeTruthy();
    expect(removeLine.textContent).toBe('-old line');
    expect(addLine.textContent).toBe('+new line');
  });

  it('renders message without editResults in pending state', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: simpleEditBlock },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.edit-block-card');
    expect(card).toBeTruthy();
    expect(card.classList.contains('edit-status-pending')).toBe(true);
  });

  it('applies backend result status to edit card', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: simpleEditBlock,
          editResults: [
            {
              file: 'src/foo.py',
              status: 'applied',
              message: '',
            },
          ],
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.edit-block-card');
    expect(card.classList.contains('edit-status-applied')).toBe(true);
    expect(card.classList.contains('edit-status-pending')).toBe(false);
  });

  it('renders failed edit with error message', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: simpleEditBlock,
          editResults: [
            {
              file: 'src/foo.py',
              status: 'failed',
              message: 'Anchor not unique',
              error_type: 'ambiguous_anchor',
            },
          ],
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.edit-block-card');
    expect(card.classList.contains('edit-status-failed')).toBe(true);
    const err = card.querySelector('.edit-error-message');
    expect(err).toBeTruthy();
    expect(err.textContent).toContain('Anchor not unique');
  });

  it('pairs multiple edits to same file in source order', async () => {
    const content = [simpleEditBlock, '', simpleEditBlock].join('\n');
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content,
          editResults: [
            {
              file: 'src/foo.py',
              status: 'applied',
              message: 'first',
            },
            {
              file: 'src/foo.py',
              status: 'failed',
              message: 'second',
            },
          ],
        },
      ],
    });
    await settle(p);
    const cards = p.shadowRoot.querySelectorAll('.edit-block-card');
    expect(cards.length).toBe(2);
    expect(cards[0].classList.contains('edit-status-applied')).toBe(true);
    expect(cards[1].classList.contains('edit-status-failed')).toBe(true);
    expect(cards[0].querySelector('.edit-error-message')).toBeNull();
    expect(cards[1].querySelector('.edit-error-message')).toBeTruthy();
  });

  it('renders create block with NEW pane only', async () => {
    const createBlock = [
      'src/new.py',
      EDIT_MARK,
      REPL_MARK,
      'print("hello")',
      END_MARK,
    ].join('\n');
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: createBlock },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.edit-block-card');
    expect(card.classList.contains('edit-status-new')).toBe(true);
    expect(card.querySelector('.edit-pane-content')).toBeTruthy();
    expect(card.querySelector('.edit-pane-old')).toBeNull();
    expect(card.querySelector('.edit-pane-new')).toBeNull();
    expect(card.querySelector('.diff-line.add')).toBeTruthy();
    expect(card.querySelector('.diff-line.remove')).toBeNull();
    expect(
      card.querySelector('.diff-line.add').textContent,
    ).toBe('+print("hello")');
  });

  // The three tests that used to live here streamed marker bytes and asserted
  // the live card grew an edit-block card, then that `edit_results` on the
  // completion payload stamped it applied. Neither happens now. Streamed text
  // arrives as block-keyed chunks and renders as prose; an edit arrives as a
  // `Write`/`Edit` tool card with its own result. The marker protocol survives
  // only for stored messages, which is what the rest of this section covers —
  // so these pin the boundary: markers in *streamed* text are just text.

  it('a streamed text block renders in the live card', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'Here is the change:',
        done: false,
      },
    });
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming).toBeTruthy();
    // The waiting line is replaced the moment there is something to show.
    expect(streaming.querySelector('.turn-waiting')).toBeNull();
    const text = streaming.querySelector('.block-text');
    expect(text).toBeTruthy();
    expect(text.textContent).toContain('Here is the change');
    expect(text.dataset.blockId).toBe(`${reqId}:b0`);
  });

  it('streaming cursor appears after the body', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'Working on it',
        done: false,
      },
    });
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming).toBeTruthy();
    const cursor = streaming.querySelector('.cursor');
    expect(cursor).toBeTruthy();
    // After the body, whatever the last block turned out to be — the blink is
    // the "still going" signal and has to be where the eye already is.
    expect(streaming.lastElementChild).toBe(cursor);
  });

  it('marker bytes in a streamed block stay text', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: simpleEditBlock,
        done: true,
      },
    });
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming.querySelector('.edit-block-card')).toBeNull();
    expect(streaming.querySelector('.block-text').textContent)
      .toContain('EDIT');
  });

  it('an edit arrives as a tool card and survives the freeze', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('tool-use', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_01',
        name: 'Edit',
        input: {
          file_path: 'src/foo.py',
          old_string: 'old line',
          new_string: 'new line',
        },
      },
    });
    await settle(p);
    const live = p.shadowRoot.querySelector(
      '.message-card.streaming .tool-card',
    );
    expect(live).toBeTruthy();
    expect(live.dataset.tool).toBe('Edit');
    expect(live.classList.contains('tool-status-pending')).toBe(true);
    pushEvent('tool-result', {
      requestId: reqId,
      data: {
        tool_use_id: 'toolu_01',
        status: 'ok',
        files_modified: ['src/foo.py'],
      },
    });
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'Done.', files_modified: ['src/foo.py'] },
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.message-card.streaming'),
    ).toBeNull();
    // Same card, same place, now with its result — and no edit-block card,
    // since the turn carries blocks rather than marker-bearing prose.
    const settled = p.shadowRoot.querySelector('.role-assistant .tool-card');
    expect(settled).toBeTruthy();
    expect(settled.dataset.tool).toBe('Edit');
    expect(settled.classList.contains('tool-status-ok')).toBe(true);
    expect(
      p.shadowRoot.querySelector('.edit-block-card'),
    ).toBeNull();
  });

  it('user message with edit-block-shaped content renders as text', async () => {
    const p = mountPanel({
      messages: [
        { role: 'user', content: simpleEditBlock },
      ],
    });
    await settle(p);
    const userCard = p.shadowRoot.querySelector('.role-user');
    expect(userCard.querySelector('.edit-block-card')).toBeNull();
    expect(userCard.textContent).toContain('EDIT');
    expect(userCard.textContent).toContain('REPL');
    expect(userCard.textContent).toContain('END');
  });

  it('error message does not segment', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: '**Error:** something broke',
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.role-assistant');
    expect(card.querySelector('strong')).toBeTruthy();
    expect(card.querySelector('.edit-block-card')).toBeNull();
  });

  it('empty assistant content renders empty body without crashing', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: '' },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.role-assistant');
    expect(card).toBeTruthy();
    expect(card.querySelector('.edit-block-card')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// File mentions
// ---------------------------------------------------------------------------

describe('ChatPanel file mentions', () => {
  it('does not wrap mentions when repoFiles is empty (default)', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: 'see src/foo.py for details',
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.role-assistant');
    expect(card.querySelector('.file-mention')).toBeNull();
    expect(card.textContent).toContain('src/foo.py');
  });

  it('wraps mentions in final assistant messages when repoFiles set', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content: 'see src/foo.py for details',
        },
      ],
    });
    await settle(p);
    const span = p.shadowRoot.querySelector('.file-mention');
    expect(span).toBeTruthy();
    expect(span.getAttribute('data-file')).toBe('src/foo.py');
    expect(span.textContent).toBe('src/foo.py');
  });

  it('does NOT wrap mentions in user messages', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        { role: 'user', content: 'please edit src/foo.py' },
      ],
    });
    await settle(p);
    const userCard = p.shadowRoot.querySelector('.role-user');
    expect(userCard.querySelector('.file-mention')).toBeNull();
    expect(userCard.textContent).toContain('src/foo.py');
  });

  it('does NOT wrap mentions in streaming messages', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel({ repoFiles: ['src/foo.py'] });
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'editing src/foo.py now',
        done: false,
      },
    });
    await settle(p);
    const streaming = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streaming).toBeTruthy();
    expect(streaming.querySelector('.file-mention')).toBeNull();
    expect(streaming.textContent).toContain('src/foo.py');
  });

  it('wraps mentions after stream completes', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel({ repoFiles: ['src/foo.py'] });
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    // `done: true` and still no mention link: the streaming card supplies no
    // mention candidates at all, so a finished block mid-turn is not a
    // half-written path that might yet grow — it is prose the turn has not
    // finished framing. Candidates arrive with the settled message, which is
    // also where the paths the turn's tool calls touched get added.
    pushEvent('stream-chunk', {
      requestId: reqId,
      chunk: {
        block_id: `${reqId}:b0`,
        seq: 0,
        content: 'editing src/foo.py now',
        done: true,
      },
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.file-mention'),
    ).toBeNull();
    pushEvent('stream-complete', {
      requestId: reqId,
      result: { response: 'editing src/foo.py now' },
    });
    await settle(p);
    const span = p.shadowRoot.querySelector('.file-mention');
    expect(span).toBeTruthy();
    expect(span.getAttribute('data-file')).toBe('src/foo.py');
  });

  it('wraps multiple mentions across multiple messages', async () => {
    const p = mountPanel({
      repoFiles: ['a.py', 'b.py'],
      messages: [
        { role: 'assistant', content: 'first edit a.py' },
        { role: 'assistant', content: 'then edit b.py' },
      ],
    });
    await settle(p);
    const spans = p.shadowRoot.querySelectorAll('.file-mention');
    expect(spans.length).toBe(2);
    const paths = Array.from(spans).map((s) =>
      s.getAttribute('data-file'),
    );
    expect(paths).toEqual(['a.py', 'b.py']);
  });

  it('does NOT wrap mentions inside rendered code blocks', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content: '```\nsrc/foo.py is a path\n```',
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector('.role-assistant');
    expect(card.textContent).toContain('src/foo.py');
    expect(card.querySelector('.file-mention')).toBeNull();
  });

  it('wraps mentions in prose but not in code within same message', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content:
            'edit src/foo.py\n\n```\ndo not wrap src/foo.py here\n```',
        },
      ],
    });
    await settle(p);
    const spans = p.shadowRoot.querySelectorAll('.file-mention');
    expect(spans.length).toBe(1);
  });
});

describe('ChatPanel file mention clicks', () => {
  it('dispatches file-mention-click on click', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content: 'see src/foo.py here',
        },
      ],
    });
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('file-mention-click', listener);
    const span = p.shadowRoot.querySelector('.file-mention');
    span.click();
    expect(listener).toHaveBeenCalledOnce();
    expect(listener.mock.calls[0][0].detail).toEqual({
      path: 'src/foo.py',
    });
  });

  it('event bubbles out of the shadow DOM (composed)', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content: 'see src/foo.py here',
        },
      ],
    });
    await settle(p);
    const outerListener = vi.fn();
    document.body.addEventListener(
      'file-mention-click',
      outerListener,
    );
    try {
      p.shadowRoot
        .querySelector('.file-mention')
        .click();
      expect(outerListener).toHaveBeenCalledOnce();
    } finally {
      document.body.removeEventListener(
        'file-mention-click',
        outerListener,
      );
    }
  });

  it('clicks on non-mention elements do not dispatch', async () => {
    const p = mountPanel({
      repoFiles: ['src/foo.py'],
      messages: [
        {
          role: 'assistant',
          content: 'prose before src/foo.py and after',
        },
      ],
    });
    await settle(p);
    const listener = vi.fn();
    p.addEventListener('file-mention-click', listener);
    p.shadowRoot
      .querySelector('.role-label')
      .click();
    expect(listener).not.toHaveBeenCalled();
    p.shadowRoot
      .querySelector('.message-card')
      .click();
    expect(listener).not.toHaveBeenCalled();
  });

  it('mention without data-file attribute does not dispatch', async () => {
    const p = mountPanel();
    await settle(p);
    const container = p.shadowRoot.querySelector('.messages');
    const fake = document.createElement('span');
    fake.className = 'file-mention';
    fake.textContent = 'broken';
    container.appendChild(fake);
    const listener = vi.fn();
    p.addEventListener('file-mention-click', listener);
    fake.click();
    expect(listener).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Send + streaming
// ---------------------------------------------------------------------------

describe('ChatPanel send flow', () => {
  it('adds the user message optimistically on send', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'hello world';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    p.shadowRoot.querySelector('.send-button').click();
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0].role).toBe('user');
    expect(p.messages[0].content).toBe('hello world');
  });

  it('calls ClaudeCodeService.chat_streaming with a request ID', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'hi';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    p.shadowRoot.querySelector('.send-button').click();
    await settle(p);
    expect(started).toHaveBeenCalledOnce();
    const [reqId, msg] = started.mock.calls[0];
    expect(typeof reqId).toBe('string');
    expect(reqId).toMatch(/^\d+-/);
    expect(msg).toBe('hi');
  });

  it('clears the input after send', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'bye';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    p.shadowRoot.querySelector('.send-button').click();
    await settle(p);
    expect(p._input).toBe('');
  });

  it('flips to streaming state after send', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'x';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    p.shadowRoot.querySelector('.send-button').click();
    await settle(p);
    expect(p._streaming).toBe(true);
    const btn = p.shadowRoot.querySelector('.send-button');
    expect(btn.classList.contains('stop')).toBe(true);
    expect(btn.textContent).toContain('Stop');
  });

  it('does nothing when input is empty', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    await p._send();
    expect(started).not.toHaveBeenCalled();
    expect(p.messages).toHaveLength(0);
  });

  it('does nothing while already streaming', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'first';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    p.shadowRoot.querySelector('.send-button').click();
    await settle(p);
    p._input = 'second';
    await p._send();
    expect(started).toHaveBeenCalledOnce();
  });

  it('shows an error message when chat_streaming rejects', async () => {
    const started = vi
      .fn()
      .mockRejectedValue(new Error('network down'));
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      p._input = 'hello';
      await p._send();
      await settle(p);
      expect(p.messages).toHaveLength(2);
      expect(p.messages[1].content).toContain('network down');
      expect(p._streaming).toBe(false);
    } finally {
      consoleSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// Input handling
// ---------------------------------------------------------------------------

describe('ChatPanel input handling', () => {
  it('Enter sends, Shift+Enter does not', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'hi';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        shiftKey: true,
      }),
    );
    await p.updateComplete;
    expect(started).not.toHaveBeenCalled();
    ta.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter' }),
    );
    await settle(p);
    expect(started).toHaveBeenCalledOnce();
  });

  it('Enter during IME composition does not send', async () => {
    // IME users press Enter to commit a composition.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'hi';
    ta.dispatchEvent(new Event('input'));
    await p.updateComplete;
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        isComposing: true,
      }),
    );
    await p.updateComplete;
    expect(started).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Input history
// ---------------------------------------------------------------------------

describe('ChatPanel input history — recording', () => {
  it('records message on send', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'my first prompt';
    await p._send();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual(['my first prompt']);
  });

  it('accumulates multiple sends', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'first';
    await p._send();
    await settle(p);
    p._streaming = false;
    p._currentRequestId = null;
    p._input = 'second';
    await p._send();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual(['first', 'second']);
  });

  it('does not record when send is rejected (empty input)', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    p._input = '';
    await p._send();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual([]);
  });

  it('records even when the RPC call rejects', async () => {
    const started = vi
      .fn()
      .mockRejectedValue(new Error('network boom'));
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const consoleSpy = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {});
    try {
      const p = mountPanel();
      await settle(p);
      p._input = 'will fail';
      await p._send();
      await settle(p);
      const history = p.shadowRoot.querySelector(
        'aic-input-history',
      );
      expect(history._entries).toEqual(['will fail']);
    } finally {
      consoleSpy.mockRestore();
    }
  });
});

describe('ChatPanel input history — session seeding', () => {
  it('seeds history from user messages in session-changed event', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        { role: 'user', content: 'first user msg' },
        { role: 'assistant', content: 'assistant reply' },
        { role: 'user', content: 'second user msg' },
      ],
    });
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual([
      'first user msg',
      'second user msg',
    ]);
  });

  it('skips system-event messages when seeding', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: 'Committed abc1234',
          system_event: true,
        },
        { role: 'user', content: 'real prompt' },
      ],
    });
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual(['real prompt']);
  });

  it('handles multimodal user messages (extracts text blocks)', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    pushEvent('session-changed', {
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'please look at this' },
            {
              type: 'image_url',
              image_url: { url: 'data:image/png;base64,...' },
            },
          ],
        },
      ],
    });
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual(['please look at this']);
  });

  it('empty session (new session) produces no seed entries', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('existing entry');
    pushEvent('session-changed', {
      session_id: 'sess_new',
      messages: [],
    });
    await settle(p);
    expect(history._entries).toEqual(['existing entry']);
  });
});

describe('ChatPanel input history — open/close interactions', () => {
  it('up-arrow at cursor 0 opens the overlay', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('prior message');
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = '';
    ta.setSelectionRange(0, 0);
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(history.isOpen).toBe(true);
  });

  it('up-arrow elsewhere in textarea does not open', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('prior');
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'some typed text';
    ta.setSelectionRange(5, 5);
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(history.isOpen).toBe(false);
  });

  it('up-arrow with empty history does not open', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.setSelectionRange(0, 0);
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(history.isOpen).toBe(false);
  });

  it('saves current input when opening', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('prior');
    p._input = 'draft message';
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'draft message';
    ta.setSelectionRange(0, 0);
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(history._savedInput).toBe('draft message');
  });
});

describe('ChatPanel input history — event handling', () => {
  it('selecting an entry replaces textarea content', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('recalled prompt');
    history.show('');
    await settle(p);
    history.handleKey(
      new KeyboardEvent('keydown', { key: 'Enter' }),
    );
    await settle(p);
    expect(p._input).toBe('recalled prompt');
  });

  it('cancelling restores the saved input', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('prior');
    history.show('my draft');
    await settle(p);
    history.handleKey(
      new KeyboardEvent('keydown', { key: 'Escape' }),
    );
    await settle(p);
    expect(p._input).toBe('my draft');
  });

  it('Enter in overlay does not send message', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    history.addEntry('prior');
    history.show('');
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Enter',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(started).not.toHaveBeenCalled();
    expect(history.isOpen).toBe(false);
    expect(p._input).toBe('prior');
  });
});

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------
//
// The ✨ button called `LLMService.new_session` until phase 2 took it off the
// bar: the CLI owns session identity now, and clearing the native engine while
// Claude Code carried the conversation would have looked like it worked and
// changed nothing. Phase 5 gives it a real target — `ClaudeCodeService`'s own
// `new_session`, which drops the resume target so the next turn connects blank.
//
// Nothing is cleared locally. The server broadcasts `sessionChanged` with an
// empty message list and the panel's handler acts on that, so a client that
// starts a session and a client that merely watches one start take the same
// path. These tests pin the call, the refusals, and the broadcast.

describe('ChatPanel new-session', () => {
  it('the button calls new_session', async () => {
    const newSession = vi
      .fn()
      .mockResolvedValue({ session_id: null, status: 'new' });
    publishFakeRpc({ 'ClaudeCodeService.new_session': newSession });
    const p = mountPanel();
    await settle(p);
    p.shadowRoot.querySelector('.new-session-button').click();
    await settle(p);
    expect(newSession).toHaveBeenCalledOnce();
  });

  it('does not clear the transcript on the reply', async () => {
    // `sessionChanged` clears it. Doing it here as well would have this
    // client jump ahead of every other one watching the same session.
    const newSession = vi
      .fn()
      .mockResolvedValue({ session_id: null, status: 'new' });
    publishFakeRpc({ 'ClaudeCodeService.new_session': newSession });
    const p = mountPanel({
      messages: [{ role: 'user', content: 'before' }],
    });
    await settle(p);
    p.shadowRoot.querySelector('.new-session-button').click();
    await settle(p);
    expect(p.messages).toHaveLength(1);
    pushEvent('session-changed', { session_id: null, messages: [] });
    await settle(p);
    expect(p.messages).toEqual([]);
  });

  it('a session-changed broadcast clears messages', async () => {
    // Server-driven, so a remote client's new session lands the same way.
    publishFakeRpc({});
    const p = mountPanel({
      messages: [{ role: 'user', content: 'before' }],
    });
    await settle(p);
    pushEvent('session-changed', {
      session_id: 'sess_new',
      messages: [],
    });
    await settle(p);
    expect(p.messages).toEqual([]);
  });

  it('reports a refusal from a non-host client', async () => {
    // Discarding the context every client is looking at is the host's
    // call, and the server is the one that decides who that is.
    const newSession = vi.fn().mockResolvedValue({
      error: 'restricted',
      reason: 'Only the host can start a new session',
    });
    publishFakeRpc({ 'ClaudeCodeService.new_session': newSession });
    const p = mountPanel();
    await settle(p);
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail);
    window.addEventListener('aic-toast', onToast);
    try {
      p.shadowRoot.querySelector('.new-session-button').click();
      await settle(p);
    } finally {
      window.removeEventListener('aic-toast', onToast);
    }
    expect(toasts[0].message).toContain('Only the host');
    expect(toasts[0].type).toBe('warning');
  });

  it('reports a turn that started underneath the click', async () => {
    const newSession = vi.fn().mockResolvedValue({
      error: 'A turn is still running',
      reason: 'turn_in_progress',
    });
    publishFakeRpc({ 'ClaudeCodeService.new_session': newSession });
    const p = mountPanel();
    await settle(p);
    const toasts = [];
    const onToast = (e) => toasts.push(e.detail);
    window.addEventListener('aic-toast', onToast);
    try {
      await p._onNewSession();
      await settle(p);
    } finally {
      window.removeEventListener('aic-toast', onToast);
    }
    expect(toasts[0].message).toContain('A turn is still running');
  });

  it('is disabled, and guarded, while streaming', async () => {
    // Both halves: the server refuses mid-turn, so the button goes flat
    // rather than offering a click whose only outcome is a toast — and the
    // handler still checks, because a keyboard path or a stale render is
    // not a reason to send the call anyway.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    const newSession = vi
      .fn()
      .mockResolvedValue({ session_id: null, status: 'new' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': started,
      'ClaudeCodeService.new_session': newSession,
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.new-session-button').disabled,
    ).toBe(true);
    await p._onNewSession();
    expect(newSession).not.toHaveBeenCalled();
  });

  it('is gone on a tab that is not the live session', async () => {
    // A subagent transcript and a read-only archive have no session to
    // restart, and the ✨ on such a tab would restart the one behind it.
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    seedTab(p, 'sub-1');
    p._activeTabId = 'sub-1';
    p.requestUpdate();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.new-session-button'),
    ).toBeNull();
    expect(p.shadowRoot.querySelector('.history-button')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// History browser integration
// ---------------------------------------------------------------------------

describe('ChatPanel history browser', () => {
  // The 📜 button came back with the ✨ one in phase 5, now that
  // `<aic-history-browser>` reads the CLI's own transcript mirrored under
  // `.aic-dc/sessions/` instead of `LLMService`'s session store. It stayed
  // mounted and closed through phases 2–4, which is why it only needed
  // repointing rather than rebuilding.

  it('the History button opens the browser', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    const browser = p.shadowRoot.querySelector(
      'aic-history-browser',
    );
    expect(browser.open).toBe(false);
    p.shadowRoot.querySelector('.history-button').click();
    await settle(p);
    expect(browser.open).toBe(true);
  });

  it('is offered while a turn is streaming', async () => {
    // Browsing is a pure read of the mirrored transcript — no subprocess,
    // no turn, no context — so unlike ✨ it has no reason to go flat
    // mid-stream. Resuming from inside it is what the server refuses.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': started,
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.history-button').disabled,
    ).toBe(false);
  });

  it('the handler opens it', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    p._onOpenHistory();
    await settle(p);
    const browser = p.shadowRoot.querySelector(
      'aic-history-browser',
    );
    expect(browser.open).toBe(true);
  });

  it('closes the modal on close event from browser', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    p._historyOpen = true;
    await settle(p);
    const browser = p.shadowRoot.querySelector(
      'aic-history-browser',
    );
    browser.dispatchEvent(
      new CustomEvent('close', {
        bubbles: true,
        composed: true,
      }),
    );
    await settle(p);
    expect(p._historyOpen).toBe(false);
  });

  it('closes the modal on session-loaded event', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel();
    await settle(p);
    p._historyOpen = true;
    await settle(p);
    const browser = p.shadowRoot.querySelector(
      'aic-history-browser',
    );
    browser.dispatchEvent(
      new CustomEvent('session-loaded', {
        detail: { session_id: 's1' },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(p);
    expect(p._historyOpen).toBe(false);
  });

  it('session-loaded event does not crash without session-changed follow-up', async () => {
    publishFakeRpc({
      'ClaudeCodeService.history_list': vi
        .fn()
        .mockResolvedValue([]),
    });
    const p = mountPanel({
      messages: [{ role: 'user', content: 'old message' }],
    });
    await settle(p);
    p._historyOpen = true;
    await settle(p);
    const browser = p.shadowRoot.querySelector(
      'aic-history-browser',
    );
    browser.dispatchEvent(
      new CustomEvent('session-loaded', {
        detail: { session_id: 's1' },
        bubbles: true,
        composed: true,
      }),
    );
    await settle(p);
    expect(p._historyOpen).toBe(false);
    expect(p.messages).toHaveLength(1);
  });

  it('can open modal while streaming', async () => {
    const started = vi
      .fn()
      .mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    p._onOpenHistory();
    await settle(p);
    expect(p._historyOpen).toBe(true);
  });

  // -------------------------------------------------------------------------
  // Telling the browser whether the live conversation has been read
  // -------------------------------------------------------------------------
  //
  // Resuming replaces the live session, and the modal that offers it covers
  // the transcript it is about to replace — so the panel has to say whether
  // the reader reached the end of it (specs5/5-webapp/chat.md § Resume Is Not
  // Load). The browser turns a yes into a second click.

  function browserOf(panel) {
    return panel.shadowRoot.querySelector('aic-history-browser');
  }

  it('says nothing is unread when the transcript is empty', async () => {
    const p = mountPanel();
    await settle(p);
    expect(browserOf(p).liveUnread).toBe(false);
  });

  it('says nothing is unread while pinned to the bottom', async () => {
    const p = mountPanel({
      messages: [{ role: 'user', content: 'read it all' }],
    });
    await settle(p);
    p._autoScroll = true;
    p.requestUpdate();
    await settle(p);
    expect(browserOf(p).liveUnread).toBe(false);
  });

  it('says the end is unread when the reader has scrolled up', async () => {
    const p = mountPanel({
      messages: [{ role: 'user', content: 'somewhere above' }],
    });
    await settle(p);
    p._autoScroll = false;
    p.requestUpdate();
    await settle(p);
    expect(browserOf(p).liveUnread).toBe(true);
  });

  it('says the end is unread while a turn is still running', async () => {
    // Pinned or not, the end has not been written yet.
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    await settle(p);
    expect(p._streaming).toBe(true);
    expect(p._autoScroll).toBe(true);
    expect(browserOf(p).liveUnread).toBe(true);
  });

  it('scrolling back to the bottom clears it', async () => {
    // The scroll listener is the only writer of the flag the browser reads,
    // so the two have to agree without anything in between.
    const p = mountPanel({
      messages: [{ role: 'user', content: 'somewhere above' }],
    });
    await settle(p);
    const container = p.shadowRoot.querySelector('.messages');
    const scrollTo = (distanceFromBottom) => {
      Object.defineProperty(container, 'scrollHeight', {
        value: 1000,
        configurable: true,
      });
      Object.defineProperty(container, 'clientHeight', {
        value: 500,
        configurable: true,
      });
      container.scrollTop = 500 - distanceFromBottom;
      container.dispatchEvent(new Event('scroll'));
    };
    scrollTo(400);
    p.requestUpdate();
    await settle(p);
    expect(browserOf(p).liveUnread).toBe(true);
    scrollTo(0);
    p.requestUpdate();
    await settle(p);
    expect(browserOf(p).liveUnread).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Image paste
// ---------------------------------------------------------------------------

describe('ChatPanel image paste', () => {
  function pasteEvent(items) {
    const ev = new Event('paste', {
      bubbles: true,
      cancelable: true,
    });
    Object.defineProperty(ev, 'clipboardData', {
      value: { items },
      writable: false,
    });
    return ev;
  }

  function fakeImageItem(mime, content = 'image-bytes') {
    return {
      kind: 'file',
      type: mime,
      getAsFile() {
        return new Blob([content], { type: mime });
      },
    };
  }

  it('paste of an image adds it to pending images', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.dispatchEvent(
      pasteEvent([fakeImageItem('image/png')]),
    );
    await settle(p);
    expect(p._pendingImages).toHaveLength(1);
    expect(p._pendingImages[0]).toMatch(/^data:image\/png;/);
  });

  it('paste of multiple images adds all of them', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.dispatchEvent(
      pasteEvent([
        fakeImageItem('image/png', 'first'),
        fakeImageItem('image/jpeg', 'second'),
      ]),
    );
    await settle(p);
    expect(p._pendingImages).toHaveLength(2);
  });

  it('text paste falls through (does not call preventDefault)', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    const ev = pasteEvent([
      { kind: 'string', type: 'text/plain' },
    ]);
    const preventSpy = vi.spyOn(ev, 'preventDefault');
    ta.dispatchEvent(ev);
    await settle(p);
    expect(preventSpy).not.toHaveBeenCalled();
    expect(p._pendingImages).toEqual([]);
  });

  it('image paste calls preventDefault', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    const ev = pasteEvent([fakeImageItem('image/png')]);
    const preventSpy = vi.spyOn(ev, 'preventDefault');
    ta.dispatchEvent(ev);
    await settle(p);
    expect(preventSpy).toHaveBeenCalled();
  });

  it('dedup: same image pasted twice only appears once', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.dispatchEvent(
      pasteEvent([fakeImageItem('image/png', 'SAME')]),
    );
    await settle(p);
    ta.dispatchEvent(
      pasteEvent([fakeImageItem('image/png', 'SAME')]),
    );
    await settle(p);
    expect(p._pendingImages).toHaveLength(1);
  });

  it('emits a warning toast when over the count limit', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      for (let i = 0; i < 6; i += 1) {
        ta.dispatchEvent(
          pasteEvent([
            fakeImageItem('image/png', `img-${i}`),
          ]),
        );
        await settle(p);
      }
      expect(p._pendingImages).toHaveLength(5);
      const warnings = toastListener.mock.calls
        .map((c) => c[0].detail)
        .filter((d) => d.type === 'warning');
      expect(warnings.length).toBeGreaterThan(0);
      expect(warnings[0].message).toMatch(/Maximum.*5/);
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });
});

describe('ChatPanel pending images', () => {
  it('renders thumbnail strip when non-empty', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    p._pendingImages = [
      'data:image/png;base64,AAA',
      'data:image/jpeg;base64,BBB',
    ];
    await settle(p);
    const thumbs = p.shadowRoot.querySelectorAll('.pending-image');
    expect(thumbs).toHaveLength(2);
    expect(thumbs[0].src).toContain('data:image/png');
    expect(thumbs[1].src).toContain('data:image/jpeg');
  });

  it('does not render strip when empty', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.pending-images'),
    ).toBeNull();
  });

  it('remove button removes the image', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    p._pendingImages = [
      'data:image/png;base64,A',
      'data:image/png;base64,B',
      'data:image/png;base64,C',
    ];
    await settle(p);
    const removeButtons = p.shadowRoot.querySelectorAll(
      '.pending-image-remove',
    );
    removeButtons[1].click();
    await settle(p);
    expect(p._pendingImages).toEqual([
      'data:image/png;base64,A',
      'data:image/png;base64,C',
    ]);
  });

  it('clicking thumbnail opens lightbox', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    p._pendingImages = ['data:image/png;base64,XYZ'];
    await settle(p);
    p.shadowRoot.querySelector('.pending-image').click();
    await settle(p);
    expect(p._lightboxImage).toBe('data:image/png;base64,XYZ');
  });
});

// ---------------------------------------------------------------------------
// Send with images
// ---------------------------------------------------------------------------

describe('ChatPanel send with images', () => {
  it('passes pending images to chat_streaming RPC', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'look at this';
    p._pendingImages = ['data:image/png;base64,PIC'];
    await p._send();
    await settle(p);
    // Images are the third positional argument since CC-21 dropped the
    // `files` list that used to sit between them and the message.
    const [, message, images, viewer] = started.mock.calls[0];
    expect(message).toBe('look at this');
    expect(images).toEqual(['data:image/png;base64,PIC']);
    expect(viewer).toBeNull();
  });

  it('clears pending images after send', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    p._pendingImages = ['data:image/png;base64,A'];
    await p._send();
    await settle(p);
    expect(p._pendingImages).toEqual([]);
  });

  it('optimistic user message carries images', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'see';
    p._pendingImages = ['data:image/png;base64,A'];
    await p._send();
    await settle(p);
    expect(p.messages).toHaveLength(1);
    expect(p.messages[0].role).toBe('user');
    expect(p.messages[0].images).toEqual([
      'data:image/png;base64,A',
    ]);
  });

  it('image-only send is allowed (empty text + image)', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = '';
    p._pendingImages = ['data:image/png;base64,A'];
    await p._send();
    await settle(p);
    expect(started).toHaveBeenCalledOnce();
    const [, message, images] = started.mock.calls[0];
    expect(message).toBe('');
    expect(images).toEqual(['data:image/png;base64,A']);
  });

  it('send button enabled with images even when text is empty', async () => {
    publishFakeRpc({});
    const p = mountPanel();
    await settle(p);
    let btn = p.shadowRoot.querySelector('.send-button');
    expect(btn.disabled).toBe(true);
    p._pendingImages = ['data:image/png;base64,A'];
    await settle(p);
    btn = p.shadowRoot.querySelector('.send-button');
    expect(btn.disabled).toBe(false);
  });

  it('image is not added to input history (only text)', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = '';
    p._pendingImages = ['data:image/png;base64,A'];
    await p._send();
    await settle(p);
    const history = p.shadowRoot.querySelector('aic-input-history');
    expect(history._entries).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Message image rendering
// ---------------------------------------------------------------------------

describe('ChatPanel message images', () => {
  it('renders thumbnails in user messages with images', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: 'see this',
          images: [
            'data:image/png;base64,A',
            'data:image/png;base64,B',
          ],
        },
      ],
    });
    await settle(p);
    const thumbs = p.shadowRoot.querySelectorAll('.message-image');
    expect(thumbs).toHaveLength(2);
  });

  it('does not render image section when images is empty', async () => {
    const p = mountPanel({
      messages: [
        { role: 'user', content: 'plain text message' },
      ],
    });
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.message-images'),
    ).toBeNull();
  });

  it('clicking a message thumbnail opens the lightbox', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: '',
          images: ['data:image/png;base64,XYZ'],
        },
      ],
    });
    await settle(p);
    p.shadowRoot.querySelector('.message-image').click();
    await settle(p);
    expect(p._lightboxImage).toBe('data:image/png;base64,XYZ');
  });

  it('re-attach button adds image to pending and emits toast', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: 'earlier',
          images: ['data:image/png;base64,REATTACH'],
        },
      ],
    });
    await settle(p);
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      p.shadowRoot
        .querySelector('.message-image-reattach')
        .click();
      await settle(p);
      expect(p._pendingImages).toEqual([
        'data:image/png;base64,REATTACH',
      ]);
      const successes = toastListener.mock.calls
        .map((c) => c[0].detail)
        .filter((d) => d.type === 'success');
      expect(successes.length).toBe(1);
      expect(successes[0].message).toContain('attached');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('re-attach of already-attached image emits neutral toast', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: '',
          images: ['data:image/png;base64,SAME'],
        },
      ],
    });
    await settle(p);
    p._pendingImages = ['data:image/png;base64,SAME'];
    await settle(p);
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      p.shadowRoot
        .querySelector('.message-image-reattach')
        .click();
      await settle(p);
      expect(p._pendingImages).toHaveLength(1);
      const infos = toastListener.mock.calls
        .map((c) => c[0].detail)
        .filter((d) => d.type === 'info');
      expect(infos.length).toBe(1);
      expect(infos[0].message).toContain('already attached');
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('re-attach click does not open the lightbox', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: '',
          images: ['data:image/png;base64,X'],
        },
      ],
    });
    await settle(p);
    p.shadowRoot
      .querySelector('.message-image-reattach')
      .click();
    await settle(p);
    expect(p._lightboxImage).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Lightbox
// ---------------------------------------------------------------------------

describe('ChatPanel lightbox', () => {
  it('renders when _lightboxImage is set', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    expect(
      p.shadowRoot.querySelector('.lightbox-backdrop'),
    ).toBeTruthy();
    expect(
      p.shadowRoot.querySelector('.lightbox-image').src,
    ).toContain('base64,X');
  });

  it('backdrop click closes the lightbox', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    const backdrop = p.shadowRoot.querySelector(
      '.lightbox-backdrop',
    );
    backdrop.click();
    await settle(p);
    expect(p._lightboxImage).toBeNull();
  });

  it('click on content does not close lightbox', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    p.shadowRoot.querySelector('.lightbox-content').click();
    await settle(p);
    expect(p._lightboxImage).toBe('data:image/png;base64,X');
  });

  it('Escape closes the lightbox', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    const backdrop = p.shadowRoot.querySelector(
      '.lightbox-backdrop',
    );
    backdrop.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
      }),
    );
    await settle(p);
    expect(p._lightboxImage).toBeNull();
  });

  it('Re-attach button attaches and closes', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    const toastListener = vi.fn();
    window.addEventListener('aic-toast', toastListener);
    try {
      const buttons = p.shadowRoot.querySelectorAll(
        '.lightbox-button',
      );
      const reattach = Array.from(buttons).find((b) =>
        b.textContent.includes('Re-attach'),
      );
      reattach.click();
      await settle(p);
      expect(p._pendingImages).toEqual([
        'data:image/png;base64,X',
      ]);
      expect(p._lightboxImage).toBeNull();
    } finally {
      window.removeEventListener('aic-toast', toastListener);
    }
  });

  it('Close button closes', async () => {
    const p = mountPanel();
    await settle(p);
    p._lightboxImage = 'data:image/png;base64,X';
    await settle(p);
    const buttons = p.shadowRoot.querySelectorAll(
      '.lightbox-button',
    );
    const close = Array.from(buttons).find((b) =>
      b.textContent.includes('Close'),
    );
    close.click();
    await settle(p);
    expect(p._lightboxImage).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Message action buttons
// ---------------------------------------------------------------------------

describe('ChatPanel message action buttons', () => {
  it('renders two toolbars (top and bottom) on each message', async () => {
    // Both ends — long messages may have either end in
    // view, so toolbars at both saves scrolling.
    const p = mountPanel({
      messages: [
        { role: 'user', content: 'hi' },
        { role: 'assistant', content: 'hello' },
      ],
    });
    await settle(p);
    const toolbars = p.shadowRoot.querySelectorAll(
      '.message-toolbar',
    );
    expect(toolbars.length).toBe(4);
    const tops = p.shadowRoot.querySelectorAll(
      '.message-toolbar.top',
    );
    const bottoms = p.shadowRoot.querySelectorAll(
      '.message-toolbar.bottom',
    );
    expect(tops.length).toBe(2);
    expect(bottoms.length).toBe(2);
  });

  it('each toolbar has copy and paste buttons', async () => {
    const p = mountPanel({
      messages: [{ role: 'user', content: 'hi' }],
    });
    await settle(p);
    const toolbar = p.shadowRoot.querySelector(
      '.message-toolbar.top',
    );
    const buttons = toolbar.querySelectorAll(
      '.message-action-button',
    );
    expect(buttons.length).toBe(2);
    const labels = Array.from(buttons).map((b) =>
      b.getAttribute('aria-label'),
    );
    expect(labels[0]).toMatch(/copy/i);
    expect(labels[1]).toMatch(/insert/i);
  });

  it('toolbar is NOT rendered on streaming message', async () => {
    const started = vi.fn().mockResolvedValue({ status: 'started' });
    publishFakeRpc({ 'ClaudeCodeService.chat_streaming': started });
    const p = mountPanel();
    await settle(p);
    p._input = 'hi';
    await p._send();
    const reqId = started.mock.calls[0][0];
    pushEvent('stream-chunk', {
      requestId: reqId,
      content: 'partial response',
    });
    await settle(p);
    const streamingCard = p.shadowRoot.querySelector(
      '.message-card.streaming',
    );
    expect(streamingCard).toBeTruthy();
    expect(
      streamingCard.querySelector('.message-toolbar'),
    ).toBeNull();
    const userCard = p.shadowRoot.querySelector(
      '.message-card.role-user',
    );
    expect(
      userCard.querySelector('.message-toolbar'),
    ).toBeTruthy();
  });

  it('system event messages get toolbars too', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: '**Committed** abc1234',
          system_event: true,
        },
      ],
    });
    await settle(p);
    const card = p.shadowRoot.querySelector(
      '.message-card.role-system',
    );
    expect(card.querySelector('.message-toolbar')).toBeTruthy();
  });
});

describe('ChatPanel copy action', () => {
  function installFakeClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    return {
      writeText,
      restore() {
        if (originalClipboard === undefined) {
          delete navigator.clipboard;
        } else {
          Object.defineProperty(navigator, 'clipboard', {
            value: originalClipboard,
            configurable: true,
          });
        }
      },
    };
  }

  it('copies raw string content, not rendered HTML', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const p = mountPanel({
        messages: [
          { role: 'assistant', content: 'use **bold** here' },
        ],
      });
      await settle(p);
      const copyBtn = p.shadowRoot
        .querySelector('.message-toolbar.top')
        .querySelectorAll('.message-action-button')[0];
      copyBtn.click();
      await settle(p);
      expect(writeText).toHaveBeenCalledOnce();
      expect(writeText).toHaveBeenCalledWith('use **bold** here');
    } finally {
      restore();
    }
  });

  it('emits success toast after copy', async () => {
    const { restore } = installFakeClipboard();
    try {
      const p = mountPanel({
        messages: [{ role: 'user', content: 'hi' }],
      });
      await settle(p);
      const toastListener = vi.fn();
      window.addEventListener('aic-toast', toastListener);
      try {
        p.shadowRoot
          .querySelector('.message-toolbar.top')
          .querySelectorAll('.message-action-button')[0]
          .click();
        await settle(p);
        const detail = toastListener.mock.calls.at(-1)[0].detail;
        expect(detail.type).toBe('success');
        expect(detail.message).toMatch(/copied/i);
      } finally {
        window.removeEventListener('aic-toast', toastListener);
      }
    } finally {
      restore();
    }
  });

  it('copies extracted text from multimodal content', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const p = mountPanel({
        messages: [
          {
            role: 'user',
            content: [
              { type: 'text', text: 'look at this' },
              {
                type: 'image_url',
                image_url: {
                  url: 'data:image/png;base64,XXX',
                },
              },
              { type: 'text', text: 'and this' },
            ],
          },
        ],
      });
      await settle(p);
      p.shadowRoot
        .querySelector('.message-toolbar.top')
        .querySelectorAll('.message-action-button')[0]
        .click();
      await settle(p);
      expect(writeText).toHaveBeenCalledWith('look at this\nand this');
    } finally {
      restore();
    }
  });

  it('does nothing for image-only messages', async () => {
    const { writeText, restore } = installFakeClipboard();
    try {
      const p = mountPanel({
        messages: [
          {
            role: 'user',
            content: [
              {
                type: 'image_url',
                image_url: {
                  url: 'data:image/png;base64,XXX',
                },
              },
            ],
          },
        ],
      });
      await settle(p);
      const toastListener = vi.fn();
      window.addEventListener('aic-toast', toastListener);
      try {
        p.shadowRoot
          .querySelector('.message-toolbar.top')
          .querySelectorAll('.message-action-button')[0]
          .click();
        await settle(p);
        expect(writeText).not.toHaveBeenCalled();
        expect(toastListener).not.toHaveBeenCalled();
      } finally {
        window.removeEventListener('aic-toast', toastListener);
      }
    } finally {
      restore();
    }
  });

  it('emits warning toast when clipboard API is unavailable', async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: undefined,
      configurable: true,
    });
    try {
      const p = mountPanel({
        messages: [{ role: 'user', content: 'hi' }],
      });
      await settle(p);
      const toastListener = vi.fn();
      window.addEventListener('aic-toast', toastListener);
      try {
        p.shadowRoot
          .querySelector('.message-toolbar.top')
          .querySelectorAll('.message-action-button')[0]
          .click();
        await settle(p);
        const detail = toastListener.mock.calls.at(-1)[0].detail;
        expect(detail.type).toBe('warning');
        expect(detail.message).toMatch(/not available/i);
      } finally {
        window.removeEventListener('aic-toast', toastListener);
      }
    } finally {
      if (originalClipboard === undefined) {
        delete navigator.clipboard;
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: originalClipboard,
          configurable: true,
        });
      }
    }
  });

  it('emits warning toast on clipboard rejection', async () => {
    const writeText = vi
      .fn()
      .mockRejectedValue(new Error('permission denied'));
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    try {
      const p = mountPanel({
        messages: [{ role: 'user', content: 'hi' }],
      });
      await settle(p);
      const toastListener = vi.fn();
      window.addEventListener('aic-toast', toastListener);
      try {
        p.shadowRoot
          .querySelector('.message-toolbar.top')
          .querySelectorAll('.message-action-button')[0]
          .click();
        await settle(p);
        const detail = toastListener.mock.calls.at(-1)[0].detail;
        expect(detail.type).toBe('warning');
        expect(detail.message).toMatch(/copy failed/i);
        expect(detail.message).toContain('permission denied');
      } finally {
        window.removeEventListener('aic-toast', toastListener);
      }
    } finally {
      if (originalClipboard === undefined) {
        delete navigator.clipboard;
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: originalClipboard,
          configurable: true,
        });
      }
    }
  });

  it('top and bottom toolbars both work', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    try {
      const p = mountPanel({
        messages: [{ role: 'user', content: 'echo' }],
      });
      await settle(p);
      p.shadowRoot
        .querySelector('.message-toolbar.top')
        .querySelectorAll('.message-action-button')[0]
        .click();
      await settle(p);
      p.shadowRoot
        .querySelector('.message-toolbar.bottom')
        .querySelectorAll('.message-action-button')[0]
        .click();
      await settle(p);
      expect(writeText).toHaveBeenCalledTimes(2);
      expect(writeText.mock.calls[0][0]).toBe('echo');
      expect(writeText.mock.calls[1][0]).toBe('echo');
    } finally {
      if (originalClipboard === undefined) {
        delete navigator.clipboard;
      } else {
        Object.defineProperty(navigator, 'clipboard', {
          value: originalClipboard,
          configurable: true,
        });
      }
    }
  });
});

describe('ChatPanel speak (text-to-speech) action', () => {
  // jsdom has no Web Speech synthesis API. Install a fake
  // queue + utterance constructor so the speaker button
  // renders and clicks drive deterministic behaviour.
  class FakeUtterance {
    constructor(text) {
      FakeUtterance.instances.push(this);
      this.text = text;
      this.lang = null;
      this.onend = null;
      this.onerror = null;
    }
  }
  FakeUtterance.instances = [];

  function installFakeSynth() {
    FakeUtterance.instances = [];
    const synth = {
      speakCalls: [],
      cancelCalls: 0,
      speak(u) {
        this.speakCalls.push(u);
      },
      cancel() {
        this.cancelCalls += 1;
      },
    };
    window.speechSynthesis = synth;
    window.SpeechSynthesisUtterance = FakeUtterance;
    return synth;
  }

  function uninstallFakeSynth() {
    delete window.speechSynthesis;
    delete window.SpeechSynthesisUtterance;
  }

  function speakButton(p) {
    // Third action button in the toolbar (after copy +
    // paste). Only present when synthesis is supported.
    const buttons = p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button');
    return buttons[2] || null;
  }

  it('renders the speaker button when synthesis is supported', async () => {
    installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'hello there' }],
      });
      await settle(p);
      const buttons = p.shadowRoot
        .querySelector('.message-toolbar.top')
        .querySelectorAll('.message-action-button');
      expect(buttons.length).toBe(3);
      expect(buttons[2].getAttribute('aria-label')).toMatch(/read/i);
    } finally {
      uninstallFakeSynth();
    }
  });

  it('hides the speaker button when synthesis is unsupported', async () => {
    uninstallFakeSynth();
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hello there' }],
    });
    await settle(p);
    const buttons = p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button');
    expect(buttons.length).toBe(2);
  });

  it('clicking speaks the rendered message text', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'use **bold** here' }],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      expect(synth.speakCalls).toHaveLength(1);
      // Rendered prose, not raw markdown — the asterisks
      // are gone.
      expect(synth.speakCalls[0].text).toBe('use bold here');
      expect(p._speakingMsgIndex).toBe(0);
    } finally {
      uninstallFakeSynth();
    }
  });

  it('button shows the stop state while speaking', async () => {
    installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'hello there' }],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      const btn = speakButton(p);
      expect(btn.classList.contains('speaking')).toBe(true);
      expect(btn.getAttribute('aria-pressed')).toBe('true');
      expect(btn.textContent.trim()).toBe('⏹');
    } finally {
      uninstallFakeSynth();
    }
  });

  it('clicking the active speaker stops playback', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'hello there' }],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(0);
      // Second click on the same speaker stops it.
      speakButton(p).click();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(-1);
      expect(synth.cancelCalls).toBeGreaterThan(0);
    } finally {
      uninstallFakeSynth();
    }
  });

  it('resets speaking state when the utterance ends', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'hello there' }],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(0);
      // Browser fires onend when playback finishes.
      synth.speakCalls[0].onend();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(-1);
    } finally {
      uninstallFakeSynth();
    }
  });

  it('reads the whole message when there is no selection', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [
          { role: 'assistant', content: 'first line\n\nsecond line' },
        ],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      expect(synth.speakCalls[0].text).toContain('first line');
      expect(synth.speakCalls[0].text).toContain('second line');
    } finally {
      uninstallFakeSynth();
    }
  });

  it('switching to another message stops the first', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [
          { role: 'assistant', content: 'first message' },
          { role: 'assistant', content: 'second message' },
        ],
      });
      await settle(p);
      const toolbars = p.shadowRoot.querySelectorAll(
        '.message-toolbar.top',
      );
      // Speak message 0.
      toolbars[0]
        .querySelectorAll('.message-action-button')[2]
        .click();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(0);
      // Speak message 1 — the new read cancels the old.
      toolbars[1]
        .querySelectorAll('.message-action-button')[2]
        .click();
      await settle(p);
      expect(p._speakingMsgIndex).toBe(1);
      expect(synth.speakCalls).toHaveLength(2);
      expect(synth.speakCalls[1].text).toBe('second message');
    } finally {
      uninstallFakeSynth();
    }
  });

  it('cancels speech on disconnect', async () => {
    const synth = installFakeSynth();
    try {
      const p = mountPanel({
        messages: [{ role: 'assistant', content: 'hello there' }],
      });
      await settle(p);
      speakButton(p).click();
      await settle(p);
      const before = synth.cancelCalls;
      p.remove();
      expect(synth.cancelCalls).toBeGreaterThan(before);
      expect(p._speakingMsgIndex).toBe(-1);
    } finally {
      uninstallFakeSynth();
    }
  });
});

describe('ChatPanel paste-to-prompt action', () => {
  it('inserts raw text into empty textarea', async () => {
    const p = mountPanel({
      messages: [
        { role: 'assistant', content: 'help me edit' },
      ],
    });
    await settle(p);
    const pasteBtn = p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1];
    pasteBtn.click();
    await settle(p);
    expect(p._input).toBe('help me edit');
  });

  it('inserts raw markdown source, not rendered HTML', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'assistant',
          content: 'say **bold** and `code`',
        },
      ],
    });
    await settle(p);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('say **bold** and `code`');
  });

  it('inserts at cursor position in non-empty textarea', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'INSERTED' }],
    });
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'before  after';
    ta.dispatchEvent(new Event('input'));
    await settle(p);
    ta.setSelectionRange(7, 7);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('before INSERTED after');
  });

  it('replaces selection when one exists', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'NEW' }],
    });
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'keep OLD keep';
    ta.dispatchEvent(new Event('input'));
    await settle(p);
    ta.setSelectionRange(5, 8);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('keep NEW keep');
  });

  it('focuses textarea after paste', async () => {
    publishFakeRpc({});
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'hi' }],
    });
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    const btn = p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1];
    btn.focus();
    btn.click();
    await settle(p);
    expect(p.shadowRoot.activeElement).toBe(ta);
  });

  it('positions cursor at end of inserted text', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'XYZ' }],
    });
    await settle(p);
    const ta = p.shadowRoot.querySelector('.input-textarea');
    ta.value = 'ab';
    ta.dispatchEvent(new Event('input'));
    await settle(p);
    ta.setSelectionRange(1, 1);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(ta.selectionStart).toBe(4);
    expect(ta.selectionEnd).toBe(4);
  });

  it('extracts text from multimodal content', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'part 1' },
            {
              type: 'image_url',
              image_url: {
                url: 'data:image/png;base64,Z',
              },
            },
            { type: 'text', text: 'part 2' },
          ],
        },
      ],
    });
    await settle(p);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('part 1\npart 2');
  });

  it('does nothing for image-only messages', async () => {
    const p = mountPanel({
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'image_url',
              image_url: {
                url: 'data:image/png;base64,Z',
              },
            },
          ],
        },
      ],
    });
    await settle(p);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('');
  });

  it('top and bottom paste buttons both work', async () => {
    const p = mountPanel({
      messages: [{ role: 'assistant', content: 'A' }],
    });
    await settle(p);
    p.shadowRoot
      .querySelector('.message-toolbar.top')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('A');
    p.shadowRoot
      .querySelector('.message-toolbar.bottom')
      .querySelectorAll('.message-action-button')[1]
      .click();
    await settle(p);
    expect(p._input).toBe('AA');
  });
});

describe('ChatPanel start-error wording', () => {
  // Found by running a live agy turn: the reply was
  // {error: "engine", message: "agy exited before sending its init frame"}
  // and the panel rendered **"Error: engine"** — a category the user
  // cannot act on, with the actionable half unread in the same payload.

  it('shows the message rather than the error code', async () => {
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': () => ({
        error: 'engine',
        phase: 'connect',
        message: 'agy exited before sending its init frame',
      }),
    });
    const panel = mountPanel();
    await settle(panel);
    panel._input = 'hi';
    await panel._send();
    await settle(panel);
    const text = panel.messages.map((m) => m.content).join(' ');
    expect(text).toContain('agy exited before sending its init frame');
    expect(text).not.toMatch(/Error:\s*engine\s*$/);
  });

  it('still shows the code when that is all there is', async () => {
    // Several refusals carry only `error`, and it is a sentence there.
    publishFakeRpc({
      'ClaudeCodeService.chat_streaming': () => ({
        error: 'The Claude Code engine is not connected.',
      }),
    });
    const panel = mountPanel();
    await settle(panel);
    panel._input = 'hi';
    await panel._send();
    await settle(panel);
    expect(panel.messages.map((m) => m.content).join(' ')).toContain(
      'engine is not connected',
    );
  });
});


describe('ChatPanel engine switch gate prompt', () => {
  // Asked at the moment of choosing rather than left to be discovered.
  // Without it the switch succeeds, the next prompt is refused with
  // `gate_not_installed`, and the user has to find a Settings panel they
  // had no reason to open.

  function withGate(gate, extra = {}) {
    publishFakeRpc({
      'Settings.get_agy_gate': () => gate,
      'ClaudeCodeService.switch_engine': () => ({ status: 'ok' }),
      ...extra,
    });
  }

  const ABSENT = {
    state: 'absent',
    path: '/home/u/.gemini/config/hooks.json',
    needed_by: 'agy',
  };

  afterEach(() => {
    delete window.confirm;
  });

  it('asks before writing outside the project, and says where', async () => {
    let asked = '';
    window.confirm = (m) => { asked = m; return false; };
    withGate(ABSENT);
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(asked).toContain('/home/u/.gemini/config/hooks.json');
    expect(asked).toContain('outside this project');
    expect(asked).toContain('0.03s');
    expect(asked).toContain('until you remove it in Settings');
  });

  it('does not switch when the user declines', async () => {
    // A selector that moved and then refused every turn would be the
    // confusing half of both options.
    const calls = [];
    window.confirm = () => false;
    withGate(ABSENT, {
      'ClaudeCodeService.switch_engine': (e) => { calls.push(e); return { status: 'ok' }; },
    });
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(calls).toEqual([]);
  });

  it('installs then switches when the user accepts', async () => {
    const calls = [];
    window.confirm = () => true;
    withGate(ABSENT, {
      'Settings.install_agy_gate': () => { calls.push('install'); return { state: 'current' }; },
      'ClaudeCodeService.switch_engine': (e) => { calls.push(`switch:${e}`); return { status: 'ok' }; },
    });
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(calls).toEqual(['install', 'switch:agy']);
  });

  it('asks nothing when the gate is already there', async () => {
    let asked = false;
    window.confirm = () => { asked = true; return true; };
    withGate({ ...ABSENT, state: 'current' });
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(asked).toBe(false);
  });

  it('asks nothing when switching to an engine that needs no gate', async () => {
    // `needed_by` is the server's, so the browser never hard-codes which
    // engine this belongs to — AG-R-4.
    let asked = false;
    window.confirm = () => { asked = true; return true; };
    withGate(ABSENT);
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'agy', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('claude');
    expect(asked).toBe(false);
  });

  it('switches anyway when the server has no such method', async () => {
    // Every engine but this transport answers "no such method", and a
    // switch that failed on that would break the ones that never needed a
    // gate at all.
    const calls = [];
    window.confirm = () => { throw new Error('must not ask'); };
    publishFakeRpc({
      'ClaudeCodeService.switch_engine': (e) => { calls.push(e); return { status: 'ok' }; },
    });
    const p = mountPanel();
    await settle(p);
    p._engines = { active: 'agy', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('claude');
    expect(calls).toEqual(['claude']);
  });
});


describe('ChatPanel engine switch failure is visible', () => {
  // The selector showed the engine the user picked while the session
  // stayed on the old one, and the only account of why lived inside a
  // notice they may have dismissed. A failed switch looked exactly like a
  // successful one.

  it('toasts the refusal rather than only recording it', async () => {
    publishFakeRpc({
      'ClaudeCodeService.switch_engine': () => ({
        error: 'A turn is still running',
        reason: 'turn_active',
      }),
    });
    const p = mountPanel();
    await settle(p);
    const toasts = [];
    p._emitToast = (m, kind) => toasts.push([m, kind]);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(p._engineSwitchError).toContain('A turn is still running');
    expect(toasts).toHaveLength(1);
    expect(toasts[0][0]).toContain('A turn is still running');
    expect(toasts[0][1]).toBe('error');
  });

  it('says nothing when the switch works', async () => {
    publishFakeRpc({
      'ClaudeCodeService.switch_engine': () => ({ engine: 'agy', changed: true }),
    });
    const p = mountPanel();
    await settle(p);
    const toasts = [];
    p._emitToast = (m) => toasts.push(m);
    p._engines = { active: 'claude', mountable: ['claude', 'agy'], labels: {} };
    await p._onSwitchEngine('agy');
    expect(toasts).toEqual([]);
  });
});
