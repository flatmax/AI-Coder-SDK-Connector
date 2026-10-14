# Reference: Peer Conversation

**Supplements:** `specs5/4-features/peer-conversation.md`

Canonical owner for the peer record format, the peer listener's endpoints, and the fixed strings
both sides depend on.

## Peer record

`~/.config/aic-dc/peers/<pid>.json`, mode `0600`, written atomically (temp file + `os.replace`).

```json
{
  "version": 1,
  "pid": 12345,
  "repo_root": "/home/user/repo2",
  "repo_name": "repo2",
  "port": 41873,
  "token": "<secrets.token_urlsafe(32)>",
  "started_at": "2026-10-14T09:12:03Z"
}
```

`repo_name` is the repository root's basename. When two live records share a basename, `list_peers`
shows the root alongside it, and `ask_peer(peer=...)` accepts either the name or the root. An
ambiguous name is refused, and the refusal lists the roots.

## Endpoints

Host `127.0.0.1`. Every request carries `Authorization: Bearer <token>`, the token from the
**responder's** record. A missing or wrong token gets `401` with body `Unauthorized.` and nothing else.

### `POST /peer/message`

Opens a conversation (`conversation_id` absent) or continues one.

```json
{
  "conversation_id": null,
  "caller": {"repo_name": "repo1", "repo_root": "/home/user/repo1", "pid": 23456},
  "message": "..."
}
```

Blocks until the responder's turn completes. Responses:

| Status | Body | Meaning |
|---|---|---|
| `200` | `{"conversation_id": "<hex>", "reply": "..."}` | The turn's `streamComplete.response` |
| `409` | `{"reason": "busy" \| "conversation_open" \| "wrong_conversation"}` | Refused without a dialog |
| `403` | `{"reason": "denied"}` | The responder's user clicked Deny |
| `410` | `{"reason": "ended", "by": "responder" \| "caller"}` | Ended before or during this message |
| `502` | `{"reason": "turn_error", "detail": "..."}` | The turn ended in error; conversation still open |

### `POST /peer/end`

```json
{"conversation_id": "<hex or null>", "caller_pid": 23456}
```

`conversation_id` is null when withdrawing a request still at the consent dialog. The pid identifies
which pending request. Always `200`, and idempotent: ending something already ended is not an error,
because both sides may end at once.

## Fixed strings

**Message header**, the first line of every peer message as stored in the transcript. The chat view's
peer label matches this line and nothing else:

```
[aic-dc peer message from <repo_name> (<repo_root>)]
```

**First-turn framing**, after the header and before the caller's message on the first turn only:

> You are answering another AI agent, which is working in the repository named above, about this
> repository. This repository is read-only for the whole conversation: no edits, no commands. Answer
> from what you read here and cite paths and symbols. Close your reply with a section headed
> "Questions for you:" listing anything you want the other agent to answer about its own repository
> on this topic, or "Questions for you: none".

**Deny reason** returned by `can_use_tool` during a conversation:

> This session is answering a peer conversation and the repository is read-only for its duration.
> Answer from what you can read. If you need something from the user, put the question to the peer
> instead.

**Tool-result footer**, appended by the caller's `ask_peer`:

- open: `[Peer conversation with <peer> is open. Reply with ask_peer, or end it with end_peer_conversation.]`
- ended: `[Peer conversation with <peer> was ended by <who>. Do not retry; tell the user.]`
