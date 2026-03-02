from celery import shared_task
from django.conf import settings
from core.log import get_logger
logger = get_logger(__name__)


class QueryHEROIC():

    @property
    def task_name(self):
        return "Query HEROIC"

    @property
    def task_handle(self):
        return self.task_func

    @property
    def task_frequency_seconds(self):
        return settings.QUERY_HEROIC_INTERVAL

    @property
    def task_initially_enabled(self):
        return True

    def __init__(self, task_func='') -> None:
        self.task_func = task_func

    def run_task(self):
        logger.info(f'Running periodic task "{self.task_name}"...')


@shared_task
def query_heroic():
    QueryHEROIC().run_task()


@shared_task(name="refresh_planned_pointings")
def refresh_planned_pointings():
    """Fetch planned pointings from HEROIC and refresh the DB table."""
    raise NotImplementedError("deferred to future work")


periodic_tasks = [
    QueryHEROIC(task_func='query_heroic'),
]
