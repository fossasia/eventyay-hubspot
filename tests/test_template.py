import pytest
from django.urls import reverse
from django_scopes import scope

from hubspot.models import HubSpotOAuthToken


@pytest.mark.django_db
def test_disconnected_shows_connect_button(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    url = reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    content = response.content.decode()

    assert response.status_code == 200
    assert "Connect to HubSpot" in content
    assert "Disconnect" not in content


@pytest.mark.django_db
def test_connected_shows_disconnect_button_and_portal_info(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    with scope(organizer=organizer):
        HubSpotOAuthToken.objects.create(
            event=event,
            access_token="test_access",
            refresh_token="test_refresh",
            hub_id="12345678",
            hub_name="my-portal.hubspot.com",
        )

    url = reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    content = response.content.decode()

    assert response.status_code == 200
    assert "Disconnect" in content
    assert "Connect to HubSpot" not in content
    assert "my-portal.hubspot.com" in content
    assert "12345678" in content


@pytest.mark.django_db
def test_no_token_values_in_rendered_output(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    with scope(organizer=organizer):
        token = HubSpotOAuthToken.objects.create(
            event=event,
            access_token="secret_access_tok_xyz",
            refresh_token="secret_refresh_tok_abc",
            hub_id="12345678",
            hub_name="my-portal.hubspot.com",
        )
        # Also grab the encrypted values so we can check those don't leak
        encrypted_access = token._access_token
        encrypted_refresh = token._refresh_token

    url = reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    content = response.content.decode()

    assert "secret_access_tok_xyz" not in content
    assert "secret_refresh_tok_abc" not in content
    assert encrypted_access not in content
    assert encrypted_refresh not in content
