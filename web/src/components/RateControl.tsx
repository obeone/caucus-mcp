/**
 * RateControl — operator-only panel for adjusting the global send rate limit.
 *
 * Lets the operator set the token-bucket parameters at runtime:
 *   - Rate value + unit toggle (`/s` or `/min`): converted to `refill_rate`
 *     in messages per second before sending.
 *   - Burst size (`capacity`): maximum tokens the bucket may hold (>= 1).
 *
 * On mount the inputs are initialised from the current `rate` stored in the
 * Zustand store (if available).  The unit is defaulted to `/min` because most
 * operators think in "messages per minute" rather than fractional per-second
 * values; the displayed value is therefore `refill_rate * 60`.
 *
 * Client-side guards: Apply is disabled when rate <= 0 or capacity < 1.  The
 * hub validates server-side as well, so this is pure UX protection.
 *
 * Operator-only: returns null when `role !== "operator"`.
 */

import { useState, useEffect } from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { Gauge } from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Display unit for the rate input. */
type RateUnit = "/s" | "/min";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Convert a displayed rate value + unit into `refill_rate` (messages/second).
 *
 * @param value - The numeric value the operator typed.
 * @param unit  - The selected unit.
 * @returns     - The refill_rate in messages per second.
 */
function toRefillRate(value: number, unit: RateUnit): number {
  return unit === "/s" ? value : value / 60;
}

/**
 * Convert a `refill_rate` (messages/second) into a display value for a given unit.
 *
 * @param refillRate - Rate in messages per second.
 * @param unit       - The target display unit.
 * @returns          - The rounded display value.
 */
function fromRefillRate(refillRate: number, unit: RateUnit): number {
  const raw = unit === "/s" ? refillRate : refillRate * 60;
  // Round to at most 4 decimal places to avoid floating-point noise in inputs.
  return Math.round(raw * 10000) / 10000;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * RateControl panel — operator-only.
 *
 * Reads `rate` from the Zustand store and exposes inputs to change
 * `refill_rate` and `capacity` via `sendSetRate`.
 */
export default function RateControl() {
  const role = useDashStore((s) => s.role);
  const rate = useDashStore((s) => s.rate);
  const sendSetRate = useDashStore((s) => s.sendSetRate);

  // Local input state — kept independent from the store so partial edits don't
  // immediately corrupt the displayed value.
  const [unit, setUnit] = useState<RateUnit>("/min");
  const [rateValue, setRateValue] = useState<string>("");
  const [burstValue, setBurstValue] = useState<string>("");

  // Initialise / sync inputs when the store receives a rate value.
  // We only initialise once (when both inputs are still empty) so we don't
  // fight the user's in-progress edits after the first server push.
  useEffect(() => {
    if (!rate) return;
    if (rateValue === "" && burstValue === "") {
      setRateValue(String(fromRefillRate(rate.refill_rate, unit)));
      setBurstValue(String(rate.capacity));
    }
  // unit is intentionally excluded: re-running on unit changes would reset
  // the user's burst input. The conversion is applied at submit time.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rate]);

  // Operator-only guard.
  if (role !== "operator") return null;

  // ---------------------------------------------------------------------------
  // Derived / validation
  // ---------------------------------------------------------------------------

  const parsedRate = parseFloat(rateValue);
  const parsedBurst = parseInt(burstValue, 10);
  const isValid =
    !isNaN(parsedRate) && parsedRate > 0 &&
    !isNaN(parsedBurst) && parsedBurst >= 1;

  /** Human-readable summary of the current effective rate from the store. */
  function effectiveSummary(): string {
    if (!rate) return "unknown";
    const perMin = Math.round(rate.refill_rate * 60 * 100) / 100;
    return `${perMin}/min (burst ${rate.capacity})`;
  }

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  /** Re-express the current rateValue in the new unit when toggling. */
  function handleUnitChange(newUnit: RateUnit) {
    // Attempt to re-express the current displayed value in the new unit.
    const current = parseFloat(rateValue);
    if (!isNaN(current) && current > 0) {
      // Convert displayed → refill_rate → new displayed
      const refill = toRefillRate(current, unit);
      setRateValue(String(fromRefillRate(refill, newUnit)));
    }
    setUnit(newUnit);
  }

  function handleApply() {
    if (!isValid) return;
    const refillRate = toRefillRate(parsedRate, unit);
    sendSetRate(refillRate, parsedBurst);
  }

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------

  return (
    <div
      className="border border-line rounded-sm bg-panel-2 p-3 flex flex-col gap-2"
      role="region"
      aria-label="Rate control"
    >
      {/* Section header */}
      <div className="flex items-center gap-1.5 text-[10px] font-chrome font-bold tracking-[2px] uppercase text-dim">
        <Gauge size={11} aria-hidden="true" />
        Rate Control
      </div>

      {/* Current effective rate */}
      <p className="text-[10px] font-mono text-dim">
        Current:{" "}
        <span className="text-ink">{effectiveSummary()}</span>
      </p>

      {/* Controls row */}
      <div className="flex items-center gap-2 flex-wrap">
        {/* Rate value input */}
        <input
          type="number"
          min={0}
          step="any"
          value={rateValue}
          onChange={(e) => setRateValue(e.target.value)}
          placeholder="rate"
          aria-label="Rate value"
          className={cn(
            "w-20 bg-bg text-ink border border-line rounded-sm",
            "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
            "placeholder:text-dim"
          )}
        />

        {/* Unit selector */}
        <select
          value={unit}
          onChange={(e) => handleUnitChange(e.target.value as RateUnit)}
          aria-label="Rate unit"
          className={cn(
            "bg-bg text-ink border border-line rounded-sm",
            "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
            "cursor-pointer"
          )}
        >
          <option value="/s">/s</option>
          <option value="/min">/min</option>
        </select>

        {/* Burst size input */}
        <input
          type="number"
          min={1}
          step={1}
          value={burstValue}
          onChange={(e) => setBurstValue(e.target.value)}
          placeholder="burst"
          aria-label="Burst size"
          className={cn(
            "w-16 bg-bg text-ink border border-line rounded-sm",
            "text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan",
            "placeholder:text-dim"
          )}
        />

        {/* Apply button */}
        <button
          onClick={handleApply}
          disabled={!isValid}
          aria-label="Apply rate limit"
          className={cn(
            "font-chrome font-bold tracking-widest text-[10px] uppercase",
            "px-3 py-1 rounded-sm border transition-all",
            isValid
              ? "border-cyan text-cyan hover:bg-cyan/10 shadow-[0_0_10px_-4px_#38c6d9]"
              : "border-line text-dim cursor-not-allowed opacity-50"
          )}
        >
          Apply
        </button>
      </div>

      {/* Caption */}
      <p className="text-[10px] font-mono text-dim/60">
        applies to all peers now; clamps in-flight bursts
      </p>
    </div>
  );
}
