/**
 * OperatorComposer — operator chat input pinned to the viewport bottom.
 *
 * Features:
 *  - Scope selector (all / #channel / peer) synced with `selectedChannel`.
 *  - Autocomplete inline dropdown anchored above the textarea.
 *  - Trigger chars: `@` → peer names, `#` → channel names, `/` → commands.
 *  - `/` commands execute immediately (pause/resume/stop/reset/export).
 *  - Keyboard: ArrowUp/Down to navigate, Enter/Tab to accept, Esc to close.
 *    While the dropdown is open, Enter accepts (does NOT send the message).
 *  - Mouse click on a suggestion also accepts.
 */

import {
  useState,
  useRef,
  useCallback,
  useEffect,
  KeyboardEvent,
} from "react";
import { useDashStore } from "../store/wsStore";
import { cn } from "../lib/utils";
import { useToast } from "./ToastProvider";
import { Send, ChevronDown } from "lucide-react";
import {
  parseAutocompleteTrigger,
  getCandidates,
  applyAutocomplete,
  type AutocompleteToken,
} from "../lib/autocomplete";
import type { Message } from "../store/types";

// ---------------------------------------------------------------------------
// Export helper (mirrors the one in FlowPanel — kept local to avoid circular)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Autocomplete dropdown
// ---------------------------------------------------------------------------

interface DropdownProps {
  candidates: string[];
  trigger: AutocompleteToken["trigger"];
  selectedIndex: number;
  onAccept: (candidate: string) => void;
  onSetIndex: (i: number) => void;
}

/**
 * Inline completion dropdown rendered ABOVE the textarea.
 * Position is controlled by the parent via relative/absolute CSS.
 */
