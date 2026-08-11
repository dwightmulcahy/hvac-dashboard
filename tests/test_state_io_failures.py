"""Tests for the remaining exception-handling branches in state.py:
double-failure during corrupt-file preservation, backup-rotation
failure, log-file write/rotate/load failures at the file-I/O level
(as opposed to per-line JSON parse failures, already covered in
test_state_gaps.py's test_load_log_file_skips_corrupt_lines).

These simulate genuine disk failures (permission denied, disk full,
etc.) by monkeypatching the specific stdlib calls state.py makes, so
each test isolates exactly one failure point rather than trying to
cause a real OS-level failure.
"""


def test_load_raw_tolerates_failure_to_preserve_corrupt_file(state_module, temp_data_file, monkeypatch):
    """If the state file is corrupt AND copying it aside for
    inspection also fails, loading should still fall back to defaults
    rather than raising."""
    temp_data_file.write_text("{not valid json")

    def failing_copy(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(state_module.shutil, "copy", failing_copy)

    result = state_module._load_raw()
    assert result["devices"] == []  # fell back to defaults despite double failure


def test_save_raw_tolerates_backup_rotation_failure(state_module, temp_data_file, monkeypatch):
    """A failure during backup rotation (e.g. disk full) shouldn't
    prevent the main state file from having already been saved."""
    def failing_copy(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(state_module.shutil, "copy", failing_copy)

    state_module._state["devices"].append({"host": "ac1.local", "name": "Test"})
    state_module._save_raw(state_module._state)  # should not raise

    # the main file itself should still have saved successfully —
    # only the backup step failed
    import json
    with open(str(temp_data_file)) as f:
        saved = json.load(f)
    assert saved["devices"] == [{"host": "ac1.local", "name": "Test"}]


def test_append_log_file_tolerates_write_failure(state_module, temp_data_file, monkeypatch):
    def failing_open(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(state_module, "open", failing_open, raising=False)

    state_module._append_log_file({"msg": "test", "level": "info"})  # should not raise


def test_rotate_log_file_tolerates_read_failure(state_module, temp_data_file, monkeypatch):
    import os
    os.makedirs(os.path.dirname(os.path.abspath(state_module.LOG_FILE)), exist_ok=True)
    with open(state_module.LOG_FILE, "w") as f:
        f.write('{"msg": "entry"}\n')

    def failing_open(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(state_module, "open", failing_open, raising=False)

    state_module._rotate_log_file()  # should not raise


def test_load_log_file_tolerates_open_failure(state_module, temp_data_file, monkeypatch):
    """Distinct from test_load_log_file_skips_corrupt_lines (which
    tests individual malformed JSON lines) — this tests the file
    itself being unreadable at the OS level."""
    import os
    os.makedirs(os.path.dirname(os.path.abspath(state_module.LOG_FILE)), exist_ok=True)
    with open(state_module.LOG_FILE, "w") as f:
        f.write('{"msg": "entry"}\n')

    def failing_open(*a, **kw):
        raise OSError("permission denied")
    monkeypatch.setattr(state_module, "open", failing_open, raising=False)

    result = state_module._load_log_file()
    assert result == []  # falls back to empty list rather than raising
