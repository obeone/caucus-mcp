/**
 * FlowPanel — virtualized markdown message timeline.
 *
 * Displays up to 500 messages in a virtual list (@tanstack/react-virtual v3,
 * dynamic row heights via measureElement + data-index). Content is rendered
 * as Markdown (react-markdown v10 + remark-gfm). Raw HTML in message content
 * is NOT rendered — rehype-raw is intentionally absent; react-markdown's
 * default pipeline escapes any HTML tags in agent-supplied content.
 *
 * Every row shows its full content — there is no expand/collapse toggle.
 *
 * Supports:
 * - Peer filter (cross-linked from HealthPanel selection)
 * - Channel filter (synced from left-rail selectedChannel)
 * - Type filter (broadcast / direct / channel)
 * - Client-side search (Cmd/Ctrl+F, Esc to close)
 * - Arrow-key row navigation (↑↓ select, Esc clears focus)
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
  ComponentPropsWithoutRef,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { useDashStore } from "../store/wsStore";
import { colorFor, fmtTime, fmtTimeUTC } from "../lib/colors";
import { cn } from "../lib/utils";
import type { Message } from "../store/types";
import { Search, X, Download, ArrowDown } from "lucide-react";

// ── Badge helpers ─────────────────────────────────────────────────────────────

/** Coloured route badge shown next to recipient in each message header. */
function routeBadge(msg: Message) {
  const r = msg.recipient ?? "";
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

/** Left-border + background class based on message kind / recipient. */
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

// ── Markdown component overrides (dark-theme) ─────────────────────────────────

/**
 * Custom react-markdown component map for the dark terminal theme.
 *
 * Security: rehype-raw is intentionally NOT included — any raw HTML tags in
 * agent-supplied content are escaped to plain text by react-markdown's default
 * pipeline. Links open in a new tab with rel="noopener noreferrer".
 */
const mdComponents: Components = {
  a: ({ href, children, ...props }: ComponentPropsWithoutRef<"a">) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-cyan underline underline-offset-2 hover:text-cyan/70 transition-colors"
      {...props}
    >
      {children}
    </a>
  ),

  /**
   * code — inline vs block distinguished by presence of a language className.
   * Fenced blocks (```lang) get class="language-xxx" from remark-gfm.
   */
  code: ({ className, children, ...props }: ComponentPropsWithoutRef<"code">) =>
    className ? (
      <code className={cn("text-[11px] font-mono text-green/90", className)} {...props}>
        {children}
      </code>
    ) : (
      <code
        className="bg-panel border border-line/60 rounded px-1 py-0.5 text-[11px] font-mono text-green/90"
        {...props}
      >
        {children}
      </code>
    ),

  pre: ({ children, ...props }: ComponentPropsWithoutRef<"pre">) => (
    <pre
      className="my-1.5 bg-bg border border-line/40 rounded-sm px-3 py-2 overflow-x-auto"
      {...props}
    >
      {children}
    </pre>
  ),

  p: ({ children, ...props }: ComponentPropsWithoutRef<"p">) => (
    <p className="text-[12px] font-body text-ink leading-relaxed my-0.5" {...props}>
      {children}
    </p>
  ),

  ul: ({ children, ...props }: ComponentPropsWithoutRef<"ul">) => (
    <ul className="list-disc pl-5 my-1 space-y-0.5" {...props}>{children}</ul>
  ),

  ol: ({ children, ...props }: ComponentPropsWithoutRef<"ol">) => (
    <ol className="list-decimal pl-5 my-1 space-y-0.5" {...props}>{children}</ol>
  ),

  li: ({ children, ...props }: ComponentPropsWithoutRef<"li">) => (
    <li className="text-[12px] font-body text-ink" {...props}>{children}</li>
  ),

  h1: ({ children, ...props }: ComponentPropsWithoutRef<"h1">) => (
    <h1 className="text-sm font-bold text-ink mt-1.5 mb-0.5" {...props}>{children}</h1>
  ),
  h2: ({ children, ...props }: ComponentPropsWithoutRef<"h2">) => (
    <h2 className="text-[13px] font-bold text-ink mt-1.5 mb-0.5" {...props}>{children}</h2>
  ),
  h3: ({ children, ...props }: ComponentPropsWithoutRef<"h3">) => (
    <h3 className="text-[12px] font-semibold text-ink mt-1 mb-0.5" {...props}>{children}</h3>
  ),
  h4: ({ children, ...props }: ComponentPropsWithoutRef<"h4">) => (
    <h4 className="text-[12px] font-semibold text-dim mt-1" {...props}>{children}</h4>
  ),

  strong: ({ children, ...props }: ComponentPropsWithoutRef<"strong">) => (
    <strong className="font-bold text-ink" {...props}>{children}</strong>
  ),

  em: ({ children, ...props }: ComponentPropsWithoutRef<"em">) => (
    <em className="italic text-ink/80" {...props}>{children}</em>
  ),

  blockquote: ({ children, ...props }: ComponentPropsWithoutRef<"blockquote">) => (
    <blockquote className="border-l-2 border-dim/50 pl-3 my-1 text-dim italic" {...props}>
      {children}
    </blockquote>
  ),

  hr: () => <hr className="border-line/40 my-2" />,

  table: ({ children, ...props }: ComponentPropsWithoutRef<"table">) => (
    <div className="overflow-x-auto my-1.5">
      <table className="text-[11px] font-mono border-collapse" {...props}>{children}</table>
    </div>
  ),
  th: ({ children, ...props }: ComponentPropsWithoutRef<"th">) => (
    <th className="border border-line px-2 py-0.5 text-left text-ink font-semibold" {...props}>
      {children}
    </th>
  ),
  td: ({ children, ...props }: ComponentPropsWithoutRef<"td">) => (
    <td className="border border-line/60 px-2 py-0.5 text-dim" {...props}>{children}</td>
  ),
};

