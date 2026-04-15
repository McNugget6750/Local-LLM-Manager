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
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# ── Constants & shared resources ─────────────────────────────────────────────
from constants import BASE_URL, CONTROL_URL, TTS_URL, console

# ── LaTeX math → Unicode for TUI display ─────────────────────────────────────
_LATEX_SYMBOLS = [
    (r"\rightarrow",  "→"), (r"\leftarrow",   "←"),
    (r"\Rightarrow",  "⇒"), (r"\Leftarrow",   "⇐"),
    (r"\implies",        "⟹"), (r"\iff",            "⟺"),
    (r"\leftrightarrow", "↔"), (r"\Leftrightarrow", "⇔"),
    (r"\uparrow",     "↑"), (r"\downarrow",   "↓"),
    (r"\times",       "×"), (r"\div",         "÷"),
    (r"\pm",          "±"), (r"\mp",          "∓"),
    (r"\leq",         "≤"), (r"\geq",         "≥"),
    (r"\ll",          "≪"), (r"\gg",          "≫"),
    (r"\neq",         "≠"), (r"\approx",      "≈"),
    (r"\sim",         "~"), (r"\simeq",       "≃"),
    (r"\equiv",       "≡"), (r"\propto",      "∝"),
    (r"\infty",       "∞"), (r"\cdot",        "·"),
    (r"\ldots",       "…"), (r"\dots",        "…"),
    (r"\in",          "∈"), (r"\notin",       "∉"),
    (r"\subset",      "⊂"), (r"\supset",      "⊃"),
    (r"\cup",         "∪"), (r"\cap",         "∩"),
    (r"\sqrt",        "√"), (r"\sum",         "∑"),
    (r"\prod",        "∏"), (r"\int",         "∫"),
    (r"\partial",     "∂"), (r"\nabla",       "∇"),
    (r"\forall",      "∀"), (r"\exists",      "∃"),
    (r"\neg",         "¬"), (r"\land",        "∧"),
    (r"\lor",         "∨"), (r"\oplus",       "⊕"),
    (r"\alpha",  "α"), (r"\beta",  "β"), (r"\gamma", "γ"),
    (r"\delta",  "δ"), (r"\epsilon","ε"), (r"\zeta",  "ζ"),
    (r"\eta",    "η"), (r"\theta", "θ"), (r"\iota",  "ι"),
    (r"\kappa",  "κ"), (r"\lambda","λ"), (r"\mu",    "μ"),
    (r"\nu",     "ν"), (r"\xi",    "ξ"), (r"\pi",    "π"),
    (r"\rho",    "ρ"), (r"\sigma", "σ"), (r"\tau",   "τ"),
    (r"\upsilon","υ"), (r"\phi",   "φ"), (r"\chi",   "χ"),
    (r"\psi",    "ψ"), (r"\omega", "ω"),
    (r"\Gamma",  "Γ"), (r"\Delta", "Δ"), (r"\Theta", "Θ"),
    (r"\Lambda", "Λ"), (r"\Pi",    "Π"), (r"\Sigma", "Σ"),
    (r"\Phi",    "Φ"), (r"\Psi",   "Ψ"), (r"\Omega", "Ω"),
]
import re as _re_latex
_LATEX_INLINE_RE = _re_latex.compile(r'\$([^$\n]+?)\$')

def _render_latex(text: str) -> str:
    """Replace inline LaTeX math ($...$) with Unicode equivalents for TUI display."""
    def _sub(m):
        expr = m.group(1)
        for latex, uni in _LATEX_SYMBOLS:
            expr = expr.replace(latex, uni)
        # Strip any remaining backslash commands we don't know
        expr = _re_latex.sub(r'\\[a-zA-Z]+', '', expr).strip()
        return expr if expr else m.group(0)
    return _LATEX_INLINE_RE.sub(_sub, text)
MODEL = "auto"

# ── Agents, slot manager, and server control ─────────────────────────────────
from agents import (
    AgentsMixin, _ism,
    _control, _find_active_profile, _extract_write_path, _switch_server,
)

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

from profiles import (
    _vision_url, _load_system_prompt, _load_memory, _load_commands,
    _load_commands_meta, _build_model_context, _load_agent_profile,
    _can_run_parallel, _all_can_parallel, _load_project_config,
    _format_project_config, SYSTEM_PROMPT,
    _load_behavioral_pulse, _PULSE_PREFIX,
)


def _build_initial_messages() -> tuple[list[dict], list[str]]:
    """Return (messages, full_paths_loaded)."""
    msgs = [{"role": "system", "content": _load_system_prompt()}]
    eli_path = str((Path(__file__).parent / "ELI.md").resolve())
    loaded_paths: list[str] = [eli_path]

    memory = _load_memory()
    if memory:
        msgs.append({"role": "system", "content": f"[Operational Memory]\n\n{memory}"})
        loaded_paths.append(str((Path(__file__).parent / "MEMORY.md").resolve()))

    model_ctx = _build_model_context()
    if model_ctx:
        msgs.append({"role": "system", "content": model_ctx})

    console.print(f"[dim]Context loaded: {', '.join(loaded_paths)}[/dim]")
    return msgs, loaded_paths

# ── Session persistence ───────────────────────────────────────────────────────
def _session_token_estimate(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages) // CHARS_PER_TOKEN

