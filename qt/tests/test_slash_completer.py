"""Tests for SlashCompleter popup widget."""
import pytest
from qt.slash_completer import SlashCompleter, SLASH_COMMANDS


def test_initial_count_matches_commands(qapp):
    """Completer starts with all slash commands listed."""
    c = SlashCompleter()
    assert c.count() == len(SLASH_COMMANDS)


def test_update_filter_narrows_results(qapp):
    """/co narrows to commands starting with /co."""
    c = SlashCompleter()
    has_matches = c.update_filter("/co")
    assert has_matches
    for i in range(c.count()):
        item_cmd = c.item(i).data(32)  # Qt.ItemDataRole.UserRole == 32
        assert item_cmd.startswith("/co"), f"{item_cmd} doesn't start with /co"


def test_update_filter_no_match_returns_false(qapp):
    """Filter with no matching prefix returns False."""
    c = SlashCompleter()
    result = c.update_filter("/zzzznotacommand")
    assert result is False
    assert c.count() == 0


def test_update_filter_exact_match(qapp):
    """/clear matches exactly one command."""
    c = SlashCompleter()
    c.update_filter("/clear")
    assert c.count() == 1
    assert c.item(0).data(32) == "/clear"


def test_command_chosen_signal_on_select_current(qapp, qtbot):
    """select_current() emits command_chosen with the selected command string."""
    c = SlashCompleter()
    c.update_filter("/cle")  # matches /clear
    c.setCurrentRow(0)

    received = []
    c.command_chosen.connect(received.append)
    c.select_current()

    assert received == ["/clear"]


def test_move_selection_clamps(qapp):
    """move_selection() does not go out of bounds."""
    c = SlashCompleter()
    c.setCurrentRow(0)
    c.move_selection(-5)
    assert c.currentRow() == 0

    c.setCurrentRow(c.count() - 1)
    c.move_selection(100)
    assert c.currentRow() == c.count() - 1
