/**
 * Unit tests for the agent-launcher slice of wsStore.ts.
 *
 * Spawning and killing a supervised agent are HTTP mutations (POST /agents,
 * DELETE /agents/{name}), not /ui frames — reusing the operator bearer token
 * captured from the /ui auth handshake. These tests pin:
 *   - the request shape (method, URL, headers, body) for both endpoints,
 *   - that the bearer token is attached when known and omitted when not
 *     (auth disabled), and
 *   - that a failed request surfaces an error toast and resolves to false
 *     rather than failing silently.
 *
 * fetch is stubbed per test via vi.stubGlobal; fireToast is mocked at the
 * module boundary (mock-prefixed per Vitest's hoisting rule for vi.mock
 * factories) so we can assert on it without a mounted ToastProvider.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const { mockFireToast } = vi.hoisted(() => ({ mockFireToast: vi.fn() }));
vi.mock("../../components/ToastProvider", () => ({
  fireToast: mockFireToast,
}));

import { useDashStore } from "../wsStore";
import type { SpawnAgentSpec } from "../types";

/** Seed the store's captured operator token ahead of an HTTP mutation call. */
function setAuthToken(token: string | null) {
  useDashStore.setState({ _authToken: token } as never);
}

const spec: SpawnAgentSpec = {
  name: "agent-a",
  type: "talker",
  permission_mode: "auto",
};

describe("wsStore — sendSpawnAgent", () => {
  beforeEach(() => {
    mockFireToast.mockClear();
  });

  it("POSTs /agents with the bearer token and the spec as JSON", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ agent: {} }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const ok = await useDashStore.getState().sendSpawnAgent(spec);

    expect(ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/agents");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({
      "Content-Type": "application/json",
      Authorization: "Bearer op-token",
    });
    expect(JSON.parse(init.body)).toEqual(spec);
  });

  it("omits the Authorization header when no token is known", async () => {
    setAuthToken(null);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore.getState().sendSpawnAgent(spec);

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers).not.toHaveProperty("Authorization");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json" });
  });

  it("surfaces an error toast and resolves false on a failed response", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: "agent ceiling reached" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const ok = await useDashStore.getState().sendSpawnAgent(spec);

    expect(ok).toBe(false);
    expect(mockFireToast).toHaveBeenCalledTimes(1);
    const [toastArg] = mockFireToast.mock.calls[0];
    expect(toastArg.variant).toBe("error");
    expect(toastArg.description).toBe("agent ceiling reached");
  });

  it("falls back to the HTTP status when the error body has no detail", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not JSON");
      },
    });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore.getState().sendSpawnAgent(spec);

    expect(mockFireToast.mock.calls[0][0].description).toBe("HTTP 500");
  });

  it("surfaces a toast and resolves false when fetch itself rejects", async () => {
    setAuthToken("op-token");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));

    const ok = await useDashStore.getState().sendSpawnAgent(spec);

    expect(ok).toBe(false);
    expect(mockFireToast).toHaveBeenCalledTimes(1);
    expect(mockFireToast.mock.calls[0][0].description).toBe("network down");
  });
});

describe("wsStore — sendKillAgent", () => {
  beforeEach(() => {
    mockFireToast.mockClear();
  });

  it("DELETEs /agents/{name} with the bearer token and no body", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    const ok = await useDashStore.getState().sendKillAgent("agent-a");

    expect(ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/agents/agent-a");
    expect(init.method).toBe("DELETE");
    expect(init.headers).toMatchObject({ Authorization: "Bearer op-token" });
    expect(init.headers).not.toHaveProperty("Content-Type");
    expect(init.body).toBeUndefined();
  });

  it("URL-encodes the agent name", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore.getState().sendKillAgent("weird name");

    expect(fetchMock.mock.calls[0][0]).toBe("/agents/weird%20name");
  });

  it("surfaces an error toast and resolves false on a failed response", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: "no such agent" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const ok = await useDashStore.getState().sendKillAgent("ghost");

    expect(ok).toBe(false);
    expect(mockFireToast).toHaveBeenCalledTimes(1);
    expect(mockFireToast.mock.calls[0][0].description).toBe("no such agent");
  });
});

describe("wsStore — auth_ok promotes the handshake token", () => {
  it("sets _authToken from _pendingToken once the hub confirms it", () => {
    useDashStore.setState({ _pendingToken: "handshake-token", _authToken: null } as never);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (useDashStore.getState() as any)._handleEvent({
      type: "auth_ok",
      role: "operator",
      auth: true,
    });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect((useDashStore.getState() as any)._authToken).toBe("handshake-token");
  });
});

describe("wsStore — agents event", () => {
  it("replaces the roster from the agents event payload", () => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (useDashStore.getState() as any)._handleEvent({
      type: "agents",
      agents: [
        {
          name: "agent-a",
          type: "talker",
          permission_mode: "auto",
          model: null,
          pid: 123,
          started_at: 1000,
          uptime_seconds: 5,
          state: "running",
          exit_code: null,
          peer_known: true,
        },
      ],
    });
    const agents = useDashStore.getState().agents;
    expect(agents).toHaveLength(1);
    expect(agents[0].name).toBe("agent-a");
    expect(agents[0].peer_known).toBe(true);
  });
});
