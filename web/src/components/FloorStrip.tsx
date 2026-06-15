/**
 * FloorStrip — amber banner showing active talking-stick floors.
 *
 * Displays one badge per active floor with scope, holder, reason, and raised
 * hands count. Operator "clear" button sends sendFloorClear(scope).
 * Hidden when no floors are active.
 */

import { useDashStore } from "../store/wsStore";
import { colorFor } from "../lib/colors";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { Mic, Hand, X } from "lucide-react";
import type { FloorEntry } from "../store/types";

interface FloorBadgeProps {
  /** The scope key (e.g. "all", "#channel"). */
  scope: string;
  entry: FloorEntry;
  isOperator: boolean;
  onClear: (scope: string) => void;
}

/** Single floor badge rendered inside the strip. */
function FloorBadge({ scope, entry, isOperator, onClear }: FloorBadgeProps) {
  const holderColor = colorFor(entry.holder);

  return (
    <div
      className="flex items-center gap-2 px-3 py-1.5 rounded-sm border border-amber/40 bg-amber/10"
      role="status"
      aria-label={`Floor held by ${entry.holder} in ${scope}`}
    >
      {/* Scope */}
      <span className="text-[10px] font-chrome font-bold tracking-[2px] text-amber/70 uppercase">
        {scope}
      </span>

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

      {/* Operator clear button */}
      {isOperator && (
        <button
          onClick={() => onClear(scope)}
          className={cn(
            "ml-1 text-[10px] font-mono text-dim",
            "hover:text-red hover:border-red/40 border border-transparent",
            "rounded-sm px-1 py-0.5 transition-all flex items-center gap-1"
          )}
          aria-label={`Clear floor for ${scope}`}
          title="Clear this floor"
        >
          <X size={9} aria-hidden="true" />
          clear
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
  const { toast } = useToast();

  const entries = Object.entries(floors);

  // Hide entirely when no floors are held.
  if (entries.length === 0) return null;

  const isOperator = role === "operator";

  function handleClear(scope: string) {
    sendFloorClear(scope);
    toast({ title: `Floor cleared for ${scope}`, variant: "default" });
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
          />
        ))}
      </div>
    </div>
  );
}
