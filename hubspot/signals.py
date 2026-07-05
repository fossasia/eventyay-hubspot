from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from eventyay.base.models import Order, OrderPosition
from eventyay.base.signals import periodic_task
from eventyay.control.signals import nav_event

from .models import (
    AuditLog,
    HubSpotFieldMapping,
    HubSpotObjectMapping,
    ObjectTypeMapping,
    SyncLog,
)


@receiver(nav_event, dispatch_uid="hubspot_nav")
def control_nav_import(sender, request=None, **kwargs):
    url = resolve(request.path_info)
    return [
        {
            "label": _("Hubspot"),
            "url": reverse(
                "plugins:hubspot:hubspot",
                kwargs={
                    "organizer": request.event.organizer.slug,
                    "event": request.event.slug,
                },
            ),
            "active": url.namespace == "plugins:hubspot" and url.url_name == "hubspot",
            "icon": "bar-chart",
        }
    ]


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
