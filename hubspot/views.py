import datetime
import logging
import os
import secrets
import urllib.parse

import requests
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied
from django.forms import modelformset_factory
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, TemplateView, View
from django_scopes import scope
from eventyay.base.models import Event, Order, OrderPosition
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views import PaginationMixin

from .field_discovery import get_available_fields
from .forms import (
    BaseHubSpotFieldMappingFormSet,
    HubSpotFieldMappingForm,
    HubSpotLogFilterForm,
    ObjectTypeMappingFormSet,
)
from .models import (
    AuditAction,
    AuditLog,
    HubSpotFieldMapping,
    HubSpotOAuthToken,
    HubSpotProperty,
    HubSpotPropertySyncState,
    ObjectTypeMapping,
    SyncAction,
    SyncDirection,
    SyncLog,
    SyncStatus,
)
from .services import get_hubspot_properties, sync_hubspot_properties
from .utils import get_hubspot_activity_logs


def get_client_ip(request):
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class EventHubSpotSettingsView(EventPermissionRequiredMixin, TemplateView):
    """Landing page for HubSpot integration settings."""

    template_name = "hubspot/settings_landing.html"
    permission = "can_change_event_settings"

    def _get_formset(self, data=None):
        return ObjectTypeMappingFormSet(
            data,
            instance=self.request.event,
            queryset=ObjectTypeMapping.objects.filter(event=self.request.event),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            token = self.request.event.hubspotoauthtoken
            context["is_connected"] = True
            context["hub_name"] = token.hub_name
            context["hub_id"] = token.hub_id
        except HubSpotOAuthToken.DoesNotExist:
            context["is_connected"] = False
        if "formset" not in context:
            context["formset"] = self._get_formset()

        context["recent_activities"] = get_hubspot_activity_logs(self.request.event)[:5]
        return context

    def post(self, request, *args, **kwargs):
        formset = self._get_formset(request.POST)
        if formset.is_valid():
            formset.save()
            AuditLog.objects.create(
                organizer=request.event.organizer,
                event=request.event,
                action=AuditAction.MAPPING_UPDATED,
                ip_address=get_client_ip(request),
            )
            messages.success(request, _("Object mappings saved."))
            return redirect(request.path)
        return self.render_to_response(self.get_context_data(formset=formset))


class EventHubSpotConnectView(EventPermissionRequiredMixin, View):
    """Initiates HubSpot OAuth flow."""

    permission = "can_change_event_settings"

    def get(self, request, *args, **kwargs):
        state_token = secrets.token_urlsafe(16)
        request.session["hubspot_oauth_state"] = state_token
        # Pass the organizer and event slugs inside state parameter
        state = f"{state_token}:{request.event.organizer.slug}:{request.event.slug}"

        redirect_uri = os.environ.get("HUBSPOT_REDIRECT_URI", "")
        if not redirect_uri:
            redirect_uri = request.build_absolute_uri(
                reverse("plugins:hubspot:callback")
            )

        params = {
            "client_id": os.environ.get("HUBSPOT_CLIENT_ID", ""),
            "redirect_uri": redirect_uri,
            "scope": os.environ.get(
                "HUBSPOT_SCOPES",
                "oauth crm.objects.contacts.read crm.objects.contacts.write crm.objects.deals.read crm.objects.deals.write",
            ),
            "state": state,
        }
        url = "https://app.hubspot.com/oauth/authorize?" + urllib.parse.urlencode(
            params
        )
        return redirect(url)


class EventHubSpotCallbackView(View):
    """Handles callback from HubSpot OAuth."""

    def get(self, request, *args, **kwargs):
        error = request.GET.get("error")
        error_description = request.GET.get("error_description")
        state = request.GET.get("state", "")
        code = request.GET.get("code")

        # Unpack organizer and event slugs from the state parameter
        try:
            state_token, organizer_slug, event_slug = state.split(":", 2)
        except ValueError:
            raise PermissionDenied(_("Invalid state parameter."))

        saved_state = request.session.pop("hubspot_oauth_state", None)

        settings_url = reverse(
            "plugins:hubspot:hubspot",
            kwargs={
                "organizer": organizer_slug,
                "event": event_slug,
            },
        )

        if error:
            messages.error(
                request,
                _("HubSpot authorization failed: {}").format(
                    error_description or error
                ),
            )
            return redirect(settings_url)

        if not state_token or state_token != saved_state:
            messages.error(request, _("Invalid state parameter. Please try again."))
            return redirect(settings_url)

        # Retrieve the Event object
        try:
            event = Event.objects.select_related("organizer").get(
                slug=event_slug,
                organizer__slug=organizer_slug,
            )
        except Event.DoesNotExist:
            raise PermissionDenied(_("Event not found."))

        # Verify permissions manually
        if not request.user.is_authenticated:
            raise PermissionDenied()
        if not request.user.has_event_permission(
            event.organizer, event, "can_change_event_settings", request=request
        ):
            raise PermissionDenied(
                _("You do not have permission to view this content.")
            )

        redirect_uri = os.environ.get("HUBSPOT_REDIRECT_URI", "")
        if not redirect_uri:
            redirect_uri = request.build_absolute_uri(
                reverse("plugins:hubspot:callback")
            )

        response = requests.post(
            "https://api.hubapi.com/oauth/v1/token",
            data={
                "grant_type": "authorization_code",
                "client_id": os.environ.get("HUBSPOT_CLIENT_ID", ""),
                "client_secret": os.environ.get("HUBSPOT_CLIENT_SECRET", ""),
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15,
        )

        if not response.ok:
            messages.error(request, _("Failed to exchange token with HubSpot."))
            return redirect(settings_url)

        data = response.json()
        expires_in = data.get("expires_in")
        expires_at = (
            now() + datetime.timedelta(seconds=expires_in) if expires_in else None
        )

        # Fetch portal info from HubSpot token info endpoint
        hub_id = ""
        hub_name = ""
        access_token = data.get("access_token", "")
        if access_token:
            try:
                info_resp = requests.get(
                    f"https://api.hubapi.com/oauth/v1/access-tokens/{access_token}",
                    timeout=10,
                )
                if info_resp.ok:
                    info = info_resp.json()
                    hub_id = str(info.get("hub_id", ""))
                    hub_name = info.get("hub_domain", "")
            except requests.RequestException:
                pass

        with scope(organizer=event.organizer):
            HubSpotOAuthToken.objects.update_or_create(
                event=event,
                defaults={
                    "access_token": access_token,
                    "refresh_token": data.get("refresh_token"),
                    "token_type": data.get("token_type", "bearer"),
                    "expires_at": expires_at,
                    "hub_id": hub_id,
                    "hub_name": hub_name,
                    "scope": os.environ.get(
                        "HUBSPOT_SCOPES",
                        "oauth crm.objects.contacts.read crm.objects.contacts.write crm.objects.deals.read crm.objects.deals.write",
                    ),
                },
            )

            SyncLog.objects.create(
                event=event,
                action=SyncAction.CONNECT,
                direction=SyncDirection.PUSH,
                status=SyncStatus.SUCCESS,
                detail={"message": "Connected to HubSpot"},
            )

            AuditLog.objects.create(
                organizer=event.organizer,
                event=event,
                action=AuditAction.CONNECT,
                ip_address=get_client_ip(request),
            )

        messages.success(request, _("Successfully connected to HubSpot."))
        return redirect(settings_url)


class EventHubSpotDisconnectView(EventPermissionRequiredMixin, View):
    """Disconnects from HubSpot, revoking the token and clearing local credentials."""

    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        settings_url = reverse(
            "plugins:hubspot:hubspot",
            kwargs={
                "organizer": request.event.organizer.slug,
                "event": request.event.slug,
            },
        )

        try:
            token = HubSpotOAuthToken.objects.get(event=request.event)
        except HubSpotOAuthToken.DoesNotExist:
            messages.info(request, _("Not connected to HubSpot."))
            return redirect(settings_url)

        # Attempt to revoke at HubSpot
        try:
            # We use the refresh token to revoke, as per HubSpot docs.
            revoke_url = (
                f"https://api.hubapi.com/oauth/v1/refresh-tokens/{token.refresh_token}"
            )
            response = requests.delete(revoke_url, timeout=10)
            if not response.ok:
                logger = logging.getLogger(__name__)
                logger.warning(
                    f"Failed to revoke HubSpot token: {response.status_code} {response.text}"
                )
        except requests.RequestException as e:
            logger = logging.getLogger(__name__)
            logger.warning(f"Error reaching HubSpot revoke endpoint: {e}")

        # Always clear local credentials
        with scope(organizer=request.event.organizer):
            token.delete()
            SyncLog.objects.create(
                event=request.event,
                action=SyncAction.DISCONNECT,
                direction=SyncDirection.PUSH,
                status=SyncStatus.SUCCESS,
                detail={"message": "Disconnected from HubSpot"},
            )
            AuditLog.objects.create(
                organizer=request.event.organizer,
                event=request.event,
                action=AuditAction.DISCONNECT,
                ip_address=get_client_ip(request),
            )

        # Clear synced HubSpot properties
        HubSpotProperty.objects.filter(event=request.event).delete()
        HubSpotPropertySyncState.objects.filter(event=request.event).delete()

        messages.success(request, _("Successfully disconnected from HubSpot."))
        return redirect(settings_url)


class EventHubSpotFieldMappingView(EventPermissionRequiredMixin, TemplateView):
    """View to manage field mapping rows for a specific object mapping type."""

    template_name = "hubspot/field_mapping.html"
    permission = "can_change_event_settings"

    def _get_formset_kwargs(self, mapping_id, request):
        try:
            mapping = ObjectTypeMapping.objects.get(pk=mapping_id, event=request.event)
        except ObjectTypeMapping.DoesNotExist:
            raise PermissionDenied(_("Invalid object mapping."))

        if mapping.eventyay_object_type == "order":
            content_type = ContentType.objects.get_for_model(Order)
        elif mapping.eventyay_object_type == "order_position":
            content_type = ContentType.objects.get_for_model(OrderPosition)
        else:
            raise PermissionDenied(_("Unsupported eventyay object type."))

        hubspot_object_type = mapping.hubspot_object_type

        queryset = HubSpotFieldMapping.objects.filter(
            event=request.event,
            content_type=content_type,
            hubspot_object_type=hubspot_object_type,
        )

        FormSet = modelformset_factory(
            HubSpotFieldMapping,
            form=HubSpotFieldMappingForm,
            formset=BaseHubSpotFieldMappingFormSet,
            extra=1 if not queryset.exists() else 0,
            can_delete=True,
        )

        eventyay_fields = get_available_fields(
            mapping.eventyay_object_type, event=request.event
        )
        sync_error = None
        try:
            hubspot_properties = get_hubspot_properties(
                request.event, mapping.hubspot_object_type
            )
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(
                "Failed to load HubSpot properties for event %s: %s",
                request.event.slug,
                e,
            )
            sync_error = _(
                "Could not retrieve HubSpot properties. "
                "Please check your connection and try again."
            )
            hubspot_properties = list(
                HubSpotProperty.objects.filter(
                    event=request.event, object_type=mapping.hubspot_object_type
                ).values("key", "label", "data_type")
            )

        form_kwargs = {
            "eventyay_fields": eventyay_fields,
            "hubspot_properties": hubspot_properties,
        }

        return {
            "FormSet": FormSet,
            "queryset": queryset,
            "form_kwargs": form_kwargs,
            "content_type": content_type,
            "hubspot_object_type": hubspot_object_type,
            "mapping": mapping,
            "sync_error": sync_error,
        }

    def get(self, request, *args, **kwargs):
        if request.GET.get("force_sync") == "1":
            mapping_id = self.kwargs.get("mapping_id")
            try:
                mapping = ObjectTypeMapping.objects.get(
                    pk=mapping_id, event=request.event
                )
            except ObjectTypeMapping.DoesNotExist:
                raise PermissionDenied(_("Invalid object mapping."))

            try:
                sync_hubspot_properties(request.event, mapping.hubspot_object_type)
                messages.success(request, _("HubSpot properties synced successfully."))
            except Exception as e:
                logger = logging.getLogger(__name__)
                logger.error(
                    "Manual force_sync failed for event %s: %s", request.event.slug, e
                )
                messages.error(
                    request,
                    _(
                        "Could not sync HubSpot properties. "
                        "Please check your connection and try again."
                    ),
                )

            clean_url = reverse(
                "plugins:hubspot:mapping_fields",
                kwargs={
                    "organizer": request.event.organizer.slug,
                    "event": request.event.slug,
                    "mapping_id": mapping_id,
                },
            )
            return redirect(clean_url)

        return super().get(request, *args, **kwargs)

    def get_context_data(self, setup=None, **kwargs):
        context = super().get_context_data(**kwargs)
        mapping_id = self.kwargs.get("mapping_id")

        if setup is None:
            setup = self._get_formset_kwargs(mapping_id, self.request)

        context["content_type"] = setup["content_type"]
        context["hubspot_object_type"] = setup["hubspot_object_type"]
        context["mapping"] = setup["mapping"]
        context["sync_error"] = setup.get("sync_error")

        if "formset" not in context:
            context["formset"] = setup["FormSet"](
                queryset=setup["queryset"], form_kwargs=setup["form_kwargs"]
            )

        context["has_rows"] = setup["queryset"].exists()

        return context

    def post(self, request, *args, **kwargs):
        mapping_id = self.kwargs.get("mapping_id")
        setup = self._get_formset_kwargs(mapping_id, request)

        formset = setup["FormSet"](
            request.POST, queryset=setup["queryset"], form_kwargs=setup["form_kwargs"]
        )

        if formset.is_valid():
            instances = formset.save(commit=False)

            for instance in instances:
                instance.event = request.event
                instance.content_type = setup["content_type"]
                instance.hubspot_object_type = setup["hubspot_object_type"]
                instance.save()

            for obj in formset.deleted_objects:
                obj.delete()

            AuditLog.objects.create(
                organizer=request.event.organizer,
                event=request.event,
                action=AuditAction.FIELD_MAPPING_UPDATED,
                ip_address=get_client_ip(request),
            )

            messages.success(
                request, _("Field mapping configuration saved successfully.")
            )
            return redirect(
                reverse(
                    "plugins:hubspot:mapping_fields",
                    kwargs={
                        "organizer": request.event.organizer.slug,
                        "event": request.event.slug,
                        "mapping_id": mapping_id,
                    },
                )
            )
        else:
            return self.render_to_response(
                self.get_context_data(setup=setup, formset=formset)
            )


class EventHubSpotLogView(EventPermissionRequiredMixin, PaginationMixin, ListView):
    """Full activity log page for HubSpot integration."""

    template_name = "hubspot/logs.html"
    permission = "can_change_event_settings"
    context_object_name = "activities"

    def get_queryset(self):
        form = HubSpotLogFilterForm(self.request.GET)
        filter_type = None
        date_from = None
        date_to = None
        search_query = None

        if form.is_valid():
            filter_type = form.cleaned_data.get("type")
            date_from = form.cleaned_data.get("date_from")
            date_to = form.cleaned_data.get("date_until")
            search_query = form.cleaned_data.get("query")

        if filter_type not in ["sync", "settings"]:
            filter_type = None

        return get_hubspot_activity_logs(
            self.request.event,
            filter_type=filter_type,
            date_from=date_from,
            date_to=date_to,
            search_query=search_query,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = HubSpotLogFilterForm(self.request.GET)
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "delete":
            if request.POST.get("select_all_pages") == "1":
                qs = self.get_queryset()
                if qs.search_query:
                    # If there's a search query, we must iterate since filtering happens in Python
                    audit_ids = []
                    sync_ids = []
                    for item in qs:
                        if item["id"].startswith("audit_"):
                            audit_ids.append(int(item["id"].split("_")[1]))
                        elif item["id"].startswith("sync_"):
                            sync_ids.append(int(item["id"].split("_")[1]))
                    if audit_ids:
                        AuditLog.objects.filter(
                            event=request.event, id__in=audit_ids
                        ).delete()
                    if sync_ids:
                        SyncLog.objects.filter(
                            event=request.event, id__in=sync_ids
                        ).delete()
                else:
                    # If no search query, we can directly delete the querysets
                    qs.audit_logs.delete()
                    qs.sync_logs.delete()
            else:
                log_ids = request.POST.getlist("log_id")
                audit_ids = []
                sync_ids = []
                for log_id in log_ids:
                    if log_id.startswith("audit_"):
                        try:
                            audit_ids.append(int(log_id.split("_")[1]))
                        except (ValueError, IndexError):
                            pass
                    elif log_id.startswith("sync_"):
                        try:
                            sync_ids.append(int(log_id.split("_")[1]))
                        except (ValueError, IndexError):
                            pass

                if audit_ids:
                    AuditLog.objects.filter(
                        event=request.event, id__in=audit_ids
                    ).delete()
                if sync_ids:
                    SyncLog.objects.filter(
                        event=request.event, id__in=sync_ids
                    ).delete()

            messages.success(request, _("Selected logs have been deleted."))
            return redirect(
                request.path_info + "?" + request.META.get("QUERY_STRING", "")
            )

        return self.get(request, *args, **kwargs)
