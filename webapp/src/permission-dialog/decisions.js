// The decision row.
//
// Three rules shape this file, the first two from
// specs5/5-webapp/permission-dialog.md § Decisions:
//
//   - Controls occupy fixed positions regardless of tool class, so
//     muscle memory targets the same button — while default focus moves
//     with risk, so muscle memory alone cannot approve a risky call.
//   - "Always allow" is labelled with the rule text and the file it will
//     be written to. A button labelled "always allow" with no rule text
//     is the promise the engine spec forbids.
//   - A mode switch gets its own button. `acceptEdits` stops this dialog
//     opening for every later edit in the session, which is a different
//     and much larger thing than a rule granting one path — so it cannot
//     share a control whose label speaks about this call.

import { html } from 'lit';

import {
  alwaysAllowTooltip,
  SHARED_RULE_TOOLTIP,
} from './constants.js';
import {
  answersComplete,
  describeMode,
  describeRule,
  offersAlwaysAllow,
} from './queue.js';

/**
 * The decision row for a request the caller may answer.
 *
 * @param {object} host — the aic-permission-dialog element
 * @param {object} payload
 */
export function renderDecisions(host, payload) {
  if (!host._canDecide) {
    // Non-localhost clients see the full body and no controls. The
    // restriction is on authority, not on information: a collaborator
    // who cannot see what the agent asked for cannot review what it did
    // (permission-dialog.md § Multiple Clients).
    return html`
      <div class="decisions">
        <span class="read-only-note">
          Only the host can answer this. You are seeing the request so you
          can follow what the agent is doing.
        </span>
      </div>
    `;
  }

  const settling = host._settling;
  const edited = host._hasEdits();
  const rules = payload.suggested_rules || [];
  const primaryRule = describeRule(rules[host._ruleIndex] || rules[0]);
  const mode = describeMode(payload);
  const interact = payload.tool_class === 'interact';
  const plan = payload.tool_class === 'plan';

  return html`
    <div class="decisions">
      ${offersAlwaysAllow(payload)
        ? html`
            <span class="menu-anchor">
              <span class="split">
                <button
                  class="decision"
                  data-decision="allow-always"
                  ?disabled=${settling}
                  title=${alwaysAllowTooltip(primaryRule)}
                  @click=${() => host._decide('allow_always')}
                >
                  <span class="rule-label">${primaryRule?.label ?? 'Always allow'}</span>
                  ${primaryRule?.destination
                    ? html`<span class="destination">→ ${primaryRule.destination}</span>`
                    : null}
                  ${primaryRule?.derived
                    ? html`<span class="derived-tag" title=${
                        'AIC-DC derived this pattern from the call. The CLI did '
                        + 'not suggest it, so check that it matches what you mean.'
                      }>derived</span>`
                    : null}
                  ${primaryRule?.shared
                    ? html`<span class="shared-tag" title=${SHARED_RULE_TOOLTIP}
                      >shared</span>`
                    : null}
                </button>
                ${rules.length > 1
                  ? html`
                      <button
                        class="decision caret"
                        aria-label="other suggested rules"
                        aria-haspopup="menu"
                        aria-expanded=${host._ruleMenuOpen ? 'true' : 'false'}
                        ?disabled=${settling}
                        @click=${() => host._toggleRuleMenu()}
                      >▾</button>
                    `
                  : null}
              </span>
              ${host._ruleMenuOpen
                ? html`
                    <div class="menu" role="menu">
                      ${rules.map((rule, index) => {
                        const described = describeRule(rule);
                        if (!described) return null;
                        return html`
                          <button
                            role="menuitem"
                            @click=${() => host._chooseRule(index)}
                          >
                            ${described.label}
                            <span class="destination">→ ${described.destination}</span>
                            ${described.derived
                              ? html`<span class="derived-tag">derived</span>`
                              : null}
                            ${described.shared
                              ? html`<span class="shared-tag" title=${SHARED_RULE_TOOLTIP}
                                >shared</span>`
                              : null}
                          </button>
                        `;
                      })}
                    </div>
                  `
                : null}
            </span>
          `
        : null}

      ${mode
        ? html`
            <button
              class="decision mode-switch"
              data-decision="allow-mode"
              ?disabled=${settling}
              title=${mode.detail}
              @click=${() => host._decide('allow_mode')}
            >
              <span class="rule-label">${mode.label}</span>
              ${mode.destination
                ? html`<span class="destination">→ ${mode.destination}</span>`
                : null}
            </button>
          `
        : null}

      <span class="menu-anchor">
        <span class="split">
          <button
            class="decision danger"
            data-decision="deny"
            ?disabled=${settling}
            title=${
              'Denies this call with a reason the agent can read. '
              + 'Escape does the same thing.'
            }
            @click=${() => host._openDeny(false)}
          >Deny</button>
          <button
            class="decision danger caret"
            aria-label="deny options"
            aria-haspopup="menu"
            aria-expanded=${host._denyMenuOpen ? 'true' : 'false'}
            ?disabled=${settling}
            @click=${() => host._toggleDenyMenu()}
          >▾</button>
        </span>
        ${host._denyMenuOpen
          ? html`
              <div class="menu" role="menu">
                <button role="menuitem" @click=${() => host._openDeny(true)}>
                  Deny and stop the turn
                </button>
              </div>
            `
          : null}
      </span>

      <span class="spacer">
        ${settling
          ? html`<span class="settling">reading…</span>`
          : null}
      </span>

      <button
        class="decision ${edited ? 'edited' : 'primary'}"
        data-decision="allow"
        ?disabled=${settling
          || (interact
            && !answersComplete(payload, host._answers, host._answerTexts))}
        @click=${() => host._decide('allow')}
      >
        ${interact
          ? 'Answer'
          : plan
            // Not "Allow once": what is being approved is the plan, and the
            // next thing that happens is the agent starting on it.
            ? 'Approve plan'
            : edited ? 'Allow with edits' : 'Allow once'}
      </button>
    </div>

    ${host._denyOpen
      ? html`
          <div class="reason-row">
            <input
              class="deny-reason"
              .value=${host._denyReason}
              placeholder="why not? the agent reads this"
              aria-label="reason for denying"
              @input=${(event) => host._onDenyReasonInput(event)}
              @keydown=${(event) => host._onDenyReasonKeydown(event)}
            />
            <button
              class="decision danger"
              @click=${() => host._decide(
                host._denyInterrupts ? 'deny_interrupt' : 'deny',
              )}
            >
              ${host._denyInterrupts ? 'Deny and stop' : 'Send denial'}
            </button>
          </div>
        `
      : null}
  `;
}
