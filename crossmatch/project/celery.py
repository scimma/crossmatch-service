import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

redis_service = os.getenv('REDIS_SERVICE', 'redis')
redis_port = os.getenv('REDIS_PORT', '6379')
redis_broker_db = os.getenv('REDIS_BROKER_DB', '0')
redis_result_db = os.getenv('REDIS_RESULT_DB', '1')

BROKER_URL = f'redis://{redis_service}:{redis_port}/{redis_broker_db}'
RESULT_BACKEND = f'redis://{redis_service}:{redis_port}/{redis_result_db}'

app = Celery(
    'crossmatch',
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    imports=[
        'tasks.crossmatch',
        'tasks.schedule',
    ],
    task_default_queue='crossmatch',
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', '3600')),
    task_time_limit=int(os.getenv('CELERY_TASK_TIME_LIMIT', '3800')),
    result_expires=3600,
    task_track_started=True,
    timezone='UTC',
    beat_scheduler='django_celery_beat.schedulers:DatabaseScheduler',
)

# ref: https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html#using-celery-with-django
app.config_from_object('django.conf:settings', namespace='CELERY')

# If the worker is running in Kubernetes, enable the liveness probe
if os.getenv('KUBERNETES_SERVICE_HOST', ''):
    from core.k8s import LivenessProbe
    app.steps["worker"].add(LivenessProbe)


if __name__ == '__main__':
    app.start()
