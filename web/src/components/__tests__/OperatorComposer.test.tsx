/**
 * Component tests for OperatorComposer — pause-while-typing feature.
 *
 * The Zustand store is pre-seeded via setState() before each test.
 * sendMode and sendChat are vi.fn() stubs injected the same way
 * RateControl.test.tsx injects sendSetRate.
 *
 * The store's _handleEvent is used to simulate inbound server events
 * (mode change echo) exactly as the real WebSocket path does.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import OperatorComposer from "../OperatorComposer";
import ToastProvider from "../ToastProvider";
import { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Wrapper
// ---------------------------------------------------------------------------

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Seed the store with operator role, given mode, and fresh mocks. */
function seedStore(
  mode: string,
  pauseOnType: boolean,
  overrides: Record<string, unknown> = {}
) {
  const sendMode = vi.fn();
  const sendChat = vi.fn();
  useDashStore.setState({
    role: "operator",
    mode,
    pauseOnType,
    peers: [],
    channels: {},
    selectedChannel: null,
    messages: [],
    sendMode,
    sendChat,
    setPauseOnType: vi.fn(),
    ...overrides,
  });
  return { sendMode, sendChat };
}

/** Fire a textarea change event with the given value. */
function typeInto(textarea: HTMLElement, value: string) {
  fireEvent.change(textarea, { target: { value } });
}

