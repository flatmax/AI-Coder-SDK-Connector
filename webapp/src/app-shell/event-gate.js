// The reconnect gate — specs5/plan-ag/risks.md § AG-R-20.
//
// `setupDone` runs on every connect, first or subsequent, and ends by asking
// the server for `get_current_state`. That is an RPC round-trip, and the
// socket is already live while it is in flight — so a connect that lands
// mid-turn takes live events *before* the baseline they are relative to:
//
//   1. the socket connects and the shell's handlers are reachable;
//   2. the shell asks for `get_current_state`;
//   3. the server, still mid-turn, pushes events 101 and 102, and the
//      reducers apply them at once;
//   4. the snapshot resolves — describing the world as of event 100 — and is
//      applied over the top.
//
// This module holds server-pushed events from connect until the snapshot has
// landed, then replays them in arrival order. It is worth closing now rather
// than when it bites: the race is rare today because reconnects are rare, and
// AG-R-19's overflow policy makes rehydration the *routine* answer to a slow
// client, which turns a rare race into a regular one.

/**
 * Every method the server pushes to, and therefore everything the gate holds.
 *
 * Written out rather than derived by reflection, because reflection over the
 * prototype cannot tell a server-pushed event from `render`, `updated` or
 * `firstUpdated` — which Lit calls locally and which must never be deferred.
 * The list being hand-written is what makes `event-gate.test.js`'s coverage
 * test necessary: it fails when a new handler is added to the shell and not
 * classified here, so the bypass is loud rather than silent.
 *
 * Uniform on purpose. Deciding per event which ones "really" race the
 * snapshot is the judgement that goes stale — the rule is simply that
 * anything the server pushes waits for the baseline.
 */
export const GATED_EVENTS = Object.freeze([
  'startupProgress',
  'streamChunk',
  'streamComplete',
  'turnUsage',
  'compactionEvent',
  'postResponseComplete',
  'filesModified',
  'userMessage',
  'userMessageImages',
  'commitResult',
  'modeChanged',
  'sessionChanged',
  'sessionDeleted',
  'reviewStarted',
  'reviewEnded',
  'navigateFile',
  'docConvertProgress',
  'admissionRequest',
  'admissionResult',
  'clientJoined',
  'clientLeft',
  'roleChanged',
  'permissionRequest',
  'permissionDeadline',
  'permissionResolved',
  'permissionModeChanged',
  'modelChanged',
  'engineChanged',
  'toolUse',
  'toolResult',
  'thinkingChunk',
  'sessionStarted',
  'subagentEvent',
  'hookEvent',
  'rateLimit',
  'engineHealth',
  'systemEvent',
]);

/**
 * The shell's own methods that are deliberately *not* gated, used by the
 * coverage test to classify the prototype exhaustively. Connection lifecycle
 * and Lit rendering: the first drives the gate, and the second is called by
 * Lit rather than by the server.
 */
export const UNGATED_METHODS = Object.freeze([
  'constructor',
  'connectedCallback',
  'disconnectedCallback',
  'setupDone',
  'remoteDisconnected',
  'setupSkip',
  'render',
  'firstUpdated',
  'updated',
]);

/**
 * How many events the gate will hold before giving up and releasing.
 *
 * Rehydration is a single round-trip to a server on localhost, so a healthy
 * gate holds single digits. This bound is far above that and far below
 * anything that costs memory; reaching it means the snapshot is not coming.
 */
export const GATE_MAX_EVENTS = 500;

/**
 * How long the gate will hold before giving up and releasing.
 *
 * `fetchCurrentState` has no client-side deadline of its own — unlike
 * `fetchContextUsage`, it does not go through `withRpcTimeout` — so nothing
 * else bounds this. Five seconds is far longer than a localhost snapshot and
 * short enough that a user never watches a dead UI wondering.
 */
export const GATE_MAX_MS = 5000;

