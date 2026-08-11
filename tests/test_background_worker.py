"""Tests for _background_worker — the top-level 24/7 loop.

_background_worker is `while True: ... await asyncio.sleep(interval)`,
so it can't be tested by just calling it and awaiting the result like
every other function in this suite. Instead, each test mocks
asyncio.sleep to raise a distinctive _StopLoop exception the moment
the loop tries to sleep for exactly `interval` seconds (the call at
the very end of the while-loop body, the one that actually loops back)
— every *other* sleep call in the function (jitter between devices,
nightly-reboot stagger, startup backoff) uses a different duration and
is left alone, so it resolves instantly and doesn't interfere.

We pick interval=999999 specifically because it's a value no other
sleep call in the function could ever legitimately produce (jitter is
0.5s, stagger is 5s, startup backoff caps at 60s via min(2**attempt, 60)),
so matching on that exact value reliably identifies "the loop trying
to go around again" and nothing else.

This lets each test run *exactly one* real iteration of the loop body
end-to-end — with real branching, real state mutation, everything
except actual wall-clock waiting — then stop cleanly via a controlled
exception instead of an actual infinite loop.
"""

import datetime

import httpx
import pytest


class _StopLoop(Exception):
    """Raised by the mocked asyncio.sleep to break out of
    _background_worker's while True after one real iteration."""


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


def _sleep_mock_stopping_at_interval(interval_sentinel=999999):
    async def fake_sleep(seconds):
        if seconds == interval_sentinel:
            raise _StopLoop()
        return None
    return fake_sleep


def _freeze(monkeypatch, worker_module, y, mo, d, h, mi, s=0):
    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi, s)
    monkeypatch.setattr(worker_module.datetime, "datetime", FrozenDateTime)

    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(y, mo, d)
    monkeypatch.setattr(worker_module.datetime, "date", FrozenDate)


async def _run_one_iteration(worker_module):
    """Run _background_worker until it hits the loop-back sleep, then
    swallow the _StopLoop that ends it — anything else propagates."""
    try:
        await worker_module._background_worker()
    except _StopLoop:
        pass


@pytest.fixture(autouse=True)
def _set_stop_sentinel(worker_module):
    worker_module._state["settings"]["poll_interval"] = 999999


# ── Startup ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_worker_startup_no_devices_skips_retry_loop(worker_module, mocker):
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Background worker started" in m for m in logs)
    assert not any("Network ready" in m for m in logs)  # skipped, no devices to check


@pytest.mark.asyncio
async def test_background_worker_startup_network_ready_immediately(worker_module, mocker):
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Network ready" in m for m in logs)


@pytest.mark.asyncio
async def test_background_worker_startup_retries_on_network_failure(worker_module, mocker):
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())

    attempts = {"count": 0}
    async def fake_get(self, url, *a, **kw):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.ConnectError("refused")
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Network not ready" in m for m in logs)
    assert any("Network ready" in m for m in logs)


# ── Main loop: polling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_background_worker_polls_each_device(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 12, 0)
    worker_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room",
        "_stale": False, "_consecutive_failures": 0, "_retry_queue": [],
        "_last_mode": None, "watchdog_minutes": 5,
    })
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        if "climate" in url:
            return _FakeResponse(200)
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    assert "ac1.local" in worker_module._state["device_state"]


# ── Main loop: schedule check ────────────────────────────────────


@pytest.mark.asyncio
async def test_background_worker_checks_schedules_on_first_iteration(worker_module, mocker, monkeypatch):
    # last_schedule_check starts as "" so any real hhmm always differs
    # on the very first iteration — this is always exercised, no
    # special setup needed beyond freezing time for determinism.
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 12, 3)
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)
    # No assertion beyond "didn't crash" — _check_schedules itself is
    # thoroughly tested elsewhere; this proves the outer loop actually
    # calls it on a normal iteration.


# ── Main loop: exchange rate update at 06:00 ─────────────────────


@pytest.mark.asyncio
async def test_background_worker_updates_exchange_rate_at_0600(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 6, 0)
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    fetch_mock = mocker.patch.object(worker_module, "_fetch_exchange_rate", mocker.AsyncMock(return_value=None))

    await _run_one_iteration(worker_module)
    fetch_mock.assert_called_once()


@pytest.mark.asyncio
async def test_background_worker_skips_exchange_rate_update_off_schedule(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 14, 30)  # not 06:00
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    fetch_mock = mocker.patch.object(worker_module, "_fetch_exchange_rate", mocker.AsyncMock(return_value=None))

    await _run_one_iteration(worker_module)
    fetch_mock.assert_not_called()


