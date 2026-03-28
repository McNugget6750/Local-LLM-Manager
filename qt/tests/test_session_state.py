"""Tests for session_state.py — persistence helpers."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch


# ── parse_agent_name ──────────────────────────────────────────────────────────

def test_parse_agent_name_from_yaml_style(tmp_path, monkeypatch):
    """Reads 'name: Eli' from ELI.md."""
    eli = tmp_path / "ELI.md"
    eli.write_text("name: Eli\n# Some heading\n\nContent.", encoding="utf-8")
    monkeypatch.setattr("qt.session_state._ELI_MD", eli)
    from qt.session_state import parse_agent_name
    assert parse_agent_name() == "Eli"


def test_parse_agent_name_from_heading(tmp_path, monkeypatch):
    """Falls back to first # Heading when no name: line."""
    eli = tmp_path / "ELI.md"
    eli.write_text("# MyAgent\n\nContent.", encoding="utf-8")
    monkeypatch.setattr("qt.session_state._ELI_MD", eli)
    from qt.session_state import parse_agent_name
    assert parse_agent_name() == "MyAgent"


def test_parse_agent_name_fallback(tmp_path, monkeypatch):
    """Returns 'Assistant' when ELI.md is absent."""
    monkeypatch.setattr("qt.session_state._ELI_MD", tmp_path / "ELI.md")
    from qt.session_state import parse_agent_name
    assert parse_agent_name() == "Assistant"


# ── get_agent_name ────────────────────────────────────────────────────────────

def test_get_agent_name_prefers_state(tmp_path, monkeypatch):
    """state.json agent_name beats ELI.md."""
    monkeypatch.setattr("qt.session_state._ELI_MD", tmp_path / "ELI.md")
    from qt.session_state import get_agent_name
    assert get_agent_name({"agent_name": "Aria"}) == "Aria"


def test_get_agent_name_empty_state_falls_back(tmp_path, monkeypatch):
    """Empty/missing agent_name in state falls back to ELI.md parsing."""
    monkeypatch.setattr("qt.session_state._ELI_MD", tmp_path / "ELI.md")
    from qt.session_state import get_agent_name
    assert get_agent_name({}) == "Assistant"


# ── list_sessions ─────────────────────────────────────────────────────────────

def test_list_sessions_returns_sorted_recent_first(tmp_path, monkeypatch):
    """list_sessions returns sessions sorted newest first, excluding state.json."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    for name in ("2026-01-01_10-00-00", "2026-03-15_14-00-00"):
        (sessions_dir / f"{name}.json").write_text(
            json.dumps({"saved_at": f"{name}", "token_estimate": 100, "messages": []}),
            encoding="utf-8",
        )
    (sessions_dir / "state.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("qt.session_state._SESSIONS_DIR", sessions_dir)
    from qt import session_state
    import importlib; importlib.reload(session_state)

    sessions = session_state.list_sessions()
    assert len(sessions) == 2
    assert sessions[0]["stem"] == "2026-03-15_14-00-00"  # newest first
    assert sessions[1]["stem"] == "2026-01-01_10-00-00"
    assert all("state" not in s["stem"] for s in sessions)


def test_list_sessions_empty_dir(tmp_path, monkeypatch):
    """list_sessions returns [] when sessions dir is empty."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("qt.session_state._SESSIONS_DIR", sessions_dir)
    from qt import session_state
    import importlib; importlib.reload(session_state)
    assert session_state.list_sessions() == []
