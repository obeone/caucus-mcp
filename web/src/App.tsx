/**
 * Root application component.
 *
 * Layout (tab-free two-column design):
 *
 *   ┌─ header ───────────────────────────────────────────────────────────────┐
 *   │ brand · mode badge · connection · health stats · FormsAlert · controls │
 *   └────────────────────────────────────────────────────────────────────────┘
 *   ┌─ DisconnectedBanner (conditional) ─────────────────────────────────────┐
 *   └────────────────────────────────────────────────────────────────────────┘
 *   ┌─ left rail ~260px ───┐ ┌─ main ──────────────────────────────────────┐
 *   │ HEALTH (compact)      │ │ FloorStrip (amber, when floors active)      │
 *   │  ─ peer rows          │ │ FlowPanel  (flex-1)                         │
 *   ├── CHANNELS (compact) ─┤ │ OperatorComposer (pinned bottom)            │
 *   │  ─ channel rows       │ └─────────────────────────────────────────────┘
 *   └──────────────────────┘
 *
 * FormsAlert renders as an amber badge in the header when pending forms exist;
 * clicking it opens a dialog list → FormModal workflow.
 */

import { useCallback } from "react";
import { useDashStore } from "./store/wsStore";
import { cn } from "./lib/utils";
import { fmtDuration } from "./lib/colors";
import HealthPanel from "./components/HealthPanel";
import FlowPanel from "./components/FlowPanel";
import ChannelsPanel from "./components/ChannelsPanel";
import FormsAlert from "./components/FormsAlert";
import DisconnectedBanner from "./components/DisconnectedBanner";
import FloorStrip from "./components/FloorStrip";
import OperatorComposer from "./components/OperatorComposer";
import ToastProvider from "./components/ToastProvider";
import { Moon, Sun, Wifi, WifiOff, HelpCircle, BookOpen } from "lucide-react";

