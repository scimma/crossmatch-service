from celery import shared_task
from celery.signals import task_failure
from ..core.models import Job, update_job_state

from ..core.log import get_logger
logger = get_logger(__name__)


@shared_task(name="Crossmatch")
def crossmatch(job_id, config={}):
    return


@task_failure.connect()
def task_failed(task_id=None, exception=None, args=None, traceback=None, einfo=None, **kwargs):
    logger.error("from task_failed ==> task_id: " + str(task_id))
    logger.error("from task_failed ==> args: " + str(args))
    logger.error("from task_failed ==> exception: " + str(exception))
    logger.error("from task_failed ==> einfo: " + str(einfo))
    try:
        job_id = kwargs['kwargs']['job_id']
        job = Job.objects.get(uuid__exact=job_id)
        if not job.error_info:
            err_msg = f"System Error: {str(einfo)}"
            update_job_state(job_id, Job.JobStatus.FAILURE, error_info=err_msg)
        else:
            logger.error("from task_failed ==> job.error_info: " + str(job.error_info))
    except KeyError:
        logger.info(f"From task_failed ==> KeyError: {kwargs['kwargs']}")
        pass
