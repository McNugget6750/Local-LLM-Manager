"""
QtChatAdapter — bridges ChatSession (asyncio) and the Qt main thread.

One persistent asyncio event loop lives inside a QThread for the session
lifetime. Work items are submitted thread-safely via call_soon_threadsafe.

Voice mode runs in a separate _VoiceThread (synchronous, threading.Event-based)
so it never touches the asyncio work queue.
"""

import asyncio
import logging
import threading
import sys
import pathlib

log = logging.getLogger(__name__)

# Allow `from chat import ChatSession` regardless of working directory
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from PySide6.QtCore import QThread, Signal
from chat import ChatSession, BASE_URL
from session_state import load_state


# ── Voice thread ──────────────────────────────────────────────────────────────

class _VoiceThread(QThread):
    """
    Standalone voice conversation loop.  Runs entirely in its own OS thread
    using synchronous sounddevice + requests — no asyncio involvement.
    Emits the parent adapter's signals directly (PySide6 queues them safely).
    """

    def __init__(self, mode: str, adapter: "QtChatAdapter", parent=None):
        super().__init__(parent)
        self._mode    = mode
        self._ad      = adapter          # emit on adapter's signals
        self._ptt_start = threading.Event()
        self._ptt_stop  = threading.Event()
        self._quit      = threading.Event()
        self._ptt_listener = None

    # ── PTT / stop controls (called from Qt main thread) ─────────────────────

    def ptt_press(self):
        self._ptt_start.set()

    def ptt_release(self):
        self._ptt_stop.set()

    def stop_voice(self):
        self._quit.set()
        # Unblock _record_ptt() which may be waiting on _ptt_start with no timeout
        self._ptt_start.set()
        self._ptt_stop.set()
        if self._ptt_listener is not None:
            try:
                self._ptt_listener.stop()
            except Exception:
                pass

    # ── Thread entry point ───────────────────────────────────────────────────

    def run(self):
        import pathlib as _pl

        TTS_BASE    = "http://127.0.0.1:1236"
        SAMPLE_RATE = 16000

        try:
            import sounddevice as sd
            import numpy as np
        except ImportError as exc:
            self._ad.error_msg.emit(f"Voice requires sounddevice + numpy: {exc}")
            return

        # ── Global Insert-key PTT (pynput daemon thread) ──────────────────────
        if self._mode == "ptt":
            try:
                from pynput import keyboard as _kb
                _active = [False]

                def _on_press(key):
                    if key == _kb.Key.insert and not _active[0]:
                        _active[0] = True
                        self._ptt_start.set()

                def _on_release(key):
                    if key == _kb.Key.insert and _active[0]:
                        _active[0] = False
                        self._ptt_stop.set()

                self._ptt_listener = _kb.Listener(
                    on_press=_on_press, on_release=_on_release
                )
                self._ptt_listener.daemon = True
                self._ptt_listener.start()
                hint = "Hold Insert key or mic button to speak"
            except ImportError:
                hint = "Hold mic button to speak  (install pynput for Insert-key support)"
        else:
            hint = "Speak naturally"

        self._ad.voice_activity.emit("Idle")
        self._ad.system_msg.emit(
            f"Voice {self._mode.upper()} — {hint}. Stop button or ESC to exit."
        )

        # ── Helpers ───────────────────────────────────────────────────────────

        def _probe_server() -> bool:
            import urllib.request
            try:
                with urllib.request.urlopen(TTS_BASE + "/", timeout=1) as r:
                    return r.status == 200
            except Exception:
                return False

        def _get_whisper():
            if self._ad._whisper_model is None:
                from faster_whisper import WhisperModel
                self._ad._whisper_model = WhisperModel(
                    "base", device="cpu", compute_type="int8"
                )
            return self._ad._whisper_model

        def _transcribe(audio: bytes) -> str:
            import json, urllib.request as _ul
            if not self._ad._tts_use_server:
                self._ad._tts_use_server = _probe_server()
                self._ad.voice_server_status.emit(self._ad._tts_use_server)
            if self._ad._tts_use_server:
                req = _ul.Request(
                    f"{TTS_BASE}/transcribe?sample_rate={SAMPLE_RATE}",
                    data=audio,
                    headers={"Content-Type": "application/octet-stream"},
                    method="POST",
                )
                try:
                    with _ul.urlopen(req, timeout=30) as r:
                        return json.loads(r.read()).get("text", "").strip()
                except Exception:
                    self._ad._tts_use_server = False
            audio_np = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            segs, _ = _get_whisper().transcribe(audio_np, language="en")
            return " ".join(s.text for s in segs).strip()


        def _record_ptt() -> bytes:
            """Block until PTT pressed, then record until released."""
            self._ptt_start.wait()
            self._ptt_start.clear()

            chunks: list[bytes] = []
            q: list[bytes]      = []
            q_lock  = threading.Lock()
            q_ready = threading.Event()

            def _cb(indata, frames, time_info, status):
                with q_lock:
                    q.append(indata.copy().tobytes())
                q_ready.set()

            frame_samples = int(SAMPLE_RATE * 0.020)
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=frame_samples, callback=_cb):
                while not self._ptt_stop.is_set() and not self._quit.is_set():
                    q_ready.wait(timeout=0.05)
                    q_ready.clear()
                    with q_lock:
                        chunks.extend(q)
                        q.clear()

            self._ptt_stop.clear()
            return b"".join(chunks)

        def _record_auto() -> bytes | None:
            """VAD-based recording; returns PCM or None when quit fires."""
            try:
                import webrtcvad
            except ImportError:
                self._ad.error_msg.emit(
                    "webrtcvad not installed — auto mode unavailable.\n"
                    "Run: pip install webrtcvad"
                )
                return None

            RMS_THRESHOLD   = 650
            ONSET_FRAMES    = 2
            MIN_SPEECH_MS   = 500
            SILENCE_TIMEOUT = 2.5

            vad           = webrtcvad.Vad(2)
            frame_ms      = 20
            frame_samples = int(SAMPLE_RATE * frame_ms / 1000)
            silence_needed = int(SILENCE_TIMEOUT * 1000 / frame_ms)
            min_frames     = int(MIN_SPEECH_MS / frame_ms)

            chunks: list[bytes] = []
            silence_count = 0
            in_speech  = False
            onset_count = 0

            q: list[bytes] = []
            q_lock  = threading.Lock()
            q_ready = threading.Event()

            def _cb(indata, frames, time_info, status):
                with q_lock:
                    q.append(indata.copy().tobytes())
                q_ready.set()

            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=frame_samples, callback=_cb):
                while not self._quit.is_set():
                    q_ready.wait(timeout=0.1)
                    q_ready.clear()
                    with q_lock:
                        frames_pending = list(q)
                        q.clear()

                    for frame in frames_pending:
                        if self._quit.is_set():
                            return None
                        rms = float(np.sqrt(np.mean(
                            np.frombuffer(frame, dtype=np.int16)
                            .astype(np.float32) ** 2
                        )))
                        is_speech = rms >= RMS_THRESHOLD and vad.is_speech(frame, SAMPLE_RATE)

                        if is_speech:
                            onset_count += 1
                            if not in_speech and onset_count >= ONSET_FRAMES:
                                in_speech = True
                                self._ad.voice_activity.emit("Listening")
                            elif in_speech:
                                chunks.append(frame)
                                silence_count = 0
                        else:
                            onset_count = 0
                            if in_speech:
                                chunks.append(frame)
                                silence_count += 1
                                if silence_count >= silence_needed:
                                    if len(chunks) >= min_frames:
                                        return b"".join(chunks)
                                    else:
                                        chunks.clear()
                                        in_speech = False
                                        silence_count = 0
            return None

        # ── Probe voice server now that helpers are defined ───────────────────
        self._ad._tts_use_server = _probe_server()
        self._ad.voice_server_status.emit(self._ad._tts_use_server)

        # ── Main voice loop ───────────────────────────────────────────────────
        MIN_PTT_BYTES = int(SAMPLE_RATE * 0.200) * 2   # 200 ms minimum

        try:
            if self._mode == "ptt":
                while not self._quit.is_set():
                    self._ad.voice_activity.emit("Idle")
                    audio = _record_ptt()
                    if not audio or len(audio) < MIN_PTT_BYTES or self._quit.is_set():
                        continue

                    self._ad.voice_activity.emit("Transcribing")
                    try:
                        transcript = _transcribe(audio)
                    except Exception as exc:
                        self._ad.error_msg.emit(f"Transcribe failed: {exc}")
                        continue
                    if not transcript:
                        continue

                    self._ad.voice_transcript.emit(transcript)

            else:  # auto VAD
                while not self._quit.is_set():
                    self._ad.voice_activity.emit("Listening")
                    audio = _record_auto()
                    if audio is None:
                        break

                    self._ad.voice_activity.emit("Transcribing")
                    try:
                        transcript = _transcribe(audio)
                    except Exception as exc:
                        self._ad.error_msg.emit(f"Transcribe failed: {exc}")
                        continue
                    if not transcript:
                        continue

                    self._ad.voice_transcript.emit(transcript)

        except Exception as exc:
            self._ad.error_msg.emit(f"Voice error: {exc}")
        finally:
            if self._ptt_listener is not None:
                try:
                    self._ptt_listener.stop()
                except Exception:
                    pass
                self._ptt_listener = None
            self._ad.voice_activity.emit("Idle")
            self._ad.system_msg.emit("Voice mode ended.")


