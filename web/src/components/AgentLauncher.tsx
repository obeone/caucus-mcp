/**
 * AgentLauncher — operator-only panel to spawn, list, and kill supervised
 * native Claude or OpenAI processes from the console.
 *
 * Two parts:
 *   - A spawn form (name, mission, type, permission mode, optional model,
 *     working directory) gated by client-side validation mirroring the hub's
 *     own refusals (see `lib/agentLauncher.ts`), so the operator sees a reason
 *     before the round trip rather than after a rejected request. The working
 *     directory is pre-filled with the hub's configured `--agent-cwd` and is
 *     editable per launch: the hub validates whatever is submitted on every
 *     spawn, so the field is operator intent, not a containment boundary (a
 *     `worker` with Bash leaves any directory with one `cd ..`).
 *   - A live roster below it: one row per supervised agent with its state, how
 *     it relates to the room (`peer_known` + `msg_count`), uptime, pid, and a
 *     kill button. Those two room fields sit here rather than in the Health
 *     panel because this is where the operator decides whether to kill
 *     something, and they are what distinguishes a healthy agent from a
 *     phantom: a wedged child keeps long-polling, so nothing else in the row
 *     gives it away.
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

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { fmtDuration } from "../lib/colors";
import { spawnFormError, type SpawnFormValues } from "../lib/agentLauncher";
import { useToast } from "./ToastProvider";
import AutocompleteDropdown from "./AutocompleteDropdown";
import type {
  AgentInfo,
  AgentRuntime,
  AgentType,
  PermissionMode,
} from "../store/types";
import {
  Bot,
  Rocket,
  X,
  Hash,
  Clock,
  Link2,
  Link2Off,
  MessageSquare,
} from "lucide-react";

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

/**
 * Debounce before asking the hub to complete the working-directory field.
 *
 * Long enough that typing a path does not fire one request per character,
 * short enough that a pause reads as instant.
 */
const CWD_COMPLETE_DEBOUNCE_MS = 150;

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

/**
 * How a running child relates to the room, read off `peer_known` + `msg_count`.
 *
 * The process being alive says almost nothing: a wedged agent keeps long-polling,
 * so its peer never goes stale and every other field in the row reads healthy.
 * These two fields are the only ones that separate the three cases the operator
 * actually has to tell apart before deciding whether to kill something.
 *
 * - `absent`  — the process runs but never joined. A crash during startup, a bad
 *   hub URL or token, or a missing `claude` extra.
 * - `silent`  — it joined and has said nothing. A mute permission mode, a dead
 *   SDK loop, a model that refused. This is the phantom, and it is the case
 *   `peer_known` alone gets actively wrong: the column reads "yes" beside a ghost.
 * - `talking` — it joined and has spoken. Healthy.
 */
type RoomLink = "absent" | "silent" | "talking";

/** Classify a running agent's relationship to the room. */
function roomLink(agent: AgentInfo): RoomLink {
  if (!agent.peer_known) return "absent";
  return (agent.msg_count ?? 0) > 0 ? "talking" : "silent";
}

/** Presentation for each {@link RoomLink} state: label, tooltip, colour. */
const ROOM_LINK_UI: Record<
  RoomLink,
  { label: string; title: string; className: string }
> = {
  absent: {
    label: "not joined",
    title: "The process is running but never registered with the hub",
    className: "text-red",
  },
  silent: {
    label: "joined, silent",
    title: "Joined the room and has not sent a single message",
    className: "text-amber",
  },
  talking: {
    label: "joined",
    title: "Joined the room and is sending messages",
    className: "text-green",
  },
};

