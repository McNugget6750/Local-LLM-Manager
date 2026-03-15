---
name: pr
description: Summarise current branch changes for a pull request description
spawn_agent: false
---
Generate a pull request description for the current branch.

Steps:
1. Run `git log --oneline main..HEAD` (or `origin/main..HEAD`) to see all commits on this branch.
2. Run `git diff main...HEAD` to see the full diff (or `git diff origin/main...HEAD`).
3. Write a clear PR description in this format:

## Summary
- Bullet list of what changed and why (3–5 bullets max)

## Changes
- Key files/modules changed and what was done to each

## Test plan
- What to verify manually or via tests

Keep it factual and concise. Focus on the *why* not the *what* — the diff shows what.

$ARGS
