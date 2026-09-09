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

/**
 * Permission modes in which a supervised child can never say a word, mirroring
 * `MUTE_PERMISSION_MODES`.
 *
 * Neither permits the `mcp__caucus__*` tools up front, so `say` is out of reach
 * until an approval arrives, and the hub spawns children with stdin closed, so
 * no approval ever can. The agent would join, look healthy in the roster, and
 * stay silent. Refused for both agent types.
 */
export const MUTE_PERMISSION_MODES: readonly PermissionMode[] = [
  "plan",
  "default",
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

/** Whether `permissionMode` is one the agent could never speak in. */
export function isMutePermissionMode(permissionMode: PermissionMode): boolean {
  return MUTE_PERMISSION_MODES.includes(permissionMode);
}

/**
 * Validate the spawn form, returning the first human-readable error found,
 * or `null` when the form is ready to submit.
 *
 * These checks are a pre-flight mirror, not a gate. Anything they miss is still
 * refused by the hub, and the store surfaces that refusal's own `detail` text in
 * an error toast, so the operator reads the server's reason rather than a bare
 * status code.
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
  if (isMutePermissionMode(values.permissionMode)) {
    return `An agent started in ${values.permissionMode} cannot speak in the room: the caucus tools are not permitted to it and no approval can reach it, so it would sit in the roster looking healthy and stay silent.`;
  }
  return null;
}
