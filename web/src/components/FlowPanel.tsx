/**
 * FlowPanel — virtualized message timeline.
 *
 * Displays up to 500 messages in a virtual list (via @tanstack/react-virtual).
 * Each row shows [timestamp] sender→recipient type channel payload.
 * Clicking a row expands it. Supports:
 * - Peer filter (cross-linked from HealthPanel selection)
 * - Channel filter
 * - Type filter (broadcast / direct / channel)
 * - Client-side search (Cmd/Ctrl+F, Esc to close)
 * - Arrow-key row navigation (↑/↓ select, Enter expands, Esc clears focus)
 * - Export transcript as JSON blob download
 * - Colour by peer
 * - UTC / local time toggle
 */

import {
  useRef,
  useState,
  useCallback,
  useEffect,
  useMemo,
  KeyboardEvent,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useDashStore } from "../store/wsStore";
import { colorFor, fmtTime, fmtTimeUTC } from "../lib/colors";
import { cn } from "../lib/utils";
import type { Message } from "../store/types";
import { Search, X, ChevronDown, ChevronRight, Download } from "lucide-react";

// ── Badge helpers ────────────────────────────────────────────────────────────

function routeBadge(msg: Message) {
  const r = msg.recipient;
  if (r === "all")
    return (
      <span className="text-[9px] font-chrome font-bold tracking-[2px] px-1.5 py-0.5 border border-cyan text-cyan opacity-75 rounded-sm uppercase">
        broadcast
      </span>
    );
  if (r.startsWith("#"))
    return (
      <span className="text-[9px] font-chrome font-bold tracking-[2px] px-1.5 py-0.5 border border-[#5ad1b0] text-[#5ad1b0] opacity-75 rounded-sm uppercase">
        channel
      </span>
    );
  if (r === "human")
    return (
      <span className="text-[9px] font-chrome font-bold tracking-[2px] px-1.5 py-0.5 bg-human text-bg border border-human rounded-sm uppercase">
        for you
      </span>
    );
  return (
    <span className="text-[9px] font-chrome font-bold tracking-[2px] px-1.5 py-0.5 border border-amber text-amber opacity-75 rounded-sm uppercase">
      direct
    </span>
  );
}

function kindClass(msg: Message): string {
  switch (msg.kind) {
    case "system":
      return "border-l-dim/40 bg-transparent";
    case "control":
      return "border-l-red bg-red/5";
    case "answer":
      return "border-l-amber bg-amber/5";
    default:
      if (msg.sender === "human") return "border-l-human bg-human/5";
      if (msg.recipient === "human") return "border-l-human bg-human/10";
      return "border-l-line bg-panel";
  }
}

// ── Message row ──────────────────────────────────────────────────────────────

interface RowProps {
  msg: Message;
  showUTC: boolean;
  expanded: boolean;
  focused: boolean;
  onToggle: () => void;
}

function MessageRow({ msg, showUTC, expanded, focused, onToggle }: RowProps) {
  const senderColor = colorFor(msg.sender);
  const isSystem = msg.kind === "system" || msg.kind === "control";

  return (
    <div
      className={cn(
        "border-l-[3px] px-4 py-2 cursor-pointer hover:brightness-110 transition-all animate-slide-in",
        kindClass(msg),
        msg.recipient === "human" && "border-l-[5px] shadow-[0_0_0_1px_rgba(192,139,255,0.3)]",
        focused && "ring-1 ring-inset ring-cyan/40 brightness-110"
      )}
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      aria-current={focused ? "true" : undefined}
      onClick={onToggle}
      onKeyDown={(e: KeyboardEvent) => e.key === "Enter" && onToggle()}
    >
      {/* Meta line */}
      <div className="flex items-center gap-2 text-[11px] font-mono text-dim flex-wrap">
        <span className="text-dim/60">
          {showUTC ? fmtTimeUTC(msg.ts) : fmtTime(msg.ts)}
        </span>
        <span className="font-semibold" style={{ color: senderColor }}>
          {msg.sender}
        </span>
        {!isSystem && (
          <>
            <span className="text-dim">→</span>
            <span style={{ color: colorFor(msg.recipient) }}>{msg.recipient}</span>
            {routeBadge(msg)}
          </>
        )}
        <span className="ml-auto flex items-center gap-1 text-dim/50">
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </span>
      </div>

      {/* Collapsed preview */}
      {!expanded && (
        <p
          className={cn(
            "mt-1 text-[12px] truncate",
            msg.kind === "system" || msg.kind === "control"
              ? "font-mono text-dim italic"
              : "font-body text-ink"
          )}
        >
          {msg.content}
        </p>
      )}

      {/* Expanded content */}
      {expanded && (
        <div
          className={cn(
            "mt-2 text-sm whitespace-pre-wrap break-words",
            msg.kind === "system" || msg.kind === "control"
              ? "font-mono text-dim italic text-[11px]"
              : "font-body text-ink leading-relaxed"
          )}
        >
          {msg.content}
        </div>
      )}
    </div>
  );
}

