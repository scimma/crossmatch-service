import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

rabbitmq_username = os.environ.get('RABBITMQ_DEFAULT_USER', 'rabbitmq')
rabbitmq_password = os.environ.get('RABBITMQ_DEFAULT_PASS', 'rabbitmq')
rabbitmq_service = os.environ.get('MESSAGE_BROKER_HOST', 'rabbitmq')
rabbitmq_port = os.environ.get('MESSAGE_BROKER_PORT', '5672')

app = Celery(
    'tasks',
    broker=f'amqp://{rabbitmq_username}:{rabbitmq_password}@{rabbitmq_service}:{rabbitmq_port}//',
    imports=[
        'tasks.tasks',
    ],
    task_default_queue='jobs',
    task_soft_time_limit=int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', "3600")),
    task_hard_time_limit=int(os.getenv('CELERY_TASK_TIME_LIMIT', "3800")),
    result_expires=3600,
    task_track_started=True,
    timezone="UTC",
    beat_scheduler="django_celery_beat.schedulers:DatabaseScheduler",
)

# ref: https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html#using-celery-with-django
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
# app.autodiscover_tasks()

# If the worker is running in Kubernetes, enable the liveness probe
if os.getenv('KUBERNETES_SERVICE_HOST', ''):
    from core.k8s import LivenessProbe
    app.steps["worker"].add(LivenessProbe)


if __name__ == '__main__':
    app.start()
