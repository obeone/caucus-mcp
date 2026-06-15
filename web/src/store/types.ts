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
  ts: number;
  sender: string;
  recipient: string;
  content: string;
  kind?: MessageKind;
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
  messages: Message[];

  // UI cross-link
  selectedPeer: string | null;

  // Timezone toggle (false = local, true = UTC)
  showUTC: boolean;

  // Dark mode (persisted in localStorage)
  darkMode: boolean;

  // Actions
  setSelectedPeer: (name: string | null) => void;
  setShowUTC: (v: boolean) => void;
  setDarkMode: (v: boolean) => void;

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
}
