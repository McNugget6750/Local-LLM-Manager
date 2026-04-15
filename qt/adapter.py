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
from qt.request_classifier import classify as _classify_request


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
    usage           = Signal(int, int, int)         # slot_index, tokens_used, ctx_window
    agent_usage     = Signal(int, str, str, int, int)  # slot_index, tool_id, agent_label, tokens_used, ctx_window
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
    orchestration_awaiting_approval = Signal()

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

        # Orchestration session state
        self._orch_active: bool = False
        self._orch_phase: str = "idle"   # explore | planning | implementing | verifying | done
        self._orch_pulse: str = ""       # loaded at startup from orchestration_pulse.md
        self._orch_original_request: str = ""  # persisted for compaction recovery

    # ── Orchestration state persistence ─────────────────────────────────────

    def _save_orch_state(self) -> None:
        """Persist current orchestration state so it survives context compaction."""
        from qt.session_state import save_state
        save_state(
            orch_active=self._orch_active,
            orch_phase=self._orch_phase,
            orch_original_request=self._orch_original_request,
        )

    def _clear_orch_state(self) -> None:
        """Remove persisted orchestration state after clean completion."""
        from qt.session_state import save_state
        save_state(orch_active=False, orch_phase="idle", orch_original_request="")

    # ── Worker thread (asyncio / chat) ───────────────────────────────────────

    def run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._work_queue = asyncio.Queue()
        # Build remote server here (background thread) — avoids binding a socket on the
        # Qt main thread which disrupts the loopback health-check poll on Windows.
        from qt.remote_chat import RemoteChatServer
        self._remote = RemoteChatServer(self)
        self.text_token.connect(self._remote.on_text_token)
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
                _orch_pulse_path = pathlib.Path(__file__).parent.parent / "agents" / "orchestration_pulse.md"
                self._orch_pulse = _orch_pulse_path.read_text(encoding="utf-8") if _orch_pulse_path.exists() else ""
                session.approval_level        = _state.get("approval_level", "auto")
                session.think_level           = _state.get("think_level", "on")
                session.compact_threshold     = float(_state.get("compact_threshold", 80)) / 100.0
                session.keep_recent           = int(_state.get("keep_recent", 6))
                session.input_compress_limit  = int(_state.get("input_compress_limit", 8000))

                # Restore orchestration state if a session was interrupted mid-flow
                if _state.get("orch_active"):
                    self._orch_active           = True
                    self._orch_phase            = _state.get("orch_phase", "explore")
                    self._orch_original_request = _state.get("orch_original_request", "")
                    _recovery = (
                        f"[orchestration] Session resumed after context compaction. "
                        f"You are in the '{self._orch_phase}' phase. "
                        f"Original request: {self._orch_original_request!r}. "
                        f"Continue from where you left off — do not restart from Phase 1."
                    )
                    self._work_queue.put_nowait((_recovery, False))
                if session._startup_files:
                    self.system_msg.emit("Context loaded:\n" + "\n".join(session._startup_files))
                # ISM observer — fires on any slot acquire/release and drives the panel
                from agents import _ism
                _ism.on_change(lambda: self.slots_updated.emit(_ism.total_slots(), _ism.in_use()))
                # Emit initial state now — initialize() already ran, observer wasn't connected yet
                self.slots_updated.emit(_ism.total_slots(), _ism.in_use())
                # Start scheduler daemon
                from scheduler import SchedulerDaemon
                _scheduler = SchedulerDaemon(session)
                session._scheduler = _scheduler
                await _scheduler.start()
                try:
                    while True:
                        item = await self._work_queue.get()
                        if item is None:
                            break
                        kind = item[0]
                        if kind == "__slash__":
                            await self._run_slash(session, item[1])
                        else:
                            text, plan_mode = item
                            await self._dispatch_turn(session, text, plan_mode)
                finally:
                    await _scheduler.stop()
        except SystemExit:
            self.error_msg.emit("Server not reachable — start the server first.")
            self.done.emit()
        except Exception as exc:
            self.error_msg.emit(f"Backend init failed: {exc}")
            self.done.emit()

    # ── Per-turn logic ───────────────────────────────────────────────────────

    async def _run_turn(
        self,
        session: ChatSession,
        text: str,
        plan_mode: bool,
        system_override: str | None = None,
        custom_pulse: str | None = None,
    ) -> None:
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

        _orig_system: str | None = None
        if system_override is not None:
            _orig_system = session.messages[0]["content"]
            session.messages[0]["content"] = system_override

        try:
            self._stream_task = asyncio.create_task(
                session.send_and_stream(text, plan_mode=plan_mode, custom_pulse=custom_pulse)
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
            if _orig_system is not None:
                session.messages[0]["content"] = _orig_system
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
        import constants as _const_mod
        import chat      as _chat_mod
        import commands  as _cmd_mod
        import agents    as _agent_mod
        from commands import handle_slash_command
        from rich.console import Console

        self.stream_started.emit()

        while not session.tui_queue.empty():
            session.tui_queue.get_nowait()

        buf = io.StringIO()
        _capture = Console(file=buf, highlight=False, markup=True,
                           no_color=True, width=80)
        # All three modules did `from constants import console` and hold their
        # own local reference — we must patch each one individually.
        _old_consoles = (
            _const_mod.console,
            _chat_mod.console,
            _cmd_mod.console,
            _agent_mod.console,
        )
        _const_mod.console = _chat_mod.console = _cmd_mod.console = _agent_mod.console = _capture

        # Track whether the slash command sent any system_html events (via
        # _gui_panel/_gui_text). If it did, we suppress the system_msg from
        # slash_output to avoid the plain-text ASCII duplicate appearing
        # alongside the already-rendered styled HTML.
        _system_html_count = [0]
        _orig_put = session.tui_queue.put

        async def _tracking_put(event):
            if isinstance(event, dict) and event.get("type") == "system_html":
                _system_html_count[0] += 1
            return await _orig_put(event)

        session.tui_queue.put = _tracking_put

        async def _run_and_signal():
            try:
                await handle_slash_command(cmd, session)
            except Exception as exc:
                await _orig_put({"type": "error", "text": str(exc)})
            finally:
                # Restore queue.put before emitting slash_output/done so the
                # drain loop receives those events through the real put path.
                session.tui_queue.put = _orig_put
                # Inject text_done BEFORE done so remote callers (Telegram, curl)
                # receive the captured output before the event is set.
                captured = buf.getvalue()
                clean = re.sub(r'\x1b\[[0-9;]*[mGKHFABCDsuhl]', '', captured).strip()
                if clean:
                    # suppress_gui=True when _gui_panel/_gui_text already sent
                    # styled HTML — avoids double display in the chat window.
                    suppress = _system_html_count[0] > 0
                    await session.tui_queue.put(
                        {"type": "slash_output", "text": clean, "suppress_gui": suppress}
                    )
                await session.tui_queue.put({"type": "done"})

        try:
            slash_task = asyncio.create_task(_run_and_signal())
            self._stream_task = slash_task
            await self._drain_queue(session, slash_task)
        except Exception as exc:
            self.error_msg.emit(str(exc))
            self.done.emit()
        finally:
            _const_mod.console, _chat_mod.console, _cmd_mod.console, _agent_mod.console = _old_consoles
            self._stream_task = None
            self._cancel_requested = False

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
                    _txt = event["text"]
                    self.text_done.emit(_txt)

                    # Tier 2: Eli self-selects orchestration
                    if not self._orch_active and _txt.lstrip().startswith("[ORCHESTRATE]"):
                        self._orch_active = True
                        self._orch_phase = "explore"
                        # Capture original user request for compaction recovery
                        _last_user = next(
                            (m["content"] for m in reversed(session.messages) if m["role"] == "user"),
                            ""
                        )
                        self._orch_original_request = (_last_user[:300] if isinstance(_last_user, str) else "")
                        self._save_orch_state()
                        if session.messages and session.messages[-1]["role"] == "assistant":
                            session.messages.pop()
                        last_user = next(
                            (m["content"] for m in reversed(session.messages) if m["role"] == "user"), None
                        )
                        if last_user:
                            self._work_queue.put_nowait((last_user, False))

                    if self._orch_active and "[ORCHESTRATION_DONE]" in _txt:
                        self._orch_active = False
                        self._orch_phase = "idle"
                        self._orch_original_request = ""
                        self._clear_orch_state()

                    # [SKIP_APPROVAL] in plan output — auto-advance to implementing
                    if (self._orch_active and self._orch_phase == "planning"
                            and "[SKIP_APPROVAL]" in _txt):
                        self._orch_phase = "implementing"
                        self._save_orch_state()
                        self._work_queue.put_nowait((
                            "[orchestration] Plan approved automatically ([SKIP_APPROVAL] set). "
                            "Proceed to the implementing phase now.",
                            False,
                        ))

                    # [READY_TO_PLAN] — Eli has enough context, skip quick-scan
                    if (self._orch_active and self._orch_phase == "explore"
                            and "[READY_TO_PLAN]" in _txt):
                        self._orch_phase = "planning"
                        self._save_orch_state()
                        self._work_queue.put_nowait((
                            "[orchestration] Context sufficient. Write the implementation plan now. "
                            "This turn runs in plan_mode.",
                            False,
                        ))
            elif etype == "usage":
                tool_id_ev = event.get("tool_id", "")
                if tool_id_ev:
                    self.agent_usage.emit(
                        int(event.get("slot_index", -1)),
                        tool_id_ev,
                        event.get("agent_label", ""),
                        int(event.get("tokens", 0)),
                        int(event.get("ctx", 0)),
                    )
                else:
                    self.usage.emit(int(event.get("slot_index", 0)), int(event.get("tokens", 0)), int(event.get("ctx", 0)))
            elif etype == "clear_chat":
                self.clear_chat.emit()
            elif etype == "cwd_changed":
                self.cwd_changed.emit(event.get("cwd", ""))
            elif etype == "session_resume_html":
                self.session_resume_html.emit(event.get("json_path", ""))
            elif etype == "slash_output":
                # Captured console output from a slash command.
                # text_done → remote callers (Telegram, curl).
                # system_msg → GUI display only when the command did NOT already send
                # styled HTML via _gui_panel/_gui_text (suppress_gui flag).
                if not event.get("suppress_gui"):
                    self.system_msg.emit(event.get("text", ""))
                self.text_done.emit(event.get("text", ""))
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
                if session._bg_agent_tasks or session._bg_process_tasks:
                    self._bg_drain_task = asyncio.create_task(
                        self._drain_bg_agents(session)
                    )
                elif session._pending_bg_results:
                    # All tasks finished during this turn (before the "done" event was processed).
                    # Results are waiting — schedule continuation immediately.
                    n = len(session._pending_bg_results)
                    noun = "task" if n == 1 else f"{n} tasks"
                    self._work_queue.put_nowait((
                        f"[System: Background {noun} completed. "
                        f"Results are now in context. "
                        f"Synthesize and respond to the user now. Do NOT dispatch more agents.]",
                        False,
                    ))
                return

    # ── Routing and orchestration ────────────────────────────────────────────

    async def _dispatch_turn(self, session: ChatSession, text: str, plan_mode: bool) -> None:
        """Classify and route a user message to the appropriate turn handler."""
        # Mid-wait injection: user spoke while background tasks are still running
        _bg_running = len(session._bg_agent_tasks) + len(session._bg_process_tasks)
        _is_system = text.startswith(("[System:", "[system:", "[background:", "[Background:", "[orchestration"))
        if _bg_running and not _is_system:
            noun = "agent" if _bg_running == 1 else f"{_bg_running} background tasks"
            text = (
                f"[context: {_bg_running} background {noun} still running — "
                f"user message arrived mid-wait. Acknowledge briefly.]\n{text}"
            )
            pulse = self._orch_pulse if self._orch_active else None
            await self._run_turn(session, text, plan_mode, custom_pulse=pulse)
            return

        if self._orch_active:
            if self._orch_phase == "planning" and not _is_system:
                # User approved the plan — advance phase and run normally with orch pulse
                self._orch_phase = "implementing"
                self._save_orch_state()
                await self._run_turn(session, text, plan_mode=False, custom_pulse=self._orch_pulse)
            elif self._orch_phase == "planning":
                # System-injected planning notification — run in plan_mode (no custom pulse)
                await self._run_turn(session, text, plan_mode=True, custom_pulse=None)
            else:
                await self._run_turn(session, text, plan_mode=False, custom_pulse=self._orch_pulse)
            return

        cls = _classify_request(text)
        if cls == "orchestrate" and not plan_mode:
            self._orch_active = True
            self._orch_phase = "explore"
            self._orch_original_request = text[:300]
            self._save_orch_state()
            await self._run_turn(session, text, plan_mode=False, custom_pulse=self._orch_pulse)
        else:
            # "direct", "ambiguous", and plan_mode all run via normal turn
            await self._run_turn(session, text, plan_mode)

    async def _drain_bg_agents(self, session: "ChatSession") -> None:
        """Drain tui_queue between turns while background agents/processes are still running.

        Loops until both _bg_agent_tasks and _bg_process_tasks are empty.
        Only counts spawn_agent tool_done events as agent completions — internal
        tool calls within agents are forwarded but not counted.
        """
        _completed = 0
        _completed_labels: list[str] = []
        _cancelled = False
        try:
            while session._bg_agent_tasks or session._bg_process_tasks:
                try:
                    event = await asyncio.wait_for(session.tui_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                etype = event.get("type")
                if etype == "text_token" and event.get("source") == "agent":
                    self.agent_text_token.emit(event["text"], event.get("agent_label", ""))
                elif etype == "tool_start":
                    self.tool_start.emit(event.get("id", ""), event.get("name", ""), event.get("args", ""))
                elif etype == "usage":
                    tool_id_ev = event.get("tool_id", "")
                    if tool_id_ev:
                        self.agent_usage.emit(
                            int(event.get("slot_index", -1)),
                            tool_id_ev,
                            event.get("agent_label", ""),
                            int(event.get("tokens", 0)),
                            int(event.get("ctx", 0)),
                        )
                    else:
                        self.usage.emit(int(event.get("slot_index", 0)), int(event.get("tokens", 0)), int(event.get("ctx", 0)))
                elif etype == "tool_done":
                    # Only count top-level spawn_agent completions, not internal agent tool calls
                    if event.get("name") == "spawn_agent":
                        _completed += 1
                        lbl = event.get("agent_label") or f"agent {_completed}"
                        _completed_labels.append(lbl)
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
            if not _cancelled and session._pending_bg_results:
                n_results = len(session._pending_bg_results)
                if _completed:
                    self.bg_agents_complete.emit(_completed)
                    label_list = ", ".join(f"'{l}'" for l in _completed_labels)
                    noun = "agent" if _completed == 1 else f"{_completed} agents"
                    if self._orch_active:
                        if self._orch_phase == "explore":
                            self._orch_phase = "planning"
                            self._save_orch_state()
                            notification = (
                                f"[orchestration] Exploration complete ({label_list}). "
                                f"Results in context. Synthesize into an implementation plan now. "
                                f"This turn runs in plan_mode."
                            )
                        elif self._orch_phase == "implementing":
                            self._orch_phase = "verifying"
                            self._save_orch_state()
                            notification = (
                                f"[orchestration] Implementation complete ({label_list}). "
                                f"Results in context. Verify the work now."
                            )
                        elif self._orch_phase == "verifying":
                            notification = (
                                f"[orchestration] Verification complete ({label_list}). "
                                f"Results in context. Determine if the workflow is done or if fixes are needed."
                            )
                        else:
                            notification = (
                                f"[orchestration] Background {noun} completed ({label_list}). "
                                f"Results in context. Continue the workflow."
                            )
                    else:
                        notification = (
                            f"[System: Background {noun} completed ({label_list}). "
                            f"Results are now in context. "
                            f"Synthesize the findings and present your response to the user now. "
                            f"Do NOT dispatch more agents — consolidate and respond directly.]"
                        )
                else:
                    # Background processes completed (no spawn_agent agents)
                    noun = "task" if n_results == 1 else f"{n_results} tasks"
                    notification = (
                        f"[System: Background {noun} completed. "
                        "Results are now in context. Continue work.]"
                    )
                self._work_queue.put_nowait((notification, False))
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
