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

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act, within } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import AgentLauncher from "../AgentLauncher";
import ToastProvider from "../ToastProvider";
import type { AgentInfo, CwdCompletion } from "../../store/types";
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

/** Render the panel as the operator, with `agents` and any overrides seeded. */
function renderWith(
  agents: AgentInfo[],
  overrides: Record<string, unknown> = {}
) {
  useDashStore.setState({ role: "operator", agents, ...overrides });
  return render(<AgentLauncher />, { wrapper: Wrapper });
}

/** The working-directory input. */
function cwdField(): HTMLInputElement {
  return screen.getByLabelText("Agent working directory") as HTMLInputElement;
}

/** The completion dropdown, or null when it is closed. */
function suggestions(): HTMLElement | null {
  return screen.queryByRole("listbox", { name: "Directory suggestions" });
}

/** Type `value` into the working-directory field. */
function typeCwd(value: string) {
  fireEvent.change(cwdField(), { target: { value } });
}

/**
 * Let the completion debounce elapse and the fetch promise settle.
 *
 * Fake timers alone are not enough: the handler awaits the store action, so
 * the pending microtasks have to flush inside the same `act` as the tick.
 */
async function flushCompletion(ms = 150) {
  await act(async () => {
    vi.advanceTimersByTime(ms);
  });
}

