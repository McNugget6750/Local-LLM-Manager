# Next Session Plan — Modularization + ISM Unification

## What Was Just Completed (this session)

- `slot_manager.py` — full `SlotManager` / `SlotHandle` / `_NullContext` implementation
- `tests/test_slot_manager.py` — 17 isolation tests, all passing in 0.61s
- `chat.py` — ISM integrated: `_ism` singleton, `_tool_spawn_agent` acquires/releases slots for
  all agents (inline + background), background eligibility rewritten, `/clear` calls
  `force_release_all`, model-switch refresh
- `qt/adapter.py` — old ad-hoc slot tracking removed, ISM observer registered, `_run_turn`
  acquires Eli's slot, `_drain_bg_agents` cleaned up, approval passthrough added
- **Critical bug fixed last minute**: `SlotHandle.release()` was setting `_released = True`
  before calling `_do_release`, which immediately returned on the guard — slots never actually
  released. Also `slot_snapshot()` was async when it should be sync. Both fixed, 17/17 pass.

---

## Known Architectural Flaw — Fix First

**Eli's slot is acquired in the wrong layer.**

Currently: `qt/adapter.py` → `_run_turn()` calls `_ism.acquire("Eli")`.
Should be: `chat.py` → `send_and_stream()` wraps itself with `_ism.acquire("Eli")`.

The adapter is a UI bridge. It should not own resource lifecycle decisions. All slot acquisition
(Eli + agents) must live in `chat.py` for consistency, debuggability, and correctness.

### Fix (straightforward):

```python
# chat.py — send_and_stream()
async def send_and_stream(self, user_text, plan_mode=False):
    await self._inject_pending_bg_results()
    ...
    async with await _ism.acquire("Eli", timeout_secs=None) as _eli_slot:
        # entire existing body of send_and_stream
        ...
```

Then remove the `_ism.acquire("Eli")` block from `qt/adapter.py` → `_run_turn()`.
The adapter becomes purely reactive — it registers the ISM observer and reacts to
`slots_updated` signals. Nothing more.

---

## Modularization Plan — chat.py is 5166 lines

This is causing daily context budget exhaustion. Every read, every edit, every pass over the
file costs tokens. Split into focused modules under ~800-1000 lines each.

### Proposed split

| New file | Contents | Approx lines |
|---|---|---|
| `chat.py` | `ChatSession` class, `send_and_stream`, `_inject_pending_bg_results`, `_detect_ctx_window`, `__aenter__`/`__aexit__`, constants, `_ism` singleton | ~600 |
| `agents.py` | `_tool_spawn_agent`, `_run_background_agent`, `_flush_agent_batch`, `_extract_write_path`, write-lock helpers | ~800 |
| `tools.py` | All tool implementations (`_run_web_search`, `_run_edit`, `_run_write_file`, `_run_bash`, etc.) | ~1000 |
| `commands.py` | All `/slash` command handlers (`handle_slash_command` and its branches) | ~600 |
| `profiles.py` | Agent profile loading, `_inject_capabilities`, system prompt assembly | ~300 |

`ChatSession` holds the state; the other modules receive `self` as needed (or are methods moved
to mixins/module-level functions that take the session as argument).

### Why this helps
- Targeted reads load only the relevant file, not 5000 lines
- Edits to agent logic don't touch chat loop and vice versa
- Tests can import `agents.py` without pulling in Qt or full session setup
- Context budget per session drops dramatically

---

## Environment Variable Audit

`local-claude.bat` currently has `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70`.
CLAUDE.md still says `50` — **update CLAUDE.md to reflect 70%**.

No other Claude-specific overrides found beyond:
- `ANTHROPIC_BASE_URL=http://localhost:1234`
- `ANTHROPIC_AUTH_TOKEN=lmstudio`
- `CLAUDE_CODE_ATTRIBUTION_HEADER=0`
- `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70`

All of these are intentional for local LLM use. Nothing stale or accidental to remove.

---

## Recommended session order

1. **Fix Eli slot layer** — move `_ism.acquire("Eli")` from `adapter._run_turn` into
   `chat.send_and_stream`. Verify 17 tests still pass. Quick GUI smoke test.

2. **Modularize chat.py** — extract in this order to minimize breakage risk:
   - `profiles.py` first (no dependencies on other extracted modules)
   - `commands.py` second (depends on session state only)
   - `tools.py` third (depends on session state + possibly profiles)
   - `agents.py` last (depends on tools + session + ISM)
   - `chat.py` becomes the thin core

3. **Update CLAUDE.md** — fix the 50% → 70% compaction note.

4. **Verify** — run existing 17 ISM tests + GUI smoke test after each extraction step.

---

## ISM Design Principles (do not regress)

- All slot acquisition in `chat.py` layer, zero slot logic in adapter
- Adapter registers `on_change` observer only — purely reactive
- `SlotHandle` is always used as async context manager — no manual release except in `finally`
- Inline agents: `timeout_secs=None`, `slot.task = None` (eviction does not apply)
- Background agents: `timeout_secs=900.0`, `slot.task = asyncio.current_task()`
- `force_release_all()` called by `/clear` — idempotent, safe to double-call
- `slot_snapshot()` is sync — safe to call from observer callback

---

## Verification checklist (post-modularization)

1. Single Eli turn → 1 red LED during response, 0 after
2. Inline agent → additional red LED while agent runs, releases with "Releasing Slot X"
3. Two background agents → non-blocking, Eli ends turn, LEDs stay red until agents finish
4. `/clear` mid-agents → all LEDs 0 immediately
5. Model switch → slot count updates
6. Server offline → no crash, keeps last slot count
