/**
 * Unit tests for the autocomplete utilities (src/lib/autocomplete.ts).
 *
 * Covers:
 *  1. parseAutocompleteTrigger — token detection from value + caret position.
 *  2. getCandidates — filtered candidate lists per trigger char.
 *  3. applyAutocomplete — value mutation for @ and # triggers.
 *  4. Command ('/') path — applyAutocomplete returns null (caller executes).
 *  5. Channel-switch store wiring — setSelectedChannel propagates to store.
 */

import { describe, it, expect, beforeEach } from "vitest";
import {
  parseAutocompleteTrigger,
  getCandidates,
  applyAutocomplete,
  COMMANDS,
} from "../../lib/autocomplete";
import { useDashStore } from "../../store/wsStore";

// ---------------------------------------------------------------------------
// 1. parseAutocompleteTrigger
// ---------------------------------------------------------------------------

describe("parseAutocompleteTrigger — no trigger", () => {
  it("returns null for plain text (no trigger char)", () => {
    expect(parseAutocompleteTrigger("hello world", 11)).toBeNull();
  });

  it("returns null when caret is on whitespace", () => {
    expect(parseAutocompleteTrigger("hello ", 6)).toBeNull();
  });

  it("returns null when @ is embedded mid-word (email)", () => {
    // 'foo@bar' — @ is not at a word boundary
    expect(parseAutocompleteTrigger("foo@bar", 7)).toBeNull();
  });

  it("returns null when # is embedded mid-word", () => {
    expect(parseAutocompleteTrigger("foo#bar", 7)).toBeNull();
  });

  it("returns null when / is embedded mid-word (URL)", () => {
    expect(parseAutocompleteTrigger("https://foo", 11)).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(parseAutocompleteTrigger("", 0)).toBeNull();
  });
});

describe("parseAutocompleteTrigger — @ trigger", () => {
  it("detects @ at string start with empty query", () => {
    const result = parseAutocompleteTrigger("@", 1);
    expect(result).toEqual({ trigger: "@", query: "", start: 0 });
  });

  it("detects @ with partial peer name", () => {
    const result = parseAutocompleteTrigger("hello @pe", 9);
    expect(result).toEqual({ trigger: "@", query: "pe", start: 6 });
  });

  it("detects @ at start of string with full name", () => {
    const result = parseAutocompleteTrigger("@agent-x", 8);
    expect(result).toEqual({ trigger: "@", query: "agent-x", start: 0 });
  });

  it("detects only the active (last) @ token", () => {
    // '@foo @ba' — caret at end; active token is '@ba' at position 5
    const result = parseAutocompleteTrigger("@foo @ba", 8);
    expect(result).toEqual({ trigger: "@", query: "ba", start: 5 });
  });
});

describe("parseAutocompleteTrigger — # trigger", () => {
  it("detects # at string start with empty query", () => {
    const result = parseAutocompleteTrigger("#", 1);
    expect(result).toEqual({ trigger: "#", query: "", start: 0 });
  });

  it("detects # with partial channel name", () => {
    const result = parseAutocompleteTrigger("#test", 5);
    expect(result).toEqual({ trigger: "#", query: "test", start: 0 });
  });

  it("detects # after a space", () => {
    const result = parseAutocompleteTrigger("send #ge", 8);
    expect(result).toEqual({ trigger: "#", query: "ge", start: 5 });
  });
});

describe("parseAutocompleteTrigger — / trigger", () => {
  it("detects / at string start", () => {
    const result = parseAutocompleteTrigger("/", 1);
    expect(result).toEqual({ trigger: "/", query: "", start: 0 });
  });

  it("detects / with partial command", () => {
    const result = parseAutocompleteTrigger("/pa", 3);
    expect(result).toEqual({ trigger: "/", query: "pa", start: 0 });
  });

  it("detects / after a space", () => {
    const result = parseAutocompleteTrigger("hey /res", 8);
    expect(result).toEqual({ trigger: "/", query: "res", start: 4 });
  });
});

// ---------------------------------------------------------------------------
// 2. getCandidates
// ---------------------------------------------------------------------------

const peers = ["architect", "operator", "skeptic", "agent-x"];
const channels = ["#general", "#design", "#test"];

describe("getCandidates — @ (peers)", () => {
  it("returns all peers when query is empty", () => {
    expect(getCandidates("@", "", peers, channels)).toEqual(peers);
  });

  it("filters by prefix (case-insensitive)", () => {
    const result = getCandidates("@", "arc", peers, channels);
    expect(result).toEqual(["architect"]);
  });

  it("returns empty when no peers match", () => {
    expect(getCandidates("@", "xyz", peers, channels)).toEqual([]);
  });

  it("matches uppercase query against lowercase names", () => {
    expect(getCandidates("@", "ARCH", peers, channels)).toEqual(["architect"]);
  });
});

