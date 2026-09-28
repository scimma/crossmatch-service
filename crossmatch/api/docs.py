"""The agent-facing docs, built from the contract builders (R25, R26, R27; KTD15).

``/llms.txt`` (llmstxt.org structure), the Markdown reference ``/api-docs.md``,
and the HTML ``/api-docs`` page (through ``web/config.py``) all render the one
dict ``reference`` returns. It is assembled from the same sources as the
OpenAPI document and ``api/describe``: the served document itself
(``api/openapi.py``) for the operations and code enums, ``describe_service``
(``api/discovery.py``) for the catalogs, limits, and vocabulary, and the
provenance builder (``core/provenance.py``) for the radius, catalog releases,
reliability cuts, and version. Nothing configured is restated here, so the four
surfaces cannot disagree with each other or with the running service.

Everything is read at call time, so ``@override_settings`` applies, and none of
it needs the database.
"""

from __future__ import annotations

from typing import Any

from django.urls import reverse

from api.contract import CODE_DESCRIPTIONS
from api.discovery import (
    COINCIDENCE_CAVEAT,
    NEAREST_SOURCE_RULE,
    NOT_IN_SERVICE_CAVEAT,
    PAGING_CAVEAT,
    describe_service,
    position_conventions,
)
from api.openapi import build_document, cuts_text
from api.service import DEFAULT_TIME_FIELD, DEFAULT_WINDOW_HOURS, TIME_FIELDS

TITLE = 'SCiMMA Rubin Crossmatch Service'

#: The HTTP methods an OpenAPI path item can carry, in rendering order.
_METHODS = ('get', 'post', 'put', 'patch', 'delete')

#: Operations that describe the service rather than query it.
DISCOVERY_OPERATIONS = ('get_openapi', 'describe_service', 'service_status')

#: The code enums of the contract, as (OpenAPI component, heading).
CODE_GROUPS = (
    ('ObjectStatus', 'Object status'),
    ('InputStatus', 'Input status'),
    ('CatalogOutcome', 'Per-catalog search outcome'),
    ('QualifiesReason', 'Qualifies reason'),
    ('ErrorCode', 'Error code'),
    ('CutEnforcedBy', 'Reliability cut enforced by'),
    ('CutStatus', 'Reliability cut status'),
)


def _schema_name(schema: dict[str, Any] | None) -> str | None:
    """The component name of a ``$ref`` schema, else ``None``."""
    ref = (schema or {}).get('$ref', '')
    return ref.rsplit('/', 1)[-1] if ref else None


def operations(document: dict[str, Any], base_url: str = '') -> list[dict[str, Any]]:
    """Every operation in the OpenAPI document, in document order.

    Args:
        document: The OpenAPI document (``build_document()``).
        base_url: Scheme and host prefixed to each path for ``url``; empty for
            site-relative links.

    Returns:
        One dict per operation: ``operation_id``, ``method`` (upper case),
        ``path``, ``url``, ``summary``, ``description``, ``parameters`` (name,
        location, required, unit, description), ``request_body`` (component
        name or ``None``), ``responses`` (status, description), and
        ``discovery`` (whether it describes the service rather than queries
        it).
    """
    result = []
    for path, item in document['paths'].items():
        for method in _METHODS:
            op = item.get(method)
            if op is None:
                continue
            body = op.get('requestBody', {}).get('content', {}).get('application/json')
            result.append({
                'operation_id': op['operationId'],
                'method': method.upper(),
                'path': path,
                'url': base_url + path,
                'summary': op['summary'],
                'description': op.get('description', ''),
                'parameters': [
                    {
                        'name': param['name'],
                        'location': param['in'],
                        'required': bool(param.get('required')),
                        'unit': param.get('schema', {}).get('x-unit'),
                        'description': param.get('description', ''),
                    }
                    for param in op.get('parameters', [])
                ],
                'request_body': _schema_name(body.get('schema')) if body else None,
                'responses': [
                    {'status': status, 'description': response['description']}
                    for status, response in op['responses'].items()
                ],
                'discovery': op['operationId'] in DISCOVERY_OPERATIONS,
            })
    return result


def doc_urls(base_url: str = '') -> dict[str, str]:
    """The URLs of the docs and the informational pages.

    Args:
        base_url: Scheme and host to prefix; empty for site-relative links.

    Returns:
        ``openapi``, ``llms``, ``markdown``, ``html``, ``catalogs``,
        ``brokers``, and ``consuming``.
    """
    return {
        'openapi': base_url + reverse('openapi'),
        'llms': base_url + reverse('web:llms-txt'),
        'markdown': base_url + reverse('web:api-markdown'),
        'html': base_url + reverse('web:api'),
        'catalogs': base_url + reverse('web:catalogs'),
        'brokers': base_url + reverse('web:brokers'),
        'consuming': base_url + reverse('web:consuming'),
    }


def reference(base_url: str = '') -> dict[str, Any]:
    """Every fact and text the docs render, from the shared builders (KTD15).

    Args:
        base_url: Scheme and host for absolute links (``/llms.txt`` and
            ``/api-docs.md``); empty for site-relative links (the HTML page).

    Returns:
        A JSON-native dict: ``title``, ``service_version``,
        ``contract_version``, ``radius_arcsec``, ``catalogs`` (as in
        ``api/describe``), ``reliability_cuts`` and ``cuts_text``, ``limits``,
        ``tns``, ``detail_levels``, ``default_detail``, ``response_modes``,
        ``generic_filters``, ``provenance_recording_release``, ``recent`` (the
        recent-crossmatches defaults and limits), ``conventions``,
        ``nearest_rule``, ``caveats``, ``operations``, ``code_groups``, and
        ``urls``.
    """
    document = build_document()
    described = describe_service()
    current = described['provenance']
    schemas = document['components']['schemas']
    return {
        'title': TITLE,
        'service_version': current['service_version'] or 'unknown',
        'contract_version': current['contract_version'],
        'radius_arcsec': current['crossmatch_radius_arcsec'],
        'catalogs': described['catalogs'],
        'reliability_cuts': current['reliability_cuts'],
        'cuts_text': cuts_text(current['reliability_cuts']),
        'limits': described['limits'],
        'tns': described['tns'],
        'detail_levels': described['detail_levels'],
        'default_detail': described['default_detail'],
        'response_modes': described['response_modes'],
        'generic_filters': described['generic_filters'],
        'provenance_recording_release': described['provenance_recording_release'],
        'recent': {
            'default_window_hours': DEFAULT_WINDOW_HOURS,
            'time_fields': list(TIME_FIELDS),
            'default_time_field': DEFAULT_TIME_FIELD,
            **described['limits']['recent_crossmatches'],
        },
        'conventions': position_conventions(),
        'nearest_rule': NEAREST_SOURCE_RULE,
        'caveats': {
            'not_in_service': NOT_IN_SERVICE_CAVEAT,
            'coincidence': COINCIDENCE_CAVEAT,
            'paging': PAGING_CAVEAT,
        },
        'operations': operations(document, base_url),
        'code_groups': [
            {
                'component': component,
                'heading': heading,
                'codes': [
                    {'code': code, 'description': CODE_DESCRIPTIONS[code]}
                    for code in schemas[component]['enum']
                ],
            }
            for component, heading in CODE_GROUPS
        ],
        'urls': doc_urls(base_url),
    }
