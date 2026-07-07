import logging
from typing import Any, Dict

from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from django.conf import settings
from django.urls import reverse

from eventyay.base.models import Event, Order, OrderPosition

from .client import (
    HubSpotTransientError,
    HubSpotPermanentError,
    HubSpotRecordNotFoundError,
    HubSpotConflictError,
    create_record,
    update_record,
    get_record,
)
from .services import get_valid_hubspot_token
from .models import (
    HubSpotEventSettings,
    HubSpotFieldMapping,
    HubSpotObjectMapping,
    HubSpotProperty,
    ObjectTypeMapping,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncMode,
    SyncStatus,
)


logger = logging.getLogger(__name__)


def _get_question_value(obj: Any, question_id: str) -> Any:
    """Helper to get a question answer from an OrderPosition"""
    if not isinstance(obj, OrderPosition):
        return None
    # We look for the answer to the specific question_id
    # The identifier in field discovery is the Question.identifier.
    for answer in obj.answers.all():
        if answer.question.identifier == question_id:
            # We return the string value. Type conversion handles the rest.
            if answer.question.type == "B":
                return answer.answer.lower() in ("true", "yes", "1")
            return answer.answer
    return None


def _resolve_eventyay_field(obj: Any, key: str) -> Any:
    """Helper to extract a specific field from Order or OrderPosition based on field_discovery keys"""
    # Event fields
    if key.startswith("event_"):
        event = (
            obj.event
            if hasattr(obj, "event")
            else (obj.order.event if hasattr(obj, "order") else None)
        )
        if not event:
            return None
        if key == "event_slug":
            return event.slug
        if key == "event_name":
            return event.name
        if key == "event_start_date":
            return event.date_from
        if key == "event_end_date":
            return event.date_to
        if key == "event_and_order_code":
            order_code = obj.code if isinstance(obj, Order) else obj.order.code
            return f"{event.slug}-{order_code}"

    # Invoice fields
    if key.startswith("invoice_"):
        order = obj if isinstance(obj, Order) else obj.order
        ia = getattr(order, "invoice_address", None)
        if not ia:
            return None

        attr = key.replace("invoice_", "")
        if attr == "name":
            return ia.name
        if attr == "company":
            return ia.company
        if attr == "given_name":
            parts = getattr(ia, "name_parts", {}) or {}
            if parts.get("_legacy"):
                return parts["_legacy"].split(" ", 1)[0]
            return parts.get("_given_name", parts.get("given_name"))
        if attr == "family_name":
            parts = getattr(ia, "name_parts", {}) or {}
            if parts.get("_legacy"):
                spl = parts["_legacy"].split(" ", 1)
                return spl[1] if len(spl) > 1 else None
            return parts.get("_family_name", parts.get("family_name"))
        if attr == "street":
            return ia.street
        if attr == "zip":
            return getattr(ia, "zipcode", getattr(ia, "zip", None))
        if attr == "city":
            return ia.city
        if attr == "country":
            return str(ia.country) if ia.country else None
        if attr == "state":
            return ia.state
        if attr == "vat_id":
            return ia.vat_id
        if attr == "is_business":
            return ia.is_business

    # OrderPosition specific fields
    if isinstance(obj, OrderPosition):
        if key == "attendee_name":
            return obj.attendee_name
        if key == "attendee_given_name":
            parts = getattr(obj, "attendee_name_parts", {}) or {}
            if parts.get("_legacy"):
                return parts["_legacy"].split(" ", 1)[0]
            return parts.get("_given_name", parts.get("given_name"))
        if key == "attendee_family_name":
            parts = getattr(obj, "attendee_name_parts", {}) or {}
            if parts.get("_legacy"):
                spl = parts["_legacy"].split(" ", 1)
                return spl[1] if len(spl) > 1 else None
            return parts.get("_family_name", parts.get("family_name"))
        if key == "attendee_email":
            return obj.attendee_email
        if key in (
            "company",
            "job_title",
            "street",
            "zipcode",
            "city",
            "state",
            "price",
            "positionid",
            "secret",
        ):
            return getattr(obj, key, None)
        if key == "country":
            return str(obj.country) if obj.country else None
        if key == "voucher":
            return obj.voucher.code if obj.voucher else None
        item = getattr(obj, "product", None) or getattr(obj, "item", None)
        if key == "item_name":
            return item.name if item else None
        if key == "item_admission":
            return item.admission if item else None

        # Order fields extracted from OrderPosition
        if key.startswith("order_"):
            attr = key.replace("order_", "")
            if attr == "code":
                return obj.order.code
            if attr == "email":
                return obj.order.email
            if attr == "email_domain":
                return obj.order.email.split("@")[-1] if obj.order.email else None
            if attr == "total":
                return obj.order.total
            if attr == "status":
                return obj.order.status
            if attr == "datetime":
                return obj.order.datetime
            if attr == "locale":
                return obj.order.locale
            if attr == "phone":
                return obj.order.phone
            if attr == "comment":
                return obj.order.comment

    # Order fields directly on Order
    if isinstance(obj, Order):
        if key == "email_domain":
            return obj.email.split("@")[-1] if obj.email else None
        if key == "order_link":
            return settings.SITE_URL + reverse(
                "presale:event.order",
                kwargs={
                    "organizer": obj.event.organizer.slug,
                    "event": obj.event.slug,
                    "order": obj.code,
                    "secret": obj.secret,
                },
            )
        # Direct attributes on Order
        if key in (
            "code",
            "status",
            "total",
            "datetime",
            "email",
            "payment_datetime",
            "locale",
            "phone",
            "comment",
            "testmode",
        ):
            return getattr(obj, key, None)

    return getattr(obj, key, None)


