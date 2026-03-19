"""
chat_tui.py — Textual TUI for Eli (Qwen3 local agent).

Replaces the Rich + prompt_toolkit display layer with a proper TUI:
  - Fixed header (model, mode, think level)
  - Scrollable chat area with live-updating tool panels
  - Multiline input with persistent history (same .chat_history file)
  - Fixed status bar (context window, compact mode, model)

Run with: .venv/Scripts/python.exe chat_tui.py
"""

# ── Venv guard ────────────────────────────────────────────────────────────────
import sys, pathlib
_expected_venv = pathlib.Path(__file__).parent / ".venv"
_running_in_venv = pathlib.Path(sys.prefix) == _expected_venv.resolve()
if not _running_in_venv:
    print(
        f"WARNING: not running in the project venv.\n"
        f"  Expected: {_expected_venv}\n"
        f"  Current:  {sys.prefix}\n"
        f"Use: .venv\\Scripts\\python.exe chat_tui.py",
        file=sys.stderr,
    )

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio
import json
from pathlib import Path
from typing import ClassVar

from rich.markup import escape as markup_escape

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.css.query import NoMatches
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, Static, TextArea

# ── History manager ───────────────────────────────────────────────────────────

HISTORY_FILE = Path(__file__).parent / ".chat_history"
_HISTORY_MAX = 1000