# ── Chat adapter ──────────────────────────────────────────────────────────────

class QtChatAdapter(QThread):
    # ── Signals ──────────────────────────────────────────────────────────────
    think_token     = Signal(str)
    text_token      = Signal(str)
    tool_start      = Signal(str, str, str)        # id, name, args_json
    tool_done       = Signal(str, str, str, bool)  # id, name, result, is_error
    approval_needed = Signal(str, str, str, str)    # title, message, tool_name, args_str
    text_done       = Signal(str)
    usage           = Signal(int, int)             # tokens_used, ctx_window
    system_msg      = Signal(str)
    system_html     = Signal(str)   # pre-formatted HTML from slash command output
    error_msg       = Signal(str)
    done            = Signal()
    stream_started  = Signal()                     # fires at start of each turn
    clear_chat      = Signal()
    cwd_changed     = Signal(str)          # new CWD path after /resume or /cd
    session_saved   = Signal(str)          # full path to the saved JSON file
    session_resume_html = Signal(str)      # full path to JSON; window loads sibling .html

    # Voice signals (emitted by _VoiceThread directly)
    voice_activity      = Signal(str)
    voice_transcript    = Signal(str)
    voice_reply         = Signal(str)
    voice_server_status = Signal(bool)   # True = server reachable, False = local whisper
    agent_text_token    = Signal(str, str)  # (token, agent_label) from a spawned sub-agent
    remote_message      = Signal(str)    # remote HTTP submission — show bubble in GUI
    open_in_editor      = Signal(str, int)          # path, line — model requests editor navigation
    highlight_in_editor = Signal(str, int, int, int, int)  # path, start_line, end_line, start_col, end_col
    bg_agents_complete  = Signal(int)    # all background agents finished; count = number that ran
    slots_updated       = Signal(int, int)  # total_slots, in_use

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._work_queue: asyncio.Queue | None = None
        self._pending_future: asyncio.Future | None = None
        self._ready = threading.Event()
        self._stream_task: asyncio.Task | None = None
        self._cancel_requested: bool = False

        # Voice (managed by _VoiceThread, independent of asyncio)
        self._voice_thread: _VoiceThread | None = None
        self._tts_use_server: bool = True
        self._whisper_model = None

        # Remote HTTP bridge — created in run() to avoid binding socket on main thread
        self._remote = None

        # Background agent drain task
        self._bg_drain_task: asyncio.Task | None = None

    # ── Worker thread (asyncio / chat) ───────────────────────────────────────

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._work_queue = asyncio.Queue()
        # Build remote server here (background thread) — avoids binding a socket on the
        # Qt main thread which disrupts the loopback health-check poll on Windows.
        from qt.remote_chat import RemoteChatServer
        self._remote = RemoteChatServer(self)
        self.text_done.connect(self._remote.on_text_done)
        self.tool_done.connect(self._remote.on_tool_done)
        self.done.connect(self._remote.on_done)
        self._ready.set()
        self._remote.start()
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        try:
            async with ChatSession() as session:
                if session.tui_queue is None:
                    session.tui_queue = asyncio.Queue()
                # Restore persisted settings into the live session
                _state = load_state()
                session.approval_level        = _state.get("approval_level", "auto")
                session.think_level           = _state.get("think_level", "on")
                session.compact_threshold     = float(_state.get("compact_threshold", 80)) / 100.0
                session.keep_recent           = int(_state.get("keep_recent", 6))
                session.input_compress_limit  = int(_state.get("input_compress_limit", 8000))
                if session._startup_files:
                    self.system_msg.emit("Context loaded:\n" + "\n".join(session._startup_files))
                # ISM observer — fires on any slot acquire/release and drives the panel
                from agents import _ism
                _ism.on_change(lambda: self.slots_updated.emit(_ism.total_slots(), _ism.in_use()))
                # Emit initial state now — initialize() already ran, observer wasn't connected yet
                self.slots_updated.emit(_ism.total_slots(), _ism.in_use())
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
        except SystemExit:
            self.error_msg.emit("Server not reachable — start the server first.")
            self.done.emit()
        except Exception as exc:
            self.error_msg.emit(f"Backend init failed: {exc}")
            self.done.emit()

    # ── Per-turn logic ───────────────────────────────────────────────────────

    async def _run_turn(self, session: ChatSession, text: str, plan_mode: bool) -> None:
        # Cancel persistent bg drain if running between turns
        if self._bg_drain_task and not self._bg_drain_task.done():
            n_bg = len(session._bg_agent_tasks)
            log.debug("_run_turn: cancelling bg drain (%d background agent(s) still running)", n_bg)
            self._bg_drain_task.cancel()
            try:
                await self._bg_drain_task
            except asyncio.CancelledError:
                pass
        self._bg_drain_task = None

        # Flush stale events only when no background agents are running
        # (skip flush while bg agents are running — their tokens are still arriving)
        if not session._bg_agent_tasks:
            while not session.tui_queue.empty():
                session.tui_queue.get_nowait()

        # Quick health probe before touching the session — avoids corrupting history
        # if the server is not up at all.
        import httpx as _httpx
        try:
            async with _httpx.AsyncClient() as _probe:
                await _probe.get(f"{BASE_URL}/health", timeout=2.0)
        except Exception:
            self.error_msg.emit("Server is offline — start the server and try again.")
            self.done.emit()
            return

        try:
            self._stream_task = asyncio.create_task(
                session.send_and_stream(text, plan_mode=plan_mode)
            )
            self.stream_started.emit()
            await self._drain_queue(session, self._stream_task)
            try:
                session._autosave()
                if session._session_path:
                    self.session_saved.emit(str(session._session_path))
            except Exception:
                pass
        except _httpx.ConnectError:
            # Server went down mid-turn — remove the dangling user message so
            # retrying doesn't corrupt history with duplicates.
            if session.messages and session.messages[-1]["role"] == "user":
                session.messages.pop()
            self.error_msg.emit("Server went offline during the request. Please try again.")
            self.done.emit()
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.done.emit()
        finally:
            self._stream_task = None
            self._cancel_requested = False

    async def _run_slash(self, session: ChatSession, cmd: str) -> None:
        """Run a slash command, streaming tui_queue events in real time.

        handle_slash_command is wrapped in a coroutine that unconditionally puts a
        "done" sentinel in the queue when it finishes — whether the command is a
        simple built-in (no events) or a skill that calls send_and_stream internally.
        _drain_queue then handles all events identically to a normal turn.
        """
        import io, re
        import chat as _chat_mod
        from commands import handle_slash_command
        from rich.console import Console

        self.stream_started.emit()

        while not session.tui_queue.empty():
            session.tui_queue.get_nowait()

        buf = io.StringIO()
        _old_console = _chat_mod.console
        _chat_mod.console = Console(file=buf, highlight=False, markup=True,
                                    no_color=True, width=80)

        async def _run_and_signal():
            try:
                await handle_slash_command(cmd, session)
            finally:
                await session.tui_queue.put({"type": "done"})

        try:
            slash_task = asyncio.create_task(_run_and_signal())
            self._stream_task = slash_task
            await self._drain_queue(session, slash_task)
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.done.emit()
        finally:
            _chat_mod.console = _old_console
            self._stream_task = None
            self._cancel_requested = False

        captured = buf.getvalue()
        clean = re.sub(r'\x1b\[[0-9;]*[mGKHFABCDsuhl]', '', captured).strip()
        if clean:
            self.system_msg.emit(clean)

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
                        session.rollback_partial_turn()
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
                if event.get("source") == "agent":
                    self.agent_text_token.emit(event["text"], event.get("agent_label", ""))
                else:
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
                    event.get("tool_name", ""),
                    event.get("tool_args_str", ""),
                )
                await self._pending_future
                self._pending_future = None
            elif etype == "system_html":
                self.system_html.emit(event["html"])
            elif etype == "text_done":
                if event.get("source") != "agent":   # sub-agent text shown via tool_done result
                    self.text_done.emit(event["text"])
            elif etype == "usage":
                self.usage.emit(
                    int(event.get("tokens", 0)),
                    int(event.get("ctx", 0)),
                )
            elif etype == "clear_chat":
                self.clear_chat.emit()
            elif etype == "cwd_changed":
                self.cwd_changed.emit(event.get("cwd", ""))
            elif etype == "session_resume_html":
                self.session_resume_html.emit(event.get("json_path", ""))
            elif etype == "system":
                self.system_msg.emit(event.get("text", ""))
            elif etype == "error":
                self.error_msg.emit(event.get("text", ""))
                self.done.emit()
                return
            elif etype == "open_in_editor":
                self.open_in_editor.emit(event.get("path", ""), int(event.get("line", 1)))
            elif etype == "highlight_in_editor":
                self.highlight_in_editor.emit(
                    event.get("path", ""),
                    int(event.get("start_line", 1)),
                    int(event.get("end_line", 1)),
                    int(event.get("start_col", -1)),
                    int(event.get("end_col", -1)),
                )
            elif etype == "bg_agents_complete":
                self.bg_agents_complete.emit(event.get("count", 0))
            elif etype == "done":
                self.done.emit()
                if session._bg_agent_tasks:
                    self._bg_drain_task = asyncio.create_task(
                        self._drain_bg_agents(session)
                    )
                return

    async def _drain_bg_agents(self, session: "ChatSession") -> None:
        """Drain tui_queue between turns while background agents are still running."""
        _completed = 0
        _cancelled = False
        try:
            while session._bg_agent_tasks:
                try:
                    event = await asyncio.wait_for(session.tui_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                etype = event.get("type")
                if etype == "text_token" and event.get("source") == "agent":
                    self.agent_text_token.emit(event["text"], event.get("agent_label", ""))
                elif etype == "tool_start":
                    self.tool_start.emit(event.get("id", ""), event.get("name", ""), event.get("args", ""))
                elif etype == "tool_done":
                    _completed += 1
                    self.tool_done.emit(event.get("id", ""), event.get("name", ""),
                                        event.get("result", ""), bool(event.get("is_error", False)))
                elif etype == "system":
                    self.system_msg.emit(event.get("text", ""))
                elif etype == "approval_request":
                    self._pending_future = event["future"]
                    self.approval_needed.emit(
                        event.get("title", ""),
                        event.get("message", ""),
                        event.get("tool_name", ""),
                        event.get("tool_args_str", ""),
                    )
                    await self._pending_future
                    self._pending_future = None
        except asyncio.CancelledError:
            _cancelled = True
            # If we were awaiting an approval future when cancelled, resolve it so
            # the background agent doesn't hang waiting for an answer that never comes.
            if self._pending_future is not None and not self._pending_future.done():
                log.warning(
                    "_drain_bg_agents: cancelled while awaiting approval future — "
                    "auto-resolving as denied so background agent can unblock"
                )
                self._pending_future.set_result((False, "cancelled"))
            else:
                log.debug("_drain_bg_agents: cancelled (no pending approval future)")
            self._pending_future = None
            raise
        finally:
            if not _cancelled and _completed:
                self.bg_agents_complete.emit(_completed)
            self._bg_drain_task = None

    # ── Thread-safe public API (chat) ────────────────────────────────────────

    def submit(self, text: str, plan_mode: bool) -> None:
        self._ready.wait()
        self._loop.call_soon_threadsafe(
            self._work_queue.put_nowait, (text, plan_mode)
        )

    def submit_slash(self, cmd: str) -> None:
        self._ready.wait()
        self._loop.call_soon_threadsafe(
            self._work_queue.put_nowait, ("__slash__", cmd)
        )

    def cancel(self) -> None:
        """Cancel the in-flight stream or voice loop."""
        task = self._stream_task
        if task and self._loop:
            self._cancel_requested = True
            self._loop.call_soon_threadsafe(task.cancel)
        elif self._voice_thread is not None and self._voice_thread.isRunning():
            self._voice_thread.stop_voice()

    def resolve_approval(self, approved: bool, notes: str = "") -> None:
        fut = self._pending_future
        if fut is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._safe_resolve, fut, approved, notes)

    def _safe_resolve(self, fut: asyncio.Future, approved: bool, notes: str) -> None:
        if not fut.done():
            fut.set_result((approved, notes))

    def shutdown(self) -> None:
        if self._remote is not None:
            self._remote.stop()
        if self._voice_thread is not None and self._voice_thread.isRunning():
            self._voice_thread.stop_voice()
            self._voice_thread.wait(3000)
            if self._voice_thread.isRunning():
                self._voice_thread.terminate()
        if self._loop is not None:
            self._ready.wait(timeout=2)
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    self._work_queue.put_nowait, None
                )

    # ── Thread-safe public API (voice) ───────────────────────────────────────

    def voice_start(self, mode: str) -> None:
        """Start voice mode in its own thread."""
        if self._voice_thread is not None and self._voice_thread.isRunning():
            return
        self._voice_thread = _VoiceThread(mode, self, self)
        self._voice_thread.finished.connect(self._on_voice_thread_finished)
        self._voice_thread.start()
        self.stream_started.emit()   # enables Stop button / ESC

    def voice_stop(self) -> None:
        if self._voice_thread is not None:
            self._voice_thread.stop_voice()

    def voice_ptt_press(self) -> None:
        if self._voice_thread is not None:
            self._voice_thread.ptt_press()

    def voice_ptt_release(self) -> None:
        if self._voice_thread is not None:
            self._voice_thread.ptt_release()

    def _on_voice_thread_finished(self) -> None:
        """Called on Qt main thread when the voice thread exits."""
        self._voice_thread = None
        self.done.emit()
