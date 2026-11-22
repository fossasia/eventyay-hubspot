from collections import OrderedDict
from datetime import timedelta

from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import resolve, reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django_scopes import scope
from eventyay.base.models import Event, Order, OrderPosition
from eventyay.base.signals import (
    order_canceled,
    order_paid,
    order_placed,
    periodic_task,
    register_global_settings,
)
from eventyay.control.signals import nav_event_settings, nav_organizer, order_info

from .models import (
    AuditLog,
    HubSpotEventSettings,
    HubSpotFieldMapping,
    HubSpotOAuthToken,
    HubSpotObjectMapping,
    ObjectTypeMapping,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncStatus,
)
from .operational_log import OUTCOME_SUCCESS, log_operation
from .services import (
    bootstrap_event_hubspot,
    get_valid_hubspot_token,
    is_auto_sync_enabled,
    is_sync_enabled,
)
from .tasks import sync_order_to_hubspot


@receiver(register_global_settings, dispatch_uid="hubspot_global_settings")
def register_global_settings_receiver(sender, **kwargs):
    return OrderedDict(
        [
            (
                "hubspot_client_id",
                forms.CharField(
                    label=_("Client ID"),
                    required=False,
                ),
            ),
            (
                "hubspot_client_secret",
                forms.CharField(
                    label=_("Client Secret"),
                    required=False,
                    widget=forms.PasswordInput(render_value=True),
                ),
            ),
            (
                "hubspot_property_sync_ttl_minutes",
                forms.IntegerField(
                    label=_("Property Sync Cache TTL (minutes)"),
                    required=False,
                    min_value=1,
                    initial=10,
                ),
            ),
        ]
    )


@receiver(nav_event_settings, dispatch_uid="hubspot_nav")
def control_nav_import(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("HubSpot"),
            "url": reverse(
                "plugins:hubspot:hubspot",
                kwargs={
                    "organizer": request.event.organizer.slug,
                    "event": request.event.slug,
                },
            ),
            "active": url.namespace == "plugins:hubspot" and url.url_name == "hubspot",
        }
    ]


@receiver(nav_organizer, dispatch_uid="hubspot_nav_org")
def control_nav_organizer_hubspot(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("HubSpot"),
            "url": reverse(
                "plugins:hubspot:org_hubspot",
                kwargs={
                    "organizer": request.organizer.slug,
                },
            ),
            "active": url.namespace == "plugins:hubspot"
            and url.url_name in ("org_hubspot", "org_connect", "org_disconnect"),
            "icon": "bar-chart",
        }
    ]


def _enqueue_hubspot_sync(sender, order, **kwargs):
    if not order:
        return

    if order.status != Order.STATUS_PAID:
        return

    with scope(organizer=order.event.organizer):
        if not is_sync_enabled(order.event):
            return
        if not get_valid_hubspot_token(order.event):
            return
        if not is_auto_sync_enabled(order.event):

            def log_pending():
                # Deduplicate multiple signals for the same order within a short window
                cache_key = f"hubspot_sync_enqueued_{order.id}"
                if cache.add(cache_key, "1", timeout=5):
                    with scope(organizer=order.event.organizer):
                        SyncLog.objects.create(
                            event=order.event,
                            action=SyncAction.UPDATE,
                            direction=SyncDirection.PUSH,
                            status=SyncStatus.PENDING,
                            detail={
                                "message": f"Auto-sync disabled, sync pending for order {order.code}",
                                "order_code": order.code,
                            },
                        )

            transaction.on_commit(log_pending)
            return

    def enqueue_task():
        # Deduplicate multiple signals for the same order within a short window
        cache_key = f"hubspot_sync_enqueued_{order.id}"
        if cache.add(cache_key, "1", timeout=5):
            sync_order_to_hubspot.apply_async(args=[order.id, order.event.id], countdown=5)
            log_operation(
                "sync.order_enqueue",
                OUTCOME_SUCCESS,
                backend="hubspot",
                event_id=order.event_id,
                order_id=order.id,
            )

    transaction.on_commit(enqueue_task)


@receiver(order_placed, dispatch_uid="hubspot_order_placed")
def on_order_placed(sender, order, **kwargs):
    _enqueue_hubspot_sync(sender, order, **kwargs)


@receiver(order_paid, dispatch_uid="hubspot_order_paid")
def on_order_paid(sender, order, **kwargs):
    _enqueue_hubspot_sync(sender, order, **kwargs)


@receiver(order_canceled, dispatch_uid="hubspot_order_canceled")
def on_order_canceled(sender, order, **kwargs):
    _enqueue_hubspot_sync(sender, order, **kwargs)


