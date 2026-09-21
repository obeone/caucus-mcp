/**
 * Component tests for FloorStrip.
 *
 * Uses @testing-library/react with jsdom. The Zustand store is pre-seeded
 * before each test via setState(), mirroring the pattern used in
 * RateControl.test.tsx and HealthPanel.test.tsx.
 *
 * The first block is an anti-regression guard: an exclusive floor must render
 * exactly as it did before round mode existed.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import FloorStrip from "../FloorStrip";
import ToastProvider from "../ToastProvider";
import type { FloorEntry, RoundInfo } from "../../store/types";
import { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Wrapper + fixtures
// ---------------------------------------------------------------------------

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

/** Arbitrary but fixed epoch second used as "now" in the timer tests. */
const NOW = 1758000000;

const exclusiveEntry: FloorEntry = {
  scope: "all",
  holder: "alpha",
  reason: "schema debate",
  hands: ["bravo"],
  since: NOW,
};

function roundInfo(overrides: Partial<RoundInfo> = {}): RoundInfo {
  return {
    ring: ["bravo", "charlie", "alpha"],
    deadline: NOW + 125,
    remaining: 125,
    turn_seconds: 300,
    extensions: 0,
    total_extensions: 0,
    silent_turns: 0,
    started_by: "alpha",
    started_at: NOW,
    paused: false,
    held: {},
    ...overrides,
  };
}

function roundEntry(overrides: Partial<RoundInfo> = {}): FloorEntry {
  return {
    scope: "all",
    holder: "bravo",
    reason: "design review",
    hands: [],
    since: NOW,
    mode: "round",
    round: roundInfo(overrides),
  };
}

function seed(state: Record<string, unknown>) {
  useDashStore.setState({
    role: "operator",
    floors: {},
    sendFloorClear: vi.fn(),
    sendFloorAdvance: vi.fn(),
    ...state,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } as any);
}

// ---------------------------------------------------------------------------
// Anti-regression: exclusive mode is untouched
// ---------------------------------------------------------------------------

describe("FloorStrip — exclusive mode (anti-regression)", () => {
  beforeEach(() => {
    seed({ floors: { all: exclusiveEntry } });
  });

  it("renders the classic badge with no round affordances", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(
      screen.getByRole("status", { name: "Floor held by alpha in all" })
    ).toBeInTheDocument();
    expect(screen.getByText("alpha")).toBeInTheDocument();
    expect(screen.getByText('"schema debate"')).toBeInTheDocument();

    // No round pill, no ring, no countdown.
    expect(screen.queryByText("round")).toBeNull();
    expect(screen.queryByTestId("floor-ring")).toBeNull();
    expect(screen.queryByText(/^\d+:\d{2}$/)).toBeNull();
  });

  it("offers clear but not skip, and keeps the old aria-label", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(
      screen.getByRole("button", { name: "Clear floor for all" })
    ).toBeInTheDocument();
    expect(screen.getByText("clear")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /skip/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /end round/i })).toBeNull();
  });

  it("renders nothing at all when no floor is held", () => {
    seed({ floors: {} });
    render(<FloorStrip />, { wrapper: Wrapper });
    expect(screen.queryByRole("region", { name: "Active floors" })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Round mode: ring
// ---------------------------------------------------------------------------

describe("FloorStrip — round mode ring", () => {
  beforeEach(() => {
    seed({ floors: { all: roundEntry() } });
  });

  it("shows the round pill and the badge's round aria-label", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(screen.getByText("round")).toBeInTheDocument();
    expect(
      screen.getByRole("status", { name: "Round in all, bravo speaking" })
    ).toBeInTheDocument();
  });

  it("renders the ring in rotation order with the holder first", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    const ring = screen.getByTestId("floor-ring");
    expect(ring.textContent).toBe("bravo›charlie›alpha");
  });

  it("emphasises the holder and dims the rest of the ring", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    const ring = screen.getByTestId("floor-ring");
    const holder = within(ring).getByText("bravo");
    const waiting = within(ring).getByText("charlie");

    expect(holder.style.opacity).toBe("1");
    expect(holder.className).toContain("font-medium");
    expect(waiting.style.opacity).toBe("0.5");
    expect(waiting.className).not.toContain("font-medium");
  });

  it("exposes the turn start time as the badge tooltip", () => {
    render(<FloorStrip />, { wrapper: Wrapper });

    const badge = screen.getByRole("status", {
      name: "Round in all, bravo speaking",
    });
    expect(badge.getAttribute("title")).toMatch(
      /^turn started \d{2}:\d{2}:\d{2}$/
    );
  });

  it("shows the extension count only when the holder has taken one", () => {
    render(<FloorStrip />, { wrapper: Wrapper });
    expect(screen.queryByText("+2")).toBeNull();

    seed({ floors: { all: roundEntry({ extensions: 2 }) } });
    render(<FloorStrip />, { wrapper: Wrapper });
    expect(screen.getAllByText("+2").length).toBeGreaterThan(0);
  });

  it("totals the withheld backlog and tooltips it per peer", () => {
    seed({ floors: { all: roundEntry({ held: { charlie: 3, alpha: 4 } }) } });
    render(<FloorStrip />, { wrapper: Wrapper });

    const backlog = screen.getByTitle("withheld until their turn: charlie 3, alpha 4");
    expect(backlog.textContent).toContain("7");
  });
});

