// Tests for the browser half of peer conversations (CC-23).
//
// The header parser is pure and tested alone. Everything else goes through a
// mounted ChatPanel, because what is drawn is a function of `_peer`, and
// `_peer` is written by two routes — the `peer-changed` push and the
// `state-loaded` snapshot — which must agree.

import { describe, expect, it, vi } from 'vitest';

import { peerBodyOf, peerHeaderOf } from './peer-conversation.js';
import {
  mountPanel,
  publishFakeRpc,
  pushEvent,
  settle,
} from './test-helpers.js';

const HEADER = '[aic-dc peer message from repo1 (/home/u/repo1)]';

function responder(status, over = {}) {
  return {
    role: 'responder',
    status,
    peer: { repo_name: 'repo1', repo_root: '/home/u/repo1' },
    conversation_id: 'c1',
    first_message: status === 'pending' ? 'How do you index symbols?' : null,
    previous_session_id: 'old-session',
    ...over,
  };
}

async function peerState(panel, state) {
  pushEvent('peer-changed', { state });
  await settle(panel);
}

function q(panel, selector) {
  return panel.shadowRoot.querySelector(selector);
}

describe('peerHeaderOf', () => {
  it('reads the name and root from the first line', () => {
    expect(peerHeaderOf(`${HEADER}\n\nhello`)).toEqual({
      repoName: 'repo1', repoRoot: '/home/u/repo1',
    });
  });

  it('keeps parentheses inside the root', () => {
    const text = '[aic-dc peer message from a (/x/a (copy))]\nhi';
    expect(peerHeaderOf(text)?.repoRoot).toBe('/x/a (copy)');
  });

  it('only the first line counts', () => {
    expect(peerHeaderOf(`hello\n${HEADER}`)).toBeNull();
    expect(peerHeaderOf('plain message')).toBeNull();
    expect(peerHeaderOf(null)).toBeNull();
  });

  it('the body drops the header and nothing else', () => {
    expect(peerBodyOf(`${HEADER}\n\nhello\nworld`)).toBe('hello\nworld');
    expect(peerBodyOf('typed by a person')).toBe('typed by a person');
  });
});

