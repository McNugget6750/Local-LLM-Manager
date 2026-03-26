"""
slash_completer.py — popup QListWidget for slash command autocomplete.

Appears above the chat input when the user types '/'. Filters as
characters are added. Emits command_chosen(str) on selection.
"""
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
]


class SlashCompleter(QListWidget):
    """Popup that autocompletes slash commands above the chat input."""

    command_chosen = Signal(str)   # emits the full command string, e.g. "/clear"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
        )
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMaximumHeight(220)
        self.itemClicked.connect(self._on_item_clicked)
        self._populate(SLASH_COMMANDS)

    # ── Public API ───────────────────────────────────────────────────────────

    def update_filter(self, prefix: str) -> bool:
        """Repopulate list with commands matching prefix. Returns True if any match."""
        prefix_lower = prefix.lower()
        matches = [(cmd, desc) for cmd, desc in SLASH_COMMANDS
                   if cmd.startswith(prefix_lower)]
        self._populate(matches)
        if self.count() > 0:
            self.setCurrentRow(0)
        return self.count() > 0

    def select_current(self) -> None:
        """Emit command_chosen for the currently highlighted row."""
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
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if cmd:
            self.command_chosen.emit(cmd)
            self.hide()
