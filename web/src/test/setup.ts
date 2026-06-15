/**
 * Vitest global setup — runs before every test file.
 *
 * 1. Extends expect with @testing-library/jest-dom matchers.
 * 2. Stubs browser globals that jsdom doesn't provide so modules that reference
 *    localStorage / matchMedia / WebSocket / history at import time don't throw.
 *
 * These stubs must live here (not inside test files) because ES module imports
 * are hoisted — by the time test-file body code runs, the store has already
 * been evaluated and its module-level IIFE (darkMode init, _initWs) has fired.
 */

import "@testing-library/jest-dom";
import { vi } from "vitest";

// ---------------------------------------------------------------------------
// localStorage — jsdom warns about missing --localstorage-file flag when node
// built-ins handle it; provide a simple in-memory shim instead.
// ---------------------------------------------------------------------------
const _ls: Record<string, string> = {};
vi.stubGlobal("localStorage", {
  getItem: (k: string) => _ls[k] ?? null,
  setItem: (k: string, v: string) => { _ls[k] = v; },
  removeItem: (k: string) => { delete _ls[k]; },
  clear: () => { Object.keys(_ls).forEach((k) => delete _ls[k]); },
});

// ---------------------------------------------------------------------------
// matchMedia — not implemented by jsdom.
// ---------------------------------------------------------------------------
vi.stubGlobal(
  "matchMedia",
  vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
);

// ---------------------------------------------------------------------------
// WebSocket — stub so _initWs() at module load doesn't attempt a real
// connection.  Tests that care about WebSocket behaviour set up their own
// spies after import.
// ---------------------------------------------------------------------------
class _FakeWS {
  static OPEN = 1;
  readyState = _FakeWS.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
}
vi.stubGlobal("WebSocket", _FakeWS);

// ---------------------------------------------------------------------------
// history.replaceState — used by getToken() to strip ?token= from the URL.
// ---------------------------------------------------------------------------
vi.stubGlobal("history", { replaceState: vi.fn() });

// ---------------------------------------------------------------------------
// window.location — provide a minimal stub.
// ---------------------------------------------------------------------------
vi.stubGlobal("location", {
  protocol: "http:",
  host: "localhost",
  search: "",
  pathname: "/",
  hash: "",
});
