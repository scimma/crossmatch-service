"""Index ``tns_objects`` on the lowercased name, concurrently (KTD13).

TNS-name lookup normalizes the input to a lowercased bare designation and
compares it with ``lower(name)``, so the IAU uppercase single-letter
designations (``2026A``) stay findable. The expression index backs that
comparison. It is built with ``CREATE INDEX CONCURRENTLY`` in its own
non-atomic migration so the refresh task's writes to ``tns_objects`` are never
blocked by an ``ACCESS EXCLUSIVE`` lock (pattern of 0007 and 0009).

If this migration is interrupted, Postgres leaves an INVALID index behind: drop
``core_tns_name_lower_idx`` and restart one ingest consumer to re-apply it.
"""

import django.db.models.functions.text
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('core', '0011_provenanceset_objectcrossmatchrecord'),
    ]

    operations = [
        AddIndexConcurrently(
            model_name='tnsobject',
            index=models.Index(
                django.db.models.functions.text.Lower('name'),
                name='core_tns_name_lower_idx',
            ),
        ),
    ]
