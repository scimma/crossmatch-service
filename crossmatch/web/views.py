"""Views for the informational web frontend.

One view per topic page (Home, Catalogs, Brokers & filtering, Consuming
matches, API reference). Every view pulls the deployed service's facts through
the single live-config seam (``web/config.py``, KTD2) and never reads
``settings`` directly, so a secret can never reach a template. Each view seeds a
shared base context (the active nav key plus the scalar service facts the footer
and pages display).

The agent-facing docs, ``/llms.txt`` and ``/api-docs.md`` (U8, KTD15), render
the same reference builder (``api/docs.py``) as the API reference page.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

from api import docs
from core.log import get_logger
from web import config

logger = get_logger(__name__)

#: ``Link`` header of the Markdown docs (KTD15).
_DESCRIBED_BY = '<{}>; rel="describedby"'

#: Body of ``/llms.txt`` and ``/api-docs.md`` when the reference cannot be built.
_REFERENCE_UNAVAILABLE = 'The API reference is temporarily unavailable; retry later.\n'


def _base_context(active: str, **extra: Any) -> dict[str, Any]:
    """Context every page needs: the active nav key and the scalar service facts.

    ``service`` carries the seam's scalar display fields (radius, reliability,
    Hopskotch broker/topic, app version, LSDB version); the footer reads
    ``service.app_version`` from it (KTD2 keeps every settings read behind the
    seam rather than a settings-reading template tag).

    Args:
        active: The nav key of the current page (marks it active in the navbar).
        **extra: Additional per-page context keys.

    Returns:
        The merged template context.
    """
    context: dict[str, Any] = {
        'active': active,
        'service': config.service_config(),
    }
    context.update(extra)
    return context


def home(request: HttpRequest) -> HttpResponse:
    """Plain-language overview with entry-point cards into the topic pages."""
    return render(request, 'web/home.html', _base_context('home'))


def catalogs(request: HttpRequest) -> HttpResponse:
    """Per-catalog published columns (lowercased), radius, and LSDB version."""
    return render(
        request,
        'web/catalogs.html',
        _base_context('catalogs', catalogs=config.catalogs()),
    )


def brokers(request: HttpRequest) -> HttpResponse:
    """Upstream brokers, their quality topics, and the reliability filter."""
    return render(
        request,
        'web/brokers.html',
        _base_context('brokers', brokers=config.brokers()),
    )


def consuming(request: HttpRequest) -> HttpResponse:
    """Where matches are published and how to subscribe (hop-client)."""
    return render(request, 'web/consuming.html', _base_context('consuming'))


def api_reference(request: HttpRequest) -> HttpResponse:
    """The API reference page; configured facts come from ``config.api_reference``."""
    return render(
        request,
        'web/api.html',
        _base_context('api', api=config.api_reference()),
    )


def _base_url(request: HttpRequest) -> str:
    """Scheme and host of the request, for absolute links in the agent docs.

    TLS terminates at the ingress (Traefik), so Django sees plain HTTP; honor
    the proxy's ``X-Forwarded-Proto`` for the scheme so deployed links are
    ``https``. Only link text depends on it, so a spoofed header cannot do
    more than change the scheme printed back to the caller who sent it.
    """
    scheme = request.scheme
    if request.headers.get('X-Forwarded-Proto', '').lower() == 'https':
        scheme = 'https'
    return f'{scheme}://{request.get_host()}'


def llms_txt(request: HttpRequest) -> HttpResponse:
    """``/llms.txt`` (llmstxt.org): where agents find the OpenAPI and Markdown docs (R25).

    A reference that cannot be built is a short 503, as the HTML page degrades.
    """
    content_type = 'text/plain; charset=utf-8'
    try:
        ref = docs.reference(_base_url(request))
    except Exception as exc:  # noqa: BLE001 -- degrade to a 503, never 500
        logger.warning('web_llms_txt_reference_unavailable', error=str(exc))
        return HttpResponse(_REFERENCE_UNAVAILABLE, status=503, content_type=content_type)
    return render(request, 'web/llms.txt', {'ref': ref}, content_type=content_type)


def api_markdown(request: HttpRequest) -> HttpResponse:
    """The API reference as Markdown (R26), described by ``/llms.txt``.

    A reference that cannot be built is a short 503, as the HTML page degrades.
    """
    content_type = 'text/markdown; charset=utf-8'
    try:
        ref = docs.reference(_base_url(request))
    except Exception as exc:  # noqa: BLE001 -- degrade to a 503, never 500
        logger.warning('web_api_markdown_reference_unavailable', error=str(exc))
        return HttpResponse(_REFERENCE_UNAVAILABLE, status=503, content_type=content_type)
    response = render(request, 'web/api.md', {'ref': ref}, content_type=content_type)
    response['Link'] = _DESCRIBED_BY.format(reverse('web:llms-txt'))
    return response
