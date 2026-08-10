"""Views for the informational web frontend.

One view per topic page (Home, Catalogs, Brokers & filtering, Consuming
matches, API reference). Every view pulls the deployed service's facts through
the single live-config seam (``web/config.py``, KTD2) and never reads
``settings`` directly, so a secret can never reach a template. Each view seeds a
shared base context (the active nav key plus the scalar service facts the footer
and pages display).
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from web import config


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
    """Hand-written reference for the recent-crossmatches REST endpoint (U7)."""
    return render(request, 'web/api.html', _base_context('api'))
