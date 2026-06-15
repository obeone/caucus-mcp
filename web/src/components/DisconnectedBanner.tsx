/**
 * DisconnectedBanner
 *
 * Renders an amber banner when the WebSocket is not connected.
 * Auto-hides when connection is restored.
 */

import { useDashStore } from "../store/wsStore";

export default function DisconnectedBanner() {
  const state = useDashStore((s) => s.connectionState);

  if (state === "connected") return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="bg-amber/10 border-b border-amber/40 text-amber text-xs font-mono tracking-widest px-5 py-2 flex items-center gap-3"
    >
      <span className="animate-blink">●</span>
      <span>
        {state === "connecting"
          ? "Connecting to hub…"
          : "Disconnected — reconnecting with backoff…"}
      </span>
    </div>
  );
}
