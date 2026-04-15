# Orchestration Layer — State & Next Steps

*Last updated: 2026-04-14*

---

## What We're Building

A multi-phase orchestration loop inside the Eli Qt GUI (`qwen3-manager`) that takes a complex user request and reliably executes it end-to-end using background agents — without the user having to manage the process.

The key insight from a manual run that worked well: **the implementation plan is the source of truth**. When Eli writes a solid plan first and then executes it, the output is consistently good. The orchestration layer's job is to make that happen reliably and faster via parallel background agents.

---

## Desired Flow (Option C)

```
User request
    │
    ▼
Phase 1 — Explore (one turn, Eli decides)
    ├── Has enough context? → emit [READY_TO_PLAN]
    └── Needs context?     → dispatch ONE quick-scan agent, wait for results
    │
    ▼
Phase 2 — Plan (plan_mode=True, Eli writes inline)
    • Structured plan: Goal / Files / Steps / Agent scopes / Verify criteria
    • Plan is the source of truth — nothing implements until this exists
    • [SKIP_APPROVAL] → skip user gate and proceed immediately
    • Otherwise → shown to user, waits for approval
    │
    ▼
Phase 3 — Implement (after approval or [SKIP_APPROVAL])
    • Dispatch one expert_coder agent per scope defined in the plan
    • All agents run in parallel
    • Scopes come from the plan — Eli does not improvise new ones
    │
    ▼
Phase 4 — Verify
    • Dispatch code-review agent OR verify directly via bash
    • Issues found → targeted expert_coder fix → re-verify
    │
    ▼
Phase 5 — Done
    • 3–5 bullet summary
    • [ORCHESTRATION_DONE] emitted → harness exits orchestration mode
```

---

## Entry Paths Into Orchestration

1. **Classifier (Tier 1):** Request auto-classified as "orchestrate" → adapter sets `_orch_active = True`, injects orchestration pulse, routes to Eli.
2. **Eli self-selects (Tier 2):** Eli outputs `[ORCHESTRATE]` as the first line → adapter rolls back the assistant message, sets state, re-runs with orchestration pulse.
3. **`!plan` / `!o` prefix (Tier 3):** Existing slash/prefix mechanic, unchanged.

---

## What's Implemented (as of today)

### Harness (`qt/adapter.py`)
- `_orch_active`, `_orch_phase`, `_orch_pulse` state on `QtChatAdapter`
- Orchestration pulse loaded at startup from `agents/orchestration_pulse.md`
- `_dispatch_turn` routes based on `_orch_phase`:
  - explore → runs with orch pulse
  - planning (system notification) → runs in plan_mode
  - planning (user approval) → advances to implementing, runs with orch pulse
  - implementing/verifying/done → runs with orch pulse
- Signal detection in `_drain_queue`:
  - `[ORCHESTRATE]` → Tier 2 rollback + re-queue
  - `[ORCHESTRATION_DONE]` → exits orchestration
  - `[SKIP_APPROVAL]` → auto-advances from planning to implementing
  - `[READY_TO_PLAN]` → advances from explore to planning, queues plan turn
- `_drain_bg_agents` phase-aware notifications:
  - explore → planning notification when agents complete
  - implementing → verifying notification
  - verifying → done-or-fix notification

### Agent Profiles
- `agents/orchestration_pulse.md` — injected as system pulse during orchestration turns
- `agents/quick-scan.md` — lightweight context gatherer (up to 8 file reads, up to 6 targeted web searches)

### chat.py
- `send_and_stream` takes `custom_pulse: str | None` — used to inject orchestration pulse instead of behavioral pulse
- Old `orchestrator_mode` param and no-tools payload branch removed

### ELI.md
- Orchestration Workflow section added with phase table and all signals

---

## Implemented This Session (2026-04-15)

### Orchestration state persistence — compaction survival

`adapter.py` now persists orchestration state to `sessions/state.json` on every phase
transition via the existing `save_state` / `load_state` mechanism.

- New field: `_orch_original_request` — stores first 300 chars of the triggering request
- New methods: `_save_orch_state()` / `_clear_orch_state()`
- `_save_orch_state()` called at all 6 phase-transition sites:
  - Tier 1 classify → orchestrate
  - Tier 2 `[ORCHESTRATE]` self-select
  - `[READY_TO_PLAN]` signal
  - `[SKIP_APPROVAL]` signal
  - User plan approval (planning → implementing)
  - `_drain_bg_agents`: explore → planning, implementing → verifying
- `_clear_orch_state()` called on `[ORCHESTRATION_DONE]`
- On startup, if `orch_active=True` in state, restores all three fields and injects a
  recovery system message into the work queue so Eli knows where he was