/** A store whose completion endpoint answers with `result`. */
function withCompletions(result: CwdCompletion | null) {
  const fetchCwdCompletions = vi.fn().mockResolvedValue(result);
  return { fetchCwdCompletions };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("AgentLauncher — roster row room state", () => {
  beforeEach(() => {
    useDashStore.setState({ role: "operator", agents: [], agentCwd: null });
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
    useDashStore.setState({ role: "operator", agents: [], agentCwd: null });
  });

  it.each(["openai", "codex"])("lets a %s worker use plan and resets permissions on runtime changes", (runtime) => {
    renderWith([]);
    fireEvent.change(screen.getByLabelText("Agent name"), { target: { value: "openai-bot" } });
    fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: runtime } });
    fireEvent.change(screen.getByLabelText("Agent type"), { target: { value: "worker" } });
    fireEvent.change(screen.getByLabelText("Agent permission mode"), { target: { value: "plan" } });
    expect(screen.queryByText(/cannot speak in the room/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Agent permission mode")).not.toHaveTextContent("bypassPermissions");
    fireEvent.change(screen.getByLabelText("Agent runtime"), { target: { value: "claude" } });
    expect(screen.getByLabelText("Agent permission mode")).toHaveValue("auto");
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

// ---------------------------------------------------------------------------
// Per-spawn working directory
// ---------------------------------------------------------------------------

describe("AgentLauncher — working directory field", () => {
  /** Seed the store and fill in a valid name, returning the spawn spy. */
  function renderForm(overrides: Record<string, unknown> = {}) {
    const sendSpawnAgent = vi.fn().mockResolvedValue(true);
    renderWith([], {
      agentCwd: null,
      ...withCompletions(null),
      sendSpawnAgent,
      ...overrides,
    });
    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "alpha" },
    });
    return sendSpawnAgent;
  }

  /** Click Spawn and let the async handler settle. */
  async function spawn() {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Spawn agent" }));
    });
  }

  beforeEach(() => {
    useDashStore.setState({ role: "operator", agents: [], agentCwd: null });
  });

  it("pre-fills the hub's default working directory", () => {
    renderForm({ agentCwd: "/srv/projects" });
    expect(cwdField()).toHaveValue("/srv/projects");
  });

  it("pre-fills when the snapshot lands after the panel mounts", () => {
    // The /ui snapshot is not guaranteed to arrive before the first render.
    renderForm({ agentCwd: null });
    expect(cwdField()).toHaveValue("");

    act(() => {
      useDashStore.setState({ agentCwd: "/srv/late" });
    });

    expect(cwdField()).toHaveValue("/srv/late");
  });

  it("keeps an operator's edit when a later snapshot arrives", () => {
    // A reconnect replays the snapshot; it must not reach into the field and
    // overwrite the directory the operator just typed.
    renderForm({ agentCwd: "/srv/projects" });
    typeCwd("/srv/elsewhere");

    act(() => {
      useDashStore.setState({ agentCwd: "/srv/another-default" });
    });

    expect(cwdField()).toHaveValue("/srv/elsewhere");
  });

  it("sends an edited working directory in the spawn request", async () => {
    const sendSpawnAgent = renderForm({ agentCwd: "/srv/projects" });
    typeCwd("/srv/elsewhere");

    await spawn();

    expect(sendSpawnAgent).toHaveBeenCalledTimes(1);
    expect(sendSpawnAgent.mock.calls[0][0]).toMatchObject({
      cwd: "/srv/elsewhere",
    });
  });

  it("omits cwd when the field is empty, so the hub uses its default", async () => {
    const sendSpawnAgent = renderForm({ agentCwd: null });

    await spawn();

    expect(sendSpawnAgent.mock.calls[0][0].cwd).toBeUndefined();
  });

  it("resets the field to the hub default after a successful spawn", async () => {
    renderForm({ agentCwd: "/srv/projects" });
    typeCwd("/srv/elsewhere");

    await spawn();

    expect(cwdField()).toHaveValue("/srv/projects");
  });

  it("refuses a relative path before the round trip", () => {
    renderForm();
    typeCwd("projects/alpha");

    expect(screen.getByText(/must be an absolute path/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Spawn agent" })).toBeDisabled();
  });

  it("refuses a path containing a '..' segment before the round trip", () => {
    renderForm();
    typeCwd("/srv/../etc");

    expect(screen.getByText(/must not contain/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Spawn agent" })).toBeDisabled();
  });

  it("accepts a path whose existence only the hub can judge", () => {
    // Client validation must never block a submit the server would accept.
    renderForm();
    typeCwd("/srv/does/not/exist/here");

    expect(
      screen.getByRole("button", { name: "Spawn agent" })
    ).not.toBeDisabled();
  });
});

describe("AgentLauncher — working directory completion", () => {
  const dirs = ["/srv/projects/alpha", "/srv/projects/beta"];

  beforeEach(() => {
    vi.useFakeTimers();
    useDashStore.setState({ role: "operator", agents: [], agentCwd: null });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("debounces the completion fetch and does not fire on an empty field", async () => {
    const { fetchCwdCompletions } = withCompletions({ dirs, truncated: false });
    renderWith([], { agentCwd: null, fetchCwdCompletions });

    typeCwd("/srv/pro");
    expect(fetchCwdCompletions).not.toHaveBeenCalled();

    await flushCompletion();
    expect(fetchCwdCompletions).toHaveBeenCalledTimes(1);
    expect(fetchCwdCompletions).toHaveBeenCalledWith("/srv/pro");

    typeCwd("");
    await flushCompletion();
    expect(fetchCwdCompletions).toHaveBeenCalledTimes(1);
    expect(suggestions()).toBeNull();
  });

  it("collapses a burst of keystrokes into one request", async () => {
    const { fetchCwdCompletions } = withCompletions({ dirs, truncated: false });
    renderWith([], { agentCwd: null, fetchCwdCompletions });

    typeCwd("/s");
    typeCwd("/sr");
    typeCwd("/srv");
    await flushCompletion();

    expect(fetchCwdCompletions).toHaveBeenCalledTimes(1);
    expect(fetchCwdCompletions).toHaveBeenCalledWith("/srv");
  });

  it("renders the candidates in the shared dropdown", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs, truncated: false }),
    });

    typeCwd("/srv/pro");
    await flushCompletion();

    const list = suggestions();
    expect(list).not.toBeNull();
    const options = within(list as HTMLElement).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(dirs);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
  });

  it("moves the selection with the arrow keys and accepts with Enter", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs, truncated: false }),
    });

    typeCwd("/srv/pro");
    await flushCompletion();

    fireEvent.keyDown(cwdField(), { key: "ArrowDown" });
    const options = within(suggestions() as HTMLElement).getAllByRole("option");
    expect(options[1]).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(cwdField(), { key: "ArrowUp" });
    expect(
      within(suggestions() as HTMLElement).getAllByRole("option")[0]
    ).toHaveAttribute("aria-selected", "true");

    fireEvent.keyDown(cwdField(), { key: "ArrowDown" });
    fireEvent.keyDown(cwdField(), { key: "Enter" });

    // Accepted with a trailing slash, so the next segment completes straight away.
    expect(cwdField()).toHaveValue("/srv/projects/beta/");
  });

  it("accepts with Tab as well", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs, truncated: false }),
    });

    typeCwd("/srv/pro");
    await flushCompletion();
    fireEvent.keyDown(cwdField(), { key: "Tab" });

    expect(cwdField()).toHaveValue("/srv/projects/alpha/");
  });

  it("accepts a clicked candidate", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs, truncated: false }),
    });

    typeCwd("/srv/pro");
    await flushCompletion();
    const options = within(suggestions() as HTMLElement).getAllByRole("option");
    fireEvent.mouseDown(options[1]);

    expect(cwdField()).toHaveValue("/srv/projects/beta/");
  });

  it("completes the next segment after an acceptance", async () => {
    const { fetchCwdCompletions } = withCompletions({ dirs, truncated: false });
    renderWith([], { agentCwd: null, fetchCwdCompletions });

    typeCwd("/srv/pro");
    await flushCompletion();
    fireEvent.keyDown(cwdField(), { key: "Enter" });
    await flushCompletion();

    expect(fetchCwdCompletions).toHaveBeenLastCalledWith("/srv/projects/alpha/");
  });

  it("closes on Escape and stays closed until the next edit", async () => {
    const { fetchCwdCompletions } = withCompletions({ dirs, truncated: false });
    renderWith([], { agentCwd: null, fetchCwdCompletions });

    typeCwd("/srv/pro");
    await flushCompletion();
    expect(suggestions()).not.toBeNull();

    fireEvent.keyDown(cwdField(), { key: "Escape" });
    expect(suggestions()).toBeNull();

    // No new request while dismissed, and the field keeps what was typed.
    await flushCompletion();
    expect(fetchCwdCompletions).toHaveBeenCalledTimes(1);
    expect(cwdField()).toHaveValue("/srv/pro");

    // The next edit arms completion again.
    typeCwd("/srv/proj");
    await flushCompletion();
    expect(suggestions()).not.toBeNull();
  });

  it("shows the truncation flag as a muted line, not an error", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs, truncated: true }),
    });

    typeCwd("/srv/pro");
    await flushCompletion();

    const list = suggestions() as HTMLElement;
    expect(list).toHaveTextContent("more…");
    // Informational only: it is not an option, so it is not selectable.
    expect(within(list).getAllByRole("option")).toHaveLength(dirs.length);
  });

  it("leaves the field usable when the fetch fails", async () => {
    // A refused or broken completion must degrade to "no suggestions".
    renderWith([], {
      agentCwd: null,
      ...withCompletions(null),
      sendSpawnAgent: vi.fn().mockResolvedValue(true),
    });

    fireEvent.change(screen.getByLabelText("Agent name"), {
      target: { value: "alpha" },
    });
    typeCwd("/srv/projects/alpha");
    await flushCompletion();

    expect(suggestions()).toBeNull();
    expect(cwdField()).toHaveValue("/srv/projects/alpha");
    expect(
      screen.getByRole("button", { name: "Spawn agent" })
    ).not.toBeDisabled();
  });

  it("closes the dropdown on an empty result list", async () => {
    renderWith([], {
      agentCwd: null,
      ...withCompletions({ dirs: [], truncated: false }),
    });

    typeCwd("/srv/zzz");
    await flushCompletion();

    expect(suggestions()).toBeNull();
  });
});
