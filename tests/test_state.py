"""Tests for state.py — persistence, rate calc, watt estimate, log file."""

import json


def test_fresh_state_has_defaults(state_module):
    assert state_module._state["devices"] == []
    assert state_module._state["settings"]["poll_interval"] == 120
    assert state_module._state["settings"]["vacation_mode"] is False
    assert state_module._state["settings"]["kiosk_quiet_hours_enabled"] is False
    assert state_module._state["settings"]["kiosk_quiet_start"] == "22:00"
    assert state_module._state["settings"]["kiosk_quiet_end"] == "07:00"


def test_save_and_reload_state(state_module):
    state_module._state["devices"].append({"host": "ac1.local", "name": "Living Room"})
    state_module._save_raw(state_module._state)

    # simulate a fresh process loading the same DATA_FILE
    reloaded = state_module._load_raw()
    assert reloaded["devices"] == [{"host": "ac1.local", "name": "Living Room"}]


def test_save_excludes_logs_from_json_file(state_module):
    state_module._state["logs"].append({"msg": "hello", "level": "info"})
    state_module._save_raw(state_module._state)

    reloaded = state_module._load_raw()
    # logs are persisted separately in the JSONL file, not the main JSON
    assert reloaded["logs"] == []


def test_save_excludes_recovery_key_from_disk(state_module, temp_data_file):
    """The recovery key is documented as living only in memory and the
    Docker startup log, regenerated fresh every restart — it must never
    land in hvac_state.json or its daily .bak rotations."""
    state_module._state["_recovery_key"] = "super-secret-one-time-key"
    state_module._save_raw(state_module._state)

    raw_on_disk = json.loads(temp_data_file.read_text())
    assert "_recovery_key" not in raw_on_disk

    # in-memory value is untouched — only the on-disk copy is excluded
    assert state_module._state["_recovery_key"] == "super-secret-one-time-key"


def test_corrupt_state_file_falls_back_to_defaults(state_module, temp_data_file):
    temp_data_file.write_text("{not valid json")
    reloaded = state_module._load_raw()
    assert reloaded["devices"] == []
    # corrupt file should be preserved for inspection
    assert (temp_data_file.parent / (temp_data_file.name + ".corrupt")).exists()


def test_add_log_persists_to_jsonl_file(state_module):
    state_module._add_log("test event", "warn")
    entries = state_module._load_log_file()
    assert len(entries) == 1
    assert entries[0]["msg"] == "test event"
    assert entries[0]["level"] == "warn"


def test_add_log_caps_in_memory_at_500(state_module):
    for i in range(510):
        state_module._add_log(f"event {i}", "info")
    assert len(state_module._state["logs"]) == 500
    # most recent should be first
    assert state_module._state["logs"][0]["msg"] == "event 509"


def test_verbose_only_logs_when_enabled(state_module):
    state_module._state["settings"]["verbose_logging"] = False
    state_module._verbose("should not appear", "info")
    assert len(state_module._state["logs"]) == 0

    state_module._state["settings"]["verbose_logging"] = True
    state_module._verbose("should appear", "info")
    assert len(state_module._state["logs"]) == 1


def test_clear_log_file(state_module):
    state_module._add_log("event", "info")
    assert len(state_module._load_log_file()) == 1
    state_module._clear_log_file()
    assert len(state_module._load_log_file()) == 0


def test_effective_rate_tiered(state_module):
    state_module._state["settings"]["tiered"] = True
    state_module._state["settings"]["monthly_kwh"] = 100  # within tier 1 (up_to 200)
    state_module._state["settings"]["exchange_rate"] = 500
    rate = state_module._effective_rate()
    assert rate == 62 / 500


def test_effective_rate_flat(state_module):
    state_module._state["settings"]["tiered"] = False
    state_module._state["settings"]["flat_rate"] = 0.20
    assert state_module._effective_rate() == 0.20


def test_effective_rate_top_tier_overflow(state_module):
    """kWh usage beyond the last tier's up_to should use the last tier's rate."""
    state_module._state["settings"]["tiered"] = True
    state_module._state["settings"]["monthly_kwh"] = 999999999
    state_module._state["settings"]["exchange_rate"] = 500
    rate = state_module._effective_rate()
    assert rate == 140 / 500


def test_est_watts_off_mode_is_zero(state_module):
    watts = state_module._est_watts({"mode": "OFF"}, 24000, 20)
    assert watts == 0.0


def test_est_watts_missing_temps_returns_none(state_module):
    watts = state_module._est_watts({"mode": "COOL"}, 24000, 20)
    assert watts is None


def test_est_watts_cooling_returns_positive(state_module):
    watts = state_module._est_watts(
        {"mode": "COOL", "current_temperature": 28, "target_temperature": 22, "outdoor_temp": 35},
        24000, 20,
    )
    assert watts is not None
    assert watts > 0


def test_est_watts_tolerates_unparseable_outdoor_temp(state_module):
    """Regression test: indoor/target parsing was already protected by
    try/except, but outdoor's penalty calculation wasn't — a malformed
    outdoor_temp reading crashed the whole estimate instead of
    gracefully falling back the same way a missing outdoor_temp does."""
    watts = state_module._est_watts(
        {"mode": "COOL", "current_temperature": 28, "target_temperature": 22, "outdoor_temp": "not-a-number"},
        24000, 20,
    )
    assert watts is not None  # didn't raise, didn't return None
    assert watts > 0


def test_now_iso_is_naive_utc(state_module):
    iso = state_module._now_iso()
    # naive datetimes parse back with no tzinfo
    import datetime
    dt = datetime.datetime.fromisoformat(iso)
    assert dt.tzinfo is None
