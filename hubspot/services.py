import datetime
import logging
import os
import uuid

import requests
from django.db import transaction
from django.utils.timezone import now
from django_scopes import scope

from .models import (
    AuditAction,
    AuditLog,
    HubSpotOAuthToken,
    HubSpotProperty,
    HubSpotPropertySyncState,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncStatus,
)


class HubSpotFetchError(Exception):
    """Raised when fetching data from HubSpot API fails."""

    pass


def get_hubspot_properties(
    event, object_type: str, force_sync: bool = False
) -> list[dict]:
    """
    Returns synced HubSpot properties from the DB.
    If no complete sync exists, is stale (older than TTL) or force_sync is True,
    triggers a chunk-wise sync first. Retries up to 4 times with 30 s / 60 s / 120 s delays.
    """
    sync_state = HubSpotPropertySyncState.objects.filter(
        event=event, object_type=object_type, is_complete=True
    ).first()

    try:
        ttl_minutes = int(os.environ.get("HUBSPOT_PROPERTY_SYNC_TTL_MINUTES", "10"))
    except ValueError:
        ttl_minutes = 10

    if (
        force_sync
        or not sync_state
        or (
            sync_state.completed_at
            and sync_state.completed_at
            < now() - datetime.timedelta(minutes=ttl_minutes)
        )
    ):
        try:
            sync_hubspot_properties(event, object_type)
        except HubSpotFetchError as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to fetch properties from HubSpot: {e}")
            if not HubSpotProperty.objects.filter(
                event=event, object_type=object_type
            ).exists():
                raise e

    return list(
        HubSpotProperty.objects.filter(event=event, object_type=object_type).values(
            "key", "label", "data_type"
        )
    )


def sync_hubspot_properties(event, object_type: str):
    """
    Fetches properties from HubSpot page by page, persisting each chunk to the DB.
    Resumes from the last cursor if a previous sync was interrupted.
    """
    token = get_valid_hubspot_token(event)
    if not token:
        raise HubSpotFetchError("Not connected to HubSpot or token is invalid.")

    sync_state, created = HubSpotPropertySyncState.objects.get_or_create(
        event=event,
        object_type=object_type,
        defaults={"sync_batch": uuid.uuid4()},
    )

    if sync_state.is_complete:
        sync_state.sync_batch = uuid.uuid4()
        sync_state.next_cursor = ""
        sync_state.is_complete = False
        sync_state.completed_at = None
        sync_state.save(
            update_fields=["sync_batch", "next_cursor", "is_complete", "completed_at"]
        )

    batch_id = sync_state.sync_batch
    cursor = sync_state.next_cursor
    base_url = f"https://api.hubapi.com/crm/v3/properties/{object_type}"
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        params = {}
        if cursor:
            params["after"] = cursor

        try:
            response = requests.get(
                base_url, headers=headers, params=params, timeout=15
            )
            response.raise_for_status()
        except requests.RequestException:
            raise HubSpotFetchError(
                "Could not connect to HubSpot API. Please check your connection and try again."
            )

        data = response.json()
        results = data.get("results", [])

        for prop in results:
            if prop.get("hidden"):
                continue
            HubSpotProperty.objects.update_or_create(
                event=event,
                object_type=object_type,
                key=prop.get("name"),
                defaults={
                    "label": prop.get("label", ""),
                    "data_type": _map_hubspot_type(prop.get("type", "string")),
                    "sync_batch": batch_id,
                },
            )

        paging = data.get("paging", {})
        next_page = paging.get("next", {})
        cursor = next_page.get("after", "")

        if cursor:
            sync_state.next_cursor = cursor
            sync_state.save(update_fields=["next_cursor"])
        else:
            HubSpotProperty.objects.filter(
                event=event, object_type=object_type
            ).exclude(sync_batch=batch_id).delete()

            sync_state.is_complete = True
            sync_state.completed_at = now()
            sync_state.next_cursor = ""
            sync_state.save()
            break


_HUBSPOT_TYPE_MAP = {
    "number": "number",
    "date": "date",
    "datetime": "date",
    "bool": "yes/no",
}


def _map_hubspot_type(hubspot_type: str) -> str:
    return _HUBSPOT_TYPE_MAP.get(hubspot_type, "text")


def get_valid_hubspot_token(event) -> str | None:
    """
    Returns a valid HubSpot access token for the given event.
    If the token expires within 5 minutes, it silently fetches a new one.
    Uses select_for_update() to prevent double-refresh on concurrent requests.
    """
    with transaction.atomic(), scope(organizer=event.organizer):
        try:
            token = HubSpotOAuthToken.objects.select_for_update().get(event=event)
        except HubSpotOAuthToken.DoesNotExist:
            return None

        # Check if the token is valid for at least 5 more minutes
        if token.expires_at and token.expires_at > now() + datetime.timedelta(
            minutes=5
        ):
            return token.access_token

        # Token is expired or expiring soon, refresh it
        logger = logging.getLogger(__name__)
        try:
            response = requests.post(
                "https://api.hubapi.com/oauth/v1/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": os.environ.get("HUBSPOT_CLIENT_ID", ""),
                    "client_secret": os.environ.get("HUBSPOT_CLIENT_SECRET", ""),
                    "refresh_token": token.refresh_token,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            logger.error(
                "Network error refreshing HubSpot token for event %s: %s", event.slug, e
            )
            AuditLog.objects.create(
                organizer=event.organizer,
                event=event,
                action=AuditAction.REFRESH_FAILED,
            )
            SyncLog.objects.create(
                event=event,
                action=SyncAction.REFRESH_FAILED,
                direction=SyncDirection.PUSH,
                status=SyncStatus.FAILED,
                detail={"error": str(e)},
            )
            return None

        if not response.ok:
            # Refresh failed. Log and return None.
            SyncLog.objects.create(
                event=event,
                action=SyncAction.REFRESH_FAILED,
                direction=SyncDirection.PUSH,
                status=SyncStatus.FAILED,
                detail={"error": response.text},
            )
            AuditLog.objects.create(
                organizer=event.organizer,
                event=event,
                action=AuditAction.REFRESH_FAILED,
            )
            return None

        data = response.json()
        expires_in = data.get("expires_in")
        expires_at = (
            now() + datetime.timedelta(seconds=expires_in) if expires_in else None
        )

        # Update token locally
        token.access_token = data.get("access_token")
        if data.get("refresh_token"):
            token.refresh_token = data.get("refresh_token")
        token.expires_at = expires_at
        token.save()

        SyncLog.objects.create(
            event=event,
            action=SyncAction.TOKEN_REFRESH,
            direction=SyncDirection.PUSH,
            status=SyncStatus.SUCCESS,
            detail={"message": "Token refreshed successfully"},
        )
        AuditLog.objects.create(
            organizer=event.organizer,
            event=event,
            action=AuditAction.TOKEN_REFRESH,
        )

        return token.access_token