def resolve_hubspot_properties(
    obj: Any, object_mapping: ObjectTypeMapping
) -> Dict[str, Any]:
    """
    Takes an eventyay object (Order or OrderPosition) and an ObjectTypeMapping,
    and returns a dict of HubSpot property values using the field mappings.
    """
    content_type = ContentType.objects.get_for_model(obj)
    field_mappings = HubSpotFieldMapping.objects.filter(
        event=object_mapping.event,
        content_type=content_type,
        hubspot_object_type=object_mapping.hubspot_object_type,
        is_active=True,
    )

    properties = {}
    for mapping in field_mappings:
        val = None
        # Handle dynamic questions
        if mapping.eventyay_field.startswith("question_"):
            q_id = mapping.eventyay_field.replace("question_", "", 1)
            val = _get_question_value(obj, q_id)
        # Handle generic paths
        else:
            val = _resolve_eventyay_field(obj, mapping.eventyay_field)

        hs_prop = HubSpotProperty.objects.filter(
            event=object_mapping.event,
            object_type=object_mapping.hubspot_object_type,
            key=mapping.hubspot_property,
        ).first()

        if not hs_prop:
            # Skip properties that don't exist in HubSpot or are read-only
            continue

        data_type = hs_prop.data_type
        val = _convert_value(val, data_type)
        if val is not None:
            properties[mapping.hubspot_property] = val

    return properties


def _convert_value(value: Any, data_type: str) -> Any:
    """Convert eventyay value to matching HubSpot data type."""
    if value is None or value == "":
        return None

    if data_type == "text":
        return str(value)
    elif data_type == "number":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    elif data_type == "date":
        # HubSpot expects date in specific format. Usually milliseconds since epoch or YYYY-MM-DD
        # Let's return YYYY-MM-DD for dates or ISO 8601 for datetime
        if hasattr(value, "isoformat"):
            # If it's a datetime, HubSpot usually accepts ISO 8601 or date
            return value.isoformat()
        return str(value)
    elif data_type == "yes/no":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return "true" if value.lower() in ("true", "yes", "1", "y") else "false"
        return "true" if value else "false"

    return str(value)


