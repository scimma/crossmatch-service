from django.core.management.base import BaseCommand
from antares.consumer import consume_alerts


class Command(BaseCommand):
    help = "Run ANTARES alert ingest"

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS('Processing alerts...')
        )
        consume_alerts()
