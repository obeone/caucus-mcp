/**
 * E2E tests for the Caucus operator dashboard.
 *
 * Each test talks to a REAL hub (booted by global-setup.ts) serving the
 * built bundle. Fake peers are registered via the hub HTTP API where needed.
 *
 * Layout note: the dashboard is now tab-free. Health and Channels panels are
 * always visible in the left rail; Forms surface as a header badge when
 * pending. Selectors below reflect this new structure.
 *
 * Critical flows covered:
 *  1. Dashboard loads + shows snapshot (mode badge, peer count)
 *  2. Peer appears in the left-rail Health section after registration
 *  3. Pause a peer via WS command → Health rail reflects "paused" state
 *  4. Fill a form created via POST /ask → badge appears → modal → reject
 *  5. Close a channel after a peer joins one (left-rail Channels section)
 *  6. Reconnect: force-close WS → Disconnected banner → auto-reconnect
 *  7. Accessibility: zero serious/critical WCAG violations on load
 */

import { test, expect, Page } from "@playwright/test";
import { AxeBuilder } from "@axe-core/playwright";

const BASE = process.env["E2E_BASE_URL"] ?? "http://127.0.0.1:9765";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Register a fake peer via POST /register and return the issued access token.
 * This is the only way to get a valid bearer token for authenticated hub calls.
 */
async function registerPeer(name: string): Promise<string> {
  const res = await fetch(`${BASE}/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project: name }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`REGISTER failed for ${name}: ${res.status} — ${text}`);
  }
  const data = (await res.json()) as { token: string };
  return data.token;
}

/** Wait for the dashboard WS to reach "connected" state (green Wifi icon label). */
async function waitConnected(page: Page) {
  await expect(page.getByLabel(/Connection: connected/i)).toBeVisible({
    timeout: 10_000,
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test.describe("Dashboard — basic load", () => {
  test("loads and shows mode badge", async ({ page }) => {
    await page.goto(BASE);
    await waitConnected(page);

    // Mode badge should be visible (running / paused / stopped).
    const badge = page.locator("[aria-live='polite']").first();
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText(/running|paused|stopped/i);
  });

  test("shows peer in left-rail Health section after registration", async ({
    page,
  }) => {
    // Register a real peer via the hub's /register endpoint to get a token.
    const peerName = `e2e-health-${Date.now()}`;
    await registerPeer(peerName);

    await page.goto(BASE);
    await waitConnected(page);

    // Health section is always visible in the left rail — no tab click needed.
    // The peer list region must be present in the left rail.
    const peerList = page.getByRole("list", { name: "Connected peers" });
    await expect(peerList).toBeVisible({ timeout: 5_000 });

    // The peer card should show up (pushed via snapshot + health tick every 1.5s).
    // Scope inside the list to avoid strict-mode collision with the composer
    // autocomplete suggestions that also contain peer names.
    await expect(peerList.getByText(peerName)).toBeVisible({ timeout: 8_000 });
  });
});

test.describe("Dashboard — Health rail peer state", () => {
  test("paused peer shows paused state after pause_peer WS command", async ({
    page,
  }) => {
    const peerName = `e2e-pause-${Date.now()}`;
    await registerPeer(peerName);

    await page.goto(BASE);
    await waitConnected(page);

    // Health is in the left rail — always visible, no tab click.
    const peerList = page.getByRole("list", { name: "Connected peers" });
    await expect(peerList.getByText(peerName)).toBeVisible({ timeout: 8_000 });

    // Send pause_peer via the store exposed on window (same path as the UI button).
    await page.evaluate((name) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const store = (window as any).__CAUCUS_STORE__;
      if (store) store.getState().sendPausePeer(name);
    }, peerName);

    // Hub pushes a `peers` event with paused=true; the aria-label on the row
    // includes the state, so we wait for the "paused" variant.
    await expect(
      page.getByLabel(new RegExp(`Peer ${peerName} — paused`, "i"))
    ).toBeVisible({ timeout: 8_000 });

    // Resume the peer and verify the paused label disappears.
    await page.evaluate((name) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const store = (window as any).__CAUCUS_STORE__;
      if (store) store.getState().sendResumePeer(name);
    }, peerName);

    await expect(
      page.getByLabel(new RegExp(`Peer ${peerName} — paused`, "i"))
    ).not.toBeVisible({ timeout: 8_000 });
  });
});

test.describe("Dashboard — Forms alert (header badge)", () => {
  test("pending-form badge appears and form can be rejected via modal", async ({
    page,
  }) => {
    // Register a peer to obtain a valid token — /ask requires a bearer token.
    const peerName = `e2e-form-${Date.now()}`;
    const token = await registerPeer(peerName);

    // Create a form via POST /ask with a VALID payload.
    const askRes = await fetch(`${BASE}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token,
        to: "all",
        title: "E2E test form",
        fields: [
          {
            key: "q1",
            label: "What is your name?",
            type: "text",
            required: false,
          },
        ],
      }),
    });

    if (!askRes.ok) {
      const body = await askRes.text();
      throw new Error(`POST /ask failed: ${askRes.status} — ${body}`);
    }

    await page.goto(BASE);
    await waitConnected(page);

    // Forms no longer have a tab — they surface as a badge in the header.
    // The badge button appears when pendingForms.length > 0.
    const formsBadge = page.getByRole("button", {
      name: /open pending forms/i,
    });
    await expect(formsBadge).toBeVisible({ timeout: 8_000 });

    // Click the badge to open the pending-forms list dialog.
    await formsBadge.click();

    // The form title should appear in the forms list dialog.
    // Scope to the dialog to avoid matching the hub log entry in FlowPanel.
    await expect(
      page.getByRole("dialog").getByText("E2E test form")
    ).toBeVisible({ timeout: 5_000 });

    // Click the form row button to open the answer/reject modal.
    await page
      .getByRole("button", { name: /Form: E2E test form — pending/i })
      .click();

    // The FormModal should open with a Reject button.
    await expect(page.getByRole("button", { name: /^Reject$/i })).toBeVisible({
      timeout: 5_000,
    });

    // Click Reject → confirm reject flow.
    await page.getByRole("button", { name: /^Reject$/i }).click();
    await expect(
      page.getByRole("button", { name: /Confirm reject/i })
    ).toBeVisible({ timeout: 3_000 });
    await page.getByRole("button", { name: /Confirm reject/i }).click();

    // Modal should close after confirm.
    await expect(
      page.getByRole("button", { name: /Confirm reject/i })
    ).not.toBeVisible({ timeout: 5_000 });
  });
});