// ── Filter bar ───────────────────────────────────────────────────────────────

type TypeFilter = "all" | "broadcast" | "direct" | "channel";

function passesFilters(
  msg: Message,
  peerFilter: string | null,
  channelFilter: string,
  typeFilter: TypeFilter,
  searchQuery: string
): boolean {
  // Peer cross-link filter
  if (peerFilter && msg.sender !== peerFilter && msg.recipient !== peerFilter)
    return false;

  // Channel filter
  if (channelFilter !== "all" && msg.recipient !== channelFilter) return false;

  // Type filter
  if (typeFilter !== "all") {
    const r = msg.recipient;
    if (typeFilter === "broadcast" && r !== "all") return false;
    if (typeFilter === "direct" && (r === "all" || r.startsWith("#"))) return false;
    if (typeFilter === "channel" && !r.startsWith("#")) return false;
  }

  // Search
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    return (
      msg.content.toLowerCase().includes(q) ||
      msg.sender.toLowerCase().includes(q) ||
      msg.recipient.toLowerCase().includes(q)
    );
  }

  return true;
}

// ── Export helper ─────────────────────────────────────────────────────────────

/**
 * Download the provided messages as a JSON blob file.
 * Pure client-side — no server round-trip needed.
 */
function exportMessages(msgs: Message[]) {
  const blob = new Blob([JSON.stringify(msgs, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `caucus-transcript-${new Date()
    .toISOString()
    .slice(0, 19)
    .replace(/:/g, "-")}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Main component ───────────────────────────────────────────────────────────

export default function FlowPanel() {
  const messages = useDashStore((s) => s.messages);
  const showUTC = useDashStore((s) => s.showUTC);
  const selectedPeer = useDashStore((s) => s.selectedPeer);
  const channels = useDashStore((s) => s.channels);

  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [channelFilter, setChannelFilter] = useState("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
  // Arrow-key navigation: index into filtered[], -1 = none selected
  const [focusedIndex, setFocusedIndex] = useState(-1);

  const searchRef = useRef<HTMLInputElement>(null);
  const parentRef = useRef<HTMLDivElement>(null);

  // Filtered message list
  const filtered = useMemo(
    () =>
      messages.filter((m) =>
        passesFilters(m, selectedPeer, channelFilter, typeFilter, searchQuery)
      ),
    [messages, selectedPeer, channelFilter, typeFilter, searchQuery]
  );

  // Virtual list
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 56,
    overscan: 10,
  });

  // Auto-scroll to bottom on new messages (unless user has scrolled up)
  useEffect(() => {
    const el = parentRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    if (atBottom) {
      virtualizer.scrollToIndex(filtered.length - 1, { align: "end" });
    }
  }, [filtered.length, virtualizer]);

  // Clamp focusedIndex when the filtered list shrinks.
  useEffect(() => {
    if (focusedIndex >= filtered.length) {
      setFocusedIndex(filtered.length - 1);
    }
  }, [filtered.length, focusedIndex]);

  // toggleExpanded is declared before the keydown handler so the closure is stable.
  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Keyboard shortcuts:
  //   Cmd/Ctrl+F   → open search
  //   Escape        → close search (if open) / clear row focus (otherwise)
  //   ArrowDown/Up  → move focus through rows (skips text inputs)
  //   Enter         → toggle expand on the focused row
  useEffect(() => {
    function handler(e: globalThis.KeyboardEvent) {
      // Don't steal keystrokes when the user is typing in an input.
      const tag = (e.target as HTMLElement).tagName;
      const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setShowSearch(true);
        setTimeout(() => searchRef.current?.focus(), 50);
        return;
      }

      if (e.key === "Escape") {
        if (showSearch) {
          setShowSearch(false);
          setSearchQuery("");
        } else {
          setFocusedIndex(-1);
        }
        return;
      }

      if (inInput) return;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setFocusedIndex((prev) => {
          const next = Math.min(prev + 1, filtered.length - 1);
          virtualizer.scrollToIndex(next, { align: "auto" });
          return next;
        });
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setFocusedIndex((prev) => {
          const next = Math.max(prev - 1, 0);
          virtualizer.scrollToIndex(next, { align: "auto" });
          return next;
        });
      } else if (e.key === "Enter" && focusedIndex >= 0) {
        e.preventDefault();
        const msg = filtered[focusedIndex];
        if (msg) toggleExpanded(msg.id);
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [showSearch, filtered, focusedIndex, virtualizer, toggleExpanded]);

  const channelNames = Object.keys(channels);

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Filter bar */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-line bg-panel flex-shrink-0 flex-wrap">
        <label className="text-[10px] font-mono text-dim tracking-widest uppercase">
          type
        </label>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value as TypeFilter)}
          className="bg-bg text-ink border border-line rounded-sm text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan"
          aria-label="Filter by message type"
        >
          <option value="all">All</option>
          <option value="broadcast">Broadcast</option>
          <option value="direct">Direct</option>
          <option value="channel">Channel</option>
        </select>

        {channelNames.length > 0 && (
          <>
            <label className="text-[10px] font-mono text-dim tracking-widest uppercase">
              channel
            </label>
            <select
              value={channelFilter}
              onChange={(e) => setChannelFilter(e.target.value)}
              className="bg-bg text-ink border border-line rounded-sm text-xs font-mono px-2 py-1 focus:outline-none focus:border-cyan"
              aria-label="Filter by channel"
            >
              <option value="all">All</option>
              {channelNames.map((ch) => (
                <option key={ch} value={ch}>
                  {ch}
                </option>
              ))}
            </select>
          </>
        )}

        {selectedPeer && (
          <span className="text-[10px] font-mono text-cyan border border-cyan/40 px-2 py-0.5 rounded-sm">
            peer: {selectedPeer}
          </span>
        )}

        {focusedIndex >= 0 && (
          <span className="text-[10px] font-mono text-dim/60">
            row {focusedIndex + 1}/{filtered.length} · ↑↓ navigate · Enter expand · Esc clear
          </span>
        )}

        <span className="text-[11px] font-mono text-dim ml-auto">
          {filtered.length} / {messages.length}
        </span>

        {/* Export button */}
        <button
          onClick={() => exportMessages(messages)}
          className="text-dim hover:text-cyan transition-colors"
          aria-label="Export transcript as JSON"
          title="Download transcript (all 500 messages) as JSON"
        >
          <Download size={14} />
        </button>

        <button
          onClick={() => {
            setShowSearch((v) => !v);
            if (!showSearch) setTimeout(() => searchRef.current?.focus(), 50);
          }}
          className="text-dim hover:text-cyan transition-colors"
          aria-label="Toggle search (Ctrl+F)"
          title="Search (Ctrl+F / Cmd+F)"
        >
          <Search size={14} />
        </button>
      </div>

      {/* Search bar */}
      {showSearch && (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-line bg-panel-2 flex-shrink-0">
          <Search size={13} className="text-dim" />
          <input
            ref={searchRef}
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search messages… (Esc to close)"
            className="flex-1 bg-transparent text-ink text-xs font-mono focus:outline-none placeholder:text-dim"
            aria-label="Search messages"
          />
          {searchQuery && (
            <button
              onClick={() => setSearchQuery("")}
              className="text-dim hover:text-ink"
              aria-label="Clear search"
            >
              <X size={13} />
            </button>
          )}
        </div>
      )}

      {/* Virtual message list */}
      <div
        ref={parentRef}
        className="flex-1 overflow-y-auto"
        role="log"
        aria-live="polite"
        aria-label="Message timeline"
      >
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-32 text-dim font-mono text-sm">
            — no messages —
          </div>
        ) : (
          <div
            style={{ height: `${virtualizer.getTotalSize()}px`, position: "relative" }}
          >
            {virtualizer.getVirtualItems().map((virtualItem) => {
              const msg = filtered[virtualItem.index];
              return (
                <div
                  key={msg.id}
                  data-index={virtualItem.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${virtualItem.start}px)`,
                  }}
                >
                  <MessageRow
                    msg={msg}
                    showUTC={showUTC}
                    expanded={expandedIds.has(msg.id)}
                    focused={focusedIndex === virtualItem.index}
                    onToggle={() => {
                      setFocusedIndex(virtualItem.index);
                      toggleExpanded(msg.id);
                    }}
                  />
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
