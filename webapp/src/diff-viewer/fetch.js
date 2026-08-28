// Repo content fetching + RPC envelope unwrapping.
//
// All filesystem-or-VCS-backed reads for diff-viewer go
// through here. Virtual paths short-circuit; everything
// else hits Repo.get_file_content twice (HEAD + working
// copy). Errors on *one* side are tolerated — a missing
// HEAD means new file, a missing working copy means
// deleted (we still render whatever side we got).
//
// Errors on *both* sides are not, and that distinction is
// the whole of `specs5/next.md` § C2's browser half. When
// one call fails the other one supplies a real reading, so
// the failure is information rather than a fault. When both
// fail nothing was read at all, and the viewer used to paint
// that as an empty file marked new — a document the user
// could scroll, select and be misled by, over a request the
// server had refused. The shape is the discriminator, so
// there is no message to sniff: two failures mean no answer.

import { SharedRpc } from '../rpc.js';

import { _VIRTUAL_PREFIX, isAbsoluteUrl } from './constants.js';

/**
 * Look up the SharedRpc call proxy. Returns null when
 * the proxy isn't published (pre-connection, or in
 * tests that don't bother with RPC). An optional
 * `__sharedRpcOverride` on globalThis lets tests
 * inject a proxy without touching the singleton.
 */
export function getRpcCall() {
  try {
    const shared = globalThis.__sharedRpcOverride;
    if (shared) return shared;
  } catch (_) {}
  try {
    return SharedRpc.call || null;
  } catch (_) {
    return null;
  }
}

/**
 * Extract content from a Repo.get_file_content RPC
 * response. The RPC may return a plain string or an
 * object with a `content` field; handle both. jrpc-oo
 * envelopes (single-key wrapping) are unwrapped
 * recursively.
 */
export function extractRpcContent(result) {
  if (typeof result === 'string') return result;
  if (
    result &&
    typeof result === 'object' &&
    typeof result.content === 'string'
  ) {
    return result.content;
  }
  if (result && typeof result === 'object') {
    const keys = Object.keys(result);
    if (keys.length === 1) {
      return extractRpcContent(result[keys[0]]);
    }
  }
  return '';
}

/**
 * Extract the data URI from a Repo.get_file_base64
 * response. Same shape variants as extractRpcContent —
 * plain string, `{data_uri}`, `{content}`, or single-key
 * envelope.
 */
export function extractBase64Uri(result) {
  if (typeof result === 'string') return result;
  if (result && typeof result === 'object') {
    if (typeof result.data_uri === 'string') return result.data_uri;
    if (typeof result.content === 'string') return result.content;
    const keys = Object.keys(result);
    if (keys.length === 1) {
      return extractBase64Uri(result[keys[0]]);
    }
  }
  return '';
}

/**
 * Unwrap a jrpc-oo envelope. jrpc-oo returns responses
 * wrapped as `{uuid: payload}` — a single key whose
 * value is the real payload. But in tests that inject
 * a direct-call fake proxy, the RPC function returns
 * the payload directly (no wrapping). We distinguish
 * by inspecting the inner value's shape: if the single
 * key's value is itself a non-array object, treat it as
 * an envelope and unwrap. Otherwise the outer object IS
 * the payload (e.g. `{available: true}` or `{html: "..."}`
 * are payloads, not envelopes).
 */
export function unwrapRpc(result) {
  if (!result || typeof result !== 'object') return result;
  if (Array.isArray(result)) return result;
  const keys = Object.keys(result);
  if (keys.length !== 1) return result;
  const inner = result[keys[0]];
  if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
    return inner;
  }
  return result;
}

/**
 * The reportable text of a rejected RPC call.
 *
 * jrpc-oo rejects with whatever the server put in the JSON-RPC
 * error field, which for a raised exception is `str(e)` — so the
 * useful sentence is already there and only needs unwrapping from
 * however the client boxed it.
 */
export function rpcErrorMessage(err) {
  if (typeof err === 'string' && err) return err;
  const message = err?.message;
  if (typeof message === 'string' && message) return message;
  return 'unknown error';
}

/**
 * Fetch HEAD and working copy content via Repo RPCs.
 * Returns {original, modified, isNew, error}. Each fetch is
 * wrapped in its own try/catch so a missing HEAD
 * (new file) doesn't prevent the working copy from
 * loading.
 *
 * `error` is a string only when *both* fetches failed, which
 * means the file could not be read at all and there is nothing
 * honest to display; it is null whenever either side answered.
 * The message reported is the working copy's, because that is
 * the file the caller asked for — the HEAD side's is git's
 * stderr about a ref, which is a different question.
 *
 * No RPC published is not an error in this sense. It is the
 * pre-connection state of a freshly loaded page, and the health
 * banner is what says so; a toast per open would be a second
 * voice on a condition already on screen.
 *
 * Virtual paths short-circuit — their content is passed
 * through openFile's options and never touches disk.
 */
export async function fetchFileContent(path) {
  if (typeof path === 'string' && path.startsWith(_VIRTUAL_PREFIX)) {
    return { original: '', modified: '', isNew: false, error: null };
  }
  const call = getRpcCall();
  if (!call) {
    return { original: '', modified: '', isNew: false, error: null };
  }
  let original = '';
  let modified = '';
  let isNew = false;
  let headFailed = false;
  let workingError = null;
  try {
    const headResult = await call['Repo.get_file_content'](
      path,
      'HEAD',
    );
    original = extractRpcContent(headResult);
  } catch (_) {
    isNew = true;
    headFailed = true;
  }
  try {
    const workingResult = await call['Repo.get_file_content'](path);
    modified = extractRpcContent(workingResult);
  } catch (err) {
    // Working copy missing — deleted file, or a refusal the
    // caller has to hear about if HEAD failed too.
    workingError = err;
  }
  if (headFailed && workingError !== null) {
    // `isNew` above was a guess made from HEAD alone, and with no
    // working copy behind it there is nothing for it to describe.
    return {
      original: '',
      modified: '',
      isNew: false,
      error: rpcErrorMessage(workingError),
    };
  }
  return { original, modified, isNew, error: null };
}

// Re-export so importers don't have to pull from two
// places when they're already on the fetch boundary.
export { isAbsoluteUrl };