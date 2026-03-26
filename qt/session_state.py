"""
session_state.py — Qt-facing wrapper around chat.py session persistence.

Provides: load_state, save_state, list_sessions, load_session,
          parse_agent_name, get_agent_name.
"""
import json
import re
import sys
import pathlib

# Ensure chat.py is importable
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from chat import (
    _load_state as _chat_load_state,
    _save_state as _chat_save_state,
    _load_session as _chat_load_session,
    SESSIONS_DIR as _SESSIONS_DIR_ORIG,
)

# Module-level paths (monkeypatched in tests).
# Use setdefault-style: only initialise if not already set, so that
# monkeypatch.setattr followed by importlib.reload() keeps the patched value.
import sys as _sys
_this_mod = _sys.modules.get(__name__)
_ELI_MD       = getattr(_this_mod, "_ELI_MD",       pathlib.Path(__file__).parent.parent / "ELI.md")
_SESSIONS_DIR = getattr(_this_mod, "_SESSIONS_DIR", _SESSIONS_DIR_ORIG)


def load_state() -> dict:
    """Load persisted GUI state (think_level, approval_level, agent_name, …)."""
    return _chat_load_state()


def save_state(**kwargs) -> None:
    """Merge kwargs into the persistent state file."""
    _chat_save_state(**kwargs)


def list_sessions() -> list[dict]:
    """Return up to 10 recent sessions, newest first. Excludes state.json."""
    if not _SESSIONS_DIR.exists():
        return []
    entries = []
    for p in sorted(_SESSIONS_DIR.glob("*.json"), reverse=True):
        if p.name == "state.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            entries.append({
                "stem": p.stem,
                "saved_at": data.get("saved_at", "")[:16].replace("T", " "),
                "token_estimate": data.get("token_estimate", 0),
                "n_messages": len(data.get("messages", [])),
                "path": p,
            })
        except Exception:
            entries.append({
                "stem": p.stem, "saved_at": "", "token_estimate": 0,
                "n_messages": 0, "path": p,
            })
    return entries[:10]


def load_session(name: str | None = None):
    """Load named (or latest) session. Returns (messages, path) or (None, None)."""
    return _chat_load_session(name)


def parse_agent_name() -> str:
    """Read agent name from ELI.md. Priority: 'name: X' line > first # Heading > 'Assistant'."""
    if not _ELI_MD.exists():
        return "Assistant"
    try:
        text = _ELI_MD.read_text(encoding="utf-8")
        m = re.search(r'^name:\s*(.+)', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
        m = re.search(r'^#\s+(.+)', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return "Assistant"


def get_agent_name(state: dict) -> str:
    """Get agent name: state['agent_name'] > ELI.md > 'Assistant'."""
    name = state.get("agent_name", "").strip()
    return name if name else parse_agent_name()
