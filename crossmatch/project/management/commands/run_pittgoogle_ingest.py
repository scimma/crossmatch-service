from django.core.management.base import BaseCommand
from brokers.pittgoogle.consumer import consume_alerts


class Command(BaseCommand):
    help = "Run Pitt-Google alert ingest"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting Pitt-Google alert ingest...'))
        consume_alerts()