// ── Message row ───────────────────────────────────────────────────────────────

interface RowProps {
  msg: Message;
  showUTC: boolean;
  focused: boolean;
  onSelect: () => void;
}

/**
 * A single message row in the timeline.
 *
 * Always renders the full content as Markdown so the parent's measureElement
 * ref can observe the true rendered height for dynamic virtual sizing.
 * System/control messages are rendered as plain text.
 *
 * Exported so unit tests can render it in isolation (the virtualizer needs
 * a real layout engine and cannot be exercised in jsdom).
 */
export function MessageRow({ msg, showUTC, focused, onSelect }: RowProps) {
  const senderColor = colorFor(msg.sender);
  const isSystem = msg.kind === "system" || msg.kind === "control";

  return (
    <div
      className={cn(
        "border-l-[3px] px-4 py-2 cursor-pointer hover:brightness-110 transition-all animate-slide-in",
        kindClass(msg),
        msg.recipient === "human" &&
          "border-l-[5px] shadow-[0_0_0_1px_rgba(192,139,255,0.3)]",
        focused && "ring-1 ring-inset ring-cyan/40 brightness-110"
      )}
      tabIndex={0}
      aria-current={focused ? "true" : undefined}
      onClick={onSelect}
      onKeyDown={(e: KeyboardEvent) => e.key === "Enter" && onSelect()}
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
      </div>

      {/* Full content — system messages as plain text, others as Markdown */}
      <div
        className={cn(
          "mt-1.5 break-words",
          isSystem && "font-mono text-[11px] text-dim italic"
        )}
      >
        {isSystem ? (
          <span>{msg.content}</span>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
            {msg.content}
          </ReactMarkdown>
        )}
      </div>
    </div>
  );
}

// ── Filters ───────────────────────────────────────────────────────────────────

type TypeFilter = "all" | "broadcast" | "direct" | "channel";

