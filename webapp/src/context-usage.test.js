// Tests for webapp/src/context-usage.js — the shared derivations over
// the engine's ContextUsageResponse.
//
// The fixture is a verbatim capture from a live opus-5 session rather
// than a hand-written shape. That matters: the previous round of tests
// for the Context tab and the usage HUD used plausible fixtures with
// hex colours and a `maxTokens` reduced by the autocompact buffer.
// Both assumptions were wrong, both suites passed, and the app was
// rendering transparent bars the whole time. Anything asserting on
// category colour or window arithmetic starts from this object.

import { describe, expect, it } from 'vitest';
import {
  UNCOLOURED,
  bandColor,
  categoryColor,
  compactionLimit,
  compactionPercent,
  mcpHealth,
  messageComposition,
  overLimit,
  partitionCategories,
  serverGroups,
  skillInventory,
  sourceLabel,
  thresholdPercent,
  windowPercent,
} from './context-usage.js';

/**
 * A real payload, captured from `ClaudeCodeService.get_context_usage`
 * against a live CLI. Trimmed of the sections this module does not
 * read (gridRows, memoryFiles, mcpTools, skills, slashCommands,
 * messageBreakdown, apiUsage) but otherwise unedited.
 */
function livePayload() {
  return {
    categories: [
      { name: 'System prompt', tokens: 3371, color: 'promptBorder' },
      { name: 'System tools', tokens: 13315, color: 'inactive' },
      {
        name: 'MCP tools (deferred)',
        tokens: 9182,
        color: 'inactive',
        isDeferred: true,
      },
      {
        name: 'System tools (deferred)',
        tokens: 10427,
        color: 'inactive',
        isDeferred: true,
      },
      { name: 'Memory files', tokens: 27, color: 'claude' },
      { name: 'Skills', tokens: 1469, color: 'warning' },
      {
        name: 'Messages',
        tokens: 2905,
        color: 'purple_FOR_SUBAGENTS_ONLY',
      },
      { name: 'Autocompact buffer', tokens: 33000, color: 'inactive' },
      { name: 'Free space', tokens: 145913, color: 'promptBorder' },
    ],
    totalTokens: 21087,
    maxTokens: 200000,
    rawMaxTokens: 200000,
    autocompactSource: 'model-default',
    percentage: 11,
    model: 'au.anthropic.claude-opus-5',
    autoCompactThreshold: 167000,
    isAutoCompactEnabled: true,
  };
}

/**
 * A `messageBreakdown`, and the one fixture here that is not a capture.
 *
 * The live session that produced `livePayload` had 2905 tokens of
 * history and the response carried no breakdown for it, so this is built
 * from the wire schema the `claude` binary validates the field against —
 * `{toolCallTokens, toolResultTokens, attachmentTokens,
 * assistantMessageTokens, userMessageTokens, redirectedContextTokens,
 * unattributedTokens, toolCallsByType, attachmentsByType}`. Not a guess,
 * but not an observation either, which is why it is labelled.
 *
 * The seven part counts sum to 2905, matching `livePayload`'s Messages
 * category, because that is the engine's own arithmetic: it derives
 * `unattributedTokens` as the category minus the other six.
 */
function messageBreakdown(overrides = {}) {
  return {
    userMessageTokens: 400,
    assistantMessageTokens: 900,
    toolCallTokens: 300,
    toolResultTokens: 1200,
    attachmentTokens: 100,
    redirectedContextTokens: 0,
    unattributedTokens: 5,
    toolCallsByType: [
      { name: 'Read', callTokens: 120, resultTokens: 800 },
      { name: 'Bash', callTokens: 180, resultTokens: 400 },
    ],
    attachmentsByType: [{ name: 'pasted_text', tokens: 100 }],
    ...overrides,
  };
}

