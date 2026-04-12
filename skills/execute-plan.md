---
name: execute-plan
description: Execute an implementation plan end-to-end with automatic review-fix loops (up to 3 cycles)
spawn_agent: false
think_level: on
triggers:
  - execute plan
  - execute the plan
  - proceed with plan
  - run the plan
  - implement and review
  - work through the plan
  - execute-plan
  - run implementation plan
---

Execute the implementation plan at: $ARGS

Follow the Execute Loop Protocol defined in your system instructions exactly:

1. **Read** — Read the plan document. State the phases and what you are about to execute.
2. **Execute** — Work through every phase in order using your standard agent delegation rules.
3. **Review loop** — After all phases are done, run the review-fix cycle (max 3 iterations):
   - Spawn `code-review` agent: compare implementation against the plan, list Critical and High issues only.
   - If any Critical or High issues found: spawn `expert_coder` to fix them, then re-review.
   - Stop when no Critical/High issues remain OR 3 cycles are exhausted.
4. **Summary** — Report phases completed, final review status, any remaining known issues.

Do not pause between steps for confirmation. This is an approved autonomous execution.