class HistoryManager:
    """Persistent input history — reads/writes the same file as prompt_toolkit."""

    def __init__(self, path: Path = HISTORY_FILE):
        self._path = path
        self._entries: list[str] = self._load()
        self._pos: int = len(self._entries)   # points past the end (no selection)
        self._draft: str = ""                 # saves in-progress input when navigating

    def _load(self) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        # prompt_toolkit stores one entry per line; de-duplicate, keep order
        seen: set[str] = set()
        entries: list[str] = []
        for line in lines:
            if line and line not in seen:
                seen.add(line)
                entries.append(line)
        return entries[-_HISTORY_MAX:]

    def add(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # Remove duplicate if already present, append to end
        self._entries = [e for e in self._entries if e != text]
        self._entries.append(text)
        if len(self._entries) > _HISTORY_MAX:
            self._entries = self._entries[-_HISTORY_MAX:]
        self._pos = len(self._entries)
        self._draft = ""
        try:
            self._path.write_text("\n".join(self._entries) + "\n", encoding="utf-8")
        except Exception:
            pass

    def prev(self, current: str) -> str | None:
        """Return the previous entry, saving current text as draft."""
        if not self._entries:
            return None
        if self._pos == len(self._entries):
            self._draft = current  # save draft before navigating
        new_pos = self._pos - 1
        if new_pos < 0:
            return None
        self._pos = new_pos
        return self._entries[self._pos]

    def next(self) -> str | None:
        """Return the next entry, or the saved draft if at the end."""
        new_pos = self._pos + 1
        if new_pos >= len(self._entries):
            self._pos = len(self._entries)
            return self._draft  # restore draft
        self._pos = new_pos
        return self._entries[self._pos]

    def reset(self) -> None:
        self._pos = len(self._entries)
        self._draft = ""


# ── Widgets ───────────────────────────────────────────────────────────────────

class EliHeader(Static):
    """Fixed top bar — model, mode, think level."""

    model_name: reactive[str] = reactive("connecting...")
    think_level: reactive[str] = reactive("on")
    mode: reactive[str] = reactive("normal")
    role: reactive[str] = reactive("eli")

    def render(self) -> str:
        role_part = f"  ·  role: {self.role}" if self.role != "eli" else ""
        return (
            f" [bold cyan]Eli[/bold cyan]"
            f"  ·  [dim]{self.model_name}[/dim]"
            f"  ·  think: [cyan]{self.think_level}[/cyan]"
            f"  ·  [dim]{self.mode}[/dim]"
            f"{role_part}"
        )


class StatusBar(Static):
    """Fixed bottom bar — context window usage and session info."""

    tokens_used: reactive[int] = reactive(0)
    ctx_window: reactive[int] = reactive(0)
    compact_mode: reactive[bool] = reactive(False)
    model_name: reactive[str] = reactive("")

    def render(self) -> str:
        if not self.tokens_used or not self.ctx_window:
            compact = "  [cyan][compact][/cyan]" if self.compact_mode else ""
            return f" [dim]ctx  no data yet{compact}[/dim]"

        pct = self.tokens_used / self.ctx_window
        bar_width = 24
        filled = min(bar_width, int(bar_width * pct))
        bar = "█" * filled + "░" * (bar_width - filled)

        if pct >= 0.8:
            bar_color = "bright_red"
        elif pct > 0.6:
            bar_color = "yellow"
        else:
            bar_color = "green"

        compact = "  [cyan][compact][/cyan]" if self.compact_mode else ""
        warn = "  [bold yellow]⚠ compact soon[/bold yellow]" if pct >= 0.75 else ""

        return (
            f" [bold]ctx[/bold]  [{bar_color}]{bar}[/{bar_color}]"
            f"  [dim]{self.tokens_used / 1000:.1f}k / {self.ctx_window / 1000:.0f}k"
            f"  ({pct * 100:.0f}%)[/dim]"
            f"  [dim]{self.model_name}[/dim]"
            f"{compact}{warn}"
        )


class MessageWidget(Static):
    """A single chat message — user or assistant."""

    DEFAULT_CSS = """
    MessageWidget {
        margin: 0 0 1 0;
    }
    """

    def __init__(self, role: str, content: str, source: str = "eli", **kwargs):
        super().__init__(**kwargs)
        self._role = role
        self._content = content
        self._source = source  # "eli" | "agent"
        self._build()

    def _build(self) -> None:
        from rich.panel import Panel
        from rich.markdown import Markdown
        from rich.text import Text

        if self._role == "user":
            panel = Panel(
                Text(self._content, style="white"),
                title="[dim]You[/dim]",
                border_style="dim blue",
                padding=(0, 1),
            )
        elif self._source == "agent":
            panel = Panel(
                Markdown(self._content),
                title="[dim yellow]Agent[/dim yellow]",
                border_style="yellow",
                padding=(0, 1),
            )
        else:
            panel = Panel(
                Markdown(self._content),
                title="[dim cyan]Eli[/dim cyan]",
                border_style="cyan",
                padding=(0, 1),
            )
        self.update(panel)

    def append_text(self, token: str) -> None:
        """Append a streaming text token and re-render."""
        self._content += token
        self._build()


class ToolPanel(Static):
    """A tool call panel — updates in place as the result streams in."""

    PENDING = "yellow"
    SUCCESS = "green"
    ERROR   = "red"

    DEFAULT_CSS = """
    ToolPanel {
        margin: 0 0 1 0;
    }
    """

    # Tools where we extract a key field for the title instead of showing raw JSON args
    _TITLE_FIELD: ClassVar[dict[str, str]] = {
        "web_search": "query",
        "grep":       "pattern",
        "glob":       "pattern",
        "read_file":  "path",
        "write_file": "path",
        "edit":       "path",
        "bash":       "command",
        "web_fetch":  "url",
    }

    def __init__(self, tool_id: str, name: str, args: str, **kwargs):
        super().__init__(**kwargs)
        self._tool_id   = tool_id
        self._name      = name
        self._args      = args
        self._result    = ""
        self._state     = "pending"   # pending | done | error
        # Extract a short label for the title from known arg fields
        try:
            _args_dict = json.loads(args) if args.strip() else {}
            _field = self._TITLE_FIELD.get(name)
            self._label = str(_args_dict.get(_field, ""))[:120] if _field else ""
        except Exception:
            self._label = ""
        self._redraw()

    def _redraw(self) -> None:
        from rich.panel import Panel

        color = self.PENDING if self._state == "pending" else (
            self.ERROR if self._state == "error" else self.SUCCESS
        )
        status = " [dim yellow](running)[/dim yellow]" if self._state == "pending" else ""
        label_suffix = f": [dim italic]{markup_escape(self._label)}[/dim italic]" if self._label else ""
        title  = f"[{color}]Tool — {self._name}[/{color}]{label_suffix}{status}"
        # When we have a label in the title, don't repeat the raw args in the body
        body_parts = [] if self._label else [f"[dim]{markup_escape(self._args[:200])}[/dim]"]
        if self._result:
            body_parts.append(markup_escape(self._result[:2000]) + ("…" if len(self._result) > 2000 else ""))
        panel = Panel(
            "\n".join(body_parts) if body_parts else " ",
            title=title,
            border_style=color,
            padding=(0, 1),
        )
        self.update(panel)

    def finalize(self, result: str, is_error: bool = False) -> None:
        """Called when the tool call completes."""
        self._result = result
        self._state  = "error" if is_error else "done"
        self._redraw()


class ThinkingWidget(Static):
    """Collapsible thinking block — shown while the model reasons."""

    DEFAULT_CSS = """
    ThinkingWidget {
        margin: 0 0 1 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._text = ""
        self._done = False
        self._redraw()

    def _redraw(self) -> None:
        from rich.panel import Panel
        from rich.text import Text
        title = "[dim cyan]Thinking[/dim cyan]" if not self._done else "[dim]Thought[/dim]"
        self.update(Panel(
            Text(self._text or "…", style="dim italic"),
            title=title,
            border_style="dim cyan",
            padding=(0, 1),
        ))

    def append(self, token: str) -> None:
        self._text += token
        self._redraw()

    def finalize(self) -> None:
        self._done = True
        self._redraw()


class EliTextArea(TextArea):
    """TextArea with custom Enter/Alt+Enter handling.

    Enter → submit message.
    Alt+Enter → insert newline (default TextArea behaviour reassigned).
    Up/Down on edge lines → history navigation.
    """

    # Override TextArea's default height:1fr — 1fr inside an auto-height parent
    # creates a circular layout dependency and causes get_height() to return None.
    DEFAULT_CSS = """
    EliTextArea {
        height: auto;
        max-height: 8;
        border: none;
        padding: 0;
        background: $background;
    }
    """

    def __init__(self, area_widget: "ChatInputArea", **kwargs):
        super().__init__(**kwargs)
        self._area = area_widget

    def _on_key(self, event) -> None:
        key = event.key

        if key == "enter":
            self.app._submit()
            event.prevent_default()   # stop TextArea inserting a newline
            event.stop()
            return

        if key == "alt+enter":
            self.insert("\n")
            event.prevent_default()
            event.stop()
            return

        if key == "ctrl+d":
            self.app.exit()
            event.stop()
            return

        if key == "ctrl+c":
            self.app._cancel_current()
            event.stop()
            return

        if key == "ctrl+o":
            self.app._toggle_compact()
            event.stop()
            return

        if key == "up" and self.cursor_at_first_line:
            prev = self._area._history.prev(self.text)
            if prev is not None:
                self.load_text(prev)
                self.move_cursor(self.document.end)
            event.stop()
            return

        if key == "down" and self.cursor_at_last_line:
            nxt = self._area._history.next()
            if nxt is not None:
                self.load_text(nxt)
                self.move_cursor(self.document.end)
            event.stop()
            return

        if key == "escape":
            self.app._cancel_current()
            event.stop()
            return

        # All other keys: default TextArea behaviour
        super()._on_key(event)


class ChatInputArea(Widget):
    """Multiline input wrapper."""

    DEFAULT_CSS = """
    ChatInputArea {
        height: auto;
        max-height: 10;
        border-top: solid $primary-darken-2;
        padding: 0 1;
    }
    """

    def __init__(self, history: HistoryManager, **kwargs):
        super().__init__(**kwargs)
        self._history = history

    def compose(self) -> ComposeResult:
        ta = EliTextArea(self, id="input-area")
        ta.show_line_numbers = False
        yield ta

    def get_text(self) -> str:
        return self.query_one("#input-area", EliTextArea).text

    def clear(self) -> None:
        self.query_one("#input-area", EliTextArea).load_text("")


class ChatLog(ScrollableContainer):
    """Scrollable chat message area."""

    DEFAULT_CSS = """
    ChatLog {
        height: 1fr;
        padding: 1 1 0 1;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._autoscroll = True

    def on_mount(self) -> None:
        # Poll at 20 Hz — keeps scroll_y pinned to the bottom while streaming
        # without racing the layout engine. Only does work when autoscroll is on
        # and there is actually something to scroll.
        self.set_interval(0.05, self._autoscroll_tick)

    def _autoscroll_tick(self) -> None:
        # Re-engage autoscroll when user has scrolled back to the bottom
        if not self._autoscroll and self.max_scroll_y > 1:
            if self.scroll_y >= self.max_scroll_y - 2:
                self._autoscroll = True
        # Pin to bottom while autoscroll is active
        if self._autoscroll and self.max_scroll_y > 0:
            self.scroll_end(animate=False)

    def on_mouse_scroll_up(self, event) -> None:
        """User scrolled up — suspend autoscroll."""
        self._autoscroll = False

    def scroll_to_end_if_following(self) -> None:
        """No-op — the tick loop handles scrolling."""
        pass

    def add_message(self, role: str, content: str) -> MessageWidget:
        w = MessageWidget(role, content)
        self.mount(w)
        return w

    def add_tool(self, tool_id: str, name: str, args: str) -> ToolPanel:
        w = ToolPanel(tool_id, name, args, id=f"tool-{tool_id}")
        self.mount(w)
        return w

    def get_tool(self, tool_id: str) -> ToolPanel | None:
        try:
            return self.query_one(f"#tool-{tool_id}", ToolPanel)
        except NoMatches:
            return None

    def add_thinking(self) -> ThinkingWidget:
        w = ThinkingWidget()
        self.mount(w)
        return w

    def add_system(self, text: str) -> None:
        from rich.text import Text
        w = Static(Text(f"  {text}", style="dim"), classes="system-msg")
        self.mount(w)


# ── Main App ──────────────────────────────────────────────────────────────────

class EliApp(App):
    """Eli — Textual TUI for Qwen3 local agent."""

    CSS = """
    Screen {
        layers: base;
    }
    EliHeader {
        height: 1;
        background: $primary-darken-3;
        color: $text;
        dock: top;
        padding: 0 1;
    }
    StatusBar {
        height: 1;
        background: $primary-darken-3;
        color: $text;
        dock: bottom;
        padding: 0 1;
    }
    ChatInputArea {
        dock: bottom;
        height: auto;
        max-height: 10;
        margin-bottom: 1;
    }
    .system-msg {
        margin: 0 0 1 0;
        color: $text-muted;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+d", "quit", "Quit"),
        Binding("ctrl+c", "clear_or_cancel", "Clear / Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    # ── Custom messages ────────────────────────────────────────────────────────
    class CancelRequest(Message):
        pass

    class SubmitMessage(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    # ── Init ──────────────────────────────────────────────────────────────────
    def __init__(self, session=None, **kwargs):
        super().__init__(**kwargs)
        self._session = session          # ChatSession (pre-connected, tui_queue set)
        self._busy = False               # True while model is running
        self._thinking_widget: ThinkingWidget | None = None
        self._assistant_widget: MessageWidget | None = None
        self._current_work = None

    # ── Compose ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        self._history = HistoryManager()
        yield EliHeader(id="eli-header")
        yield ChatLog(id="chat-log")
        yield ChatInputArea(self._history, id="input-area-wrapper")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        self.query_one("#input-area", EliTextArea).focus()
        log = self.query_one("#chat-log", ChatLog)
        if self._session:
            h = self.query_one("#eli-header", EliHeader)
            h.model_name  = self._session.model
            h.think_level = self._session.think_level
            h.role        = self._session.role
            sb = self.query_one("#status-bar", StatusBar)
            sb.model_name = self._session.model
            sb.ctx_window = self._session.ctx_window
            sb.compact_mode = self._session.compact_mode
            log.add_system(f"Connected to {self._session.model}  ·  ctx {self._session.ctx_window // 1000}k tokens")
            self._drain_events()
        else:
            log.add_system("Eli is starting... (stub mode — backend not yet connected)")
            self.query_one("#eli-header", EliHeader).model_name = "qwen3-30b-a3b"
            self.query_one("#status-bar", StatusBar).model_name = "qwen3-30b-a3b"

    # ── Event drain worker ─────────────────────────────────────────────────────
    @work(exclusive=False)
    async def _drain_events(self) -> None:
        """Continuously drain tui_queue and update widgets."""
        if not self._session or not self._session.tui_queue:
            return
        q = self._session.tui_queue
        log = self.query_one("#chat-log", ChatLog)

        while True:
            event = await q.get()
            etype = event.get("type")

            if etype == "think_token":
                if self._thinking_widget is None:
                    w = ThinkingWidget()
                    await log.mount(w)
                    self._thinking_widget = w
                self._thinking_widget.append(event["text"])
                log.scroll_to_end_if_following()

            elif etype == "text_token":
                if self._thinking_widget is not None:
                    self._thinking_widget.finalize()
                    self._thinking_widget = None
                if self._assistant_widget is None:
                    src = event.get("source", "eli")
                    w = MessageWidget("assistant", "", source=src)
                    await log.mount(w)
                    self._assistant_widget = w
                self._assistant_widget.append_text(event["text"])
                log.scroll_to_end_if_following()

            elif etype == "text_done":
                if self._thinking_widget is not None:
                    self._thinking_widget.finalize()
                    self._thinking_widget = None
                if self._assistant_widget is None and event.get("text"):
                    src = event.get("source", "eli")
                    w = MessageWidget("assistant", event["text"], source=src)
                    await log.mount(w)
                    log.scroll_to_end_if_following()
                self._assistant_widget = None

            elif etype == "tool_start":
                if self._thinking_widget is not None:
                    self._thinking_widget.finalize()
                    self._thinking_widget = None
                self._assistant_widget = None
                w = ToolPanel(event["id"], event["name"], event.get("args", ""),
                              id=f"tool-{event['id']}")
                await log.mount(w)
                log.scroll_to_end_if_following()

            elif etype == "tool_done":
                panel = log.get_tool(event["id"])
                if panel:
                    panel.finalize(event.get("result", ""), is_error=event.get("is_error", False))
                log.scroll_to_end_if_following()

            elif etype == "usage":
                sb = self.query_one("#status-bar", StatusBar)
                sb.tokens_used = event["tokens"]
                sb.ctx_window  = event["ctx"]

            elif etype == "system":
                from rich.text import Text
                w = Static(Text(f"  {event['text']}", style="dim"), classes="system-msg")
                await log.mount(w)
                log.scroll_to_end_if_following()

            elif etype == "done":
                self._busy = False
                self._thinking_widget = None
                self._assistant_widget = None
                log._autoscroll = True  # snap back to follow on completion

            elif etype == "error":
                from rich.text import Text
                w = Static(Text(f"  Error: {event.get('text', '?')}", style="dim red"), classes="system-msg")
                await log.mount(w)
                log.scroll_to_end_if_following()
                self._busy = False

            q.task_done()

    # ── Submit ─────────────────────────────────────────────────────────────────
    def _submit(self) -> None:
        if self._busy:
            self.notify("Eli is thinking… (Ctrl+C or Escape to cancel)", severity="warning")
            return
        wrapper = self.query_one("#input-area-wrapper", ChatInputArea)
        text = wrapper.get_text().strip()
        if not text:
            return
        wrapper.clear()
        self.query_one("#input-area", EliTextArea).focus()
        self._history.add(text)
        self._history.reset()

        if text.startswith("/"):
            self._run_slash(text)
            return

        log = self.query_one("#chat-log", ChatLog)
        log.add_message("user", text)
        self._busy = True
        self._thinking_widget = None
        self._assistant_widget = None
        self._current_work = self._send_to_session(text)

    @work(exclusive=False)
    async def _send_to_session(self, text: str) -> None:
        """Run send_and_stream in a background task."""
        if not self._session:
            return
        try:
            await self._session.send_and_stream(text)
        except Exception as e:
            if self._session.tui_queue:
                await self._session.tui_queue.put({"type": "error", "text": str(e)})
            self._busy = False

    @work(exclusive=False)
    async def _run_slash(self, text: str) -> None:
        """Run a slash command, capturing Rich renderables into a RichLog widget."""
        import chat as chat_module
        from chat import handle_slash_command

        # Shim that intercepts console.print() calls and collects renderables
        class _CapConsole:
            def __init__(self):
                self._items: list = []
            def print(self, *objects, sep=" ", end="\n", **kwargs):
                for obj in objects:
                    self._items.append(obj)
            def rule(self, *args, **kwargs):
                from rich.rule import Rule
                self._items.append(Rule(*args, **kwargs))
            # Satisfy any attribute access chat.py might do on the console
            def __getattr__(self, name):
                return lambda *a, **kw: None

        cap = _CapConsole()
        old_console = chat_module.console
        chat_module.console = cap
        try:
            await handle_slash_command(text, self._session)
        except Exception as e:
            cap._items.append(f"[red]Error: {e}[/red]")
        finally:
            chat_module.console = old_console

        log = self.query_one("#chat-log", ChatLog)
        if cap._items:
            from rich.console import Group
            w = Static(Group(*cap._items))
            w.styles.margin = (0, 0, 1, 0)
            await log.mount(w)

        self._sync_header()

    def _sync_header(self) -> None:
        """Re-read session state into header and status bar."""
        if not self._session:
            return
        h = self.query_one("#eli-header", EliHeader)
        h.model_name  = self._session.model
        h.think_level = self._session.think_level
        h.role        = self._session.role
        sb = self.query_one("#status-bar", StatusBar)
        sb.model_name   = self._session.model
        sb.compact_mode = self._session.compact_mode
        sb.ctx_window   = self._session.ctx_window

    def _toggle_compact(self) -> None:
        if not self._session:
            return
        self._session.compact_mode = not self._session.compact_mode
        self._sync_header()
        state = "on" if self._session.compact_mode else "off"
        self.notify(f"Compact mode {state}", severity="information")

    def _cancel_current(self) -> None:
        """Cancel any running model call and unblock input."""
        self._busy = False
        self._thinking_widget = None
        self._assistant_widget = None
        self.notify("Cancelled", severity="warning")
        # Drain any pending events so the queue doesn't replay stale state
        if self._session and self._session.tui_queue:
            q = self._session.tui_queue
            while not q.empty():
                try:
                    q.get_nowait()
                    q.task_done()
                except Exception:
                    break

    # ── Type-to-focus ──────────────────────────────────────────────────────────
    def on_key(self, event) -> None:
        """Any printable character typed outside the input field focuses it and types there."""
        ta = self.query_one("#input-area", EliTextArea)
        if self.focused is ta:
            return
        char = event.character
        if not char or not char.isprintable():
            return
        ta.focus()
        ta.insert(char)
        event.prevent_default()
        event.stop()

    # ── Key actions ───────────────────────────────────────────────────────────
    def action_clear_or_cancel(self) -> None:
        wrapper = self.query_one("#input-area-wrapper", ChatInputArea)
        text = wrapper.get_text()
        if text.strip():
            wrapper.clear()
        else:
            self._cancel_current()

    def action_cancel(self) -> None:
        self._cancel_current()

    # ── Status update helpers (called from backend) ────────────────────────────
    def update_status(self, tokens: int, ctx: int) -> None:
        bar = self.query_one("#status-bar", StatusBar)
        bar.tokens_used = tokens
        bar.ctx_window  = ctx

    def update_header(self, *, model: str | None = None,
                      think: str | None = None, mode: str | None = None,
                      role: str | None = None) -> None:
        h = self.query_one("#eli-header", EliHeader)
        if model is not None:
            h.model_name  = model
            self.query_one("#status-bar", StatusBar).model_name = model
        if think is not None:
            h.think_level = think
        if mode  is not None:
            h.mode        = mode
        if role  is not None:
            h.role        = role


# ── Entry point ───────────────────────────────────────────────────────────────
async def _run() -> None:
    """Connect the backend before Textual takes over the terminal."""
    # Import here to avoid circular issues at module level
    from chat import ChatSession

    async with ChatSession() as session:
        session.tui_queue = asyncio.Queue()
        app = EliApp(session=session)
        await app.run_async()


if __name__ == "__main__":
    asyncio.run(_run())