/** Returns true if the message passes all active filters. */
function passesFilters(
  msg: Message,
  peerFilter: string | null,
  channelFilter: string,
  typeFilter: TypeFilter,
  searchQuery: string
): boolean {
  if (peerFilter && msg.sender !== peerFilter && msg.recipient !== peerFilter)
    return false;
  if (channelFilter !== "all" && msg.recipient !== channelFilter) return false;
  if (typeFilter !== "all") {
    const r = msg.recipient ?? "";
    if (typeFilter === "broadcast" && r !== "all") return false;
    if (typeFilter === "direct" && (r === "all" || r.startsWith("#"))) return false;
    if (typeFilter === "channel" && !r.startsWith("#")) return false;
  }
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
 * Download the provided messages as a prettified JSON blob.
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

// ── Main component ────────────────────────────────────────────────────────────

/** Virtualized, markdown-rendering message timeline for the operator dashboard. */
export default function FlowPanel() {
  const messages = useDashStore((s) => s.messages);
  const showUTC = useDashStore((s) => s.showUTC);
  const selectedPeer = useDashStore((s) => s.selectedPeer);
  const selectedChannel = useDashStore((s) => s.selectedChannel);
  const channels = useDashStore((s) => s.channels);

  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [channelFilter, setChannelFilter] = useState("all");

  // Sync channelFilter when the left-rail channel selection changes.
  useEffect(() => {
    setChannelFilter(selectedChannel ?? "all");
  }, [selectedChannel]);

  const [searchQuery, setSearchQuery] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  // Arrow-key navigation index; -1 means nothing focused.
  const [focusedIndex, setFocusedIndex] = useState(-1);

  const searchRef = useRef<HTMLInputElement>(null);
  const parentRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(
    () =>
      messages.filter((m) =>
        passesFilters(m, selectedPeer, channelFilter, typeFilter, searchQuery)
      ),
    [messages, selectedPeer, channelFilter, typeFilter, searchQuery]
  );

  /**
   * Dynamic-height virtual list.
   *
   * Each item wrapper carries `ref={virtualizer.measureElement}` +
   * `data-index={virtualItem.index}`. react-virtual observes the actual
   * rendered height of each element via ResizeObserver and updates its
   * internal size map, so variable-height markdown rows work correctly.
   */
  const virtualizer = useVirtualizer({
    count: filtered.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 72,
    overscan: 10,
  });

  // Stick-to-bottom: follow new messages only while the viewport is pinned to
  // the bottom. The instant the operator scrolls up (to read history) we detach
  // and stop auto-scrolling; scrolling back down re-attaches. `stickToBottom` is
  // a ref (not state) so updating it on every scroll frame never re-renders.
  const stickToBottom = useRef(true);
  const [detached, setDetached] = useState(false);

  // Distance from the bottom (px) under which the view counts as "pinned" — a
  // bit over one estimated row so a small upward scroll detaches cleanly.
  const STICK_THRESHOLD = 80;

  const handleScroll = useCallback(() => {
    const el = parentRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const pinned = distance <= STICK_THRESHOLD;
    stickToBottom.current = pinned;
    setDetached((prev) => (prev === !pinned ? prev : !pinned));
  }, []);

  const jumpToBottom = useCallback(() => {
    stickToBottom.current = true;
    setDetached(false);
    if (filtered.length > 0) {
      virtualizer.scrollToIndex(filtered.length - 1, { align: "end" });
    }
  }, [filtered.length, virtualizer]);

  // Auto-scroll to the latest message whenever the list grows — but only while
  // pinned to the bottom (see stickToBottom above).
  useEffect(() => {
    if (filtered.length === 0 || !stickToBottom.current) return;
    virtualizer.scrollToIndex(filtered.length - 1, { align: "end" });
  }, [filtered.length, virtualizer]);

  // Clamp focusedIndex when the filtered list shrinks.
  useEffect(() => {
    if (focusedIndex >= filtered.length) {
      setFocusedIndex(filtered.length - 1);
    }
  }, [filtered.length, focusedIndex]);

  const handleSelect = useCallback((index: number) => {
    setFocusedIndex(index);
  }, []);

  // Global keyboard shortcuts.
  useEffect(() => {
    function handler(e: globalThis.KeyboardEvent) {
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
      }
    }
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [showSearch, filtered, focusedIndex, virtualizer]);

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
            row {focusedIndex + 1}/{filtered.length} · ↑↓ navigate · Esc clear
          </span>
        )}

        <span className="text-[11px] font-mono text-dim ml-auto">
          {filtered.length} / {messages.length}
        </span>

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

      {/* Virtualized message list */}
      <div className="relative flex-1 min-h-0">
        <div
          ref={parentRef}
          onScroll={handleScroll}
          className="h-full overflow-y-auto"
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
            style={{
              height: `${virtualizer.getTotalSize()}px`,
              position: "relative",
            }}
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
                    focused={focusedIndex === virtualItem.index}
                    onSelect={() => handleSelect(virtualItem.index)}
                  />
                </div>
              );
            })}
          </div>
        )}
        </div>

        {detached && (
          <button
            type="button"
            onClick={jumpToBottom}
            className="absolute bottom-3 right-4 z-10 flex items-center gap-1 px-2.5 py-1.5 rounded-full bg-cyan text-bg text-[11px] font-mono font-bold shadow-lg hover:brightness-110 transition-all"
            aria-label="Scroll to latest message"
          >
            <ArrowDown size={13} /> latest
          </button>
        )}
      </div>
    </div>
  );
}
