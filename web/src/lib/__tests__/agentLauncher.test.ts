/**
 * Unit tests for lib/agentLauncher.ts — client-side validation for the
 * operator agent-launcher spawn form.
 *
 * These mirror the hub's own refusals (AGENT_NAME_RE, MAX_MISSION_CHARS,
 * UNGUARDED_PERMISSION_MODES in src/caucus/supervisor.py) so a regression here
 * would let the operator submit a request the hub is guaranteed to reject.
 */

import { describe, it, expect } from "vitest";
import {
  isValidAgentName,
  isMissionTooLong,
  isUnsafeWorkerCombo,
  spawnFormError,
  MAX_MISSION_CHARS,
  type SpawnFormValues,
} from "../agentLauncher";

describe("isValidAgentName", () => {
  it("accepts a plain alphanumeric name", () => {
    expect(isValidAgentName("agent1")).toBe(true);
  });

  it("accepts dots, underscores, and hyphens after the first character", () => {
    expect(isValidAgentName("a.b_c-d")).toBe(true);
  });

  it("rejects an empty name", () => {
    expect(isValidAgentName("")).toBe(false);
  });

  it("rejects a name starting with a hyphen", () => {
    expect(isValidAgentName("-agent")).toBe(false);
  });

  it("rejects a name containing a space", () => {
    expect(isValidAgentName("agent one")).toBe(false);
  });

  it("rejects a name containing a slash", () => {
    expect(isValidAgentName("agent/one")).toBe(false);
  });

  it("rejects a name longer than 64 characters", () => {
    expect(isValidAgentName("a".repeat(65))).toBe(false);
  });

  it("accepts a name exactly 64 characters long", () => {
    expect(isValidAgentName("a".repeat(64))).toBe(true);
  });
});

describe("isMissionTooLong", () => {
  it("accepts an empty mission", () => {
    expect(isMissionTooLong("")).toBe(false);
  });

  it("accepts a mission at exactly the cap", () => {
    expect(isMissionTooLong("a".repeat(MAX_MISSION_CHARS))).toBe(false);
  });

  it("rejects a mission one character past the cap", () => {
    expect(isMissionTooLong("a".repeat(MAX_MISSION_CHARS + 1))).toBe(true);
  });
});

describe("isUnsafeWorkerCombo", () => {
  it("refuses a worker with bypassPermissions", () => {
    expect(isUnsafeWorkerCombo("worker", "bypassPermissions")).toBe(true);
  });

  it("refuses a worker with dontAsk", () => {
    expect(isUnsafeWorkerCombo("worker", "dontAsk")).toBe(true);
  });

  it("allows a worker with auto", () => {
    expect(isUnsafeWorkerCombo("worker", "auto")).toBe(false);
  });

  it("allows a talker with bypassPermissions (no shell/filesystem tools at stake)", () => {
    expect(isUnsafeWorkerCombo("talker", "bypassPermissions")).toBe(false);
  });
});

describe("spawnFormError", () => {
  const base: SpawnFormValues = {
    name: "agent-a",
    mission: "",
    type: "talker",
    permissionMode: "auto",
  };

  it("returns null for a valid form", () => {
    expect(spawnFormError(base)).toBeNull();
  });

  it("flags an empty name before checking the pattern", () => {
    expect(spawnFormError({ ...base, name: "" })).toBe("Name is required.");
  });

  it("flags an invalid name pattern", () => {
    expect(spawnFormError({ ...base, name: "bad name" })).toMatch(/letter or digit/);
  });

  it("flags an over-long mission", () => {
    expect(
      spawnFormError({ ...base, mission: "a".repeat(MAX_MISSION_CHARS + 1) })
    ).toMatch(/at most/);
  });

  it("flags the unsafe worker + bypassPermissions combination", () => {
    expect(
      spawnFormError({ ...base, type: "worker", permissionMode: "bypassPermissions" })
    ).toMatch(/guardrail/);
  });
});
