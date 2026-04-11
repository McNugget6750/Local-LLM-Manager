"""
tools.py — Tool schemas, standalone tool implementations, and approval helpers.

All functions here are pure and self-contained — no chat.py imports.
Imported by chat.py at module level and by agents.py.
"""
import asyncio
import fnmatch
import json
import os
import re as _re
import subprocess
from pathlib import Path

import httpx

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
            "name": "edit",
            "description": (
                "Replace an exact string in a file with new content. "
                "ALWAYS use this for modifying existing files — it is precise, safe, and uses minimal context. "
                "Read the file first to get the exact text, then pass it as old_string. "
                "Fails if old_string is not found or matches multiple times (make it more specific in that case). "
                "Returns a unified diff confirming the change — do NOT re-read the file to verify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":       {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to find and replace (must be unique in the file)"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, creating parent directories as needed. "
                "Only use this for NEW files or when replacing an entire file from scratch. "
                "NEVER use this to modify an existing file — use `edit` instead. "
                "After a successful write the result includes a content preview; do NOT re-read the file to verify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Full file content to write"},
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
                "Spawn a specialized sub-agent and return its result. "
                "Use for any agent task: code review, documentation, research, test writing. "
                "Agent profiles: code-review, doc-writer, generic, researcher, test-writer, web_designer. "
                "Do NOT specify a model unless the user explicitly requested a different one — "
                "only use profile names listed in the system context (commands.json). "
                "The server switches automatically and restores the original model when done. "
                "To run multiple agents concurrently, call spawn_agent multiple times in the "
                "same response — the system handles parallelisation automatically."
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
                "ONLY use this for strict ordered pipelines where agent B cannot start until "
                "agent A has finished and its output is passed forward (e.g. build → test → "
                "deploy). NEVER use for: a single agent task, research, code review, or any "
                "independent tasks. For those, always use spawn_agent instead — it runs in "
                "background mode and does not block the conversation. queue_agents is always "
                "synchronous and will block the conversation until all agents complete."
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


def _split_bash_commands(command: str) -> list[str]:
    """Split a shell command string into individual sub-commands on &&, ||, ;, |."""
    return [s.strip() for s in _re.split(r'&&|\|\||[;|]', command) if s.strip()]


def _extract_paths_from_subcmd(subcmd: str) -> list[str]:
    """Heuristically extract file path candidates from a single shell sub-command.

    Covers:
    - Positional args (tokens not starting with -)
    - --flag=value and -f value forms
    - Redirect targets: > file, >> file, 2> file
    """
    candidates = []
    tokens = subcmd.split()
    if not tokens:
        return candidates
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if i == 0:
            continue  # skip the command itself
        # redirect target
        if tok in (">", ">>", "2>", "2>>", "<"):
            if i + 1 < len(tokens):
                candidates.append(tokens[i + 1])
                skip_next = True
            continue
        if tok.startswith(">>") or tok.startswith(">") or tok.startswith("2>"):
            after = _re.sub(r'^2?>>?', '', tok)
            if after:
                candidates.append(after)
            continue
        # --flag=value
        if tok.startswith("--") and "=" in tok:
            candidates.append(tok.split("=", 1)[1])
            continue
        # -f value (single-char flag followed by path-like token)
        if _re.match(r'^-[a-zA-Z]$', tok) and i + 1 < len(tokens):
            candidates.append(tokens[i + 1])
            skip_next = True
            continue
        # skip other flags
        if tok.startswith("-"):
            continue
        candidates.append(tok)
    return candidates


def _analyze_bash_command(command: str, cwd: Path) -> list[tuple[str, str, bool]]:
    """Analyze a bash command for file paths outside cwd.

    Splits on shell operators, extracts path candidates from each sub-command,
    resolves them to absolute paths relative to cwd, and checks containment.

    Returns a list of (subcmd, resolved_abs_path, within_cwd) tuples — one entry
    per (subcmd, path) pair where the path could be resolved. Subcmds with no
    path candidates are omitted.
    """
    results = []
    cwd_resolved = cwd.resolve()
    subcmds = _split_bash_commands(command)
    for subcmd in subcmds:
        paths = _extract_paths_from_subcmd(subcmd)
        for raw in paths:
            # Strip quotes
            raw = raw.strip("'\"")
            if not raw or raw.startswith("$"):
                continue
            try:
                resolved = (cwd_resolved / raw).resolve()
                within = str(resolved).startswith(str(cwd_resolved))
                results.append((subcmd, str(resolved), within))
            except Exception:
                continue
    return results


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


def _matches_session_rule(name: str, args: dict, rules: list[str], cwd: Path | None = None) -> bool:
    """Return True if this tool call is covered by a session-level allow rule."""
    raw_path = args.get("path", "")
    cmd = args.get("command", "")

    # Resolve path to absolute so relative paths match prefix rules correctly.
    if raw_path and cwd:
        try:
            resolved_path = str((cwd / raw_path).resolve())
        except Exception:
            resolved_path = raw_path
    else:
        resolved_path = raw_path

    for rule in rules:
        if rule.startswith("path_prefix:"):
            prefix = rule[len("path_prefix:"):]
            check = resolved_path or raw_path
            if check and os.path.normcase(check).startswith(os.path.normcase(prefix)):
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
    cwd: Path | None = None,
) -> tuple[bool, str, str, str]:
    """Return (ask_needed, title, message, style) for a tool call approval check.

    `prefix` is prepended to titles (e.g. "Sub-Agent — ") to distinguish sub-agent prompts.
    `cwd` is used to resolve relative paths for session-rule matching and bash path analysis.
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
    elif name == "bash" and cmd and cwd:
        # Check for paths outside CWD in bash commands (handles &&-chained cmds and ../ paths)
        outside = [
            (subcmd, resolved)
            for subcmd, resolved, within in _analyze_bash_command(cmd, cwd)
            if not within
        ]
        if outside:
            ask_needed = True
            ask_title = f"{prefix}Command Targets Outside CWD"
            lines = [f"Command targets path(s) outside the current working directory:\n"]
            for subcmd, resolved in outside:
                lines.append(f"  Command : {subcmd}")
                lines.append(f"  Resolves: {resolved}\n")
            lines.append(f"CWD: {cwd}\n\nFull command:\n{cmd}")
            ask_msg = "\n".join(lines)
            ask_style = "yellow"
        elif approval_level == "auto" and _is_exec(cmd):
            ask_needed = True
            ask_title = f"{prefix}Script Execution"
            ask_msg = f"Script execution detected:\n\n{_fmt_tool_args(name, args)}"
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
    if ask_needed and ask_style != "red" and session_rules and _matches_session_rule(name, args, session_rules, cwd=cwd):
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
        lines = content.splitlines()
        preview = "\n".join(lines[:3])
        tail = f"\n  ... ({len(lines) - 3} more lines)" if len(lines) > 3 else ""
        return (
            f"Written {len(lines)} lines ({len(content)} chars) to {path}\n"
            f"Content preview:\n  {preview.replace(chr(10), chr(10) + '  ')}{tail}"
        )
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

