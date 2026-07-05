import base64
import hashlib
from datetime import datetime, time
from functools import lru_cache

from cryptography.fernet import Fernet
from django.conf import settings
from django.utils.timezone import make_aware
from django.utils.translation import gettext_lazy as _


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    )
    return Fernet(key)


def encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _get_fernet().decrypt(value.encode()).decode()


class ActivityLogSequence:
    """A lazy sequence wrapper around AuditLog and SyncLog querysets.

    Supports len(), slicing, and iteration without eagerly loading all rows
    into memory when paginating without a search query.
    """

    def __init__(self, audit_logs, sync_logs, search_query=None):
        self.audit_logs = audit_logs.order_by("-created_at")
        self.sync_logs = sync_logs.order_by("-created_at")
        self.search_query = search_query
        self._cached_all = None
        self._len = None

        self.AUDIT_ACTION_MAP = {
            "connect": (_("HubSpot was connected"), "connection"),
            "disconnect": (_("HubSpot was disconnected"), "connection"),
            "token_refresh": (
                _("HubSpot connection was renewed automatically"),
                "connection",
            ),
            "refresh_failed": (
                _("HubSpot connection could not be renewed automatically"),
                "connection",
            ),
            "mapping_updated": (_("Field mapping settings were updated"), "settings"),
            "field_map_updated": (
                _("Field mapping settings were updated"),
                "settings",
            ),
            "field_mapping_updated": (
                _("Field mapping settings were updated"),
                "settings",
            ),
        }

        self.SYNC_STATUS_MESSAGES = {
            "success": _("%(obj_name)s synced to HubSpot successfully"),
            "failed": _("%(obj_name)s could not be synced to HubSpot"),
        }

    def __len__(self):
        if self._len is not None:
            return self._len
        if self._cached_all is not None:
            return len(self._cached_all)
        if self.search_query:
            self._evaluate_all()
            return len(self._cached_all)
        self._len = self.audit_logs.count() + self.sync_logs.count()
        return self._len

    def count(self):
        return len(self)

    def exists(self):
        return len(self) > 0

    def all(self):
        return self

    def __bool__(self):
        return len(self) > 0

    def __iter__(self):
        self._evaluate_all()
        return iter(self._cached_all)

    def _format_audit_log(self, log):
        if log.action in [
            "connect",
            "disconnect",
            "token_refresh",
            "refresh_failed",
        ]:
            return None

        text, type_ = self.AUDIT_ACTION_MAP.get(log.action, (log.action, "settings"))
        if self.search_query and self.search_query.lower() not in str(text).lower():
            return None
        return {
            "timestamp": log.created_at,
            "text": text,
            "type": type_,
            "id": f"audit_{log.id}",
            "user": "System",
            "raw": log,
        }

    def _format_sync_log(self, log):
        # Skip actions that are already recorded in AuditLog (like connect, token refresh)
        if log.action in [
            "connect",
            "disconnect",
            "token_refresh",
            "refresh_failed",
        ]:
            return None

        obj_name = "Object"
        if log.object_mapping:
            if log.object_mapping.content_object:
                obj = log.object_mapping.content_object
                content_type_name = log.object_mapping.content_type.name.title()
                model_name = log.object_mapping.content_type.model
                if model_name == "orderposition" and hasattr(obj, "order"):
                    attendee_info = getattr(
                        obj, "attendee_name_cached", None
                    ) or getattr(obj, "attendee_email", None)
                    if not attendee_info and hasattr(obj.order, "email"):
                        attendee_info = obj.order.email
                    order_info = (
                        f" for Order {obj.order.code}"
                        if hasattr(obj.order, "code")
                        else ""
                    )
                    info_str = f" ({attendee_info})" if attendee_info else ""
                    obj_name = f"{content_type_name} {obj}{order_info}{info_str}"
                elif model_name == "order" and hasattr(obj, "code"):
                    email_str = (
                        f" ({obj.email})" if hasattr(obj, "email") and obj.email else ""
                    )
                    obj_name = f"{content_type_name} {obj.code}{email_str}"
                else:
                    obj_name = f"{content_type_name} {obj}"
            elif log.object_mapping.content_type:
                obj_name = log.object_mapping.content_type.name.title()

        message_template = self.SYNC_STATUS_MESSAGES.get(
            log.status, _("%(obj_name)s sync is pending")
        )
        text = message_template % {"obj_name": obj_name}

        if self.search_query and self.search_query.lower() not in str(text).lower():
            return None

        return {
            "timestamp": log.created_at,
            "text": text,
            "type": "sync",
            "id": f"sync_{log.id}",
            "user": "System",
            "raw": log,
        }

    def _fetch_slice(self, limit):
        audit_items = []
        for log in self.audit_logs[:limit]:
            item = self._format_audit_log(log)
            if item:
                audit_items.append(item)
        sync_items = []
        for log in self.sync_logs[:limit]:
            item = self._format_sync_log(log)
            if item:
                sync_items.append(item)
        combined = audit_items + sync_items
        combined.sort(key=lambda x: x["timestamp"], reverse=True)
        return combined

    def __getitem__(self, k):
        if self._cached_all is not None:
            return self._cached_all[k]
        if not self.search_query:
            if isinstance(k, slice):
                if k.stop is not None and k.stop >= 0 and (k.step in (None, 1)):
                    combined = self._fetch_slice(k.stop)
                    return combined[k]
            elif isinstance(k, int) and k >= 0:
                combined = self._fetch_slice(k + 1)
                return combined[k]
        self._evaluate_all()
        return self._cached_all[k]

    def _evaluate_all(self):
        if self._cached_all is not None:
            return
        audit_items = []
        for log in self.audit_logs:
            item = self._format_audit_log(log)
            if item:
                audit_items.append(item)
        sync_items = []
        for log in self.sync_logs:
            item = self._format_sync_log(log)
            if item:
                sync_items.append(item)
        combined = audit_items + sync_items
        combined.sort(key=lambda x: x["timestamp"], reverse=True)
        self._cached_all = combined
        self._len = len(combined)


def get_hubspot_activity_logs(
    event, filter_type=None, date_from=None, date_to=None, search_query=None
):
    from .models import AuditLog, SyncLog

    connection_actions = [
        "connect",
        "disconnect",
        "token_refresh",
        "refresh_failed",
    ]

    if filter_type in [None, "settings"]:
        audit_logs = AuditLog.objects.filter(event=event).exclude(
            action__in=connection_actions
        )
        if date_from:
            dt_from = make_aware(datetime.combine(date_from, time.min))
            audit_logs = audit_logs.filter(created_at__gte=dt_from)
        if date_to:
            dt_to = make_aware(datetime.combine(date_to, time.max))
            audit_logs = audit_logs.filter(created_at__lte=dt_to)
    else:
        audit_logs = AuditLog.objects.none()

    if filter_type in [None, "sync"]:
        sync_logs = SyncLog.objects.filter(event=event).select_related(
            "object_mapping__content_type"
        )
        if date_from:
            dt_from = make_aware(datetime.combine(date_from, time.min))
            sync_logs = sync_logs.filter(created_at__gte=dt_from)
        if date_to:
            dt_to = make_aware(datetime.combine(date_to, time.max))
            sync_logs = sync_logs.filter(created_at__lte=dt_to)
    else:
        sync_logs = SyncLog.objects.none()

    return ActivityLogSequence(audit_logs, sync_logs, search_query=search_query)
