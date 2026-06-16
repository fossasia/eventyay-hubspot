import datetime
import os
import secrets
import urllib.parse

import requests
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View
from django_scopes import scope
from eventyay.base.models import Event
from eventyay.control.permissions import EventPermissionRequiredMixin

from .models import HubSpotOAuthToken, SyncLog, SyncAction, SyncDirection, SyncStatus

# Environment variables are loaded dynamically in the views


class EventHubSpotSettingsView(EventPermissionRequiredMixin, TemplateView):
    """Landing page for HubSpot integration settings."""

    template_name = "hubspot/settings_landing.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_connected"] = hasattr(self.request.event, "hubspotoauthtoken")
        return context


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

        if not code:
            messages.error(request, _("No authorization code provided."))
            return redirect(settings_url)

        redirect_uri = os.environ.get("HUBSPOT_REDIRECT_URI", "")
        if not redirect_uri:
            redirect_uri = request.build_absolute_uri(
                reverse("plugins:hubspot:callback")
            )

        try:
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
        except requests.RequestException:
            messages.error(
                request, _("Network error while trying to connect to HubSpot.")
            )
            return redirect(settings_url)

        if not response.ok:
            messages.error(request, _("Failed to exchange token with HubSpot."))
            return redirect(settings_url)

        data = response.json()
        expires_in = data.get("expires_in")
        expires_at = (
            now() + datetime.timedelta(seconds=expires_in) if expires_in else None
        )

        with scope(organizer=event.organizer):
            HubSpotOAuthToken.objects.update_or_create(
                event=event,
                defaults={
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "token_type": data.get("token_type", "bearer"),
                    "expires_at": expires_at,
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

        messages.success(request, _("Successfully connected to HubSpot."))
        return redirect(settings_url)
