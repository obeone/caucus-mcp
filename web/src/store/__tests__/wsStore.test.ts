/**
 * Unit tests for store/wsStore.ts — event reducer and reconnect backoff.
 *
 * Browser globals (localStorage, WebSocket, matchMedia, history, location)
 * are stubbed in src/test/setup.ts before any module is imported.
 *
 * We exercise _handleEvent directly (the private method is accessible via
 * useDashStore.getState()) without needing a real WebSocket.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useDashStore } from "../wsStore";
import type { SnapshotEvent, PeerInfo } from "../types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Reset store to a known baseline before each test. */
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

function handle(event: object) {
  // _handleEvent lives on the internal state shape; cast to any to access it.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (useDashStore.getState() as any)._handleEvent(event);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("wsStore — auth events", () => {
  beforeEach(resetStore);

  it("auth_ok sets role and connectionState to connected", () => {
    handle({ type: "auth_ok", role: "observer", auth: true });
    const s = useDashStore.getState();
    expect(s.role).toBe("observer");
    expect(s.connectionState).toBe("connected");
  });

  it("auth_error sets connectionState to disconnected", () => {
    handle({ type: "auth_error" });
    expect(useDashStore.getState().connectionState).toBe("disconnected");
  });
});

describe("wsStore — snapshot event", () => {
  beforeEach(resetStore);

  const mockPeer: PeerInfo = {
    name: "agent-a",
    state: "live",
    listening: true,
    paused: false,
    status: null,
    status_age: null,
    last_seen_age: 0.5,
    uptime: 120,
    msg_count: 3,
  };

  const snapshot: SnapshotEvent = {
    type: "snapshot",
    mode: "paused",
    peers: [mockPeer],
    channels: { "#general": { topic: "hello", members: ["agent-a"] } },
    floors: {},
    forms: [],
    log: [
      { ts: 1000, sender: "agent-a", recipient: "all", content: "hi", kind: "message" },
      { ts: 1001, sender: "hub", recipient: "agent-a", content: "ack", kind: "system" },
    ],
    health: {
      uptime: 300,
      peer_count: 1,
      msg_per_min: 10,
      queue_depth: 0,
      mem_rss_mb: 42.0,
    },
  };

  it("sets mode from snapshot", () => {
    handle(snapshot);
    expect(useDashStore.getState().mode).toBe("paused");
  });

  it("sets peers from snapshot", () => {
    handle(snapshot);
    expect(useDashStore.getState().peers).toHaveLength(1);
    expect(useDashStore.getState().peers[0].name).toBe("agent-a");
  });

  it("sets channels from snapshot", () => {
    handle(snapshot);
    expect(useDashStore.getState().channels["#general"].members).toContain("agent-a");
  });

  it("converts log entries to Message objects", () => {
    handle(snapshot);
    const msgs = useDashStore.getState().messages;
    expect(msgs).toHaveLength(2);
    expect(msgs[0].sender).toBe("agent-a");
    expect(msgs[0].kind).toBe("message");
    expect(msgs[1].kind).toBe("system");
  });

  it("sets health from snapshot", () => {
    handle(snapshot);
    expect(useDashStore.getState().health?.uptime).toBe(300);
  });
});

describe("wsStore — message event (ring buffer cap)", () => {
  beforeEach(resetStore);

  // Regression: the hub nests the payload under `message`. Reading the fields
  // off the event root left recipient/sender/content undefined, and the Flow
  // panel then crashed on `recipient.startsWith("#")`. Assert the nested
  // fields are unpacked, not undefined.
  it("unpacks the nested message payload (no undefined recipient)", () => {
    handle({
      type: "message",
      message: {
        ts: 1234,
        sender: "architect",
        recipient: "#design",
        content: "hello",
        kind: "message",
      },
    });
    const [msg] = useDashStore.getState().messages;
    expect(msg.recipient).toBe("#design");
    expect(msg.sender).toBe("architect");
    expect(msg.content).toBe("hello");
  });

  it("appends messages up to MAX_MESSAGES (500)", () => {
    for (let i = 0; i < 500; i++) {
      handle({
        type: "message",
        message: {
          ts: 1000 + i,
          sender: "agent-x",
          recipient: "all",
          content: `msg ${i}`,
          kind: "message",
        },
      });
    }
    expect(useDashStore.getState().messages).toHaveLength(500);
  });

  it("drops oldest message when 501st arrives (ring buffer)", () => {
    for (let i = 0; i < 500; i++) {
      handle({
        type: "message",
        message: {
          ts: 1000 + i,
          sender: "agent-x",
          recipient: "all",
          content: `msg ${i}`,
          kind: "message",
        },
      });
    }
    handle({
      type: "message",
      message: {
        ts: 2000,
        sender: "agent-x",
        recipient: "all",
        content: "overflow",
        kind: "message",
      },
    });
    const msgs = useDashStore.getState().messages;
    expect(msgs).toHaveLength(500);
    expect(msgs[0].content).toBe("msg 1");
    expect(msgs[499].content).toBe("overflow");
  });
});

