// Mention / chip click handlers + path insertion.
//
// Three small handlers extracted from index.js. They share
// a theme (events from the picker or the chat panel that
// either write into the chat textarea or move the viewer)
// so they live together even though there's no internal
// coupling between them.
//
// None of them touch selection any more. There is no
// selection: a file the user wants the agent to read is
// named in the prompt, and `onInsertPath` is how the
// picker helps them name it (``specs5/plan/decisions.md``
// CC-21). What used to be a selection toggle on a mention
// or a chip is now plain navigation.

/**
 * Handle the picker's `insert-path` event — fired on
 * middle-click of a file or directory row, and by the
 * "Insert path in prompt" / "Insert @path" context-menu
 * items. Inserts the path into the chat panel's textarea
 * at the current cursor position, padded with spaces so
 * it doesn't jam against surrounding prose.
 *
 * `detail.mention` picks the form. The two are not
 * cosmetic variants of each other:
 *
 *   - bare `path/to/file.py` — a pointer. The agent
 *     reads it if the work needs reading it, and the
 *     turn costs one path's worth of tokens if it
 *     doesn't.
 *   - `@path/to/file.py` — a read. The CLI expands the
 *     mention into the file's full text before the turn
 *     starts, whether or not the agent would have asked
 *     for it.
 *
 * This is the picker's primary verb now that the
 * checkbox is gone, which is why it is reachable both by
 * gesture and by menu — see `_CONTEXT_MENU_FILE_ITEMS`.
 *
 * On Linux, middle-click triggers the selection-
 * buffer paste AFTER focus() is called. We set the
 * chat panel's `_suppressNextPaste` flag BEFORE
 * focus to pre-empt that paste — the flag is
 * one-shot and clears in the paste handler, so a
 * later intentional paste still works.
 *
 * Path padding — a space either side of the inserted
 * text, always, so the path is never run together with
 * what the user has already typed and whatever they type
 * next starts clear of it. The only thing that removes a
 * space is a space already being there: adjacent
 * whitespace is left to do the job rather than doubled.
 * Nothing already in the textarea is replaced except an
 * active selection, which the insertion stands in for.
 *
 * At the very start or end of the composer the space is
 * still added, which can leave the input with a leading
 * or trailing space. Harmless — `send()` trims, so it
 * never reaches the CLI.
 */
export function onInsertPath(host, event) {
  const path = event.detail?.path;
  if (typeof path !== 'string' || !path) return;
  const chat = host._chat();
  if (!chat) return;
  // Find the textarea inside the chat panel's shadow
  // DOM. Querying via the chat panel's shadowRoot
  // respects encapsulation.
  const ta = chat.shadowRoot?.querySelector('.input-textarea');
  if (!ta) return;
  // `@` goes on here rather than at either dispatch site,
  // so the gesture and the menu item cannot drift apart on
  // what an `@path` looks like.
  const text = event.detail?.mention === true ? `@${path}` : path;
  // Compute surround-padding from the textarea's
  // current state (not from any reactive property),
  // so the insertion reflects exactly what the user
  // sees.
  const before = ta.value.slice(0, ta.selectionStart);
  const after = ta.value.slice(ta.selectionEnd);
  const prefix = /\s$/.test(before) ? '' : ' ';
  const suffix = /^\s/.test(after) ? '' : ' ';
  const insertion = `${prefix}${text}${suffix}`;
  const next = `${before}${insertion}${after}`;
  // Push through the chat panel's reactive state so
  // the send-button enablement and auto-resize
  // respond to the change. Direct textarea value
  // assignment keeps cursor positioning accurate;
  // Lit's next render reflects the reactive value.
  chat._input = next;
  ta.value = next;
  const cursor = before.length + insertion.length;
  ta.setSelectionRange(cursor, cursor);
  // Set the suppression flag BEFORE focus — on Linux
  // the focus() call triggers the selection-buffer
  // auto-paste, which we need to swallow.
  chat._suppressNextPaste = true;
  ta.focus();
  // Fire an input event so the auto-resize logic
  // runs. The chat panel's _onInputChange handles
  // this via the native input event.
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

/**
 * Chat panel emits `file-mention-click` when the user
 * clicks a `.file-mention` span inside a rendered
 * assistant message. The event bubbles up through the
 * shadow DOM boundary (composed: true) and reaches us
 * via the `@file-mention-click` binding on `<ac-chat-panel>`
 * in the template.
 *
 * Open the file in the viewer, and nothing else. This
 * used to also toggle the file's selection, which meant
 * a click meant to read a file the agent had just talked
 * about silently changed what the next turn claimed the
 * user wanted (CC-21). Reading is now all a mention
 * click does, which is all a user clicking a filename in
 * prose was ever asking for.
 */
export function onFileMentionClick(host, event) {
  const path = event.detail?.path;
  if (typeof path !== 'string' || !path) return;
  window.dispatchEvent(
    new CustomEvent('navigate-file', {
      detail: { path },
      bubbles: false,
    }),
  );
}

/**
 * Chat panel emits `file-chip-click` when the user
 * clicks a chip in the "Files Referenced" summary
 * section at the bottom of an assistant message.
 *
 * The chips used to be a context-management surface —
 * each one toggled the file into or out of the selection,
 * and carried `navigate: false` so that curating context
 * didn't yank the user into the viewer. With the
 * selection gone the summary is a collected index of the
 * files this message named, and opening one is the only
 * thing left to want from it. So chips navigate now, the
 * same as the inline prose mentions they were once
 * distinguished from.
 */
export function onFileChipClick(host, event) {
  const path = event.detail?.path;
  if (typeof path !== 'string' || !path) return;
  window.dispatchEvent(
    new CustomEvent('navigate-file', {
      detail: { path },
      bubbles: false,
    }),
  );
}