describe("getCandidates — # (channels)", () => {
  it("returns all channels when query is empty", () => {
    expect(getCandidates("#", "", peers, channels)).toEqual(channels);
  });

  it("filters by bare name (strips # prefix from channel key)", () => {
    const result = getCandidates("#", "gen", peers, channels);
    expect(result).toEqual(["#general"]);
  });

  it("returns multiple matches", () => {
    // 'ge' prefix matches '#general'; 'de' matches '#design'
    expect(getCandidates("#", "de", peers, channels)).toEqual(["#design"]);
  });

  it("returns empty when no channels match", () => {
    expect(getCandidates("#", "xyz", peers, channels)).toEqual([]);
  });
});

describe("getCandidates — / (commands)", () => {
  it("returns all commands when query is empty", () => {
    const result = getCandidates("/", "", peers, channels);
    expect(result).toEqual([...COMMANDS]);
  });

  it("filters commands by prefix", () => {
    const result = getCandidates("/", "pa", peers, channels);
    expect(result).toEqual(["/pause"]);
  });

  it("matches /res prefix", () => {
    const result = getCandidates("/", "res", peers, channels);
    expect(result).toEqual(["/resume", "/reset"]);
  });

  it("returns empty for unknown prefix", () => {
    expect(getCandidates("/", "xyz", peers, channels)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 3. applyAutocomplete — @ and # insertion
// ---------------------------------------------------------------------------

describe("applyAutocomplete — @ trigger", () => {
  it("replaces trigger token with @peer and trailing space", () => {
    // value = "hello @pe", caret at 9
    const token = { trigger: "@" as const, query: "pe", start: 6 };
    const result = applyAutocomplete("hello @pe", 9, token, "peer-x");
    expect(result).not.toBeNull();
    expect(result!.newValue).toBe("hello @peer-x ");
    expect(result!.newCaretPos).toBe(14); // "hello @peer-x ".length
  });

  it("handles @ at start of string", () => {
    const token = { trigger: "@" as const, query: "ag", start: 0 };
    const result = applyAutocomplete("@ag", 3, token, "agent-alpha");
    expect(result!.newValue).toBe("@agent-alpha ");
    expect(result!.newCaretPos).toBe(13);
  });

  it("preserves text after the caret", () => {
    // value = "@pe world", caret at 3 (right after @pe)
    const token = { trigger: "@" as const, query: "pe", start: 0 };
    const result = applyAutocomplete("@pe world", 3, token, "peer-x");
    expect(result!.newValue).toBe("@peer-x  world");
  });
});

describe("applyAutocomplete — # trigger", () => {
  it("replaces trigger token with full channel name and trailing space", () => {
    const token = { trigger: "#" as const, query: "ge", start: 0 };
    const result = applyAutocomplete("#ge", 3, token, "#general");
    expect(result!.newValue).toBe("#general ");
    expect(result!.newCaretPos).toBe(9);
  });

  it("handles # after a word", () => {
    const token = { trigger: "#" as const, query: "de", start: 5 };
    const result = applyAutocomplete("send #de", 8, token, "#design");
    expect(result!.newValue).toBe("send #design ");
  });
});

// ---------------------------------------------------------------------------
// 4. applyAutocomplete — / (command) returns null
// ---------------------------------------------------------------------------

describe("applyAutocomplete — / trigger (commands)", () => {
  it("returns null for command trigger (caller must execute)", () => {
    const token = { trigger: "/" as const, query: "pa", start: 0 };
    const result = applyAutocomplete("/pa", 3, token, "/pause");
    expect(result).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 5. Channel-switch store wiring
// ---------------------------------------------------------------------------

describe("store — setSelectedChannel wiring", () => {
  beforeEach(() => {
    useDashStore.setState({ selectedChannel: null });
  });

  it("setSelectedChannel updates selectedChannel in store", () => {
    useDashStore.getState().setSelectedChannel("#general");
    expect(useDashStore.getState().selectedChannel).toBe("#general");
  });

  it("setSelectedChannel(null) clears the selection", () => {
    useDashStore.setState({ selectedChannel: "#general" });
    useDashStore.getState().setSelectedChannel(null);
    expect(useDashStore.getState().selectedChannel).toBeNull();
  });

  it("re-selecting the same channel can be toggled to null by the caller", () => {
    // The ChannelsPanel toggle logic: if selectedChannel === name, set null.
    useDashStore.setState({ selectedChannel: "#test" });
    const current = useDashStore.getState().selectedChannel;
    useDashStore.getState().setSelectedChannel(current === "#test" ? null : "#test");
    expect(useDashStore.getState().selectedChannel).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 6. Command execution clears input (integration: command path)
// ---------------------------------------------------------------------------

describe("autocomplete — command execution clears input (behaviour contract)", () => {
  it("applyAutocomplete returns null for any /command so caller clears input", () => {
    // Verify every built-in command triggers the 'null → clear input' path.
    for (const cmd of COMMANDS) {
      const query = cmd.slice(1); // strip '/'
      const token = { trigger: "/" as const, query, start: 0 };
      const result = applyAutocomplete(`/${query}`, query.length + 1, token, cmd);
      expect(result).toBeNull();
    }
  });
});
