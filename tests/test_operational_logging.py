"""Privacy and fail-open behavior of plugin operational logs."""

import logging

import pytest
import requests

import hubspot.operational_log as operational_log
from hubspot.operational_log import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    log_operation,
    logged_request,
    traced_job,
)


@pytest.fixture
def captured(monkeypatch):
    records = []

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)
    # Force the fallback logger even when the host package is installed.
    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", None)
    monkeypatch.setattr(operational_log, "_HOST_BIND_JOB_ID", None)
    monkeypatch.setattr(operational_log, "_HOST_RESET_JOB_ID", None)
    monkeypatch.setattr(operational_log, "_HOST_GET_JOB_ID", None)
    return records


def test_drops_email_token_and_unknown_fields(captured):
    log_operation(
        "plugin.loaded",
        OUTCOME_SUCCESS,
        backend="hubspot",
        email="person@example.com",
        token="secret-token",
        body="{raw}",
        event_id=12,
        payment_provider="stripe",
        order_code="ABC12",
        webhook_id=99,
        recipient_count=5,
    )
    assert len(captured) == 1
    extra = captured[0][2]
    assert extra["backend"] == "hubspot"
    assert extra["event_id"] == 12
    assert extra["outcome"] == "success"
    assert "email" not in extra
    assert "token" not in extra
    assert "body" not in extra
    assert "payment_provider" not in extra
    assert "order_code" not in extra
    assert "webhook_id" not in extra
    assert "recipient_count" not in extra
    blob = str(extra)
    assert "person@example.com" not in blob
    assert "secret-token" not in blob


def test_rejects_unsafe_action_and_backend(captured):
    log_operation("has space", OUTCOME_SUCCESS, backend="hubspot")
    log_operation("connection.request", OUTCOME_FAILURE, backend="pay pal", error_code="not safe")
    assert len(captured) == 1
    extra = captured[0][2]
    assert "backend" not in extra
    assert "error_code" not in extra
    assert "not safe" not in str(captured)
    assert all(item[1] != "has space" for item in captured)


def test_logging_failure_is_swallowed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", boom)
    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", boom)
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="hubspot")


def test_host_emitter_fallthrough_on_failure(monkeypatch):
    records = []

    def boom(*args, **kwargs):
        raise RuntimeError("host emitter failed")

    def fake_log(level, action, extra=None):
        records.append((level, action, extra or {}))

    monkeypatch.setattr(operational_log, "_HOST_LOG_EVENT", boom)
    monkeypatch.setattr(logging.getLogger("eventyay.plugins"), "log", fake_log)

    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="hubspot")
    assert len(records) == 1
    assert records[0][1] == "plugin.loaded"
    assert records[0][2]["backend"] == "hubspot"


def test_logged_request_records_status_without_url(captured, monkeypatch):
    class Response:
        status_code = 201

    def fake_post(url, **kwargs):
        assert url == "https://example.invalid/secret"
        assert kwargs["json"]["token"] == "raw"
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    logged_request("hubspot", "POST", "https://example.invalid/secret", json={"token": "raw"})
    assert captured[-1][1] == "connection.request"
    extra = captured[-1][2]
    assert extra["status"] == 201
    assert extra["outcome"] == "success"
    assert "example.invalid" not in str(extra)
    assert "raw" not in str(extra)


def test_logged_request_honors_verb_mocks(captured, monkeypatch):
    class Response:
        status_code = 200

    called = []
    for verb in ("get", "patch", "delete"):
        monkeypatch.setattr(requests, verb, lambda url, v=verb, **kw: called.append(v) or Response())
        logged_request("hubspot", verb.upper(), "https://api.hubapi.com/test")

    assert called == ["get", "patch", "delete"]
    assert len(captured) == 3


def test_traced_job_correlates_job_id_and_resets(captured):
    @traced_job("hubspot.test_job")
    def run_job():
        log_operation("sync.order", OUTCOME_SUCCESS, backend="hubspot", event_id=7, order_id=11)
        return "ok"

    assert run_job() == "ok"
    assert [item[1] for item in captured] == ["job.start", "sync.order", "job.finish"]
    job_ids = {item[2].get("job_id") for item in captured}
    assert len(job_ids) == 1
    assert None not in job_ids
    assert operational_log._current_job_id() is None

    captured.clear()
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend="hubspot")
    assert "job_id" not in captured[0][2]
