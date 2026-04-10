"""
MainWindow - five-panel layout.
Panels: Explorer | Chat+Input | Editor | Server Stats
"""

import os
import sys
from pathlib import Path

# Ensure qt/ siblings are importable regardless of working directory
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import (
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QToolBar, QStatusBar, QComboBox, QLineEdit,
    QTreeView, QTabWidget, QTextBrowser, QPlainTextEdit, QTextEdit,
    QPushButton, QProgressBar, QMessageBox, QFileSystemModel,
    QScrollArea, QScrollBar, QGroupBox, QSlider, QSpinBox, QDialog,
    QDialogButtonBox, QListWidget, QListWidgetItem, QSizePolicy, QCheckBox,
    QStyledItemDelegate, QStyleOptionViewItem, QFileDialog,
)
from PySide6.QtCore import Qt, QThread, QTimer, QRect, QSize, Signal, Slot
from PySide6.QtGui import (
    QAction, QColor, QTextCharFormat, QTextCursor, QKeySequence, QShortcut,
    QPainter, QTextFormat, QLinearGradient, QBrush, QIcon,
)

import httpx

from colors import USER_COLOR, ASST_COLOR, BG_CODE, BORDER_CODE, ACCENT, TEXT_DIM, ELI_BORDER, ELI_BG, AGENT_BG, REMOTE_COLOR
from highlighter import SyntaxHighlighter, detect_language, highlight_code_html
from file_watcher import DirWatcher
from adapter import QtChatAdapter

try:
    from session_state import load_state, save_state, list_sessions, load_session, get_agent_name
    from slash_completer import SlashCompleter, load_skill_commands
except ImportError:
    from qt.session_state import load_state, save_state, list_sessions, load_session, get_agent_name
    from qt.slash_completer import SlashCompleter, load_skill_commands

HOME_DIR   = str(Path.home() / "claude-projects")
DRIVE_ROOT = ""   # "" = My Computer root (shows all drives)


# ── Explorer CWD delegate ─────────────────────────────────────────────────────

class _CwdDelegate(QStyledItemDelegate):
    """Renders the current-working-directory row bold purple in the explorer tree."""

    CWD_COLOR = "#a855f7"

    def __init__(self, fs_model: QFileSystemModel, get_cwd, parent=None):
        super().__init__(parent)
        self._fs_model = fs_model
        self._get_cwd  = get_cwd   # callable returning the current cwd string

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        path = self._fs_model.filePath(index)
        if os.path.normcase(path) == os.path.normcase(self._get_cwd()):
            option.font.setBold(True)
            option.palette.setColor(option.palette.ColorRole.Text, QColor(self.CWD_COLOR))


# ── Code editor with line numbers ────────────────────────────────────────────

class _LineNumberArea(QWidget):
    """Gutter widget that paints line numbers alongside a CodeEditor."""

    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor._gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor._paint_gutter(event)


class _KnightRiderBar(QWidget):
    """Bouncing red glow bar shown while the model is working."""
    _H      = 4
    _SPOT_W = 240
    _STEP   = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self._pos   = 0
        self._dir   = 1
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._active = True
        self._pos = 0
        self._dir = 1
        self._timer.start()
        self.update()

    def stop(self):
        self._active = False
        self._timer.stop()
        self.update()

    def _tick(self):
        w = self.width()
        self._pos += self._dir * self._STEP
        if self._pos + self._SPOT_W >= w:
            self._pos = w - self._SPOT_W
            self._dir = -1
        elif self._pos <= 0:
            self._pos = 0
            self._dir = 1
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor("#000000"))
            if not self._active:
                return
            grad = QLinearGradient(self._pos, 0, self._pos + self._SPOT_W, 0)
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(0.25, QColor(180, 10, 0, 200))
            grad.setColorAt(0.5,  QColor(255, 40, 0, 255))
            grad.setColorAt(0.75, QColor(180, 10, 0, 200))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(self._pos, 0, self._SPOT_W, self._H, QBrush(grad))
        finally:
            painter.end()


class _MarkedScrollBar(QScrollBar):
    """Vertical scrollbar subclass that paints tick marks over the native bar.

    Two independent mark sets, each with its own colour:
      - model highlights  (amber  #c8a000) — set via set_model_marks()
      - change highlights (green  #2d7a2d) — set via set_change_marks()
    """
    _MARK_H = 3

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Vertical, parent)
        self._model_ratios:  list[float] = []
        self._change_ratios: list[float] = []
        self._search_ratios: list[float] = []

    def set_model_marks(self, ratios: list[float]) -> None:
        self._model_ratios = ratios
        self.update()

    def set_change_marks(self, ratios: list[float]) -> None:
        self._change_ratios = ratios
        self.update()

    def set_search_marks(self, ratios: list[float]) -> None:
        self._search_ratios = ratios
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)          # native scrollbar renders first
        if not self._model_ratios and not self._change_ratios and not self._search_ratios:
            return
        from PySide6.QtGui import QPainter, QColor
        painter = QPainter(self)
        h = self.height()
        w = self.width()
        for r in self._change_ratios:
            y = max(0, min(h - self._MARK_H, int(r * h)))
            painter.fillRect(0, y, w, self._MARK_H, QColor("#2d7a2d"))
        for r in self._model_ratios:
            y = max(0, min(h - self._MARK_H, int(r * h)))
            painter.fillRect(0, y, w, self._MARK_H, QColor("#c8a000"))
        for r in self._search_ratios:
            y = max(0, min(h - self._MARK_H, int(r * h)))
            painter.fillRect(0, y, w, self._MARK_H, QColor("#e05c00"))


