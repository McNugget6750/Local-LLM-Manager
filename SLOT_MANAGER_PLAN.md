# Inference Slot Manager — Implementation Plan

## 1. Problem Statement

The existing ad-hoc slot tracking (`_slots_in_use`, `bg_slot_acquired`, `bg_slot_released`,
`_query_live_slots`) is unreliable because:

- No single owner: counters are incremented in one place and decremented in another with no
  guarantee the two always pair up.
- No exception safety: any error that bypasses the decrement path leaves the counter drifted.
- No timeout: a stuck background agent holds its "slot" forever.
- No identity: nothing records *who* is using a slot, making debugging impossible.
- No force-release: `/clear` cannot reliably stop all agents and reset state.

**Goal**: replace all of this with a single `SlotManager` that is the sole source of truth
for slot occupancy.

---

## 2. Architecture Decision: Hybrid (Client + Server Polling)

**Why not pure server-side**: The llama.cpp `/slots` endpoint shows physical slot state but
has no concept of who owns a slot, when it expires, or how to force-evict a specific task.
Timeouts, ownership labels, and force-release require client-side logic.

**Why not pure client-side**: Model switches can change the server's `--parallel N`
configuration. A client-only manager would hold a stale slot count after a switch.

**Decision**: Client-side `SlotManager` for ownership/timeout/force-release, plus periodic
`GET /slots` polling to correct capacity drift after model switches.

---

## 3. `SlotManager` Class (`slot_manager.py`)

### 3.1 Data Structures

```
SlotHandle (dataclass)
├── index: int                    # 0-based slot number assigned by ISM
├── label: str                    # "Eli" | "Agent: researcher" | "Agent 1" etc.
├── task: asyncio.Task | None     # asyncio Task owning this slot (None = inline/Eli)
├── acquired_at: float            # time.monotonic()
├── timeout_secs: float | None    # None = no timeout; 900.0 = 15 min agent timeout
├── _manager: SlotManager         # back-reference for release
└── _released: bool               # idempotency guard

SlotManager
├── _total: int                          # current physical slot count from server
├── _slots: dict[int, SlotHandle]        # index → active handle
├── _condition: asyncio.Condition        # waiters sleep here; wraps _lock
├── _lock: asyncio.Lock                  # underlying lock for _condition
├── _base_url: str
├── _refresh_task: asyncio.Task | None   # periodic refresh coroutine
├── _change_callbacks: list[Callable]    # zero-arg observers for UI
└── _refresh_interval: float             # seconds between polls (default 180)
```

### 3.2 Public API

```python
async def initialize() -> None
    # GET /slots → set _total; start _refresh_task.
    # Called once from ChatSession.__aenter__ (via _detect_ctx_window).
    # Defaults _total = 1 before server responds (safe minimum).

async def refresh_from_server() -> None
    # GET /slots → update _total; prune capacity if shrunk (does NOT evict
    # active holders — they already hold server resources).
    # Non-raising: on HTTP failure, keeps last-known total and logs.
    # Notifies observers.

async def acquire(label: str, timeout_secs: float | None = None) -> SlotHandle
    # Block under _condition until in_use < _total.
    # Assign lowest free index, create and record SlotHandle, notify observers.
    # Caller MUST use as async context manager or call handle.release() manually.

async def force_release_all() -> None
    # Under lock: copy all handles, clear _slots.
    # After lock: cancel each handle's task (if set).
    # Notifies observers.
    # Called by /clear command handler.

def slot_snapshot() -> list[dict]
    # Returns [{index, label, acquired_at_secs, timeout_secs, timed_out}, ...]
    # Safe to call without lock (dict copy under GIL is safe for reading).

def on_change(callback: Callable[[], None]) -> None
    # Register a zero-argument observer. Called synchronously after any state change.

def total_slots() -> int        # current _total
def in_use() -> int             # len(_slots)
def is_initialized() -> bool    # True after first successful initialize()

async def shutdown() -> None
    # Cancel _refresh_task; do not release slots (let __aexit__ handle them).
```

### 3.3 `SlotHandle` protocol

```python
async def __aenter__(self) -> SlotHandle: return self
async def __aexit__(self, *_) -> None:   await self.release()
async def release(self) -> None:
    if self._released: return
    self._released = True
    await self._manager._do_release(self)

def is_expired(self) -> bool:
    if self.timeout_secs is None: return False
    return (time.monotonic() - self.acquired_at) > self.timeout_secs
```

### 3.4 Timeout Eviction

Eviction is checked inside `_periodic_refresh` (not via per-handle timers).
After each server poll, walk active handles and evict expired ones:

```python
for handle in list(self._slots.values()):
    if handle.is_expired():
        await self._evict(handle, reason="15-minute timeout")

async def _evict(self, handle, reason):
    handle._released = True
    async with self._condition:
        self._slots.pop(handle.index, None)
        self._condition.notify_all()
    if handle.task and not handle.task.done():
        handle.task.cancel()
    self._notify_observers()
```

---

## 4. Integration Points

