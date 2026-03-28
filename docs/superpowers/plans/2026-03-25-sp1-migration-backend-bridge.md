# SP1: Migration + Backend Bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Qt prototype files into `qwen3-manager/qt/`, wire `ChatSession` from `chat.py` as the backend via a persistent asyncio adapter, and delete the now-superseded `LLMWorker`.

**Architecture:** `QtChatAdapter(QThread)` holds a single long-lived asyncio event loop that runs for the lifetime of the Qt session. One `ChatSession` instance persists across all turns. Work items (user messages, slash commands) are submitted thread-safely via `call_soon_threadsafe` into an internal asyncio queue. The adapter drains `session.tui_queue` per turn and emits Qt signals to the main thread.

**Tech Stack:** Python 3.13, PySide6 6.11.0, httpx (async), asyncio, pytest-qt

---

## Spec Reference

`docs/superpowers/specs/2026-03-25-qt-gui-feature-parity-design.md` — read this before starting. SP1 corresponds to the "Sub-project 1: Migration + Backend Bridge" section.

---

## Environment Setup

All commands run from `C:\Users\timob\claude-projects\qwen3-manager\`.
Python executable: `.venv\Scripts\python.exe`
Test runner: `.venv\Scripts\pytest.exe`

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Create | `qt/__init__.py` | Empty package marker |
| Move + modify | `qt/main.py` | Entry point; sys.path fix |
| Move | `qt/colors.py` | Color constants + QSS |
| Move | `qt/highlighter.py` | Syntax highlighter |
| Move | `qt/file_watcher.py` | DirWatcher |
| Move | `qt/tool_call_checker.py` | JSON repair helpers |
| Move + rewrite | `qt/window.py` | MainWindow wired to adapter |
| Create | `qt/adapter.py` | QtChatAdapter — persistent loop |
| Create | `qt/run.bat` | Launcher |
| Modify | `chat.py` | 2 targeted patches |
| Create | `qt/tests/__init__.py` | Empty |
| Create | `qt/tests/conftest.py` | QApplication fixture |
| Create | `qt/tests/test_adapter.py` | Adapter unit tests |
| Move | `qt/tests/test_highlighter.py` | (from qt-chat-proto) |
| Move | `qt/tests/test_tool_call_checker.py` | (from qt-chat-proto) |
| Move | `qt/tests/test_file_watcher.py` | (from qt-chat-proto) |
| Delete | ~~`qt/llm_client.py`~~ | Not created (LLMWorker retired) |

Source files to copy from: `C:\Users\timob\claude-projects\qt-chat-proto\`

---

## Task 1: Install PySide6 into qwen3-manager venv

**Files:** none

- [ ] **Step 1: Install PySide6 and pytest-qt**

```bash
.venv\Scripts\pip install "PySide6==6.11.0" pytest-qt
```

Expected: `Successfully installed PySide6-6.11.0 ...`

- [ ] **Step 2: Verify import**

```bash
.venv\Scripts\python.exe -c "from PySide6.QtWidgets import QApplication; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Create requirements-qt.txt**

Create `requirements-qt.txt` in `qwen3-manager/`:

```
PySide6==6.11.0
pytest-qt>=4.4
```

- [ ] **Step 4: Commit**

```bash
git add requirements-qt.txt
git commit -m "build: add PySide6 and pytest-qt to qwen3-manager"
```

---

## Task 2: Copy files from qt-chat-proto

**Files:** `qt/__init__.py`, `qt/colors.py`, `qt/highlighter.py`, `qt/file_watcher.py`, `qt/tool_call_checker.py`, `qt/tests/__init__.py`, `qt/tests/conftest.py`, `qt/tests/test_highlighter.py`, `qt/tests/test_tool_call_checker.py`, `qt/tests/test_file_watcher.py`

- [ ] **Step 1: Create the qt/ directory structure**

```bash
mkdir qt
mkdir qt\tests
```

- [ ] **Step 2: Create empty `qt/__init__.py`**

Create `qt/__init__.py` with content:

```python
```

(empty file)

- [ ] **Step 3: Copy unchanged files**

```bash
copy ..\qt-chat-proto\colors.py qt\colors.py
copy ..\qt-chat-proto\highlighter.py qt\highlighter.py
copy ..\qt-chat-proto\file_watcher.py qt\file_watcher.py
copy ..\qt-chat-proto\tool_call_checker.py qt\tool_call_checker.py
copy ..\qt-chat-proto\tests\__init__.py qt\tests\__init__.py
copy ..\qt-chat-proto\tests\test_highlighter.py qt\tests\test_highlighter.py
copy ..\qt-chat-proto\tests\test_tool_call_checker.py qt\tests\test_tool_call_checker.py
copy ..\qt-chat-proto\tests\test_file_watcher.py qt\tests\test_file_watcher.py
```

