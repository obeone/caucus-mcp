/**
 * Component tests for HealthPanel — a11y and render checks.
 *
 * Uses @testing-library/react with jsdom.  The Zustand store is pre-seeded
 * with fixture data before each test via setState().
 */

import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import type { PeerInfo } from "../../store/types";
import HealthPanel from "../HealthPanel";

// ---------------------------------------------------------------------------
// ToastProvider wrapper (HealthPanel uses useToast internally)
// ---------------------------------------------------------------------------

import ToastProvider from "../ToastProvider";
import { ReactNode } from "react";

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const livePeer: PeerInfo = {
  name: "agent-alpha",
  state: "live",
  listening: true,
  paused: false,
  status: "building the API",
  status_age: 5.2,
  last_seen_age: 0.3,
  uptime: 240,
  msg_count: 12,
};

const pausedPeer: PeerInfo = {
  name: "agent-beta",
  state: "live",
  listening: false,
  paused: true,
  status: null,
  status_age: null,
  last_seen_age: 1.1,
  uptime: 60,
  msg_count: 3,
};

const reapedPeer: PeerInfo = {
  name: "agent-gamma",
  state: "reaped",
  listening: false,
  paused: false,
  status: null,
  status_age: null,
  last_seen_age: 90,
  uptime: 400,
  msg_count: 8,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("HealthPanel — render", () => {
  beforeEach(() => {
    useDashStore.setState({
      peers: [livePeer, pausedPeer, reapedPeer],
      selectedPeer: null,
      role: "operator",
      health: null,
      showUTC: false,
    });
  });

  it("renders the peer list region with correct aria-label", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(screen.getByRole("list", { name: "Connected peers" })).toBeInTheDocument();
  });

  it("renders a listitem for each peer", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    const items = screen.getAllByRole("listitem");
    expect(items.length).toBe(3);
  });

  it("shows peer names", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(screen.getByText("agent-alpha")).toBeInTheDocument();
    expect(screen.getByText("agent-beta")).toBeInTheDocument();
    expect(screen.getByText("agent-gamma")).toBeInTheDocument();
  });

  it("shows 'no peers connected' when list is empty", () => {
    useDashStore.setState({ peers: [] });
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(screen.getByText(/no peers connected/i)).toBeInTheDocument();
  });
});

describe("HealthPanel — PeerCard ARIA labels", () => {
  beforeEach(() => {
    useDashStore.setState({
      peers: [livePeer, pausedPeer, reapedPeer],
      selectedPeer: null,
      role: "operator",
      health: null,
      showUTC: false,
    });
  });

  it("each PeerCard has aria-label containing peer name and state", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    // aria-label = "Peer <name> — <state>"
    expect(
      screen.getByRole("button", { name: /Peer agent-alpha — live/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Peer agent-beta — paused/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Peer agent-gamma — reaped/i })
    ).toBeInTheDocument();
  });

  it("operator action buttons have aria-labels", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    // Live peer gets pause, kick, and heartbeat buttons
    expect(
      screen.getByRole("button", { name: /Pause agent-alpha/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Kick agent-alpha/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Send heartbeat to agent-alpha/i })
    ).toBeInTheDocument();
  });

  it("paused peer shows Resume button instead of Pause", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(
      screen.getByRole("button", { name: /Resume agent-beta/i })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Pause agent-beta/i })
    ).not.toBeInTheDocument();
  });

  it("reaped peer shows no operator action buttons", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(
      screen.queryByRole("button", { name: /Kick agent-gamma/i })
    ).not.toBeInTheDocument();
  });
});

describe("HealthPanel — observer role", () => {
  beforeEach(() => {
    useDashStore.setState({
      peers: [livePeer],
      selectedPeer: null,
      role: "observer",
      health: null,
      showUTC: false,
    });
  });

  it("does not render operator action buttons for observer", () => {
    render(<HealthPanel />, { wrapper: Wrapper });
    expect(
      screen.queryByRole("button", { name: /Pause agent-alpha/i })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Kick agent-alpha/i })
    ).not.toBeInTheDocument();
  });
});
