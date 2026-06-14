import pytest
import responses
from django.contrib.messages import get_messages
from django.urls import reverse

from django_scopes import scope

from hubspot.models import HubSpotOAuthToken, SyncLog, SyncAction


@pytest.mark.django_db
def test_connect_view_redirects_to_hubspot(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    url = reverse(
        "plugins:hubspot:connect",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    assert response.status_code == 302
    assert "https://app.hubspot.com/oauth/authorize" in response.url
    assert "client_id=" in response.url
    assert "state=" in response.url

    assert "hubspot_oauth_state" in logged_in_organizer_client.session


@pytest.mark.django_db
@responses.activate
def test_callback_view_success(logged_in_organizer_client, organizer, event, settings):
    settings.SITE_URL = "https://testserver"

    responses.add(
        responses.POST,
        "https://api.hubapi.com/oauth/v1/token",
        json={
            "access_token": "acc_123",
            "refresh_token": "ref_123",
            "expires_in": 1800,
            "token_type": "bearer",
        },
        status=200,
    )

    session = logged_in_organizer_client.session
    session["hubspot_oauth_state"] = "valid_state"
    session.save()

    url = reverse("plugins:hubspot:callback")
    state_param = f"valid_state:{organizer.slug}:{event.slug}"
    response = logged_in_organizer_client.get(
        url, {"state": state_param, "code": "auth_code"}
    )

    assert response.status_code == 302
    assert response.url == reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )

    with scope(organizer=organizer):
        token = HubSpotOAuthToken.objects.get(event=event)
        assert token.access_token == "acc_123"
        assert token.refresh_token == "ref_123"

        assert SyncLog.objects.filter(event=event, action=SyncAction.CONNECT).exists()


@pytest.mark.django_db
def test_callback_view_invalid_state(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    session = logged_in_organizer_client.session
    session["hubspot_oauth_state"] = "valid_state"
    session.save()

    url = reverse("plugins:hubspot:callback")
    state_param = f"invalid_state:{organizer.slug}:{event.slug}"
    response = logged_in_organizer_client.get(
        url, {"state": state_param, "code": "auth_code"}
    )

    assert response.status_code == 302
    messages = list(get_messages(response.wsgi_request))
    assert any("Invalid state parameter" in str(m) for m in messages)
    with scope(organizer=organizer):
        assert not HubSpotOAuthToken.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_callback_view_hubspot_error(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    url = reverse("plugins:hubspot:callback")
    state_param = f"any_state:{organizer.slug}:{event.slug}"
    response = logged_in_organizer_client.get(
        url,
        {
            "state": state_param,
            "error": "access_denied",
            "error_description": "User denied access",
        },
    )

    assert response.status_code == 302
    messages = list(get_messages(response.wsgi_request))
    assert any("HubSpot authorization failed" in str(m) for m in messages)


@pytest.mark.django_db
@responses.activate
def test_callback_view_api_error(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    responses.add(
        responses.POST,
        "https://api.hubapi.com/oauth/v1/token",
        json={"message": "invalid request"},
        status=400,
    )

    session = logged_in_organizer_client.session
    session["hubspot_oauth_state"] = "valid_state"
    session.save()

    url = reverse("plugins:hubspot:callback")
    state_param = f"valid_state:{organizer.slug}:{event.slug}"
    response = logged_in_organizer_client.get(
        url, {"state": state_param, "code": "auth_code"}
    )

    assert response.status_code == 302
    messages = list(get_messages(response.wsgi_request))
    assert any("Failed to exchange token with HubSpot" in str(m) for m in messages)
    with scope(organizer=organizer):
        assert not HubSpotOAuthToken.objects.filter(event=event).exists()
