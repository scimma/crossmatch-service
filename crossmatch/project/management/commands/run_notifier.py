from django.core.management.base import BaseCommand
from notifier.watch import watch_and_notify


class Command(BaseCommand):
    help = "Run the match notifier service"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Starting notifier...'))
        watch_and_notify()
