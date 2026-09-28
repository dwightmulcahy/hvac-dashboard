"""Dongle HTTP efficiency: per-host path cache, short timeouts/retries,
live vs. diagnostic sensor split, and request latency stats."""

import httpx
import pytest


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {}

    def json(self):
        return self._data


def _dongle(mocker, *, dead=False, fail_after=None):
    """Fake current-firmware dongle: only underscore paths exist."""
    calls = []
    entities = {
        "climate/air_conditioner": {"mode": "COOL", "current_temperature": 24, "target_temperature": 23},
        "sensor/air_conditioner_outdoor_temperature": {"value": 30},
        "sensor/air_conditioner_power_usage": {"value": 500},
        "text_sensor/air_conditioner_eco_status": {"state": "ON"},
        "text_sensor/air_conditioner_firmware": {"state": "SMLIGHT.SLWF-01Pro-Pro 1.1.1"},
        "sensor/air_conditioner_uptime_days": {"value": 1.5},
        "switch/air_conditioner_beeper": {"value": False},
        "sensor/air_conditioner_wi-fi_signal": {"value": -55},
        "text_sensor/air_conditioner_esphome_version": {
            "state": "2026.6.5 (config hash 0xabc, built 2026-09-27 10:00:00 -0600)"},
    }

    async def get(self, url, *a, **kw):
        calls.append(("GET", url))
        if dead or (fail_after is not None and len(calls) > fail_after):
            raise httpx.ConnectTimeout("timed out")
        path = url.split("/", 3)[3]
        return _Resp(200, entities[path]) if path in entities else _Resp(404)

    async def post(self, url, *a, **kw):
        calls.append(("POST", url))
        if dead:
            raise httpx.ConnectTimeout("timed out")
        path = url.split("/", 3)[3].split("?")[0]
        base = path.rsplit("/", 1)[0] if path.endswith(("/set", "/press", "/turn_on", "/turn_off")) else path
        return _Resp(200) if base in entities or "eco_on" in path else _Resp(404)

    mocker.patch.object(httpx.AsyncClient, "get", get)
    mocker.patch.object(httpx.AsyncClient, "post", post)
    mocker.patch("asyncio.sleep", return_value=None)
    return calls


def _urls(calls):
    return [u for _, u in calls]


# ── path cache ──

@pytest.mark.asyncio
async def test_climate_path_cached_after_first_404(worker_module, mocker):
    calls = _dongle(mocker)
    await worker_module._fetch_state("ac1.local")
    assert len(calls) == 2  # spaced path 404, then underscore
    calls.clear()
    await worker_module._fetch_state("ac1.local")
    await worker_module._send_cmd("ac1.local", {"target_temperature": 22})
    assert _urls(calls) == [
        "http://ac1.local/climate/air_conditioner",
        "http://ac1.local/climate/air_conditioner/set?target_temperature=22",
    ]


@pytest.mark.asyncio
async def test_stale_cached_path_is_rediscovered(worker_module, mocker):
    calls = _dongle(mocker)
    worker_module._path_cache["ac1.local"] = {"climate": "climate/Air%20Conditioner"}
    assert (await worker_module._fetch_state("ac1.local"))["mode"] == "COOL"
    assert worker_module._path_cache["ac1.local"]["climate"] == "climate/air_conditioner"


@pytest.mark.asyncio
async def test_missing_sensor_not_reprobed_until_ttl(worker_module, mocker):
    calls = _dongle(mocker)
    await worker_module._fetch_sensors("ac1.local", diagnostics=False)
    calls.clear()
    worker_module._path_cache["ac1.local"].pop("power_usage")  # pretend it's missing
    worker_module._path_cache["ac1.local"]["power_usage"] = (worker_module._MISSING, __import__("time").time())
    await worker_module._fetch_sensors("ac1.local", diagnostics=False)
    assert not any("power" in u for u in _urls(calls))


@pytest.mark.asyncio
async def test_second_sensor_read_makes_no_404s(worker_module, mocker):
    calls = _dongle(mocker)
    await worker_module._fetch_sensors("ac1.local")
    first = len(calls)
    calls.clear()
    out = await worker_module._fetch_sensors("ac1.local")
    assert len(calls) == 8  # firmware + 3 live + 4 diagnostics, one request each
    assert first > len(calls)
    assert out["eco_status"]["state"] == "ON"


# ── timeouts / retries ──

@pytest.mark.asyncio
async def test_dead_dongle_gives_up_after_two_attempts_on_one_path(worker_module, mocker):
    calls = _dongle(mocker, dead=True)
    assert await worker_module._fetch_state("ac1.local") is None
    assert len(calls) == worker_module.POLL_ATTEMPTS == 2
    assert worker_module.HTTP_TIMEOUT.connect <= 1.5