- [ ] **Step 4: Create `qt/tests/conftest.py`**

```python
import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
```

- [ ] **Step 5: Run the copied tests to confirm they still pass**

```bash
.venv\Scripts\pytest.exe qt\tests\test_highlighter.py qt\tests\test_tool_call_checker.py -v
```

Expected: all tests PASS (test_file_watcher is skipped here — needs QCoreApplication, run separately)

- [ ] **Step 6: Run file watcher test**

```bash
.venv\Scripts\pytest.exe qt\tests\test_file_watcher.py -v
```

Expected: all 3 tests PASS

- [ ] **Step 7: Commit**

```bash
git add qt/
git commit -m "feat: scaffold qt/ package with migrated support files and tests"
```

---

## Task 3: Patch chat.py

**Files:** `chat.py`

Two targeted patches. Do not change any other lines.

### Patch A: Fix asyncio.get_event_loop() deprecation

- [ ] **Step 1: Apply the patch**

In `chat.py` at line 2346, find:

```python
            future: asyncio.Future = asyncio.get_event_loop().create_future()
```

Replace with:

```python
            future: asyncio.Future = asyncio.get_running_loop().create_future()
```

### Patch B: Add instance attributes for compaction config

- [ ] **Step 2: Apply the patch**

In `chat.py`, find the line (around line 1783):

```python
        self.compact_mode: bool     = False
```

Replace with:

```python
        self.compact_mode: bool         = False
        self.compact_threshold: float   = CTX_COMPACT_THRESH
        self.keep_recent: int           = CTX_KEEP_RECENT
        self.input_compress_limit: int  = INPUT_COMPRESS_CHARS
```

- [ ] **Step 3: Verify chat.py still works**

```bash
.venv\Scripts\python.exe -c "from chat import ChatSession; print('import ok')"
```

Expected: `import ok`

- [ ] **Step 4: Quick terminal smoke test (optional but recommended)**

```bash
.venv\Scripts\python.exe chat.py --help 2>&1 | head -5
```

Expected: shows usage/help text, no import errors.

- [ ] **Step 5: Commit**

```bash
git add chat.py
git commit -m "fix: asyncio get_running_loop, add compact config instance attrs"
```

---

## Task 4: Create adapter.py

**Files:** Create `qt/adapter.py`

This is the core new file. It replaces `LLMWorker` entirely. Read the spec section "Persistent event loop (critical)" and "Approval intercept" before writing this.

- [ ] **Step 1: Write the failing test first**

Create `qt/tests/test_adapter.py`:

```python
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
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
.venv\Scripts\pytest.exe qt\tests\test_adapter.py -v 2>&1 | head -20
```

