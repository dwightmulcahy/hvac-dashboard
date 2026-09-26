"""Firmware identity: air_conditioner_firmware sensor, outdated flag, build
date, and skipping sensors older firmware can't have."""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest


def test_latest_firmware_version_matches_yaml(worker_module):
    """Bumping firmware/packages/slwf-base.yaml without the backend constant
    (or vice versa) would mis-flag every dongle."""
    yaml = Path("firmware/packages/slwf-base.yaml").read_text()
    version = re.search(r'^\s+version:\s*"([^"]+)"', yaml, re.M).group(1)
    assert worker_module.LATEST_FIRMWARE_VERSION == version


@pytest.mark.parametrize("raw,expected", [
    ("SMLIGHT.SLWF-01Pro-Pro 1.1.0", {"name": "SMLIGHT.SLWF-01Pro-Pro", "version": "1.1.0"}),
    ("  SMLIGHT.SLWF-01Pro-Pro 1.2.0-beta ", {"name": "SMLIGHT.SLWF-01Pro-Pro", "version": "1.2.0-beta"}),
    ("", None),
    (None, None),
])
def test_parse_firmware(worker_module, raw, expected):
    assert worker_module._parse_firmware(raw) == expected


@pytest.mark.parametrize("version,outdated", [("1.0.0", True), ("1.1.0", False), ("1.10.0", False), (None, True)])
def test_firmware_status_outdated(worker_module, version, outdated):
    fw = {"name": "X", "version": version} if version else None
    st = worker_module._firmware_status(fw)
    assert st["firmware_outdated"] is outdated
    assert st["firmware_latest"] == worker_module.LATEST_FIRMWARE_VERSION


@pytest.mark.asyncio
async def test_poll_sets_firmware_fields_and_build_date(worker_module, mocker):
    device = {"host": "ac1.local", "name": "LR", "_retry_queue": []}
    worker_module._state["devices"] = [device]
    mocker.patch.object(worker_module, "_fetch_state", AsyncMock(return_value={"mode": "COOL"}))
    mocker.patch.object(worker_module, "_fetch_sensors", AsyncMock(return_value={
        "firmware": {"state": "SMLIGHT.SLWF-01Pro-Pro 1.1.0"},
        "esphome_version": {"state": "2026.6.5 Sep 26 2026, 16:34:31"},
    }))
    await worker_module._poll_device(device)
    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["firmware_name"] == "SMLIGHT.SLWF-01Pro-Pro"
    assert ds["firmware_version"] == "1.1.0"
    assert ds["firmware_outdated"] is False
    assert ds["firmware_built"] == "Sep 26 2026, 16:34:31"


@pytest.mark.asyncio
async def test_poll_legacy_firmware_is_outdated(worker_module, mocker):
    device = {"host": "ac1.local", "name": "LR", "_retry_queue": []}
    worker_module._state["devices"] = [device]
    mocker.patch.object(worker_module, "_fetch_state", AsyncMock(return_value={"mode": "COOL"}))
    mocker.patch.object(worker_module, "_fetch_sensors", AsyncMock(return_value={}))
    await worker_module._poll_device(device)
    ds = worker_module._state["device_state"]["ac1.local"]
    assert ds["firmware_version"] is None
    assert ds["firmware_outdated"] is True


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


def _fake_get(firmware_state, calls):
    async def get(self, url, *a, **kw):
        calls.append(url)
        if url.endswith("/text_sensor/air_conditioner_firmware") and firmware_state:
            return _Resp(200, {"state": firmware_state})
        if url.endswith("/text_sensor/air_conditioner_eco_status"):
            return _Resp(200, {"state": "ON"})
        return _Resp(404)
    return get


@pytest.mark.asyncio
async def test_fetch_sensors_skips_eco_status_on_older_firmware(worker_module, mocker):
    calls = []
    mocker.patch.object(httpx.AsyncClient, "get", _fake_get("SMLIGHT.SLWF-01Pro-Pro 1.0.0", calls))
    out = await worker_module._fetch_sensors("ac1.local")
    assert "eco_status" not in out
    assert not any("eco_status" in c for c in calls)


@pytest.mark.asyncio
async def test_fetch_sensors_reads_eco_status_on_current_firmware(worker_module, mocker):
    calls = []
    mocker.patch.object(httpx.AsyncClient, "get", _fake_get("SMLIGHT.SLWF-01Pro-Pro 1.1.0", calls))
    out = await worker_module._fetch_sensors("ac1.local")
    assert out["eco_status"]["state"] == "ON"
    assert out["firmware"]["state"].endswith("1.1.0")


@pytest.mark.asyncio
async def test_fetch_sensors_without_firmware_sensor_still_probes_eco(worker_module, mocker):
    """Unknown version (sensor missing or failed to read) must not hide ECO."""
    calls = []
    mocker.patch.object(httpx.AsyncClient, "get", _fake_get(None, calls))
    out = await worker_module._fetch_sensors("ac1.local")
    assert out["eco_status"]["state"] == "ON"
