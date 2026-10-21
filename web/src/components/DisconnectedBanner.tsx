/**
 * DisconnectedBanner
 *
 * Renders an amber banner when the WebSocket is not connected.
 * Auto-hides when connection is restored.
 */

import { useDashStore } from "../store/wsStore";

/** Explain the disconnected state and allow a cancelled login to be retried. */
export default function DisconnectedBanner() {
  const state = useDashStore((s) => s.connectionState);
  const authCancelled = useDashStore((s) => s._authCancelled);
  const connect = useDashStore((s) => s.connect);

  if (state === "connected") return null;

  return (
    <div
      role="alert"
      aria-live="assertive"
      className="bg-amber/10 border-b border-amber/40 text-amber text-xs font-mono tracking-widest px-5 py-2 flex items-center gap-3"
    >
      <span className={authCancelled ? "" : "animate-blink"}>●</span>
      <span>
        {state === "connecting"
          ? "Connecting to hub…"
          : authCancelled
            ? "Authentication cancelled. Connection paused."
            : "Disconnected — reconnecting with backoff…"}
      </span>
      {authCancelled && (
        <button
          type="button"
          onClick={connect}
          className="underline underline-offset-2 focus-visible:outline focus-visible:outline-2"
        >
          Retry connection
        </button>
      )}
    </div>
  );
}
