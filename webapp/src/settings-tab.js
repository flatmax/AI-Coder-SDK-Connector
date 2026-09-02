// SettingsTab — config editing and hot-reload.
//
// Layer 5 Phase 3.3 — Settings tab.
//
// Renders a card grid of whitelisted config types. Clicking
// a card opens its content in an inline monospace textarea.
// Save writes via Settings.save_config_content; app.json
// auto-triggers its reload RPC on save.
//
// The card set shrank with the native engine. The five prompt
// files (system, system extra, doc, review, compaction) were
// AIC⚡DC's own prompt assembly, and llm.json its provider
// credentials — neither exists now. What replaced them is
// engine.json, which is editable but deliberately NOT
// reloadable: the model, permission mode and CLI path are
// read when the subprocess starts, so a mid-session reload
// would report success while the running engine kept its
// original values.
//
// The model panel is the exception to that sentence, and the
// reason it is here. `specs5/5-webapp/settings.md § Info
// Banner` asks for the "resolved model — what the CLI actually
// resolved, which can legitimately differ from the configured
// alias", and for a long time nothing could answer it: the
// banner's old model name came from `get_config_info`, which is
// the *file's* request, and that field was removed rather than
// left to mislead. `ClaudeCodeService.get_model` answers it
// properly — the session's alias paired with the CLI's own
// resolution of it — and `set_model` is a control request, so
// unlike everything else on this tab it takes effect on the
// running session. It is also where `/model` lands
// (specs5/3-engine/session.md § Slash Commands).
//
// `/permissions` lands here too, and lands somewhere narrower:
// the mode the *next* session starts in is a field inside
// engine.json rather than a control, so the route opens that
// card and selects the `permission_mode` line. The running
// session's mode is the composer's own selector and is not this
// tab's (specs5/5-webapp/settings.md § Preference Cards).
//
// Governing spec: specs5/1-foundation/configuration.md

import { LitElement, css, html } from 'lit';
// The one owner of a byte count's rendering, which is why this reaches into
// the chat panel's render module for it rather than keeping six lines here.
// It grew a GB tier for this caller — see the note on the function.
import { formatBytes } from './chat-panel/block-render.js';
import { RpcMixin } from './rpc-mixin.js';
import {
  SURFACE,
  loadCapabilities,
  supports,
} from './engine-capabilities.js';
import { readPreference, writePreference } from './settings-preferences.js';

/**
 * Config cards — one per whitelisted type. The `key` field
 * matches the backend's CONFIG_TYPES keys. `reloadable`
 * controls whether save auto-triggers a reload RPC.
 *
 * Every card opens the textarea editor. There was a
 * `renderer: 'toggle'` variant for one card — **Agentic
 * coding**, gating whether AIC⚡DC told the model about its
 * `🟧🟧🟧 AGENT` spawn protocol — and it went with the
 * protocol: the agent's `Task` tool is part of the platform
 * and is not ours to switch off. A user who wants to
 * constrain delegation writes a `Task` deny rule in project
 * settings, which the permission layer honours like any
 * other rule. The switch machinery went with it; the
 * preference cards in
 * `specs5/5-webapp/settings.md § Preference Cards` will need
 * their own, sized to what they actually bind to.
 */
const CONFIG_CARDS = [
  { key: 'engine', icon: '🤖', label: 'Engine Config', format: 'json', reloadable: false },
  { key: 'app', icon: '⚙️', label: 'App Config', format: 'json', reloadable: true },
];

/**
 * Preference cards — a switch over one key of a config file.
 *
 * `specs5/5-webapp/settings.md` § Preference Cards. Each of these binds
 * to a field that was already editable in the textarea beside it, so
 * what a card adds is **discoverability, not capability** — and the part
 * worth getting right is therefore not the control but the sentence
 * under it, because the two cards below take effect at different times
 * and neither takes effect now.
 *
 * `applies` is that sentence's key, and the two values are the whole
 * point of the pair:
 *
 * - `next-session` — `engine.json` is read when the CLI subprocess
 *   starts, so the Restart button below is what applies this. The field
 *   joins `_pendingFields`, which is what makes the restart
 *   confirmation name it
 * - `next-pass` — `app.json` is reloadable and the save calls the
 *   reload, but the consumer is a background build rather than a value
 *   read per use. Switching enrichment off stops the *next* pass; it
 *   does not remove keywords already computed, and switching it back on
 *   does not start a pass. "Applied now" would be the wrong claim in
 *   both directions
 *
 * The third row the spec lists — **Deny-read scope** — is not here. It
 * resets a remembered answer to a prompt that does not exist yet
 * (`file-picker.md`, and § B4 of `specs5/next.md`), so a control that
 * forgets it would be a control over nothing.
 */
const PREFERENCE_CARDS = [
  {
    key: 'thinking-display',
    configType: 'engine',
    path: ['thinking_display'],
    icon: '💭',
    label: 'Thinking display',
    control: 'select',
    // '' is the file's `null` — "let the CLI decide", which is a real
    // third state and not a synonym for either of the other two.
    fallback: '',
    options: [
      { value: '', label: 'Engine default' },
      { value: 'summarized', label: 'Summarised' },
      { value: 'omitted', label: 'Omitted' },
    ],
    applies: 'next-session',
    note: 'Read when the CLI starts — restart the session to apply it.',
    title:
      'Whether the engine sends thinking regions, and how. Engine default'
      + ' leaves the choice to the CLI; Summarised sends condensed thinking;'
      + ' Omitted sends none. engine.json → thinking_display.',
  },
  {
    key: 'doc-enrichment',
    configType: 'app',
    path: ['doc_index', 'keywords_enabled'],
    icon: '🔑',
    label: 'Doc enrichment',
    control: 'checkbox',
    fallback: true,
    applies: 'next-pass',
    note: 'Applies to the next enrichment pass, not to keywords already found.',
    title:
      'Whether KeyBERT adds keywords to document outlines after the'
      + ' structural build. Off saves roughly a gigabyte of resident model'
      + ' and the pass that loads it; outlines keep their structure either'
      + ' way. app.json → doc_index.keywords_enabled.',
  },
];

/**
 * The CLI's own alias for "whatever you would have picked anyway".
 *
 * A session's model is null when nothing named one: engine.json may omit it, in
 * which case no model argument was passed and the CLI used its own default.
 * That lands where this alias lands, and the CLI advertises the alias as a
 * `models` entry with a resolution filled in — so a null model shows as this
 * entry rather than as a blank select. The distinction is not thrown away: the
 * note under the control says the file pins nothing, because somebody who
 * wants a model pinned needs to know that it is not.
 */
export const DEFAULT_MODEL_ALIAS = 'default';

/** How long the panel stays marked after a `/model` route lands on it. */
const _FLASH_MS = 2200;

/**
 * Where the retired-files note remembers it was dismissed.
 *
 * Persisted rather than per-load: the note answers a question that is
 * asked once — "where did my System extra card go?" — and a note that
 * came back on every visit after being read would be nagging about a
 * set of files that is never going to change again.
 *
 * Keyed on the file list, not a bare boolean. If a later upgrade
 * retires something else, the new name has never been explained to this
 * user and the note is owed again; a boolean would swallow it.
 */
const RETIRED_NOTE_DISMISSED_KEY = 'aic-dc-retired-note-dismissed';

/**
 * What a dismissal is recorded *as* — the list the user actually read.
 *
 * Sorted so the key does not depend on the backend's ordering, joined
 * on a character that cannot occur in a filename we ship.
 */
export function retiredNoteSignature(files) {
  return (Array.isArray(files) ? files : [])
    .filter((f) => typeof f === 'string' && f)
    .slice()
    .sort()
    .join('|');
}

/**
 * The engine's `models` list, normalised, and guaranteed to contain `current`.
 *
 * Same lesson as the chat panel's `permissionModeOptions`: a `<select>` whose
 * value is not among its options renders as the *first* option, so an alias the
 * engine does not advertise — a custom one from engine.json, a session resumed
 * from a newer CLI — would silently read as whatever happens to be at the top
 * of the list. Appended rather than dropped, and marked as unlisted.
 *
 * An empty list stays empty. The engine connects lazily, so having nothing to
 * offer is the ordinary state before the first turn, and a one-item menu built
 * from the alias would look like a choice the user had.
 */
export function modelEntries(models, current) {
  const entries = [];
  for (const m of Array.isArray(models) ? models : []) {
    if (!m || typeof m !== 'object') continue;
    const value = typeof m.value === 'string' ? m.value : '';
    if (!value) continue;
    entries.push({
      value,
      label: typeof m.displayName === 'string' && m.displayName
        ? m.displayName
        : value,
      resolved: typeof m.resolvedModel === 'string' ? m.resolvedModel : '',
      detail: typeof m.description === 'string' ? m.description : '',
    });
  }
  if (entries.length === 0) return entries;
  const alias = current || DEFAULT_MODEL_ALIAS;
  if (!entries.some((e) => e.value === alias)) {
    entries.push({
      value: alias,
      label: alias,
      resolved: '',
      detail: 'In force for this session, but not in the engine list.',
    });
  }
  return entries;
}

/**
 * The span of the line that sets `field` in a JSON document, or null.
 *
 * Text, not `JSON.parse`: the editor holds whatever the user has typed, which
 * may not parse at all, and a field is worth pointing at precisely while the
 * file is being edited. The span runs from the key's opening quote to the end of
 * its line, so what gets selected reads as the setting — `"permission_mode":
 * "acceptEdits",` — rather than as an offset.
 *
 * A key, not a value that happens to contain the name: the quotes must be
 * followed by a colon. Without that check, a `"cli_path": "/opt/permission_mode"`
 * would be the line the reader is sent to.
 *
 * @param {string} content
 * @param {string} field
 * @returns {{start: number, end: number}|null}
 */
