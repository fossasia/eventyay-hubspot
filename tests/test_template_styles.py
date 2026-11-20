from pathlib import Path
from unittest.mock import patch

import pytest
from django.urls import reverse
from django_scopes import scope

from hubspot.models import AuditAction, AuditLog, ObjectTypeMapping, OrganizerDefaultObjectTypeMapping


@pytest.mark.django_db
@pytest.mark.parametrize("page", ["mapping", "default_mapping", "organizer", "logs", "sync_problems"])
def test_plugin_panels_load_external_styles(logged_in_organizer_client, organizer, event, settings, page):
    settings.SITE_URL = "https://testserver"
    kwargs = {"organizer": organizer.slug, "event": event.slug}
    with scope(organizer=organizer):
        if page == "mapping":
            mapping = ObjectTypeMapping.objects.create(
                event=event, eventyay_object_type="order", hubspot_object_type="deals"
            )
            kwargs["mapping_id"] = mapping.pk
            route = "mapping_fields"
        elif page == "default_mapping":
            mapping = OrganizerDefaultObjectTypeMapping.objects.create(
                organizer=organizer, eventyay_object_type="order", hubspot_object_type="deals"
            )
            kwargs = {"organizer": organizer.slug, "mapping_id": mapping.pk}
            route = "org_default_mapping_fields"
        elif page == "organizer":
            kwargs = {"organizer": organizer.slug}
            route = "org_hubspot"
        else:
            route = page
            if page == "logs":
                for _ in range(30):
                    AuditLog.objects.create(organizer=organizer, event=event, action=AuditAction.MAPPING_UPDATED)
    with patch("hubspot.views.get_hubspot_properties", return_value=[]):
        response = logged_in_organizer_client.get(reverse(f"plugins:hubspot:{route}", kwargs=kwargs))
    assert response.status_code == 200
    content = response.content.decode()
    assert 'href="/static/hubspot/css/common.css"' in content
    if page == "default_mapping":
        assert 'href="/static/hubspot/css/field_mapping.css"' in content
    if page == "logs":
        assert 'class="alert alert-info text-center hubspot-log-selection"' in content


def test_plugin_templates_and_generated_markup_have_no_inline_styles():
    plugin = Path(__file__).resolve().parents[1] / "hubspot"
    for path in (plugin / "templates").rglob("*.html"):
        assert "style=" not in path.read_text(), path
    for path in (plugin / "static/hubspot/js").glob("*.js"):
        assert "style=" not in path.read_text(), path
    for path in (plugin / "static/hubspot/css").glob("*.css"):
        assert "!important" not in path.read_text(), path
