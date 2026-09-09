/**
 * AgentLauncher — operator-only panel to spawn, list, and kill supervised
 * `caucus-claude-agent` processes from the console.
 *
 * Two parts:
 *   - A spawn form (name, mission, type, permission mode, optional model)
 *     gated by client-side validation mirroring the hub's own refusals (see
 *     `lib/agentLauncher.ts`), so the operator sees a reason before the round
 *     trip rather than after a rejected request. There is no working-directory
 *     field: the hub fixes it at startup and rejects an unknown `cwd` in the
 *     request body.
 *   - A live roster below it: one row per supervised agent with its state,
 *     uptime, pid, and a kill button.
 *
 * Spawning and killing go over HTTP (`POST /agents`, `DELETE /agents/{name}`),
 * not the `/ui` socket, so both are awaited and a failed request surfaces its
 * own error toast from the store; the roster itself keeps arriving over the
 * socket via the `agents` event.
 *
 * Operator-only: returns null when `role !== "operator"`, exactly like
 * `RateControl`. The hub still enforces every rule server-side; this panel
 * is pure UX plus the API calls.
 */

import { useState } from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { fmtDuration } from "../lib/colors";
import { spawnFormError, type SpawnFormValues } from "../lib/agentLauncher";
import { useToast } from "./ToastProvider";
import type { AgentInfo, AgentType, PermissionMode } from "../store/types";
import { Bot, Rocket, X, Hash, Clock } from "lucide-react";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const AGENT_TYPES: AgentType[] = ["talker", "worker"];

const PERMISSION_MODES: PermissionMode[] = [
  "auto",
  "default",
  "acceptEdits",
  "plan",
  "bypassPermissions",
  "dontAsk",
];

// ---------------------------------------------------------------------------
// Roster row
// ---------------------------------------------------------------------------

interface AgentRowProps {
  agent: AgentInfo;
  onKill: (name: string) => void;
}

/** Colour class for a given agent's lifecycle state. */
function stateColor(agent: AgentInfo): string {
  if (agent.state === "exited") {
    return agent.exit_code === 0 ? "text-dim" : "text-red";
  }
  return "text-green";
}

