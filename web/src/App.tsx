/**
 * Root application component.
 *
 * Renders the fixed header chrome (brand, mode badge, controls, health stats)
 * and the four-panel dashboard grid below it.
 */

import { useState, useCallback } from "react";
import { useDashStore } from "./store/wsStore";
import { cn } from "./lib/utils";
import { fmtDuration } from "./lib/colors";
import HealthPanel from "./components/HealthPanel";
import FlowPanel from "./components/FlowPanel";
import ChannelsPanel from "./components/ChannelsPanel";
import FormsPanel from "./components/FormsPanel";
import DisconnectedBanner from "./components/DisconnectedBanner";
import ToastProvider from "./components/ToastProvider";
import { Moon, Sun, Wifi, WifiOff } from "lucide-react";

type ActivePanel = "health" | "flow" | "channels" | "forms";

export default function App() {
  const mode = useDashStore((s) => s.mode);
  const connectionState = useDashStore((s) => s.connectionState);
  const health = useDashStore((s) => s.health);
  const forms = useDashStore((s) => s.forms);
  const darkMode = useDashStore((s) => s.darkMode);
  const showUTC = useDashStore((s) => s.showUTC);
  const role = useDashStore((s) => s.role);
  const sendMode = useDashStore((s) => s.sendMode);
  const setDarkMode = useDashStore((s) => s.setDarkMode);
  const setShowUTC = useDashStore((s) => s.setShowUTC);

  const [activePanel, setActivePanel] = useState<ActivePanel>("flow");

  const pendingForms = forms.filter((f) => f.status === "pending");

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
        {/* ── Header ─────────────────────────────────────────────────── */}
        <header className="flex items-center gap-4 px-5 py-3 border-b border-line bg-panel-2 flex-shrink-0">
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

        {/* ── Panel tabs ───────────────────────────────────────────────── */}
        <nav
          className="flex gap-0 border-b border-line bg-panel flex-shrink-0"
          aria-label="Dashboard panels"
        >
          {(
            [
              { id: "health", label: "Health" },
              { id: "flow", label: "Flow" },
              { id: "channels", label: "Channels" },
              {
                id: "forms",
                label: `Forms${pendingForms.length > 0 ? ` (${pendingForms.length})` : ""}`,
              },
            ] as { id: ActivePanel; label: string }[]
          ).map(({ id, label }) => (
            <button
              key={id}
              role="tab"
              aria-selected={activePanel === id}
              onClick={() => setActivePanel(id)}
              className={cn(
                "font-chrome font-bold tracking-[2px] text-[11px] px-5 py-2.5 uppercase border-b-2 transition-all",
                activePanel === id
                  ? "text-cyan border-cyan"
                  : "text-dim border-transparent hover:text-ink hover:border-line",
                id === "forms" && pendingForms.length > 0 && activePanel !== id
                  ? "text-amber"
                  : ""
              )}
            >
              {label}
            </button>
          ))}
        </nav>

        {/* ── Panel content ───────────────────────────────────────────── */}
        <main className="flex-1 min-h-0 overflow-hidden">
          <div className={cn("h-full", activePanel !== "health" && "hidden")}>
            <HealthPanel />
          </div>
          <div className={cn("h-full", activePanel !== "flow" && "hidden")}>
            <FlowPanel />
          </div>
          <div className={cn("h-full", activePanel !== "channels" && "hidden")}>
            <ChannelsPanel />
          </div>
          <div className={cn("h-full", activePanel !== "forms" && "hidden")}>
            <FormsPanel />
          </div>
        </main>
      </div>
    </ToastProvider>
  );
}
