"""
commands.py — Slash command handlers and voice conversation loop.

Contains all /slash command handlers and the voice conversation subsystem.
Imported lazily by chat.py:main() and directly by qt/adapter.py.
"""
import asyncio
import html as _html_lib
import json
import re as _re
from datetime import datetime
from pathlib import Path

import httpx
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from constants import BASE_URL, TTS_URL, console
from profiles import (
    _load_commands, _build_model_context, _load_agent_profile, _format_project_config,
)

# Imported from chat/agents at module level — commands.py is always loaded after chat.py is
# fully initialized (lazy import from main() or direct import by adapter).
from agents import _ism, _switch_server, _find_active_profile
from chat import (
    ChatSession, TOOLS, CTX_COMPACT_THRESH, SESSIONS_DIR,
    _build_initial_messages, _load_session, _save_state,
    _invoke_skill, _load_skills, stream_events,
)

# ── GUI slash-output helpers ───────────────────────────────────────────────────

_RICH_STYLES: dict[str, str] = {
    "bold cyan":    "font-weight:600;color:#22d3ee",
    "bold green":   "font-weight:600;color:#4ade80",
    "bold yellow":  "font-weight:600;color:#facc15",
    "bold red":     "font-weight:600;color:#f87171",
    "bold magenta": "font-weight:600;color:#c084fc",
    "bold blue":    "font-weight:600;color:#60a5fa",
    "bold":         "font-weight:600",
    "dim":          "opacity:0.5",
    "cyan":         "color:#22d3ee",
    "green":        "color:#4ade80",
    "yellow":       "color:#facc15",
    "red":          "color:#f87171",
    "magenta":      "color:#c084fc",
    "blue":         "color:#60a5fa",
    "italic":       "font-style:italic",
}
_BORDER_COLORS: dict[str, str] = {
    "cyan":    "#22d3ee",
    "magenta": "#c084fc",
    "blue":    "#60a5fa",
    "green":   "#4ade80",
    "yellow":  "#facc15",
    "red":     "#f87171",
    "dim":     "#6b7280",
}
_RICH_TAG_RE = _re.compile(r'\[([a-z ]+)\](.*?)\[/[a-z ]+\]', _re.DOTALL)


def _rich_markup_to_html(text: str) -> str:
    """Convert Rich markup tags to HTML spans. Safe for use in Qt WebEngine."""
    # Preserve escaped brackets before HTML-escaping
    text = text.replace("\\[", "\x00LB\x00").replace("\\]", "\x00RB\x00")
    text = _html_lib.escape(text)
    text = text.replace("\x00LB\x00", "[").replace("\x00RB\x00", "]")

    def _sub(m: _re.Match) -> str:
        tag = m.group(1).strip().lower()
        inner = m.group(2)
        style = _RICH_STYLES.get(tag)
        return f'<span style="{style}">{inner}</span>' if style else inner

    # Apply up to 3 passes to handle nesting (e.g. [bold][cyan]...[/cyan][/bold])
    for _ in range(3):
        text = _RICH_TAG_RE.sub(_sub, text)
    return text


async def _gui_panel(
    session: "ChatSession",
    content: str,
    title: str = "",
    border_style: str = "cyan",
) -> None:
    """Send a styled panel to the GUI chat view. No-op when GUI is not open."""
    if session.tui_queue is None:
        return
    color = _BORDER_COLORS.get(border_style, "#22d3ee")
    title_html = ""
    if title:
        title_markup = _rich_markup_to_html(title)
        title_html = (
            f'<div style="font-weight:600;color:{color};'
            f'margin-bottom:5px;font-size:12px;letter-spacing:.04em;">'
            f'{title_markup}</div>'
        )
    body = _rich_markup_to_html(content).replace("\n", "<br>")
    separator = "<br>" if title_html else ""
    html = (
        f'<div style="border-left:3px solid {color};padding:7px 12px;'
        f'margin:4px 0 8px 0;background:rgba(255,255,255,0.03);'
        f'border-radius:3px;font-family:monospace;font-size:13px;line-height:1.55;">'
        f'{title_html}{separator}{body}</div>'
    )
    await session.tui_queue.put({"type": "system_html", "html": html})


async def _gui_text(session: "ChatSession", markup: str) -> None:
    """Send a single Rich-markup line to the GUI chat view. No-op when GUI is not open."""
    if session.tui_queue is None:
        return
    html = (
        f'<div style="padding:2px 4px;margin:2px 0;'
        f'font-family:monospace;font-size:13px;line-height:1.5;">'
        f'{_rich_markup_to_html(markup)}</div>'
    )
    await session.tui_queue.put({"type": "system_html", "html": html})


# ── Voice mode config ──────────────────────────────────────────────────────────
PTT_KEY          = "scroll_lock"   # pynput Key name or single char
VOICE_DEFAULT_MODE = "ptt"         # "ptt" or "auto"
VOICE_SAMPLE_RATE     = 16000  # Hz — must match what /transcribe expects
VOICE_SILENCE_TIMEOUT = 2.5    # seconds of silence before auto-send (auto mode)
VOICE_MIN_SPEECH_MS   = 500    # ms of speech required before transcribing (auto mode)
VOICE_RMS_THRESHOLD      = 650  # int16 RMS below this is ignored even if VAD says speech
VOICE_ONSET_FRAMES       = 2    # consecutive speech frames required before recording starts
VOICE_POST_TTS_DELAY  = 0.6    # seconds to wait after TTS finishes before listening again

VOICE_SYSTEM_PROMPT = """You are a sharp, engaged conversational partner.
You're a colleague and friend — not an assistant, not a tool.

Rules:
- Respond in 2–4 sentences. You're in a voice conversation — keep it tight.
- Challenge ideas. Push back when something seems off. Ask the one question
  that cuts to the core of the matter.
- No preamble. No affirmations. Just engage directly with what was said.
- Think out loud with the person. Build on their idea or dismantle it.
- When you need to go deeper, do — but never ramble.
- No lists, no bullet points. Spoken prose only.
"""


# ── Voice conversation loop ────────────────────────────────────────────────────