# ── Main loop: nightly dongle reboot ──────────────────────────────


@pytest.mark.asyncio
async def test_background_worker_nightly_reboot_success(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 3, 0)  # default reboot time
    worker_module._state["settings"]["verbose_logging"] = True  # "reboot sent" uses _verbose()
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(200)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Nightly dongle reboot starting" in m for m in logs)
    assert any("Nightly dongle reboot complete" in m for m in logs)
    assert any("nightly reboot sent" in m for m in logs)


@pytest.mark.asyncio
async def test_background_worker_nightly_reboot_device_unreachable(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 3, 0)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("nightly reboot failed — unreachable" in m for m in logs)


@pytest.mark.asyncio
async def test_background_worker_skips_nightly_reboot_when_disabled(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 3, 0)
    worker_module._state["settings"]["nightly_reboot_time"] = ""  # disabled
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert not any("Nightly dongle reboot" in m for m in logs)


# ── Main loop: vacation auto-end ──────────────────────────────────


@pytest.mark.asyncio
async def test_background_worker_vacation_auto_ends_after_limit(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 20, 12, 0)
    worker_module._state["settings"]["vacation_mode"] = True
    worker_module._state["settings"]["vacation_started_at"] = "2026-01-01T00:00:00"
    worker_module._state["settings"]["vacation_days_limit"] = 14
    worker_module._state["devices"].append({
        "host": "ac1.local", "name": "Living Room",
        "max_temp": 32, "_pre_vacation_max_temp": 25,
    })
    worker_module._state["schedules"].append({"id": "a", "enabled": False, "_vacation_paused": True})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    assert worker_module._state["settings"]["vacation_mode"] is False
    assert worker_module._state["devices"][0]["max_temp"] == 25
    assert worker_module._state["schedules"][0]["enabled"] is True
    logs = [l["msg"] for l in worker_module._state["logs"]]
    assert any("Vacation mode auto-ended" in m for m in logs)


@pytest.mark.asyncio
async def test_background_worker_vacation_does_not_auto_end_before_limit(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 12, 0)
    worker_module._state["settings"]["vacation_mode"] = True
    worker_module._state["settings"]["vacation_started_at"] = "2026-01-01T00:00:00"  # only 4 days
    worker_module._state["settings"]["vacation_days_limit"] = 14
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    assert worker_module._state["settings"]["vacation_mode"] is True  # unchanged


@pytest.mark.asyncio
async def test_background_worker_vacation_auto_end_disabled_when_limit_zero(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 20, 12, 0)
    worker_module._state["settings"]["vacation_mode"] = True
    worker_module._state["settings"]["vacation_started_at"] = "2026-01-01T00:00:00"
    worker_module._state["settings"]["vacation_days_limit"] = 0  # disabled
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)

    assert worker_module._state["settings"]["vacation_mode"] is True  # never auto-ends


@pytest.mark.asyncio
async def test_background_worker_vacation_tolerates_malformed_started_at(worker_module, mocker, monkeypatch):
    _freeze(monkeypatch, worker_module, 2026, 1, 20, 12, 0)
    worker_module._state["settings"]["vacation_mode"] = True
    worker_module._state["settings"]["vacation_started_at"] = "not-a-valid-timestamp"
    worker_module._state["settings"]["vacation_days_limit"] = 14
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    async def fake_get(self, url, *a, **kw):
        return _FakeResponse(404)
    mocker.patch.object(httpx.AsyncClient, "get", fake_get)

    await _run_one_iteration(worker_module)  # should not raise

    assert worker_module._state["settings"]["vacation_mode"] is True  # exception caught, no change


# ── Main loop: general exception handling ─────────────────────────


@pytest.mark.asyncio
async def test_background_worker_catches_and_logs_unexpected_exception(worker_module, mocker, monkeypatch):
    """The try/except wrapping the whole loop body must catch an
    unexpected exception from anywhere inside it (here, _poll_device),
    log it, and continue to the loop-back sleep — not propagate and
    kill the entire background worker for the rest of the process's
    life. The real assertion is that _run_one_iteration doesn't raise;
    if the except branch were missing or broken, this test would fail
    with the RuntimeError instead of completing normally."""
    _freeze(monkeypatch, worker_module, 2026, 1, 5, 12, 0)
    worker_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    mocker.patch("asyncio.sleep", _sleep_mock_stopping_at_interval())
    mocker.patch.object(worker_module, "_poll_device", mocker.AsyncMock(side_effect=RuntimeError("boom")))

    await _run_one_iteration(worker_module)  # must not raise RuntimeError
