/**
 * TypeScript types mirroring the Caucus dashboard WebSocket protocol contract.
 * See docs/dashboard-protocol.md for the authoritative definitions.
 */

// ---------------------------------------------------------------------------
// Peer
// ---------------------------------------------------------------------------

export type PeerState = "live" | "reaped";

export interface PeerInfo {
  name: string;
  state: PeerState;
  /** True when a /receive long-poll is in-flight. */
  listening: boolean;
  /** Operator-paused: delivery withheld. */
  paused: boolean;
  status: string | null;
  status_age: number | null;
  last_seen_age: number | null;
  uptime: number;
  msg_count: number;
  /**
   * Advisory liveness flag: true when a live, non-paused peer has gone past
   * the hub's quiet threshold with neither a /receive poll nor a status update.
   * A peer can legitimately be quiet mid-long-turn — render amber, not red.
   */
  quiet: boolean;
  /**
   * True when the peer's self-reported status line is older than the hub's
   * status-stale threshold. Dims the status text; purely advisory.
   */
  status_stale: boolean;
}

// ---------------------------------------------------------------------------
// Channel
// ---------------------------------------------------------------------------

export interface ChannelInfo {
  topic: string | null;
  members: string[];
}

/** Full channel map: { "#name": ChannelInfo } */
export type ChannelsMap = Record<string, ChannelInfo>;

// ---------------------------------------------------------------------------
// Floor (talking stick)
// ---------------------------------------------------------------------------

export interface FloorEntry {
  scope: string;
  holder: string;
  reason: string | null;
  hands: string[];
  since: number;
}

export type FloorsMap = Record<string, FloorEntry>;

// ---------------------------------------------------------------------------
// Message
// ---------------------------------------------------------------------------

export type MessageKind = "message" | "system" | "control" | "answer";

export interface Message {
  /** Unique client-side ID (assigned on receipt). */
  id: string;
  ts: number;
  sender: string;
  recipient: string;
  content: string;
  kind: MessageKind;
}

// ---------------------------------------------------------------------------
// Forms
// ---------------------------------------------------------------------------

export type FieldType = "text" | "textarea" | "radio" | "checkbox";

export interface FormField {
  key: string;
  label: string;
  type: FieldType;
  options?: string[];
  required?: boolean;
  /**
   * For `radio`/`checkbox` fields, whether the operator may supply a value
   * outside `options` via an "Other…" affordance. Ignored for free-text fields.
   */
  allow_other?: boolean;
}

export interface FormObj {
  id: string;
  title: string;
  fields: FormField[];
  audience: string;
  asker: string;
  status: "pending" | "answered" | "cancelled";
}

// ---------------------------------------------------------------------------
// Rate limit
// ---------------------------------------------------------------------------

/**
 * Token-bucket rate-limit parameters as reported by the hub.
 *
 * `refill_rate` is the sustained rate in messages per second (may be
 * fractional, e.g. 0.5 = 30 msg/min).  `capacity` is the burst size
 * (maximum tokens in the bucket; always >= 1).
 */
