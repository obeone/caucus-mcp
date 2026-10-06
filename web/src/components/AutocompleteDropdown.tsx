/**
 * AutocompleteDropdown — the console's one inline completion list.
 *
 * Lifted out of OperatorComposer so every completable field in the console
 * shares a single visual language and a single set of keyboard semantics. The
 * markup, classes and mouse behaviour are the composer's, unchanged; only the
 * trigger-specific bits it used to hardcode (the `@` prefix on peer names, the
 * `/command` hint) became props, plus the two things a non-composer caller
 * needs: a placement, because an input mid-panel completes downwards rather
 * than above a bottom-pinned textarea, and a footer line for a truncation
 * marker.
 *
 * Keyboard handling deliberately stays with the caller: the keys belong to the
 * focused control (textarea or input), not to this list, and the composer's
 * Enter has to mean "accept" or "send" depending on whether the list is open.
 * What this component owns is the pixels and the mouse.
 *
 * `onMouseDown` with `preventDefault` rather than `onClick`: a click on a
 * suggestion otherwise blurs the field first, which can close the dropdown
 * before the click lands.
 */

import type { ReactNode } from "react";
import { cn } from "../lib/utils";

/** Where the dropdown sits relative to its anchor. */
export type DropdownPlacement = "above" | "below";

export interface AutocompleteDropdownProps {
  /** Candidate strings, already filtered and ordered by the caller. */
  candidates: string[];
  /** Index of the highlighted candidate within `candidates`. */
  selectedIndex: number;
  /** Called with the chosen candidate when the operator accepts one. */
  onAccept: (candidate: string) => void;
  /** Called with a candidate's index when the pointer moves over it. */
  onSetIndex: (index: number) => void;
  /** Format a candidate for display; defaults to the candidate itself. */
  renderLabel?: (candidate: string) => ReactNode;
  /** Muted trailing hint for a candidate; omitted when undefined. */
  renderHint?: (candidate: string) => ReactNode;
  /** Muted line below the last candidate, e.g. a truncation marker. */
  footer?: ReactNode;
  /** Vertical placement relative to the anchor; defaults to `"above"`. */
  placement?: DropdownPlacement;
  /** Accessible name for the listbox; defaults to "Autocomplete suggestions". */
  ariaLabel?: string;
  /** Extra container classes, merged over the defaults (e.g. a wider width). */
  className?: string;
}

/**
 * Inline completion dropdown, absolutely positioned against the nearest
 * positioned ancestor. The caller wraps the field in a `relative` element.
 *
 * Renders nothing when `candidates` is empty, so the caller can mount it
 * unconditionally if that reads better than guarding at the call site.
 */
export default function AutocompleteDropdown({
  candidates,
  selectedIndex,
  onAccept,
  onSetIndex,
  renderLabel,
  renderHint,
  footer,
  placement = "above",
  ariaLabel = "Autocomplete suggestions",
  className,
}: AutocompleteDropdownProps) {
  if (candidates.length === 0) return null;

  return (
    <div
      role="listbox"
      aria-label={ariaLabel}
      className={cn(
        placement === "above"
          ? "absolute bottom-full left-0 mb-1 z-50"
          : "absolute top-full left-0 mt-1 z-50",
        "w-64 max-h-48 overflow-y-auto",
        "bg-panel-2 border border-line rounded-sm shadow-xl",
        "flex flex-col",
        className
      )}
    >
      {candidates.map((c, i) => (
        <div
          key={c}
          role="option"
          aria-selected={i === selectedIndex}
          onMouseDown={(e) => {
            // Prevent the field losing focus before the click registers.
            e.preventDefault();
            onAccept(c);
          }}
          onMouseEnter={() => onSetIndex(i)}
          className={cn(
            "px-3 py-1.5 text-xs font-mono cursor-pointer transition-colors",
            i === selectedIndex
              ? "bg-cyan/20 text-cyan"
              : "text-ink hover:bg-panel"
          )}
        >
          {renderLabel ? renderLabel(c) : c}
          {renderHint && (
            <span className="ml-2 text-[10px] text-dim/60">{renderHint(c)}</span>
          )}
        </div>
      ))}

      {/* Footer is informational, never an option: it carries no `role` so it
          stays out of the listbox's option set and out of arrow navigation. */}
      {footer && (
        <div className="px-3 py-1 text-[10px] font-mono text-dim/60">{footer}</div>
      )}
    </div>
  );
}
