"""
slash_completer.py — popup QListWidget for slash command autocomplete.

Appears above the chat input when the user types '/'. Filters as
characters are added. Emits command_chosen(str) on selection.
"""
from __future__ import annotations
import os
from pathlib import Path
from PySide6.QtWidgets import QListWidget, QListWidgetItem
from PySide6.QtCore import Qt, Signal

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/clear",         "Reset message history"),
    ("/tools",         "List available tools"),
    ("/think",         "Set thinking level: off | on | deep"),
    ("/save",          "Save conversation to JSON"),
    ("/compact",       "Summarise older messages to free context"),
    ("/status",        "Show token usage and context window info"),
    ("/sessions",      "List saved sessions"),
    ("/resume",        "Load a saved session"),
    ("/approval",      "Set approval tier: auto | ask-writes | ask-all | yolo"),
    ("/cd",            "Set working directory for bash commands"),
    ("/pwd",           "Show current working directory"),
    ("/model",         "Switch model or list available models"),
    ("/role",          "Adopt an agent persona in the current session"),
    ("/config",        "Show loaded eli.toml project config"),
    ("/skills",        "List available skills"),
    ("/skill",         "Invoke a skill explicitly"),
    ("/queue-results", "List recent agent queue runs"),
    ("/excerpt",       "Insert selected editor code into prompt with line numbers"),
]


def load_skill_commands(skills_dir: str) -> list[tuple[str, str]]:
    """Read skills/*.md and return [("/skillname", description), ...]."""
    result = []
    skills_path = Path(skills_dir)
    if not skills_path.is_dir():
        return result
    for md in sorted(skills_path.glob("*.md")):
        name = f"/{md.stem}"
        desc = ""
        try:
            for line in md.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#"):
                    desc = line.lstrip("#").strip()
                    break
                if line and not line.startswith("---"):
                    desc = line[:80]
                    break
        except Exception:
            pass
        result.append((name, desc or md.stem))
    return result


class SlashCompleter(QListWidget):
    """Popup that autocompletes slash commands above the chat input."""

    command_chosen = Signal(str)   # emits the full command string, e.g. "/clear"
    session_chosen = Signal(str)   # emits session stem when in session-picker mode

    def __init__(self, parent=None):
        super().__init__(parent)
        # Plain child widget — no Popup flag. Popup grabs OS keyboard events
        # and blocks typing in the input field even with NoFocus set.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMaximumHeight(220)
        self.itemClicked.connect(self._on_item_clicked)
        self._all_commands: list[tuple[str, str]] = list(SLASH_COMMANDS)
        self._session_mode: bool = False
        self._populate(self._all_commands)

    # ── Public API ───────────────────────────────────────────────────────────

    def add_commands(self, entries: list[tuple[str, str]]) -> None:
        """Append additional commands (e.g. skills) to the completer list."""
        self._all_commands.extend(entries)

    def update_filter(self, prefix: str) -> bool:
        """Repopulate list with commands matching prefix. Returns True if any match."""
        self._session_mode = False
        prefix_lower = prefix.lower()
        matches = [(cmd, desc) for cmd, desc in self._all_commands
                   if cmd.startswith(prefix_lower)]
        self._populate(matches)
        if self.count() > 0:
            self.setCurrentRow(0)
        return self.count() > 0

    def set_sessions(self, sessions: list[dict], filter_text: str = "") -> bool:
        """Switch to session-picker mode and populate with session names."""
        self._session_mode = True
        f = filter_text.lower()
        matches = [s for s in sessions if not f or f in s["stem"].lower()]
        self.clear()
        for s in matches:
            saved = s.get("saved_at", "")[:16]
            n = s.get("n_messages", 0)
            label = f"{s['stem']}  —  {saved}  ({n} msgs)"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, s["stem"])
            self.addItem(item)
        if self.count() > 0:
            self.setCurrentRow(0)
        return self.count() > 0

    def select_current(self) -> None:
        """Emit the appropriate signal for the currently highlighted row."""
        item = self.currentItem()
        if item:
            self._emit(item)

    def move_selection(self, delta: int) -> None:
        """Move selection up (delta=-1) or down (delta=1), clamped to list bounds."""
        row = max(0, min(self.count() - 1, self.currentRow() + delta))
        self.setCurrentRow(row)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _populate(self, commands: list[tuple[str, str]]) -> None:
        self.clear()
        for cmd, desc in commands:
            item = QListWidgetItem(f"{cmd}  —  {desc}")
            item.setData(Qt.ItemDataRole.UserRole, cmd)
            self.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._emit(item)

    def _emit(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            if self._session_mode:
                self.session_chosen.emit(data)
            else:
                self.command_chosen.emit(data)
            self.hide()
