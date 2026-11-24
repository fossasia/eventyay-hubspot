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
    task_job_id,
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
    monkeypatch.setattr(operational_log, "_HOST_GET_REQUEST_ID", None)
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
        assert "operation" not in kwargs
        assert "retry_count" not in kwargs
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    logged_request(
        "hubspot",
        "POST",
        "https://example.invalid/secret",
        operation="POST:crm.objects.create",
        retry_count=2,
        json={"token": "raw"},
    )
    assert captured[-1][1] == "connection.request"
    extra = captured[-1][2]
    assert extra["status"] == 201
    assert extra["outcome"] == "success"
    assert extra["route"] == "POST:crm.objects.create"
    assert extra["retry_count"] == 2
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
    assert [item[2]["route"] for item in captured] == ["GET", "PATCH", "DELETE"]


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


def test_traced_job_correlates_celery_task_id_and_request_id(captured):
    class FakeResult:
        id = "celery-task-123"

    class FakeNonStringMock:
        id = object()

    class FakeTask:
        class Request:
            id = "celery-task-123"

        request = Request()

    assert task_job_id(FakeResult()) == "celery-task-123"
    assert task_job_id(FakeNonStringMock()) is None

    @traced_job("hubspot.sync_order")
    def run_task(self):
        log_operation("sync.order", OUTCOME_SUCCESS, backend="hubspot", event_id=3, order_id=9)

    token = operational_log._REQUEST_ID.set("req-abc-123")
    try:
        log_operation("sync.order_enqueue", OUTCOME_SUCCESS, backend="hubspot", job_id=task_job_id(FakeResult()))
        run_task(FakeTask())
    finally:
        operational_log._REQUEST_ID.reset(token)

    assert [item[1] for item in captured] == ["sync.order_enqueue", "job.start", "sync.order", "job.finish"]
    assert {item[2]["job_id"] for item in captured} == {"celery-task-123"}
    assert {item[2]["request_id"] for item in captured} == {"req-abc-123"}


def test_logged_request_http_failure(captured, monkeypatch):
    class ErrorResponse:
        status_code = 500

    monkeypatch.setattr(requests, "get", lambda url, **kw: ErrorResponse())

    logged_request(
        "hubspot", "GET", "https://api.hubapi.com/crm/v3/objects/contacts/123", operation="GET:crm.objects.get"
    )
    assert captured[-1][1] == "connection.request"
    extra = captured[-1][2]
    assert extra["outcome"] == "failure"
    assert extra["status"] == 500
    assert extra["error_code"] == "http_error"
    assert extra["route"] == "GET:crm.objects.get"


def test_logged_request_tolerates_ok_statuses(captured, monkeypatch):
    class ConflictResponse:
        status_code = 409

    class NotFoundResponse:
        status_code = 404

    monkeypatch.setattr(requests, "post", lambda url, **kw: ConflictResponse())
    logged_request(
        "hubspot",
        "POST",
        "https://api.hubapi.com/crm/v3/objects/contacts",
        operation="POST:crm.objects.create",
        ok_statuses=(409,),
    )
    extra_409 = captured[-1][2]
    assert extra_409["outcome"] == "success"
    assert extra_409["status"] == 409
    assert "error_code" not in extra_409

    monkeypatch.setattr(requests, "patch", lambda url, **kw: NotFoundResponse())
    logged_request(
        "hubspot",
        "PATCH",
        "https://api.hubapi.com/crm/v3/objects/contacts/123",
        operation="PATCH:crm.objects.update",
        ok_statuses=(404,),
    )
    extra_404 = captured[-1][2]
    assert extra_404["outcome"] == "success"
    assert extra_404["status"] == 404
    assert "error_code" not in extra_404


def test_domain_failure_drops_sensitive_fields(captured):
    log_operation(
        "validation.failed",
        OUTCOME_FAILURE,
        backend="hubspot",
        event_id=42,
        user_id=10,
        error_code="validation_failed",
        email="operator@example.org",
        secret_token="tok_live_123",
        raw_error="Internal database details at postgres://...",
    )
    assert len(captured) == 1
    action = captured[0][1]
    extra = captured[0][2]
    assert action == "validation.failed"
    assert extra["outcome"] == "failure"
    assert extra["error_code"] == "validation_failed"
    assert extra["event_id"] == 42
    assert extra["user_id"] == 10
    assert "email" not in extra
    assert "secret_token" not in extra
    assert "raw_error" not in extra
    assert "operator@example.org" not in str(extra)
    assert "tok_live_123" not in str(extra)
    assert "postgres" not in str(extra)
