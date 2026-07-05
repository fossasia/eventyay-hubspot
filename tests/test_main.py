import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction

from hubspot.models import (
    HubSpotEventSettings,
    HubSpotFieldMapping,
    HubSpotOAuthToken,
    HubSpotObjectMapping,
    SyncLog,
)


@pytest.mark.django_db
def test_oauth_token_model(event):
    token = HubSpotOAuthToken(event=event, hub_id="hub_1")
    token.access_token = "acc_123"
    token.refresh_token = "ref_123"
    token.save()
    assert token.event == event
    assert token._access_token != "acc_123"
    assert token._refresh_token != "ref_123"
    assert token.access_token == "acc_123"
    assert str(token) == f"OAuth Token for {event.name}"


@pytest.mark.django_db
def test_oauth_token_unique_per_event(event):
    token = HubSpotOAuthToken(event=event)
    token.access_token = "a"
    token.refresh_token = "r"
    token.save()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            dup = HubSpotOAuthToken(event=event)
            dup.access_token = "b"
            dup.refresh_token = "s"
            dup.save()


@pytest.mark.django_db
def test_event_settings_model(event):
    settings = HubSpotEventSettings.objects.create(
        event=event,
        sync_enabled=True,
    )
    assert settings.sync_enabled is True
    assert str(settings) == f"HubSpot Settings for {event.name}"


@pytest.mark.django_db
def test_object_mapping_model(event):
    content_type = ContentType.objects.get_for_model(event)
    mapping = HubSpotObjectMapping.objects.create(
        event=event,
        content_type=content_type,
        object_id=101,
        hubspot_object_type="deal",
        hubspot_object_id="202",
    )
    assert mapping.content_type == content_type
    assert str(mapping) == f"{content_type.model} (101) -> deal (202)"


@pytest.mark.django_db
def test_object_mapping_unique_together(event):
    content_type = ContentType.objects.get_for_model(event)
    HubSpotObjectMapping.objects.create(
        event=event,
        content_type=content_type,
        object_id=101,
        hubspot_object_type="deal",
        hubspot_object_id="202",
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            HubSpotObjectMapping.objects.create(
                event=event,
                content_type=content_type,
                object_id=101,
                hubspot_object_type="deal",
                hubspot_object_id="999",
            )


@pytest.mark.django_db
def test_field_mapping_model(event):
    content_type = ContentType.objects.get_for_model(event)
    mapping = HubSpotFieldMapping.objects.create(
        event=event,
        content_type=content_type,
        eventyay_field="total",
        hubspot_object_type="deal",
        hubspot_property="amount",
    )
    assert mapping.is_active is True
    assert str(mapping) == f"{content_type.model}.total -> deal.amount"


@pytest.mark.django_db
def test_field_mapping_unique_together(event):
    content_type = ContentType.objects.get_for_model(event)
    HubSpotFieldMapping.objects.create(
        event=event,
        content_type=content_type,
        eventyay_field="total",
        hubspot_object_type="deal",
        hubspot_property="amount",
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            HubSpotFieldMapping.objects.create(
                event=event,
                content_type=content_type,
                eventyay_field="total",
                hubspot_object_type="deal",
                hubspot_property="other_amount",
            )


@pytest.mark.django_db
def test_sync_log_model(event):
    log = SyncLog.objects.create(
        event=event,
        action="create",
        direction="push",
        status="success",
        detail={"status_code": 200},
    )
    assert log.action == "create"
    assert "create" in str(log)
    assert log.detail["status_code"] == 200


@pytest.mark.django_db
def test_sync_log_set_null_on_mapping_delete(event):
    content_type = ContentType.objects.get_for_model(event)
    mapping = HubSpotObjectMapping.objects.create(
        event=event,
        content_type=content_type,
        object_id=101,
        hubspot_object_type="deal",
        hubspot_object_id="202",
    )
    log = SyncLog.objects.create(
        event=event,
        object_mapping=mapping,
        action="create",
        direction="push",
        status="success",
    )
    mapping.delete()
    log.refresh_from_db()
    assert log.object_mapping is None