/** Simulate an inbound hub mode event through the real _handleEvent path. */
function dispatchModeEvent(mode: string) {
  act(() => {
    useDashStore.getState()._handleEvent({ type: "mode", mode });
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("OperatorComposer — pause while typing", () => {
  beforeEach(() => {
    // Reset store to a clean baseline before every test.
    seedStore("running", false);
  });

  // -------------------------------------------------------------------------

  it("auto_pauses_on_first_keystroke_when_toggle_on", () => {
    const { sendMode } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    typeInto(textarea, "hello");

    expect(sendMode).toHaveBeenCalledOnce();
    expect(sendMode).toHaveBeenCalledWith("pause");
  });

  // -------------------------------------------------------------------------

  it("does_not_pause_when_toggle_off", () => {
    const { sendMode } = seedStore("running", false);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    typeInto(textarea, "hello");

    expect(sendMode).not.toHaveBeenCalled();
  });

  // -------------------------------------------------------------------------

  it("does_not_double_pause_when_already_paused", () => {
    // Room is already paused (manual pause by the operator or another operator).
    const { sendMode, sendChat } = seedStore("paused", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type into empty box — should NOT issue another pause.
    typeInto(textarea, "hello");
    expect(sendMode).not.toHaveBeenCalledWith("pause");

    // Send the message — should NOT auto-resume (ref was never set).
    const btn = screen.getByRole("button", { name: /send message/i });
    fireEvent.click(btn);
    expect(sendChat).toHaveBeenCalled();
    expect(sendMode).not.toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("auto_resumes_on_send_only_when_auto_paused", () => {
    const { sendMode, sendChat } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type — triggers auto-pause.
    typeInto(textarea, "hello");
    expect(sendMode).toHaveBeenCalledWith("pause");

    // Send — should auto-resume.
    const btn = screen.getByRole("button", { name: /send message/i });
    fireEvent.click(btn);
    expect(sendChat).toHaveBeenCalled();
    expect(sendMode).toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("clearing_box_resumes_when_auto_paused", () => {
    const { sendMode } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type to trigger auto-pause.
    typeInto(textarea, "hello");
    expect(sendMode).toHaveBeenCalledWith("pause");

    // Clear the box — should auto-resume.
    typeInto(textarea, "");
    expect(sendMode).toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("turning_toggle_off_midtype_resumes", async () => {
    const sendMode = vi.fn();
    const setPauseOnType = vi.fn((v: boolean) => {
      // Simulate the setter updating the store (normally done by wsStore).
      useDashStore.setState({ pauseOnType: v });
    });
    useDashStore.setState({
      role: "operator",
      mode: "running",
      pauseOnType: true,
      peers: [],
      channels: {},
      selectedChannel: null,
      messages: [],
      sendMode,
      sendChat: vi.fn(),
      setPauseOnType,
    });

    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type to trigger auto-pause.
    typeInto(textarea, "hello");
    expect(sendMode).toHaveBeenCalledWith("pause");

    // Toggle off via checkbox.
    const checkbox = screen.getByLabelText("Pause while typing");
    act(() => {
      fireEvent.click(checkbox);
    });

    // Should have issued resume.
    expect(sendMode).toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("does_not_auto_resume_when_mode_changed_away_from_paused", async () => {
    const { sendMode, sendChat } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type to trigger auto-pause.
    typeInto(textarea, "hello");
    expect(sendMode).toHaveBeenCalledWith("pause");

    // Hub echoes our pause back.
    dispatchModeEvent("paused");

    // Another operator resumes the room — ref should be cleared.
    dispatchModeEvent("running");

    // Reset the mock call history to check only future calls.
    sendMode.mockClear();

    // Now send — ref is cleared, so sendMode("resume") must NOT be called.
    const btn = screen.getByRole("button", { name: /send message/i });
    fireEvent.click(btn);
    expect(sendChat).toHaveBeenCalled();
    expect(sendMode).not.toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("auto_resumes_when_only_paused_echo_seen", async () => {
    const { sendMode, sendChat } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Type to trigger auto-pause.
    typeInto(textarea, "hello");
    expect(sendMode).toHaveBeenCalledWith("pause");

    // Hub echoes our pause back (only this — no foreign resume).
    dispatchModeEvent("paused");

    // Reset call history to check only future calls.
    sendMode.mockClear();

    // Send — ref is still set, so sendMode("resume") IS called.
    const btn = screen.getByRole("button", { name: /send message/i });
    fireEvent.click(btn);
    expect(sendChat).toHaveBeenCalled();
    expect(sendMode).toHaveBeenCalledWith("resume");
  });

  // -------------------------------------------------------------------------

  it("slash_export_via_dropdown_releases_auto_pause", () => {
    // Regression: picking a slash-command from the autocomplete clears the box
    // through executeCommand, NOT handleChange — so the typing-pause it armed
    // must be released here or /export leaves the room paused forever.
    const { sendMode } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    // Typing "/export" goes empty → non-empty (arms the auto-pause) and opens
    // the "/" autocomplete with "/export" highlighted.
    typeInto(textarea, "/export");
    expect(sendMode).toHaveBeenCalledWith("pause");
    expect(screen.getByTestId("auto-paused-hint")).toBeInTheDocument();

    // Accept the highlighted command via Enter → executeCommand("/export").
    fireEvent.keyDown(textarea, { key: "Enter" });

    // The transient typing-pause must be released and the machine reset.
    expect(sendMode).toHaveBeenCalledWith("resume");
    expect(screen.queryByTestId("auto-paused-hint")).toBeNull();
  });

  // -------------------------------------------------------------------------

  it("slash_pause_via_dropdown_does_not_leak_state_machine", () => {
    // /pause sets its own terminal mode — the auto-pause must be forgotten
    // (no stray resume undoing the explicit pause, no lingering hint), but we
    // must NOT issue a resume that would cancel what the operator asked for.
    const { sendMode } = seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    const textarea = screen.getByLabelText("Compose operator message");
    typeInto(textarea, "/pause");
    expect(sendMode).toHaveBeenCalledWith("pause");

    sendMode.mockClear();
    // Accept "/pause" → executeCommand("/pause").
    fireEvent.keyDown(textarea, { key: "Enter" });

    // The command's own pause fires; no spurious resume undoes it.
    expect(sendMode).toHaveBeenCalledWith("pause");
    expect(sendMode).not.toHaveBeenCalledWith("resume");
    // Machine reset → hint gone, so the next send can't emit a stale resume.
    expect(screen.queryByTestId("auto-paused-hint")).toBeNull();
  });

  // -------------------------------------------------------------------------

  it("shows_auto_paused_hint_while_auto_paused", () => {
    seedStore("running", true);
    render(<OperatorComposer />, { wrapper: Wrapper });

    // Hint is not visible before typing.
    expect(screen.queryByTestId("auto-paused-hint")).toBeNull();

    const textarea = screen.getByLabelText("Compose operator message");
    typeInto(textarea, "hello");

    // Hint should appear.
    expect(screen.getByTestId("auto-paused-hint")).toBeInTheDocument();
    expect(screen.getByTestId("auto-paused-hint")).toHaveTextContent(
      "auto-paused — sending resumes"
    );

    // After clearing the box, hint disappears.
    typeInto(textarea, "");
    expect(screen.queryByTestId("auto-paused-hint")).toBeNull();
  });
});
