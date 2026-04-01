"""
Isolation tests for SlotManager.

No GUI. No llama.cpp server. All HTTP is mocked.

Run:
    .venv\Scripts\pytest tests/test_slot_manager.py -v
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# Allow import from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from slot_manager import SlotManager, SlotHandle, _NullContext


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _mock_slots(n: int):
    """Return a mock httpx response for GET /slots with n slots."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = [{"id": i, "n_ctx": 8192} for i in range(n)]
    return mock_response


async def _init_ism(n: int, refresh_interval: float = 9999.0) -> SlotManager:
    """Create and initialize a SlotManager with n mocked slots."""
    ism = SlotManager(base_url="http://localhost:1234", refresh_interval=refresh_interval)
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_slots(n))
        mock_client_cls.return_value = mock_client
        await ism.initialize()
    return ism


# ── TC-1: initialize sets total from /slots ────────────────────────────────────

@pytest.mark.asyncio
async def test_tc1_initialize_sets_total():
    ism = await _init_ism(2)
    assert ism.total_slots() == 2
    assert ism.in_use() == 0
    assert ism.is_initialized()
    await ism.shutdown()


# ── TC-2: acquire returns handle with correct fields ───────────────────────────

@pytest.mark.asyncio
async def test_tc2_acquire_returns_correct_handle():
    ism = await _init_ism(2)
    handle = await ism.acquire("Eli", timeout_secs=None)
    try:
        assert handle.label == "Eli"
        assert handle.timeout_secs is None
        assert handle.index == 0
        assert handle.task is None
        assert not handle._released
        assert ism.in_use() == 1
    finally:
        await handle.release()
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-3: context manager releases on clean exit ──────────────────────────────

@pytest.mark.asyncio
async def test_tc3_context_manager_clean_exit():
    ism = await _init_ism(2)
    async with await ism.acquire("Eli") as handle:
        assert ism.in_use() == 1
        assert handle.label == "Eli"
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-4: context manager releases on exception ───────────────────────────────

@pytest.mark.asyncio
async def test_tc4_context_manager_releases_on_exception():
    ism = await _init_ism(2)
    try:
        async with await ism.acquire("Eli"):
            assert ism.in_use() == 1
            raise ValueError("boom")
    except ValueError:
        pass
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-5: third acquire blocks, unblocks on release ───────────────────────────

@pytest.mark.asyncio
async def test_tc5_third_acquire_blocks_then_unblocks():
    ism = await _init_ism(2)

    h1 = await ism.acquire("Eli")
    h2 = await ism.acquire("Agent 1")
    assert ism.in_use() == 2

    unblocked = asyncio.Event()

    async def waiter():
        async with await ism.acquire("Agent 2"):
            unblocked.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)    # give waiter a chance to start
    assert not unblocked.is_set(), "Agent 2 should be blocked while all slots full"

    await h1.release()           # free one slot
    await asyncio.sleep(0.01)    # let waiter run
    assert unblocked.is_set(), "Agent 2 should have unblocked after h1 release"

    await task
    await h2.release()
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-6: double-release is idempotent ────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc6_double_release_idempotent():
    ism = await _init_ism(2)
    handle = await ism.acquire("Eli")
    await handle.release()
    await handle.release()   # should not raise or decrement below 0
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-7: slot_snapshot returns active handles ────────────────────────────────

@pytest.mark.asyncio
async def test_tc7_slot_snapshot():
    ism = await _init_ism(2)
    h1 = await ism.acquire("Eli")
    h2 = await ism.acquire("Agent: researcher", timeout_secs=900.0)
    snap = ism.slot_snapshot()
    labels = {s["label"] for s in snap}
    assert labels == {"Eli", "Agent: researcher"}
    assert len(snap) == 2
    await h1.release()
    await h2.release()
    assert ism.slot_snapshot() == []
    await ism.shutdown()


# ── TC-8: force_release_all clears slots and cancels tasks ────────────────────

@pytest.mark.asyncio
async def test_tc8_force_release_all():
    ism = await _init_ism(2)

    # Create dummy tasks to reference from handles
    async def _dummy():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    t1 = asyncio.create_task(_dummy())
    t2 = asyncio.create_task(_dummy())

    h1 = await ism.acquire("Eli")
    h2 = await ism.acquire("Agent 1", timeout_secs=900.0)
    h1.task = t1
    h2.task = t2

    assert ism.in_use() == 2

    await ism.force_release_all()

    assert ism.in_use() == 0
    assert ism.slot_snapshot() == []

    await asyncio.sleep(0.01)   # let cancellations propagate
    assert t1.cancelled() or t1.done()
    assert t2.cancelled() or t2.done()

    await ism.shutdown()


# ── TC-9: refresh_from_server increases total ─────────────────────────────────

