/**
 * Autocomplete utilities for the OperatorComposer textarea.
 *
 * Detects active trigger tokens (@peer, #channel, /command) from the current
 * caret position and builds filtered candidate lists for the inline dropdown.
 */

/** A character that activates the autocomplete dropdown. */
export type TriggerChar = "@" | "#" | "/";

/** Parsed state of the active autocomplete token at the current caret. */
export interface AutocompleteToken {
  /** The trigger character that activated completion. */
  trigger: TriggerChar;
  /** Text typed after the trigger, up to the caret (the filter query). */
  query: string;
  /** Index of the trigger character in the original string. */
  start: number;
}

/** All recognised slash-commands, including their leading '/'. */
export const COMMANDS = [
  "/pause",
  "/resume",
  "/stop",
  "/reset",
  "/export",
] as const;
export type CommandName = (typeof COMMANDS)[number];

/**
 * Parse the active autocomplete token from `value` at `caretPos`.
 *
 * Scans backwards from the caret looking for a trigger char (@, #, /).
 * A trigger is valid only when it sits at the start of the string or is
 * immediately preceded by whitespace (i.e. at a word boundary).
 *
 * Returns null when no active trigger is found (plain text, trigger
 * embedded mid-word, or caret is inside whitespace).
 *
 * @example
 *   parseAutocompleteTrigger("hello @pe", 9)
 *     → { trigger: '@', query: 'pe', start: 6 }
 *   parseAutocompleteTrigger("#test", 5)
 *     → { trigger: '#', query: 'test', start: 0 }
 *   parseAutocompleteTrigger("/pa", 3)
 *     → { trigger: '/', query: 'pa', start: 0 }
 *   parseAutocompleteTrigger("foo bar", 7)
 *     → null
 *   parseAutocompleteTrigger("some@thing", 10)
 *     → null  (trigger not at word boundary)
 */
export function parseAutocompleteTrigger(
  value: string,
  caretPos: number
): AutocompleteToken | null {
  const before = value.slice(0, caretPos);

  for (let i = before.length - 1; i >= 0; i--) {
    const ch = before[i];

    // Whitespace terminates the backwards scan without finding a trigger.
    if (ch === " " || ch === "\t" || ch === "\n") return null;

    if (ch === "@" || ch === "#" || ch === "/") {
      // Valid trigger: must be at start of string or preceded by whitespace.
      if (i === 0 || /\s/.test(before[i - 1])) {
        return {
          trigger: ch as TriggerChar,
          query: before.slice(i + 1),
          start: i,
        };
      }
      // Embedded mid-word — not a trigger.
      return null;
    }
  }

  return null;
}

/**
 * Build a filtered candidate list for the given trigger + query.
 *
 * - `@` → peer names (bare strings without '@'); query prefix-matched.
 * - `#` → channel names (store keys already include '#'); the query is
 *         compared against the bare name (after the '#').
 * - `/` → slash-command strings with '/' prefix; query matched after '/'.
 *
 * @param trigger      - Active trigger character.
 * @param query        - Text typed after the trigger (filter string).
 * @param peerNames    - Live peer names from the store.
 * @param channelNames - Channel name keys from the store (e.g. "#general").
 */
export function getCandidates(
  trigger: TriggerChar,
  query: string,
  peerNames: string[],
  channelNames: string[]
): string[] {
  const q = query.toLowerCase();

  switch (trigger) {
    case "@":
      return peerNames.filter((p) => p.toLowerCase().startsWith(q));

    case "#": {
      // Channel names carry the '#' prefix; compare query against bare name.
      return channelNames.filter((ch) => {
        const bare = ch.startsWith("#") ? ch.slice(1) : ch;
        return bare.toLowerCase().startsWith(q);
      });
    }

    case "/":
      // COMMANDS include their '/' prefix; query is what follows '/'.
      return [...COMMANDS].filter((cmd) =>
        cmd.slice(1).toLowerCase().startsWith(q)
      );
  }
}

/**
 * Apply a selected candidate to the textarea value.
 *
 * For `@` and `#` triggers: replaces the trigger token (from `token.start`
 * to `caretPos`) with the candidate and a trailing space, returning the
 * updated value and new caret position.
 *
 * For `/` triggers: returns null — the caller is responsible for executing
 * the command and clearing the input.
 *
 * @param value    - Current textarea value.
 * @param caretPos - Current caret position.
 * @param token    - The parsed autocomplete token.
 * @param selected - The candidate chosen by the user.
 */
export function applyAutocomplete(
  value: string,
  caretPos: number,
  token: AutocompleteToken,
  selected: string
): { newValue: string; newCaretPos: number } | null {
  // Commands are executed by the caller, not inserted.
  if (token.trigger === "/") return null;

  // '@peer' → '@peername '  |  '#gen' → '#general ' (channel already has '#')
  const replacement =
    token.trigger === "@" ? `@${selected} ` : `${selected} `;

  const newValue =
    value.slice(0, token.start) + replacement + value.slice(caretPos);

  return { newValue, newCaretPos: token.start + replacement.length };
}
