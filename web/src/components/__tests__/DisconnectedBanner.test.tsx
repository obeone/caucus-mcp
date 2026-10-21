/** Connection banner coverage for cancelled authentication and explicit retry. */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import DisconnectedBanner from "../DisconnectedBanner";

afterEach(() => {
  cleanup();
  useDashStore.setState({ _authCancelled: false, _ws: null });
});

describe("DisconnectedBanner", () => {
  it("explains cancelled authentication and resumes through the retry button", () => {
    useDashStore.setState({ connectionState: "disconnected", _authCancelled: true });
    render(<DisconnectedBanner />);

    expect(screen.getByRole("alert")).toHaveTextContent("Authentication cancelled");
    expect(screen.getByRole("alert")).not.toHaveTextContent("reconnecting");
    fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));

    expect(useDashStore.getState()._authCancelled).toBe(false);
    expect(useDashStore.getState()._ws).not.toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Connecting to hub");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("still describes automatic reconnection after a transport failure", () => {
    useDashStore.setState({ connectionState: "disconnected", _authCancelled: false });
    render(<DisconnectedBanner />);
    expect(screen.getByRole("alert")).toHaveTextContent("reconnecting with backoff");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("hides the banner while connected", () => {
    useDashStore.setState({ connectionState: "connected", _authCancelled: false });
    render(<DisconnectedBanner />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
