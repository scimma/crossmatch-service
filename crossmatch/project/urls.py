"""Root URLconf for the crossmatch project.

Greenfield HTTP surface: this project historically ran only Celery workers and
Kafka/PubSub consumers with no web entry point. The web workload
(``entrypoints/run_web.sh``) serves the read-model API mounted under ``api/``
(see ``api/urls.py``) plus a health path for liveness/readiness probes. Auth for
the API path is handled at the ingress, not here (R11). The Django admin is
deliberately not mounted: the DEV ingress routes only ``/api`` to this workload
and the endpoint is public, so exposing an admin login here would be surface
with no gate.
"""

from django.http import JsonResponse
from django.urls import include, path


def healthz(request):
    """Liveness/readiness probe target: always 200 with a small JSON body."""
    return JsonResponse({'status': 'ok'})


urlpatterns = [
    path('healthz', healthz, name='healthz'),
    path('api/', include('api.urls')),
    # Informational web frontend (server-rendered pages) at root paths. Kept
    # last so its catch-all page routes never shadow healthz or the api/ prefix.
    path('', include('web.urls')),
]
