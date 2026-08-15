// Body renderers, one per tool class.
//
// The rule the whole file serves: what the user sees is the
// *consequence*, not a tool name. A Monaco diff for an edit, the exact
// command and working directory for a shell call, the question and its
// options for an interactive tool
// (specs5/5-webapp/permission-dialog.md § Body by Tool Class).
//
// Every renderer takes the host so it can read edit state and bind
// handlers; none of them own state.

import { html } from 'lit';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';

import { renderMarkdown } from '../markdown.js';
import {
  CLASS_LABELS,
  FLAG_TOOLTIPS,
  OTHER_ANSWER_PLACEHOLDER,
} from './constants.js';
import { interactQuestions } from './queue.js';

const NO_SELECTION = new Set();

/** Formatted JSON, or a short marker when the value will not stringify. */
export function formatJson(value) {
  try {
    return JSON.stringify(value ?? null, null, 2);
  } catch (err) {
    return `(could not be formatted: ${err?.message || err})`;
  }
}

/**
 * `write` — the diff is the feature.
 *
 * The Monaco instance is created imperatively against `.diff-host` by
 * diff-editor.js once the container is in the DOM; this only renders the
 * container, or the explicit label for each case where a diff is
 * impossible. It never falls back to a tool name and a JSON blob — that
 * fallback is what the invariant exists to forbid.
 */
export function renderWriteBody(host, payload) {
  const diff = payload.diff;
  if (!diff) {
    return html`
      <div class="not-shown">
        AC-DC could not work out which file this call targets. The
        verbatim input is below — read it before deciding.
      </div>
    `;
  }
  if (diff.is_binary) {
    return html`
      <div class="not-shown">
        <div><strong>${diff.path}</strong></div>
        <div>binary — cannot be shown</div>
      </div>
    `;
  }
  if (diff.too_large) {
    return html`
      <div class="not-shown">
        <div><strong>${diff.path}</strong></div>
        <div>too large to diff</div>
        <div>
          The verbatim input is in the disclosure below. Nothing has been
          truncated silently.
        </div>
      </div>
    `;
  }
  if (diff.is_new_file) {
    // A single pane of the full proposed content. An empty diff against
    // nothing tells the user less than the file they are about to create.
    return html`
      <span class="label">new file — full proposed content</span>
      <pre class="new-file-pane">${diff.proposed ?? ''}</pre>
    `;
  }
  if (diff.proposed == null) {
    return html`
      <div class="not-shown">
        <div><strong>${diff.path}</strong></div>
        <div>
          AC-DC could not compute the proposed result — the replacement
          text does not match the file on disk.
        </div>
        <div>
          Read the verbatim input below. Showing a guessed diff would show
          you a change the agent is not asking for.
        </div>
      </div>
    `;
  }
  return html`<div class="diff-host"></div>`;
}

/**
 * `exec` — the command, verbatim.
 *
 * Monospace, unwrapped, horizontally scrollable, never re-formatted:
 * what is shown is exactly what will run.
 */
export function renderExecBody(host, payload) {
  const command = payload.command || {};
  const editing = host._editingCommand;
  return html`
    <span class="label">command</span>
    ${editing
      ? html`
          <input
            class="command-edit"
            .value=${host._commandDraft}
            spellcheck="false"
            @input=${(event) => host._onCommandInput(event)}
          />
        `
      : html`<pre class="command">${command.command ?? ''}</pre>`}
    ${command.truncated
      ? html`
          <details class="full-input">
            <summary>full command (${
              (payload.input?.command || '').length
            } characters)</summary>
            <pre class="json">${payload.input?.command ?? ''}</pre>
          </details>
        `
      : null}

    <span class="label">working directory</span>
    <div class="cwd">${command.cwd ?? ''}</div>

    ${command.description
      ? html`
          <span class="label">description</span>
          <div class="agent-description">${command.description}</div>
        `
      : null}

    ${Array.isArray(command.flags) && command.flags.length
      ? html`
          <div class="chips">
            ${command.flags.map((flag) => html`
              <span class="chip ${flag}" title=${FLAG_TOOLTIPS[flag] || 'Heuristic.'}>
                ${flag}
              </span>
            `)}
          </div>
        `
      : null}
  `;
}

/**
 * `plan` — the plan, rendered.
 *
 * `ExitPlanMode` asks the user to approve a plan written as markdown, so
 * the body renders it as markdown. Before this renderer existed the tool
 * fell through `classify_tool`'s unknown-name path to `exec` and the plan
 * arrived as a summarised blob truncated at 4000 characters — a body that
 * asked for approval of something it was not showing, which is the
 * fallback the write renderer's comment forbids for the same reason.
 *
 * Markdown here is the same trust call the chat panel makes: the content
 * is the model's, `marked` escapes HTML by default, and the alternative
 * is a wall of `##` and `-` for the one artefact the user has to read
 * carefully.
 */
export function renderPlanBody(host, payload) {
  const plan = payload.plan;
  if (!plan?.plan) {
    return html`
      <div class="not-shown">
        <div>This call carries no plan text.</div>
        <div>
          The CLI injects the plan from disk, so an absent one usually
          means the file could not be read. The verbatim input is below —
          read it before approving.
        </div>
      </div>
    `;
  }
  return html`
    <span class="label">proposed plan</span>
    <div class="plan markdown">${unsafeHTML(renderMarkdown(plan.plan))}</div>
    ${plan.file_path
      ? html`
          <span class="label">saved at</span>
          <div class="cwd">${plan.file_path}</div>
        `
      : null}
  `;
}

