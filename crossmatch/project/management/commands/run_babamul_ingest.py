from django.core.management.base import BaseCommand
from brokers.babamul.consumer import consume_alerts
from core.metrics import start_metrics_server


class Command(BaseCommand):
    help = "Run Babamul alert ingest"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting Babamul alert ingest...'))
        start_metrics_server()
        consume_alerts()
