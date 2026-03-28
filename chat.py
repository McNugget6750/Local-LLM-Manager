"""
chat.py — Terminal chat client for Qwen3 via llama-server (OpenAI-compatible API).
Connects to localhost:1234, supports tool use (bash, read_file, write_file, list_dir).
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
        f"Use chat.bat or: .venv\\Scripts\\python.exe chat.py",
        file=sys.stderr,
    )

# ── Imports ───────────────────────────────────────────────────────────────────
import asyncio
import fnmatch
import json
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL     = "http://localhost:1234"
CONTROL_URL  = "http://localhost:1235"   # server_manager.py control API
TTS_URL      = "http://127.0.0.1:1236"  # eli_server TTS + transcribe
MODEL = "auto"

# ── Tool announce fallback (shown when model emits no text before first tool call) ──
_TOOL_STATUS = {
    "spawn_agent":  "Looking into this…",
    "queue_agents": "Spinning up agents…",
    "web_search":   "Searching the web…",
    "web_fetch":    "Fetching that…",
    "read_file":    "Reading the file…",
    "list_dir":     "Listing directory…",
    "glob":         "Searching files…",
    "grep":         "Searching code…",
    "ripgrep":      "Searching code…",
    "bash":         "Running a command…",
    "edit":         "Editing…",
    "write_file":   "Writing the file…",
    "speak":        "Speaking…",
}

def _tool_announce(tool_calls: list) -> str:
    names = [tc["function"]["name"] for tc in tool_calls]
    if len(names) == 1:
        return _TOOL_STATUS.get(names[0], f"{names[0]}…")
    return "  /  ".join(_TOOL_STATUS.get(n, f"{n}…") for n in names)

# ── Voice mode config ──────────────────────────────────────────────────────────
PTT_KEY          = "scroll_lock"   # pynput Key name or single char
VOICE_DEFAULT_MODE = "ptt"         # "ptt" or "auto"
VOICE_SAMPLE_RATE     = 16000  # Hz — must match what /transcribe expects
VOICE_SILENCE_TIMEOUT = 2.5    # seconds of silence before auto-send (auto mode)
VOICE_MIN_SPEECH_MS   = 500    # ms of speech required before transcribing (auto mode)
VOICE_RMS_THRESHOLD      = 650  # int16 RMS below this is ignored even if VAD says speech
VOICE_ONSET_FRAMES       = 2    # consecutive speech frames required before recording starts
VOICE_POST_TTS_DELAY  = 0.6    # seconds to wait after TTS finishes before listening again

VOICE_SYSTEM_PROMPT = """You are a sharp, engaged conversational partner.
You're a colleague and friend — not an assistant, not a tool.

Rules:
- Respond in 2–4 sentences. You're in a voice conversation — keep it tight.
- Challenge ideas. Push back when something seems off. Ask the one question
  that cuts to the core of the matter.
- No preamble. No affirmations. Just engage directly with what was said.
- Think out loud with the person. Build on their idea or dismantle it.
- When you need to go deeper, do — but never ramble.
- No lists, no bullet points. Spoken prose only.
"""

def _vision_url() -> str:
    """Read vision server URL from commands.json _meta, fallback to localhost:1236."""
    return _load_commands_meta().get("vision_url", "http://localhost:1236")

def _load_system_prompt() -> str:
    eli_md = Path(__file__).parent / "ELI.md"
    if eli_md.exists():
        return eli_md.read_text(encoding="utf-8")
    # Fallback if ELI.md is missing
    return (
        "You are Eli, a local AI coding assistant running on Qwen3. "
        "You have access to tools: bash, read_file, write_file, edit, list_dir, glob, grep, web_fetch, web_search. "
        "Use them proactively. Prefer edit over write_file. Be concise and direct."
    )

SYSTEM_PROMPT = _load_system_prompt()

def _load_memory() -> str | None:
    mem = Path(__file__).parent / "MEMORY.md"
    if mem.exists():
        return mem.read_text(encoding="utf-8")
    return None

def _load_commands() -> dict:
    """Load model profiles from commands.json. Skips meta keys (starting with _)."""
    commands_file = Path(__file__).parent / "commands.json"
    if not commands_file.exists():
        return {}
    try:
        data = json.loads(commands_file.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}
    except Exception:
        return {}

def _load_commands_meta() -> dict:
    """Load the _meta block from commands.json (profile descriptions, vision_url, etc.)."""
    commands_file = Path(__file__).parent / "commands.json"
    if not commands_file.exists():
        return {}
    try:
        data = json.loads(commands_file.read_text(encoding="utf-8"))
        return data.get("_meta", {})
    except Exception:
        return {}

def _build_model_context() -> str | None:
    """Format available model profiles + descriptions as a system message for injection at startup."""
    commands = _load_commands()
    meta = _load_commands_meta()
    if not commands:
        return None
    profiles_meta = meta.get("profiles", {})
    vision_url = meta.get("vision_url", "")
    lines = ["[Available Model Profiles — commands.json]", ""]
    lines.append("Use these profile names exactly when calling spawn_agent(model=...) or queue_agents(agents=[{model: ...}]).")
    lines.append("")
    for name in commands:
        m = profiles_meta.get(name, {})
        lines.append(f"• {name}")
        if m.get("description"):
            lines.append(f"  {m['description']}")
        if m.get("strengths"):
            lines.append(f"  Strengths: {m['strengths']}")
        if m.get("weaknesses"):
            lines.append(f"  Weaknesses: {m['weaknesses']}")
        if m.get("speed"):
            lines.append(f"  Speed: {m['speed']}")
        if m.get("vision"):
            lines.append(f"  Vision: yes")
        lines.append("")
    if vision_url:
        lines.append(f"Vision API: {vision_url}  (use analyze_image tool for image analysis)")
    return "\n".join(lines).strip()

async def _control(method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the server_manager control API. Returns parsed JSON or None on failure."""
    try:
        async with httpx.AsyncClient() as c:
            if method == "GET":
                r = await c.get(f"{CONTROL_URL}{path}", timeout=5)
            else:
                r = await c.post(f"{CONTROL_URL}{path}", json=body or {}, timeout=5)
            return r.json()
    except Exception:
        return None

async def _find_active_profile() -> str | None:
    """Ask server_manager which profile is currently running. Returns profile name or None."""
    data = await _control("GET", "/api/status")
    if data and data.get("running") and data.get("model"):
        return data["model"]
    return None

async def _switch_server(profile: str, timeout: int = 120) -> bool:
    """Ask server_manager to switch to a named profile and wait until the server is healthy.

    Flow: POST /api/stop → poll until server goes down → POST /api/start →
          poll /health until the new model is accepting requests.
    """
    # 1. Stop
    console.print(f"[dim yellow]  Requesting stop via Server Manager...[/dim yellow]")
    result = await _control("POST", "/api/stop")
    if result is None:
        console.print("[red]  Server Manager not reachable on port 1235. Is it running?[/red]")
        return False

    # 2. Wait for server to go down (max 30 s)
    for i in range(30):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient() as probe:
                await probe.get(f"{BASE_URL}/health", timeout=1)
        except Exception:
            break  # connection refused — server is down
    else:
        console.print("[dim yellow]  Server still up after 30 s, continuing anyway...[/dim yellow]")
    await asyncio.sleep(1)  # brief settle

    # 3. Start the requested profile
    console.print(f"[dim yellow]  Requesting start: {profile}[/dim yellow]")
    result = await _control("POST", "/api/start", {"profile": profile})
    if result is None or "error" in result:
        err = result.get("error", "unknown error") if result else "no response"
        console.print(f"[red]  Start failed: {err}[/red]")
        return False

    # 4. Poll until healthy
    for i in range(timeout):
        await asyncio.sleep(1)
        try:
            async with httpx.AsyncClient() as probe:
                r = await probe.get(f"{BASE_URL}/health", timeout=2)
                if r.status_code == 200:
                    console.print(f"[dim green]  Server ready after {i + 1}s[/dim green]")
                    return True
        except Exception:
            pass
    console.print(f"[red]  Server failed to become healthy within {timeout}s[/red]")
    return False

def _load_mission_objective() -> str | None:
    """Load MISSION_OBJECTIVE.md from cwd or any parent (up to 5 levels)."""
    path = Path.cwd()
    for _ in range(5):
        candidate = path / "MISSION_OBJECTIVE.md"
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8")
            except Exception:
                return None
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None


def _build_initial_messages() -> list[dict]:
    msgs = [{"role": "system", "content": _load_system_prompt()}]
    loaded: list[str] = ["ELI.md"]

    memory = _load_memory()
    if memory:
        msgs.append({"role": "system", "content": f"[Operational Memory]\n\n{memory}"})
        loaded.append("MEMORY.md")

    mission = _load_mission_objective()
    if mission:
        msgs.append({"role": "system", "content": f"[Mission Objective — auto-loaded from MISSION_OBJECTIVE.md]\n\n{mission}"})
        loaded.append("MISSION_OBJECTIVE.md")

    model_ctx = _build_model_context()
    if model_ctx:
        msgs.append({"role": "system", "content": model_ctx})

    console.print(f"[dim]Context loaded: {', '.join(loaded)}[/dim]")
    return msgs

def _load_project_config(cwd: Path) -> dict:
    """Walk up from cwd looking for eli.toml (up to 10 levels). Returns {} if not found."""
    try:
        import tomllib as _tomllib
    except ImportError:
        return {}
    path = cwd
    for _ in range(10):
        candidate = path / "eli.toml"
        if candidate.exists():
            try:
                return _tomllib.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                return {}
        parent = path.parent
        if parent == path:
            break
        path = parent
    return {}


def _format_project_config(config: dict) -> str:
    """Format eli.toml contents as a system message string."""
    lines = ["[Project Config — eli.toml]"]
    project = config.get("project", {})
    if project.get("name"):
        lines.append(f"Project: {project['name']}")
    build = config.get("build", {})
    if build.get("command"):
        cwd_note = f"  (cwd: {build['cwd']})" if build.get("cwd") else ""
        lines.append(f"Build: {build['command']}{cwd_note}")
    test_cfg = config.get("test", {})
    if test_cfg.get("command"):
        cwd_note = f"  (cwd: {test_cfg['cwd']})" if test_cfg.get("cwd") else ""
        lines.append(f"Test: {test_cfg['command']}{cwd_note}")
    tools = config.get("tools", {})
    for k, v in tools.items():
        lines.append(f"{k}: {v}")
    hooks = config.get("hooks", {})
    if hooks:
        hook_parts = [f"{pat} → {action}" for pat, action in hooks.items()]
        lines.append(f"Hooks: {' | '.join(hook_parts)}")
    return "\n".join(lines)


# ── Session persistence ───────────────────────────────────────────────────────
def _session_token_estimate(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages) // CHARS_PER_TOKEN

def _save_session(messages: list[dict], n_fixed: int, session_path: Path | None = None) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    if session_path is None:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session_path = SESSIONS_DIR / f"{ts}.json"
    conversation = messages[n_fixed:]
    data = {
        "saved_at": datetime.now().isoformat(),
        "token_estimate": _session_token_estimate(conversation),
        "messages": conversation,
    }
    session_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    all_sessions = sorted(p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json")
    for old in all_sessions[:-MAX_SESSIONS]:
        try:
            old.unlink()
        except Exception:
            pass
    _save_state(last_session=session_path.stem)
    return session_path

def _load_session(name: str | None = None) -> tuple[list[dict], Path] | tuple[None, None]:
    if not SESSIONS_DIR.exists():
        return None, None
    all_sessions = sorted(p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json")
    if not all_sessions:
        return None, None
    if name:
        candidates = [s for s in all_sessions if name in s.stem]
        if not candidates:
            return None, None
        target = candidates[-1]
    else:
        target = all_sessions[-1]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data.get("messages", []), target
    except Exception:
        return None, None

def _load_state() -> dict:
    """Load persisted session settings (think level, role, etc.)."""
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_state(**kwargs) -> None:
    """Merge kwargs into the persistent state file (creates it if absent)."""
    SESSIONS_DIR.mkdir(exist_ok=True)
    try:
        current = _load_state()
        current.update(kwargs)
        STATE_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ── Compaction constants ──────────────────────────────────────────────────────
CTX_WINDOW           = 32_768   # fallback if /slots doesn't respond
CTX_COMPACT_THRESH   = 0.80     # trigger history compaction at this fraction
CTX_KEEP_RECENT      = 6        # tail messages kept verbatim after compact
INPUT_COMPRESS_CHARS = 8_000    # auto-compress user input above this char count
CHARS_PER_TOKEN      = 4        # fallback estimator when server usage unavailable

SESSIONS_DIR = Path(__file__).parent / "sessions"
STATE_FILE   = SESSIONS_DIR / "state.json"
MAX_SESSIONS = 10

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command and return combined stdout+stderr. "
                "The working directory defaults to the session cwd. "
                "Use the 'cwd' parameter to run in a different directory — "
                "prefer this over 'cd X && command' to avoid venv path confusion."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",  "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                    "cwd":     {"type": "string",  "description": "Working directory for this command (absolute path)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read and return the contents of a file with line numbers. "
                "Use offset and limit to read a specific range of lines (1-based). "
                "Line numbers are shown as '  N | content' — do NOT include them in old_string when editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string",  "description": "File path to read"},
                    "offset": {"type": "integer", "description": "First line to read, 1-based (default: 1)"},
                    "limit":  {"type": "integer", "description": "Maximum number of lines to return (default: 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating parent directories as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: current dir)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern (supports ** for recursive search).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                    "path": {"type": "string", "description": "Root directory to search from (default: current dir)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents for a regex pattern, optionally filtered by file glob.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory or file to search (default: current dir)"},
                    "glob": {"type": "string", "description": "File glob filter, e.g. '*.py' (default: all files)"},
                    "case_insensitive": {"type": "boolean", "description": "Case-insensitive search (default: false)"},
                    "context_lines": {"type": "integer", "description": "Lines of context around each match (default: 2)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ripgrep",
            "description": (
                "Fast code search using ripgrep (rg). Preferred over grep for large codebases. "
                "Supports regex, file-type filters, fixed-string search, context lines."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern (or literal if fixed_strings=true)"},
                    "path": {"type": "string", "description": "Directory or file to search (default '.')"},
                    "glob": {"type": "string", "description": "File glob filter e.g. '*.cpp'"},
                    "type_filter": {"type": "string", "description": "ripgrep file type e.g. 'cpp', 'py', 'rust'"},
                    "case_insensitive": {"type": "boolean", "description": "Case-insensitive search"},
                    "context_lines": {"type": "integer", "description": "Lines of context around matches (default 2)"},
                    "fixed_strings": {"type": "boolean", "description": "Treat pattern as literal string (-F flag)"},
                    "max_results": {"type": "integer", "description": "Max matches to return (default 100)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace an exact string in a file with new content. Fails if old_string is not found or matches multiple times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return its content as plain text (HTML is converted to readable text).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo and return titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Number of results to return (default: 6)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "Read, create, or update a TASKS.md task list for tracking multi-step work. Use 'read' to check current tasks, 'create' to start a new list, 'update' to check/uncheck a task by 0-based index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {"type": "string", "description": "'read', 'create', or 'update'"},
                    "path": {"type": "string", "description": "Path to TASKS.md (default: TASKS.md in current dir)"},
                    "content": {"type": "string", "description": "Full markdown content for 'create' operation"},
                    "index": {"type": "integer", "description": "0-based task index for 'update' operation"},
                    "checked": {"type": "boolean", "description": "True to check off, False to uncheck (for 'update')"},
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn a single specialized sub-agent and return its result. "
                "Use for one-off tasks: code review, documentation, research, test writing. "
                "Agent profiles: code-review, doc-writer, researcher, test-writer, web_designer. "
                "Do NOT specify a model unless the user explicitly requested a different one — "
                "only use profile names listed in the system context (commands.json). "
                "The server switches automatically and restores the original model when done. "
                "For multiple agents or pipelines, use queue_agents instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "system_prompt": {
                        "type": "string",
                        "description": (
                            "Agent profile name (e.g. 'code-review') OR a raw system "
                            "prompt string. If the value contains no whitespace it is "
                            "treated as a profile name and loaded from agents/<name>.md."
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "The task to give the agent.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tool whitelist. Omit for all tools except spawn_agent.",
                    },
                    "think_level": {
                        "type": "string",
                        "description": "Thinking level: 'off', 'on', or 'deep'. Defaults to parent level.",
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "Max tool-use iterations (default 10, hard max 50). Increase for large codebases or deep research — code-review may need 20–30, research 40–50.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model profile name from commands.json to use for this agent. "
                            "The server switches to this model before the agent runs and restores "
                            "the original model afterward. Profile names match the keys in commands.json."
                        ),
                    },
                },
                "required": ["system_prompt", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": (
                "Send one or more images to the local vision model and return analysis. "
                "If vision_external is false in commands.json _meta, the server will switch "
                "to the vision model, process all images in sequence, then restore the text "
                "model — minimising load/unload cycles. If vision_external is true the vision "
                "server is assumed always-on (separate machine/GPU) and no switching occurs. "
                "Use image_path for a single image, or images[] for a batch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Path to a single image file (jpg, png, webp). Ignored if images[] is provided.",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of image paths to analyse in sequence. Use this for batch processing.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to ask about each image. Applied to all images in a batch.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_agents",
            "description": (
                "Run a sequence of agents one after another, each with its own task, model, "
                "and time budget. Results are stored to disk and returned as a summary. "
                "Use this for research or development pipelines that need multiple agents. "
                "Agents run sequentially — model switches only when the next agent needs a "
                "different model (no redundant reloads). The original model is restored after "
                "the entire queue completes. Each agent gets a per-agent timeout; on timeout "
                "the agent is asked to summarise before moving on."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "description": "Ordered list of agent specs to run.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "system_prompt":    {"type": "string", "description": "Agent profile name or raw system prompt."},
                                "task":             {"type": "string", "description": "Task to give this agent."},
                                "model":            {"type": "string", "description": "Optional profile name from commands.json. Only set if the user explicitly requested a different model — do not guess or invent model names."},
                                "timeout_seconds":  {"type": "integer", "description": "Max seconds for this agent (default 300)."},
                                "tools":            {"type": "array", "items": {"type": "string"}, "description": "Optional tool whitelist."},
                                "think_level":      {"type": "string", "description": "'off', 'on', or 'deep'."},
                                "max_iterations":   {"type": "integer", "description": "Max tool-use iterations (default 10, hard max 50). Increase for large codebases or deep research — code-review may need 20–30, research 40–50."},
                            },
                            "required": ["system_prompt", "task"],
                        },
                    },
                    "label": {
                        "type": "string",
                        "description": "Human-readable label for this queue run (used in filenames and display).",
                    },
                },
                "required": ["agents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": (
                "Speak a brief message aloud via the local TTS server. "
                "Use for: task-done notifications, questions that need the user at the keyboard, "
                "unexpected blockers. "
                "Keep to 1–2 short sentences — conversational length. "
                "Do NOT narrate ongoing work, repeat what is already on screen, or read out long results. "
                "Formatting rules: substitute '.' with '...' to create natural pauses. "
                "Only use punctuation from this set: . , ? ! ' "
                "Write currency as words, e.g. '20.45 Dollars' not '$20.45'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak (1–2 sentences max). Use '...' for pauses. Only .,?!' punctuation allowed."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "highlight_in_editor",
            "description": (
                "Highlight a passage in the GUI editor panel with a yellow background. "
                "Use this to draw the user's attention to a specific part of the open file — "
                "e.g. after explaining something, to show exactly which lines you mean. "
                "The highlight is cleared when the user clicks, sends a message, or the file is edited. "
                "For a single-line character range, also provide start_col and end_col."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string",  "description": "Absolute path to the file (must already be open or openable in the editor)."},
                    "start_line": {"type": "integer", "description": "First line to highlight (1-based)."},
                    "end_line":   {"type": "integer", "description": "Last line to highlight (1-based, inclusive)."},
                    "start_col":  {"type": "integer", "description": "Start column for character-level highlight on a single line (0-based, optional)."},
                    "end_col":    {"type": "integer", "description": "End column for character-level highlight on a single line (0-based, exclusive, optional)."},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_in_editor",
            "description": (
                "Open a file in the GUI editor panel and scroll to a specific line. "
                "Use this when the user asks 'where is X in the code?' or 'can you show me Y?' "
                "so they can see the relevant location without manually navigating to it. "
                "The user will be shown a confirmation dialog if the file differs from the one "
                "currently open."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to open."},
                    "line": {"type": "integer", "description": "1-based line number to scroll to (optional)."},
                },
                "required": ["path"],
            },
        },
    },
]

DANGEROUS_PATTERNS = [
    # Filesystem destruction
    "rm -rf",
    "rmdir /s",
    "format ",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "del /f /s /q",
    # Process termination
    "taskkill",
    "kill -9",
    "kill -sigkill",
    "pkill",
    "killall",
    "stop-process",
    # Git — irreversible or history-rewriting operations
    "git push --force",
    "git push -f ",
    "git push -f\t",
    "git reset --hard",
    "git clean -f",
    "git checkout -- .",
    "git restore .",
    "git rebase -i",
    "git filter-branch",
    "git filter-repo",
]

# ── Compact mode ──────────────────────────────────────────────────────────────
COMPACT_QUOTES = [
    "Scanning the databanks...",
    "Accessing the Grid...",
    "Consulting Deep Thought...",
    "Diving into the net...",
    "Searching through the ghost...",
    "Enhance... enhance...",
    "Triangulating source coordinates...",
    "Routing through the ansible...",
    "Querying subspace frequencies...",
    "Jacking into the Matrix...",
    "Interfacing with the mainframe...",
    "The Guide has an entry for this...",
    "Searching across all networks...",
    "Cross-referencing with WOPR...",
    "Initiating long-range sensor sweep...",
    "Accessing secure datalink...",
    "Running pattern recognition...",
    "The Machines are thinking...",
    "Searching through the tesseract...",
    "Probing memory cores...",
    "Navigating hyperspace index...",
    "Establishing contact with the oracle...",
    "Synchronizing with the hive...",
    "All frequencies open...",
    "Scanning for life signs... data incoming...",
]


class _NullLive:
    """Drop-in for rich.live.Live that silently discards all updates — used in compact mode."""
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def update(self, *_): pass
    def stop(self): pass
    def start(self): pass


console = Console()

# ── ToolCallAccumulator ───────────────────────────────────────────────────────
@dataclass
class ToolCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }

# ── Debug stream capture ──────────────────────────────────────────────────────
_debug_file: "IO[str] | None" = None
_debug_path: str = ""


def _debug_open(path: str) -> str:
    """Open (or reopen) the debug capture file. Returns the resolved path."""
    global _debug_file, _debug_path
    import datetime
    _debug_close()
    if not path or path in ("1", "on", "true"):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"debug_stream_{ts}.log"
    resolved = str(Path(path).resolve())
    _debug_file = open(resolved, "a", encoding="utf-8", buffering=1)  # line-buffered
    _debug_path = resolved
    return resolved


def _debug_close() -> None:
    global _debug_file, _debug_path
    if _debug_file:
        try:
            _debug_file.close()
        except Exception:
            pass
        _debug_file = None
        _debug_path = ""


def _debug_write_line(line: str) -> None:
    if _debug_file:
        try:
            _debug_file.write(line + "\n")
        except Exception:
            pass


# ── SSE stream parser ─────────────────────────────────────────────────────────
async def stream_events(
    response: httpx.Response,
    label: str = "",
) -> AsyncIterator[tuple[str, Any]]:
    """
    Parse an SSE stream from the llama-server and yield typed events:
      ("think", token)
      ("text", token)
      ("tool_calls", list[dict])
      ("stop", reason)
    """
    accumulators: dict[int, ToolCallAccumulator] = {}
    in_think = False
    # Holdback buffer for <think>/</ think> tag detection at chunk boundaries
    holdback = ""
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"
    MAX_HOLD = max(len(OPEN_TAG), len(CLOSE_TAG))

    def flush_text(token: str, is_thinking: bool):
        if token:
            yield ("think" if is_thinking else "text", token)

    if _debug_file:
        import datetime
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        _debug_write_line(f"\n{'='*72}")
        _debug_write_line(f"=== {ts}  {label}")
        _debug_write_line(f"{'='*72}")

    async for line in response.aiter_lines():
        if _debug_file:
            _debug_write_line(line)
        if not line.startswith("data: "):
            continue
        raw = line[6:]
        if raw.strip() == "[DONE]":
            break

        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue

        # Usage chunk: llama-server sends this when stream_options.include_usage=true
        usage = chunk.get("usage")
        if usage and not chunk.get("choices"):
            yield ("usage", usage)
            continue

        choice = chunk.get("choices", [{}])[0]
        finish_reason = choice.get("finish_reason")
        delta = choice.get("delta", {})

        # Accumulate tool call fragments
        for tc in delta.get("tool_calls", []):
            idx = tc.get("index", 0)
            if idx not in accumulators:
                accumulators[idx] = ToolCallAccumulator(index=idx)
            acc = accumulators[idx]
            acc.id = acc.id or tc.get("id", "")
            fn = tc.get("function", {})
            acc.name = acc.name or fn.get("name", "")
            acc.arguments += fn.get("arguments", "")

        # Handle text content with <think> tag state machine
        content = delta.get("content") or ""
        if content:
            holdback += content
            # Process holdback: emit safe prefix, keep potential-tag suffix
            while True:
                if in_think:
                    pos = holdback.find(CLOSE_TAG)
                    if pos != -1:
                        yield ("think", holdback[:pos])
                        holdback = holdback[pos + len(CLOSE_TAG):]
                        in_think = False
                    else:
                        # Keep last MAX_HOLD chars in holdback (might be partial tag)
                        safe = holdback[:-MAX_HOLD] if len(holdback) > MAX_HOLD else ""
                        if safe:
                            yield ("think", safe)
                        holdback = holdback[len(safe):]
                        break
                else:
                    pos = holdback.find(OPEN_TAG)
                    if pos != -1:
                        if pos > 0:
                            yield ("text", holdback[:pos])
                        holdback = holdback[pos + len(OPEN_TAG):]
                        in_think = True
                    else:
                        safe = holdback[:-MAX_HOLD] if len(holdback) > MAX_HOLD else ""
                        if safe:
                            yield ("text", safe)
                        holdback = holdback[len(safe):]
                        break

        if finish_reason == "tool_calls":
            # Flush holdback
            if holdback:
                yield ("think" if in_think else "text", holdback)
                holdback = ""
            yield ("tool_calls", [acc.to_dict() for acc in sorted(accumulators.values(), key=lambda a: a.index)])
        elif finish_reason == "stop":
            if holdback:
                yield ("think" if in_think else "text", holdback)
                holdback = ""
            yield ("stop", "stop")

# ── Text tool-call fallback parser ────────────────────────────────────────────
import re as _re, uuid as _uuid

def _try_parse_text_tool_calls(text: str) -> list[dict] | None:
    """Fallback for the 30B model whose jinja template emits tool calls as raw text.

    The qwen3-30b-a3b-chat-template.jinja instructs the model to output:

        <tool_call>
        <function=name>
        <parameter=param>
        value
        </parameter>
        </function>
        </tool_call>

    llama-server with --jinja should convert these to structured API tool_call
    objects, but occasionally passes them through as text. This parser catches
    that case.

    We REQUIRE the outer <tool_call>...</tool_call> wrapper — this is the
    strongest injection guard: casual prose descriptions of tool calls are very
    unlikely to include the full nested XML structure.

    Returns a list of tool call dicts (OpenAI shape), or None if no valid calls found.
    """
    calls = []

    # Primary format — <tool_call> wrapping <function=name><parameter=p>v</parameter></function>
    for tc_m in _re.finditer(r'<tool_call>(.*?)</tool_call>', text, _re.DOTALL):
        block = tc_m.group(1)

        # Inner block may be JSON: {"name": ..., "arguments": {...}}
        json_m = _re.search(r'\{.*\}', block, _re.DOTALL)
        if json_m:
            try:
                obj = json.loads(json_m.group(0))
                name = obj.get("name", "")
                args = obj.get("arguments") or obj.get("parameters") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                if name:
                    calls.append({
                        "id": f"call_{_uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args) if isinstance(args, dict) else str(args),
                        },
                    })
                    continue
            except Exception:
                pass

        # Inner block is XML: <function=name><parameter=p>v</parameter>...</function>
        fn_m = _re.search(r'<function=(\w+)>(.*?)(?:</function>|$)', block, _re.DOTALL)
        if fn_m:
            name = fn_m.group(1)
            params_text = fn_m.group(2)
            args = {}
            for pm in _re.finditer(r'<parameter=(\w+)>\s*(.*?)\s*</parameter>', params_text, _re.DOTALL):
                args[pm.group(1)] = pm.group(2).strip()
            if not args:
                # Abbreviated inline: <parameter=key>\nvalue\n (no closing tag)
                for pm in _re.finditer(r'<parameter=(\w+)>\s*([^<\n]+)', params_text):
                    args[pm.group(1)] = pm.group(2).strip().rstrip('"')
            if name:
                calls.append({
                    "id": f"call_{_uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                })

    if not calls:
        # Secondary fallback — naked <function=name> without <tool_call> wrapper.
        # Only fires when the body (after thinking) is almost entirely markup,
        # i.e. the model forgot the wrapper but clearly intends a tool call.
        body = _re.sub(r'<think>.*?</think>', '', text, flags=_re.DOTALL).strip()
        known = {t["function"]["name"] for t in TOOLS}
        fn_m = _re.match(r'^<function=(\w+)>(.*?)(?:</function>|$)', body, _re.DOTALL)
        if fn_m and fn_m.group(1) in known:
            name = fn_m.group(1)
            params_text = fn_m.group(2)
            args = {}
            for pm in _re.finditer(r'<parameter=(\w+)>\s*(.*?)\s*</parameter>', params_text, _re.DOTALL):
                args[pm.group(1)] = pm.group(2).strip()
            if not args:
                for pm in _re.finditer(r'<parameter=(\w+)>\s*([^<]+)', params_text, _re.DOTALL):
                    args[pm.group(1)] = pm.group(2).strip()
            # Require at least one parameter to avoid false positives
            if args:
                calls.append({
                    "id": f"call_{_uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                })

        if not calls:
            return None

    # Gate — tool name validation.
    known = {t["function"]["name"] for t in TOOLS}
    if not all(c["function"]["name"] in known for c in calls):
        return None

    # Gate — no significant text after the last </tool_call>.
    # The template says reasoning is allowed BEFORE the call but NOT after.
    # Text following the last </tool_call> means the model is describing, not calling.
    body = _re.sub(r'<think>.*?</think>', '', text, flags=_re.DOTALL)
    last_tc = list(_re.finditer(r'</tool_call>', body))
    if last_tc:
        after = body[last_tc[-1].end():].strip()
        if len(after) > 80:
            return None

    return calls

# ── Tool executors ────────────────────────────────────────────────────────────
def _is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(pat in cmd_lower for pat in DANGEROUS_PATTERNS)

# Sentinel returned by gates when the user rejects a tool call.
# The dispatch loop watches for this prefix and stops the current batch immediately.
_GATE_REJECTED_PREFIX = "[GATE_REJECTED]"

INSTALL_PATTERNS = [
    "pip install", "pip3 install", "python -m pip",
    "npm install", "npm i ", "yarn add", "yarn install",
    "conda install", "mamba install",
    "winget install", "choco install", "scoop install",
    "apt install", "apt-get install", "brew install",
]

def _is_install(command: str) -> bool:
    import re
    cmd_lower = command.lower()
    if any(pat in cmd_lower for pat in INSTALL_PATTERNS):
        return True
    # Also catch venv pip.exe and pip3.exe forms: pip.exe install, pip3.exe install
    return bool(re.search(r'pip(?:3)?\.exe\s+install', cmd_lower))

def _is_bare_python(command: str) -> bool:
    """Detect bare python/pip calls that would hit system Python instead of a venv.

    Splits multi-command pipelines on &&, ||, ;, | and checks each segment.
    Returns True if ANY segment is a bare python/pip invocation.

    Allowed (returns False):
      - Explicit venv path: .venv/Scripts/python.exe, venv/bin/python, etc.
      - Venv creation: python -m venv, py -m venv
      - Non-python commands chained with python-looking segments won't flag
    """
    # Executables that go to system Python when unqualified.
    _BARE_EXES = _re.compile(
        r'^(?:python3?(?:\.exe)?|pip3?(?:\.exe)?|py)\s',
        _re.IGNORECASE,
    )
    # A path component that identifies an explicit venv.
    _VENV_PATH = _re.compile(
        r'[/\\](?:\.venv|venv|env|\.env)[/\\]',
        _re.IGNORECASE,
    )
    # Venv creation — always allowed regardless of other checks.
    _VENV_CREATE = _re.compile(
        r'^(?:python3?(?:\.exe)?|py)\s+.*-m\s+venv\b',
        _re.IGNORECASE,
    )
    # Split on shell sequence operators and pipes (crude but sufficient).
    segments = _re.split(r'&&|\|\||[;|]', command)
    for raw in segments:
        seg = raw.strip()
        if not seg:
            continue
        if not _BARE_EXES.match(seg):
            continue                       # not a python/pip invocation
        if _VENV_PATH.search(seg):
            continue                       # qualified with a venv path
        if _VENV_CREATE.match(seg):
            continue                       # creating a venv — always allowed
        return True                        # bare invocation found
    return False

# Prefixes that are definitely read-only / safe — don't flag as script execution.
_EXEC_SAFE_PREFIXES = (
    "cat ", "type ", "grep ", "rg ", "find ", "ls ", "dir ", "echo ",
    "git ", "code ", "notepad", "cmake ", "ctest ", "make ", "ninja ",
    "python --version", "python3 --version", ".venv",
)

def _is_exec(command: str) -> bool:
    """Detect script/binary execution (.py/.js/.sh/.bat/.ps1/.exe).

    Python/pip interpreters are stripped before the check so that
    `path/to/.venv/Scripts/python.exe -c "..."` doesn't falsely trigger.
    """
    stripped = command.strip()
    lower = stripped.lower()
    if any(lower.startswith(ex) for ex in _EXEC_SAFE_PREFIXES):
        return False
    # Strip a leading python/pip interpreter token (quoted or bare) before checking.
    # This prevents the interpreter path itself (.exe) from triggering the gate.
    candidate = _re.sub(
        r'^"?[^\s"]*(?:python\d*(?:\.\d+)?|pip\d*)(?:\.exe)?"?\s+',
        "", stripped, flags=_re.IGNORECASE,
    )
    return bool(_re.search(r'\b\S+\.(py|js|sh|bat|ps1|exe)\b', candidate, _re.IGNORECASE))


def _fmt_tool_args(name: str, args: dict) -> str:
    """Format tool args as a human-readable string for approval dialogs."""
    if not args:
        return ""
    # Keys to show first for each tool, in order
    priority = {
        "bash":       ["command"],
        "read_file":  ["path", "file_path", "offset", "limit"],
        "write_file": ["path", "file_path"],
        "edit":       ["path", "file_path", "old_string", "new_string"],
        "glob":       ["pattern", "path"],
        "grep":       ["pattern", "path"],
        "list_dir":   ["path"],
        "web_fetch":  ["url"],
        "web_search": ["query"],
        "task_list":  ["operation", "title", "id", "status"],
    }
    keys = priority.get(name, [])
    ordered = [k for k in keys if k in args] + [k for k in args if k not in keys]
    lines = []
    for k in ordered:
        v = args[k]
        v_str = str(v)
        if len(v_str) > 200:
            v_str = v_str[:197] + "..."
        lines.append(f"{k}: {v_str}")
    return "\n".join(lines)


def _matches_session_rule(name: str, args: dict, rules: list[str]) -> bool:
    """Return True if this tool call is covered by a session-level allow rule."""
    path = args.get("path", "")
    cmd  = args.get("command", "")
    for rule in rules:
        if rule.startswith("path_prefix:"):
            prefix = rule[len("path_prefix:"):]
            if path and os.path.normcase(path).startswith(os.path.normcase(prefix)):
                return True
        elif rule.startswith("cmd_pattern:"):
            pattern = rule[len("cmd_pattern:"):]
            if cmd and fnmatch.fnmatch(cmd.strip(), pattern):
                return True
        elif rule.startswith("tool:"):
            tool = rule[len("tool:"):]
            if name == tool:
                return True
    return False


def _build_approval_check(
    name: str,
    args: dict,
    approval_level: str,
    prefix: str = "",
    session_rules: list[str] | None = None,
) -> tuple[bool, str, str, str]:
    """Return (ask_needed, title, message, style) for a tool call approval check.

    `prefix` is prepended to titles (e.g. "Sub-Agent — ") to distinguish sub-agent prompts.
    Returns ask_needed=False when no approval is required.
    """
    if approval_level == "yolo":
        return False, "", "", "yellow"

    cmd = args.get("command", "") if name == "bash" else ""
    ask_needed = False
    ask_title = f"{prefix}Approval Required"
    ask_msg = ""
    ask_style = "yellow"

    if name == "bash" and _is_dangerous(cmd):
        ask_needed = True
        ask_title = f"{prefix}Warning — Dangerous Command"
        ask_msg = f"Dangerous command detected:\n\n{_fmt_tool_args(name, args)}"
        ask_style = "red"
    elif name == "bash" and _is_install(cmd):
        ask_needed = True
        ask_title = f"{prefix}Install Guard"
        ask_msg = (
            f"Package install detected:\n\n{_fmt_tool_args(name, args)}\n\n"
            "Run it? Or install yourself and press Enter when ready."
        )
        ask_style = "yellow"
    elif name == "bash" and approval_level == "auto" and _is_exec(cmd):
        ask_needed = True
        ask_title = f"{prefix}Script Execution"
        ask_msg = f"Script execution detected:\n\n{_fmt_tool_args(name, args)}"
        ask_style = "yellow"
    elif approval_level == "ask-all":
        ask_needed = True
        ask_title = f"{prefix}Approval Required"
        ask_msg = f"Tool: {name}\n\n{_fmt_tool_args(name, args)}"
    elif approval_level == "ask-writes":
        WRITE_TOOLS = {"bash", "write_file", "edit"}
        if name in WRITE_TOOLS or (name == "task_list" and args.get("operation") != "read"):
            ask_needed = True
            ask_title = f"{prefix}Write Operation — {name}"
            ask_msg = f"Write operation — {name}:\n\n{_fmt_tool_args(name, args)}"

    # Session rules can suppress non-dangerous prompts (install/exec/ask-writes/ask-all)
    if ask_needed and ask_style != "red" and session_rules and _matches_session_rule(name, args, session_rules):
        return False, "", "", "yellow"

    return ask_needed, ask_title, ask_msg, ask_style


def _menu_select(options: list[str]) -> int:
    """Interactive menu: arrow keys or number keys to select. Returns 0-based index."""
    import sys, os

    n = len(options)
    selected = 0

    def _render(sel: int, first: bool) -> None:
        if not first:
            sys.stdout.write(f"\033[{n}A")   # move cursor up n lines
        for i, opt in enumerate(options):
            if i == sel:
                sys.stdout.write(f"\r\033[2K  \033[1;36m❯ {i + 1}. {opt}\033[0m\n")
            else:
                sys.stdout.write(f"\r\033[2K    {i + 1}. {opt}\n")
        sys.stdout.flush()

    _render(selected, first=True)

    if os.name == "nt":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                break
            if ch in ("1", "2", "3"):
                idx = int(ch) - 1
                if 0 <= idx < n:
                    selected = idx
                    _render(selected, first=False)
            if ch in ("\xe0", "\x00"):          # escape prefix for arrow keys
                arrow = msvcrt.getwch()
                if arrow == "H":                # up arrow
                    selected = (selected - 1) % n
                elif arrow == "P":              # down arrow
                    selected = (selected + 1) % n
                _render(selected, first=False)
    else:
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n"):
                    break
                if ch == "\x03":                # Ctrl+C
                    raise KeyboardInterrupt
                if ch in ("1", "2", "3"):
                    idx = int(ch) - 1
                    if 0 <= idx < n:
                        selected = idx
                        _render(selected, first=False)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    if seq == "[A":             # up arrow
                        selected = (selected - 1) % n
                    elif seq == "[B":           # down arrow
                        selected = (selected + 1) % n
                    _render(selected, first=False)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    return selected


def _new_project_path(name: str, args: dict, cwd: Path) -> Path | None:
    """Return the first new directory that would be created, or None.

    Fires on:
      - bash commands containing 'mkdir' targeting a path that doesn't exist yet
      - write_file calls whose parent directory doesn't exist yet

    Used to gate new-project scaffolding behind a plan-approval prompt.
    """
    if name == "bash":
        cmd = args.get("command", "")
        if "mkdir" not in cmd.lower():
            return None
        # Extract the first path argument after mkdir (with optional -p / -m flags)
        m = _re.search(
            r'\bmkdir\b(?:\s+(?:-[a-zA-Z0-9]+\s+)*)"?([^\s"&|;><]+)"?',
            cmd,
        )
        if not m:
            return None
        target = Path(m.group(1).strip('"\''))
        if not target.is_absolute():
            target = cwd / target
        if not target.exists():
            return target
    elif name == "write_file":
        raw = args.get("path", "")
        if not raw:
            return None
        target = Path(raw)
        if not target.is_absolute():
            target = cwd / target
        if not target.parent.exists():
            return target.parent
    return None


async def tool_bash(command: str, timeout: int = 30, cwd: Path | None = None) -> str:
    try:
        effective_cwd = Path(cwd) if cwd else None
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(effective_cwd) if effective_cwd else None,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[timeout after {timeout}s]"
        output = stdout.decode(errors="replace").rstrip()
        rc = proc.returncode
        cwd_line = f"[cwd: {effective_cwd}]\n" if effective_cwd else ""
        if output:
            return f"{cwd_line}[exit {rc}]\n{output}" if rc != 0 else f"{cwd_line}{output}"
        else:
            return f"{cwd_line}(exit {rc})"
    except Exception as e:
        return f"[error: {e}]"


async def tool_read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(0, offset - 1)          # convert 1-based to 0-based
        end   = min(total, start + limit)
        slice_ = lines[start:end]
        width  = len(str(start + len(slice_)))
        numbered = "\n".join(f"{start + i + 1:>{width}} | {l}" for i, l in enumerate(slice_))
        footer = ""
        if end < total:
            footer = f"\n... [{total - end} more lines — use offset={end + 1} to continue]"
        return numbered + footer
    except Exception as e:
        return f"[error: {e}]"


async def tool_write_file(path: str, content: str) -> str:
    p = Path(path)
    if p.exists():
        old = p.read_text(encoding="utf-8", errors="replace")
        old_lines = old.splitlines()
        new_lines = content.splitlines()
        added = sum(1 for l in new_lines if l not in old_lines)
        removed = sum(1 for l in old_lines if l not in new_lines)
        console.print(
            Panel(
                f"[cyan]{path}[/cyan]\n[green]+{added} lines[/green]  [red]-{removed} lines[/red]",
                title="Write Preview",
                border_style="yellow",
            )
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {path}"
    except Exception as e:
        return f"[error: {e}]"


async def tool_list_dir(path: str = ".") -> str:
    try:
        p = Path(path)
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        for entry in entries:
            prefix = "[DIR]  " if entry.is_dir() else "       "
            size = "" if entry.is_dir() else f"  ({entry.stat().st_size:,} bytes)"
            lines.append(f"{prefix}{entry.name}{size}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"[error: {e}]"


async def tool_glob(pattern: str, path: str = ".") -> str:
    import fnmatch
    try:
        root = Path(path)
        matches = sorted(root.glob(pattern))
        if not matches:
            return "(no matches)"
        lines = []
        for m in matches:
            suffix = "/" if m.is_dir() else f"  ({m.stat().st_size:,} bytes)"
            lines.append(f"{m.resolve()}{suffix}")
        return "\n".join(lines)
    except Exception as e:
        return f"[error: {e}]"


async def tool_grep(
    pattern: str,
    path: str = ".",
    glob: str = "**/*",
    case_insensitive: bool = False,
    context_lines: int = 2,
) -> str:
    import re
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)
        root = Path(path)

        # If path is a file, search just that file
        if root.is_file():
            candidates = [root]
        else:
            candidates = [f for f in root.glob(glob) if f.is_file()]

        output_parts: list[str] = []
        total_matches = 0

        for filepath in sorted(candidates):
            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            lines = text.splitlines()
            match_lines = [i for i, ln in enumerate(lines) if regex.search(ln)]
            if not match_lines:
                continue

            file_parts = [f"── {filepath.resolve()} ──"]
            shown: set[int] = set()
            for mi in match_lines:
                start = max(0, mi - context_lines)
                end = min(len(lines) - 1, mi + context_lines)
                for i in range(start, end + 1):
                    if i not in shown:
                        prefix = ">" if i == mi else " "
                        file_parts.append(f"{prefix} {i+1:4}: {lines[i]}")
                        shown.add(i)
                if mi != match_lines[-1]:
                    next_start = max(0, match_lines[match_lines.index(mi) + 1] - context_lines)
                    if next_start > end + 1:
                        file_parts.append("   ...")
            output_parts.append("\n".join(file_parts))
            total_matches += len(match_lines)

            if total_matches > 200:
                output_parts.append("[truncated: too many matches]")
                break

        if not output_parts:
            return "(no matches)"
        return f"({total_matches} match{'es' if total_matches != 1 else ''})\n\n" + "\n\n".join(output_parts)
    except re.error as e:
        return f"[regex error: {e}]"
    except Exception as e:
        return f"[error: {e}]"


async def tool_ripgrep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    type_filter: str | None = None,
    case_insensitive: bool = False,
    context_lines: int = 2,
    fixed_strings: bool = False,
    max_results: int = 100,
) -> str:
    args = ["rg", "--line-number", "--no-heading", "--color=never"]
    if case_insensitive:
        args.append("-i")
    if fixed_strings:
        args.append("-F")
    if context_lines:
        args.extend(["-C", str(context_lines)])
    if glob:
        args.extend(["--glob", glob])
    if type_filter:
        args.extend(["--type", type_filter])
    if max_results:
        args.extend(["-m", str(max_results)])
    args.append(pattern)
    args.append(path)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return "[timeout after 30s]"
        if proc.returncode == 1:
            return "(no matches)"
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return f"[error: rg exit {proc.returncode}: {err}]"
        output = stdout.decode(errors="replace")
        lines = output.splitlines()
        if len(lines) > 200:
            extra = len(lines) - 200
            return "\n".join(lines[:200]) + f"\n[+{extra} more lines]"
        return output if output else "(no matches)"
    except FileNotFoundError:
        return "[error: ripgrep not installed — install from https://github.com/BurntSushi/ripgrep]"
    except Exception as e:
        return f"[error: {e}]"


async def tool_edit(path: str, old_string: str, new_string: str) -> str:
    try:
        p = Path(path)
        if not p.exists():
            return f"[error: file not found: {path}]"
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            # Give the model a fuzzy hint: find the line in the file most similar to
            # the first line of old_string so it can correct its old_string.
            import difflib as _difflib
            target_first = old_string.splitlines()[0].strip() if old_string.strip() else ""
            file_lines = text.splitlines()
            if target_first:
                matches = _difflib.get_close_matches(target_first, file_lines, n=3, cutoff=0.4)
                if matches:
                    hint = "\n".join(f"  {m!r}" for m in matches)
                    return (
                        "[error: old_string not found in file]\n"
                        f"Closest lines in file (use read_file to get exact content):\n{hint}"
                    )
            return "[error: old_string not found — use read_file to get the exact content before editing]"
        if count > 1:
            return f"[error: old_string found {count} times — make it more specific]"
        new_text = text.replace(old_string, new_string, 1)
        p.write_text(new_text, encoding="utf-8")
        import difflib as _difflib
        diff = list(_difflib.unified_diff(
            old_string.splitlines(keepends=False),
            new_string.splitlines(keepends=False),
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            lineterm="",
        ))
        if diff:
            return "\n".join(diff)
        return f"Edited {p.name}: applied (whitespace-only change)"
    except Exception as e:
        return f"[error: {e}]"


def _tts_preprocess(text: str) -> str:
    """Normalise text for TTS: currency, punctuation strip, period → ellipsis."""
    import re as _re
    # Protect existing ellipses from double-expansion
    text = text.replace("...", "\x00")
    # Currency: $20.45 → 20.45 Dollars
    text = _re.sub(r"\$(\d+(?:\.\d+)?)", r"\1 Dollars", text)
    # Strip anything not in the allowed set (letters, digits, space, .,?!', placeholder)
    text = _re.sub(r"[^a-zA-Z0-9 .,?!'\x00]", " ", text)
    # Expand sentence-ending periods to ellipses (not decimal points between digits)
    text = _re.sub(r"(?<!\d)\.(?!\d)", "...", text)
    # Restore protected ellipses
    text = text.replace("\x00", "...")
    # Collapse runs of spaces
    text = _re.sub(r" {2,}", " ", text).strip()
    return text


async def tool_speak(text: str) -> str:
    text = _tts_preprocess(text)
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post("http://127.0.0.1:1236/play", json={"text": text}, timeout=30)
        return "spoken" if r.is_success else f"[voice error: {r.status_code}]"
    except Exception as e:
        return f"[voice unavailable: {e}]"


async def tool_web_fetch(url: str) -> str:
    import re as _re
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        # Tags whose entire subtree is discarded (chrome, boilerplate)
        SKIP_TAGS = {
            "script", "style", "head", "noscript",
            "nav", "header", "footer", "aside",
            "form", "menu", "menuitem", "banner",
        }
        # Tags that introduce a line break in the output
        BLOCK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "dt", "dd"}

        def __init__(self):
            super().__init__()
            self.parts: list[str] = []
            self._skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in self.SKIP_TAGS:
                self._skip += 1
            if not self._skip and tag in self.BLOCK_TAGS:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in self.SKIP_TAGS:
                self._skip = max(0, self._skip - 1)
            if not self._skip and tag in self.BLOCK_TAGS:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self._skip:
                self.parts.append(data)

        def get_text(self):
            raw = "".join(self.parts)
            raw = _re.sub(r"[ \t]+", " ", raw)       # collapse horizontal whitespace
            raw = _re.sub(r"\n[ \t]+", "\n", raw)    # trim leading spaces on lines
            raw = _re.sub(r"\n{3,}", "\n\n", raw)    # max one blank line between paragraphs
            # Drop lines that are just whitespace or a single short word (nav remnants)
            lines = [l for l in raw.splitlines() if len(l.strip()) > 2 or l == ""]
            return "\n".join(lines).strip()

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; chat.py)"})
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            if "html" in content_type:
                stripper = _Stripper()
                stripper.feed(r.text)
                text = stripper.get_text()
            else:
                text = r.text
            if len(text) > 12000:
                text = text[:12000] + f"\n... [truncated, {len(text)} chars total]"
            return text
    except Exception as e:
        return f"[error: {e}]"


SEARXNG_URL = "http://localhost:8888"  # optional — used only if reachable

async def tool_web_search(query: str, max_results: int = 6) -> str:
    if not query or not query.strip():
        return "[error: web_search requires a non-empty query — the model may have produced malformed tool call arguments]"
    # Opportunistic SearXNG check (2s timeout — silent fail if not running)
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(
                f"{SEARXNG_URL}/search",
                params={"q": query, "format": "json", "language": "en-US"},
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])[:max_results]
            if results:
                lines = []
                for i, item in enumerate(results, 1):
                    lines.append(
                        f"{i}. {item.get('title', '')}\n"
                        f"   {item.get('url', '')}\n"
                        f"   {item.get('content', '')}"
                    )
                return "\n\n".join(lines)
    except Exception:
        pass

    # DuckDuckGo via ddgs (sync — run in thread executor to stay non-blocking)
    try:
        from ddgs import DDGS
    except ImportError:
        return "[error: ddgs not installed — run: .venv\\Scripts\\pip.exe install ddgs]"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: list(DDGS().text(query, max_results=max_results)),
            )
            if not results:
                return "(no results)"
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}\n   {r['href']}\n   {r['body']}")
            return "\n\n".join(lines)
        except Exception as e:
            last_error = e
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s then 2s before retries

    return f"[error: search failed after 3 attempts — {last_error}]"