/**
 * Install the gate over `host`'s server-push handlers.
 *
 * The wrappers are installed as **own properties shadowing the prototype
 * methods**, which is what makes this a single edit rather than 37. jrpc-oo's
 * `ExposeClass` resolves `classToExpose[name]` *per call*, so an own property
 * wins every dispatch; and it enumerates the *prototype* when deciding what to
 * expose, so the RPC surface is unchanged by anything here.
 *
 * Installed **open**, and held by `setupDone` immediately before it asks for
 * the snapshot. Holding from installation instead was tried and is wrong: the
 * race this closes is between a snapshot request and the events that overtake
 * it, so until a request has been made there is no baseline for anything to
 * race. A gate armed at construction would also hold — and then warn — on a
 * page that never manages to connect at all.
 */
export function installEventGate(host, names = GATED_EVENTS) {
  host._gate = { held: false, buffer: [], timer: null };

  for (const name of names) {
    const original = host[name];
    if (typeof original !== 'function') {
      // Loud, because the alternative is a handler that silently stops being
      // gated when it is renamed.
      throw new Error(`installEventGate: AcApp has no method "${name}"`);
    }
    Object.defineProperty(host, name, {
      configurable: true,
      writable: true,
      value: function gated(...args) {
        const gate = host._gate;
        if (!gate || !gate.held) return original.apply(host, args);

        if (gate.buffer.length >= GATE_MAX_EVENTS) {
          // Release rather than drop. A dropped event is a permanently
          // missing row that nothing reports, which is the failure class this
          // whole register keeps hitting; releasing early is no worse than the
          // behaviour before this gate existed.
          console.warn(
            `[event-gate] held ${gate.buffer.length} events without a ` +
            'snapshot; releasing early. The reconnect baseline may be stale.',
          );
          releaseEvents(host);
          return original.apply(host, args);
        }

        gate.buffer.push([name, original, args]);
        // The server is told the push was accepted, because it was: the
        // handlers it calls all return a constant, and the value is discarded
        // at the far end anyway (AG-R-19 § *What the turn is actually waiting
        // for*).
        return true;
      },
    });
  }

  return host._gate;
}

/** True while the gate is buffering. Exported for tests and for logging. */
export function isHolding(host) {
  return Boolean(host._gate && host._gate.held);
}

/** How many events are waiting. Exported for tests. */
export function heldCount(host) {
  return host._gate ? host._gate.buffer.length : 0;
}

/**
 * Start holding. Idempotent, so a second `setupDone` cannot lose a buffer that
 * is already filling, and the timer is not restarted under an event storm.
 */
export function holdEvents(host) {
  const gate = host._gate;
  if (!gate || gate.held) return;
  gate.held = true;
  gate.timer = setTimeout(() => {
    console.warn(
      `[event-gate] no snapshot within ${GATE_MAX_MS}ms; releasing ` +
      `${gate.buffer.length} held event(s).`,
    );
    releaseEvents(host);
  }, GATE_MAX_MS);
}

/**
 * Stop holding and replay what was held, in arrival order.
 *
 * Each replayed handler is isolated: one that throws must not strand the
 * events behind it, which is the same rule the engine side applies to its own
 * sinks — no single consumer is allowed to be load-bearing.
 */
export function releaseEvents(host) {
  const gate = host._gate;
  if (!gate) return;

  if (gate.timer !== null) {
    clearTimeout(gate.timer);
    gate.timer = null;
  }
  gate.held = false;

  // Taken before replaying, so a handler that somehow re-enters the gate
  // starts from an empty buffer rather than replaying its own tail.
  const buffered = gate.buffer;
  gate.buffer = [];

  for (const [name, fn, args] of buffered) {
    try {
      fn.apply(host, args);
    } catch (err) {
      console.error(`[event-gate] replaying ${name} failed:`, err);
    }
  }
}

/**
 * Drop the gate without replaying, for teardown.
 *
 * Deliberately not `releaseEvents`: replaying into a component that is being
 * disconnected would run reducers against a tree that is going away. A
 * disconnected shell has no state worth correcting.
 */
export function disposeEventGate(host) {
  const gate = host._gate;
  if (!gate) return;
  if (gate.timer !== null) {
    clearTimeout(gate.timer);
    gate.timer = null;
  }
  gate.held = false;
  gate.buffer = [];
}
