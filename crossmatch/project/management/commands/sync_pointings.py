import os
from django.core.management.base import BaseCommand
from heroic.schedule_sync import refresh_planned_pointings


class Command(BaseCommand):
    help = "Sync planned pointings from the HEROIC API"

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop',
            action='store_true',
            help='Run continuously (re-syncing on each interval)',
        )

    def handle(self, *args, **options):
        heroic_url = os.getenv('HEROIC_BASE_URL', '')
        self.stdout.write(self.style.SUCCESS(f'Syncing planned pointings from {heroic_url}...'))
        refresh_planned_pointings(heroic_url)
