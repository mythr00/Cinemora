"""Phase 5c-1 (B-13) — structured logging + request IDs.

Covers:
- the request-ID middleware generates an ID, echoes it in the
  X-Request-ID response header, and exposes it on request.state;
- a safe incoming X-Request-ID is honoured verbatim; an unsafe
  one is replaced, never echoed;
- the ID propagates into log records emitted during the request
  (caplog) via the logging context;
- the structured formatter renders request_id + fields as
  key=value;
- setup_logging() is idempotent (no stacked handlers).
"""

import logging
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import main
from services import logging_setup


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------
# Middleware on the real app
# ---------------------------------------------------------------------


def test_request_id_generated_and_echoed(client):
    response = client.get("/health")
    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id
    # Generated IDs are uuid4 hex.
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_incoming_request_id_honoured(client):
    response = client.get(
        "/health", headers={"X-Request-ID": "req-test-5c1-abc"}
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-test-5c1-abc"


def test_unsafe_incoming_request_id_replaced(client):
    response = client.get(
        "/health", headers={"X-Request-ID": "bad id with spaces"}
    )
    echoed = response.headers["x-request-id"]
    assert echoed != "bad id with spaces"
    assert re.fullmatch(r"[0-9a-f]{32}", echoed)


def test_request_id_reaches_log_record_during_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="cinemora.http"):
        response = client.get(
            "/health", headers={"X-Request-ID": "req-caplog-5c1"}
        )
    assert response.headers["x-request-id"] == "req-caplog-5c1"
    records = [
        record
        for record in caplog.records
        if record.name == "cinemora.http"
        and record.getMessage() == "http_request"
    ]
    assert records, "middleware emitted no http_request record"
    assert records[-1].request_id == "req-caplog-5c1"
    assert records[-1].fields["path"] == "/health"
    assert records[-1].fields["status"] == 200


# ---------------------------------------------------------------------
# Middleware in isolation: request.state carries the ID
# ---------------------------------------------------------------------


def test_request_state_carries_request_id():
    app = FastAPI()
    app.add_middleware(logging_setup.RequestIdMiddleware)

    @app.get("/whoami")
    def whoami(request: Request):
        return {"request_id": request.state.request_id}

    isolated = TestClient(app, raise_server_exceptions=False)
    response = isolated.get(
        "/whoami", headers={"X-Request-ID": "req-state-5c1"}
    )
    assert response.json() == {"request_id": "req-state-5c1"}
    assert response.headers["x-request-id"] == "req-state-5c1"


# ---------------------------------------------------------------------
# Logging context + formatter + idempotent setup
# ---------------------------------------------------------------------


def test_context_propagates_to_any_cinemora_logger(caplog):
    logger = logging_setup.get_logger("phase5c_test")
    token = logging_setup.set_request_id("req-context-5c1")
    try:
        with caplog.at_level(logging.INFO, logger="cinemora.phase5c_test"):
            logging_setup.log_event(
                logger, logging.INFO, "unit_event", answer=42
            )
    finally:
        logging_setup.reset_request_id(token)
    assert caplog.records[-1].request_id == "req-context-5c1"
    assert caplog.records[-1].fields == {"answer": 42}


def test_structured_formatter_renders_key_value():
    formatter = logging_setup.StructuredFormatter()
    record = logging.LogRecord(
        name="cinemora.jobs",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="run_started",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-fmt-5c1"
    record.fields = {"project_id": "p 1", "run_id": "r-1"}
    line = formatter.format(record)
    assert "level=INFO" in line
    assert "logger=cinemora.jobs" in line
    assert "request_id=req-fmt-5c1" in line
    assert "msg=run_started" in line
    assert 'project_id="p 1"' in line
    assert "run_id=r-1" in line


def test_setup_logging_idempotent():
    logging_setup.setup_logging()
    root = logging.getLogger("cinemora")
    marked = [
        handler
        for handler in root.handlers
        if getattr(handler, logging_setup._SETUP_MARKER, False)
    ]
    assert len(marked) == 1
    logging_setup.setup_logging()
    marked_after = [
        handler
        for handler in root.handlers
        if getattr(handler, logging_setup._SETUP_MARKER, False)
    ]
    assert len(marked_after) == 1
