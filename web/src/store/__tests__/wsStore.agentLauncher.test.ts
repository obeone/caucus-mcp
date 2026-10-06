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

  it("sends a non-empty per-spawn cwd in the body", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore
      .getState()
      .sendSpawnAgent({ ...spec, cwd: "/srv/projects/alpha" });

    const [, init] = fetchMock.mock.calls[0];
    expect(JSON.parse(init.body).cwd).toBe("/srv/projects/alpha");
  });

  it("trims a per-spawn cwd before sending it", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore.getState().sendSpawnAgent({ ...spec, cwd: "  /srv/alpha  " });

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).cwd).toBe("/srv/alpha");
  });

  it.each([undefined, "", "   "])(
    "omits cwd entirely when the field is %p, so the hub uses its default",
    async (cwd) => {
      setAuthToken("op-token");
      const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
      vi.stubGlobal("fetch", fetchMock);

      await useDashStore.getState().sendSpawnAgent({ ...spec, cwd });

      const body = JSON.parse(fetchMock.mock.calls[0][1].body);
      expect(body).not.toHaveProperty("cwd");
    }
  );
});

describe("wsStore — fetchCwdCompletions", () => {
  beforeEach(() => {
    mockFireToast.mockClear();
  });

  it("GETs /agents/cwd-complete with the prefix and the bearer token", async () => {
    setAuthToken("op-token");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ dirs: ["/srv/a", "/srv/b"], truncated: false }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await useDashStore.getState().fetchCwdCompletions("/srv/");

    expect(result).toEqual({ dirs: ["/srv/a", "/srv/b"], truncated: false });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/agents/cwd-complete?prefix=%2Fsrv%2F");
    expect(init.headers).toMatchObject({ Authorization: "Bearer op-token" });
    expect(init.headers).not.toHaveProperty("Content-Type");
  });

  it("omits the Authorization header when no token is known", async () => {
    setAuthToken(null);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ dirs: [], truncated: false }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await useDashStore.getState().fetchCwdCompletions("/srv/");

    expect(fetchMock.mock.calls[0][1].headers).not.toHaveProperty("Authorization");
  });

  it("carries the truncated flag through", async () => {
    setAuthToken("op-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ dirs: ["/srv/a"], truncated: true }),
      })
    );

    const result = await useDashStore.getState().fetchCwdCompletions("/srv/");

    expect(result?.truncated).toBe(true);
  });

  // Completion is a convenience: every failure mode has to degrade to "no
  // suggestions" with no toast, or a refused completion would nag an operator
  // who can still type the path by hand.
  it.each([403, 400, 500])("resolves null and stays silent on HTTP %i", async (status) => {
    setAuthToken("op-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({}) })
    );

    expect(await useDashStore.getState().fetchCwdCompletions("/srv/")).toBeNull();
    expect(mockFireToast).not.toHaveBeenCalled();
  });

  it("resolves null and stays silent when fetch itself rejects", async () => {
    setAuthToken("op-token");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));

    expect(await useDashStore.getState().fetchCwdCompletions("/srv/")).toBeNull();
    expect(mockFireToast).not.toHaveBeenCalled();
  });

  it("resolves null when the body carries no dirs array", async () => {
    setAuthToken("op-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ dirs: "nope" }) })
    );

    expect(await useDashStore.getState().fetchCwdCompletions("/srv/")).toBeNull();
  });

  it("drops non-string entries rather than rendering them", async () => {
    setAuthToken("op-token");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ dirs: ["/srv/a", 7, null], truncated: false }),
      })
    );

    expect(await useDashStore.getState().fetchCwdCompletions("/srv/")).toEqual({
      dirs: ["/srv/a"],
      truncated: false,
    });
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

describe("wsStore — snapshot agent_cwd", () => {
  /** Feed a minimal snapshot through the real event path. */
  function snapshot(extra: Record<string, unknown>) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (useDashStore.getState() as any)._handleEvent({
      type: "snapshot",
      mode: "running",
      peers: [],
      channels: {},
      floors: {},
      forms: [],
      log: [],
      health: null,
      ...extra,
    });
  }

  it("stores the hub's default working directory for the form to pre-fill", () => {
    snapshot({ agent_cwd: "/srv/projects" });
    expect(useDashStore.getState().agentCwd).toBe("/srv/projects");
  });

  // The hub withholds the field from an observer and from a launcher-less hub;
  // either way there is no default, which must not linger from a past snapshot.
  it("falls back to null when the field is absent", () => {
    snapshot({ agent_cwd: "/srv/projects" });
    snapshot({});
    expect(useDashStore.getState().agentCwd).toBeNull();
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
          msg_count: 4,
        },
      ],
    });
    const agents = useDashStore.getState().agents;
    expect(agents).toHaveLength(1);
    expect(agents[0].name).toBe("agent-a");
    expect(agents[0].peer_known).toBe(true);
    expect(agents[0].msg_count).toBe(4);
  });
});
