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
    QMainWindow, QSplitter, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QToolBar, QStatusBar, QComboBox, QLineEdit,
    QTreeView, QTabWidget, QTextBrowser, QPlainTextEdit,
    QPushButton, QProgressBar, QMessageBox, QFileSystemModel,
    QScrollArea, QGroupBox, QSlider, QSpinBox, QDialog,
    QDialogButtonBox, QListWidget, QListWidgetItem,
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QTextCharFormat, QTextCursor, QKeySequence, QShortcut

import httpx

from colors import USER_COLOR, ASST_COLOR, BG_CODE, BORDER_CODE, ACCENT, TEXT_DIM
from highlighter import SyntaxHighlighter, detect_language
from file_watcher import DirWatcher
from adapter import QtChatAdapter

try:
    from session_state import load_state, save_state, list_sessions, load_session, get_agent_name
    from slash_completer import SlashCompleter
except ImportError:
    from qt.session_state import load_state, save_state, list_sessions, load_session, get_agent_name
    from qt.slash_completer import SlashCompleter

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

        # Load persisted state
        _state = load_state()
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

    def closeEvent(self, event):
        self._adapter.shutdown()
        self._adapter.wait(3000)
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
        self._think_combo.setCurrentText(self._saved_think)
        self._think_combo.setFixedWidth(70)
        self._think_combo.currentTextChanged.connect(self._on_think_changed)
        tb.addWidget(self._think_combo)
        tb.addSeparator()

        tb.addWidget(QLabel("Approval:"))
        self._approval_combo = QComboBox()
        self._approval_combo.addItems(["auto", "ask-writes", "ask-all", "yolo"])
        self._approval_combo.setCurrentText(self._saved_approval)
        self._approval_combo.setFixedWidth(90)
        self._approval_combo.currentTextChanged.connect(self._on_approval_changed)
        tb.addWidget(self._approval_combo)
        tb.addSeparator()

        # Plan mode toggle
        self._plan_btn = QPushButton("Plan")
        self._plan_btn.setCheckable(True)
        self._plan_btn.setFixedWidth(50)
        self._plan_btn.setToolTip("Plan mode — model describes actions without running tools")
        tb.addWidget(self._plan_btn)

        # Compact view toggle
        self._compact_btn = QPushButton("Compact")
        self._compact_btn.setCheckable(True)
        self._compact_btn.setChecked(self._saved_compact)
        self._compact_btn.setFixedWidth(70)
        self._compact_btn.setToolTip("Compact mode — hides thinking tokens in Full View")
        self._compact_btn.toggled.connect(self._on_compact_toggled)
        tb.addWidget(self._compact_btn)

        # Stop / interrupt button
        self._stop_btn = QPushButton("■ Stop")
        self._stop_btn.setFixedWidth(65)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setToolTip("Cancel the in-flight response (ESC)")
        self._stop_btn.clicked.connect(self._adapter.cancel)
        tb.addWidget(self._stop_btn)
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

        # Token context bar (temporary SP3 placement; migrated to right panel in SP4)
        ctx_row = QWidget()
        ctx_layout = QHBoxLayout(ctx_row)
        ctx_layout.setContentsMargins(6, 2, 6, 2)
        ctx_layout.setSpacing(6)

        self._ctx_bar = QProgressBar()
        self._ctx_bar.setRange(0, 100)
        self._ctx_bar.setValue(0)
        self._ctx_bar.setTextVisible(False)
        self._ctx_bar.setFixedHeight(6)
        ctx_layout.addWidget(self._ctx_bar, stretch=1)

        self._ctx_label = QLabel("Context: —")
        self._ctx_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        ctx_layout.addWidget(self._ctx_label)

        self._ctx_warn = QLabel("⚠ compact soon")
        self._ctx_warn.setStyleSheet("color: #fbbf24; font-size: 10px;")
        self._ctx_warn.setVisible(False)
        ctx_layout.addWidget(self._ctx_warn)

        layout.addWidget(ctx_row)

        # Input area
        input_container = QWidget()
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(6, 6, 6, 6)
        input_layout.setSpacing(6)

        self._input = QPlainTextEdit()
        self._input.setPlaceholderText("Type a message… (Enter to send, Shift+Enter for newline, / for commands)")
        self._input.setFixedHeight(90)
        self._input.installEventFilter(self)
        input_layout.addWidget(self._input, stretch=1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setFixedWidth(60)
        self._send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(self._send_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        layout.addWidget(input_container)

        # Slash completer (positioned dynamically above input)
        self._completer = SlashCompleter(self)
        self._completer.hide()
        self._completer.command_chosen.connect(self._on_command_chosen)

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
        self._adapter.think_token.connect(self._on_think_token)
        self._adapter.text_token.connect(self._on_text_token)
        self._adapter.text_done.connect(self._on_text_done)
        self._adapter.tool_start.connect(self._on_tool_start)
        self._adapter.tool_done.connect(self._on_tool_done_signal)
        self._adapter.approval_needed.connect(self._on_approval_needed)
        self._adapter.usage.connect(self._on_usage)
        self._adapter.system_msg.connect(self._on_system_msg)
        self._adapter.error_msg.connect(self._on_error_msg)
        self._adapter.done.connect(self._on_turn_done)

        # SP3 additions
        self._adapter.stream_started.connect(self._on_stream_started)
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.activated.connect(self._on_esc)
        self._plan_btn.toggled.connect(lambda checked: setattr(self, '_plan_mode', checked))
        self._input.textChanged.connect(self._on_input_changed)

    # ── Event filter ─────────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()

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

            # Arrow keys navigate completer
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
            f'<span style="color:{ASST_COLOR};font-weight:bold;">{self._agent_name}</span><br>'
        )
        self._adapter.submit(text, self._plan_mode)

    @Slot(str)
    def _on_think_token(self, token: str):
        if getattr(self, "_compact_mode", False):
            return   # suppress thinking display in compact mode
        _insert_plain(self._full_view, token)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_text_token(self, token: str):
        self._response_buf += token
        _insert_plain(self._full_view, token)
        self._auto_scroll(self._full_view)

    @Slot(str)
    def _on_text_done(self, full_text: str):
        header = f'<span style="color:{ASST_COLOR};font-weight:bold;">{self._agent_name}</span><br>'
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
        if ctx > 0:
            pct = int(tokens / ctx * 100)
            self._ctx_bar.setValue(pct)
            self._ctx_label.setText(f"{tokens // 1000:.1f}k / {ctx // 1000:.0f}k tokens ({pct}%)")
            self._ctx_bar.setStyleSheet(
                "QProgressBar::chunk { background: #ef4444; }" if pct >= 80 else
                "QProgressBar::chunk { background: #fbbf24; }" if pct >= 60 else
                "QProgressBar::chunk { background: #7dff7d; }"
            )
            self._ctx_warn.setVisible(pct >= 75)

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
        self._plan_btn.setChecked(False)
        self._stop_btn.setEnabled(False)
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
            self._server_status.setStyleSheet("color: #7dff7d; font-size: 14px;")
        else:
            self._stat_status.setText("● Offline")
            self._stat_status.setStyleSheet("color: #ef4444;")
            self._server_status.setStyleSheet("color: #ef4444; font-size: 14px;")
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

    @Slot()
    def _on_stream_started(self):
        self._stop_btn.setEnabled(True)

    @Slot()
    def _on_esc(self):
        if self._completer.isVisible():
            self._completer.hide()
        elif self._stop_btn.isEnabled():
            self._adapter.cancel()
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
        self._input.setPlainText(cmd + " ")
        cursor = self._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._input.setTextCursor(cursor)
        self._input.setFocus()

    @Slot()
    def _on_new_session(self):
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
            has_matches = self._completer.update_filter(text.rstrip())
            if has_matches:
                input_pos = self._input.mapToGlobal(self._input.rect().topLeft())
                popup_height = min(220, self._completer.sizeHintForRow(0) * self._completer.count() + 4)
                self._completer.setFixedWidth(self._input.width())
                self._completer.move(input_pos.x(), input_pos.y() - popup_height)
                self._completer.show()
            else:
                self._completer.hide()
        else:
            self._completer.hide()

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
        self._send_btn.setEnabled(enabled)
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
