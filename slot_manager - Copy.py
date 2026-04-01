"""
slot_manager.py — Inference Slot Manager (ISM)

Single source of truth for inference slot occupancy. Tracks who holds each slot,
enforces 15-minute agent timeouts, and provides force-release for /clear.

Usage:
    from slot_manager import SlotManager, SlotHandle

    _ism = SlotManager(base_url="http://localhost:1234")

    # In async context:
    await _ism.initialize()

    async with await _ism.acquire("Eli") as slot:
        # inference runs here
        ...   # slot released in __aexit__ even on exception/cancellation
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

log = logging.getLogger(__name__)


# ── SlotHandle ─────────────────────────────────────────────────────────────────

@dataclass
class SlotHandle:
    """Represents a single acquired inference slot.

    Use as an async context manager for automatic release:

        async with await ism.acquire("Eli") as slot:
            ...

    Or hold the handle and call release() manually.
    """
    index:        int
    label:        str
    acquired_at:  float                   # time.monotonic()
    timeout_secs: float | None            # None = no timeout (Eli); 900 = agents
    _manager:     "SlotManager"
    _released:    bool = field(default=False, init=False)
    task:         asyncio.Task | None = field(default=None, init=False)

    # ── Context manager ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "SlotHandle":
        return self

    async def __aexit__(self, *_) -> None:
        await self.release()

    # ── Release ───────────────────────────────────────────────────────────────

    async def release(self) -> None:
        """Release this slot. Idempotent — safe to call multiple times."""
        if self._released:
            return
        self._released = True
        await self._manager._do_release(self)

    # ── Timeout check ─────────────────────────────────────────────────────────

    def is_expired(self) -> bool:
        """True if the slot has exceeded its timeout."""
        if self.timeout_secs is None:
            return False
        return (time.monotonic() - self.acquired_at) > self.timeout_secs

    def age_secs(self) -> float:
        return time.monotonic() - self.acquired_at


# ── _NullContext ───────────────────────────────────────────────────────────────

class _NullContext:
    """Async context manager that does nothing.

    Used when a background agent passes its already-acquired SlotHandle into
    _tool_spawn_agent — the inner call skips its own acquire.
    """
    def __init__(self, handle: SlotHandle):
        self._handle = handle

    async def __aenter__(self) -> SlotHandle:
        return self._handle

    async def __aexit__(self, *_) -> None:
        pass   # releasing is the caller's responsibility


# ── SlotManager ───────────────────────────────────────────────────────────────

class SlotManager:
    """
    Asyncio-safe inference slot manager.

    Thread-safety: all public coroutines must be called from the same asyncio
    event loop. Observer callbacks are called synchronously from within that loop.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:1234",
        refresh_interval: float = 180.0,   # seconds between periodic polls
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._refresh_interval = refresh_interval

        # Slot state — protected by _condition
        self._total:  int = 1              # safe default before initialize()
        self._slots:  dict[int, SlotHandle] = {}   # index → handle

        # Synchronisation
        self._lock      = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

        # Background refresh task
        self._refresh_task: asyncio.Task | None = None

        # Observers (UI callbacks)
        self._change_callbacks: list[Callable[[], None]] = []

        # Raw server slot data (list of dicts) — kept for n_ctx extraction
        self._raw_slots: list[dict] = []

        self._initialized = False

    # ── Initialization ─────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Query /slots and start the periodic refresh task.

        Safe to call multiple times — subsequent calls refresh the count and
        restart the refresh task if it died.
        """
        await self.refresh_from_server()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._periodic_refresh(), name="slot_manager_refresh"
            )
        self._initialized = True

    # ── Server query ───────────────────────────────────────────────────────────

    async def refresh_from_server(self) -> None:
        """GET /slots, update _total, prune capacity if it shrank.

        Non-raising: on failure keeps last-known total.
        """
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self._base_url}/slots", timeout=5.0)
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list) and data:
                    self._raw_slots = data
                    new_total = len(data)
                    async with self._condition:
                        self._total = new_total
                        # Wake waiters — capacity may have increased
                        self._condition.notify_all()
                    self._notify_observers()
                    log.debug("SlotManager: server reports %d slot(s)", new_total)
        except Exception as exc:
            log.warning("SlotManager: /slots query failed (%s), keeping total=%d", exc, self._total)

    async def _periodic_refresh(self) -> None:
        """Background task: poll /slots every refresh_interval seconds."""
        try:
            while True:
                await asyncio.sleep(self._refresh_interval)
                await self.refresh_from_server()
                await self._evict_expired()
        except asyncio.CancelledError:
            pass

    # ── Acquire ───────────────────────────────────────────────────────────────

    async def acquire(
        self,
        label: str,
        timeout_secs: float | None = None,
    ) -> SlotHandle:
        """Block until a free slot is available, then return a SlotHandle.

        The handle MUST be used as an async context manager or release() called
        manually. Failing to release leaks the slot permanently.

        Args:
            label:        Human-readable owner label ("Eli", "Agent: researcher").
            timeout_secs: Agent eviction timeout. None = no timeout (use for Eli).
        """
        async with self._condition:
            # Wait until at least one slot is free
            await self._condition.wait_for(
                lambda: len(self._slots) < self._total
            )
            index = self._next_free_index()
            handle = SlotHandle(
                index=index,
                label=label,
                acquired_at=time.monotonic(),
                timeout_secs=timeout_secs,
                _manager=self,
            )
            self._slots[index] = handle
            log.debug(
                "SlotManager: acquired slot %d for '%s' (%d/%d in use)",
                index, label, len(self._slots), self._total,
            )
        self._notify_observers()
        return handle

    def _next_free_index(self) -> int:
        """Return the lowest index not currently occupied. Caller holds lock."""
        used = set(self._slots.keys())
        i = 0
        while i in used:
            i += 1
        return i

    # ── Release ───────────────────────────────────────────────────────────────

    async def _do_release(self, handle: SlotHandle) -> None:
        """Internal release called by SlotHandle.release()."""
        async with self._condition:
            removed = self._slots.pop(handle.index, None)
            if removed is not None:
                log.debug(
                    "SlotManager: released slot %d ('%s', held %.1fs) — %d/%d in use",
                    handle.index, handle.label, handle.age_secs(),
                    len(self._slots), self._total,
                )
            self._condition.notify_all()
        if removed is not None:
            self._notify_observers()

    # ── Eviction ───────────────────────────────────────────────────────────────

    async def _evict_expired(self) -> None:
        """Cancel and release all handles that have exceeded their timeout."""
        expired: list[SlotHandle] = []
        async with self._condition:
            for handle in list(self._slots.values()):
                if handle.is_expired():
                    handle._released = True
                    self._slots.pop(handle.index, None)
                    expired.append(handle)
            if expired:
                self._condition.notify_all()

        for handle in expired:
            log.info(
                "SlotManager: evicting '%s' (slot %d, held %.0fs > %.0fs timeout)",
                handle.label, handle.index, handle.age_secs(), handle.timeout_secs,
            )
            if handle.task and not handle.task.done():
                handle.task.cancel()

        if expired:
            self._notify_observers()

    # ── Force-release all ─────────────────────────────────────────────────────

    async def force_release_all(self) -> None:
        """Release all slots and cancel all associated tasks.

        Called by /clear. Idempotent.
        """
        all_handles: list[SlotHandle] = []
        async with self._condition:
            for handle in list(self._slots.values()):
                handle._released = True
                all_handles.append(handle)
            self._slots.clear()
            self._condition.notify_all()

        for handle in all_handles:
            if handle.task and not handle.task.done():
                handle.task.cancel()

        if all_handles:
            log.info("SlotManager: force-released %d slot(s)", len(all_handles))
            self._notify_observers()

    # ── Observers ─────────────────────────────────────────────────────────────

    def on_change(self, callback: Callable[[], None]) -> None:
        """Register a zero-argument observer, called on any slot state change."""
        self._change_callbacks.append(callback)

    def _notify_observers(self) -> None:
        for cb in self._change_callbacks:
            try:
                cb()
            except Exception as exc:
                log.warning("SlotManager observer raised: %s", exc)

    # ── Shutdown ───────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Cancel the periodic refresh task. Does not release active slots."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None

    # ── Read-only accessors ────────────────────────────────────────────────────

    def total_slots(self) -> int:
        return self._total

    def in_use(self) -> int:
        return len(self._slots)

    def is_initialized(self) -> bool:
        return self._initialized

    def slot_snapshot(self) -> list[dict]:
        """Thread-safe snapshot of current slot occupancy for UI display."""
        return [
            {
                "index":      h.index,
                "label":      h.label,
                "age_secs":   round(h.age_secs(), 1),
                "timeout_secs": h.timeout_secs,
                "expired":    h.is_expired(),
            }
            for h in self._slots.values()
        ]

    def __repr__(self) -> str:
        return (
            f"<SlotManager total={self._total} in_use={self.in_use()} "
            f"initialized={self._initialized}>"
        )
