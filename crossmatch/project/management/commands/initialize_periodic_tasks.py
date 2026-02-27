from django.core.management.base import BaseCommand
from django_celery_beat.models import IntervalSchedule
from django_celery_beat.models import PeriodicTask
from tasks.tasks import periodic_tasks


class Command(BaseCommand):
    help = "Initialize the periodic tasks"

    def handle(self, *args, **options):
        for periodic_task in periodic_tasks:
            interval, created = IntervalSchedule.objects.get_or_create(
                every=periodic_task.task_frequency_seconds, period=IntervalSchedule.SECONDS
            )

            PeriodicTask.objects.update_or_create(
                name=periodic_task.task_name,
                defaults={
                    'interval': interval,
                    'task': f'tasks.{periodic_task.task_handle}',
                    'enabled': periodic_task.task_initially_enabled,
                }
            )

            self.stdout.write(
                self.style.SUCCESS(f'Successfully initialized periodic task "{periodic_task.task_name}".')
            )