describe('the responder', () => {
  it('shows the consent card with the first message, and answers it', async () => {
    const answer = vi.fn().mockResolvedValue({ status: 'allowed' });
    publishFakeRpc({ 'ClaudeCodeService.peer_answer': answer });
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    await peerState(panel, responder('pending'));

    const card = q(panel, '.peer-consent');
    expect(card).not.toBeNull();
    expect(card.textContent).toContain('repo1');
    expect(card.textContent).toContain('How do you index symbols?');
    q(panel, '.peer-allow').click();
    expect(answer).toHaveBeenCalledWith(true);
  });

  it('Deny answers false', async () => {
    const answer = vi.fn().mockResolvedValue({ status: 'denied' });
    publishFakeRpc({ 'ClaudeCodeService.peer_answer': answer });
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    await peerState(panel, responder('pending'));
    q(panel, '.peer-deny').click();
    expect(answer).toHaveBeenCalledWith(false);
  });

  it('locks the composer while open, and End ends it', async () => {
    const end = vi.fn().mockResolvedValue({ status: 'ended' });
    publishFakeRpc({ 'ClaudeCodeService.peer_end': end });
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    await peerState(panel, responder('open'));

    const textarea = q(panel, '.input-textarea');
    expect(textarea.disabled).toBe(true);
    expect(textarea.placeholder).toContain('Peer conversation with repo1');
    expect(q(panel, '.new-session-button').disabled).toBe(true);
    expect(q(panel, '.peer-banner').textContent).toContain('repo1');
    q(panel, '.peer-end').click();
    expect(end).toHaveBeenCalled();
  });

  it('gives the composer back when it ends, with a way back', async () => {
    const resume = vi.fn().mockResolvedValue({ status: 'resumed' });
    const dismiss = vi.fn().mockResolvedValue({ status: 'dismissed' });
    publishFakeRpc({
      'ClaudeCodeService.resume_session': resume,
      'ClaudeCodeService.peer_dismiss': dismiss,
    });
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    await peerState(panel, responder('open'));
    await peerState(panel, {
      role: 'responder',
      status: 'ended',
      peer: { repo_name: 'repo1', repo_root: '/home/u/repo1' },
      ended_by: 'caller',
      previous_session_id: 'old-session',
    });

    expect(q(panel, '.input-textarea').disabled).toBe(false);
    expect(q(panel, '.peer-ended').textContent).toMatch(/ended by\s+repo1/);
    q(panel, '.peer-previous').click();
    await settle(panel);
    expect(resume).toHaveBeenCalledWith('old-session', false);
    expect(dismiss).toHaveBeenCalled();
  });

  it('a reload mid-conversation draws the same thing from the snapshot', async () => {
    publishFakeRpc({});
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    pushEvent('state-loaded', { messages: [], peer: responder('open') });
    await settle(panel);
    expect(q(panel, '.input-textarea').disabled).toBe(true);

    pushEvent('state-loaded', { messages: [], peer: null });
    await settle(panel);
    expect(q(panel, '.peer-banner')).toBeNull();
  });

  it('shows its own reply to a peer message as it streams', async () => {
    publishFakeRpc({});
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    await peerState(panel, responder('open'));

    pushEvent('user-message', {
      content: `${HEADER}\n\nWhat is in your repo?`,
      request_id: 'peer-1',
    });
    pushEvent('stream-chunk', {
      requestId: 'peer-1',
      chunk: { block_id: 'b1', seq: 1, content: 'Just hello.cpp.', done: true },
    });
    await settle(panel);
    expect(panel.shadowRoot.querySelector('.message-card.streaming')).not.toBeNull();

    pushEvent('stream-complete', {
      requestId: 'peer-1',
      result: { response: 'Just hello.cpp.' },
    });
    await settle(panel);
    expect(panel.messages.map((m) => m.role)).toEqual(['user', 'assistant']);
    expect(panel.messages[1].content).toBe('Just hello.cpp.');
    expect(panel._streaming).toBe(false);
  });

  it('labels a peer message from its header, and hides the header', async () => {
    publishFakeRpc({});
    const panel = mountPanel({ rpcConnected: true });
    panel.messages = [
      { role: 'user', content: `${HEADER}\n\nWhere is the parser?` },
      { role: 'user', content: 'typed by me' },
    ];
    await settle(panel);
    const cards = panel.shadowRoot.querySelectorAll('.message-card.role-user');
    expect(cards[0].classList.contains('from-peer')).toBe(true);
    expect(cards[0].querySelector('.role-label').textContent).toContain('From repo1');
    expect(cards[0].textContent).not.toContain('[aic-dc peer message');
    expect(cards[0].textContent).toContain('Where is the parser?');
    expect(cards[1].querySelector('.role-label').textContent).toContain('You');
  });
});

describe('the caller', () => {
  it('says what it is waiting for and does not lock the composer', async () => {
    const end = vi.fn().mockResolvedValue({ status: 'ended' });
    publishFakeRpc({ 'ClaudeCodeService.peer_end': end });
    const panel = mountPanel({ rpcConnected: true });
    await settle(panel);
    const caller = (status) => ({
      role: 'caller',
      status,
      peer: { repo_name: 'repo2', repo_root: '/home/u/repo2', pid: 2 },
      conversation_id: status === 'opening' ? null : 'c1',
    });

    await peerState(panel, caller('opening'));
    expect(q(panel, '.peer-banner').textContent).toContain('allow the conversation');
    await peerState(panel, caller('waiting'));
    expect(q(panel, '.peer-banner').textContent).toContain('waiting for its reply');
    expect(q(panel, '.input-textarea').disabled).toBe(false);
    q(panel, '.peer-end').click();
    expect(end).toHaveBeenCalled();
  });
});
