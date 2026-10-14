// Streaming gate shared by the git controls.
//
// The commit and reset buttons stay locked while a turn streams, in the app
// shell's header and in the file picker alike. "A turn streams" used to mean
// "a chunk arrived and no completion since", which held while one request
// streamed at a time. It no longer does: a turn whose background subagents
// outlive its result keeps streaming their work after the next turn starts,
// so the gate tracks request ids and opens only when none remain.

/**
 * Record a streaming request on a gate host and raise
 * `_streaming`.
 */
export function noteStreamChunk(host, event) {
  const requestId = event?.detail?.requestId;
  if (!host._streamingRequests) host._streamingRequests = new Set();
  if (requestId) host._streamingRequests.add(requestId);
  if (!host._streaming) host._streaming = true;
}

/**
 * Release a request on a gate host; `_streaming` drops
 * once none remain. A completion naming no request
 * cannot say which one ended, so it releases them all —
 * the gate's old behaviour, and the safe side of a stuck
 * lock.
 */
export function noteStreamComplete(host, event) {
  const requestId = event?.detail?.requestId;
  const open = host._streamingRequests;
  if (requestId && open) {
    open.delete(requestId);
  } else {
    open?.clear();
  }
  host._streaming = (open?.size ?? 0) > 0;
}
