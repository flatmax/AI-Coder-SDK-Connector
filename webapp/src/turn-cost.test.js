// Tests for turn-cost.js — what a turn cost, as against what the session has.
//
// The module exists because two components read the engine's cumulative
// `total_cost_usd` / `model_usage` as if they were per-turn, and got it wrong
// in different ways. Most of what follows is about the difference between
// those two scopes, and about the three answers when the per-turn one cannot
// be recovered. Its server-side counterpart is tests/test_claude_code_cost.py.
//
// `totalTokens`, `usageLines` and `formatCost` used to live in
// chat-panel/block-render.js; their coverage came with them.

import { describe, expect, it } from 'vitest';

import {
  MEASURED,
  RESET,
  UNPRICED,
  UNRECORDED,
  costLabel,
  formatCost,
  modelNames,
  modelUsageLines,
  reportsUsage,
  turnCost,
  turnTokens,
} from './turn-cost.js';

/** A priced `streamComplete` payload, both scopes present and different. */
function result(overrides = {}) {
  return {
    response: 'done',
    is_error: false,
    tool_calls: 3,
    total_cost_usd: 1.2,
    model_usage: { 'claude-opus-5': { inputTokens: 4000, costUSD: 1.2 } },
    turn_cost_usd: 0.0342,
    turn_cost_basis: MEASURED,
    turn_model_usage: { 'claude-opus-5': { inputTokens: 100, costUSD: 0.0342 } },
    ...overrides,
  };
}

describe('turnTokens', () => {
  it('is zero for a usage dict that is absent or empty', () => {
    expect(turnTokens(null)).toBe(0);
    expect(turnTokens(undefined)).toBe(0);
    expect(turnTokens('12000')).toBe(0);
    expect(turnTokens({})).toBe(0);
  });

  it('counts cache traffic as tokens the turn moved', () => {
    // Omitting cache reads would report a large cached turn as a tiny one,
    // which is the opposite of what a usage line is for.
    expect(turnTokens({
      input_tokens: 100,
      output_tokens: 200,
      cache_creation_input_tokens: 1000,
      cache_read_input_tokens: 40_000,
    })).toBe(41_300);
  });

  it('reads the engine’s camelCase as well as a transcript’s snake_case', () => {
    // The two spellings are the CLI's own: camelCase on the wire, snake_case
    // in the transcript on disk. Knowing only one is how live turns came to
    // render no usage at all while replayed ones rendered fine.
    expect(turnTokens({
      inputTokens: 100,
      outputTokens: 200,
      cacheCreationInputTokens: 1000,
      cacheReadInputTokens: 40_000,
    })).toBe(41_300);
  });

  it('counts a mixed-spelling entry once per counter', () => {
    expect(turnTokens({ inputTokens: 100, input_tokens: 100 })).toBe(100);
  });

  it('ignores junk fields rather than propagating NaN', () => {
    expect(turnTokens({
      input_tokens: 100,
      output_tokens: '200',
      cache_read_input_tokens: -5,
      web_search_requests: 3,
    })).toBe(100);
  });
});

describe('turnCost', () => {
  it('reads the per-turn figure, never the session total', () => {
    expect(turnCost(result())).toEqual({ usd: 0.0342, basis: MEASURED });
  });

  it('keeps a measured zero as a number', () => {
    // Zero is the answer "this turn cost nothing extra". Collapsing it to
    // null would make it indistinguishable from "we do not know".
    expect(turnCost(result({ turn_cost_usd: 0 })))
      .toEqual({ usd: 0, basis: MEASURED });
  });

  it('passes the reason through when there is no figure', () => {
    for (const basis of [RESET, UNPRICED]) {
      expect(turnCost(result({ turn_cost_usd: null, turn_cost_basis: basis })))
        .toEqual({ usd: null, basis });
    }
  });

  it('is unrecorded when the payload says nothing about cost', () => {
    // A browsed turn, or a payload from before the engine priced turns.
    const browsed = result();
    delete browsed.turn_cost_usd;
    delete browsed.turn_cost_basis;
    expect(turnCost(browsed)).toEqual({ usd: null, basis: UNRECORDED });
    expect(turnCost(null)).toEqual({ usd: null, basis: UNRECORDED });
    expect(turnCost('nope')).toEqual({ usd: null, basis: UNRECORDED });
  });

  it('does not invent a basis of its own devising', () => {
    expect(turnCost(result({ turn_cost_basis: 'cheap' })).basis).toBe(UNRECORDED);
  });

  it('trusts the number over the label when they contradict', () => {
    // Printing nothing beats printing a figure that cannot be sourced.
    for (const junk of [null, undefined, NaN, '0.42', true, -1]) {
      expect(turnCost(result({ turn_cost_usd: junk })))
        .toEqual({ usd: null, basis: UNPRICED });
    }
  });
});

