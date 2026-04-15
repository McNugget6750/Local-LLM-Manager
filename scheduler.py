"""
scheduler.py — Cron-like scheduled jobs daemon.

Runs as a single asyncio.Task inside the Qt event loop.
Jobs fire research agents and push results to Telegram.
"""
import asyncio
import json
import logging
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# ── Telegram helper (also used by tools.py send_telegram tool) ─────────────────

def _read_env() -> dict[str, str]:
    """Read telegram_bot/.env into a dict. Returns {} on any error."""
    env_path = Path(__file__).parent / "telegram_bot" / ".env"
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip().strip('"').strip("'")
    except Exception as e:
        log.warning("scheduler: failed to read .env: %s", e)
    return result


def _load_bot_token() -> str | None:
    """Read BOT_TOKEN from telegram_bot/.env."""
    return _read_env().get("BOT_TOKEN")


def _load_admin_id() -> int | None:
    """Read ADMIN_ID from telegram_bot/.env. Returns None if not set."""
    val = _read_env().get("ADMIN_ID")
    try:
        return int(val) if val else None
    except ValueError:
        return None


async def tg_send(user_id: int, text: str) -> str:
    """Send a Telegram message. Returns 'ok' or an error string."""
    token = _load_bot_token()
    if not token:
        return "[send_telegram error: BOT_TOKEN not found in telegram_bot/.env]"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Only split if the message exceeds Telegram's 4096-char limit
    limit = 4096
    chunks = [text[i:i+limit] for i in range(0, len(text), limit)] if len(text) > limit else [text]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for chunk in chunks:
                resp = await client.post(url, json={
                    "chat_id": user_id,
                    "text": chunk,
                })
                if resp.status_code != 200:
                    data = resp.json()
                    return f"[send_telegram error: {data.get('description', resp.status_code)}]"
        return f"Message sent to {user_id}"
    except Exception as e:
        return f"[send_telegram error: {e}]"


async def tg_send_approval(user_id: int, text: str, keyboard: list) -> int | None:
    """Send a message with an inline keyboard. Returns message_id or None on error."""
    token = _load_bot_token()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json={
                "chat_id": user_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            })
            if resp.status_code == 200:
                return resp.json()["result"]["message_id"]
    except Exception as e:
        log.warning("tg_send_approval error: %s", e)
    return None


async def tg_edit_message(user_id: int, message_id: int, text: str) -> None:
    """Edit the text of a previously sent message (best-effort, silently ignores errors)."""
    token = _load_bot_token()
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": user_id,
                "message_id": message_id,
                "text": text,
            })
    except Exception:
        pass


# ── Schedule parsing ───────────────────────────────────────────────────────────

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DEFAULT_TIME = "08:00"


