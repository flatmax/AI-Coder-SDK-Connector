// Image pointers — the shared half of reading images out of a transcript.
//
// `history_load` renders a prompt's image blocks as pointers rather than
// bytes: `{session_id, entry_uuid, block, media_type}`. Entries hold pasted
// images verbatim as base64 (specs5/4-features/images.md — the transcript
// *is* the storage), so a load that inlined them would push megabytes at
// every client on every open and again on every reconnect.
//
// Two components read those pointers: the history browser, previewing a
// session the user is deciding whether to resume, and the chat panel,
// rendering a session that was resumed or restored after a reconnect. They
// draw different tiles — the browser's are 60px and inert, the panel's are
// 80px with a lightbox and a re-attach button — but the fetching is the same
// problem, and the part that is easy to get subtly wrong is the part that is
// shared here.
//
// Governing spec: specs5/4-features/images.md § Reading Flow;
// specs5/5-webapp/chat.md § History Browser.

import { withRpcTimeout } from './rpc.js';

/**
 * Deadline for one `history_image` call. Same value as the other history
 * reads for the same reason: this is disk I/O on the server's executor, so
 * reaching 30s means no reply is coming rather than that the read is slow.
 * Without it a pointer whose reply was dropped is a tile that spins for the
 * life of the session.
 */
export const IMAGE_TIMEOUT_MS = 30000;

/**
 * The image pointers on a rendered message, always an array.
 *
 * A pointer is `{session_id, entry_uuid, block, media_type}` — what
 * `history_load` renders in place of an image block's base64.
 */
export function imageRefsOf(msg) {
  if (!msg || !Array.isArray(msg.image_refs)) return [];
  return msg.image_refs.filter((ref) => ref && typeof ref === 'object');
}

/**
 * Cache key for one image pointer, or '' if it names nothing fetchable.
 * Keyed on the pointer's own three fields rather than on the session being
 * viewed, so the cache survives switching away and back.
 */
export function imageRefKey(ref) {
  if (!ref || !ref.session_id || !ref.entry_uuid) return '';
  return `${ref.session_id}|${ref.entry_uuid}|${ref.block ?? 0}`;
}

/**
 * Resolve every unresolved pointer in `messages` to bytes, into `cache`.
 *
 * Sequentially, and not for ordering — a session that pasted twenty
 * screenshots would otherwise open twenty concurrent RPCs at a backend whose
 * reads are disk-bound anyway. Tiles fill in as each one lands, and the
 * caller is expected to have drawn them at their final size first so the
 * transcript does not reflow image by image.
 *
 * `isStale()` is re-checked before every fetch rather than once at entry:
 * the loop awaits, and during the await the user is free to select another
 * session or resume a different one, at which point the rest of this work
 * belongs to nobody. Cached entries — failures included — are never
 * refetched: a missing image is missing every time it is looked at, and
 * retrying on each reselect is a stall the user cannot do anything about.
 *
 * Not awaited by its callers. Resolving pointers must not gate the
 * transcript; the text is readable while the bytes are still arriving.
 *
 * @param host   an RpcMixin element — `rpcConnected`, `rpcExtract`,
 *               `requestUpdate`. A Map is not a reactive property, so the
 *               tile waiting on an entry only redraws when we say so.
 * @param cache  Map of `imageRefKey` → `{dataUri}` or `{error}`
 * @param isStale () => boolean, true once this work has been superseded
 * @param label  log prefix, so a failure names the component that asked
 */
export async function hydrateImageRefs(
  host,
  messages,
  { cache, isStale, label },
) {
  if (!host.rpcConnected) return;
  for (const msg of messages) {
    for (const ref of imageRefsOf(msg)) {
      if (isStale()) return;
      const key = imageRefKey(ref);
      if (!key || cache.has(key)) continue;
      let entry;
      try {
        const result = await withRpcTimeout(
          host.rpcExtract(
            'ClaudeCodeService.history_image',
            ref.session_id,
            ref.entry_uuid,
            ref.block,
          ),
          IMAGE_TIMEOUT_MS,
          'history_image',
        );
        entry =
          result && result.data_uri
            ? { dataUri: String(result.data_uri) }
            : {
                error:
                  (result && result.error && String(result.error)) ||
                  'That image is no longer readable',
              };
      } catch (err) {
        console.error(`[${label}] history_image failed`, err);
        entry = { error: err?.message || 'Could not read that image' };
      }
      cache.set(key, entry);
      host.requestUpdate();
    }
  }
}