describe('formatCost', () => {
  it('is null when there is no cost to format', () => {
    for (const junk of [null, undefined, NaN, '0.42', -1, Infinity, true]) {
      expect(formatCost(junk)).toBeNull();
    }
  });

  it('keeps four decimals up to fifty cents', () => {
    // Two would render nearly every turn as "$0.00", which reads as free
    // rather than as small. The cut-over is the CLI's own.
    expect(formatCost(0.0004)).toBe('$0.0004');
    expect(formatCost(0.009)).toBe('$0.0090');
    expect(formatCost(0.5)).toBe('$0.5000');
  });

  it('drops to two decimals above fifty cents', () => {
    expect(formatCost(0.51)).toBe('$0.51');
    expect(formatCost(1.239)).toBe('$1.24');
    expect(formatCost(12)).toBe('$12.00');
  });

  it('formats a real zero, leaving the wording to costLabel', () => {
    expect(formatCost(0)).toBe('$0.0000');
  });
});

describe('costLabel', () => {
  it('prices a turn that spent something', () => {
    const label = costLabel(result());
    expect(label.text).toBe('$0.0342');
    expect(label.known).toBe(true);
  });

  it('names the session total in the tooltip, where it belongs', () => {
    // The running total is worth showing — it was just never this turn's.
    const label = costLabel(result({ turn_cost_usd: 0.0342, total_cost_usd: 1.2 }));
    expect(label.title).toContain('$1.20');
    expect(label.title).toContain('estimate');
  });

  it('says a turn cost nothing extra rather than printing $0', () => {
    const label = costLabel(result({ turn_cost_usd: 0 }));
    expect(label.text).toBe('nothing extra');
    expect(label.known).toBe(true);
    // And makes no claim about the billing plan, which is what the old
    // wording ("included") did.
    expect(label.title).toMatch(/not a claim about your billing plan/i);
  });

  it('says the cost is unknown, with the reason, for a reset', () => {
    const label = costLabel(result({ turn_cost_usd: null, turn_cost_basis: RESET }));
    expect(label.text).toBe('cost unknown');
    expect(label.known).toBe(false);
    expect(label.title).toContain('/clear');
  });

  it('says the cost is unknown, with the reason, for an unpriced turn', () => {
    const label = costLabel(result({ turn_cost_usd: null, turn_cost_basis: UNPRICED }));
    expect(label.text).toBe('cost unknown');
    expect(label.known).toBe(false);
    expect(label.title).toMatch(/lands on the next turn/);
  });

  it('is silent for a turn that never recorded a cost', () => {
    const browsed = result();
    delete browsed.turn_cost_usd;
    delete browsed.turn_cost_basis;
    expect(costLabel(browsed)).toBeNull();
  });

  it('never says "included"', () => {
    // It asserted a billing mode the payload knows nothing about; the only
    // real signal is `credential_source` on the engine-health record.
    for (const basis of [MEASURED, RESET, UNPRICED]) {
      for (const usd of [0, 0.5, null]) {
        const label = costLabel(result({ turn_cost_basis: basis, turn_cost_usd: usd }));
        expect(label?.text ?? '').not.toContain('included');
      }
    }
  });
});

