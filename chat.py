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
BASE_URL = "http://localhost:1234"
MODEL = "auto"

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

def _build_initial_messages() -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    memory = _load_memory()
    if memory:
        msgs.append({"role": "system", "content": f"[Operational Memory]\n\n{memory}"})
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
    all_sessions = sorted(SESSIONS_DIR.glob("*.json"))
    for old in all_sessions[:-MAX_SESSIONS]:
        try:
            old.unlink()
        except Exception:
            pass
    return session_path

def _load_session(name: str | None = None) -> tuple[list[dict], Path] | tuple[None, None]:
    if not SESSIONS_DIR.exists():
        return None, None
    all_sessions = sorted(SESSIONS_DIR.glob("*.json"))
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

# ── Compaction constants ──────────────────────────────────────────────────────
CTX_WINDOW           = 32_768   # fallback if /slots doesn't respond
CTX_COMPACT_THRESH   = 0.80     # trigger history compaction at this fraction
CTX_KEEP_RECENT      = 6        # tail messages kept verbatim after compact
INPUT_COMPRESS_CHARS = 8_000    # auto-compress user input above this char count
CHARS_PER_TOKEN      = 4        # fallback estimator when server usage unavailable

SESSIONS_DIR = Path(__file__).parent / "sessions"
MAX_SESSIONS = 10

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return combined stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read and return the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"},
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
                "Spawn a specialized sub-agent to perform a task in isolation. "
                "The agent has its own message history and tool-use loop. "
                "Results are returned as a string. Use this to delegate specialized "
                "work (code review, documentation, research, test writing) to a "
                "focused assistant. Profile names: code-review, doc-writer, "
                "researcher, test-writer."
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
                        "description": "Max tool-use iterations (default 10, hard max 10).",
                    },
                },
                "required": ["system_prompt", "task"],
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

# ── SSE stream parser ─────────────────────────────────────────────────────────
async def stream_events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
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

    async for line in response.aiter_lines():
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

# ── Tool executors ────────────────────────────────────────────────────────────
def _is_dangerous(command: str) -> bool:
    cmd_lower = command.lower()
    return any(pat in cmd_lower for pat in DANGEROUS_PATTERNS)

INSTALL_PATTERNS = [
    "pip install", "pip3 install", "python -m pip",
    "npm install", "npm i ", "yarn add", "yarn install",
    "conda install", "mamba install",
    "winget install", "choco install", "scoop install",
    "apt install", "apt-get install", "brew install",
]

# Bare Python/pip invocations that bypass the project venv.
# Matched against the first token(s) of the command.
BARE_PYTHON_PATTERNS = [
    "pip ", "pip\n", "pip3 ", "pip3\n",
    "python ", "python\n", "python3 ", "python3\n",
]

def _is_install(command: str) -> bool:
    import re
    cmd_lower = command.lower()
    if any(pat in cmd_lower for pat in INSTALL_PATTERNS):
        return True
    # Also catch venv pip.exe and pip3.exe forms: pip.exe install, pip3.exe install
    return bool(re.search(r'pip(?:3)?\.exe\s+install', cmd_lower))

def _is_bare_python(command: str) -> bool:
    """Detect bare python/pip calls that would hit system Python instead of the venv."""
    stripped = command.strip().lower()
    return any(stripped.startswith(pat) for pat in BARE_PYTHON_PATTERNS)

# Prefixes that are definitely read-only / safe — don't flag as script execution.
_EXEC_SAFE_PREFIXES = (
    "cat ", "type ", "grep ", "rg ", "find ", "ls ", "dir ", "echo ",
    "git ", "code ", "notepad", "cmake ", "ctest ", "make ", "ninja ",
    "python --version", "python3 --version", ".venv",
)

def _is_exec(command: str) -> bool:
    """Detect script/binary execution (.py/.js/.sh/.bat/.ps1/.exe)."""
    import re
    stripped = command.strip()
    lower = stripped.lower()
    if any(lower.startswith(ex) for ex in _EXEC_SAFE_PREFIXES):
        return False
    return bool(re.search(r'\b\S+\.(py|js|sh|bat|ps1|exe)\b', stripped, re.IGNORECASE))


