"""Structured logging + request IDs (Phase 5c-1 / B-13).

One small observability seam for the backend:

- A ``contextvars``-held request ID. The ASGI middleware in this
  module (:class:`RequestIdMiddleware`, wired in main.py) honours
  an incoming ``X-Request-ID`` header or generates a uuid4 hex,
  stores it on ``request.state`` / the ASGI scope, echoes it in
  the response header, and binds it to the logging context for
  the duration of the request — so every structured log record
  emitted while handling that request carries the same ID.
- A ``cinemora`` logger tree. :func:`get_logger` hands out
  loggers under that tree, each carrying a filter that stamps the
  current request ID onto every record. :func:`setup_logging`
  (idempotent, called from main.py at startup) attaches ONE
  handler whose formatter emits key=value lines including
  ``request_id`` when present, plus any structured ``fields``
  passed via :func:`log_event`.

Scope note (honest): this is the structured path for the hot
paths that have adopted it (provider calls, dispatcher, jobs run
lifecycle, search calls, HTTP requests). The rest of the codebase
still uses its pre-existing ``logging.getLogger(__name__)``
loggers and print() calls — no repo-wide purge was performed.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
import uuid

ROOT_LOGGER_NAME = "cinemora"

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cinemora_request_id", default=None
)

# An incoming X-Request-ID is honoured only when it is a short,
# header/log-safe token; anything else is replaced with a fresh
# generated ID (a caller-supplied value is echoed into response
# headers and log lines, so it must not carry control characters
# or whitespace that could split log records).
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


def current_request_id() -> str | None:
    """The request ID bound to the current context, if any."""
    return _request_id_var.get()


def set_request_id(request_id: str | None):
    """Bind a request ID to the current context; returns the
    contextvars token (pass it to :func:`reset_request_id`)."""
    return _request_id_var.set(request_id)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)


def new_request_id() -> str:
    return uuid.uuid4().hex


def sanitize_request_id(value) -> str | None:
    """The incoming header value if it is safe to honour, else
    None (caller generates a fresh one)."""
    if value is None:
        return None
    value = str(value).strip()
    if _REQUEST_ID_RE.match(value):
        return value
    return None


class RequestIdFilter(logging.Filter):
    """Stamps ``record.request_id`` from the logging context."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = _request_id_var.get()
        return True


_REQUEST_ID_FILTER = RequestIdFilter()


def _format_value(value) -> str:
    text = str(value)
    if text == "" or any(ch.isspace() for ch in text) or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


class StructuredFormatter(logging.Formatter):
    """key=value lines: ``ts level logger request_id msg k=v ...``.

    ``request_id`` comes from the record when the filter stamped
    one, else from the logging context at format time, else ``-``.
    Structured fields attached via :func:`log_event` (record
    attribute ``fields``, a dict) are appended sorted by key.
    """

    def format(self, record: logging.LogRecord) -> str:
        request_id = (
            getattr(record, "request_id", None)
            or _request_id_var.get()
            or "-"
        )
        parts = [
            self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            f"level={record.levelname}",
            f"logger={record.name}",
            f"request_id={request_id}",
            f"msg={_format_value(record.getMessage())}",
        ]
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            for key in sorted(fields):
                parts.append(f"{key}={_format_value(fields[key])}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def get_logger(name: str) -> logging.Logger:
    """A logger in the ``cinemora`` tree (``name`` is the short
    component, e.g. ``"jobs"`` -> ``cinemora.jobs``). The request-ID
    filter is attached once per logger so records carry the ID
    even when a foreign handler (e.g. pytest's caplog) renders
    them."""
    logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")
    if _REQUEST_ID_FILTER not in logger.filters:
        logger.addFilter(_REQUEST_ID_FILTER)
    return logger


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """Emit one structured event: the event name is the message,
    ``fields`` ride along as key=value pairs in the formatted
    line. None-valued fields are dropped (unknown stays absent,
    never rendered as a fabricated value)."""
    clean = {k: v for k, v in fields.items() if v is not None}
    logger.log(level, event, extra={"fields": clean})


_SETUP_MARKER = "_cinemora_structured_handler"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the ``cinemora`` logger tree. Idempotent: repeat
    calls return the already-configured root logger without
    stacking handlers. Propagation stays ON so embedding/test
    harnesses (caplog, uvicorn root handlers) still see records."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(level)
    if _REQUEST_ID_FILTER not in root.filters:
        root.addFilter(_REQUEST_ID_FILTER)
    for handler in root.handlers:
        if getattr(handler, _SETUP_MARKER, False):
            return root
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(_REQUEST_ID_FILTER)
    setattr(handler, _SETUP_MARKER, True)
    root.addHandler(handler)
    return root


class RequestIdMiddleware:
    """Pure-ASGI request-ID middleware.

    Honours a safe incoming ``X-Request-ID`` or generates one,
    binds it to the logging context + ``scope["state"]`` (so
    ``request.state.request_id`` works), echoes it as the
    ``X-Request-ID`` response header, and emits one structured
    ``http_request`` completion event per request.
    """

    def __init__(self, app):
        self.app = app
        self._logger = get_logger("http")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        request_id = sanitize_request_id(headers.get("x-request-id"))
        if request_id is None:
            request_id = new_request_id()

        scope.setdefault("state", {})["request_id"] = request_id
        token = set_request_id(request_id)
        status_holder: dict = {}

        async def _send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status")
                response_headers = list(message.get("headers") or [])
                response_headers.append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
                message = dict(message, headers=response_headers)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            log_event(
                self._logger,
                logging.INFO,
                "http_request",
                method=scope.get("method"),
                path=scope.get("path"),
                status=status_holder.get("status"),
            )
            reset_request_id(token)
