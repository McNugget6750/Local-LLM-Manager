---
name: git-status
description: Full git situation report — status, staged diff, recent log
spawn_agent: false
---
Give me a complete git situation report for the current working directory:

1. `git status` — what's changed, staged, untracked
2. `git diff --staged` — full content of what's staged (if anything)
3. `git diff` — unstaged changes (summarise if large, show fully if small)
4. `git log --oneline -10` — last 10 commits
5. `git stash list` — any stashed work

Summarise what you see: what's ready to commit, what's not staged yet, anything unusual
(untracked files that probably shouldn't be, detached HEAD, merge conflicts, etc.).

$ARGS
