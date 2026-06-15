/**
 * Zustand store + WebSocket client for the Caucus operator dashboard.
 *
 * Responsibilities:
 * - Opens a WebSocket to /ui (same-origin), sends auth frame if a token is
 *   present (from ?token= URL param or localStorage "caucus_token").
 * - Handles all inbound hub events defined in the protocol contract.
 * - Exposes send helpers for every operator command.
 * - Reconnects with exponential backoff (1s → 2s → 4s … max 30s).
 * - Keeps the last MAX_MESSAGES messages in memory.
 */

import { create } from "zustand";
import { fireToast } from "../components/ToastProvider";
import type {
  DashboardState,
  HubEvent,
  Message,
  PeerInfo,
  ChannelsMap,
  FloorsMap,
  FormObj,
  HealthInfo,
  RawMessage,
  ConnectionState,
  UserRole,
} from "./types";

// Maximum messages kept in memory (client-side ring buffer).
const MAX_MESSAGES = 500;

// Backoff config (ms).
const BACKOFF_INITIAL = 1_000;
const BACKOFF_MAX = 30_000;

let msgSeq = 0;

/** Generate a lightweight unique ID for each received message. */
function nextId(): string {
  return `m${Date.now()}-${++msgSeq}`;
}

/** Convert a raw hub message object into a typed Message. */
function rawToMessage(raw: RawMessage): Message {
  return {
    id: nextId(),
    ts: raw.ts,
    sender: raw.sender,
    recipient: raw.recipient ?? "all",
    content: raw.content ?? "",
    kind: raw.kind ?? "message",
  };
}

/**
 * Read the auth token from ?token= URL param or localStorage.
 *
 * Security: when the token is supplied in the URL we immediately strip it from
 * the address bar (via replaceState) before returning so it does not linger in
 * browser history, Referer headers, or server access logs.  localStorage is the
 * durable store; the ?token= param is only the one-time bootstrap path.
 */
function getToken(): string | null {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token");
  if (fromUrl) {
    localStorage.setItem("caucus_token", fromUrl);
    // Strip the token from the URL immediately to prevent leakage via browser
    // history, server access logs, and Referer headers.
    window.history.replaceState(
      {},
      "",
      window.location.pathname + window.location.hash,
    );
    return fromUrl;
  }
  return localStorage.getItem("caucus_token");
}