async def tool_task_list(
    operation: str,
    path: str = "TASKS.md",
    content: str = "",
    index: int | None = None,
    checked: bool | None = None,
) -> str:
    import re as _re
    p = Path(path)
    if operation == "read":
        if not p.exists():
            return f"[no task list at {path}]"
        return p.read_text(encoding="utf-8")
    elif operation == "create":
        if not content:
            return "[error: content required for create]"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Created {path}"
    elif operation == "update":
        if index is None or checked is None:
            return "[error: index and checked required for update]"
        if not p.exists():
            return f"[no task list at {path}]"
        text = p.read_text(encoding="utf-8")
        lines = text.splitlines()
        task_lines = [(i, l) for i, l in enumerate(lines) if _re.match(r"\s*- \[[ x]\]", l)]
        if index < 0 or index >= len(task_lines):
            return f"[error: index {index} out of range (0–{len(task_lines)-1})]"
        line_idx, line = task_lines[index]
        new_mark = "x" if checked else " "
        new_line = _re.sub(r"- \[[ x]\]", f"- [{new_mark}]", line, count=1)
        lines[line_idx] = new_line
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        action = "checked" if checked else "unchecked"
        return f"Task {index} {action}: {new_line.strip()}"
    else:
        return f"[unknown operation: {operation}]"

