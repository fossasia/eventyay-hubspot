import logging
import re

import requests

from .services import get_valid_hubspot_token

logger = logging.getLogger(__name__)


class HubSpotAPIError(Exception):
    """Base for all HubSpot API errors."""

    def __init__(self, message, status_code=None, response_body=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class HubSpotTransientError(HubSpotAPIError):
    """5xx, 429, timeouts — safe to retry."""

    def __init__(
        self, message, status_code=None, response_body=None, retry_after_seconds=None
    ):
        super().__init__(message, status_code, response_body)
        self.retry_after_seconds = retry_after_seconds


class HubSpotPermanentError(HubSpotAPIError):
    """4xx (except 409) — do not retry."""

    pass


class HubSpotRecordNotFoundError(HubSpotPermanentError):
    """404 Not Found — record was deleted in HubSpot."""

    pass


def _raise_for_status(response: requests.Response) -> None:
    """Raise HubSpotTransientError or HubSpotPermanentError based on status code."""
    if response.ok:
        return

    status_code = response.status_code
    response_body = None
    try:
        response_body = response.json()
    except ValueError:
        response_body = response.text

    message = f"HubSpot API error: {status_code} {response.reason}"
    if isinstance(response_body, dict) and "message" in response_body:
        message += f" - {response_body['message']}"

    if status_code in (429, 500, 502, 503, 504):
        retry_after = response.headers.get("Retry-After")
        retry_after_seconds = None
        if retry_after:
            try:
                retry_after_seconds = int(retry_after)
            except ValueError:
                pass
        raise HubSpotTransientError(
            message=message,
            status_code=status_code,
            response_body=response_body,
            retry_after_seconds=retry_after_seconds,
        )

    if status_code == 404:
        raise HubSpotRecordNotFoundError(
            message=message,
            status_code=status_code,
            response_body=response_body,
        )
    raise HubSpotPermanentError(
        message=message,
        status_code=status_code,
        response_body=response_body,
    )


def update_record(event, object_type: str, record_id: str, properties: dict) -> str:
    """
    Updates an existing HubSpot record.
    Raises HubSpotTransientError or HubSpotPermanentError on failure.
    Returns the record ID on success.
    """
    token = get_valid_hubspot_token(event)
    if not token:
        raise HubSpotPermanentError("Not connected to HubSpot or token is invalid.")

    object_type = {"contact": "contacts", "deal": "deals"}.get(object_type, object_type)
    if object_type not in {"contacts", "deals"}:
        raise HubSpotPermanentError(f"Unsupported HubSpot object type: {object_type}")

    url = f"https://api.hubapi.com/crm/v3/objects/{object_type}/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"properties": properties}

    try:
        response = requests.patch(url, headers=headers, json=payload, timeout=15)
    except requests.exceptions.Timeout as e:
        raise HubSpotTransientError(f"Request to HubSpot timed out: {e}")
    except requests.exceptions.ConnectionError as e:
        raise HubSpotTransientError(f"Connection to HubSpot failed: {e}")
    except requests.exceptions.RequestException as e:
        raise HubSpotTransientError(f"Request to HubSpot failed: {e}")

    _raise_for_status(response)
    return str(record_id)


def create_record(event, object_type: str, properties: dict) -> str:
    """
    Creates a new HubSpot record.
    If a 409 Conflict is returned with an existing ID, falls back to updating that record.
    Raises HubSpotTransientError or HubSpotPermanentError on failure.
    Returns the new or updated record ID on success.
    """
    token = get_valid_hubspot_token(event)
    if not token:
        raise HubSpotPermanentError("Not connected to HubSpot or token is invalid.")

    object_type = {"contact": "contacts", "deal": "deals"}.get(object_type, object_type)
    if object_type not in {"contacts", "deals"}:
        raise HubSpotPermanentError(f"Unsupported HubSpot object type: {object_type}")

    url = f"https://api.hubapi.com/crm/v3/objects/{object_type}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"properties": properties}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
    except requests.exceptions.Timeout as e:
        raise HubSpotTransientError(f"Request to HubSpot timed out: {e}")
    except requests.exceptions.ConnectionError as e:
        raise HubSpotTransientError(f"Connection to HubSpot failed: {e}")
    except requests.exceptions.RequestException as e:
        raise HubSpotTransientError(f"Request to HubSpot failed: {e}")

    # Handle 409 Conflict specifically for fallback
    if response.status_code == 409:
        try:
            response_body = response.json()
        except ValueError:
            response_body = response.text

        error_msg = (
            response_body.get("message", "")
            if isinstance(response_body, dict)
            else str(response_body)
        )
        match = re.search(r"Existing ID:\s*(\d+)", error_msg)

        if match:
            existing_id = match.group(1)
            logger.info(
                f"HubSpot 409 Conflict for {object_type}. Falling back to update for ID {existing_id}."
            )
            return update_record(event, object_type, existing_id, properties)
        else:
            # If we get a 409 but can't extract the ID, treat it as a permanent error
            raise HubSpotPermanentError(
                message=f"HubSpot API conflict error but unable to extract existing ID: {error_msg}",
                status_code=409,
                response_body=response_body,
            )

    # Handle other statuses via the common helper
    _raise_for_status(response)

    return str(response.json()["id"])


def get_record(event, object_type: str, record_id: str, properties: list) -> dict:
    """
    Fetches an existing HubSpot record to get its properties.
    Raises HubSpotTransientError or HubSpotPermanentError on failure.
    Returns the record's properties dict.
    """
    token = get_valid_hubspot_token(event)
    if not token:
        raise HubSpotPermanentError("Not connected to HubSpot or token is invalid.")

    object_type = {"contact": "contacts", "deal": "deals"}.get(object_type, object_type)
    if object_type not in {"contacts", "deals"}:
        raise HubSpotPermanentError(f"Unsupported HubSpot object type: {object_type}")

    url = f"https://api.hubapi.com/crm/v3/objects/{object_type}/{record_id}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"properties": ",".join(properties)} if properties else {}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=15)
    except requests.exceptions.Timeout as e:
        raise HubSpotTransientError(f"Request to HubSpot timed out: {e}")
    except requests.exceptions.ConnectionError as e:
        raise HubSpotTransientError(f"Connection to HubSpot failed: {e}")
    except requests.exceptions.RequestException as e:
        raise HubSpotTransientError(f"Request to HubSpot failed: {e}")

    _raise_for_status(response)
    data = response.json()
    return data.get("properties", {})
