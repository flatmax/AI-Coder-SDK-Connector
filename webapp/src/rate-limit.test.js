// Tests for the shared `rateLimit` derivations.
//
// Two components read one record for opposite purposes — the chat panel
// raises a toast on a transition, the usage HUD renders the standing figure —
// so the wording and the arithmetic live in one module. These are the rules
// that would otherwise be duplicated and drift, plus the two the SDK's own
// types settle: `utilization` is a fraction, and `resets_at` is Unix seconds.

import { describe, expect, it } from 'vitest';

import {
  formatResetTime,
  hasSomethingToSay,
  limitTypeLabel,
  utilizationPercent,
  windowIsOpen,
} from './rate-limit.js';

describe('limitTypeLabel', () => {
  it('names each window the CLI models', () => {
    expect(limitTypeLabel('five_hour')).toBe('5-hour');
    expect(limitTypeLabel('seven_day')).toBe('7-day');
    expect(limitTypeLabel('seven_day_opus')).toBe('7-day Opus');
    expect(limitTypeLabel('seven_day_sonnet')).toBe('7-day Sonnet');
    expect(limitTypeLabel('overage')).toBe('Overage');
  });

  it('opens out a window this build has never heard of', () => {
    // `RateLimitType` is the CLI's enum to extend. A window we cannot name
    // is still a window the user is being limited by, and naming it badly
    // beats dropping it.
    expect(limitTypeLabel('thirty_day_haiku')).toBe('thirty day haiku');
  });

  it('has nothing to say about a missing type', () => {
    expect(limitTypeLabel(undefined)).toBe('');
    expect(limitTypeLabel('')).toBe('');
    expect(limitTypeLabel(7)).toBe('');
  });
});

describe('utilizationPercent', () => {
  it('reads the field as the fraction the SDK documents', () => {
    // 0.0–1.0, per `RateLimitInfo`. Reading it as a percentage already would
    // render every real figure as under one percent.
    expect(utilizationPercent({ utilization: 0.42 })).toBeCloseTo(42);
    expect(utilizationPercent({ utilization: 0 })).toBe(0);
  });

  it('clamps above the top of the bar', () => {
    expect(utilizationPercent({ utilization: 1.03 })).toBe(100);
  });

  it('answers null rather than a number it cannot support', () => {
    expect(utilizationPercent({ utilization: null })).toBeNull();
    expect(utilizationPercent({ utilization: 'high' })).toBeNull();
    expect(utilizationPercent({ utilization: NaN })).toBeNull();
    expect(utilizationPercent({ utilization: -0.5 })).toBeNull();
    expect(utilizationPercent(undefined)).toBeNull();
  });
});

describe('formatResetTime', () => {
  it('reads the timestamp as Unix seconds', () => {
    // Not milliseconds and not ISO — the one detail that turns a reset time
    // into a date in 1970 or in the year 57000.
    const at = Date.UTC(2026, 7, 28, 12, 30) / 1000;
    const expected = new Date(at * 1000)
      .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(formatResetTime(at, at * 1000)).toBe(`at ${expected}`);
  });

  it('leaves the day off a reset that lands today', () => {
    // A five-hour window mostly resets today, and "at 04:00 AM today" is
    // noise on every render of the common case.
    const at = Date.UTC(2026, 7, 28, 12, 30) / 1000;
    // An hour earlier the same local day, whatever the zone: the reset is
    // read against the browser's own clock.
    const now = new Date(at * 1000);
    now.setHours(now.getHours() - 1, 0, 0, 0);
    expect(formatResetTime(at, now.getTime())).not.toMatch(/ on /);
  });

  it('dates a reset that is not today', () => {
    // The complaint this exists for: a seven-day window resets up to a week
    // out, and a bare "at 04:00 AM" reads as *this coming* 04:00 AM — hours
    // of waiting where the truth is days.
    const now = new Date(Date.UTC(2026, 7, 28, 12, 30));
    const later = new Date(now.getTime());
    later.setDate(later.getDate() + 5);
    const day = later
      .toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
    const time = later.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    expect(formatResetTime(later.getTime() / 1000, now.getTime()))
      .toBe(`at ${time} on ${day}`);
  });

  it('dates a reset a full week out, where the weekday alone would say today', () => {
    const now = new Date(Date.UTC(2026, 7, 28, 12, 30));
    const later = new Date(now.getTime());
    later.setDate(later.getDate() + 7);
    expect(formatResetTime(later.getTime() / 1000, now.getTime())).toMatch(/ on .*\d/);
  });

  it('says nothing rather than something wrong', () => {
    expect(formatResetTime(0)).toBe('');
    expect(formatResetTime(-1)).toBe('');
    expect(formatResetTime(null)).toBe('');
    expect(formatResetTime('soon')).toBe('');
  });
});

describe('windowIsOpen', () => {
  const now = 1_800_000_000_000;

  it('is open until the reset it names', () => {
    expect(windowIsOpen({ resets_at: now / 1000 + 60 }, now)).toBe(true);
    expect(windowIsOpen({ resets_at: now / 1000 - 60 }, now)).toBe(false);
  });

  it('treats a record with no reset as open', () => {
    // The window is real whether or not the CLI said when it ends; the
    // caller renders the utilisation without a reset line.
    expect(windowIsOpen({ utilization: 0.5 }, now)).toBe(true);
    expect(windowIsOpen({ resets_at: null }, now)).toBe(true);
  });

  it('has no window without a record', () => {
    expect(windowIsOpen(null, now)).toBe(false);
    expect(windowIsOpen('rejected', now)).toBe(false);
  });
});

describe('hasSomethingToSay', () => {
  const now = 1_800_000_000_000;
  const open = now / 1000 + 3600;

  it('is true for a figure, and for a named window without one', () => {
    expect(hasSomethingToSay({ utilization: 0.1, resets_at: open }, now)).toBe(true);
    expect(hasSomethingToSay({ rate_limit_type: 'seven_day', resets_at: open }, now))
      .toBe(true);
  });

  it('is true for a rejection carrying neither', () => {
    // "You are blocked" is worth a section on its own.
    expect(hasSomethingToSay({ status: 'rejected', resets_at: open }, now)).toBe(true);
  });

  it('is false for a status and nothing else', () => {
    expect(hasSomethingToSay({ status: 'allowed', resets_at: open }, now)).toBe(false);
  });

  it('is false once the window it describes has reset', () => {
    // A record stands until the next transition, so it outlives its own
    // window — and a stale utilisation is worse than none, because nothing
    // else on screen contradicts it.
    expect(hasSomethingToSay({ utilization: 0.9, resets_at: now / 1000 - 1 }, now))
      .toBe(false);
  });
});
