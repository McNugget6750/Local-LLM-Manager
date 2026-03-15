---
name: commit
description: Generate a conventional commit message from staged changes
spawn_agent: false
---
Run `git diff --staged` to see what's staged. Then write a conventional commit message following this format:

```
<type>(<scope>): <short summary>

<optional body — explain WHY if not obvious>
```

Types: feat, fix, refactor, docs, test, chore, perf, style.
Scope: the subsystem or file area affected (optional but helpful).
Summary: imperative mood, lowercase, no period, ≤72 chars.

Rules:
- Read the diff carefully before writing anything.
- The summary line describes what the commit does, not what changed.
- Add a body only if the why isn't obvious from the summary.
- If nothing is staged, say so.

$ARGS
