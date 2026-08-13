# Image Persistence

Images pasted into the chat input are persisted to disk so they can be displayed when browsing previous
sessions. Stored as individual files in the per-repo working directory, referenced by content hash in
the mirrored JSONL history.

This feature survives the conversion almost intact, because it never had anything to do with the
engine. It is a browser affordance plus a disk format: the data URI goes to the engine once, as part of
the turn, and the file on disk is what lets us render it again a week later. What changed is only the
writer's name and the fact that a *resumed* session's images come from two places — see § Session
Loading and Resume.

## Storage Location

- Images subdirectory within the per-repo working directory
- Created on working directory init and on history store construction (both idempotent, whichever runs first wins)

## File Naming

- Timestamp prefix plus short content-hash suffix
- Extension derived from MIME type with a fallback for unknown types
- Deduplication via content hash — identical data URIs produce identical filenames

### MIME to Extension Mapping

| MIME | Extension |
|---|---|
| PNG | .png |
| JPEG | .jpg |
| GIF | .gif |
| WebP | .webp |
| Fallback | .png |

## Writing Flow

- When a user message with images is persisted, each base64 data URI is processed
- Hash computed, MIME extracted, data decoded, filename generated
- Write to images directory, skip if file already exists (deduplication)
- Store filenames list as image refs in the JSONL record

## Reading Flow

- For each image ref filename, read binary data
- Determine MIME from extension
- Encode as base64 data URI
- Missing files skipped with a warning

## Message Schema Interaction

- Image refs field on JSONL records — list of filenames in the images directory
- Legacy image count field — kept for backward compatibility, deprecated
- Old messages with count-only field load correctly but won't have displayable images

## Engine Service Integration

- `ClaudeCodeService.chat_streaming(request_id, message, files, images, viewer)` receives the data URIs and does two independent things with them: forwards them to the SDK as the user turn's image content blocks, and hands the same list to the persistence layer
- The persistence layer accepts either a list of strings (saves each image, stores filenames as refs) or an integer (legacy path, stored as-is)
- Persistence happens on the way in, before the turn starts, so an interrupted or failed turn still has its images on disk and in the mirror
- The `userMessage` broadcast carries `image_refs` (filenames), not data URIs, so a collaborator's transcript renders from disk rather than re-receiving megabytes over the WebSocket

## Session Loading and Resume

- `history_get_session` and the initial `get_current_state` reconstruct images from refs. Frontend receives data URI arrays ready to render
- Each reconstruction is independent — a failed image read does not prevent other images from loading
- `load_session_into_context` and `get_session_messages_for_context` are gone. Loading a past session into the model's view is `resume_session`, which hands the engine its own transcript; we never re-inject message content

### Resume has two image sources, and only one is ours

After `resume_session`, the model's view of the images comes from the **engine's** transcript, which
holds the original content blocks. The browser's view comes from **our** mirror, which holds refs to
files on disk. Both are correct and they are not the same data.

The consequence to hold onto: deleting `.ac-dc4/images/` blinds the *browser*, not the model. A resumed
turn can still reason about an image the user can no longer see. The reverse also holds — a mirror gap
(see [`../3-engine/history.md`](../3-engine/history.md)) can leave an image on disk with no record
pointing at it, which is a leak, not a failure.

## Frontend Paste Input

- Accepted formats — PNG, JPEG, GIF, WebP
- Size limit per image (default 5MB) — reject before encoding with a visible error
- Maximum images per message (default 5)
- Encoding — base64 data URI
- Display — thumbnail previews with remove button, below textarea
- No token counting. AC⚡DC no longer estimates what an image costs; the engine reports actual usage in `streamComplete` and the context HUD reflects it afterwards (see [`../3-engine/context-visibility.md`](../3-engine/context-visibility.md))

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
- Loaded history sessions (reconstructed from refs as multimodal content blocks)
- Both rendering paths wrap thumbnails so the overlay button is consistent

## What Does Not Change

- AC⚡DC never re-sends an image. The engine keeps the original content blocks in its own context for the rest of the session, so the model does not need us to; re-attaching is a deliberate user action that sends a fresh copy
- Image size and count limits unchanged
- The on-disk format, the naming scheme, and the ref field are unchanged — an `.ac-dc4/images/` directory written by the native engine reads correctly here

## Cleanup

- No automatic cleanup
- Users can delete the images directory to reclaim space without affecting functionality (messages load without images, no errors)
- A future enhancement could add an explicit cleanup method that cross-references all refs in the JSONL against files in the images directory and removes orphans

## Invariants

- Identical image data URIs produce identical filenames (content-hash-based)
- Writing is idempotent — re-persisting a message never produces duplicate files
- Missing image files never fail message load — just skipped with a warning
- Per-message image count limit is enforced at paste, re-attach, and message send
- Re-attach never bypasses the deduplication or count limit