"""
Unit tests for scheduler.py — _parse_when, _compute_next_run, CRUD, _load_bot_token.
"""
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scheduler import (
    SchedulerDaemon,
    _compute_next_run,
    _load_bot_token,
    _parse_when,
)


# ── _parse_when ────────────────────────────────────────────────────────────────

class TestParseWhen:
    def test_daily_bare(self):
        assert _parse_when("daily") == ("daily", None, "08:00")

    def test_daily_with_time(self):
        assert _parse_when("daily:14:30") == ("daily", None, "14:30")

    def test_weekly_day_only(self):
        assert _parse_when("weekly:mon") == ("weekly", "mon", "08:00")

    def test_weekly_day_with_time(self):
        assert _parse_when("weekly:fri:09:00") == ("weekly", "fri", "09:00")

    def test_once_date_only(self):
        assert _parse_when("2026-05-01") == ("once", "2026-05-01", "08:00")

    def test_once_date_with_time(self):
        assert _parse_when("2026-05-01:14:30") == ("once", "2026-05-01", "14:30")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_when("garbage")

    def test_invalid_time_raises(self):
        with pytest.raises(ValueError):
            _parse_when("daily:25:00")

    def test_invalid_weekday_raises(self):
        with pytest.raises(ValueError):
            _parse_when("weekly:xyz")


# ── _compute_next_run ─────────────────────────────────────────────────────────

class TestComputeNextRun:
    def _dt(self, s: str) -> datetime:
        return datetime.fromisoformat(s)

    def test_daily_future_today(self):
        after = self._dt("2026-05-01T07:00:00")
        nxt = _compute_next_run("daily:08:00", after)
        assert nxt == self._dt("2026-05-01T08:00:00")

    def test_daily_same_minute_rolls_to_tomorrow(self):
        after = self._dt("2026-05-01T08:00:01")
        nxt = _compute_next_run("daily:08:00", after)
        assert nxt == self._dt("2026-05-02T08:00:00")

    def test_weekly_same_dow_future(self):
        # 2026-05-01 is a Friday (weekday 4)
        after = self._dt("2026-05-01T07:00:00")
        nxt = _compute_next_run("weekly:fri:09:00", after)
        assert nxt == self._dt("2026-05-01T09:00:00")

    def test_weekly_same_dow_past_today_rolls_7_days(self):
        after = self._dt("2026-05-01T10:00:00")
        nxt = _compute_next_run("weekly:fri:09:00", after)
        assert nxt == self._dt("2026-05-08T09:00:00")

    def test_weekly_different_dow(self):
        # 2026-05-01 is Friday; next Monday is 2026-05-04
        after = self._dt("2026-05-01T12:00:00")
        nxt = _compute_next_run("weekly:mon:08:00", after)
        assert nxt == self._dt("2026-05-04T08:00:00")

    def test_once_future(self):
        after = self._dt("2026-04-01T00:00:00")
        nxt = _compute_next_run("2026-05-01:14:30", after)
        assert nxt == self._dt("2026-05-01T14:30:00")

    def test_once_past_returns_none(self):
        after = self._dt("2026-06-01T00:00:00")
        nxt = _compute_next_run("2026-05-01", after)
        assert nxt is None


# ── _load_bot_token ────────────────────────────────────────────────────────────

class TestLoadBotToken:
    def test_reads_token_from_env_file(self):
        # Write the .env in the real location and restore it afterward
        real_env = Path(__file__).parent.parent / "telegram_bot" / ".env"
        env_bak = real_env.read_bytes() if real_env.exists() else None
        try:
            real_env.parent.mkdir(parents=True, exist_ok=True)
            real_env.write_text("BOT_TOKEN=testtoken999\nOTHER=val\n", encoding="utf-8")
            token = _load_bot_token()
            assert token == "testtoken999"
        finally:
            if env_bak is not None:
                real_env.write_bytes(env_bak)
            elif real_env.exists():
                real_env.unlink()

    def test_missing_file_returns_none(self, tmp_path):
        # Ensure no .env exists by checking the actual path
        env_path = Path(__file__).parent.parent / "telegram_bot" / ".env"
        if not env_path.exists():
            assert _load_bot_token() is None


# ── SchedulerDaemon CRUD ──────────────────────────────────────────────────────

class TestSchedulerDaemonCRUD:
    def _make_daemon(self, tmp_path):
        session = MagicMock()
        daemon = SchedulerDaemon(session)
        daemon.SCHEDULES_PATH = tmp_path / "schedules.json"
        daemon._load()
        return daemon

    def test_add_job(self, tmp_path):
        d = self._make_daemon(tmp_path)
        job = d.add_job("daily:09:00", 123456, "research AI news")
        assert job["when"] == "daily:09:00"
        assert job["telegram_user_id"] == 123456
        assert job["task"] == "research AI news"
        assert job["enabled"] is True
        assert job["next_run"] is not None
        assert len(job["id"]) == 4  # token_hex(2) → 4 hex chars

    def test_add_job_persists(self, tmp_path):
        d = self._make_daemon(tmp_path)
        d.add_job("daily", 111, "task")
        data = json.loads(d.SCHEDULES_PATH.read_text())
        assert len(data["jobs"]) == 1

    def test_remove_job(self, tmp_path):
        d = self._make_daemon(tmp_path)
        job = d.add_job("daily", 111, "task")
        assert d.remove_job(job["id"]) is True
        assert d.get_job(job["id"]) is None

    def test_remove_nonexistent(self, tmp_path):
        d = self._make_daemon(tmp_path)
        assert d.remove_job("zzzz") is False

    def test_set_enabled_false(self, tmp_path):
        d = self._make_daemon(tmp_path)
        job = d.add_job("daily", 111, "task")
        assert d.set_enabled(job["id"], False) is True
        assert d.get_job(job["id"])["enabled"] is False

    def test_set_enabled_true_recomputes_next_run(self, tmp_path):
        d = self._make_daemon(tmp_path)
        job = d.add_job("daily", 111, "task")
        d.set_enabled(job["id"], False)
        job_ref = d.get_job(job["id"])
        job_ref["next_run"] = None  # simulate stale
        d._save()
        d.set_enabled(job["id"], True)
        assert d.get_job(job["id"])["next_run"] is not None

    def test_list_jobs(self, tmp_path):
        d = self._make_daemon(tmp_path)
        d.add_job("daily", 111, "task A")
        d.add_job("weekly:fri", 222, "task B")
        jobs = d.list_jobs()
        assert len(jobs) == 2

    def test_add_invalid_when_raises(self, tmp_path):
        d = self._make_daemon(tmp_path)
        with pytest.raises(ValueError):
            d.add_job("badformat", 111, "task")
