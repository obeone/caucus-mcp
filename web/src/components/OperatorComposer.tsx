/**
 * OperatorComposer — operator chat input pinned to the viewport bottom.
 *
 * Renders a scope selector (all / #channel) + text field + send button.
 * Calls sendChat(to, content) from the store on submit.
 * Only mounted for operator role; hidden for observer.
 */

import { useState, useRef, useCallback, useEffect } from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { Send, ChevronDown } from "lucide-react";

/** OperatorComposer is operator-only — callers should guard on role. */
export default function OperatorComposer() {
  const role = useDashStore((s) => s.role);
  const channels = useDashStore((s) => s.channels);
  const peers = useDashStore((s) => s.peers);
  const sendChat = useDashStore((s) => s.sendChat);
  const { toast } = useToast();

  const [to, setTo] = useState("all");
  const [content, setContent] = useState("");
  const textRef = useRef<HTMLTextAreaElement>(null);

  // Build scope options: "all" + live channels + live peers
  const channelNames = Object.keys(channels);
  const peerNames = peers.filter((p) => p.state === "live").map((p) => p.name);

  /** Handle send on Enter (without Shift) or button click. */
  const handleSend = useCallback(() => {
    const trimmed = content.trim();
    if (!trimmed) return;
    sendChat(to, trimmed);
    setContent("");
    toast({
      title: `Message sent → ${to}`,
      variant: "success",
    });
    textRef.current?.focus();
  }, [content, to, sendChat, toast]);

  /** Textarea keydown: Enter sends, Shift+Enter inserts newline. */
  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  // Auto-grow textarea up to 4 rows.
  useEffect(() => {
    const el = textRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`;
  }, [content]);

  if (role !== "operator") return null;

  return (
    <div
      className="flex-shrink-0 border-t border-line bg-panel-2 px-4 py-3"
      role="region"
      aria-label="Operator composer"
    >
      <div className="flex items-end gap-2">
        {/* Scope selector */}
        <div className="relative flex-shrink-0">
          <select
            value={to}
            onChange={(e) => setTo(e.target.value)}
            className={cn(
              "appearance-none bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-2 py-1.5 pr-6 focus:outline-none focus:border-cyan",
              "cursor-pointer max-w-[140px]"
            )}
            aria-label="Message recipient / scope"
          >
            <option value="all">all (broadcast)</option>
            {channelNames.map((ch) => (
              <option key={ch} value={ch}>
                {ch}
              </option>
            ))}
            {peerNames.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <ChevronDown
            size={11}
            className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 text-dim"
            aria-hidden="true"
          />
        </div>

        {/* Text input */}
        <textarea
          ref={textRef}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Message… (Enter to send, Shift+Enter for newline)"
          rows={1}
          className={cn(
            "flex-1 resize-none overflow-hidden bg-bg text-ink border border-line rounded-sm",
            "text-xs font-mono px-3 py-1.5 focus:outline-none focus:border-cyan",
            "placeholder:text-dim leading-relaxed"
          )}
          aria-label="Compose operator message"
        />

        {/* Send button */}
        <button
          onClick={handleSend}
          disabled={!content.trim()}
          className={cn(
            "flex-shrink-0 flex items-center gap-1.5 font-chrome font-bold tracking-widest",
            "text-[10px] uppercase px-3 py-1.5 rounded-sm border transition-all",
            content.trim()
              ? "border-cyan text-cyan hover:bg-cyan/10 shadow-[0_0_10px_-4px_#38c6d9]"
              : "border-line text-dim cursor-not-allowed opacity-50"
          )}
          aria-label="Send message"
        >
          <Send size={11} aria-hidden="true" />
          Send
        </button>
      </div>

      <p className="text-[10px] font-mono text-dim/50 mt-1.5 pl-1">
        Sending as <span className="text-dim">operator</span> → {to}
      </p>
    </div>
  );
}