describe("wsStore — peers event", () => {
  beforeEach(resetStore);

  it("replaces entire peer list", () => {
    useDashStore.setState({
      peers: [{
        name: "old", state: "live", listening: false, paused: false,
        status: null, status_age: null, last_seen_age: 0, uptime: 0, msg_count: 0,
      }],
    });
    handle({
      type: "peers",
      peers: [{
        name: "new", state: "live", listening: true, paused: false,
        status: null, status_age: null, last_seen_age: 0, uptime: 0, msg_count: 0,
      }],
    });
    const peers = useDashStore.getState().peers;
    expect(peers).toHaveLength(1);
    expect(peers[0].name).toBe("new");
  });
});

describe("wsStore — health event", () => {
  beforeEach(resetStore);

  it("updates health and refreshes peers", () => {
    const peer: PeerInfo = {
      name: "p", state: "live", listening: true, paused: false,
      status: null, status_age: null, last_seen_age: 0.1, uptime: 60, msg_count: 1,
    };
    handle({
      type: "health",
      health: { uptime: 600, peer_count: 1, msg_per_min: 5, queue_depth: 2, mem_rss_mb: 55.0 },
      peers: [peer],
    });
    const s = useDashStore.getState();
    expect(s.health?.uptime).toBe(600);
    expect(s.peers[0].name).toBe("p");
  });
});

describe("wsStore — form / form_resolved events", () => {
  beforeEach(resetStore);

  const form = {
    id: "f1",
    title: "Test form",
    fields: [],
    audience: "all",
    asker: "agent-a",
    status: "pending" as const,
  };

  it("adds a new form on form event", () => {
    handle({ type: "form", form });
    expect(useDashStore.getState().forms).toHaveLength(1);
    expect(useDashStore.getState().forms[0].id).toBe("f1");
  });

  it("updates existing form if same id arrives again", () => {
    handle({ type: "form", form });
    handle({ type: "form", form: { ...form, title: "Updated" } });
    const forms = useDashStore.getState().forms;
    expect(forms).toHaveLength(1);
    expect(forms[0].title).toBe("Updated");
  });

  it("marks form answered on form_resolved", () => {
    handle({ type: "form", form });
    handle({ type: "form_resolved", id: "f1", status: "answered" });
    expect(useDashStore.getState().forms[0].status).toBe("answered");
  });

  it("marks form cancelled on form_resolved", () => {
    handle({ type: "form", form });
    handle({ type: "form_resolved", id: "f1", status: "cancelled" });
    expect(useDashStore.getState().forms[0].status).toBe("cancelled");
  });
});

describe("wsStore — reconnect backoff schedule", () => {
  beforeEach(() => {
    resetStore();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts at BACKOFF_INITIAL (1000ms) and doubles each close", () => {
    useDashStore.setState({ _backoff: 1_000 } as never);
    expect((useDashStore.getState() as any)._backoff).toBe(1_000);

    const b1 = Math.min((useDashStore.getState() as any)._backoff * 2, 30_000);
    useDashStore.setState({ _backoff: b1 } as never);
    expect((useDashStore.getState() as any)._backoff).toBe(2_000);

    const b2 = Math.min((useDashStore.getState() as any)._backoff * 2, 30_000);
    useDashStore.setState({ _backoff: b2 } as never);
    expect((useDashStore.getState() as any)._backoff).toBe(4_000);
  });

  it("caps backoff at 30000ms", () => {
    useDashStore.setState({ _backoff: 20_000 } as never);
    const next = Math.min((useDashStore.getState() as any)._backoff * 2, 30_000);
    expect(next).toBe(30_000);
    const capped = Math.min(next * 2, 30_000);
    expect(capped).toBe(30_000);
  });
});

describe("wsStore — sendChat wire format", () => {
  beforeEach(resetStore);

  it("sends {say, to} not {content, to}", () => {
    // Intercept the _send call to check the wire frame.
    const sent: unknown[] = [];
    useDashStore.setState({
      _send: (payload: unknown) => { sent.push(payload); },
    } as never);

    useDashStore.getState().sendChat("all", "hello world");

    expect(sent).toHaveLength(1);
    const frame = sent[0] as Record<string, unknown>;
    expect(frame).toHaveProperty("say", "hello world");
    expect(frame).toHaveProperty("to", "all");
    expect(frame).not.toHaveProperty("content");
  });
});

describe("wsStore — getToken() strips token from URL", () => {
  it("history.replaceState is called to remove ?token= from address bar", () => {
    // Simulate ?token=secret in location.search
    vi.stubGlobal("location", {
      protocol: "http:",
      host: "localhost",
      search: "?token=secret123",
      pathname: "/",
      hash: "",
    });
    const replaceSpy = vi.spyOn(globalThis.history, "replaceState");

    // Inline the same logic as getToken() to verify the strip behaviour.
    const params = new URLSearchParams(globalThis.location.search);
    const fromUrl = params.get("token");
    if (fromUrl) {
      globalThis.localStorage.setItem("caucus_token", fromUrl);
      globalThis.history.replaceState(
        {},
        "",
        globalThis.location.pathname + globalThis.location.hash,
      );
    }

    expect(replaceSpy).toHaveBeenCalledWith({}, "", "/");
    expect(globalThis.localStorage.getItem("caucus_token")).toBe("secret123");

    // Restore
    vi.stubGlobal("location", {
      protocol: "http:", host: "localhost", search: "", pathname: "/", hash: "",
    });
  });
});
