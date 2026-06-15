/**
 * Unit tests for FlowPanel's markdown rendering and XSS safety.
 *
 * MessageRow is tested directly (named export) rather than through FlowPanel's
 * virtualizer, because jsdom has no layout engine — the virtualizer sees a
 * zero-height container and renders zero items in tests.
 *
 * Covers:
 *  1. Markdown content renders as semantic HTML elements (strong, code, etc.)
 *  2. Raw HTML in message content is escaped — NOT rendered as live DOM nodes.
 *  3. GFM features (strikethrough, tables) render correctly.
 *  4. Links get safe rel/target attributes.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import type { Message } from "../../store/types";
import { MessageRow } from "../FlowPanel";

/** Build a minimal Message fixture with the given content. */
function makeMsg(content: string, overrides: Partial<Message> = {}): Message {
  return {
    id: Math.random().toString(36).slice(2),
    sender: "agent-x",
    recipient: "all",
    kind: "say",
    content,
    ts: Date.now() / 1000,
    ...overrides,
  };
}

/** Render a MessageRow with sensible defaults. */
function renderRow(msg: Message) {
  return render(
    <MessageRow
      msg={msg}
      showUTC={false}
      focused={false}
      onSelect={() => {}}
    />
  );
}

// ---------------------------------------------------------------------------
// 1. Markdown elements render as semantic HTML
// ---------------------------------------------------------------------------

describe("MessageRow — markdown rendering", () => {
  it("renders **bold** as <strong> element", () => {
    renderRow(makeMsg("**hello**"));
    expect(screen.getByText("hello").tagName).toBe("STRONG");
  });

  it("renders inline `code` as <code> element", () => {
    renderRow(makeMsg("`myFunc()`"));
    expect(screen.getByText("myFunc()").tagName).toBe("CODE");
  });

  it("renders both **bold** and `code` in the same message", () => {
    renderRow(makeMsg("**hi** `x`"));
    expect(screen.getByText("hi").tagName).toBe("STRONG");
    expect(screen.getByText("x").tagName).toBe("CODE");
  });

  it("renders _italic_ as <em> element", () => {
    renderRow(makeMsg("_emphasis_"));
    expect(screen.getByText("emphasis").tagName).toBe("EM");
  });

  it("renders an unordered list as <ul>/<li>", () => {
    renderRow(makeMsg("- item one\n- item two"));
    const items = screen.getAllByRole("listitem");
    expect(items.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("item one")).toBeInTheDocument();
    expect(screen.getByText("item two")).toBeInTheDocument();
  });

  it("renders an ordered list as <ol>/<li>", () => {
    const { container } = renderRow(makeMsg("1. first\n2. second"));
    expect(container.querySelector("ol")).toBeTruthy();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it("renders # heading as <h1>", () => {
    renderRow(makeMsg("# Title"));
    expect(screen.getByRole("heading", { level: 1, name: "Title" })).toBeInTheDocument();
  });

  it("renders ## heading as <h2>", () => {
    renderRow(makeMsg("## Sub"));
    expect(screen.getByRole("heading", { level: 2, name: "Sub" })).toBeInTheDocument();
  });

  it("renders GFM ~~strikethrough~~ as <del> element", () => {
    const { container } = renderRow(makeMsg("~~gone~~"));
    const del = container.querySelector("del");
    expect(del).toBeTruthy();
    expect(del!.textContent).toBe("gone");
  });

  it("renders a fenced code block inside a <pre><code>", () => {
    const { container } = renderRow(makeMsg("```js\nconsole.log('hi')\n```"));
    const pre = container.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre!.textContent).toContain("console.log");
  });
});

// ---------------------------------------------------------------------------
// 2. XSS safety — raw HTML must NOT render as live DOM nodes
// ---------------------------------------------------------------------------

describe("MessageRow — XSS / raw HTML safety", () => {
  it("does not render <script> tags from message content", () => {
    const { container } = renderRow(makeMsg("<script>window.__xss=1</script>"));
    // No live <script> element should exist inside the row
    expect(container.querySelectorAll("script").length).toBe(0);
    // The global must not have been set
    expect((window as Record<string, unknown>).__xss).toBeUndefined();
  });

  it("does not render <img onerror> XSS payload", () => {
    renderRow(makeMsg('<img src="x" onerror="window.__xss2=1">'));
    // No img with an onerror attribute — raw HTML is stripped
    const imgs = document.querySelectorAll("img[onerror]");
    expect(imgs.length).toBe(0);
    expect((window as Record<string, unknown>).__xss2).toBeUndefined();
  });

  it("does not render a raw <b> tag as a live element", () => {
    const { container } = renderRow(makeMsg("<b>bold via html</b>"));
    // react-markdown without rehype-raw must not produce a <b> element
    expect(container.querySelector("b")).toBeNull();
  });

  it("does not render <iframe> from message content", () => {
    const { container } = renderRow(
      makeMsg('<iframe src="https://evil.example"></iframe>')
    );
    expect(container.querySelector("iframe")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 3. Link safety
// ---------------------------------------------------------------------------

describe("MessageRow — link safety", () => {
  it("renders markdown links with rel=noopener noreferrer and target=_blank", () => {
    renderRow(makeMsg("[click me](https://example.com)"));
    const link = screen.getByRole("link", { name: "click me" });
    expect(link).toBeInTheDocument();
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("href")).toBe("https://example.com");
  });
});

// ---------------------------------------------------------------------------
// 4. System messages render as plain text (not markdown)
// ---------------------------------------------------------------------------

describe("MessageRow — system message plain text", () => {
  it("renders system message content without markdown parsing", () => {
    const { container } = renderRow(
      makeMsg("**raw** text", { kind: "system", sender: "hub", recipient: "all" })
    );
    // System messages are plain text — no <strong> tag
    expect(container.querySelector("strong")).toBeNull();
    expect(screen.getByText("**raw** text")).toBeInTheDocument();
  });
});