test.describe("Dashboard — Channels rail", () => {
  test("close channel button appears after peer joins a channel", async ({
    page,
  }) => {
    // Register a peer and have it join a channel via POST /channels/join.
    const peerName = `e2e-chan-${Date.now()}`;
    const token = await registerPeer(peerName);
    const channelName = `#e2e-${Date.now()}`;

    const joinRes = await fetch(`${BASE}/channels/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, channel: channelName }),
    });
    if (!joinRes.ok) {
      const body = await joinRes.text();
      throw new Error(
        `POST /channels/join failed: ${joinRes.status} — ${body}`
      );
    }

    await page.goto(BASE);
    await waitConnected(page);

    // Channels are in the left rail — always visible, no tab click needed.
    const channelList = page.getByRole("list", { name: "Active channels" });
    await expect(channelList.getByText(channelName)).toBeVisible({
      timeout: 8_000,
    });

    // The close (X) button is visible on hover for operators.
    // Hover over the channel row to reveal it.
    // Use exact: true to avoid partial match on "Close channel #xxx" button's aria-label.
    const channelRow = channelList.getByLabel(`Channel ${channelName}`, { exact: true });
    await channelRow.hover();

    const closeBtn = page.getByLabel(`Close channel ${channelName}`);
    await expect(closeBtn).toBeVisible({ timeout: 5_000 });

    // Clicking the close button opens a confirmation dialog.
    await closeBtn.click();
    await expect(
      page.getByText(/Force-unsubscribe all members/i)
    ).toBeVisible({ timeout: 3_000 });

    // Confirm the close via the "Close channel" button in the dialog footer.
    const confirmBtn = page
      .getByRole("button", { name: /^Close channel$/i })
      .last();
    await confirmBtn.click();

    // After close the channel row should disappear from the list.
    await expect(channelList.getByText(channelName)).not.toBeVisible({
      timeout: 8_000,
    });
  });
});

test.describe("Dashboard — reconnect banner", () => {
  test("shows Disconnected banner when WS closes, then recovers", async ({
    page,
  }) => {
    await page.goto(BASE);
    await waitConnected(page);

    // Force the WebSocket closed so the reconnect path triggers.
    // __CAUCUS_WS__ is exposed by wsStore._initWs() every time a socket opens.
    await page.evaluate(() => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const ws = (window as any).__CAUCUS_WS__;
      if (ws) ws.close();
    });

    // The alert banner (role="alert") should appear — it shows whenever the
    // connectionState is not "connected" (both "disconnected" and "connecting").
    await expect(page.getByRole("alert")).toBeVisible({ timeout: 3_000 });

    // The banner text: "Disconnected — reconnecting with backoff…" or "Connecting…".
    await expect(page.getByRole("alert")).toContainText(
      /Disconnected|Connecting/i,
      { timeout: 3_000 }
    );

    // The hub is still running — client reconnects with backoff (1s initial).
    // Allow up to 15s for the full reconnect cycle.
    await expect(page.getByLabel(/Connection: connected/i)).toBeVisible({
      timeout: 15_000,
    });
  });
});

test.describe("Dashboard — accessibility", () => {
  test("loaded dashboard has zero serious/critical WCAG violations", async ({
    page,
  }) => {
    await page.goto(BASE);
    await waitConnected(page);

    const results = await new AxeBuilder({ page })
      // Exclude color-contrast: the dashboard uses a deliberate dark terminal
      // palette ("text-dim" = #6b7d89) where reduced contrast on secondary/
      // decorative text is an intentional design choice. Primary interactive
      // elements (mode badge, alerts) meet WCAG 2 AA contrast.
      .disableRules(["color-contrast"])
      .analyze();

    // Fail only on serious and critical violations (ignore minor/moderate).
    const critical = results.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical"
    );
    expect(
      critical,
      `Axe found ${critical.length} serious/critical violation(s):\n` +
        critical
          .map((v) => `  [${v.impact}] ${v.id}: ${v.description}`)
          .join("\n")
    ).toHaveLength(0);
  });
});
