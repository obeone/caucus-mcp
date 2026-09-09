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
// Agent launcher
// ---------------------------------------------------------------------------

/** Agent tool profile: `talker` is caucus-only, `worker` also gets shell/filesystem tools. */
export type AgentType = "talker" | "worker";

/** Claude Code permission mode the spawned agent runs under. */
export type PermissionMode =
  | "auto"
  | "default"
  | "acceptEdits"
  | "plan"
  | "bypassPermissions"
  | "dontAsk";

/** Lifecycle state of a supervised agent process. */
export type AgentState = "running" | "exited";

/**
 * Public view of a supervised `caucus-claude-agent` process, as reported by
 * the hub's `AgentSupervisor.to_public()`. Deliberately excludes the working
 * directory and environment. The roster carried by the `agents` event and the
 * `snapshot.agents` field never include `stdout` or `stderr` (see
 * `to_public`'s `include_output` guard) — those tails are served only by the
 * operator-gated `GET /agents`, which this console does not call.
 */
export interface AgentInfo {
  name: string;
  type: AgentType;
  permission_mode: PermissionMode;
  model: string | null;
  pid: number;
  started_at: number;
  uptime_seconds: number;
  state: AgentState;
  exit_code: number | null;
  /** Whether the hub currently has a live peer registered under this name. */
  peer_known: boolean;
  /**
   * How many messages that peer has *sent*, or `null` when the hub knows no
   * peer under this name.
   *
   * Read together with `peer_known`, this is what separates a healthy agent
   * from a phantom. A wedged child keeps long-polling, so its peer stays fresh
   * and every other field in the row looks fine; only the send count stays at
   * zero. See `AgentLauncher`'s roster row for the three states these two
   * fields encode.
   */
  msg_count: number | null;
}

/**
 * Body for `POST /agents` — the operator launches one native agent.
 *
 * There is no `cwd` field: the working directory is fixed hub policy set at
 * startup (`--agent-cwd`), and the endpoint rejects an unrecognised field with
 * a 422. `mission` and `model` are omitted from the JSON body when unset
 * (rather than sent as `null`), matching `JSON.stringify`'s handling of
 * `undefined` object fields.
 */
export interface SpawnAgentSpec {
  name: string;
  mission?: string;
  type: AgentType;
  permission_mode: PermissionMode;
  model?: string;
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
  /** Supervised agent roster; present when the agent launcher is enabled. */
  agents?: AgentInfo[];
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

/** Broadcast whenever the supervised agent roster changes. */
export interface AgentsEvent {
  type: "agents";
  agents: AgentInfo[];
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
  | AgentsEvent
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
  /** Supervised agent-launcher roster; empty when the launcher is disabled. */
  agents: AgentInfo[];
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
  sendCommand: (name: string, command: "interrupt" | "reset") => void;
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
  /**
   * Ask the hub to spawn a supervised `caucus-claude-agent` process.
   *
   * Unlike the other `send*` commands this is an HTTP mutation, not a `/ui`
   * frame: `POST /agents` with an `Authorization: Bearer <operator token>`
   * header (the token captured from the `/ui` auth handshake). Resolves to
   * `true` on success; on failure a toast surfaces the hub's error and the
   * promise resolves to `false`.
   */
  sendSpawnAgent: (spec: SpawnAgentSpec) => Promise<boolean>;
  /**
   * Ask the hub to kill a supervised agent by name.
   *
   * `DELETE /agents/{name}` with the same bearer token as
   * {@link DashboardState.sendSpawnAgent}. Resolves to `true` on success;
   * on failure a toast surfaces the hub's error and the promise resolves to
   * `false`.
   */
  sendKillAgent: (name: string) => Promise<boolean>;
}
