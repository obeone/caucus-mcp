/**
 * ChannelsPanel — list of active hub channels.
 *
 * Two rendering modes:
 *   - default (compact=false): full channel cards with topic, member list,
 *     and a close-channel confirmation dialog.
 *   - compact (compact=true):  dense left-rail rows. Clicking a channel
 *     sets `selectedChannel` in the store, syncing the Flow channel filter
 *     and OperatorComposer scope simultaneously. Close button still reachable.
 *
 * Cross-links with selectedPeer: highlights channels where selectedPeer is a member.
 */

import { useState, useCallback } from "react";
import { useDashStore } from "../store/wsStore";
import { colorFor } from "../lib/colors";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { X, Users, Hash } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";

// ---------------------------------------------------------------------------
// Shared: close-channel confirmation dialog
// ---------------------------------------------------------------------------

interface CloseChannelDialogProps {
  channelName: string;
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirmation modal for the force-close-channel action. */
function CloseChannelDialog({
  channelName,
  open,
  onConfirm,
  onCancel,
}: CloseChannelDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={(v) => !v && onCancel()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-bg/80 backdrop-blur-sm z-50 animate-fade-in" />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-50 w-80 bg-panel border border-line rounded-sm shadow-xl animate-wizard-in"
          aria-describedby="close-channel-desc"
        >
          <div className="p-5 border-b border-line">
            <Dialog.Title className="font-chrome font-bold tracking-wide text-sm text-ink">
              Close channel
            </Dialog.Title>
            <p id="close-channel-desc" className="text-xs font-mono text-dim mt-1">
              Force-unsubscribe all members from{" "}
              <span className="text-cyan">{channelName}</span>?
            </p>
            <p className="text-[10px] font-mono text-dim/60 mt-2">
              Non-sticky: agents may re-join automatically.
            </p>
          </div>
          <div className="flex gap-2 justify-end p-4">
            <button
              onClick={onCancel}
              className="font-chrome font-bold tracking-widest text-[10px] uppercase px-3 py-1.5 border border-line rounded-sm text-dim hover:border-ink hover:text-ink transition-all"
            >
              Cancel
            </button>
            <button
              onClick={onConfirm}
              className="font-chrome font-bold tracking-widest text-[10px] uppercase px-3 py-1.5 border border-red rounded-sm text-red hover:bg-red hover:text-bg transition-all"
            >
              Close channel
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ---------------------------------------------------------------------------
// Full channel card (default mode)
// ---------------------------------------------------------------------------

interface ChannelCardProps {
  name: string;
  topic: string | null;
  members: string[];
  highlighted: boolean;
  role: "operator" | "observer";
  onClose: (name: string) => void;
}

/** Full channel card rendered in the standalone ChannelsPanel. */
function ChannelCard({ name, topic, members, highlighted, role, onClose }: ChannelCardProps) {
  const [showMembers, setShowMembers] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);
  const accent = colorFor(name);

  return (
    <>
      <div
        className={cn(
          "flex flex-col gap-2 p-4 rounded-sm border bg-panel-2 transition-all",
          highlighted ? "border-cyan shadow-[0_0_10px_-4px_#38c6d9]" : "border-line"
        )}
        style={{ borderLeftColor: accent, borderLeftWidth: 3 }}
      >
        {/* Header */}
        <div className="flex items-center gap-2">
          <Hash size={13} style={{ color: accent }} />
          <span className="font-mono font-semibold text-sm" style={{ color: accent }}>
            {name}
          </span>
          <span className="text-[10px] font-mono text-dim ml-auto flex items-center gap-1">
            <Users size={10} />
            {members.length}
          </span>
          {role === "operator" && (
            <button
              onClick={() => setConfirmClose(true)}
              className="text-dim hover:text-red transition-colors ml-1"
              aria-label={`Close channel ${name}`}
              title="Force-close channel"
            >
              <X size={13} />
            </button>
          )}
        </div>

        {/* Topic */}
        <p
          className={cn(
            "text-[11px] font-mono pl-1",
            topic ? "text-dim italic" : "text-dim/40 italic"
          )}
        >
          {topic ?? "— no topic —"}
        </p>

        {/* Member list toggle */}
        <button
          onClick={() => setShowMembers((v) => !v)}
          className="text-[10px] font-mono text-dim hover:text-cyan transition-colors text-left"
          aria-expanded={showMembers}
        >
          {showMembers ? "hide members ▲" : `show ${members.length} member(s) ▼`}
        </button>

        {showMembers && (
          <div className="flex flex-wrap gap-1.5 pl-1">
            {members.length === 0 ? (
              <span className="text-[10px] font-mono text-dim italic">
                — no members —
              </span>
            ) : (
              members.map((m) => (
                <span
                  key={m}
                  className="text-[10px] font-mono px-1.5 py-0.5 rounded-sm border border-line bg-panel"
                  style={{ color: colorFor(m) }}
                >
                  {m}
                </span>
              ))
            )}
          </div>
        )}
      </div>

      <CloseChannelDialog
        channelName={name}
        open={confirmClose}
        onConfirm={() => {
          setConfirmClose(false);
          onClose(name);
        }}
        onCancel={() => setConfirmClose(false)}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Compact channel row (left-rail mode)
// ---------------------------------------------------------------------------

interface CompactChannelRowProps {
  name: string;
  members: string[];
  highlighted: boolean;
  selected: boolean;
  role: "operator" | "observer";
  onSelect: (name: string) => void;
  onClose: (name: string) => void;
}

/**
 * Dense single-row channel entry for the left-rail Channels slot.
 *
 * Clicking the row selects/deselects the channel (updating the shared
 * selectedChannel state in the store). The close button is always visible
 * for operators.
 */
function CompactChannelRow({
  name,
  members,
  highlighted,
  selected,
  role,
  onSelect,
  onClose,
}: CompactChannelRowProps) {
  const [confirmClose, setConfirmClose] = useState(false);
  const accent = colorFor(name);

  return (
    <>
      <div
        tabIndex={0}
        aria-label={`Channel ${name}`}
        onClick={() => onSelect(name)}
        onKeyDown={(e) => e.key === "Enter" && onSelect(name)}
        className={cn(
          "group flex items-center gap-1.5 px-3 py-1.5 cursor-pointer transition-all",
          "border-l-[3px] border-b border-b-line/20",
          selected
            ? "bg-cyan/10 border-l-cyan"
            : highlighted
            ? "bg-panel/60 border-l-cyan/40"
            : "bg-transparent hover:bg-panel"
        )}
        style={selected || highlighted ? undefined : { borderLeftColor: accent }}
      >
        {/* Channel name */}
        <Hash size={10} style={{ color: accent }} className="flex-shrink-0" aria-hidden="true" />
        <span
          className="font-mono text-[11px] font-semibold truncate flex-1 min-w-0"
          style={{ color: accent }}
        >
          {name}
        </span>

        {/* Member count */}
        <span className="text-[9px] font-mono text-dim flex-shrink-0">
          {members.length}
        </span>

        {/* Close button (operator only) */}
        {role === "operator" && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setConfirmClose(true);
            }}
            className="p-0.5 text-dim/40 hover:text-red transition-colors flex-shrink-0 opacity-0 group-hover:opacity-100"
            aria-label={`Close channel ${name}`}
            title="Force-close channel"
          >
            <X size={10} />
          </button>
        )}
      </div>

      <CloseChannelDialog
        channelName={name}
        open={confirmClose}
        onConfirm={() => {
          setConfirmClose(false);
          onClose(name);
        }}
        onCancel={() => setConfirmClose(false)}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// ChannelsPanel
// ---------------------------------------------------------------------------

interface ChannelsPanelProps {
  /**
   * When true, renders compact single-row channels suitable for the left-rail
   * slot. Clicking a channel updates `selectedChannel` in the store.
   */
  compact?: boolean;
}

/** Channels panel — active hub channels. Supports compact mode for the left rail. */
export default function ChannelsPanel({ compact = false }: ChannelsPanelProps) {
  const channels = useDashStore((s) => s.channels);
  const selectedPeer = useDashStore((s) => s.selectedPeer);
  const selectedChannel = useDashStore((s) => s.selectedChannel);
  const setSelectedChannel = useDashStore((s) => s.setSelectedChannel);
  const role = useDashStore((s) => s.role);
  const sendCloseChannel = useDashStore((s) => s.sendCloseChannel);
  const { toast } = useToast();

  const channelEntries = Object.entries(channels);

  const handleClose = useCallback(
    (name: string) => {
      sendCloseChannel(name);
      // If the closed channel is currently selected, clear the selection.
      if (selectedChannel === name) setSelectedChannel(null);
      toast({ title: `Closed channel ${name}`, variant: "default" });
    },
    [sendCloseChannel, selectedChannel, setSelectedChannel, toast]
  );

  /** Toggle channel selection: re-clicking the active channel clears it. */
  const handleSelect = useCallback(
    (name: string) => {
      setSelectedChannel(selectedChannel === name ? null : name);
    },
    [selectedChannel, setSelectedChannel]
  );

  /** Highlight channels where selectedPeer is a member. */
  const isHighlighted = useCallback(
    (members: string[]) => {
      if (!selectedPeer) return false;
      return members.includes(selectedPeer);
    },
    [selectedPeer]
  );

  // ── Compact mode (left rail) ─────────────────────────────────────────────
  if (compact) {
    return (
      <div className="flex flex-col overflow-hidden h-full">
        {/* Mini stats + "All" affordance */}
        <div className="flex items-center gap-2 px-3 py-1.5 border-b border-line/30 text-[10px] font-mono text-dim flex-shrink-0 bg-panel/40">
          <span>{channelEntries.length} ch</span>
          {selectedChannel && (
            <button
              onClick={() => setSelectedChannel(null)}
              className="ml-auto text-[9px] text-dim/60 hover:text-cyan tracking-wider transition-colors"
              aria-label="Clear channel selection (show all)"
            >
              ← all
            </button>
          )}
        </div>

        {/* Compact channel list */}
        <div
          className="flex-1 overflow-y-auto"
          role="list"
          aria-label="Active channels"
        >
          {channelEntries.length === 0 ? (
            <div role="listitem" className="flex items-center justify-center h-12 text-dim font-mono text-[11px]">
              — no channels —
            </div>
          ) : (
            channelEntries.map(([name, info]) => (
              <div key={name} role="listitem">
                <CompactChannelRow
                  name={name}
                  members={info.members ?? []}
                  highlighted={isHighlighted(info.members ?? [])}
                  selected={selectedChannel === name}
                  role={role}
                  onSelect={handleSelect}
                  onClose={handleClose}
                />
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  // ── Full mode (standalone panel) ─────────────────────────────────────────
  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Stats bar */}
      <div className="flex items-center gap-4 px-5 py-2.5 border-b border-line bg-panel text-[11px] font-mono text-dim flex-shrink-0">
        <span>{channelEntries.length} channel(s)</span>
        {selectedPeer && (
          <span className="text-cyan">filtering for {selectedPeer}</span>
        )}
      </div>

      {/* Channel list */}
      <div
        className="flex-1 overflow-y-auto p-4"
        role="list"
        aria-label="Active channels"
      >
        {channelEntries.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-dim font-mono text-sm">
            — no active channels —
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {channelEntries.map(([name, info]) => (
              <div key={name} role="listitem">
                <ChannelCard
                  name={name}
                  topic={info.topic}
                  members={info.members ?? []}
                  highlighted={isHighlighted(info.members ?? [])}
                  role={role}
                  onClose={handleClose}
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