@receiver(
    post_delete,
    sender=ObjectTypeMapping,
    dispatch_uid="hubspot_object_type_mapping_delete",
)
def cleanup_associated_mappings(sender, instance, **kwargs):
    if instance.eventyay_object_type == "order":
        model_class = Order
    elif instance.eventyay_object_type == "order_position":
        model_class = OrderPosition
    else:
        return

    try:
        content_type = ContentType.objects.get_for_model(model_class)
    except ContentType.DoesNotExist:
        return

    # Delete associated Field Mappings
    HubSpotFieldMapping.objects.filter(
        event=instance.event,
        content_type=content_type,
        hubspot_object_type=instance.hubspot_object_type,
    ).delete()

    HubSpotObjectMapping.objects.filter(
        event=instance.event,
        content_type=content_type,
        hubspot_object_type=instance.hubspot_object_type,
    ).delete()


@receiver(periodic_task, dispatch_uid="hubspot_clear_audit_logs")
def clear_audit_logs(sender, **kwargs):
    days = 180

    threshold = now() - timedelta(days=days)
    AuditLog.objects.filter(created_at__lt=threshold).delete()
    SyncLog.objects.filter(created_at__lt=threshold).delete()


@receiver(order_info, dispatch_uid="hubspot_control_order_info")
def control_order_info(sender, request, order, **kwargs):
    if not request.user.has_event_permission(request.organizer, request.event, "can_view_orders", request):
        return ""

    is_connected = False
    has_token = HubSpotOAuthToken.objects.filter(event=order.event).exists()
    settings = HubSpotEventSettings.objects.filter(event=order.event).first()
    if settings and settings.sync_enabled and has_token:
        is_connected = True

    status_text = _("Not synced yet")
    status_class = "label label-default"
    last_synced_at = None
    sync_needed = True

    content_type_order = ContentType.objects.get_for_model(Order)
    content_type_position = ContentType.objects.get_for_model(OrderPosition)

    order_mapping = HubSpotObjectMapping.objects.filter(
        event=order.event, content_type=content_type_order, object_id=order.id
    ).first()

    position_ids = order.positions.values_list("id", flat=True)
    position_mappings = HubSpotObjectMapping.objects.filter(
        event=order.event,
        content_type=content_type_position,
        object_id__in=position_ids,
    )

    mappings = []
    if order_mapping:
        mappings.append(order_mapping)
    mappings.extend(position_mappings)

    if mappings:
        mapping_ids = [m.id for m in mappings]
        latest_log = SyncLog.objects.filter(object_mapping_id__in=mapping_ids).order_by("-created_at").first()
        last_synced_at = max(
            (m.last_synced_at for m in mappings if m.last_synced_at is not None),
            default=None,
        )

        if latest_log:
            if latest_log.status == SyncStatus.SUCCESS:
                status_text = _("Synced")
                status_class = "label label-success"
            elif latest_log.status == SyncStatus.FAILED:
                status_text = _("Failed — could not reach HubSpot")
                status_class = "label label-danger"
            elif latest_log.status == SyncStatus.PENDING:
                status_text = _("Waiting to sync")
                status_class = "label label-warning"
    else:
        pending_log = SyncLog.objects.filter(
            event=order.event,
            status=SyncStatus.PENDING,
            detail__order_code=order.code,
        ).first()

        if pending_log:
            status_text = _("Waiting to sync")
            status_class = "label label-warning"

    has_mapping_changes = False
    if status_text == _("Synced"):
        active_types = ObjectTypeMapping.objects.filter(
            event=order.event, eventyay_object_type__in=["order", "order_position"]
        )
        if active_types.exists() and last_synced_at:
            for otm in active_types:
                if last_synced_at < otm.updated_at:
                    has_mapping_changes = True
                    break

            if not has_mapping_changes:
                active_fields = HubSpotFieldMapping.objects.filter(
                    event=order.event,
                    content_type__in=[content_type_order, content_type_position],
                    is_active=True,
                )
                for fm in active_fields:
                    if last_synced_at < fm.updated_at:
                        has_mapping_changes = True
                        break

            if not has_mapping_changes:
                sync_needed = False

    can_sync = request.user.has_event_permission(request.organizer, request.event, "can_change_event_settings", request)

    template = get_template("hubspot/control_order_info.html")
    ctx = {
        "order": order,
        "request": request,
        "event": sender,
        "is_connected": is_connected,
        "is_paid": order.status == Order.STATUS_PAID,
        "status_text": status_text,
        "status_class": status_class,
        "last_synced_at": last_synced_at,
        "sync_needed": sync_needed,
        "has_mapping_changes": has_mapping_changes,
        "can_sync": can_sync,
    }
    return template.render(ctx, request=request)


@receiver(post_save, sender=Event, dispatch_uid="hubspot_event_created")
def on_event_created(sender, instance, created, **kwargs):
    if not created:
        return

    bootstrap_event_hubspot(instance)
