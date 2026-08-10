"""Backfill ``Alert.notified_at`` for existing terminal alerts.

Brings pre-existing terminal rows under payload retention: a NULL ``notified_at``
is never caught by the retention sweep, so every already-terminal alert needs one.

Terminal alerts are:
  * ``NOTIFIED`` alerts, and
  * ``MATCHED`` alerts with no unsent notification (no-match alerts have zero
    notifications; others may have all notifications already SENT).
``MATCHED`` alerts that still have a PENDING/FAILED notification are in flight and
are left NULL, so their payloads stay until they actually reach a terminal state.

``notified_at`` is set to ``ingest_time`` (a defensible floor — the alert became
terminal shortly after ingest, and for existing rows the grace math is dominated by
their age). A production rollout on a live, actively-ingesting table should still run
this in a quiet window — see the retention plan (KTD2).

Uses a **monotonic pk-cursor keyset**, not a streaming ``.iterator()`` and not a
re-filtered ``notified_at IS NULL`` slice. This migration is ``atomic=False`` so it
commits each chunk incrementally on the live multi-million-row table, but a COMMIT
closes a WITHOUT-HOLD server-side cursor, which would break a long-lived
``.iterator()`` mid-run (``InvalidCursorName``) and abort ``migrate``. A prior version
re-selected ``notified_at IS NULL`` rows each pass; because updated rows drop out of
that predicate but still sit physically in the table, every batch rescanned the
growing already-updated (and never-updated in-flight) prefix, degrading to O(n^2) on a
large table. This version instead advances a pk cursor (``pk > last_pk``, ordered by
pk): each row is visited exactly once via the pk index, and only the terminal subset
of each window is updated, so the whole backfill is O(n). The per-window
``notified_at__isnull=True`` guard keeps a resumed run (which restarts from the first
pk) from overwriting anchors an earlier pass already set.
"""

from django.db import migrations
from django.db.models import Exists, F, OuterRef, Q

BATCH_SIZE = 5000


def backfill_notified_at(apps, schema_editor):
    Alert = apps.get_model('core', 'Alert')
    Notification = apps.get_model('core', 'Notification')

    unsent = Notification.objects.filter(
        alert_id=OuterRef('lsst_diaObject_diaObjectId')
    ).exclude(state='sent')

    terminal = Q(status='NOTIFIED') | (Q(status='MATCHED') & ~Exists(unsent))

    last_pk = None
    while True:
        window = Alert.objects.all()
        if last_pk is not None:
            window = window.filter(pk__gt=last_pk)
        pks = list(window.order_by('pk').values_list('pk', flat=True)[:BATCH_SIZE])
        if not pks:
            break
        last_pk = pks[-1]
        Alert.objects.filter(pk__in=pks, notified_at__isnull=True).filter(
            terminal
        ).update(notified_at=F('ingest_time'))


def noop_reverse(apps, schema_editor):
    """Reverse is a no-op: notified_at is a derived anchor, safe to leave populated."""


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('core', '0007_notified_at_index_concurrent'),
    ]

    operations = [
        migrations.RunPython(backfill_notified_at, noop_reverse),
    ]
