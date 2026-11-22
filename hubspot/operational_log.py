"""Privacy-safe operational logs for this plugin.

Delegates to the host logger when it is importable. Otherwise uses
``eventyay.plugins``. Logging never raises into product code.

Emails, names, tokens, secrets, and request or response bodies are dropped.
Only opaque IDs, status codes, durations, and safe error codes are kept.
"""

from __future__ import annotations

import functools
import logging
import re
import time
import uuid
from contextvars import ContextVar

import requests
import requests.api as requests_api


try:
    from eventyay.base import operational_logging as _host_logging
except Exception:
    _HOST_LOG_EVENT = None
    _HOST_BIND_JOB_ID = None
    _HOST_RESET_JOB_ID = None
    _HOST_GET_JOB_ID = None
else:
    _HOST_LOG_EVENT = getattr(_host_logging, "log_event", None)
    _HOST_BIND_JOB_ID = getattr(_host_logging, "bind_job_id", None)
    _HOST_RESET_JOB_ID = getattr(_host_logging, "reset_job_id", None)
    _HOST_GET_JOB_ID = getattr(_host_logging, "get_job_id", None)


OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_LOGGER = logging.getLogger("eventyay.plugins")
_JOB_ID: ContextVar[str | None] = ContextVar("hubspot_job_id", default=None)
_INT_FIELDS = frozenset(
    {
        "event_id",
        "order_id",
        "user_id",
        "status",
        "duration_ms",
        "retry_count",
    }
)
_IDENTIFIER_FIELDS = frozenset(
    {
        "error_code",
        "backend",
        "job_name",
        "job_id",
        "model",
    }
)
_REQUEST_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _safe_identifier(value):
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def _current_job_id() -> str | None:
    local_id = _JOB_ID.get()
    if local_id is not None:
        return local_id
    if _HOST_GET_JOB_ID is not None:
        try:
            return _HOST_GET_JOB_ID()
        except Exception:
            return None
    return None


def _clean(fields):
    extra = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if key in _INT_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            extra[key] = value
            continue
        if key in _IDENTIFIER_FIELDS:
            cleaned = _safe_identifier(value if isinstance(value, str) else None)
            if cleaned is not None:
                extra[key] = cleaned
            continue
        if key == "object_id":
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                extra[key] = value
            else:
                cleaned = _safe_identifier(str(value))
                if cleaned is not None:
                    extra[key] = cleaned
    return extra


def log_operation(action, outcome, **fields):
    """Emit one allowlisted operational line. Failures are swallowed."""
    try:
        if not isinstance(action, str) or _safe_identifier(action) is None:
            return
        if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE):
            outcome = OUTCOME_FAILURE
        level = logging.WARNING if outcome == OUTCOME_FAILURE else logging.INFO
        payload = _clean(fields)
        if "job_id" not in payload:
            current_job_id = _safe_identifier(_current_job_id())
            if current_job_id is not None:
                payload["job_id"] = current_job_id
        if _HOST_LOG_EVENT is not None:
            try:
                _HOST_LOG_EVENT("plugins", action, outcome, level=level, **payload)
                return
            except Exception:
                pass
        extra = {
            "component": "plugins",
            "outcome": outcome,
            "action": action,
            **payload,
        }
        _LOGGER.log(level, action, extra=extra)
    except Exception:
        return


def log_plugin_loaded(backend):
    log_operation("plugin.loaded", OUTCOME_SUCCESS, backend=backend)


def _elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def _send_request(method, url, **kwargs):
    """Call requests without changing mocks or redirect defaults.

    A patched ``requests.get`` or ``requests.post`` wins. Otherwise a patched
    ``requests.request`` wins. Unpatched calls use the verb helper.
    """
    verb_name = str(method).lower()
    if verb_name in _REQUEST_VERBS:
        patched = getattr(requests, verb_name, None)
        real = getattr(requests_api, verb_name, None)
        if patched is not None and patched is not real:
            return patched(url, **kwargs)  # codeql[py/full-ssrf]
    if requests.request is not requests_api.request:
        return requests.request(method, url, **kwargs)  # codeql[py/full-ssrf]
    if verb_name in _REQUEST_VERBS:
        real = getattr(requests_api, verb_name)
        return real(url, **kwargs)  # codeql[py/full-ssrf]
    return requests_api.request(method, url, **kwargs)  # codeql[py/full-ssrf]


def logged_request(backend, method, url, **kwargs):
    """Log status and duration for one HTTP call.

    ``ok_statuses`` is not sent to the server. Use it when the caller already
    treats a 4xx response as success. The same exception is re-raised.
    The URL and body are not logged.
    """
    ok_statuses = kwargs.pop("ok_statuses", ())
    started = time.monotonic()
    try:
        response = _send_request(method, url, **kwargs)
    except requests.Timeout:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend=backend,
            error_code="timeout",
            duration_ms=_elapsed_ms(started),
        )
        raise
    except requests.RequestException:
        log_operation(
            "connection.request",
            OUTCOME_FAILURE,
            backend=backend,
            error_code="request_error",
            duration_ms=_elapsed_ms(started),
        )
        raise
    status = getattr(response, "status_code", None)
    status_ok = isinstance(status, int) and not isinstance(status, bool)
    tolerated = status_ok and status in set(ok_statuses)
    failed = status_ok and status >= 400 and not tolerated
    log_operation(
        "connection.request",
        OUTCOME_FAILURE if failed else OUTCOME_SUCCESS,
        backend=backend,
        status=status if status_ok else None,
        duration_ms=_elapsed_ms(started),
        error_code="http_error" if failed else None,
    )
    return response


def traced_job(job_name):
    """Log job boundaries with job_id correlation without changing behavior."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            existing_job_id = _current_job_id()
            job_id = _safe_identifier(existing_job_id) or uuid.uuid4().hex
            token = _JOB_ID.set(job_id)
            bound_host = False
            if not existing_job_id and _HOST_BIND_JOB_ID is not None:
                try:
                    _HOST_BIND_JOB_ID(job_id)
                    bound_host = True
                except Exception:
                    bound_host = False
            started = time.monotonic()
            log_operation("job.start", OUTCOME_SUCCESS, job_name=job_name, job_id=job_id)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                retried = type(exc).__name__ == "Retry"
                log_operation(
                    "job.retry" if retried else "job.fail",
                    OUTCOME_FAILURE,
                    job_name=job_name,
                    job_id=job_id,
                    duration_ms=_elapsed_ms(started),
                    error_code="retry" if retried else type(exc).__name__,
                )
                raise
            else:
                log_operation(
                    "job.finish",
                    OUTCOME_SUCCESS,
                    job_name=job_name,
                    job_id=job_id,
                    duration_ms=_elapsed_ms(started),
                )
                return result
            finally:
                if bound_host and _HOST_RESET_JOB_ID is not None:
                    try:
                        _HOST_RESET_JOB_ID()
                    except Exception:
                        pass
                _JOB_ID.reset(token)

        return wrapper

    return decorator