@pytest.mark.asyncio
async def test_sensor_read_stops_at_first_transport_error(worker_module, mocker):
    calls = _dongle(mocker, fail_after=2)
    out = await worker_module._fetch_sensors("ac1.local")
    assert out["_aborted"] is True
    assert len(calls) == 3  # didn't wait out a timeout for every remaining sensor


@pytest.mark.asyncio
async def test_command_retries_once_on_transport_error(worker_module, mocker):
    n = []

    async def post(self, url, *a, **kw):
        n.append(url)
        if len(n) == 1:
            raise httpx.ReadTimeout("slow")
        return _Resp(200)
    mocker.patch.object(httpx.AsyncClient, "post", post)
    mocker.patch("asyncio.sleep", return_value=None)
    assert await worker_module._send_cmd("ac1.local", {"mode": "COOL"})
    assert len(n) == 2


# ── live vs diagnostics ──

def _device(worker_module):
    from state import DEVICE_DEFAULTS

    d = {**DEVICE_DEFAULTS, "_retry_queue": [], "host": "ac1.local", "name": "LR"}
    worker_module._state["devices"] = [d]
    return d


@pytest.mark.asyncio
async def test_diagnostics_read_on_first_poll_then_carried_forward(worker_module, mocker):
    calls = _dongle(mocker)
    d = _device(worker_module)
    await worker_module._poll_device(d)
    ds1 = dict(worker_module._state["device_state"]["ac1.local"])
    assert ds1["wifi_signal"] == -55 and ds1["firmware_version"] == "1.1.1"
    calls.clear()

    await worker_module._poll_device(d)
    urls = _urls(calls)
    assert not any(k in u for u in urls for k in ("wi-fi", "uptime", "firmware", "esphome", "beeper"))
    assert any("eco_status" in u for u in urls) and any("outdoor" in u for u in urls)
    ds2 = worker_module._state["device_state"]["ac1.local"]
    for k in ("wifi_signal", "uptime_days", "firmware_version", "firmware_built", "esphome_version"):
        assert ds2[k] == ds1[k], k
    assert ds2["firmware_outdated"] is False


@pytest.mark.asyncio
async def test_diagnostics_reread_after_interval_or_full_poll(worker_module, mocker):
    calls = _dongle(mocker)
    d = _device(worker_module)
    await worker_module._poll_device(d)
    calls.clear()
    await worker_module._poll_device(d, full=True)
    assert any("wi-fi" in u for u in _urls(calls))
    calls.clear()
    worker_module._diag_at["ac1.local"] -= worker_module.DIAGNOSTIC_INTERVAL_SECS + 1
    await worker_module._poll_device(d)
    assert any("wi-fi" in u for u in _urls(calls))


@pytest.mark.asyncio
async def test_reconnect_clears_path_cache_and_rereads_diagnostics(worker_module, mocker):
    calls = _dongle(mocker)
    d = _device(worker_module)
    await worker_module._poll_device(d)
    d["_stale"] = True
    calls.clear()
    await worker_module._poll_device(d)
    urls = _urls(calls)
    # cache dropped once the dongle answered → sensors are re-discovered
    assert "http://ac1.local/text_sensor/Air%20Conditioner%20Firmware" in urls
    assert any("wi-fi" in u for u in urls)


# ── latency stats ──

@pytest.mark.asyncio
async def test_latency_stats_in_device_state(worker_module, mocker):
    _dongle(mocker)
    d = _device(worker_module)
    await worker_module._poll_device(d)
    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["http_requests"] > 0
    assert ds["http_fail"] == 0
    assert ds["http_ms_avg"] is not None and ds["http_ms_max"] >= ds["http_ms_avg"]


@pytest.mark.asyncio
async def test_latency_counts_failures(worker_module, mocker):
    _dongle(mocker, dead=True)
    await worker_module._fetch_state("ac1.local")
    s = worker_module._latency_summary("ac1.local")
    assert s["http_fail"] == 2 and s["http_ms_avg"] is None


def test_health_endpoint_includes_latency(client, auth_headers, api_module):
    import worker

    api_module._state["devices"].append({"host": "ac1.local", "name": "LR"})
    worker._record_latency("ac1.local", 80.0)
    worker._record_latency("ac1.local", 120.0)
    dev = next(x for x in client.get("/health", headers=auth_headers).json()["devices"] if x["host"] == "ac1.local")
    assert dev["http_ms_avg"] == 100 and dev["http_ms_max"] == 120 and dev["http_fail"] == 0
