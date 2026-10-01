"""URL patterns for the informational web frontend.

Included at root paths by the project URLconf (``project/urls.py``). The API
reference *page* lives at ``/api-docs`` so it never collides with the ``api/``
JSON prefix (U7). The agent-facing docs, ``llms.txt`` and ``api-docs.md``,
sit at the site root beside it (U8).
"""

from django.urls import path

from web import views

app_name = 'web'

urlpatterns = [
    path('', views.home, name='home'),
    path('catalogs', views.catalogs, name='catalogs'),
    path('brokers', views.brokers, name='brokers'),
    path('consuming', views.consuming, name='consuming'),
    path('api-docs', views.api_reference, name='api'),
    path('api-docs.md', views.api_markdown, name='api-markdown'),
    path('llms.txt', views.llms_txt, name='llms-txt'),
]
