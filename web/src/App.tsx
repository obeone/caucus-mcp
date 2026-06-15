/**
 * Root application component.
 *
 * Renders the fixed header chrome (brand, mode badge, controls, health stats),
 * the floor strip (active talking-stick floors), the four-panel tab bar, the
 * active panel content, and the operator composer pinned at the bottom.
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
import FloorStrip from "./components/FloorStrip";
import OperatorComposer from "./components/OperatorComposer";
import ToastProvider from "./components/ToastProvider";
import { Moon, Sun, Wifi, WifiOff, HelpCircle, BookOpen } from "lucide-react";

type ActivePanel = "health" | "flow" | "channels" | "forms";

/** Tooltip hint shown when the user hovers the ? button on a panel tab. */
const PANEL_HELP: Record<ActivePanel, string> = {
  health: "Health: live peer roster, pause/resume/kick individual agents, send heartbeat pings.",
  flow: "Flow: real-time message timeline. ↑↓ navigate rows, Enter expand, Ctrl+F search, ⬇ export JSON.",
  channels: "Channels: active hub channels. Operator can force-close a channel (non-sticky).",
  forms: "Forms: pending operator questionnaires from agents. Fill or reject each form.",
};

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
  const [helpTooltip, setHelpTooltip] = useState<string | null>(null);

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

          {/* Help / Runbook link */}
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

        {/* Floor strip — amber band showing active talking-stick floors */}
        <FloorStrip />

        {/* ── Panel tabs ───────────────────────────────────────────────── */}
        {/*
          ARIA structure: the outer <nav> is the landmark; the inner <div
          role="tablist"> contains ONLY the <button role="tab"> children so
          aria-required-children is satisfied.  The help-icon buttons are
          siblings of the tablist (inside the nav) so they don't pollute the
          tablist's allowed-children set.
        */}
        <nav
          className="flex gap-0 border-b border-line bg-panel flex-shrink-0 relative"
          aria-label="Dashboard panels"
        >
          <div role="tablist" className="flex">
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
          </div>

          {/* Context-help "?" affordances — outside tablist so ARIA is satisfied */}
          <div className="flex items-center">
            {(["health", "flow", "channels", "forms"] as ActivePanel[]).map((id) => (
              <button
                key={id}
                onMouseEnter={() => setHelpTooltip(PANEL_HELP[id])}
                onMouseLeave={() => setHelpTooltip(null)}
                onFocus={() => setHelpTooltip(PANEL_HELP[id])}
                onBlur={() => setHelpTooltip(null)}
                className="px-1 text-dim/40 hover:text-dim transition-colors"
                aria-label={`Help for ${id} panel`}
                tabIndex={0}
              >
                <HelpCircle size={11} aria-hidden="true" />
              </button>
            ))}
          </div>

          {/* Help tooltip */}
          {helpTooltip && (
            <div
              role="tooltip"
              className="absolute top-full left-0 mt-1 ml-2 z-50 max-w-xs bg-panel-2 border border-line rounded-sm px-3 py-2 text-[11px] font-mono text-dim shadow-lg pointer-events-none"
            >
              {helpTooltip}
            </div>
          )}
        </nav>

        {/* ── Panel content ───────────────────────────────────────────── */}
        <main className="flex-1 min-h-0 overflow-hidden flex flex-col">
          <div className={cn("flex-1 min-h-0", activePanel !== "health" && "hidden")}>
            <HealthPanel />
          </div>
          <div className={cn("flex-1 min-h-0", activePanel !== "flow" && "hidden")}>
            <FlowPanel />
          </div>
          <div className={cn("flex-1 min-h-0", activePanel !== "channels" && "hidden")}>
            <ChannelsPanel />
          </div>
          <div className={cn("flex-1 min-h-0", activePanel !== "forms" && "hidden")}>
            <FormsPanel />
          </div>

          {/* Operator composer — pinned to panel bottom, operator-only */}
          <OperatorComposer />
        </main>
      </div>
    </ToastProvider>
  );
}