def _parse_hhmm(s: str) -> tuple[int, int]:
    """Parse HH:MM — raises ValueError on bad input."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {s!r} (expected HH:MM)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Time out of range: {s!r}")
    return h, m


def _parse_when(s: str) -> tuple[str, str | None, str]:
    """
    Parse a 'when' string into (kind, day_or_date, hhmm).

    Formats:
      daily                   -> ('daily', None, '08:00')
      daily:14:30             -> ('daily', None, '14:30')
      weekly:mon              -> ('weekly', 'mon', '08:00')
      weekly:fri:09:00        -> ('weekly', 'fri', '09:00')
      2026-05-01              -> ('once', '2026-05-01', '08:00')
      2026-05-01:14:30        -> ('once', '2026-05-01', '14:30')
    """
    s = s.strip().lower()

    # daily
    if s == "daily":
        return ("daily", None, _DEFAULT_TIME)
    if s.startswith("daily:"):
        rest = s[len("daily:"):]
        h, m = _parse_hhmm(rest)
        return ("daily", None, f"{h:02d}:{m:02d}")

    # weekly
    if s.startswith("weekly:"):
        rest = s[len("weekly:"):]
        parts = rest.split(":", 1)
        day = parts[0]
        if day not in _DAYS:
            raise ValueError(f"Unknown weekday: {day!r}. Use mon/tue/wed/thu/fri/sat/sun")
        if len(parts) == 2:
            h, m = _parse_hhmm(parts[1])
            hhmm = f"{h:02d}:{m:02d}"
        else:
            hhmm = _DEFAULT_TIME
        return ("weekly", day, hhmm)

    # once: bare date or date:HH:MM
    # Try ISO date at the start: YYYY-MM-DD
    import re
    m_date = re.match(r'^(\d{4}-\d{2}-\d{2})(?::(.+))?$', s)
    if m_date:
        date_str = m_date.group(1)
        # Validate date
        datetime.strptime(date_str, "%Y-%m-%d")
        time_part = m_date.group(2)
        if time_part:
            h, m_val = _parse_hhmm(time_part)
            hhmm = f"{h:02d}:{m_val:02d}"
        else:
            hhmm = _DEFAULT_TIME
        return ("once", date_str, hhmm)

    raise ValueError(
        f"Invalid schedule format: {s!r}\n"
        "Examples: daily, daily:14:30, weekly:fri:09:00, 2026-05-01, 2026-05-01:14:30"
    )


def _compute_next_run(when_str: str, after: datetime) -> datetime | None:
    """Compute the next fire time for a 'when' canonical string, after the given datetime."""
    kind, day_or_date, hhmm = _parse_when(when_str)
    h, m = _parse_hhmm(hhmm)

    if kind == "daily":
        candidate = after.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    if kind == "weekly":
        target_dow = _DAYS[day_or_date]
        # Find next occurrence of target_dow
        days_ahead = (target_dow - after.weekday()) % 7
        candidate = (after + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    if kind == "once":
        dt = datetime.strptime(f"{day_or_date} {hhmm}", "%Y-%m-%d %H:%M")
        if dt <= after:
            return None  # already passed
        return dt

    return None


# ── SchedulerDaemon ────────────────────────────────────────────────────────────

class SchedulerDaemon:
    SCHEDULES_PATH = Path(__file__).parent / "schedules.json"

    def __init__(self, session) -> None:
        self._session = session
        self._task: asyncio.Task | None = None
        self._data: dict = {"version": 1, "jobs": []}
        self._running_jobs: set[str] = set()
        self._research_prompt: str | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._load()
        self._load_research_prompt()
        self._task = asyncio.create_task(self._run_loop(), name="scheduler-daemon")
        log.info("SchedulerDaemon started (%d job(s))", len(self._data["jobs"]))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save()
        log.info("SchedulerDaemon stopped")

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self.SCHEDULES_PATH.exists():
            try:
                self._data = json.loads(self.SCHEDULES_PATH.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("scheduler: failed to load schedules.json: %s", e)

    def _save(self) -> None:
        try:
            tmp = self.SCHEDULES_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.SCHEDULES_PATH)
        except Exception as e:
            log.warning("scheduler: failed to save schedules.json: %s", e)

    def _load_research_prompt(self) -> None:
        p = Path(__file__).parent / "skills" / "research.md"
        try:
            raw = p.read_text(encoding="utf-8")
            # Strip YAML frontmatter
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    raw = raw[end + 4:].lstrip()
            self._research_prompt = raw
        except Exception as e:
            log.warning("scheduler: failed to load research.md: %s", e)
            self._research_prompt = "Research the following topic thoroughly:\n$ARGS"

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                for job in list(self._data["jobs"]):
                    if not job.get("enabled", True):
                        continue
                    if job["id"] in self._running_jobs:
                        continue
                    next_run = job.get("next_run")
                    if not next_run:
                        continue
                    due = datetime.fromisoformat(next_run)
                    if due <= now:
                        asyncio.create_task(self._fire_job(job), name=f"job-{job['id']}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("scheduler loop error: %s", e)

    # ── Job firing ─────────────────────────────────────────────────────────────

    async def _fire_job(self, job: dict) -> None:
        job_id = job["id"]
        self._running_jobs.add(job_id)
        log.info("scheduler: firing job %s — %s", job_id, job["task"])
        try:
            # Build agent prompt
            task_desc = job["task"]
            prompt = (self._research_prompt or "Research: $ARGS").replace("$ARGS", task_desc)

            result = await self._session._tool_spawn_agent(
                system_prompt=prompt,
                task=task_desc,
                tools=None,         # all tools except spawn_agent
                think_level=None,   # inherit
                max_iterations=40,
                model=None,
            )

            # Push to Telegram
            tg_id = job["telegram_user_id"]
            header = f"[Scheduled report: {task_desc}]\n\n"
            send_result = await tg_send(tg_id, header + result)
            log.info("scheduler: job %s sent to Telegram %s — %s", job_id, tg_id, send_result)

            # Update job state
            job["run_count"] = job.get("run_count", 0) + 1
            job["last_run"] = datetime.now().isoformat(timespec="seconds")
            job["last_error"] = None

            # One-time jobs: disable after firing
            kind, _, _ = _parse_when(job["when"])
            if kind == "once":
                job["enabled"] = False
                job["next_run"] = None
            else:
                job["next_run"] = _compute_next_run(job["when"], datetime.now()).isoformat(timespec="seconds")

        except Exception as e:
            log.error("scheduler: job %s failed: %s", job_id, e)
            job["last_error"] = str(e)
        finally:
            self._running_jobs.discard(job_id)
            self._save()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def add_job(self, when: str, telegram_user_id: int, task: str) -> dict:
        """Parse when-string, compute next_run, persist, return new job dict."""
        # Validate when (raises ValueError on bad input)
        _parse_when(when)
        next_run_dt = _compute_next_run(when, datetime.now())
        job = {
            "id": secrets.token_hex(2),
            "enabled": True,
            "when": when,
            "telegram_user_id": telegram_user_id,
            "task": task,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "last_run": None,
            "next_run": next_run_dt.isoformat(timespec="seconds") if next_run_dt else None,
            "run_count": 0,
            "last_error": None,
        }
        self._data["jobs"].append(job)
        self._save()
        return job

    def remove_job(self, job_id: str) -> bool:
        before = len(self._data["jobs"])
        self._data["jobs"] = [j for j in self._data["jobs"] if j["id"] != job_id]
        if len(self._data["jobs"]) < before:
            self._save()
            return True
        return False

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        job = self.get_job(job_id)
        if job is None:
            return False
        job["enabled"] = enabled
        # Recompute next_run when re-enabling a non-once job
        if enabled:
            try:
                kind, _, _ = _parse_when(job["when"])
                if kind != "once":
                    nxt = _compute_next_run(job["when"], datetime.now())
                    job["next_run"] = nxt.isoformat(timespec="seconds") if nxt else None
            except Exception:
                pass
        self._save()
        return True

    def list_jobs(self) -> list[dict]:
        return list(self._data["jobs"])

    def get_job(self, job_id: str) -> dict | None:
        return next((j for j in self._data["jobs"] if j["id"] == job_id), None)