export interface RateInfo {
  refill_rate: number;
  capacity: number;
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

export interface HealthInfo {
  uptime: number;
  peer_count: number;
  msg_per_min: number;
  queue_depth: number;
  mem_rss_mb: number;
}

// ---------------------------------------------------------------------------
// Hub → UI events (inbound)
// ---------------------------------------------------------------------------

export interface AuthOkEvent {
  type: "auth_ok";
  role: "operator" | "observer";
  auth: boolean;
}

export interface AuthErrorEvent {
  type: "auth_error";
}

export interface SnapshotEvent {
  type: "snapshot";
  mode: string;
  peers: PeerInfo[];
  channels: ChannelsMap;
  floors: FloorsMap;
  forms: FormObj[];
  log: RawMessage[];
  health: HealthInfo;
  /** Current rate-limit config; present when the hub has one configured. */
  rate?: RateInfo;
}

export interface RawMessage {
  ts: number;
  sender: string;
  recipient: string;
  content: string;
  kind?: MessageKind;
}

export interface PeersEvent {
  type: "peers";
  peers: PeerInfo[];
}

export interface ChannelsEvent {
  type: "channels";
  channels: ChannelsMap;
}

export interface ModeEvent {
  type: "mode";
  mode: string;
}

export interface FloorEvent {
  type: "floor";
  floors: FloorsMap;
}

export interface MessageEvent {
  type: "message";
  /**
   * The hub nests the message fields under `message` (mirrors the backend's
   * `{"type":"message","message": to_public()}`). Reading them at the event
   * root yields undefined — which previously crashed the Flow panel.
   */
  message: RawMessage;
}

export interface FormEvent {
  type: "form";
  form: FormObj;
}

export interface FormResolvedEvent {
  type: "form_resolved";
  id: string;
  status: "answered" | "cancelled";
}

export interface HealthEvent {
  type: "health";
  health: HealthInfo;
  peers: PeerInfo[];
}

export interface HeartbeatResultEvent {
  type: "heartbeat_result";
  result: {
    peer: string;
    state: string;
    present: boolean;
    last_seen_age?: number;
    listening?: boolean;
    status?: string | null;
    status_age?: number | null;
    reaped_age?: number | null;
  };
}

export interface RateEvent {
  type: "rate";
  rate: RateInfo;
}

export interface ErrorEvent {
  type: "error";
  reason: string;
  command?: string;
}

export type HubEvent =
  | AuthOkEvent
  | AuthErrorEvent
  | SnapshotEvent
  | PeersEvent
  | ChannelsEvent
  | ModeEvent
  | FloorEvent
  | MessageEvent
  | FormEvent
  | FormResolvedEvent
  | HealthEvent
  | HeartbeatResultEvent
  | RateEvent
  | ErrorEvent;

// ---------------------------------------------------------------------------
// Connection state
// ---------------------------------------------------------------------------

export type ConnectionState = "connecting" | "connected" | "disconnected";

export type UserRole = "operator" | "observer";

// ---------------------------------------------------------------------------
// UI state
// ---------------------------------------------------------------------------

export interface DashboardState {
  // Connection
  connectionState: ConnectionState;
  role: UserRole;

  // Hub data
  mode: string;
  peers: PeerInfo[];
  channels: ChannelsMap;
  floors: FloorsMap;
  forms: FormObj[];
  health: HealthInfo | null;
  /** Current token-bucket rate-limit config; null until hub sends one. */
  rate: RateInfo | null;
  messages: Message[];

  // UI cross-link
  selectedPeer: string | null;

  /** Channel selected from the left-rail Channels list.
   *  Drives Flow channel filter and OperatorComposer scope simultaneously. */
  selectedChannel: string | null;

  // Timezone toggle (false = local, true = UTC)
  showUTC: boolean;

  // Dark mode (persisted in localStorage)
  darkMode: boolean;

  // Pause-while-typing toggle (persisted in localStorage)
  pauseOnType: boolean;

  // Actions
  setSelectedPeer: (name: string | null) => void;
  setSelectedChannel: (name: string | null) => void;
  setShowUTC: (v: boolean) => void;
  setDarkMode: (v: boolean) => void;
  setPauseOnType: (v: boolean) => void;

  // WS commands
  sendMode: (action: "pause" | "resume" | "reset" | "stop") => void;
  sendKick: (name: string) => void;
  sendPausePeer: (name: string) => void;
  sendResumePeer: (name: string) => void;
  sendHeartbeat: (name: string) => void;
  sendCloseChannel: (name: string) => void;
  sendAnswer: (id: string, answers: Record<string, string | string[]>) => void;
  sendCancelForm: (id: string, reason?: string) => void;
  sendFloorClear: (scope: string) => void;
  /** Send operator message. Wire format: {"say":"<text>","to":"<scope>"}. */
  sendChat: (to: string, content: string) => void;
  /**
   * Set the global token-bucket rate limit at runtime.
   *
   * @param refillRate - Sustained rate in messages per second (e.g. 0.5 = 30/min).
   * @param capacity   - Burst size; must be >= 1.
   */
  sendSetRate: (refillRate: number, capacity: number) => void;
}
