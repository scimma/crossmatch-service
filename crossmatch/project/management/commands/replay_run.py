"""Replay a sample through the crossmatch compute step and write a snapshot.

DEV only. Runs the same Dask version-alignment check the Celery worker runs at
startup, then crossmatches the sample on the cluster without writing to the
database or publishing anything. See docs/runbooks/crossmatch-replay.md.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

import replay.snapshot as snapshot
from core.dask import DaskAlignmentError, close_client_quietly
from replay.sample import SampleFormatError, load_sample

_DEFAULT_APP_VERSION = "0.0.0"


class Command(BaseCommand):
    help = (
        "Replay a crossmatch sample on the Dask cluster and write a snapshot (DEV only)"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample",
            required=True,
            help="Sample JSON written by replay_export_sample",
        )
        parser.add_argument(
            "--output", required=True, help="Path to write the snapshot JSON file"
        )
        parser.add_argument(
            "--image-tag", help="Deployed image tag to record (default: APP_VERSION)"
        )

    def handle(self, *args, **options):
        if not settings.CROSSMATCH_REPLAY_ENABLED:
            raise CommandError(
                "Replay is disabled: set CROSSMATCH_REPLAY_ENABLED=true (DEV only; "
                "never run a replay against the PROD Dask cluster)."
            )
        address = settings.DASK_SCHEDULER_ADDRESS
        if not address:
            raise CommandError(
                "DASK_SCHEDULER_ADDRESS is not set; a replay must run on the Dask "
                "cluster, not the local scheduler."
            )
        image_tag = options.get("image_tag") or settings.APP_VERSION
        if not image_tag or image_tag == _DEFAULT_APP_VERSION:
            raise CommandError(
                "Cannot determine the deployed image tag (APP_VERSION is unset in "
                "this pod); pass --image-tag."
            )
        try:
            sample = load_sample(options["sample"])
        except (OSError, ValueError, SampleFormatError) as exc:
            raise CommandError(f"Cannot load sample: {exc}") from exc

        try:
            client, drifted = snapshot.check_cluster_alignment(
                address, settings.DASK_VERSION_CHECK_TIMEOUT_SECONDS
            )
        except DaskAlignmentError as exc:
            raise CommandError(f"Dask cluster not ready: {exc.reason}") from exc
        try:
            if drifted:
                packages = ", ".join(d["package"] for d in drifted)
                raise CommandError(
                    f"Dask version drift between client and cluster ({packages}): "
                    f"{drifted}"
                )
            versions = snapshot.cluster_package_versions(client)
            data = snapshot.build_snapshot(sample, image_tag, versions)
        finally:
            close_client_quietly(client)

        snapshot.write_snapshot(data, options["output"])
        ctx = data["run_context"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(data['matches'])} matches for {ctx['sample']['alert_count']} "
                f"alerts to {options['output']}"
            )
        )
        for name, outcome in ctx["catalog_outcomes"].items():
            self.stdout.write(f"  {name}: {outcome}")
        if not ctx["tns"]["current"]:
            self.stdout.write(
                self.style.WARNING(
                    "TNS snapshot not current: TNS enrichment was not exercised."
                )
            )
