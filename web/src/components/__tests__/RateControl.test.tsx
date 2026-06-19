/**
 * Component tests for RateControl.
 *
 * Uses @testing-library/react with jsdom.  The Zustand store is pre-seeded
 * before each test via setState(), mirroring the pattern used in
 * HealthPanel.test.tsx and FormsPanel.test.tsx.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import RateControl from "../RateControl";
import ToastProvider from "../ToastProvider";
import { ReactNode } from "react";

// ---------------------------------------------------------------------------
// Wrapper
// ---------------------------------------------------------------------------

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("RateControl — operator role with rate data", () => {
  beforeEach(() => {
    useDashStore.setState({
      role: "operator",
      rate: { refill_rate: 2, capacity: 10 },
      sendSetRate: vi.fn(),
    });
  });

  it("renders the region with aria-label 'Rate control'", () => {
    render(<RateControl />, { wrapper: Wrapper });
    expect(screen.getByRole("region", { name: "Rate control" })).toBeInTheDocument();
  });

  it("displays the current effective rate from the store", () => {
    render(<RateControl />, { wrapper: Wrapper });
    // refill_rate=2 → 120/min, burst=10
    expect(screen.getByText(/120\/min/)).toBeInTheDocument();
    expect(screen.getByText(/burst 10/)).toBeInTheDocument();
  });

  it("renders the 'applies to all peers now; clamps in-flight bursts' caption", () => {
    render(<RateControl />, { wrapper: Wrapper });
    expect(
      screen.getByText("applies to all peers now; clamps in-flight bursts")
    ).toBeInTheDocument();
  });

  it("renders rate value, unit selector, burst, and Apply button", () => {
    render(<RateControl />, { wrapper: Wrapper });
    expect(screen.getByLabelText("Rate value")).toBeInTheDocument();
    expect(screen.getByLabelText("Rate unit")).toBeInTheDocument();
    expect(screen.getByLabelText("Burst size")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /apply rate limit/i })).toBeInTheDocument();
  });
});

describe("RateControl — sendSetRate integration", () => {
  it("entering rate 30 /min and burst 5, then Apply, calls sendSetRate(0.5, 5)", () => {
    const sendSetRate = vi.fn();
    useDashStore.setState({
      role: "operator",
      // Start with no rate so inputs initialise empty (easier to control).
      rate: null,
      sendSetRate,
    });

    render(<RateControl />, { wrapper: Wrapper });

    // Set unit to /min first (it already defaults to /min, but be explicit).
    const unitSelect = screen.getByLabelText("Rate unit");
    fireEvent.change(unitSelect, { target: { value: "/min" } });

    // Type 30 into the rate input.
    const rateInput = screen.getByLabelText("Rate value");
    fireEvent.change(rateInput, { target: { value: "30" } });

    // Type 5 into the burst input.
    const burstInput = screen.getByLabelText("Burst size");
    fireEvent.change(burstInput, { target: { value: "5" } });

    // Click Apply.
    const applyBtn = screen.getByRole("button", { name: /apply rate limit/i });
    expect(applyBtn).not.toBeDisabled();
    fireEvent.click(applyBtn);

    // 30 /min = 0.5 /s
    expect(sendSetRate).toHaveBeenCalledOnce();
    expect(sendSetRate).toHaveBeenCalledWith(0.5, 5);
  });
});

describe("RateControl — caption always present for operator", () => {
  it("shows the required caption text", () => {
    useDashStore.setState({
      role: "operator",
      rate: { refill_rate: 1, capacity: 5 },
      sendSetRate: vi.fn(),
    });
    render(<RateControl />, { wrapper: Wrapper });
    expect(
      screen.getByText("applies to all peers now; clamps in-flight bursts")
    ).toBeInTheDocument();
  });
});

describe("RateControl — observer role", () => {
  it("renders nothing when role is observer", () => {
    useDashStore.setState({
      role: "observer",
      rate: { refill_rate: 1, capacity: 5 },
    });
    render(<RateControl />, { wrapper: Wrapper });
    // When the component returns null, no rate-control region is present in the DOM.
    expect(screen.queryByRole("region", { name: "Rate control" })).toBeNull();
  });
});
