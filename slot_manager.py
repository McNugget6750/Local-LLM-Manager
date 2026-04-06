import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

log = logging.getLogger(__name__)


@dataclass
class SlotHandle:
    index: int
    label: str
    acquired_at: float
    timeout_secs: float | None
    _manager: "SlotManager"
    _released: bool = field(default=False, init=False)
    task: asyncio.Task | None = field(default=None, init=False)

    async def __aenter__(self) -> "SlotHandle":
        return self

    async def __aexit__(self, *_) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        log.debug(
            "SlotHandle.release(): label=%r index=%d who=code (normal)",
            self.label,
            self.index,
        )
        await self._manager._do_release(self)

    def is_expired(self) -> bool:
        if self.timeout_secs is None:
            return False
        return (time.monotonic() - self.acquired_at) > self.timeout_secs

    def age_secs(self) -> float:
        return time.monotonic() - self.acquired_at


class _NullContext:
    def __init__(self, handle: SlotHandle):
        self._handle = handle

    async def __aenter__(self) -> SlotHandle:
        return self._handle

    async def __aexit__(self, *_) -> None:
        pass


class SlotManager:
    def __init__(
        self,
        base_url: str = "http://localhost:1234",
        refresh_interval: float = 180.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._refresh_interval = refresh_interval

        self._total: int = 1
        self._slots: dict[int, SlotHandle] = {}

        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

        self._refresh_task: asyncio.Task | None = None
        self._change_callbacks: list[Callable[[], None]] = []

        self._raw_slots: list[dict] = []
        self._initialized = False
        self._shutdown = False

    async def initialize(self) -> None:
        if self._shutdown:
            log.debug("SlotManager: initialize() skipped — already shut down")
            return

        await self.refresh_from_server()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(
                self._periodic_refresh(), name="slot_manager_refresh"
            )
            log.debug("SlotManager: started periodic refresh task")

        self._initialized = True
        log.debug("SlotManager: initialized, total=%d", self._total)

    async def refresh_from_server(self) -> None:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self._base_url}/slots", timeout=5.0)
                r.raise_for_status()
                data = r.json()

            if isinstance(data, list):
                async with self._condition:
                    self._raw_slots = data
                    self._total = len(data)
                    self._condition.notify_all()

                self._notify_observers()
                log.debug("SlotManager: server reports %d slot(s)", self._total)

        except Exception as exc:
            log.warning(
                "SlotManager: /slots query failed (%s), keeping total=%d",
                exc,
                self._total,
            )

    async def _periodic_refresh(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._refresh_interval)
                await self.refresh_from_server()
                await self._evict_expired()
        except asyncio.CancelledError:
            log.debug("SlotManager._periodic_refresh(): task cancelled")

    async def acquire(
        self,
        label: str,
        timeout_secs: float | None = None,
        preempt_agents: bool = False,
        bypass_capacity: bool = False,
    ) -> SlotHandle:
        """Acquire an inference slot.

        bypass_capacity: skip the capacity wait entirely.  Use for Eli so it
        can always respond even when background agents hold all named slots.
        The LLM server serialises concurrent HTTP requests internally, so Eli
        will at most wait behind the agent's *current* generation step rather
        than its entire run.
        """
        if self._shutdown:
            raise RuntimeError("SlotManager has been shut down")

        preempted_handles: list[SlotHandle] = []

        async with self._condition:
            if preempt_agents and len(self._slots) >= self._total:
                agent_indices = [
                    idx for idx, h in self._slots.items()
                    if h.label.startswith("Agent")
                ]

                if agent_indices:
                    log.info(
                        "SlotManager: preempting %d agent slot(s) for '%s'",
                        len(agent_indices),
                        label,
                    )

                    for idx in agent_indices:
                        handle = self._slots.get(idx)
                        if handle is None:
                            continue

                        if handle.task:
                            handle.task.cancel()

                        preempted_handles.append(handle)

                    for handle in preempted_handles:
                        self._slots.pop(handle.index, None)
                        handle._released = True

                    self._condition.notify_all()

            if bypass_capacity:
                if len(self._slots) >= self._total:
                    log.debug(
                        "SlotManager: '%s' bypassing capacity (%d/%d in use)",
                        label, len(self._slots), self._total,
                    )
            else:
                await self._condition.wait_for(lambda: len(self._slots) < self._total)

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
                "SlotManager: ACQUIRED slot %d for '%s' (timeout=%s) — %d/%d in use",
                index,
                label,
                "no" if timeout_secs is None else f"{timeout_secs:.0f}s",
                len(self._slots),
                self._total,
            )

            self._condition.notify_all()

        # Notify observers and await cancelled tasks outside the lock.
        if preempted_handles:
            self._notify_observers()
        for h in preempted_handles:
            if h.task is not None:
                try:
                    await h.task
                except asyncio.CancelledError:
                    pass

        self._notify_observers()
        return handle

    def _next_free_index(self) -> int:
        used = set(self._slots.keys())
        i = 0
        while i in used:
            i += 1
        return i

    async def _do_release(self, handle: SlotHandle) -> None:
        if handle._released:
            return

        async with self._condition:
            removed = self._slots.pop(handle.index, None)
            if removed is None:
                return

            handle._released = True
            self._condition.notify_all()

        log.debug(
            "SlotManager: RELEASED slot %d '%s'",
            handle.index,
            handle.label,
        )
        self._notify_observers()

    async def _safe_cancel_task(self, task: asyncio.Task) -> None:
        if task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _evict_expired(self) -> None:
        expired: list[SlotHandle] = []
        async with self._condition:
            for handle in list(self._slots.values()):
                if handle.is_expired():
                    if handle.task is None or handle.task.done():
                        expired.append(handle)
                    else:
                        log.debug(
                            "SlotManager: slot %d '%s' expired but task still running, skipping",
                            handle.index, handle.label,
                        )

        for handle in expired:
            if handle.task is not None:
                await self._safe_cancel_task(handle.task)
            await self._do_release(handle)

    async def force_release_all(self) -> None:
        all_handles: list[SlotHandle] = []
        async with self._condition:
            for handle in list(self._slots.values()):
                if handle.task is not None:
                    handle.task.cancel()
                handle._released = True
                all_handles.append(handle)
            self._slots.clear()
            self._condition.notify_all()

        for handle in all_handles:
            if handle.task is not None:
                await self._safe_cancel_task(handle.task)

        self._notify_observers()

    def on_change(self, callback: Callable[[], None]) -> None:
        self._change_callbacks.append(callback)

    def _notify_observers(self) -> None:
        for cb in self._change_callbacks:
            try:
                cb()
            except Exception as exc:
                log.warning("SlotManager observer raised: %s", exc)

    async def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True

        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None

    def total_slots(self) -> int:
        return self._total

    def in_use(self) -> int:
        return len(self._slots)

    def is_initialized(self) -> bool:
        return self._initialized

    def slot_snapshot(self) -> list[dict]:
        return [
            {
                "index": h.index,
                "label": h.label,
                "age_secs": round(h.age_secs(), 1),
                "timeout_secs": h.timeout_secs,
                "expired": h.is_expired(),
            }
            for h in list(self._slots.values())
        ]

    def __repr__(self) -> str:
        return (
            f"<SlotManager total={self._total} in_use={self.in_use()} "
            f"initialized={self._initialized}>"
        )