### 4.1 `chat.py`

#### Module-level singleton

```python
from slot_manager import SlotManager, SlotHandle
_ism = SlotManager(base_url=BASE_URL)
```

#### `_detect_ctx_window` — initialization

```python
async def _detect_ctx_window(self) -> None:
    await _ism.initialize()
    self.server_parallel_slots = _ism.total_slots()   # keep for compat
    # n_ctx extraction: _ism exposes raw slot data via _raw_slots attribute
```

#### `send_and_stream` — Eli's slot

```python
async def send_and_stream(self, user_text, plan_mode=False):
    await self._inject_pending_bg_results()
    ...
    async with await _ism.acquire("Eli", timeout_secs=None) as eli_slot:
        # entire body of send_and_stream
```

The `async with` guarantees release even on `httpx.ConnectError` or cancellation.

#### `_tool_spawn_agent` — inline and background agents

Add `_slot_handle: SlotHandle | None = None` parameter.
When `None` (inline call), acquire internally. When provided (background call), skip acquire:

```python
async def _tool_spawn_agent(self, ..., _slot_handle=None):
    self._subagent_depth += 1
    _own = _slot_handle is None
    if _own:
        _ctx = await _ism.acquire(label=agent_label or "Agent", timeout_secs=900.0)
    else:
        _ctx = _NullContext(_slot_handle)   # no-op context manager
    async with _ctx as _slot:
        if _slot_handle is None:
            _slot.task = asyncio.current_task()   # register for eviction only for background
        try:
            ...   # existing body
        finally:
            self._subagent_depth -= 1
```

Note: inline agents (`_slot_handle=None`) set `task=None` so the eviction loop skips them
(inline agents are bounded by the parent turn, not by a 15-min timeout).

#### `_run_background_agent` — background agents

```python
async def _run_background_agent(self, tc, args, label, current_model):
    async with await _ism.acquire(label=label or "Agent", timeout_secs=900.0) as slot:
        slot.task = asyncio.current_task()
        result = "[cancelled]"
        try:
            result = await self._tool_spawn_agent(
                ..., _slot_handle=slot
            )
        except asyncio.CancelledError:
            result = "[agent evicted — 15-minute timeout reached]"
            raise
        except Exception as exc:
            result = f"[background agent error: {exc}]"
    # slot released by __aexit__ — always, before the following lines execute
    self._pending_bg_results.append((tc["id"], result))
    if self.tui_queue:
        await self.tui_queue.put({"type": "tool_done", ...})
```

#### `/clear` command handler

```python
# In handle_slash_command, /clear branch:
for task in list(session._bg_agent_tasks):
    task.cancel()
await _ism.force_release_all()
session._bg_agent_tasks.clear()
session._pending_bg_results.clear()
session._pending_bg_tool_calls.clear()
# existing message reset follows
```

#### `_flush_agent_batch` — background eligibility

```python
# Replace:
_bg_slots = await _query_live_slots()
_bg_eligible = _bg_slots >= 2 and ...

# With:
_bg_eligible = _ism.total_slots() >= 2 and ...
```

Remove `bg_slot_acquired` / `bg_slot_released` queue events from `_flush_agent_batch`
and `_run_background_agent`.

#### After `_switch_server` calls

```python
ok = await _switch_server(_target_model)
if ok:
    await _ism.refresh_from_server()   # updates total for new model's --parallel N
```

#### Remove

- `_query_live_slots()` module function
- All `_slots = await _query_live_slots()` call sites
- `session.server_parallel_slots` attribute (or keep as read-through to `_ism.total_slots()`)

### 4.2 `qt/adapter.py`

#### Register observer

In `_main()`, after `ChatSession.__aenter__` (which calls `initialize()`):

```python
from chat import _ism
_ism.on_change(lambda: self.slots_updated.emit(_ism.total_slots(), _ism.in_use()))
```

This is the ONLY place `slots_updated` is emitted. No more slot math anywhere in the adapter.

#### Remove from adapter

- `self._slots_total`, `self._slots_in_use` fields
- Slot math in `_run_turn` (`1 + _n_bg` calculation)
- `bg_slot_acquired`, `bg_slot_released`, `slots_total` event handlers in `_drain_queue`
- Manual slot adjustment in `done` event handler
- Slot math in `_drain_bg_agents`
- `_drain_bg_agents` finally block slot reset

#### Keep in `_drain_bg_agents`

Keep the loop that forwards `text_token`, `tool_done`, `system` events and emits
`bg_agents_complete`. Remove only the slot-counting lines.

---

## 5. Removal Checklist

