import datetime
import logging
import os

import requests
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db import transaction
from django.utils.timezone import now
from django_scopes import scope
from eventyay.base.models import Order, OrderPosition
from eventyay.base.settings import GlobalSettingsObject

from hubspot.operational_log import logged_request

from .models import (
    AuditAction,
    AuditLog,
    EventyayObjectType,
    HubSpotEventSettings,
    HubSpotFieldMapping,
    HubSpotOAuthToken,
    ObjectTypeMapping,
    OrganizerDefaultObjectTypeMapping,
    OrganizerHubSpotOAuthToken,
    OrganizerHubSpotSettings,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncMode,
    SyncStatus,
)


class HubSpotFetchError(Exception):
    """Raised when fetching data from HubSpot API fails."""

    pass


def get_hubspot_properties(event, object_type: str, force_sync: bool = False, organizer=None) -> list[dict]:
    """
    Returns synced HubSpot properties from the cache.
    If no complete sync exists, fetches synchronously.
    If stale (older than TTL) or force_sync is True,
    triggers a background Celery task and serves stale data.
    """
    prefix = f"org_{organizer.id}" if organizer else f"evt_{event.id}"
    data_key = f"hubspot_properties_{prefix}_{object_type}"
    lock_key = f"hubspot_properties_lock_{prefix}_{object_type}"
    error_key = f"hubspot_properties_error_{prefix}_{object_type}"
    rate_limit_key = f"hubspot_auto_sync_limit_{prefix}_{object_type}"

    gs = GlobalSettingsObject()
    try:
        ttl_minutes = int(
            gs.settings.hubspot_property_sync_ttl_minutes or os.environ.get("HUBSPOT_PROPERTY_SYNC_TTL_MINUTES", "10")
        )
    except (ValueError, TypeError):
        ttl_minutes = 10

    if force_sync:
        cache.delete(error_key)

    has_error = cache.get(error_key) is not None
    cached_data = cache.get(data_key)

    is_stale = False
    if cached_data:
        fetched_at = cached_data.get("fetched_at")
        if not fetched_at or fetched_at < now() - datetime.timedelta(minutes=ttl_minutes):
            is_stale = True

    if not cached_data:
        if has_error and not force_sync:
            return []
        if cache.add(lock_key, "1", timeout=300):
            manual_sync_lock_key = f"hubspot_manual_sync_lock_{prefix}_{object_type}"
            cache.add(manual_sync_lock_key, "1", timeout=60)

            from .tasks import refresh_hubspot_properties_task

            refresh_hubspot_properties_task.apply_async(
                args=[
                    event.id if event else None,
                    object_type,
                    organizer.id if organizer else None,
                ]
            )
        else:
            cached_data = cache.get(data_key)
            if cached_data:
                return cached_data.get("properties", [])
        return []

    if force_sync or (not has_error and is_stale):
        if cache.add(lock_key, "1", timeout=300):
            if force_sync or cache.add(rate_limit_key, "1", timeout=30):
                from .tasks import refresh_hubspot_properties_task

                refresh_hubspot_properties_task.apply_async(
                    args=[
                        event.id if event else None,
                        object_type,
                        organizer.id if organizer else None,
                    ]
                )
            else:
                cache.delete(lock_key)

    return cached_data.get("properties", [])


def sync_hubspot_properties(event, object_type: str, organizer=None) -> list[dict]:
    """
    Fetches properties from HubSpot page by page and returns a list.
    """
    if organizer:
        token = get_valid_organizer_hubspot_token(organizer)
        prefix = f"org_{organizer.id}"
    else:
        token = get_valid_hubspot_token(event, allow_conflict=True, require_sync_enabled=False)
        prefix = f"evt_{event.id}"

    if not token:
        raise HubSpotFetchError("Not connected to HubSpot or token is invalid.")

    base_url = f"https://api.hubapi.com/crm/v3/properties/{object_type}"
    headers = {"Authorization": f"Bearer {token}"}
    cursor = ""
    properties = []
    lock_key = f"hubspot_properties_lock_{prefix}_{object_type}"

    while True:
        cache.set(lock_key, "1", timeout=300)
        params = {}
        if cursor:
            params["after"] = cursor

        try:
            response = logged_request("hubspot", "GET", base_url, headers=headers, params=params, timeout=15)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                e = HubSpotFetchError("Rate limited by HubSpot")
                if retry_after:
                    e.retry_after = int(retry_after)
                raise e
            response.raise_for_status()
        except requests.RequestException as e:
            raise HubSpotFetchError(
                "Could not connect to HubSpot API. Please check your connection and try again."
            ) from e

        data = response.json()
        results = data.get("results", [])

        for prop in results:
            if prop.get("hidden"):
                continue

            modification_metadata = prop.get("modificationMetadata", {})
            if modification_metadata.get("readOnlyValue"):
                continue

            if prop.get("calculated"):
                continue

            properties.append(
                {
                    "key": prop.get("name"),
                    "label": prop.get("label", ""),
                    "data_type": _map_hubspot_type(prop.get("type", "string")),
                }
            )

        paging = data.get("paging", {})
        next_page = paging.get("next", {})
        cursor = next_page.get("after", "")

        if not cursor:
            break

    return properties


