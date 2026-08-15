// Permission dialog styles.
//
// The z-index is the load-bearing part: this dialog renders above every
// other surface in the application, including the startup overlay and
// the toast layer, because the dialog panel can be minimized, docked,
// or dragged mostly offscreen while a permission request has stalled the
// turn (specs5/5-webapp/permission-dialog.md § Placement).
//
// The shell's own layers, for reference:
//   dialog panel      100
//   startup overlay  1000
//   toast layer      2000
// This sits at 9000, with the scrim just below it.

import { css } from 'lit';

export const PERMISSION_DIALOG_STYLES = css`
  :host {
    display: contents;
  }

  .scrim {
    position: fixed;
    inset: 0;
    z-index: 8999;
    background: rgba(0, 0, 0, 0.62);
    /* Clicks land here and stop. The scrim is deliberately not a
       dismiss: a stray click on a modal over a UI the user was
       mid-gesture in must do nothing at all
       (permission-dialog.md § Escape and the scrim). */
    cursor: default;
  }

  .dialog {
    position: fixed;
    z-index: 9000;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    display: flex;
    flex-direction: column;
    width: min(1100px, 92vw);
    max-height: 88vh;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.7);
    color: #c9d1d9;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px;
    overflow: hidden;
  }

  .dialog.risky {
    border-color: #6e2a2a;
  }

  /* ---------------- header ---------------- */

  header {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 10px 14px;
    background: #1c2128;
    border-bottom: 1px solid #30363d;
    flex: 0 0 auto;
  }

  .glyph {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 14px;
    color: #8b949e;
    flex: 0 0 auto;
  }

  .tool-name {
    font-weight: 600;
    color: #e6edf3;
    flex: 0 0 auto;
  }

  .target {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #8b949e;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1 1 auto;
    min-width: 0;
  }

  .queue-position {
    flex: 0 0 auto;
    color: #8b949e;
    font-variant-numeric: tabular-nums;
  }

  .countdown {
    flex: 0 0 auto;
    font-variant-numeric: tabular-nums;
    color: #8b949e;
  }

  .countdown.amber { color: #d29922; }
  .countdown.red { color: #f85149; font-weight: 600; }

  .attribution,
  .why {
    padding: 6px 14px;
    background: #1c2128;
    border-bottom: 1px solid #30363d;
    color: #8b949e;
    font-size: 12px;
    flex: 0 0 auto;
  }

  .attribution {
    color: #d2a8ff;
  }

  .no-localhost {
    padding: 6px 14px;
    background: #3d1d1d;
    border-bottom: 1px solid #6e2a2a;
    color: #ffa198;
    font-size: 12px;
    flex: 0 0 auto;
  }

  /* ---------------- body ---------------- */

  .body {
    flex: 1 1 auto;
    min-height: 140px;
    overflow: auto;
    padding: 12px 14px;
  }

  .body.no-padding {
    padding: 0;
  }

  .diff-host {
    width: 100%;
    height: min(52vh, 520px);
  }

  .new-file-pane,
  .command {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    line-height: 1.55;
    white-space: pre;
    overflow-x: auto;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 0;
    color: #e6edf3;
  }

  .command-edit {
    width: 100%;
    box-sizing: border-box;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12.5px;
    background: #0d1117;
    border: 1px solid #58a6ff;
    border-radius: 6px;
    padding: 10px 12px;
    color: #e6edf3;
  }

  .label {
    display: block;
    color: #8b949e;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin: 12px 0 4px;
  }

  .label:first-child { margin-top: 0; }

  .cwd {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #e6edf3;
  }

  .agent-description {
    font-style: italic;
    color: #8b949e;
  }

  .agent-description::before {
    content: 'the agent says: ';
    font-style: normal;
    color: #6e7681;
  }

  .chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-top: 10px;
  }

  .chip {
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    border: 1px solid #30363d;
    background: #21262d;
    color: #8b949e;
    cursor: help;
  }

  .chip.deletes,
  .chip.sudo {
    border-color: #6e2a2a;
    background: #3d1d1d;
    color: #ffa198;
  }

  .chip.network { border-color: #9e6a03; color: #e3b341; }

  .not-shown {
    padding: 24px;
    text-align: center;
    color: #8b949e;
    border: 1px dashed #30363d;
    border-radius: 6px;
    background: #0d1117;
  }

  .json {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    white-space: pre-wrap;
    word-break: break-word;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 0;
    color: #e6edf3;
    max-height: 40vh;
    overflow: auto;
  }

  /* interact */

  .question-group + .question-group {
    margin-top: 18px;
    padding-top: 14px;
    border-top: 1px solid #21262d;
  }

  .question-header {
    display: inline-block;
    margin-bottom: 6px;
    padding: 1px 7px;
    border-radius: 10px;
    background: #21262d;
    color: #8b949e;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .question {
    font-size: 14px;
    color: #e6edf3;
    margin: 0 0 10px;
    line-height: 1.5;
  }

  .options {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .option {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 8px 10px;
    border: 1px solid #30363d;
    border-radius: 6px;
    background: #0d1117;
    cursor: pointer;
  }

  .option:hover { border-color: #58a6ff; }
  .option input { margin-top: 2px; }
  .option .option-label { color: #e6edf3; }
  .option .option-description { color: #8b949e; font-size: 12px; }

  .other-answer {
    margin-top: 8px;
    width: 100%;
    box-sizing: border-box;
    padding: 8px 10px;
    font-family: inherit;
    font-size: 13px;
    color: #e6edf3;
    background: #0d1117;
    border: 1px dashed #30363d;
    border-radius: 6px;
  }

  .other-answer:focus {
    outline: none;
    border-style: solid;
    border-color: #58a6ff;
  }

  .other-note {
    display: block;
    margin-top: 4px;
    color: #8b949e;
    font-size: 11px;
  }

  /* plan */

  .plan {
    max-height: 52vh;
    overflow: auto;
    padding: 12px 14px;
    border: 1px solid #30363d;
    border-radius: 6px;
    background: #0d1117;
    color: #e6edf3;
    font-size: 13px;
    line-height: 1.55;
  }

  .plan h1, .plan h2, .plan h3, .plan h4 {
    margin: 14px 0 6px;
    line-height: 1.3;
  }

  .plan h1 { font-size: 17px; }
  .plan h2 { font-size: 15px; }
  .plan h3, .plan h4 { font-size: 14px; }
  .plan > :first-child { margin-top: 0; }
  .plan p, .plan ul, .plan ol { margin: 0 0 10px; }
  .plan ul, .plan ol { padding-left: 22px; }
  .plan li { margin: 3px 0; }
  .plan code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    background: #161b22;
    border-radius: 4px;
    padding: 1px 4px;
  }
  .plan pre {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 10px 12px;
    overflow: auto;
  }
  .plan pre code { background: none; padding: 0; }
  .plan a { color: #58a6ff; }
  .plan table { border-collapse: collapse; margin: 0 0 10px; }
  .plan th, .plan td {
    border: 1px solid #30363d;
    padding: 4px 8px;
    text-align: left;
  }

  /* ---------------- detail strip ---------------- */

  .detail-strip {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 7px 14px;
    background: #1c2128;
    border-top: 1px solid #30363d;
    font-size: 12px;
    color: #8b949e;
  }

  .stats .added { color: #7ee787; }
  .stats .removed { color: #ffa198; }

  details.full-input {
    flex: 1 1 auto;
  }

  details.full-input > summary {
    cursor: pointer;
    color: #58a6ff;
    list-style: none;
    user-select: none;
  }

  details.full-input > summary::-webkit-details-marker { display: none; }
  details.full-input > summary::before { content: '▸ '; }
  details.full-input[open] > summary::before { content: '▾ '; }

  details.full-input .json {
    margin-top: 8px;
    max-height: 30vh;
  }

  .edit-toggle {
    flex: 0 0 auto;
    background: none;
    border: 1px solid #30363d;
    border-radius: 5px;
    color: #58a6ff;
    padding: 3px 9px;
    font-size: 11.5px;
    cursor: pointer;
  }

  .edit-toggle:hover { border-color: #58a6ff; }

  /* ---------------- decisions ---------------- */

  .decisions {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 14px;
    background: #1c2128;
    border-top: 1px solid #30363d;
  }

  .decisions .spacer { flex: 1 1 auto; }

  button.decision {
    font: inherit;
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid #30363d;
    background: #21262d;
    color: #c9d1d9;
    cursor: pointer;
  }

  button.decision:hover:not(:disabled) { border-color: #8b949e; }

  button.decision:focus-visible {
    outline: 2px solid #58a6ff;
    outline-offset: 1px;
  }

  button.decision:disabled {
    opacity: 0.45;
    cursor: default;
  }

  button.decision.primary {
    background: #238636;
    border-color: #2ea043;
    color: #ffffff;
    font-weight: 600;
  }

  button.decision.primary:hover:not(:disabled) { background: #2ea043; }

  button.decision.danger {
    color: #ffa198;
    border-color: #6e2a2a;
  }

  button.decision.edited {
    background: #9e6a03;
    border-color: #d29922;
    color: #ffffff;
    font-weight: 600;
  }

  /* The mode switch. Marked out from the rule buttons beside it because it
     grants something of a different kind — every later edit in the session
     rather than one path — and a control that looks like its neighbour
     reads as a variation on it. Amber, the same warning colour the derived
     tag uses, rather than the green of the primary action. */
  button.decision.mode-switch {
    color: #e3b341;
    border-color: #6d4c0f;
  }

  button.decision.mode-switch:hover:not(:disabled) {
    background: #2d2410;
  }

  .split {
    display: flex;
    align-items: stretch;
  }

  .split > button.decision:first-child {
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
    border-right: none;
  }

  .split > button.caret {
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    padding: 6px 8px;
  }

  .rule-label {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: inline-block;
    vertical-align: bottom;
  }

  .destination {
    color: #8b949e;
    font-size: 11px;
    margin-left: 6px;
  }

  .derived-tag {
    color: #d29922;
    font-size: 11px;
    margin-left: 6px;
  }

  /* Louder than the derived tag on purpose: a derived rule may be wrong and
     asks to be read, whereas a shared one leaves this machine (CC-16). */
  .shared-tag {
    color: #f0883e;
    border: 1px solid #f0883e;
    border-radius: 4px;
    font-size: 10px;
    letter-spacing: 0.03em;
    margin-left: 6px;
    padding: 0 4px;
    text-transform: uppercase;
  }

  .menu {
    position: absolute;
    bottom: 46px;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6);
    padding: 4px;
    min-width: 260px;
    z-index: 1;
  }

  .menu button {
    display: block;
    width: 100%;
    text-align: left;
    font: inherit;
    background: none;
    border: none;
    color: #c9d1d9;
    padding: 6px 10px;
    border-radius: 4px;
    cursor: pointer;
  }

  .menu button:hover { background: #30363d; }

  .menu-anchor { position: relative; }

  .reason-row {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 14px 10px;
    background: #1c2128;
  }

  .reason-row input {
    flex: 1 1 auto;
    font: inherit;
    padding: 6px 10px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    color: #e6edf3;
  }

  .reason-row input:focus {
    outline: none;
    border-color: #58a6ff;
  }

  .read-only-note {
    flex: 1 1 auto;
    color: #8b949e;
    font-size: 12px;
    text-align: right;
  }

  .settling {
    color: #6e7681;
    font-size: 11px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
`;