# ── Skills ────────────────────────────────────────────────────────────────────
def _load_skills() -> dict:
    """Load skill files from skills/*.md. Returns dict[name → skill_dict]."""
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.exists():
        return {}
    result = {}
    for path in sorted(skills_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            end = text.find("---", 3)
            if end == -1:
                continue
            front = text[3:end].strip()
            body = text[end + 3:].strip()
            meta: dict = {}
            for line in front.splitlines():
                line = line.strip()
                if not line or ":" not in line:
                    continue
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip()
                if val.lower() in ("true", "false"):
                    meta[key] = val.lower() == "true"
                elif val.startswith("[") and val.endswith("]"):
                    items = [v.strip().strip("\"'") for v in val[1:-1].split(",") if v.strip()]
                    meta[key] = items
                else:
                    meta[key] = val.strip("\"'")
            if "name" in meta:
                # context_files: [file1.md, file2.md] — append additional files into body
                for cf in (meta.get("context_files") or []):
                    cf_path = path.parent / cf
                    try:
                        body += "\n\n---\n\n" + cf_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
                meta["_body"] = body
                result[meta["name"]] = meta
        except Exception:
            continue
    return result


async def _invoke_skill(skill_name: str, skill_args: str, session: "ChatSession") -> bool:
    """Invoke a named skill. Returns True if found and invoked, False otherwise."""
    skills = _load_skills()
    if skill_name not in skills:
        return False
    skill = skills[skill_name]
    body = skill.get("_body", "")
    expanded = body.replace("$ARGS", skill_args).strip()
    spawn = skill.get("spawn_agent", False)
    if spawn:
        tools      = skill.get("agent_tools") or None
        think      = skill.get("think_level") or None
        max_iter   = int(skill.get("max_iterations", 10))
        if not session.tui_queue:
            console.print(Panel(
                f"[dim]Invoking agent skill '[bold]{skill_name}[/bold]'...[/dim]",
                title="[cyan]Skill[/cyan]",
                border_style="cyan",
            ))
        _skill_id = f"__skill_{skill_name}"
        is_error = False
        if session.tui_queue:
            await session.tui_queue.put({
                "type": "tool_start",
                "id": _skill_id,
                "name": "spawn_agent",
                "args": f'{{"task": {__import__("json").dumps(skill_args[:120])}}}',
            })
        try:
            result = await session._tool_spawn_agent(expanded, skill_args, tools, think, max_iter)
        except Exception as e:
            result = f"[skill agent error: {e}]"
            is_error = True
            if not session.tui_queue:
                console.print(f"[red]Skill agent failed: {e}[/red]")
        if session.tui_queue:
            await session.tui_queue.put({
                "type": "tool_done",
                "id": _skill_id,
                "name": "spawn_agent",
                "result": result,
                "is_error": is_error,
            })
        # Feed the report back to Eli via a proper send_and_stream call so it lands in
        # Eli's message history as a valid user→assistant exchange. This is necessary for:
        #   (a) Eli to actually read and process the research findings
        #   (b) tokens_used to update (fixes "0% context" display after research)
        #   (c) message sequence integrity (avoids back-to-back "assistant" messages)
        _return_prompt = skill.get("return_prompt") or (
            "The agent has completed the above report. "
            "Acknowledge receipt in one sentence only — do NOT summarize. "
            "The full content is in your context."
        )
        await session.send_and_stream(
            f"[Agent Report — '{skill_name}']\n\n{result}\n\n{_return_prompt}"
        )
    else:
        await session.send_and_stream(expanded)
    return True


# ── ChatSession ───────────────────────────────────────────────────────────────
class ChatSession:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=120.0)
        _initial = _build_initial_messages()
        self.messages: list[dict] = _initial
        self._n_fixed: int          = len(_initial)
        self.think_level: str       = "on"   # "off" | "on" | "deep"
        self.model: str             = MODEL
        self.ctx_window: int        = CTX_WINDOW
        self.tokens_used: int       = 0
        self.tokens_prompt: int     = 0
        self.tokens_completion: int = 0
        self._compacting: bool      = False
        self.cwd: Path              = Path.cwd()
        self.approval_level: str    = "auto"
        self._session_path: Path | None = None
        self._in_subagent: bool     = False
        self.compact_mode: bool         = False
        self.compact_threshold: float   = CTX_COMPACT_THRESH
        self.keep_recent: int           = CTX_KEEP_RECENT
        self.input_compress_limit: int  = INPUT_COMPRESS_CHARS
        self.role: str              = "eli"  # active role name
        self._project_config: dict  = {}
        self._approval_notes: str   = ""  # injected into tool result after dispatch
        self.session_rules: list[str] = []  # persistent allow-rules for this session
        self.tui_queue: asyncio.Queue | None = None  # set by TUI to receive typed events

    async def __aenter__(self):
        await self._health_check()
        await self._detect_ctx_window()
        await self._refresh_project_config()
        if not self.tui_queue:
            console.print(
                Panel(
                    "[bold cyan]Qwen3 Chat[/bold cyan]  —  connected to [green]localhost:1234[/green]\n"
                    "Type [bold]/help[/bold] for commands  |  [bold]Alt+Enter[/bold] newline  |  [bold]Shift+Tab[/bold] cycle mode  |  [bold]Ctrl+O[/bold] compact  |  [bold]Ctrl+D[/bold] exit",
                    border_style="cyan",
                )
            )
        return self

    async def __aexit__(self, *_):
        await self.client.aclose()

    async def _health_check(self):
        try:
            r = await self.client.get(f"{BASE_URL}/v1/models")
            r.raise_for_status()
        except Exception as e:
            console.print(f"[red]Server not reachable at {BASE_URL}: {e}[/red]")
            console.print("[yellow]Start the server first via server_manager.py[/yellow]")
            sys.exit(1)

    async def _detect_ctx_window(self) -> None:
        try:
            r = await self.client.get(f"{BASE_URL}/slots", timeout=5)
            r.raise_for_status()
            slots = r.json()
            if isinstance(slots, list) and slots:
                n_ctx = slots[0].get("n_ctx")
                if n_ctx and isinstance(n_ctx, int) and n_ctx > 0:
                    self.ctx_window = n_ctx
                    console.print(f"[dim]Context window: {n_ctx:,} tokens[/dim]")
                    return
        except Exception:
            pass
        console.print(f"[dim]Context window: {CTX_WINDOW:,} tokens (default — /slots unavailable)[/dim]")

    def _remove_config_message(self) -> None:
        """Remove any previously injected project config system message."""
        SENTINEL = "[Project Config — eli.toml]"
        for i in range(self._n_fixed):
            if (self.messages[i].get("role") == "system" and
                    self.messages[i].get("content", "").startswith(SENTINEL)):
                del self.messages[i]
                self._n_fixed -= 1
                return

    async def _refresh_project_config(self) -> None:
        """Load eli.toml for current cwd and inject/update system message."""
        self._remove_config_message()
        config = _load_project_config(self.cwd)
        self._project_config = config
        if config:
            msg_text = _format_project_config(config)
            self.messages.insert(self._n_fixed, {"role": "system", "content": msg_text})
            self._n_fixed += 1
            name = config.get("project", {}).get("name", "eli.toml")
            console.print(f"[dim]Project Config loaded: {name}[/dim]")
        self._inject_cwd_context()

    def _remove_cwd_context(self) -> None:
        SENTINEL = "[Session Context]"
        for i in range(self._n_fixed):
            if (self.messages[i].get("role") == "system" and
                    self.messages[i].get("content", "").startswith(SENTINEL)):
                del self.messages[i]
                self._n_fixed -= 1
                return

    def _inject_cwd_context(self) -> None:
        """Inject current working directory and date as a system message."""
        import datetime
        self._remove_cwd_context()
        today = datetime.date.today().strftime("%Y-%m-%d")
        content = (
            f"[Session Context]\n"
            f"Today's date: {today}\n"
            f"Current working directory: {self.cwd}\n"
            f"All relative file paths resolve against this directory."
        )
        self.messages.insert(self._n_fixed, {"role": "system", "content": content})
        self._n_fixed += 1

    async def _run_post_edit_hook(self, file_path: str) -> str | None:
        """Run post-edit hook if an eli.toml pattern matches the edited file."""
        config = self._project_config
        if not config:
            return None
        hooks = config.get("hooks", {})
        if not hooks:
            return None
        filename = Path(file_path).name
        for pat, action in hooks.items():
            if fnmatch.fnmatch(filename, pat):
                if action == "build":
                    build_cfg = config.get("build", {})
                    cmd = build_cfg.get("command", "")
                    cwd_rel = build_cfg.get("cwd", ".")
                    hook_cwd = (self.cwd / cwd_rel).resolve()
                elif action == "test":
                    test_cfg = config.get("test", {})
                    cmd = test_cfg.get("command", "")
                    cwd_rel = test_cfg.get("cwd", ".")
                    hook_cwd = (self.cwd / cwd_rel).resolve()
                else:
                    cmd = action
                    hook_cwd = self.cwd
                if not cmd:
                    return None
                console.print(f"[dim]  ↪ hook({action}): {cmd}[/dim]")
                try:
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=str(hook_cwd),
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                    except asyncio.TimeoutError:
                        proc.kill()
                        return f"[Hook: {action}]\n[FAILED] [timeout after 120s]"
                    output = stdout.decode(errors="replace")
                    if not output:
                        output = "(no output)"
                    prefix = "[FAILED] " if proc.returncode != 0 else ""
                    return f"[Hook: {action}]\n{prefix}{output}"
                except Exception as e:
                    return f"[Hook: {action}]\n[FAILED] [error: {e}]"
        return None

    async def _compact_history(self, *, manual: bool = False) -> None:
        if self._compacting:
            return
        summarisable = self.messages[self._n_fixed:-CTX_KEEP_RECENT] \
            if len(self.messages) > self._n_fixed + CTX_KEEP_RECENT else []
        if len(summarisable) < 4:
            if manual:
                console.print("[dim]Nothing to compact (history too short)[/dim]")
            return
        self._compacting = True
        orig_count = len(self.messages)
        try:
            # Serialise the slice for the summariser
            lines = []
            for m in summarisable:
                role = m["role"].upper()
                if m.get("tool_calls"):
                    calls = ", ".join(
                        f"{tc['function']['name']}({tc['function']['arguments'][:80]})"
                        for tc in m["tool_calls"]
                    )
                    lines.append(f"[ASSISTANT tool calls]: {calls}")
                else:
                    content = (m.get("content") or "")[:2000]
                    lines.append(f"[{role}]: {content}")
            serialised = "\n\n".join(lines)

            r = await self.client.post(f"{BASE_URL}/v1/chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": (
                        "You are a conversation summarizer for an AI assistant session. "
                        "Produce a dense, structured summary. Use sections:\n"
                        "## Work Done — what was built, changed, or decided\n"
                        "## Key Facts — file names, identifiers, commands, error messages, "
                        "numeric values, URLs, paths\n"
                        "## Active State — current mode settings, active tools or roles, "
                        "anything the assistant should remember about how to behave\n"
                        "## Open Items — unresolved questions, things in progress, next steps\n"
                        "Be complete. Missing a detail here means it is permanently lost."
                    )},
                    {"role": "user", "content": f"Summarize this conversation:\n\n{serialised}"},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 2048,
            })
            r.raise_for_status()
            summary = r.json()["choices"][0]["message"]["content"].strip()
            if not summary:
                raise ValueError("empty summary")

            # Re-read all behavior files fresh from disk so compaction also refreshes
            # any instructions that may have changed since session start (e.g. ELI.md,
            # MEMORY.md). This mirrors how Claude Code re-reads CLAUDE.md after its own
            # context compaction.
            fresh_initial = _build_initial_messages()
            new_messages = list(fresh_initial)
            self._n_fixed = len(fresh_initial)

            # Re-inject active role right after the refreshed system messages.
            # The original role injection lived in conversation history and was
            # just summarised away; re-reading the file also picks up any edits.
            if self.role != "eli":
                _agents_dir = Path(__file__).parent / "agents"
                _agent_file = _agents_dir / f"{self.role}.md"
                if _agent_file.exists():
                    _profile = _agent_file.read_text(encoding="utf-8")
                    new_messages.append({
                        "role": "system",
                        "content": (
                            f"[Role Override — {self.role}] (restored after context compaction)\n\n"
                            f"Continue embodying this persona fully. Your tools and capabilities "
                            f"remain unchanged.\n\n{_profile}"
                        ),
                    })

            new_messages.append(
                {"role": "system", "content": f"[Conversation summary — earlier messages compacted]\n\n{summary}"}
            )
            new_messages.extend(self.messages[-CTX_KEEP_RECENT:])

            self.messages = new_messages
            self.tokens_used = 0
            msg = f"Context compacted ({orig_count} → {len(self.messages)} messages)"
            if self.tui_queue:
                await self.tui_queue.put({"type": "system", "text": msg})
            else:
                console.print(Rule(f"[yellow]{msg}[/yellow]", style="yellow"))
        except Exception as e:
            err = f"Compaction failed — history unchanged: {e}"
            if self.tui_queue:
                await self.tui_queue.put({"type": "system", "text": err})
            else:
                console.print(f"[yellow]{err}[/yellow]")
        finally:
            self._compacting = False

    async def _maybe_compact_input(self, text: str) -> str:
        if len(text) <= INPUT_COMPRESS_CHARS:
            return text
        console.print(Panel(
            f"[yellow]Large input ({len(text):,} chars) — compressing...[/yellow]",
            title="[dim]Input Compaction[/dim]",
            border_style="yellow",
        ))
        try:
            r = await self.client.post(f"{BASE_URL}/v1/chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": (
                        "Compress the following text to its essential information. "
                        "Preserve ALL: code identifiers, function names, file paths, "
                        "error messages, stack traces, numeric values, URLs, and the "
                        "user's core question or request. Remove repetition (note "
                        "'repeated N times'). Output only the compressed text."
                    )},
                    {"role": "user", "content": f"Compress this:\n\n{text}"},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 2048,
            })
            r.raise_for_status()
            compressed = r.json()["choices"][0]["message"]["content"].strip()
            if compressed:
                console.print(f"[dim]Compressed: {len(text):,} → {len(compressed):,} chars[/dim]")
                return compressed
        except Exception:
            pass
        return text

    def _autosave(self) -> None:
        try:
            self._session_path = _save_session(self.messages, self._n_fixed, self._session_path)
        except Exception:
            pass

    def rollback_partial_turn(self) -> None:
        """Strip any incomplete assistant/tool messages added during a cancelled turn.

        Leaves the last user message in place so the model knows what was asked.
        If the last message is also a user message with no response at all, removes it
        to avoid a duplicate when the user re-submits.
        """
        # Remove trailing non-user messages (incomplete assistant/tool state)
        while len(self.messages) > self._n_fixed and self.messages[-1]["role"] != "user":
            self.messages.pop()
        # If the very last message is a user message with nothing after it,
        # the turn was cancelled before any response — remove it so re-submit works cleanly.
        if len(self.messages) > self._n_fixed and self.messages[-1]["role"] == "user":
            self.messages.pop()

    async def send_and_stream(self, user_text: str, plan_mode: bool = False):
        user_text = await self._maybe_compact_input(user_text)
        self.messages.append({"role": "user", "content": user_text})

        while True:
            temperature = 0.3 if self.think_level == "deep" else 0.6
            think_kwargs: dict = {}
            if self.think_level == "off":
                think_kwargs["chat_template_kwargs"] = {"enable_thinking": False}
            else:
                think_kwargs["chat_template_kwargs"] = {"enable_thinking": True}

            if plan_mode:
                plan_system = (
                    "You are a helpful coding assistant running in a terminal. "
                    "You are currently in PLAN MODE. "
                    "Your only job is to output a written plan as plain markdown prose. "
                    "STRICT RULES for plan mode:\n"
                    "- You MAY call web_fetch and web_search to research before writing the plan.\n"
                    "- Do NOT invoke any other tools (bash, edit, write_file, read_file, etc.).\n"
                    "- DO describe, step by step, exactly what you would do and why: "
                    "which tools you would call, with what arguments, in what order, "
                    "and what you expect each step to return.\n"
                    "- Write the plan as a numbered markdown list. Be specific and actionable.\n"
                    "- End with a one-sentence summary of the expected outcome.\n"
                    "Output the plan now, then stop."
                )
                plan_tools = [t for t in TOOLS if t["function"]["name"] in ("web_fetch", "web_search")]
                send_messages = [{"role": "system", "content": plan_system}, *self.messages[1:]]
                payload = {
                    "model": self.model,
                    "messages": send_messages,
                    "tools": plan_tools,
                    "tool_choice": "auto",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": temperature,
                    **think_kwargs,
                }
            else:
                payload = {
                    "model": self.model,
                    "messages": self.messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": temperature,
                    **think_kwargs,
                }

            thinking_buf = ""
            text_buf = ""
            tool_calls_received = []
            assistant_content = ""
            usage_data: dict | None = None

            async with self.client.stream(
                "POST",
                f"{BASE_URL}/v1/chat/completions",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as response:
                response.raise_for_status()

                # Render text live via rich.live (or null in TUI mode)
                _live_ctx = _NullLive() if self.tui_queue else Live(console=console, refresh_per_second=8)
                with _live_ctx as live:
                    thinking_started = False
                    text_started = False

                    show_thinking = self.think_level != "off" and not self.compact_mode
                    think_title = "[dim]Thinking (deep)...[/dim]" if self.think_level == "deep" else "[dim]Thinking...[/dim]"
                    think_border = "blue" if self.think_level == "deep" else "dim"

                    async for event_type, data in stream_events(
                        response,
                        label=f"send_and_stream | model={self.model} | {BASE_URL}",
                    ):
                        if event_type == "think":
                            thinking_buf += data
                            if self.tui_queue:
                                await self.tui_queue.put({"type": "think_token", "text": data})
                            elif show_thinking:
                                live.update(
                                    Panel(
                                        Text(thinking_buf, style="dim italic"),
                                        title=think_title,
                                        border_style=think_border,
                                    )
                                )

                        elif event_type == "text":
                            if self.tui_queue:
                                text_buf += data
                                assistant_content += data
                                await self.tui_queue.put({"type": "text_token", "text": data})
                            else:
                                if thinking_buf and show_thinking:
                                    # Commit thinking panel, start fresh for text
                                    live.update(Text(""))
                                    live.stop()
                                    console.print(
                                        Panel(
                                            Text(thinking_buf, style="dim italic"),
                                            title=think_title.replace("...", ""),
                                            border_style=think_border,
                                        )
                                    )
                                    live.start()
                                    thinking_buf = ""

                                text_buf += data
                                assistant_content += data
                                live.update(Markdown(text_buf))

                        elif event_type == "tool_calls":
                            tool_calls_received = data
                            if not self.tui_queue:
                                live.update(Text(""))

                        elif event_type == "usage":
                            usage_data = data

                        elif event_type == "stop":
                            if self.tui_queue:
                                await self.tui_queue.put({"type": "text_done", "text": text_buf})
                            else:
                                live.update(Markdown(text_buf) if text_buf else Text(""))

            # Update token tracking
            if usage_data:
                self.tokens_used       = usage_data.get("total_tokens", 0)
                self.tokens_prompt     = usage_data.get("prompt_tokens", 0)
                self.tokens_completion = usage_data.get("completion_tokens", 0)
            else:
                self.tokens_used = sum(
                    len(m.get("content") or "") for m in self.messages
                ) // CHARS_PER_TOKEN

            # Auto-compact if approaching context limit
            if not self._compacting and self.tokens_used >= int(self.ctx_window * CTX_COMPACT_THRESH):
                await self._compact_history()

            # Fallback: model emitted tool calls as text (e.g. 30B with custom template)
            if not tool_calls_received and assistant_content:
                _parsed = _try_parse_text_tool_calls(assistant_content)
                if _parsed:
                    tool_calls_received = _parsed
                    assistant_content = ""  # don't echo raw text back into message history

            # Auto-announce if model produced no text before first tool call
            if tool_calls_received and not text_buf.strip():
                if self.tui_queue:
                    tool_names = ", ".join(tc["function"]["name"] for tc in tool_calls_received)
                    await self.tui_queue.put({"type": "system", "text": f"→ {tool_names}…"})
                else:
                    console.print(f"[dim]{_tool_announce(tool_calls_received)}[/dim]")

            # Append assistant message
            if tool_calls_received:
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_content or None,
                    "tool_calls": tool_calls_received,
                })
                # Execute tool calls: read-only ops in parallel, write/exec sequentially.
                # Write ops (bash, write_file, edit) must be sequential so interactive
                # gates (approval prompts, plan gates) fire in order and can't be raced.
                _READ_ONLY_TOOLS = {"read_file", "list_dir", "glob", "grep", "ripgrep",
                                    "web_search", "web_fetch", "speak"}

                async def _run_one(tc):
                    return tc["id"], await self._call_tool(
                        tc["function"]["name"],
                        tc["function"]["arguments"],
                        tc["id"],
                    )

                # Split into runs: flush a parallel batch whenever a write op appears.
                # Stop the entire batch immediately if any gate rejects a call.
                _batch: list = []
                _gate_rejected = False

                async def _emit_tool_done(tc_name: str, tc_id: str, result: str) -> None:
                    if self.tui_queue:
                        is_err = result.startswith(("[error", "[unknown", "[blocked", "[cancelled", _GATE_REJECTED_PREFIX))
                        await self.tui_queue.put({"type": "tool_done", "id": tc_id, "name": tc_name, "result": result, "is_error": is_err})

                for tc in tool_calls_received:
                    if _gate_rejected:
                        break
                    if tc["function"]["name"] in _READ_ONLY_TOOLS:
                        _batch.append(tc)
                    else:
                        # Flush pending reads in parallel first
                        if _batch:
                            _results = await asyncio.gather(*[_run_one(t) for t in _batch])
                            for _bt, (_tc_id, _result) in zip(_batch, _results):
                                self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                                await _emit_tool_done(_bt["function"]["name"], _tc_id, _result)
                            _batch = []
                        # Then run the write op sequentially
                        _tc_id, _result = await _run_one(tc)
                        self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                        await _emit_tool_done(tc["function"]["name"], _tc_id, _result)
                        if _result.startswith(_GATE_REJECTED_PREFIX):
                            _gate_rejected = True
                # Flush any remaining reads (only if not rejected)
                if _batch and not _gate_rejected:
                    _results = await asyncio.gather(*[_run_one(t) for t in _batch])
                    for _bt, (_tc_id, _result) in zip(_batch, _results):
                        self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                        await _emit_tool_done(_bt["function"]["name"], _tc_id, _result)
                # Loop: send tool results back to model
                continue
            else:
                if assistant_content:
                    self.messages.append({"role": "assistant", "content": assistant_content})
                break

        if self.tui_queue:
            await self.tui_queue.put({"type": "usage", "tokens": self.tokens_used, "ctx": self.ctx_window})
            await self.tui_queue.put({"type": "done"})
        elif self.tokens_used:
            pct   = self.tokens_used / self.ctx_window
            style = "yellow" if pct > 0.6 else "dim"
            label = f"~{self.tokens_used / 1000:.1f}k / {self.ctx_window / 1000:.0f}k tokens"
            console.print(Rule(f"[{style}]{label}[/{style}]", style="dim"))
        else:
            console.print(Rule(style="dim"))
        self._autosave()

    @staticmethod
    def _compact_args(name: str, args: dict) -> str:
        """One-line argument summary for compact tool display."""
        if name == "bash":
            return " " + args.get("command", "")[:80].replace("\n", " ")
        if name in ("read_file", "write_file", "list_dir"):
            return " " + args.get("path", "")
        if name == "edit":
            return " " + args.get("path", "")
        if name == "web_search":
            return f" \"{args.get('query', '')}\""
        if name == "web_fetch":
            return " " + args.get("url", "")[:60]
        if name == "glob":
            return f" {args.get('pattern', '')} in {args.get('path', '.')}"
        if name == "grep":
            return f" /{args.get('pattern', '')}/ in {args.get('path', '.')}"
        if name == "ripgrep":
            return f" /{args.get('pattern', '')}/ in {args.get('path', '.')}"
        if name == "spawn_agent":
            return f" [{args.get('system_prompt', '')}]"
        if name == "task_list":
            return f" {args.get('operation', '')}"
        return ""

    @staticmethod
    def _compact_result(result: str) -> str:
        """One-line result summary for compact tool display."""
        if result.startswith("[error") or result.startswith("[blocked") or result.startswith("[unknown"):
            return result.split("\n")[0][:100]
        if result.startswith("[cancelled"):
            return "[cancelled]"
        if "(no results)" in result[:30]:
            return "(no results)"
        if result.startswith("---") or result.startswith("@@"):
            added   = sum(1 for l in result.splitlines() if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in result.splitlines() if l.startswith("-") and not l.startswith("---"))
            return f"+{added} / -{removed} lines"
        first = result.split("\n")[0][:80]
        n = result.count("\n") + 1
        return first + (f"  [+{n - 1} lines]" if n > 1 else "")

    def _resolve_path(self, path: str, default: str = ".") -> str:
        """Resolve a path against session cwd if relative. Falls back to default if empty."""
        p = path.strip() if path else default
        resolved = Path(p)
        if not resolved.is_absolute():
            resolved = self.cwd / resolved
        return str(resolved)

    async def _approval_prompt(
        self,
        title: str,
        message: str,
        style: str = "yellow",
        tool_name: str = "",
        tool_args_str: str = "",
    ) -> tuple[bool, str]:
        """Three-option approval prompt: y / b (yes, but...) / n (no, with reason).

        Returns (approved: bool, notes: str).
        Notes are non-empty when user chose 'b' or 'n' and typed something.
        Caller injects notes into the tool result (approved) or rejection message (denied).
        In TUI mode, posts an approval_request event and awaits a Future resolved by the modal.
        """
        if self.tui_queue:
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            await self.tui_queue.put({
                "type": "approval_request",
                "title": title,
                "message": message,
                "style": style,
                "tool_name": tool_name,
                "tool_args_str": tool_args_str,
                "future": future,
            })
            try:
                return await future
            except Exception:
                return False, ""

        # CLI mode
        console.print(Panel(message, title=f"[{style}]{title}[/{style}]", border_style=style))
        loop = asyncio.get_event_loop()
        try:
            selected = await loop.run_in_executor(
                None, lambda: _menu_select(["Yes", "Yes, but... (add notes)", "No"])
            )
        except (EOFError, KeyboardInterrupt):
            return False, ""

        if selected == 0:
            return True, ""
        elif selected == 1:
            console.print("[dim]Notes (sent to Eli as context):[/dim]")
            try:
                notes = await loop.run_in_executor(None, lambda: input("   > ").strip())
            except (EOFError, KeyboardInterrupt):
                notes = ""
            return True, notes
        else:
            console.print("[dim]Reason (optional, sent to Eli):[/dim]")
            try:
                reason = await loop.run_in_executor(None, lambda: input("   > ").strip())
            except (EOFError, KeyboardInterrupt):
                reason = ""
            return False, reason

    async def _dispatch_tool(self, name: str, args: dict) -> str:
        """Pure tool dispatch — no display, no approval check."""
        try:
            if name == "bash":
                _bash_cwd = Path(args["cwd"]) if args.get("cwd") else self.cwd
                return await tool_bash(args.get("command", ""), args.get("timeout", 30), cwd=_bash_cwd)
            elif name == "read_file":
                return await tool_read_file(
                    self._resolve_path(args.get("path", "")),
                    offset=int(args.get("offset", 1)),
                    limit=int(args.get("limit", 200)),
                )
            elif name == "write_file":
                return await tool_write_file(self._resolve_path(args.get("path", "")), args.get("content", ""))
            elif name == "list_dir":
                return await tool_list_dir(self._resolve_path(args.get("path", ".")))
            elif name == "glob":
                return await tool_glob(args.get("pattern", "*"), self._resolve_path(args.get("path", ".")))
            elif name == "grep":
                return await tool_grep(
                    args.get("pattern", ""),
                    self._resolve_path(args.get("path", ".")),
                    args.get("glob", "**/*"),
                    args.get("case_insensitive", False),
                    args.get("context_lines", 2),
                )
            elif name == "ripgrep":
                return await tool_ripgrep(
                    args.get("pattern", ""),
                    self._resolve_path(args.get("path", ".")),
                    args.get("glob"),
                    args.get("type_filter"),
                    args.get("case_insensitive", False),
                    args.get("context_lines", 2),
                    args.get("fixed_strings", False),
                    args.get("max_results", 100),
                )
            elif name == "edit":
                return await tool_edit(self._resolve_path(args.get("path", "")), args.get("old_string", ""), args.get("new_string", ""))
            elif name == "web_fetch":
                return await tool_web_fetch(args.get("url", ""))
            elif name == "web_search":
                return await tool_web_search(args.get("query", ""), args.get("max_results", 6))
            elif name == "task_list":
                return await tool_task_list(
                    args.get("operation", "read"),
                    self._resolve_path(args.get("path", "TASKS.md"), default="TASKS.md"),
                    args.get("content", ""),
                    args.get("index"),
                    args.get("checked"),
                )
            elif name == "spawn_agent":
                if self._in_subagent:
                    return "[error: nested sub-agent spawning is not allowed]"
                return await self._tool_spawn_agent(
                    args.get("system_prompt", ""),
                    args.get("task", ""),
                    args.get("tools"),
                    args.get("think_level"),
                    min(args.get("max_iterations", 10), 30),
                    args.get("model"),
                )
            elif name == "analyze_image":
                if self._in_subagent:
                    return "[error: analyze_image not available inside sub-agents]"
                images = args.get("images") or ([args["image_path"]] if args.get("image_path") else [])
                return await self._tool_analyze_image(images, args.get("prompt"))
            elif name == "queue_agents":
                if self._in_subagent:
                    return "[error: queue_agents not available inside sub-agents]"
                return await self._tool_queue_agents(
                    args.get("agents", []),
                    args.get("label", ""),
                )
            elif name == "speak":
                return await tool_speak(args.get("text", ""))
            elif name == "highlight_in_editor":
                path = args.get("path", "")
                start_line = int(args.get("start_line", 1))
                end_line   = int(args.get("end_line", start_line))
                if self.tui_queue:
                    await self.tui_queue.put({
                        "type":       "highlight_in_editor",
                        "path":       path,
                        "start_line": start_line,
                        "end_line":   end_line,
                        "start_col":  int(args.get("start_col", -1)),
                        "end_col":    int(args.get("end_col",   -1)),
                    })
                return f"[highlighted {path} lines {start_line}–{end_line}]"
            elif name == "open_in_editor":
                path = args.get("path", "")
                line = int(args.get("line", 1))
                if self.tui_queue:
                    await self.tui_queue.put({"type": "open_in_editor", "path": path, "line": line})
                return f"[opened {path} at line {line} in editor]"
            else:
                return f"[unknown tool: {name}]"
        except Exception as e:
            return f"[tool error: {e}]"

    async def _tool_spawn_agent(
        self,
        system_prompt: str,
        task: str,
        tools: list[str] | None = None,
        think_level: str | None = None,
        max_iterations: int = 10,
        model: str | None = None,
    ) -> str:
        """Run an isolated sub-agent loop and return its final text response."""
        if self._in_subagent:
            return "[error: nested sub-agent spawning is not allowed]"

        # Resolve profile name → system prompt, and auto-extract recommended model
        if system_prompt and " " not in system_prompt.strip():
            profile_path = Path(__file__).parent / "agents" / f"{system_prompt}.md"
            if profile_path.exists():
                system_prompt = profile_path.read_text(encoding="utf-8")
                # Use the profile's recommended model if caller didn't specify one
                if not model:
                    _m = _re.search(r'\*\*Recommended model:\*\*\s*`([^`]+)`', system_prompt)
                    if _m:
                        model = _m.group(1).strip()
            # If not found, use the string as-is (may be a short raw prompt)

        # Build tool list — always exclude spawn_agent from sub-agents
        sub_tools = [t for t in TOOLS if t["function"]["name"] != "spawn_agent"]
        if tools:
            sub_tools = [t for t in sub_tools if t["function"]["name"] in tools]

        think = think_level or self.think_level
        max_iter = min(max_iterations, 50)

        # ── Model switch ──────────────────────────────────────────────────────
        restore_profile: str | None = None
        if model:
            commands = _load_commands()
            if model not in commands:
                available = "  ·  ".join(commands) or "(none)"
                if not self.tui_queue:
                    console.print(f"[dim yellow]⚠ unknown model '{model}' — using current model. Available: {available}[/dim yellow]")
                model = None
            # Capture what's running now so we can restore it afterward
            restore_profile = await _find_active_profile()
            if restore_profile == model:
                # Already on the right model — no switch needed, no restore needed
                restore_profile = None
                if not self.tui_queue:
                    console.print(f"[dim]  Model already loaded: {model}[/dim]")
            else:
                if not self.tui_queue:
                    console.print(Panel(
                        f"[yellow]Switching server to:[/yellow] {model}\n"
                        f"[dim]Will restore '{restore_profile or 'original'}' after agent finishes.[/dim]",
                        title="[yellow]Model Switch[/yellow]",
                        border_style="yellow",
                    ))
                ready = await _switch_server(model)
                if not ready:
                    return f"[error: server failed to start model '{model}' — agent aborted]"

        import datetime as _dt
        _today = _dt.date.today().strftime("%Y-%m-%d")
        _ctx = (
            f"\n\n[Session Context]\n"
            f"Today's date: {_today}\n"
            f"Current working directory: {self.cwd}\n"
            f"All relative file paths resolve against this directory."
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt + _ctx},
            {"role": "user", "content": task},
        ]

        if self.tui_queue:
            await self.tui_queue.put({"type": "system", "text": f"Agent: {task[:200]}{'…' if len(task) > 200 else ''}"})
        elif self.compact_mode:
            quote = random.choice(COMPACT_QUOTES)
            console.print(f"[dim cyan]  ◌ {quote}[/dim cyan]")
        else:
            console.print(Panel(
                f"[bold cyan]Task:[/bold cyan] {task[:300]}{'...' if len(task) > 300 else ''}",
                title="[cyan]Sub-Agent Spawned[/cyan]",
                border_style="cyan",
            ))

        self._in_subagent = True
        final_text = ""
        _hit_max_iter = True  # cleared when agent breaks naturally
        try:
            for _iter in range(max_iter):
                temperature = 0.3 if think == "deep" else 0.6
                think_kwargs: dict = {}
                if think == "off":
                    think_kwargs["chat_template_kwargs"] = {"enable_thinking": False}
                else:
                    think_kwargs["chat_template_kwargs"] = {"enable_thinking": True}

                payload = {
                    "model": self.model,
                    "messages": messages,
                    "tools": sub_tools,
                    "tool_choice": "auto",
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": temperature,
                    **think_kwargs,
                }

                thinking_buf = ""
                text_buf = ""
                tool_calls_received = []
                assistant_content = ""

                async with self.client.stream(
                    "POST",
                    f"{BASE_URL}/v1/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    response.raise_for_status()
                    _live_ctx = _NullLive() if (self.tui_queue or self.compact_mode) else Live(console=console, refresh_per_second=8)
                    with _live_ctx as live:
                        show_thinking = think != "off" and not self.compact_mode

                        async for event_type, data in stream_events(
                            response,
                            label=f"spawn_agent[iter] | model={model or self.model} | {BASE_URL}",
                        ):
                            if event_type == "think":
                                thinking_buf += data
                                if self.tui_queue:
                                    await self.tui_queue.put({"type": "think_token", "text": data})
                                elif show_thinking:
                                    live.update(Panel(
                                        Text(thinking_buf, style="dim italic"),
                                        title="[dim cyan]Agent Thinking...[/dim cyan]",
                                        border_style="dim cyan",
                                    ))
                            elif event_type == "text":
                                if self.tui_queue:
                                    text_buf += data
                                    assistant_content += data
                                    final_text = assistant_content
                                    await self.tui_queue.put({"type": "text_token", "text": data, "source": "agent"})
                                else:
                                    if thinking_buf and show_thinking:
                                        live.update(Text(""))
                                        live.stop()
                                        console.print(Panel(
                                            Text(thinking_buf, style="dim italic"),
                                            title="[dim cyan]Agent Thinking[/dim cyan]",
                                            border_style="dim cyan",
                                        ))
                                        live.start()
                                        thinking_buf = ""
                                    text_buf += data
                                    assistant_content += data
                                    final_text = assistant_content
                                    live.update(Panel(
                                        Markdown(text_buf),
                                        title="[cyan]Agent[/cyan]",
                                        border_style="cyan",
                                    ))
                            elif event_type == "tool_calls":
                                tool_calls_received = data
                                if not self.tui_queue:
                                    live.update(Text(""))
                            elif event_type == "stop":
                                if self.tui_queue:
                                    await self.tui_queue.put({"type": "text_done", "text": text_buf, "source": "agent"})
                                elif text_buf:
                                    live.update(Panel(
                                        Markdown(text_buf),
                                        title="[cyan]Agent[/cyan]",
                                        border_style="cyan",
                                    ))
                                else:
                                    live.update(Text(""))

                if assistant_content:          # keep last meaningful text; don't overwrite with ""
                    final_text = assistant_content

                # Fallback: model emitted tool calls as text
                if not tool_calls_received and assistant_content:
                    _parsed = _try_parse_text_tool_calls(assistant_content)
                    if _parsed:
                        tool_calls_received = _parsed
                        assistant_content = ""

                # Auto-announce if model produced no text before first tool call
                if tool_calls_received and not text_buf.strip():
                    if self.tui_queue:
                        tool_names = ", ".join(tc["function"]["name"] for tc in tool_calls_received)
                        await self.tui_queue.put({"type": "system", "text": f"  → {tool_names}…"})
                    else:
                        console.print(f"[dim]  {_tool_announce(tool_calls_received)}[/dim]")

                if tool_calls_received:
                    messages.append({
                        "role": "assistant",
                        "content": assistant_content or None,
                        "tool_calls": tool_calls_received,
                    })

                    async def _run_agent_tool(tc):
                        tc_name = tc["function"]["name"]
                        tc_args_str = tc["function"]["arguments"]
                        try:
                            tc_args = json.loads(tc_args_str) if tc_args_str.strip() else {}
                        except json.JSONDecodeError as _je:
                            _err = f"[error: malformed tool arguments — JSON parse failed: {_je}. Raw: {tc_args_str[:200]}]"
                            if self.tui_queue:
                                await self.tui_queue.put({"type": "tool_done", "id": tc["id"], "name": tc_name, "result": _err, "is_error": True})
                            return tc["id"], _err
                        if self.tui_queue:
                            await self.tui_queue.put({"type": "tool_start", "id": tc["id"], "name": tc_name, "args": tc_args_str})
                        elif self.compact_mode:
                            console.print(f"[dim]    ◌ {tc_name}{markup_escape(self._compact_args(tc_name, tc_args))}[/dim]")
                        else:
                            args_display = json.dumps(tc_args, indent=2) if tc_args else "(no args)"
                            console.print(Panel(
                                f"[bold]{tc_name}[/bold]\n[dim]{args_display}[/dim]",
                                title="[cyan]Agent Tool Call[/cyan]",
                                border_style="cyan",
                            ))
                        # Hard block — bare python/pip (venv rule, no override)
                        if tc_name == "bash":
                            cmd = tc_args.get("command", "")
                            if _is_bare_python(cmd):
                                if not self.tui_queue:
                                    console.print(Panel(
                                        f"[red]Bare python/pip call blocked.[/red] Sub-agents must use the project venv.\n"
                                        f"[dim]{cmd}[/dim]",
                                        title="[red]Venv Rule Violation[/red]",
                                        border_style="red",
                                    ))
                                tc_result = (
                                    "[blocked: bare python/pip — all Python must run inside the project venv. "
                                    "If no venv exists yet, create one first: python -m venv .venv "
                                    "Then use: .venv\\Scripts\\python.exe  or  .venv\\Scripts\\pip.exe install <pkg>]"
                                )
                                if not self.tui_queue:
                                    console.print(Panel(tc_result, title="[dim cyan]Agent Tool Result[/dim cyan]", border_style="red"))
                                return tc["id"], tc_result

                        # Apply same approval rules as top-level _call_tool
                        _sa_ask, _sa_title, _sa_msg, _sa_style = _build_approval_check(
                            tc_name, tc_args, self.approval_level,
                            prefix="Sub-Agent — ", session_rules=self.session_rules
                        )
                        if _sa_ask:
                            import json as _json
                            _sa_args_str = _json.dumps(tc_args, ensure_ascii=False)
                            _sa_approved, _sa_notes = await self._approval_prompt(
                                _sa_title, _sa_msg, _sa_style,
                                tool_name=tc_name, tool_args_str=_sa_args_str,
                            )
                            if not _sa_approved:
                                _reason = f" User says: {_sa_notes}." if _sa_notes else ""
                                tc_result = f"[cancelled by user]{_reason}"
                                if not self.tui_queue:
                                    console.print(Panel(
                                        tc_result,
                                        title="[dim cyan]Agent Tool Result[/dim cyan]",
                                        border_style="cyan",
                                    ))
                                return tc["id"], tc_result
                            if _sa_notes.startswith("session_allow:"):
                                self.session_rules.append(_sa_notes[len("session_allow:"):])
                            elif _sa_notes:
                                self._approval_notes = _sa_notes
                        tc_result = await self._dispatch_tool(tc_name, tc_args)
                        if self._approval_notes:
                            tc_result += f"\n[Note from user: {self._approval_notes}]"
                            self._approval_notes = ""
                        if self.tui_queue:
                            is_err = tc_result.startswith(("[error", "[unknown", "[blocked", "[cancelled"))
                            await self.tui_queue.put({"type": "tool_done", "id": tc["id"], "name": tc_name, "result": tc_result, "is_error": is_err})
                        elif self.compact_mode:
                            console.print(f"[dim]      → {markup_escape(self._compact_result(tc_result))}[/dim]")
                        else:
                            border = "cyan" if not tc_result.startswith("[error") and not tc_result.startswith("[unknown") and not tc_result.startswith("[blocked") else "red"
                            console.print(Panel(
                                markup_escape(tc_result[:2000]) + ("..." if len(tc_result) > 2000 else ""),
                                title="[dim cyan]Agent Tool Result[/dim cyan]",
                                border_style=border,
                            ))
                        return tc["id"], tc_result

                    _FETCH_SUMMARIZE_THRESHOLD = 2_000  # chars — below this, summarizing isn't worth it
                    _FETCH_INPUT_CAP = 40_000           # chars fed to the summarizer (hard ceiling)

                    async def _summarize_fetch(raw: str) -> str:
                        """Distil a long web_fetch result down to task-relevant facts only."""
                        prompt = (
                            "Extract ONLY the facts, figures, dates, names, and quotes from the "
                            "following web page content that are directly relevant to this research task:\n\n"
                            f"Task: {task[:400]}\n\n"
                            "Return a dense, factual summary — no fluff, no navigation, no ads. "
                            "Keep important quotes verbatim. If nothing is relevant, say so in one sentence.\n\n"
                            f"Content:\n{raw[:_FETCH_INPUT_CAP]}"
                        )
                        try:
                            r = await self.client.post(
                                f"{BASE_URL}/v1/chat/completions",
                                json={
                                    "model": self.model,
                                    "messages": [
                                        {"role": "system", "content": "You are a precise research extraction assistant. Be concise and factual."},
                                        {"role": "user", "content": prompt},
                                    ],
                                    "stream": False,
                                    "temperature": 0.1,
                                    "chat_template_kwargs": {"enable_thinking": False},
                                },
                                timeout=60,
                            )
                            r.raise_for_status()
                            summary = r.json()["choices"][0]["message"]["content"].strip()
                            if summary:
                                if not self.tui_queue:
                                    console.print(f"[dim]  ↳ web fetch distilled: {len(raw):,} → {len(summary):,} chars[/dim]")
                                return summary
                        except Exception as e:
                            if not self.tui_queue:
                                console.print(f"[dim yellow]  ↳ fetch summarize failed ({e}), using truncation[/dim yellow]")
                        return raw[:_FETCH_INPUT_CAP] + "\n[...truncated]"

                    for tc in tool_calls_received:
                        tc_id, tc_result_val = await _run_agent_tool(tc)
                        tc_name = tc["function"]["name"]
                        if tc_name == "web_fetch" and len(tc_result_val) > _FETCH_SUMMARIZE_THRESHOLD:
                            tc_result_val = await _summarize_fetch(tc_result_val)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tc_result_val,
                        })
                else:
                    _hit_max_iter = False
                    if assistant_content:
                        messages.append({"role": "assistant", "content": assistant_content})
                    break
        finally:
            self._in_subagent = False

            # Graceful summarise — always attempt when:
            #   (a) agent produced no text output at all, or
            #   (b) hit max iterations while still in a tool-call loop
            #   (c) agent concluded naturally but wrote < 200 chars (likely a bare "done" message)
            # This covers both model-switch and same-model agents.
            _last_role = messages[-1]["role"] if messages else "user"
            _thin_conclusion = bool(final_text) and len(final_text.strip()) < 200
            _needs_summary = (not final_text) or _thin_conclusion or (_hit_max_iter and _last_role == "tool")
            if _needs_summary and len(messages) > 2:
                _stop_reason = (
                    "You have reached the maximum number of tool-use iterations."
                    if _hit_max_iter
                    else "You are being stopped due to a model switch."
                )
                try:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"{_stop_reason} "
                            "Write a comprehensive research report covering everything you found. "
                            "Structure it with clear sections: Key Findings, Details & Evidence, "
                            "Sources, and Conclusions. Include all specific facts, figures, dates, "
                            "names, and quotes that are relevant. Do not omit important findings — "
                            "the caller will use this report as their primary record of the research."
                        ),
                    })
                    if not self.tui_queue:
                        console.print("[dim cyan]  Agent reached iteration limit — requesting summary...[/dim cyan]")
                    # Send system + user task + all assistant messages + last tool results.
                    # We want all the agent's reasoning visible, but tool results are large
                    # so we keep only the last 20 messages to stay within context.
                    _summary_msgs = messages[:2] + messages[-20:]
                    async with self.client.stream(
                        "POST",
                        f"{BASE_URL}/v1/chat/completions",
                        json={"model": self.model, "messages": _summary_msgs,
                              "stream": True, "temperature": 0.3},
                        headers={"Accept": "text/event-stream"},
                    ) as resp:
                        async for ev_type, ev_data in stream_events(
                            resp, label=f"agent-summary | {BASE_URL}"
                        ):
                            if ev_type == "text":
                                final_text += ev_data
                                if self.tui_queue:
                                    await self.tui_queue.put({"type": "text_token", "text": ev_data, "source": "agent"})
                except Exception:
                    pass  # best-effort only

            if model and restore_profile:
                if not self.tui_queue:
                    console.print(Panel(
                        f"[dim]Restoring server: {restore_profile}[/dim]",
                        title="[yellow]Model Restore[/yellow]",
                        border_style="yellow",
                    ))
                await _switch_server(restore_profile)

        if self.compact_mode and not self.tui_queue and final_text:
            console.print(Panel(Markdown(final_text), title="[cyan]Agent Report[/cyan]", border_style="cyan"))
        return final_text or "[sub-agent returned no text]"

    async def _tool_analyze_image(self, images: list[str], prompt: str | None = None) -> str:
        """Send one or more images to the vision model. Handles local model switching if needed."""
        import base64
        if not images:
            return "[error: no images provided]"
        DEFAULT_PROMPT = "Describe this image in detail: content, composition, any text or code visible."
        prompt = prompt or DEFAULT_PROMPT

        meta = _load_commands_meta()
        vision_external = meta.get("vision_external", False)
        # External: vision runs on a separate machine — use vision_url directly.
        # Local: vision model shares port 1234 (switched in/out by Server Manager).
        vision_url = meta.get("vision_url", "http://localhost:1236") if vision_external else BASE_URL

        # Find the vision profile name (first profile with vision: true in _meta)
        vision_profile: str | None = None
        for pname, pdata in meta.get("profiles", {}).items():
            if pdata.get("vision"):
                vision_profile = pname
                break

        # Decide whether to switch models
        need_switch = (not vision_external) and (vision_profile is not None)
        restore_profile: str | None = None

        async def _call_one(path_str: str) -> str:
            path = Path(self._resolve_path(path_str))
            if not path.exists():
                return f"[error: image not found: {path}]"
            ext = path.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
            try:
                b64 = base64.b64encode(path.read_bytes()).decode()
            except Exception as e:
                return f"[error: could not read image: {e}]"
            payload = {
                "model": "auto",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 1024,
                "temperature": 0.3,
            }
            try:
                async with httpx.AsyncClient(timeout=120.0) as c:
                    r = await c.post(f"{vision_url}/v1/chat/completions", json=payload)
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                return f"[error: vision API call failed: {e}]"

        if need_switch:
            restore_profile = await _find_active_profile()
            if restore_profile == vision_profile:
                need_switch = False  # already on vision model
            else:
                console.print(Panel(
                    f"Switching to vision model: [bold]{vision_profile}[/bold]\n"
                    f"Will restore [dim]{restore_profile or 'previous model'}[/dim] after.",
                    border_style="magenta",
                ))
                ok = await _switch_server(vision_profile)
                if not ok:
                    return f"[error: failed to switch to vision model '{vision_profile}']"

        results = []
        total = len(images)
        try:
            for i, img_path in enumerate(images):
                if total > 1:
                    console.print(f"[magenta][Vision {i+1}/{total}][/magenta] {img_path}")
                result = await _call_one(img_path)
                results.append(result)
        finally:
            if need_switch and restore_profile:
                console.print(f"[magenta]Vision done. Restoring [bold]{restore_profile}[/bold]...[/magenta]")
                await _switch_server(restore_profile)

        if total == 1:
            return results[0]
        return "\n\n".join(
            f"[Image {i+1}: {Path(p).name}]\n{r}"
            for i, (p, r) in enumerate(zip(images, results))
        )

    async def _tool_queue_agents(self, agent_specs: list[dict], label: str = "") -> str:
        """Run a list of agents sequentially, store results, return consolidated summary."""
        if not agent_specs:
            return "[error: queue_agents called with empty agents list]"

        # Validate models upfront — strip unknown ones rather than aborting
        commands = _load_commands()
        for spec in agent_specs:
            m = spec.get("model")
            if m and m not in commands:
                available = "  ·  ".join(commands) or "(none)"
                console.print(f"[dim yellow]  ⚠ unknown model '{m}' — using current model. Available: {available}[/dim yellow]")
                spec["model"] = None

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        slug = label.lower().replace(" ", "-")[:32] if label else "run"
        queue_dir = SESSIONS_DIR / f"queue_{ts}_{slug}"
        queue_dir.mkdir(parents=True, exist_ok=True)

        restore_profile = await _find_active_profile()
        current_model = restore_profile
        total = len(agent_specs)
        results = []

        console.print(Panel(
            f"[bold cyan]Queue:[/bold cyan] {total} agent(s)  ·  label: {label or '(none)'}\n"
            f"[dim]Results → {queue_dir}[/dim]",
            title="[cyan]Agent Queue Started[/cyan]",
            border_style="cyan",
        ))

        loop = asyncio.get_event_loop()

        for idx, spec in enumerate(agent_specs):
            agent_num = idx + 1
            target_model = spec.get("model")
            timeout_s = max(30, int(spec.get("timeout_seconds", 300)))
            max_iter = min(int(spec.get("max_iterations", 10)), 50)
            think = spec.get("think_level") or self.think_level
            tools_wl = spec.get("tools")
            task = spec.get("task", "")
            sp = spec.get("system_prompt", "")

            # Resolve profile → system prompt, auto-extract recommended model
            if sp and " " not in sp.strip():
                profile_path = Path(__file__).parent / "agents" / f"{sp}.md"
                if profile_path.exists():
                    sp = profile_path.read_text(encoding="utf-8")
                    if not target_model:
                        _m = _re.search(r'\*\*Recommended model:\*\*\s*`([^`]+)`', sp)
                        if _m:
                            target_model = _m.group(1).strip()

            # Switch model only when needed
            if target_model and target_model != current_model:
                if not self.tui_queue:
                    console.print(f"[dim yellow]  Switching to: {target_model}[/dim yellow]")
                switched = await _switch_server(target_model)
                if not switched:
                    results.append({
                        "index": idx, "system_prompt": spec.get("system_prompt", ""),
                        "task": task, "model": target_model,
                        "timeout_seconds": timeout_s, "status": "error",
                        "result": f"[error: failed to switch to model '{target_model}']",
                        "duration_seconds": 0.0,
                    })
                    if not self.tui_queue:
                        console.print(f"[red]  Agent {agent_num}/{total} skipped — model switch failed[/red]")
                    continue
                current_model = target_model

            # Build tool list
            sub_tools = [t for t in TOOLS if t["function"]["name"] not in ("spawn_agent", "queue_agents", "analyze_image")]
            if tools_wl:
                sub_tools = [t for t in sub_tools if t["function"]["name"] in tools_wl]

            messages: list[dict] = [
                {"role": "system", "content": sp},
                {"role": "user",   "content": task},
            ]

            if not self.tui_queue:
                console.print(Panel(
                    f"[bold]Task:[/bold] {task[:200]}{'...' if len(task) > 200 else ''}\n"
                    f"[dim]Model: {target_model or current_model or 'current'}  ·  "
                    f"Timeout: {timeout_s}s  ·  Max iter: {max_iter}[/dim]",
                    title=f"[cyan]Queue Agent {agent_num}/{total}[/cyan]",
                    border_style="cyan",
                ))

            start_t = loop.time()
            agent_status = "completed"
            final_text = ""
            self._in_subagent = True
            try:
                deadline = loop.time() + timeout_s
                for _iter in range(max_iter):
                    # Check deadline before starting new iteration
                    if loop.time() >= deadline:
                        agent_status = "timeout"
                        if messages and messages[-1]["role"] != "user":
                            messages.append({
                                "role": "user",
                                "content": "Time limit reached. Summarise your findings concisely now.",
                            })
                            try:
                                async with self.client.stream(
                                    "POST", f"{BASE_URL}/v1/chat/completions",
                                    json={"model": self.model, "messages": messages,
                                          "stream": True, "temperature": 0.3},
                                    headers={"Accept": "text/event-stream"},
                                ) as resp:
                                    async for ev_type, ev_data in stream_events(
                                        resp, label=f"queue_agents-summary | {BASE_URL}"
                                    ):
                                        if ev_type == "text":
                                            final_text += ev_data
                            except Exception:
                                pass
                        break

                    think_kwargs: dict = {}
                    if think == "off":
                        think_kwargs["chat_template_kwargs"] = {"enable_thinking": False}
                    else:
                        think_kwargs["chat_template_kwargs"] = {"enable_thinking": True}

                    payload = {
                        "model": self.model, "messages": messages,
                        "tools": sub_tools, "tool_choice": "auto",
                        "stream": True, "stream_options": {"include_usage": True},
                        "temperature": 0.3 if think == "deep" else 0.6,
                        **think_kwargs,
                    }

                    text_buf = ""
                    assistant_content = ""
                    tool_calls_received = []

                    async with self.client.stream(
                        "POST", f"{BASE_URL}/v1/chat/completions",
                        json=payload, headers={"Accept": "text/event-stream"},
                    ) as response:
                        response.raise_for_status()
                        _live = _NullLive() if (self.tui_queue or self.compact_mode) else Live(console=console, refresh_per_second=8)
                        with _live as live:
                            async for ev_type, ev_data in stream_events(
                                response, label=f"queue_agents[iter] | model={current_model} | {BASE_URL}"
                            ):
                                if ev_type == "text":
                                    text_buf += ev_data
                                    assistant_content += ev_data
                                    final_text = assistant_content
                                    live.update(Panel(
                                        Markdown(text_buf),
                                        title=f"[cyan]Agent {agent_num}[/cyan]",
                                        border_style="cyan",
                                    ))
                                elif ev_type == "tool_calls":
                                    tool_calls_received = ev_data
                                    live.update(Text(""))
                                elif ev_type == "stop":
                                    if not text_buf:
                                        live.update(Text(""))

                    # Fallback: model emitted tool calls as text
                    if not tool_calls_received and assistant_content:
                        _parsed = _try_parse_text_tool_calls(assistant_content)
                        if _parsed:
                            tool_calls_received = _parsed
                            assistant_content = ""

                    if tool_calls_received:
                        messages.append({
                            "role": "assistant",
                            "content": assistant_content or None,
                            "tool_calls": tool_calls_received,
                        })
                        async def _run_q_tool(tc):
                            tc_name = tc["function"]["name"]
                            try:
                                tc_args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"].strip() else {}
                            except json.JSONDecodeError:
                                tc_args = {}
                            if self.compact_mode:
                                console.print(f"[dim]    ◌ {tc_name}{markup_escape(self._compact_args(tc_name, tc_args))}[/dim]")
                            tc_result = await self._dispatch_tool(tc_name, tc_args)
                            if self.compact_mode:
                                console.print(f"[dim]      → {markup_escape(self._compact_result(tc_result))}[/dim]")
                            return tc["id"], tc_result
                        for tc in tool_calls_received:
                            tc_id, tc_result_val = await _run_q_tool(tc)
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tc_result_val})
                    else:
                        if assistant_content:
                            messages.append({"role": "assistant", "content": assistant_content})
                        break

            except Exception as e:
                agent_status = "error"
                final_text = final_text or f"[error during agent execution: {e}]"
            finally:
                self._in_subagent = False

            duration = round(loop.time() - start_t, 1)
            status_icon = {"completed": "✓", "timeout": "⏱", "error": "✗"}.get(agent_status, "?")
            if not self.tui_queue:
                console.print(Panel(
                    Markdown(final_text[:500] + ("..." if len(final_text) > 500 else "")) if final_text else "[dim](no output)[/dim]",
                    title=f"[cyan]Agent {agent_num}/{total}  {status_icon} {agent_status}  ({duration}s)[/cyan]",
                    border_style="cyan" if agent_status == "completed" else "yellow" if agent_status == "timeout" else "red",
                ))

            results.append({
                "index": idx,
                "system_prompt": spec.get("system_prompt", ""),
                "task": task,
                "model": target_model or current_model or "",
                "timeout_seconds": timeout_s,
                "status": agent_status,
                "result": final_text or "[no output]",
                "duration_seconds": duration,
            })

        # Restore original model if we moved away from it
        if current_model != restore_profile and restore_profile:
            if not self.tui_queue:
                console.print(Panel(
                    f"[dim]Restoring: {restore_profile}[/dim]",
                    title="[yellow]Model Restore[/yellow]",
                    border_style="yellow",
                ))
            await _switch_server(restore_profile)

        # Write results to disk
        output = {
            "label": label,
            "started": ts,
            "completed_at": datetime.now().isoformat(),
            "agent_count": total,
            "results": results,
        }
        results_path = queue_dir / "results.json"
        results_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

        # Build return summary
        counts = {"completed": 0, "timeout": 0, "error": 0}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary_lines = [
            f"Queue complete: {total} agent(s) — "
            f"{counts['completed']} completed, {counts['timeout']} timeout, {counts['error']} error(s)",
            f"Results saved: {results_path}",
            "",
        ]
        for r in results:
            icon = {"completed": "✓", "timeout": "⏱", "error": "✗"}.get(r["status"], "?")
            snippet = r["result"][:200].replace("\n", " ")
            summary_lines.append(f"{icon} Agent {r['index']+1}: {snippet}{'...' if len(r['result']) > 200 else ''}")
        return "\n".join(summary_lines)

    async def _call_tool(self, name: str, arguments_str: str, call_id: str) -> str:
        try:
            args = json.loads(arguments_str) if arguments_str.strip() else {}
        except json.JSONDecodeError as _je:
            return f"[error: malformed tool arguments — JSON parse failed: {_je}. Raw: {arguments_str[:200]}]"

        # Display tool call
        if self.tui_queue:
            await self.tui_queue.put({"type": "tool_start", "id": call_id, "name": name, "args": arguments_str})
        elif self.compact_mode:
            console.print(f"[dim]  ↳ {name}{markup_escape(self._compact_args(name, args))}[/dim]")
        else:
            args_display = json.dumps(args, indent=2) if args else "(no args)"
            console.print(
                Panel(
                    f"[bold]{name}[/bold]\n[dim]{args_display}[/dim]",
                    title="[yellow]Tool Call[/yellow]",
                    border_style="yellow",
                )
            )

        # Hard block — bare python/pip commands always refused (venv rule)
        if name == "bash":
            cmd = args.get("command", "")
            if _is_bare_python(cmd):
                console.print(Panel(
                    f"[red]Bare python/pip call blocked.[/red]\n"
                    f"[dim]{cmd}[/dim]\n\n"
                    "All Python must run inside the project venv.\n"
                    "[yellow]New project?[/yellow] Create venv first: [bold]python -m venv .venv[/bold]\n"
                    "[yellow]Then use:[/yellow] [bold].venv\\Scripts\\python.exe script.py[/bold]  or  [bold].venv\\Scripts\\pip.exe install <pkg>[/bold]",
                    title="[red]Venv Rule Violation[/red]",
                    border_style="red",
                ))
                return (
                    "[blocked: bare python/pip — all Python must run inside the project venv. "
                    "New project? Create the venv first: python -m venv .venv "
                    "Then: .venv\\Scripts\\python.exe script.py  or  .venv\\Scripts\\pip.exe install <pkg>]"
                )

        # New-project gate — fires before the general approval guard, in all modes except yolo.
        # Blocks scaffolding until the user confirms a plan has been approved.
        if self.approval_level != "yolo":
            _np = _new_project_path(name, args, self.cwd)
            if _np is not None:
                # Detect whether Eli asked any questions before trying to build.
                _asked_questions = any(
                    m["role"] == "assistant" and m.get("content") and not m.get("tool_calls")
                    for m in self.messages
                )
                _skipped_step1 = not _asked_questions
                _gate_msg = (
                    f"[magenta bold]New project structure detected.[/magenta bold]\n"
                    f"[dim]Path: {_np}[/dim]\n\n"
                )
                if _skipped_step1:
                    _gate_msg += (
                        "[red]You went straight to creating files without asking a single question.[/red]\n"
                        "Step 1 of the workflow is mandatory: ask all clarifying questions first,\n"
                        "then wait for answers before doing anything else."
                    )
                else:
                    _gate_msg += (
                        "Have you: asked clarifying questions, received answers, run research,\n"
                        "written a proposal, had it reviewed, and received explicit approval?"
                    )
                _np_approved, _np_notes = await self._approval_prompt(
                    "Plan Approval Required", _gate_msg, style="magenta",
                )
                if not _np_approved:
                    _reason = f" User says: {_np_notes}." if _np_notes else ""
                    _step1_reminder = (
                        " You skipped Step 1 entirely. Your NEXT action must be a message"
                        " asking the user clarifying questions — not a tool call."
                        if _skipped_step1 else ""
                    )
                    return (
                        _GATE_REJECTED_PREFIX +
                        f" No plan approved.{_reason}{_step1_reminder} STOP all file/directory creation. "
                        "Follow the new project workflow: "
                        "(1) Ask clarifying questions in one message and wait for answers. "
                        "(2) spawn_agent researcher. "
                        "(3) Write proposal, spawn expert_coder to review it. "
                        "(4) Present plan, ask 'Shall I proceed?', wait for yes."
                    )
                if _np_notes:
                    self._approval_notes = _np_notes

        # Approval guard
        _ask, _ask_title, _ask_msg, _ask_style = _build_approval_check(
            name, args, self.approval_level, session_rules=self.session_rules
        )
        if _ask:
            import json as _json
            _args_str = _json.dumps(args, ensure_ascii=False)
            _approved, _notes = await self._approval_prompt(
                _ask_title, _ask_msg, _ask_style,
                tool_name=name, tool_args_str=_args_str,
            )
            if not _approved:
                _reason = f" User says: {_notes}." if _notes else ""
                return f"[cancelled by user]{_reason}"
            if _notes.startswith("session_allow:"):
                self.session_rules.append(_notes[len("session_allow:"):])
            elif _notes:
                self._approval_notes = _notes

        # Dispatch
        result = await self._dispatch_tool(name, args)

        # Inject any approval notes (from "yes, but...") into the tool result
        if self._approval_notes:
            result += f"\n[Note from user: {self._approval_notes}]"
            self._approval_notes = ""

        # Post-edit hook — run build/test after file edits
        if name in ("edit", "write_file") and not result.startswith("[error"):
            hook_out = await self._run_post_edit_hook(args.get("path", ""))
            if hook_out:
                result += f"\n\n{hook_out}"

        if self.tui_queue:
            pass  # tool_done emitted by send_and_stream after _run_one returns
        elif self.compact_mode:
            console.print(f"[dim]    → {markup_escape(self._compact_result(result))}[/dim]")
        elif name == "edit" and not result.startswith("[error"):
            from rich.syntax import Syntax
            # Split diff from any appended hook output
            diff_part = result
            hook_part = None
            if "\n\n[Hook:" in result:
                diff_part, hook_part = result.split("\n\n[Hook:", 1)
                hook_part = "[Hook:" + hook_part
            console.print(Panel(
                Syntax(diff_part, "diff", theme="ansi_dark", word_wrap=False, line_numbers=True),
                title=f"[yellow]Edit — {Path(args.get('path', '')).name}[/yellow]",
                border_style="yellow",
            ))
            if hook_part:
                hook_border = "red" if "[FAILED]" in hook_part else "green"
                console.print(Panel(
                    markup_escape(hook_part[:2000]),
                    title="[dim]Hook Result[/dim]",
                    border_style=hook_border,
                ))
        else:
            border = "green" if not result.startswith("[error") and not result.startswith("[unknown") and not result.startswith("[blocked") else "red"
            preview = markup_escape(result[:2000]) + ("..." if len(result) > 2000 else "")
            console.print(
                Panel(preview, title="[dim]Tool Result[/dim]", border_style=border)
            )
        return result

