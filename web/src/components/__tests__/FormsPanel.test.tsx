/**
 * Component tests for FormsPanel — render and a11y checks.
 *
 * Verifies that form fields render correctly and the panel exposes proper
 * ARIA roles/labels.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useDashStore } from "../../store/wsStore";
import type { FormObj } from "../../store/types";
import FormsPanel, { FormModal } from "../FormsPanel";
import ToastProvider from "../ToastProvider";
import { ReactNode } from "react";

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const pendingForm: FormObj = {
  id: "form-1",
  title: "API design approval",
  fields: [
    { key: "decision", label: "Decision", type: "radio", options: ["approve", "reject"], required: true },
    { key: "notes", label: "Notes", type: "textarea" },
  ],
  audience: "all",
  asker: "agent-alpha",
  status: "pending",
};

const answeredForm: FormObj = {
  id: "form-2",
  title: "Architecture review",
  fields: [{ key: "verdict", label: "Verdict", type: "text", required: true }],
  audience: "#design",
  asker: "agent-beta",
  status: "answered",
};

const cancelledForm: FormObj = {
  id: "form-3",
  title: "Cancelled task",
  fields: [],
  audience: "all",
  asker: "agent-gamma",
  status: "cancelled",
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("FormsPanel — empty state", () => {
  beforeEach(() => {
    useDashStore.setState({ forms: [], role: "operator" });
  });

  it("shows empty state message", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText(/no forms/i)).toBeInTheDocument();
  });

  it("renders the forms list region", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByRole("list", { name: "Operator forms" })).toBeInTheDocument();
  });
});

describe("FormsPanel — pending forms", () => {
  beforeEach(() => {
    useDashStore.setState({ forms: [pendingForm, answeredForm, cancelledForm], role: "operator" });
  });

  it("shows the pending form title", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText("API design approval")).toBeInTheDocument();
  });

  it("shows the answered form title", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText("Architecture review")).toBeInTheDocument();
  });

  it("shows the cancelled form title", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText("Cancelled task")).toBeInTheDocument();
  });

  it("pending count badge appears in stats bar", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText(/1 pending form/i)).toBeInTheDocument();
  });

  it("each form row has an aria-label", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(
      screen.getByRole("button", { name: /Form: API design approval — pending/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Form: Architecture review — answered/i })
    ).toBeInTheDocument();
  });

  it("renders Pending section heading", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    // The section heading is an exact "Pending" div (uppercase heading, not the
    // "1 pending form(s)" stats span); use getAllByText and confirm at least one
    // element has that exact text.
    const matches = screen.getAllByText(/Pending/i);
    expect(matches.length).toBeGreaterThan(0);
    // At least one match should be the section header div (text exactly "Pending")
    const heading = matches.find((el) => el.textContent?.trim() === "Pending");
    expect(heading).toBeTruthy();
  });

  it("renders Resolved section heading when resolved forms exist", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    const matches = screen.getAllByText(/Resolved/i);
    expect(matches.length).toBeGreaterThan(0);
    const heading = matches.find((el) => el.textContent?.trim() === "Resolved");
    expect(heading).toBeTruthy();
  });
});

describe("FormModal — allow_other affordance", () => {
  const radioOtherForm: FormObj = {
    id: "form-other-radio",
    title: "Custom radio form",
    fields: [
      {
        key: "channel",
        label: "Rollout channel",
        type: "radio",
        options: ["stable", "beta"],
        required: true,
        allow_other: true,
      },
    ],
    audience: "all",
    asker: "agent-alpha",
    status: "pending",
  };

  const checkboxOtherForm: FormObj = {
    id: "form-other-check",
    title: "Custom checkbox form",
    fields: [
      {
        key: "surfaces",
        label: "Surfaces",
        type: "checkbox",
        options: ["dashboard", "docs"],
        required: true,
        allow_other: true,
      },
    ],
    audience: "all",
    asker: "agent-alpha",
    status: "pending",
  };

  function renderModal(form: FormObj) {
    const onAnswer = vi.fn();
    render(
      <FormModal
        form={form}
        open
        onClose={() => {}}
        onAnswer={onAnswer}
        onCancel={() => {}}
        role="operator"
      />,
      { wrapper: Wrapper }
    );
    return onAnswer;
  }

  it("radio: reveals a custom input on 'Other' and submits the typed value", () => {
    const onAnswer = renderModal(radioOtherForm);

    // The custom input is hidden until "Other" is chosen.
    expect(
      screen.queryByLabelText("Rollout channel — custom value")
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Rollout channel — other"));
    const custom = screen.getByLabelText("Rollout channel — custom value");
    fireEvent.change(custom, { target: { value: "canary" } });

    fireEvent.click(screen.getByRole("button", { name: /submit answer/i }));
    expect(onAnswer).toHaveBeenCalledWith("form-other-radio", {
      channel: "canary",
    });
  });

  it("checkbox: appends the custom value alongside listed selections", () => {
    const onAnswer = renderModal(checkboxOtherForm);

    fireEvent.click(screen.getByLabelText("dashboard"));
    fireEvent.click(screen.getByLabelText("Surfaces — other"));
    fireEvent.change(screen.getByLabelText("Surfaces — custom value"), {
      target: { value: "cli" },
    });

    fireEvent.click(screen.getByRole("button", { name: /submit answer/i }));
    expect(onAnswer).toHaveBeenCalledWith("form-other-check", {
      surfaces: ["dashboard", "cli"],
    });
  });

  it("radio without allow_other shows no 'Other' option", () => {
    render(
      <FormModal
        form={pendingForm}
        open
        onClose={() => {}}
        onAnswer={() => {}}
        onCancel={() => {}}
        role="operator"
      />,
      { wrapper: Wrapper }
    );
    expect(screen.queryByLabelText("Decision — other")).not.toBeInTheDocument();
  });
});

describe("FormsPanel — observer role", () => {
  beforeEach(() => {
    useDashStore.setState({ forms: [pendingForm], role: "observer" });
  });

  it("still renders the pending form row", () => {
    render(<FormsPanel />, { wrapper: Wrapper });
    expect(screen.getByText("API design approval")).toBeInTheDocument();
  });
});
