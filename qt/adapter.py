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
    # ── Signals ──────────────────────────────────────────────────────────────
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
    stream_started  = Signal()                     # fires at start of each turn

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_queue: asyncio.Queue | None = None
        self._pending_future: asyncio.Future | None = None
        self._ready = threading.Event()
        self._stream_task: asyncio.Task | None = None
        self._cancel_requested: bool = False

    # ── Worker thread ────────────────────────────────────────────────────────

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._work_queue = asyncio.Queue()
        self._ready.set()
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        try:
            async with ChatSession() as session:
                if session.tui_queue is None:
                    session.tui_queue = asyncio.Queue()
                while True:
                    item = await self._work_queue.get()
                    if item is None:
                        break
                    kind = item[0]
                    if kind == "__slash__":
                        await self._run_slash(session, item[1])
                    else:
                        text, plan_mode = item
                        await self._run_turn(session, text, plan_mode)
        except Exception as exc:
            self.error_msg.emit(f"Backend init failed: {exc}")
            self.done.emit()

    # ── Per-turn logic ───────────────────────────────────────────────────────

    async def _run_turn(self, session: ChatSession, text: str, plan_mode: bool) -> None:
        while not session.tui_queue.empty():
            session.tui_queue.get_nowait()

        try:
            self._stream_task = asyncio.create_task(
                session.send_and_stream(text, plan_mode=plan_mode)
            )
            self.stream_started.emit()
            await self._drain_queue(session, self._stream_task)
            # Autosave after every successful turn
            try:
                session._autosave()
            except Exception:
                pass
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.done.emit()
        finally:
            self._stream_task = None

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
                if stream_task.done():
                    if stream_task.cancelled():
                        if self._cancel_requested:
                            self._cancel_requested = False
                            self.system_msg.emit("(interrupted)")
                        else:
                            self.error_msg.emit("Stream cancelled unexpectedly.")
                        self.done.emit()
                        return
                    if stream_task.exception():
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

    # ── Thread-safe public API ───────────────────────────────────────────────

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

    def cancel(self) -> None:
        """Cancel the in-flight stream. Safe to call when nothing is running."""
        task = self._stream_task
        if task and self._loop:
            self._cancel_requested = True
            self._loop.call_soon_threadsafe(task.cancel)

    def resolve_approval(self, approved: bool, notes: str = "") -> None:
        """Resolve the pending approval Future. Called from the main thread."""
        fut = self._pending_future          # snapshot on main thread
        if fut is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._safe_resolve, fut, approved, notes)

    def _safe_resolve(self, fut: asyncio.Future, approved: bool, notes: str) -> None:
        """Resolve a future safely — runs on the asyncio thread."""
        if not fut.done():
            fut.set_result((approved, notes))

    def shutdown(self) -> None:
        """Signal the worker loop to exit cleanly."""
        if self._loop is not None:
            self._ready.wait()
            self._loop.call_soon_threadsafe(
                self._work_queue.put_nowait, None
            )