function AutocompleteDropdown({
  candidates,
  trigger,
  selectedIndex,
  onAccept,
  onSetIndex,
}: DropdownProps) {
  if (candidates.length === 0) return null;

  /** Format a candidate for display (add '@' prefix for peer names). */
  function displayLabel(c: string): string {
    if (trigger === "@") return `@${c}`;
    return c; // '#channel' and '/command' already have their prefix
  }

  return (
    <div
      role="listbox"
      aria-label="Autocomplete suggestions"
      className={cn(
        "absolute bottom-full left-0 mb-1 z-50",
        "w-64 max-h-48 overflow-y-auto",
        "bg-panel-2 border border-line rounded-sm shadow-xl",
        "flex flex-col"
      )}
    >
      {candidates.map((c, i) => (
        <div
          key={c}
          role="option"
          aria-selected={i === selectedIndex}
          onMouseDown={(e) => {
            // Prevent textarea blur before the click registers.
            e.preventDefault();
            onAccept(c);
          }}
          onMouseEnter={() => onSetIndex(i)}
          className={cn(
            "px-3 py-1.5 text-xs font-mono cursor-pointer transition-colors",
            i === selectedIndex
              ? "bg-cyan/20 text-cyan"
              : "text-ink hover:bg-panel"
          )}
        >
          {displayLabel(c)}
          {trigger === "/" && (
            <span className="ml-2 text-[10px] text-dim/60">
              {c === "/export" ? "download transcript" : `hub ${c.slice(1)}`}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pause-while-typing state machine
// ---------------------------------------------------------------------------

/**
 * Lifecycle phases for the pause-while-typing feature.
 *
 * - "idle"      — no auto-pause in effect.
 * - "waiting"   — operator sent pause; waiting for the hub echo.
 * - "confirmed" — hub echoed "paused"; auto-resume is armed.
 * - "cancelled" — a foreign operator changed mode after confirmation;
 *                 pending auto-resume has been abandoned.
 */
type AutoPausePhase = "idle" | "waiting" | "confirmed" | "cancelled";

// ---------------------------------------------------------------------------
// OperatorComposer
// ---------------------------------------------------------------------------

/** OperatorComposer is operator-only — callers should guard on role. */
export default function OperatorComposer() {
  const role = useDashStore((s) => s.role);
  const channels = useDashStore((s) => s.channels);
  const peers = useDashStore((s) => s.peers);
  const messages = useDashStore((s) => s.messages);
  const sendChat = useDashStore((s) => s.sendChat);
  const sendMode = useDashStore((s) => s.sendMode);
  const selectedChannel = useDashStore((s) => s.selectedChannel);
  const mode = useDashStore((s) => s.mode);
  const pauseOnType = useDashStore((s) => s.pauseOnType);
  const setPauseOnType = useDashStore((s) => s.setPauseOnType);
  const { toast } = useToast();

  const [to, setTo] = useState("all");
  const [content, setContent] = useState("");
  const textRef = useRef<HTMLTextAreaElement>(null);

  // Autocomplete state
  const [acToken, setAcToken] = useState<AutocompleteToken | null>(null);
  const [acCandidates, setAcCandidates] = useState<string[]>([]);
  const [acIndex, setAcIndex] = useState(0);
  const caretPosRef = useRef(0);

  // Pause-while-typing state machine.
  // A ref is used so handleSend always reads the latest value without stale closures.
  // autoPauseState mirrors it as React state so the hint re-renders and the
  // provenance-gate useEffect fires on transitions.
  const autoPausePhaseRef = useRef<AutoPausePhase>("idle");
  const [autoPauseState, setAutoPauseState] = useState<AutoPausePhase>("idle");

  /** Update both the ref (instant, no re-render) and state (triggers effects + hint). */
  function setAutoPausePhase(phase: AutoPausePhase) {
    autoPausePhaseRef.current = phase;
    setAutoPauseState(phase);
  }

  // Show the hint whenever we are in any non-idle, non-cancelled phase.
  const autoPausedVisible =
    autoPauseState === "waiting" || autoPauseState === "confirmed";

  // PROVENANCE GATE: watch the hub mode echoes to advance or cancel the state machine.
  useEffect(() => {
    const phase = autoPauseState;
    if (phase === "idle") return;

    if (phase === "waiting" && mode === "paused") {
      // Our pause echo landed — advance to confirmed.
      setAutoPausePhase("confirmed");
      return;
    }

    if (phase === "confirmed" && mode !== "paused") {
      // Another operator resumed/stopped the room after our pause was confirmed.
      // Cancel our pending auto-resume — don't fight them.
      setAutoPausePhase("cancelled");
      return;
    }
  }, [mode, autoPauseState]);

  // TOGGLE-OFF RESUME: when the toggle is turned off while an auto-pause is active,
  // release it (but only if we issued the pause and it hasn't been cancelled).
  useEffect(() => {
    if (!pauseOnType && (autoPausePhaseRef.current === "waiting" || autoPausePhaseRef.current === "confirmed")) {
      sendMode("resume");
      setAutoPausePhase("idle");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pauseOnType]);

  // Build scope options: live channels + live peers
  const channelNames = Object.keys(channels);
  const peerNames = peers.filter((p) => p.state === "live").map((p) => p.name);

  // Sync `to` when the left-rail channel selection changes.
  useEffect(() => {
    setTo(selectedChannel ?? "all");
  }, [selectedChannel]);

  // ---------------------------------------------------------------------------
  // Autocomplete helpers
  // ---------------------------------------------------------------------------

  /** Recompute autocomplete candidates from value + caret position. */
  function updateAutocomplete(value: string, caret: number) {
    const token = parseAutocompleteTrigger(value, caret);
    if (!token) {
      setAcToken(null);
      setAcCandidates([]);
      return;
    }
    const candidates = getCandidates(
      token.trigger,
      token.query,
      peerNames,
      channelNames
    );
    setAcToken(token);
    setAcCandidates(candidates);
    setAcIndex(0);
  }

  /** Execute a slash-command and clear the input. */
  function executeCommand(cmd: string) {
    // Typing the leading "/" goes empty → non-empty, so with pauseOnType ON the
    // composer has already armed an auto-pause (phase waiting/confirmed). Picking
    // a command from the dropdown clears the box here WITHOUT going through
    // handleChange's clear-box-resume branch, so we must release that auto-pause
    // ourselves or the machine leaks (stale phase, lingering hint, a spurious
    // resume on the next send — and for /export, the room stays paused forever).
    const autoPaused =
      autoPausePhaseRef.current === "waiting" ||
      autoPausePhaseRef.current === "confirmed";

    switch (cmd) {
      case "/pause":
        sendMode("pause");
        toast({ title: "Hub paused", variant: "default" });
        break;
      case "/resume":
        sendMode("resume");
        toast({ title: "Hub resumed", variant: "success" });
        break;
      case "/stop":
        sendMode("stop");
        toast({ title: "Hub stopped", variant: "default" });
        break;
      case "/reset":
        sendMode("reset");
        toast({ title: "Hub reset", variant: "default" });
        break;
      case "/export":
        // /export sets no mode of its own, so the only pause in effect is the
        // transient typing-pause we caused — actively release it.
        if (autoPaused) sendMode("resume");
        exportMessages(messages);
        toast({ title: "Transcript exported", variant: "success" });
        break;
    }
    // The other commands (pause/resume/stop/reset) set their own terminal mode,
    // which is authoritative — don't fight it with a resume; just forget the
    // transient auto-pause so it can't trigger a stray resume later.
    if (autoPaused) setAutoPausePhase("idle");
    setContent("");
    setAcToken(null);
    setAcCandidates([]);
  }

  /** Accept the currently highlighted autocomplete suggestion. */
  function acceptSuggestion(index: number) {
    if (!acToken || !acCandidates[index]) return;
    const selected = acCandidates[index];

    if (acToken.trigger === "/") {
      executeCommand(selected);
      return;
    }

    const result = applyAutocomplete(
      content,
      caretPosRef.current,
      acToken,
      selected
    );
    if (result) {
      setContent(result.newValue);
      // Restore caret position after React re-render.
      setTimeout(() => {
        if (textRef.current) {
          textRef.current.selectionStart = result.newCaretPos;
          textRef.current.selectionEnd = result.newCaretPos;
        }
      }, 0);
    }
    setAcToken(null);
    setAcCandidates([]);
  }

  // ---------------------------------------------------------------------------
  // Send
  // ---------------------------------------------------------------------------

  /** Handle send on Enter (without Shift) or button click. */
  const handleSend = useCallback(() => {
    const trimmed = content.trim();
    if (!trimmed) return;
    sendChat(to, trimmed);
    // AUTO-RESUME ON SEND: resume if we issued the current pause and it hasn't
    // been cancelled by a foreign operator. Both "waiting" and "confirmed" phases
    // are eligible — the hub will handle a redundant resume gracefully.
    const phase = autoPausePhaseRef.current;
    if (phase === "waiting" || phase === "confirmed") {
      sendMode("resume");
      setAutoPausePhase("idle");
    }
    setContent("");
    setAcToken(null);
    setAcCandidates([]);
    toast({
      title: `Message sent → ${to}`,
      variant: "success",
    });
    textRef.current?.focus();
  }, [content, to, sendChat, sendMode, toast]);

  // ---------------------------------------------------------------------------
  // Keyboard handler
  // ---------------------------------------------------------------------------

  /** Textarea keydown — autocomplete keys take priority when dropdown is open. */
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    const dropdownOpen = acToken !== null && acCandidates.length > 0;

    if (dropdownOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAcIndex((i) => Math.min(i + 1, acCandidates.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setAcIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        acceptSuggestion(acIndex);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setAcToken(null);
        setAcCandidates([]);
        return;
      }
    }

    // Normal Enter: send (Shift+Enter inserts newline).
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  // ---------------------------------------------------------------------------
  // Change handler
  // ---------------------------------------------------------------------------

  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    const value = e.target.value;
    const caret = e.target.selectionStart ?? value.length;
    const prevEmpty = !content.trim();
    const nextEmpty = !value.trim();

    // TRIGGER: empty → non-empty while pauseOnType is ON and not already paused.
    if (pauseOnType && prevEmpty && !nextEmpty && mode !== "paused") {
      sendMode("pause");
      setAutoPausePhase("waiting");
    }

    // CLEAR-BOX RESUME: non-empty → empty while we hold the auto-pause
    // (in either waiting or confirmed phase).
    const phase = autoPausePhaseRef.current;
    if (!prevEmpty && nextEmpty && (phase === "waiting" || phase === "confirmed")) {
      sendMode("resume");
      setAutoPausePhase("idle");
    }

    caretPosRef.current = caret;
    setContent(value);
    updateAutocomplete(value, caret);
  }

  // Update autocomplete candidates on every keystroke that moves the caret.
  function handleSelect(e: React.SyntheticEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget;
    const caret = el.selectionStart ?? 0;
    if (caret !== caretPosRef.current) {
      caretPosRef.current = caret;
      updateAutocomplete(content, caret);
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

  const dropdownOpen = acToken !== null && acCandidates.length > 0;

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

        {/* Textarea + autocomplete dropdown wrapper */}
        <div className="relative flex-1">
          {/* Autocomplete dropdown anchored above the textarea */}
          {dropdownOpen && (
            <AutocompleteDropdown
              candidates={acCandidates}
              trigger={acToken!.trigger}
              selectedIndex={acIndex}
              onAccept={(c) => acceptSuggestion(acCandidates.indexOf(c))}
              onSetIndex={setAcIndex}
            />
          )}

          <textarea
            ref={textRef}
            value={content}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onSelect={handleSelect}
            placeholder="Message… (Enter to send, Shift+Enter for newline, @/#// for autocomplete)"
            rows={1}
            className={cn(
              "w-full resize-none overflow-hidden bg-bg text-ink border border-line rounded-sm",
              "text-xs font-mono px-3 py-1.5 focus:outline-none focus:border-cyan",
              "placeholder:text-dim leading-relaxed"
            )}
            aria-label="Compose operator message"
            aria-autocomplete="list"
          />
        </div>

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

      {/* Bottom row: metadata + pause-while-typing toggle + auto-paused hint */}
      <div className="flex items-center justify-between mt-1.5 pl-1">
        <p className="text-[10px] font-mono text-dim/50">
          Sending as <span className="text-dim">operator</span> → {to}
          {selectedChannel && selectedChannel !== to && (
            <span className="text-dim/40"> (channel selected: {selectedChannel})</span>
          )}
        </p>

        <div className="flex items-center gap-3">
          {/* Auto-paused hint — only visible when this component holds the pause */}
          {autoPausedVisible && (
            <span
              className="text-[10px] font-mono text-amber-400/80"
              aria-live="polite"
              data-testid="auto-paused-hint"
            >
              auto-paused — sending resumes
            </span>
          )}

          {/* Pause-while-typing toggle */}
          <label className="flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={pauseOnType}
              onChange={(e) => setPauseOnType(e.target.checked)}
              className="w-3 h-3 accent-cyan cursor-pointer"
              aria-label="Pause while typing"
            />
            <span className="text-[10px] font-mono text-dim/70">
              Pause while typing
            </span>
          </label>
        </div>
      </div>
    </div>
  );
}
