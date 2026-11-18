"""Open-Meteo outdoor weather: worker._fetch_weather / _maybe_fetch_weather /
_geocode and GET /weather, GET /weather/geocode, location change on PUT /settings."""

import httpx


class _Resp:
    def __init__(self, data=None, status_code=200):
        self._data = data or {}
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


CURRENT = {"current": {"temperature_2m": 30.5, "weather_code": 2, "is_day": 1}}


def _patch_get(mocker, resp=None, exc=None, calls=None):
    async def fake_get(self, url, *a, **kw):
        if calls is not None:
            calls.append((url, kw.get("params")))
        if exc:
            raise exc
        return resp

    mocker.patch.object(httpx.AsyncClient, "get", fake_get)


async def test_fetch_weather_no_location_clears(worker_module, mocker):
    calls = []
    _patch_get(mocker, _Resp(CURRENT), calls=calls)
    worker_module._state["weather"] = {"temp_c": 1}
    assert await worker_module._fetch_weather() is None
    assert worker_module._state["weather"] is None
    assert calls == []


async def test_fetch_weather_stores_current(worker_module, mocker):
    calls = []
    _patch_get(mocker, _Resp(CURRENT), calls=calls)
    worker_module._state["settings"].update(weather_lat=10.3, weather_lon=-85.8)
    w = await worker_module._fetch_weather()
    assert w["temp_c"] == 30.5 and w["code"] == 2 and w["is_day"] is True
    assert worker_module._state["weather"]["lat"] == 10.3
    url, params = calls[0]
    assert url == worker_module.WEATHER_URL
    assert params["latitude"] == 10.3 and "weather_code" in params["current"]


async def test_fetch_weather_failure_keeps_last_reading(worker_module, mocker):
    _patch_get(mocker, exc=httpx.ConnectError("down"))
    worker_module._state["settings"].update(weather_lat=10, weather_lon=-85)
    worker_module._state["weather"] = {"temp_c": 29.0}
    assert await worker_module._fetch_weather() is None
    assert worker_module._state["weather"] == {"temp_c": 29.0}


async def test_fetch_weather_rejects_out_of_range_location(worker_module, mocker):
    calls = []
    _patch_get(mocker, _Resp(CURRENT), calls=calls)
    worker_module._state["settings"].update(weather_lat=95, weather_lon=0)
    assert await worker_module._fetch_weather() is None
    assert calls == []


async def test_maybe_fetch_weather_throttles(worker_module, mocker):
    fetch = mocker.patch.object(worker_module, "_fetch_weather", mocker.AsyncMock())
    worker_module._weather_last_fetch = None
    await worker_module._maybe_fetch_weather()
    assert fetch.await_count == 1
    import time
    worker_module._weather_last_fetch = time.monotonic()
    await worker_module._maybe_fetch_weather()
    assert fetch.await_count == 1


async def test_geocode_builds_labels(worker_module, mocker):
    data = {"results": [{"name": "Tamarindo", "admin1": "Guanacaste", "country": "Costa Rica",
                         "latitude": 10.3, "longitude": -85.84}]}
    _patch_get(mocker, _Resp(data))
    assert await worker_module._geocode("tama") == [
        {"name": "Tamarindo, Guanacaste, Costa Rica", "lat": 10.3, "lon": -85.84}]


def test_get_weather_unconfigured(client, auth_headers):
    r = client.get("/weather", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"configured": False, "location_name": "", "weather": None}


def test_put_settings_location_change_fetches_weather(client, auth_headers, mocker):
    _patch_get(mocker, _Resp(CURRENT))
    client.put("/settings", headers=auth_headers, json={
        "weather_lat": 10.3, "weather_lon": -85.8, "weather_location_name": "Tamarindo"})
    j = client.get("/weather", headers=auth_headers).json()
    assert j["configured"] is True and j["location_name"] == "Tamarindo"
    assert j["weather"]["temp_c"] == 30.5


def test_put_settings_without_location_change_does_not_fetch(client, auth_headers, mocker):
    calls = []
    _patch_get(mocker, _Resp(CURRENT), calls=calls)
    client.put("/settings", headers=auth_headers, json={"poll_interval": 90})
    assert not any("open-meteo" in u for u, _ in calls)


def test_geocode_endpoint(client, auth_headers, mocker):
    _patch_get(mocker, _Resp({"results": [{"name": "Liberia", "country": "Costa Rica",
                                           "latitude": 10.6, "longitude": -85.4}]}))
    r = client.get("/weather/geocode", params={"q": "Liberia"}, headers=auth_headers)
    assert r.json()["results"][0]["name"] == "Liberia, Costa Rica"
    assert client.get("/weather/geocode", params={"q": " "}, headers=auth_headers).json() == {"results": []}


def test_geocode_endpoint_upstream_failure_is_502(client, auth_headers, mocker):
    _patch_get(mocker, exc=httpx.ConnectError("down"))
    r = client.get("/weather/geocode", params={"q": "Liberia"}, headers=auth_headers)
    assert r.status_code == 502
