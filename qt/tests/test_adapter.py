"""Tests for QtChatAdapter — the persistent-loop backend bridge."""
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

# Import QtChatAdapter at module level so sys.path manipulation runs once.
# patch("qt.adapter.ChatSession") patches the name in the already-imported module.
import qt.adapter  # noqa: F401 — ensure module is loaded before tests run
from qt.adapter import QtChatAdapter


def _mock_session(queue: asyncio.Queue | None = None):
    """Build a mock ChatSession that passes through __aenter__/__aexit__."""
    s = MagicMock()
    s.tui_queue = queue if queue is not None else asyncio.Queue()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    return s


# ── Startup synchronization ──────────────────────────────────────────────────

def test_submit_before_ready_does_not_crash(qapp):
    """submit() called immediately after start() must not raise AttributeError.
    threading.Event.wait() inside submit() blocks until the loop is ready."""
    with patch("qt.adapter.ChatSession") as MockSession:
        ms = _mock_session()
        ms.send_and_stream = AsyncMock()
        MockSession.return_value = ms

        adapter = QtChatAdapter()
        adapter.start()
        try:
            adapter.submit("hi", False)
        except AttributeError:
            pytest.fail("submit() raised AttributeError before loop was ready")
        adapter.shutdown()
        adapter.wait(3000)


# ── Signal emission ──────────────────────────────────────────────────────────

def test_text_token_signal_emitted(qapp, qtbot):
    """text_token signal fires for each text_token event on tui_queue."""
    q = asyncio.Queue()
    with patch("qt.adapter.ChatSession") as MockSession:
        ms = _mock_session(q)

        async def fake_stream(text, plan_mode):
            await q.put({"type": "text_token", "text": "hello"})
            await q.put({"type": "text_done", "text": "hello"})
            await q.put({"type": "done"})

        ms.send_and_stream = fake_stream
        MockSession.return_value = ms

        adapter = QtChatAdapter()
        adapter.start()

        received = []
        adapter.text_token.connect(lambda t: received.append(t))

        with qtbot.waitSignal(adapter.done, timeout=5000):
            adapter.submit("test", False)

        adapter.shutdown()
        adapter.wait(3000)

    assert received == ["hello"]


def test_done_signal_emitted_after_turn(qapp, qtbot):
    """done signal fires at the end of each turn."""
    q = asyncio.Queue()
    with patch("qt.adapter.ChatSession") as MockSession:
        ms = _mock_session(q)

        async def fake_stream(text, plan_mode):
            await q.put({"type": "text_done", "text": "reply"})
            await q.put({"type": "done"})

        ms.send_and_stream = fake_stream
        MockSession.return_value = ms

        adapter = QtChatAdapter()
        adapter.start()

        with qtbot.waitSignal(adapter.done, timeout=5000):
            adapter.submit("hello", False)

        adapter.shutdown()
        adapter.wait(3000)


# ── Multi-turn persistence ────────────────────────────────────────────────────

def test_multi_turn_loop_survives(qapp, qtbot):
    """Two sequential messages both complete — proves the loop persists across turns."""
    q = asyncio.Queue()
    with patch("qt.adapter.ChatSession") as MockSession:
        ms = _mock_session(q)
        call_count = {"n": 0}

        async def fake_stream(text, plan_mode):
            call_count["n"] += 1
            await q.put({"type": "text_done", "text": f"reply{call_count['n']}"})
            await q.put({"type": "done"})

        ms.send_and_stream = fake_stream
        MockSession.return_value = ms

        adapter = QtChatAdapter()
        adapter.start()

        with qtbot.waitSignal(adapter.done, timeout=5000):
            adapter.submit("first", False)

        with qtbot.waitSignal(adapter.done, timeout=5000):
            adapter.submit("second", False)

        adapter.shutdown()
        adapter.wait(3000)

    assert call_count["n"] == 2


# ── Approval flow ─────────────────────────────────────────────────────────────

def test_approval_future_resolved_by_resolve_approval(qapp, qtbot):
    """resolve_approval(True, '') unblocks the asyncio future in the worker loop."""
    q = asyncio.Queue()
    with patch("qt.adapter.ChatSession") as MockSession:
        ms = _mock_session(q)

        async def fake_stream(text, plan_mode):
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            await q.put({
                "type": "approval_request",
                "title": "Confirm",
                "message": "Run bash?",
                "style": "yellow",
                "future": future,
            })
            approved, notes = await future
            assert approved is True
            assert notes == ""
            await q.put({"type": "done"})

        ms.send_and_stream = fake_stream
        MockSession.return_value = ms

        adapter = QtChatAdapter()
        adapter.approval_needed.connect(lambda title, msg: adapter.resolve_approval(True, ""))
        adapter.start()

        with qtbot.waitSignal(adapter.done, timeout=5000):
            adapter.submit("run something", False)

        adapter.shutdown()
        adapter.wait(3000)


def test_window_imports_without_llm_worker(qapp):
    """window.py must not import llm_client (LLMWorker is retired)."""
    import ast, pathlib
    src = (pathlib.Path(__file__).parent.parent / "window.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            assert "llm_client" not in names and "llm_client" not in module, \
                "window.py still imports llm_client"
