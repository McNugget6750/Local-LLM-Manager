# Local LLM Manager

A local LLM chat GUI + server manager supporting multiple inference backends:
[ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp), [llama.cpp](https://github.com/ggml-org/llama.cpp), and [vLLM](https://github.com/vllm-project/vllm) (via WSL).

Includes **Eli** — a coding assistant with tool use, background agents, agent queues, vision analysis, voice I/O, plan mode, autonomous execute-plan loops, Telegram remote access, slash commands, and persistent session state.

---

## Components

| File / directory | Role |
|-----------------|------|
| `server_manager.py` | Tkinter GUI — launch, monitor, and switch inference servers |
| `qt/main.py` | Qt chat GUI (primary interface) — launch via `qt/run.bat` or **Open Chat** |
| `chat.py` | Terminal chat client — same backend as Qt GUI |
| `commands.json` | Model profiles (gitignored — copy from `commands.example.json`) |
| `ELI.md` | Eli's behavioral rules and persona |
| `behavioral_pulse.md` | Condensed rules injected before every turn for attention retention |
| `agents/` | Agent persona definitions |
| `skills/` | Slash command prompt workflows |
| `eli.toml` | Project-specific config (gitignored, auto-loaded from cwd) |
| `USER_PROFILE.md` | Personal info Eli uses to personalize responses (gitignored) |
| `telegram_bot/` | Telegram bot interface |

---

## Quick start

```bat
git clone https://github.com/McNugget6750/Local-LLM-Manager.git
cd Local-LLM-Manager

python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

copy commands.example.json commands.json
:: Edit commands.json — set your binary path and model paths

copy USER_PROFILE.example.md USER_PROFILE.md
:: Edit USER_PROFILE.md with your background and preferences

run.bat          :: server manager
qt\run.bat       :: chat GUI (connect to a running server)
chat.bat         :: terminal chat
```

---

## Server manager

`run.bat` opens the Tkinter GUI. Select a model profile, click **Start**. Add new profiles with **+ Add Model** — saved to `commands.json`.

Once running, click **Open Chat** to launch the Qt GUI with `--continue` (resumes last session). The voice server starts and stops alongside the inference server. Click **🤖 Telegram** to start or stop the Telegram bot manually, or check **Auto-start Telegram** to have it start and stop with the inference server automatically.

The server manager exposes a loopback control API on port 1235. Eli uses this to switch models automatically when dispatching agents — the GUI tracks state correctly throughout.

---

## Supported backends

All backends expose an OpenAI-compatible `/v1` API. The engine is auto-detected from the first token of the launch command.

| Backend | Engine tag | Notes |
|---------|-----------|-------|
| ik_llama.cpp / llama.cpp | `llama` | GGUF models, Windows-native, CPU+GPU offload |
| vLLM via WSL | `wsl` | HuggingFace safetensors, NVFP4/FP8 quants, Blackwell GPU |

### llama.cpp profile

```json
"My Model · Q6_K": [
  "..\\llama.cpp\\build\\bin\\Release\\llama-server.exe",
  "-m", "path\\to\\model.gguf",
  "-ngl", "999", "-c", "32768",
  "-ctk", "q4_0", "-ctv", "q4_0", "-fa", "on",
  "--no-mmap", "--jinja",
  "-b", "512", "-ub", "512", "-t", "16",
  "--parallel", "2", "--port", "1234", "--host", "0.0.0.0"
]
```

> **Large context tip:** Use `-b 512 -ub 512` for contexts above 32k. A large batch size combined with flash attention causes a temporary memory spike during prefill proportional to `batch × context` — at 128k this can crash the server silently.

### vLLM (WSL) profile

```json
"My Model · NVFP4 [vLLM]": [
  "wsl", "--exec", "bash", "-c",
  "/home/user/miniconda3/bin/conda run -n vllm-env python -m vllm.entrypoints.openai.api_server --model /mnt/c/path/to/model --quantization modelopt --dtype bfloat16 --max-model-len 8192 --max-num-seqs 2 --port 1234 --host 0.0.0.0"
]
```

NVFP4 requires a Blackwell GPU (RTX 5000/6000 series, B100/B200) and ~96 GB VRAM for comfortable use. On a 32 GB card (RTX 5090), use 4096 context maximum and `--enforce-eager`.

---

## Qt chat GUI

```bat
qt\run.bat               :: new session
qt\run.bat --continue    :: resume last session
qt\run.bat --resume name :: resume named session
```

- File explorer on the left — double-click a directory to set the working directory
- Code editor panel with syntax highlighting, excerpt selection, and line references
- Agent tab streams sub-agent output in real time
- Per-slot context bars above the input — one bar for Eli, one added per active agent slot. Color shifts yellow at 60%, red at 80%. Agents are stopped automatically at 92% context fill to prevent silent server crashes.
- Slash command output renders as styled HTML panels in the chat view
- Up/Down arrows in the input box navigate message history
- Voice selector in the server manager populates once the voice server is running; selection persists across restarts

---

## Slash commands

| Command | Description |
|---------|-------------|
| `/skills` | List available skills with triggers |
| `/commit` | Generate a conventional commit message |
| `/review <file>` | Deep code review sub-agent |
| `/research <topic>` | 3-pass skeptical research sub-agent |
| `/plan <feature>` | Implementation planning sub-agent |
| `/implementation_plan` | Create and validate a structured TDD implementation plan |
| `/code <task>` | Production code writing sub-agent |
| `/execute-plan <path>` | Execute a plan end-to-end with automatic review-fix loops (up to 3 cycles) |
| `/queue-results [label]` | List or show agent queue run results |
| `/model [name]` | List profiles or switch to one |
| `/role <name>` | Adopt an agent persona (`/role eli` to revert) |
| `/voice [ptt\|auto] [tools]` | Start voice conversation mode |
| `/config` | Show loaded `eli.toml` config |
| `/cd <path>` | Change working directory |
| `/think [off\|on\|deep]` | Set thinking level |
| `/approval [auto\|ask-writes\|ask-all\|yolo]` | Set tool approval level |
| `/compact` | Summarise older messages to free context |
| `/status` | Token usage and context window info |

**Keyboard shortcuts (Qt GUI):**

- `Ctrl+C` — cancel current response
- `Shift+Tab` — toggle plan mode (reads/searches only, write tools blocked)
- Up / Down — navigate input history

---

## Voice mode

Requires `eli_voice_server` on port 1236 (Kokoro ONNX TTS + faster-whisper STT). Starts automatically with the inference server, or run `voice_server.bat` standalone.

```
/voice              :: PTT mode (hold Insert key to record)
/voice auto         :: VAD mode (silence detection triggers send)
/voice ptt tools    :: PTT with tool access enabled
/voice auto tools   :: VAD with tool access
```

The active voice in the server manager is saved to `ui_prefs.json` and restored on restart. The `speak` tool sends audio output and also displays the spoken text in the chat view.

---

## Telegram Bot

Provides remote access to Eli via Telegram. The bot forwards messages to the Qt Chat GUI, which must be running alongside the inference server.

**Prerequisites — both must be running before starting the bot:**
1. **Inference server** — start via Server Manager or `server_manager.py`
2. **Qt Chat GUI** — start via "Open Chat" in Server Manager, or `qt/main.py` directly

```bat
.venv\Scripts\python.exe -m telegram_bot.main
```

The Server Manager can handle this automatically: check **Auto-start Telegram** and the bot will start/stop with the inference server. Open the Chat GUI once via "Open Chat" — it persists in the background.

**Configuration (`.env`):**
- `BOT_TOKEN`: Telegram Bot API token.
- `ALLOWED_USERS`: Comma-separated list of Telegram user IDs allowed to use the bot.
- `SILENT_REJECTION`: If `true`, the bot will not respond to unauthorized users.

**Security:**
- **Allowlist**: Only users in `ALLOWED_USERS` can interact with the bot.
- **Auto-blocking**: Users not on the allowlist are automatically added to `blocklist.txt` after 10 unauthorized attempts.
- **Blocklist**: Blocked users are ignored immediately without further processing.

---

Eli dispatches sub-agents for specialized tasks. Agents run on the inference server (with an optional model switch) and report back. The GUI shows a per-agent context bar while it's running.

### Background vs inline

- **Background agents** run in parallel while Eli stays responsive. Results are injected into context and Eli is notified automatically to continue any pending task list.
- **Inline agents** block the current turn. Use only when the result is required before anything else can proceed.

Eli's default is background — fire immediately and stay available for other questions.

### Agent profiles (`agents/*.md`)

Each profile is a Markdown file with a YAML frontmatter block and a system prompt body:

```markdown
---
write_domains: [python_files]
read_domains: [python_files, test_files, docs]
---

You are an expert software engineer...
```

**Frontmatter fields:**

| Field | Type | Description |
|-------|------|-------------|
| `write_domains` | list | Tool domains the agent may write to. Empty = read-only. |
| `read_domains` | list | Tool domains the agent may read from. |
| `Recommended model` | string | If present, Eli switches to this model profile before spawning the agent, then restores the original model when done. **Omit this field to keep the agent on the current model and preserve background mode.** |

**Domain values:** `python_files`, `test_files`, `docs`, `html_css_js`, `text files`

> **Model switching and background mode:** When an agent profile contains a `Recommended model` line, the server must switch models before the agent can run. This forces the agent to run inline (blocking) because background mode cannot survive a server switch. If you want agents to run in background, leave `Recommended model` out of the profile — the agent will use whatever model is currently loaded.

**Available profiles:**

| Profile | Write domains | Purpose |
|---------|--------------|---------|
| `code-researcher` | — | Targeted code research, API lookup, install commands |
| `code-review` | — | Review code for correctness, safety, and design issues |
| `doc-writer` | docs | Write docstrings or README sections |
| `expert_coder` | python_files | Production code implementation |
| `generic` | — | General-purpose tasks, quick tests, system checks |
| `graphics_designer` | — | Brand identity, icons, colour systems |
| `level_designer` | — | Game level layout, encounter design, puzzle design |
| `researcher` | — | Research a library, API, or technical question |
| `test-writer` | test_files | Write unit tests |
| `voice` | — | Voice interaction persona |
| `web_designer` | html_css_js | UI/UX, layout feedback, CSS/HTML critique |

### Spawning agents

```python
# Single agent (background by default)
spawn_agent(system_prompt="researcher", task="What are the tradeoffs of X?")

# Two independent agents in parallel
spawn_agent(system_prompt="code-researcher", task="Find the httpx streaming API")
spawn_agent(system_prompt="expert_coder", task="Implement the download manager")

# Ordered pipeline (researcher output feeds expert_coder)
queue_agents(tasks=[
    {"system_prompt": "researcher", "task": "Research library X"},
    {"system_prompt": "expert_coder", "task": "Implement using the research above"}
])
```

Results from completed background agents are injected into Eli's context before the next user turn. Eli is then notified with a system message listing which agents finished, so it can continue any pending task list items that were waiting.

---

## Agent queues (`/queue-results`)

`queue_agents` runs a sequence where each agent's output is available to the next. Model switches between consecutive agents on the same model are skipped. The original model is restored after the queue completes. Results are written to `sessions/queue_{ts}_{label}/results.json`.

Browse results with `/queue-results` (list all) or `/queue-results <label>` (show one).

---

## Vision

Eli analyses images via a local vision-language model. The server switches automatically, processes all queued images, then restores the text model.

```json
{
  "_meta": {
    "vision_url": "http://192.168.x.x:1234",
    "vision_external": false
  }
}
```

Set `vision_external: true` for a vision model on a separate machine — Eli calls it directly without switching the local server.

---

## Model switching

Eli knows all available model profiles at startup (descriptions, strengths, weaknesses, speed) from `commands.json`. It can:

- Switch models manually via `/model <name>`
- Switch automatically before an agent that has a `Recommended model` in its profile
- Restore the original model when the agent or queue completes

All switches go through the server manager control API so UI state stays in sync.

---

## Configuration

### `commands.json`

Model profiles plus optional metadata. Copy from `commands.example.json`. Gitignored.

Profile metadata (optional `_meta` block):

```json
{
  "_meta": {
    "vision_url": "http://localhost:1234",
    "vision_external": false
  },
  "My Model · Q6_K": ["..."]
}
```

Each profile entry is a list of command tokens (the engine is auto-detected from `entry[0]`). The profile name, strengths, and speed description are injected into Eli's context at startup.

### `eli.toml`

Project-specific config injected as a system message at startup. Place in any project root (or a parent directory — Eli walks up from the cwd):

```toml
[project]
name = "my-project"

[build]
command = "cmake --build build --preset release"
cwd = "."

[hooks]
post_edit = "run_tests.bat"
```

Run `/config` to inspect what's loaded.

### `behavioral_pulse.md`

Condensed behavioral rules injected as a system message immediately before every user turn. This keeps critical rules in high-attention position as the conversation grows long — the same mechanism Claude Code uses for CLAUDE.md. The previous injection is replaced each turn so history stays flat (exactly one pulse in context at all times). Edit this file to tune Eli's priorities without touching code.

### `ELI.md`

Full behavioral rules, persona, tool protocols, and workflow guides. Loaded once as the system prompt. For rules that Eli tends to forget over long conversations, the key points should also appear in `behavioral_pulse.md`.

### `USER_PROFILE.md`

Personal info about you — name, background, projects, preferences. Eli reads this at startup to personalize responses. Gitignored. Copy from `USER_PROFILE.example.md`.

---

## Skills

Prompt workflows in `skills/`. Invoked with `/skillname` or triggered automatically when your message matches a skill's trigger list.

```yaml
---
name: my-skill
description: One-line description shown in /skills
spawn_agent: true
think_level: deep
max_iterations: 20
triggers: [keyword, another keyword]
context_files: [path/to/extra.md]
---

Your skill prompt here...
```

| Skill | Agent | Description |
|-------|:-----:|-------------|
| `/research` | yes | 3-pass skeptical research — sweep, cross-examine, synthesise |
| `/plan` | yes | Implementation planning — reads codebase, evaluates approaches, produces task checklist |
| `/implementation_plan` | no | Create and validate structured TDD implementation plans with phase gates |
| `/code` | yes | Production code — reads first, designs, implements, tests, self-reviews |
| `/review` | yes | Deep code review — reads code, callers, and tests; reports issues by severity |
| `/execute-plan <path>` | no | Execute a plan document end-to-end; spawns agents per phase, then runs up to 3 review-fix cycles before reporting |
| `/commit` | no | Conventional commit message template |
| `/pr` | no | Pull request description template |
| `/git-status` | no | Git status summary |

---

## Session persistence

The following are saved per session and restored on `--continue` or `/resume`:

- Think level, compact mode, approval level
- Active model and role
- Working directory
- Full message history (with compaction summary if `/compact` was used)

Sessions are stored in `sessions/` as JSON.

---

## Credits

- [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp) — high-performance llama.cpp fork
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — upstream project
- [vLLM](https://github.com/vllm-project/vllm) — GPU-accelerated inference for HuggingFace models
- [Kokoro](https://github.com/remsky/Kokoro-FastAPI) — ONNX TTS
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — STT