// ---------------------------------------------------------------------------
// Round mode: countdown
// ---------------------------------------------------------------------------

describe("FloorStrip — round countdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW * 1000);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("formats the remaining turn time as M:SS and ticks down", () => {
    seed({ floors: { all: roundEntry({ deadline: NOW + 125 }) } });
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(screen.getByText("2:05")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByText("2:02")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(62_000);
    });
    expect(screen.getByText("1:00")).toBeInTheDocument();
  });

  it("turns red under 30 seconds and clamps at 0:00", () => {
    seed({ floors: { all: roundEntry({ deadline: NOW + 20 }) } });
    render(<FloorStrip />, { wrapper: Wrapper });

    const countdown = screen.getByText("0:20");
    expect(countdown.className).toContain("text-red");

    act(() => {
      vi.advanceTimersByTime(40_000);
    });
    expect(screen.getByText("0:00")).toBeInTheDocument();
  });

  it("renders --:-- when the hub sent no deadline", () => {
    seed({ floors: { all: roundEntry({ deadline: null, remaining: null }) } });
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(screen.getByText("--:--")).toBeInTheDocument();
  });

  it("freezes the clock while the room is paused", () => {
    seed({
      floors: { all: roundEntry({ deadline: NOW + 125, remaining: 125, paused: true }) },
    });
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(screen.getByText("2:05")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    // Still 2:05: a paused round has a frozen turn clock.
    expect(screen.getByText("2:05")).toBeInTheDocument();
  });

  it("clears its interval on unmount", () => {
    seed({ floors: { all: roundEntry() } });
    const { unmount } = render(<FloorStrip />, { wrapper: Wrapper });

    const clearSpy = vi.spyOn(window, "clearInterval");
    unmount();
    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// Round mode: operator controls
// ---------------------------------------------------------------------------

describe("FloorStrip — round mode operator controls", () => {
  it("an observer sees the ring but neither skip nor end round", () => {
    seed({ role: "observer", floors: { all: roundEntry() } });
    render(<FloorStrip />, { wrapper: Wrapper });

    expect(screen.getByTestId("floor-ring")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Skip current turn in all" })).toBeNull();
    expect(screen.queryByRole("button", { name: "End round in all" })).toBeNull();
  });

  it("skip calls sendFloorAdvance with the scope and toasts", () => {
    const sendFloorAdvance = vi.fn();
    seed({ floors: { "#design": { ...roundEntry(), scope: "#design" } }, sendFloorAdvance });
    render(<FloorStrip />, { wrapper: Wrapper });

    fireEvent.click(
      screen.getByRole("button", { name: "Skip current turn in #design" })
    );

    expect(sendFloorAdvance).toHaveBeenCalledOnce();
    expect(sendFloorAdvance).toHaveBeenCalledWith("#design");
    expect(screen.getByText("Turn skipped in #design")).toBeInTheDocument();
  });

  it("the clear button becomes 'end round' and still calls sendFloorClear", () => {
    const sendFloorClear = vi.fn();
    seed({ floors: { all: roundEntry() }, sendFloorClear });
    render(<FloorStrip />, { wrapper: Wrapper });

    const endRound = screen.getByRole("button", { name: "End round in all" });
    expect(endRound.textContent).toContain("end round");

    fireEvent.click(endRound);
    expect(sendFloorClear).toHaveBeenCalledWith("all");
  });
});