# ── Voice conversation loop ────────────────────────────────────────────────────

async def _voice_model_call(
    history: list,
    client: httpx.AsyncClient,
    session: "ChatSession | None" = None,
    use_tools: bool = False,
) -> str:
    """Send voice history to the model and return the full reply text. Returns '' on failure.

    When use_tools=True and session is provided, the model may call tools.  The
    tool loop runs silently (results shown in terminal but not spoken).  The final
    text reply is streamed to the terminal and returned for TTS.
    """

    # ── Tool loop (non-streaming so we can inspect tool_calls) ─────────────────
    if use_tools and session is not None:
        working_history = list(history)
        tools_used = False
        for _round in range(6):   # cap tool-calling rounds to prevent infinite loops
            payload_nt = {
                "model": "auto",
                "messages": working_history,
                "stream": False,
                "temperature": 0.85,
                "max_tokens": 600,
                "tools": TOOLS,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            try:
                r = await client.post(f"{BASE_URL}/v1/chat/completions", json=payload_nt)
                r.raise_for_status()
            except httpx.ConnectError:
                console.print("[red]LLM server went away.[/red]")
                return ""
            except httpx.HTTPError as e:
                console.print(f"[red]Server error: {e}[/red]")
                return ""

            choice = r.json()["choices"][0]
            msg    = choice["message"]
            finish = choice.get("finish_reason", "")

            if finish == "tool_calls" or msg.get("tool_calls"):
                # Execute every tool call and append results to working history
                tools_used = True
                working_history.append({"role": "assistant", **{k: v for k, v in msg.items() if k != "role"}})
                for tc in msg.get("tool_calls", []):
                    tc_id   = tc["id"]
                    tc_name = tc["function"]["name"]
                    try:
                        tc_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        tc_args = {}
                    console.print(f"[dim]  ◌ {tc_name}({tc_args})[/dim]")
                    tc_result = await session._dispatch_tool(tc_name, tc_args)
                    console.print(f"[dim]    → {tc_result[:200]}[/dim]")
                    working_history.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tc_result,
                    })
                continue   # go around for the model to process results

            # Model returned text directly (no tool calls this round).
            text_reply = (msg.get("content") or "").strip()

            if not tools_used:
                # No tools were called at all — return the direct reply as-is.
                # A summary call here would produce a past-tense recap of a live reply.
                if text_reply:
                    console.print()
                    console.print(text_reply, markup=False)
                    console.print()
                    return text_reply
                return ""

            # Tools were used — the 600-token reply may be long or structured.
            # Make one tight follow-up call: no tools, 180 tokens, spoken-summary prompt.
            if text_reply:
                working_history.append({"role": "assistant", "content": text_reply})

            working_history.append({
                "role": "user",
                "content": (
                    "[Internal instruction — not from the human] "
                    "Summarise what you just found or did in one to three short spoken sentences. "
                    "No lists, no markdown. Speak directly to the person."
                ),
            })
            try:
                r2 = await client.post(f"{BASE_URL}/v1/chat/completions", json={
                    "model": "auto",
                    "messages": working_history,
                    "stream": False,
                    "temperature": 0.85,
                    "max_tokens": 180,
                    "chat_template_kwargs": {"enable_thinking": False},
                })
                r2.raise_for_status()
                text_content = (r2.json()["choices"][0]["message"].get("content") or "").strip()
            except Exception:
                text_content = text_reply

            if text_content:
                console.print()
                console.print(text_content, markup=False)
                console.print()
                return text_content

            return ""

        return ""   # ran out of tool rounds without a text reply

    # ── Simple streaming (no tools) ────────────────────────────────────────────
    payload = {
        "model": "auto",
        "messages": history,
        "stream": True,
        "temperature": 0.85,
        "max_tokens": 180,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    reply_parts: list[str] = []
    console.print()
    try:
        async with client.stream(
            "POST",
            f"{BASE_URL}/v1/chat/completions",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            async for event_type, data in stream_events(
                response, label=f"simple-stream | {BASE_URL}"
            ):
                if event_type == "text":
                    reply_parts.append(data)
                    console.print(data, end="", markup=False)
    except httpx.ConnectError:
        console.print("\n[red]LLM server went away — is llama-server still running?[/red]")
        return ""
    except httpx.RemoteProtocolError:
        console.print("\n[red]LLM server closed the connection mid-stream.[/red]")
        return ""
    except httpx.HTTPError as e:
        console.print(f"\n[red]Server error: {e}[/red]")
        return ""
    console.print()
    return "".join(reply_parts).strip()


async def _voice_record_ptt(ptt_event_start: asyncio.Event, ptt_event_stop: asyncio.Event) -> bytes:
    """Record audio while PTT is held. Returns raw int16 PCM bytes."""
    import numpy as np
    import sounddevice as sd

    await ptt_event_start.wait()
    ptt_event_start.clear()
    console.print("[bold red]● REC[/bold red]", end="\r")

    chunks: list[bytes] = []
    loop = asyncio.get_event_loop()
    chunk_done = asyncio.Event()
    current_chunk: list = [None]

    def callback(indata, frames, time_info, status):
        current_chunk[0] = indata.copy()
        loop.call_soon_threadsafe(chunk_done.set)

    block_size = int(VOICE_SAMPLE_RATE * 0.05)  # 50ms chunks
    with sd.InputStream(
        samplerate=VOICE_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=block_size,
        callback=callback,
    ):
        while not ptt_event_stop.is_set():
            chunk_done.clear()
            await asyncio.wait_for(chunk_done.wait(), timeout=0.2)
            if current_chunk[0] is not None:
                chunks.append(current_chunk[0].tobytes())

    return b"".join(chunks)


async def _voice_record_auto(quit_event: asyncio.Event) -> bytes | None:
    """Record audio using WebRTC VAD + RMS gate. Returns PCM bytes or None if quit."""
    import numpy as np
    import sounddevice as sd
    import webrtcvad

    vad = webrtcvad.Vad(2)
    frame_ms = 20
    frame_samples = int(VOICE_SAMPLE_RATE * frame_ms / 1000)
    silence_frames_needed = int(VOICE_SILENCE_TIMEOUT * 1000 / frame_ms)
    min_speech_frames = int(VOICE_MIN_SPEECH_MS / frame_ms)

    speech_chunks: list[bytes] = []
    silence_count = 0
    in_speech = False
    onset_count = 0   # consecutive loud+speech frames; must reach VOICE_ONSET_FRAMES

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(q.put_nowait, indata.copy().tobytes())

    with sd.InputStream(
        samplerate=VOICE_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
        callback=callback,
    ):
        while not quit_event.is_set():
            try:
                frame = await asyncio.wait_for(q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            # RMS gate — ignore frames below volume threshold regardless of VAD
            audio_np = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(audio_np ** 2)))
            is_speech = rms >= VOICE_RMS_THRESHOLD and vad.is_speech(frame, VOICE_SAMPLE_RATE)

            if is_speech:
                onset_count += 1
                if not in_speech:
                    if onset_count >= VOICE_ONSET_FRAMES:
                        # Confirmed speech onset
                        in_speech = True
                        console.print("[bold red]● REC[/bold red]", end="\r")
                else:
                    speech_chunks.append(frame)
                    silence_count = 0
            else:
                onset_count = 0
                if in_speech:
                    speech_chunks.append(frame)
                    silence_count += 1
                    if silence_count >= silence_frames_needed:
                        if len(speech_chunks) >= min_speech_frames:
                            return b"".join(speech_chunks)
                        else:
                            speech_chunks.clear()
                            in_speech = False
                            silence_count = 0
                            console.print("[dim]  (too short, discarded)[/dim]")
    return None


async def _voice_transcribe(audio_bytes: bytes) -> str:
    """POST raw PCM to /transcribe and return transcript."""
    import requests as _requests
    console.print("[dim]⏳ transcribing...[/dim]", end="\r")
    resp = _requests.post(
        f"{TTS_URL}/transcribe",
        data=audio_bytes,
        params={"sample_rate": VOICE_SAMPLE_RATE},
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


async def _voice_speak(text: str) -> None:
    """Send text to TTS server for playback (blocking)."""
    import requests as _requests
    _requests.post(
        f"{TTS_URL}/play",
        json={"text": text},
        timeout=60,
    )


async def _voice_conversation_loop(session: ChatSession, mode: str = "ptt", use_tools: bool = False) -> None:
    """Blocking voice conversation loop. Exit: Escape (ptt/auto) or q+Enter (CLI).
    TUI-aware: when session.tui_queue is set, emits events instead of console.print.
    """
    _tq = session.tui_queue  # None in CLI, asyncio.Queue in TUI

    async def _sys(text: str) -> None:
        """Emit a status line — TUI system event or console.print."""
        if _tq:
            await _tq.put({"type": "system", "text": text})
        else:
            console.print(f"[dim]{text}[/dim]")

    async def _voice_msg(role: str, text: str) -> None:
        """Emit a transcript/reply line as a chat bubble or console line."""
        if _tq:
            # Reuse text_done so the drain loop renders it as a message widget
            src = "eli" if role == "assistant" else "eli"
            prefix = "You said" if role == "user" else "Eli"
            await _tq.put({"type": "system", "text": f"[{prefix}] {text}"})
        else:
            label = "[bold green]You:[/bold green]" if role == "user" else "[bold cyan]Eli:[/bold cyan]"
            console.print(f"{label} {text}")

    try:
        import sounddevice  # noqa: F401 — check it's available
    except ImportError:
        if _tq:
            await _tq.put({"type": "system", "text": "sounddevice not installed — voice unavailable"})
        else:
            console.print("[red]sounddevice not installed. Run: .venv\\Scripts\\pip install sounddevice[/red]")
        return

    # Load persona from agents/voice.md; fall back to inline constant
    voice_agent_file = Path(__file__).parent / "agents" / "voice.md"
    voice_prompt = (
        voice_agent_file.read_text(encoding="utf-8")
        if voice_agent_file.exists()
        else VOICE_SYSTEM_PROMPT
    )
    history = [{"role": "system", "content": voice_prompt}]

    from pynput import keyboard as _kb

    quit_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    if mode == "ptt":
        # Resolve the PTT key
        try:
            ptt_key = getattr(_kb.Key, PTT_KEY)
        except AttributeError:
            ptt_key = _kb.KeyCode.from_char(PTT_KEY)

        ptt_start = asyncio.Event()
        ptt_stop  = asyncio.Event()

        def on_press(key):
            if key == ptt_key:
                loop.call_soon_threadsafe(ptt_start.set)
                loop.call_soon_threadsafe(ptt_stop.clear)
            elif key == _kb.Key.esc:
                loop.call_soon_threadsafe(quit_event.set)

        def on_release(key):
            if key == ptt_key:
                loop.call_soon_threadsafe(ptt_stop.set)

        listener = _kb.Listener(on_press=on_press, on_release=on_release)
        listener.start()

        tools_note = "tools: on" if use_tools else "tools: off"
        await _sys(f"Voice PTT — hold {PTT_KEY} to speak, release to send. ESC to exit. {tools_note}")

        try:
            while not quit_event.is_set():
                while not ptt_start.is_set() and not quit_event.is_set():
                    await asyncio.sleep(0.05)

                if quit_event.is_set():
                    break

                audio = await _voice_record_ptt(ptt_start, ptt_stop)
                if not audio:
                    continue

                transcript = await _voice_transcribe(audio)
                if not transcript:
                    await _sys("(nothing heard)")
                    continue

                await _voice_msg("user", transcript)
                history.append({"role": "user", "content": transcript})

                reply = await _voice_model_call(history, session.client, session=session, use_tools=use_tools)
                if not reply:
                    continue
                history.append({"role": "assistant", "content": reply})

                await _voice_msg("assistant", reply)
                await _voice_speak(reply)
                await asyncio.sleep(VOICE_POST_TTS_DELAY)

        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()

    else:  # auto VAD mode
        try:
            import webrtcvad  # noqa: F401
        except ImportError:
            if _tq:
                await _tq.put({"type": "system", "text": "webrtcvad not installed — auto voice unavailable"})
            else:
                console.print("[red]webrtcvad not installed. Run: .venv\\Scripts\\pip install webrtcvad[/red]")
            return

        def on_press_auto(key):
            if key == _kb.Key.esc:
                loop.call_soon_threadsafe(quit_event.set)

        listener = _kb.Listener(on_press=on_press_auto)
        listener.start()

        tools_note = "tools: on" if use_tools else "tools: off"
        await _sys(f"Voice AUTO — speak naturally, pause to send. ESC to exit. {tools_note}")

        try:
            while not quit_event.is_set():
                audio = await _voice_record_auto(quit_event)
                if not audio or quit_event.is_set():
                    break

                transcript = await _voice_transcribe(audio)
                if not transcript:
                    await _sys("(nothing heard)")
                    continue

                await _voice_msg("user", transcript)
                history.append({"role": "user", "content": transcript})

                reply = await _voice_model_call(history, session.client, session=session, use_tools=use_tools)
                if not reply:
                    continue
                history.append({"role": "assistant", "content": reply})

                await _voice_msg("assistant", reply)
                await _voice_speak(reply)
                await asyncio.sleep(VOICE_POST_TTS_DELAY)

        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()

    await _sys("Voice mode ended")


# ── Slash command handler ─────────────────────────────────────────────────────
async def handle_slash_command(cmd: str, session: ChatSession) -> bool:
    """Returns True if command was handled (skip sending to model)."""
    parts = cmd.strip().split()
    name = parts[0].lower()

    if name == "/help":
        console.print(
            Panel(
                "\n".join([
                    "[bold]/clear[/bold]                 Reset message history",
                    "[bold]/tools[/bold]                 List available tools",
                    "[bold]/think \\[off|on|deep\\][/bold]   Set thinking level (or cycle)",
                    "[bold]/save \\[path\\][/bold]           Save conversation to JSON",
                    "[bold]/compact[/bold]               Summarise older messages to free context",
                    "[bold]/debug \\[path|off\\][/bold]     Capture raw SSE stream to file (default: debug_stream_TIMESTAMP.log)",
                    "[bold]/status[/bold]                Show token usage and context window info",
                    "[bold]/sessions[/bold]              List saved sessions",
                    "[bold]/resume \\[name\\][/bold]         Load a saved session (replaces current)",
                    "[bold]/approval \\[mode\\][/bold]       Set approval tier: auto|ask-writes|ask-all|yolo",
                    "[bold]/cd \\[path\\][/bold]             Set working directory for bash commands",
                    "[bold]/pwd[/bold]                   Show current working directory",
                    "[bold]/model \\[id\\][/bold]             Switch model or list available models",
                    "[bold]/role \\[name\\][/bold]            Adopt an agent persona in the current session",
                    "[bold]/config[/bold]                Show loaded eli.toml project config",
                    "[bold]/skills[/bold]                List available skills",
                    "[bold]/skill <name> \\[args\\][/bold]   Invoke a skill explicitly",
                    "[bold]/queue-results \\[label\\][/bold]  List recent agent queue runs, or show one by label",
                    "[bold]/voice \\[ptt|auto\\] \\[tools\\][/bold]  Start voice sparring mode (tools flag enables tool use)",
                    "[bold]/help[/bold]                  Show this message",
                    "",
                    "[bold]Shift+Tab[/bold]              Cycle mode: normal → plan → normal",
                    "[dim]  normal  tools are executed automatically[/dim]",
                    "[dim]  plan    model describes its plan, no tools run[/dim]",
                    "[bold]Ctrl+O[/bold]                 Toggle compact mode (collapse thinking/tools)",
                    "",
                    "[dim]Enter  Submit  |  Alt+Enter  Newline  |  Ctrl+D  Exit  |  Ctrl+C  Interrupt[/dim]",
                ]),
                title="Commands",
                border_style="cyan",
            )
        )
        return True

    elif name == "/clear":
        _initial = _build_initial_messages()
        session.messages = _initial
        session._n_fixed = len(_initial)
        session.tokens_used = session.tokens_prompt = session.tokens_completion = 0
        await session._refresh_project_config()
        console.print(Rule("[dim]History cleared[/dim]", style="dim"))
        return True

    elif name == "/tools":
        lines = []
        for t in TOOLS:
            fn = t["function"]
            params = list(fn["parameters"]["properties"].keys())
            lines.append(f"[bold cyan]{fn['name']}[/bold cyan]({', '.join(params)})  —  {fn['description']}")
        console.print(Panel("\n".join(lines), title="Available Tools", border_style="cyan"))
        return True

    elif name == "/think":
        LEVELS = ("off", "on", "deep")
        if len(parts) > 1 and parts[1].lower() in LEVELS:
            session.think_level = parts[1].lower()
        else:
            # cycle: off → on → deep → off
            idx = LEVELS.index(session.think_level)
            session.think_level = LEVELS[(idx + 1) % len(LEVELS)]
        labels = {"off": "[dim]off — thinking disabled[/dim]",
                  "on":  "[cyan]on — normal thinking[/cyan]",
                  "deep": "[yellow]deep — thorough reasoning, temp 0.3[/yellow]"}
        console.print(f"Think level: {labels[session.think_level]}")
        _save_state(think_level=session.think_level)
        return True

    elif name == "/debug":
        import chat as _chat_mod
        if len(parts) > 1 and parts[1].lower() in ("off", "0", "false"):
            _chat_mod._debug_close()
            console.print("[dim]Debug stream capture: off[/dim]")
        elif _chat_mod._debug_file:
            console.print(f"[dim]Debug stream capture already active → {_chat_mod._debug_path}[/dim]")
            console.print("[dim]Use /debug off to stop.[/dim]")
        else:
            path_arg = parts[1] if len(parts) > 1 else "1"
            resolved = _chat_mod._debug_open(path_arg)
            console.print(f"[yellow]Debug stream capture: on → {resolved}[/yellow]")
        return True

    elif name == "/compact":
        await session._compact_history(manual=True)
        return True

    elif name == "/status":
        pct = session.tokens_used / session.ctx_window * 100 if session.ctx_window else 0
        bar_width = 30
        filled = int(bar_width * pct / 100)
        bar = "█" * filled + "░" * (bar_width - filled)
        bar_style = "yellow" if pct > 60 else "green"
        think_label = {"off": "off", "on": "on", "deep": "deep (temp 0.3)"}[session.think_level]
        console.print(Panel("\n".join([
            f"[bold]Context window:[/bold]  {session.ctx_window:,} tokens",
            f"[bold]Tokens used:[/bold]     {session.tokens_used:,}  (~{pct:.0f}%)",
            f"[bold]Usage bar:[/bold]       [{bar_style}]{bar}[/{bar_style}]",
            f"[bold]Messages:[/bold]        {len(session.messages) - session._n_fixed} (+ {session._n_fixed} fixed system)",
            f"[bold]Think level:[/bold]     {think_label}",
            f"[bold]Compact at:[/bold]      {int(session.ctx_window * CTX_COMPACT_THRESH):,} "
            f"tokens ({CTX_COMPACT_THRESH * 100:.0f}%)",
        ]), title="[cyan]Session Status[/cyan]", border_style="cyan"))
        return True

    elif name == "/save":
        path = parts[1] if len(parts) > 1 else f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(session.messages, f, indent=2, ensure_ascii=False)
            console.print(f"[green]Saved to {path}[/green]")
        except Exception as e:
            console.print(f"[red]Save failed: {e}[/red]")
        return True

    elif name == "/sessions":
        _all = [p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json"] if SESSIONS_DIR.exists() else []
        if not _all:
            console.print("[dim]No saved sessions.[/dim]")
            return True
        all_sessions = sorted(_all, reverse=True)
        lines = []
        for i, s in enumerate(all_sessions):
            try:
                data = json.loads(s.read_text(encoding="utf-8"))
                tok = data.get("token_estimate", 0)
                saved_at = data.get("saved_at", "")[:16].replace("T", " ")
                lines.append(f"[cyan]{s.stem}[/cyan]  [dim]{saved_at}  ~{tok:,} tokens[/dim]")
            except Exception:
                lines.append(f"[cyan]{s.stem}[/cyan]  [dim](unreadable)[/dim]")
        console.print(Panel("\n".join(lines), title="Saved Sessions", border_style="cyan"))
        return True

    elif name == "/resume":
        resume_name = parts[1] if len(parts) > 1 else None
        saved_msgs, sess_path = _load_session(resume_name)
        if not saved_msgs:
            hint = resume_name or "latest"
            console.print(f"[yellow]No session found matching '{hint}'[/yellow]")
            return True
        _initial = _build_initial_messages()
        session.messages = _initial + saved_msgs
        session._n_fixed = len(_initial)
        session._session_path = sess_path
        session.tokens_used = session.tokens_prompt = session.tokens_completion = 0
        console.print(Rule(f"[cyan]Session loaded: {sess_path.name}[/cyan]", style="cyan"))
        return True

    elif name == "/approval":
        VALID = ("auto", "ask-writes", "ask-all", "yolo")
        if len(parts) > 1 and parts[1].lower() in VALID:
            session.approval_level = parts[1].lower()
        labels = {
            "auto":       "[green]auto — installs and dangerous commands ask[/green]",
            "ask-writes": "[yellow]ask-writes — all writes and bash ask[/yellow]",
            "ask-all":    "[yellow]ask-all — every tool call asks[/yellow]",
            "yolo":       "[red]yolo — nothing asks (use with care)[/red]",
        }
        console.print(f"Approval: {labels[session.approval_level]}")
        if len(parts) <= 1:
            console.print(f"  Usage: /approval [{' | '.join(VALID)}]")
        else:
            _save_state(approval_level=session.approval_level)
        return True

    elif name == "/cd":
        if len(parts) < 2:
            console.print(f"[dim]Current directory: {session.cwd}[/dim]")
            return True
        new_path = Path(" ".join(parts[1:])).expanduser()
        if not new_path.is_absolute():
            new_path = session.cwd / new_path
        new_path = new_path.resolve()
        if not new_path.is_dir():
            console.print(f"[red]Not a directory: {new_path}[/red]")
            return True
        session.cwd = new_path
        console.print(f"[green]Working directory: {session.cwd}[/green]")
        await session._refresh_project_config()
        return True

    elif name == "/pwd":
        console.print(f"[dim]{session.cwd}[/dim]")
        return True

    elif name == "/skills":
        skills = _load_skills()
        if not skills:
            console.print("[dim]No skills found in skills/[/dim]")
            return True
        lines = []
        for sname, skill in sorted(skills.items()):
            tag = " [cyan][agent][/cyan]" if skill.get("spawn_agent") else ""
            desc = skill.get("description", "(no description)")
            raw_triggers = skill.get("triggers", [])
            if isinstance(raw_triggers, str):
                raw_triggers = [t.strip() for t in raw_triggers.split(",") if t.strip()]
            trigger_str = f"  [dim]· triggers: {', '.join(raw_triggers)}[/dim]" if raw_triggers else ""
            lines.append(f"[bold cyan]/{sname}[/bold cyan]{tag}  —  {desc}{trigger_str}")
        console.print(Panel("\n".join(lines), title="Skills", border_style="cyan"))
        return True

    elif name == "/skill":
        if len(parts) < 2:
            console.print("[yellow]Usage: /skill <name> [args][/yellow]")
            return True
        skill_name = parts[1].lower()
        skill_args = " ".join(parts[2:]) if len(parts) > 2 else ""
        found = await _invoke_skill(skill_name, skill_args, session)
        if not found:
            console.print(f"[yellow]Unknown skill: {skill_name} (try /skills)[/yellow]")
        return True

    elif name == "/model":
        # Fetch available profiles and currently loaded model from Server Manager
        profiles_data = await _control("GET", "/api/profiles")
        status_data   = await _control("GET", "/api/status")
        profiles: list[str] = profiles_data if isinstance(profiles_data, list) else list(_load_commands().keys())
        loaded: str | None  = status_data.get("model") if isinstance(status_data, dict) else None

        if len(parts) > 1:
            target = " ".join(parts[1:])
            # Accept unambiguous prefix matches
            matches = [p for p in profiles if p.lower().startswith(target.lower())]
            if not matches:
                matches = [p for p in profiles if target.lower() in p.lower()]
            if len(matches) == 1:
                target = matches[0]
            elif len(matches) > 1:
                console.print(f"[yellow]Ambiguous — did you mean:[/yellow]")
                for m in matches:
                    console.print(f"  {m}")
                return True
            elif target not in profiles:
                console.print(f"[yellow]Unknown profile: {target}[/yellow]")
                console.print(f"[dim]Available: {', '.join(profiles)}[/dim]")
                return True

            if target == loaded:
                console.print(f"[dim]{target} is already loaded.[/dim]")
                return True

            console.print(f"[cyan]Switching to {target}…[/cyan]")
            ok = await _switch_server(target)
            if ok:
                session.model = "auto"
                _save_state(model=session.model)
            return True

        # No argument — list all profiles
        lines = []
        for p in profiles:
            marker = "  [green]● loaded[/green]" if p == loaded else ""
            lines.append(f"[bold cyan]{p}[/bold cyan]{marker}")
        if not loaded:
            lines.append("\n[dim]Server Manager not reachable — profile list from commands.json[/dim]")
        lines.append(f"\n[dim]Usage: /model <name>   (prefix match supported)[/dim]")
        console.print(Panel("\n".join(lines), title="Models", border_style="cyan"))
        return True

    elif name == "/role":
        agents_dir = Path(__file__).parent / "agents"
        if len(parts) < 2:
            profiles = sorted(p.stem for p in agents_dir.glob("*.md")) if agents_dir.exists() else []
            if profiles:
                lines = ["[bold cyan]eli[/bold cyan]  [dim](default — revert to Eli)[/dim]"]
                lines += [f"[bold magenta]{p}[/bold magenta]" for p in profiles]
                lines.append("\n[dim]Usage: /role <name>  — adopt this persona in the current session[/dim]")
                console.print(Panel("\n".join(lines), title="Agent Profiles", border_style="magenta"))
            else:
                console.print("[dim]No agent profiles found in agents/[/dim]")
            return True
        role_name = parts[1].lower().replace("-", "_")

        # "eli" reverts to the base system prompt
        if role_name == "eli":
            session.messages.append({
                "role": "system",
                "content": (
                    "[Role Revert — Eli]\n\n"
                    "Discard any previous role overrides. You are Eli again, operating under "
                    "your original system instructions. Conversation context is preserved."
                ),
            })
            session.role = "eli"
            _save_state(role="eli")
            console.print(Panel(
                "Reverted to [bold cyan]Eli[/bold cyan].\n"
                "[dim]Conversation context preserved.[/dim]",
                border_style="cyan",
            ))
            return True

        agent_file = agents_dir / f"{role_name}.md"
        if not agent_file.exists():
            agent_file = agents_dir / f"{parts[1].lower()}.md"
        if not agent_file.exists():
            console.print(f"[yellow]No profile found: agents/{role_name}.md — try /role with no args to list[/yellow]")
            return True
        profile = agent_file.read_text(encoding="utf-8")
        session.messages.append({
            "role": "system",
            "content": (
                f"[Role Override — {role_name}]\n\n"
                f"The user has asked you to adopt the following agent persona for the remainder of "
                f"this conversation. Read and embody it fully. Your tools and capabilities remain "
                f"unchanged. Continue the current conversation context.\n\n{profile}"
            ),
        })
        session.role = role_name
        _save_state(role=role_name)
        console.print(Panel(
            f"Persona loaded: [bold magenta]{role_name}[/bold magenta]\n"
            f"[dim]Profile injected as system message. Conversation context preserved.[/dim]",
            border_style="magenta",
        ))
        return True

    elif name == "/config":
        config = session._project_config
        if not config:
            console.print("[dim]No eli.toml found in current directory tree.[/dim]")
        else:
            console.print(Panel(
                _format_project_config(config),
                title="[cyan]Project Config (eli.toml)[/cyan]",
                border_style="cyan",
            ))
        return True

    elif name == "/queue-results":
        if not SESSIONS_DIR.exists():
            console.print("[dim]No queue runs found.[/dim]")
            return True
        queue_dirs = sorted(
            [d for d in SESSIONS_DIR.iterdir() if d.is_dir() and d.name.startswith("queue_")],
            reverse=True,
        )
        if not queue_dirs:
            console.print("[dim]No queue runs found.[/dim]")
            return True
        label_filter = " ".join(parts[1:]).lower() if len(parts) > 1 else ""
        if label_filter:
            # Show full details for matching run
            matches = [d for d in queue_dirs if label_filter in d.name.lower()]
            if not matches:
                console.print(f"[yellow]No queue run matching '{label_filter}'[/yellow]")
                return True
            qdir = matches[0]
            results_file = qdir / "results.json"
            if not results_file.exists():
                console.print(f"[yellow]results.json missing in {qdir.name}[/yellow]")
                return True
            try:
                data = json.loads(results_file.read_text(encoding="utf-8"))
                results = data.get("results", [])
                label = data.get("label", "")
                total_dur = data.get("total_duration_seconds", 0)
                lines = []
                if label:
                    lines.append(f"[bold]Label:[/bold] {label}")
                lines.append(f"[bold]Agents:[/bold] {len(results)}   [bold]Total:[/bold] {total_dur:.0f}s")
                lines.append("")
                for r in results:
                    status_col = {"completed": "green", "timeout": "yellow", "error": "red"}.get(r.get("status", ""), "white")
                    lines.append(
                        f"[{status_col}]{r.get('status','?').upper()}[/{status_col}]  "
                        f"[bold]{r.get('index',0)+1}. {r.get('label', r.get('system_prompt',''))}[/bold]  "
                        f"[dim]{r.get('model','')[:40]}  {r.get('duration_seconds',0):.0f}s[/dim]"
                    )
                    result_text = (r.get("result") or "").strip()
                    if result_text:
                        # show first 400 chars
                        preview = result_text[:400] + ("…" if len(result_text) > 400 else "")
                        lines.append(f"  [dim]{preview}[/dim]")
                    lines.append("")
                console.print(Panel("\n".join(lines), title=f"[cyan]Queue: {qdir.name}[/cyan]", border_style="cyan"))
            except Exception as e:
                console.print(f"[red]Failed to read {results_file}: {e}[/red]")
        else:
            # List last 5 queue runs
            lines = []
            for qdir in queue_dirs[:5]:
                results_file = qdir / "results.json"
                try:
                    data = json.loads(results_file.read_text(encoding="utf-8"))
                    results = data.get("results", [])
                    label = data.get("label", "")
                    total_dur = data.get("total_duration_seconds", 0)
                    statuses = [r.get("status", "?") for r in results]
                    err_count = statuses.count("error")
                    timeout_count = statuses.count("timeout")
                    status_str = (
                        f"[red]{err_count} error{'s' if err_count!=1 else ''}[/red]  " if err_count else ""
                    ) + (
                        f"[yellow]{timeout_count} timeout{'s' if timeout_count!=1 else ''}[/yellow]  " if timeout_count else ""
                    ) + (
                        f"[green]{statuses.count('completed')} completed[/green]" if statuses.count("completed") else ""
                    )
                    label_str = f"  [dim]{label}[/dim]" if label else ""
                    lines.append(
                        f"[bold cyan]{qdir.name}[/bold cyan]{label_str}\n"
                        f"  {len(results)} agents  {total_dur:.0f}s  {status_str}"
                    )
                except Exception:
                    lines.append(f"[bold cyan]{qdir.name}[/bold cyan]  [dim](unreadable)[/dim]")
            lines.append("\n[dim]Usage: /queue-results <label>  — show full results for a run[/dim]")
            console.print(Panel("\n".join(lines), title="Recent Queue Runs", border_style="cyan"))
        return True

    elif name == "/voice":
        # Accept: /voice [ptt|auto] [tools]  (order of ptt/auto and tools is flexible)
        flags = [p.lower() for p in parts[1:]]
        voice_mode  = next((f for f in flags if f in ("ptt", "auto")), VOICE_DEFAULT_MODE)
        use_tools   = "tools" in flags
        await _voice_conversation_loop(session, mode=voice_mode, use_tools=use_tools)
        return True

    # Unknown /command — try skill lookup before giving up
    skill_name = name[1:]  # strip leading /
    skill_args = " ".join(parts[1:]) if len(parts) > 1 else ""
    found = await _invoke_skill(skill_name, skill_args, session)
    if found:
        return True
    console.print(f"[yellow]Unknown command: {name} (try /help or /skills)[/yellow]")
    return True

MODES = ["normal", "plan"]

PROMPT_STYLE = Style.from_dict({
    "normal": "ansibrightgreen bold",
    "plan-label": "ansibrightyellow bold",
    "plan-arrow": "ansiyellow bold",
    "deep-label": "ansiyellow bold",
    "deep-arrow": "ansiyellow bold",
    "bottom-toolbar": "noreverse bg:ansibrightblack fg:ansiwhite",
})

# ── main ──────────────────────────────────────────────────────────────────────
async def main():
    import argparse as _argparse
    parser = _argparse.ArgumentParser(description="Chat with Eli (Qwen3 local agent)", add_help=True)
    parser.add_argument("--resume", nargs="?", const="", metavar="NAME",
                        help="Resume last session, or named session (partial name match)")
    parser.add_argument("--continue", dest="continue_last", action="store_true",
                        help="Continue the last session with all previous settings restored")
    args = parser.parse_args()
    resume_name: str | None = args.resume if args.resume is not None else None
    do_resume    = args.resume is not None
    do_continue  = args.continue_last

    current_task: list[asyncio.Task | None] = [None]
    mode = ["normal"]   # mutable so closures can update it

    def get_prompt():
        if mode[0] == "plan":
            return [("class:plan-label", "plan "), ("class:plan-arrow", "❯ ")]
        if chat_ref[0] and chat_ref[0].think_level == "deep":
            return [("class:deep-label", "deep "), ("class:deep-arrow", "❯ ")]
        if chat_ref[0] and chat_ref[0].think_level == "off":
            return [("class:normal", "·❯ ")]
        return [("class:normal", "❯ ")]

    chat_ref: list = [None]  # holds the ChatSession once created

    def get_bottom_toolbar():
        chat = chat_ref[0]
        if not chat:
            return [("fg:ansiblue", " Eli"), ("", "  connecting...")]
        tokens = chat.tokens_used
        ctx = chat.ctx_window
        if not tokens or not ctx:
            extra = [("fg:ansicyan", "  [compact]")] if chat.compact_mode else []
            return [("fg:ansiblue", " ctx"), ("", "  no data yet")] + extra
        pct = tokens / ctx
        compact_pct = CTX_COMPACT_THRESH
        bar_width = 24
        filled = min(bar_width, int(bar_width * pct))
        bar = "█" * filled + "░" * (bar_width - filled)
        bar_color = "fg:ansibrightred" if pct >= compact_pct else "fg:ansiyellow" if pct > 0.6 else "fg:ansigreen"
        parts: list = [
            ("fg:ansiblue", " ctx "),
            (bar_color, bar),
            ("fg:ansiwhite", (
                f"  {tokens / 1000:.1f}k / {ctx / 1000:.0f}k"
                f"  ({pct * 100:.0f}%)"
                f"  compact@{int(compact_pct * 100)}%"
            )),
        ]
        parts.append(("fg:ansiblue", f"  {chat.model}"))
        if pct >= compact_pct - 0.05:
            parts.append(("fg:ansibrightyellow", "  ⚠ auto-compact soon"))
        if chat.compact_mode:
            parts.append(("fg:ansicyan", "  [compact]"))
        return parts

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _interrupt(event):
        task = current_task[0]
        if task and not task.done():
            task.cancel()
        else:
            event.app.exit(exception=KeyboardInterrupt())
        event.app.invalidate()

    @bindings.add("c-d")
    def _eof(event):
        event.app.exit(exception=EOFError())

    @bindings.add("s-tab")
    def _cycle_mode(event):
        idx = (MODES.index(mode[0]) + 1) % len(MODES)
        mode[0] = MODES[idx]
        event.app.invalidate()

    @bindings.add("c-o")
    def _toggle_compact(event):
        if chat_ref[0]:
            chat_ref[0].compact_mode = not chat_ref[0].compact_mode
            _save_state(compact_mode=chat_ref[0].compact_mode)
        event.app.invalidate()

    @bindings.add("enter")
    def _submit(event):
        """Submit on Enter (works in multiline mode)."""
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _newline(event):
        """Insert a real newline with Alt+Enter."""
        event.current_buffer.newline(copy_margin=False)

    prompt_session = PromptSession(
        history=FileHistory(".chat_history"),
        key_bindings=bindings,
        multiline=True,
        wrap_lines=True,
        style=PROMPT_STYLE,
        bottom_toolbar=get_bottom_toolbar,
    )

    async with ChatSession() as chat:
        chat_ref[0] = chat

        # ── --continue: restore all settings + resume last session ────────────
        if do_continue:
            state = _load_state()
            chat.think_level   = state.get("think_level",   "on")
            chat.compact_mode  = state.get("compact_mode",  False)
            chat.approval_level = state.get("approval_level", "auto")
            chat.model         = state.get("model",         MODEL)
            chat.role          = state.get("role",          "eli")
            last_name          = state.get("last_session")  # stem, e.g. "2025-01-01_12-00-00"
            saved_msgs, sess_path = _load_session(last_name)
            if saved_msgs:
                chat.messages.extend(saved_msgs)
                chat._session_path = sess_path
                role_note = f"  role: {chat.role}" if chat.role != "eli" else ""
                think_note = f"  think: {chat.think_level}"
                console.print(Rule(
                    f"[cyan]↩ Continuing: {sess_path.name}{role_note}{think_note}[/cyan]",
                    style="cyan",
                ))
            else:
                console.print("[dim]No previous session found — starting fresh.[/dim]")

        # ── --resume: load named (or latest) session only ─────────────────────
        elif do_resume:
            saved_msgs, sess_path = _load_session(resume_name)
            if saved_msgs:
                chat.messages.extend(saved_msgs)
                chat._session_path = sess_path
                console.print(Rule(f"[cyan]Session resumed: {sess_path.name}[/cyan]", style="cyan"))
            else:
                hint = resume_name or "latest"
                console.print(f"[yellow]No session found matching '{hint}'[/yellow]")

        while True:
            try:
                user_input = await prompt_session.prompt_async(get_prompt)
            except KeyboardInterrupt:
                console.print("\n[dim](interrupted — Ctrl+D to exit)[/dim]")
                continue
            except EOFError:
                console.print("\n[dim]Bye.[/dim]")
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.startswith("/"):
                try:
                    await handle_slash_command(user_input, chat)
                except asyncio.CancelledError:
                    console.print("[dim](interrupted)[/dim]")
                except httpx.ConnectError:
                    console.print("[red]LLM server not reachable. Is llama-server running?[/red]")
                except httpx.RemoteProtocolError:
                    console.print("[red]LLM server closed the connection unexpectedly.[/red]")
                except httpx.HTTPError as e:
                    console.print(f"[red]Server error: {e}[/red]")
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                continue  # handle_slash_command always handles the command (returns True)

            # Rule below the user's input, above the response
            console.print(Rule(style="dim"))

            was_plan = mode[0] == "plan"
            task = asyncio.create_task(
                chat.send_and_stream(user_input, plan_mode=was_plan)
            )
            current_task[0] = task
            try:
                await task
            except asyncio.CancelledError:
                if chat.messages and chat.messages[-1]["role"] == "user":
                    chat.messages.pop()
                console.print("[dim](interrupted)[/dim]")
            except httpx.HTTPError as e:
                console.print(f"[red]HTTP error: {e}[/red]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            finally:
                current_task[0] = None
                # Auto-reset plan mode after each response so the next message runs normally
                if was_plan:
                    mode[0] = "normal"


if __name__ == "__main__":
    asyncio.run(main())
