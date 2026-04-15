"""
remote_chat.py — HTTP bridge: POST /chat → Eli GUI chat session.

Endpoints:
  POST /chat    {"message": "...", "plan": false}
                Blocks until turn complete (max 15 min).
                Returns {"ok": true, "text": "...", "agent_results": [...]}
                  text          — Eli's closing response
                  agent_results — list of raw agent output strings (may be empty)
                Returns {"ok": false, "error": "busy"} if a turn is in progress.

  GET  /status  Returns {"busy": bool, "port": 1237}

Usage from curl (always pass --max-time to avoid client-side timeout):
  curl -s --max-time 960 -X POST http://localhost:1237/chat \\
    -H "Content-Type: application/json" \\
    -d '{"message": "Please review qt/window.py"}'
"""
import asyncio
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

PORT    = 1237
TIMEOUT = 900   # 15 minutes server-side; use --max-time 960 on the curl side


class RemoteChatServer:
    def __init__(self, adapter):
        self._adapter       = adapter
        self._lock          = threading.Lock()   # prevents concurrent remote calls
        self._event         = threading.Event()  # set when 'done' fires
        self._eli_text      = ""                 # captured from text_done (Eli's reply)
        self._agent_results: list[str] = []      # captured from tool_done for agent tools
        self._telegram_uid: int | None = None    # set when request came via Telegram bot
        self._busy               = False
        self.mirror_enabled      = True          # mirror all Eli replies to ADMIN_ID
        self._had_text_tokens    = False         # True only when real LLM tokens arrived
        self._pending_approval   = False         # True while waiting for tool approval
        self._approval_rule      = ""            # session-allow rule for the pending approval
        self._approval_admin_id: int | None = None
        self._approval_tg_mid:   int | None = None  # message_id of the TG approval prompt
        self._tg_approval:       tuple | None = None  # (approved, notes) set by /approve, consumed by QTimer

        self._server = ThreadingHTTPServer(("127.0.0.1", PORT), self._make_handler())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="RemoteChatHTTP",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # server.shutdown() blocks until serve_forever() returns. Run it in a
        # thread so closing the app doesn't hang while a long request is in flight.
        threading.Thread(target=self._server.shutdown, daemon=True).start()

    # ── Signal callbacks (called from adapter's asyncio/Qt thread) ────────────

    def on_text_token(self, _token: str) -> None:
        self._had_text_tokens = True

    def on_text_done(self, text: str) -> None:
        self._eli_text = text

    def on_tool_done(self, tool_id: str, name: str, result: str, is_error: bool) -> None:
        """Capture agent output and immediately mirror it to ADMIN_ID."""
        if name in ("spawn_agent", "queue_agents") and result.strip() and not is_error:
            self._agent_results.append(result)
            if self.mirror_enabled:
                from scheduler import tg_send, _load_admin_id
                admin_id = _load_admin_id()
                if admin_id and not (self._busy and self._telegram_uid == admin_id):
                    text = f"[Agent]\n{result}"
                    threading.Thread(
                        target=lambda: asyncio.run(tg_send(admin_id, text)),
                        daemon=True,
                    ).start()

    def on_approval_needed(self, title: str, message: str, tool_name: str, args_str: str) -> None:
        """Send a Telegram approval prompt to ADMIN_ID with inline buttons."""
        from scheduler import tg_send_approval, _load_admin_id
        admin_id = _load_admin_id()
        if not admin_id:
            return
        # Compute session-allow rule (mirrors window.py logic, minus CWD path_prefix)
        import json as _json, os as _os
        try:
            args = _json.loads(args_str) if args_str else {}
        except Exception:
            args = {}
        cmd = args.get("command", "")
        if tool_name == "bash" and cmd:
            first = cmd.strip().split()[0]
            self._approval_rule = f"cmd_pattern:{first}*"
        elif tool_name in ("edit", "write_file"):
            path = args.get("path", "")
            self._approval_rule = f"path_prefix:{_os.path.dirname(path)}" if path else f"tool:{tool_name}"
        else:
            self._approval_rule = f"tool:{tool_name}" if tool_name else ""

        # Build message text
        detail = cmd if (tool_name == "bash" and cmd) else args.get("path", args_str or "")
        tg_text = (
            f"⚠️ Approval Required\n\n"
            f"Tool: {tool_name}\n"
            + (f"{detail[:300]}\n" if detail else "")
            + f"\n{message[:600]}"
        )
        keyboard = [[
            {"text": "✅ Allow once",    "callback_data": "approve:1"},
            {"text": "🔒 Allow session", "callback_data": "approve:2"},
            {"text": "❌ Deny",          "callback_data": "approve:3"},
        ]]
        self._pending_approval = True
        self._approval_admin_id = admin_id
        self._approval_tg_mid = None

        def _send():
            import asyncio as _aio
            mid = _aio.run(tg_send_approval(admin_id, tg_text, keyboard))
            self._approval_tg_mid = mid

        threading.Thread(target=_send, daemon=True).start()

    def on_approval_resolved(self) -> None:
        """Called when approval is resolved (from GUI or Telegram) — clean up state."""
        if not self._pending_approval:
            return
        self._pending_approval = False
        admin_id = self._approval_admin_id
        mid = self._approval_tg_mid
        if admin_id and mid:
            from scheduler import tg_edit_message
            threading.Thread(
                target=lambda: asyncio.run(tg_edit_message(admin_id, mid, "✅ Approval resolved.")),
                daemon=True,
            ).start()
        self._approval_admin_id = None
        self._approval_tg_mid = None
        self._approval_rule = ""

    def on_done(self) -> None:
        self._push_to_admin()
        self._had_text_tokens = False
        self._event.set()

    def _push_to_admin(self) -> None:
        """Mirror Eli's reply + agent results to ADMIN_ID via Telegram (fire-and-forget).

        Only pushes when real LLM tokens were streamed (suppresses slash-command noise)
        or when agent results are present.
        """
        if not self.mirror_enabled:
            return
        from scheduler import tg_send, _load_admin_id
        admin_id = _load_admin_id()
        if not admin_id:
            return
        # If an HTTP request is still live it will deliver via the normal bot path —
        # skip the push only for that turn when the sender is already ADMIN_ID.
        if self._busy and self._telegram_uid == admin_id:
            return
        # Only push if real LLM tokens were streamed (not slash-command output)
        if not (self._eli_text and self._had_text_tokens):
            return
        _text = self._eli_text
        threading.Thread(
            target=lambda: asyncio.run(tg_send(admin_id, _text)),
            daemon=True,
        ).start()

    # ── HTTP handler ─────────────────────────────────────────────────────────

    def _make_handler(self):
        srv = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # silence default request log
                pass

            def _send_json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    with srv._lock:
                        busy = srv._busy
                    self._send_json(200, {
                        "busy": busy,
                        "port": PORT,
                        "pending_approval": srv._pending_approval,
                    })
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path == "/approve":
                    length = int(self.headers.get("Content-Length", 0))
                    try:
                        body = json.loads(self.rfile.read(length))
                    except Exception:
                        self._send_json(400, {"error": "invalid JSON"})
                        return
                    if not srv._pending_approval:
                        self._send_json(200, {"ok": False, "reason": "no pending approval"})
                        return
                    response = str(body.get("response", "")).strip()
                    if response == "1":
                        approved, notes = True, ""
                    elif response == "2":
                        approved, notes = True, (f"session_allow:{srv._approval_rule}"
                                                  if srv._approval_rule else "")
                    elif response == "3":
                        approved, notes = False, ""
                    else:
                        self._send_json(400, {"error": "response must be '1', '2', or '3'"})
                        return
                    import sys, threading as _thr
                    print(f"[/approve] received approved={approved} notes={notes!r} thread={_thr.current_thread().name!r}", flush=True, file=sys.stderr)
                    print(f"[/approve] pending_future={srv._adapter._pending_future}", flush=True, file=sys.stderr)
                    # Store for the main-thread QTimer to consume; also call resolve_approval
                    # so the asyncio future is resolved via call_soon_threadsafe.
                    srv._tg_approval = (approved, notes)
                    print(f"[/approve] _tg_approval set to {srv._tg_approval!r}", flush=True, file=sys.stderr)
                    srv._adapter.resolve_approval(approved, notes)
                    print(f"[/approve] resolve_approval() called", flush=True, file=sys.stderr)
                    self._send_json(200, {"ok": True})
                    return

                if self.path != "/chat":
                    self._send_json(404, {"error": "not found"})
                    return

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self._send_json(400, {"error": "invalid JSON"})
                    return

                raw     = str(body.get("message", "")).strip()
                plan    = bool(body.get("plan", False))

                # Extract [TELEGRAM_REQUEST from user_id=N] wrapper if present.
                # The inner message is passed to Eli clean; routing/delivery is
                # handled here — Eli doesn't need to know about Telegram at all.
                telegram_user_id: int | None = None
                message = raw
                tg_match = re.search(
                    r'\[TELEGRAM_REQUEST from user_id=(\d+)\]\s*(.*?)\s*\[/TELEGRAM_REQUEST\]',
                    raw, re.DOTALL,
                )
                if tg_match:
                    telegram_user_id = int(tg_match.group(1))
                    message = tg_match.group(2).strip()

                if not message:
                    self._send_json(400, {"error": "empty message"})
                    return

                if not srv._lock.acquire(blocking=False):
                    self._send_json(503, {"ok": False, "error": "busy"})
                    return

                try:
                    srv._busy             = True
                    srv._eli_text         = ""
                    srv._agent_results    = []
                    srv._telegram_uid     = telegram_user_id
                    srv._had_text_tokens  = False
                    srv._event.clear()

                    if message.startswith("/"):
                        # Slash command — execute directly; captured output feeds text_done
                        srv._adapter.submit_slash(message)
                    else:
                        # Normal message — prepend Telegram identity so Eli knows the sender
                        eli_message = (
                            f"[Telegram · user_id={telegram_user_id}] {message}"
                            if telegram_user_id else message
                        )
                        # Show the clean message in the GUI bubble, not the tagged version
                        srv._adapter.remote_message.emit(message)
                        srv._adapter.submit(eli_message, plan)

                    finished = srv._event.wait(timeout=TIMEOUT)
                    if not finished:
                        self._send_json(504, {"ok": False, "error": "timeout"})
                        return

                    self._send_json(200, {
                        "ok":            True,
                        "text":          srv._eli_text,
                        "agent_results": srv._agent_results,
                    })
                finally:
                    srv._busy = False
                    srv._lock.release()

        return _Handler