describe('context-usage derivations', () => {
  // -----------------------------------------------------------------
  // The identities that make the name-based partition checkable.
  // If a future SDK breaks one of these, this is the test that says so.
  // -----------------------------------------------------------------

  describe('live payload identities', () => {
    const u = livePayload();
    const byName = (n) => u.categories.find((c) => c.name === n).tokens;

    it('content categories sum to totalTokens', () => {
      const content = u.categories.filter(
        (c) => !c.isDeferred
          && c.name !== 'Free space'
          && c.name !== 'Autocompact buffer',
      );
      const sum = content.reduce((s, c) => s + c.tokens, 0);
      expect(sum).toBe(u.totalTokens);
    });

    it('free space is measured to the autocompact threshold', () => {
      expect(byName('Free space'))
        .toBe(u.autoCompactThreshold - u.totalTokens);
    });

    it('the autocompact buffer is the reserve above the threshold', () => {
      expect(byName('Autocompact buffer'))
        .toBe(u.maxTokens - u.autoCompactThreshold);
    });

    it('non-deferred categories tile the whole window', () => {
      const sum = u.categories
        .filter((c) => !c.isDeferred)
        .reduce((s, c) => s + c.tokens, 0);
      expect(sum).toBe(u.maxTokens);
    });

    it('maxTokens is NOT reduced by the autocompact buffer', () => {
      // The assumption three components were built on. Kept as an
      // explicit assertion so nobody reintroduces it from the docs.
      expect(u.maxTokens).toBe(u.rawMaxTokens);
      expect(u.maxTokens).toBeGreaterThan(u.autoCompactThreshold);
    });
  });

  // -----------------------------------------------------------------
  // categoryColor
  // -----------------------------------------------------------------

  describe('categoryColor', () => {
    it('maps every theme token in the live payload to CSS', () => {
      for (const c of livePayload().categories) {
        const resolved = categoryColor(c.color);
        expect(resolved, `token ${c.color}`).toMatch(/^#[0-9a-f]{6}$/i);
      }
    });

    it('maps the tokens observed live to distinct hues where the engine does', () => {
      expect(categoryColor('claude')).not.toBe(categoryColor('warning'));
      expect(categoryColor('promptBorder'))
        .not.toBe(categoryColor('inactive'));
      expect(categoryColor('purple_FOR_SUBAGENTS_ONLY'))
        .not.toBe(categoryColor('claude'));
    });

    it('preserves the engine\'s own promptBorder collision', () => {
      // System prompt and Free space share a token upstream. Faithful
      // beats pretty — a swatch that disagrees with /context is worse.
      const u = livePayload();
      const prompt = u.categories.find((c) => c.name === 'System prompt');
      const free = u.categories.find((c) => c.name === 'Free space');
      expect(prompt.color).toBe(free.color);
      expect(categoryColor(prompt.color)).toBe(categoryColor(free.color));
    });

    it('passes hex through untouched', () => {
      expect(categoryColor('#ff0000')).toBe('#ff0000');
      expect(categoryColor('#f00')).toBe('#f00');
      expect(categoryColor('#ff0000cc')).toBe('#ff0000cc');
    });

    it('passes rgb() and hsl() through untouched', () => {
      expect(categoryColor('rgb(1, 2, 3)')).toBe('rgb(1, 2, 3)');
      expect(categoryColor('rgba(1, 2, 3, 0.5)')).toBe('rgba(1, 2, 3, 0.5)');
      expect(categoryColor('hsl(200 50% 50%)')).toBe('hsl(200 50% 50%)');
    });

    it('trims surrounding whitespace', () => {
      expect(categoryColor('  claude  ')).toBe(categoryColor('claude'));
    });

    it('colours the two rows the live capture could not show', () => {
      // The session behind `livePayload` had every MCP tool deferred and
      // no custom agents, so "MCP tools" and "Custom agents" never
      // appeared as content rows. Their tokens come from the CLI's own
      // category builder; grey here would put the row naming our bridge
      // in the uncoloured bucket.
      expect(categoryColor('cyan_FOR_SUBAGENTS_ONLY')).not.toBe(UNCOLOURED);
      expect(categoryColor('permission')).not.toBe(UNCOLOURED);
      expect(categoryColor('cyan_FOR_SUBAGENTS_ONLY'))
        .not.toBe(categoryColor('permission'));
    });

    it('falls back to grey for an unknown token', () => {
      expect(categoryColor('someFutureToken')).toBe(UNCOLOURED);
    });

    it('falls back to grey for junk', () => {
      for (const junk of [undefined, null, '', '   ', 0, 42, {}, []]) {
        expect(categoryColor(junk)).toBe(UNCOLOURED);
      }
    });

    it('never returns an empty string', () => {
      // The value goes straight into a style attribute; an empty
      // background is the transparent-segment bug all over again.
      for (const v of [undefined, null, '', 'nope', 'claude', '#abc']) {
        expect(categoryColor(v).length).toBeGreaterThan(0);
      }
    });
  });

  // -----------------------------------------------------------------
  // partitionCategories
  // -----------------------------------------------------------------

  describe('partitionCategories', () => {
    it('splits the live payload three ways', () => {
      const { content, deferred, structural } = partitionCategories(
        livePayload(),
      );
      expect(content.map((c) => c.name)).toEqual([
        'System prompt',
        'System tools',
        'Memory files',
        'Skills',
        'Messages',
      ]);
      expect(deferred.map((c) => c.name)).toEqual([
        'MCP tools (deferred)',
        'System tools (deferred)',
      ]);
      expect(structural.map((c) => c.name)).toEqual([
        'Autocompact buffer',
        'Free space',
      ]);
    });

    it('reports contentTokens equal to totalTokens, and verifies', () => {
      const { contentTokens, verified } = partitionCategories(livePayload());
      expect(contentTokens).toBe(21087);
      expect(verified).toBe(true);
    });

    it('preserves the engine\'s category order within each list', () => {
      const { content } = partitionCategories(livePayload());
      expect(content[0].name).toBe('System prompt');
      expect(content[content.length - 1].name).toBe('Messages');
    });

    it('matches structural names case- and space-insensitively', () => {
      const u = {
        totalTokens: 10,
        categories: [
          { name: 'Messages', tokens: 10 },
          { name: '  FREE SPACE ', tokens: 90 },
          { name: 'autocompact BUFFER', tokens: 33 },
        ],
      };
      const { content, structural } = partitionCategories(u);
      expect(content.map((c) => c.name)).toEqual(['Messages']);
      expect(structural).toHaveLength(2);
    });

    it('treats the autocompact-off reserve as structural too', () => {
      // With autocompact off the engine still holds tokens back and
      // names the row "Compact buffer". That name was missing from the
      // structural set, so it counted as content, the sum overshot
      // `totalTokens`, and every autocompact-off session lost its
      // segmented bar to the degrade path.
      const { content, structural, verified } = partitionCategories({
        totalTokens: 21087,
        maxTokens: 200000,
        isAutoCompactEnabled: false,
        categories: [
          { name: 'Messages', tokens: 21087 },
          { name: 'Compact buffer', tokens: 33000 },
          { name: 'Free space', tokens: 145913 },
        ],
      });
      expect(content.map((c) => c.name)).toEqual(['Messages']);
      expect(structural.map((c) => c.name))
        .toEqual(['Compact buffer', 'Free space']);
      expect(verified).toBe(true);
    });

    it('treats a deferred row as deferred even if named structurally', () => {
      const { deferred, structural } = partitionCategories({
        totalTokens: 1,
        categories: [
          { name: 'Messages', tokens: 1 },
          { name: 'Free space', tokens: 5, isDeferred: true },
        ],
      });
      expect(deferred).toHaveLength(1);
      expect(structural).toHaveLength(0);
    });

    it('drops zero and negative token rows', () => {
      const { content, contentTokens } = partitionCategories({
        totalTokens: 5,
        categories: [
          { name: 'Messages', tokens: 5 },
          { name: 'Empty', tokens: 0 },
          { name: 'Weird', tokens: -3 },
          { name: 'Missing' },
        ],
      });
      expect(content.map((c) => c.name)).toEqual(['Messages']);
      expect(contentTokens).toBe(5);
    });

    it('does not verify when the content sum misses totalTokens', () => {
      // The signal a caller uses to fall back to an unsegmented bar,
      // e.g. after the engine renames "Free space".
      const { verified } = partitionCategories({
        totalTokens: 21087,
        categories: [
          { name: 'Messages', tokens: 2905 },
          { name: 'Headroom', tokens: 145913 },
        ],
      });
      expect(verified).toBe(false);
    });

    it('verifies within a one percent tolerance', () => {
      const { verified } = partitionCategories({
        totalTokens: 10000,
        categories: [{ name: 'Messages', tokens: 9950 }],
      });
      expect(verified).toBe(true);
    });

    it('does not verify beyond the tolerance', () => {
      const { verified } = partitionCategories({
        totalTokens: 10000,
        categories: [{ name: 'Messages', tokens: 9000 }],
      });
      expect(verified).toBe(false);
    });

    it('does not verify without a usable totalTokens', () => {
      for (const totalTokens of [undefined, null, 0, -1, 'x', NaN]) {
        expect(partitionCategories({
          totalTokens,
          categories: [{ name: 'Messages', tokens: 5 }],
        }).verified).toBe(false);
      }
    });

    it('survives junk payloads', () => {
      for (const u of [undefined, null, {}, { categories: null }, { categories: 'x' }]) {
        const r = partitionCategories(u);
        expect(r.content).toEqual([]);
        expect(r.deferred).toEqual([]);
        expect(r.structural).toEqual([]);
        expect(r.contentTokens).toBe(0);
        expect(r.verified).toBe(false);
      }
    });

    it('skips non-object entries', () => {
      const { content } = partitionCategories({
        totalTokens: 5,
        categories: [null, 'nope', 7, { name: 'Messages', tokens: 5 }],
      });
      expect(content).toHaveLength(1);
    });
  });

  // -----------------------------------------------------------------
  // compactionLimit / compactionPercent
  // -----------------------------------------------------------------

  describe('compactionLimit', () => {
    it('is the autocompact threshold when autocompact is on', () => {
      expect(compactionLimit(livePayload())).toBe(167000);
    });

    it('is the raw window when autocompact is off', () => {
      // Nothing intervenes, so the turn runs until the model refuses.
      expect(compactionLimit({
        ...livePayload(),
        isAutoCompactEnabled: false,
      })).toBe(200000);
    });

    it('falls back to maxTokens when no threshold is reported', () => {
      const u = livePayload();
      delete u.autoCompactThreshold;
      expect(compactionLimit(u)).toBe(200000);
    });

    it('ignores a threshold above the window', () => {
      // Nonsense in, window out — a limit beyond the window would
      // make the bar unreachable.
      expect(compactionLimit({
        ...livePayload(),
        autoCompactThreshold: 500000,
      })).toBe(200000);
    });

    it('returns 0 when there is no usable window', () => {
      expect(compactionLimit({})).toBe(0);
      expect(compactionLimit(null)).toBe(0);
      expect(compactionLimit({ maxTokens: 0 })).toBe(0);
    });
  });

  describe('compactionPercent', () => {
    it('measures against the threshold, not the window', () => {
      // 21087/167000, not 21087/200000. The gap is the whole point:
      // the engine's own percentage says 11%.
      expect(compactionPercent(livePayload())).toBeCloseTo(12.63, 2);
      expect(windowPercent(livePayload())).toBe(11);
    });

    it('reaches 100 exactly at the threshold', () => {
      expect(compactionPercent({
        ...livePayload(),
        totalTokens: 167000,
      })).toBe(100);
    });

    it('goes red before the raw window is full', () => {
      // The defect this exists to fix: at 155K tokens a compact is
      // imminent, but 155K/200K is 77.5% — amber at most.
      const u = { ...livePayload(), totalTokens: 155000, percentage: 78 };
      expect(bandColor(compactionPercent(u))).toBe('#f85149');
      expect(bandColor(windowPercent(u))).toBe('#d29922');
    });

    it('exceeds 100 past the threshold rather than clamping', () => {
      // Unclamped so a caller can say "over" instead of "exactly full".
      expect(compactionPercent({
        ...livePayload(),
        totalTokens: 180000,
      })).toBeGreaterThan(100);
    });

    it('is null when unknowable', () => {
      expect(compactionPercent({})).toBeNull();
      expect(compactionPercent(null)).toBeNull();
      expect(compactionPercent({ maxTokens: 200000 })).toBeNull();
    });

    it('is 0 at an empty context', () => {
      expect(compactionPercent({ ...livePayload(), totalTokens: 0 })).toBe(0);
    });
  });

  // -----------------------------------------------------------------
  // thresholdPercent — where the mark goes on the gauge
  // -----------------------------------------------------------------

  describe('thresholdPercent', () => {
    it('places the mark well short of the bar\'s end', () => {
      // 167000/200000. The 16.5 points between the mark and the end are
      // the reason the gauge needs one at all: the bar reads 11% full
      // and gives out at 83.5%, not at 100%.
      expect(thresholdPercent(livePayload())).toBeCloseTo(83.5, 6);
    });

    it('is null when autocompact is off', () => {
      // Nothing triggers, so there is nothing to mark. A mark drawn
      // anyway would promise an intervention that is not coming.
      expect(thresholdPercent({
        ...livePayload(),
        isAutoCompactEnabled: false,
      })).toBeNull();
    });

    it('is null when the engine reports no threshold', () => {
      const u = livePayload();
      delete u.autoCompactThreshold;
      expect(thresholdPercent(u)).toBeNull();
    });

    it('is null when the threshold is the window itself', () => {
      // The mark would sit on the bar's own end and read as a second
      // limit inside the first.
      expect(thresholdPercent({
        ...livePayload(),
        autoCompactThreshold: 200000,
      })).toBeNull();
      expect(thresholdPercent({
        ...livePayload(),
        autoCompactThreshold: 250000,
      })).toBeNull();
    });

    it('is null for junk', () => {
      for (const u of [undefined, null, {}, { maxTokens: 0 },
        { maxTokens: 200000 }, { autoCompactThreshold: 167000 }]) {
        expect(thresholdPercent(u)).toBeNull();
      }
    });
  });

  // -----------------------------------------------------------------
  // overLimit — past the window, which is reachable
  // -----------------------------------------------------------------

  describe('overLimit', () => {
    it('is null while the context fits', () => {
      expect(overLimit(livePayload())).toBeNull();
    });

    it('is null exactly at the window', () => {
      expect(overLimit({ ...livePayload(), totalTokens: 200000 })).toBeNull();
    });

    it('reports the overshoot against the compaction window', () => {
      expect(overLimit({ ...livePayload(), totalTokens: 210000 })).toEqual({
        over: 10000,
        window: 200000,
        kind: 'compaction_window',
      });
    });

    it('calls an auto-sized window a hard limit', () => {
      // The engine's own split: a window it sized itself has no
      // compaction headroom left to overshoot into.
      expect(overLimit({
        ...livePayload(),
        autocompactSource: 'auto',
        totalTokens: 210000,
      }).kind).toBe('hard_limit');
    });

    it('measures against rawMaxTokens, falling back to maxTokens', () => {
      // Equal by construction today; asserted so a future payload that
      // separates them is measured the way the engine measures it.
      expect(overLimit({
        ...livePayload(),
        rawMaxTokens: 220000,
        totalTokens: 210000,
      })).toBeNull();
      const u = livePayload();
      delete u.rawMaxTokens;
      expect(overLimit({ ...u, totalTokens: 210000 }).window).toBe(200000);
    });

    it('is null for junk', () => {
      for (const u of [undefined, null, {}, { maxTokens: 0 },
        { maxTokens: 200000 }, { maxTokens: 200000, totalTokens: 'x' }]) {
        expect(overLimit(u)).toBeNull();
      }
    });
  });

  // -----------------------------------------------------------------
  // messageComposition
  // -----------------------------------------------------------------

  describe('messageComposition', () => {
    const withBreakdown = (mb = messageBreakdown()) => ({
      ...livePayload(),
      messageBreakdown: mb,
    });

    it('lists the non-zero parts in conversation order', () => {
      // Order is fixed rather than ranked: these are segments of one bar
      // across repeated fetches, and a bar whose colours reorder every
      // refresh cannot be read at a glance.
      const { parts } = messageComposition(withBreakdown());
      expect(parts.map((p) => p.label)).toEqual([
        'User messages',
        'Assistant messages',
        'Tool calls',
        'Tool results',
        'Attachments',
        'Unattributed',
      ]);
    });

    it('drops a part the engine counted at zero', () => {
      const { parts } = messageComposition(withBreakdown());
      // redirectedContextTokens is 0 in the fixture.
      expect(parts.map((p) => p.key))
        .not.toContain('redirectedContextTokens');
    });

    it('gives every part a colour of its own', () => {
      const { parts } = messageComposition(withBreakdown());
      const colours = parts.map((p) => p.color);
      expect(new Set(colours).size).toBe(colours.length);
      for (const c of colours) expect(c).toMatch(/^#[0-9a-f]{6}$/i);
    });

    it('reconciles with the Messages category', () => {
      const c = messageComposition(withBreakdown());
      expect(c.partsTokens).toBe(2905);
      expect(c.messagesTokens).toBe(2905);
      expect(c.reconciled).toBe(true);
    });

    it('does not reconcile when the parts overshoot the category', () => {
      // Reachable rather than defensive: the engine derives
      // `unattributedTokens` as the category minus the other six and
      // floors it at zero, so its own per-part estimate can exceed what
      // the category is charged. The caller says so instead of drawing a
      // bar that does not add up.
      const c = messageComposition(withBreakdown(messageBreakdown({
        assistantMessageTokens: 2000,
        unattributedTokens: 0,
      })));
      expect(c.partsTokens).toBe(4000);
      expect(c.messagesTokens).toBe(2905);
      expect(c.reconciled).toBe(false);
    });

    it('totals each tool\'s calls and results, heaviest first', () => {
      const { byTool } = messageComposition(withBreakdown());
      expect(byTool).toEqual([
        { name: 'Read', callTokens: 120, resultTokens: 800, tokens: 920 },
        { name: 'Bash', callTokens: 180, resultTokens: 400, tokens: 580 },
      ]);
    });

    it('re-sorts tools the engine sent out of order', () => {
      const { byTool } = messageComposition(withBreakdown(messageBreakdown({
        toolCallsByType: [
          { name: 'Grep', callTokens: 10, resultTokens: 20 },
          { name: 'Read', callTokens: 120, resultTokens: 800 },
        ],
      })));
      expect(byTool.map((t) => t.name)).toEqual(['Read', 'Grep']);
    });

    it('sorts attachments by cost and drops empty ones', () => {
      const { byAttachment } = messageComposition(
        withBreakdown(messageBreakdown({
          attachmentsByType: [
            { name: 'image', tokens: 40 },
            { name: 'empty', tokens: 0 },
            { name: 'pasted_text', tokens: 60 },
          ],
        })),
      );
      expect(byAttachment).toEqual([
        { name: 'pasted_text', tokens: 60 },
        { name: 'image', tokens: 40 },
      ]);
    });

    it('names an entry the engine sent without one', () => {
      const { byTool, byAttachment } = messageComposition(
        withBreakdown(messageBreakdown({
          toolCallsByType: [{ callTokens: 5, resultTokens: 5 }],
          attachmentsByType: [{ tokens: 5 }],
        })),
      );
      expect(byTool[0].name).toBe('unknown');
      expect(byAttachment[0].name).toBe('unknown');
    });

    it('is null when the engine sent no breakdown', () => {
      expect(messageComposition(livePayload())).toBeNull();
      for (const mb of [undefined, null, 'x', 42, []]) {
        expect(messageComposition({ ...livePayload(), messageBreakdown: mb }))
          .toBeNull();
      }
      expect(messageComposition(null)).toBeNull();
    });

    it('is null for a session with no turns yet', () => {
      // An all-zero breakdown is not an empty section with zeroes in it;
      // there is nothing to show.
      expect(messageComposition(withBreakdown({
        userMessageTokens: 0,
        assistantMessageTokens: 0,
        toolCallsByType: [],
        attachmentsByType: [],
      }))).toBeNull();
    });

    it('skips non-object list entries', () => {
      const { byTool, byAttachment } = messageComposition(
        withBreakdown(messageBreakdown({
          toolCallsByType: [null, 'x', 7, { name: 'Read', callTokens: 5 }],
          attachmentsByType: [null, { name: 'image', tokens: 5 }],
        })),
      );
      expect(byTool).toHaveLength(1);
      expect(byAttachment).toHaveLength(1);
    });

    it('reports messagesTokens null when there is no Messages category', () => {
      const c = messageComposition({
        messageBreakdown: messageBreakdown(),
        categories: [{ name: 'System prompt', tokens: 3371 }],
      });
      expect(c.messagesTokens).toBeNull();
      expect(c.reconciled).toBe(false);
    });
  });

  // -----------------------------------------------------------------
  // windowPercent
  // -----------------------------------------------------------------

  describe('windowPercent', () => {
    it('prefers the engine\'s own percentage', () => {
      // Parity with /context is the point: a locally computed ratio
      // would read 10.5% where the CLI prints 11%.
      expect(windowPercent(livePayload())).toBe(11);
    });

    it('computes a ratio when the engine omits percentage', () => {
      const u = livePayload();
      delete u.percentage;
      expect(windowPercent(u)).toBeCloseTo(10.54, 2);
    });

    it('clamps to 0-100', () => {
      expect(windowPercent({ percentage: 140 })).toBe(100);
      expect(windowPercent({ percentage: -5 })).toBe(0);
    });

    it('is 0 for junk', () => {
      expect(windowPercent(null)).toBe(0);
      expect(windowPercent({})).toBe(0);
      expect(windowPercent({ totalTokens: 5, maxTokens: 0 })).toBe(0);
    });
  });

  // -----------------------------------------------------------------
  // sourceLabel
  //
  // The mapping is lifted from the CLI's own shared label mapper, not
  // inferred from the two values a live session happened to report —
  // which is also how the raw keys were found to be reaching our tables:
  // "projectSettings" where /context prints "Project".
  // -----------------------------------------------------------------

  describe('sourceLabel', () => {
    it('speaks the CLI\'s words for a settings scope', () => {
      expect(sourceLabel('userSettings')).toBe('User');
      expect(sourceLabel('projectSettings')).toBe('Project');
      expect(sourceLabel('localSettings')).toBe('Local');
      expect(sourceLabel('flagSettings')).toBe('Flag');
      expect(sourceLabel('plugin')).toBe('Plugin');
      expect(sourceLabel('built-in')).toBe('Built-in');
      expect(sourceLabel('mcp')).toBe('MCP');
      expect(sourceLabel('memoryStore')).toBe('Memory store');
    });

    it('calls policySettings "Managed", as the shared mapper does', () => {
      // The CLI disagrees with itself here: the switch inlined in its
      // markdown renderer says "Policy". The shared mapper wins, because
      // it is the one the CLI also uses for skills.
      expect(sourceLabel('policySettings')).toBe('Managed');
    });

    it('passes an unknown scope through rather than blanking it', () => {
      // A scope we have not seen is still more informative raw.
      expect(sourceLabel('newScopeSettings')).toBe('newScopeSettings');
    });

    it('is an em dash when there is nothing to label', () => {
      expect(sourceLabel(undefined)).toBe('—');
      expect(sourceLabel('')).toBe('—');
      expect(sourceLabel('   ')).toBe('—');
      expect(sourceLabel(7)).toBe('—');
    });
  });

  // -----------------------------------------------------------------
  // mcpHealth
  // -----------------------------------------------------------------

  describe('mcpHealth', () => {
    it('keys each server by name with a label and a colour', () => {
      const h = mcpHealth({
        mcpServers: [
          { name: 'ac-dc', status: 'connected' },
          { name: 'other', status: 'pending' },
        ],
      });
      expect([...h.keys()]).toEqual(['ac-dc', 'other']);
      expect(h.get('ac-dc').label).toBe('connected');
      expect(h.get('ac-dc').color).toBe('#7ee787');
      // "pending" is what the wire says; "connecting" is what it means.
      expect(h.get('other').label).toBe('connecting');
    });

    it('ambers needs-auth rather than reddening it', () => {
      // The server is reachable and waiting on the user, which is not
      // the same situation as one that failed to start.
      const h = mcpHealth({
        mcpServers: [
          { name: 'a', status: 'needs-auth' },
          { name: 'b', status: 'failed' },
        ],
      });
      expect(h.get('a').label).toBe('needs auth');
      expect(h.get('a').color).toBe('#d29922');
      expect(h.get('b').color).toBe('#f85149');
    });

    it('reads the transport off the config discriminant', () => {
      // `config` is a union over stdio / sse / http / sdk /
      // claudeai-proxy, and `type` is the arm every one of them carries.
      const h = mcpHealth({
        mcpServers: [
          {
            name: 'ac-dc',
            status: 'connected',
            scope: 'project',
            config: { type: 'sse', url: 'http://localhost:9000' },
            serverInfo: { name: 'ac-dc', version: '0.4.1' },
            tools: [{ name: 'symbol_map' }, { name: 'ui_state' }],
          },
        ],
      });
      expect(h.get('ac-dc')).toMatchObject({
        scope: 'project',
        transport: 'sse',
        version: '0.4.1',
        toolCount: 2,
      });
    });

    it('carries the error a failed server reported', () => {
      const h = mcpHealth({
        mcpServers: [{ name: 'a', status: 'failed', error: 'spawn ENOENT' }],
      });
      expect(h.get('a').error).toBe('spawn ENOENT');
    });

    it('leaves toolCount null when the server listed none', () => {
      // Absent and empty are different: a connected server with zero
      // tools is a fact, and "not reported" is not zero.
      const h = mcpHealth({ mcpServers: [{ name: 'a', status: 'connected' }] });
      expect(h.get('a').toolCount).toBeNull();
      expect(
        mcpHealth({ mcpServers: [{ name: 'b', status: 'connected', tools: [] }] })
          .get('b').toolCount,
      ).toBe(0);
    });

    it('keeps an unrecognised status rather than dropping the server', () => {
      const h = mcpHealth({ mcpServers: [{ name: 'a', status: 'reticulating' }] });
      expect(h.get('a').label).toBe('reticulating');
      expect(h.get('a').color).toBe(UNCOLOURED);
    });

    it('is an empty map for junk', () => {
      expect(mcpHealth(null).size).toBe(0);
      expect(mcpHealth({}).size).toBe(0);
      expect(mcpHealth({ mcpServers: 'nope' }).size).toBe(0);
      expect(mcpHealth({ mcpServers: [null, {}, { name: '  ' }] }).size).toBe(0);
    });
  });

  // -----------------------------------------------------------------
  // serverGroups
  // -----------------------------------------------------------------

  describe('serverGroups', () => {
    const tools = [
      { name: 'symbol_map', serverName: 'ac-dc', tokens: 900 },
      { name: 'doc_outline', serverName: 'ac-dc', tokens: 700, isLoaded: false },
      { name: 'search', serverName: 'other', tokens: 4000 },
    ];

    it('groups tools under the server that provides them', () => {
      const g = serverGroups({ mcpTools: tools });
      expect(g.map((x) => x.name)).toEqual(['other', 'ac-dc']);
      expect(g[1].tools.map((t) => t.name)).toEqual([
        'symbol_map',
        'doc_outline',
      ]);
    });

    it('counts loaded and deferred separately', () => {
      // Summing them would report a cost the session is not paying: a
      // deferred tool's schema is not in the window until first use.
      const [, acdc] = serverGroups({ mcpTools: tools });
      expect(acdc).toMatchObject({
        tokens: 1600,
        loadedTokens: 900,
        deferredTokens: 700,
        loadedCount: 1,
        deferredCount: 1,
      });
    });

    it('sorts tools within a group heaviest first, then by name', () => {
      const g = serverGroups({
        mcpTools: [
          { name: 'b', serverName: 's', tokens: 10 },
          { name: 'a', serverName: 's', tokens: 10 },
          { name: 'c', serverName: 's', tokens: 90 },
        ],
      });
      expect(g[0].tools.map((t) => t.name)).toEqual(['c', 'a', 'b']);
    });

    it('joins health onto the group', () => {
      const health = mcpHealth({
        mcpServers: [{ name: 'ac-dc', status: 'connected' }],
      });
      const g = serverGroups({ mcpTools: tools }, health);
      expect(g.find((x) => x.name === 'ac-dc').health.label).toBe('connected');
      expect(g.find((x) => x.name === 'other').health).toBeNull();
    });

    it('lists a server that failed before offering any tools', () => {
      // The interesting case, not an edge one: a server has no tools
      // *because* it failed, so a listing built from mcpTools alone
      // answers "which servers do I have" by omitting the broken one.
      const health = mcpHealth({
        mcpServers: [{ name: 'broken', status: 'failed', error: 'refused' }],
      });
      const g = serverGroups({ mcpTools: [] }, health);
      expect(g).toHaveLength(1);
      expect(g[0]).toMatchObject({ name: 'broken', tokens: 0, tools: [] });
      expect(g[0].health.error).toBe('refused');
    });

    it('puts an unwell server above a heavier healthy one', () => {
      // A failed server costs nothing, so heaviest-first buries the one
      // row that needs acting on.
      const health = mcpHealth({
        mcpServers: [
          { name: 'ac-dc', status: 'failed' },
          { name: 'other', status: 'connected' },
        ],
      });
      expect(serverGroups({ mcpTools: tools }, health).map((x) => x.name))
        .toEqual(['ac-dc', 'other']);
    });

    it('does not promote a disabled or connecting server', () => {
      // Disabled is a choice somebody made, and pending resolves itself.
      const health = mcpHealth({
        mcpServers: [
          { name: 'ac-dc', status: 'disabled' },
          { name: 'other', status: 'pending' },
        ],
      });
      expect(serverGroups({ mcpTools: tools }, health).map((x) => x.name))
        .toEqual(['other', 'ac-dc']);
    });

    it('breaks a token tie by name, so the order is stable', () => {
      const g = serverGroups({
        mcpTools: [
          { name: 't', serverName: 'zeta', tokens: 100 },
          { name: 't', serverName: 'alpha', tokens: 100 },
        ],
      });
      expect(g.map((x) => x.name)).toEqual(['alpha', 'zeta']);
    });

    it('files a server-less tool under "unknown"', () => {
      const g = serverGroups({ mcpTools: [{ name: 't', tokens: 5 }] });
      expect(g[0].name).toBe('unknown');
      expect(g[0].tools[0].name).toBe('t');
    });

    it('is empty for junk, and ignores a non-Map health argument', () => {
      expect(serverGroups(null)).toEqual([]);
      expect(serverGroups({})).toEqual([]);
      expect(serverGroups({ mcpTools: 'nope' })).toEqual([]);
      expect(serverGroups({ mcpTools: [null, 'x'] })).toEqual([]);
      expect(serverGroups({ mcpTools: tools }, { 'ac-dc': {} })).toHaveLength(2);
    });
  });

  // -----------------------------------------------------------------
  // skillInventory
  // -----------------------------------------------------------------

  describe('skillInventory', () => {
    it('reports both counts, because they answer different questions', () => {
      // 3 of 40 loaded means 37 skills cost nothing right now.
      const inv = skillInventory({
        skills: { totalSkills: 40, includedSkills: 3, tokens: 1469 },
      });
      expect(inv).toMatchObject({ total: 40, included: 3, tokens: 1469 });
    });

    it('sorts frontmatter heaviest first, then by name', () => {
      const inv = skillInventory({
        skills: {
          tokens: 300,
          skillFrontmatter: [
            { name: 'b', source: 'projectSettings', tokens: 100 },
            { name: 'a', source: 'projectSettings', tokens: 100 },
            { name: 'c', source: 'userSettings', tokens: 200 },
          ],
        },
      });
      expect(inv.rows.map((r) => r.name)).toEqual(['c', 'a', 'b']);
      expect(inv.rows[0].source).toBe('User');
    });

    it('keeps the plugin name, which is all that separates its skills', () => {
      const inv = skillInventory({
        skills: {
          tokens: 50,
          skillFrontmatter: [
            { name: 'deploy', source: 'plugin', pluginName: 'acme', tokens: 50 },
          ],
        },
      });
      expect(inv.rows[0]).toMatchObject({ source: 'Plugin', plugin: 'acme' });
    });

    it('survives a total with no itemisation', () => {
      const inv = skillInventory({ skills: { totalSkills: 3, tokens: 90 } });
      expect(inv.rows).toEqual([]);
      expect(inv.tokens).toBe(90);
      expect(inv.included).toBeNull();
    });

    it('is null when there is nothing to report', () => {
      expect(skillInventory(null)).toBeNull();
      expect(skillInventory({})).toBeNull();
      expect(skillInventory({ skills: [] })).toBeNull();
      expect(skillInventory({ skills: { totalSkills: 0, tokens: 0 } })).toBeNull();
    });
  });
});