### `chat.py`
- [ ] Remove `_query_live_slots()` function (~line 238)
- [ ] Remove `_bg_slots = await _query_live_slots()` in `_flush_agent_batch`
- [ ] Remove `_slots = await _query_live_slots()` after inline model switch in `_flush_agent_batch`
- [ ] Remove `bg_slot_acquired` tui_queue puts from `_flush_agent_batch`
- [ ] Remove `bg_slot_released` tui_queue put from `_run_background_agent`
- [ ] Add `_ism` singleton declaration
- [ ] Add `await _ism.initialize()` in `_detect_ctx_window`
- [ ] Add `async with await _ism.acquire("Eli")` in `send_and_stream`
- [ ] Add `_slot_handle` param + conditional acquire in `_tool_spawn_agent`
- [ ] Update `_run_background_agent` to use ISM acquire
- [ ] Add `force_release_all` + task cancel in `/clear` handler
- [ ] Add `_ism.refresh_from_server()` after `_switch_server` calls

### `qt/adapter.py`
- [ ] Remove `_slots_total`, `_slots_in_use` fields from `__init__`
- [ ] Remove slot math from `_run_turn`
- [ ] Remove `bg_slot_acquired`, `bg_slot_released`, `slots_total` handlers from `_drain_queue`
- [ ] Remove slot adjustment from `done` handler in `_drain_queue`
- [ ] Remove slot math from `_drain_bg_agents` (keep event forwarding)
- [ ] Remove slot reset from `_drain_bg_agents` finally block
- [ ] Add `_ism.on_change(...)` registration in `_main()`

---

## 6. Isolation Test Plan

**File**: `tests/test_slot_manager.py`
**Dependencies**: `pytest`, `pytest-asyncio` — no GUI, no llama.cpp server required.

All tests mock `GET /slots` via `httpx` mock or a lightweight fixture.

| # | Test | Assert |
|---|------|--------|
| TC-1 | `initialize()` sets total from /slots | `total == 2`, `in_use == 0` |
| TC-2 | `acquire()` returns handle with correct fields | `label`, `index`, `timeout_secs` correct |
| TC-3 | Context manager releases on clean exit | `in_use == 0` after block |
| TC-4 | Context manager releases on exception | `in_use == 0` after exception |
| TC-5 | Third acquire blocks when 2 slots full, unblocks on release | Timing assertion |
| TC-6 | Double-release is idempotent | No exception; `in_use` stays correct |
| TC-7 | `slot_snapshot()` returns both active handles | Labels present |
| TC-8 | `force_release_all()` clears all slots and cancels tasks | `in_use == 0`, tasks cancelled |
| TC-9 | `refresh_from_server()` increases total | New acquires unblock |
| TC-10 | `refresh_from_server()` downsizes total without evicting active holders | Existing slots survive |
| TC-11 | Observer fires on acquire and release | Callback count correct |
| TC-12 | `is_expired()` logic | True after timeout, False with `None` |
| TC-13 | Periodic refresh calls `refresh_from_server` | AsyncMock call count |
| TC-14 | Eviction cancels task and releases slot | Task cancelled; `in_use` decrements |
| TC-15 | Acquire before `initialize()` defaults to 1 slot | One acquire succeeds, second blocks |
| TC-16 | `force_release_all()` is idempotent (double-/clear) | No exception |

---

## 7. Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Model switch mid-background | Background agents keep their slots (already submitted HTTP). `refresh_from_server()` after switch corrects capacity for new acquires. |
| `/clear` mid-Eli-turn | `send_and_stream` is cancelled → `async with` releases Eli's slot. `force_release_all()` clears agents. |
| /slots endpoint down | `refresh_from_server()` swallows exception, keeps last-known total. Retries in 3 minutes. |
| Timeout eviction raises `CancelledError` inside `_tool_spawn_agent` | `_subagent_depth -= 1` in finally before slot release; then `CancelledError` propagates to `_run_background_agent` which stores error result. |
| Concurrent `force_release_all` (double-/clear) | Second call sees empty `_slots`, loops over nothing. All handles have `_released=True`, double-release guard prevents panic. |
| N waiters wake simultaneously after `force_release_all` | `asyncio.Condition.notify_all()` + predicate check ensures each waiter gets exactly one slot. No over-admission. |
| Inline agent (not a Task) — eviction must not apply | `SlotHandle.task = None` for inline agents; eviction loop skips handles with `task is None`. |

---

## 8. Implementation Order

1. **`slot_manager.py`** — implement `SlotManager` + `SlotHandle` + `_NullContext`
2. **`tests/test_slot_manager.py`** — implement TC-1 through TC-16
3. Run tests, iterate until all pass
4. **`chat.py`** — integration (remove old code, add ISM acquire/release)
5. **`qt/adapter.py`** — remove slot tracking, add observer
6. Full end-to-end GUI test against all verification scenarios

---

## 9. Verification (post-integration)

1. Single request: 1 red LED during Eli's turn → 0 after done
2. Two background agents: 1 red (Eli) + 2 red (agents) during turn; 2 red after Eli done; 0 after both agents done
3. `/clear` mid-agents: all LEDs go to 0 immediately
4. Model switch: slot count updates after switch completes
5. Agent timeout (force with 5s timeout in test): agent evicted, error result in next Eli turn
6. Agent exception: slot released; next acquire succeeds; no stuck LEDs
7. Server /slots endpoint offline: ISM holds last count; no crash