async def tool_bash(command: str, timeout: int = 30, cwd: Path | None = None) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[timeout after {timeout}s]"
        output = stdout.decode(errors="replace")
        return output if output else "(no output)"
    except Exception as e:
        return f"[error: {e}]"


async def tool_read_file(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        if len(text) > 8000:
            text = text[:8000] + f"\n... [truncated, {len(text)} chars total]"
        return text
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
            return "[error: old_string not found in file]"
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
        tools = skill.get("agent_tools") or None
        think = skill.get("think_level") or None
        console.print(Panel(
            f"[dim]Invoking agent skill '[bold]{skill_name}[/bold]'...[/dim]",
            title="[cyan]Skill[/cyan]",
            border_style="cyan",
        ))
        result = await session._tool_spawn_agent(expanded, skill_args, tools, think)
        session.messages.append({"role": "assistant", "content": result})
        console.print(Panel(
            Markdown(result),
            title=f"[cyan]Skill Result — {skill_name}[/cyan]",
            border_style="cyan",
        ))
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
        self.compact_mode: bool     = False
        self._project_config: dict  = {}

    async def __aenter__(self):
        await self._health_check()
        await self._detect_ctx_window()
        await self._refresh_project_config()
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
        """Inject current working directory as a system message so Eli always knows where it is."""
        self._remove_cwd_context()
        content = f"[Session Context]\nCurrent working directory: {self.cwd}\nAll relative file paths resolve against this directory."
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
                        "You are a conversation summarizer. Produce a dense, complete "
                        "summary as a bullet list. Preserve: technical decisions, file "
                        "names, code identifiers, error messages, numeric values, "
                        "commands run, and conclusions reached."
                    )},
                    {"role": "user", "content": f"Summarize this conversation:\n\n{serialised}"},
                ],
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 1024,
            })
            r.raise_for_status()
            summary = r.json()["choices"][0]["message"]["content"].strip()
            if not summary:
                raise ValueError("empty summary")

            self.messages = [
                *self.messages[:self._n_fixed],
                {"role": "system", "content": f"[Conversation summary — earlier messages compacted]\n\n{summary}"},
                *self.messages[-CTX_KEEP_RECENT:],
            ]
            self.tokens_used = 0
            console.print(Rule(
                f"[yellow]Context compacted[/yellow] [dim]({orig_count} → {len(self.messages)} messages)[/dim]",
                style="yellow",
            ))
        except Exception as e:
            console.print(f"[yellow]Compaction failed — history unchanged[/yellow] [dim]({e})[/dim]")
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

                # Render text live via rich.live
                with Live(console=console, refresh_per_second=8) as live:
                    thinking_started = False
                    text_started = False

                    show_thinking = self.think_level != "off" and not self.compact_mode
                    think_title = "[dim]Thinking (deep)...[/dim]" if self.think_level == "deep" else "[dim]Thinking...[/dim]"
                    think_border = "blue" if self.think_level == "deep" else "dim"

                    async for event_type, data in stream_events(response):
                        if event_type == "think":
                            if show_thinking:
                                thinking_buf += data
                                live.update(
                                    Panel(
                                        Text(thinking_buf, style="dim italic"),
                                        title=think_title,
                                        border_style=think_border,
                                    )
                                )

                        elif event_type == "text":
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
                            live.update(Text(""))

                        elif event_type == "usage":
                            usage_data = data

                        elif event_type == "stop":
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

            # Append assistant message
            if tool_calls_received:
                self.messages.append({
                    "role": "assistant",
                    "content": assistant_content or None,
                    "tool_calls": tool_calls_received,
                })
                # Execute tool calls in parallel
                async def _run_one(tc):
                    return await self._call_tool(
                        tc["function"]["name"],
                        tc["function"]["arguments"],
                        tc["id"],
                    )
                results = await asyncio.gather(*[_run_one(tc) for tc in tool_calls_received])
                for tc, result in zip(tool_calls_received, results):
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                # Loop: send tool results back to model
                continue
            else:
                if assistant_content:
                    self.messages.append({"role": "assistant", "content": assistant_content})
                break

        if self.tokens_used:
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

    async def _dispatch_tool(self, name: str, args: dict) -> str:
        """Pure tool dispatch — no display, no approval check."""
        try:
            if name == "bash":
                return await tool_bash(args.get("command", ""), args.get("timeout", 30), cwd=self.cwd)
            elif name == "read_file":
                return await tool_read_file(self._resolve_path(args.get("path", "")))
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
                    min(args.get("max_iterations", 10), 10),
                )
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
    ) -> str:
        """Run an isolated sub-agent loop and return its final text response."""
        if self._in_subagent:
            return "[error: nested sub-agent spawning is not allowed]"

        # Resolve profile name → system prompt
        if system_prompt and " " not in system_prompt.strip():
            profile_path = Path(__file__).parent / "agents" / f"{system_prompt}.md"
            if profile_path.exists():
                system_prompt = profile_path.read_text(encoding="utf-8")
            # If not found, use the string as-is (may be a short raw prompt)

        # Build tool list — always exclude spawn_agent from sub-agents
        sub_tools = [t for t in TOOLS if t["function"]["name"] != "spawn_agent"]
        if tools:
            sub_tools = [t for t in sub_tools if t["function"]["name"] in tools]

        think = think_level or self.think_level
        max_iter = min(max_iterations, 10)

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        if self.compact_mode:
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
                    _live_ctx = _NullLive() if self.compact_mode else Live(console=console, refresh_per_second=8)
                    with _live_ctx as live:
                        show_thinking = think != "off" and not self.compact_mode

                        async for event_type, data in stream_events(response):
                            if event_type == "think":
                                if show_thinking:
                                    thinking_buf += data
                                    live.update(Panel(
                                        Text(thinking_buf, style="dim italic"),
                                        title="[dim cyan]Agent Thinking...[/dim cyan]",
                                        border_style="dim cyan",
                                    ))
                            elif event_type == "text":
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
                                live.update(Panel(
                                    Markdown(text_buf),
                                    title="[cyan]Agent[/cyan]",
                                    border_style="cyan",
                                ))
                            elif event_type == "tool_calls":
                                tool_calls_received = data
                                live.update(Text(""))
                            elif event_type == "stop":
                                if text_buf:
                                    live.update(Panel(
                                        Markdown(text_buf),
                                        title="[cyan]Agent[/cyan]",
                                        border_style="cyan",
                                    ))
                                else:
                                    live.update(Text(""))

                final_text = assistant_content

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
                        except json.JSONDecodeError:
                            tc_args = {}
                        if self.compact_mode:
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
                                console.print(Panel(
                                    f"[red]Bare python/pip call blocked.[/red] Sub-agents must use the project venv.\n"
                                    f"[dim]{cmd}[/dim]",
                                    title="[red]Venv Rule Violation[/red]",
                                    border_style="red",
                                ))
                                tc_result = "[blocked: bare python/pip — must use .venv\\Scripts\\pip.exe or .venv\\Scripts\\python.exe]"
                                console.print(Panel(tc_result, title="[dim cyan]Agent Tool Result[/dim cyan]", border_style="red"))
                                return tc["id"], tc_result

                        # Apply same approval rules as top-level _call_tool
                        if self.approval_level != "yolo" and tc_name == "bash":
                            cmd = tc_args.get("command", "")
                            ask_needed = False
                            ask_title = "Sub-Agent Approval Required"
                            ask_msg = ""
                            ask_style = "yellow"
                            if _is_dangerous(cmd):
                                ask_needed = True
                                ask_title = "Sub-Agent — Dangerous Command"
                                ask_msg = f"[red]Dangerous command from sub-agent![/red]\n[dim]{cmd}[/dim]"
                                ask_style = "red"
                            elif _is_install(cmd):
                                ask_needed = True
                                ask_title = "Sub-Agent — Install Guard"
                                ask_msg = (
                                    f"[yellow]Sub-agent wants to install a package.[/yellow]\n[dim]{cmd}[/dim]\n"
                                    "Run it? Or install yourself and press Enter when ready."
                                )
                                ask_style = "yellow"
                            elif self.approval_level == "auto" and _is_exec(cmd):
                                ask_needed = True
                                ask_title = "Sub-Agent — Script Execution"
                                ask_msg = f"[yellow]Sub-agent script execution detected.[/yellow]\n[dim]{cmd}[/dim]"
                                ask_style = "yellow"
                            elif self.approval_level == "ask-all":
                                ask_needed = True
                                ask_msg = "[yellow]Sub-agent bash command — approve?[/yellow]"
                            elif self.approval_level == "ask-writes":
                                ask_needed = True
                                ask_msg = "[yellow]Sub-agent bash command — approve?[/yellow]"
                            if ask_needed:
                                console.print(Panel(
                                    ask_msg,
                                    title=f"[{ask_style}]{ask_title}[/{ask_style}]",
                                    border_style=ask_style,
                                ))
                                try:
                                    confirm = await asyncio.get_event_loop().run_in_executor(
                                        None, lambda: input("Run it? [y/N] ")
                                    )
                                except (EOFError, KeyboardInterrupt):
                                    confirm = "n"
                                if confirm.strip().lower() != "y":
                                    tc_result = "[cancelled by user]"
                                    console.print(Panel(
                                        tc_result,
                                        title="[dim cyan]Agent Tool Result[/dim cyan]",
                                        border_style="cyan",
                                    ))
                                    return tc["id"], tc_result
                        tc_result = await self._dispatch_tool(tc_name, tc_args)
                        if self.compact_mode:
                            console.print(f"[dim]      → {markup_escape(self._compact_result(tc_result))}[/dim]")
                        else:
                            border = "cyan" if not tc_result.startswith("[error") and not tc_result.startswith("[unknown") and not tc_result.startswith("[blocked") else "red"
                            console.print(Panel(
                                markup_escape(tc_result[:2000]) + ("..." if len(tc_result) > 2000 else ""),
                                title="[dim cyan]Agent Tool Result[/dim cyan]",
                                border_style=border,
                            ))
                        return tc["id"], tc_result

                    results = await asyncio.gather(*[_run_agent_tool(tc) for tc in tool_calls_received])
                    for tc_id, tc_result_val in results:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": tc_result_val,
                        })
                else:
                    if assistant_content:
                        messages.append({"role": "assistant", "content": assistant_content})
                    break
        finally:
            self._in_subagent = False

        if self.compact_mode and final_text:
            first_line = final_text.split("\n")[0][:120]
            console.print(f"[dim cyan]  ✓ {first_line}[/dim cyan]")
        return final_text or "[sub-agent returned no text]"

    async def _call_tool(self, name: str, arguments_str: str, call_id: str) -> str:
        try:
            args = json.loads(arguments_str) if arguments_str.strip() else {}
        except json.JSONDecodeError:
            args = {}

        # Display tool call
        if self.compact_mode:
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
                    f"[red]Bare python/pip call blocked.[/red] Use the project venv instead:\n"
                    f"[dim]{cmd}[/dim]\n\n"
                    "[yellow]Example:[/yellow] [bold].venv\\Scripts\\pip.exe install package[/bold]",
                    title="[red]Venv Rule Violation[/red]",
                    border_style="red",
                ))
                return "[blocked: bare python/pip call — must use project venv (.venv\\Scripts\\pip.exe or .venv\\Scripts\\python.exe)]"

        # Approval guard
        if self.approval_level != "yolo":
            cmd = args.get("command", "") if name == "bash" else ""
            ask_needed = False
            ask_title = "Approval Required"
            ask_msg = ""
            ask_style = "yellow"
            if name == "bash" and _is_dangerous(cmd):
                ask_needed = True
                ask_title = "Warning — Dangerous Command"
                ask_msg = f"[red]Dangerous command detected![/red]\n[dim]{cmd}[/dim]"
                ask_style = "red"
            elif name == "bash" and _is_install(cmd):
                ask_needed = True
                ask_title = "Install Guard"
                ask_msg = (
                    f"[yellow]Package install detected.[/yellow]\n[dim]{cmd}[/dim]\n"
                    "Run it? Or install yourself and press Enter when ready."
                )
                ask_style = "yellow"
            elif name == "bash" and self.approval_level == "auto" and _is_exec(cmd):
                ask_needed = True
                ask_title = "Script Execution"
                ask_msg = f"[yellow]Script execution detected.[/yellow]\n[dim]{cmd}[/dim]"
                ask_style = "yellow"
            elif self.approval_level == "ask-all":
                ask_needed = True
                ask_msg = f"[yellow]Approve tool call?[/yellow]"
            elif self.approval_level == "ask-writes":
                WRITE_TOOLS = {"bash", "write_file", "edit"}
                if name in WRITE_TOOLS or (name == "task_list" and args.get("operation") != "read"):
                    ask_needed = True
                    ask_msg = "[yellow]Write operation — approve?[/yellow]"
            if ask_needed:
                console.print(Panel(
                    ask_msg,
                    title=f"[{ask_style}]{ask_title}[/{ask_style}]",
                    border_style=ask_style,
                ))
                try:
                    confirm = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: input("Run it? [y/N] ")
                    )
                except (EOFError, KeyboardInterrupt):
                    confirm = "n"
                if confirm.strip().lower() != "y":
                    return "[cancelled by user]"

        # Dispatch
        result = await self._dispatch_tool(name, args)

        # Post-edit hook — run build/test after file edits
        if name in ("edit", "write_file") and not result.startswith("[error"):
            hook_out = await self._run_post_edit_hook(args.get("path", ""))
            if hook_out:
                result += f"\n\n{hook_out}"

        if self.compact_mode:
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
                    "[bold]/status[/bold]                Show token usage and context window info",
                    "[bold]/sessions[/bold]              List saved sessions",
                    "[bold]/resume \\[name\\][/bold]         Load a saved session (replaces current)",
                    "[bold]/approval \\[mode\\][/bold]       Set approval tier: auto|ask-writes|ask-all|yolo",
                    "[bold]/cd \\[path\\][/bold]             Set working directory for bash commands",
                    "[bold]/pwd[/bold]                   Show current working directory",
                    "[bold]/model \\[id\\][/bold]             Switch model or list available models",
                    "[bold]/config[/bold]                Show loaded eli.toml project config",
                    "[bold]/skills[/bold]                List available skills",
                    "[bold]/skill <name> \\[args\\][/bold]   Invoke a skill explicitly",
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
        if not SESSIONS_DIR.exists() or not list(SESSIONS_DIR.glob("*.json")):
            console.print("[dim]No saved sessions.[/dim]")
            return True
        all_sessions = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
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
            lines.append(f"[bold cyan]/{sname}[/bold cyan]{tag}  —  {desc}")
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
        if len(parts) > 1:
            session.model = parts[1]
            console.print(f"[green]Model: {session.model}[/green]")
            return True
        try:
            r = await session.client.get(f"{BASE_URL}/v1/models", timeout=5)
            models = r.json().get("data", [])
            lines = [
                f"[bold cyan]{m['id']}[/bold cyan]"
                + ("  ← current" if m['id'] == session.model else "")
                for m in models
            ]
            lines.append(f"\n[dim]Usage: /model <id>[/dim]")
            console.print(Panel("\n".join(lines), title="Models", border_style="cyan"))
        except Exception as e:
            console.print(f"[dim]Current: {session.model}  ({e})[/dim]")
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

    # Unknown /command — try skill lookup before giving up
    skill_name = name[1:]  # strip leading /
    skill_args = " ".join(parts[1:]) if len(parts) > 1 else ""
    found = await _invoke_skill(skill_name, skill_args, session)
    if found:
        return True
    console.print(f"[yellow]Unknown command: {name} (try /help or /skills)[/yellow]")
    return True

# ── Modes ─────────────────────────────────────────────────────────────────────
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
    args = parser.parse_args()
    resume_name: str | None = args.resume if args.resume is not None else None
    do_resume = args.resume is not None

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
        if do_resume:
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
                await handle_slash_command(user_input, chat)
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
