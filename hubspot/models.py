from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _
from django_scopes import ScopedManager

from .utils import decrypt, encrypt


class TokenType(models.TextChoices):
    BEARER = "bearer"


class SyncAction(models.TextChoices):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    TOKEN_REFRESH = "token_refresh"
    REFRESH_FAILED = "refresh_failed"


class SyncDirection(models.TextChoices):
    PUSH = "push"
    PULL = "pull"


class SyncStatus(models.TextChoices):
    SUCCESS = "success"
    FAILED = "failed"
    PENDING = "pending"


class SyncMode(models.TextChoices):
    IDENTIFIER = "identifier", _("Identifier")
    OVERWRITE = "overwrite", _("Overwrite")
    FILL_IF_NEW = "fill_if_new", _("Fill if new")
    FILL_IF_EMPTY = "fill_if_empty", _("Fill if empty")


class HubSpotOAuthToken(models.Model):
    event = models.OneToOneField("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    _access_token = models.TextField(db_column="access_token")
    _refresh_token = models.TextField(db_column="refresh_token")
    token_type = models.CharField(
        max_length=50, choices=TokenType.choices, default=TokenType.BEARER
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    hub_id = models.CharField(max_length=100, blank=True)
    hub_name = models.CharField(max_length=200, blank=True)
    scope = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def access_token(self):
        return decrypt(self._access_token)

    @access_token.setter
    def access_token(self, value):
        self._access_token = encrypt(value)

    @property
    def refresh_token(self):
        return decrypt(self._refresh_token)

    @refresh_token.setter
    def refresh_token(self, value):
        self._refresh_token = encrypt(value)

    class Meta:
        verbose_name = "HubSpot OAuth Token"
        verbose_name_plural = "HubSpot OAuth Tokens"

    def __str__(self):
        return f"OAuth Token for {self.event.name}"


class HubSpotEventSettings(models.Model):
    event = models.OneToOneField("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    sync_enabled = models.BooleanField(default=False)
    sync_contacts = models.BooleanField(default=True)
    sync_deals = models.BooleanField(default=True)
    deal_pipeline = models.CharField(max_length=200, blank=True)
    deal_stage = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "HubSpot Event Settings"
        verbose_name_plural = "HubSpot Event Settings"

    def __str__(self):
        return f"HubSpot Settings for {self.event.name}"


class HubSpotObjectMapping(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.BigIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    hubspot_object_type = models.CharField(max_length=50)
    hubspot_object_id = models.CharField(max_length=190)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (
            "event",
            "content_type",
            "object_id",
            "hubspot_object_type",
        )
        verbose_name = "HubSpot Object Mapping"
        verbose_name_plural = "HubSpot Object Mappings"

    def __str__(self):
        return f"{self.content_type.model} ({self.object_id}) -> {self.hubspot_object_type} ({self.hubspot_object_id})"


class HubSpotFieldMapping(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    eventyay_field = models.CharField(max_length=190)
    hubspot_object_type = models.CharField(max_length=50)
    hubspot_property = models.CharField(max_length=190)
    sync_mode = models.CharField(
        max_length=20, choices=SyncMode.choices, default=SyncMode.OVERWRITE
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (
            "event",
            "content_type",
            "eventyay_field",
            "hubspot_object_type",
        )
        verbose_name = "HubSpot Field Mapping"
        verbose_name_plural = "HubSpot Field Mappings"

    def __str__(self):
        return f"{self.content_type.model}.{self.eventyay_field} -> {self.hubspot_object_type}.{self.hubspot_property}"


class SyncLog(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    object_mapping = models.ForeignKey(
        HubSpotObjectMapping, null=True, blank=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=20, choices=SyncAction.choices)
    direction = models.CharField(max_length=10, choices=SyncDirection.choices)
    status = models.CharField(max_length=10, choices=SyncStatus.choices)
    # Expected shape: {"error": str, "request": dict, "response": dict}
    detail = JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Sync Log"
        verbose_name_plural = "Sync Logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} ({self.direction}) - {self.status} at {self.created_at}"


class AuditAction(models.TextChoices):
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    TOKEN_REFRESH = "token_refresh"
    REFRESH_FAILED = "refresh_failed"
    MAPPING_UPDATED = "mapping_updated"
    FIELD_MAPPING_UPDATED = "field_map_updated"


class AuditLog(models.Model):
    organizer = models.ForeignKey("base.Organizer", on_delete=models.CASCADE)
    event = models.ForeignKey(
        "base.Event", null=True, blank=True, on_delete=models.SET_NULL
    )
    action = models.CharField(max_length=20, choices=AuditAction.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    objects = ScopedManager(organizer="organizer")

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} by {self.organizer} at {self.created_at}"


class EventyayObjectType(models.TextChoices):
    ORDER = "order", _("Order")
    ORDER_POSITION = "order_position", _("Order position")


class HubSpotObjectType(models.TextChoices):
    CONTACTS = "contacts", _("Contacts")
    DEALS = "deals", _("Deals")


class ObjectTypeMapping(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    eventyay_object_type = models.CharField(
        max_length=50, choices=EventyayObjectType.choices
    )
    hubspot_object_type = models.CharField(
        max_length=50, choices=HubSpotObjectType.choices
    )
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("event", "eventyay_object_type", "hubspot_object_type")
        verbose_name = _("Object Type Mapping")
        verbose_name_plural = _("Object Type Mappings")
        ordering = ["position", "pk"]

    def __str__(self):
        return (
            f"{self.get_eventyay_object_type_display()}"
            f" \u2192 {self.get_hubspot_object_type_display()}"
        )


class HubSpotProperty(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    object_type = models.CharField(max_length=50)  # "contact" or "deal"
    key = models.CharField(max_length=190)  # HubSpot internal name
    label = models.CharField(max_length=500)  # Human-readable label
    data_type = models.CharField(max_length=20)  # "text", "number", "date", "yes/no"
    sync_batch = models.UUIDField()  # Groups rows from the same sync run
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("event", "object_type", "key")
        verbose_name = "HubSpot Property"
        verbose_name_plural = "HubSpot Properties"

    def __str__(self):
        return f"{self.object_type} property '{self.key}' for {self.event.name}"


class HubSpotPropertySyncState(models.Model):
    event = models.ForeignKey("base.Event", on_delete=models.CASCADE)
    objects = ScopedManager(organizer="event__organizer")
    object_type = models.CharField(max_length=50)
    sync_batch = models.UUIDField()
    next_cursor = models.CharField(max_length=500, blank=True, default="")
    is_complete = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("event", "object_type")
        verbose_name = "HubSpot Property Sync State"

    def __str__(self):
        return f"Sync state for {self.event.name} ({self.object_type})"
