from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django_scopes import scope, scopes_disabled
from eventyay.base.models import Order

from hubspot.models import HubSpotFieldMapping, ObjectTypeMapping, SyncMode


@pytest.fixture(autouse=True)
def mock_hubspot_properties():
    with patch("hubspot.views.get_hubspot_properties") as mock_get:
        mock_get.return_value = [
            {
                "key": "dealname",
                "label": "Deal Name",
                "data_type": "text",
                "category": "Deal",
            },
            {
                "key": "amount",
                "label": "Amount",
                "data_type": "number",
                "category": "Deal",
            },
        ]
        yield mock_get


@pytest.fixture
def mapping(organizer, event):
    with scope(organizer=organizer):
        return ObjectTypeMapping.objects.create(
            event=event,
            eventyay_object_type="order",
            hubspot_object_type="deals",
            position=0,
        )


@pytest.fixture
def order_content_type():
    return ContentType.objects.get_for_model(Order)


@pytest.fixture
def mapping_url(event, mapping):
    return reverse(
        "plugins:hubspot:mapping_fields",
        kwargs={
            "organizer": event.organizer.slug,
            "event": event.slug,
            "mapping_id": mapping.id,
        },
    )


@pytest.mark.django_db
def test_empty_state_renders(logged_in_organizer_client, mapping_url, settings):
    settings.SITE_URL = "https://testserver"
    response = logged_in_organizer_client.get(mapping_url)
    assert response.status_code == 200
    assert b"No field mappings exist yet" in response.content


@pytest.mark.django_db
def test_valid_save_one_identifier(
    logged_in_organizer_client,
    mapping_url,
    organizer,
    event,
    order_content_type,
    settings,
):
    settings.SITE_URL = "https://testserver"
    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-0-eventyay_field": "code",
        "form-0-hubspot_property": "dealname",
        "form-0-sync_mode": SyncMode.IDENTIFIER,
        "form-0-is_active": "on",
    }
    response = logged_in_organizer_client.post(mapping_url, data)
    assert response.status_code == 302

    with scopes_disabled():
        assert (
            HubSpotFieldMapping.objects.filter(
                event=event,
                content_type=order_content_type,
                sync_mode=SyncMode.IDENTIFIER,
            ).count()
            == 1
        )


@pytest.mark.django_db
def test_no_identifier_blocks_save(
    logged_in_organizer_client, mapping_url, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-0-eventyay_field": "code",
        "form-0-hubspot_property": "dealname",
        "form-0-sync_mode": SyncMode.OVERWRITE,
        "form-0-is_active": "on",
    }
    response = logged_in_organizer_client.post(mapping_url, data)
    assert response.status_code == 200
    assert b"Exactly one row must have its sync mode set to" in response.content
    assert b"Identifier" in response.content

    with scope(organizer=organizer):
        assert HubSpotFieldMapping.objects.filter(event=event).count() == 0


@pytest.mark.django_db
def test_multiple_identifiers_blocks_save(
    logged_in_organizer_client, mapping_url, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    data = {
        "form-TOTAL_FORMS": "2",
        "form-INITIAL_FORMS": "0",
        "form-0-eventyay_field": "code",
        "form-0-hubspot_property": "dealname",
        "form-0-sync_mode": SyncMode.IDENTIFIER,
        "form-0-is_active": "on",
        "form-1-eventyay_field": "total",
        "form-1-hubspot_property": "amount",
        "form-1-sync_mode": SyncMode.IDENTIFIER,
        "form-1-is_active": "on",
    }
    response = logged_in_organizer_client.post(mapping_url, data)
    assert response.status_code == 200
    assert b"Only one row can be set as" in response.content
    assert b"Identifier" in response.content

    with scope(organizer=organizer):
        assert HubSpotFieldMapping.objects.filter(event=event).count() == 0


@pytest.mark.django_db
def test_incompatible_types_warning_but_saves(
    logged_in_organizer_client, mapping_url, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    # Mapping a boolean (testmode) to a string (dealname)
    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-0-eventyay_field": "testmode",
        "form-0-hubspot_property": "dealname",
        "form-0-sync_mode": SyncMode.IDENTIFIER,
        "form-0-is_active": "on",
    }
    # It should save successfully (redirect)
    response = logged_in_organizer_client.post(mapping_url, data)
    assert response.status_code == 302
    with scope(organizer=organizer):
        assert HubSpotFieldMapping.objects.filter(event=event).count() == 1

    # Check that warning is rendered in GET response with existing data
    response = logged_in_organizer_client.get(mapping_url)
    assert b"Warning: Possible type mismatch." in response.content


@pytest.mark.django_db
def test_compatible_types_save_without_warning(
    logged_in_organizer_client, mapping_url, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    data = {
        "form-TOTAL_FORMS": "1",
        "form-INITIAL_FORMS": "0",
        "form-0-eventyay_field": "code",
        "form-0-hubspot_property": "dealname",
        "form-0-sync_mode": SyncMode.IDENTIFIER,
        "form-0-is_active": "on",
    }
    response = logged_in_organizer_client.post(mapping_url, data)
    assert response.status_code == 302

    response = logged_in_organizer_client.get(mapping_url)
    assert b"Warning: Possible type mismatch." not in response.content