/** Build the WebSocket URL for the /ui endpoint (same-origin). */
function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ui`;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

interface InternalState extends DashboardState {
  _ws: WebSocket | null;
  _backoff: number;
  _reconnectTimer: ReturnType<typeof setTimeout> | null;
  _initWs: () => void;
  _handleEvent: (evt: HubEvent) => void;
  _send: (payload: unknown) => void;
}

export const useDashStore = create<InternalState>()((set, get) => ({
  // ---- public state -------------------------------------------------------
  connectionState: "connecting" as ConnectionState,
  role: "operator" as UserRole,
  mode: "running",
  peers: [],
  channels: {},
  floors: {},
  forms: [],
  health: null as HealthInfo | null,
  messages: [],
  selectedPeer: null,
  selectedChannel: null as string | null,
  showUTC: false,
  darkMode: (() => {
    const stored = localStorage.getItem("caucus_dark");
    if (stored !== null) return stored === "true";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  })(),

  // ---- internal state -----------------------------------------------------
  _ws: null,
  _backoff: BACKOFF_INITIAL,
  _reconnectTimer: null,

  // ---- UI setters ---------------------------------------------------------

  setSelectedPeer: (name) => set({ selectedPeer: name }),

  setSelectedChannel: (name) => set({ selectedChannel: name }),

  setShowUTC: (v) => set({ showUTC: v }),

  setDarkMode: (v) => {
    localStorage.setItem("caucus_dark", String(v));
    if (v) {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    set({ darkMode: v });
  },

  // ---- WebSocket internals ------------------------------------------------

  _send: (payload) => {
    const ws = get()._ws;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
    }
  },

  _handleEvent: (evt: HubEvent) => {
    switch (evt.type) {
      case "auth_ok":
        set({ role: evt.role, connectionState: "connected", _backoff: BACKOFF_INITIAL });
        break;

      case "auth_error":
        // Hub will close the socket; we just mark disconnected.
        set({ connectionState: "disconnected" });
        break;

      case "snapshot": {
        const msgs: Message[] = (evt.log ?? []).map(rawToMessage);
        set({
          mode: evt.mode,
          peers: evt.peers ?? [],
          channels: (evt.channels ?? {}) as ChannelsMap,
          floors: (evt.floors ?? {}) as FloorsMap,
          forms: (evt.forms ?? []) as FormObj[],
          health: evt.health ?? null,
          messages: msgs.slice(-MAX_MESSAGES),
        });
        break;
      }

      case "message": {
        // The hub nests the payload under `message` (mirrors to_public());
        // reading fields off the event root would yield undefined.
        const msg = rawToMessage(evt.message);
        set((s) => ({
          messages:
            s.messages.length >= MAX_MESSAGES
              ? [...s.messages.slice(1), msg]
              : [...s.messages, msg],
        }));
        break;
      }

      case "peers":
        set({ peers: evt.peers as PeerInfo[] });
        break;

      case "channels":
        set({ channels: (evt.channels ?? {}) as ChannelsMap });
        break;

      case "mode":
        set({ mode: evt.mode });
        break;

      case "floor":
        set({ floors: (evt.floors ?? {}) as FloorsMap });
        break;

      case "form": {
        const form = evt.form as FormObj;
        set((s) => {
          const existing = s.forms.find((f) => f.id === form.id);
          if (existing) {
            return { forms: s.forms.map((f) => (f.id === form.id ? form : f)) };
          }
          return { forms: [...s.forms, form] };
        });
        break;
      }

      case "form_resolved":
        set((s) => ({
          forms: s.forms.map((f) =>
            f.id === evt.id ? { ...f, status: evt.status } : f
          ),
        }));
        break;

      case "health":
        set({
          health: evt.health,
          peers: evt.peers as PeerInfo[],
        });
        break;

      case "heartbeat_result": {
        // Fire a toast with the ping result so the operator sees liveness inline.
        const r = evt.result;
        const latency =
          r.last_seen_age !== null && r.last_seen_age !== undefined
            ? ` (seen ${r.last_seen_age.toFixed(1)}s ago)`
            : "";
        const status = r.status ? ` — ${r.status}` : "";
        fireToast({
          title: `Heartbeat: ${r.peer} is ${r.present ? "present" : "absent"}`,
          description: `state: ${r.state}${latency}${status}`,
          variant: r.present ? "success" : "error",
        });
        break;
      }

      case "error":
        console.warn("[caucus] server error", evt);
        break;

      default:
        // Unknown event — ignore per backward-tolerance rule.
        break;
    }
  },

  _initWs: () => {
    const { _handleEvent, _initWs } = get();

    const url = wsUrl();
    const ws = new WebSocket(url);
    set({ _ws: ws, connectionState: "connecting" });
    // Expose for E2E tests so Playwright can force-close the socket.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (window as any).__CAUCUS_WS__ = ws;

    ws.onopen = () => {
      // Send auth frame if a token is configured.
      const token = getToken();
      if (token) {
        ws.send(JSON.stringify({ auth: token }));
      }
      // If no token is configured the hub sends auth_ok immediately and we
      // transition to "connected" in the auth_ok handler.
      // When there IS a token we stay "connecting" until auth_ok arrives.
      if (!token) {
        set({ connectionState: "connected", _backoff: BACKOFF_INITIAL });
      }
    };

    ws.onmessage = (raw) => {
      let evt: HubEvent;
      try {
        evt = JSON.parse(raw.data as string) as HubEvent;
      } catch {
        console.warn("[caucus] unparseable frame", raw.data);
        return;
      }
      _handleEvent(evt);
    };

    ws.onclose = () => {
      set({ connectionState: "disconnected", _ws: null });
      const backoff = Math.min(get()._backoff * 2, BACKOFF_MAX);
      set({ _backoff: backoff });
      const timer = setTimeout(() => {
        _initWs();
      }, backoff);
      set({ _reconnectTimer: timer });
    };

    ws.onerror = () => {
      // onclose fires after onerror; no extra handling needed.
      ws.close();
    };
  },

  // ---- Public command senders --------------------------------------------

  sendMode: (action) => get()._send({ mode: action }),

  sendKick: (name) => get()._send({ kick: name }),

  sendPausePeer: (name) => get()._send({ pause_peer: name }),

  sendResumePeer: (name) => get()._send({ resume_peer: name }),

  sendHeartbeat: (name) => get()._send({ heartbeat: name }),

  sendCloseChannel: (name) => get()._send({ close_channel: name }),

  sendAnswer: (id, answers) => get()._send({ answer: { id, answers } }),

  sendCancelForm: (id, reason) =>
    get()._send({ cancel_form: id, ...(reason ? { reason } : {}) }),

  sendFloorClear: (scope) =>
    get()._send({ floor: { action: "clear", scope } }),

  // Wire format: {"say":"<text>","to":"<scope>"} — hub dispatches on the "say"
  // key (legacy console format, hub.py line ~1062); a {to,content} payload is
  // silently ignored.
  sendChat: (to, content) => get()._send({ say: content, to }),
}));

// ---------------------------------------------------------------------------
// Bootstrap: initiate the WebSocket connection on module load.
// ---------------------------------------------------------------------------

// Apply persisted dark-mode preference before any render.
if (useDashStore.getState().darkMode) {
  document.documentElement.classList.add("dark");
} else {
  document.documentElement.classList.remove("dark");
}

// Expose the store on window so E2E tests can reach store actions directly.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(window as any).__CAUCUS_STORE__ = useDashStore;

useDashStore.getState()._initWs();
