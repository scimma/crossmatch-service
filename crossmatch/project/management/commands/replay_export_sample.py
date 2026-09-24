"""Export a coverage sample of historical alerts for crossmatch replay.

Runs on PROD, read-only. The written file is copied to DEV (``kubectl cp``) and
replayed there with ``replay_run``. See docs/runbooks/crossmatch-replay.md.
"""

from django.core.management.base import BaseCommand

from replay.sample import read_only_transaction, select_sample, write_sample


class Command(BaseCommand):
    help = (
        "Export a read-only coverage sample of historical alerts for crossmatch replay"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", required=True, help="Path to write the sample JSON file"
        )
        parser.add_argument(
            "--per-category",
            type=int,
            default=250,
            help="Maximum alerts per coverage category (default 250)",
        )
        parser.add_argument(
            "--seed",
            default="replay",
            help="Seed for the deterministic selection order",
        )

    def handle(self, *args, **options):
        with read_only_transaction():
            sample = select_sample(options["per_category"], options["seed"])
        write_sample(sample, options["output"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(sample['alerts'])} alerts to {options['output']}"
            )
        )
        for name, count in sample["categories"].items():
            self.stdout.write(f"  {name}: {count}")
        if sample["unfilled_categories"]:
            self.stdout.write(
                self.style.WARNING(
                    "Unfilled categories: " + ", ".join(sample["unfilled_categories"])
                )
            )
