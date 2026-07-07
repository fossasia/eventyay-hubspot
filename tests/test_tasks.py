import pytest
import uuid
from unittest import mock
from django.contrib.contenttypes.models import ContentType
from eventyay.base.models import InvoiceAddress
from hubspot.models import (
    HubSpotEventSettings,
    HubSpotFieldMapping,
    HubSpotObjectMapping,
    HubSpotProperty,
    ObjectTypeMapping,
    SyncLog,
    SyncMode,
    SyncStatus,
)
from hubspot.tasks import (
    _convert_value,
    sync_order_to_hubspot,
)
from hubspot.client import HubSpotTransientError, HubSpotPermanentError
from django_scopes import scopes_disabled
from celery.exceptions import Retry


@pytest.fixture(autouse=True)
def disable_scopes():
    with scopes_disabled():
        yield


def test_convert_value():
    assert _convert_value(None, "text") is None
    assert _convert_value("", "text") is None
    assert _convert_value(123, "text") == "123"

    assert _convert_value("123.45", "number") == 123.45
    assert _convert_value("abc", "number") is None

    assert _convert_value(True, "yes/no") == "true"
    assert _convert_value(False, "yes/no") == "false"
    assert _convert_value("Yes", "yes/no") == "true"
    assert _convert_value("N", "yes/no") == "false"


@pytest.fixture
def mock_event(event):
    HubSpotEventSettings.objects.create(event=event, sync_enabled=True)
    from hubspot.models import HubSpotOAuthToken
    from django.utils.timezone import now
    import datetime

    HubSpotOAuthToken.objects.create(
        event=event,
        access_token="valid_access",
        refresh_token="valid_refresh",
        expires_at=now() + datetime.timedelta(hours=1),
    )
    return event


@pytest.fixture
def object_mapping(mock_event):
    return ObjectTypeMapping.objects.create(
        event=mock_event, eventyay_object_type="order", hubspot_object_type="contacts"
    )


@pytest.mark.django_db
@mock.patch("hubspot.tasks.create_record")
def test_sync_skipped_when_disabled(mock_create, mock_event, object_mapping, order):
    settings = HubSpotEventSettings.objects.get(event=mock_event)
    settings.sync_enabled = False
    settings.save()

    sync_order_to_hubspot(order.id, mock_event.id)

    mock_create.assert_not_called()


@pytest.mark.django_db
@mock.patch("hubspot.tasks.create_record")
def test_sync_order_success(mock_create, mock_event, object_mapping, order):
    mock_create.return_value = "hub_123"

    ct = ContentType.objects.get_for_model(order)
    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="email",
        hubspot_object_type="contacts",
        hubspot_property="email",
        sync_mode=SyncMode.IDENTIFIER,
    )
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="email",
        data_type="text",
        sync_batch="00000000-0000-0000-0000-000000000000",
    )

    sync_order_to_hubspot(order.id, mock_event.id)

    mock_create.assert_called_once()
    assert mock_create.call_args[0][2] == {"email": order.email}

    # Verify SyncRecord created
    mapping = HubSpotObjectMapping.objects.get(event=mock_event, object_id=order.id)
    assert mapping.hubspot_object_id == "hub_123"

    log = SyncLog.objects.get(event=mock_event, object_mapping=mapping)
    assert log.status == SyncStatus.SUCCESS


@pytest.mark.django_db
@mock.patch("hubspot.tasks.get_record")
@mock.patch("hubspot.tasks.update_record")
def test_sync_modes(mock_update, mock_get_record, mock_event, object_mapping, order):
    mock_update.return_value = "hub_123"
    mock_get_record.return_value = {"company": "OldCompany", "phone": ""}

    ct = ContentType.objects.get_for_model(order)

    # Existing mapping
    HubSpotObjectMapping.objects.create(
        event=mock_event,
        content_type=ct,
        object_id=order.id,
        hubspot_object_type="contacts",
        hubspot_object_id="hub_123",
    )

    # Add field mappings
    dummy_batch = uuid.uuid4()
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="email",
        data_type="text",
        sync_batch=dummy_batch,
    )
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="amount",
        data_type="number",
        sync_batch=dummy_batch,
    )
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="locale",
        data_type="text",
        sync_batch=dummy_batch,
    )

    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="email",
        hubspot_object_type="contacts",
        hubspot_property="email",
        sync_mode=SyncMode.IDENTIFIER,
    )
    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="total",
        hubspot_object_type="contacts",
        hubspot_property="amount",
        sync_mode=SyncMode.OVERWRITE,
    )
    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="locale",
        hubspot_object_type="contacts",
        hubspot_property="locale",
        sync_mode=SyncMode.FILL_IF_NEW,
    )

    # Mocking order values
    order.email = "test@example.com"
    order.total = 100.0
    order.locale = "en"
    order.save()

    sync_order_to_hubspot(order.id, mock_event.id)

    mock_update.assert_called_once()
    properties_sent = mock_update.call_args[0][3]

    assert "email" in properties_sent
    assert "amount" in properties_sent
    assert (
        "locale" not in properties_sent
    )  # Skipped because Fill if New and record exists