/** Single roster row: state, uptime, pid, kill button. */
function AgentRow({ agent, onKill }: AgentRowProps) {
  const running = agent.state === "running";

  return (
    <div
      className="flex flex-col gap-1 px-2 py-1.5 border-b border-line/30 last:border-b-0"
      role="listitem"
    >
      <div className="flex items-center gap-2 min-w-0">
        <span
          className={cn(
            "w-1.5 h-1.5 rounded-full flex-shrink-0",
            running ? "bg-green animate-pulse" : "bg-dim"
          )}
          aria-hidden="true"
        />
        <span className="font-mono text-[11px] font-semibold truncate">
          {agent.name}
        </span>
        <span className="text-[9px] font-mono text-dim uppercase">
          {agent.type}
        </span>
        <span className={cn("text-[9px] font-mono ml-auto", stateColor(agent))}>
          {agent.state}
          {agent.state === "exited" &&
            agent.exit_code !== null &&
            ` (${agent.exit_code})`}
        </span>
        {running && (
          <button
            onClick={() => onKill(agent.name)}
            className="p-0.5 text-dim hover:text-red hover:bg-red/10 rounded-sm transition-colors flex-shrink-0"
            aria-label={`Kill agent ${agent.name}`}
            title="Kill agent"
          >
            <X size={11} />
          </button>
        )}
      </div>

      <div className="flex items-center gap-3 text-[10px] font-mono text-dim pl-3.5">
        <span className="flex items-center gap-1" title="Uptime">
          <Clock size={9} />
          {fmtDuration(agent.uptime_seconds)}
        </span>
        <span className="flex items-center gap-1" title="Process ID">
          <Hash size={9} />
          {agent.pid}
        </span>
        <span title="Permission mode">{agent.permission_mode}</span>
        {agent.model && <span title="Model">{agent.model}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * AgentLauncher panel — operator-only.
 *
 * Reads `agents` from the Zustand store and exposes a spawn form plus a kill
 * action per row, via `sendSpawnAgent` / `sendKillAgent`.
 */
export default function AgentLauncher() {
  const role = useDashStore((s) => s.role);
  const agents = useDashStore((s) => s.agents);
  const sendSpawnAgent = useDashStore((s) => s.sendSpawnAgent);
  const sendKillAgent = useDashStore((s) => s.sendKillAgent);
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [mission, setMission] = useState("");
  const [type, setType] = useState<AgentType>("talker");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("auto");
  const [model, setModel] = useState("");
  // True while the spawn request is in flight, to prevent a double submit.
  const [spawning, setSpawning] = useState(false);

  // Operator-only guard.
  if (role !== "operator") return null;

  // ---------------------------------------------------------------------------
  // Validation
  // ---------------------------------------------------------------------------

  const formValues: SpawnFormValues = { name, mission, type, permissionMode };
  const error = spawnFormError(formValues);
  const isValid = error === null;

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  async function handleSpawn() {
    if (!isValid || spawning) return;
    setSpawning(true);
    try {
      const ok = await sendSpawnAgent({
        name,
        mission: mission || undefined,
        type,
        permission_mode: permissionMode,
        model: model || undefined,
      });
      // A failed request already surfaced its own error toast from the
      // store; only confirm success and reset the form here.
      if (!ok) return;
      toast({ title: `Spawned ${name}`, variant: "success" });
      setName("");
      setMission("");
      setModel("");
    } finally {
      setSpawning(false);
    }
  }

  async function handleKill(agentName: string) {
    if (!confirm(`Kill agent "${agentName}"?`)) return;
    const ok = await sendKillAgent(agentName);
    if (!ok) return;
    toast({ title: `Killed ${agentName}`, variant: "success" });
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div
      className="border border-line rounded-sm bg-panel-2 p-3 flex flex-col gap-2"
      role="region"
      aria-label="Agent launcher"
    >
      {/* Section header */}
      <div className="flex items-center gap-1.5 text-[10px] font-chrome font-bold tracking-[2px] uppercase text-dim">
        <Bot size={11} aria-hidden="true" />
        Agent Launcher
      </div>

      {/* Spawn form */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2 flex-wrap">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="name"
            aria-label="Agent name"
            aria-describedby={error ? "agent-launcher-error" : undefined}
            className={cn(
              "w-32 bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
              "placeholder:text-dim"
            )}
          />

          <select
            value={type}
            onChange={(e) => setType(e.target.value as AgentType)}
            aria-label="Agent type"
            className={cn(
              "bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
              "cursor-pointer"
            )}
          >
            {AGENT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>

          <select
            value={permissionMode}
            onChange={(e) => setPermissionMode(e.target.value as PermissionMode)}
            aria-label="Agent permission mode"
            className={cn(
              "bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
              "cursor-pointer"
            )}
          >
            {PERMISSION_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>

          <input
            type="text"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="model (optional)"
            aria-label="Agent model (optional)"
            className={cn(
              "w-32 bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
              "placeholder:text-dim"
            )}
          />
        </div>

        <textarea
          value={mission}
          onChange={(e) => setMission(e.target.value)}
          placeholder="mission"
          aria-label="Agent mission"
          rows={2}
          className={cn(
            "w-full bg-bg text-ink border border-line rounded-sm resize-y",
            "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
            "placeholder:text-dim"
          )}
        />

        <div className="flex items-center gap-2">
          <button
            onClick={handleSpawn}
            disabled={!isValid || spawning}
            aria-label="Spawn agent"
            className={cn(
              "font-chrome font-bold tracking-widest text-[10px] uppercase",
              "px-3 py-1 rounded-sm border transition-all flex items-center gap-1.5",
              isValid && !spawning
                ? "border-cyan text-cyan hover:bg-cyan/10 shadow-[0_0_10px_-4px_#38c6d9]"
                : "border-line text-dim cursor-not-allowed opacity-50"
            )}
          >
            <Rocket size={10} aria-hidden="true" />
            {spawning ? "Spawning…" : "Spawn"}
          </button>

          {error && (
            <p id="agent-launcher-error" className="text-[10px] font-mono text-red">
              {error}
            </p>
          )}
        </div>
      </div>

      {/* Roster. `role="list"` only applies once there is at least one
          `listitem` row: an empty roster uses `role="group"` instead so the
          panel keeps its accessible name without tripping axe's
          aria-required-children check on a list with no list items. */}
      <div
        className="flex flex-col border-t border-line/40 pt-1.5 max-h-48 overflow-y-auto"
        role={agents.length > 0 ? "list" : "group"}
        aria-label="Supervised agents"
      >
        {agents.length === 0 ? (
          <p className="text-[10px] font-mono text-dim/60 px-2 py-1">
            No supervised agents.
          </p>
        ) : (
          agents.map((agent) => (
            <AgentRow key={agent.name} agent={agent} onKill={handleKill} />
          ))
        )}
      </div>
    </div>
  );
}
