"""Tests for routers/devices_control.py: on-demand poll, commands,
temp lock, display toggle, beeper. mock_device_response already
covers the "happy path" cmd test in test_endpoints.py — these fill in
404/not-found branches, lock behavior, and beeper/display variants.
"""

import httpx


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def json(self):
        return {}


# ── /poll ─────────────────────────────────────────────────────


def test_poll_unknown_device_returns_404(client, auth_headers):
    r = client.post("/devices/nonexistent.local/poll", headers=auth_headers)
    assert r.status_code == 404


def test_poll_known_device_returns_state(client, auth_headers, api_module, mock_device_response):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    r = client.post("/devices/ac1.local/poll", headers=auth_headers)
    assert r.status_code == 200
    assert "state" in r.json()


# ── /cmd ──────────────────────────────────────────────────────


def test_cmd_failure_queues_retry(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "_retry_queue": []})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/cmd", headers=auth_headers, json={"params": {"mode": "COOL"}})
    assert r.status_code == 200
    assert r.json()["queued"] is True
    device = api_module._state["devices"][0]
    assert len(device["_retry_queue"]) == 1


def test_cmd_turn_off_logs_off_not_on(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    client.post("/devices/ac1.local/cmd", headers=auth_headers, json={"params": {"mode": "OFF"}})
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("turned off by" in m for m in logs)


def test_cmd_set_temperature_logs_correctly(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    client.post("/devices/ac1.local/cmd", headers=auth_headers, json={"params": {"target_temperature": 22}})
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("set → 22" in m for m in logs)


def test_cmd_unknown_device_still_attempts_and_uses_host_as_name(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/unknown.local/cmd", headers=auth_headers, json={"params": {"mode": "COOL"}})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── /lock ─────────────────────────────────────────────────────


def test_lock_unknown_device_returns_not_found(client, auth_headers):
    r = client.post("/devices/nonexistent.local/lock", headers=auth_headers, json={"lock": True, "target_temp": 22})
    assert r.json()["ok"] is False
    assert r.json()["error"] == "not found"


def test_lock_enables_with_target_temp(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "lock_temp": False, "locked_target_temp": None})
    r = client.post("/devices/ac1.local/lock", headers=auth_headers, json={"lock": True, "target_temp": 22.5})
    assert r.json()["lock_temp"] is True
    assert r.json()["locked_target_temp"] == 22.5
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("locked at 22.5" in m for m in logs)


def test_lock_disable_clears_target_temp(client, auth_headers, api_module):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "lock_temp": True, "locked_target_temp": 22.5})
    r = client.post("/devices/ac1.local/lock", headers=auth_headers, json={"lock": False})
    assert r.json()["lock_temp"] is False
    assert r.json()["locked_target_temp"] is None
    logs = [l["msg"] for l in api_module._state["logs"]]
    assert any("unlocked" in m for m in logs)


# ── /display-toggle ───────────────────────────────────────────


def test_display_toggle_success(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": True})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/display-toggle", headers=auth_headers)
    assert r.json()["ok"] is True


def test_display_toggle_blocked_without_ir_emitter(client, auth_headers, api_module, mocker):
    """A registered device without has_ir_emitter should be blocked
    server-side, not just hidden in the UI — defense in depth against
    someone hitting the endpoint directly."""
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": False})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)  # would succeed if it were ever attempted
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/display-toggle", headers=auth_headers)
    assert r.json()["ok"] is False
    assert "IR emitter" in r.json()["error"]


def test_display_toggle_unsupported_returns_error(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/display-toggle", headers=auth_headers)
    assert r.json()["ok"] is False
    assert "not supported" in r.json()["error"]


def test_display_toggle_network_exception_returns_error(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        raise httpx.ConnectError("refused")
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/display-toggle", headers=auth_headers)
    assert r.json()["ok"] is False


# ── /swing-step ──────────────────────────────────────────────
# Same underlying helper as display-toggle (_press_ir_button), same
# IR-emitter gate, same firmware pattern (slwf-base.yaml defines both
# as template buttons calling a midea_ac.XXX: action) — so these
# mirror the display-toggle tests above rather than duplicating every
# edge case independently.


def test_swing_step_success(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": True})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/swing-step", headers=auth_headers)
    assert r.json()["ok"] is True


def test_swing_step_blocked_without_ir_emitter(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": False})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/swing-step", headers=auth_headers)
    assert r.json()["ok"] is False
    assert "IR emitter" in r.json()["error"]


def test_swing_step_falls_through_to_underscore_path(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": True})
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        if "Air%20Conditioner" in url:
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/swing-step", headers=auth_headers)
    assert r.json()["ok"] is True
    assert len(calls) == 2


def test_swing_step_unsupported_returns_error(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "has_ir_emitter": True})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=404)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/swing-step", headers=auth_headers)
    assert r.json()["ok"] is False
    assert "not supported" in r.json()["error"]


# ── /beeper/test and /beeper/{state} ─────────────────────────


def test_beeper_test_success(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "beeper": "OFF"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)
    mocker.patch("asyncio.sleep", return_value=None)

    r = client.post("/devices/ac1.local/beeper/test", headers=auth_headers)
    assert r.json()["ok"] is True


def test_beeper_test_restores_saved_on_state(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "beeper": "ON"})
    calls = []
    async def fake_post(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)
    mocker.patch("asyncio.sleep", return_value=None)

    client.post("/devices/ac1.local/beeper/test", headers=auth_headers)
    assert any("turn_on" in c for c in calls)


def test_set_beeper_on_updates_saved_state(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "beeper": "OFF"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/beeper/on", headers=auth_headers)
    assert r.json()["ok"] is True
    assert api_module._state["devices"][0]["beeper"] == "ON"


def test_set_beeper_off_updates_saved_state(client, auth_headers, api_module, mocker):
    api_module._state["devices"].append({"host": "ac1.local", "name": "Living Room", "beeper": "ON"})
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/ac1.local/beeper/off", headers=auth_headers)
    assert r.status_code == 200
    assert api_module._state["devices"][0]["beeper"] == "OFF"


def test_set_beeper_unknown_device_still_attempts(client, auth_headers, mocker):
    async def fake_post(self, url, *a, **kw):
        return _FakeResponse(status_code=200)
    mocker.patch.object(httpx.AsyncClient, "post", fake_post)

    r = client.post("/devices/unknown.local/beeper/on", headers=auth_headers)
    assert r.status_code == 200
