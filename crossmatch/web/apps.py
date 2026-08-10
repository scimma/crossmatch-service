from django.apps import AppConfig


class WebConfig(AppConfig):
    """Informational web frontend (server-rendered Django templates).

    Peer to the ``api`` app: the ``api`` app serves the JSON read-model under
    ``api/`` while this app serves the human-facing pages at root paths. See
    ``docs/plans/2026-08-05-001-feat-web-frontend-plan.md``.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'web'