@shared_task(bind=True, max_retries=3)
def sync_order_to_hubspot(self, order_id: int, event_id: int):
    """
    Main sync task that resolves fields, applies sync modes, and pushes to HubSpot.
    """
    lock_key = f"hubspot_sync_running_{order_id}"
    if not cache.add(lock_key, "1", timeout=60 * 5):
        raise self.retry(countdown=10)

    try:
        try:
            with scopes_disabled():
                event = Event.objects.get(id=event_id)
        except Event.DoesNotExist:
            return

        with scope(organizer=event.organizer):
            settings = HubSpotEventSettings.objects.filter(event=event).first()
            if not settings or not settings.sync_enabled:
                return

            if not get_valid_hubspot_token(event):
                return

            try:
                order = (
                    Order.objects.select_related("invoice_address")
                    .prefetch_related(
                        "positions__product",
                        "positions__voucher",
                        "positions__answers__question",
                    )
                    .get(id=order_id, event=event)
                )
            except Order.DoesNotExist:
                return

            active_mappings = ObjectTypeMapping.objects.filter(event=event)

            try:
                for object_mapping_config in active_mappings:
                    objects_to_sync = []
                    if object_mapping_config.eventyay_object_type == "order":
                        objects_to_sync = [order]
                    elif object_mapping_config.eventyay_object_type == "order_position":
                        objects_to_sync = list(order.positions.all())

                    for obj in objects_to_sync:
                        _sync_single_object(event, object_mapping_config, obj)
            except HubSpotTransientError as e:
                delay = 2**self.request.retries
                retry_after = getattr(e, "retry_after_seconds", None)
                if retry_after:
                    delay = max(delay, retry_after)
                raise self.retry(exc=e, countdown=delay)
    finally:
        cache.delete(lock_key)