class _SlotPanel(QWidget):
    """Row of colored squares: green=free, red=in-use."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = 1
        self._in_use = 0
        self.setFixedHeight(14)
        self.setMinimumWidth(20)

    def update_slots(self, total: int, in_use: int) -> None:
        self._total = max(1, total)
        self._in_use = max(0, min(in_use, self._total))
        self.setFixedWidth(self._total * 12 - 2)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for i in range(self._total):
            color = QColor("#ef4444") if i < self._in_use else QColor("#22c55e")
            p.fillRect(i * 12, 0, 10, 14, color)


class _CtxBarsWidget(QWidget):
    """Fixed pool of context bars: slot 0 = Eli, slots 1..N = agent slots.
    Always visible; agent slots show zero when idle."""

    _CHUNK_CSS = (
        "QProgressBar {{ border: none; background: #1a1a1a; }}"
        "QProgressBar::chunk {{ background: {color}; }}"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._vl = QVBoxLayout(self)
        self._vl.setContentsMargins(0, 0, 0, 0)
        self._vl.setSpacing(2)
        # Each entry: (bar, val_lbl, name_lbl, row_widget)
        self._rows: list[tuple] = []
        self._tool_id_to_slot: dict[str, int] = {}  # tool_id → slot_index (for remove_agent)
        self._ensure_slots(1)   # always at least Eli

    def _make_row(self, name: str) -> tuple:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px;")
        name_lbl.setFixedWidth(72)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(5)
        val = QLabel("— / —")
        val.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px;")
        rl.addWidget(name_lbl)
        rl.addWidget(bar, stretch=1)
        rl.addWidget(val)
        self._vl.addWidget(row)
        self.updateGeometry()
        return bar, val, name_lbl, row

    def _ensure_slots(self, n: int):
        """Grow the pool to at least n rows (index 0 = Eli, 1..n-1 = agent slots)."""
        while len(self._rows) < n:
            idx = len(self._rows)
            label = "Eli" if idx == 0 else f"Slot {idx}"
            self._rows.append(self._make_row(label))

    def _slot_name(self, idx: int) -> str:
        return "Eli" if idx == 0 else f"Slot {idx}"

    def _set_bar(self, bar, val_lbl, tokens: int, ctx: int):
        if ctx <= 0:
            bar.setValue(0)
            bar.setStyleSheet(self._CHUNK_CSS.format(color="#7dff7d"))
            val_lbl.setText("— / —")
            return
        pct = min(int(tokens / ctx * 100), 100)
        color = "#ef4444" if pct >= 80 else "#fbbf24" if pct >= 60 else "#7dff7d"
        bar.setValue(pct)
        bar.setStyleSheet(self._CHUNK_CSS.format(color=color))
        val_lbl.setText(f"{tokens // 1000:.1f}k / {ctx // 1000:.0f}k ({pct}%)")

    def update_slot_count(self, total: int, _in_use: int):
        """Called from slots_updated signal — ensures we have enough bars."""
        self._ensure_slots(max(total, 1))

    def update_eli(self, tokens: int, ctx: int):
        bar, val, _, _ = self._rows[0]
        self._set_bar(bar, val, tokens, ctx)

    def update_slot(self, slot_index: int, tool_id: str, display_name: str, tokens: int, ctx: int):
        """Update a bar directly by ISM slot index."""
        if slot_index < 0:
            return
        self._ensure_slots(slot_index + 1)
        if tool_id:
            self._tool_id_to_slot[tool_id] = slot_index
        bar, val, name_lbl, _ = self._rows[slot_index]
        if display_name and slot_index > 0 and name_lbl.text() == self._slot_name(slot_index):
            name_lbl.setText(display_name[:16])
        self._set_bar(bar, val, tokens, ctx)

    def remove_agent(self, tool_id: str):
        slot_index = self._tool_id_to_slot.pop(tool_id, None)
        if slot_index is not None and slot_index < len(self._rows):
            bar, val, name_lbl, _ = self._rows[slot_index]
            name_lbl.setText(self._slot_name(slot_index))
            self._set_bar(bar, val, 0, 0)


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit extended with a line-number gutter."""

    excerpt_changed = Signal()   # emitted when a Ctrl+drag excerpt selection is finalized
    excerpt_cleared = Signal()   # emitted when a plain (non-Ctrl) click discards the excerpt
    editor_clicked  = Signal()   # emitted on any LMB press (used to clear model highlights)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gutter = _LineNumberArea(self)
        self._marker_bar = _MarkedScrollBar(self)
        self.setVerticalScrollBar(self._marker_bar)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter_on_scroll)
        self._update_gutter_width()
        self._excerpt_drag: bool = False   # True while Ctrl+LMB drag is in progress
        self._has_excerpt:  bool = False   # True after a Ctrl+drag excerpt is captured

    # ── Ctrl+LMB drag → excerpt selection ────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.editor_clicked.emit()
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._excerpt_drag = True
            else:
                self._excerpt_drag = False
                if self._has_excerpt:
                    self._has_excerpt = False
                    self.excerpt_cleared.emit()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if self._excerpt_drag and event.button() == Qt.MouseButton.LeftButton:
            self._excerpt_drag = False
            if self.textCursor().hasSelection():
                self._has_excerpt = True
                self.excerpt_changed.emit()

    # ── Gutter geometry ──────────────────────────────────────────────────────

    def _gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        char_w = self.fontMetrics().horizontalAdvance("9")
        return 8 + char_w * digits

    def _update_gutter_width(self, _=None) -> None:
        self.setViewportMargins(self._gutter_width(), 0, 0, 0)

    def _update_gutter_on_scroll(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._gutter.setGeometry(
            QRect(cr.left(), cr.top(), self._gutter_width(), cr.height())
        )

    # ── Gutter painting ──────────────────────────────────────────────────────

    def _paint_gutter(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor("#0f0f1a"))

        block     = self.firstVisibleBlock()
        block_num = block.blockNumber()
        top = round(
            self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        )
        bottom = top + round(self.blockBoundingRect(block).height())
        fm_height = self.fontMetrics().height()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                current = (block_num == self.textCursor().blockNumber())
                painter.setPen(QColor("#aaaaaa" if current else "#444466"))
                painter.drawText(
                    0, top,
                    self._gutter.width() - 4, fm_height,
                    Qt.AlignmentFlag.AlignRight,
                    str(block_num + 1),
                )
            block     = block.next()
            top       = bottom
            bottom    = top + round(self.blockBoundingRect(block).height())
            block_num += 1


class _ServerPollWorker(QThread):
    """Background thread for server health/stats polling."""
    polled = Signal(bool, str, str, int, str, int, int)

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self._base = base_url

    def run(self) -> None:
        running = False
        ctx_text = "Context: —"
        speed_text = "Speed: —"
        vram_pct = 0
        vram_label = "—"
        ctx  = 0
        used = 0
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
        self.polled.emit(running, ctx_text, speed_text, vram_pct, vram_label, ctx, used)


# ── Tool / agent display constants ────────────────────────────────────────────

_AGENT_PALETTE = [
    "#22d3ee",  # teal
    "#a3e635",  # lime
    "#fbbf24",  # amber
    "#f472b6",  # pink
    "#c084fc",  # purple
    "#fb923c",  # orange
    "#38bdf8",  # sky
    "#4ade80",  # green
]

_TOOL_COLOR = {
    "bash":         "#f59e0b",
    "read_file":    "#60a5fa",
    "write_file":   "#34d399",
    "edit":         "#34d399",
    "glob":         "#60a5fa",
    "grep":         "#60a5fa",
    "ripgrep":      "#60a5fa",
    "list_dir":     "#93c5fd",
    "web_fetch":    "#4ade80",
    "web_search":   "#4ade80",
    "spawn_agent":  None,       # assigned from _AGENT_PALETTE
    "queue_agents": None,
    "speak":        "#c084fc",
    "task_list":    "#fb923c",
    "analyze_image":"#f472b6",
}

_TOOL_ICON = {
    "bash":         "$",
    "read_file":    "r",
    "write_file":   "w",
    "edit":         "e",
    "glob":         "*",
    "grep":         "?",
    "ripgrep":      "?",
    "list_dir":     "/",
    "web_fetch":    "@",
    "web_search":   "@",
    "spawn_agent":  "▶",
    "queue_agents": "▶▶",
    "speak":        "♦",
    "task_list":    "T",
    "analyze_image":"img",
}

# Which argument to surface as the primary detail for each tool
_TOOL_KEY_ARG = {
    "bash":         "command",
    "web_search":   "query",
    "web_fetch":    "url",
    "read_file":    "path",
    "write_file":   "path",
    "edit":         "path",
    "glob":         "pattern",
    "grep":         "pattern",
    "ripgrep":      "pattern",
    "list_dir":     "path",
    "spawn_agent":  "task",
    "queue_agents": "label",
    "speak":        "text",
    "analyze_image":"image_path",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._current_file: str | None = None
        self._poll_worker: _ServerPollWorker | None = None
        self._cwd: str = HOME_DIR
        self._response_buf: str = ""
        self._agent_buf: str = ""
        self._current_agent_label: str = ""  # label of last agent that wrote to the tab
        self._file_touch_history: list[str] = []   # abs paths touched by tool calls, most recent first
        self._input_history: list[str] = []        # sent messages, newest first
        self._input_history_idx: int = -1          # -1 = not browsing history
        self._input_history_draft: str = ""        # saved draft while browsing
        self._plan_mode: bool = False
        self._busy: bool = False
        self._message_queue: list[tuple[str, bool]] = []  # (submit_text, plan_mode)
        self._agent_header_pending: bool = False
        self._response_anchor: int = 0   # char position after agent header in full_view
        self._agent_counter: int = 0     # cycles through _AGENT_PALETTE
        self._active_agents: dict[str, str] = {}   # tool_id → agent color
        self._agent_nesting: int = 0     # >0 means we're inside a spawned agent
        self._current_agent_color: str = "#22d3ee"  # color of innermost active agent
        self._pending_tools: dict[str, dict] = {}  # tool_id → buffered start metadata

        self._watcher = DirWatcher(self)

        # Adapter — starts its worker thread immediately
        self._adapter = QtChatAdapter(self)
        self._adapter.start()

        # Load persisted state
        _state = load_state()
        self._saved_state = _state           # kept for compaction spinbox defaults
        self._agent_name: str = get_agent_name(_state)
        self._saved_think: str = _state.get("think_level", "on")
        self._saved_approval: str = _state.get("approval_level", "auto")
        self._saved_compact: bool = bool(_state.get("compact_mode", False))
        self._compact_mode: bool = self._saved_compact

        self._build_menu()
        self._build_toolbar()
        self._build_panels()
        self._build_statusbar()
        self._wire_signals()

        self._watcher.set_cwd(self._cwd)
        self._update_status()

        # Restore last window geometry and panel layout
        from PySide6.QtCore import QByteArray
        geo = _state.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromHex(geo.encode()))
        spl = _state.get("splitter_state")
        if spl:
            self._splitter.restoreState(QByteArray.fromHex(spl.encode()))

    def closeEvent(self, event):
        save_state(
            window_geometry=self.saveGeometry().toHex().data().decode(),
            splitter_state=self._splitter.saveState().toHex().data().decode(),
        )
        self._adapter.shutdown()
        if not self._adapter.wait(5000):
            self._adapter.terminate()
        super().closeEvent(event)

    # ── Menu ─────────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        file_menu.addAction("Save Session As…", self._on_save_session)

        sessions_menu = mb.addMenu("Sessions")
        sessions_menu.addAction("New Session", self._on_new_session)
        sessions_menu.addAction("Resume Last", self._on_resume_last)
        sessions_menu.addAction("Browse Sessions…", self._on_browse_sessions)
        sessions_menu.addSeparator()
        sessions_menu.addAction("Browse Queue Results…", self._on_browse_queue_results)

        for name in ("Model", "Tools", "Skills", "Voice", "Help"):
            mb.addMenu(name)

    # ── Toolbar ──────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = QToolBar("Ribbon", self)
        tb.setMovable(False)
        tb.setStyleSheet(
            "QToolBar { spacing: 3px; }"
            "QToolBar QComboBox { padding: 1px 2px; }"
            "QToolBar QPushButton { padding: 1px 3px; }"
        )
        self.addToolBar(tb)

        self._server_status = QLabel("⬤")
        self._server_status.setStyleSheet("color: #ef4444; font-size: 14px;")
        self._server_status.setToolTip("Server status")
        tb.addWidget(self._server_status)
        tb.addSeparator()

        self._model_combo = QComboBox()
        self._model_combo.addItems(["local-model", "qwen3-coder-80b"])
        self._model_combo.setFixedWidth(120)
        self._model_combo.setToolTip("Model")
        tb.addWidget(self._model_combo)
        tb.addSeparator()

        self._think_combo = QComboBox()
        self._think_combo.addItems(["think:off", "think:on", "think:deep"])
        self._think_combo.setCurrentIndex(["off", "on", "deep"].index(self._saved_think))
        self._think_combo.setFixedWidth(100)
        self._think_combo.setToolTip("Thinking level")
        self._think_combo.currentIndexChanged.connect(
            lambda i: self._on_think_changed(["off", "on", "deep"][i])
        )
        tb.addWidget(self._think_combo)

        self._approval_combo = QComboBox()
        self._approval_combo.addItems(["ask:danger", "ask:writes", "ask:all", "ask:none"])
        _appr_map = {"auto": 0, "ask-writes": 1, "ask-all": 2, "yolo": 3}
        self._approval_combo.setCurrentIndex(_appr_map.get(self._saved_approval, 0))
        self._approval_combo.setFixedWidth(100)
        self._approval_combo.setToolTip(
            "ask:danger — only destructive/dangerous commands ask\n"
            "ask:writes — all file writes and bash ask\n"
            "ask:all    — every tool call asks\n"
            "ask:none   — nothing asks (yolo)"
        )
        _appr_vals = ["auto", "ask-writes", "ask-all", "yolo"]
        self._approval_combo.currentIndexChanged.connect(
            lambda i: self._on_approval_changed(_appr_vals[i])
        )
        tb.addWidget(self._approval_combo)
        tb.addSeparator()

        self._plan_btn = QPushButton("Plan")
        self._plan_btn.setCheckable(True)
        self._plan_btn.setFixedWidth(50)
        self._plan_btn.setToolTip("Plan mode — model describes actions without running tools")
        self._plan_btn.setStyleSheet("QPushButton:checked { background: #fbbf24; color: #1a1a2e; font-weight: bold; }")
        tb.addWidget(self._plan_btn)

        self._compact_btn = QPushButton("Compact")
        self._compact_btn.setCheckable(True)
        self._compact_btn.setChecked(self._saved_compact)
        self._compact_btn.setFixedWidth(100)
        self._compact_btn.setToolTip("Compact mode — hides thinking tokens")
        self._compact_btn.setStyleSheet("QPushButton:checked { background: #22d3ee; color: #1a1a2e; font-weight: bold; }")
        self._compact_btn.toggled.connect(self._on_compact_toggled)
        tb.addWidget(self._compact_btn)

        self._stop_btn = QPushButton("Abort Output")
        self._stop_btn.setFixedWidth(100)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setToolTip("Stop streaming (ESC)")
        self._stop_btn.clicked.connect(self._on_abort)
        tb.addWidget(self._stop_btn)
        tb.addSeparator()

        self._cwd_label = QLabel(f"  {Path(self._cwd).name}")
        self._cwd_label.setStyleSheet(f"color: {TEXT_DIM};")
        self._cwd_label.setToolTip(f"CWD: {self._cwd}")
        self._cwd_label.setMaximumWidth(300)
        tb.addWidget(self._cwd_label)
        tb.addSeparator()

        # ── Voice section ────────────────────────────────────────────────────
        self._voice_server_dot = QLabel("⬤")
        self._voice_server_dot.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        self._voice_server_dot.setToolTip("Voice server: unknown")
        tb.addWidget(self._voice_server_dot)

        self._voice_btn = QPushButton("🎤")
        self._voice_btn.setCheckable(True)
        self._voice_btn.setFixedWidth(32)
        self._voice_btn.setToolTip("Toggle voice mode")
        self._voice_btn.setStyleSheet(
            "QPushButton:checked { background: #22c55e; color: #1a1a2e; font-weight: bold; }"
        )
        self._voice_btn.toggled.connect(self._on_voice_toggled)
        tb.addWidget(self._voice_btn)

        self._voice_mode_combo = QComboBox()
        self._voice_mode_combo.addItems(["PTT", "Auto"])
        self._voice_mode_combo.setFixedWidth(52)
        self._voice_mode_combo.setToolTip("PTT = hold mic button  |  Auto = VAD-triggered")
        tb.addWidget(self._voice_mode_combo)

        self._mic_btn = QPushButton("🎙")
        self._mic_btn.setFixedWidth(30)
        self._mic_btn.setEnabled(False)
        self._mic_btn.setToolTip("Hold Insert key or this button to speak (PTT)")
        self._mic_btn.pressed.connect(self._adapter.voice_ptt_press)
        self._mic_btn.released.connect(self._adapter.voice_ptt_release)
        tb.addWidget(self._mic_btn)

        self._voice_activity_lbl = QLabel("Idle")
        self._voice_activity_lbl.setFixedWidth(72)
        self._voice_activity_lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        tb.addWidget(self._voice_activity_lbl)

        self._voice_autosend_cb = QCheckBox("Auto-Send")
        self._voice_autosend_cb.setChecked(True)
        self._voice_autosend_cb.setToolTip("Auto-submit transcribed text to chat")
        tb.addWidget(self._voice_autosend_cb)

        # Keep hidden server_url for compatibility with poll logic
        self._server_url = QLineEdit("localhost:1234")
        self._server_url.hide()

    # ── Panels ───────────────────────────────────────────────────────────────

    def _build_panels(self):
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._build_explorer())
        self._splitter.addWidget(self._build_chat_area())
        self._splitter.addWidget(self._build_editor())
        self._splitter.addWidget(self._build_server_stats())
        self._splitter.setSizes([180, 480, 480, 260])
        self.setCentralWidget(self._splitter)

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
        self._fs_model.setRootPath(DRIVE_ROOT)
        self._fs_model.setReadOnly(True)

        self._tree = QTreeView()
        self._tree.setModel(self._fs_model)
        self._tree.setRootIndex(self._fs_model.index(DRIVE_ROOT))
        self._tree.setHeaderHidden(True)
        for col in (1, 2, 3):
            self._tree.hideColumn(col)
        self._tree.doubleClicked.connect(self._on_tree_double_click)
        self._tree.clicked.connect(self._on_tree_click)
        self._tree.setItemDelegate(_CwdDelegate(self._fs_model, lambda: self._cwd, self._tree))
        layout.addWidget(self._tree)
        return w

    # ── Chat + Input ─────────────────────────────────────────────────────────

    def _build_chat_area(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # _compact_view kept as a hidden sink so legacy insert calls are harmless
        self._compact_view = QTextBrowser()
        self._compact_view.setOpenExternalLinks(False)

        self._full_view = QTextBrowser()
        self._full_view.setOpenExternalLinks(False)
        self._full_view.setOpenLinks(False)
        self._full_view.viewport().installEventFilter(self)

        self._agent_view = QTextBrowser()
        self._agent_view.setOpenExternalLinks(False)
        self._agent_view.setOpenLinks(False)

        self._chat_tabs = QTabWidget()
        self._chat_tabs.addTab(self._full_view, "Chat")
        self._chat_tabs.addTab(self._agent_view, "Agent")
        layout.addWidget(self._chat_tabs, stretch=1)

        # Per-slot context bars (above input) — Eli row always visible, agent rows added dynamically
        self._ctx_bars = _CtxBarsWidget()
        ctx_wrap = QWidget()
        ctx_wl = QHBoxLayout(ctx_wrap)
        ctx_wl.setContentsMargins(6, 2, 6, 2)
        ctx_wl.addWidget(self._ctx_bars)
        layout.addWidget(ctx_wrap)

        # Input area
        input_container = QWidget()
        input_container.setStyleSheet(
            "QWidget { background: #0f0f1f;"
            " border-top: 1px solid #2a2a4a;"
            " border-bottom: 1px solid #2a2a4a; }"
            "QPlainTextEdit { background: #0d0d1a; border: 1px solid #2a2a4a;"
            " border-radius: 2px; }"
        )
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(6, 8, 6, 8)
        input_layout.setSpacing(6)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Type a message… (Enter to send, Shift+Enter for newline, / for commands)")
        self._input.setFixedHeight(90)
        self._input.installEventFilter(self)
        input_layout.addWidget(self._input, stretch=1)

        send_col = QVBoxLayout()
        send_col.setContentsMargins(0, 0, 0, 0)
        send_col.setSpacing(4)

        self._editor_ctx_cb = QCheckBox("File ctx")
        self._editor_ctx_cb.setChecked(False)
        self._editor_ctx_cb.setToolTip("Include open file path in every message sent to the model")
        self._editor_ctx_cb.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        send_col.addWidget(self._editor_ctx_cb, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(60)
        self._send_btn.clicked.connect(self._send_message)
        send_col.addWidget(self._send_btn)

        self._queue_label = QLabel("")
        self._queue_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._queue_label.setStyleSheet(f"color:#fbbf24;font-size:10px;")
        self._queue_label.setVisible(False)
        send_col.addWidget(self._queue_label)

        input_layout.addLayout(send_col)

        layout.addWidget(input_container)

        self._kr_bar = _KnightRiderBar()
        layout.addWidget(self._kr_bar)
        layout.addSpacing(8)

        # Slash completer (positioned dynamically above input)
        self._completer = SlashCompleter(self)
        _skills_dir = str(Path(__file__).parent.parent / "skills")
        self._completer.add_commands(load_skill_commands(_skills_dir))
        self._completer.hide()
        self._completer.command_chosen.connect(self._on_command_chosen)
        self._completer.session_chosen.connect(self._on_session_chosen)

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

        _qt_dir = str(Path(__file__).parent).replace("\\", "/")
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search…")
        self._search_input.setClearButtonEnabled(True)
        self._search_input.setFixedWidth(180)
        self._search_input.setStyleSheet(
            "QLineEdit { background: #1a1a2e; color: #cccccc; border: 1px solid #444;"
            " border-radius: 3px; padding: 2px 4px; }"
            "QLineEdit:focus { border-color: #e05c00; }"
        )
        header_layout.addWidget(self._search_input)

        self._search_prev_btn = QPushButton()
        self._search_prev_btn.setIcon(QIcon(f"{_qt_dir}/arrow_up.svg"))
        self._search_prev_btn.setFixedSize(22, 22)
        self._search_prev_btn.setToolTip("Previous match (Shift+Enter)")
        header_layout.addWidget(self._search_prev_btn)

        self._search_next_btn = QPushButton()
        self._search_next_btn.setIcon(QIcon(f"{_qt_dir}/arrow_down.svg"))
        self._search_next_btn.setFixedSize(22, 22)
        self._search_next_btn.setToolTip("Next match (Enter)")
        header_layout.addWidget(self._search_next_btn)

        self._search_count_label = QLabel("")
        self._search_count_label.setStyleSheet(f"color: {TEXT_DIM}; min-width: 44px;")
        header_layout.addWidget(self._search_count_label)

        self._new_btn = QPushButton("New")
        self._new_btn.setFixedWidth(50)
        self._new_btn.clicked.connect(self._new_file)
        header_layout.addWidget(self._new_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFixedWidth(60)
        self._save_btn.setEnabled(False)
        self._save_btn.clicked.connect(self._save_file)
        header_layout.addWidget(self._save_btn)

        self._close_btn = QPushButton("Close")
        self._close_btn.setFixedWidth(60)
        self._close_btn.setEnabled(False)
        self._close_btn.clicked.connect(self._close_file)
        header_layout.addWidget(self._close_btn)
        layout.addWidget(header)

        self._editor = CodeEditor()
        self._editor.setReadOnly(False)
        self._editor.modificationChanged.connect(self._on_editor_modified)
        self._editor.cursorPositionChanged.connect(self._editor._gutter.update)
        self._highlighter: SyntaxHighlighter | None = None
        self._change_sels: list = []
        self._excerpt_sels: list = []     # ExtraSelections for the red excerpt highlight
        self._highlight_sels: list = []   # ExtraSelections for model-requested yellow highlights
        self._search_sels: list = []      # ExtraSelections for search matches
        self._search_cursors: list = []   # QTextCursors for each search match
        self._search_idx: int = -1        # index of current (highlighted) match
        self._pending_excerpt: str = ""   # silent excerpt prepended on send (Ctrl+drag)
        self._editor.excerpt_changed.connect(self._on_excerpt_changed)
        self._editor.excerpt_cleared.connect(self._clear_excerpt)

        layout.addWidget(self._editor, stretch=1)
        return w

    # ── Right Panel ───────────────────────────────────────────────────────────

    def _build_server_stats(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: #0f0f1a; border: none;")

        inner = QWidget()
        inner.setMinimumWidth(250)
        inner.setStyleSheet(
            "background: #0f0f1a;"
            "QGroupBox { font-size: 10px; font-weight: bold; color: #888;"
            "  padding-top: 16px; margin-top: 4px; border: 1px solid #2a2a3e; border-radius: 3px; }"
            "QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;"
            "  left: 6px; top: 1px; }"
        )
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)

        # ── SERVER ────────────────────────────────────────────────────────────
        srv = QGroupBox("SERVER")
        srv_l = QVBoxLayout(srv)
        srv_l.setContentsMargins(6, 20, 6, 20)
        srv_l.setSpacing(4)
        self._stat_status = QLabel("● Unknown")
        self._stat_status.setStyleSheet("color: #888;")
        srv_l.addWidget(self._stat_status)
        url_row = QWidget()
        url_rl = QHBoxLayout(url_row)
        url_rl.setContentsMargins(0, 0, 0, 0)
        url_rl.setSpacing(4)
        self._server_url_panel = QLineEdit("localhost:1234")
        url_rl.addWidget(self._server_url_panel, stretch=1)
        connect_btn = QPushButton("Connect")
        connect_btn.setFixedWidth(100)
        connect_btn.clicked.connect(self._on_server_connect)
        url_rl.addWidget(connect_btn)
        srv_l.addWidget(url_row)
        slot_row = QWidget()
        slot_rl = QHBoxLayout(slot_row)
        slot_rl.setContentsMargins(0, 0, 0, 0)
        slot_rl.setSpacing(6)
        slot_lbl = QLabel("Inference Slots:")
        slot_lbl.setStyleSheet("color: #888888;")
        self._slot_panel = _SlotPanel()
        slot_rl.addWidget(slot_lbl)
        slot_rl.addWidget(self._slot_panel)
        slot_rl.addStretch()
        srv_l.addWidget(slot_row)
        layout.addWidget(srv)

        # ── CONTEXT ───────────────────────────────────────────────────────────
        ctx = QGroupBox("CONTEXT")
        ctx_l = QVBoxLayout(ctx)
        ctx_l.setContentsMargins(6, 20, 6, 20)
        ctx_l.setSpacing(4)
        self._ctx_bar = QProgressBar()
        self._ctx_bar.setRange(0, 100)
        self._ctx_bar.setValue(0)
        self._ctx_bar.setTextVisible(False)
        self._ctx_bar.setFixedHeight(8)
        ctx_l.addWidget(self._ctx_bar)
        self._ctx_label = QLabel("— / — tokens")
        self._ctx_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        ctx_l.addWidget(self._ctx_label)
        self._ctx_warn = QLabel("⚠ compact soon")
        self._ctx_warn.setStyleSheet("color: #fbbf24; font-size: 10px;")
        self._ctx_warn.setVisible(False)
        ctx_l.addWidget(self._ctx_warn)
        compact_now = QPushButton("Compact Now")
        compact_now.clicked.connect(lambda: self._adapter.submit_slash("/compact"))
        ctx_l.addWidget(compact_now)
        layout.addWidget(ctx)

        # ── SPEED ─────────────────────────────────────────────────────────────
        spd = QGroupBox("SPEED")
        spd_l = QVBoxLayout(spd)
        spd_l.setContentsMargins(6, 20, 6, 20)
        spd_l.setSpacing(2)
        self._stat_speed = QLabel("— t/s")
        spd_l.addWidget(self._stat_speed)
        layout.addWidget(spd)

        # ── SESSION ───────────────────────────────────────────────────────────
        ses = QGroupBox("SESSION")
        ses_l = QVBoxLayout(ses)
        ses_l.setContentsMargins(6, 20, 6, 20)
        ses_l.setSpacing(2)
        self._stat_msgs = QLabel("Messages: —")
        ses_l.addWidget(self._stat_msgs)
        self._stat_role = QLabel("Role: —")
        self._stat_role.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        ses_l.addWidget(self._stat_role)
        layout.addWidget(ses)

        # ── AGENT ─────────────────────────────────────────────────────────────
        agt = QGroupBox("AGENT")
        agt_l = QFormLayout(agt)
        agt_l.setSpacing(4)
        agt_l.setContentsMargins(6, 20, 6, 20)
        self._agent_name_edit = QLineEdit(self._agent_name)
        self._agent_name_edit.editingFinished.connect(self._on_agent_name_edited)
        agt_l.addRow("Name:", self._agent_name_edit)
        self._role_combo = QComboBox()
        self._role_combo.addItem("eli")
        agents_dir = Path(__file__).parent.parent / "agents"
        if agents_dir.exists():
            for p in sorted(agents_dir.glob("*.md")):
                if p.stem != "eli":
                    self._role_combo.addItem(p.stem)
        self._role_combo.currentTextChanged.connect(self._on_role_changed)
        agt_l.addRow("Role:", self._role_combo)
        layout.addWidget(agt)

        # ── COMPACTION SETTINGS ───────────────────────────────────────────────
        cmp = QGroupBox("COMPACTION")
        cmp_l = QFormLayout(cmp)
        cmp_l.setSpacing(3)
        cmp_l.setContentsMargins(6, 20, 6, 20)

        thr_row = QWidget()
        thr_rl = QHBoxLayout(thr_row)
        thr_rl.setContentsMargins(0, 0, 0, 0)
        thr_rl.setSpacing(4)
        self._compact_slider = QSlider(Qt.Orientation.Horizontal)
        self._compact_slider.setRange(50, 95)
        self._compact_slider.setSingleStep(5)
        self._compact_slider.setValue(int(self._saved_state.get("compact_threshold", 80)))
        self._compact_slider_label = QLabel(f"{self._compact_slider.value()}%")
        self._compact_slider_label.setFixedWidth(30)
        self._compact_slider.valueChanged.connect(
            lambda v: (self._compact_slider_label.setText(f"{v}%"),
                       self._on_compact_threshold_changed(v)))
        thr_rl.addWidget(self._compact_slider, stretch=1)
        thr_rl.addWidget(self._compact_slider_label)
        cmp_l.addRow("Threshold:", thr_row)

        _qt_dir = str(Path(__file__).parent).replace("\\", "/")
        _spin_qss = (
            f"QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;"
            f" width: 16px; border-left: 1px solid #333333; }}"
            f"QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;"
            f" width: 16px; border-left: 1px solid #333333; }}"
            f"QSpinBox::up-arrow {{ image: url({_qt_dir}/arrow_up.svg); width: 8px; height: 5px; }}"
            f"QSpinBox::down-arrow {{ image: url({_qt_dir}/arrow_down.svg); width: 8px; height: 5px; }}"
        )

        self._keep_recent_spin = QSpinBox()
        self._keep_recent_spin.setRange(1, 20)
        self._keep_recent_spin.setValue(int(self._saved_state.get("keep_recent", 6)))
        self._keep_recent_spin.setMinimumWidth(70)
        self._keep_recent_spin.setStyleSheet(_spin_qss)
        self._keep_recent_spin.valueChanged.connect(self._on_keep_recent_changed)
        cmp_l.addRow("Keep recent:", self._keep_recent_spin)

        self._input_limit_spin = QSpinBox()
        self._input_limit_spin.setRange(1000, 50000)
        self._input_limit_spin.setSingleStep(500)
        self._input_limit_spin.setValue(int(self._saved_state.get("input_compress_limit", 8000)))
        self._input_limit_spin.setMinimumWidth(80)
        self._input_limit_spin.setStyleSheet(_spin_qss)
        self._input_limit_spin.valueChanged.connect(self._on_input_limit_changed)
        cmp_l.addRow("Input limit:", self._input_limit_spin)
        layout.addWidget(cmp)

        # ── PROJECT ───────────────────────────────────────────────────────────
        self._project_group = QGroupBox("PROJECT")
        proj_l = QVBoxLayout(self._project_group)
        proj_l.setContentsMargins(6, 20, 6, 20)
        proj_l.setSpacing(2)
        self._proj_name_lbl  = QLabel("")
        self._proj_build_lbl = QLabel("")
        self._proj_test_lbl  = QLabel("")
        self._proj_build_lbl.setWordWrap(True)
        self._proj_test_lbl.setWordWrap(True)
        for lbl in (self._proj_name_lbl, self._proj_build_lbl, self._proj_test_lbl):
            lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
            proj_l.addWidget(lbl)
        layout.addWidget(self._project_group)
        self._refresh_project_panel()

        layout.addStretch()
        scroll.setWidget(inner)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_server)
        self._poll_timer.start()
        return scroll

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
        self._adapter.think_token.connect(self._on_think_token)
        self._adapter.text_token.connect(self._on_text_token)
        self._adapter.text_done.connect(self._on_text_done)
        self._adapter.tool_start.connect(self._on_tool_start)
        self._adapter.tool_done.connect(self._on_tool_done_signal)
        self._adapter.approval_needed.connect(self._on_approval_needed)
        self._adapter.usage.connect(self._on_usage)
        self._adapter.agent_usage.connect(self._on_agent_usage)
        self._adapter.system_msg.connect(self._on_system_msg)
        self._adapter.system_html.connect(self._on_system_html)
        self._adapter.error_msg.connect(self._on_error_msg)
        self._adapter.done.connect(self._on_turn_done)
        self._adapter.clear_chat.connect(self._on_clear_chat)
        self._adapter.cwd_changed.connect(self._on_cwd_changed)
        self._adapter.session_saved.connect(self._on_session_saved)
        self._adapter.session_resume_html.connect(self._on_session_resume_html)

        # Align session CWD with GUI initial CWD on startup
        self._adapter.submit_slash(f"/cd {self._cwd}")

        # SP3 additions
        self._adapter.stream_started.connect(self._on_stream_started)
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.activated.connect(self._on_esc)
        self._plan_btn.toggled.connect(lambda checked: setattr(self, '_plan_mode', checked))
        self._input.textChanged.connect(self._on_input_changed)

        # SP6 voice
        self._adapter.voice_activity.connect(self._on_voice_activity)
        self._adapter.voice_transcript.connect(self._on_voice_transcript)
        self._adapter.voice_server_status.connect(self._on_voice_server_status)

        # Agent sub-stream
        self._adapter.agent_text_token.connect(self._on_agent_text_token)

        # Remote HTTP bridge
        self._adapter.remote_message.connect(self._on_remote_message)

        # Background agents
        self._adapter.slots_updated.connect(self._slot_panel.update_slots)
        self._adapter.slots_updated.connect(self._ctx_bars.update_slot_count)
        self._adapter.bg_agents_complete.connect(self._on_bg_agents_complete)

        # Editor navigation and highlights from model
        self._adapter.open_in_editor.connect(self._on_open_in_editor)
        self._adapter.highlight_in_editor.connect(self._on_highlight_in_editor)

        # Editor search bar
        self._search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self._search_shortcut.activated.connect(self._focus_search)
        self._search_input.textChanged.connect(self._update_search)
        self._search_prev_btn.clicked.connect(self._search_prev)
        self._search_next_btn.clicked.connect(self._search_next)
        self._search_input.installEventFilter(self)

    # ── Event filter ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        w = self._full_view.viewport().width()
        if w > 0:
            self._full_view.document().setTextWidth(w)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if hasattr(self, "_search_input") and obj is self._search_input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._search_input.clear()
                self._editor.setFocus()
                return True
            if key == Qt.Key.Key_Return:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._search_prev()
                else:
                    self._search_next()
                return True

        if hasattr(self, "_input") and obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()

            # Insert key — eat it entirely; pynput uses it for PTT, Qt would toggle overwrite mode
            if key == Qt.Key.Key_Insert:
                return True

            # Shift+Enter → newline
            if key == Qt.Key.Key_Return and (mods & Qt.KeyboardModifier.ShiftModifier):
                self._input.insertPlainText("\n")
                return True

            # Enter (no shift) → send
            if key == Qt.Key.Key_Return and not (mods & Qt.KeyboardModifier.ShiftModifier):
                if self._completer.isVisible():
                    self._completer.select_current()
                else:
                    self._send_message()
                return True

            # Arrow keys navigate completer or input history
            if self._completer.isVisible():
                if key == Qt.Key.Key_Up:
                    self._completer.move_selection(-1)
                    return True
                if key == Qt.Key.Key_Down:
                    self._completer.move_selection(1)
                    return True
                if key == Qt.Key.Key_Escape:
                    self._completer.hide()
                    return True

            if key == Qt.Key.Key_Up and self._input_history:

                cursor_rect = self._input.cursorRect()
                cursor_y = cursor_rect.top() + self._input.verticalScrollBar().value()
                line_height = self._input.fontMetrics().lineSpacing()
                on_first_line = cursor_y < line_height

                if on_first_line:
                    if self._input_history_idx == -1:
                        self._input_history_draft = self._input.toPlainText()
                    next_idx = self._input_history_idx + 1
                    if next_idx < len(self._input_history):
                        self._input_history_idx = next_idx
                        self._input.setPlainText(self._input_history[next_idx])
                        self._input.moveCursor(self._input.textCursor().MoveOperation.End)
                    return True

            if key == Qt.Key.Key_Down and self._input_history_idx >= 0:
                # Get current cursor vertical position
                current_cursor_y = self._input.cursorRect().top() + self._input.verticalScrollBar().value()
                
                # Get the vertical position of the last character in the document
                last_cursor = self._input.textCursor()
                last_cursor.movePosition(last_cursor.MoveOperation.End)
                last_line_y = self._input.cursorRect(last_cursor).top() + self._input.verticalScrollBar().value()
                
                # Trigger if the cursor is on or below the last visual line
                on_last_line = current_cursor_y >= last_line_y
                
                if on_last_line:
                    next_idx = self._input_history_idx - 1
                    if next_idx < 0:
                        self._input_history_idx = -1
                        self._input.setPlainText(self._input_history_draft)
                    else:
                        self._input_history_idx = next_idx
                        self._input.setPlainText(self._input_history[next_idx])
                    self._input.moveCursor(self._input.textCursor().MoveOperation.End)
                    return True

        # Keep document text width in sync with viewport — required for width:100% tables
        from PySide6.QtCore import QEvent
        if obj is self._full_view.viewport() and event.type() == QEvent.Type.Resize:
            w = self._full_view.viewport().width()
            if w > 0:
                self._full_view.document().setTextWidth(w)

        # Ctrl+LMB on _full_view viewport — open file path links
        if (obj is self._full_view.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            href = self._full_view.anchorAt(event.pos())
            if href.startswith("eli://open/"):
                raw = href[len("eli://open/"):]
                path, line = self._resolve_file_path(raw)
                if path:
                    self._on_open_in_editor(path, line)
                else:
                    self._status_bar.showMessage(f"File not found: {raw}", 3000)
                return True

        return super().eventFilter(obj, event)

    # ── Editor search ─────────────────────────────────────────────────────────

    def _apply_all_sels(self) -> None:
        """Merge all ExtraSelection lists and apply them to the editor."""
        self._editor.setExtraSelections(
            self._change_sels + self._excerpt_sels + self._highlight_sels + self._search_sels
        )

    def _build_search_sels(self) -> None:
        """Rebuild _search_sels from _search_cursors, highlighting _search_idx differently."""
        fmt_all = QTextCharFormat()
        fmt_all.setBackground(QColor("#7a3000"))   # dim orange — all matches
        fmt_cur = QTextCharFormat()
        fmt_cur.setBackground(QColor("#e05c00"))   # bright orange — current match

        sels = []
        for i, cur in enumerate(self._search_cursors):
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            sel.format = fmt_cur if i == self._search_idx else fmt_all
            sels.append(sel)
        self._search_sels = sels

    def _update_search(self, text: str) -> None:
        """Find all occurrences of text in the editor, build highlights and scrollbar marks."""
        doc = self._editor.document()
        self._search_cursors = []
        self._search_idx = -1

        if text:
            cursor = doc.find(text)
            while not cursor.isNull():
                self._search_cursors.append(QTextCursor(cursor))
                cursor = doc.find(text, cursor)
            if self._search_cursors:
                self._search_idx = 0

        self._build_search_sels()
        self._apply_all_sels()
        self._update_search_scrollbar()
        self._update_search_count()
        if self._search_idx >= 0:
            self._scroll_to_current_match()

    def _search_next(self) -> None:
        if not self._search_cursors:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_cursors)
        self._build_search_sels()
        self._apply_all_sels()
        self._scroll_to_current_match()
        self._update_search_count()

    def _search_prev(self) -> None:
        if not self._search_cursors:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_cursors)
        self._build_search_sels()
        self._apply_all_sels()
        self._scroll_to_current_match()
        self._update_search_count()

    def _scroll_to_current_match(self) -> None:
        if 0 <= self._search_idx < len(self._search_cursors):
            self._editor.setTextCursor(self._search_cursors[self._search_idx])
            self._editor.ensureCursorVisible()

    def _update_search_scrollbar(self) -> None:
        total = max(self._editor.document().blockCount(), 1)
        ratios = sorted({cur.blockNumber() / total for cur in self._search_cursors})
        self._editor._marker_bar.set_search_marks(ratios)

    def _update_search_count(self) -> None:
        n = len(self._search_cursors)
        text = self._search_input.text()
        if not text:
            self._search_count_label.setText("")
        elif n == 0:
            self._search_count_label.setText("0/0")
        else:
            self._search_count_label.setText(f"{self._search_idx + 1}/{n}")

    def _focus_search(self) -> None:
        self._search_input.setFocus()
        self._search_input.selectAll()

    def _resolve_file_path(self, raw: str) -> tuple[str, int]:
        """Resolve a raw path token (from a chat link) to (absolute_path, line). Returns ('', 1) if not found."""
        line = 1
        # Strip optional :N line suffix
        if ":" in raw:
            head, tail = raw.rsplit(":", 1)
            if tail.isdigit():
                raw, line = head, int(tail)

        # 1. Absolute path
        p = Path(raw)
        if p.is_absolute() and p.exists():
            return str(p), line

        # 2. Relative to CWD
        p = Path(self._cwd) / raw
        if p.exists():
            return str(p.resolve()), line

        # 3. Basename match against touch history (most recent first)
        name = Path(raw).name
        for touched in self._file_touch_history:
            if Path(touched).name == name:
                return touched, line

        # 4. Recursive glob from CWD
        matches = list(Path(self._cwd).rglob(name))
        if len(matches) == 1:
            return str(matches[0].resolve()), line
        if len(matches) > 1:
            # Show a small popup menu to pick
            from PySide6.QtWidgets import QMenu
            menu = QMenu(self)
            for m in matches:
                action = menu.addAction(str(m.relative_to(self._cwd) if m.is_relative_to(self._cwd) else m))
                action.setData(str(m.resolve()))
            chosen = menu.exec(self._full_view.mapToGlobal(self._full_view.rect().center()))
            if chosen:
                return chosen.data(), line

        return "", line

    # ── Slots ────────────────────────────────────────────────────────────────

    @Slot()
    def _send_message(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._input_history_idx = -1
        self._input_history_draft = ""
        if not self._input_history or self._input_history[0] != text:
            self._input_history.insert(0, text)
        if text.startswith("/"):
            self._adapter.submit_slash(text)
            return
        prefix = (f"[Editor: {self._current_file}]\n"
                  if self._current_file and self._editor_ctx_cb.isChecked() else "")
        submit_text = prefix + (self._pending_excerpt + text if self._pending_excerpt else text)
        self._clear_excerpt()
        self._clear_model_highlight()
        if self._busy:
            self._message_queue.append((submit_text, self._plan_mode))
            self._queue_label.setText(f"↑{len(self._message_queue)} queued")
            self._queue_label.setVisible(True)
            # Show a dim preview of the queued message in chat
            self._append_user(text, queued=True)
            return
        self._set_input_enabled(False)
        self._append_user(text)
        self._full_view.verticalScrollBar().setValue(
            self._full_view.verticalScrollBar().maximum()
        )
        self._response_buf = ""
        self._agent_buf = ""
        self._current_agent_label = ""
        self._full_view.append("")
        self._adapter.submit(submit_text, self._plan_mode)

    @Slot(str)
    def _on_think_token(self, token: str):
        if getattr(self, "_compact_mode", False):
            return   # suppress thinking display in compact mode
        _insert_plain(self._full_view, token)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_text_token(self, token: str):
        # Buffer only — do not insert into view so tool/agent panels are not wiped
        self._response_buf += token

    @Slot(str)
    def _on_agent_text_token(self, token: str, label: str = ""):
        if label and label != self._current_agent_label:
            self._current_agent_label = label
            sep = f'<p style="color:#555;font-size:10px;margin:4px 0;">── {label} ──</p>'
            self._agent_view.append(sep)
        self._agent_buf += token
        _insert_plain(self._agent_view, token)
        self._auto_scroll(self._agent_view)

    @Slot(str)
    def _on_text_done(self, full_text: str):
        rendered = _markdown_to_html(full_text)
        # Use table pattern: narrow colored left cell = border stripe; background on content cell.
        # Qt's QTextDocument does not support border-left or background on <div>.
        self._full_view.document().setTextWidth(self._full_view.viewport().width())
        self._full_view.append(
            f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:2px 0;">'
            f'<tr>'
            f'<td width="3" style="background:{ELI_BORDER};padding:0;vertical-align:top;"></td>'
            f'<td width="100%" style="background:{ELI_BG};padding:4px 10px;">'
            f'<span style="color:{ELI_BORDER};font-weight:bold;font-size:11px;">Eli</span><br>'
            f'{rendered}'
            f'</td></tr></table>'
        )
        self._full_view.append("")   # escape trailing table frames
        self._full_view.verticalScrollBar().setValue(self._full_view.verticalScrollBar().maximum())

    @Slot(str, str, str)
    def _on_tool_start(self, tool_id: str, name: str, args: str):
        import json
        try:
            a = json.loads(args) if args else {}
        except Exception:
            a = {}

        # Track files touched by tool calls for path resolution
        if name in ("read_file", "write_file", "edit", "grep"):
            raw = a.get("path", "")
            if raw:
                p = Path(raw) if os.path.isabs(raw) else Path(self._cwd) / raw
                abs_path = str(p.resolve())
                if abs_path in self._file_touch_history:
                    self._file_touch_history.remove(abs_path)
                self._file_touch_history.insert(0, abs_path)

        # Pick color — agents get a palette slot, others get a fixed category color
        if name in ("spawn_agent", "queue_agents"):
            color = _AGENT_PALETTE[self._agent_counter % len(_AGENT_PALETTE)]
            _ag_display = a.get("system_prompt", "") or f"Agent {self._agent_counter + 1}"
            self._active_agents[tool_id] = (color, tool_id)
            self._agent_counter += 1
            self._agent_nesting += 1
            self._current_agent_color = color

            # Clear agent view and highlight Agent tab in amber
            self._agent_view.clear()
            self._agent_buf = ""
            self._current_agent_label = ""
            self._chat_tabs.tabBar().setTabTextColor(1, QColor("#fbbf24"))

            # Special framing: yellow stripe, dark yellow bg, magenta bold label + full task text
            _AGENT_STRIPE = "#fbbf24"
            _AGENT_DISPATCH_BG = "#131000"
            _AGENT_LABEL_COLOR = "#e040fb"
            task_raw = str(a.get("task", a.get("tasks", ""))).replace("\n", " ")
            task_safe = task_raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            agent_num = self._agent_counter
            html = (
                f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:3px 0;">'
                f'<tr>'
                f'<td width="3" style="background:{_AGENT_STRIPE};padding:0;vertical-align:top;"></td>'
                f'<td width="100%" style="background:{_AGENT_DISPATCH_BG};padding:4px 10px;">'
                f'<span style="color:{_AGENT_LABEL_COLOR};font-weight:bold;">Agent {agent_num}:</span> '
                f'<span style="color:#cccccc;">{task_safe}</span>'
                f'</td></tr></table>'
            )
            self._full_view.append(html)
            self._auto_scroll(self._full_view)
            return
        else:
            # Non-agent tools: buffer metadata — render as a single combined line on tool_done
            if self._agent_nesting > 0:
                color = self._current_agent_color
                indent = "padding-left:22px;"
                bg = AGENT_BG
            else:
                color = _TOOL_COLOR.get(name, TEXT_DIM)
                indent = ""
                bg = ELI_BG
            icon   = _TOOL_ICON.get(name, "⚙")
            keyarg = _TOOL_KEY_ARG.get(name)
            detail = str(a.get(keyarg, ""))[:120].replace("\n", " ") if keyarg and keyarg in a else ""
            detail_safe = detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            self._pending_tools[tool_id] = {
                "name": name, "icon": icon, "color": color,
                "bg": bg, "indent": indent, "detail": detail_safe,
            }

    @Slot(str, str, str, bool)
    def _on_tool_done_signal(self, tool_id: str, name: str, result: str, is_error: bool):
        if name in ("write_file", "edit") and not is_error:
            self._clear_model_highlight()
        _agent_entry = self._active_agents.pop(tool_id, None)
        agent_color = (_agent_entry[0] if isinstance(_agent_entry, tuple) else _agent_entry)
        is_agent_tool = name in ("spawn_agent", "queue_agents")

        if is_agent_tool:
            self._agent_nesting = max(0, self._agent_nesting - 1)
            if result.startswith("[background:"):
                # Placeholder tool_done — agent is still running in background.
                # Keep the context bar slot alive so usage events can update it.
                # The real tool_done from _run_background_agent will call remove_agent.
                return
            self._ctx_bars.remove_agent(tool_id)
            color = agent_color or "#22d3ee"
            indent = ""
            bg = AGENT_BG
            icon      = "✗" if is_error else "✓"
            err_color = "#ef4444" if is_error else color
            done_html = (
                f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:1px 0 3px 0;">'
                f'<tr>'
                f'<td width="2" style="background:{err_color};padding:0;vertical-align:top;"></td>'
                f'<td width="100%" style="background:{bg};padding:1px 8px;font-family:Consolas,monospace;font-size:11px;">'
                f'<span style="color:{err_color};">{icon} {name}</span>'
                f'</td></tr></table>'
            )
            self._full_view.append(done_html)
        else:
            # Pop buffered start metadata and render a single combined line
            pending = self._pending_tools.pop(tool_id, None)
            if pending:
                color  = pending["color"]
                bg     = pending["bg"]
                indent = pending["indent"]
                icon   = pending["icon"]
                detail = pending["detail"]
            else:
                # Fallback if start was never buffered
                color  = _TOOL_COLOR.get(name, TEXT_DIM)
                bg     = AGENT_BG if self._agent_nesting > 0 else ELI_BG
                indent = "padding-left:22px;" if self._agent_nesting > 0 else ""
                icon   = _TOOL_ICON.get(name, "⚙")
                detail = ""
            outcome_icon  = "✗" if is_error else "✓"
            outcome_color = "#ef4444" if is_error else color
            detail_span   = f' <span style="color:#555577;">{detail}</span>' if detail else ""
            done_html = (
                f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:1px 0 2px 0;">'
                f'<tr>'
                f'<td width="2" style="background:{outcome_color};padding:0;vertical-align:top;"></td>'
                f'<td width="100%" style="background:{bg};padding:2px 8px;{indent}font-family:Consolas,monospace;font-size:11px;">'
                f'<span style="color:{color};">{icon} {name}</span>'
                f'{detail_span}'
                f' <span style="color:{outcome_color};">{outcome_icon}</span>'
                f'</td></tr></table>'
            )
            self._full_view.append(done_html)

        # Render agent output with its assigned color and full markdown
        if is_agent_tool:
            ac = agent_color or "#22d3ee"
            label_color = "#ef4444" if is_error else ac
            label = "▶ Agent error" if is_error else "▶ Agent result"
            body = _markdown_to_html(result) if result.strip() else (
                '<span style="color:#555566;font-style:italic;">(no text output)</span>'
            )
            self._full_view.document().setTextWidth(self._full_view.viewport().width())
            self._full_view.append(
                f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:2px 0;">'
                f'<tr>'
                f'<td width="3" style="background:{label_color};padding:0;vertical-align:top;"></td>'
                f'<td width="100%" style="background:{AGENT_BG};padding:4px 10px;">'
                f'<span style="color:{label_color};font-weight:bold;">{label}</span><br>'
                f'{body}'
                f'</td></tr></table>'
            )
            self._agent_buf = ""
            self._current_agent_label = ""
            self._chat_tabs.tabBar().setTabTextColor(1, QColor())

        self._full_view.verticalScrollBar().setValue(self._full_view.verticalScrollBar().maximum())

    @Slot(str, str, str, str)
    def _on_approval_needed(self, title: str, message: str, tool_name: str, args_str: str):
        import json as _json
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
            QPushButton, QFrame,
        )

        # --- generate a session rule suggestion ---
        try:
            args = _json.loads(args_str) if args_str else {}
        except Exception:
            args = {}
        path = args.get("path", "")
        cmd  = args.get("command", "")
        if tool_name in ("edit", "write_file") and path:
            rule = f"path_prefix:{self._cwd}"
            rule_label = f"Allow all edits/writes in CWD ({self._cwd})"
        elif tool_name == "bash" and cmd:
            first = cmd.strip().split()[0]
            rule = f"cmd_pattern:{first}*"
            rule_label = f"Allow all commands starting with '{first}'"
        else:
            rule = f"tool:{tool_name}" if tool_name else ""
            rule_label = f"Allow all '{tool_name}' calls" if tool_name else ""

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(520)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(10)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(message)
        txt.setMaximumHeight(200)
        txt.setStyleSheet("background:#1a1a1a; color:#d4d4d4; border:1px solid #333; font-size:12px;")
        layout.addWidget(txt)

        if rule_label:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); sep.setStyleSheet("color:#333;")
            layout.addWidget(sep)
            hint = QLabel(f"Session rule: <b>{rule_label}</b>")
            hint.setStyleSheet("color:#a0a0a0; font-size:11px;")
            layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_once    = QPushButton("Allow once")
        btn_session = QPushButton("Allow for session")
        btn_deny    = QPushButton("Deny")
        btn_once.setStyleSheet("background:#1e3a1e; color:#7dff7d; border:1px solid #3a5a3a; padding:6px 14px;")
        btn_session.setStyleSheet("background:#1a2a3a; color:#7dd3fc; border:1px solid #2a4a6a; padding:6px 14px;")
        btn_deny.setStyleSheet("background:#3a1e1e; color:#f87171; border:1px solid #5a2a2a; padding:6px 14px;")
        if not rule:
            btn_session.setEnabled(False)
        btn_row.addWidget(btn_deny)
        btn_row.addStretch()
        btn_row.addWidget(btn_once)
        btn_row.addWidget(btn_session)
        layout.addLayout(btn_row)

        result = {"approved": False, "notes": ""}
        btn_once.clicked.connect(lambda: (result.update({"approved": True, "notes": ""}), dlg.accept()))
        btn_session.clicked.connect(lambda: (result.update({"approved": True, "notes": f"session_allow:{rule}"}), dlg.accept()))
        btn_deny.clicked.connect(dlg.reject)

        dlg.exec()
        self._adapter.resolve_approval(result["approved"], result["notes"])

    @Slot(int, int)
    def _on_usage(self, tokens: int, ctx: int):
        self._stat_msgs.setText(f"Tokens: {tokens:,}")
        if ctx > 0:
            pct = int(tokens / ctx * 100)
            chunk_css = (
                "QProgressBar::chunk { background: #ef4444; }" if pct >= 80 else
                "QProgressBar::chunk { background: #fbbf24; }" if pct >= 60 else
                "QProgressBar::chunk { background: #7dff7d; }"
            )
            self._ctx_bar.setValue(pct)
            self._ctx_label.setText(f"{tokens // 1000:.1f}k / {ctx // 1000:.0f}k tokens ({pct}%)")
            self._ctx_bar.setStyleSheet(chunk_css)
            self._ctx_warn.setVisible(pct >= 75)
            self._ctx_bars.update_eli(tokens, ctx)
        else:
            self._ctx_bar.setValue(0)
            self._ctx_label.setText("— / — tokens")
            self._ctx_bars.update_eli(0, 0)

    @Slot(str, str, int, int)
    def _on_agent_usage(self, slot_index: int, tool_id: str, label: str, tokens: int, ctx: int):
        self._ctx_bars.update_slot(slot_index, tool_id, label or "Agent", tokens, ctx)

    @Slot(str)
    def _on_system_msg(self, text: str):
        self._status_bar.showMessage(text.splitlines()[0] if text else "", 4000)
        html = _markdown_to_html(text)
        self._compact_view.append(html)
        self._full_view.append(html)
        self._auto_scroll(self._compact_view)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_system_html(self, html: str):
        """Display pre-formatted HTML from slash command output in the chat views."""
        self._compact_view.append(html)
        self._full_view.append(html)
        self._auto_scroll(self._compact_view)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_error_msg(self, msg: str):
        self._compact_view.append(f'<span style="color:#ef4444;">Error: {msg}</span><br>')
        self._message_queue.clear()
        self._queue_label.setVisible(False)
        self._set_input_enabled(True)

    @Slot()
    def _on_clear_chat(self):
        self._full_view.clear()
        self._agent_view.clear()

    def _on_session_saved(self, json_path: str) -> None:
        """Save the current chat view HTML alongside the session JSON."""
        try:
            html_path = Path(json_path).with_suffix(".html")
            html_path.write_text(self._full_view.toHtml(), encoding="utf-8")
        except Exception:
            pass

    def _on_session_resume_html(self, json_path: str) -> None:
        """Restore chat view from saved HTML on session resume."""
        html_path = Path(json_path).with_suffix(".html")
        if not html_path.exists():
            return
        try:
            html = html_path.read_text(encoding="utf-8")
        except Exception:
            return
        self._full_view.setHtml(html)
        # Append a visual separator so new messages are clearly distinct
        self._full_view.append(
            "<hr style='border:none;border-top:1px solid #333;margin:6px 0;'>"
            "<p style='color:#444;font-size:10px;text-align:center;margin:2px 0;'>"
            "─── resume point ───</p>"
        )
        # Scroll to bottom so the resume point is visible
        sb = self._full_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_cwd_changed(self, cwd: str):
        if not cwd or not Path(cwd).is_dir():
            return
        self._cwd = cwd
        self._cwd_label.setText(f"  {Path(cwd).name}")
        self._cwd_label.setToolTip(f"CWD: {cwd}")
        self._watcher.set_cwd(cwd)
        self._refresh_project_panel()
        # Scroll the explorer tree to show the new CWD
        idx = self._fs_model.index(cwd)
        if idx.isValid():
            self._tree.setCurrentIndex(idx)
            self._tree.scrollTo(idx)
            self._tree.expand(idx)
            self._tree.viewport().update()

    @Slot()
    def _on_turn_done(self):
        self._full_view.append("<br>")
        self._plan_mode = False
        self._plan_btn.setChecked(False)
        self._stop_btn.setEnabled(False)
        self._update_status()
        self._refresh_session_panel()
        if self._message_queue:
            submit_text, plan_mode = self._message_queue.pop(0)
            n = len(self._message_queue)
            self._queue_label.setText(f"↑{n} queued" if n else "")
            self._queue_label.setVisible(n > 0)
            self._response_buf = ""
            self._agent_buf = ""
            self._current_agent_label = ""
            self._full_view.append("")
            self._adapter.submit(submit_text, plan_mode)
            # stay busy — _set_input_enabled(False) was already called
        else:
            self._set_input_enabled(True)
        # If voice mode ended internally (error/stop), un-check the button
        if self._voice_btn.isChecked():
            self._voice_btn.blockSignals(True)
            self._voice_btn.setChecked(False)
            self._voice_btn.blockSignals(False)
            self._mic_btn.setEnabled(False)
            self._voice_mode_combo.setEnabled(True)

    # ── Voice slots ──────────────────────────────────────────────────────────

    @Slot(bool)
    def _on_voice_toggled(self, checked: bool):
        if checked:
            mode = self._voice_mode_combo.currentText().lower()
            self._adapter.voice_start(mode)
            self._mic_btn.setEnabled(mode == "ptt")
            self._voice_mode_combo.setEnabled(False)
        else:
            self._adapter.voice_stop()
            self._mic_btn.setEnabled(False)
            self._voice_mode_combo.setEnabled(True)

    @Slot(str)
    def _on_voice_activity(self, state: str):
        self._voice_activity_lbl.setText(state)
        _colors = {
            "Idle":         TEXT_DIM,
            "Listening":    "#ef4444",
            "Transcribing": "#fbbf24",
            "Thinking":     "#7dff7d",
            "Speaking":     "#22d3ee",
        }
        color = _colors.get(state, TEXT_DIM)
        self._voice_activity_lbl.setStyleSheet(f"color: {color}; font-size: 10px;")

    @Slot(bool)
    def _on_voice_server_status(self, found: bool):
        if found:
            self._voice_server_dot.setStyleSheet("color: #22c55e; font-size: 10px;")
            self._voice_server_dot.setToolTip("Voice server: connected (port 1236)")
        else:
            self._voice_server_dot.setStyleSheet("color: #fbbf24; font-size: 10px;")
            self._voice_server_dot.setToolTip("Voice server: not found — using local Whisper")

    @Slot(str)
    def _on_voice_transcript(self, text: str):
        """Put transcript in the chat input field; auto-send if checkbox is checked."""
        self._input.setPlainText(text)
        if self._voice_autosend_cb.isChecked():
            self._send_btn.click()

    @Slot()
    def _on_tree_click(self, index):
        path = self._fs_model.filePath(index)
        self._status_bar.showMessage(path, 3000)

    @Slot()
    def _on_tree_double_click(self, index):
        path = self._fs_model.filePath(index)
        if os.path.isdir(path):
            self._cwd = path
            self._cwd_label.setText(f"  {Path(path).name}")
            self._cwd_label.setToolTip(f"CWD: {path}")
            self._watcher.set_cwd(path)
            self._update_status()
            self._refresh_project_panel()
            self._tree.viewport().update()
            self._adapter.submit_slash(f"/cd {path}")
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
        self._close_btn.setEnabled(True)
        # Re-run any active search against the new document
        if hasattr(self, "_search_input"):
            self._update_search(self._search_input.text())

    @Slot()
    def _new_file(self):
        if self._editor.document().isModified():
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "Discard changes and open a new file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        if self._current_file:
            self._watcher.unwatch_file(self._current_file)
        self._current_file = None
        if self._highlighter:
            self._highlighter.setDocument(None)
            self._highlighter = None
        self._editor.setPlainText("")
        self._editor.document().setModified(False)
        self._editor_label.setText("untitled")
        self._save_btn.setEnabled(True)
        self._close_btn.setEnabled(True)
        self._clear_model_highlight()
        self._clear_excerpt()

    @Slot()
    def _save_file(self):
        if not self._current_file:
            # New unsaved file — show Save As dialog
            path, _ = QFileDialog.getSaveFileName(
                self, "Save File", HOME_DIR, "All Files (*)"
            )
            if not path:
                return
            self._current_file = path
            self._watcher.watch_file(path)
            lang = detect_language(path)
            if self._highlighter:
                self._highlighter.setDocument(None)
            self._highlighter = SyntaxHighlighter(self._editor.document(), lang)
        with open(self._current_file, "w", encoding="utf-8") as f:
            f.write(self._editor.toPlainText())
        self._editor.document().setModified(False)
        self._editor_label.setText(os.path.basename(self._current_file))

    @Slot()
    def _close_file(self):
        if self._editor.document().isModified():
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "Close without saving?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        if self._current_file:
            self._watcher.unwatch_file(self._current_file)
        self._current_file = None
        if self._highlighter:
            self._highlighter.setDocument(None)
            self._highlighter = None
        self._editor.setPlainText("")
        self._editor.document().setModified(False)
        self._editor_label.setText("No file open")
        self._save_btn.setEnabled(False)
        self._close_btn.setEnabled(False)
        self._clear_model_highlight()
        self._clear_excerpt()

    @Slot(bool)
    def _on_editor_modified(self, modified: bool):
        if not self._current_file:
            self._editor_label.setText("*untitled" if modified else "untitled")
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
        # Save the line number at the centre of the viewport so we can restore it after reload.
        center_cursor = self._editor.cursorForPosition(
            self._editor.viewport().rect().center()
        )
        center_line = center_cursor.blockNumber()

        old_text = self._editor.toPlainText()
        self._load_file(path)
        new_text = self._editor.toPlainText()

        # Restore scroll: move cursor to saved line and centre it in the viewport.
        doc = self._editor.document()
        block = doc.findBlockByNumber(
            min(center_line, doc.blockCount() - 1)
        )
        if block.isValid():
            cur = QTextCursor(block)
            self._editor.setTextCursor(cur)
            self._editor.centerCursor()

        self._highlight_changes(old_text, new_text)

    @Slot()
    def _poll_server(self):
        if self._poll_worker is not None and self._poll_worker.isRunning():
            return
        base = f"http://{self._server_url.text().strip()}"
        self._poll_worker = _ServerPollWorker(base, parent=self)
        self._poll_worker.polled.connect(self._on_poll_result)
        self._poll_worker.start()

    @Slot(bool, str, str, int, str, int, int)
    def _on_poll_result(self, running: bool, ctx_text: str, speed_text: str,
                        vram_pct: int, vram_label: str, ctx: int, used: int):
        if running:
            self._stat_status.setText("● Running")
            self._stat_status.setStyleSheet("color: #7dff7d;")
            self._server_status.setStyleSheet("color: #7dff7d; font-size: 14px;")
            if ctx > 0:
                self._ctx_bars.update_eli(used, ctx)
        else:
            self._stat_status.setText("● Offline")
            self._stat_status.setStyleSheet("color: #ef4444;")
            self._server_status.setStyleSheet("color: #ef4444; font-size: 14px;")
            self._ctx_bars.update_eli(0, 0)
        self._stat_speed.setText(speed_text)

    @Slot(str)
    def _on_think_changed(self, val: str):
        if hasattr(self, '_adapter') and self._adapter is not None:
            self._adapter.submit_slash(f"/think {val}")
        save_state(think_level=val)
        self._update_status()

    @Slot(str)
    def _on_approval_changed(self, val: str):
        if hasattr(self, '_adapter') and self._adapter is not None:
            self._adapter.submit_slash(f"/approval {val}")
        save_state(approval_level=val)
        self._update_status()

    @Slot()
    def _on_server_connect(self):
        """Sync server URL from right panel into toolbar and trigger a poll."""
        url = self._server_url_panel.text().strip()
        self._server_url.setText(url)
        self._poll_server()

    @Slot()
    def _on_agent_name_edited(self):
        name = self._agent_name_edit.text().strip() or "Assistant"
        self._agent_name = name
        save_state(agent_name=name)

    @Slot(str)
    def _on_role_changed(self, role: str):
        self._adapter.submit_slash(f"/role {role}")
        self._stat_role.setText(f"Role: {role}")

    @Slot(int)
    def _on_compact_threshold_changed(self, value: int):
        save_state(compact_threshold=value)

    @Slot(int)
    def _on_keep_recent_changed(self, value: int):
        save_state(keep_recent=value)

    @Slot(int)
    def _on_input_limit_changed(self, value: int):
        save_state(input_compress_limit=value)

    @Slot()
    def _on_stream_started(self):
        self._stop_btn.setEnabled(True)
        self._agent_header_pending = True
        self._agent_buf = ""
        self._current_agent_label = ""
        self._clear_change_highlights()

    @Slot()
    def _on_abort(self):
        self._message_queue.clear()
        self._queue_label.setVisible(False)
        self._adapter.cancel()

    @Slot()
    def _on_esc(self):
        if self._completer.isVisible():
            self._completer.hide()
        elif self._stop_btn.isEnabled():
            self._on_abort()
        else:
            self._input.clear()

    @Slot(bool)
    def _on_compact_toggled(self, checked: bool):
        # Compact toggle suppresses thinking tokens in the display (display-side only).
        # It does NOT call /compact (which is a one-shot summarisation command).
        self._compact_mode = checked
        save_state(compact_mode=checked)
        self._update_status()

    @Slot(str)
    def _on_command_chosen(self, cmd: str):
        """Paste chosen slash command into input and move cursor to end."""
        if cmd == "/excerpt":
            self._insert_excerpt()
            return
        self._input.setPlainText(cmd + " ")
        cursor = self._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._input.setTextCursor(cursor)
        self._input.setFocus()

    @Slot(str)
    def _on_session_chosen(self, stem: str):
        """Submit /resume SESSION directly — no further typing needed."""
        self._input.clear()
        self._adapter.submit_slash(f"/resume {stem}")

    @Slot()
    def _on_excerpt_changed(self):
        """Capture the Ctrl+drag selection as a silent excerpt and highlight it in red."""
        ec = self._editor.textCursor()
        if not ec.hasSelection():
            return

        doc      = self._editor.document()
        sel_start = ec.selectionStart()
        sel_end   = ec.selectionEnd()

        start_block = doc.findBlock(sel_start)
        end_block   = doc.findBlock(sel_end)
        # If selection ends exactly at the start of a block, don't include that block
        if end_block.position() == sel_end and end_block != start_block:
            end_block = end_block.previous()

        start_line = start_block.blockNumber() + 1
        end_line   = end_block.blockNumber() + 1
        filename   = os.path.basename(self._current_file) if self._current_file else "file"
        lang       = detect_language(filename) if self._current_file else "plain"
        loc        = f"line {start_line}" if start_line == end_line else f"lines {start_line}–{end_line}"

        # Collect full line text for each block (whole-line context sent to model)
        lines = []
        block = start_block
        while block.isValid():
            lines.append(block.text())
            if block == end_block:
                break
            block = block.next()
        full_lines = "\n".join(lines)

        self._pending_excerpt = f"[{filename}, {loc}]\n```{lang}\n{full_lines}\n```\n"

        # Red full-width background over every selected line
        bg_fmt = QTextCharFormat()
        bg_fmt.setBackground(QColor("#2a0505"))
        bg_fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)

        sels = []
        block = start_block
        while block.isValid():
            sel = QTextEdit.ExtraSelection()
            sel.format = bg_fmt
            sel.cursor = QTextCursor(block)
            sels.append(sel)
            if block == end_block:
                break
            block = block.next()

        # Bold overlay on the exact selected character range
        bold_fmt = QTextCharFormat()
        bold_fmt.setFontWeight(700)
        bold_sel = QTextEdit.ExtraSelection()
        bold_sel.format = bold_fmt
        bold_cur = QTextCursor(doc)
        bold_cur.setPosition(sel_start)
        bold_cur.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        bold_sel.cursor = bold_cur
        sels.append(bold_sel)

        # Clear the text-cursor selection so only the ExtraSelections are visible
        clear_cur = QTextCursor(doc)
        clear_cur.setPosition(sel_start)
        self._editor.setTextCursor(clear_cur)

        # Merge with any existing change highlights
        self._excerpt_sels = sels
        self._apply_all_sels()

    def _clear_excerpt(self):
        """Remove the pending excerpt and its red highlight."""
        self._pending_excerpt = ""
        self._excerpt_sels = []

        self._editor._has_excerpt = False
        self._apply_all_sels()

    def _insert_excerpt(self):
        """Legacy /excerpt handler — now just activates excerpt from current selection."""
        if self._editor.textCursor().hasSelection():
            self._on_excerpt_changed()

    @Slot()
    def _on_new_session(self):
        self._file_touch_history.clear()
        self._adapter.submit_slash("/clear")

    @Slot()
    def _on_resume_last(self):
        self._adapter.submit_slash("/resume")

    @Slot()
    def _on_browse_sessions(self):
        sessions = list_sessions()
        if not sessions:
            QMessageBox.information(self, "Sessions", "No saved sessions found.")
            return
        dlg = _SessionPickerDialog(sessions, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_name:
            self._adapter.submit_slash(f"/resume {dlg.selected_name}")

    @Slot()
    def _on_browse_queue_results(self):
        QMessageBox.information(self, "Queue Results", "Queue results viewer coming in SP5.")

    @Slot()
    def _on_save_session(self):
        self._adapter.submit_slash("/save")

    @Slot()
    def _on_input_changed(self):
        text = self._input.toPlainText()
        if text.startswith("/") and "\n" not in text:
            stripped = text.rstrip()
            # /resume triggers session picker
            if stripped.startswith("/resume"):
                filter_text = stripped[7:].strip()  # text after "/resume"
                sessions = list_sessions()
                has_matches = self._completer.set_sessions(sessions, filter_text)
            else:
                has_matches = self._completer.update_filter(stripped)
            if has_matches:
                # Child widget coords (not global) — avoids OS popup keyboard grab
                input_pos = self._input.mapTo(self, self._input.rect().topLeft())
                row_h = max(self._completer.sizeHintForRow(0), 24)
                actual_h = min(220, row_h * self._completer.count() + 6)
                self._completer.setFixedSize(self._input.width(), actual_h)
                self._completer.move(input_pos.x(), input_pos.y() - actual_h)
                self._completer.raise_()
                self._completer.show()
            else:
                self._completer.hide()
        else:
            self._completer.hide()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _refresh_project_panel(self):
        """Walk up from CWD to find eli.toml; populate or hide the PROJECT group."""
        import tomllib
        cwd = Path(self._cwd)
        toml_path = None
        for parent in [cwd, *cwd.parents][:10]:
            candidate = parent / "eli.toml"
            if candidate.exists():
                toml_path = candidate
                break
        if toml_path is None:
            self._project_group.setVisible(False)
            return
        self._project_group.setVisible(True)
        try:
            with open(toml_path, "rb") as f:
                cfg = tomllib.load(f)
            name  = cfg.get("project", {}).get("name", toml_path.parent.name)
            build = cfg.get("build", {}).get("command", "")
            test  = cfg.get("test",  {}).get("command", "")
            self._proj_name_lbl.setText(f"Project: {name}")
            self._proj_build_lbl.setText(f"Build: {build}" if build else "")
            self._proj_build_lbl.setVisible(bool(build))
            self._proj_test_lbl.setText(f"Test: {test}" if test else "")
            self._proj_test_lbl.setVisible(bool(test))
        except Exception:
            self._proj_name_lbl.setText(f"Project: {toml_path.parent.name}")
            self._proj_build_lbl.setVisible(False)
            self._proj_test_lbl.setVisible(False)

    def _highlight_changes(self, old_text: str, new_text: str) -> None:
        """Highlight lines that differ between old_text and new_text."""
        import difflib
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        changed = set()
        for tag, _a0, _a1, b0, b1 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                changed.update(range(b0, b1))
        if not changed:
            return

        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#1a3a1a"))
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)

        doc  = self._editor.document()
        sels = []
        for line_idx in sorted(changed):
            block = doc.findBlockByNumber(line_idx)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            cursor = QTextCursor(block)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            sel.cursor = cursor
            sels.append(sel)

        self._change_sels = sels
        self._apply_all_sels()

        total = max(self._editor.document().blockCount(), 1)
        ratios = sorted({sel.cursor.blockNumber() / total for sel in sels})
        self._editor._marker_bar.set_change_marks(ratios)

        if hasattr(self, "_highlight_timer"):
            self._highlight_timer.stop()
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_change_highlights)
        self._highlight_timer.start(30_000)

    def _clear_change_highlights(self) -> None:
        self._change_sels = []
        self._apply_all_sels()
        self._editor._marker_bar.set_change_marks([])
        if hasattr(self, "_highlight_timer"):
            self._highlight_timer.stop()

    def _refresh_session_panel(self):
        """Update SESSION group with current message count."""
        try:
            from chat import _load_state as _ls
            st = _ls()
            msgs = st.get("message_count", "—")
            self._stat_msgs.setText(f"Messages: {msgs}")
        except Exception:
            pass

    def _append_user(self, text: str, queued: bool = False):
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        stripe = "#555555" if queued else USER_COLOR
        bg     = "#0d0d0d" if queued else "#0b1a1a"
        label  = '<span style="color:#555555;font-weight:bold;">You (queued)</span>' if queued else f'<span style="color:{USER_COLOR};font-weight:bold;">You</span>'
        text_color = "#666666" if queued else "#cccccc"
        html = (
            f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:2px 0;">'
            f'<tr>'
            f'<td width="3" style="background:{stripe};padding:0;vertical-align:top;"></td>'
            f'<td width="100%" style="background:{bg};padding:5px 10px;">'
            f'{label}<br>'
            f'<span style="color:{text_color};">{safe}</span>'
            f'</td>'
            f'</tr>'
            f'</table>'
        )
        # append() always inserts a new paragraph outside any table frame
        self._compact_view.append(html)
        self._full_view.document().setTextWidth(self._full_view.viewport().width())
        self._full_view.append(html)
        self._auto_scroll(self._compact_view)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_remote_message(self, text: str):
        """Prepare the view for a remote-submitted turn (mirrors _send_message minus input handling)."""
        self._set_input_enabled(False)
        self._append_remote(text)
        self._response_buf = ""
        self._agent_buf    = ""
        self._current_agent_label = ""
        self._full_view.append("")

    def _append_remote(self, text: str):
        """Render a remote-injected prompt — bright blue stripe, labelled 'Remote'."""
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = (
            f'<table width="100%" style="border-spacing:0;border-collapse:collapse;table-layout:fixed;margin:2px 0;">'
            f'<tr>'
            f'<td width="3" style="background:{REMOTE_COLOR};padding:0;vertical-align:top;"></td>'
            f'<td width="100%" style="background:#071828;padding:5px 10px;">'
            f'<span style="color:{REMOTE_COLOR};font-weight:bold;">Remote</span><br>'
            f'<span style="color:#cccccc;">{safe}</span>'
            f'</td></tr></table>'
        )
        self._full_view.document().setTextWidth(self._full_view.viewport().width())
        self._full_view.append(html)
        self._auto_scroll(self._full_view)

    @Slot(int)
    def _on_bg_agents_complete(self, count: int) -> None:
        noun = "agent" if count == 1 else "agents"
        html = (
            '<table width="100%" style="border-spacing:0;border-collapse:collapse;'
            'table-layout:fixed;margin:3px 0;">'
            '<tr>'
            '<td width="3" style="background:#22d3ee;padding:0;vertical-align:top;"></td>'
            '<td style="background:#071820;padding:4px 10px;">'
            f'<span style="color:#22d3ee;font-weight:bold;">&#10003; {count} background {noun} complete</span>'
            ' <span style="color:#666666;">— results included in your next message to Eli</span>'
            '</td></tr></table>'
        )
        self._full_view.append(html)
        self._auto_scroll(self._full_view)

    @Slot(str, int)
    def _on_open_in_editor(self, path: str, line: int):
        """Model requested editor navigation — confirm if a different file is open, then scroll."""
        if not path:
            return
        need_load = path != self._current_file
        if need_load and self._current_file:
            reply = QMessageBox.question(
                self,
                "Open in Editor",
                f"Switch editor to:\n{path}\n\n(Currently viewing {os.path.basename(self._current_file)})",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        if need_load:
            self._load_file(path)
        # Scroll to line
        doc = self._editor.document()
        block = doc.findBlockByNumber(max(0, line - 1))
        if block.isValid():
            cursor = QTextCursor(block)
            self._editor.setTextCursor(cursor)
            self._editor.centerCursor()

    def _clear_model_highlight(self):
        """Remove any model-requested yellow highlights from the editor."""
        if self._highlight_sels:
            self._highlight_sels = []
            self._apply_all_sels()
            self._editor._marker_bar.set_model_marks([])

    @Slot(str, int, int, int, int)
    def _on_highlight_in_editor(self, path: str, start_line: int, end_line: int,
                                 start_col: int, end_col: int):
        """Apply yellow highlight to a line range (and optional char range) in the editor."""
        if not path:
            return
        if path != self._current_file:
            self._load_file(path)

        doc = self._editor.document()

        # Full-width yellow background per line
        bg_fmt = QTextCharFormat()
        bg_fmt.setBackground(QColor("#2d2600"))
        bg_fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)

        sels = []
        for ln in range(start_line - 1, end_line):
            block = doc.findBlockByNumber(ln)
            if not block.isValid():
                break
            sel = QTextEdit.ExtraSelection()
            sel.format = bg_fmt
            sel.cursor = QTextCursor(block)
            sels.append(sel)

        # Optional character-range overlay with brighter yellow background
        if start_col >= 0 and end_col >= 0 and start_line == end_line:
            start_block = doc.findBlockByNumber(start_line - 1)
            if start_block.isValid():
                char_fmt = QTextCharFormat()
                char_fmt.setBackground(QColor("#4a3e00"))
                char_sel = QTextEdit.ExtraSelection()
                char_sel.format = char_fmt
                cur = QTextCursor(doc)
                cur.setPosition(start_block.position() + start_col)
                cur.setPosition(start_block.position() + end_col,
                                QTextCursor.MoveMode.KeepAnchor)
                char_sel.cursor = cur
                sels.append(char_sel)

        self._highlight_sels += sels
        self._apply_all_sels()

        # Update scrollbar markers
        total = max(self._editor.document().blockCount(), 1)
        ratios = sorted({sel.cursor.blockNumber() / total for sel in self._highlight_sels})
        self._editor._marker_bar.set_model_marks(ratios)

        # Scroll to the highlighted range
        start_block = doc.findBlockByNumber(start_line - 1)
        if start_block.isValid():
            self._editor.setTextCursor(QTextCursor(start_block))
            self._editor.centerCursor()

    @staticmethod
    def _auto_scroll(view: QTextBrowser):
        sb = view.verticalScrollBar()
        if sb.value() >= sb.maximum() - 10:
            sb.setValue(sb.maximum())

    def _set_input_enabled(self, enabled: bool):
        self._busy = not enabled
        # Input stays writable so the user can type/queue while the model works.
        # Only the send button label reflects the current mode.
        if enabled:
            self._send_btn.setText("Send")
            self._send_btn.setEnabled(True)
            self._kr_bar.stop()
        else:
            self._send_btn.setText("Queue")
            self._send_btn.setEnabled(True)   # always clickable — queues when busy
            self._kr_bar.start()
        self._input.setFocus()