/**
 * `interact` — real choices.
 *
 * The options are selectable controls, not a JSON dump of a question the
 * user then answers in prose somewhere else. Every question in the call
 * is rendered, each with its own control group, because the agent is
 * waiting on all of them and a dialog that shows the first alone leaves
 * the rest silently unanswered.
 *
 * Each question also gets a freeform reply, because the terminal's own
 * question UI always does — the tool tells the model not to write an
 * "Other" option because the front end provides it. Without one, a user
 * whose answer is none of the options has to deny the call and start
 * again in prose.
 *
 * It is a plain field rather than an "Other" radio: typing an answer and
 * picking an option are mutually exclusive for a single-select question,
 * and one control that clears the other says so without a third state
 * that can be checked-but-empty.
 */
export function renderInteractBody(host, payload) {
  const questions = interactQuestions(payload);
  if (!questions.length) {
    return html`<pre class="json">${formatJson(payload.input)}</pre>`;
  }
  return html`
    ${questions.map((question, questionIndex) => {
      const multi = question.multi_select;
      const chosen = host._answers.get(questionIndex) || NO_SELECTION;
      const typed = host._answerTexts.get(questionIndex) || '';
      return html`
        <div class="question-group">
          ${question.header
            ? html`<span class="question-header">${question.header}</span>`
            : null}
          <p class="question">${question.question}</p>
          <div
            class="options"
            role=${multi ? 'group' : 'radiogroup'}
            data-question=${questionIndex}
          >
            ${(question.options || []).map((option, index) => html`
              <label class="option">
                <input
                  type=${multi ? 'checkbox' : 'radio'}
                  name="permission-answer-${questionIndex}"
                  .checked=${chosen.has(index)}
                  @change=${(event) =>
                    host._onOptionToggle(questionIndex, index, event, multi)}
                />
                <span>
                  <span class="option-label">${option.label}</span>
                  ${option.description
                    ? html`<br /><span class="option-description">${option.description}</span>`
                    : null}
                </span>
              </label>
            `)}
          </div>
          <input
            class="other-answer"
            data-question=${questionIndex}
            .value=${typed}
            placeholder=${OTHER_ANSWER_PLACEHOLDER}
            aria-label="your own answer to: ${question.question}"
            spellcheck="true"
            @input=${(event) =>
              host._onAnswerTextInput(questionIndex, event, multi)}
          />
          ${typed.trim() && !multi
            ? html`<span class="other-note">
                Sent instead of the options above.
              </span>`
            : null}
          ${typed.trim() && multi
            ? html`<span class="other-note">
                Sent alongside anything ticked above.
              </span>`
            : null}
        </div>
      `;
    })}
  `;
}

/**
 * `mcp` — third-party tools.
 *
 * Server and tool name get equal prominence, because "which server is
 * this?" is the security-relevant question. The full input goes in the
 * body rather than behind the disclosure: there is no meaningful summary
 * view for an arbitrary third-party tool and inventing one would hide
 * the payload.
 */
export function renderMcpBody(host, payload) {
  return html`
    <span class="label">server</span>
    <div class="cwd">${payload.server ?? '(unnamed)'}</div>
    <span class="label">tool</span>
    <div class="cwd">${payload.tool_name}</div>
    ${payload.description
      ? html`
          <span class="label">description</span>
          <div class="agent-description">${payload.description}</div>
        `
      : null}
    <span class="label">input</span>
    <pre class="json">${formatJson(payload.input)}</pre>
  `;
}

/**
 * `read` and `delegate` — normally absent.
 *
 * These classes are ungated, so a dialog for one means something
 * specific happened. The body says so, because a prompt that looked
 * routine here is exactly the click-through trainer the tiering exists
 * to avoid.
 */
export function renderUngatedBody(host, payload) {
  const label = CLASS_LABELS[payload.tool_class] || payload.tool_class;
  const reason = payload.decision_reason;
  const explanation = reason?.reason
    || reason?.message
    || (payload.blocked_path
      ? `${payload.blocked_path} is outside the directories this session may touch.`
      : 'A deny or ask rule matched, or a hook asked for confirmation.');
  return html`
    <div class="why">
      ${label} calls are not normally gated. This one is:
      ${explanation}
    </div>
    ${payload.tool_class === 'delegate'
      ? html`
          <span class="label">subagent prompt</span>
          <pre class="json">${payload.input?.prompt ?? formatJson(payload.input)}</pre>
        `
      : html`
          <span class="label">path</span>
          <div class="cwd">
            ${payload.input?.file_path
              || payload.input?.path
              || payload.blocked_path
              || '(none named)'}
          </div>
          <span class="label">input</span>
          <pre class="json">${formatJson(payload.input)}</pre>
        `}
  `;
}

/** Dispatch on `tool_class`, defaulting to the most explicit renderer. */
export function renderBody(host, payload) {
  switch (payload.tool_class) {
    case 'write': return renderWriteBody(host, payload);
    case 'exec': return renderExecBody(host, payload);
    case 'plan': return renderPlanBody(host, payload);
    case 'interact': return renderInteractBody(host, payload);
    case 'mcp': return renderMcpBody(host, payload);
    case 'read':
    case 'delegate': return renderUngatedBody(host, payload);
    default:
      // An unknown class means the backend classified something we have
      // no renderer for. Show everything rather than nothing.
      return html`<pre class="json">${formatJson(payload.input)}</pre>`;
  }
}
