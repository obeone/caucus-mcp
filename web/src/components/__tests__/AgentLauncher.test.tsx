/**
 * Component tests for AgentLauncher's roster rows.
 *
 * The point of these is the phantom. A wedged child keeps long-polling, so its
 * peer never goes stale and every field the row used to show reads healthy; only
 * `peer_known` together with `msg_count` gives it away. Both were travelling the
 * wire and dying before the pixel, so these tests assert the pixel.
 *
 * Uses @testing-library/react with jsdom, pre-seeding the Zustand store via
 * setState(), the pattern RateControl.test.tsx and HealthPanel.test.tsx follow.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import AgentLauncher from "../AgentLauncher";
import ToastProvider from "../ToastProvider";
import type { AgentInfo } from "../../store/types";
import { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

/** Build a running roster row, overriding whichever fields a test cares about. */
function agent(overrides: Partial<AgentInfo> = {}): AgentInfo {
  return {
    name: "alpha",
    type: "talker",
    permission_mode: "auto",
    model: null,
    pid: 4242,
    started_at: 1000,
    uptime_seconds: 90,
    state: "running",
    exit_code: null,
    peer_known: true,
    msg_count: 3,
    ...overrides,
  };
}

/** Render the panel as the operator, with `agents` seeded. */
function renderWith(agents: AgentInfo[]) {
  useDashStore.setState({ role: "operator", agents });
  return render(<AgentLauncher />, { wrapper: Wrapper });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("AgentLauncher — roster row room state", () => {
  beforeEach(() => {
    useDashStore.setState({ role: "operator", agents: [] });
  });

  it("shows a healthy agent as joined, with its send count", () => {
    renderWith([agent({ msg_count: 3 })]);
    expect(screen.getByText("joined")).toBeInTheDocument();
    expect(screen.getByText("3 sent")).toBeInTheDocument();
  });

  it("shows a joined-but-mute agent as silent, with a zero send count", () => {
    // The phantom. `peer_known` alone reads "yes" here, which is exactly the
    // reading that sent an operator chasing a ghost.
    renderWith([agent({ peer_known: true, msg_count: 0 })]);
    expect(screen.getByText("joined, silent")).toBeInTheDocument();
    expect(screen.getByText("0 sent")).toBeInTheDocument();
  });

  it("shows a process that never registered as not joined", () => {
    renderWith([agent({ peer_known: false, msg_count: null })]);
    expect(screen.getByText("not joined")).toBeInTheDocument();
    // No peer means no count to report, rather than a misleading zero.
    expect(screen.queryByText("0 sent")).not.toBeInTheDocument();
  });

  it("renders the room state beside the kill button, not in another panel", () => {
    renderWith([agent({ peer_known: true, msg_count: 0 })]);
    const row = screen.getByRole("listitem");
    expect(row).toHaveTextContent("joined, silent");
    expect(row).toHaveTextContent("0 sent");
    expect(
      screen.getByRole("button", { name: "Kill agent alpha" })
    ).toBeInTheDocument();
  });

  it("omits the room state for an exited agent", () => {
    // A dead process has no relationship to the room worth reporting.
    renderWith([agent({ state: "exited", exit_code: 3, peer_known: false })]);
    expect(screen.queryByText("not joined")).not.toBeInTheDocument();
    expect(screen.getByText(/exited/)).toBeInTheDocument();
  });
});

describe("AgentLauncher — mute permission modes", () => {
  beforeEach(() => {
    useDashStore.setState({ role: "operator", agents: [] });
  });

  it.each(["plan", "default"])(
    "refuses %s before the round trip, naming what is lost",
    (mode) => {
      renderWith([]);
      fireEvent.change(screen.getByLabelText("Agent name"), {
        target: { value: "alpha" },
      });
      fireEvent.change(screen.getByLabelText("Agent permission mode"), {
        target: { value: mode },
      });

      expect(screen.getByText(/cannot speak in the room/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Spawn agent" })).toBeDisabled();
    }
  );

  it("leaves a speakable mode submittable", () => {
    renderWith([]);
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "alpha" },
    });

    expect(screen.queryByText(/cannot speak in the room/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Spawn agent" })
    ).not.toBeDisabled();
  });
});