describe('modelUsageLines', () => {
  it('is empty for no per-turn usage at all', () => {
    expect(modelUsageLines(null)).toEqual([]);
    expect(modelUsageLines({})).toEqual([]);
    expect(modelUsageLines({ turn_model_usage: 'claude' })).toEqual([]);
  });

  it('never falls back to the session’s cumulative map', () => {
    // That fallback is the bug: `model_usage` grows all session, so the line
    // would report the session's tokens under this turn's label.
    expect(modelUsageLines({
      model_usage: { 'claude-opus-5': { inputTokens: 400_000 } },
    })).toEqual([]);
  });

  it('reports tokens and cost per model', () => {
    expect(modelUsageLines({
      turn_model_usage: {
        'claude-opus-5': { inputTokens: 900, outputTokens: 100, costUSD: 0.02 },
      },
    })).toEqual([{ model: 'claude-opus-5', tokens: 1000, usd: 0.02 }]);
  });

  it('prefers the canonical model name over a provider-specific key', () => {
    // On Bedrock and Vertex the key is an id like this. `canonicalModel` is a
    // field the schema actually has, unlike the `modelName` this code used to
    // reach for.
    expect(modelUsageLines({
      turn_model_usage: {
        'us.anthropic.claude-opus-5-v1:0': {
          inputTokens: 50,
          canonicalModel: 'claude-opus-5',
        },
      },
    })[0].model).toBe('claude-opus-5');
  });

  it('puts the busiest model first', () => {
    // So a "+n" built from this hides the subagents, not the main model.
    expect(modelNames({
      turn_model_usage: {
        'claude-haiku-4-5': { inputTokens: 40 },
        'claude-opus-5': { inputTokens: 900 },
      },
    })).toEqual(['claude-opus-5', 'claude-haiku-4-5']);
  });

  it('drops entries with nothing to report', () => {
    expect(modelUsageLines({
      turn_model_usage: {
        'claude-haiku-4-5': { inputTokens: 0, outputTokens: 0 },
        'claude-opus-5': null,
        'claude-sonnet-5': 'nope',
      },
    })).toEqual([]);
  });

  it('reports no cost rather than a bad one', () => {
    expect(modelUsageLines({
      turn_model_usage: { m: { inputTokens: 10, costUSD: '0.02' } },
    })[0].usd).toBeNull();
  });
});

describe('reportsUsage', () => {
  it('is true for a turn with a measured cost', () => {
    expect(reportsUsage(result())).toBe(true);
  });

  it('is true for a turn with tokens but no price', () => {
    expect(reportsUsage({
      turn_cost_basis: UNPRICED,
      turn_model_usage: { m: { inputTokens: 400 } },
    })).toBe(true);
  });

  it('is true for a crash footer once the turn had done work', () => {
    // The footer is AC⚡DC's own, so it carries no usage whatsoever; that the
    // turn ran tools or answered is the only evidence it spent money.
    expect(reportsUsage({ turn_cost_basis: UNPRICED, tool_calls: 4 })).toBe(true);
    expect(reportsUsage({ turn_cost_basis: UNPRICED, response: 'partial' })).toBe(true);
  });

  it('is false for a turn that died before doing anything', () => {
    // Nothing ran, nothing was said, nothing was priced — there is no number
    // to show, and the error is already on screen twice.
    expect(reportsUsage({
      is_error: true,
      turn_cost_usd: null,
      turn_cost_basis: UNPRICED,
      turn_model_usage: null,
      response: '',
      tool_calls: 0,
    })).toBe(false);
    expect(reportsUsage(null)).toBe(false);
  });

  it('is false for a measured zero with nothing else to show', () => {
    // "$0.00 · 0.0s" is not worth an overlay.
    expect(reportsUsage({
      turn_cost_usd: 0,
      turn_cost_basis: MEASURED,
      response: '  ',
      tool_calls: 0,
    })).toBe(false);
  });
});
