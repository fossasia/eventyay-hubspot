import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django_scopes import scope
from eventyay.base.models import Event

from hubspot.models import (
    AuditAction,
    AuditLog,
    HubSpotObjectMapping,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncStatus,
)


@pytest.mark.django_db
def test_hubspot_settings_recent_activity_preview(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    with scope(organizer=event.organizer):
        for i in range(10):
            AuditLog.objects.create(
                organizer=event.organizer,
                event=event,
                action=(
                    AuditAction.CONNECT if i % 2 == 0 else AuditAction.MAPPING_UPDATED
                ),
                ip_address="127.0.0.1",
            )

    url = reverse(
        "plugins:hubspot:hubspot",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200

    # Check that the preview shows at most 5 entries
    content = response.content.decode()
    assert "Recent Activity" in content

    # We should have 5 items in the table body
    # Since we created 10, the preview only shows 5
    assert content.count('<td class="text-muted">') == 5


@pytest.mark.django_db
def test_hubspot_logs_view_all_entries(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    with scope(organizer=event.organizer):
        AuditLog.objects.create(
            organizer=event.organizer,
            event=event,
            action=AuditAction.MAPPING_UPDATED,
            ip_address="127.0.0.1",
        )
        SyncLog.objects.create(
            event=event,
            action=SyncAction.CREATE,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
        )

    url = reverse(
        "plugins:hubspot:logs",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )

    # Test all activities
    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Field mapping settings were updated" in content
    assert "Object synced to HubSpot successfully" in content
    assert content.count('<td class="text-muted">') == 2

    # Test sync filter
    response_sync = logged_in_organizer_client.get(url + "?type=sync")
    assert response_sync.status_code == 200
    assert "Object synced to HubSpot successfully" in response_sync.content.decode()
    assert response_sync.content.decode().count('<td class="text-muted">') == 1

    # Test settings filter
    response_sett = logged_in_organizer_client.get(url + "?type=settings")
    assert response_sett.status_code == 200
    assert "Field mapping settings were updated" in response_sett.content.decode()
    assert response_sett.content.decode().count('<td class="text-muted">') == 1


@pytest.mark.django_db
def test_hubspot_logs_view_specific_synced_object(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    with scope(organizer=event.organizer):
        content_type = ContentType.objects.get_for_model(event)
        mapping = HubSpotObjectMapping.objects.create(
            event=event,
            content_type=content_type,
            object_id=event.id,
            hubspot_object_type="deal",
            hubspot_object_id="202",
        )
        SyncLog.objects.create(
            event=event,
            object_mapping=mapping,
            action=SyncAction.CREATE,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
        )

    url = reverse(
        "plugins:hubspot:logs",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )

    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    expected_message = f"Event {event.name} synced to HubSpot successfully"
    assert expected_message in content


@pytest.mark.django_db
def test_hubspot_logs_view_other_event_entries_not_shown(
    logged_in_organizer_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"

    event2 = Event.objects.create(
        organizer=organizer, name="Event 2", slug="event2", date_from=event.date_from
    )

    with scope(organizer=event.organizer):
        AuditLog.objects.create(
            organizer=event.organizer,
            event=event2,  # Using different event
            action=AuditAction.MAPPING_UPDATED,
            ip_address="127.0.0.1",
        )

    url = reverse(
        "plugins:hubspot:logs",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200
    assert "Field mapping settings were updated" not in response.content.decode()
    assert response.content.decode().count('<td class="text-muted">') == 0


@pytest.mark.django_db
def test_hubspot_logs_view_permission_denied(
    logged_in_client, organizer, event, settings
):
    settings.SITE_URL = "https://testserver"
    url = reverse(
        "plugins:hubspot:logs",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )
    # Logged in client who is not organizer for the event
    response = logged_in_client.get(url)
    assert response.status_code in [403, 404]


@pytest.mark.django_db
def test_hubspot_logs_view_order_and_position_synced_formatting(
    logged_in_organizer_client, organizer, event, settings
):
    from datetime import timedelta

    from django.utils.timezone import now
    from eventyay.base.models import Order, OrderPosition, Product

    settings.SITE_URL = "https://testserver"

    with scope(organizer=event.organizer):
        order = Order.objects.create(
            event=event,
            code="ABCDE",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=1),
            total=100.00,
            email="buyer@example.com",
        )
        product = Product.objects.create(
            event=event,
            name="Standard Ticket",
            default_price=10.00,
        )
        order_position = OrderPosition.objects.create(
            order=order,
            product=product,
            price=10.00,
            positionid=1,
            attendee_name_parts={"_legacy": "John Doe"},
            attendee_name_cached="John Doe",
            attendee_email="attendee@example.com",
        )

        content_type_order = ContentType.objects.get_for_model(Order)
        mapping_order = HubSpotObjectMapping.objects.create(
            event=event,
            content_type=content_type_order,
            object_id=order.id,
            hubspot_object_type="deal",
            hubspot_object_id="202",
        )
        SyncLog.objects.create(
            event=event,
            object_mapping=mapping_order,
            action=SyncAction.CREATE,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
        )

        content_type_pos = ContentType.objects.get_for_model(OrderPosition)
        mapping_pos = HubSpotObjectMapping.objects.create(
            event=event,
            content_type=content_type_pos,
            object_id=order_position.id,
            hubspot_object_type="contact",
            hubspot_object_id="303",
        )
        SyncLog.objects.create(
            event=event,
            object_mapping=mapping_pos,
            action=SyncAction.CREATE,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
        )

    url = reverse(
        "plugins:hubspot:logs",
        kwargs={"organizer": organizer.slug, "event": event.slug},
    )

    response = logged_in_organizer_client.get(url)
    assert response.status_code == 200
    content = response.content.decode()

    # The order synced log text should contain code and buyer email
    assert "Order ABCDE (buyer@example.com) synced to HubSpot successfully" in content

    # The position synced log text should contain position representation, order code and attendee name
    expected_pos_message = "Order Position #1 – Standard Ticket for Order ABCDE (John Doe) synced to HubSpot successfully"
    assert expected_pos_message in content
