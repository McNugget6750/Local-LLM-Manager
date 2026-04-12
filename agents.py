"""
agents.py — Agent orchestration: spawn_agent, queue_agents, background agents.

Contains the AgentsMixin class (inherited by ChatSession), module-level helpers
for server control and profile switching, and the _ism SlotManager singleton.

Import chain: constants ← profiles ← tools ← agents ← chat (lazy)
No circular imports — chat.py symbols imported lazily inside methods.
"""

import asyncio
import json
import logging
import os
import random
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

import httpx
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape as markup_escape
from rich.panel import Panel
from rich.text import Text

from constants import BASE_URL, CONTROL_URL, console
from slot_manager import SlotManager
from profiles import (
    _load_commands,
    _load_agent_profile,
    _load_commands_meta,
    _can_run_parallel,
    _all_can_parallel,
)
from tools import TOOLS, _is_bare_python, _build_approval_check, _GATE_REJECTED_PREFIX
from unicode_normalize import normalize_tool_args


# ── Inference Slot Manager singleton ─────────────────────────────────────────
_ism = SlotManager(base_url=BASE_URL)


# ── Server control helpers ────────────────────────────────────────────────────

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


def _extract_write_path(tc_name: str, tc_args: dict) -> str | None:
    """Return the file path being written for structured write tools, or None."""
    if tc_name in ("edit", "write_file"):
        return tc_args.get("file_path") or tc_args.get("path")
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


# ── AgentsMixin ───────────────────────────────────────────────────────────────

