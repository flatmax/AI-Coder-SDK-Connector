# Image Persistence

Images pasted into the chat input are persisted so they can be displayed when browsing previous
sessions. **They live in the transcript, as the base64 content blocks they were sent as** — there is no
separate image directory and no ref field.

The frontend half of this feature — paste, thumbnails, lightbox, re-attach, the limits — is untouched by
the conversion. The storage half is replaced by [CC-19](../plan/decisions.md#cc-19): entries reach our
`SessionStore` as pass-through blobs, so we cannot extract a payload out of one and leave a pointer
behind without also rehydrating it on the way back. The image goes to the engine once as part of the
turn, and the entry the engine writes is what lets us render it again a week later.

## Storage

- **The transcript under `.aic-dc/sessions/`**, verbatim. A pasted screenshot is base64 inside a user
  entry, which is why a session with several of them produces a multi-MB transcript file.
- **No content-hash filenames and no deduplication.** Pasting the same image in two turns stores it
  twice, once per entry. The native engine's `{epoch_ms}-{hash12}.{ext}` scheme and its idempotent
  re-persist are retired with `history_store.py`.
- **The size revisit is a store-internal change.** If transcript size becomes a real problem, images are
  extracted on `append` and rehydrated on `load`, which preserves the round-trip invariant the protocol
  requires and changes nothing outside the store — including nothing in this document.
- **An `.aic-dc/images/` directory written by the native engine is ignored**, not read and not migrated.

## Reading Flow

- Entries are parsed with the SDK's `*_from_store` readers; image blocks come back as the content blocks
  they were written as.
- Each image is independent — one unreadable block does not prevent the rest of a message from
  rendering.
- A message whose entry never reached the store (see the mirror-gap discussion in
  [`../3-engine/history.md`](../3-engine/history.md)) renders without its images, with the gap already
  surfaced by the health banner rather than as a per-image error.

## Engine Service Integration

- `ClaudeCodeService.chat_streaming(request_id, message, files, images, viewer)` receives the data URIs
  and forwards them to the SDK as the user turn's image content blocks. There is no second hand-off to a
  persistence layer: persistence *is* the transcript.
- **Persistence is no longer ahead of the turn.** The old path wrote images to disk before the turn
  started, so a failed turn still had them. Now they are durable once the CLI has written the user entry
  and the eager flush has handed us a copy. A turn that dies before that leaves the pasted image only in
  the browser's own pending state.
- **The `userMessage` broadcast carries a pointer, not bytes** — session ID, entry `uuid`, block index.
  Collaborators fetch image data on demand through `history_image`, which reads the store, so a paste
  does not push megabytes down every socket. The initiating client already holds the data URI it pasted
  and fetches nothing.
- **The pointers arrive a moment after the message, as `userMessageImages`.** They cannot ride on
  `userMessage`: a pointer names the entry `uuid` the image lives in, and that entry does not exist when
  the broadcast goes out — the CLI writes it mid-turn, and we only learn its `uuid` when the mirror hands
  it to us. So the store gained an append observer, and the service reads the pointers off the entries as
  they are written (`RepoSessionStore.add_append_observer` →
  `history.image_refs_for_entry`, the one owner of the pointer shape). The follow-up is turn-scoped: the
  request ID says which message the pointers belong to, which is also what makes it a collaborator's
  event by construction — only a passively-received message carries a request ID to match, so the sender
  never attaches pointers to the bytes it already has. A prompt without images announces nothing, which
  is the overwhelmingly common case.

## Session Loading and Resume

- Opening a past session reads its images from that session's entries, through the same parsers as the
  rest of the transcript. Thumbnails resolve by pointer; the lightbox fetches full data on demand via
  `history_image`.
- **Resuming one does the same thing in the chat panel.** A restored message carries `image_refs` rather
  than data URIs, and the panel resolves them the way the browser does — after the transcript is on
  screen, one at a time, tiles at their final size, failures marked and cached. The two components draw
  different tiles and share the fetching, because the part that is easy to get subtly wrong (abandoning
  the work when the user resumes something else mid-fetch) is the part that is identical.
- `load_session_into_context` and `get_session_messages_for_context` are gone. Loading a past session
  into the model's view is resume, which hands the engine its own transcript; we never re-inject message
  content.

### Resume has one image source now

The model's view and the browser's view of a resumed session's images are the *same* entries. Under the
two-store design they were two different records of the same paste — the engine's content blocks and our
refs-to-disk — and the asymmetry had a memorable failure mode: deleting `.aic-dc/images/` blinded the
browser while the model could still reason about an image the user could no longer see.

That divergence is gone, and with it the leak in the other direction (a file on disk with no record
pointing at it). What replaces it is simpler and worth stating plainly: **losing the transcript loses the
images too.** There is no second copy, and deleting a session deletes its pictures.

## Frontend Paste Input

- Accepted formats — PNG, JPEG, GIF, WebP
- Size limit per image (default 5MB) — reject before encoding with a visible error
- Maximum images per message (default 5)
- Encoding — base64 data URI
- Display — thumbnail previews with remove button, below textarea
- No token counting. AIC⚡DC no longer estimates what an image costs; the engine reports actual usage in `streamComplete` and the context HUD reflects it afterwards (see [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md))

## Frontend Message Display

- Thumbnails in user cards with a re-attach overlay button (appears on hover)
- Clicking a thumbnail opens a lightbox overlay
- Lightbox — full-size view, Escape to close, re-attach action button at bottom
- Overlay is focusable for keyboard handling but does not implement full focus trapping

## Re-Attaching Previous Images

Two interaction paths to re-attach an image to the current input:

- Thumbnail overlay — small button (top-right, on hover) on the thumbnail
- Lightbox action — button at the bottom of the lightbox view

### Behavior

- Adds the data URI to the pending images array (same array used by paste)
- Respects the per-message limit — toast on overflow
- Duplicate detection — same data URI cannot be attached twice
- Confirmation toast on success
- Image appears in the thumbnail preview strip below the textarea, identical to a freshly pasted image
- Lightbox action button turns a success color on click for visual confirmation; lightbox stays open

### Scope

- Current session messages (stored as data URI arrays on message objects)
- Loaded history sessions (fetched by pointer from the transcript, then attached as a fresh data URI)
- Both rendering paths wrap thumbnails so the overlay button is consistent

## What Does Not Change

- AIC⚡DC never re-sends an image. The engine keeps the original content blocks in its own context for the rest of the session, so the model does not need us to; re-attaching is a deliberate user action that sends a fresh copy
- Image size and count limits unchanged
- The whole frontend surface — paste, thumbnails, lightbox, re-attach, limits, the absence of token counting — is unchanged. What moved is where the bytes rest

## Cleanup

- No automatic cleanup
- Reclaiming space means deleting sessions, which deletes their images with them. There is no image
  directory to clear independently, and therefore no orphan class to cross-reference: an image is either
  in an entry or it does not exist
- If transcript size becomes the reason cleanup is wanted, the extraction-and-rehydration option in
  § Storage is the cheaper answer, because it reclaims space without discarding history

## Invariants

- An image is stored exactly where the turn that carried it is stored, and nowhere else
- A message with an unreadable image block still renders; the image is skipped with a warning
- Per-message image count limit is enforced at paste, re-attach, and message send
- Re-attach never bypasses the count limit
- No image payload travels over a broadcast; collaborators receive pointers and fetch on demand