export function fieldLineRange(content, field) {
  if (typeof content !== 'string' || !field) return null;
  const needle = `"${field}"`;
  let at = 0;
  for (const line of content.split('\n')) {
    const col = line.indexOf(needle);
    if (col !== -1 && line.slice(col + needle.length).trimStart().startsWith(':')) {
      return { start: at + col, end: at + line.length };
    }
    at += line.length + 1;
  }
  return null;
}

/**
 * One list out of a save's disposition, defensively.
 *
 * `null` is a real answer from the RPC — content that did not parse has no
 * fields to diff — and so is an absent key from an older backend. Both read as
 * "nothing to report" rather than throwing on the render path.
 *
 * @param {object|null} disposition
 * @param {'live'|'next_session'|'changed'} which
 * @returns {string[]}
 */
export function fieldList(disposition, which) {
  if (!disposition || typeof disposition !== 'object') return [];
  const list = disposition[which];
  return Array.isArray(list) ? list.filter((f) => typeof f === 'string') : [];
}

/** `a`, `a and b`, `a, b and c` — for a sentence, not a table. */
export function joinFields(fields) {
  const list = [...fields];
  if (list.length <= 1) return list.join('');
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

export class SettingsTab extends RpcMixin(LitElement) {
  static properties = {
    /** Info banner data from get_config_info. */
    _info: { type: Object, state: true },
    /** Currently-open card key, or null. */
    _activeKey: { type: String, state: true },
    /** Content loaded into the editor textarea. */
    _editorContent: { type: String, state: true },
    /** Whether the editor is loading content. */
    _loading: { type: Boolean, state: true },
    /** Whether a save is in flight. */
    _saving: { type: Boolean, state: true },
    /**
     * The models the engine advertises, from `get_model().models`. Empty
     * before the engine's handshake, which is every load before the first
     * turn — an ordinary state, not a failure.
     */
    _models: { type: Array, state: true },
    /**
     * The model alias in force, or '' when the session pins none.
     *
     * Written from `get_model`, from `set_model`'s reply, and from a
     * `modelChanged` broadcast — never from the `<select>`'s own value on
     * change. The select is a request; this is the answer.
     */
    _model: { type: String, state: true },
    /** What the engine says `_model` resolves to, or '' if it has not said. */
    _resolved: { type: String, state: true },
    /** True while a `set_model` call is in flight. */
    _modelPending: { type: Boolean, state: true },
    /**
     * `list_engines()`'s answer, or null before the first read.
     *
     * `{active, available, mountable}`. `available` and `mountable` are
     * different questions and the control needs both: an engine this build
     * knows about but that has no credential here is offered as a disabled
     * option with the reason, not omitted — omitting it makes a missing key
     * look like a build that never had the engine.
     */
    _engines: { type: Object, state: true },
    /** True while a `switch_engine` call is in flight. */
    _enginePending: { type: Boolean, state: true },
    /** Why the last switch was refused, or '' — the server's own sentence. */
    _engineError: { type: String, state: true },
    /**
     * False once `Collab.get_collab_role` reports this client is not the host.
     * `set_model` and `restart_session` are both localhost-only, so an enabled
     * control for a participant would be one that always fails. Only ever
     * narrows what is offered — the engine keeps the real gate.
     */
    _isHost: { type: Boolean, state: true },
    /**
     * What the last save did, from the RPC's per-field disposition, or null
     * before the first one.
     * `{type, changed, applied, pending, controls, compared}`. `changed` is
     * kept alongside the other two because they do not have to cover it: a
     * reloadable type whose reload failed has a changed field that is neither
     * applied nor waiting for a restart, and that case needs saying.
     *
     * Rendered rather than only toasted because the toast goes away and the
     * question it answers — is the thing I just saved in force? — does not.
     */
    _summary: { type: Object, state: true },
    /**
     * Every field saved since the last restart that the running session has
     * not picked up. A Set, and accumulated across saves rather than replaced:
     * two saves each touching one field leave two fields waiting, and a
     * restart applies the whole file either way.
     */
    _pendingFields: { type: Object, state: true },
    /** True while `restart_session` is in flight. */
    _restarting: { type: Boolean, state: true },
    /**
     * `get_session_storage`'s answer, or null before the first read.
     *
     * Held as the whole reply — `{bytes, over_warning}` or `{error, reason}` —
     * because the three states this card renders are the three shapes the RPC
     * can answer with, and flattening them into a number plus a flag would
     * lose which of "not measured yet" and "nothing to measure" is true.
     */
    _storage: { type: Object, state: true },
    /**
     * True once the retired-files note has been dismissed for this exact
     * file list. Read from localStorage on load, so a dismissal survives
     * a refresh — see `RETIRED_NOTE_DISMISSED_KEY`.
     */
    _retiredDismissed: { type: Boolean, state: true },
    /**
     * True for a moment after `/model` routed here, so the panel the command
     * meant is visibly the one it landed on. A route can arrive with this tab
     * already open and already scrolled to the panel, where scrolling alone
     * changes nothing on screen and the command looks like it did nothing.
     */
    _modelFlash: { type: Boolean, state: true },
    /** The same mark, for the editor a field-naming route opened. */
    _editorFlash: { type: Boolean, state: true },
    /**
     * Raw file text per config type, for the preference cards to read
     * their values out of. `{engine: string, app: string}`, missing keys
     * until the read lands.
     *
     * The whole file rather than the fields, because a preference write
     * has to put the value back into the text it came from without
     * disturbing the rest — see `settings-preferences.js`. Two reads on
     * mount, of files this tab was going to read anyway the moment
     * somebody opened a card.
     */
    _prefContent: { type: Object, state: true },
    /** The preference card whose write is in flight, or null. */
    _prefPending: { type: String, state: true },
  };

  static styles = css`
    :host {
      display: flex;
      flex-direction: column;
      height: 100%;
      min-height: 0;
      background: var(--bg-primary, #0d1117);
      color: var(--text-primary, #c9d1d9);
      font-size: 0.9375rem;
      overflow-y: auto;
      padding: 1rem;
    }

    .toolbar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 1rem;
    }
    .toolbar .minimize-right {
      margin-left: auto;
    }
    .back-btn {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-secondary, #8b949e);
      padding: 0.2rem 0.5rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.875rem;
      line-height: 1;
      font-family: inherit;
    }
    .back-btn:hover {
      background: rgba(240, 246, 252, 0.06);
      color: var(--text-primary, #c9d1d9);
      border-color: rgba(240, 246, 252, 0.3);
    }

    .info-banner {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.8125rem;
      color: var(--text-secondary, #8b949e);
    }
    .info-banner strong {
      color: var(--text-primary, #c9d1d9);
    }
    .info-row {
      display: flex;
      gap: 0.5rem;
      margin-top: 0.25rem;
    }
    .info-label {
      opacity: 0.7;
      min-width: 5rem;
    }

    /*
      The retired-files note. Same chrome as the info banner because it
      is the same kind of thing — a fact about the config directory, not
      a control — but muted further and set apart by the left rule: it
      is history, and it is the one block here a reader is meant to
      finish with rather than return to.
    */
    .retired-note {
      background: rgba(22, 27, 34, 0.4);
      border: 1px solid rgba(240, 246, 252, 0.08);
      border-left: 3px solid rgba(187, 128, 9, 0.5);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.8125rem;
      color: var(--text-secondary, #8b949e);
      line-height: 1.5;
    }
    .retired-note-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 1rem;
    }
    .retired-note-title {
      color: var(--text-primary, #c9d1d9);
      font-weight: 600;
    }
    .retired-note ul {
      margin: 0.5rem 0;
      padding-left: 1.25rem;
    }
    .retired-note li {
      font-family: var(--font-mono, ui-monospace, monospace);
      font-size: 0.75rem;
    }
    .retired-note p {
      margin: 0.5rem 0 0;
    }
    .retired-note code {
      font-family: var(--font-mono, ui-monospace, monospace);
      color: var(--text-primary, #c9d1d9);
    }
    /*
      A text button, not the icon-only ✕ this would usually be: the
      note is dismissed for good, and a bare glyph does not say that.
    */
    .retired-dismiss {
      flex: none;
      background: none;
      border: 1px solid rgba(240, 246, 252, 0.15);
      border-radius: 4px;
      color: var(--text-secondary, #8b949e);
      cursor: pointer;
      font-size: 0.75rem;
      padding: 0.15rem 0.5rem;
    }
    .retired-dismiss:hover {
      color: var(--text-primary, #c9d1d9);
      border-color: rgba(240, 246, 252, 0.3);
    }

    /*
      The model panel. Same card chrome as the info banner so it
      reads as part of the header rather than as a config card —
      it is not one: every card below opens an editor on a file,
      and this control reaches the running session.
    */
    .model-panel {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-bottom: 1rem;
      font-size: 0.8125rem;
      transition: border-color 240ms ease, background 240ms ease;
    }
    /*
      The mark a routed command leaves. Border and background
      only, no animation: the tab may already be scrolled here,
      and a moving thing is the one way to be sure the reader
      sees that something answered.
    */
    .model-panel.flash {
      border-color: var(--accent-primary, #58a6ff);
      background: rgba(88, 166, 255, 0.08);
    }
    .model-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .model-title {
      font-weight: 600;
      color: var(--text-primary, #c9d1d9);
    }
    .model-select {
      background: rgba(13, 17, 23, 0.8);
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-primary, #c9d1d9);
      border-radius: 4px;
      padding: 0.25rem 0.4rem;
      font-family: inherit;
      font-size: 0.8125rem;
      max-width: 16rem;
    }
    .model-select:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    .model-pending {
      color: var(--text-secondary, #8b949e);
    }
    .model-resolved {
      margin-top: 0.4rem;
      color: var(--text-secondary, #8b949e);
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.75rem;
      word-break: break-all;
    }
    .model-note {
      margin: 0.4rem 0 0;
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      line-height: 1.45;
    }
    .model-note code {
      font-family: 'SFMono-Regular', Consolas, monospace;
      background: rgba(240, 246, 252, 0.06);
      border-radius: 3px;
      padding: 0 0.2rem;
    }

    /*
      The preference grid. Same card chrome, wider track: a select
      with "Engine default" in it needs more than the 110px an icon and
      a two-word label need, and a preference card is read rather than
      clicked so it is not competing for the same compactness.

      Not hoverable and not clickable — the card is a frame around a
      control, and a hover highlight on the frame would promise the
      whole thing does something.
    */
    .pref-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }
    .pref-card {
      cursor: default;
      text-align: left;
      padding: 0.5rem 0.6rem;
    }
    .pref-card:hover {
      background: rgba(22, 27, 34, 0.6);
      border-color: rgba(240, 246, 252, 0.1);
    }
    .pref-card .card-icon {
      display: inline;
      margin-right: 0.35rem;
    }
    .pref-card .card-label {
      color: var(--text-primary, #c9d1d9);
      font-size: 0.8125rem;
    }
    .pref-control {
      display: flex;
      align-items: center;
      gap: 0.35rem;
      margin-top: 0.4rem;
      font-size: 0.8125rem;
      color: var(--text-primary, #c9d1d9);
    }
    .pref-select {
      background: rgba(13, 17, 23, 0.8);
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-primary, #c9d1d9);
      border-radius: 4px;
      padding: 0.2rem 0.35rem;
      font-family: inherit;
      width: 100%;
      box-sizing: border-box;
    }
    .pref-control input:disabled,
    .pref-select:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }
    /*
      The load-bearing line. Every field a preference card binds to is
      also a line in the textarea below, so this sentence — when the
      value starts being true — is the only thing the card adds.
    */
    .pref-note {
      display: block;
      margin-top: 0.35rem;
      font-size: 0.6875rem;
      line-height: 1.35;
      color: var(--text-secondary, #8b949e);
    }

    .card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
      gap: 0.5rem;
      margin-bottom: 0.75rem;
    }
    .card {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 5px;
      padding: 0.4rem 0.5rem;
      cursor: pointer;
      text-align: center;
      transition: border-color 120ms ease, background 120ms ease;
    }
    .card:hover {
      background: rgba(240, 246, 252, 0.04);
      border-color: rgba(240, 246, 252, 0.2);
    }
    .card.active {
      border-color: var(--accent-primary, #58a6ff);
      background: rgba(88, 166, 255, 0.08);
    }
    .card-icon {
      font-size: 1.1rem;
      display: block;
      margin-bottom: 0.2rem;
      line-height: 1;
    }
    .card-label {
      font-size: 0.75rem;
      color: var(--text-secondary, #8b949e);
      line-height: 1.2;
    }

    .editor-area {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      overflow: hidden;
      transition: border-color 240ms ease;
    }
    /* Same mark as the model panel's, for the same reason. */
    .editor-area.flash {
      border-color: var(--accent-primary, #58a6ff);
    }
    .editor-toolbar {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 0.75rem;
      background: rgba(22, 27, 34, 0.6);
      border-bottom: 1px solid rgba(240, 246, 252, 0.08);
    }
    .editor-toolbar .toolbar-label {
      font-size: 0.8125rem;
      font-weight: 600;
      flex: 1;
    }
    .toolbar-button {
      background: transparent;
      border: 1px solid rgba(240, 246, 252, 0.15);
      color: var(--text-primary, #c9d1d9);
      padding: 0.3rem 0.6rem;
      border-radius: 4px;
      cursor: pointer;
      font-size: 0.75rem;
      font-family: inherit;
    }
    .toolbar-button:hover {
      background: rgba(240, 246, 252, 0.06);
      border-color: rgba(240, 246, 252, 0.3);
    }
    .toolbar-button:disabled {
      opacity: 0.4;
      cursor: not-allowed;
    }
    .toolbar-button.primary {
      background: var(--accent-primary, #58a6ff);
      border-color: var(--accent-primary, #58a6ff);
      color: #0d1117;
    }
    .toolbar-button.primary:hover {
      filter: brightness(1.1);
    }
    /*
      Between the toolbar and the textarea, so it reads as being about the
      file above the one it sits under. Bordered rather than tinted amber:
      "applies later" is the ordinary outcome of a save on this tab, not a
      fault, and colouring it as one would train the reader past it.
    */
    .save-summary {
      padding: 0.5rem 0.75rem;
      background: rgba(88, 166, 255, 0.06);
      border-bottom: 1px solid rgba(240, 246, 252, 0.08);
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      line-height: 1.5;
      display: flex;
      flex-direction: column;
      gap: 0.2rem;
    }
    .save-summary strong {
      color: var(--text-primary, #c9d1d9);
    }
    .save-summary-note {
      padding-left: 0.75rem;
    }

    .session-controls {
      background: rgba(22, 27, 34, 0.6);
      border: 1px solid rgba(240, 246, 252, 0.1);
      border-radius: 6px;
      padding: 0.75rem 1rem;
      margin-top: 1rem;
      font-size: 0.8125rem;
    }
    .session-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .session-title {
      font-weight: 600;
      color: var(--text-primary, #c9d1d9);
      flex: 1;
    }
    .session-note {
      margin: 0.4rem 0 0;
      color: var(--text-secondary, #8b949e);
      font-size: 0.75rem;
      line-height: 1.45;
    }
    .session-note code {
      font-family: 'SFMono-Regular', Consolas, monospace;
      background: rgba(240, 246, 252, 0.06);
      border-radius: 3px;
      padding: 0 0.2rem;
    }
    .session-note strong {
      color: var(--text-primary, #c9d1d9);
    }
    /* Its own line under the restart note, and a rule above it: the two
     * sentences answer different questions, and a figure run on from the
     * restart paragraph reads as a consequence of restarting. */
    .storage-note {
      margin-top: 0.6rem;
      padding-top: 0.5rem;
      border-top: 1px solid rgba(240, 246, 252, 0.08);
    }
    .storage-warn {
      color: var(--warning, #d29922);
    }
    /* A link, not a button, because it goes somewhere rather than doing
     * something — but a real button element underneath it, since there is no
     * URL to put in an anchor and an anchor with no href is not focusable.
     * (No backticks in this block: it is all one template literal.) */
    .storage-link {
      background: none;
      border: none;
      padding: 0;
      font: inherit;
      color: var(--accent, #58a6ff);
      cursor: pointer;
      text-decoration: underline;
    }
    .storage-link:hover {
      color: var(--text-primary, #c9d1d9);
    }

    .editor-textarea {
      flex: 1;
      min-height: 200px;
      width: 100%;
      box-sizing: border-box;
      resize: none;
      padding: 0.75rem;
      background: rgba(13, 17, 23, 0.8);
      border: none;
      color: var(--text-primary, #c9d1d9);
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.8125rem;
      line-height: 1.5;
    }
    .editor-textarea:focus {
      outline: none;
    }
    .loading-note {
      padding: 2rem;
      text-align: center;
      color: var(--text-secondary, #8b949e);
      font-style: italic;
    }
  `;

  constructor() {
    super();
    this._info = null;
    this._activeKey = null;
    this._editorContent = '';
    this._loading = false;
    this._saving = false;
    this._models = [];
    this._model = '';
    this._resolved = '';
    this._modelPending = false;
    this._engines = null;
    this._enginePending = false;
    this._engineError = '';
    this._isHost = true;
    this._summary = null;
    this._pendingFields = new Set();
    this._restarting = false;
    this._storage = null;
    this._retiredDismissed = false;
    this._modelFlash = false;
    this._editorFlash = false;
    this._prefContent = {};
    this._prefPending = null;
    /** Pending flash-clear timer, so a second route restarts it. */
    this._flashTimer = null;
    // Bound so add/removeEventListener find the same reference.
    this._onModelChanged = this._onModelChanged.bind(this);
    this._onEngineChanged = this._onEngineChanged.bind(this);
  }

  connectedCallback() {
    super.connectedCallback();
    // Another window's switch. The window that made the call has the answer in
    // its reply and does not need this, but it arrives there too and says the
    // same thing, so no guard is needed.
    window.addEventListener('model-changed', this._onModelChanged);
    window.addEventListener('engine-changed', this._onEngineChanged);
  }

  disconnectedCallback() {
    window.removeEventListener('model-changed', this._onModelChanged);
    window.removeEventListener('engine-changed', this._onEngineChanged);
    if (this._flashTimer) {
      clearTimeout(this._flashTimer);
      this._flashTimer = null;
    }
    this._modelFlash = false;
    this._editorFlash = false;
    super.disconnectedCallback();
  }

  onRpcReady() {
    // The descriptor first, and awaited before the reads that depend on
    // it. `supports()` answers "yes" until the real answer lands — the
    // right default, since hiding every panel for one round trip on the
    // shipped engine is the worse trade — but a fetch fired before it
    // lands is a call the router may refuse, and the re-render is what
    // takes the panel away once it has.
    loadCapabilities(this).then(() => {
      this.requestUpdate();
      this._loadStorage();
    });
    this._loadInfo();
    this._loadModel();
    this._loadEngines();
    this._loadPreferences();
  }

  /**
   * Re-read the model when this tab comes back on screen.
   *
   * The list is empty until the engine's handshake, and the engine connects on
   * the first turn — so a tab opened before that turn holds an empty menu with
   * nothing to tell it otherwise. `get_model` is answered from the stored
   * initialize reply, so this costs a round trip and no control request.
   */
  onTabVisible() {
    this._loadModel();
    // The files can move without this tab: the textarea in another
    // window, `/permissions` in this one, an editor outside the app. A
    // switch showing a value the file stopped holding is worse than the
    // round trip, because the next click writes the stale one back.
    this._loadPreferences();
    // And the session directory grows with every turn, and shrinks when the
    // user takes this card's own advice. Re-reading on reveal is what closes
    // that loop: the figure argues for a deletion, the deletion happens in
    // another surface, and coming back here is when the new number is worth
    // a round trip. Nothing pushes the size, so nothing else would correct it.
    this._loadStorage();
  }

  /**
   * Read the files the preference cards render from.
   *
   * Both types unconditionally, and failures are warned rather than
   * surfaced: a card whose file could not be read renders its control
   * disabled with the reason on the tooltip (`_prefState`), which is a
   * better answer than a toast on a tab the user may have opened for
   * something else entirely.
   */
  async _loadPreferences() {
    if (!this.rpcConnected) return;
    const types = [...new Set(PREFERENCE_CARDS.map((c) => c.configType))];
    const reads = await Promise.all(
      types.map(async (type) => {
        try {
          const res = await this.rpcExtract('Settings.get_config_content', type);
          if (res && typeof res === 'object' && !res.error) {
            return [type, typeof res.content === 'string' ? res.content : ''];
          }
          console.warn('[settings] get_config_content failed', type, res?.error);
        } catch (err) {
          console.warn('[settings] get_config_content failed', type, err);
        }
        return null;
      }),
    );
    const next = { ...this._prefContent };
    for (const entry of reads) {
      if (entry) next[entry[0]] = entry[1];
    }
    this._prefContent = next;
  }

  /**
   * What one card should show, and whether it can be operated.
   *
   * Three states rather than two, because "we have not read the file
   * yet" and "the file will not parse" want different tooltips and only
   * one of them is the user's problem.
   *
   * @param {object} card an entry of `PREFERENCE_CARDS`
   * @returns {{value: *, ready: boolean, reason: string}}
   */
  _prefState(card) {
    const content = this._prefContent[card.configType];
    if (typeof content !== 'string') {
      return { value: card.fallback, ready: false, reason: 'Reading the config file…' };
    }
    if (writePreference(content, card.path, card.fallback) === null) {
      return {
        value: card.fallback,
        ready: false,
        reason:
          `${card.configType}.json is not a JSON object, so this switch cannot`
          + ' edit it. Open the card below and fix the file.',
      };
    }
    return {
      value: readPreference(content, card.path, card.fallback),
      ready: true,
      reason: card.title,
    };
  }

  /**
   * Write one preference back to its file.
   *
   * The base is the **textarea** when that file is open for editing, and
   * a fresh read otherwise. Not the copy in `_prefContent`: a switch that
   * wrote a stale base would silently discard whatever the user had typed
   * into the editor above it, which is the one failure this control could
   * cause that the textarea alone never could. When the editor is open
   * the result goes back into it too, so the two surfaces cannot end up
   * describing different files.
   */
  async _setPreference(card, value) {
    if (!this.rpcConnected || this._prefPending || this._isHost === false) return;
    this._prefPending = card.key;
    let wrote = false;
    try {
      const openHere = this._activeKey === card.configType;
      const textarea = openHere
        ? this.shadowRoot?.querySelector('.editor-textarea')
        : null;
      let base;
      if (textarea) {
        base = textarea.value;
      } else {
        const res = await this.rpcExtract(
          'Settings.get_config_content',
          card.configType,
        );
        if (!res || typeof res !== 'object' || res.error) {
          this._emitToast(res?.error || 'Could not read the config file', 'error');
          return;
        }
        base = typeof res.content === 'string' ? res.content : '';
      }
      const next = writePreference(base, card.path, value);
      if (next === null) {
        this._emitToast(
          `${card.configType}.json does not parse, so ${card.label} could not be`
          + ' written. Open the card below and fix the file.',
          'error',
        );
        return;
      }
      const result = await this.rpcExtract(
        'Settings.save_config_content',
        card.configType,
        next,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        return;
      }
      wrote = true;
      this._prefContent = { ...this._prefContent, [card.configType]: next };
      if (textarea) {
        textarea.value = next;
        this._editorContent = next;
        // The panel above the textarea describes an older save of this
        // file and now sits over content it did not produce.
        this._summary = null;
      }
      await this._announcePreference(card, value, result);
    } catch (err) {
      this._emitToast(`Save failed: ${err?.message || err}`, 'error');
    } finally {
      this._prefPending = null;
      if (!wrote) this._restorePrefControl(card);
    }
  }

  /**
   * Put a control back to what its file says.
   *
   * A native `<select>` or checkbox flips itself on the gesture, before
   * anything is written, and Lit will not put it back — the `.value`
   * binding only re-commits when the bound value changes, and a save
   * that failed changed nothing. So a refused write would leave the
   * control claiming a setting the file does not have, which is the
   * failure this tab exists to prevent one screen up (§ *Invariants*:
   * no unqualified success for a field that did not apply).
   *
   * The same lesson as the model select's reply-is-authoritative rule,
   * one control class down: the gesture is a request, the file is the
   * answer.
   */
  _restorePrefControl(card) {
    const state = this._prefState(card);
    const root = this.shadowRoot;
    if (!root) return;
    if (card.control === 'checkbox') {
      const box = root.querySelector(`.pref-card[data-pref="${card.key}"] input`);
      if (box) box.checked = state.value !== false;
      return;
    }
    const select = root.querySelector(`.pref-card[data-pref="${card.key}"] select`);
    if (select) select.value = state.value ?? '';
  }

  /**
   * Say where the value just written takes effect, and get it there.
   *
   * The disposition comes from the save rather than from the card,
   * because the save is the thing that knows whether the field actually
   * moved — flipping a switch back to what the file already said is a
   * real gesture with nothing to report, and a card-shaped message would
   * promise a restart for it.
   */
  async _announcePreference(card, value, result) {
    const changed = fieldList(result?.disposition, 'changed');
    const label = this._prefValueLabel(card, value);
    if (!changed.length) {
      this._emitToast(`${card.label}: ${label}. Already what the file said.`, 'info');
      return;
    }
    if (card.applies === 'next-session') {
      for (const field of fieldList(result?.disposition, 'next_session')) {
        this._pendingFields.add(field);
      }
      this.requestUpdate();
      this._emitToast(
        `${card.label}: ${label}. Applies when the session next starts —`
        + ' use Restart session below.',
        'info',
      );
      return;
    }
    // `next-pass`. The reload is what lets the running process see the
    // new value at all; without it the build would read the old one.
    const reloaded = await this._reload(card.configType);
    this._emitToast(
      reloaded
        ? `${card.label}: ${label}. ${card.note}`
        : `${card.label}: ${label} — saved to the file, but the reload did not`
          + ' apply, so the running process is still on the old value.',
      reloaded ? 'success' : 'warning',
    );
  }

  /** How a written value reads in a sentence. */
  _prefValueLabel(card, value) {
    if (card.control === 'checkbox') return value ? 'on' : 'off';
    const option = card.options.find((o) => o.value === (value ?? ''));
    return option ? option.label : String(value);
  }

  async _loadInfo() {
    if (!this.rpcConnected) return;
    try {
      const result = await this.rpcExtract('Settings.get_config_info');
      this._info = result && typeof result === 'object' ? result : null;
      this._retiredDismissed = this._readRetiredDismissal();
    } catch (err) {
      console.warn('[settings] get_config_info failed', err);
    }
  }

  /**
   * Read what `.aic-dc/sessions/` is using.
   *
   * The reply is stored whatever shape it has, errors included, because this
   * is the one figure on the tab a user might come looking for: a card that
   * silently showed nothing would leave "the transcripts are tiny" and "the
   * walk failed" looking identical, which is the fault the RPC refuses to
   * commit on its own side. A thrown call is treated the same way, since a
   * disconnect mid-read is a reason and not an absence.
   */
  async _loadStorage() {
    if (!this.rpcConnected) return;
    // The mirror is one engine's, not the product's: an engine that keeps
    // its transcripts somewhere this app does not own has no directory to
    // measure, and the router refuses the method rather than answering
    // zero. Asked by capability, never by engine name (AG-R-4).
    if (!supports(SURFACE.SESSION_MIRROR)) return;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.get_session_storage');
      this._storage =
        res && typeof res === 'object'
          ? res
          : { error: 'The engine gave no answer for the session directory' };
    } catch (err) {
      this._storage = {
        error: `Could not read the session directory: ${err?.message || err}`,
      };
    }
  }

  /**
   * Ask the chat panel for its history browser.
   *
   * Deletion belongs next to what is being deleted, so this card offers the
   * route and not the delete — one button here that opened a confirm would be
   * a second way to destroy a transcript, sited where the thing destroyed is
   * not on screen. The dialog minimizes on the way for the reason the Context
   * tab's file links do: the browser opens behind it, and a click that reveals
   * nothing is indistinguishable from a click that did nothing.
   */
  _browseHistory() {
    window.dispatchEvent(new CustomEvent('open-history', { bubbles: false }));
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  /** The retired files this install has, or `[]` — never null. */
  get _retiredFiles() {
    const files = this._info?.retired_files;
    return Array.isArray(files)
      ? files.filter((f) => typeof f === 'string' && f)
      : [];
  }

  /**
   * Whether this exact list has already been dismissed.
   *
   * Storage access is wrapped because it throws outright in a browser
   * with site data blocked, and a Settings tab that failed to render
   * over a dismissal preference would be a bad trade. Unreadable
   * storage means "not dismissed": showing the note twice is a smaller
   * fault than never showing it.
   */
  _readRetiredDismissal() {
    const signature = retiredNoteSignature(this._retiredFiles);
    if (!signature) return false;
    try {
      return localStorage.getItem(RETIRED_NOTE_DISMISSED_KEY) === signature;
    } catch {
      return false;
    }
  }

  _dismissRetiredNote() {
    this._retiredDismissed = true;
    try {
      localStorage.setItem(
        RETIRED_NOTE_DISMISSED_KEY,
        retiredNoteSignature(this._retiredFiles),
      );
    } catch {
      // Dismissed for this load either way; it will return next visit.
    }
  }

  /**
   * Read the model in force and the menu the engine offers.
   *
   * One RPC, because the two halves are one fact and `get_model` pairs them on
   * the server where the list already is. A failure is warned and left alone
   * rather than blanking what is on screen: the alias last read is still the
   * best answer available, and a panel that empties itself on a transient RPC
   * error would claim the session has no model.
   */
  async _loadModel() {
    if (!this.rpcConnected) return;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.get_model');
      if (!res || typeof res !== 'object' || res.error) {
        console.warn('[settings] get_model failed', res?.error);
        return;
      }
      this._model = typeof res.model === 'string' ? res.model : '';
      this._resolved = typeof res.resolved === 'string' ? res.resolved : '';
      this._models = Array.isArray(res.models) ? res.models : [];
    } catch (err) {
      console.warn('[settings] get_model failed', err);
      return;
    }
    this._probeHostAuthority();
  }

  /**
   * Find out whether this client is the host.
   *
   * One question for both of this tab's engine controls — the model switch and
   * the session restart — because it is one answer. Same probe and the same
   * defaults as the chat panel's `probeModeAuthority`: no collab service
   * registered means single-user, which means we are the host. It only narrows
   * the UI — both RPCs enforce the real gate, so being wrong here costs a
   * rejected call, not an unauthorised one.
   */
  async _probeHostAuthority() {
    try {
      const role = await this.rpcExtract('Collab.get_collab_role');
      if (role && typeof role === 'object' && !role.error) {
        this._isHost = role.is_localhost !== false;
        return;
      }
    } catch (_) {
      // No collab service — single-user, and we are the host.
    }
    this._isHost = true;
  }

  /**
   * Read which engines are mounted and which one is master.
   *
   * `list_engines` is the one RPC a component is *allowed* to read an
   * engine name from, and the distinction is worth restating where it is
   * used: a human choosing which engine to start a session on is a
   * different thing from a component deciding whether to draw a bar, and
   * only the second is forbidden from knowing the name (AG-R-4). Nothing
   * on this tab branches on the answer — it is rendered as text and sent
   * back as a choice.
   *
   * A failure leaves the panel unrendered rather than showing an empty
   * selector. One engine that cannot be listed is indistinguishable from
   * one engine that exists, and offering a control that cannot say what
   * it would switch to is worse than offering none.
   */
  async _loadEngines() {
    if (!this.rpcConnected) return;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.list_engines');
      if (!res || typeof res !== 'object' || res.error) {
        console.warn('[settings] list_engines failed', res?.error);
        return;
      }
      this._engines = res;
    } catch (err) {
      console.warn('[settings] list_engines failed', err);
    }
  }

  /**
   * Another window switched the engine — or this one did.
   *
   * The shell has already replaced the capability descriptor by the time
   * this runs, so there is nothing to refetch here; what is left is the
   * name in the selector and clearing the "in flight" state, which the
   * calling window would otherwise hold until its own reply landed.
   */
  _onEngineChanged(event) {
    const engine = event?.detail?.engine;
    if (typeof engine !== 'string') return;
    this._engines = { ...(this._engines || {}), active: engine };
    this._enginePending = false;
    this._engineError = '';
  }

  /**
   * Handle an engine selection.
   *
   * Like `_onModelSelect`, the `<select>`'s own value is not the answer —
   * the reply is. Unlike it, a refusal is *shown* rather than only warned
   * to the console: `switch_engine` declines for reasons the user can act
   * on (a turn is still running; that engine has no credential here), and
   * a control that silently snapped back would be indistinguishable from
   * one that is broken.
   */
  async _onEngineSelect(event) {
    const wanted = event?.target?.value;
    const active = this._engines?.active || '';
    if (event?.target) event.target.value = active;
    if (!wanted || wanted === active) return;
    this._enginePending = true;
    this._engineError = '';
    try {
      const res = await this.rpcExtract(
        'ClaudeCodeService.switch_engine', wanted,
      );
      if (!res || typeof res !== 'object' || res.error) {
        this._engineError = res?.error
          || 'The engine did not say why the switch failed.';
        this._enginePending = false;
        return;
      }
      // The broadcast is what updates `_engines.active`, in this window
      // and every other, so there is one writer rather than two that can
      // disagree. It arrives here too.
      if (res.changed === false) this._enginePending = false;
    } catch (err) {
      this._engineError = `Could not switch engines: ${err?.message || err}`;
      this._enginePending = false;
    }
  }

  /**
   * The engine in force, and a switch — when there is more than one.
   *
   * Rendered only where a second engine is mountable. A selector with one
   * option is a control that cannot do anything, and on the overwhelmingly
   * common single-engine install it would be a permanent question about a
   * feature that install does not have.
   *
   * The warning under it is not decoration. This control ends the
   * conversation on screen: the two engines' transcripts do not translate,
   * so there is no version of this that carries the current session
   * across, and a user who reads "switch" as "switch and continue" would
   * lose a conversation they thought they were keeping.
   */
  _renderEnginePanel() {
    const engines = this._engines;
    if (!engines) return '';
    const mountable = Array.isArray(engines.mountable) ? engines.mountable : [];
    const available = Array.isArray(engines.available) ? engines.available : [];
    if (mountable.length < 2) return '';
    const active = engines.active || '';
    const readOnly = this._isHost === false;
    const disabled = !this.rpcConnected || readOnly || this._enginePending;
    return html`
      <div class="model-panel" role="group" aria-label="Engine">
        <div class="model-head">
          <span class="model-title">⚙️ Engine</span>
          <select
            class="model-select"
            .value=${active}
            ?disabled=${disabled}
            autocomplete="off"
            aria-label="Engine"
            title=${readOnly
              ? 'Only the host can switch engines'
              : 'The engine this session runs on'}
            @change=${(e) => this._onEngineSelect(e)}
          >
            ${available.map(
              (name) => html`<option
                value=${name}
                ?selected=${name === active}
                ?disabled=${!mountable.includes(name)}
                title=${mountable.includes(name)
                  ? ''
                  : 'Not mounted in this session — no credential, or the '
                    + 'optional dependency is not installed'}
              >
                ${name}${mountable.includes(name) ? '' : ' (not mounted)'}
              </option>`,
            )}
          </select>
          ${this._enginePending
            ? html`<span class="model-pending" aria-hidden="true">…</span>`
            : ''}
        </div>
        <p class="model-note">
          Switching engines <strong>starts a new session</strong>. The two
          engines keep their transcripts in different formats, so a
          conversation cannot follow you across — nothing is deleted, and the
          conversation you leave stays in the history browser.
          ${readOnly ? 'Only the host can switch engines.' : ''}
        </p>
        ${this._engineError
          ? html`<p class="model-note engine-error">${this._engineError}</p>`
          : ''}
      </div>
    `;
  }

  /** Another window changed the model. */
  _onModelChanged(event) {
    const model = event?.detail?.model;
    if (typeof model !== 'string' && model !== null) return;
    this._model = model || '';
    this._resolved = this._resolvedFor(this._model);
    this._modelPending = false;
  }

  /** What the engine's list says an alias resolves to, or ''. */
  _resolvedFor(alias) {
    const entry = modelEntries(this._models, alias)
      .find((e) => e.value === (alias || DEFAULT_MODEL_ALIAS));
    return entry?.resolved || '';
  }

  /**
   * Handle a selection.
   *
   * The `<select>`'s DOM value goes straight back to the alias in force, before
   * the RPC resolves — the native element flips itself and Lit will not put it
   * back, because `_model` has not changed yet. The real flip comes from the
   * reply, which is authoritative: the service records the new alias only after
   * the engine's control request came back.
   *
   * The gesture latch is the one from the chat panel's permission-mode
   * selector, and it is here for the same reason a switch that costs money
   * should not be reachable by an event nobody raised — a browser restoring
   * form state on load raises a trusted `change` with no pointer or key in
   * front of it. See `webapp/src/chat-panel/permission-mode.js` for the full
   * account; the difference is that a model switch has no confirmation to
   * intercept it, so the latch is the only guard there is.
   */
  _onModelSelect(event) {
    const select = event?.target;
    const picked = select?.value;
    const current = this._model || DEFAULT_MODEL_ALIAS;
    if (select) select.value = current;
    const gesture = this._modelGesture === true;
    this._modelGesture = false;
    if (!gesture) return;
    if (typeof picked !== 'string' || !picked || picked === current) return;
    this._setModel(picked);
  }

  /** Record that a user is actually operating the control. */
  _noteModelGesture() {
    this._modelGesture = true;
  }

  /**
   * Ask the engine to switch models.
   *
   * `_modelPending` disables the control while the call is out: two switches in
   * flight would race, and the reply that landed last would win regardless of
   * which the user picked last.
   */
  async _setModel(value) {
    if (!this.rpcConnected || this._modelPending) return;
    this._modelPending = true;
    try {
      const res = await this.rpcExtract('ClaudeCodeService.set_model', value);
      if (res && typeof res === 'object' && res.error) {
        const reason = res.error === 'restricted'
          ? res.reason || 'Only the host can change the model'
          : res.error;
        this._emitToast(reason, 'warning');
        return;
      }
      const applied = res && typeof res === 'object' ? res.model : undefined;
      this._model = typeof applied === 'string' ? applied : '';
      this._resolved = this._resolvedFor(this._model);
      this._emitToast(`Model: ${this._modelLabel()}`, 'success');
    } catch (err) {
      this._emitToast(
        `Could not change the model: ${err?.message || err}`,
        'error',
      );
    } finally {
      this._modelPending = false;
    }
  }

  /** The display name for the alias in force, falling back to the alias. */
  _modelLabel() {
    const alias = this._model || DEFAULT_MODEL_ALIAS;
    const entry = modelEntries(this._models, alias)
      .find((e) => e.value === alias);
    return entry?.label || alias;
  }

  /**
   * A routed command asked for one part of this tab.
   *
   * Duck-typed hook the shell calls after switching tabs. This tab has no
   * segmented control, so "show a section" means bring the thing the command
   * named to where the reader is looking and mark it — one panel for `/model`,
   * one field inside a config file for `/permissions`.
   *
   * An id this tab does not have is ignored, which is what keeps a service that
   * grows a new anchor from making the tab itself unreachable.
   *
   * Returns a promise for the tests' benefit; the shell ignores it.
   *
   * @param {string} id
   * @returns {Promise<void>}
   */
  async showSection(id) {
    if (id === 'model') {
      // Re-read: the command asked about the model, and the menu may have
      // arrived since this tab last looked.
      this._loadModel();
      this._flash('model');
      await this.updateComplete;
      this._scrollTo('.model-panel', 'start');
      return;
    }
    if (id === 'permission-mode') {
      await this._showEngineField('permission_mode');
    }
  }

  /**
   * Mark one part of this tab for a moment, so a routed command visibly landed.
   *
   * One timer for both marks, and each flash clears the other: two parts of the
   * tab lit at once would say two commands arrived when only the second did.
   *
   * @param {'model'|'editor'} which
   * @private
   */
  _flash(which) {
    this._modelFlash = which === 'model';
    this._editorFlash = which === 'editor';
    if (this._flashTimer) clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._flashTimer = null;
      this._modelFlash = false;
      this._editorFlash = false;
    }, _FLASH_MS);
  }

  /**
   * Open the engine.json card and select the line that sets `field`.
   *
   * What `/permissions` lands on. The mode the *next* session starts in is a
   * field in a JSON file, not a control, so "show me the permission mode" can
   * only mean: open the file that holds it and put the reader's cursor on the
   * line. Opening the tab alone left them in front of a card grid with the field
   * they asked for one click and a scroll inside a file away.
   *
   * The selection is read off the textarea rather than `_editorContent`, because
   * the textarea is where the user's unsaved edits are — including, plausibly,
   * the line they just added after this said the field was missing.
   *
   * @param {string} field
   * @private
   */
  async _showEngineField(field) {
    await this._openCard('engine');
    this._flash('editor');
    await this.updateComplete;
    this._scrollTo('.editor-area', 'center');
    const textarea = this.shadowRoot?.querySelector('.editor-textarea');
    if (!textarea) return;
    const range = fieldLineRange(textarea.value, field);
    if (!range) {
      // Say so rather than leave a flash over a file with no such line: an
      // absent key is a real answer — the CLI's own default is in force — and
      // the reader who typed the command is the one who would set it.
      this._emitToast(
        `engine.json does not set ${field} — the engine's own default is in force`,
        'info',
      );
      return;
    }
    if (typeof textarea.setSelectionRange === 'function') {
      textarea.focus();
      textarea.setSelectionRange(range.start, range.end);
    }
  }

  /**
   * Scroll one of this tab's own elements into view, if the engine can.
   *
   * jsdom has no `scrollIntoView`, and neither does an older browser.
   *
   * @private
   */
  _scrollTo(selector, block) {
    const el = this.shadowRoot?.querySelector(selector);
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ block, behavior: 'smooth' });
    }
  }

  async _openCard(key) {
    if (this._activeKey === key) return;
    this._activeKey = key;
    // Belongs to the file being left. `_pendingFields` does not — those are
    // waiting on the session, not on the editor.
    this._summary = null;
    this._editorContent = '';
    this._loading = true;
    try {
      const result = await this.rpcExtract(
        'Settings.get_config_content',
        key,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        this._activeKey = null;
        return;
      }
      const content =
        typeof result === 'object' && result !== null
          ? result.content ?? ''
          : typeof result === 'string'
            ? result
            : '';
      this._editorContent = content;
    } catch (err) {
      this._emitToast(`Load failed: ${err?.message || err}`, 'error');
      this._activeKey = null;
    } finally {
      this._loading = false;
    }
  }

  _closeEditor() {
    this._activeKey = null;
    this._editorContent = '';
    this._summary = null;
  }

  async _save() {
    if (!this._activeKey || this._saving) return;
    this._saving = true;
    try {
      const textarea = this.shadowRoot?.querySelector('.editor-textarea');
      const content = textarea ? textarea.value : this._editorContent;
      const key = this._activeKey;
      const result = await this.rpcExtract(
        'Settings.save_config_content',
        key,
        content,
      );
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        return;
      }
      const disposition = result ? result.disposition : null;
      const pending = fieldList(disposition, 'next_session');
      // Advisory JSON warning from save.
      if (result && result.warning) {
        this._emitToast(result.warning, 'warning');
      } else if (pending.length) {
        // Qualified, not "Saved": the invariant this tab has to keep is that
        // a save never shows an unqualified success for a field that did not
        // apply (`specs5/5-webapp/settings.md` § Invariants). The panel says
        // the same thing at more length, and outlives the toast.
        this._emitToast(
          `Saved. ${joinFields(pending)} ${pending.length === 1 ? 'applies' : 'apply'}`
          + ' when the session next starts.',
          'info',
        );
      } else {
        this._emitToast('Saved', 'success');
      }
      for (const field of pending) this._pendingFields.add(field);
      // Auto-reload for reloadable configs. `applied` is joined here rather
      // than reported by the save, because the save cannot know: the reload
      // is this next call, and it can fail.
      const card = CONFIG_CARDS.find((c) => c.key === key);
      const reloaded = card && card.reloadable ? await this._reload() : false;
      this._summary = disposition
        ? {
          type: key,
          changed: fieldList(disposition, 'changed'),
          applied: reloaded ? fieldList(disposition, 'live') : [],
          pending,
          controls: disposition.live_control || {},
          compared: disposition.compared !== false,
        }
        : null;
      this.requestUpdate();
    } catch (err) {
      this._emitToast(`Save failed: ${err?.message || err}`, 'error');
    } finally {
      this._saving = false;
    }
  }

  /**
   * Re-read the active config into the running process.
   *
   * Only app.json is reloadable, so there is one target. A card
   * marked `reloadable: false` — engine.json is the only one left —
   * returns early rather than calling a reload that would either
   * fail or lie: engine.json's values were consumed when the
   * subprocess started.
   *
   * `key` is the config type to reload, defaulting to the open editor's.
   * A preference card writes a file that may not be open at all, and the
   * reloadability question is about the *file*, never about which panel
   * asked — reading it off `_activeKey` unconditionally would have made
   * the switch's reload depend on whether a textarea happened to be
   * showing.
   *
   * @param {string} [key]
   * @returns {Promise<boolean>} whether the reload applied. The save's
   *   summary reports fields as applied only on a true, so a reload that
   *   failed leaves them unclaimed rather than claimed by the call before it.
   */
  async _reload(key = this._activeKey) {
    if (!key) return false;
    const card = CONFIG_CARDS.find((c) => c.key === key);
    if (!card || !card.reloadable) return false;
    try {
      const result = await this.rpcExtract('Settings.reload_app_config');
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(`Reload failed: ${result.error}`, 'error');
        return false;
      }
      this._emitToast('Config reloaded', 'success');
      return true;
    } catch (err) {
      this._emitToast(`Reload failed: ${err?.message || err}`, 'error');
      return false;
    }
  }

  /**
   * Replace the CLI subprocess so the file's options are the running ones.
   *
   * The confirmation names the fields waiting, because a restart is not free —
   * it ends the CLI's warm state — and "restart the session?" with nothing
   * named is a question nobody can weigh. It also states the one thing the
   * pending list cannot cover: a model or mode changed by hand this session
   * goes back to what the file says, whether or not this save touched it.
   */
  async _restartSession() {
    if (this._restarting || !this.rpcConnected || this._isHost === false) return;
    if (!window.confirm(this.restartConfirmText())) return;
    this._restarting = true;
    try {
      const result = await this.rpcExtract('ClaudeCodeService.restart_session');
      if (result && typeof result === 'object' && result.error) {
        this._emitToast(result.error, 'error');
        return;
      }
      // Nothing is waiting any more either way: a restarted session was built
      // from the file, and a cold one has now adopted it.
      this._pendingFields = new Set();
      this._summary = null;
      this._emitToast(
        result && result.status === 'adopted'
          ? 'The engine had not started yet, so there was nothing to replace —'
            + ' it will start on the file as you have just saved it'
          : 'Session restarted. The conversation was resumed.',
        result && result.status === 'adopted' ? 'info' : 'success',
      );
      // The file may have taken the model back from a mid-session switch. The
      // broadcast says so too, and does not carry the resolution line.
      this._loadModel();
    } catch (err) {
      this._emitToast(`Restart failed: ${err?.message || err}`, 'error');
    } finally {
      this._restarting = false;
    }
  }

  /**
   * The confirmation text. Public for the test that reads it without a
   * `window.confirm` to intercept.
   */
  restartConfirmText() {
    const waiting = [...this._pendingFields].sort();
    return `Restart the session?\n\n${
      waiting.length
        ? `This applies ${joinFields(waiting)}.`
        : 'This applies engine.json as it is on disk.'
    }\n\nThe conversation is resumed, so the transcript and the model's own `
      + 'context come back. The CLI starts again, so the session\'s cost '
      + 'totals start from zero. A model or permission mode you changed by '
      + 'hand this session goes back to what engine.json says.';
  }

  _onEditorKeyDown(event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 's') {
      event.preventDefault();
      this._save();
    }
  }

  _emitToast(message, type = 'info') {
    window.dispatchEvent(
      new CustomEvent('aic-toast', {
        detail: { message, type },
        bubbles: false,
      }),
    );
  }

  /**
   * Dispatch a request to the app shell to flip the
   * active dialog tab back to the chat. Companion to
   * the equivalent method in ContextUsageTab and DocConvertTab.
   */
  _goBackToChat() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-tab', {
        detail: { tab: 'files' },
        bubbles: true,
        composed: true,
      }),
    );
  }

  /**
   * Dispatch a request to the app shell to minimize
   * the dialog. Companion to ``_goBackToChat``: the
   * tab-strip minimize button isn't reachable when
   * Settings is the active tab (the strip sits in
   * the chat panel which is a sibling tab-panel),
   * so each overlay carries its own minimize
   * affordance.
   */
  _minimizeDialog() {
    this.dispatchEvent(
      new CustomEvent('request-dialog-minimize', {
        bubbles: true,
        composed: true,
      }),
    );
  }

  render() {
    return html`
      <div class="toolbar">
        <button
          class="back-btn"
          title="Back to chat"
          aria-label="Back to chat"
          @click=${() => this._goBackToChat()}
        >← Chat</button>
        <button
          class="back-btn minimize-right"
          title="Minimize dialog"
          aria-label="Minimize dialog"
          @click=${() => this._minimizeDialog()}
        >▾</button>
      </div>
      <!--
        The banner used to lead with the model name from
        get_config_info. That RPC no longer reports one, on
        purpose: the model in engine.json is a request, and the
        engine can answer with a different one (a fallback on
        rate-limit, a set_model mid-session). The model in
        force is reported where it is known — per turn, in the
        usage HUD.
      -->
      ${this._info
        ? html`
            <div class="info-banner">
              ${this._info.config_dir
                ? html`
                    <div class="info-row">
                      <span class="info-label">Config:</span>
                      <span>${this._info.config_dir}</span>
                    </div>
                  `
                : ''}
            </div>
          `
        : ''}

      ${this._renderRetiredNote()}

      ${this._renderEnginePanel()}

      ${this._renderModelPanel()}

      ${this._renderPreferenceCards()}

      <div class="card-grid">
        ${CONFIG_CARDS.map(
          (card) => html`
            <div
              class="card ${this._activeKey === card.key ? 'active' : ''}"
              @click=${() => this._openCard(card.key)}
              title="${card.label} (${card.format})"
            >
              <span class="card-icon">${card.icon}</span>
              <span class="card-label">${card.label}</span>
            </div>
          `,
        )}
      </div>

      ${this._activeKey ? this._renderEditor() : ''}
      ${this._renderSessionControls()}
    `;
  }

  /**
   * Why six cards are gone, for the installs that can tell they went.
   *
   * `specs5/5-webapp/settings.md` § Deleted cards argues this at length:
   * a user who customised `system_extra.md` over months and finds the
   * card gone deserves to know why. Phase 3 left the files on disk and
   * inert precisely so nothing irreversible happened to them, and then
   * never said so — the leaving-alone was right and the silence was the
   * mistake.
   *
   * Rendered only when this install actually has such a file. A fresh
   * install never had the cards, so the note would be explaining the
   * disappearance of something the reader has never seen; the backend
   * answers with names on disk rather than the constant list for
   * exactly this reason.
   *
   * Above the model panel, because it is about what is *not* here and
   * the reader is looking for a card they cannot find. Below the
   * banner, because the directory it names is the one the note is
   * about.
   */
  _renderRetiredNote() {
    const files = this._retiredFiles;
    if (!files.length || this._retiredDismissed) return '';
    return html`
      <div class="retired-note" role="note">
        <div class="retired-note-head">
          <span class="retired-note-title">
            Some config cards are gone, and their files are not
          </span>
          <button
            class="retired-dismiss"
            title="Dismiss — this will not come back"
            @click=${() => this._dismissRetiredNote()}
          >Dismiss</button>
        </div>
        <p>
          These are still in your config directory. Nothing reads them
          any more, nothing migrated them, and nothing will delete
          them:
        </p>
        <ul>
          ${files.map((name) => html`<li>${name}</li>`)}
        </ul>
        <p>
          They held the system prompt AIC⚡DC used to assemble and the
          provider settings it used to need. There is no system prompt
          to own now — the agent's instructions come from
          <code>CLAUDE.md</code> and <code>.claude/</code>, which you
          edit in the viewer like any other file in the repository,
          with the agent's help, and which the Context tab prices in
          tokens.
        </p>
      </div>
    `;
  }

  /**
   * The switches, above the files they write.
   *
   * Above rather than below, and in a grid of their own: these are the
   * settings somebody came here to change, and the config cards under
   * them are the escape hatch for everything that has no switch. Their
   * own grid because a `<select>` does not fit the 110px track the icon
   * cards use — same card chrome, wider column, which is what
   * `specs5/5-webapp/settings.md` § Preference Cards asks for when it
   * says "the same card shape".
   *
   * The note under each control is not decoration. Both fields are
   * already editable in the textarea below, so the card adds nothing
   * except the thing the textarea cannot say — when the value it just
   * wrote starts being true.
   */
  _renderPreferenceCards() {
    const readOnly = this._isHost === false;
    return html`
      <div class="pref-grid" role="group" aria-label="Preferences">
        ${PREFERENCE_CARDS.map((card) => {
          const state = this._prefState(card);
          const busy = this._prefPending === card.key;
          const disabled = !this.rpcConnected || readOnly || busy || !state.ready;
          const title = readOnly
            ? 'Only the host can change settings'
            : state.reason;
          return html`
            <div class="card pref-card" data-pref=${card.key} title=${title}>
              <span class="card-icon">${card.icon}</span>
              <span class="card-label">${card.label}</span>
              ${card.control === 'checkbox'
                ? html`
                    <label class="pref-control">
                      <input
                        type="checkbox"
                        .checked=${state.value !== false}
                        ?disabled=${disabled}
                        aria-label=${card.label}
                        @change=${(e) =>
                          this._setPreference(card, e.target.checked)}
                      />
                      <span>${state.value !== false ? 'On' : 'Off'}</span>
                    </label>
                  `
                : html`
                    <select
                      class="pref-control pref-select"
                      .value=${state.value ?? ''}
                      ?disabled=${disabled}
                      autocomplete="off"
                      aria-label=${card.label}
                      @change=${(e) =>
                        this._setPreference(card, e.target.value || null)}
                    >
                      ${card.options.map(
                        (option) => html`<option
                          value=${option.value}
                          ?selected=${option.value === (state.value ?? '')}
                        >${option.label}</option>`,
                      )}
                    </select>
                  `}
              <span class="pref-note">${busy ? 'Saving…' : card.note}</span>
            </div>
          `;
        })}
      </div>
    `;
  }

  /**
   * What the last save did, and where each field it moved took effect.
   *
   * Inside the editor area rather than under the grid: it is about the file in
   * the textarea above it, and a reader who has just pressed Save is looking
   * there. Absent until a save has happened — there is nothing to report
   * about a file that has only been read.
   */
  _renderSaveSummary() {
    const summary = this._summary;
    if (!summary) return '';
    const {
      applied, pending, controls, changed,
    } = summary;
    if (!changed.length) {
      return html`<div class="save-summary" role="status">
        Saved. Nothing in the file changed, so nothing is waiting.
      </div>`;
    }
    if (!applied.length && !pending.length) {
      // Reloadable type, `live` non-empty, and the reload came back false.
      // "Nothing is waiting" would be the one wrong answer here: the fields
      // moved on disk and the running process is still on the old ones.
      return html`<div class="save-summary" role="status">
        Saved to the file. The reload did not apply, so
        ${joinFields(changed)}
        ${changed.length === 1 ? 'is' : 'are'} not in force yet.
      </div>`;
    }
    const shortcuts = pending.filter((f) => controls && controls[f]);
    return html`
      <div class="save-summary" role="status">
        ${applied.length
          ? html`<div>
              <strong>Applied now:</strong> ${joinFields(applied)} — read
              through accessors, so the new value is what the next use sees.
            </div>`
          : ''}
        ${pending.length
          ? html`
              <div>
                <strong>Applies when the session next starts:</strong>
                ${joinFields(pending)}.
                ${summary.compared
                  ? ''
                  : html`<em>
                      The previous file could not be read, so every field in it
                      is listed rather than only the ones that moved.
                    </em>`}
              </div>
              ${shortcuts.map(
                (field) => html`<div class="save-summary-note">
                  ${field} can also be changed now, without a restart:
                  ${controls[field]}.
                </div>`,
              )}
            `
          : ''}
      </div>
    `;
  }

  /**
   * Restart, and the two sentences that make it a decision rather than a dare.
   *
   * Always rendered, not only after a save that needs it: a user who edited
   * `engine.json` in another editor has the same problem and no save on this
   * tab to hang the offer off. Session storage is the other half of
   * `specs5/5-webapp/settings.md` § Session Controls, and is below the note
   * rather than beside the button: it is a figure to read, not a control, and
   * the only thing it can be acted on with is in another surface.
   */
  _renderSessionControls() {
    const readOnly = this._isHost === false;
    const waiting = [...this._pendingFields].sort();
    return html`
      <div class="session-controls" role="group" aria-label="Session controls">
        <div class="session-head">
          <span class="session-title">🔄 Session</span>
          <button
            class="toolbar-button"
            @click=${() => this._restartSession()}
            ?disabled=${!this.rpcConnected || readOnly || this._restarting}
            title=${readOnly
              ? 'Only the host can restart the session'
              : 'Reconnect the engine on engine.json as it is on disk'}
          >
            ${this._restarting ? 'Restarting…' : '↻ Restart session'}
          </button>
        </div>
        <p class="session-note">
          ${readOnly
            ? html`Only the host can restart the session. `
            : ''}
          Every field in <code>engine.json</code> except the model and the
          permission mode is read when the CLI starts, so a restart is the only
          thing that applies one. The conversation is resumed, so the transcript
          stays — the cost totals start again.
          ${waiting.length
            ? html`<strong>Waiting to apply:</strong> ${joinFields(waiting)}.`
            : ''}
        </p>
        ${this._renderSessionStorage()}
      </div>
    `;
  }

  /**
   * What the mirrored transcripts cost, and where to spend less.
   *
   * Three renderings for the three answers the RPC can give, and the point of
   * keeping them apart is that two of them are not sizes. A run with no repo
   * has no `.aic-dc/` to measure and says so; a failed directory walk says
   * that instead of showing a zero. Nothing at all is rendered before the
   * first read lands — a card that flashed "0 B" on the way to the real figure
   * would be briefly wrong about the one thing it exists to report.
   *
   * `over_warning` arrives as the engine's verdict rather than a threshold to
   * compare against here, matching how the health banner is handed a
   * mirror-gap verdict. The number behind it is user-editable
   * (`history.session_dir_warning_bytes`), and a second copy of it in the
   * browser is a second answer waiting to disagree.
   */
  _renderSessionStorage() {
    // Ahead of the answer, because "this engine keeps no mirror here"
    // outranks any state of a figure it never produces. Hidden rather
    // than shown as "0 B" or "not mirrored": both of those are claims
    // about a directory, and a number on screen is believed (AG-9).
    if (!supports(SURFACE.SESSION_MIRROR)) return '';
    const storage = this._storage;
    if (!storage) return '';
    if (storage.error) {
      return html`<p class="session-note storage-note">
        <strong>Session storage:</strong> ${storage.reason === 'no_repo'
          ? 'not mirrored — this run has no repo directory, so the CLI\'s own'
            + ' transcript is the only copy.'
          : storage.error}
      </p>`;
    }
    const size = formatBytes(storage.bytes);
    if (!size) return '';
    return html`<p class="session-note storage-note">
      <strong>Session storage:</strong> ${size} in
      <code>.aic-dc/sessions/</code>.
      ${storage.over_warning
        ? html`<span class="storage-warn"
            >Past the size this repo asks to be warned at.</span
          >
          Pasted images are stored in the transcript itself, so a few
          image-heavy sessions usually account for most of it.`
        : ''}
      <button
        class="storage-link"
        @click=${this._browseHistory}
        title="Open the history browser, where sessions are deleted"
      >Browse history</button>
    </p>`;
  }

  /**
   * The model in force, what it resolves to, and a switch.
   *
   * Three lines rather than one because the alias and the model are different
   * facts and the spec's Info Banner asks for both — an alias like `opus` says
   * what was requested, `au.anthropic.claude-opus-5` says what will answer, and
   * they are allowed to disagree. The resolution is the CLI's own, from its
   * `models` list, never derived here.
   *
   * Above the card grid and outside it: every card below opens an editor on a
   * file that mostly applies next session, and this one control reaches the
   * session that is running. Putting it in the grid would file it under the
   * same "applies later" promise the rest of the tab has to make.
   */
  _renderModelPanel() {
    const entries = modelEntries(this._models, this._model);
    const alias = this._model || DEFAULT_MODEL_ALIAS;
    const entry = entries.find((e) => e.value === alias);
    const readOnly = this._isHost === false;
    const offline = entries.length === 0;
    const disabled =
      !this.rpcConnected || readOnly || this._modelPending || offline;
    const title = readOnly
      ? 'Only the host can change the model'
      : this._modelPending
        ? 'Waiting for the engine to confirm the switch'
        : entry?.detail || 'The model this session runs on';
    return html`
      <div
        class="model-panel ${this._modelFlash ? 'flash' : ''}"
        role="group"
        aria-label="Model"
      >
        <div class="model-head">
          <span class="model-title">🧠 Model</span>
          <select
            class="model-select"
            .value=${alias}
            ?disabled=${disabled}
            autocomplete="off"
            aria-label="Model"
            title=${title}
            @pointerdown=${() => this._noteModelGesture()}
            @keydown=${() => this._noteModelGesture()}
            @change=${(e) => this._onModelSelect(e)}
          >
            ${entries.map(
              (option) => html`<option
                value=${option.value}
                ?selected=${option.value === alias}
                title=${option.detail}
              >
                ${option.label}
              </option>`,
            )}
          </select>
          ${this._modelPending
            ? html`<span class="model-pending" aria-hidden="true">…</span>`
            : ''}
        </div>
        ${offline
          ? ''
          : html`
              <div class="model-resolved">
                ${alias} →
                ${this._resolved
                  || entry?.resolved
                  || '(the engine did not say what this resolves to)'}
              </div>
            `}
        ${this._renderModelNote(offline, readOnly)}
      </div>
    `;
  }

  /**
   * The sentences under the control, one per thing that is true.
   *
   * "From your next turn" is measured, not reasoned. A `set_model` fired 22.8s
   * into a live 34s turn answered in 252ms without interrupting it — and the
   * turn went on billing `claude-opus-5`, including a usage report 124ms
   * *after* the switch landed; the turn after it billed haiku. So the switch is
   * accepted mid-turn and applies to the next one, which is exactly the thing
   * somebody reaching for a cheaper model to rescue a runaway turn needs told.
   * Said unconditionally rather than only while streaming: it is true either
   * way, and a sentence that appears only sometimes is one nobody learns.
   */
  _renderModelNote(offline, readOnly) {
    if (offline) {
      return html`<p class="model-note">
        The engine has not connected yet, so it has not said which models it
        offers — the list arrives with the first turn.
      </p>`;
    }
    return html`
      <p class="model-note">
        ${readOnly
          ? html`Only the host can change the model. `
          : ''}
        ${this._model
          ? html`Set for this session rather than in a file: a switch here
              leaves <code>engine.json</code> alone and lasts as long as the
              session. `
          : html`<code>engine.json</code> pins no model, so the CLI uses its
              own default. `}
        A switch takes effect from your <strong>next</strong> turn — a turn
        already running finishes on the model it started with. Which model
        actually answered is reported per turn, by model, in the usage HUD.
      </p>
    `;
  }

  _renderEditor() {
    const card = CONFIG_CARDS.find((c) => c.key === this._activeKey);
    if (!card) return '';
    return html`
      <div class="editor-area ${this._editorFlash ? 'flash' : ''}">
        <div class="editor-toolbar">
          <span class="toolbar-label">
            ${card.icon} ${card.label}
          </span>
          ${card.reloadable
            ? html`
                <button
                  class="toolbar-button"
                  @click=${() => this._reload()}
                  ?disabled=${!this.rpcConnected}
                  title="Reload config from disk"
                >
                  ↻ Reload
                </button>
              `
            : ''}
          <button
            class="toolbar-button primary"
            @click=${this._save}
            ?disabled=${this._saving || !this.rpcConnected}
            title="Save (Ctrl+S)"
          >
            💾 Save
          </button>
          <button
            class="toolbar-button"
            @click=${this._closeEditor}
            title="Close editor"
          >
            ✕
          </button>
        </div>
        ${this._renderSaveSummary()}
        ${this._loading
          ? html`<div class="loading-note">Loading…</div>`
          : html`
              <textarea
                class="editor-textarea"
                .value=${this._editorContent}
                @keydown=${this._onEditorKeyDown}
                spellcheck="false"
              ></textarea>
            `}
      </div>
    `;
  }
}

customElements.define('aic-settings-tab', SettingsTab);