/**
 * Unit tests for FlowPanel's message-filtering logic.
 *
 * `passesFilters` is a pure function (exported from FlowPanel) so it is tested
 * directly here, without mounting the component's virtualizer — jsdom has no
 * layout engine, so the virtualizer would report a zero-height container and
 * render nothing (see FlowPanel.test.tsx's header comment for the same note).
 */

import { describe, it, expect } from "vitest";
import { passesFilters } from "../FlowPanel";
import type { Message } from "../../store/types";

/** Build a minimal Message fixture with sensible defaults. */
function makeMsg(overrides: Partial<Message> = {}): Message {
  return {
    id: "m1",
    ts: 1_700_000_000,
    sender: "agent-a",
    recipient: "all",
    content: "hello world",
    kind: "message",
    ...overrides,
  };
}

describe("passesFilters — empty filters", () => {
  it("passes every message when no filter is active (default behaviour)", () => {
    const msg = makeMsg();
    expect(passesFilters(msg, null, "all", "all", "")).toBe(true);
  });
});

describe("passesFilters — peer filter", () => {
  it("passes a message where the peer is the sender", () => {
    const msg = makeMsg({ sender: "agent-a", recipient: "all" });
    expect(passesFilters(msg, "agent-a", "all", "all", "")).toBe(true);
  });

  it("passes a message where the peer is the recipient", () => {
    const msg = makeMsg({ sender: "agent-b", recipient: "agent-a" });
    expect(passesFilters(msg, "agent-a", "all", "all", "")).toBe(true);
  });

  it("rejects a message that involves neither as sender nor recipient", () => {
    const msg = makeMsg({ sender: "agent-b", recipient: "agent-c" });
    expect(passesFilters(msg, "agent-a", "all", "all", "")).toBe(false);
  });
});

describe("passesFilters — channel filter", () => {
  it("passes a message addressed to the selected channel", () => {
    const msg = makeMsg({ recipient: "#design" });
    expect(passesFilters(msg, null, "#design", "all", "")).toBe(true);
  });

  it("rejects a message addressed to a different channel", () => {
    const msg = makeMsg({ recipient: "#design" });
    expect(passesFilters(msg, null, "#ops", "all", "")).toBe(false);
  });

  it("'all' channel filter does not restrict by recipient", () => {
    const msg = makeMsg({ recipient: "#design" });
    expect(passesFilters(msg, null, "all", "all", "")).toBe(true);
  });
});

describe("passesFilters — type filter", () => {
  it("broadcast matches recipient 'all' only", () => {
    expect(passesFilters(makeMsg({ recipient: "all" }), null, "all", "broadcast", "")).toBe(true);
    expect(passesFilters(makeMsg({ recipient: "agent-b" }), null, "all", "broadcast", "")).toBe(false);
    expect(passesFilters(makeMsg({ recipient: "#design" }), null, "all", "broadcast", "")).toBe(false);
  });

  it("direct matches a named peer recipient, not 'all' or a channel", () => {
    expect(passesFilters(makeMsg({ recipient: "agent-b" }), null, "all", "direct", "")).toBe(true);
    expect(passesFilters(makeMsg({ recipient: "all" }), null, "all", "direct", "")).toBe(false);
    expect(passesFilters(makeMsg({ recipient: "#design" }), null, "all", "direct", "")).toBe(false);
  });

  it("channel matches only recipients starting with '#'", () => {
    expect(passesFilters(makeMsg({ recipient: "#design" }), null, "all", "channel", "")).toBe(true);
    expect(passesFilters(makeMsg({ recipient: "agent-b" }), null, "all", "channel", "")).toBe(false);
    expect(passesFilters(makeMsg({ recipient: "all" }), null, "all", "channel", "")).toBe(false);
  });
});

describe("passesFilters — free-text search", () => {
  it("matches content case-insensitively", () => {
    const msg = makeMsg({ content: "Deploy the API gateway" });
    expect(passesFilters(msg, null, "all", "all", "api")).toBe(true);
    expect(passesFilters(msg, null, "all", "all", "nope")).toBe(false);
  });

  it("matches sender", () => {
    const msg = makeMsg({ sender: "architect" });
    expect(passesFilters(msg, null, "all", "all", "arch")).toBe(true);
  });

  it("matches recipient", () => {
    const msg = makeMsg({ recipient: "#design" });
    expect(passesFilters(msg, null, "all", "all", "design")).toBe(true);
  });

  it("empty search string does not filter anything out", () => {
    const msg = makeMsg({ content: "anything at all" });
    expect(passesFilters(msg, null, "all", "all", "")).toBe(true);
  });
});

describe("passesFilters — combined filters (AND semantics)", () => {
  it("requires peer, channel, type and search to all match", () => {
    const msg = makeMsg({
      sender: "agent-a",
      recipient: "#design",
      content: "shipping the release",
    });
    expect(passesFilters(msg, "agent-a", "#design", "channel", "release")).toBe(true);
    // Any single mismatched filter rejects the message.
    expect(passesFilters(msg, "agent-b", "#design", "channel", "release")).toBe(false);
    expect(passesFilters(msg, "agent-a", "#ops", "channel", "release")).toBe(false);
    expect(passesFilters(msg, "agent-a", "#design", "direct", "release")).toBe(false);
    expect(passesFilters(msg, "agent-a", "#design", "channel", "nope")).toBe(false);
  });
});