class AgentsMixin:
    """Mixin for ChatSession: all agent-related methods live here."""

    async def _inject_pending_bg_results(self) -> None:
        """Replace background-agent placeholder tool results with real output.

        Called at the start of the next send_and_stream turn after background agents
        complete. Only results that have arrived are injected; any still running remain
        in _pending_bg_results for the following turn.

        After injection, if all tool results in the current batch are resolved (no
        placeholders remaining), a synthetic assistant message is appended to close
        the tool-call cycle.  This prevents Eli from feeling obligated to explicitly
        process and summarise the results — they are silently available as context
        for all future turns.
        """
        _PLACEHOLDER = "[background: agent dispatched — result pending]"

        results_map = {tc_id: result for tc_id, result in self._pending_bg_results}
        self._pending_bg_results.clear()

        injected_count = 0
        for msg in self.messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id") in results_map:
                msg["content"] = results_map[msg["tool_call_id"]]
                injected_count += 1

        self._pending_bg_tool_calls.clear()

        # Only close the cycle if no placeholders remain (all agents in this batch done).
        still_pending = any(
            msg.get("role") == "tool" and msg.get("content") == _PLACEHOLDER
            for msg in self.messages
        )
        if injected_count and not still_pending:
            noun = "agent" if injected_count == 1 else f"{injected_count} agents"
            self.messages.append({
                "role": "assistant",
                "content": f"[Background {noun} complete — results available in context]",
            })
            log.debug("_inject_pending_bg_results: closed %d tool result(s) with synthetic ack", injected_count)

    async def _run_background_agent(
        self, tc: dict, args: dict, label: str, current_model: str | None
    ) -> None:
        """Run a single spawn_agent call as a background asyncio Task.

        Acquires an ISM slot for its lifetime. The slot is released in __aexit__
        regardless of exception or cancellation.
        Task removal from _bg_agent_tasks is handled by add_done_callback in the caller.
        """
        result = "[cancelled]"
        log.debug("BgAgent[%s]: starting", label)
        try:
            result = await self._tool_spawn_agent(
                args.get("system_prompt", ""),
                args.get("task", ""),
                args.get("tools"),
                args.get("think_level"),
                min(args.get("max_iterations", 60), 60),
                None,                        # model=None: eligibility confirmed same-model
                agent_label=label,
                current_model_hint=current_model,
                _is_background=True,
                _tool_id=tc["id"],
            )
            log.debug("BgAgent[%s]: completed normally", label)
        except asyncio.CancelledError:
            result = "[agent evicted — 45-minute timeout reached]"
            log.warning("BgAgent[%s]: cancelled (evicted)", label)
            raise
        except Exception as exc:
            result = f"[background agent error: {exc}]"
            log.warning("BgAgent[%s]: error — %s", label, exc)
        self._pending_bg_results.append((tc["id"], result))
        if self.tui_queue:
            await self.tui_queue.put({
                "type": "tool_done",
                "id": tc["id"],
                "name": "spawn_agent",
                "result": result,
                "is_error": result.startswith(("[error", "[background agent error", "[agent evicted")),
                "agent_label": label,
            })

    async def _flush_agent_batch(self, batch: list, emit_fn: Callable) -> None:
        """Run spawn_agent calls, grouping by target model, parallelising within each group when domain-safe.

        Model switching is done once per group (not per agent). Live slot count is
        queried after each switch so the capacity reflects the actual loaded model.
        A try/finally guarantees the original model is always restored, even on
        exception or asyncio cancellation.

        emit_fn: async callable(tc_name, tc_id, result) — routes result to tui_queue.
        """
        batch_args = []
        for _tc in batch:
            try:
                _a = json.loads(_tc["function"]["arguments"]) if isinstance(_tc["function"]["arguments"], str) else _tc["function"]["arguments"]
            except Exception:
                _a = {}
            batch_args.append(_a)

        # Snapshot current model and valid profile names once
        _original_model = await _find_active_profile()
        _commands = _load_commands()

        # Group agents by resolved target model; original-model group runs first
        _groups: dict = {}
        for _tc, _args in zip(batch, batch_args):
            _m = _args.get("model")
            _tgt = _m if (_m and _m in _commands) else _original_model
            _groups.setdefault(_tgt, []).append((_tc, _args))
        _ordered = sorted(_groups.items(), key=lambda kv: kv[0] != _original_model)

        # Background eligibility: all agents target the current model + at least 2 slots exist
        # (one for the background agent, one for Eli's next turn — single-slot servers
        #  would just serialize with extra overhead, so we skip background dispatch there)
        _bg_eligible = (
            _ism.total_slots() >= 2
            and _ism.total_slots() - _ism.in_use() >= 1
            and len(_groups) == 1
            and list(_groups.keys())[0] == _original_model
        )
        if _bg_eligible and _original_model is not None:
            # Only check profile model when we know the active model;
            # if _original_model is None we can't compare, so allow bg.
            _all_profiles = [_load_agent_profile(_a.get("system_prompt", "")) for _a in batch_args]
            for _ba, _bp in zip(batch_args, _all_profiles):
                _pm = _ba.get("model") or _bp.get("model")
                if _pm and _pm != _original_model:
                    _bg_eligible = False
                    break

        if _bg_eligible:
            # Dispatch as background asyncio Tasks — return placeholders immediately.
            # Each task acquires its own ISM slot on start, releases on finish.
            for _i, (_tc, _args) in enumerate(zip(batch, batch_args)):
                _lbl = f"Agent {_i + 1}" if len(batch) > 1 else ""
                _bg_task = _args.get("task", "")
                if not _bg_task or not str(_bg_task).strip():
                    _err = "[error: spawn_agent called with empty task — agent not started]"
                    self.messages.append({"role": "tool", "tool_call_id": _tc["id"], "content": _err})
                    await emit_fn("spawn_agent", _tc["id"], _err)
                    continue
                _placeholder = "[background: agent dispatched — result pending]"
                self.messages.append({"role": "tool", "tool_call_id": _tc["id"], "content": _placeholder})
                await emit_fn("spawn_agent", _tc["id"], _placeholder)
                _task = asyncio.create_task(
                    self._run_background_agent(_tc, _args, _lbl, _original_model)
                )
                # Remove from list when the task is truly done (asyncio marks it done
                # only after the coroutine fully returns — not during finally block).
                _task.add_done_callback(
                    lambda t: self._bg_agent_tasks.remove(t)
                    if t in self._bg_agent_tasks else None
                )
                self._bg_agent_tasks.append(_task)
                self._pending_bg_tool_calls.append(_tc)
            return  # no model switch occurred; no try/finally restore needed

        _current_running = _original_model
        _attempted_switch = False
        _label_offset = 0
        _pair_results: list = []

        # On a single-slot server Eli holds the only slot during send_and_stream.
        # Release it now so inline agents can acquire it; re-acquire in finally.
        _single_slot = _ism.total_slots() == 1
        if _single_slot and self._eli_slot is not None:
            await self._eli_slot.release()
            self._eli_slot = None

        try:
            for _target_model, _group in _ordered:
                # Switch model if needed (only when original is known)
                if _target_model and _target_model != _current_running and _original_model:
                    _attempted_switch = True
                    _ok = await _switch_server(_target_model)
                    if not _ok:
                        for _tc, _args in _group:
                            _pair_results.append((_tc["id"], f"[error: server failed to start model '{_target_model}' — agent skipped]"))
                        _label_offset += len(_group)
                        continue
                    _current_running = _target_model
                    await _ism.refresh_from_server()   # update slot count for new model

                # Slot count for parallelism decision
                _slots = _ism.total_slots()

                # Domain conflict check
                _profiles = [_load_agent_profile(_a.get("system_prompt", "")) for _, _a in _group]
                _run_parallel = _slots >= 2 and len(_group) > 1 and _all_can_parallel(_profiles)
                _hint = _target_model  # suppresses per-agent switch/restore

                if _run_parallel:
                    _labels = [f"Agent {_label_offset + i + 1}" for i in range(len(_group))]
                    async def _run_one_parallel(_tc, _args, _label, _h=_hint):
                        _res = await self._tool_spawn_agent(
                            _args.get("system_prompt", ""),
                            _args.get("task", ""),
                            _args.get("tools"),
                            _args.get("think_level"),
                            min(_args.get("max_iterations", 60), 60),
                            _args.get("model"),
                            agent_label=_label,
                            current_model_hint=_h,
                            _tool_id=_tc["id"],
                        )
                        return _tc["id"], _res
                    _raw_results = await asyncio.gather(*[
                        _run_one_parallel(_tc, _args, _lbl)
                        for (_tc, _args), _lbl in zip(_group, _labels)
                    ], return_exceptions=True)
                    # Convert any exception objects into error strings so one dead
                    # agent doesn't cancel the others or crash the parent turn.
                    _group_results = []
                    for (_tc, _args), _r in zip(_group, _raw_results):
                        if isinstance(_r, BaseException):
                            _err = f"[agent died: {type(_r).__name__}: {_r}]"
                            log.warning("Parallel agent crashed: %s", _r)
                            _group_results.append((_tc["id"], _err))
                        else:
                            _group_results.append(_r)
                    _pair_results.extend(_group_results)
                else:
                    for _i, (_tc, _args) in enumerate(_group):
                        _lbl = f"Agent {_label_offset + _i + 1}" if len(batch) > 1 else ""
                        _res = await self._tool_spawn_agent(
                            _args.get("system_prompt", ""),
                            _args.get("task", ""),
                            _args.get("tools"),
                            _args.get("think_level"),
                            min(_args.get("max_iterations", 60), 60),
                            _args.get("model"),
                            agent_label=_lbl,
                            current_model_hint=_hint,
                            _tool_id=_tc["id"],
                        )
                        _pair_results.append((_tc["id"], _res))

                _label_offset += len(_group)

        finally:
            # Always restore original model if any switch was attempted.
            # Fires on normal exit, exception, and asyncio cancellation.
            if _attempted_switch and _original_model:
                await _switch_server(_original_model)
            # Re-acquire Eli's slot so send_and_stream can release it at turn end.
            if _single_slot:
                self._eli_slot = await _ism.acquire(
                    "Eli", timeout_secs=None, bypass_capacity=True)

        for (_tc_id, _result), _tc in zip(_pair_results, batch):
            self.messages.append({"role": "tool", "tool_call_id": _tc_id, "content": _result})
            await emit_fn("spawn_agent", _tc_id, _result)

    async def _tool_spawn_agent(
        self,
        system_prompt: str,
        task: str,
        tools: list[str] | None = None,
        think_level: str | None = None,
        max_iterations: int = 60,
        model: str | None = None,
        agent_label: str = "",
        current_model_hint: str | None = None,
        _is_background: bool = False,
        _tool_id: str = "",
    ) -> str:
        """Run an isolated sub-agent loop and return its final text response.

        agent_label: display label for the Agent tab (e.g. "Agent 1"). Empty = unlabeled.
        current_model_hint: pre-fetched active profile name; skips _find_active_profile() call.
        """
        from chat import stream_events, _try_parse_text_tool_calls, _tool_announce, _NullLive, COMPACT_QUOTES

        # Increment depth immediately before any await so parallel gather is safe
        self._subagent_depth += 1

        # Resolve profile name → system prompt, and auto-extract recommended model
        if system_prompt and " " not in system_prompt.strip():
            profile = _load_agent_profile(system_prompt)
            system_prompt = profile["prompt"]
            if not model:
                model = profile.get("model")
        # If not found, use the string as-is (may be a short raw prompt)

        # Build tool list — always exclude spawn_agent from sub-agents
        sub_tools = [t for t in TOOLS if t["function"]["name"] != "spawn_agent"]
        if tools:
            sub_tools = [t for t in sub_tools if t["function"]["name"] in tools]

        think = think_level or self.think_level
        max_iter = min(max_iterations, 60)

        # ── Model switch ──────────────────────────────────────────────────────
        restore_profile: str | None = None
        if model:
            commands = _load_commands()
            if model not in commands:
                available = "  ·  ".join(commands) or "(none)"
                if not self.tui_queue:
                    console.print(f"[dim yellow]⚠ unknown model '{model}' — using current model. Available: {available}[/dim yellow]")
                model = None
            if model:
                # Use hint from parallel batch if provided (avoids redundant API call)
                active = current_model_hint if current_model_hint is not None else await _find_active_profile()
                if active == model:
                    restore_profile = None  # Already on right model
                    if not self.tui_queue:
                        console.print(f"[dim]  Model already loaded: {model}[/dim]")
                else:
                    restore_profile = active
                    if not self.tui_queue:
                        console.print(Panel(
                            f"[yellow]Switching server to:[/yellow] {model}\n"
                            f"[dim]Will restore '{restore_profile or 'original'}' after agent finishes.[/dim]",
                            title="[yellow]Model Switch[/yellow]",
                            border_style="yellow",
                        ))
                    ready = await _switch_server(model)
                    if not ready:
                        self._subagent_depth -= 1
                        return f"[error: server failed to start model '{model}' — agent aborted]"

        import datetime as _dt
        _today = _dt.date.today().strftime("%Y-%m-%d")
        _ctx = (
            f"\n\n[Session Context]\n"
            f"Today's date: {_today}\n"
            f"Current working directory: {self.cwd}\n"
            f"All relative file paths resolve against this directory."
        )
        if not task or not task.strip():
            self._subagent_depth -= 1
            return "[error: spawn_agent called with empty task — agent not started]"

        _TASK_CHAR_LIMIT = 40_000  # ~10k tokens — tasks should be instructions, not data dumps
        if len(task) > _TASK_CHAR_LIMIT:
            self._subagent_depth -= 1
            return (
                f"[error: task string is {len(task):,} chars (~{len(task)//4:,} tokens) which exceeds the "
                f"{_TASK_CHAR_LIMIT:,}-char limit. Pass instructions only — never embed file contents in a task. "
                f"The agent can read files itself.]"
            )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt + _ctx},
            {"role": "user", "content": task},
        ]

        _label_prefix = f"[{agent_label}] " if agent_label else ""
        if self.tui_queue:
            await self.tui_queue.put({"type": "system", "text": f"{_label_prefix}Agent: {task}", "agent_label": agent_label})
        elif self.compact_mode:
            quote = random.choice(COMPACT_QUOTES)
            console.print(f"[dim cyan]  ◌ {quote}[/dim cyan]")
        else:
            title_suffix = f" [{agent_label}]" if agent_label else ""
            console.print(Panel(
                f"[bold cyan]Task:[/bold cyan] {task[:300]}{'...' if len(task) > 300 else ''}",
                title=f"[cyan]Sub-Agent Spawned{title_suffix}[/cyan]",
                border_style="cyan",
            ))

        # Acquire inference slot — inline agents use no timeout; background agents use 15 min.
        _slot_label = (f"{'Background' if _is_background else 'In-Line'} Agent"
                       + (f" [{agent_label}]" if agent_label else ""))
        _slot = await _ism.acquire(_slot_label, timeout_secs=2700.0 if _is_background else None)
        if _is_background:
            _slot.task = asyncio.current_task()
        if self.tui_queue:
            await self.tui_queue.put({"type": "system",
                "text": f"{_slot_label} using Slot {_slot.index + 1}"})

        final_text = ""
        _hit_max_iter = True  # cleared when agent breaks naturally
        try:
            for _iter in range(max_iter):
                temperature = 0.3 if think == "deep" else 0.6
                think_kwargs: dict = {}
                if self.backend == "llamacpp":
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
                _usage_data: dict = {}

                async with self.client.stream(
                    "POST",
                    f"{BASE_URL}/v1/chat/completions",
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        raise RuntimeError(f"HTTP {response.status_code}: {body.decode('utf-8', errors='replace')[:600]}")
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
                                    await self.tui_queue.put({"type": "text_token", "text": data, "source": "agent", "agent_label": agent_label})
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
                            elif event_type == "usage":
                                _usage_data = data
                            elif event_type == "stop":
                                if self.tui_queue:
                                    await self.tui_queue.put({"type": "text_done", "text": text_buf, "source": "agent", "agent_label": agent_label})
                                elif text_buf:
                                    live.update(Panel(
                                        Markdown(text_buf),
                                        title="[cyan]Agent[/cyan]",
                                        border_style="cyan",
                                    ))
                                else:
                                    live.update(Text(""))

                # Emit agent context usage and check for approaching limit
                if _usage_data:
                    _prompt_toks = _usage_data.get("prompt_tokens", 0)
                    _ctx = self.ctx_window
                    if self.tui_queue:
                        await self.tui_queue.put({"type": "usage", "tokens": _usage_data.get("total_tokens", 0), "ctx": _ctx, "agent_label": agent_label, "tool_id": _tool_id, "slot_index": _slot.index})
                    _pct = _prompt_toks / _ctx if _ctx else 0
                    _warn_msg = None
                    if _pct >= 0.92:
                        _warn_msg = f"[{agent_label}] Context at {_pct:.0%} ({_prompt_toks:,} / {_ctx:,} tokens) — stopping agent to avoid server crash."
                        log.warning("Agent context limit: %s", _warn_msg)
                        if self.tui_queue:
                            await self.tui_queue.put({"type": "system", "text": _warn_msg})
                        final_text = (final_text or "") + f"\n\n[Agent stopped: context {_pct:.0%} full — report based on work completed so far.]"
                        break
                    elif _pct >= 0.75:
                        _warn_msg = f"[{agent_label}] Context at {_pct:.0%} ({_prompt_toks:,} / {_ctx:,} tokens)"
                        if self.tui_queue:
                            await self.tui_queue.put({"type": "system", "text": _warn_msg})
                        else:
                            console.print(f"[yellow]{_warn_msg}[/yellow]")

                if assistant_content:          # keep last meaningful text; don't overwrite with ""
                    final_text = assistant_content

                # Fallback: model emitted tool calls as text
                if not tool_calls_received and assistant_content:
                    _parsed = _try_parse_text_tool_calls(assistant_content)
                    if _parsed:
                        tool_calls_received = _parsed
                        assistant_content = ""

                # Auto-announce if model produced no text before first tool call (TUI only)
                if tool_calls_received and not text_buf.strip() and not self.tui_queue:
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
                        tc_args = normalize_tool_args(tc_args)
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
                            prefix="Sub-Agent — ", session_rules=self.session_rules,
                            cwd=self.cwd,
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
                        # File write lock check for structured write tools
                        _ag_wl_path = _extract_write_path(tc_name, tc_args)
                        _ag_wl_abs = os.path.abspath(_ag_wl_path) if _ag_wl_path else None
                        if _ag_wl_abs and _ag_wl_abs in self._write_locks:
                            tc_result = f"[error: '{os.path.basename(_ag_wl_abs)}' is currently locked for writing by {self._write_locks[_ag_wl_abs]} — retry after it finishes]"
                        else:
                            if _ag_wl_abs:
                                self._write_locks[_ag_wl_abs] = agent_label or "agent"
                            try:
                                tc_result = await self._dispatch_tool(tc_name, tc_args)
                            finally:
                                if _ag_wl_abs:
                                    self._write_locks.pop(_ag_wl_abs, None)
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
                                    **({"chat_template_kwargs": {"enable_thinking": False}} if self.backend == "llamacpp" else {}),
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
        except asyncio.CancelledError:
            raise  # let cancellation propagate so eviction works correctly
        except Exception as _iter_exc:
            # Catch network errors, HTTP failures, and any other crash so a dead
            # agent doesn't kill the parent turn.  The finally block still runs
            # to release the slot; the error string is returned to the parent
            # model as the agent result so it can decide whether to retry.
            _err_msg = f"[agent died: {type(_iter_exc).__name__}: {_iter_exc}]"
            log.warning("Agent[%s] crashed: %s", agent_label or "?", _iter_exc)
            if self.tui_queue:
                await self.tui_queue.put({"type": "system", "text": _err_msg})
            final_text = (final_text + "\n\n" if final_text else "") + _err_msg
            _hit_max_iter = False  # don't trigger the iteration-limit summary prompt
        finally:
            self._subagent_depth -= 1
            if self.tui_queue:
                await self.tui_queue.put({"type": "system",
                    "text": f"Releasing Slot {_slot.index + 1} ({_slot_label})"})
            await _slot.release()

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
                                    await self.tui_queue.put({"type": "text_token", "text": ev_data, "source": "agent", "agent_label": agent_label})
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
        from chat import stream_events, _try_parse_text_tool_calls, SESSIONS_DIR

        if not agent_specs:
            return "[error: queue_agents called with empty agents list]"

        # Local models sometimes emit the agents array as a JSON-encoded string instead of
        # an actual array. Detect and decode it before validation.
        if isinstance(agent_specs, str):
            import json as _json
            try:
                agent_specs = _json.loads(agent_specs)
            except Exception:
                return "[error: queue_agents 'agents' value is a string and could not be parsed as JSON. Pass a proper JSON array of objects.]"

        if not isinstance(agent_specs, list):
            return "[error: queue_agents 'agents' must be a JSON array of objects.]"

        # Defensive: local models sometimes emit strings instead of objects
        bad = [i for i, s in enumerate(agent_specs) if not isinstance(s, dict)]
        if bad:
            return (
                f"[error: queue_agents received non-object agent specs at position(s) {bad}. "
                "Each entry in 'agents' must be a JSON object with 'system_prompt' and 'task' keys.]"
            )

        # Validate models upfront — strip unknown ones rather than aborting
        commands = _load_commands()
        for spec in agent_specs:
            m = spec.get("model")
            if m and m not in commands:
                available = "  ·  ".join(commands) or "(none)"
                console.print(f"[dim yellow]  ⚠ unknown model '{m}' — using current model. Available: {available}[/dim yellow]")
                spec["model"] = None

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        _slug_raw = _re.sub(r'[<>:"/\\|?*]', '', label.lower()).replace(" ", "-").strip("-") if label else ""
        slug = _slug_raw[:32].strip("-") or "run"
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
            max_iter = min(int(spec.get("max_iterations", 60)), 60)
            think = spec.get("think_level") or self.think_level
            tools_wl = spec.get("tools")
            task = spec.get("task", "")
            if not task or not task.strip():
                results.append({
                    "index": idx, "system_prompt": spec.get("system_prompt", ""),
                    "task": "", "model": target_model,
                    "timeout_seconds": timeout_s, "status": "error",
                    "result": f"[error: queue_agents agent {agent_num}/{total} has empty task — skipped. Provide a non-empty task string.]",
                    "duration_seconds": 0.0,
                })
                if not self.tui_queue:
                    console.print(f"[red]  Agent {agent_num}/{total} skipped — empty task[/red]")
                continue
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

            _TASK_CHAR_LIMIT = 40_000
            if len(task) > _TASK_CHAR_LIMIT:
                results.append({
                    "index": idx, "system_prompt": spec.get("system_prompt", ""),
                    "task": task[:200], "model": target_model,
                    "timeout_seconds": timeout_s, "status": "error",
                    "result": (
                        f"[error: task string is {len(task):,} chars (~{len(task)//4:,} tokens) which exceeds the "
                        f"{_TASK_CHAR_LIMIT:,}-char limit. Pass instructions only — never embed file contents in a task. "
                        f"The agent can read files itself.]"
                    ),
                    "duration_seconds": 0.0,
                })
                if not self.tui_queue:
                    console.print(f"[red]  Agent {agent_num}/{total} skipped — task too large ({len(task):,} chars)[/red]")
                continue

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
            self._subagent_depth += 1
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
                    if self.backend == "llamacpp":
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

                    from chat import _NullLive
                    async with self.client.stream(
                        "POST", f"{BASE_URL}/v1/chat/completions",
                        json=payload, headers={"Accept": "text/event-stream"},
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            raise RuntimeError(f"HTTP {response.status_code}: {body.decode('utf-8', errors='replace')[:600]}")
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
                            tc_args = normalize_tool_args(tc_args)
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
                self._subagent_depth -= 1

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