def _sync_single_object(event: Event, config: ObjectTypeMapping, obj: Any):
    content_type = ContentType.objects.get_for_model(obj)

    # 1. Resolve raw fields using mapping
    raw_properties = resolve_hubspot_properties(obj, config)

    # 2. Apply sync mode rules
    field_mappings = HubSpotFieldMapping.objects.filter(
        event=event,
        content_type=content_type,
        hubspot_object_type=config.hubspot_object_type,
        is_active=True,
    )
    mapping_modes = {fm.hubspot_property: fm.sync_mode for fm in field_mappings}

    # Fetch existing tracking record if any
    try:
        hs_mapping = HubSpotObjectMapping.objects.get(
            event=event,
            content_type=content_type,
            object_id=obj.id,
            hubspot_object_type=config.hubspot_object_type,
        )
        existing_id = hs_mapping.hubspot_object_id
        if not existing_id:
            existing_id = None
    except HubSpotObjectMapping.DoesNotExist:
        hs_mapping = None
        existing_id = None

    existing_properties = {}
    if existing_id:
        fill_if_empty_fields = [
            prop
            for prop, val in raw_properties.items()
            if mapping_modes.get(prop) == SyncMode.FILL_IF_EMPTY
        ]
        if fill_if_empty_fields:
            try:
                existing_properties = get_record(
                    event, config.hubspot_object_type, existing_id, fill_if_empty_fields
                )
            except HubSpotTransientError as e:
                raise e
            except HubSpotPermanentError as e:
                # If we fail to fetch, it's safer to not overwrite anything
                logger.warning(
                    f"Could not fetch record {existing_id} from HubSpot: {e}"
                )

    properties_to_send = {}
    for prop, val in raw_properties.items():
        mode = mapping_modes.get(prop, SyncMode.OVERWRITE)

        if mode == SyncMode.IDENTIFIER:
            # Identifier is used to find/match, but also usually sent
            properties_to_send[prop] = val
        elif mode == SyncMode.OVERWRITE:
            properties_to_send[prop] = val
        elif mode == SyncMode.FILL_IF_NEW:
            if not existing_id:
                properties_to_send[prop] = val
            # If existing_id is known, we skip FILL_IF_NEW
        elif mode == SyncMode.FILL_IF_EMPTY:
            if not existing_id:
                properties_to_send[prop] = val
            else:
                existing_val = existing_properties.get(prop)
                if existing_val is None or existing_val == "":
                    properties_to_send[prop] = val

    if not properties_to_send:
        with transaction.atomic():
            hs_mapping, _ = HubSpotObjectMapping.objects.update_or_create(
                event=event,
                content_type=content_type,
                object_id=obj.id,
                hubspot_object_type=config.hubspot_object_type,
                defaults={
                    "hubspot_object_id": existing_id or "",
                    "last_synced_at": now(),
                },
            )
            SyncLog.objects.create(
                event=event,
                object_mapping=hs_mapping,
                action=SyncAction.UPDATE if existing_id else SyncAction.CREATE,
                direction=SyncDirection.PUSH,
                status=SyncStatus.SUCCESS,
                detail={
                    "message": "No properties to sync based on mappings and current values"
                },
            )
        return

    try:
        conflict_detected = False
        if existing_id:
            try:
                hubspot_id = update_record(
                    event, config.hubspot_object_type, existing_id, properties_to_send
                )
                action = SyncAction.UPDATE
            except HubSpotRecordNotFoundError:
                try:
                    hubspot_id = create_record(
                        event, config.hubspot_object_type, properties_to_send
                    )
                    action = SyncAction.CREATE
                except HubSpotConflictError as conflict:
                    existing_id = conflict.existing_id
                    action = SyncAction.UPDATE
                    conflict_detected = True
        else:
            try:
                hubspot_id = create_record(
                    event, config.hubspot_object_type, properties_to_send
                )
                action = SyncAction.CREATE
            except HubSpotConflictError as conflict:
                existing_id = conflict.existing_id
                action = SyncAction.UPDATE
                conflict_detected = True

        if action == SyncAction.UPDATE and conflict_detected:
            # We must re-evaluate properties using the newly discovered existing_id to respect FILL_IF_NEW / FILL_IF_EMPTY!
            existing_properties = {}
            fill_if_empty_fields = [
                prop
                for prop, val in raw_properties.items()
                if mapping_modes.get(prop) == SyncMode.FILL_IF_EMPTY
            ]
            if fill_if_empty_fields:
                try:
                    existing_properties = get_record(
                        event,
                        config.hubspot_object_type,
                        existing_id,
                        fill_if_empty_fields,
                    )
                except HubSpotTransientError as e:
                    raise e
                except HubSpotPermanentError as e:
                    logger.warning(
                        f"Could not fetch record {existing_id} from HubSpot on conflict: {e}"
                    )

            properties_to_send = {}
            for prop, val in raw_properties.items():
                mode = mapping_modes.get(prop, SyncMode.OVERWRITE)
                if mode == SyncMode.IDENTIFIER:
                    properties_to_send[prop] = val
                elif mode == SyncMode.OVERWRITE:
                    properties_to_send[prop] = val
                elif mode == SyncMode.FILL_IF_NEW:
                    # Record already exists, skip FILL_IF_NEW
                    pass
                elif mode == SyncMode.FILL_IF_EMPTY:
                    existing_val = existing_properties.get(prop)
                    if existing_val is None or existing_val == "":
                        properties_to_send[prop] = val

            if properties_to_send:
                hubspot_id = update_record(
                    event, config.hubspot_object_type, existing_id, properties_to_send
                )
            else:
                hubspot_id = existing_id or ""

        # Update or create the tracking record
        with transaction.atomic():
            hs_mapping, _ = HubSpotObjectMapping.objects.update_or_create(
                event=event,
                content_type=content_type,
                object_id=obj.id,
                hubspot_object_type=config.hubspot_object_type,
                defaults={
                    "hubspot_object_id": hubspot_id,
                    "last_synced_at": now(),
                },
            )
            SyncLog.objects.create(
                event=event,
                object_mapping=hs_mapping,
                action=action,
                direction=SyncDirection.PUSH,
                status=SyncStatus.SUCCESS,
            )
    except HubSpotTransientError as e:
        raise e
    except HubSpotPermanentError as e:
        if not hs_mapping:
            hs_mapping, _ = HubSpotObjectMapping.objects.update_or_create(
                event=event,
                content_type=content_type,
                object_id=obj.id,
                hubspot_object_type=config.hubspot_object_type,
                defaults={
                    "hubspot_object_id": existing_id or "",
                },
            )
        SyncLog.objects.create(
            event=event,
            object_mapping=hs_mapping,
            action=SyncAction.UPDATE if existing_id else SyncAction.CREATE,
            direction=SyncDirection.PUSH,
            status=SyncStatus.FAILED,
            detail={"error": str(e), "status_code": getattr(e, "status_code", None)},
        )