**Note:** Eli's announce-then-act pattern is intentional UX. The problem was not the
announcement but that compaction could fire between the announcement turn and the action
turn. State persistence closes that gap — on restart, Eli is told his phase and original
request explicitly.

---

## Known Problems (fix next)

### 1. Phase 1 tool restriction — NOT enforced at the harness level

**Symptom:** Eli reads files and searches the web himself in the explore turn instead of dispatching a quick-scan agent or emitting `[READY_TO_PLAN]`. The pulse says not to, but the model ignores it.

**Root cause:** The pulse can guide but not enforce. Eli has access to all tools during the explore turn, and the instruction to only call `spawn_agent` is advisory.

**Fix:** In `_run_turn` (adapter.py), when `_orch_active and _orch_phase == "explore"`, build the payload with a restricted tool list — `spawn_agent` only. Same mechanism as `plan_mode` already uses for web_search/web_fetch restriction.

Specifically, in `send_and_stream` (chat.py), when `_orch_phase == "explore"`, filter `TOOLS` to only the `spawn_agent` entry before building the payload. This needs a way to signal that we're in explore phase — either pass it as a parameter or derive it from a flag.

Simplest approach: add `explore_mode: bool = False` to `send_and_stream` (like `plan_mode`), and in `_run_turn` pass it when `_orch_phase == "explore"`. In `send_and_stream`, when `explore_mode=True`, filter tools to `spawn_agent` only.

### 2. Explore turn completes with no agents and no [READY_TO_PLAN]

**Symptom:** If Eli ignores both paths (no agent dispatched, no signal emitted), the harness stays stuck in `_orch_phase == "explore"` indefinitely.

**Fix:** In the `done` event handler in `_drain_queue`, add a fallback: if `_orch_active and _orch_phase == "explore"` and no bg agents are running and no bg results are pending, treat it as `[READY_TO_PLAN]` — advance to planning and queue the plan turn.

### 3. Old orchestrator references still in codebase

`agents/orchestrator.md` still exists (the old single-turn recon approach). It's no longer used by the harness but may confuse Eli if he reads it. Consider deleting or clearly marking it as deprecated.

### 4. [ORCHESTRATION_DONE] fires too early

Seen in a real run: Eli emitted `[ORCHESTRATION_DONE]` at the end of the explore phase, before any plan or implementation. Under the new harness this would actually terminate orchestration incorrectly.

The pulse now says explicitly "only in Phase 5, after verification" in two places. The harness-level fix (explore_mode tool restriction in #1) will also prevent Eli from writing a full response in the explore turn that could contain this signal.

---

## Remaining Design Questions

- **Should [SKIP_APPROVAL] exist?** In the dlss-video-upscaler run, skipping the approval gate led to bad implementation (no verified plan, agent hallucinated success). Consider requiring approval always, or only skipping it for very simple targeted requests.

- **Verify phase is currently optional in practice.** The pulse says it's mandatory, but if Eli emits `[ORCHESTRATION_DONE]` without dispatching code-review, the harness won't stop it. A counter-measure: track whether a code-review agent ran; if not, queue a verify notification before allowing Done.

- **quick-scan vs explore agent.** The `explore` agent profile is still there and still used for non-orchestrated requests. `quick-scan` is the orchestration-specific lighter variant. Make sure Eli uses `quick-scan` (not `explore`) in orchestration Phase 1.

---

## Files Changed This Session

| File | Change |
|------|--------|
| `qt/adapter.py` | Orchestration state, phase routing, signal detection, phase-aware drain notifications |
| `chat.py` | `custom_pulse` param, removed `orchestrator_mode` |
| `agents/orchestration_pulse.md` | New — injected pulse for orchestration turns |
| `agents/quick-scan.md` | New — lightweight context agent for Phase 1 |
| `ELI.md` | Orchestration Workflow section |
| `behavioral_pulse.md` | Updated orchestrator routing description |
| `ORCHESTRATION_NEXT_STEPS.md` | This file |

---

## Next Session — Priority Order

1. **`explore_mode` tool restriction** — `send_and_stream` + `adapter.py`: add `explore_mode: bool` param, filter tools to `spawn_agent` only when active. Fixes Problems #1 and #2. (See problem descriptions above.)
2. **Run dlss-video-upscaler test** — observe Phase 1 behavior with restriction in place
3. **Decide on `[SKIP_APPROVAL]`** — keep or remove based on what the test run shows
4. **Plan annotation format** — update `orchestration_pulse.md` to require `[parallel: true/false]` and `[activity: type]` annotations in Phase 2 scopes so the harness can eventually drive dispatch from the plan directly (borrowed from `skills/implementation_plan.md` — not the full TDD/spec machinery, just the annotation syntax)
5. **Verify phase enforcement** — track whether a code-review agent ran; block `[ORCHESTRATION_DONE]` if not
