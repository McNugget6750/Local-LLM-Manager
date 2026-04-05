"""
constants.py — Shared URL constants and Rich console.

Imported by chat.py, agents.py, commands.py, and tools.py.
No project-local imports — safe as a leaf node in the import graph.
"""
from rich.console import Console

BASE_URL    = "http://localhost:1234"
CONTROL_URL = "http://localhost:1235"   # server_manager.py control API
TTS_URL     = "http://127.0.0.1:1236"  # eli_server TTS + transcribe

console = Console()
