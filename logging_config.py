"""
HVAC Dashboard — logging configuration.

Two independent things live here:

1. `configure_logging()` — sets up the root logging format. Defaults to
   the existing human-readable text format (`LOG_FORMAT` unset or
   "text"), since this project's primary audience is someone tailing
   `docker logs` directly in a terminal, where JSON lines are strictly
   harder to read at a glance than plain text. Setting `LOG_FORMAT=json`
   switches to structured JSON output instead — one JSON object per
   line, safe for a log aggregator (Loki, ELK, CloudWatch, etc.) to
   ingest and query. Nothing else in the codebase needs to change
   regardless of which format is active; every existing `log.info(...)`
   call works unmodified either way.

2. `AccessLogMiddleware` — logs every HTTP request (method, path,
   status code, duration, client IP) through the same logger, so
   access logs get the same format toggle for free. This is a REST
   API state-changing surface (commands, schedules, users) — knowing
   who hit what and when is worth having independent of the app's own
   `_add_log()` in-app activity feed (state.py), which is a different,
   user-facing surface with different retention/rotation rules and
   isn't a substitute for this.
"""

import json
import logging
import os
import time

from starlette.middleware.base import BaseHTTPMiddleware

# standard attributes every LogRecord has — anything else found on a
# record (i.e. passed via logging's `extra={...}` kwarg) is assumed to
# be a deliberately-added structured field and gets included as its
# own top-level JSON key. This is what lets AccessLogMiddleware below
# attach method/path/status_code/etc. as queryable fields rather than
# baking them into an unstructured message string.
_RESERVED_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    }
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_our_handler = None  # tracks the one handler we own, so repeat calls
# (e.g. across fresh-import test cycles) replace
# only that handler rather than wiping out anything
# else attached to the root logger — most notably,
# pytest's own caplog fixture attaches its handler
# to the root logger before a test body runs;
# `root.handlers = [handler]` would silently
# discard that handler and break log capturing in
# tests, which is exactly what happened before this
# was tracked explicitly instead.


def configure_logging():
    """Call once, at import time, before any logging happens — same
    timing/placement requirement api.py's previous plain
    logging.basicConfig() call already had."""
    global _our_handler
    fmt = os.environ.get("LOG_FORMAT", "text").strip().lower()
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root = logging.getLogger()
    if _our_handler is not None and _our_handler in root.handlers:
        root.removeHandler(_our_handler)
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    _our_handler = handler


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Logs one line per HTTP request. Deliberately excludes request/
    response bodies — this is a self-hosted home-automation API, and
    request bodies here can include things like password-change
    payloads; logging them would be a real credential-leak risk for
    no operational benefit method/path/status/timing don't already
    cover."""

    def __init__(self, app):
        super().__init__(app)
        self._log = logging.getLogger("hvac.access")

    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 1)
        self._log.info(
            f"{request.method} {request.url.path} {response.status_code} {duration_ms}ms",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_ip": request.client.host if request.client else None,
            },
        )
        return response
