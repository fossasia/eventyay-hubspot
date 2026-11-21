from datetime import timedelta

import pytest
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from eventyay.base.models import Event, Organizer

from hubspot.models import AuditAction, AuditLog, SyncAction, SyncDirection, SyncLog, SyncStatus
from hubspot.signals import clear_audit_logs


@pytest.mark.django_db
def test_periodic_cleanup_removes_expired_logs_across_organizers(event, monkeypatch):
    run_at = now()
    cutoff = run_at - timedelta(days=180)
    monkeypatch.setattr("hubspot.signals.now", lambda: run_at)
    second_organizer = Organizer.objects.create(name="Second Organizer", slug="second-org")
    second_event = Event.objects.create(
        organizer=second_organizer,
        name="Second Event",
        slug="second-event",
        date_from=run_at,
        date_to=run_at + timedelta(days=1),
        currency="USD",
        live=True,
    )

    def create_log_pair(for_event, created_at):
        with scope(organizer=for_event.organizer):
            audit = AuditLog.objects.create(organizer=for_event.organizer, action=AuditAction.CONNECT)
            sync = SyncLog.objects.create(
                event=for_event, action=SyncAction.CREATE, direction=SyncDirection.PUSH, status=SyncStatus.SUCCESS
            )
            AuditLog.objects.filter(pk=audit.pk).update(created_at=created_at)
            SyncLog.objects.filter(pk=sync.pk).update(created_at=created_at)
        return audit.pk, sync.pk

    for expired_event in (event, second_event):
        create_log_pair(expired_event, cutoff - timedelta(seconds=1))
    cutoff_audit, cutoff_sync = create_log_pair(event, cutoff)
    recent_audit, recent_sync = create_log_pair(second_event, cutoff + timedelta(seconds=1))

    clear_audit_logs(sender=None)

    with scopes_disabled():
        assert set(AuditLog.objects.values_list("pk", flat=True)) == {cutoff_audit, recent_audit}
        assert set(SyncLog.objects.values_list("pk", flat=True)) == {cutoff_sync, recent_sync}