@pytest.mark.asyncio
async def test_tc9_refresh_increases_total():
    ism = await _init_ism(2)
    h1 = await ism.acquire("Eli")
    h2 = await ism.acquire("Agent 1")
    assert ism.in_use() == 2

    # Server now reports 4 slots
    unblocked = asyncio.Event()

    async def waiter():
        async with await ism.acquire("Agent 2"):
            unblocked.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert not unblocked.is_set()   # still blocked at 2/2

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_slots(4))
        mock_cls.return_value = mock_client
        await ism.refresh_from_server()

    assert ism.total_slots() == 4
    await asyncio.sleep(0.01)
    assert unblocked.is_set()   # waiter should have unblocked

    await task
    await h1.release()
    await h2.release()
    await ism.shutdown()


# ── TC-10: refresh downsizes — does not evict active holders ──────────────────

@pytest.mark.asyncio
async def test_tc10_refresh_downsizes_no_eviction():
    ism = await _init_ism(4)
    h1 = await ism.acquire("Eli")
    h2 = await ism.acquire("Agent 1")
    assert ism.in_use() == 2

    # Server drops to 2 slots
    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_slots(2))
        mock_cls.return_value = mock_client
        await ism.refresh_from_server()

    assert ism.total_slots() == 2
    assert ism.in_use() == 2   # existing holders survive

    # Third acquire should block (at capacity)
    blocked = True

    async def try_acquire():
        nonlocal blocked
        async with await ism.acquire("Agent 2"):
            blocked = False

    task = asyncio.create_task(try_acquire())
    await asyncio.sleep(0.02)
    assert blocked, "Agent 2 should remain blocked (2/2 slots used)"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    await h1.release()
    await h2.release()
    await ism.shutdown()


# ── TC-11: observer fires on acquire and release ──────────────────────────────

@pytest.mark.asyncio
async def test_tc11_observer_fires():
    ism = await _init_ism(2)
    calls = []
    ism.on_change(lambda: calls.append(1))

    h = await ism.acquire("Eli")
    assert len(calls) == 1, "Observer should fire on acquire"

    await h.release()
    assert len(calls) == 2, "Observer should fire on release"

    await ism.shutdown()


# ── TC-12: is_expired logic ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc12_is_expired():
    ism = await _init_ism(2)

    # Handle with tiny timeout — will expire immediately
    h_timeout = await ism.acquire("Agent", timeout_secs=0.001)
    await asyncio.sleep(0.01)
    assert h_timeout.is_expired()

    # Handle with no timeout — never expires
    h_eli = await ism.acquire("Eli", timeout_secs=None)
    assert not h_eli.is_expired()
    await asyncio.sleep(0.01)
    assert not h_eli.is_expired()

    await h_timeout.release()
    await h_eli.release()
    await ism.shutdown()


# ── TC-13: periodic refresh is called ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_tc13_periodic_refresh_called():
    ism = SlotManager(base_url="http://localhost:1234", refresh_interval=0.05)
    ism.refresh_from_server = AsyncMock()
    ism._evict_expired = AsyncMock()

    # Start just the periodic task (bypass initialize's HTTP call)
    ism._total = 2
    ism._initialized = True
    ism._refresh_task = asyncio.create_task(ism._periodic_refresh())

    await asyncio.sleep(0.18)
    count = ism.refresh_from_server.await_count
    assert count >= 2, f"Expected >=2 refresh calls, got {count}"

    await ism.shutdown()


# ── TC-14: eviction cancels task and releases slot ────────────────────────────

@pytest.mark.asyncio
async def test_tc14_eviction_cancels_task():
    ism = await _init_ism(2)

    async def long_agent():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    agent_task = asyncio.create_task(long_agent())
    h = await ism.acquire("Agent", timeout_secs=0.01)   # 10ms timeout
    h.task = agent_task

    await asyncio.sleep(0.05)   # let it expire
    await ism._evict_expired()

    assert ism.in_use() == 0
    await asyncio.sleep(0.01)
    assert agent_task.cancelled() or agent_task.done()

    await ism.shutdown()


# ── TC-15: acquire before initialize defaults to 1 slot ───────────────────────

@pytest.mark.asyncio
async def test_tc15_acquire_before_initialize():
    ism = SlotManager(base_url="http://localhost:1234")
    # _total defaults to 1, so first acquire should succeed immediately
    h = await ism.acquire("Eli")
    assert ism.in_use() == 1
    await h.release()
    assert ism.in_use() == 0


# ── TC-16: force_release_all is idempotent ────────────────────────────────────

@pytest.mark.asyncio
async def test_tc16_force_release_all_idempotent():
    ism = await _init_ism(2)
    await ism.acquire("Eli")
    await ism.force_release_all()
    await ism.force_release_all()   # should not raise
    assert ism.in_use() == 0
    await ism.shutdown()


# ── TC-17: _NullContext passes through the handle ─────────────────────────────

@pytest.mark.asyncio
async def test_tc17_null_context():
    ism = await _init_ism(2)
    outer = await ism.acquire("Agent 1", timeout_secs=900.0)

    async with _NullContext(outer) as inner:
        assert inner is outer
        assert ism.in_use() == 1   # _NullContext did NOT re-acquire

    assert ism.in_use() == 1   # _NullContext did NOT release

    await outer.release()
    assert ism.in_use() == 0
    await ism.shutdown()
