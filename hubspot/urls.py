from django.urls import path

from hubspot.views import (
    EventHubSpotCallbackView,
    EventHubSpotConnectView,
    EventHubSpotDisconnectView,
    EventHubSpotFieldMappingView,
    EventHubSpotLogView,
    EventHubSpotSettingsView,
)

urlpatterns = [
    path(
        "control/event/<orgslug:organizer>/<slug:event>/hubspot/",
        EventHubSpotSettingsView.as_view(),
        name="hubspot",
    ),
    path(
        "control/event/<orgslug:organizer>/<slug:event>/hubspot/connect/",
        EventHubSpotConnectView.as_view(),
        name="connect",
    ),
    path(
        "control/event/<orgslug:organizer>/<slug:event>/hubspot/disconnect/",
        EventHubSpotDisconnectView.as_view(),
        name="disconnect",
    ),
    path(
        "control/hubspot/callback/",
        EventHubSpotCallbackView.as_view(),
        name="callback",
    ),
    path(
        "control/event/<orgslug:organizer>/<slug:event>/hubspot/mapping/<int:mapping_id>/fields/",
        EventHubSpotFieldMappingView.as_view(),
        name="mapping_fields",
    ),
    path(
        "control/event/<orgslug:organizer>/<slug:event>/hubspot/logs/",
        EventHubSpotLogView.as_view(),
        name="logs",
    ),
]