def _save_session(messages: list[dict], n_fixed: int, session_path: Path | None = None, cwd: Path | None = None) -> Path:
    SESSIONS_DIR.mkdir(exist_ok=True)
    if session_path is None:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        session_path = SESSIONS_DIR / f"{ts}.json"
    conversation = messages[n_fixed:]
    data = {
        "saved_at": datetime.now().isoformat(),
        "token_estimate": _session_token_estimate(conversation),
        "cwd": str(cwd) if cwd else None,
        "messages": conversation,
    }
    session_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    all_sessions = sorted(p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json")
    for old in all_sessions[:-MAX_SESSIONS]:
        try:
            old.unlink()
        except Exception:
            pass
        try:
            html_sibling = old.with_suffix(".html")
            if html_sibling.exists():
                html_sibling.unlink()
        except Exception:
            pass
    _save_state(last_session=session_path.stem)
    return session_path

def _load_session(name: str | None = None) -> tuple[list[dict], Path, str | None] | tuple[None, None, None]:
    if not SESSIONS_DIR.exists():
        return None, None, None
    all_sessions = sorted(p for p in SESSIONS_DIR.glob("*.json") if p.name != "state.json")
    if not all_sessions:
        return None, None, None
    if name:
        candidates = [s for s in all_sessions if name in s.stem]
        if not candidates:
            return None, None, None
        target = candidates[-1]
    else:
        # Prefer last-used session from state; fall back to newest file
        state = _load_state()
        last = state.get("last_session")
        if last:
            candidates = [s for s in all_sessions if s.stem == last]
            target = candidates[0] if candidates else all_sessions[-1]
        else:
            target = all_sessions[-1]
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data.get("messages", []), target, data.get("cwd")
    except Exception:
        return None, None, None

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
CTX_COMPACT_THRESH   = 0.70     # trigger history compaction at this fraction
CTX_KEEP_RECENT      = 6        # tail messages kept verbatim after compact
INPUT_COMPRESS_CHARS = 8_000    # auto-compress user input above this char count
CHARS_PER_TOKEN      = 4        # fallback estimator when server usage unavailable

SESSIONS_DIR = Path(__file__).parent / "sessions"
STATE_FILE   = SESSIONS_DIR / "state.json"
MAX_SESSIONS = 10

from unicode_normalize import normalize_tool_args
from sanity_detector import SanityDetector, SanityError

_ELI_MAX_SANITY_RETRIES = 1   # Eli gets 1 retry (2 total attempts) per user turn

class ContextWindowError(RuntimeError):
    """Raised when the server rejects the prompt for exceeding context window."""

from tools import (
    TOOLS, DANGEROUS_PATTERNS, _GATE_REJECTED_PREFIX,
    _is_dangerous, _is_install, _is_bare_python, _is_exec,
    _fmt_tool_args, _matches_session_rule, _build_approval_check,
    _menu_select, _new_project_path,
    tool_bash, tool_read_file, tool_write_file, tool_list_dir,
    tool_glob, tool_grep, tool_ripgrep, tool_edit,
    _tts_preprocess, tool_speak, tool_web_fetch, tool_web_search,
    tool_task_list,
)

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


async def _post_timing(data: dict) -> None:
    """Fire-and-forget: POST timing stats to the server manager control API."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as _c:
            await _c.post(f"{CONTROL_URL}/api/timing", json=data)
    except Exception:
        pass  # server manager not running — silently ignore


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
    # Holdback buffer for think-tag detection across chunk boundaries.
    # Supports Qwen3 (<think>/</think>) and Gemma 4 (<|channel>/<channel|>).
    holdback = ""
    _OPEN_TAGS  = ["<think>", "<|channel>"]
    _CLOSE_TAGS = {"<think>": "</think>", "<|channel>": "<channel|>"}
    active_close = "</think>"
    MAX_HOLD = max(len(t) for t in _OPEN_TAGS + list(_CLOSE_TAGS.values()))

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

        # Handle text content with think-tag state machine (Qwen3 + Gemma 4)
        content = delta.get("content") or ""
        if content:
            holdback += content
            # Process holdback: emit safe prefix, keep potential-tag suffix
            while True:
                if in_think:
                    pos = holdback.find(active_close)
                    if pos != -1:
                        if pos > 0:
                            yield ("think", holdback[:pos])
                        holdback = holdback[pos + len(active_close):]
                        in_think = False
                    else:
                        safe = holdback[:-MAX_HOLD] if len(holdback) > MAX_HOLD else ""
                        if safe:
                            yield ("think", safe)
                        holdback = holdback[len(safe):]
                        break
                else:
                    # Find whichever open tag appears earliest
                    best_pos, best_tag = -1, ""
                    for ot in _OPEN_TAGS:
                        p = holdback.find(ot)
                        if p != -1 and (best_pos == -1 or p < best_pos):
                            best_pos, best_tag = p, ot
                    if best_pos != -1:
                        if best_pos > 0:
                            yield ("text", holdback[:best_pos])
                        holdback = holdback[best_pos + len(best_tag):]
                        active_close = _CLOSE_TAGS[best_tag]
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


def _check_skill_triggers(user_input: str) -> tuple[str | None, str]:
    """Check if any skill triggers are present in user input."""
    skills = _load_skills()
    for name, skill in skills.items():
        triggers = skill.get("triggers", [])
        if isinstance(triggers, str):
            triggers = [triggers]
        for trigger in triggers:
            if trigger and trigger in user_input:
                return name, user_input
    return None, ""


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
        max_iter   = int(skill.get("max_iterations", 60))
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
        # Inject the agent report directly into message history — no model round-trip.
        # The report lands as a user→assistant exchange so message sequence stays valid
        # and Eli can reference it in subsequent turns without an "acknowledge receipt" waste.
        report_text = f"[Agent Report — '{skill_name}']\n\n{result}"
        session.messages.append({"role": "user", "content": report_text})
        session.messages.append({"role": "assistant", "content": "Understood. The agent report is in context."})
    else:
        if not session.tui_queue:
            console.print(f"[dim]Loading skill '[bold]{skill_name}[/bold]'...[/dim]")
        try:
            await session.send_and_stream(expanded)
        except Exception as e:
            console.print(f"[red]Could not send skill to model: {e}[/red]")
            if session.tui_queue:
                await session.tui_queue.put({"type": "error", "text": f"Server not reachable: {e}"})

    return True


# ── ChatSession ───────────────────────────────────────────────────────────────
class ChatSession(AgentsMixin):
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=120.0)
        _initial, _startup_paths = _build_initial_messages()
        self.messages: list[dict] = _initial
        self._n_fixed: int          = len(_initial)
        self._startup_files: list[str] = _startup_paths
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
        self._subagent_depth: int   = 0    # nesting depth; >0 blocks nested spawn
        self.server_parallel_slots: int = 1  # filled by _detect_ctx_window
        self._capabilities_injected: bool = False
        self.compact_mode: bool         = False
        self.compact_threshold: float   = CTX_COMPACT_THRESH
        self.keep_recent: int           = CTX_KEEP_RECENT
        self.input_compress_limit: int  = INPUT_COMPRESS_CHARS
        self.role: str              = "eli"  # active role name
        self._project_config: dict  = {}
        self._approval_notes: str   = ""  # injected into tool result after dispatch
        self.session_rules: list[str] = []  # persistent allow-rules for this session
        self.tui_queue: asyncio.Queue | None = None  # set by TUI to receive typed events
        # Background agent support
        self._bg_agent_tasks:       list[asyncio.Task] = []
        self._pending_bg_results:   list[tuple[str, str]] = []   # (tool_call_id, result_text)
        self._pending_bg_tool_calls: list[dict] = []             # tc dicts for tool_done emit
        # Background process support
        self._bg_process_tasks:     list[asyncio.Task] = []
        self._turn_active:          bool = False
        self._auto_trigger:         asyncio.Event = asyncio.Event()
        self._auto_trigger_msg:     str = ""
        self._auto_turn_count:      int = 0
        self._auto_turn_limit:      int = 5
        self._write_locks:          dict[str, str] = {}          # abs_path → holder label
        self._last_read:            set[str]       = set()       # abs paths freshly read; cleared after write/edit
        self._eli_slot:             "SlotHandle | None" = None   # held during send_and_stream; released before inline agents
        self.backend: str           = "llamacpp"                 # "llamacpp" | "vllm" — set by _health_check
        self._sanity_retry_count: int = 0                        # resets on each new user message
        self._sanity_detector: SanityDetector = SanityDetector()
        self._telegram_origin: bool = False                      # set when message arrived via Telegram bridge

    async def __aenter__(self):
        await self._health_check()
        await self._detect_ctx_window()
        self._inject_capabilities()
        await self._refresh_project_config()
        if not self.tui_queue:
            console.print(
                Panel(
                    "[bold cyan]Open Harness TUI[/bold cyan]  —  connected to [green]localhost:1234[/green]\n"
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
            data = r.json()
            if data.get("data"):
                first_model = data["data"][0]
                if self.model == "auto":
                    self.model = first_model["id"]
                # vLLM identifies itself via owned_by field or by full-path model IDs
                owned_by = first_model.get("owned_by", "")
                model_id = first_model.get("id", "")
                if owned_by == "vllm" or "/" in model_id or "\\" in model_id:
                    self.backend = "vllm"
                    # vLLM exposes max_model_len directly — use it as ctx_window
                    max_len = first_model.get("max_model_len")
                    if max_len and isinstance(max_len, int) and max_len > 0:
                        self.ctx_window = max_len
                    console.print(f"[dim]Backend: vLLM  model={self.model}  ctx={self.ctx_window:,}[/dim]")
                else:
                    console.print(f"[dim]Backend: llama.cpp  model={self.model}[/dim]")
        except Exception as e:
            console.print(f"[red]Server not reachable at {BASE_URL}: {e}[/red]")
            console.print("[yellow]Start the server first via server_manager.py[/yellow]")
            sys.exit(1)

    async def _detect_ctx_window(self) -> None:
        await _ism.initialize()
        self.server_parallel_slots = _ism.total_slots()
        raw = _ism._raw_slots
        if raw:
            n_ctx = raw[0].get("n_ctx")
            if n_ctx and isinstance(n_ctx, int) and n_ctx > 0:
                self.ctx_window = n_ctx
                slots_note = f"  ·  {self.server_parallel_slots} parallel slot(s)" if self.server_parallel_slots > 1 else ""
                console.print(f"[dim]Context window: {n_ctx:,} tokens{slots_note}[/dim]")
                return
        console.print(f"[dim]Context window: {CTX_WINDOW:,} tokens (default — /slots unavailable)[/dim]")

    def _inject_capabilities(self) -> None:
        """Inject a one-time system message advertising parallel agent execution."""
        if self._capabilities_injected:
            return
        msg = (
            "[Agent capabilities]\n"
            "You may issue multiple spawn_agent tool calls in a single response to run agents "
            "concurrently. The system automatically groups agents by model, checks domain-level "
            "resource conflicts, and queries live server capacity — running agents in parallel "
            "when safe, falling back to sequential when not. You do not need to reason about "
            "this.\n\n"
            "IMPORTANT: Use multiple spawn_agent calls (not queue_agents) when you want agents "
            "to run in parallel. queue_agents is always sequential. Prefer multiple spawn_agent "
            "calls for independent tasks such as researching different topics simultaneously, "
            "running a reviewer alongside a designer, or any work with no shared file dependencies.\n\n"
            "Background mode: when the server has spare inference slots, spawn_agent calls are "
            "dispatched as background tasks — you will receive a placeholder result immediately. "
            "Write a brief acknowledgement (e.g. 'Exploring in background — I'll continue when done.') "
            "and end your turn. When all background agents complete, the session auto-continues: "
            "results are injected and a new turn fires automatically without user input. "
            "You may also discuss other topics with the user while agents are running — the results "
            "will be injected on the next turn regardless."
        )
        self.messages.insert(self._n_fixed, {"role": "system", "content": msg})
        self._n_fixed += 1
        self._capabilities_injected = True

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
            fresh_initial, fresh_paths = _build_initial_messages()
            new_messages = list(fresh_initial)
            self._n_fixed = len(fresh_initial)
            if self.tui_queue:
                await self.tui_queue.put({"type": "system", "text": "Context re-read: " + ", ".join(fresh_paths)})

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
            self._session_path = _save_session(self.messages, self._n_fixed, self._session_path, cwd=self.cwd)
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

    async def send_and_stream(self, user_text: str, plan_mode: bool = False,
                              _sanity_retry: bool = False, _is_autonomous: bool = False,
                              custom_pulse: str | None = None):
        self._turn_active = True
        if _is_autonomous:
            self._auto_turn_count += 1
        else:
            self._auto_turn_count = 0
        self._eli_slot = await _ism.acquire("Eli", timeout_secs=None, bypass_capacity=True)
        if not _sanity_retry:
            # Fresh user turn — reset sanity state
            self._sanity_retry_count = 0
            self._sanity_detector.reset()
            self._telegram_origin = False
        try:
            # Inject completed background results only when ALL agents/processes are done.
            # Partial injection (some agents still running) would let Eli write a report
            # from incomplete data. Hard barrier: inject everything at once or not at all.
            if self._pending_bg_results and not self._bg_agent_tasks and not self._bg_process_tasks:
                await self._inject_pending_bg_results()
            user_text = await self._maybe_compact_input(user_text)
            # Inject behavioral pulse right before user message (high attention proximity).
            # Remove previous pulse first so it never accumulates in history.
            _pulse_text = custom_pulse if custom_pulse is not None else _load_behavioral_pulse()
            if _pulse_text:
                if (self.messages and self.messages[-1].get("role") == "system"
                        and self.messages[-1].get("content", "").startswith(_PULSE_PREFIX)):
                    self.messages.pop()
                self.messages.append({"role": "system", "content": _pulse_text})
            self.messages.append({"role": "user", "content": user_text})

            # Per-turn call-count tracking for loop detection.
            # Key: (tool_name, arguments_string). Resets each user turn.
            _call_counts: dict[tuple[str, str], int] = {}
            # How many identical calls are allowed before the next one is blocked.
            # web_search / web_fetch: block on the 2nd identical call (result won't change).
            # Everything else: block on the 3rd (allows one legitimate retry).
            _LOOP_LIMITS: dict[str, int] = {"web_search": 1, "web_fetch": 1, "edit": 1, "write_file": 1}
            _DEFAULT_LOOP_LIMIT = 2

            while True:
                temperature = 0.3 if self.think_level == "deep" else 0.6
                think_kwargs: dict = {}
                if self.backend == "llamacpp":
                    # llama.cpp Qwen3 jinja template needs this flag to toggle thinking
                    if self.think_level == "off":
                        think_kwargs["chat_template_kwargs"] = {"enable_thinking": False}
                    else:
                        think_kwargs["chat_template_kwargs"] = {"enable_thinking": True}
                # vLLM: no chat_template_kwargs — let the model template handle thinking natively

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
                _t_request = asyncio.get_event_loop().time()
                _t_first_token: float | None = None

                if _debug_file:
                    _debug_write_line(f"\n--- PAYLOAD ---\n{json.dumps(payload, indent=2, default=str)}\n--- END PAYLOAD ---")

                async with self.client.stream(
                    "POST",
                    f"{BASE_URL}/v1/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    if _debug_file:
                        _debug_write_line(f"--- HTTP {response.status_code} headers: {dict(response.headers)} ---")
                    if response.status_code >= 400:
                        body = await response.aread()
                        body_text = body.decode("utf-8", errors="replace")[:600]
                        _ctx_keywords = ("context", "too long", "token limit", "kv cache",
                                         "exceeds", "maximum", "prompt", "capacity")
                        if any(kw in body_text.lower() for kw in _ctx_keywords):
                            raise ContextWindowError(
                                f"HTTP {response.status_code} — context window exceeded: {body_text}"
                            )
                        raise RuntimeError(
                            f"HTTP {response.status_code} from server: {body_text}"
                        )

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
                            if event_type in ("think", "text"):
                                _sd_trigger = self._sanity_detector.feed(
                                    data, mode="think" if event_type == "think" else "text"
                                )
                                if _sd_trigger:
                                    raise SanityError(_sd_trigger)

                            if event_type == "think":
                                if _t_first_token is None:
                                    _t_first_token = asyncio.get_event_loop().time()
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
                                if _t_first_token is None:
                                    _t_first_token = asyncio.get_event_loop().time()
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
                                    live.update(Markdown(_render_latex(text_buf)))

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
                                    live.update(Markdown(_render_latex(text_buf)) if text_buf else Text(""))

                # Update token tracking
                if usage_data:
                    self.tokens_used       = usage_data.get("total_tokens", 0)
                    self.tokens_prompt     = usage_data.get("prompt_tokens", 0)
                    self.tokens_completion = usage_data.get("completion_tokens", 0)
                else:
                    self.tokens_used = sum(
                        len(m.get("content") or "") for m in self.messages
                    ) // CHARS_PER_TOKEN

                # Report timing to server manager (non-blocking fire-and-forget)
                _t_end = asyncio.get_event_loop().time()
                _gen_n = usage_data.get("completion_tokens", 0) if usage_data else 0
                _pre_n = usage_data.get("prompt_tokens", 0) if usage_data else 0
                if _gen_n > 0 and _t_first_token is not None:
                    _gen_elapsed = max(_t_end - _t_first_token, 0.001)
                    _pre_elapsed = max((_t_first_token - _t_request), 0.001)
                    _timing = {
                        "gen":      round(_gen_n / _gen_elapsed, 2),
                        "pre":      round(_pre_n / _pre_elapsed, 2),
                        "gen_n":    _gen_n,
                        "pre_n":    _pre_n,
                        "total_ms": round((_t_end - _t_request) * 1000, 1),
                    }
                    asyncio.create_task(_post_timing(_timing))

                # Auto-compact if approaching context limit
                if not self._compacting and self.tokens_used >= int(self.ctx_window * self.compact_threshold):
                    await self._compact_history()

                # Fallback: model emitted tool calls as text (e.g. 30B with custom template)
                if not tool_calls_received and assistant_content:
                    _parsed = _try_parse_text_tool_calls(assistant_content)
                    if _parsed:
                        tool_calls_received = _parsed
                        assistant_content = ""  # don't echo raw text back into message history

                # Auto-announce if model produced no text before first tool call (TUI only)
                if tool_calls_received and not text_buf.strip() and not self.tui_queue:
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

                    # Split into runs: read-only in parallel, agents in a domain-checked batch,
                    # write ops sequentially.  Stop immediately if any gate rejects a call.
                    _batch: list = []        # pending read-only calls
                    _agent_batch: list = []  # pending spawn_agent calls
                    _gate_rejected = False

                    async def _emit_tool_done(tc_name: str, tc_id: str, result: str) -> None:
                        if self.tui_queue:
                            is_err = result.startswith(("[error", "[unknown", "[blocked", "[cancelled", "[loop-detected]", _GATE_REJECTED_PREFIX))
                            await self.tui_queue.put({"type": "tool_done", "id": tc_id, "name": tc_name, "result": result, "is_error": is_err})

                    # If any state-changing tool ran in this iteration, the world has changed —
                    # reset loop-detection counts so a legitimate re-run isn't blocked.
                    # BUT preserve counts for file ops themselves so read→edit→read loops
                    # don't escape detection by resetting on each edit.
                    _STATE_CHANGING = {"edit", "write_file"}
                    if any(tc["function"]["name"] in _STATE_CHANGING for tc in tool_calls_received):
                        _FILE_OPS = {"read_file", "edit", "write_file"}
                        _call_counts = {k: v for k, v in _call_counts.items() if k[0] in _FILE_OPS}

                    for tc in tool_calls_received:
                        if _gate_rejected:
                            break

                        # Loop detection: block repeated identical calls this turn
                        _tc_name = tc["function"]["name"]
                        _tc_args = tc["function"]["arguments"]
                        _canon   = (_tc_name, _tc_args)
                        _call_counts[_canon] = _call_counts.get(_canon, 0) + 1
                        _limit   = _LOOP_LIMITS.get(_tc_name, _DEFAULT_LOOP_LIMIT)
                        if _call_counts[_canon] > _limit:
                            _loop_advice = {
                                "bash": (
                                    "The environment has not changed since the last run — repeating "
                                    "the same command will produce the same result. "
                                    "Write a script that handles the problem differently, "
                                    "or use write_file/edit to change the inputs before re-running."
                                ),
                                "web_search": (
                                    "A search with these exact terms has already been executed. "
                                    "Reformulate with different keywords, broaden or narrow the query, "
                                    "or synthesize your final answer from the results already retrieved."
                                ),
                                "web_fetch": (
                                    "This URL has already been fetched this turn. "
                                    "The content will not have changed. Use what you already retrieved."
                                ),
                            }
                            _advice = _loop_advice.get(
                                _tc_name,
                                "Try a different approach or formulate your final answer from what you already have.",
                            )
                            _warn = (
                                f"[loop-detected] {_tc_name} has been called with these exact "
                                f"arguments {_call_counts[_canon]} time(s) this turn. {_advice}"
                            )
                            self.messages.append({"role": "tool", "tool_call_id": tc["id"], "content": _warn})
                            await _emit_tool_done(_tc_name, tc["id"], _warn)
                            continue

                        if _tc_name in _READ_ONLY_TOOLS:
                            _batch.append(tc)
                        elif _tc_name == "spawn_agent":
                            _agent_batch.append(tc)
                        else:
                            # Write op: flush pending reads and agents first
                            if _batch:
                                _results = await asyncio.gather(*[_run_one(t) for t in _batch])
                                for _bt, (_tc_id, _result) in zip(_batch, _results):
                                    self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                                    await _emit_tool_done(_bt["function"]["name"], _tc_id, _result)
                                _batch = []
                            if _agent_batch:
                                await self._flush_agent_batch(_agent_batch, _emit_tool_done)
                                _agent_batch = []
                            # Then run the write op sequentially (with file write locking)
                            try:
                                _wl_args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else (tc["function"]["arguments"] or {})
                            except Exception:
                                _wl_args = {}
                            _wl_path = _extract_write_path(_tc_name, _wl_args)
                            _wl_abs = os.path.abspath(_wl_path) if _wl_path else None
                            if _wl_abs and _wl_abs in self._write_locks:
                                _tc_id = tc["id"]
                                _result = f"[error: '{os.path.basename(_wl_abs)}' is currently locked for writing by {self._write_locks[_wl_abs]} — retry after it finishes]"
                                self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                                await _emit_tool_done(_tc_name, _tc_id, _result)
                            else:
                                if _wl_abs:
                                    self._write_locks[_wl_abs] = "Eli"
                                try:
                                    _tc_id, _result = await _run_one(tc)
                                    self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                                    await _emit_tool_done(tc["function"]["name"], _tc_id, _result)
                                finally:
                                    if _wl_abs:
                                        self._write_locks.pop(_wl_abs, None)
                            if _result.startswith(_GATE_REJECTED_PREFIX):
                                _gate_rejected = True
                    # Flush any remaining reads and agents (only if not rejected)
                    if _batch and not _gate_rejected:
                        _results = await asyncio.gather(*[_run_one(t) for t in _batch])
                        for _bt, (_tc_id, _result) in zip(_batch, _results):
                            self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
                            await _emit_tool_done(_bt["function"]["name"], _tc_id, _result)
                    if _agent_batch and not _gate_rejected:
                        await self._flush_agent_batch(_agent_batch, _emit_tool_done)
                    # If every tool call this turn was speak, display the spoken text and
                    # end the turn — no follow-up model round needed (avoids the model
                    # hallucinating "(see above)" instead of writing a real response).
                    _speak_calls = [tc for tc in tool_calls_received
                                    if tc["function"]["name"] == "speak"]
                    if _speak_calls and len(_speak_calls) == len(tool_calls_received):
                        import json as _j
                        for _stc in _speak_calls:
                            try:
                                _spoken = _j.loads(_stc["function"]["arguments"]).get("text", "")
                            except Exception:
                                _spoken = ""
                            if _spoken and self.tui_queue:
                                await self.tui_queue.put({"type": "text_done", "text": _spoken})
                        break
                    # Loop: send tool results back to model
                    continue
                else:
                    if assistant_content:
                        self.messages.append({"role": "assistant", "content": assistant_content})
                    elif not tool_calls_received:
                        # Server returned a 200 stream with no content and no tool calls —
                        # surface this as a visible error rather than silent no-op.
                        _warn = "[empty response from server — no content or tool calls in stream]"
                        if self.tui_queue:
                            await self.tui_queue.put({"type": "system", "text": _warn})
                        else:
                            console.print(f"[yellow]{_warn}[/yellow]")
                    break

            if self.tui_queue:
                await self.tui_queue.put({"type": "usage", "tokens": self.tokens_used, "ctx": self.ctx_window, "slot_index": self._eli_slot.index if self._eli_slot else 0})
            elif self.tokens_used:
                pct   = self.tokens_used / self.ctx_window
                style = "yellow" if pct > 0.6 else "dim"
                label = f"~{self.tokens_used / 1000:.1f}k / {self.ctx_window / 1000:.0f}k tokens"
                console.print(Rule(f"[{style}]{label}[/{style}]", style="dim"))
            else:
                console.print(Rule(style="dim"))
            self._autosave()
        except ContextWindowError:
            # Discard the failed user message so it can be re-sent
            if self.messages and self.messages[-1]["role"] == "user":
                self.messages.pop()
            if (self.messages and self.messages[-1].get("role") == "system"
                    and self.messages[-1].get("content", "").startswith(_PULSE_PREFIX)):
                self.messages.pop()

            _notice = "⚠ Context window exceeded — forcing history compaction and retrying…"
            if self.tui_queue:
                await self.tui_queue.put({"type": "system", "text": _notice})
            else:
                console.print(f"[yellow]{_notice}[/yellow]")

            # Force compaction regardless of threshold
            await self._compact_history(manual=False)

            # If compaction was skipped (history too short), hard-trim oldest non-fixed messages
            if not self._compacting:
                trim_target = self._n_fixed + CTX_KEEP_RECENT
                if len(self.messages) > trim_target:
                    self.messages = self.messages[:self._n_fixed] + self.messages[-CTX_KEEP_RECENT:]

            # Inject notice so the model understands what happened
            _warn = (
                "[CONTEXT WINDOW EXCEEDED — HISTORY COMPACTED]\n"
                "Your previous request was rejected by the server because the conversation "
                "history was too long for the context window. The history has been compacted "
                "automatically. Please retry with a shorter or more focused request if needed."
            )
            self.messages.insert(self._n_fixed, {"role": "system", "content": _warn})

            if not getattr(self, "_ctx_retry_active", False):
                self._ctx_retry_active = True
                self._eli_slot_ctx_retry = (user_text, plan_mode)
            else:
                # Second failure — give up
                self._ctx_retry_active = False
                _msg = "[red]Context window still exceeded after compaction. Please /compact or start a shorter task.[/red]"
                if self.tui_queue:
                    await self.tui_queue.put({"type": "system", "text": _msg})
                else:
                    console.print(_msg)
        except SanityError as _se:
            # Discard partial response — pop last user message so it can be re-sent
            if self.messages and self.messages[-1]["role"] == "user":
                self.messages.pop()
            # Also remove the pulse that was injected before the user message
            if (self.messages and self.messages[-1].get("role") == "system"
                    and self.messages[-1].get("content", "").startswith(_PULSE_PREFIX)):
                self.messages.pop()
            self._sanity_retry_count += 1
            _trigger = str(_se)
            if self._sanity_retry_count <= _ELI_MAX_SANITY_RETRIES:
                _notice = f"⚠ Sanity check triggered ({_trigger}) — retrying automatically…"
                if self.tui_queue:
                    await self.tui_queue.put({"type": "system", "text": _notice})
                else:
                    console.print(f"[yellow]{_notice}[/yellow]")
                # Inject system warning so Eli knows what happened, then re-submit
                _warn = (
                    f"[SANITY CHECK FAILED — RETRY ATTEMPT {self._sanity_retry_count} of {_ELI_MAX_SANITY_RETRIES}]\n"
                    f"Your previous response was aborted by the sanity detector ({_trigger}).\n"
                    "The output entered a degenerate repetition loop and was discarded entirely.\n"
                    "Please retry from scratch. Do not repeat or continue the previous output."
                )
                self.messages.insert(self._n_fixed, {"role": "system", "content": _warn})
                # Re-submit the original user text as a retry (slot is released in finally first)
                self._eli_slot_sanity_retry = (user_text, plan_mode)
            else:
                await self._sanity_abort(_trigger)
        finally:
            self._turn_active = False
            if self._eli_slot is not None:
                await self._eli_slot.release()
                self._eli_slot = None
        # Handle context window retry outside the slot so the new turn can acquire it cleanly
        if hasattr(self, "_eli_slot_ctx_retry"):
            _retry_text, _retry_plan = self._eli_slot_ctx_retry
            del self._eli_slot_ctx_retry
            await self.send_and_stream(_retry_text, plan_mode=_retry_plan)
            self._ctx_retry_active = False
            return
        # Handle sanity retry outside the slot so the new turn can acquire it cleanly
        if hasattr(self, "_eli_slot_sanity_retry"):
            _retry_text, _retry_plan = self._eli_slot_sanity_retry
            del self._eli_slot_sanity_retry
            self._sanity_detector.reset()
            await self.send_and_stream(_retry_text, plan_mode=_retry_plan, _sanity_retry=True)
            return
        # "done" is emitted AFTER the slot is released so _drain_queue can
        # acquire the slot for the next turn before it returns.
        if self.tui_queue:
            await self.tui_queue.put({"type": "done"})

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

    # ── Sanity abort ─────────────────────────────────────────────────────────

    async def _sanity_abort(self, trigger: str) -> None:
        """Display the full abort message and notify Telegram if applicable."""
        _msg = (
            f"The model entered a degenerate output loop twice in a row.\n"
            f"Trigger: {trigger}\n\n"
            "Suggestions:\n"
            "  • Rephrase your prompt — shorter or more specific often helps\n"
            "  • Try a different model profile (lower quantisation or smaller context)\n"
            "  • Reduce context window size or clear history with /clear\n"
            "  • Restart the inference server if the problem persists"
        )
        if self.tui_queue:
            await self.tui_queue.put({"type": "error", "text": f"⚠ Sanity Check — Inference Aborted\n\n{_msg}"})
        else:
            console.print(Panel(_msg, title="[red]⚠ Sanity Check — Inference Aborted[/red]", border_style="red"))
        if self._telegram_origin:
            await self._notify_telegram_sanity_abort(trigger)

    async def _notify_telegram_sanity_abort(self, trigger: str) -> None:
        """Send a sanity abort notice back through the Telegram bridge."""
        _tg_msg = (
            f"⚠ Inference aborted (sanity check: {trigger})\n\n"
            "The model entered a degenerate loop and could not recover.\n"
            "Please try rephrasing your prompt, or try again later."
        )
        try:
            import urllib.request as _ur, json as _json
            _body = _json.dumps({"message": _tg_msg}).encode()
            _req = _ur.Request("http://localhost:1237/chat", data=_body,
                               headers={"Content-Type": "application/json"}, method="POST")
            _ur.urlopen(_req, timeout=5)
        except Exception:
            pass  # Telegram bridge may not be running — fail silently

    @staticmethod
    def _validate_path_arg(raw: str) -> str | None:
        """Return an error string if raw looks like a malformed path, else None."""
        if not raw:
            return None
        if "\n" in raw or "\r" in raw:
            return f"[error: malformed path argument — contains newline: {raw[:120]!r}]"
        # Reject paths longer than 512 chars (prose bleed-through)
        if len(raw) > 512:
            return f"[error: malformed path argument — suspiciously long ({len(raw)} chars): {raw[:80]!r}...]"
        return None

    async def _dispatch_tool(self, name: str, args: dict, tc_id: str = "") -> str:
        """Pure tool dispatch — no display, no approval check."""
        try:
            if name == "bash":
                _bash_cwd = Path(args["cwd"]) if args.get("cwd") else self.cwd
                return await tool_bash(args.get("command", ""), args.get("timeout", 30), cwd=_bash_cwd)
            elif name == "read_file":
                _rf_raw = args.get("path", "") or args.get("file_path", "")
                if _pv_err := self._validate_path_arg(_rf_raw):
                    return _pv_err
                _rf_abs = os.path.abspath(self._resolve_path(_rf_raw))
                result = await tool_read_file(_rf_abs, offset=int(args.get("offset", 1)), limit=int(args.get("limit", 200)))
                self._last_read.add(_rf_abs)
                return result
            elif name == "write_file":
                _wf_raw = args.get("path", "") or args.get("file_path", "")
                if _pv_err := self._validate_path_arg(_wf_raw):
                    return _pv_err
                _wf_abs = os.path.abspath(self._resolve_path(_wf_raw))
                if os.path.exists(_wf_abs) and os.path.getsize(_wf_abs) > 0:
                    return (f"[error: '{_wf_abs}' already exists and has content — "
                            f"use edit to modify existing files. "
                            f"write_file is only for creating new files or overwriting empty files.]")
                result = await tool_write_file(_wf_abs, args.get("content", ""))
                return result
            elif name == "list_dir":
                return await tool_list_dir(self._resolve_path(args.get("path", ".")))
            elif name == "glob":
                return await tool_glob(args.get("pattern", "*"), self._resolve_path(args.get("path", ".")), args.get("include_all", False))
            elif name == "grep":
                return await tool_grep(
                    args.get("pattern", ""),
                    self._resolve_path(args.get("path", ".")),
                    args.get("glob", "**/*"),
                    args.get("case_insensitive", False),
                    args.get("context_lines", 2),
                    args.get("include_all", False),
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
                _ed_raw = args.get("path", "") or args.get("file_path", "")
                if _pv_err := self._validate_path_arg(_ed_raw):
                    return _pv_err
                _ed_abs = os.path.abspath(self._resolve_path(_ed_raw))
                if _ed_abs not in self._last_read:
                    return f"[error: must read '{_ed_abs}' with read_file before editing it]"
                result = await tool_edit(_ed_abs, args.get("old_string", ""), args.get("new_string", ""))
                return result
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
                if self._subagent_depth > 0:
                    return "[error: nested sub-agent spawning is not allowed]"
                return await self._tool_spawn_agent(
                    args.get("system_prompt", ""),
                    args.get("task", ""),
                    args.get("tools"),
                    args.get("think_level"),
                    min(args.get("max_iterations", 60), 60),
                    args.get("model"),
                )
            elif name == "analyze_image":
                if self._subagent_depth > 0:
                    return "[error: analyze_image not available inside sub-agents]"
                images = args.get("images") or ([args["image_path"]] if args.get("image_path") else [])
                return await self._tool_analyze_image(images, args.get("prompt"))
            elif name == "queue_agents":
                if self._subagent_depth > 0:
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
            elif name == "send_telegram":
                if self._subagent_depth > 0:
                    return "[error: send_telegram not available inside sub-agents]"
                from scheduler import tg_send, _load_admin_id
                _tg_uid = args.get("user_id")
                if _tg_uid is None:
                    _tg_uid = _load_admin_id()
                if not _tg_uid:
                    return "[send_telegram error: no user_id provided and ADMIN_ID not set in telegram_bot/.env]"
                return await tg_send(int(_tg_uid), args.get("message", ""))
            elif name == "run_background":
                if self._subagent_depth > 0:
                    return "[error: run_background not available inside sub-agents]"
                _rb_cmd   = args.get("command", "")
                _rb_label = args.get("label", "process")
                _rb_to    = min(int(args.get("timeout", 300)), 3600)
                _rb_cwd   = str(Path(args["cwd"]) if args.get("cwd") else self.cwd)
                _rb_tc_id = tc_id  # real tool-call id from the model
                _task = asyncio.create_task(
                    self._run_background_process(_rb_tc_id, _rb_cmd, _rb_label, _rb_to, _rb_cwd)
                )
                _task.add_done_callback(
                    lambda t: self._bg_process_tasks.remove(t) if t in self._bg_process_tasks else None
                )
                self._bg_process_tasks.append(_task)
                return "[background: process running — result pending]"
            elif name == "manage_schedule":
                scheduler = getattr(self, "_scheduler", None)
                if scheduler is None:
                    return "[manage_schedule error: scheduler not running]"
                action = args.get("action", "list")
                if action == "list":
                    jobs = scheduler.list_jobs()
                    if not jobs:
                        return "No scheduled jobs."
                    lines = ["ID    En   When            Next Run              Runs  Task"]
                    for j in jobs:
                        en = "yes" if j.get("enabled") else "no"
                        lines.append(f"{j['id']}  {en:4}  {j.get('when',''):15} {j.get('next_run') or '—':21} {j.get('run_count',0):4}  {j.get('task','')[:60]}")
                    return "\n".join(lines)
                elif action == "add":
                    when = args.get("when")
                    task = args.get("task")
                    if not when or not task:
                        return "[manage_schedule error: 'when' and 'task' required for add]"
                    from scheduler import _load_admin_id
                    tg_id = args.get("telegram_user_id") or _load_admin_id()
                    if not tg_id:
                        return "[manage_schedule error: no telegram_user_id and ADMIN_ID not set]"
                    try:
                        job = scheduler.add_job(when, int(tg_id), task)
                        return f"Job {job['id']} created.\n  When: {job['when']}\n  Next run: {job.get('next_run') or 'N/A'}\n  Telegram: {tg_id}\n  Task: {task}"
                    except ValueError as e:
                        return f"[manage_schedule error: {e}]"
                elif action in ("remove", "enable", "disable"):
                    job_id = args.get("job_id")
                    if not job_id:
                        return f"[manage_schedule error: job_id required for {action}]"
                    if action == "remove":
                        return f"Job {job_id} removed." if scheduler.remove_job(job_id) else f"[manage_schedule error: job {job_id} not found]"
                    ok = scheduler.set_enabled(job_id, action == "enable")
                    word = "enabled" if action == "enable" else "disabled"
                    return f"Job {job_id} {word}." if ok else f"[manage_schedule error: job {job_id} not found]"
                return f"[manage_schedule error: unknown action '{action}']"
            else:
                return f"[unknown tool: {name}]"
        except Exception as e:
            return f"[tool error: {e}]"

    async def _call_tool(self, name: str, arguments_str: str, call_id: str) -> str:
        try:
            args = json.loads(arguments_str) if arguments_str.strip() else {}
        except json.JSONDecodeError as _je:
            return f"[error: malformed tool arguments — JSON parse failed: {_je}. Raw: {arguments_str[:200]}]"
        args = normalize_tool_args(args)

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
            name, args, self.approval_level, session_rules=self.session_rules, cwd=self.cwd
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
        result = await self._dispatch_tool(name, args, tc_id=call_id)

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
    from commands import handle_slash_command
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

        # ── Scheduler daemon (TUI mode) ───────────────────────────────────────
        from scheduler import SchedulerDaemon as _SchedulerDaemon
        _tui_scheduler = _SchedulerDaemon(chat)
        chat._scheduler = _tui_scheduler
        await _tui_scheduler.start()

        # ── --continue: restore all settings + resume last session ────────────
        if do_continue:
            state = _load_state()
            chat.think_level   = state.get("think_level",   "on")
            chat.compact_mode  = state.get("compact_mode",  False)
            chat.approval_level = state.get("approval_level", "auto")
            chat.model         = state.get("model",         MODEL)
            chat.role          = state.get("role",          "eli")
            last_name          = state.get("last_session")  # stem, e.g. "2025-01-01_12-00-00"
            saved_msgs, sess_path, saved_cwd = _load_session(last_name)
            if saved_msgs:
                chat.messages.extend(saved_msgs)
                chat._session_path = sess_path
                if saved_cwd and Path(saved_cwd).is_dir():
                    chat.cwd = Path(saved_cwd)
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
            saved_msgs, sess_path, saved_cwd = _load_session(resume_name)
            if saved_msgs:
                chat.messages.extend(saved_msgs)
                chat._session_path = sess_path
                if saved_cwd and Path(saved_cwd).is_dir():
                    chat.cwd = Path(saved_cwd)
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

            # Automatic skill trigger activation
            skill_name, skill_args = _check_skill_triggers(user_input)
            if skill_name:
                if await _invoke_skill(skill_name, skill_args, chat):
                    continue

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

            # Auto-trigger: if a background process finished during the turn, continue
            while chat._auto_trigger.is_set():
                chat._auto_trigger.clear()
                _wake_msg = chat._auto_trigger_msg
                chat._auto_trigger_msg = ""
                console.print(Rule(style="dim"))
                _auto_task = asyncio.create_task(
                    chat.send_and_stream(_wake_msg, _is_autonomous=True)
                )
                current_task[0] = _auto_task
                try:
                    await _auto_task
                except asyncio.CancelledError:
                    console.print("[dim](interrupted)[/dim]")
                except Exception as _ae:
                    console.print(f"[red]Error: {_ae}[/red]")
                finally:
                    current_task[0] = None

        await _tui_scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
