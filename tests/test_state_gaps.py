"""Tests filling the remaining gaps in state.py: daily backup
rotation (keeping only the last 3), the save_state() convenience
wrapper, log file line rotation, and corrupt-line tolerance when
loading the JSONL log file.
"""

import json


def test_save_raw_creates_daily_backup(state_module, temp_data_file):
    state_module._state["devices"].append({"host": "ac1.local", "name": "Test"})
    state_module._save_raw(state_module._state)

    import datetime
    today = datetime.date.today().isoformat()
    backup_path = str(temp_data_file) + f".bak.{today}"
    import os
    assert os.path.exists(backup_path)


def test_save_raw_rotates_old_backups_keeping_last_3(state_module, temp_data_file):
    import os
    bak_dir = os.path.dirname(os.path.abspath(str(temp_data_file)))
    base = os.path.basename(str(temp_data_file))

    for i in range(4):
        fake_backup = os.path.join(bak_dir, f"{base}.bak.2025-01-0{i+1}")
        with open(fake_backup, "w") as f:
            f.write("{}")

    state_module._save_raw(state_module._state)

    remaining = sorted(f for f in os.listdir(bak_dir) if f.startswith(base + ".bak."))
    assert len(remaining) <= 3


async def test_save_state_helper_acquires_lock_and_saves(state_module, temp_data_file):
    state_module._state["devices"].append({"host": "ac1.local", "name": "Test"})
    await state_module.save_state()

    reloaded = state_module._load_raw()
    assert reloaded["devices"] == [{"host": "ac1.local", "name": "Test"}]


def test_rotate_log_file_trims_to_max_lines(state_module, temp_data_file):
    state_module._LOG_MAX_LINES = 5
    for i in range(10):
        state_module._append_log_file({"msg": f"entry {i}", "level": "info"})

    with open(state_module.LOG_FILE) as f:
        lines = f.readlines()
    assert len(lines) <= 5


def test_rotate_log_file_noop_when_file_missing(state_module, temp_data_file):
    import os
    if os.path.exists(state_module.LOG_FILE):
        os.remove(state_module.LOG_FILE)
    state_module._rotate_log_file()


def test_load_log_file_skips_corrupt_lines(state_module, temp_data_file):
    import os
    os.makedirs(os.path.dirname(os.path.abspath(state_module.LOG_FILE)), exist_ok=True)
    with open(state_module.LOG_FILE, "w") as f:
        f.write(json.dumps({"msg": "good entry", "level": "info"}) + "\n")
        f.write("not valid json at all\n")
        f.write(json.dumps({"msg": "another good entry", "level": "info"}) + "\n")

    entries = state_module._load_log_file()
    msgs = [e["msg"] for e in entries]
    assert "good entry" in msgs
    assert "another good entry" in msgs
    assert len(entries) == 2


def test_clear_log_file_handles_missing_directory_gracefully(state_module, temp_data_file, monkeypatch):
    monkeypatch.setattr(state_module, "LOG_FILE", "/nonexistent-dir-xyz/log.jsonl")
    state_module._clear_log_file()
