# Next Session Plan — Modularization Complete

## What Was Completed (across two sessions)

### Session 1: ISM + Eli Slot Fix
- `slot_manager.py` — full `SlotManager` / `SlotHandle` / `_NullContext` implementation
- `tests/test_slot_manager.py` — 17 isolation tests, all passing in 0.61s
- `chat.py` — ISM integrated, `send_and_stream` wraps with `_ism.acquire("Eli")`
- `qt/adapter.py` — Eli slot removed from adapter, ISM observer registered

### Session 2: Full Modularization
Split `chat.py` (5167 lines) into 6 focused modules:

| File | Lines | Contents |
|---|---|---|
| `constants.py` | 13 | `BASE_URL`, `CONTROL_URL`, `TTS_URL`, `console` |
| `profiles.py` | 190 | Profile loaders, `SYSTEM_PROMPT`, agent profile helpers |
| `tools.py` | 1170 | `TOOLS` schema, all `tool_*` functions, approval helpers |
| `agents.py` | 1112 | `AgentsMixin`, `_ism`, server control, agent orchestration |
| `commands.py` | 961 | Slash commands, voice subsystem |
| `chat.py` | 1878 | Core: streaming, session, `send_and_stream`, `main()` |

Import chain: `constants ← profiles ← tools ← agents ← chat ← commands (lazy)`

All 17 ISM tests pass (0.60s).

---

## Known Remaining Work

### Optional: DispatchMixin extraction (~200 lines)
Move `_compact_args`, `_compact_result`, `_resolve_path`, `_approval_prompt`,
`_dispatch_tool`, `_call_tool` from `chat.py` into `DispatchMixin` in `tools.py`.
Would reduce `chat.py` by ~200 lines. Low priority.

### Optional: Move `_load_skills` + `_invoke_skill` to `commands.py`
These are only called from `commands.py` (imported back to chat.py). Moving them would
clean up ~100 lines from chat.py. Low priority.

---

## Verification Checklist (next GUI smoke test)

1. `python -m pytest tests/test_slot_manager.py -v` → 17/17 pass ✓ (done)
2. Single Eli turn → 1 red LED during response, 0 after
3. Inline agent → additional red LED while agent runs, releases with "Releasing Slot X"
4. Two background agents → non-blocking, Eli ends turn, LEDs stay red until agents finish
5. `/clear` mid-agents → all LEDs 0 immediately
6. Model switch → slot count updates
7. Server offline → no crash, keeps last slot count
8. `/voice ptt` starts voice mode without crash