class _SessionPickerDialog(QDialog):
    """Modal dialog to browse and select a saved session."""

    def __init__(self, sessions: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Browse Sessions")
        self.setMinimumWidth(420)
        self.selected_name: str | None = None

        layout = QVBoxLayout(self)
        self._list = QListWidget()
        for s in sessions:
            tok = s.get("token_estimate", 0)
            label = f"{s['stem']}  —  {s.get('saved_at', '')}  ~{tok:,} tokens"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, s["stem"])
            self._list.addItem(item)
        layout.addWidget(self._list)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._list.itemDoubleClicked.connect(lambda _: self._on_accept())

    def _on_accept(self):
        item = self._list.currentItem()
        if item:
            self.selected_name = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


# ── Module-level helpers ───────────────────────────────────────────────────────

# ── Markdown → HTML renderer ─────────────────────────────────────────────────

_LANG_ALIAS = {
    "js": "javascript", "ts": "javascript", "jsx": "javascript", "tsx": "javascript",
    "sh": "bash", "zsh": "bash",
    "cpp": "c", "c++": "c", "cxx": "c",
    "cs": "csharp",
    "py": "python",
    "yml": "yaml",
}

_MD_ESC = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;"})

# LaTeX command → Unicode symbol, used for $...$ inline math spans.
_LATEX_SYMS: dict[str, str] = {
    # Arrows
    r"\rightarrow": "→", r"\to": "→",
    r"\leftarrow": "←", r"\gets": "←",
    r"\leftrightarrow": "↔",
    r"\Rightarrow": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔",
    r"\uparrow": "↑", r"\Uparrow": "⇑",
    r"\downarrow": "↓", r"\Downarrow": "⇓",
    r"\nearrow": "↗", r"\searrow": "↘",
    r"\nwarrow": "↖", r"\swarrow": "↙",
    r"\mapsto": "↦",
    # Relations
    r"\neq": "≠", r"\ne": "≠",
    r"\leq": "≤", r"\le": "≤",
    r"\geq": "≥", r"\ge": "≥",
    r"\approx": "≈", r"\equiv": "≡",
    r"\sim": "∼", r"\simeq": "≃",
    r"\propto": "∝", r"\cong": "≅",
    r"\ll": "≪", r"\gg": "≫",
    # Arithmetic / operators
    r"\pm": "±", r"\mp": "∓",
    r"\times": "×", r"\div": "÷",
    r"\cdot": "·", r"\circ": "∘",
    r"\oplus": "⊕", r"\otimes": "⊗",
    # Set / logic
    r"\in": "∈", r"\notin": "∉",
    r"\subset": "⊂", r"\supset": "⊃",
    r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\cup": "∪", r"\cap": "∩",
    r"\emptyset": "∅", r"\varnothing": "∅",
    r"\forall": "∀", r"\exists": "∃",
    r"\neg": "¬", r"\lnot": "¬",
    r"\land": "∧", r"\lor": "∨",
    # Calculus / analysis
    r"\infty": "∞",
    r"\nabla": "∇", r"\partial": "∂",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫",
    r"\sqrt": "√",
    r"\ldots": "…", r"\cdots": "⋯", r"\vdots": "⋮",
    # Greek lowercase
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\epsilon": "ε", r"\varepsilon": "ε",
    r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ", r"\vartheta": "ϑ",
    r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ",
    r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\pi": "π", r"\varpi": "ϖ",
    r"\rho": "ρ", r"\sigma": "σ", r"\varsigma": "ς",
    r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ", r"\varphi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    # Greek uppercase
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ",
    r"\Lambda": "Λ", r"\Xi": "Ξ", r"\Pi": "Π",
    r"\Sigma": "Σ", r"\Upsilon": "Υ", r"\Phi": "Φ",
    r"\Psi": "Ψ", r"\Omega": "Ω",
    # Misc
    r"\hbar": "ℏ", r"\ell": "ℓ",
    r"\dag": "†", r"\ddag": "‡",
    r"\bullet": "•", r"\star": "★",
}


