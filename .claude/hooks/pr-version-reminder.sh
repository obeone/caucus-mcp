#!/usr/bin/env bash
#
# pr-version-reminder.sh — PreToolUse(Bash) hook for Claude Code.
#
# Purpose
# -------
# Caucus derives its version from git tags (hatch-vcs): a PR must NOT bump any
# version field, and releases happen by tagging `vX.Y.Z`. This hook fires right
# before a Bash command runs; when that command looks like it opens a PR / MR
# (any platform CLI: gh / fj / glab), it injects a one-shot reminder so Claude
# both follows the rule AND restates it to the human in its reply — the
# counterpart to .github/pull_request_template.md for humans.
#
# Contract
# --------
# Claude Code passes the tool invocation as JSON on stdin (it contains
# `tool_input.command`). We grep the raw payload rather than depend on `jq`;
# a false positive is harmless (it is only a reminder). On a match we print the
# documented PreToolUse `additionalContext` JSON, otherwise nothing. The hook
# is advisory and always exits 0 — it must never block a command.

set -euo pipefail

payload="$(cat)"

# Match the PR/MR creation verbs across the platform CLIs this repo may use:
# gh pr create | fj pr create | glab mr create (and hyphenated variants).
if printf '%s' "$payload" | grep -Eiq '(pr|mr|pull-request|merge-request)[[:space:]_-]*create'; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"[VERSIONING REMINDER] This repo derives its version from git tags via hatch-vcs. Do NOT bump any version in this PR — there is no version field to edit and no chore(release) commit; ensure user-visible changes are under [Unreleased] in CHANGELOG.md. Also explicitly remind the human in your reply: this PR must not bump the version, and to release AFTER merge they create a GitHub Release vX.Y.Z (which pushes the tag) — that tag becomes version X.Y.Z and the Release workflow publishes it."}}'
fi

exit 0
