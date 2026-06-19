<!--
  Thanks for the PR! A few project conventions are below. Delete this comment
  block once you've read it — keep the sections that apply.
-->

## What changed

<!-- A short description of the change and why. -->

## How I verified it

<!-- Commands run / tests added. CI runs ruff, mypy (strict), pytest, and the
     web build — make sure they're green. -->

## Checklist

- [ ] User-visible changes are recorded under `## [Unreleased]` in `CHANGELOG.md`.
- [ ] If `PROTOCOL_TEXT` changed in `hub.py`, `PROTOCOL_VERSION` was bumped too.

> [!IMPORTANT]
> **Don't bump the version in this PR.** The version is derived from git tags by
> `hatch-vcs` — there is no version field to edit and no `chore(release)` commit.
>
> **To release after merge:** create a GitHub Release `vX.Y.Z` (which pushes the
> tag). That tag *becomes* version `X.Y.Z`, and the `Release` workflow builds and
> publishes it. Merge as many PRs as you like between releases — tag only when
> you're ready to ship.