/** Single roster row: state, room link, send count, uptime, pid, kill button. */
function AgentRow({ agent, onKill }: AgentRowProps) {
  const running = agent.state === "running";
  const link = ROOM_LINK_UI[roomLink(agent)];
  const sent = agent.msg_count ?? 0;

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
          {` · ${agent.runtime ?? "claude"}`}
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

      {/* Room link and send count, on the same line as the kill button's row
          block on purpose: this is where the operator decides, and making him
          cross-reference the Health panel by name is the work we claim to save
          him. Both facts are shown, never one: either alone leaves the three
          states ambiguous. */}
      {running && (
        <div className="flex items-center gap-3 text-[10px] font-mono pl-3.5">
          <span
            className={cn("flex items-center gap-1", link.className)}
            title={link.title}
          >
            {agent.peer_known ? <Link2 size={9} /> : <Link2Off size={9} />}
            {link.label}
          </span>
          <span
            className={cn(
              "flex items-center gap-1",
              sent > 0 ? "text-dim" : "text-amber"
            )}
            title={
              agent.peer_known
                ? "Messages this agent has sent to the room"
                : "No peer under this name, so nothing has been sent"
            }
          >
            <MessageSquare size={9} />
            {agent.peer_known ? `${sent} sent` : "—"}
          </span>
        </div>
      )}

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
// Working-directory field
// ---------------------------------------------------------------------------

interface CwdPathInputProps {
  /** Current field value, owned by the form so a spawn can reset it. */
  value: string;
  /** Called with the new value on every edit and on an accepted completion. */
  onChange: (value: string) => void;
  /** Id of the form's error paragraph, for `aria-describedby`. */
  describedBy?: string;
}

/**
 * The working-directory text input, with hub-backed directory completion.
 *
 * Completion is a convenience and is built so it can never get in the way: it
 * never fires on an empty field, a failed or empty response simply closes the
 * list, and nothing here gates the submit. The hub remains the authority on
 * whether a path is acceptable.
 *
 * Keyboard semantics are the operator composer's, because it is the same
 * interaction: Arrow keys move, Enter or Tab accepts, Escape closes, a click
 * accepts. Accepting appends a `/` so the next segment completes immediately,
 * which is also why the debounced fetch re-runs on an accepted value rather
 * than being suppressed.
 *
 * Escape has to both close the list and remember that it was closed, or the
 * next keystroke would reopen it with the previous candidates still in hand.
 */
function CwdPathInput({ value, onChange, describedBy }: CwdPathInputProps) {
  const fetchCwdCompletions = useDashStore((s) => s.fetchCwdCompletions);

  const [candidates, setCandidates] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [index, setIndex] = useState(0);
  // Set by Escape, cleared by the next edit: "the operator asked for silence".
  const [dismissed, setDismissed] = useState(false);

  const open = !dismissed && candidates.length > 0;

  // Debounced completion fetch. `live` guards against a response from a stale
  // prefix landing after a newer keystroke already superseded it.
  useEffect(() => {
    if (dismissed) return;
    if (value.trim().length === 0) {
      setCandidates([]);
      setTruncated(false);
      return;
    }

    let live = true;
    const timer = setTimeout(() => {
      void (async () => {
        const result = await fetchCwdCompletions(value);
        if (!live) return;
        if (!result || result.dirs.length === 0) {
          setCandidates([]);
          setTruncated(false);
          return;
        }
        setCandidates(result.dirs);
        setTruncated(result.truncated);
        setIndex(0);
      })();
    }, CWD_COMPLETE_DEBOUNCE_MS);

    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [value, dismissed, fetchCwdCompletions]);

  /** Take a candidate into the field, with a trailing slash for the next segment. */
  function accept(dir: string) {
    onChange(dir.endsWith("/") ? dir : `${dir}/`);
    // Drop the current list: the value change re-runs the fetch for the new
    // prefix, so the operator gets that directory's children next.
    setCandidates([]);
    setTruncated(false);
  }

  /** Close the list and keep it closed until the next edit. */
  function dismiss() {
    setDismissed(true);
    setCandidates([]);
    setTruncated(false);
  }

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (!open) return;

    if (e.key === "ArrowDown") {
      e.preventDefault();
      setIndex((i) => Math.min(i + 1, candidates.length - 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setIndex((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      accept(candidates[Math.min(index, candidates.length - 1)]);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
    }
  }

  return (
    <div className="relative">
      {open && (
        <AutocompleteDropdown
          candidates={candidates}
          selectedIndex={index}
          onAccept={accept}
          onSetIndex={setIndex}
          placement="below"
          ariaLabel="Directory suggestions"
          className="w-[22rem]"
          footer={truncated ? "more…" : undefined}
        />
      )}

      <input
        type="text"
        value={value}
        onChange={(e) => {
          setDismissed(false);
          onChange(e.target.value);
        }}
        onKeyDown={handleKeyDown}
        placeholder="working directory"
        aria-label="Agent working directory"
        aria-describedby={describedBy}
        aria-autocomplete="list"
        className={cn(
          "w-56 bg-bg text-ink border border-line rounded-sm",
          "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
          "placeholder:text-dim"
        )}
      />
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
  const agentCwd = useDashStore((s) => s.agentCwd);
  const sendSpawnAgent = useDashStore((s) => s.sendSpawnAgent);
  const sendKillAgent = useDashStore((s) => s.sendKillAgent);
  const { toast } = useToast();

  const [name, setName] = useState("");
  const [mission, setMission] = useState("");
  const [type, setType] = useState<AgentType>("talker");
  const [runtime, setRuntime] = useState<AgentRuntime>("claude");
  const [permissionMode, setPermissionMode] = useState<PermissionMode>("auto");
  const [model, setModel] = useState("");
  const [cwd, setCwd] = useState(agentCwd ?? "");
  // True while the spawn request is in flight, to prevent a double submit.
  const [spawning, setSpawning] = useState(false);

  // The hub's default arrives with the /ui snapshot, which can land after this
  // panel mounts, so pre-fill on arrival rather than only at mount. Once only:
  // a later snapshot must not overwrite a directory the operator has typed.
  const prefilled = useRef(agentCwd !== null);
  useEffect(() => {
    if (agentCwd === null || prefilled.current) return;
    prefilled.current = true;
    setCwd(agentCwd);
  }, [agentCwd]);

  // Operator-only guard.
  if (role !== "operator") return null;

  // ---------------------------------------------------------------------------
  // Validation
  // ---------------------------------------------------------------------------

  const formValues: SpawnFormValues = {
    name, mission, type, permissionMode, runtime, cwd,
  };
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
        runtime,
        mission: mission || undefined,
        type,
        permission_mode: permissionMode,
        model: model || undefined,
        cwd: cwd || undefined,
      });
      // A failed request already surfaced its own error toast from the
      // store; only confirm success and reset the form here.
      if (!ok) return;
      toast({ title: `Spawned ${name}`, variant: "success" });
      setName("");
      setMission("");
      setModel("");
      // Back to the hub's default rather than to empty: the next launch most
      // likely wants it, and a one-off directory should not become sticky.
      setCwd(agentCwd ?? "");
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
          <select
            value={runtime}
            onChange={(e) => {
              setRuntime(e.target.value as AgentRuntime);
              setPermissionMode("auto");
            }}
            aria-label="Agent runtime"
            className="bg-bg text-ink border border-line rounded-sm text-xs font-mono px-2 py-1"
          >
            <option value="claude">Claude</option>
            <option value="openai">OpenAI — API key</option>
            <option value="codex">Codex — ChatGPT subscription</option>
          </select>
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
            {PERMISSION_MODES.filter(
              (m) => runtime === "claude" || !["bypassPermissions", "dontAsk"].includes(m)
            ).map((m) => (
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

          <CwdPathInput
            value={cwd}
            onChange={setCwd}
            describedBy={error ? "agent-launcher-error" : undefined}
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
