import datetime
from unittest import mock

import pytest
import requests
from django.utils.timezone import now
from django_scopes import scope

from hubspot.client import (
    HubSpotPermanentError,
    HubSpotTransientError,
    create_record,
    get_record,
    update_record,
)
from hubspot.models import HubSpotOAuthToken


@pytest.fixture
def hubspot_token(event):
    with scope(organizer=event.organizer):
        return HubSpotOAuthToken.objects.create(
            event=event,
            access_token="old_access",
            refresh_token="old_refresh",
            expires_at=now() + datetime.timedelta(hours=1),
        )


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_create_record_success(mock_post, event, hubspot_token):
    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.status_code = 201
    mock_response.json.return_value = {"id": "12345"}
    mock_post.return_value = mock_response

    record_id = create_record(event, "contact", {"firstname": "John"})

    assert record_id == "12345"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert "/crm/v3/objects/contacts" in args[0]
    assert kwargs["json"]["properties"] == {"firstname": "John"}
    assert kwargs["headers"]["Authorization"] == "Bearer old_access"


@pytest.mark.django_db
def test_create_record_no_token(event, caplog):
    # No token created
    with pytest.raises(HubSpotPermanentError, match="Not connected to HubSpot"):
        create_record(event, "contact", {"firstname": "John"})


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.patch")
def test_update_record_success(mock_patch, event, hubspot_token):
    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "12345"}
    mock_patch.return_value = mock_response

    record_id = update_record(event, "contact", "12345", {"firstname": "John"})

    assert record_id == "12345"
    mock_patch.assert_called_once()
    args, kwargs = mock_patch.call_args
    assert "/crm/v3/objects/contacts/12345" in args[0]
    assert kwargs["json"]["properties"] == {"firstname": "John"}


@pytest.mark.django_db
def test_update_record_no_token(event, caplog):
    with pytest.raises(HubSpotPermanentError, match="Not connected to HubSpot"):
        update_record(event, "contact", "12345", {"firstname": "John"})


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
@mock.patch("hubspot.client.requests.patch")
def test_create_409_falls_back_to_update(mock_patch, mock_post, event, hubspot_token):
    mock_post_response = mock.Mock()
    mock_post_response.ok = False
    mock_post_response.status_code = 409
    mock_post_response.json.return_value = {
        "message": "Contact already exists. Existing ID: 98765"
    }
    mock_post.return_value = mock_post_response

    mock_patch_response = mock.Mock()
    mock_patch_response.ok = True
    mock_patch_response.status_code = 200
    mock_patch.return_value = mock_patch_response

    record_id = create_record(event, "contact", {"firstname": "John"})

    assert record_id == "98765"
    mock_post.assert_called_once()
    mock_patch.assert_called_once()

    args, kwargs = mock_patch.call_args
    assert "/crm/v3/objects/contacts/98765" in args[0]


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_create_409_unparseable_id(mock_post, event, hubspot_token):
    mock_post_response = mock.Mock()
    mock_post_response.ok = False
    mock_post_response.status_code = 409
    mock_post_response.json.return_value = {"message": "Conflict without an ID"}
    mock_post.return_value = mock_post_response

    with pytest.raises(HubSpotPermanentError) as exc_info:
        create_record(event, "contact", {"firstname": "John"})

    assert exc_info.value.status_code == 409
    assert "unable to extract existing ID" in str(exc_info.value)


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_5xx_raises_transient(mock_post, event, hubspot_token):
    for status in [500, 502, 503, 504]:
        mock_response = mock.Mock()
        mock_response.ok = False
        mock_response.status_code = status
        mock_response.json.side_effect = ValueError()
        mock_response.text = "Internal Server Error"
        mock_response.headers = {}
        mock_post.return_value = mock_response

        with pytest.raises(HubSpotTransientError) as exc_info:
            create_record(event, "contact", {"firstname": "John"})

        assert exc_info.value.status_code == status


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_429_raises_transient_with_retry_after(mock_post, event, hubspot_token):
    mock_response = mock.Mock()
    mock_response.ok = False
    mock_response.status_code = 429
    mock_response.json.return_value = {"message": "Rate limit exceeded"}
    mock_response.headers = {"Retry-After": "10"}
    mock_post.return_value = mock_response

    with pytest.raises(HubSpotTransientError) as exc_info:
        create_record(event, "contact", {"firstname": "John"})

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 10


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_4xx_raises_permanent(mock_post, event, hubspot_token):
    for status in [400, 401, 403, 404]:
        mock_response = mock.Mock()
        mock_response.ok = False
        mock_response.status_code = status
        mock_response.json.return_value = {"message": "Bad request"}
        mock_post.return_value = mock_response

        with pytest.raises(HubSpotPermanentError) as exc_info:
            create_record(event, "contact", {"firstname": "John"})

        assert exc_info.value.status_code == status


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_timeout_raises_transient(mock_post, event, hubspot_token):
    mock_post.side_effect = requests.exceptions.Timeout("Timeout")

    with pytest.raises(HubSpotTransientError, match="Request to HubSpot timed out"):
        create_record(event, "contact", {"firstname": "John"})


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.post")
def test_connection_error_raises_transient(mock_post, event, hubspot_token):
    mock_post.side_effect = requests.exceptions.ConnectionError("Connection error")

    with pytest.raises(HubSpotTransientError, match="Connection to HubSpot failed"):
        create_record(event, "contact", {"firstname": "John"})


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.get")
def test_get_record_success(mock_get, event, hubspot_token):
    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {"properties": {"firstname": "John"}}
    mock_get.return_value = mock_response

    properties = get_record(event, "contact", "12345", ["firstname"])

    assert properties == {"firstname": "John"}
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert "contacts" in args[0]
    assert "12345" in args[0]
    assert kwargs["params"] == {"properties": "firstname"}


@pytest.mark.django_db
@mock.patch("hubspot.client.requests.get")
def test_get_record_failure(mock_get, event, hubspot_token):
    mock_response = mock.Mock()
    mock_response.ok = False
    mock_response.status_code = 500
    mock_response.json.side_effect = ValueError()
    mock_response.text = "Internal Server Error"
    mock_response.headers = {}
    mock_get.return_value = mock_response

    with pytest.raises(HubSpotTransientError) as exc_info:
        get_record(event, "contact", "12345", ["firstname"])

    assert exc_info.value.status_code == 500