def _inline_html(text: str) -> str:
    """Convert inline markdown (bold, italic, code, links) to HTML.
    Input must NOT be HTML-escaped yet — this function escapes it first.
    """
    import re
    t = text.translate(_MD_ESC)
    # Inline LaTeX math: $...$  →  unicode symbols in a subtle italic span.
    # Processed before bold/italic so that _ inside math doesn't trigger italic.
    def _latex_math(m: re.Match) -> str:
        inner = m.group(1)
        def _sub_cmd(cm: re.Match) -> str:
            return _LATEX_SYMS.get(cm.group(0), cm.group(0))
        inner = re.sub(r'\\[A-Za-z]+', _sub_cmd, inner)
        return f'<span style="color:#c8a8e8;font-style:italic;">{inner}</span>'
    t = re.sub(r'\$([^$\n]+)\$', _latex_math, t)
    # Bold+italic combined
    t = re.sub(r'\*\*\*(.*?)\*\*\*', r'<b><i>\1</i></b>', t)
    # Bold
    t = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'__(.*?)__',     r'<b>\1</b>', t)
    # Italic
    t = re.sub(r'\*(.*?)\*',     r'<i>\1</i>', t)
    t = re.sub(r'\b_(.*?)_\b',   r'<i>\1</i>', t)
    # Inline code — file paths become Ctrl+clickable links; other code stays as styled spans
    _FILE_PATH_RE = re.compile(
        r'^[a-zA-Z0-9_./ \\:@-]*[a-zA-Z0-9_-]+'
        r'\.(py|cpp|c|h|hpp|cs|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|txt|md|sh|bat|ps1|go|rs|java|rb|lua|html|css|sql)'
        r'(?::(\d+))?$'
    )
    def _code_span(m):
        inner = m.group(1)
        if _FILE_PATH_RE.match(inner.strip()):
            safe = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return (
                f'<a href="eli://open/{inner}" style="background:#0d0d1f;color:#7dd3fc;'
                f'font-family:Consolas,monospace;padding:0 3px;text-decoration:none;">'
                f'{safe}</a>'
            )
        return (
            f'<span style="background:#0d0d1f;color:#ffcc66;'
            f'font-family:Consolas,monospace;padding:0 3px;">'
            f'{inner}</span>'
        )
    t = re.sub(r'`([^`]+)`', _code_span, t)
    # Links → show text only (QTextBrowser handles href separately)
    t = re.sub(r'\[([^\]]+)\]\([^)]*\)',
               r'<span style="color:#6699cc;text-decoration:underline;">\1</span>', t)
    return t


