/**
 * Unit tests for lib/colors.ts — formatter functions and colorFor() determinism.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { fmtTime, fmtTimeUTC, fmtDuration, colorFor } from "../colors";

// A fixed Unix timestamp: 2024-03-15 14:32:05 UTC  (1710509525)
const FIXED_TS = 1710509525;

describe("fmtDuration", () => {
  it("formats sub-minute durations as seconds only", () => {
    expect(fmtDuration(0)).toBe("0s");
    expect(fmtDuration(45)).toBe("45s");
    expect(fmtDuration(59)).toBe("59s");
  });

  it("formats minute-range durations as Xm Ys", () => {
    expect(fmtDuration(60)).toBe("1m 0s");
    expect(fmtDuration(90)).toBe("1m 30s");
    expect(fmtDuration(3599)).toBe("59m 59s");
  });

  it("formats hour-range durations as Xh Ym Zs", () => {
    expect(fmtDuration(3600)).toBe("1h 0m 0s");
    expect(fmtDuration(7323)).toBe("2h 2m 3s");
  });

  it("floors fractional seconds", () => {
    expect(fmtDuration(1.9)).toBe("1s");
    expect(fmtDuration(61.7)).toBe("1m 1s");
  });
});

describe("fmtTimeUTC", () => {
  it("includes UTC suffix", () => {
    const result = fmtTimeUTC(FIXED_TS);
    expect(result).toMatch(/UTC$/);
  });

  it("produces a time-like string HH:MM:SS UTC", () => {
    const result = fmtTimeUTC(FIXED_TS);
    // Should match e.g. "14:32:05 UTC"
    expect(result).toMatch(/^\d{2}:\d{2}:\d{2} UTC$/);
  });

  it("returns consistent output for the same timestamp", () => {
    expect(fmtTimeUTC(FIXED_TS)).toBe(fmtTimeUTC(FIXED_TS));
  });
});

describe("fmtTime", () => {
  it("returns a non-empty time string", () => {
    const result = fmtTime(FIXED_TS);
    expect(result.length).toBeGreaterThan(0);
  });

  it("does NOT include UTC suffix (local time)", () => {
    const result = fmtTime(FIXED_TS);
    expect(result).not.toMatch(/UTC/);
  });

  it("returns consistent output for the same timestamp", () => {
    expect(fmtTime(FIXED_TS)).toBe(fmtTime(FIXED_TS));
  });
});

describe("colorFor", () => {
  // colorFor uses a module-level Map, so we test within a single describe block
  // to control insertion order.

  it("returns a fixed colour for 'human'", () => {
    expect(colorFor("human")).toBe("#c08bff");
  });

  it("returns a fixed colour for 'hub'", () => {
    expect(colorFor("hub")).toBe("#6b7d89");
  });

  it("returns a hex colour string for arbitrary names", () => {
    const colour = colorFor("agent-alpha");
    expect(colour).toMatch(/^#[0-9a-fA-F]{6}$/);
  });

  it("is deterministic — same name always returns same colour", () => {
    const name = "peer-stable-test";
    const first = colorFor(name);
    const second = colorFor(name);
    expect(second).toBe(first);
  });

  it("assigns different colours to the first few distinct names", () => {
    // Unique names generated fresh to avoid collision with module-level state
    const names = ["__t1__", "__t2__", "__t3__", "__t4__", "__t5__", "__t6__", "__t7__"];
    const colours = names.map(colorFor);
    const unique = new Set(colours);
    // With 7 palette entries these 7 names should all get distinct colours
    expect(unique.size).toBe(names.length);
  });

  it("wraps around the palette after 7 distinct names", () => {
    // After 7 unique names the 8th wraps to the same colour as the 1st in that batch
    const base = "__wrap__";
    const wrapped = `__wrap__extra__`;
    // These names may already be in the map; what matters is the palette length is 7
    // so every 7th new name repeats. We test using names unlikely to have been used.
    const batch = Array.from({ length: 8 }, (_, i) => `__palette_wrap_${i}__`);
    const colours = batch.map(colorFor);
    expect(colours[7]).toBe(colours[0]);
    // Suppress "unused variable" — base/wrapped are reference anchors for clarity
    void base;
    void wrapped;
  });
});
