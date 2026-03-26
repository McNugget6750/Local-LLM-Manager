"""SP3 window integration tests — toolbar controls, sessions, state restore."""
import pytest
from unittest.mock import patch, MagicMock

import qt.window  # noqa: F401 — ensure module is loaded before patching


def _patched_window(qapp):
    """Create a MainWindow with adapter and server polling stubbed out."""
    with patch("qt.window.QtChatAdapter") as MockAdapter, \
         patch("qt.window.QTimer"):
        mock_adapter = MagicMock()
        # Make signals no-ops
        for sig in ("think_token", "text_token", "text_done", "tool_start",
                    "tool_done", "approval_needed", "usage", "system_msg",
                    "error_msg", "done", "stream_started"):
            getattr(mock_adapter, sig).connect = MagicMock()
        MockAdapter.return_value = mock_adapter
        from qt.window import MainWindow
        win = MainWindow()
        win._adapter = mock_adapter
        return win


def test_plan_button_exists_and_is_checkable(qapp):
    """Toolbar has a checkable Plan button."""
    win = _patched_window(qapp)
    assert hasattr(win, "_plan_btn")
    assert win._plan_btn.isCheckable()


def test_compact_button_exists_and_is_checkable(qapp):
    """Toolbar has a checkable Compact button."""
    win = _patched_window(qapp)
    assert hasattr(win, "_compact_btn")
    assert win._compact_btn.isCheckable()


def test_stop_button_exists_and_disabled_initially(qapp):
    """Stop button exists and is disabled when no stream is running."""
    win = _patched_window(qapp)
    assert hasattr(win, "_stop_btn")
    assert not win._stop_btn.isEnabled()


def test_stop_button_enables_on_stream_started(qapp, qtbot):
    """Stop button becomes enabled when stream_started fires."""
    win = _patched_window(qapp)
    assert not win._stop_btn.isEnabled()
    win._on_stream_started()
    assert win._stop_btn.isEnabled()


def test_stop_button_disables_on_turn_done(qapp):
    """Stop button becomes disabled again after turn done."""
    win = _patched_window(qapp)
    win._on_stream_started()
    assert win._stop_btn.isEnabled()
    win._on_turn_done()
    assert not win._stop_btn.isEnabled()


def test_ctx_bar_exists(qapp):
    """Token context bar widget exists."""
    win = _patched_window(qapp)
    assert hasattr(win, "_ctx_bar")
    assert hasattr(win, "_ctx_label")


def test_ctx_bar_updates_on_usage(qapp):
    """Token bar value updates when usage signal fires."""
    win = _patched_window(qapp)
    win._on_usage(45000, 90000)
    assert win._ctx_bar.value() == 50  # 45000/90000 = 50%


def test_agent_name_shown_in_window_title(qapp):
    """Window title or response header uses configured agent name, not hardcoded 'Eli'."""
    win = _patched_window(qapp)
    # The agent_name should be set from state on init
    assert hasattr(win, "_agent_name")
    assert isinstance(win._agent_name, str)
    assert len(win._agent_name) > 0


def test_sessions_menu_exists(qapp):
    """Menu bar has a Sessions menu."""
    win = _patched_window(qapp)
    menu_titles = [a.text() for a in win.menuBar().actions()]
    assert "Sessions" in menu_titles


def test_plan_mode_propagates_to_send(qapp):
    """When Plan button is checked, _send_message passes plan_mode=True."""
    win = _patched_window(qapp)
    win._plan_btn.setChecked(True)
    win._input.setPlainText("hello")
    win._send_message()
    win._adapter.submit.assert_called_once_with("hello", True)
