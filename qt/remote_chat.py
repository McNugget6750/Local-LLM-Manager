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
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT    = 1237
TIMEOUT = 900   # 15 minutes server-side; use --max-time 960 on the curl side


class RemoteChatServer:
    def __init__(self, adapter):
        self._adapter       = adapter
        self._lock          = threading.Lock()   # prevents concurrent remote calls
        self._event         = threading.Event()  # set when 'done' fires
        self._eli_text      = ""                 # captured from text_done (Eli's reply)
        self._agent_results: list[str] = []      # captured from tool_done for agent tools
        self._busy          = False

        self._server = HTTPServer(("127.0.0.1", PORT), self._make_handler())
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

    def on_text_done(self, text: str) -> None:
        self._eli_text = text

    def on_tool_done(self, tool_id: str, name: str, result: str, is_error: bool) -> None:
        """Capture agent output so the caller gets the full review, not just Eli's summary."""
        if name in ("spawn_agent", "queue_agents") and result.strip() and not is_error:
            self._agent_results.append(result)

    def on_done(self) -> None:
        self._event.set()

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
                    self._send_json(200, {"busy": busy, "port": PORT})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/chat":
                    self._send_json(404, {"error": "not found"})
                    return

                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length))
                except Exception:
                    self._send_json(400, {"error": "invalid JSON"})
                    return

                message = str(body.get("message", "")).strip()
                plan    = bool(body.get("plan", False))

                if not message:
                    self._send_json(400, {"error": "empty message"})
                    return

                if not srv._lock.acquire(blocking=False):
                    self._send_json(503, {"ok": False, "error": "busy"})
                    return

                try:
                    srv._busy          = True
                    srv._eli_text      = ""
                    srv._agent_results = []
                    srv._event.clear()

                    if message.startswith("/"):
                        # Slash command — route through submit_slash, no bubble
                        srv._adapter.submit_slash(message)
                    else:
                        # Normal message — show Remote bubble in GUI, then submit
                        srv._adapter.remote_message.emit(message)
                        srv._adapter.submit(message, plan)

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