@pytest.mark.django_db
@mock.patch("hubspot.tasks.get_record")
@mock.patch("hubspot.tasks.update_record")
def test_sync_fill_if_empty(
    mock_update, mock_get_record, mock_event, object_mapping, order
):
    mock_update.return_value = "hub_123"
    mock_get_record.return_value = {"phone": "", "company": "ExistingCorp"}

    ct = ContentType.objects.get_for_model(order)
    HubSpotObjectMapping.objects.create(
        event=mock_event,
        content_type=ct,
        object_id=order.id,
        hubspot_object_type="contacts",
        hubspot_object_id="hub_123",
    )

    dummy_batch = uuid.uuid4()
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="phone",
        data_type="text",
        sync_batch=dummy_batch,
    )
    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="company",
        data_type="text",
        sync_batch=dummy_batch,
    )

    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="phone",
        hubspot_object_type="contacts",
        hubspot_property="phone",
        sync_mode=SyncMode.FILL_IF_EMPTY,
    )
    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="invoice_company",
        hubspot_object_type="contacts",
        hubspot_property="company",
        sync_mode=SyncMode.FILL_IF_EMPTY,
    )

    order.phone = "12345"
    order.save()

    InvoiceAddress.objects.create(order=order, company="NewCorp")

    # Wait, we need to save the order or mock it properly since `invoice_address` is a related model in Eventyay.
    # Eventyay order.invoice_address is a relation. Let's create an invoice address if needed or just use another field.
    # To be safe, we'll map event_name instead.

    HubSpotFieldMapping.objects.filter(hubspot_property="company").update(
        eventyay_field="event_name"
    )
    order.event.name = "NewEvent"
    order.event.save()

    sync_order_to_hubspot(order.id, mock_event.id)

    properties_sent = mock_update.call_args[0][3]
    assert "phone" in properties_sent  # Was empty, so sent
    assert properties_sent["phone"] == "12345"
    assert "company" not in properties_sent  # Was not empty, so skipped


@pytest.mark.django_db
@mock.patch("hubspot.tasks.create_record")
@mock.patch("hubspot.tasks.sync_order_to_hubspot.retry")
def test_transient_error_retries(
    mock_retry, mock_create, mock_event, object_mapping, order
):
    mock_retry.side_effect = Retry()
    mock_create.side_effect = HubSpotTransientError("Timeout", retry_after_seconds=10)

    ct = ContentType.objects.get_for_model(order)

    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="email",
        data_type="text",
        sync_batch=uuid.uuid4(),
    )

    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="email",
        hubspot_object_type="contacts",
        hubspot_property="email",
        sync_mode=SyncMode.IDENTIFIER,
    )

    with pytest.raises(Retry):
        sync_order_to_hubspot(order.id, mock_event.id)

    mock_retry.assert_called_once()
    assert mock_retry.call_args[1]["countdown"] == 10


@pytest.mark.django_db
@mock.patch("hubspot.tasks.create_record")
def test_permanent_error_no_retry(mock_create, mock_event, object_mapping, order):
    mock_create.side_effect = HubSpotPermanentError("Invalid data", status_code=400)

    ct = ContentType.objects.get_for_model(order)

    HubSpotProperty.objects.create(
        event=mock_event,
        object_type="contacts",
        key="email",
        data_type="text",
        sync_batch=uuid.uuid4(),
    )

    HubSpotFieldMapping.objects.create(
        event=mock_event,
        content_type=ct,
        eventyay_field="email",
        hubspot_object_type="contacts",
        hubspot_property="email",
        sync_mode=SyncMode.IDENTIFIER,
    )

    # Should not raise Retry, but should silently record failure
    sync_order_to_hubspot(order.id, mock_event.id)

    logs = SyncLog.objects.filter(event=mock_event)
    assert logs.count() == 1
    assert logs.first().status == SyncStatus.FAILED