async def _voice_model_call(
    history: list,
    client: httpx.AsyncClient,
    session: "ChatSession | None" = None,
    use_tools: bool = False,
) -> str:
    """Send voice history to the model and return the full reply text. Returns '' on failure.

    When use_tools=True and session is provided, the model may call tools.  The
    tool loop runs silently (results shown in terminal but not spoken).  The final
    text reply is streamed to the terminal and returned for TTS.
    """

    # ── Tool loop (non-streaming so we can inspect tool_calls) ─────────────────
    if use_tools and session is not None:
        working_history = list(history)
        tools_used = False
        for _round in range(6):   # cap tool-calling rounds to prevent infinite loops
            payload_nt = {
                "model": "auto",
                "messages": working_history,
                "stream": False,
                "temperature": 0.85,
                "max_tokens": 600,
                "tools": TOOLS,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            try:
                r = await client.post(f"{BASE_URL}/v1/chat/completions", json=payload_nt)
                r.raise_for_status()
            except httpx.ConnectError:
                console.print("[red]LLM server went away.[/red]")
                return ""
            except httpx.HTTPError as e:
                console.print(f"[red]Server error: {e}[/red]")
                return ""

            choice = r.json()["choices"][0]
            msg    = choice["message"]
            finish = choice.get("finish_reason", "")

            if finish == "tool_calls" or msg.get("tool_calls"):
                # Execute every tool call and append results to working history
                tools_used = True
                working_history.append({"role": "assistant", **{k: v for k, v in msg.items() if k != "role"}})
                for tc in msg.get("tool_calls", []):
                    tc_id   = tc["id"]
                    tc_name = tc["function"]["name"]
                    try:
                        tc_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        tc_args = {}
                    console.print(f"[dim]  ◌ {tc_name}({tc_args})[/dim]")
                    tc_result = await session._dispatch_tool(tc_name, tc_args)
                    console.print(f"[dim]    → {tc_result[:200]}[/dim]")
                    working_history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tc_result,
                    })
                continue   # go around for the model to process results

            # Model returned text directly (no tool calls this round).
            text_reply = (msg.get("content") or "").strip()

            if not tools_used:
                # No tools were called at all — return the direct reply as-is.
                # A summary call here would produce a past-tense recap of a live reply.
                if text_reply:
                    console.print()
                    console.print(text_reply, markup=False)
                    console.print()
                    return text_reply
                return ""

            # Tools were used — the 600-token reply may be long or structured.
            # Make one tight follow-up call: no tools, 180 tokens, spoken-summary prompt.
            if text_reply:
                working_history.append({"role": "assistant", "content": text_reply})

            working_history.append({
                "role": "user",
                "content": (
                    "[Internal instruction — not from the human] "
                    "Summarise what you just found or did in one to three short spoken sentences. "
                    "No lists, no markdown. Speak directly to the person."
                ),
            })
            try:
                r2 = await client.post(f"{BASE_URL}/v1/chat/completions", json={
                    "model": "auto",
                    "messages": working_history,
                    "stream": False,
                    "temperature": 0.85,
                    "max_tokens": 180,
                    "chat_template_kwargs": {"enable_thinking": False},
                })
                r2.raise_for_status()
                text_content = (r2.json()["choices"][0]["message"].get("content") or "").strip()
            except Exception:
                text_content = text_reply

            if text_content:
                console.print()
                console.print(text_content, markup=False)
                console.print()
                return text_content

            return ""

        return ""   # ran out of tool rounds without a text reply

    # ── Simple streaming (no tools) ────────────────────────────────────────────
    payload = {
        "model": "auto",
        "messages": history,
        "stream": True,
        "temperature": 0.85,
        "max_tokens": 180,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    reply_parts: list[str] = []
    console.print()
    try:
        async with client.stream(
            "POST",
            f"{BASE_URL}/v1/chat/completions",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            async for event_type, data in stream_events(
                response, label=f"simple-stream | {BASE_URL}"
            ):
                if event_type == "text":
                    reply_parts.append(data)
                    console.print(data, end="", markup=False)
    except httpx.ConnectError:
        console.print("\n[red]LLM server went away — is llama-server still running?[/red]")
        return ""
    except httpx.RemoteProtocolError:
        console.print("\n[red]LLM server closed the connection mid-stream.[/red]")
        return ""
    except httpx.HTTPError as e:
        console.print(f"\n[red]Server error: {e}[/red]")
        return ""
    console.print()
    return "".join(reply_parts).strip()


async def _voice_record_ptt(ptt_event_start: asyncio.Event, ptt_event_stop: asyncio.Event) -> bytes:
    """Record audio while PTT is held. Returns raw int16 PCM bytes."""
    import numpy as np
    import sounddevice as sd

    await ptt_event_start.wait()
    ptt_event_start.clear()
    console.print("[bold red]● REC[/bold red]", end="\r")

    chunks: list[bytes] = []
    loop = asyncio.get_event_loop()
    chunk_done = asyncio.Event()
    current_chunk: list = [None]

    def callback(indata, frames, time_info, status):
        current_chunk[0] = indata.copy()
        loop.call_soon_threadsafe(chunk_done.set)

    block_size = int(VOICE_SAMPLE_RATE * 0.05)  # 50ms chunks
    with sd.InputStream(
        samplerate=VOICE_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=block_size,
        callback=callback,
    ):
        while not ptt_event_stop.is_set():
            chunk_done.clear()
            await asyncio.wait_for(chunk_done.wait(), timeout=0.2)
            if current_chunk[0] is not None:
                chunks.append(current_chunk[0].tobytes())

    return b"".join(chunks)


async def _voice_record_auto(quit_event: asyncio.Event) -> bytes | None:
    """Record audio using WebRTC VAD + RMS gate. Returns PCM bytes or None if quit."""
    import numpy as np
    import sounddevice as sd
    import webrtcvad

    vad = webrtcvad.Vad(2)
    frame_ms = 20
    frame_samples = int(VOICE_SAMPLE_RATE * frame_ms / 1000)
    silence_frames_needed = int(VOICE_SILENCE_TIMEOUT * 1000 / frame_ms)
    min_speech_frames = int(VOICE_MIN_SPEECH_MS / frame_ms)

    speech_chunks: list[bytes] = []
    silence_count = 0
    in_speech = False
    onset_count = 0   # consecutive loud+speech frames; must reach VOICE_ONSET_FRAMES

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(q.put_nowait, indata.copy().tobytes())

    with sd.InputStream(
        samplerate=VOICE_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
        callback=callback,
    ):
        while not quit_event.is_set():
            try:
                frame = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            # RMS gate — ignore frames below volume threshold regardless of VAD
            audio_np = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(audio_np ** 2)))
            is_speech = rms >= VOICE_RMS_THRESHOLD and vad.is_speech(frame, VOICE_SAMPLE_RATE)

            if is_speech:
                onset_count += 1
                if not in_speech:
                    if onset_count >= VOICE_ONSET_FRAMES:
                        # Confirmed speech onset
                        in_speech = True
                        console.print("[bold red]● REC[/bold red]", end="\r")
                else:
                    speech_chunks.append(frame)
                    silence_count = 0
            else:
                onset_count = 0
                if in_speech:
                    speech_chunks.append(frame)
                    silence_count += 1
                    if silence_count >= silence_frames_needed:
                        if len(speech_chunks) >= min_speech_frames:
                            return b"".join(speech_chunks)
                        else:
                            speech_chunks.clear()
                            in_speech = False
                            silence_count = 0
                            console.print("[dim]  (too short, discarded)[/dim]")
    return None


