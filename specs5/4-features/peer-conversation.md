# Peer Conversation

Two AIC⚡DC processes on the same machine, each in its own repository, let their agents talk. The agent
in one repository — the **caller** — asks the agent in the other — the **responder** — a question about
the responder's repository. The responder answers from its own repository and may ask questions back
on the same topic. The caller answers those in its next message, and the exchange goes on until either
user ends it.

Governing decision: [`../plan/decisions.md` § CC-23](../plan/decisions.md#cc-23). Wire shapes, file
formats and fixed strings are in the
[reference twin](../../specs-reference/4-features/peer-conversation.md).

## Scope

- **Same machine, same OS user.** Discovery is a directory under the config directory, and the
  transport is loopback. Nothing here binds a non-loopback interface, and collaboration mode does not
  change that.
- **Claude engine on both sides.** The caller's tool is an in-process SDK MCP tool, which has no
  client-side deadline. Under `agy` the same tool would meet the 180-second MCP wall
  ([`../plan-ag/decisions.md` § AG-24](../plan-ag/decisions.md)), and nothing in this design is
  shaped to survive it. An instance on another engine does not advertise itself and does not offer
  the tool.
- **One open conversation per process**, in either role. A process that is responding cannot also be
  calling, and the reverse.
- **No timeouts, anywhere** (user decision). A conversation ends only when a user ends it, the caller's
  agent ends it, or a process exits. The cost is stated under [Ending](#ending).

## Shape: the caller always asks

Every exchange is one request from the caller and one reply from the responder. The responder never
opens a connection to the caller. Its questions travel **inside its reply**, as a closing section of
the same prose, and the caller's agent answers them in its next message.

That is what keeps the design small. A responder that could call back would arrive at the caller while
the caller's turn is blocked inside the very tool call waiting for it. That means re-entrant turns, a
deadlock whenever both wait on each other, and permission prompts landing on whichever user happens to
be looking. Riding the reply has none of these, and costs nothing the user asked for: a question from
the responder is still answered, one round later.

## Discovery

Every instance on the Claude engine writes a **peer record** to `~/.config/aic-dc/peers/<pid>.json` at
startup and removes it at shutdown. The record names the repository (root and display name), the peer
listener's port, and a bearer token minted for this process. Mode `0600`, because the token is the
whole of the authentication.

A record whose pid is not alive is stale: readers ignore it, and the next instance to start sweeps it.
The liveness test is the one [`sweep_scratch`](../../src/aic_dc/claude_code/consultant.py) already
uses. Its own record, and records naming the same repository root as itself, are never offered to an
instance's agent as peers.

## The peer listener

A loopback HTTP listener of its own, separate from the consultation listener and from the browser's
WebSocket server.

- **Not the consultation listener**, because under the Claude engine that listener is never started:
  only the `agy` service starts it. Starting it here just to add a route would bring up two MCP servers
  that have no client under this engine.
- **Not the browser WebSocket**, because the first connection to that server is auto-admitted as
  collaboration host. A peer connecting first would become the host.
- **Plain JSON over HTTP, not MCP.** The caller side is our own in-process code, not a model's MCP
  client, so there is no protocol for MCP to match. It reuses the consultation listener's mechanics:
  an embedded uvicorn with signal capture suppressed, a pre-bound socket so the port exists before the
  record naming it is written, and a bearer gate compared with `hmac.compare_digest`.
- **Requests block with no deadline.** A message request returns when the responder's turn completes,
  however long that takes, including the time its user spends on the consent dialog. The caller's
  HTTP client is configured with no timeout.
- **A dropped connection is not an end.** Ending is an explicit request. The caller sends one whenever
  its side ends, and the responder never infers one from a socket closing. This is the consultation
  listener's lesson ([`consult_listener.py`](../../src/aic_dc/claude_code/consult_listener.py) module
  notes) applied to a protocol we own.

## The caller's tools

Three tools on their own in-process server, `aic-dc-peer`, kept apart from `aic-dc` because that
server's invariant is that all of its tools are read-only ([`../3-engine/mcp-bridge.md`](../3-engine/mcp-bridge.md)),
and these send this repository's content to another process.

| Tool | What it does |
|---|---|
| `list_peers` | The live peer records: display name, repository root. |
| `ask_peer(peer, message)` | Opens a conversation if none is open with that peer, or continues the open one. Returns the responder's reply as text. |
| `end_peer_conversation()` | Ends the open conversation. |

**The conversation id is held by the service, not the model.** There is at most one, so the model has
nothing to track and nothing to get wrong. `ask_peer` naming a different peer while a conversation is
open is refused with a message telling the agent to end the current one first.

**Ungated.** The `aic-dc-peer` tools are allowed in `can_use_tool` without a dialog, like `aic-dc`.
The gate is on the other side: nothing reaches the responder's repository until its user accepts.
The caller's user sees every call as a tool card and can stop it.

**What the tool result says besides the reply.** A fixed line naming the conversation's state. It says
the conversation is still open after a reply, and when it ends it says who ended it and that the agent
should not retry. As [`consult_listener.py`](../../src/aic_dc/claude_code/consult_listener.py)
measured for `BUDGET_SPENT`, it is the prose and not an error flag that stops a model retrying.

**Stopping the caller's turn while `ask_peer` is in flight ends the conversation.** The tool's
cancellation sends the end request before it returns. One rule for both sides, and the alternative
(stop this round, keep the conversation) would leave the responder locked behind a turn it interrupts
for a caller that has stopped waiting.

## The responder

### Consent

The first message of a conversation raises a **peer request** on the responder: a dialog naming the
caller's repository and showing the first message in full. **Allow** or **Deny**.

- Answerable by a localhost client only, for the reason of
  [CC-15](../plan/decisions.md#cc-15--permission-prompts-are-localhost-only): accepting hands a
  session to another agent.
- Waits indefinitely. The request is in `get_current_state`, so a browser that connects or reloads
  while it is pending is shown it. If no browser is connected the caller waits, and its tool card says
  what it is waiting for.
- Refused at once, without a dialog, when a turn is running or a conversation is already open. The
  refusal names the reason, so the caller's agent can tell its user rather than retry.
- The caller can withdraw a pending request by ending: the dialog closes and says why.

### Accepting starts a session

Allow does four things, in order:

1. Records the current session id and permission mode, so both can be named again at the end.
2. Starts a new session, as `new_session()` does.
3. Sets permission mode to `plan`.
4. Locks the session (below) and broadcasts that a peer conversation is open.

The previous session is not lost. It is in the history browser like any other, and the end-of-
conversation banner links to it.

### Each message is a turn

Each message from the caller becomes one turn of that session, through the same admission and pump as
a turn the user typed. The message is sent with a fixed header naming the caller's repository. The
first turn also gets a framing paragraph: you are answering another agent about this repository, the
repository is read-only for this conversation, cite paths and symbols, and close with any questions
you have for the caller on the same topic.

**The header is the record.** The transcript stores the header with the message, and the chat view
labels a user message as *from <peer>* by recognising the header. It does not use a live flag. A
reloaded browser, and the history browser a month later, must label it the same way the live view did.

**The reply is the turn's final text**, the `response` of its `streamComplete`. A turn that ends in error
is returned to the caller as an error, and the conversation stays open: the caller's agent may try
again or end it. A turn the responder's user stops ends the conversation.

### Read-only

`plan` mode is the first mechanism and not the only one, for the reason
[AG-25](../plan-ag/decisions.md#ag-25) gives about one control quietly not working. While a peer
conversation is open, `can_use_tool` **denies every request it receives, without a dialog**, with a
reason saying the repository is read-only for a peer conversation. That covers `ExitPlanMode`, which
would otherwise leave plan mode on the peer's say-so, and `AskUserQuestion`, which has no one to ask
— the reason tells the agent to put the question to the peer instead. The `aic-dc` index tools stay
ungated, by the same early return that ungates them now.

No dialog, because the user is locked out and the request comes from another agent's text. A dialog
here would ask the user to approve edits they did not ask for, in their own repository.

**A peer's message is sent verbatim**, and this is the control the two above do not cover. The CLI
expands an `@/path` in a prompt into that file's contents *itself*, before the model runs and so
before plan mode or `can_use_tool` is consulted. A message asking about `@~/.ssh/id_ed25519` would put
the key in the responder's context, and from there into the reply. Measured on 2026-10-14 against the
consultant's identical path: the file reached the wire with every other control in place. So
`_peer_turn` marks its turn `verbatim`, which sends it as a single message carrying
`client_composed: true` — no `@` expansion, no slash-command dispatch, for that message only. Not the
session-wide `verbatim_prompts`, which would take `@file` and `/command` from the user's own turns
after the conversation ends. The marker also skips the CLI's turn-start attachments (nested
`CLAUDE.md`, skill listings) for that turn, which is no loss to a read-only answer. Needs CLI 2.1.248+;
an older one ignores the field, and the expansion is back.

### The lock

While a conversation is open, the responder refuses, with reason `peer_conversation`: `chat_streaming`,
`new_session`, `resume_session`, `set_permission_mode`, model and engine changes, and commit. It is one
more check beside `_check_localhost_only` in the same methods.

**Stop and End are not locked.** Stop cancels the turn in flight and so ends the conversation. End
ends it whether or not a turn is running.

The user can still read everything: the transcript, the files, diffs, the context tab.

## Ending

Any of these ends a conversation, on both sides:

| Trigger | Where |
|---|---|
| End in the banner | Either side |
| Stop on the turn in flight | Either side; on the caller, only while `ask_peer` is in flight |
| `end_peer_conversation` | Caller's agent |
| Deny on the consent dialog | Responder |
| The other process exits | Either side, as below |

On the responder, ending cancels the turn in flight if there is one, answers any waiting request with
"ended by <side>", restores the recorded permission mode, releases the lock and broadcasts. The
session stays current: the user can keep talking to it, and their agent remembers the conversation.

**Process exit.** On a clean shutdown an instance sends an end request to its open peer, best-effort.
The caller notices a responder that died because its request fails, and treats that as an end. **A
caller killed hard leaves the responder locked until its user clicks End.** No timeout or liveness poll
releases it. That is the cost of having no timeouts, and the banner's End is the way out.

## Browser

**Caller.** A banner while a conversation is open, "Talking to <peer> — End". The `ask_peer` tool card
shows the message sent and the reply, and while waiting it says what it is waiting for: the peer user's
consent, or the peer's turn.

**Responder.** The consent dialog. While open, a banner "Peer conversation with <caller> — End" above
the transcript, and the composer disabled with that sentence as its placeholder. Peer messages render
as user messages labelled *from <caller>*, and each reply streams beneath it as an ordinary assistant
turn: no browser sent the message, so the panel adopts the turn the way a reconnect adopts a running
one. When it ends, the banner changes to say who ended it, with a
link back to the previous session, and the composer comes back.

## Invariants

- The responder never opens a connection to the caller.
- Nothing reaches the responder's engine before its user accepts.
- During a conversation the responder's `can_use_tool` allows only the `aic-dc` index tools, and shows
  no dialog.
- Ending is always explicit. No socket event, elapsed time or missing heartbeat ends a conversation.
- The peer label on a message is derived from the stored transcript, never from live state.
- Nothing binds a non-loopback interface.
- A peer's message reaches the engine verbatim: the CLI reads no file on its behalf.

## Not in scope

Cross-machine peers, which need TLS, real identity and the paused admission flow. The `agy` and
`antigravity` engines. More than one conversation at a time. The responder starting a conversation.
The responder's user editing or approving a reply before it goes back.