@pytest.mark.django_db
@mock.patch("hubspot.tasks.cache.add")
@mock.patch("hubspot.tasks.sync_order_to_hubspot.retry")
def test_sync_order_lock(mock_retry, mock_cache_add, mock_event, object_mapping, order):
    # Simulate that the lock is already held
    mock_cache_add.return_value = False
    mock_retry.side_effect = Retry()

    with pytest.raises(Retry):
        sync_order_to_hubspot(order.id, mock_event.id)

    mock_retry.assert_called_once()
    assert mock_retry.call_args[1]["countdown"] == 10
    mock_cache_add.assert_called_once_with(
        f"hubspot_sync_running_{order.id}", "1", timeout=300
    )


@pytest.mark.django_db
@mock.patch("hubspot.tasks.sync_order_to_hubspot.apply_async")
def test_recovery_sweep_missing(mock_apply_async, mock_event, object_mapping, order):
    order.status = order.STATUS_PAID
    order.save()

    from hubspot.tasks import hubspot_recovery_sweep

    # The order has no HubSpotObjectMapping at all.
    hubspot_recovery_sweep()

    # The order should be enqueued
    mock_apply_async.assert_called_once()
    assert mock_apply_async.call_args[1]["args"] == [order.id, mock_event.id]


@pytest.mark.django_db
@mock.patch("hubspot.tasks.sync_order_to_hubspot.apply_async")
def test_recovery_sweep_failed(mock_apply_async, mock_event, object_mapping, order):
    order.status = order.STATUS_PAID
    order.save()

    ct = ContentType.objects.get_for_model(order)

    mapping = HubSpotObjectMapping.objects.create(
        event=mock_event,
        content_type=ct,
        object_id=order.id,
        hubspot_object_type="contacts",
    )
    # The last_synced_at is None, but there is a FAILED SyncLog
    SyncLog.objects.create(
        event=mock_event,
        object_mapping=mapping,
        action="create",
        direction="push",
        status=SyncStatus.FAILED,
    )

    from hubspot.tasks import hubspot_recovery_sweep

    hubspot_recovery_sweep()

    mock_apply_async.assert_called_once()
    assert mock_apply_async.call_args[1]["args"] == [order.id, mock_event.id]


@pytest.mark.django_db
@mock.patch("hubspot.tasks.sync_order_to_hubspot.apply_async")
def test_recovery_sweep_success_skipped(
    mock_apply_async, mock_event, object_mapping, order
):
    order.status = order.STATUS_PAID
    order.save()

    from django.utils.timezone import now

    ct = ContentType.objects.get_for_model(order)

    mapping = HubSpotObjectMapping.objects.create(
        event=mock_event,
        content_type=ct,
        object_id=order.id,
        hubspot_object_type="contacts",
        last_synced_at=now(),
        hubspot_object_id="hub_123",
    )
    # Latest log is SUCCESS
    SyncLog.objects.create(
        event=mock_event,
        object_mapping=mapping,
        action="create",
        direction="push",
        status=SyncStatus.SUCCESS,
    )

    from hubspot.tasks import hubspot_recovery_sweep

    hubspot_recovery_sweep()

    # The order is fully synced, should NOT be queued
    mock_apply_async.assert_not_called()


@pytest.mark.django_db
@mock.patch("hubspot.tasks.sync_order_to_hubspot.apply_async")
def test_recovery_sweep_ignores_disabled(
    mock_apply_async, mock_event, object_mapping, order
):
    settings = HubSpotEventSettings.objects.get(event=mock_event)
    settings.sync_enabled = False
    settings.save()

    order.status = order.STATUS_PAID
    order.save()

    from hubspot.tasks import hubspot_recovery_sweep

    hubspot_recovery_sweep()

    mock_apply_async.assert_not_called()
