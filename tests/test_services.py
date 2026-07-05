import datetime
import uuid
from unittest import mock

import pytest
import requests
from django.utils.timezone import now
from django_scopes import scope

from hubspot.models import (
    HubSpotOAuthToken,
    HubSpotProperty,
    HubSpotPropertySyncState,
)
from hubspot.services import (
    HubSpotFetchError,
    get_hubspot_properties,
    get_valid_hubspot_token,
)


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
def test_valid_token_returned_as_is(event, hubspot_token):
    # Token has 1 hour left, should be returned as is
    with scope(organizer=event.organizer):
        token_str = get_valid_hubspot_token(event)
        assert token_str == "old_access"


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.post")
def test_token_expiring_soon_is_refreshed(mock_post, event, hubspot_token):
    # Set expiration to 4 minutes from now
    with scope(organizer=event.organizer):
        hubspot_token.expires_at = now() + datetime.timedelta(minutes=4)
        hubspot_token.save()

    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_in": 1800,
    }
    mock_post.return_value = mock_response

    token_str = get_valid_hubspot_token(event)

    assert token_str == "new_access"
    mock_post.assert_called_once()

    with scope(organizer=event.organizer):
        hubspot_token.refresh_from_db()
        assert hubspot_token.access_token == "new_access"
        assert hubspot_token.refresh_token == "new_refresh"
        assert hubspot_token.expires_at > now() + datetime.timedelta(minutes=20)


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.post")
def test_concurrent_refresh_attempts(mock_post, event, hubspot_token):
    # To test select_for_update we usually need threading or special DB setup
    # But we can at least assert that we are calling select_for_update.
    # A true concurrent test is hard in sqlite but we can mock select_for_update.
    with mock.patch(
        "hubspot.models.HubSpotOAuthToken.objects.select_for_update"
    ) as mock_sfu:
        mock_qs = mock.Mock()
        mock_sfu.return_value = mock_qs
        mock_qs.get.return_value = hubspot_token

        with scope(organizer=event.organizer):
            get_valid_hubspot_token(event)

        mock_sfu.assert_called_once()


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.get")
def test_get_hubspot_properties_success_and_db_cache(mock_get, event, hubspot_token):
    with scope(organizer=event.organizer):
        HubSpotProperty.objects.all().delete()
        HubSpotPropertySyncState.objects.all().delete()

    mock_response_1 = mock.Mock()
    mock_response_1.ok = True
    mock_response_1.json.return_value = {
        "results": [
            {"name": "firstname", "label": "First Name", "type": "string"},
            {"name": "age", "label": "Age", "type": "number"},
        ],
        "paging": {"next": {"after": "page2"}},
    }

    mock_response_2 = mock.Mock()
    mock_response_2.ok = True
    mock_response_2.json.return_value = {
        "results": [
            {"name": "createdate", "label": "Create Date", "type": "datetime"},
            {"name": "is_active", "label": "Is Active", "type": "bool"},
        ]
    }
    mock_get.side_effect = [mock_response_1, mock_response_2]

    with scope(organizer=event.organizer):
        properties = get_hubspot_properties(event, "contact")

    assert len(properties) == 4
    assert properties[0] == {
        "key": "firstname",
        "label": "First Name",
        "data_type": "text",
    }
    assert properties[1] == {"key": "age", "label": "Age", "data_type": "number"}
    assert properties[2] == {
        "key": "createdate",
        "label": "Create Date",
        "data_type": "date",
    }
    assert properties[3] == {
        "key": "is_active",
        "label": "Is Active",
        "data_type": "yes/no",
    }

    assert mock_get.call_count == 2

    # Second call should use DB and not hit API
    with scope(organizer=event.organizer):
        properties2 = get_hubspot_properties(event, "contact")

    assert properties == properties2
    assert mock_get.call_count == 2


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.get")
def test_get_hubspot_properties_ttl_expiry(mock_get, event, hubspot_token):
    with scope(organizer=event.organizer):
        HubSpotProperty.objects.all().delete()
        HubSpotPropertySyncState.objects.all().delete()

    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "results": [
            {"name": "firstname", "label": "First Name", "type": "string"},
        ]
    }
    mock_get.return_value = mock_response

    with scope(organizer=event.organizer):
        properties = get_hubspot_properties(event, "contact")

    assert len(properties) == 1
    assert mock_get.call_count == 1

    # Simulate TTL expiry
    with scope(organizer=event.organizer):
        sync_state = HubSpotPropertySyncState.objects.get(
            event=event, object_type="contact"
        )
        sync_state.completed_at = now() - datetime.timedelta(minutes=15)
        sync_state.save()

    # Second call should hit API again
    with scope(organizer=event.organizer):
        properties2 = get_hubspot_properties(event, "contact")

    assert len(properties2) == 1
    assert mock_get.call_count == 2


@pytest.mark.django_db
def test_get_hubspot_properties_no_token(event, caplog):
    with scope(organizer=event.organizer):
        HubSpotProperty.objects.all().delete()
        HubSpotPropertySyncState.objects.all().delete()

    with scope(organizer=event.organizer):
        with pytest.raises(HubSpotFetchError):
            get_hubspot_properties(event, "contact")

    assert "Not connected to HubSpot or token is invalid" in caplog.text


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.get")
def test_get_hubspot_properties_api_failure(mock_get, event, hubspot_token, caplog):
    with scope(organizer=event.organizer):
        HubSpotProperty.objects.all().delete()
        HubSpotPropertySyncState.objects.all().delete()

    mock_get.side_effect = requests.RequestException("API error")

    with scope(organizer=event.organizer):
        with pytest.raises(HubSpotFetchError):
            get_hubspot_properties(event, "contact")
    assert "Failed to fetch properties from HubSpot" in caplog.text


@pytest.mark.django_db
@mock.patch("hubspot.services.requests.get")
def test_sync_resumes_after_crash(mock_get, event, hubspot_token):
    with scope(organizer=event.organizer):
        HubSpotProperty.objects.all().delete()
        HubSpotPropertySyncState.objects.all().delete()

    # Simulate an interrupted sync with cursor="page3"
    with scope(organizer=event.organizer):
        HubSpotPropertySyncState.objects.create(
            event=event,
            object_type="contact",
            sync_batch=uuid.uuid4(),
            next_cursor="page3",
            is_complete=False,
        )

    mock_response = mock.Mock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "results": [
            {"name": "new_prop", "label": "New", "type": "string"},
        ]
    }
    mock_get.return_value = mock_response

    with scope(organizer=event.organizer):
        properties = get_hubspot_properties(event, "contact")

    assert len(properties) == 1
    assert properties[0] == {"key": "new_prop", "label": "New", "data_type": "text"}

    # Assert that the API was called with the cursor "page3"
    mock_get.assert_called_once()
    args, kwargs = mock_get.call_args
    assert kwargs["params"] == {"after": "page3"}