async def _voice_transcribe(audio_bytes: bytes) -> str:
    """POST raw PCM to /transcribe and return transcript."""
    import requests as _requests
    console.print("[dim]⏳ transcribing...[/dim]", end="\r")
    resp = _requests.post(
        f"{TTS_URL}/transcribe",
        data=audio_bytes,
        params={"sample_rate": VOICE_SAMPLE_RATE},
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


async def _voice_speak(text: str) -> None:
    """Send text to TTS server for playback (blocking)."""
    import requests as _requests
    _requests.post(
        f"{TTS_URL}/play",
        json={"text": text},
        timeout=60,
    )


async def _voice_conversation_loop(session: ChatSession, mode: str = "ptt", use_tools: bool = False) -> None:
    """Blocking voice conversation loop. Exit: Escape (ptt/auto) or q+Enter (CLI).
    TUI-aware: when session.tui_queue is set, emits events instead of console.print.
    """
    _tq = session.tui_queue  # None in CLI, asyncio.Queue in TUI

    async def _sys(text: str) -> None:
        """Emit a status line — TUI system event or console.print."""
        if _tq:
            await _tq.put({"type": "system", "text": text})
        else:
            console.print(f"[dim]{text}[/dim]")

    async def _voice_msg(role: str, text: str) -> None:
        """Emit a transcript/reply line as a chat bubble or console line."""
        if _tq:
            # Reuse text_done so the drain loop renders it as a message widget
            src = "eli" if role == "assistant" else "eli"
            prefix = "You said" if role == "user" else "Eli"
            await _tq.put({"type": "system", "text": f"[{prefix}] {text}"})
        else:
            label = "[bold green]You:[/bold green]" if role == "user" else "[bold cyan]Eli:[/bold cyan]"
            console.print(f"{label} {text}")

    try:
        import sounddevice  # noqa: F401 — check it's available
    except ImportError:
        if _tq:
            await _tq.put({"type": "system", "text": "sounddevice not installed — voice unavailable"})
        else:
            console.print("[red]sounddevice not installed. Run: .venv\\Scripts\\pip install sounddevice[/red]")
        return

    # Load persona from agents/voice.md; fall back to inline constant
    voice_agent_file = Path(__file__).parent / "agents" / "voice.md"
    voice_prompt = (
        voice_agent_file.read_text(encoding="utf-8")
        if voice_agent_file.exists()
        else VOICE_SYSTEM_PROMPT
    )
    history = [{"role": "system", "content": voice_prompt}]

    from pynput import keyboard as _kb

    quit_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    if mode == "ptt":
        # Resolve the PTT key
        try:
            ptt_key = getattr(_kb.Key, PTT_KEY)
        except AttributeError:
            ptt_key = _kb.KeyCode.from_char(PTT_KEY)

        ptt_start = asyncio.Event()
        ptt_stop  = asyncio.Event()

        def on_press(key):
            if key == ptt_key:
                loop.call_soon_threadsafe(ptt_start.set)
                loop.call_soon_threadsafe(ptt_stop.clear)
            elif key == _kb.Key.esc:
                loop.call_soon_threadsafe(quit_event.set)

        def on_release(key):
            if key == ptt_key:
                loop.call_soon_threadsafe(ptt_stop.set)

        listener = _kb.Listener(on_press=on_press, on_release=on_release)
        listener.start()

        tools_note = "tools: on" if use_tools else "tools: off"
        await _sys(f"Voice PTT — hold {PTT_KEY} to speak, release to send. ESC to exit. {tools_note}")

        try:
            while not quit_event.is_set():
                while not ptt_start.is_set() and not quit_event.is_set():
                    await asyncio.sleep(0.05)

                if quit_event.is_set():
                    break

                audio = await _voice_record_ptt(ptt_start, ptt_stop)
                if not audio:
                    continue

                transcript = await _voice_transcribe(audio)
                if not transcript:
                    await _sys("(nothing heard)")
                    continue

                await _voice_msg("user", transcript)
                history.append({"role": "user", "content": transcript})

                reply = await _voice_model_call(history, session.client, session=session, use_tools=use_tools)
                if not reply:
                    continue
                history.append({"role": "assistant", "content": reply})

                await _voice_msg("assistant", reply)
                await _voice_speak(reply)
                await asyncio.sleep(VOICE_POST_TTS_DELAY)

        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()

    else:  # auto VAD mode
        try:
            import webrtcvad  # noqa: F401
        except ImportError:
            if _tq:
                await _tq.put({"type": "system", "text": "webrtcvad not installed — auto voice unavailable"})
            else:
                console.print("[red]webrtcvad not installed. Run: .venv\\Scripts\\pip install webrtcvad[/red]")
            return

        def on_press_auto(key):
            if key == _kb.Key.esc:
                loop.call_soon_threadsafe(quit_event.set)

        listener = _kb.Listener(on_press=on_press_auto)
        listener.start()

        tools_note = "tools: on" if use_tools else "tools: off"
        await _sys(f"Voice AUTO — speak naturally, pause to send. ESC to exit. {tools_note}")

        try:
            while not quit_event.is_set():
                audio = await _voice_record_auto(quit_event)
                if not audio or quit_event.is_set():
                    break

                transcript = await _voice_transcribe(audio)
                if not transcript:
                    await _sys("(nothing heard)")
                    continue

                await _voice_msg("user", transcript)
                history.append({"role": "user", "content": transcript})

                reply = await _voice_model_call(history, session.client, session=session, use_tools=use_tools)
                if not reply:
                    continue
                history.append({"role": "assistant", "content": reply})

                await _voice_msg("assistant", reply)
                await _voice_speak(reply)
                await asyncio.sleep(VOICE_POST_TTS_DELAY)

        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()

    await _sys("Voice mode ended")


# ── Slash command handler ─────────────────────────────────────────────────────
async def handle_slash_command(cmd: str, session: ChatSession) -> bool:
    """Returns True if command was handled (skip sending to model)."""
    parts = cmd.strip().split()
    name = parts[0].lower()

    if name == "/help":
        _help_lines = [
            "[bold]/clear[/bold]                 Reset message history",
            "[bold]/tools[/bold]                 List available tools",
            "[bold]/think \\[off|on|deep\\][/bold]   Set thinking level (or cycle)",
            "[bold]/save \\[path\\][/bold]           Save conversation to JSON",
            "[bold]/compact[/bold]               Summarise older messages to free context",
            "[bold]/debug \\[path|off\\][/bold]     Capture raw SSE stream to file (default: debug_stream_TIMESTAMP.log)",
            "[bold]/status[/bold]                Show token usage and context window info",
            "[bold]/sessions[/bold]              List saved sessions",
            "[bold]/resume \\[name\\][/bold]         Load a saved session (replaces current)",
            "[bold]/approval \\[mode\\][/bold]       Set approval tier: auto|ask-writes|ask-all|yolo",
            "[bold]/cd \\[path\\][/bold]             Set working directory for bash commands",
            "[bold]/pwd[/bold]                   Show current working directory",
            "[bold]/model \\[id\\][/bold]             Switch model or list available models",
            "[bold]/role \\[name\\][/bold]            Adopt an agent persona in the current session",
            "[bold]/config[/bold]                Show loaded eli.toml project config",
            "[bold]/skills[/bold]                List available skills",
            "[bold]/skill <name> \\[args\\][/bold]   Invoke a skill explicitly",
            "[bold]/plan <feature>[/bold]          Implementation planning sub-agent",
            "[bold]/implementation_plan[/bold]     Create and validate a structured TDD implementation plan",
            "[bold]/code <task>[/bold]             Production code writing sub-agent",
            "[bold]/review <file>[/bold]           Deep code review sub-agent",
            "[bold]/research <topic>[/bold]        3-pass skeptical research sub-agent",
            "[bold]/execute-plan <path>[/bold]     Execute plan end-to-end with review-fix loops (max 3 cycles)",
            "[bold]/commit[/bold]                  Generate a conventional commit message",
            "[bold]/pr[/bold]                      Generate a pull request description",
            "[bold]/git-status[/bold]              Full git situation report",
            "[bold]/queue-results \\[label\\][/bold]  List recent agent queue runs, or show one by label",
            "[bold]/voice \\[ptt|auto\\] \\[tools\\][/bold]  Start voice sparring mode (tools flag enables tool use)",
            "[bold]/schedule <when> <tg_id> <task>[/bold]  Add scheduled research job (when: daily, daily:HH:MM, weekly:dow:HH:MM, YYYY-MM-DD)",
            "[bold]/schedule list[/bold]          List all scheduled jobs",
            "[bold]/schedule run <id>[/bold]      Fire a job immediately",
            "[bold]/schedule remove|enable|disable <id>[/bold]  Manage jobs",
            "[bold]/tasks clear[/bold]           Delete the current TASKS.md task list",
            "[bold]/help[/bold]                  Show this message",
            "",
            "[bold]Shift+Tab[/bold]              Cycle mode: normal → plan → normal",
            "[dim]  normal  tools are executed automatically[/dim]",
            "[dim]  plan    model describes its plan, no tools run[/dim]",
            "[bold]Ctrl+O[/bold]                 Toggle compact mode (collapse thinking/tools)",
            "",
            "[dim]Enter  Submit  |  Alt+Enter  Newline  |  Ctrl+D  Exit  |  Ctrl+C  Interrupt[/dim]",
        ]
        _help_content = "\n".join(_help_lines)
        console.print(Panel(_help_content, title="Commands", border_style="cyan"))
        await _gui_panel(session, _help_content, "Commands", "cyan")
        return True

    elif name == "/clear":
        # Cancel background agent and process tasks; release all ISM slots
        for _t in list(session._bg_agent_tasks):
            if not _t.done():
                _t.cancel()
        session._bg_agent_tasks.clear()
        for _t in list(session._bg_process_tasks):
            if not _t.done():
                _t.cancel()
        session._bg_process_tasks.clear()
        session._auto_turn_count = 0
        session._auto_trigger.clear()
        session._pending_bg_results.clear()
        session._pending_bg_tool_calls.clear()
        await _ism.force_release_all()
        _initial, _ = _build_initial_messages()
        session.messages = _initial
        session._n_fixed = len(_initial)
        session.tokens_used = session.tokens_prompt = session.tokens_completion = 0
        await session._refresh_project_config()
        # Also clear the task list
        from tools import tool_task_list
        _tasks_path = str(Path(session.cwd) / "TASKS.md")
        await tool_task_list("clear", path=_tasks_path)
        console.print(Rule("[dim]History cleared[/dim]", style="dim"))
        if session.tui_queue:
            await session.tui_queue.put({"type": "clear_chat"})
        return True

    elif name == "/tools":
        lines = []
        for t in TOOLS:
            fn = t["function"]
            params = list(fn["parameters"]["properties"].keys())
            lines.append(f"[bold cyan]{fn['name']}[/bold cyan]({', '.join(params)})  —  {fn['description']}")
        _tools_content = "\n".join(lines)
        console.print(Panel(_tools_content, title="Available Tools", border_style="cyan"))
        await _gui_panel(session, _tools_content, "Available Tools", "cyan")
        return True

    elif name == "/think":
        LEVELS = ("off", "on", "deep")
        if len(parts) > 1 and parts[1].lower() in LEVELS:
            session.think_level = parts[1].lower()
        else:
            # cycle: off → on → deep → off
            idx = LEVELS.index(session.think_level)
            session.think_level = LEVELS[(idx + 1) % len(LEVELS)]
        labels = {"off": "[dim]off — thinking disabled[/dim]",
                  "on":  "[cyan]on — normal thinking[/cyan]",
                  "deep": "[yellow]deep — thorough reasoning, temp 0.3[/yellow]"}
        _think_msg = f"Think level: {labels[session.think_level]}"
        console.print(_think_msg)
        await _gui_text(session, _think_msg)
        _save_state(think_level=session.think_level)
        return True

    elif name == "/debug":
        import chat as _chat_mod
        if len(parts) > 1 and parts[1].lower() in ("off", "0", "false"):
            _chat_mod._debug_close()
            console.print("[dim]Debug stream capture: off[/dim]")
        elif _chat_mod._debug_file:
            console.print(f"[dim]Debug stream capture already active → {_chat_mod._debug_path}[/dim]")
            console.print("[dim]Use /debug off to stop.[/dim]")
        else:
            path_arg = parts[1] if len(parts) > 1 else "1"
            resolved = _chat_mod._debug_open(path_arg)
            console.print(f"[yellow]Debug stream capture: on → {resolved}[/yellow]")
        return True

    elif name == "/compact":
        await session._compact_history(manual=True)
        return True

    elif name == "/status":
        pct = session.tokens_used / session.ctx_window * 100 if session.ctx_window else 0
        bar_width = 30
        filled = int(bar_width * pct / 100)
        bar = "█" * filled + "░" * (bar_width - filled)
        bar_style = "yellow" if pct > 60 else "green"
        think_label = {"off": "off", "on": "on", "deep": "deep (temp 0.3)"}[session.think_level]
        _status_lines = [
            f"[bold]Context window:[/bold]  {session.ctx_window:,} tokens",
            f"[bold]Tokens used:[/bold]     {session.tokens_used:,}  (~{pct:.0f}%)",
            f"[bold]Usage bar:[/bold]       [{bar_style}]{bar}[/{bar_style}]",
            f"[bold]Messages:[/bold]        {len(session.messages) - session._n_fixed} (+ {session._n_fixed} fixed system)",
            f"[bold]Think level:[/bold]     {think_label}",
            f"[bold]Compact at:[/bold]      {int(session.ctx_window * CTX_COMPACT_THRESH):,} "
            f"tokens ({CTX_COMPACT_THRESH * 100:.0f}%)",
        ]
        _status_content = "\n".join(_status_lines)
        console.print(Panel(_status_content, title="[cyan]Session Status[/cyan]", border_style="cyan"))
        await _gui_panel(session, _status_content, "Session Status", "cyan")
        return True

    elif name == "/save":
        path = parts[1] if len(parts) > 1 else f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(session.messages, f, indent=2, ensure_ascii=False)
            console.print(f"[green]Saved to {path}[/green]")
            await _gui_text(session, f"[green]Saved to {path}[/green]")
        except Exception as e:
            console.print(f"[red]Save failed: {e}[/red]")
            await _gui_text(session, f"[red]Save failed: {e}[/red]")
        return True

    elif name == "/sessions":
        _all = [p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json"] if SESSIONS_DIR.exists() else []
        if not _all:
            console.print("[dim]No saved sessions.[/dim]")
            if session.tui_queue:
                await session.tui_queue.put({"type": "system", "text": "No saved sessions."})
            return True
        all_sessions = sorted(_all, reverse=True)
        if session.tui_queue:
            rows = []
            for s in all_sessions:
                try:
                    data = json.loads(s.read_text(encoding="utf-8"))
                    tok = data.get("token_estimate", 0)
                    saved_at = data.get("saved_at", "")[:16].replace("T", " ")
                    rows.append(f"{s.stem}  —  {saved_at}  (~{tok:,} tokens)")
                except Exception:
                    rows.append(f"{s.stem}  —  (unreadable)")
            await session.tui_queue.put({"type": "system", "text": "Saved sessions:\n" + "\n".join(rows)})
        else:
            lines = []
            for s in all_sessions:
                try:
                    data = json.loads(s.read_text(encoding="utf-8"))
                    tok = data.get("token_estimate", 0)
                    saved_at = data.get("saved_at", "")[:16].replace("T", " ")
                    lines.append(f"[cyan]{s.stem}[/cyan]  [dim]{saved_at}  ~{tok:,} tokens[/dim]")
                except Exception:
                    lines.append(f"[cyan]{s.stem}[/cyan]  [dim](unreadable)[/dim]")
            console.print(Panel("\n".join(lines), title="Saved Sessions", border_style="cyan"))
        return True

    elif name == "/resume":
        resume_name = parts[1] if len(parts) > 1 else None
        saved_msgs, sess_path, saved_cwd = _load_session(resume_name)
        if not saved_msgs:
            hint = resume_name or "latest"
            console.print(f"[yellow]No session found matching '{hint}'[/yellow]")
            return True
        _initial, _ = _build_initial_messages()
        session.messages = _initial + saved_msgs
        session._n_fixed = len(_initial)
        session._session_path = sess_path
        session.tokens_used = session.tokens_prompt = session.tokens_completion = 0
        if saved_cwd and Path(saved_cwd).is_dir():
            session.cwd = Path(saved_cwd)
            await session._refresh_project_config()
            if session.tui_queue:
                await session.tui_queue.put({"type": "cwd_changed", "cwd": str(session.cwd)})
        if session.tui_queue:
            await session.tui_queue.put({"type": "session_resume_html", "json_path": str(sess_path)})
        console.print(Rule(f"[cyan]Session loaded: {sess_path.name}[/cyan]", style="cyan"))
        return True

    elif name == "/approval":
        VALID = ("auto", "ask-writes", "ask-all", "yolo")
        if len(parts) > 1 and parts[1].lower() in VALID:
            session.approval_level = parts[1].lower()
        labels = {
            "auto":       "[green]auto — installs and dangerous commands ask[/green]",
            "ask-writes": "[yellow]ask-writes — all writes and bash ask[/yellow]",
            "ask-all":    "[yellow]ask-all — every tool call asks[/yellow]",
            "yolo":       "[red]yolo — nothing asks (use with care)[/red]",
        }
        _appr_msg = f"Approval: {labels[session.approval_level]}"
        console.print(_appr_msg)
        await _gui_text(session, _appr_msg)
        if len(parts) <= 1:
            _usage = f"  Usage: /approval [{' | '.join(VALID)}]"
            console.print(_usage)
            await _gui_text(session, f"[dim]{_usage.strip()}[/dim]")
        else:
            _save_state(approval_level=session.approval_level)
        return True

    elif name == "/cd":
        if len(parts) < 2:
            console.print(f"[dim]Current directory: {session.cwd}[/dim]")
            return True
        new_path = Path(" ".join(parts[1:])).expanduser()
        if not new_path.is_absolute():
            new_path = session.cwd / new_path
        new_path = new_path.resolve()
        if not new_path.is_dir():
            console.print(f"[red]Not a directory: {new_path}[/red]")
            return True
        session.cwd = new_path
        console.print(f"[green]Working directory: {session.cwd}[/green]")
        await session._refresh_project_config()
        if session.tui_queue:
            await session.tui_queue.put({"type": "cwd_changed", "cwd": str(session.cwd)})
        return True

    elif name == "/pwd":
        console.print(f"[dim]{session.cwd}[/dim]")
        await _gui_text(session, f"[dim]{session.cwd}[/dim]")
        return True

    elif name == "/skills":
        skills = _load_skills()
        if not skills:
            console.print("[dim]No skills found in skills/[/dim]")
            return True
        lines = []
        for sname, skill in sorted(skills.items()):
            tag = " [cyan][agent][/cyan]" if skill.get("spawn_agent") else ""
            desc = skill.get("description", "(no description)")
            raw_triggers = skill.get("triggers", [])
            if isinstance(raw_triggers, str):
                raw_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
            trigger_str = f"  [dim]· triggers: {', '.join(raw_triggers)}[/dim]" if raw_triggers else ""
            lines.append(f"[bold cyan]/{sname}[/bold cyan]{tag}  —  {desc}{trigger_str}")
        _skills_content = "\n".join(lines)
        console.print(Panel(_skills_content, title="Skills", border_style="cyan"))
        await _gui_panel(session, _skills_content, "Skills", "cyan")
        return True

    elif name == "/skill":
        if len(parts) < 2:
            console.print("[yellow]Usage: /skill <name> [args][/yellow]")
            return True
        skill_name = parts[1].lower()
        skill_args = " ".join(parts[2:]) if len(parts) > 2 else ""
        found = await _invoke_skill(skill_name, skill_args, session)
        if not found:
            console.print(f"[yellow]Unknown skill: {skill_name} (try /skills)[/yellow]")
        return True

    elif name == "/model":
        # Fetch available profiles and currently loaded model from Server Manager
        profiles_data = await _control("GET", "/api/profiles")
        status_data   = await _control("GET", "/api/status")
        profiles: list[str] = profiles_data if isinstance(profiles_data, list) else list(_load_commands().keys())
        loaded: str | None  = status_data.get("model") if isinstance(status_data, dict) else None

        if len(parts) > 1:
            target = " ".join(parts[1:])
            # Accept unambiguous prefix matches
            matches = [p for p in profiles if p.lower().startswith(target.lower())]
            if not matches:
                matches = [p for p in profiles if target.lower() in p.lower()]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                _amb = f"[yellow]Ambiguous — did you mean:[/yellow]\n" + "\n".join(f"  {m}" for m in matches)
                console.print(f"[yellow]Ambiguous — did you mean:[/yellow]")
                for m in matches:
                    console.print(f"  {m}")
                await _gui_text(session, _amb)
                return True
            elif target not in profiles:
                console.print(f"[yellow]Unknown profile: {target}[/yellow]")
                console.print(f"[dim]Available: {', '.join(profiles)}[/dim]")
                await _gui_text(session, f"[yellow]Unknown profile: {target}[/yellow]\n[dim]Available: {', '.join(profiles)}[/dim]")
                return True

            if target == loaded:
                console.print(f"[dim]{target} is already loaded.[/dim]")
                await _gui_text(session, f"[dim]{target} is already loaded.[/dim]")
                return True

            console.print(f"[cyan]Switching to {target}…[/cyan]")
            await _gui_text(session, f"[cyan]Switching to {target}…[/cyan]")
            ok = await _switch_server(target)
            if ok:
                session.model = "auto"
                _save_state(model=session.model)
            return True

        # No argument — list all profiles
        lines = []
        for p in profiles:
            marker = "  [green]● loaded[/green]" if p == loaded else ""
            lines.append(f"[bold cyan]{p}[/bold cyan]{marker}")
        if not loaded:
            lines.append("\n[dim]Server Manager not reachable — profile list from commands.json[/dim]")
        lines.append(f"\n[dim]Usage: /model <name>   (prefix match supported)[/dim]")
        _model_content = "\n".join(lines)
        console.print(Panel(_model_content, title="Models", border_style="cyan"))
        await _gui_panel(session, _model_content, "Models", "cyan")
        return True

    elif name == "/role":
        agents_dir = Path(__file__).parent / "agents"
        if len(parts) < 2:
            profiles = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []
            if profiles:
                lines = ["[bold cyan]eli[/bold cyan]  [dim](default — revert to Eli)[/dim]"]
                lines += [f"[bold magenta]{p}[/bold magenta]" for p in profiles]
                lines.append("\n[dim]Usage: /role <name>  — adopt this persona in the current session[/dim]")
                _role_list = "\n".join(lines)
                console.print(Panel(_role_list, title="Agent Profiles", border_style="magenta"))
                await _gui_panel(session, _role_list, "Agent Profiles", "magenta")
            else:
                console.print("[dim]No agent profiles found in agents/[/dim]")
                await _gui_text(session, "[dim]No agent profiles found in agents/[/dim]")
            return True
        role_name = parts[1].lower().replace("-", "_")

        # "eli" reverts to the base system prompt
        if role_name == "eli":
            session.messages.append({
                "role": "system",
                "content": (
                    "[Role Revert — Eli]\n\n"
                    "Discard any previous role overrides. You are Eli again, operating under "
                    "your original system instructions. Conversation context is preserved."
                ),
            })
            session.role = "eli"
            _save_state(role="eli")
            _revert_content = "Reverted to [bold cyan]Eli[/bold cyan].\n[dim]Conversation context preserved.[/dim]"
            console.print(Panel(_revert_content, border_style="cyan"))
            await _gui_panel(session, _revert_content, "", "cyan")
            return True

        agent_file = agents_dir / f"{role_name}.md"
        if not agent_file.exists():
            agent_file = agents_dir / f"{parts[1].lower()}.md"
        if not agent_file.exists():
            console.print(f"[yellow]No profile found: agents/{role_name}.md — try /role with no args to list[/yellow]")
            return True
        profile = agent_file.read_text(encoding="utf-8")
        session.messages.append({
            "role": "system",
            "content": (
                f"[Role Override — {role_name}]\n\n"
                f"The user has asked you to adopt the following agent persona for the remainder of "
                f"this conversation. Read and embody it fully. Your tools and capabilities remain "
                f"unchanged. Continue the current conversation context.\n\n{profile}"
            ),
        })
        session.role = role_name
        _save_state(role=role_name)
        _persona_content = (
            f"Persona loaded: [bold magenta]{role_name}[/bold magenta]\n"
            f"[dim]Profile injected as system message. Conversation context preserved.[/dim]"
        )
        console.print(Panel(_persona_content, border_style="magenta"))
        await _gui_panel(session, _persona_content, "", "magenta")
        return True

    elif name == "/config":
        config = session._project_config
        if not config:
            console.print("[dim]No eli.toml found in current directory tree.[/dim]")
            await _gui_text(session, "[dim]No eli.toml found in current directory tree.[/dim]")
        else:
            _cfg_content = _format_project_config(config)
            console.print(Panel(_cfg_content, title="[cyan]Project Config (eli.toml)[/cyan]", border_style="cyan"))
            await _gui_panel(session, _cfg_content, "Project Config (eli.toml)", "cyan")
        return True

    elif name == "/queue-results":
        if not SESSIONS_DIR.exists():
            console.print("[dim]No queue runs found.[/dim]")
            return True
        queue_dirs = sorted(
            [d for d in SESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith("queue_")],
            reverse=True,
        )
        if not queue_dirs:
            console.print("[dim]No queue runs found.[/dim]")
            return True
        label_filter = " ".join(parts[1:]).lower() if len(parts) > 1 else ""
        if label_filter:
            # Show full details for matching run
            matches = [d for d in queue_dirs if label_filter in d.name.lower()]
            if not matches:
                console.print(f"[yellow]No queue run matching '{label_filter}'[/yellow]")
                return True
            qdir = matches[0]
            results_file = qdir / "results.json"
            if not results_file.exists():
                console.print(f"[yellow]results.json missing in {qdir.name}[/yellow]")
                return True
            try:
                data = json.loads(results_file.read_text(encoding="utf-8"))
                results = data.get("results", [])
                label = data.get("label", "")
                total_dur = data.get("total_duration_seconds", 0)
                lines = []
                if label:
                    lines.append(f"[bold]Label:[/bold] {label}")
                lines.append(f"[bold]Agents:[/bold] {len(results)}   [bold]Total:[/bold] {total_dur:.0f}s")
                lines.append("")
                for r in results:
                    status_col = {"completed": "green", "timeout": "yellow", "error": "red"}.get(r.get("status", ""), "white")
                    lines.append(
                        f"[{status_col}]{r.get('status','?').upper()}[/{status_col}]  "
                        f"[bold]{r.get('index',0)+1}. {r.get('label', r.get('system_prompt',''))}[/bold]  "
                        f"[dim]{r.get('model','')[:40]}  {r.get('duration_seconds',0):.0f}s[/dim]"
                    )
                    result_text = (r.get("result") or "").strip()
                    if result_text:
                        # show first 400 chars
                        preview = result_text[:400] + ("…" if len(result_text) > 400 else "")
                        lines.append(f"  [dim]{preview}[/dim]")
                    lines.append("")
                console.print(Panel("\n".join(lines), title=f"[cyan]Queue: {qdir.name}[/cyan]", border_style="cyan"))
            except Exception as e:
                console.print(f"[red]Failed to read {results_file}: {e}[/red]")
        else:
            # List last 5 queue runs
            lines = []
            for qdir in queue_dirs[:5]:
                results_file = qdir / "results.json"
                try:
                    data = json.loads(results_file.read_text(encoding="utf-8"))
                    results = data.get("results", [])
                    label = data.get("label", "")
                    total_dur = data.get("total_duration_seconds", 0)
                    statuses = [r.get("status", "?") for r in results]
                    err_count = statuses.count("error")
                    timeout_count = statuses.count("timeout")
                    status_str = (
                        f"[red]{err_count} error{'s' if err_count!=1 else ''}[/red]  " if err_count else ""
                    ) + (
                        f"[yellow]{timeout_count} timeout{'s' if timeout_count!=1 else ''}[/yellow]  " if timeout_count else ""
                    ) + (
                        f"[green]{statuses.count('completed')} completed[/green]" if statuses.count("completed") else ""
                    )
                    label_str = f"  [dim]{label}[/dim]" if label else ""
                    lines.append(
                        f"[bold cyan]{qdir.name}[/bold cyan]{label_str}\n"
                        f"  {len(results)} agents  {total_dur:.0f}s  {status_str}"
                    )
                except Exception:
                    lines.append(f"[bold cyan]{qdir.name}[/bold cyan]  [dim](unreadable)[/dim]")
            lines.append("\n[dim]Usage: /queue-results <label>  — show full results for a run[/dim]")
            console.print(Panel("\n".join(lines), title="Recent Queue Runs", border_style="cyan"))
        return True

    elif name == "/voice":
        # Accept: /voice [ptt|auto] [tools]  (order of ptt/auto and tools is flexible)
        flags = [p.lower() for p in parts[1:]]
        voice_mode  = next((f for f in flags if f in ("ptt", "auto")), VOICE_DEFAULT_MODE)
        use_tools   = "tools" in flags
        await _voice_conversation_loop(session, mode=voice_mode, use_tools=use_tools)
        return True

    elif name == "/schedule":
        scheduler = getattr(session, "_scheduler", None)
        if scheduler is None:
            console.print("[red]Scheduler not running (start via Qt GUI)[/red]")
            await _gui_text(session, "[red]Scheduler not running — start via the Qt GUI.[/red]")
            return True

        sub = parts[1].lower() if len(parts) > 1 else "list"

        if sub == "list":
            jobs = scheduler.list_jobs()
            if not jobs:
                console.print("No scheduled jobs.")
                await _gui_text(session, "[dim]No scheduled jobs.[/dim]")
            else:
                from rich.table import Table
                tbl = Table(title="Scheduled Jobs", border_style="cyan")
                tbl.add_column("ID",      style="bold cyan", no_wrap=True)
                tbl.add_column("Enabled", no_wrap=True)
                tbl.add_column("When",    no_wrap=True)
                tbl.add_column("TG User", no_wrap=True)
                tbl.add_column("Next Run", no_wrap=True)
                tbl.add_column("Runs", no_wrap=True)
                tbl.add_column("Task")
                for j in jobs:
                    tbl.add_row(
                        j["id"],
                        "[green]yes[/green]" if j.get("enabled") else "[red]no[/red]",
                        j.get("when", ""),
                        str(j.get("telegram_user_id", "")),
                        (j.get("next_run") or "[dim]—[/dim]"),
                        str(j.get("run_count", 0)),
                        j.get("task", ""),
                    )
                console.print(tbl)
                # GUI: render a proper styled HTML table instead of the garbled ASCII art
                _C = "#22d3ee"
                _hdr = (
                    f'<tr style="color:#6699cc;font-weight:600;border-bottom:1px solid {_C};">'
                    '<th style="padding:3px 8px;text-align:left;">ID</th>'
                    '<th style="padding:3px 8px;text-align:left;">En</th>'
                    '<th style="padding:3px 8px;text-align:left;">When</th>'
                    '<th style="padding:3px 8px;text-align:left;">TG</th>'
                    '<th style="padding:3px 8px;text-align:left;">Next Run</th>'
                    '<th style="padding:3px 8px;text-align:left;">×</th>'
                    '<th style="padding:3px 8px;text-align:left;">Task</th>'
                    '</tr>'
                )
                _rows = []
                for j in jobs:
                    _en_style = "color:#4ade80;" if j.get("enabled") else "color:#f87171;"
                    _en = "yes" if j.get("enabled") else "no"
                    _task = (j.get("task") or "")
                    _rows.append(
                        f'<tr style="border-top:1px solid #1a2a3a;">'
                        f'<td style="color:{_C};padding:3px 8px;white-space:nowrap;">{j["id"]}</td>'
                        f'<td style="{_en_style}padding:3px 8px;">{_en}</td>'
                        f'<td style="padding:3px 8px;">{j.get("when","")}</td>'
                        f'<td style="padding:3px 8px;">{j.get("telegram_user_id","")}</td>'
                        f'<td style="padding:3px 8px;white-space:nowrap;">{j.get("next_run") or "—"}</td>'
                        f'<td style="padding:3px 8px;">{j.get("run_count",0)}</td>'
                        f'<td style="padding:3px 8px;color:#aaaaaa;">{_task}</td>'
                        f'</tr>'
                    )
                _table_html = (
                    f'<div style="border-left:3px solid {_C};padding:7px 12px;margin:4px 0 8px 0;'
                    f'background:rgba(255,255,255,0.03);border-radius:3px;font-family:monospace;font-size:12px;">'
                    f'<div style="font-weight:600;color:{_C};margin-bottom:5px;letter-spacing:.04em;">Scheduled Jobs</div>'
                    f'<table style="width:100%;border-collapse:collapse;">{_hdr}{"".join(_rows)}</table>'
                    f'</div>'
                )
                if session.tui_queue is not None:
                    await session.tui_queue.put({"type": "system_html", "html": _table_html})
            return True

        if sub == "remove":
            if len(parts) < 3:
                console.print("[yellow]Usage: /schedule remove <id>[/yellow]")
                await _gui_text(session, "[yellow]Usage: /schedule remove <id>[/yellow]")
                return True
            job_id = parts[2]
            if scheduler.remove_job(job_id):
                console.print(f"[green]Job {job_id} removed.[/green]")
                await _gui_text(session, f"[green]Job [bold cyan]{job_id}[/bold cyan] removed.[/green]")
            else:
                console.print(f"[red]Job {job_id} not found.[/red]")
                await _gui_text(session, f"[red]Job {job_id} not found.[/red]")
            return True

        if sub == "enable":
            if len(parts) < 3:
                console.print("[yellow]Usage: /schedule enable <id>[/yellow]")
                await _gui_text(session, "[yellow]Usage: /schedule enable <id>[/yellow]")
                return True
            job_id = parts[2]
            if scheduler.set_enabled(job_id, True):
                console.print(f"[green]Job {job_id} enabled.[/green]")
                await _gui_text(session, f"[green]Job [bold cyan]{job_id}[/bold cyan] enabled.[/green]")
            else:
                console.print(f"[red]Job {job_id} not found.[/red]")
                await _gui_text(session, f"[red]Job {job_id} not found.[/red]")
            return True

        if sub == "disable":
            if len(parts) < 3:
                console.print("[yellow]Usage: /schedule disable <id>[/yellow]")
                await _gui_text(session, "[yellow]Usage: /schedule disable <id>[/yellow]")
                return True
            job_id = parts[2]
            if scheduler.set_enabled(job_id, False):
                console.print(f"[yellow]Job {job_id} disabled.[/yellow]")
                await _gui_text(session, f"[yellow]Job [bold cyan]{job_id}[/bold cyan] disabled.[/yellow]")
            else:
                console.print(f"[red]Job {job_id} not found.[/red]")
                await _gui_text(session, f"[red]Job {job_id} not found.[/red]")
            return True

        if sub == "run":
            if len(parts) < 3:
                console.print("[yellow]Usage: /schedule run <id>[/yellow]")
                await _gui_text(session, "[yellow]Usage: /schedule run <id>[/yellow]")
                return True
            job_id = parts[2]
            job = scheduler.get_job(job_id)
            if job is None:
                console.print(f"[red]Job {job_id} not found.[/red]")
                await _gui_text(session, f"[red]Job {job_id} not found.[/red]")
                return True
            console.print(f"[cyan]Firing job {job_id} immediately…[/cyan]")
            await _gui_text(session, f"[cyan]Firing job [bold]{job_id}[/bold] immediately…[/cyan]")
            asyncio.create_task(scheduler._fire_job(job), name=f"job-manual-{job_id}")
            return True

        # Otherwise: /schedule <when> <telegram_user_id> <task...>
        if len(parts) < 4:
            _usage = (
                "[yellow]Usage: /schedule <when> <telegram_user_id> <task...>\n"
                "  when: daily | daily:HH:MM | weekly:dow:HH:MM | YYYY-MM-DD | YYYY-MM-DD:HH:MM\n"
                "Sub-commands: list | remove <id> | enable <id> | disable <id> | run <id>[/yellow]"
            )
            console.print(_usage)
            await _gui_text(session, _usage)
            return True
        when_str  = parts[1]
        try:
            tg_id = int(parts[2])
        except ValueError:
            _err = f"[red]Invalid telegram_user_id: {parts[2]!r} (must be integer)[/red]"
            console.print(_err)
            await _gui_text(session, _err)
            return True
        task_text = " ".join(parts[3:])
        try:
            job = scheduler.add_job(when_str, tg_id, task_text)
            _msg = (
                f"[green]Job [bold cyan]{job['id']}[/bold cyan] created.[/green]\n"
                f"  [dim]When:[/dim] {job['when']}\n"
                f"  [dim]Next run:[/dim] {job.get('next_run') or 'N/A'}\n"
                f"  [dim]Task:[/dim] {task_text}"
            )
            console.print(
                f"[green]Job [bold]{job['id']}[/bold] created.[/green]\n"
                f"  When: {job['when']}\n"
                f"  Next run: {job.get('next_run') or 'N/A'}\n"
                f"  Task: {task_text}"
            )
            await _gui_text(session, _msg)
        except ValueError as e:
            console.print(f"[red]Invalid schedule: {e}[/red]")
            await _gui_text(session, f"[red]Invalid schedule: {e}[/red]")
        return True

    elif name == "/tasks":
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "clear":
            from tools import tool_task_list
            tasks_path = str(Path(session.cwd) / "TASKS.md")
            result = await tool_task_list("clear", path=tasks_path)
            console.print(f"[green]{result}[/green]")
            await _gui_text(session, f"[green]{result}[/green]")
        else:
            console.print("[yellow]Usage: /tasks clear[/yellow]")
            await _gui_text(session, "[yellow]Usage: /tasks clear[/yellow]")
        return True

    # Unknown /command — try skill lookup before giving up
    skill_name = name[1:]  # strip leading /
    skill_args = " ".join(parts[1:]) if len(parts) > 1 else ""
    found = await _invoke_skill(skill_name, skill_args, session)
    if found:
        return True
    console.print(f"[yellow]Unknown command: {name} (try /help or /skills)[/yellow]")
    return True

