import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        # ── alerts ──────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='Alert',
            fields=[
                ('uuid', models.UUIDField(
                    default=uuid.uuid4,
                    unique=True,
                    db_index=True,
                    primary_key=True,
                    serialize=False,
                )),
                ('lsst_diaObject_diaObjectId', models.BigIntegerField(unique=True, db_column='lsst_diaobject_diaobjectid')),
                ('lsst_diaSource_diaSourceId', models.BigIntegerField(null=True, db_column='lsst_diasource_diasourceid')),
                ('ra_deg', models.FloatField()),
                ('dec_deg', models.FloatField()),
                ('event_time', models.DateTimeField()),
                ('ingest_time', models.DateTimeField(auto_now_add=True)),
                ('schema_version', models.IntegerField(default=1)),
                ('payload', models.JSONField()),
                ('status', models.TextField(
                    choices=[
                        ('INGESTED', 'ingested'),
                        ('QUEUED', 'queued'),
                        ('MATCHED', 'matched'),
                        ('NOTIFIED', 'notified'),
                    ],
                    default='INGESTED',
                )),
            ],
        ),
        migrations.AddIndex(
            model_name='alert',
            index=models.Index(fields=['status'], name='core_alert_status_idx'),
        ),

        # ── alert_deliveries ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name='AlertDelivery',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaobject_diaobjectid',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('broker', models.TextField()),
                ('ingest_time', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'alert_deliveries',
            },
        ),
        migrations.AddIndex(
            model_name='alertdelivery',
            index=models.Index(fields=['alert'], name='core_ad_alert_idx'),
        ),
        migrations.AddConstraint(
            model_name='alertdelivery',
            constraint=models.UniqueConstraint(
                fields=['alert', 'broker'],
                name='unique_alert_delivery',
            ),
        ),

        # ── planned_pointings ────────────────────────────────────────────────────
        migrations.CreateModel(
            name='PlannedPointing',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('source', models.TextField(default='heroic')),
                ('heroic_pointing_id', models.TextField(null=True)),
                ('obs_id', models.TextField(null=True)),
                ('target_name', models.TextField(null=True)),
                ('planned', models.BooleanField(default=True)),
                ('s_ra_deg', models.FloatField()),
                ('s_dec_deg', models.FloatField()),
                ('radius_deg', models.FloatField()),
                ('field_geojson', models.JSONField(null=True)),
                ('t_min_mjd', models.FloatField()),
                ('t_max_mjd', models.FloatField()),
                ('t_planning_mjd', models.FloatField(null=True)),
                ('instrument_name', models.TextField(null=True)),
                ('ingest_time', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='plannedpointing',
            index=models.Index(fields=['ingest_time'], name='core_pp_ingest_time_idx'),
        ),
        migrations.AddIndex(
            model_name='plannedpointing',
            index=models.Index(fields=['t_min_mjd', 't_max_mjd'], name='core_pp_time_window_idx'),
        ),

        # ── catalog_matches ──────────────────────────────────────────────────────
        migrations.CreateModel(
            name='CatalogMatch',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaobject_diaobjectid',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('catalog_name', models.TextField()),
                ('catalog_source_id', models.TextField()),
                ('match_distance_arcsec', models.FloatField()),
                ('match_score', models.FloatField(null=True)),
                ('source_ra_deg', models.FloatField(null=True)),
                ('source_dec_deg', models.FloatField(null=True)),
                ('catalog_payload', models.JSONField(null=True)),
                ('match_version', models.IntegerField(default=1)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'catalog_matches',
            },
        ),
        migrations.AddIndex(
            model_name='catalogmatch',
            index=models.Index(fields=['alert'], name='core_cm_alert_idx'),
        ),
        migrations.AddIndex(
            model_name='catalogmatch',
            index=models.Index(fields=['catalog_name'], name='core_cm_catalog_name_idx'),
        ),
        migrations.AddIndex(
            model_name='catalogmatch',
            index=models.Index(fields=['catalog_source_id'], name='core_cm_catalog_source_id_idx'),
        ),
        migrations.AddConstraint(
            model_name='catalogmatch',
            constraint=models.UniqueConstraint(
                fields=['alert', 'catalog_name', 'catalog_source_id', 'match_version'],
                name='unique_catalog_match',
            ),
        ),

        # ── crossmatch_runs ──────────────────────────────────────────────────────
        migrations.CreateModel(
            name='CrossmatchRun',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaobject_diaobjectid',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('match_version', models.IntegerField(default=1)),
                ('celery_task_id', models.TextField(null=True)),
                ('state', models.TextField(
                    choices=[
                        ('queued', 'queued'),
                        ('running', 'running'),
                        ('succeeded', 'succeeded'),
                        ('failed', 'failed'),
                    ],
                    default='queued',
                )),
                ('attempts', models.IntegerField(default=0)),
                ('started_at', models.DateTimeField(null=True)),
                ('finished_at', models.DateTimeField(null=True)),
                ('last_error', models.TextField(null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='crossmatchrun',
            index=models.Index(fields=['alert'], name='core_cmr_alert_idx'),
        ),
        migrations.AddIndex(
            model_name='crossmatchrun',
            index=models.Index(fields=['state'], name='core_cmr_state_idx'),
        ),

        # ── notifications ────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaobject_diaobjectid',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('catalog_match', models.ForeignKey(
                    db_column='catalog_match_id',
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='core.catalogmatch',
                )),
                ('destination', models.TextField()),
                ('payload', models.JSONField()),
                ('state', models.TextField(
                    choices=[
                        ('pending', 'pending'),
                        ('sent', 'sent'),
                        ('failed', 'failed'),
                    ],
                    default='pending',
                )),
                ('attempts', models.IntegerField(default=0)),
                ('last_error', models.TextField(null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('sent_at', models.DateTimeField(null=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['state'], name='core_notif_state_idx'),
        ),
        migrations.AddIndex(
            model_name='notification',
            index=models.Index(fields=['alert'], name='core_notif_alert_idx'),
        ),
    ]
