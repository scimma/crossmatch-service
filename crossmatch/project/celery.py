import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

rabbitmq_username = os.environ.get('RABBITMQ_DEFAULT_USER', 'rabbitmq')
rabbitmq_password = os.environ.get('RABBITMQ_DEFAULT_PASS', 'rabbitmq')
rabbitmq_service = os.environ.get('MESSAGE_BROKER_HOST', 'rabbitmq')
rabbitmq_port = os.environ.get('MESSAGE_BROKER_PORT', '5672')

celery_app = Celery("app")
celery_app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
# celery_app.autodiscover_tasks()

# If the worker is running in Kubernetes, enable the liveness probe
if os.getenv('KUBERNETES_SERVICE_HOST', ''):
    from core.k8s import LivenessProbe
    celery_app.steps["worker"].add(LivenessProbe)


if __name__ == '__main__':
    celery_app.start()
