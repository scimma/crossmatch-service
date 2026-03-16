"""Connect to a remote Dask scheduler at Celery worker startup."""

from celery.signals import worker_process_init
from django.conf import settings
from core.log import get_logger

logger = get_logger(__name__)


@worker_process_init.connect
def connect_dask_scheduler(**kwargs):
    """Connect to remote Dask scheduler if DASK_SCHEDULER_ADDRESS is set.

    When connected, dask.distributed.Client automatically registers itself
    as the default scheduler for the process. LSDB .compute() calls will
    use it without any code changes.

    If the connection fails, the worker starts anyway using the local
    synchronous scheduler.
    """
    address = settings.DASK_SCHEDULER_ADDRESS
    if not address:
        logger.info('No DASK_SCHEDULER_ADDRESS set, using local Dask scheduler')
        return

    from dask.distributed import Client
    logger.info('Connecting to remote Dask scheduler', address=address)
    try:
        Client(address)
        logger.info('Connected to remote Dask scheduler', address=address)
    except Exception:
        logger.exception('Failed to connect to Dask scheduler, falling back to local',
                         address=address)