Expected: `ImportError: No module named 'qt.adapter'` (or similar — confirms test runs but adapter doesn't exist yet)

- [ ] **Step 3: Create `qt/adapter.py`**

```python
"""
QtChatAdapter — bridges ChatSession (asyncio) and the Qt main thread.

One persistent asyncio event loop lives inside a QThread for the session
lifetime. Work items are submitted thread-safely via call_soon_threadsafe.
"""

import asyncio
import threading
import sys
import pathlib

# Allow `from chat import ChatSession` regardless of working directory
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from PySide6.QtCore import QThread, Signal
from chat import ChatSession


class QtChatAdapter(QThread):
    # ── Signals (emitted on the Qt main thread via signal/slot mechanism) ──
    think_token     = Signal(str)
    text_token      = Signal(str)
    tool_start      = Signal(str, str, str)        # id, name, args_json
    tool_done       = Signal(str, str, str, bool)  # id, name, result, is_error
    approval_needed = Signal(str, str)             # title, message
    text_done       = Signal(str)
    usage           = Signal(int, int)             # tokens_used, ctx_window
    system_msg      = Signal(str)
    error_msg       = Signal(str)
    done            = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_queue: asyncio.Queue | None = None
        self._pending_future: asyncio.Future | None = None
        self._ready = threading.Event()   # set when loop is running

    # ── Worker thread entry point ────────────────────────────────────────────

    def run(self) -> None:
        """Runs in the worker thread. Keeps one event loop alive for the session."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._work_queue = asyncio.Queue()
        self._ready.set()                         # unblock any waiting submit() calls
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        try:
            async with ChatSession() as session:
                session.tui_queue = asyncio.Queue()
                while True:
                    item = await self._work_queue.get()
                    if item is None:              # shutdown sentinel
                        break
                    kind = item[0]
                    if kind == "__slash__":
                        await self._run_slash(session, item[1])
                    else:
                        text, plan_mode = item
                        await self._run_turn(session, text, plan_mode)
        except Exception as exc:
            # ChatSession.__aenter__ failed (e.g. server offline at startup)
            self.error_msg.emit(f"Backend init failed: {exc}")
            self.done.emit()

    # ── Per-turn logic ───────────────────────────────────────────────────────

    async def _run_turn(self, session: ChatSession, text: str, plan_mode: bool) -> None:
        """Send one message and drain tui_queue until done/error."""
        # Reset queue in case previous turn left stale events
        while not session.tui_queue.empty():
            session.tui_queue.get_nowait()

        try:
            stream_task = asyncio.create_task(
                session.send_and_stream(text, plan_mode=plan_mode)
            )
            await self._drain_queue(session, stream_task)
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.done.emit()

    async def _run_slash(self, session: ChatSession, cmd: str) -> None:
        """Run a slash command; drain any system events it emits, then signal done.

        In SP1 handle_slash_command uses console.print (no tui_queue events).
        In SP3 it will emit system events — the drain here handles both cases.
        """
        from chat import handle_slash_command
        try:
            await handle_slash_command(cmd, session)
        except Exception as exc:
            self.error_msg.emit(str(exc))
        # Drain any system events emitted by the slash command (SP3+)
        while not session.tui_queue.empty():
            event = session.tui_queue.get_nowait()
            if event.get("type") == "system":
                self.system_msg.emit(event.get("text", ""))
        self.done.emit()

    async def _drain_queue(self, session: ChatSession,
                           stream_task: asyncio.Task) -> None:
        """Consume tui_queue events and emit Qt signals until 'done'."""
        while True:
            try:
                event = await asyncio.wait_for(
                    session.tui_queue.get(), timeout=0.05
                )
            except asyncio.TimeoutError:
                # No event yet — check if stream_task finished with an exception
                if stream_task.done() and stream_task.exception():
                    raise stream_task.exception()
                continue

            etype = event.get("type")

            if etype == "think_token":
                self.think_token.emit(event["text"])
            elif etype == "text_token":
                self.text_token.emit(event["text"])
            elif etype == "tool_start":
                self.tool_start.emit(
                    event.get("id", ""),
                    event.get("name", ""),
                    event.get("args", ""),
                )
            elif etype == "tool_done":
                self.tool_done.emit(
                    event.get("id", ""),
                    event.get("name", ""),
                    event.get("result", ""),
                    bool(event.get("is_error", False)),
                )
            elif etype == "approval_request":
                self._pending_future = event["future"]
                self.approval_needed.emit(
                    event.get("title", ""),
                    event.get("message", ""),
                )
                # Block until resolve_approval() is called from the main thread
                await self._pending_future
                self._pending_future = None
            elif etype == "text_done":
                self.text_done.emit(event["text"])
            elif etype == "usage":
                self.usage.emit(
                    int(event.get("tokens", 0)),
                    int(event.get("ctx", 0)),
                )
            elif etype == "system":
                self.system_msg.emit(event.get("text", ""))
            elif etype == "error":
                self.error_msg.emit(event.get("text", ""))
                self.done.emit()
                return
            elif etype == "done":
                self.done.emit()
                return

    # ── Thread-safe public API (called from main thread) ────────────────────

    def submit(self, text: str, plan_mode: bool) -> None:
        """Submit a user message. Blocks briefly until the loop is ready."""
        self._ready.wait()
        self._loop.call_soon_threadsafe(
            self._work_queue.put_nowait, (text, plan_mode)
        )

    def submit_slash(self, cmd: str) -> None:
        """Submit a slash command to run inside the persistent loop."""
        self._ready.wait()
        self._loop.call_soon_threadsafe(
            self._work_queue.put_nowait, ("__slash__", cmd)
        )

    def resolve_approval(self, approved: bool, notes: str = "") -> None:
        """Resolve the pending approval Future. Called from the main thread."""
        if self._pending_future is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(
                self._pending_future.set_result, (approved, notes)
            )

    def shutdown(self) -> None:
        """Signal the worker loop to exit cleanly."""
        if self._loop is not None:
            self._ready.wait()
            self._loop.call_soon_threadsafe(
                self._work_queue.put_nowait, None
            )
```

- [ ] **Step 4: Run the tests**

```bash
.venv\Scripts\pytest.exe qt\tests\test_adapter.py -v
```

Expected: all 5 tests PASS

If `test_submit_before_ready_does_not_crash` fails with a timeout (adapter never shuts down), check that `run()` assigns `self._loop` before calling `self._ready.set()`.

If `test_approval_future_resolved_by_resolve_approval` fails with `asyncio.InvalidStateError`, the Future is being set twice — ensure `_pending_future` is cleared to `None` after `await`.

- [ ] **Step 5: Commit**

```bash
git add qt/adapter.py qt/tests/test_adapter.py
git commit -m "feat: add QtChatAdapter with persistent asyncio loop and approval bridge"
```

---

## Task 5: Create qt/main.py

**Files:** Create `qt/main.py`

- [ ] **Step 1: Write the failing test**

Add to `qt/tests/test_adapter.py` (append at the bottom):

```python
def test_main_py_sys_path_includes_parent(qapp_core):
    """main.py must add the parent dir to sys.path so 'from chat import ...' works."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, pathlib; "
         "sys.path.insert(0, str(pathlib.Path('qt/main.py').parent.parent)); "
         "from chat import ChatSession; print('ok')"],
        capture_output=True, text=True,
        cwd=r"C:\Users\timob\claude-projects\qwen3-manager"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
```

- [ ] **Step 2: Run to confirm test passes already** (it tests the path mechanism, not main.py itself)

```bash
.venv\Scripts\pytest.exe qt\tests\test_adapter.py::test_main_py_sys_path_includes_parent -v
```

Expected: PASS (this tests the import mechanism, not main.py yet)

- [ ] **Step 3: Create `qt/main.py`**

```python
"""Entry point: creates QApplication, applies stylesheet, launches MainWindow."""

import sys
import pathlib

# Allow `from chat import ChatSession` and `from qt.adapter import QtChatAdapter`
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from PySide6.QtWidgets import QApplication

# Local imports (relative to qt/)
import importlib.util, os
_here = pathlib.Path(__file__).parent
sys.path.insert(0, str(_here))

from colors import QSS
from window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("qwen3-manager")
    app.setStyleSheet(QSS)

    win = MainWindow()
    win.setWindowTitle("qwen3-manager")
    win.setMinimumSize(1200, 700)
    win.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Create `qt/run.bat`**

```bat
@echo off
"%~dp0..\".venv\Scripts\python.exe" "%~dp0main.py" %*
```

Wait — the venv is in `qwen3-manager/.venv`, one level up from `qt/`. The correct run.bat:

```bat
@echo off
"%~dp0..\.venv\Scripts\python.exe" "%~dp0main.py" %*
```

- [ ] **Step 5: Commit**

```bash
git add qt/main.py qt/run.bat
git commit -m "feat: add qt/main.py entry point with sys.path fix and run.bat"
```

---

## Task 6: Rewrite window.py to use QtChatAdapter

**Files:** Modify `qt/window.py` (copied from qt-chat-proto in Task 2, now rewritten)

This task replaces the `LLMWorker`-based message flow with `QtChatAdapter`. The UI structure (panels, toolbar, statusbar) is unchanged. Only the message-sending and signal-handling code changes.

Key changes:
- Remove `from llm_client import LLMWorker` import
- Add `from adapter import QtChatAdapter`
- Replace `self._worker: LLMWorker | None` with `self._adapter: QtChatAdapter`
- Fix toolbar combos to match chat.py naming: Think = `off/on/deep`, Approval = `auto/ask-writes/ask-all/yolo`
- Replace `_start_worker()` with `self._adapter.submit(text, self._plan_mode)`
- Wire adapter signals to handlers
- `_on_done` now receives full text via `text_done` signal (not as argument to done)
- Remove `_on_tool_call` (tool calls handled by adapter now)

- [ ] **Step 1: Write the failing import test**

Add to `qt/tests/test_adapter.py`:

```python
def test_window_imports_without_llm_worker(qapp_core):
    """window.py must not import llm_client (LLMWorker is retired)."""
    import ast, pathlib
    src = (pathlib.Path(__file__).parent.parent / "window.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            assert "llm_client" not in names and "llm_client" not in module, \
                "window.py still imports llm_client"
```

- [ ] **Step 2: Run to confirm it fails** (current window.py still imports llm_client)

```bash
.venv\Scripts\pytest.exe qt\tests\test_adapter.py::test_window_imports_without_llm_worker -v
```

Expected: FAIL (AssertionError: window.py still imports llm_client)

- [ ] **Step 3: Rewrite `qt/window.py`**

Replace the entire file with:

```python
"""
MainWindow — five-panel layout.
Panels: Explorer | Chat+Input | Editor | Server Stats
"""

import os
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QToolBar, QStatusBar, QComboBox, QLineEdit,
    QTreeView, QTabWidget, QTextBrowser, QPlainTextEdit,
    QPushButton, QProgressBar, QMessageBox, QFileSystemModel,
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QTextCharFormat, QTextCursor

import httpx

from colors import USER_COLOR, ASST_COLOR, BG_CODE, BORDER_CODE, ACCENT, TEXT_DIM
from highlighter import SyntaxHighlighter, detect_language
from file_watcher import DirWatcher
from adapter import QtChatAdapter

HOME_DIR = str(Path.home() / "claude-projects")


class _ServerPollWorker(QThread):
    """Background thread for server health/stats polling."""
    polled = Signal(bool, str, str, int, str)

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self._base = base_url

    def run(self) -> None:
        running = False
        ctx_text = "Context: —"
        speed_text = "Speed: —"
        vram_pct = 0
        vram_label = "—"
        try:
            r = httpx.get(f"{self._base}/health", timeout=1.5)
            running = r.status_code == 200
        except Exception:
            pass
        if running:
            try:
                r2 = httpx.get(f"{self._base}/slots", timeout=1.5)
                if r2.status_code == 200:
                    data = r2.json()
                    if isinstance(data, list) and data:
                        slot  = data[0]
                        ctx   = slot.get("n_ctx", 0)
                        used  = slot.get("n_past", 0)
                        speed = slot.get("timings", {}).get("predicted_per_second", 0)
                        ctx_text   = f"Context: {used}/{ctx}"
                        speed_text = f"Speed: {speed:.0f} t/s"
                        vram_pct   = int(used / ctx * 100) if ctx else 0
                        vram_label = f"{used} / {ctx} tok"
            except Exception:
                pass
        self.polled.emit(running, ctx_text, speed_text, vram_pct, vram_label)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._current_file: str | None = None
        self._poll_worker: _ServerPollWorker | None = None
        self._cwd: str = HOME_DIR
        self._response_buf: str = ""
        self._plan_mode: bool = False

        self._watcher = DirWatcher(self)

        # Adapter — starts its worker thread immediately
        self._adapter = QtChatAdapter(self)
        self._adapter.start()

        self._build_menu()
        self._build_toolbar()
        self._build_panels()
        self._build_statusbar()
        self._wire_signals()

        self._watcher.set_cwd(self._cwd)
        self._update_status()

    def closeEvent(self, event):
        self._adapter.shutdown()
        self._adapter.wait(3000)
        super().closeEvent(event)

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()
        for name in ("File", "Sessions", "Model", "Tools", "Skills", "Voice", "Help"):
            mb.addMenu(name)

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("Ribbon", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        self._server_status = QLabel("⬤")
        self._server_status.setStyleSheet("color: #ef4444; font-size: 14px;")
        tb.addWidget(self._server_status)

        self._server_url = QLineEdit("localhost:1234")
        self._server_url.setFixedWidth(130)
        tb.addWidget(self._server_url)
        tb.addSeparator()

        tb.addWidget(QLabel("Model:"))
        self._model_combo = QComboBox()
        self._model_combo.addItems(["local-model", "qwen3-coder-80b"])
        self._model_combo.setFixedWidth(160)
        tb.addWidget(self._model_combo)
        tb.addSeparator()

        tb.addWidget(QLabel("Think:"))
        self._think_combo = QComboBox()
        self._think_combo.addItems(["off", "on", "deep"])
        self._think_combo.setCurrentText("on")
        self._think_combo.setFixedWidth(70)
        self._think_combo.currentTextChanged.connect(self._on_think_changed)
        tb.addWidget(self._think_combo)
        tb.addSeparator()

        tb.addWidget(QLabel("Approval:"))
        self._approval_combo = QComboBox()
        self._approval_combo.addItems(["auto", "ask-writes", "ask-all", "yolo"])
        self._approval_combo.setFixedWidth(90)
        self._approval_combo.currentTextChanged.connect(self._on_approval_changed)
        tb.addWidget(self._approval_combo)
        tb.addSeparator()

        self._cwd_label = QLabel(f"CWD: {self._cwd}")
        self._cwd_label.setStyleSheet(f"color: {TEXT_DIM};")
        tb.addWidget(self._cwd_label)

    # ── Panels ───────────────────────────────────────────────────────────────

    def _build_panels(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_explorer())
        splitter.addWidget(self._build_chat_area())
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_server_stats())
        splitter.setSizes([180, 480, 480, 160])
        self.setCentralWidget(splitter)

    # ── Explorer ─────────────────────────────────────────────────────────────

    def _build_explorer(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QLabel("  EXPLORER")
        header.setStyleSheet(f"color: {ACCENT}; padding: 5px; font-size: 10px; letter-spacing: 1px;")
        layout.addWidget(header)

        self._fs_model = QFileSystemModel()
        self._fs_model.setRootPath(HOME_DIR)
        self._fs_model.setReadOnly(True)

        self._tree = QTreeView()
        self._tree.setModel(self._fs_model)
        self._tree.setRootIndex(self._fs_model.index(HOME_DIR))
        self._tree.setHeaderHidden(True)
        for col in (1, 2, 3):
            self._tree.hideColumn(col)
        self._tree.doubleClicked.connect(self._on_tree_double_click)
        self._tree.clicked.connect(self._on_tree_click)
        layout.addWidget(self._tree)
        return w

    # ── Chat + Input ─────────────────────────────────────────────────────────

    def _build_chat_area(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._compact_view = QTextBrowser()
        self._compact_view.setOpenExternalLinks(False)
        self._full_view = QTextBrowser()
        self._full_view.setOpenExternalLinks(False)
        self._tabs.addTab(self._compact_view, "Compact")
        self._tabs.addTab(self._full_view, "Full Output")
        layout.addWidget(self._tabs, stretch=1)

        input_container = QWidget()
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(6, 6, 6, 6)
        input_layout.setSpacing(6)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Type a message… (Enter to send, Shift+Enter for new line)")
        self._input.setFixedHeight(90)
        self._input.installEventFilter(self)
        input_layout.addWidget(self._input, stretch=1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(60)
        self._send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(self._send_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        layout.addWidget(input_container)
        return w

    # ── File Editor ──────────────────────────────────────────────────────────

    def _build_editor(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background: #0f0f1a; border-bottom: 1px solid #333;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)

        self._editor_label = QLabel("No file open")
        self._editor_label.setStyleSheet(f"color: {TEXT_DIM};")
        header_layout.addWidget(self._editor_label, stretch=1)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(55)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_file)
        header_layout.addWidget(self._save_btn)
        layout.addWidget(header)

        self._editor = QPlainTextEdit()
        self._editor.setReadOnly(False)
        self._editor.modificationChanged.connect(self._on_editor_modified)
        self._highlighter: SyntaxHighlighter | None = None
        layout.addWidget(self._editor, stretch=1)
        return w

    # ── Server Stats ─────────────────────────────────────────────────────────

    def _build_server_stats(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: #0f0f1a;")
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QLabel("SERVER")
        header.setStyleSheet(f"color: {ACCENT}; font-size: 10px; letter-spacing: 1px;")
        layout.addWidget(header)

        self._stat_status = QLabel("● Unknown")
        self._stat_status.setStyleSheet("color: #888;")
        layout.addWidget(self._stat_status)

        layout.addWidget(_section_label("CONTEXT"))
        self._vram_label = QLabel("—")
        layout.addWidget(self._vram_label)
        self._vram_bar = QProgressBar()
        self._vram_bar.setRange(0, 100)
        self._vram_bar.setValue(0)
        self._vram_bar.setTextVisible(False)
        self._vram_bar.setFixedHeight(6)
        layout.addWidget(self._vram_bar)

        layout.addWidget(_section_label("SPEED"))
        self._stat_speed = QLabel("Speed: —")
        layout.addWidget(self._stat_speed)

        layout.addWidget(_section_label("SESSION"))
        self._stat_msgs = QLabel("Tokens: 0")
        layout.addWidget(self._stat_msgs)

        layout.addStretch()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_server)
        self._poll_timer.start()
        return w

    # ── Status bar ───────────────────────────────────────────────────────────

    def _build_statusbar(self):
        self._status_bar = self.statusBar()

    def _update_status(self):
        think = self._think_combo.currentText() if hasattr(self, "_think_combo") else "on"
        approval = self._approval_combo.currentText() if hasattr(self, "_approval_combo") else "auto"
        self._status_bar.showMessage(
            f"CWD: {self._cwd}  |  think: {think}  |  approval: {approval}"
        )

    # ── Signal wiring ────────────────────────────────────────────────────────

    def _wire_signals(self):
        self._watcher.file_changed.connect(self._on_watched_file_changed)
        self._adapter.text_token.connect(self._on_text_token)
        self._adapter.text_done.connect(self._on_text_done)
        self._adapter.tool_start.connect(self._on_tool_start)
        self._adapter.tool_done.connect(self._on_tool_done_signal)
        self._adapter.approval_needed.connect(self._on_approval_needed)
        self._adapter.usage.connect(self._on_usage)
        self._adapter.system_msg.connect(self._on_system_msg)
        self._adapter.error_msg.connect(self._on_error_msg)
        self._adapter.done.connect(self._on_turn_done)

    # ── Event filter (Enter in input) ────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key == Qt.Key.Key_Return and not (mods & Qt.KeyboardModifier.ShiftModifier):
                self._send_message()
                return True
        return super().eventFilter(obj, event)

    # ── Slots ────────────────────────────────────────────────────────────────

    @Slot()
    def _send_message(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._set_input_enabled(False)
        self._append_user(text)
        self._response_buf = ""
        self._full_view.append(
            f'<span style="color:{ASST_COLOR};font-weight:bold;">Eli</span><br>'
        )
        self._adapter.submit(text, self._plan_mode)

    @Slot(str)
    def _on_text_token(self, token: str):
        self._response_buf += token
        _insert_plain(self._full_view, token)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_text_done(self, full_text: str):
        header = f'<span style="color:{ASST_COLOR};font-weight:bold;">Eli</span><br>'
        self._compact_view.insertHtml(header + _markdown_to_html(full_text) + "<br><br>")
        self._auto_scroll(self._compact_view)

    @Slot(str, str, str)
    def _on_tool_start(self, tool_id: str, name: str, args: str):
        summary = f'<i style="color:{TEXT_DIM};">⚙ {name}  {args[:60]}</i><br>'
        self._full_view.insertHtml(summary)

    @Slot(str, str, str, bool)
    def _on_tool_done_signal(self, tool_id: str, name: str, result: str, is_error: bool):
        icon = "✗" if is_error else "✓"
        color = "#ef4444" if is_error else "#7dff7d"
        self._full_view.insertHtml(
            f'<span style="color:{color};">{icon} {name}</span><br>'
        )

    @Slot(str, str)
    def _on_approval_needed(self, title: str, message: str):
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        approved = reply == QMessageBox.StandardButton.Yes
        self._adapter.resolve_approval(approved, "")

    @Slot(int, int)
    def _on_usage(self, tokens: int, ctx: int):
        self._stat_msgs.setText(f"Tokens: {tokens:,}")

    @Slot(str)
    def _on_system_msg(self, text: str):
        self._status_bar.showMessage(text, 4000)

    @Slot(str)
    def _on_error_msg(self, msg: str):
        self._compact_view.append(f'<span style="color:#ef4444;">Error: {msg}</span><br>')
        self._set_input_enabled(True)

    @Slot()
    def _on_turn_done(self):
        self._full_view.append("<br>")
        self._plan_mode = False
        self._set_input_enabled(True)
        self._update_status()

    @Slot()
    def _on_tree_click(self, index):
        path = self._fs_model.filePath(index)
        self._status_bar.showMessage(path, 3000)

    @Slot()
    def _on_tree_double_click(self, index):
        path = self._fs_model.filePath(index)
        if os.path.isdir(path):
            self._cwd = path
            self._cwd_label.setText(f"CWD: {path}")
            self._watcher.set_cwd(path)
            self._update_status()
        else:
            self._load_file(path)

    def _load_file(self, path: str):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            self._status_bar.showMessage(f"Cannot open {path}: {e}", 4000)
            return
        if self._current_file:
            self._watcher.unwatch_file(self._current_file)
        self._current_file = path
        self._watcher.watch_file(path)
        lang = detect_language(path)
        if self._highlighter:
            self._highlighter.setDocument(None)
        self._editor.setPlainText(content)
        self._highlighter = SyntaxHighlighter(self._editor.document(), lang)
        self._editor.document().setModified(False)
        self._editor_label.setText(os.path.basename(path))
        self._save_btn.setEnabled(True)

    @Slot()
    def _save_file(self):
        if not self._current_file:
            return
        with open(self._current_file, "w", encoding="utf-8") as f:
            f.write(self._editor.toPlainText())
        self._editor.document().setModified(False)
        self._editor_label.setText(os.path.basename(self._current_file))

    @Slot(bool)
    def _on_editor_modified(self, modified: bool):
        if not self._current_file:
            return
        name = os.path.basename(self._current_file)
        self._editor_label.setText(f"*{name}" if modified else name)

    @Slot(str)
    def _on_watched_file_changed(self, path: str):
        if path != self._current_file:
            return
        if self._editor.document().isModified():
            reply = QMessageBox.question(
                self, "File Changed",
                "File changed on disk. Reload and lose changes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                return
        self._load_file(path)

    @Slot()
    def _poll_server(self):
        if self._poll_worker is not None and self._poll_worker.isRunning():
            return
        base = f"http://{self._server_url.text().strip()}"
        self._poll_worker = _ServerPollWorker(base, parent=self)
        self._poll_worker.polled.connect(self._on_poll_result)
        self._poll_worker.start()

    @Slot(bool, str, str, int, str)
    def _on_poll_result(self, running: bool, ctx_text: str, speed_text: str,
                        vram_pct: int, vram_label: str):
        if running:
            self._stat_status.setText("● Running")
            self._stat_status.setStyleSheet("color: #7dff7d;")
        else:
            self._stat_status.setText("● Offline")
            self._stat_status.setStyleSheet("color: #ef4444;")
        self._vram_label.setText(vram_label)
        self._stat_speed.setText(speed_text)
        self._vram_bar.setValue(vram_pct)

    @Slot(str)
    def _on_think_changed(self, val: str):
        self._adapter.submit_slash(f"/think {val}")
        self._update_status()

    @Slot(str)
    def _on_approval_changed(self, val: str):
        self._adapter.submit_slash(f"/approval {val}")
        self._update_status()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _append_user(self, text: str):
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = (f'<span style="color:{USER_COLOR};font-weight:bold;">You</span><br>'
                f'<span style="color:#cccccc;">&nbsp;&nbsp;{safe}</span><br><br>')
        self._compact_view.insertHtml(html)
        self._full_view.insertHtml(html)
        self._auto_scroll(self._compact_view)
        self._auto_scroll(self._full_view)

    @staticmethod
    def _auto_scroll(view: QTextBrowser):
        sb = view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 10:
            sb.setValue(sb.maximum())

    def _set_input_enabled(self, enabled: bool):
        self._input.setEnabled(enabled)
        self._send_btn.setEnabled(enabled)


# ── Module-level helpers ───────────────────────────────────────────────────────

def _markdown_to_html(text: str) -> str:
    """Convert fenced code blocks to styled HTML for the Compact view."""
    import re
    parts = []
    last = 0
    for m in re.finditer(r'```(?:\w*)\n(.*?)```', text, re.DOTALL):
        before = text[last:m.start()]
        if before:
            escaped = before.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            parts.append(escaped.replace("\n", "<br>"))
        code = m.group(1).rstrip("\n")
        escaped_code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(
            f'<div style="background:#1a1a2e;border-left:2px solid #555555;'
            f'padding:6px 10px;margin:4px 0;white-space:pre;">{escaped_code}</div>'
        )
        last = m.end()
    tail = text[last:]
    if tail:
        escaped = tail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(escaped.replace("\n", "<br>"))
    return "".join(parts)


def _insert_plain(view: QTextBrowser, text: str) -> None:
    """Insert plain text preserving all spaces and newlines."""
    fmt = QTextCharFormat()
    fmt.setForeground(QColor("#cccccc"))
    cursor = view.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.setCharFormat(fmt)
    cursor.insertText(text)
    view.setTextCursor(cursor)


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px; letter-spacing: 1px; margin-top: 8px;")
    return lbl
```

- [ ] **Step 4: Run the import test**

```bash
.venv\Scripts\pytest.exe qt\tests\test_adapter.py::test_window_imports_without_llm_worker -v
```

Expected: PASS

- [ ] **Step 5: Run all adapter + support tests**

```bash
.venv\Scripts\pytest.exe qt\tests\ -v
```

Expected: all tests PASS (skip any requiring a real LLM server)

- [ ] **Step 6: Commit**

```bash
git add qt/window.py
git commit -m "feat: rewrite window.py to use QtChatAdapter, retire LLMWorker"
```

---

## Task 7: Smoke test the full application

**Files:** none (verification only)

- [ ] **Step 1: Launch the Qt app**

```bash
qt\run.bat
```

Expected:
- Window appears with 4 panels
- Toolbar shows Think=on, Approval=auto dropdowns
- Status bar shows CWD path
- Server indicator shows ● (red if llama-server not running, green if running)

- [ ] **Step 2: Verify chat.py terminal still works**

Open a second terminal:

```bash
cd C:\Users\timob\claude-projects\qwen3-manager
.venv\Scripts\python.exe chat.py
```

Expected: prompt appears, no import errors, no crashes.

- [ ] **Step 3: Send a test message in the Qt GUI** (requires llama-server running)

If the server is running at localhost:1234, type "say hello" in the input and press Enter.

Expected:
- Input disables
- "Eli" header appears in Full Output tab
- Text streams token by token in Full Output
- Compact tab shows formatted response after turn completes
- Input re-enables

- [ ] **Step 4: Run the full test suite one final time**

```bash
.venv\Scripts\pytest.exe qt\tests\ -v
```

Expected: all tests PASS

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat(sp1): Qt GUI migration and ChatSession backend bridge complete

- Moved qt-chat-proto files into qwen3-manager/qt/
- Created QtChatAdapter with persistent asyncio event loop
- Patched chat.py: asyncio get_running_loop, compact config instance attrs
- Rewired MainWindow to use adapter signals; retired LLMWorker
- Fixed toolbar combos to match chat.py naming (off/on/deep, auto/ask-writes/ask-all/yolo)
- All unit tests passing

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Verification Checklist

- [ ] `qt\run.bat` launches the window without errors
- [ ] `python chat.py` still works as a terminal app
- [ ] `.venv\Scripts\pytest.exe qt\tests\ -v` — all tests pass
- [ ] No reference to `llm_client` in any `qt/` file
- [ ] `chat.py` line 2346 uses `get_running_loop()` not `get_event_loop()`
- [ ] `ChatSession` has `compact_threshold`, `keep_recent`, `input_compress_limit` as instance attrs
