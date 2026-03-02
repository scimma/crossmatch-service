import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        # Add index on Alert.status
        migrations.AddIndex(
            model_name='alert',
            index=models.Index(fields=['status'], name='core_alert_status_idx'),
        ),

        # PlannedPointing
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

        # GaiaMatch
        migrations.CreateModel(
            name='GaiaMatch',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaObject_diaObjectId',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('gaia_source_id', models.BigIntegerField()),
                ('match_distance_arcsec', models.FloatField()),
                ('match_score', models.FloatField(null=True)),
                ('gaia_ra_deg', models.FloatField(null=True)),
                ('gaia_dec_deg', models.FloatField(null=True)),
                ('gaia_payload', models.JSONField(null=True)),
                ('match_version', models.IntegerField(default=1)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name='gaiamatch',
            index=models.Index(fields=['alert'], name='core_gm_alert_idx'),
        ),
        migrations.AddIndex(
            model_name='gaiamatch',
            index=models.Index(fields=['gaia_source_id'], name='core_gm_gaia_source_id_idx'),
        ),
        migrations.AddConstraint(
            model_name='gaiamatch',
            constraint=models.UniqueConstraint(
                fields=['alert', 'gaia_source_id', 'match_version'],
                name='unique_gaia_match',
            ),
        ),

        # CrossmatchRun
        migrations.CreateModel(
            name='CrossmatchRun',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaObject_diaObjectId',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('match_version', models.IntegerField(default=1)),
                ('celery_task_id', models.TextField(null=True)),
                ('state', models.TextField(
                    choices=[('queued', 'queued'), ('running', 'running'),
                              ('succeeded', 'succeeded'), ('failed', 'failed')],
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

        # Notification
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('alert', models.ForeignKey(
                    db_column='lsst_diaObject_diaObjectId',
                    on_delete=django.db.models.deletion.CASCADE,
                    to='core.alert',
                    to_field='lsst_diaObject_diaObjectId',
                )),
                ('gaia_match', models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='core.gaiamatch',
                )),
                ('destination', models.TextField()),
                ('payload', models.JSONField()),
                ('state', models.TextField(
                    choices=[('pending', 'pending'), ('sent', 'sent'), ('failed', 'failed')],
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
