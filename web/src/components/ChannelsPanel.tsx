/**
 * ChannelsPanel — list of active hub channels.
 *
 * Displays channel name, topic, member count, and a close-channel action
 * (operator only) with a confirmation modal.
 * Cross-links with selectedPeer: highlights channels where selectedPeer is a member.
 */

import { useState, useCallback } from "react";
import { useDashStore } from "../store/wsStore";
import { colorFor } from "../lib/colors";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { X, Users, Hash } from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";

interface CloseChannelDialogProps {
  channelName: string;
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Confirmation modal for close-channel action. */
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

// ── Channel card ──────────────────────────────────────────────────────────────

interface ChannelCardProps {
  name: string;
  topic: string | null;
  members: string[];
  highlighted: boolean;
  role: "operator" | "observer";
  onClose: (name: string) => void;
}

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

// ── Panel ─────────────────────────────────────────────────────────────────────

export default function ChannelsPanel() {
  const channels = useDashStore((s) => s.channels);
  const selectedPeer = useDashStore((s) => s.selectedPeer);
  const role = useDashStore((s) => s.role);
  const sendCloseChannel = useDashStore((s) => s.sendCloseChannel);
  const { toast } = useToast();

  const channelEntries = Object.entries(channels);

  const handleClose = useCallback(
    (name: string) => {
      sendCloseChannel(name);
      toast({ title: `Closed channel ${name}`, variant: "default" });
    },
    [sendCloseChannel, toast]
  );

  // Highlight channels where selectedPeer is a member
  const isHighlighted = useCallback(
    (members: string[]) => {
      if (!selectedPeer) return false;
      return members.includes(selectedPeer);
    },
    [selectedPeer]
  );

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
