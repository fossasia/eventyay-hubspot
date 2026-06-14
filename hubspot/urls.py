from django.urls import path
from hubspot.views import (
    EventHubSpotCallbackView,
    EventHubSpotConnectView,
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
        "control/hubspot/callback/",
        EventHubSpotCallbackView.as_view(),
        name="callback",
    ),
]
