/**
 * FloorStrip — amber banner showing active talking-stick floors.
 *
 * Displays one badge per active floor with scope, holder, reason, and raised
 * hands count. Operator "clear" button sends sendFloorClear(scope).
 * Hidden when no floors are active.
 *
 * A scope running the rotating round table (`mode === "round"`) gets extra
 * affordances on the same badge: a round pill, the rotation ring, a live turn
 * countdown, the extension count, the withheld-backlog depth, and an operator
 * "skip" button. During a round the hub withholds the scope's traffic from
 * everyone but the holder, so this strip is the only place a human sees who is
 * actually speaking.
 */

import { useEffect, useState } from "react";
import { useDashStore } from "../store/wsStore";
import { colorFor } from "../lib/colors";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { Mic, Hand, X, SkipForward, Timer, Pause, Inbox } from "lucide-react";
import type { FloorEntry, RoundInfo } from "../store/types";

/** Seconds left below which the countdown turns red. */
const URGENT_SECONDS = 30;

/**
 * Format a second count as `M:SS`, or `--:--` when unknown.
 *
 * Negative values (an expired deadline the hub has not swept yet) clamp to 0.
 */
function formatCountdown(seconds: number | null): string {
  if (seconds === null) return "--:--";
  const clamped = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(clamped / 60);
  const rest = clamped % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

/** Format an epoch-second timestamp as a local `HH:MM:SS` wall clock. */
function formatClock(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

interface RoundCountdownProps {
  /** Absolute epoch seconds at which the turn expires, if known. */
  deadline: number | null | undefined;
  /** Seconds left as of the last hub push; only used while paused. */
  remaining: number | null | undefined;
  /** True when the room is paused and the turn clock is frozen. */
  paused: boolean;
}

/**
 * Live turn countdown, extrapolated client-side from the turn deadline.
 *
 * Ticks on a component-local `setInterval` and never writes to the store: a
 * 1 Hz store update would re-render every dashboard subscriber (see
 * `wsStore.perf.test.ts`). The interval is cleared on unmount and restarted
 * whenever the deadline or the paused flag changes.
 *
 * Caveat: this compares a **server** epoch (`deadline`) against the **client**
 * clock. On the default localhost deployment both are the same clock, so the
 * reading is exact. Against a remote hub with a skewed clock it only
 * mis-renders a countdown, never mis-drives a control: every floor decision
 * (expiry, handover) is taken by the hub on its own clock.
 */
function RoundCountdown({ deadline, remaining, paused }: RoundCountdownProps) {
  const [now, setNow] = useState(() => Date.now() / 1000);

  useEffect(() => {
    // Nothing to extrapolate without a deadline, and a paused turn has a
    // frozen clock: in both cases leave the interval unstarted.
    if (deadline == null || paused) return;
    setNow(Date.now() / 1000);
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [deadline, paused]);

  // While paused, prefer the hub's own push-time figure; it is the frozen
  // truth, whereas deadline - now keeps drifting against a stopped clock.
  const live = deadline == null ? null : deadline - now;
  const seconds = paused && remaining != null ? remaining : live;
  const urgent = seconds !== null && seconds < URGENT_SECONDS;

  return (
    <span
      className={cn(
        "flex items-center gap-1 text-[10px] font-mono tabular-nums",
        urgent ? "text-red" : "text-amber"
      )}
      title={
        paused
          ? "turn clock frozen: the room is paused"
          : "time left in the current turn"
      }
    >
      {paused ? (
        <Pause size={10} aria-hidden="true" />
      ) : (
        <Timer size={10} aria-hidden="true" />
      )}
      {formatCountdown(seconds)}
    </span>
  );
}

interface RoundRingProps {
  round: RoundInfo;
  holder: string;
}

/** Rotation order, holder first and emphasised, the rest dimmed. */
function RoundRing({ round, holder }: RoundRingProps) {
  return (
    <span
      className="flex items-center gap-1 text-[11px] font-mono"
      data-testid="floor-ring"
      title="rotation order, current holder first"
    >
      {round.ring.map((name, i) => (
        <span key={name} className="flex items-center gap-1">
          {i > 0 && (
            <span className="text-dim/60" aria-hidden="true">
              ›
            </span>
          )}
          <span
            className={cn(name === holder ? "font-medium" : "font-normal")}
            style={{ color: colorFor(name), opacity: name === holder ? 1 : 0.5 }}
          >
            {name}
          </span>
        </span>
      ))}
    </span>
  );
}

interface FloorBadgeProps {
  /** The scope key (e.g. "all", "#channel"). */
  scope: string;
  entry: FloorEntry;
  isOperator: boolean;
  onClear: (scope: string) => void;
  onAdvance: (scope: string) => void;
}

/** Single floor badge rendered inside the strip. */
function FloorBadge({
  scope,
  entry,
  isOperator,
  onClear,
  onAdvance,
}: FloorBadgeProps) {
  const holderColor = colorFor(entry.holder);

  // Round affordances need the payload, not just the flag: a mode without a
  // round object falls back to the classic exclusive rendering.
  const round = entry.mode === "round" ? entry.round ?? null : null;

  const heldEntries = round ? Object.entries(round.held) : [];
  const heldTotal = heldEntries.reduce((sum, [, count]) => sum + count, 0);

  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 rounded-sm border border-amber/40 bg-amber/10"
      role="status"
      aria-label={
        round
          ? `Round in ${scope}, ${entry.holder} speaking`
          : `Floor held by ${entry.holder} in ${scope}`
      }
      title={round ? `turn started ${formatClock(entry.since)}` : undefined}
    >
      {/* Scope */}
      <span className="text-[10px] font-chrome font-bold tracking-[2px] text-amber/70 uppercase">
        {scope}
      </span>

      {/* Round mode marker, absent in exclusive mode */}
      {round && (
        <span className="text-[9px] font-chrome font-bold tracking-[1px] text-amber uppercase border border-amber/50 rounded-sm px-1 py-px">
          round
        </span>
      )}

      {/* Mic icon + holder */}
      <Mic size={11} className="text-amber flex-shrink-0" aria-hidden="true" />
      <span
        className="text-xs font-mono font-semibold"
        style={{ color: holderColor }}
      >
        {entry.holder}
      </span>

      {/* Reason */}
      {entry.reason && (
        <span className="text-[11px] font-mono text-dim italic truncate max-w-[160px]">
          "{entry.reason}"
        </span>
      )}

      {/* Rotation ring */}
      {round && <RoundRing round={round} holder={entry.holder} />}

      {/* Turn countdown */}
      {round && (
        <RoundCountdown
          deadline={round.deadline}
          remaining={round.remaining}
          paused={round.paused}
        />
      )}

      {/* Extensions taken by the current holder */}
      {round && round.extensions > 0 && (
        <span
          className="text-[10px] font-mono text-amber/80"
          title="turn extensions taken by the current holder; extensions are unlimited, so a climbing count is the filibuster tell"
        >
          +{round.extensions}
        </span>
      )}

      {/* Withheld backlog: what tells a parked peer from a dead one */}
      {round && heldTotal > 0 && (
        <span
          className="flex items-center gap-1 text-[10px] font-mono text-dim"
          title={`withheld until their turn: ${heldEntries
            .map(([name, count]) => `${name} ${count}`)
            .join(", ")}`}
        >
          <Inbox size={10} aria-hidden="true" />
          {heldTotal}
        </span>
      )}

      {/* Raised hands */}
      {entry.hands.length > 0 && (
        <span
          className="flex items-center gap-1 text-[10px] font-mono text-amber/80"
          title={`Raised hands: ${entry.hands.join(", ")}`}
        >
          <Hand size={10} aria-hidden="true" />
          {entry.hands.length}
        </span>
      )}

      {/* Operator skip button, round mode only */}
      {isOperator && round && (
        <button
          onClick={() => onAdvance(scope)}
          className={cn(
            "ml-1 text-[10px] font-mono text-dim",
            "hover:text-amber hover:border-amber/40 border border-transparent",
            "rounded-sm px-1 py-0.5 transition-all flex items-center gap-1"
          )}
          aria-label={`Skip current turn in ${scope}`}
          title="Hand the stick to the next peer in the ring"
        >
          <SkipForward size={9} aria-hidden="true" />
          skip
        </button>
      )}

      {/* Operator clear button; ends the round outright in round mode */}
      {isOperator && (
        <button
          onClick={() => onClear(scope)}
          className={cn(
            "ml-1 text-[10px] font-mono text-dim",
            "hover:text-red hover:border-red/40 border border-transparent",
            "rounded-sm px-1 py-0.5 transition-all flex items-center gap-1"
          )}
          aria-label={round ? `End round in ${scope}` : `Clear floor for ${scope}`}
          title={
            round
              ? "End the round and release the floor"
              : "Clear this floor"
          }
        >
          <X size={9} aria-hidden="true" />
          {round ? "end round" : "clear"}
        </button>
      )}
    </div>
  );
}

/** Amber strip rendered below the DisconnectedBanner when floors are active. */
export default function FloorStrip() {
  const floors = useDashStore((s) => s.floors);
  const role = useDashStore((s) => s.role);
  const sendFloorClear = useDashStore((s) => s.sendFloorClear);
  const sendFloorAdvance = useDashStore((s) => s.sendFloorAdvance);
  const { toast } = useToast();

  const entries = Object.entries(floors);

  // Hide entirely when no floors are held.
  if (entries.length === 0) return null;

  const isOperator = role === "operator";

  function handleClear(scope: string) {
    sendFloorClear(scope);
    toast({ title: `Floor cleared for ${scope}`, variant: "default" });
  }

  function handleAdvance(scope: string) {
    sendFloorAdvance(scope);
    toast({ title: `Turn skipped in ${scope}`, variant: "default" });
  }

  return (
    <div
      className="flex items-center gap-2 px-4 py-1.5 border-b border-amber/30 bg-amber/5 flex-shrink-0 overflow-x-auto"
      role="region"
      aria-label="Active floors"
    >
      <span className="text-[10px] font-chrome font-bold tracking-[3px] text-amber/60 uppercase flex-shrink-0">
        floors
      </span>
      <div className="flex items-center gap-2 flex-wrap">
        {entries.map(([scope, entry]) => (
          <FloorBadge
            key={scope}
            scope={scope}
            entry={entry}
            isOperator={isOperator}
            onClear={handleClear}
            onAdvance={handleAdvance}
          />
        ))}
      </div>
    </div>
  );
}
