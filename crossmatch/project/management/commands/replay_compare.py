"""Compare two crossmatch replay snapshots and write a Markdown report.

Paste the report into the upgrade pull request and explain every difference
group there. See docs/runbooks/crossmatch-replay.md.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from replay.compare import (
    SnapshotFormatError,
    compare_snapshots,
    load_snapshot,
    render_report,
)


class Command(BaseCommand):
    help = "Compare a baseline and a candidate replay snapshot into a Markdown report"

    def add_arguments(self, parser):
        parser.add_argument("baseline", help="Snapshot taken before the stack change")
        parser.add_argument("candidate", help="Snapshot taken after the stack change")
        parser.add_argument(
            "--output", required=True, help="Path to write the Markdown report"
        )

    def handle(self, *args, **options):
        try:
            baseline = load_snapshot(options["baseline"])
            candidate = load_snapshot(options["candidate"])
        except (OSError, ValueError, SnapshotFormatError) as exc:
            raise CommandError(f"Cannot load snapshot: {exc}") from exc

        result = compare_snapshots(baseline, candidate)
        Path(options["output"]).write_text(
            render_report(result, options["baseline"], options["candidate"])
        )
        self.stdout.write(
            f"{result.difference_count} output differences in {len(result.groups)} "
            f"groups; {len(result.context_differences)} run-context differences; "
            f"report written to {options['output']}"
        )
        if result.flags:
            self.stdout.write(
                self.style.WARNING(
                    f"{len(result.flags)} comparability problem(s) -- see the report."
                )
            )
