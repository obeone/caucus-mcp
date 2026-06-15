/**
 * HealthPanel — peer roster with live state indicators.
 *
 * Shows a grid of PeerInfo cards: name, state (live/idle/reaped) colour-coded,
 * status text, last_seen age, listening indicator. Hover tooltip shows uptime
 * and msg_count. Clicking a peer selects it for cross-panel linking.
 */

import { useCallback } from "react";
import { useDashStore } from "../store/wsStore";
import { colorFor, fmtDuration } from "../lib/colors";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import type { PeerInfo } from "../store/types";
import {
  Activity,
  Radio,
  PauseCircle,
  Heart,
  X,
  Play,
  Pause,
} from "lucide-react";

/** Colour class for a given peer state. */
function stateColor(peer: PeerInfo): string {
  if (peer.state === "reaped") return "text-red";
  if (peer.paused) return "text-amber";
  if (peer.listening) return "text-green";
  return "text-dim";
}

/** Human-readable state label. */
function stateLabel(peer: PeerInfo): string {
  if (peer.state === "reaped") return "reaped";
  if (peer.paused) return "paused";
  if (peer.listening) return "live";
  return "idle";
}

interface PeerCardProps {
  peer: PeerInfo;
  selected: boolean;
  onSelect: (name: string) => void;
  showUTC: boolean;
  role: "operator" | "observer";
  onKick: (name: string) => void;
  onPause: (name: string) => void;
  onResume: (name: string) => void;
  onHeartbeat: (name: string) => void;
}

/** Single peer card in the health grid. */
function PeerCard({
  peer,
  selected,
  onSelect,
  role,
  onKick,
  onPause,
  onResume,
  onHeartbeat,
}: PeerCardProps) {
  const accent = colorFor(peer.name);
  const isOperator = role === "operator";

  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      aria-label={`Peer ${peer.name} — ${stateLabel(peer)}`}
      onClick={() => onSelect(peer.name)}
      onKeyDown={(e) => e.key === "Enter" && onSelect(peer.name)}
      className={cn(
        "relative flex flex-col gap-2 p-3 rounded-sm border cursor-pointer transition-all",
        "bg-panel-2 hover:bg-panel",
        selected ? "border-cyan shadow-[0_0_12px_-4px_#38c6d9]" : "border-line"
      )}
      style={{ borderLeftColor: selected ? undefined : accent, borderLeftWidth: 3 }}
      title={`Uptime: ${fmtDuration(peer.uptime)} | Messages sent: ${peer.msg_count}`}
    >
      {/* Name + state dot */}
      <div className="flex items-center gap-2 min-w-0">
        <span
          className={cn(
            "w-2 h-2 rounded-full flex-shrink-0",
            peer.state === "reaped"
              ? "bg-red shadow-[0_0_6px_#ff4d5e]"
              : peer.paused
              ? "bg-amber shadow-[0_0_6px_#ffb22e]"
              : peer.listening
              ? "bg-green shadow-[0_0_6px_#4fd67a] animate-pulse"
              : "bg-dim"
          )}
          aria-hidden="true"
        />
        <span
          className="font-mono text-sm font-semibold truncate"
          style={{ color: accent }}
        >
          {peer.name}
        </span>
        <span className={cn("text-[10px] font-mono ml-auto", stateColor(peer))}>
          {stateLabel(peer)}
        </span>
      </div>

      {/* Status text */}
      {peer.status && (
        <p className="text-[11px] font-mono text-dim italic truncate pl-4">
          {peer.status}
        </p>
      )}

      {/* Meta row */}
      <div className="flex items-center gap-3 text-[10px] font-mono text-dim pl-4">
        {peer.listening && (
          <span className="flex items-center gap-1 text-cyan" title="Listening">
            <Radio size={10} />
            listening
          </span>
        )}
        {peer.last_seen_age !== null && (
          <span title="Last seen">
            seen {peer.last_seen_age.toFixed(1)}s ago
          </span>
        )}
        <span title="Messages sent" className="ml-auto">
          {peer.msg_count} msgs
        </span>
      </div>

      {/* Operator action buttons */}
      {isOperator && peer.state !== "reaped" && (
        <div className="flex gap-1 pt-1 border-t border-line/50">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onHeartbeat(peer.name);
            }}
            className="flex-1 text-[10px] font-mono text-dim hover:text-cyan border border-transparent hover:border-line rounded-sm px-1 py-0.5 transition-all flex items-center justify-center gap-1"
            aria-label={`Send heartbeat to ${peer.name}`}
            title="Heartbeat ping"
          >
            <Heart size={9} />
            ping
          </button>
          {peer.paused ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onResume(peer.name);
              }}
              className="flex-1 text-[10px] font-mono text-green hover:bg-green/10 border border-transparent hover:border-green/40 rounded-sm px-1 py-0.5 transition-all flex items-center justify-center gap-1"
              aria-label={`Resume ${peer.name}`}
            >
              <Play size={9} />
              resume
            </button>
          ) : (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onPause(peer.name);
              }}
              className="flex-1 text-[10px] font-mono text-amber hover:bg-amber/10 border border-transparent hover:border-amber/40 rounded-sm px-1 py-0.5 transition-all flex items-center justify-center gap-1"
              aria-label={`Pause ${peer.name}`}
            >
              <Pause size={9} />
              pause
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onKick(peer.name);
            }}
            className="flex-1 text-[10px] font-mono text-dim hover:text-red hover:bg-red/10 border border-transparent hover:border-red/40 rounded-sm px-1 py-0.5 transition-all flex items-center justify-center gap-1"
            aria-label={`Kick ${peer.name}`}
          >
            <X size={9} />
            kick
          </button>
        </div>
      )}
    </div>
  );
}