_HUBSPOT_TYPE_MAP = {
    "number": "number",
    "date": "date",
    "datetime": "date",
    "bool": "yes/no",
}


def _map_hubspot_type(hubspot_type: str) -> str:
    return _HUBSPOT_TYPE_MAP.get(hubspot_type, "text")


def _refresh_token_record(token_obj, event_or_organizer, is_organizer=False):
    """
    Refreshes the token and updates the record.
    Returns the new access_token if successful, otherwise None.
    """
    logger = logging.getLogger(__name__)
    organizer = event_or_organizer if is_organizer else event_or_organizer.organizer

    try:
        gs = GlobalSettingsObject()
        response = logged_request(
            "hubspot",
            "POST",
            "https://api.hubapi.com/oauth/v1/token",
            data={
                "grant_type": "refresh_token",
                "client_id": gs.settings.hubspot_client_id or os.environ.get("HUBSPOT_CLIENT_ID", ""),
                "client_secret": gs.settings.hubspot_client_secret or os.environ.get("HUBSPOT_CLIENT_SECRET", ""),
                "refresh_token": token_obj.refresh_token,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        logger.error(
            "Network error refreshing HubSpot token for %s %s: %s",
            "organizer" if is_organizer else "event",
            event_or_organizer.slug,
            e,
        )
        AuditLog.objects.create(
            organizer=organizer,
            event=None if is_organizer else event_or_organizer,
            action=AuditAction.REFRESH_FAILED,
        )
        if not is_organizer:
            SyncLog.objects.create(
                event=event_or_organizer,
                action=SyncAction.REFRESH_FAILED,
                direction=SyncDirection.PUSH,
                status=SyncStatus.FAILED,
                detail={"error": str(e)},
            )
        return None

    if not response.ok:
        AuditLog.objects.create(
            organizer=organizer,
            event=None if is_organizer else event_or_organizer,
            action=AuditAction.REFRESH_FAILED,
        )
        if not is_organizer:
            SyncLog.objects.create(
                event=event_or_organizer,
                action=SyncAction.REFRESH_FAILED,
                direction=SyncDirection.PUSH,
                status=SyncStatus.FAILED,
                detail={"error": response.text},
            )
        return None

    data = response.json()
    expires_in = data.get("expires_in")
    expires_at = now() + datetime.timedelta(seconds=expires_in) if expires_in else None

    token_obj.access_token = data.get("access_token")
    if data.get("refresh_token"):
        token_obj.refresh_token = data.get("refresh_token")
    token_obj.expires_at = expires_at
    token_obj.save()

    AuditLog.objects.create(
        organizer=organizer,
        event=None if is_organizer else event_or_organizer,
        action=AuditAction.TOKEN_REFRESH,
    )
    if not is_organizer:
        SyncLog.objects.create(
            event=event_or_organizer,
            action=SyncAction.TOKEN_REFRESH,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
            detail={"message": "Token refreshed successfully"},
        )

    return token_obj.access_token


def get_valid_organizer_hubspot_token(organizer) -> str | None:
    """
    Returns a valid HubSpot access token for the given organizer.
    """
    with transaction.atomic():
        try:
            org_token = OrganizerHubSpotOAuthToken.objects.select_for_update().get(organizer=organizer)
            if org_token.expires_at and org_token.expires_at > now() + datetime.timedelta(minutes=5):
                return org_token.access_token
            return _refresh_token_record(org_token, organizer, is_organizer=True)
        except OrganizerHubSpotOAuthToken.DoesNotExist:
            return None


def get_valid_hubspot_token(event, allow_conflict=False, require_sync_enabled=True) -> str | None:
    """
    Returns a valid HubSpot access token for the given event.
    If sync is disabled for the event, returns None.
    If the event has a token, uses it.
    Otherwise, checks the organizer's token if organizer sync is enabled.
    """
    if require_sync_enabled and not is_sync_enabled(event):
        return None

    if not allow_conflict:
        with scope(organizer=event.organizer):
            settings = HubSpotEventSettings.objects.filter(event=event).first()
            if settings and settings.has_mapping_conflict:
                return None

    with transaction.atomic(), scope(organizer=event.organizer):
        # 1. Try event token
        try:
            event_token = HubSpotOAuthToken.objects.select_for_update().get(event=event)
            if event_token.expires_at and event_token.expires_at > now() + datetime.timedelta(minutes=5):
                return event_token.access_token
            return _refresh_token_record(event_token, event, is_organizer=False)
        except HubSpotOAuthToken.DoesNotExist:
            pass

        # 2. Check Organizer settings
        if require_sync_enabled:
            try:
                org_settings = OrganizerHubSpotSettings.objects.get(organizer=event.organizer)
                if not org_settings.sync_enabled:
                    return None
            except OrganizerHubSpotSettings.DoesNotExist:
                return None

        # 3. Try Organizer token
        try:
            org_token = OrganizerHubSpotOAuthToken.objects.select_for_update().get(organizer=event.organizer)
            if org_token.expires_at and org_token.expires_at > now() + datetime.timedelta(minutes=5):
                return org_token.access_token
            return _refresh_token_record(org_token, event.organizer, is_organizer=True)
        except OrganizerHubSpotOAuthToken.DoesNotExist:
            return None


def is_sync_enabled(event) -> bool:
    """
    Returns True if HubSpot sync is enabled and a valid token exists
    for the event or its organizer.
    If HubSpotEventSettings exists for the event, its sync_enabled overrides organizer fallback.
    """
    with scope(organizer=event.organizer):
        ev_settings = HubSpotEventSettings.objects.filter(event=event).first()
        if ev_settings is not None:
            if not ev_settings.sync_enabled:
                return False

        # Event sync is enabled (or settings don't exist yet), check if event has a token
        if HubSpotOAuthToken.objects.filter(event=event).exists():
            return True

        org_settings = OrganizerHubSpotSettings.objects.filter(organizer=event.organizer).first()
        if org_settings is not None and org_settings.sync_enabled:
            if OrganizerHubSpotOAuthToken.objects.filter(organizer=event.organizer).exists():
                return True

        return False


def is_auto_sync_enabled(event) -> bool:
    """
    Returns True if auto sync is enabled for the event.
    """
    try:
        return HubSpotEventSettings.objects.get(event=event).auto_sync_enabled
    except HubSpotEventSettings.DoesNotExist:
        return False


def apply_default_mappings_to_all_events(organizer):
    """
    Applies organizer-level default HubSpot mappings to all existing events.
    Reconciles: upserts current defaults, deletes orphaned organizer_default
    rows, then recomputes conflicts from the full event mapping set.
    """

    with scope(organizer=organizer):
        events = list(organizer.events.all())
        default_obj_mappings = list(
            OrganizerDefaultObjectTypeMapping.objects.filter(organizer=organizer).prefetch_related("field_mappings")
        )

        order_ct = ContentType.objects.get_for_model(Order)
        order_position_ct = ContentType.objects.get_for_model(OrderPosition)

        existing_settings = {s.event_id: s for s in HubSpotEventSettings.objects.filter(event__in=events)}

        settings_to_create = []
        for event in events:
            if event.id not in existing_settings:
                settings_to_create.append(HubSpotEventSettings(event=event))
        if settings_to_create:
            HubSpotEventSettings.objects.bulk_create(settings_to_create)
            existing_settings = {s.event_id: s for s in HubSpotEventSettings.objects.filter(event__in=events)}

        object_type_mappings_to_create = []
        for event in events:
            for default_obj in default_obj_mappings:
                object_type_mappings_to_create.append(
                    ObjectTypeMapping(
                        event=event,
                        eventyay_object_type=default_obj.eventyay_object_type,
                        hubspot_object_type=default_obj.hubspot_object_type,
                        position=default_obj.position,
                    )
                )
        if object_type_mappings_to_create:
            ObjectTypeMapping.objects.bulk_create(
                object_type_mappings_to_create,
                update_conflicts=True,
                unique_fields=["event", "eventyay_object_type", "hubspot_object_type"],
                update_fields=["position"],
            )

        valid_default_obj_pairs = set()
        valid_default_field_keys = set()
        for default_obj in default_obj_mappings:
            if default_obj.eventyay_object_type == EventyayObjectType.ORDER:
                ct = order_ct
            elif default_obj.eventyay_object_type == EventyayObjectType.ORDER_POSITION:
                ct = order_position_ct
            else:
                continue
            valid_default_obj_pairs.add((default_obj.eventyay_object_type, default_obj.hubspot_object_type))
            for df in default_obj.field_mappings.all():
                valid_default_field_keys.add((ct.id, df.eventyay_field, default_obj.hubspot_object_type))

        field_mappings_to_create = []
        for event in events:
            for default_obj in default_obj_mappings:
                if default_obj.eventyay_object_type == EventyayObjectType.ORDER:
                    content_type = order_ct
                elif default_obj.eventyay_object_type == EventyayObjectType.ORDER_POSITION:
                    content_type = order_position_ct
                else:
                    continue

                for default_field in default_obj.field_mappings.all():
                    field_mappings_to_create.append(
                        HubSpotFieldMapping(
                            event=event,
                            content_type=content_type,
                            eventyay_field=default_field.eventyay_field,
                            hubspot_object_type=default_obj.hubspot_object_type,
                            source="organizer_default",
                            hubspot_property=default_field.hubspot_property,
                            sync_mode=default_field.sync_mode,
                            is_active=default_field.is_active,
                        )
                    )

        if field_mappings_to_create:
            HubSpotFieldMapping.objects.bulk_create(
                field_mappings_to_create,
                update_conflicts=True,
                unique_fields=[
                    "event",
                    "content_type",
                    "eventyay_field",
                    "hubspot_object_type",
                    "source",
                ],
                update_fields=["hubspot_property", "sync_mode", "is_active"],
            )

        # -- Delete orphaned organizer_default field mappings --
        orphaned_fields = HubSpotFieldMapping.objects.filter(event__in=events, source="organizer_default")
        if valid_default_field_keys:
            from django.db.models import Q

            keep = Q()
            for ct_id, ey_field, hs_obj_type in valid_default_field_keys:
                keep |= Q(
                    content_type_id=ct_id,
                    eventyay_field=ey_field,
                    hubspot_object_type=hs_obj_type,
                )
            orphaned_fields = orphaned_fields.exclude(keep)
        orphaned_fields.delete()

        settings_to_update = []
        for event in events:
            settings = existing_settings[event.id]
            all_mappings = list(HubSpotFieldMapping.objects.filter(event=event))
            conflict_found = _check_event_mapping_conflicts(all_mappings)

            if conflict_found != settings.has_mapping_conflict:
                settings.has_mapping_conflict = conflict_found
                settings_to_update.append(settings)

        if settings_to_update:
            HubSpotEventSettings.objects.bulk_update(settings_to_update, ["has_mapping_conflict"])


def _check_event_mapping_conflicts(event_mappings):
    """
    Returns True if any duplicate eventyay_field, hubspot_property, or
    multiple identifiers exist within the same (content_type, hubspot_object_type)
    group across all sources.
    """
    from collections import defaultdict

    # Group by (content_type_id, hubspot_object_type)
    groups = defaultdict(list)
    for m in event_mappings:
        groups[(m.content_type_id, m.hubspot_object_type)].append(m)

    for mappings in groups.values():
        seen_fields = set()
        seen_props = set()
        identifier_count = 0
        for m in mappings:
            if m.eventyay_field in seen_fields:
                return True
            seen_fields.add(m.eventyay_field)

            if m.hubspot_property in seen_props:
                return True
            seen_props.add(m.hubspot_property)

            if m.sync_mode == SyncMode.IDENTIFIER:
                identifier_count += 1
                if identifier_count > 1:
                    return True
    return False


def check_and_clear_mapping_conflict(event):
    """
    Checks if an event's field mappings have any conflict (duplicate field,
    property, or identifier) and updates the flag accordingly.
    """
    with scope(organizer=event.organizer):
        settings = HubSpotEventSettings.objects.filter(event=event).first()
        if not settings:
            return

        event_mappings = list(HubSpotFieldMapping.objects.filter(event=event))
        conflict_found = _check_event_mapping_conflicts(event_mappings)

        if conflict_found != settings.has_mapping_conflict:
            settings.has_mapping_conflict = conflict_found
            settings.save(update_fields=["has_mapping_conflict"])


def bootstrap_event_hubspot(event):
    """
    Bootstraps HubSpot settings for a newly created event if the organizer has HubSpot sync enabled.
    """
    with scope(organizer=event.organizer):
        try:
            org_settings = OrganizerHubSpotSettings.objects.get(organizer=event.organizer)
        except OrganizerHubSpotSettings.DoesNotExist:
            return

        if not org_settings.sync_enabled:
            return

        HubSpotEventSettings.objects.get_or_create(event=event, defaults={"sync_enabled": True})

        plugins = event.get_plugins()
        if "hubspot" not in plugins:
            event.enable_plugin("hubspot")

        apply_default_mappings_to_all_events(event.organizer)
