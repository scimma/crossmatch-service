import os
from celery import Celery

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'project.settings')

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