export default function HealthPanel() {
  const peers = useDashStore((s) => s.peers);
  const selectedPeer = useDashStore((s) => s.selectedPeer);
  const showUTC = useDashStore((s) => s.showUTC);
  const role = useDashStore((s) => s.role);
  const setSelectedPeer = useDashStore((s) => s.setSelectedPeer);
  const sendKick = useDashStore((s) => s.sendKick);
  const sendPausePeer = useDashStore((s) => s.sendPausePeer);
  const sendResumePeer = useDashStore((s) => s.sendResumePeer);
  const sendHeartbeat = useDashStore((s) => s.sendHeartbeat);
  const health = useDashStore((s) => s.health);
  const { toast } = useToast();

  const handleSelect = useCallback(
    (name: string) => {
      setSelectedPeer(selectedPeer === name ? null : name);
    },
    [selectedPeer, setSelectedPeer]
  );

  const handleKick = useCallback(
    (name: string) => {
      if (!confirm(`Kick peer "${name}"?`)) return;
      sendKick(name);
      toast({ title: `Kicked ${name}`, variant: "default" });
    },
    [sendKick, toast]
  );

  const handlePause = useCallback(
    (name: string) => {
      sendPausePeer(name);
      toast({ title: `Paused delivery for ${name}`, variant: "default" });
    },
    [sendPausePeer, toast]
  );

  const handleResume = useCallback(
    (name: string) => {
      sendResumePeer(name);
      toast({ title: `Resumed delivery for ${name}`, variant: "success" });
    },
    [sendResumePeer, toast]
  );

  const handleHeartbeat = useCallback(
    (name: string) => {
      sendHeartbeat(name);
      toast({ title: `Heartbeat sent to ${name}`, description: "Waiting for result…" });
    },
    [sendHeartbeat, toast]
  );

  const live = peers.filter((p) => p.state === "live");
  const reaped = peers.filter((p) => p.state === "reaped");

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Stats bar */}
      <div className="flex items-center gap-6 px-5 py-2.5 border-b border-line bg-panel text-[11px] font-mono text-dim flex-shrink-0">
        <span className="flex items-center gap-1.5">
          <Activity size={11} className="text-cyan" />
          {live.length} live
        </span>
        {reaped.length > 0 && (
          <span className="text-red">{reaped.length} reaped</span>
        )}
        {peers.filter((p) => p.paused).length > 0 && (
          <span className="flex items-center gap-1 text-amber">
            <PauseCircle size={11} />
            {peers.filter((p) => p.paused).length} paused
          </span>
        )}
        {health && (
          <span className="ml-auto">{health.msg_per_min} msg/min</span>
        )}
        {selectedPeer && (
          <button
            onClick={() => setSelectedPeer(null)}
            className="text-dim hover:text-ink text-[10px] tracking-widest"
            aria-label="Clear peer selection"
          >
            clear selection
          </button>
        )}
      </div>

      {/* Peer grid */}
      <div
        className="flex-1 overflow-y-auto p-4"
        role="list"
        aria-label="Connected peers"
      >
        {peers.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-dim font-mono text-sm">
            — no peers connected —
          </div>
        ) : (
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {peers.map((peer) => (
              <div key={peer.name} role="listitem">
                <PeerCard
                  peer={peer}
                  selected={selectedPeer === peer.name}
                  onSelect={handleSelect}
                  showUTC={showUTC}
                  role={role}
                  onKick={handleKick}
                  onPause={handlePause}
                  onResume={handleResume}
                  onHeartbeat={handleHeartbeat}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