export default function App() {
  const mode = useDashStore((s) => s.mode);
  const connectionState = useDashStore((s) => s.connectionState);
  const health = useDashStore((s) => s.health);
  const darkMode = useDashStore((s) => s.darkMode);
  const showUTC = useDashStore((s) => s.showUTC);
  const role = useDashStore((s) => s.role);
  const sendMode = useDashStore((s) => s.sendMode);
  const setDarkMode = useDashStore((s) => s.setDarkMode);
  const setShowUTC = useDashStore((s) => s.setShowUTC);

  const handleModeAction = useCallback(
    (action: "pause" | "resume" | "reset" | "stop") => {
      if (role === "observer") return;
      sendMode(action);
    },
    [role, sendMode]
  );

  return (
    <ToastProvider>
      <div className="flex flex-col h-screen overflow-hidden bg-bg text-ink">
        {/* ── Header ──────────────────────────────────────────────────────── */}
        <header className="flex items-center gap-4 px-5 py-3 border-b border-line bg-panel-2 flex-shrink-0 flex-wrap">
          {/* Brand */}
          <div
            className="font-chrome font-bold tracking-[4px] text-lg text-ink"
            aria-label="Caucus operator console"
          >
            CAU<span className="text-amber">CUS</span>
          </div>

          {/* Mode badge */}
          <div
            aria-live="polite"
            className={cn(
              "font-chrome font-bold tracking-[3px] px-3 py-1 rounded-sm border text-xs uppercase",
              mode === "running" &&
                "text-green border-green shadow-[0_0_16px_-6px_#4fd67a]",
              mode === "paused" &&
                "text-amber border-amber shadow-[0_0_16px_-6px_#ffb22e] animate-blink",
              mode === "stopped" && "text-red border-red shadow-[0_0_16px_-6px_#ff4d5e]"
            )}
          >
            {mode}
          </div>

          {/* Connection indicator */}
          <div
            className={cn(
              "flex items-center gap-1.5 text-xs font-mono",
              connectionState === "connected" ? "text-green" : "text-red"
            )}
            aria-label={`Connection: ${connectionState}`}
          >
            {connectionState === "connected" ? (
              <Wifi size={13} />
            ) : (
              <WifiOff size={13} />
            )}
            <span>{connectionState}</span>
          </div>

          {/* Role badge (observer = read-only) */}
          {role === "observer" && (
            <span className="text-[10px] font-mono tracking-widest text-dim border border-line px-2 py-0.5 rounded-sm uppercase">
              observer
            </span>
          )}

          {/* Health stats */}
          {health && (
            <div className="flex items-center gap-4 text-[11px] font-mono text-dim ml-2">
              <span title="Hub uptime">up {fmtDuration(health.uptime)}</span>
              <span title="Messages per minute">{health.msg_per_min} msg/min</span>
              <span title="Queue depth">q:{health.queue_depth}</span>
              <span title="Memory RSS">{health.mem_rss_mb.toFixed(1)} MB</span>
            </div>
          )}

          <div className="flex-1" />

          {/* ── Forms alert badge (transient, operator-facing) ── */}
          <FormsAlert />

          {/* Runbook link */}
          <a
            href="/docs/operator-runbook.md"
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1.5 text-[10px] font-mono text-dim hover:text-cyan transition-colors"
            aria-label="Open operator runbook"
            title="Operator runbook"
          >
            <BookOpen size={13} aria-hidden="true" />
            <span className="hidden sm:inline">Runbook</span>
          </a>

          {/* UTC toggle */}
          <button
            onClick={() => setShowUTC(!showUTC)}
            className={cn(
              "text-[10px] font-mono tracking-widest px-2 py-1 border rounded-sm transition-all",
              showUTC
                ? "text-cyan border-cyan"
                : "text-dim border-line hover:border-cyan hover:text-cyan"
            )}
            aria-pressed={showUTC}
            title="Toggle UTC / local time display"
          >
            {showUTC ? "UTC" : "LOCAL"}
          </button>

          {/* Dark mode toggle */}
          <button
            onClick={() => setDarkMode(!darkMode)}
            className="text-dim hover:text-ink transition-colors p-1"
            aria-label={darkMode ? "Switch to light mode" : "Switch to dark mode"}
          >
            {darkMode ? <Sun size={15} /> : <Moon size={15} />}
          </button>

          {/* Mode controls (operator only) */}
          {role === "operator" && (
            <div className="flex gap-2" role="group" aria-label="Hub mode controls">
              <button
                onClick={() => handleModeAction("pause")}
                className="font-chrome font-bold tracking-widest text-xs px-3 py-1.5 border border-line rounded-sm bg-panel-2 text-ink hover:border-amber hover:text-amber transition-all uppercase"
                aria-label="Pause all agents"
              >
                Pause
              </button>
              <button
                onClick={() => handleModeAction("resume")}
                className="font-chrome font-bold tracking-widest text-xs px-3 py-1.5 border border-line rounded-sm bg-panel-2 text-ink hover:border-cyan hover:text-ink transition-all uppercase"
                aria-label="Resume all agents"
              >
                Resume
              </button>
              <button
                onClick={() => handleModeAction("stop")}
                className="font-chrome font-bold tracking-widest text-xs px-3 py-1.5 border border-red rounded-sm bg-panel-2 text-red hover:bg-red hover:text-bg transition-all uppercase"
                aria-label="Stop all agents"
              >
                Stop All
              </button>
              <button
                onClick={() => handleModeAction("reset")}
                className="font-chrome font-bold tracking-widest text-xs px-3 py-1.5 border border-line rounded-sm bg-panel-2 text-ink hover:border-cyan hover:text-ink transition-all uppercase"
                aria-label="Reset hub"
              >
                Reset
              </button>
            </div>
          )}
        </header>

        {/* Disconnected banner */}
        <DisconnectedBanner />

        {/* ── Body: left rail + main ───────────────────────────────────────── */}
        <div className="flex flex-1 min-h-0 overflow-hidden">
          {/* ── Left rail ──────────────────────────────────────────────────── */}
          <aside
            className="w-[260px] flex-shrink-0 border-r border-line flex flex-col overflow-hidden bg-panel/30"
            aria-label="Peers and channels"
          >
            {/* Health section */}
            <div
              className="flex flex-col overflow-hidden flex-shrink-0"
              style={{ maxHeight: "50%" }}
            >
              {/* Section header */}
              <div className="flex items-center gap-2 px-3 py-2 border-b border-line/50 flex-shrink-0">
                <span className="font-chrome font-bold tracking-[2px] text-[10px] uppercase text-dim flex-1">
                  Health
                </span>
                <button
                  title="Health: live peer roster, pause/resume/kick individual agents, send heartbeat pings."
                  className="text-dim/40 hover:text-dim transition-colors"
                  tabIndex={0}
                  aria-label="Health section help"
                >
                  <HelpCircle size={11} aria-hidden="true" />
                </button>
              </div>
              <HealthPanel compact />
            </div>

            {/* Channels section */}
            <div className="flex flex-col flex-1 overflow-hidden border-t border-line/30">
              {/* Section header */}
              <div className="flex items-center gap-2 px-3 py-2 border-b border-line/50 flex-shrink-0">
                <span className="font-chrome font-bold tracking-[2px] text-[10px] uppercase text-dim flex-1">
                  Channels
                </span>
                <button
                  title="Channels: active hub channels. Click to filter Flow + set composer scope. Operator can force-close (non-sticky)."
                  className="text-dim/40 hover:text-dim transition-colors"
                  tabIndex={0}
                  aria-label="Channels section help"
                >
                  <HelpCircle size={11} aria-hidden="true" />
                </button>
              </div>
              <ChannelsPanel compact />
            </div>
          </aside>

          {/* ── Main area ───────────────────────────────────────────────────── */}
          <main className="flex-1 flex flex-col min-h-0 overflow-hidden">
            {/* Floor strip — amber band showing active talking-stick floors */}
            <FloorStrip />

            {/* Message flow — takes all remaining vertical space */}
            <div className="flex-1 min-h-0 overflow-hidden">
              <FlowPanel />
            </div>

            {/* Operator composer — pinned to bottom, operator-only */}
            <OperatorComposer />
          </main>
        </div>
      </div>
    </ToastProvider>
  );
}
