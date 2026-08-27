"""Tests for logging_config.py's pure logic: JSONFormatter's field
mapping and configure_logging()'s text-vs-json format selection.

Integration-level behavior (the actual format taking effect end-to-end
in a running app, and AccessLogMiddleware's request/response logging)
is covered separately in test_access_log_middleware.py, since that
needs the same fresh-import-before-api-is-loaded pattern test_cors.py
already established for LOG_FORMAT's sibling env var, CORS_ALLOWED_ORIGINS.
"""

import json
import logging
import sys

import pytest

sys.path.insert(0, ".")
import logging_config
from logging_config import JSONFormatter, configure_logging


def _make_record(msg="hello", level=logging.INFO, **extra):
    record = logging.LogRecord(
        name="hvac.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_json_formatter_produces_valid_json():
    formatter = JSONFormatter()
    record = _make_record("test message")
    parsed = json.loads(formatter.format(record))
    assert parsed["message"] == "test message"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "hvac.test"
    assert "timestamp" in parsed


def test_json_formatter_includes_extra_fields_as_top_level_keys():
    formatter = JSONFormatter()
    record = _make_record("request handled", method="GET", status_code=200, duration_ms=12.3)
    parsed = json.loads(formatter.format(record))
    assert parsed["method"] == "GET"
    assert parsed["status_code"] == 200
    assert parsed["duration_ms"] == 12.3


def test_json_formatter_excludes_standard_logrecord_attributes():
    formatter = JSONFormatter()
    record = _make_record("plain message")
    parsed = json.loads(formatter.format(record))
    # these are real attributes on every LogRecord — if the reserved-
    # attrs filtering broke, they'd leak into the output as noise
    assert "pathname" not in parsed
    assert "lineno" not in parsed
    assert "args" not in parsed
    assert "msg" not in parsed


def test_json_formatter_includes_exception_info():
    formatter = JSONFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="hvac.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="something failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    parsed = json.loads(formatter.format(record))
    assert "exception" in parsed
    assert "ValueError" in parsed["exception"]
    assert "boom" in parsed["exception"]


def test_json_formatter_handles_non_serializable_extra_values():
    """extra={...} could contain anything — a custom object, a set,
    etc. default=str in json.dumps means this should never raise,
    just stringify whatever it can't natively serialize."""
    formatter = JSONFormatter()
    record = _make_record("weird value", custom_field={1, 2, 3})
    # should not raise
    parsed = json.loads(formatter.format(record))
    assert "custom_field" in parsed


@pytest.mark.parametrize("value", ["json", "JSON", " json ", "Json"])
def test_configure_logging_json_mode_is_case_and_whitespace_insensitive(monkeypatch, value):
    monkeypatch.setenv("LOG_FORMAT", value)
    configure_logging()
    # pytest's own logging plugin has already attached its own
    # handler(s) to the root logger by the time this runs — checking
    # handlers[0] would be checking whichever of those happens to be
    # first, not necessarily ours. logging_config tracks exactly the
    # one handler it owns for precisely this reason.
    assert isinstance(logging_config._our_handler.formatter, JSONFormatter)


@pytest.mark.parametrize("value", [None, "text", "TEXT", "", "anything-else"])
def test_configure_logging_defaults_to_text_format(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LOG_FORMAT", raising=False)
    else:
        monkeypatch.setenv("LOG_FORMAT", value)
    configure_logging()
    assert not isinstance(logging_config._our_handler.formatter, JSONFormatter)


def test_configure_logging_replaces_existing_handlers_not_accumulates():
    """Calling this twice (e.g. across two test-module reloads) must
    not leave duplicate *own* handlers attached — that would print
    every log line twice. Checks the handler count delta across two
    calls rather than an absolute count, since pytest's own logging
    plugin already has its own handlers on the root logger regardless
    of anything this module does."""
    configure_logging()
    before = len(logging.getLogger().handlers)
    configure_logging()
    after = len(logging.getLogger().handlers)
    assert after == before
