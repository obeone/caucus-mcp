/**
 * Client-side validation for the operator agent-launcher spawn form.
 *
 * Mirrors the server-side refusals in `AgentSupervisor.spawn()` (see
 * `src/caucus/supervisor.py`) so the operator sees a reason before the round
 * trip, rather than after a rejected request. The hub is still the source of
 * truth — these checks are pure UX, not a security boundary.
 */

import type { AgentType, PermissionMode } from "../store/types";

/** Agent name pattern: must start with a letter or digit, then up to 63 more
 *  letters, digits, dots, underscores, or hyphens. Mirrors `AGENT_NAME_RE`. */
export const AGENT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

/** Maximum mission length in characters, mirroring `MAX_MISSION_CHARS`. */
export const MAX_MISSION_CHARS = 4_000;

/**
 * Permission modes that remove the operator's approval guardrail. Combined
 * with a `worker` agent type (which gets shell and filesystem tools), this
 * lets the agent act without any human check.
 */
export const UNSAFE_WORKER_PERMISSION_MODES: readonly PermissionMode[] = [
  "bypassPermissions",
  "dontAsk",
];

/** Values the spawn form's fields carry, ahead of submission. */
export interface SpawnFormValues {
  name: string;
  mission: string;
  type: AgentType;
  permissionMode: PermissionMode;
}

/** Whether `name` matches the agent-name pattern the hub requires. */
export function isValidAgentName(name: string): boolean {
  return AGENT_NAME_RE.test(name);
}

/** Whether `mission` exceeds the character cap the hub enforces. */
export function isMissionTooLong(mission: string): boolean {
  return mission.length > MAX_MISSION_CHARS;
}

/**
 * Whether `type` + `permissionMode` is the refused worker combination: a
 * worker agent with no approval guardrail on its shell and filesystem tools.
 */
export function isUnsafeWorkerCombo(
  type: AgentType,
  permissionMode: PermissionMode
): boolean {
  return (
    type === "worker" && UNSAFE_WORKER_PERMISSION_MODES.includes(permissionMode)
  );
}

/**
 * Validate the spawn form, returning the first human-readable error found,
 * or `null` when the form is ready to submit.
 *
 * @param values - The current form field values.
 * @returns A user-facing error string, or `null` when valid.
 */
export function spawnFormError(values: SpawnFormValues): string | null {
  if (values.name.length === 0) {
    return "Name is required.";
  }
  if (!isValidAgentName(values.name)) {
    return "Name must start with a letter or digit, followed by up to 63 letters, digits, dots, underscores, or hyphens.";
  }
  if (isMissionTooLong(values.mission)) {
    return `Mission must be at most ${MAX_MISSION_CHARS} characters (currently ${values.mission.length}).`;
  }
  if (isUnsafeWorkerCombo(values.type, values.permissionMode)) {
    return "A worker agent cannot use bypassPermissions or dontAsk: those modes remove the only guardrail around its shell and filesystem tools.";
  }
  return null;
}