@shared_task(bind=True, max_retries=3)
def sync_all_mappings_task(self, event_id: int):
    """
    Background task to enqueue sync_order_to_hubspot for all orders of an event,
    which processes all active object/field mappings for each order.
    """
    with scopes_disabled():
        order_ids = list(
            Order.objects.filter(event_id=event_id).values_list("id", flat=True)
        )

    for order_id in order_ids:
        sync_order_to_hubspot.apply_async(args=[order_id, event_id], countdown=0)


@shared_task
def hubspot_recovery_sweep():
    """
    Finds and re-queues HubSpot syncs that failed, are incomplete, or never ran.
    """
    with scopes_disabled():
        # Iterate over all events with sync_enabled and valid token
        events = list(Event.objects.filter(hubspoteventsettings__sync_enabled=True))

    for event in events:
        if not get_valid_hubspot_token(event):
            continue

        with scopes_disabled():
            active_mappings = list(ObjectTypeMapping.objects.filter(event=event))

        if not active_mappings:
            continue

        _sweep_event(event, active_mappings)


def _sweep_event(event: Event, active_mappings: list):
    with scopes_disabled():
        paid_orders = Order.objects.filter(event=event, status=Order.STATUS_PAID)
        order_ct = ContentType.objects.get_for_model(Order)
        order_pos_ct = ContentType.objects.get_for_model(OrderPosition)

        orders_to_sync = set()

        for config in active_mappings:
            if config.eventyay_object_type == "order":
                ct = order_ct

                # Missing completely
                synced = HubSpotObjectMapping.objects.filter(
                    event=event,
                    content_type=ct,
                    hubspot_object_type=config.hubspot_object_type,
                ).values_list("object_id", flat=True)

                missing = paid_orders.exclude(id__in=synced).values_list(
                    "id", flat=True
                )
                orders_to_sync.update(missing)

                # Failed / Incomplete
                failed = (
                    HubSpotObjectMapping.objects.filter(
                        event=event,
                        content_type=ct,
                        hubspot_object_type=config.hubspot_object_type,
                        synclog__status=SyncStatus.FAILED,
                    )
                    .filter(
                        Q(last_synced_at__isnull=True)
                        | Q(synclog__created_at__gt=F("last_synced_at"))
                    )
                    .values_list("object_id", flat=True)
                )

                failed_paid = paid_orders.filter(id__in=failed).values_list(
                    "id", flat=True
                )
                orders_to_sync.update(failed_paid)

            elif config.eventyay_object_type == "order_position":
                ct = order_pos_ct
                paid_positions = OrderPosition.objects.filter(
                    order__event=event, order__status=Order.STATUS_PAID
                )

                synced = HubSpotObjectMapping.objects.filter(
                    event=event,
                    content_type=ct,
                    hubspot_object_type=config.hubspot_object_type,
                ).values_list("object_id", flat=True)

                missing_pos = paid_positions.exclude(id__in=synced)
                orders_to_sync.update(missing_pos.values_list("order_id", flat=True))

                failed = (
                    HubSpotObjectMapping.objects.filter(
                        event=event,
                        content_type=ct,
                        hubspot_object_type=config.hubspot_object_type,
                        synclog__status=SyncStatus.FAILED,
                    )
                    .filter(
                        Q(last_synced_at__isnull=True)
                        | Q(synclog__created_at__gt=F("last_synced_at"))
                    )
                    .values_list("object_id", flat=True)
                )

                failed_pos = paid_positions.filter(id__in=failed)
                orders_to_sync.update(failed_pos.values_list("order_id", flat=True))

        for order_id in orders_to_sync:
            cache_key = f"hubspot_sync_enqueued_{order_id}"
            if cache.add(cache_key, "1", timeout=5):
                sync_order_to_hubspot.apply_async(
                    args=[order_id, event.id], countdown=5
                )
