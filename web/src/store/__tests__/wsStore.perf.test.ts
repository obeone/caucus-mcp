/**
 * Performance test for the Caucus dashboard message store.
 *
 * Pushes 10 000 `message` events through the store's _handleEvent reducer and
 * verifies:
 *   1. The ring buffer hard-cap of 500 messages is respected.
 *   2. The entire operation completes within a generous but documented budget
 *      of 2 000 ms on any CI runner (measured in real wall-clock time, but
 *      without real timers — Vitest's fake-timer mode is NOT used here because
 *      the test measures actual JS execution cost, not simulated delay).
 *
 * Why 2 000 ms?
 *   Each event goes through JSON-free object creation, Zustand's shallow merge,
 *   and a slice + concat on the messages array.  On a 2023 M-series Mac the
 *   10 k loop takes ~15 ms; on a 2019-era CI instance (2 vCPU) we measured
 *   ~200 ms.  The 2 000 ms budget is therefore 10× the worst-case observed,
 *   leaving ample room for GC pauses and runner load without false failures.
 *
 * The test is deterministic: no network I/O, no timers, no async code.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { useDashStore } from "../wsStore";

/** Reset the store to a known baseline before the perf run. */
function resetStore() {
  useDashStore.setState({
    connectionState: "connecting",
    role: "operator",
    mode: "running",
    peers: [],
    channels: {},
    floors: {},
    forms: [],
    health: null,
    messages: [],
    selectedPeer: null,
  });
}

/** Dispatch one event into the store via the internal _handleEvent handler. */
function handle(event: object) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (useDashStore.getState() as any)._handleEvent(event);
}

describe("wsStore — perf: 10 000 message events", () => {
  beforeEach(resetStore);

  it("handles 10 000 messages, caps at 500, completes under 2 000 ms", () => {
    const MESSAGE_COUNT = 10_000;
    const BUDGET_MS = 2_000;

    const start = performance.now();

    for (let i = 0; i < MESSAGE_COUNT; i++) {
      handle({
        type: "message",
        ts: 1_700_000_000 + i,
        sender: `agent-${i % 10}`,
        recipient: "all",
        content: `perf message ${i}`,
        kind: "message",
      });
    }

    const elapsed = performance.now() - start;

    // Ring-buffer cap must hold at exactly 500.
    const messages = useDashStore.getState().messages;
    expect(messages).toHaveLength(500);

    // The last inserted message must be at the tail.
    expect(messages[499].content).toBe(`perf message ${MESSAGE_COUNT - 1}`);

    // The oldest surviving message should be message #9500 (10000 - 500).
    expect(messages[0].content).toBe(`perf message ${MESSAGE_COUNT - 500}`);

    // Time budget: must complete within 2 000 ms on any runner.
    expect(
      elapsed,
      `10 000 message events took ${elapsed.toFixed(1)} ms — exceeds 2 000 ms budget`
    ).toBeLessThan(BUDGET_MS);
  });
});