def _looks_like_diff(code: str) -> bool:
    lines = code.splitlines()
    return (any(l.startswith("---") for l in lines)
            and any(l.startswith("+++") for l in lines)
            and any(l.startswith("@@")  for l in lines))


def _diff_block_html(code: str) -> str:
    """Render a unified diff with coloured rows, line numbers, and syntax-highlighted content."""
    import re

    # Detect underlying language from the +++ filename line
    src_lang = "plain"
    for line in code.splitlines():
        if line.startswith("+++ "):
            filename = re.split(r"[\s/]", line[4:])[-1].split("\t")[0]
            src_lang = detect_language(filename)
            break

    _HUNK_RE = re.compile(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    # Row styles: (bg, glyph-color, glyph)
    _STYLES = {
        "+":   ("background:#0a2210;", "#33bb33", "+"),
        "-":   ("background:#220a0a;", "#cc3333", "-"),
        "@@":  ("background:#0a1828;", "#6699cc", "~"),
        "hdr": ("background:#0d0d1a;", "#555577", " "),
        " ":   ("background:#090914;", "#444466", " "),
    }

    _NUM_STYLE = (
        "color:#445566;padding:0 6px;text-align:right;white-space:pre;"
        "vertical-align:top;user-select:none;min-width:32px;"
    )
    _GLYPH_STYLE = (
        "padding:0 5px;white-space:pre;vertical-align:top;user-select:none;"
    )

    old_num = 0
    new_num = 0

    rows = []
    for raw in code.splitlines():
        if not raw:          # blank line with no prefix — formatting noise, skip
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            bg, gc, glyph = _STYLES["hdr"]
            content_html = f'<span style="color:#666688;">{raw.translate(_MD_ESC)}</span>'
            num_cell = ""
        elif raw.startswith("@@"):
            bg, gc, glyph = _STYLES["@@"]
            content_html = f'<span style="color:#6699cc;">{raw.translate(_MD_ESC)}</span>'
            m = _HUNK_RE.match(raw)
            if m:
                old_num = int(m.group(1))
                new_num = int(m.group(2))
            num_cell = "···"
        elif raw.startswith("+"):
            bg, gc, glyph = _STYLES["+"]
            content_html = highlight_code_html(raw[1:], src_lang)
            num_cell = str(new_num)
            new_num += 1
        elif raw.startswith("-"):
            bg, gc, glyph = _STYLES["-"]
            content_html = highlight_code_html(raw[1:], src_lang)
            num_cell = str(old_num)
            old_num += 1
        elif raw.startswith("\\"):
            bg, gc, glyph = _STYLES[" "]
            content_html = f'<span style="color:#555566;">{raw.translate(_MD_ESC)}</span>'
            num_cell = ""
        else:
            bg, gc, glyph = _STYLES[" "]
            content = raw[1:] if raw.startswith(" ") else raw
            content_html = highlight_code_html(content, src_lang)
            num_cell = str(new_num)
            old_num += 1
            new_num += 1

        rows.append(
            f'<tr style="{bg}">'
            f'<td style="{_NUM_STYLE}">{num_cell}</td>'
            f'<td style="color:{gc};{_GLYPH_STYLE}">{glyph}</td>'
            f'<td style="white-space:pre;padding:0;">{content_html}</td>'
            f'</tr>'
        )

    inner = (
        f'<table style="border-spacing:0;border-collapse:collapse;width:100%;">'
        + "".join(rows)
        + "</table>"
    )
    return (
        f'<div style="background:#090914;border-left:3px solid #334455;'
        f'padding:4px 0;margin:4px 0;font-family:Consolas,monospace;font-size:12px;">'
        f'{inner}</div>'
    )


def _code_block_html(lang: str, code: str) -> str:
    """Render a fenced code block as a numbered-line HTML table."""
    lang = _LANG_ALIAS.get(lang, lang) or "plain"

    # Markdown content — render as formatted prose, not a numbered code block
    if lang in ("markdown", "md"):
        return _markdown_to_html(code)

    # Unified diff — use specialised renderer regardless of declared language
    if lang in ("diff", "patch") or _looks_like_diff(code):
        return _diff_block_html(code)

    code_lines = highlight_code_html(code, lang).split("<br>")
    num_w = len(str(len(code_lines)))
    rows = "".join(
        f'<tr>'
        f'<td style="color:#444466;text-align:right;padding:0 8px 0 6px;'
        f'white-space:pre;vertical-align:top;">{str(i).rjust(num_w)}</td>'
        f'<td style="white-space:pre;padding:0;">{line_html}</td>'
        f'</tr>'
        for i, line_html in enumerate(code_lines, 1)
    )
    inner = (
        f'<table style="border-spacing:0;border-collapse:collapse;width:100%;">'
        f'{rows}</table>'
    )
    return (
        f'<div style="background:#0d0d1f;border-left:3px solid #334466;'
        f'padding:4px 0;margin:4px 0;font-family:Consolas,monospace;'
        f'font-size:12px;">{inner}</div>'
    )


def _cell_align(raw: str) -> str:
    """Guess alignment from a raw (unstripped) cell string."""
    stripped = raw.strip()
    if not stripped:
        return "left"
    lead  = len(raw) - len(raw.lstrip())
    trail = len(raw) - len(raw.rstrip())
    if lead > 1 and trail > 1 and abs(lead - trail) <= 2:
        return "center"
    if lead > trail:
        return "right"
    return "left"


def _gfm_sep_aligns(sep_line: str) -> list[str]:
    """Extract per-column alignment from a GFM separator row.
    e.g. '|:---|:---:|---:|' → ['left', 'center', 'right']
    """
    cells = [c.strip() for c in sep_line.split("|")[1:-1]]
    result = []
    for c in cells:
        if c.startswith(":") and c.endswith(":"):
            result.append("center")
        elif c.endswith(":"):
            result.append("right")
        else:
            result.append("left")
    return result


def _table_html(lines: list[str], footer_rows: set[int] | None = None) -> str:
    """Render pipe-table rows as HTML.  Handles both GFM and box-table row lists.

    footer_rows: 0-based indices into `lines` that should use footer styling.
    """
    import re
    footer_rows = footer_rows or set()
    sep_re = re.compile(r'^[\s|:\-]+$')

    def parse_cells(line: str) -> tuple[list[str], list[str]]:
        parts = line.split("|")[1:-1]
        return [p.strip() for p in parts], parts

    # Extract GFM separator row alignment BEFORE dropping it
    aligns: list[str] = []
    for line in lines:
        if sep_re.match(line) and re.search(r'-', line):
            aligns = _gfm_sep_aligns(line)
            break

    # Drop separator lines to get pure data rows
    data = [(i, l) for i, l in enumerate(lines) if not sep_re.match(l)]
    if not data:
        return ""

    _, header_line = data[0]
    header_cells, header_raw = parse_cells(header_line)

    # Fall back to padding-inference if no GFM separator was found
    if not aligns:
        aligns = [_cell_align(r) for r in header_raw]

    th = "".join(
        f'<th style="padding:5px 12px;border-bottom:2px solid #334466;'
        f'border-right:1px solid #1e2a40;color:#7aafdd;background:#080818;'
        f'text-align:{aligns[ci] if ci < len(aligns) else "left"};font-weight:bold;">'
        f'{_inline_html(h)}</th>'
        for ci, h in enumerate(header_cells)
    )

    trs = []
    for li, (orig_i, row_line) in enumerate(data[1:]):
        cells, raw = parse_cells(row_line)
        is_footer = orig_i in footer_rows
        bg = "#0e0e22" if is_footer else ("#07071a" if li % 2 == 0 else "#0a0a1e")
        color = "color:#aaccee;font-weight:bold;" if is_footer else ""
        tds = "".join(
            f'<td style="padding:4px 12px;border-bottom:1px solid #141428;'
            f'border-right:1px solid #141428;background:{bg};{color}'
            f'text-align:{aligns[ci] if ci < len(aligns) else "left"};">'
            f'{_inline_html(c)}</td>'
            for ci, c in enumerate(cells)
        )
        trs.append(f"<tr>{tds}</tr>")

    return (
        f'<table style="border-collapse:collapse;margin:6px 0;font-size:12px;'
        f'border:1px solid #1e2a40;">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table>'
    )


def _box_table_html(lines: list[str]) -> str:
    """Render an ASCII box table (+---+---+ style) as a styled HTML table."""
    import re
    # Separate separator lines from data lines; track which data rows follow a separator
    sep_re  = re.compile(r'^\+[-+]+\+\s*$')
    data_re = re.compile(r'^\|.*\|\s*$')

    segments: list[list[str]] = []   # groups of data rows between separators
    current:  list[str]       = []
    for line in lines:
        if sep_re.match(line):
            if current:
                segments.append(current)
                current = []
        elif data_re.match(line):
            current.append(line)
    if current:
        segments.append(current)

    if not segments:
        return ""

    # First segment = header, middle = body, last (if more than 2) = footer
    all_data_lines: list[str] = []
    footer_set: set[int] = set()
    offset = 0
    for si, seg in enumerate(segments):
        for row in seg:
            if si > 0 and si == len(segments) - 1 and len(segments) > 2:
                footer_set.add(offset)
            all_data_lines.append(row)
            offset += 1

    return _table_html(all_data_lines, footer_set)


def _panel_html(box_lines: list[str]) -> str:
    """Render a ╭─ Title ─╮ / │ content │ / ╰──╯ box as a styled HTML panel."""
    import re

    # Extract title from the first line: ╭──── Title ────╮
    m = re.search(r"─ (.+?) ─", box_lines[0])
    title = m.group(1).strip() if m else ""

    # Strip │ prefix/suffix from each content line (skip first/last box lines)
    raw: list[str] = []
    for line in box_lines[1:-1]:
        s = line.strip()
        if s.startswith("│"):
            s = s[1:]
        if s.endswith("│"):
            s = s[:-1]
        raw.append(s.strip())

    # Re-join wrapped lines into logical entries.
    # A new entry begins when the line starts with  word(  or  /word
    _ENTRY_RE = re.compile(r"^(\w+\(|/\w+)")
    entries: list[str] = []
    cur = ""
    for line in raw:
        if _ENTRY_RE.match(line) and cur:
            entries.append(cur)
            cur = line
        else:
            cur = (cur + " " + line).strip() if cur else line
    if cur:
        entries.append(cur)

    rows: list[str] = []
    for entry in entries:
        if " — " in entry:
            sig, desc = entry.split(" — ", 1)
        else:
            sig, desc = entry, ""

        # Colour the signature
        if sig.startswith("/"):
            sig_html = (
                f'<span style="color:#ffcc66;font-weight:bold;">'
                f'{sig.translate(_MD_ESC)}</span>'
            )
        else:
            pm = re.match(r"^(\w+)(\(.*\))$", sig)
            if pm:
                sig_html = (
                    f'<span style="color:#6699cc;font-weight:bold;">'
                    f'{pm.group(1).translate(_MD_ESC)}</span>'
                    f'<span style="color:#5577aa;">'
                    f'{pm.group(2).translate(_MD_ESC)}</span>'
                )
            else:
                sig_html = (
                    f'<span style="color:#6699cc;font-weight:bold;">'
                    f'{sig.translate(_MD_ESC)}</span>'
                )

        # Highlight trigger list:  · triggers: a, b, c
        desc_safe = desc.translate(_MD_ESC)
        desc_safe = re.sub(
            r"· triggers: (.+)",
            lambda tm: (
                '<span style="color:#445566;"> · triggers: </span>'
                + " ".join(
                    f'<span style="background:#12122a;color:#7799aa;'
                    f'padding:0 4px;border-radius:2px;">{t.strip()}</span>'
                    for t in tm.group(1).split(",")
                )
            ),
            desc_safe,
        )

        sep_html = ' <span style="color:#2a3a55;font-weight:bold;">—</span> ' if desc else ""
        rows.append(
            f'<div style="padding:4px 12px;border-bottom:1px solid #0e0e20;">'
            f'{sig_html}{sep_html}'
            f'<span style="color:#888899;">{desc_safe}</span>'
            f'</div>'
        )

    return (
        f'<div style="border:1px solid #1e2e4a;border-radius:3px;margin:6px 0;'
        f'font-family:Consolas,monospace;font-size:12px;">'
        f'<div style="background:#0a1828;color:#7ec8e3;font-weight:bold;'
        f'padding:5px 12px;border-bottom:1px solid #1e2e4a;">'
        f'{title.translate(_MD_ESC)}</div>'
        f'<div style="background:#070710;">{"".join(rows)}</div>'
        f'</div>'
    )



def _prose_to_html(text: str) -> str:
    """Convert a prose markdown segment (no fenced code blocks, headings, lists, etc.) to HTML."""
    import re
    lines = text.split("\n")
    out: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Empty line → paragraph gap ──────────────────────────────────────
        if not line.strip():
            if out and not out[-1].endswith("<br>"):
                out.append("<br>")
            i += 1
            continue

        # ── Regular paragraph line ──────────────────────────────────────────
        out.append(_inline_html(line.rstrip()) + "<br>")
        i += 1

    return "".join(out)


def _markdown_to_html(text: str) -> str:
    """Convert full markdown response to HTML for the chat output window."""
    import re
    _FENCE_OPEN = re.compile(r'^(`{3,})(\w*)\s*$')
    _HEADING = re.compile(r'^(#{1,4})\s+(.*)')
    _BQ_START = re.compile(r'^>')
    _UL_START = re.compile(r'^\s*[-*+]\s')
    _OL_START = re.compile(r'^\s*\d+(?:\.\d+)*\.\s')   # matches "1. ", "1.1. ", "2.1.3. "
    _OL_HIER  = re.compile(r'^\s*(\d+(?:\.\d+)+)\.\s')  # hierarchical only (2+ components)
    _BOX_TABLE = re.compile(r'^\+[-+]+\+')
    _PIPE_TABLE = re.compile(r'^\s*\|')
    _BOX_START = re.compile(r'^╭')
    _HR = re.compile(r'^[-*=_]{3,}\s*$')

    parts: list[str] = []
    lines = text.split("\n")
    i = 0
    prose_buf: list[str] = []

    def _flush_prose() -> None:
        if not prose_buf:
            return
        html = _prose_to_html("\n".join(prose_buf))
        if html and not html.endswith("<br>"):
            html += "<br>"
        parts.append(html)
        prose_buf.clear()

    def _is_prose_line(line: str) -> bool:
        """Check if line is prose (not a structural element)."""
        stripped = line.strip()
        if not stripped:
            return True  # empty lines are prose
        if stripped.startswith("╭"):
            return False
        if _BOX_TABLE.match(stripped):
            return False
        if _PIPE_TABLE.match(stripped):
            return False
        if _HEADING.match(stripped):
            return False
        if _BQ_START.match(stripped):
            return False
        if _UL_START.match(stripped):
            return False
        if _OL_START.match(stripped):
            return False
        if _HR.match(stripped):
            return False
        return True

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Fenced code block ───────────────────────────────────────────────────
        m = _FENCE_OPEN.match(stripped)
        if m:
            _flush_prose()
            lang = m.group(2).lower()
            close_re = re.compile(r'^`{' + str(len(m.group(1))) + r',}\s*$')
            i += 1
            code_buf: list[str] = []
            while i < len(lines):
                if close_re.match(lines[i].strip()):
                    i += 1
                    break
                code_buf.append(lines[i])
                i += 1
            code = "\n".join(code_buf).rstrip("\n")
            parts.append(_code_block_html(lang, code))
            parts.append("<br>")
            continue

        # ── Box-drawing panel ───────────────────────────────────────────────────
        if stripped.startswith("╭"):
            _flush_prose()
            box: list[str] = [line]
            i += 1
            while i < len(lines):
                box.append(lines[i])
                if lines[i].startswith("╰"):
                    break
                i += 1
            parts.append(_panel_html(box))
            parts.append("<br>")
            continue

        # ── ASCII box table ─────────────────────────────────────────────────────
        if _BOX_TABLE.match(stripped):
            _flush_prose()
            tbl: list[str] = []
            while i < len(lines) and re.match(r'^[+|]', lines[i]):
                tbl.append(lines[i])
                i += 1
            parts.append(_box_table_html(tbl))
            parts.append("<br>")
            continue

        # ── GFM pipe table ──────────────────────────────────────────────────────
        if _PIPE_TABLE.match(stripped):
            _flush_prose()
            tbl = []
            while i < len(lines) and "|" in lines[i]:
                tbl.append(lines[i])
                i += 1
            parts.append(_table_html(tbl))
            parts.append("<br>")
            continue

        # ── Heading ─────────────────────────────────────────────────────────────
        hm = _HEADING.match(stripped)
        if hm:
            _flush_prose()
            level = len(hm.group(1))
            content = _inline_html(hm.group(2))
            sizes = {1: "15px", 2: "14px", 3: "13px", 4: "12px"}
            colors = {1: "#88bbee", 2: "#7799cc", 3: "#6688bb", 4: "#557799"}
            border = ("border-bottom:1px solid #223344;padding-bottom:3px;margin:8px 0 4px 0;"
                      if level <= 2 else "margin:5px 0 2px 0;")
            parts.append(
                f'<div style="font-size:{sizes[level]};font-weight:bold;'
                f'color:{colors[level]};{border}">{content}</div><br>'
            )
            i += 1
            continue

        # ── Blockquote ──────────────────────────────────────────────────────────
        if _BQ_START.match(stripped):
            _flush_prose()
            bq: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                bq.append(lines[i].lstrip(">").lstrip())
                i += 1
            inner = "<br>".join(_inline_html(l) for l in bq)
            parts.append(
                f'<div style="border-left:3px solid #7755aa;padding:2px 8px;'
                f'color:#9977cc;margin:3px 0;">{inner}</div><br>'
            )
            continue

        # ── List (ordered / unordered, nested, mixed) ───────────────────────────
        # No <ul>/<li> (Qt list-context bleed) and no nested tables (breaks outer frame).
        # Each item is emitted as inline text: &nbsp; indent + marker + gap + content + <br>.
        # Nesting tracked via indent stack; 4 &nbsp; per depth level.
        if _UL_START.match(stripped) or _OL_START.match(stripped):
            _flush_prose()
            # stack entries: (abs_indent: int, list_type: "ul"|"ol", counter: int)
            stack: list[tuple[int, str, int]] = []

            while i < len(lines):
                raw = lines[i]
                lstripped = raw.lstrip()
                abs_indent = len(raw) - len(lstripped)
                is_ul = bool(_UL_START.match(raw))
                is_ol = bool(_OL_START.match(raw))

                if not lstripped:
                    # blank line: continue only if next non-blank is still a list item
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and (_UL_START.match(lines[j]) or _OL_START.match(lines[j])):
                        i += 1
                        continue
                    else:
                        break

                if not (is_ul or is_ol):
                    break

                # Hierarchical OL (e.g. "1.1.", "2.1.3."): depth and marker from dot count.
                hier_m = _OL_HIER.match(raw) if is_ol else None
                if hier_m:
                    components = hier_m.group(1).split('.')
                    depth = len(components) - 1
                    marker = f"{components[-1]}."
                    content = re.sub(r'^\s*[\d.]+\.\s+', '', raw)
                else:
                    cur_type = "ul" if is_ul else "ol"

                    if not stack:
                        stack.append((abs_indent, cur_type, 0))
                    else:
                        top_indent = stack[-1][0]
                        if abs_indent > top_indent:
                            stack.append((abs_indent, cur_type, 0))
                        elif abs_indent < top_indent:
                            while len(stack) > 1 and stack[-1][0] > abs_indent:
                                stack.pop()
                            if stack[-1][0] != abs_indent:
                                stack[-1] = (abs_indent, cur_type, stack[-1][2])
                        if stack[-1][1] != cur_type:
                            stack[-1] = (stack[-1][0], cur_type, 0)

                    depth = len(stack) - 1
                    s_indent, s_type, s_counter = stack[-1]
                    if s_type == "ol":
                        s_counter += 1
                        stack[-1] = (s_indent, s_type, s_counter)
                        marker = f"{s_counter}."
                    else:
                        marker = "•"

                    if is_ul:
                        content = re.sub(r'^\s*[-*+]\s+', "", raw)
                    else:
                        content = re.sub(r'^\s*\d+\.\s+', "", raw)

                pad = '&nbsp;' * (2 + depth * 4)
                parts.append(
                    f'<span style="color:#888888;">{pad}{marker}&nbsp;&nbsp;</span>'
                    f'{_inline_html(content)}<br>'
                )
                i += 1

            continue

        # ── Horizontal rule ─────────────────────────────────────────────────────
        if _HR.match(stripped):
            _flush_prose()
            parts.append('<hr style="border:none;border-top:1px solid #334466;margin:6px 0;"><br>')
            i += 1
            continue

        # ── Empty line ──────────────────────────────────────────────────────────
        if not stripped:
            prose_buf.append("")
            i += 1
            continue

        # ── Prose line ──────────────────────────────────────────────────────────
        prose_buf.append(line)
        i += 1

    _flush_prose()
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
