/**
 * Deterministic peer-name → accent colour mapping.
 * Mirrors the legacy PALETTE array from index.html so colours stay consistent
 * across page reloads (insertion order is stable within a session).
 */

const PALETTE = [
  "#ffb22e",
  "#4fd67a",
  "#38c6d9",
  "#ff7eb6",
  "#9d8bff",
  "#ffd95e",
  "#5ad1b0",
];

const colorMap = new Map<string, string>();
let colorIdx = 0;

/**
 * Return a stable accent colour for a given peer name.
 * Special names "human" and "hub" get fixed palette entries.
 */
export function colorFor(name: string): string {
  if (name === "human") return "#c08bff";
  if (name === "hub") return "#6b7d89";
  if (!colorMap.has(name)) {
    colorMap.set(name, PALETTE[colorIdx % PALETTE.length]);
    colorIdx++;
  }
  return colorMap.get(name)!;
}

/**
 * Format a Unix timestamp (seconds) as a locale time string HH:MM:SS.
 */
export function fmtTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Format a Unix timestamp as UTC ISO string, e.g. "14:32:05 UTC".
 */
export function fmtTimeUTC(ts: number): string {
  const d = new Date(ts * 1000);
  return (
    d.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "UTC",
    }) + " UTC"
  );
}

/**
 * Format seconds duration into a human-readable string, e.g. "2h 3m 4s".
 */
export function fmtDuration(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
