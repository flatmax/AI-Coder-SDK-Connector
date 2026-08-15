// Constants for the files-tab orchestrator.
//
// Extracted from the monolithic files-tab.js so the
// individual modules (helpers, styles, the main class)
// can import what they need without dragging in the
// whole class file.

// ---------------------------------------------------------------
// Left-panel resizer constants
// ---------------------------------------------------------------
//
// Spec'd in specs4/5-webapp/file-picker.md § Left Panel Resizer.
// Minimum width prevents the picker from collapsing below the
// point where tree rows become unreadable (file names truncate
// hard, context-menu buttons start overlapping). Maximum is
// expressed as 50% of the host width so the chat pane always
// retains half the dialog. Collapsed state shrinks the picker
// to a thin affordance strip — the stored _pickerWidthPx
// survives so double-click-to-expand restores the user's prior
// width rather than snapping to a default.

export const _PICKER_WIDTH_KEY = 'ac-dc-picker-width';
export const _PICKER_COLLAPSED_KEY = 'ac-dc-picker-collapsed';
export const _PICKER_MIN_WIDTH = 180;
export const _PICKER_COLLAPSED_WIDTH = 24;
export const _PICKER_DEFAULT_WIDTH = 280;

// The `_L0_EXCLUDE_PREF_*` keys lived here until
// conversion phase 3. They persisted an answer to a
// question the app no longer asks — whether to pay for an
// L0 cache rewrite when denying the agent a file. See
// ./exclusion.js for what replaced the dialog.

/**
 * Default tree stub used before the first RPC load. Lets the
 * picker render empty rather than showing a spinner while the
 * tree is en route — the picker's empty-state placeholder
 * handles the "no files yet" case gracefully.
 */
export const EMPTY_TREE = {
  name: '',
  path: '',
  type: 'dir',
  lines: 0,
  children: [],
};