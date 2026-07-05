import pytest
from django.urls import reverse
from django_scopes import scope

from hubspot.models import HubSpotOAuthToken, ObjectTypeMapping


def _settings_url(organizer, event):
    return reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )


def _connect_event(event):
    """Create an OAuth token so the event appears connected."""
    token = HubSpotOAuthToken(event=event, hub_id="123", hub_name="test-hub")
    token.access_token = "acc"
    token.refresh_token = "ref"
    token.save()


@pytest.mark.django_db
def test_mappings_listed_on_settings(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    _connect_event(event)
    with scope(organizer=organizer):
        ObjectTypeMapping.objects.create(
            event=event, eventyay_object_type="order", hubspot_object_type="contacts"
        )
    url = _settings_url(organizer, event)
    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Object mappings" in content
    assert "order" in content


@pytest.mark.django_db
def test_create_object_mapping(logged_in_organizer_client, organizer, event, settings):
    settings.SITE_URL = "https://testserver"
    _connect_event(event)
    url = _settings_url(organizer, event)
    data = {
        "objecttypemapping_set-TOTAL_FORMS": "1",
        "objecttypemapping_set-INITIAL_FORMS": "0",
        "objecttypemapping_set-MIN_NUM_FORMS": "0",
        "objecttypemapping_set-MAX_NUM_FORMS": "1000",
        "objecttypemapping_set-0-eventyay_object_type": "order",
        "objecttypemapping_set-0-hubspot_object_type": "contacts",
        "objecttypemapping_set-0-position": "0",
        "objecttypemapping_set-0-id": "",
    }
    response = logged_in_organizer_client.post(url, data)
    assert response.status_code == 302
    with scope(organizer=organizer):
        assert ObjectTypeMapping.objects.filter(event=event).count() == 1
        m = ObjectTypeMapping.objects.get(event=event)
        assert m.eventyay_object_type == "order"
        assert m.hubspot_object_type == "contacts"


@pytest.mark.django_db
def test_duplicate_mapping_blocked(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    _connect_event(event)
    url = _settings_url(organizer, event)
    data = {
        "objecttypemapping_set-TOTAL_FORMS": "2",
        "objecttypemapping_set-INITIAL_FORMS": "0",
        "objecttypemapping_set-MIN_NUM_FORMS": "0",
        "objecttypemapping_set-MAX_NUM_FORMS": "1000",
        "objecttypemapping_set-0-eventyay_object_type": "order",
        "objecttypemapping_set-0-hubspot_object_type": "contacts",
        "objecttypemapping_set-0-position": "0",
        "objecttypemapping_set-0-id": "",
        "objecttypemapping_set-1-eventyay_object_type": "order",
        "objecttypemapping_set-1-hubspot_object_type": "contacts",
        "objecttypemapping_set-1-position": "1",
        "objecttypemapping_set-1-id": "",
    }
    response = logged_in_organizer_client.post(url, data)
    assert response.status_code == 200  # Re-renders form with errors
    content = response.content.decode()
    assert "Duplicate mapping" in content
    with scope(organizer=organizer):
        assert ObjectTypeMapping.objects.filter(event=event).count() == 0


@pytest.mark.django_db
def test_edit_existing_mapping(logged_in_organizer_client, organizer, event, settings):
    settings.SITE_URL = "https://testserver"
    _connect_event(event)
    with scope(organizer=organizer):
        mapping = ObjectTypeMapping.objects.create(
            event=event, eventyay_object_type="order", hubspot_object_type="contacts"
        )
    url = _settings_url(organizer, event)
    data = {
        "objecttypemapping_set-TOTAL_FORMS": "1",
        "objecttypemapping_set-INITIAL_FORMS": "1",
        "objecttypemapping_set-MIN_NUM_FORMS": "0",
        "objecttypemapping_set-MAX_NUM_FORMS": "1000",
        "objecttypemapping_set-0-eventyay_object_type": "order",
        "objecttypemapping_set-0-hubspot_object_type": "deals",
        "objecttypemapping_set-0-position": "0",
        "objecttypemapping_set-0-id": str(mapping.pk),
    }
    response = logged_in_organizer_client.post(url, data)
    assert response.status_code == 302
    with scope(organizer=organizer):
        mapping.refresh_from_db()
        assert mapping.hubspot_object_type == "deals"


@pytest.mark.django_db
def test_delete_mapping(logged_in_organizer_client, organizer, event, settings):
    settings.SITE_URL = "https://testserver"
    _connect_event(event)
    with scope(organizer=organizer):
        mapping = ObjectTypeMapping.objects.create(
            event=event, eventyay_object_type="order", hubspot_object_type="contacts"
        )
    url = _settings_url(organizer, event)
    data = {
        "objecttypemapping_set-TOTAL_FORMS": "1",
        "objecttypemapping_set-INITIAL_FORMS": "1",
        "objecttypemapping_set-MIN_NUM_FORMS": "0",
        "objecttypemapping_set-MAX_NUM_FORMS": "1000",
        "objecttypemapping_set-0-eventyay_object_type": "order",
        "objecttypemapping_set-0-hubspot_object_type": "contacts",
        "objecttypemapping_set-0-position": "0",
        "objecttypemapping_set-0-id": str(mapping.pk),
        "objecttypemapping_set-0-DELETE": "on",
    }
    response = logged_in_organizer_client.post(url, data)
    assert response.status_code == 302
    with scope(organizer=organizer):
        assert ObjectTypeMapping.objects.filter(event=event).count() == 0


@pytest.mark.django_db
def test_mapping_permission_required(client, organizer, event, settings):
    settings.SITE_URL = "https://testserver"
    url = _settings_url(organizer, event)
    response = client.post(url, {})
    assert response.status_code == 302
    assert "login" in response.url
