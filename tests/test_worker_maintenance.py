"""Tests for worker._check_maintenance: overdue detection against
_state["maintenance"], the once-per-transition notify pattern (mirrors
_check_watchdog's `was_stale` flag via `_notified_overdue`), and the
generic notification_webhook forwarding via notify.notify().
"""

import datetime

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


@pytest.mark.asyncio
async def test_check_maintenance_noop_when_nothing_overdue(worker_module):
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": worker_module._now_iso(),
        "_notified_overdue": False,
    }]
    await worker_module._check_maintenance()
    assert worker_module._state["maintenance"][0]["_notified_overdue"] is False
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("overdue" in m for m in logs)


@pytest.mark.asyncio
async def test_check_maintenance_logs_and_sets_flag_when_overdue(worker_module):
    long_ago = (worker_module._utcnow() - datetime.timedelta(days=100)).isoformat()
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": long_ago,
        "_notified_overdue": False,
    }]
    await worker_module._check_maintenance()
    item = worker_module._state["maintenance"][0]
    assert item["_notified_overdue"] is True
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Maintenance overdue: Filters" in m for m in logs)


@pytest.mark.asyncio
async def test_check_maintenance_does_not_relog_already_notified_item(worker_module):
    long_ago = (worker_module._utcnow() - datetime.timedelta(days=100)).isoformat()
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": long_ago,
        "_notified_overdue": True,  # already notified on a prior cycle
    }]
    await worker_module._check_maintenance()
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("overdue" in m for m in logs)  # no duplicate log entry


@pytest.mark.asyncio
async def test_check_maintenance_clears_flag_once_no_longer_overdue(worker_module):
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": worker_module._now_iso(),
        "_notified_overdue": True,  # was overdue before, item was just completed
    }]
    await worker_module._check_maintenance()
    assert worker_module._state["maintenance"][0]["_notified_overdue"] is False


@pytest.mark.asyncio
async def test_check_maintenance_forwards_to_notification_webhook(worker_module, mocker):
    worker_module._state["settings"]["notification_webhook"] = "https://example.com/hook"
    calls = []

    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(200)

    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    long_ago = (worker_module._utcnow() - datetime.timedelta(days=100)).isoformat()
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": long_ago,
        "_notified_overdue": False,
    }]
    await worker_module._check_maintenance()
    assert calls == ["https://example.com/hook"]


@pytest.mark.asyncio
async def test_check_maintenance_webhook_failure_does_not_raise(worker_module, mocker):
    worker_module._state["settings"]["notification_webhook"] = "https://example.com/hook"

    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")

    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    long_ago = (worker_module._utcnow() - datetime.timedelta(days=100)).isoformat()
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Filters", "trigger_type": "days",
        "interval_days": 30, "last_done_at": long_ago,
        "_notified_overdue": False,
    }]
    await worker_module._check_maintenance()  # should not raise
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Notification webhook failed" in m for m in logs)


@pytest.mark.asyncio
async def test_check_maintenance_runs_for_whole_house_items(worker_module):
    """device_host is None for whole-house items (e.g. annual service
    call) — status computation must not require a device."""
    long_ago = (worker_module._utcnow() - datetime.timedelta(days=400)).isoformat()
    worker_module._state["maintenance"] = [{
        "id": "m1", "name": "Annual service", "device_host": None,
        "trigger_type": "days", "interval_days": 365, "last_done_at": long_ago,
        "_notified_overdue": False,
    }]
    await worker_module._check_maintenance()
    assert worker_module._state["maintenance"][0]["_notified_overdue"] is True


@pytest.mark.asyncio
async def test_watchdog_forwards_offline_notification_to_webhook(worker_module, mocker, monkeypatch):
    """Device-offline notifications (via _check_watchdog) should also
    go through the same generic notification_webhook."""
    import datetime as real_datetime

    class FrozenDateTime(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 10, 0)

    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)
    worker_module._state["settings"]["notification_webhook"] = "https://example.com/hook"
    calls = []

    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(200)

    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    device = {
        "host": "ac1.local", "name": "Test", "watchdog_minutes": 5,
        "_last_seen": "2026-01-01T12:00:00", "_stale": False,
    }
    worker_module._state["device_state"]["ac1.local"] = {}
    await worker_module._check_watchdog(device)

    assert calls == ["https://example.com/hook"]
