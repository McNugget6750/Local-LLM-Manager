"""
profiles.py — Profile and config loading helpers.

Pure file-reading utilities with no UI or session dependencies.
Imported by chat.py, agents.py, and commands.py.
"""
import json
import re as _re
from pathlib import Path


# ── System prompt & memory ────────────────────────────────────────────────────

def _vision_url() -> str:
    """Read vision server URL from commands.json _meta, fallback to localhost:1236."""
    return _load_commands_meta().get("vision_url", "http://localhost:1236")


def _load_system_prompt() -> str:
    eli_md = Path(__file__).parent / "ELI.md"
    if eli_md.exists():
        return eli_md.read_text(encoding="utf-8")
    return (
        "You are Eli, a local AI coding assistant running on Qwen3. "
        "You have access to tools: bash, read_file, write_file, edit, list_dir, glob, grep, web_fetch, web_search. "
        "Use them proactively. Prefer edit over write_file. Be concise and direct."
    )


def _load_memory() -> str | None:
    mem = Path(__file__).parent / "MEMORY.md"
    if mem.exists():
        return mem.read_text(encoding="utf-8")
    return None


SYSTEM_PROMPT = _load_system_prompt()

_PULSE_PREFIX = "[Behavioral Reminder"

def _load_behavioral_pulse() -> str | None:
    pulse_file = Path(__file__).parent / "behavioral_pulse.md"
    if pulse_file.exists():
        return pulse_file.read_text(encoding="utf-8").strip()
    return None


# ── Model profiles (commands.json) ────────────────────────────────────────────

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
    """Format available model profiles + agent profiles as a system message for injection at startup."""
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
        lines.append("")

    agent_ctx = _list_agent_profiles()
    if agent_ctx:
        lines.append(agent_ctx)

    return "\n".join(lines).strip()


# ── Agent profiles ─────────────────────────────────────────────────────────────

def _list_agent_profiles() -> str:
    """Return a formatted string listing all available agent profiles with one-line descriptions.

    The first non-empty, non-heading line of each profile's content is used as
    the description (typically the "You are a ..." identity line).
    """
    agents_dir = Path(__file__).parent / "agents"
    if not agents_dir.exists():
        return ""
    lines = ["[Available Agent Profiles — pass name as system_prompt in spawn_agent]", ""]
    for md in sorted(agents_dir.glob("*.md")):
        profile = _load_agent_profile(md.stem)
        # Extract first meaningful sentence from the prompt as a description
        desc = ""
        for line in profile["prompt"].splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                desc = line
                break
        lines.append(f"• {md.stem}  —  {desc}")
    lines.append("")
    lines.append(
        "Always pass the profile name as system_prompt (e.g. system_prompt='researcher'). "
        "Do NOT write a raw description — the profile already contains the full system prompt."
    )
    return "\n".join(lines).strip()


def _load_agent_profile(name: str) -> dict:
    """Load an agent profile .md and return {prompt, write_domains, read_domains, model}.

    If `name` contains whitespace it is treated as a raw inline prompt (no file lookup).
    Domain fields are parsed from YAML frontmatter:  write_domains: [a, b]
    """
    if " " in name.strip():
        # Raw inline prompt — unknown domains, conservative
        _m = _re.search(r'\*\*Recommended model:\*\*\s*`([^`]+)`', name)
        return {"prompt": name, "write_domains": [], "read_domains": [], "model": _m.group(1).strip() if _m else None}
    profile_path = Path(__file__).parent / "agents" / f"{name}.md"
    if not profile_path.exists():
        return {"prompt": name, "write_domains": [], "read_domains": [], "model": None}
    text = profile_path.read_text(encoding="utf-8")
    result: dict = {"prompt": text, "write_domains": [], "read_domains": [], "model": None}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_block = text[3:end]
            for field in ("write_domains", "read_domains"):
                m = _re.search(rf'^{field}:\s*\[([^\]]*)\]', fm_block, _re.MULTILINE)
                if m:
                    result[field] = [d.strip() for d in m.group(1).split(",") if d.strip()]
            result["prompt"] = text[end + 4:].strip()
    _m = _re.search(r'\*\*Recommended model:\*\*\s*`([^`]+)`', result["prompt"])
    if _m:
        result["model"] = _m.group(1).strip()
    return result


def _can_run_parallel(profile_a: dict, profile_b: dict) -> bool:
    """Return True if two agent profiles have no write/read domain conflicts."""
    aw = set(profile_a.get("write_domains", []))
    bw = set(profile_b.get("write_domains", []))
    ar = set(profile_a.get("read_domains", []))
    br = set(profile_b.get("read_domains", []))
    return not (aw & bw) and not (aw & br) and not (bw & ar)


def _all_can_parallel(profiles: list[dict]) -> bool:
    """Return True if every pair of profiles can run concurrently."""
    for i in range(len(profiles)):
        for j in range(i + 1, len(profiles)):
            if not _can_run_parallel(profiles[i], profiles[j]):
                return False
    return True


# ── Project config (eli.toml) ─────────────────────────────────────────────────

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
