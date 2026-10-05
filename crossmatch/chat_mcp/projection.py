"""Compact chat projections of the API's own return values (KTD5, KTD9-KTD11).

Each tool calls ``lookup_objects``, ``cone_search`` or ``describe_service``
once; the functions here turn that return value into the compact answer the
tool sends as one JSON text block (``to_text``). They are pure: no database,
no request. Every fact is copied from the API result, so a chat answer and an
API response about the same object never disagree (R10).

* ``project_lookup``: one result per distinct identifier, in order, with an
  object summary per Rubin object (R1, R5, R6, R8).
* ``project_near_position``: the nearest objects around a position (R2).
* ``project_coverage``: what the service covers and its current state (R3).

Answers are capped by settings read at call time (KTD9): at most
``MCP_MAX_OBJECTS`` summaries, ``MCP_MATCHES_PER_CATALOG`` nearest sources per
catalog, and ``MCP_MAX_RESULT_CHARS`` of serialized text, met by dropping
whole objects from the end. Anything cut marks the answer ``truncated`` with a
ready-to-run API request for the full set (R7). URLs and requests are built
only from settings and canonical identifiers (digits or normalized TNS
designations), never from raw input or free-text fields.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from django.conf import settings

from api.contract import CODE_DESCRIPTIONS, InputStatus, ObjectStatus
from api.discovery import COINCIDENCE_CAVEAT, NOT_IN_SERVICE_CAVEAT
from chat_mcp.identifiers import (
    KIND_ID, KIND_PRECISION_LOST, KIND_TNS, KIND_UNRECOGNIZED, Classified, Identifier,
)
from matching.payload import _to_json_scalar

#: ``kind`` of a projected lookup result, by identifier kind.
RESULT_KINDS = {
    KIND_ID: 'diaObjectId',
    KIND_TNS: 'tns_name',
    KIND_UNRECOGNIZED: 'unrecognized',
    KIND_PRECISION_LOST: 'precision_lost',
}

#: ``status`` of an identifier that was not looked up.
STATUS_UNRECOGNIZED = 'unrecognized_identifier'
STATUS_PRECISION_LOST = 'identifier_precision_lost'

#: ``state`` of an object's stored TNS association.
TNS_MATCHED = 'matched'
TNS_NONE_WITHIN_RADIUS = 'none_within_radius'
TNS_NOT_CHECKED = 'not_checked'

TNS_STATE_NOTES = {
    TNS_MATCHED: (
        'The TNS object positionally associated with this Rubin object when it '
        'was crossmatched, as of the TNS snapshot named by snapshot_epoch.'
    ),
    TNS_NONE_WITHIN_RADIUS: (
        'No TNS object lay within the association radius in the TNS snapshot '
        'named by snapshot_epoch. A name reported to TNS after that snapshot '
        'would not show here.'
    ),
    TNS_NOT_CHECKED: (
        'Not compared with TNS when crossmatched (no current TNS snapshot at '
        'the time, or crossmatched before TNS association existed). This says '
        'nothing about whether TNS has a name for it now.'
    ),
}

RECORDED_PROVENANCE_NOTE = (
    'The catalogs, releases, crossmatch radius and reliability cuts in effect '
    'were recorded when the object was crossmatched.'
)

TRUNCATED_OBJECTS = 'chat_object_limit'
TRUNCATED_SIZE = 'chat_size_limit'
TRUNCATED_API = 'api_listing_limit'

_DOES_NOT_ANSWER = (
    NOT_IN_SERVICE_CAVEAT,
    COINCIDENCE_CAVEAT,
    'The service does not hold Rubin light curves, photometry or '
    'classifications; the brokers (Lasair, ANTARES, Pitt-Google) serve those.',
    'TNS associations are positional and as of the TNS snapshot named in each '
    'answer; the service does not query TNS live.',
)


def to_text(projection: dict[str, Any]) -> str:
    """Serialize a projection as the tool's compact JSON text block.

    The character budget (``MCP_MAX_RESULT_CHARS``) is measured on this text.

    Args:
        projection: A projection from this module.

    Returns:
        Compact JSON (no whitespace between tokens).
    """
    return json.dumps(projection, separators=(',', ':'))


def _base_url() -> str:
    """The public API's scheme and host, without a trailing slash."""
    return str(settings.MCP_API_BASE_URL).rstrip('/')


def _key_columns() -> dict[str, list[str]]:
    """``{catalog: [lowercased key column, ...]}`` from ``CROSSMATCH_CATALOGS``."""
    return {
        str(cat['name']): [str(c).lower() for c in cat.get('key_columns') or []]
        for cat in settings.CROSSMATCH_CATALOGS
    }


def _coincident_sources(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group matches by catalog, nearest first, with key values (R5, KTD10).

    Catalogs come in configured order; a catalog no longer configured follows,
    with no key values.
    """
    per_catalog = int(settings.MCP_MATCHES_PER_CATALOG)
    keys = _key_columns()
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in keys}
    for match in matches:
        groups.setdefault(str(match['catalog_name']), []).append(match)
    result = []
    for name, group in groups.items():
        if not group:
            continue
        group = sorted(
            group, key=lambda m: (m['separation_arcsec'], str(m['catalog_source_id']))
        )
        nearest = []
        for match in group[:per_catalog]:
            payload = match.get('catalog_payload') or {}
            nearest.append({
                'source_id': str(match['catalog_source_id']),
                'separation_arcsec': _to_json_scalar(match['separation_arcsec']),
                'values': {
                    column: _to_json_scalar(payload.get(column))
                    for column in keys.get(name, [])
                },
            })
        result.append({
            'catalog': name,
            'count': len(group),
            'nearest': nearest,
            'more': len(group) - len(nearest),
        })
    return result


def _tns_association(block: dict[str, Any] | None) -> dict[str, Any]:
    """An object's stored TNS association, as of its snapshot (R5, KTD7).

    Args:
        block: The object's API ``tns`` block, or ``None``.

    Returns:
        ``{'state': ...}``, with the match fields and snapshot epoch when the
        association was checked.
    """
    if block is None or not block.get('checked'):
        return {'state': TNS_NOT_CHECKED}
    if block.get('name') is None:
        return {'state': TNS_NONE_WITHIN_RADIUS, 'snapshot_epoch': block['snapshot_epoch']}
    return {
        'state': TNS_MATCHED,
        'name': block['name'],
        'name_prefix': block['name_prefix'],
        'classification': block['classification'],
        'redshift': block['redshift'],
        'separation_arcsec': block['separation_arcsec'],
        'snapshot_epoch': block['snapshot_epoch'],
    }


def _provenance_basis(
    crossmatch: dict[str, Any] | None, provenance_sets: dict[str, Any],
) -> str | None:
    """The ``basis`` of the provenance set an object refers to, or ``None``."""
    if crossmatch is None:
        return None
    key = crossmatch.get('provenance_set') or crossmatch.get('best_guess_provenance_set')
    entry = provenance_sets.get(key) if key is not None else None
    return entry['basis'] if entry is not None else None


def _links(object_id: str, tns_url: str | None) -> dict[str, str | None]:
    """TNS, Lasair and ANTARES links for one object (R6, KTD11)."""
    return {
        'tns': tns_url,
        'lasair': settings.LASAIR_OBJECT_URL_TEMPLATE.format(diaObjectId=object_id),
        'antares': settings.ANTARES_OBJECT_URL_TEMPLATE.format(diaObjectId=object_id),
    }


def _summary(
    obj: dict[str, Any],
    provenance_sets: dict[str, Any],
    *,
    tns_url: str | None = None,
) -> dict[str, Any]:
    """One object's chat summary (R5, R6, R9).

    Args:
        obj: An API object entry at detail ``full``.
        provenance_sets: The response's ``provenance_sets``.
        tns_url: The TNS link from a resolved TNS-name input, which takes
            precedence over the stored association's link.

    Returns:
        ``diaObjectId`` (string) and ``status``; for an object in the
        service also its separation from a searched position (when given),
        position, brokers, provenance basis, TNS association (once
        crossmatched), coincident sources and links.
    """
    object_id = str(obj['diaObjectId_str'])
    summary: dict[str, Any] = {'diaObjectId': object_id, 'status': obj['status']}
    if obj['status'] == ObjectStatus.NOT_IN_SERVICE:
        return summary
    if 'separation_arcsec' in obj:
        summary['separation_arcsec'] = _to_json_scalar(obj['separation_arcsec'])
    summary['ra'] = obj.get('ra')
    summary['dec'] = obj.get('dec')
    summary['brokers'] = list(obj.get('brokers') or [])
    crossmatch = obj.get('crossmatch')
    summary['provenance'] = _provenance_basis(crossmatch, provenance_sets)
    if crossmatch is not None:
        summary['tns_association'] = _tns_association(obj.get('tns'))
    summary['coincident_sources'] = _coincident_sources(obj.get('matches') or [])
    stored_url = (obj.get('tns') or {}).get('url')
    summary['links'] = _links(object_id, tns_url or stored_url)
    return summary


def _notes(
    summaries: list[dict[str, Any]], provenance_sets: dict[str, Any],
) -> dict[str, Any]:
    """The meanings of the statuses, provenance bases and TNS states present."""
    meanings: dict[str, str] = {}
    provenance_notes: dict[str, str] = {}
    tns_notes: dict[str, str] = {}
    best_guess = {
        entry['basis']: entry.get('description')
        for entry in provenance_sets.values() if entry.get('description')
    }
    for summary in summaries:
        meanings.setdefault(summary['status'], CODE_DESCRIPTIONS[summary['status']])
        basis = summary.get('provenance')
        if basis is not None and basis not in provenance_notes:
            provenance_notes[basis] = best_guess.get(basis) or RECORDED_PROVENANCE_NOTE
        state = (summary.get('tns_association') or {}).get('state')
        if state is not None:
            tns_notes.setdefault(state, TNS_STATE_NOTES[state])
    return {
        'status_meanings': meanings,
        'provenance_notes': provenance_notes,
        'tns_notes': tns_notes,
    }


def _resolved_tns(record: dict[str, Any], epoch: str | None) -> dict[str, Any]:
    """The TNS record a name input resolved to, as of its snapshot (R5, AE7)."""
    return {
        'name': record['name'],
        'name_prefix': record['name_prefix'],
        'classification': record['classification'],
        'redshift': record['redshift'],
        'ra': record['ra'],
        'dec': record['dec'],
        'url': record['url'],
        'snapshot_epoch': epoch,
    }


def _tns_reason(api: dict[str, Any], name: str) -> str | None:
    """The reason sentence of a TNS-name result that lists no object (R8)."""
    status = api['status']
    if status == InputStatus.TNS_NAME_NOT_FOUND:
        return (
            f"{name} is not in the service's TNS snapshot as of "
            f"{api['tns_snapshot_epoch']}; names reported in the last hour or two "
            'may not be there yet.'
        )
    if status == InputStatus.RESOLVER_UNAVAILABLE:
        return (
            "TNS-name lookup is unavailable right now: the service's TNS snapshot "
            'is missing or out of date. Ask by diaObjectId or by position instead.'
        )
    if status == InputStatus.NO_RUBIN_OBJECT:
        radius = float(api['normalized']['radius_arcsec'])
        return (
            f'TNS knows {name}, but no Rubin object in this service lies within '
            f'{radius:g} arcsec of its TNS position.'
        )
    return None


def _lookup_entry(
    ident: Identifier, api: dict[str, Any], provenance_sets: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """One looked-up identifier's result and how many objects it found."""
    entry: dict[str, Any] = {
        'position': ident.index,
        'identifier': ident.value,
        'kind': RESULT_KINDS[ident.kind],
        'status': api['status'],
    }
    if api['status'] == InputStatus.INVALID_INPUT:
        entry['reason'] = 'The service rejected this identifier as malformed.'
        entry['objects'] = []
        return entry, 0
    if ident.kind == KIND_ID:
        entry['objects'] = [_summary(o, provenance_sets) for o in api['objects']]
        return entry, len(api['objects'])
    reason = _tns_reason(api, ident.value)
    if reason is not None:
        entry['reason'] = reason
    tns_url = None
    if api.get('tns') is not None:
        entry['tns'] = _resolved_tns(api['tns'], api.get('tns_snapshot_epoch'))
        tns_url = api['tns']['url']
    total = int(api.get('total') or 0)
    if api['objects']:
        entry['objects_total'] = total
    entry['objects'] = [
        _summary(o, provenance_sets, tns_url=tns_url) for o in api['objects']
    ]
    return entry, max(total, len(api['objects']))


def _lookup_request(classified: Classified) -> dict[str, str]:
    """The ready-to-run ``POST /api/lookup`` for the full set (R7).

    The body lists only canonical identifiers (digits or normalized TNS
    designations), so it needs no escaping inside single quotes.
    """
    body = json.dumps(
        {'inputs': classified.lookup_inputs(), 'detail': 'full'}, separators=(',', ':'),
    )
    curl = (
        f"curl -s -X POST '{_base_url()}/api/lookup' "
        f"-H 'Content-Type: application/json' -d '{body}'"
    )
    note = (
        'This request to the public API returns every object for these '
        'identifiers as JSON.'
    )
    left_out = sum(1 for i in classified.identifiers if i.lookup_input() is None)
    if left_out:
        note += (
            f' {left_out} unrecognized or imprecise identifiers were left out of it.'
        )
    return {'note': note, 'curl': curl}


def _fit(
    entries: list[dict[str, Any]],
    assemble: Callable[[list[str]], dict[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    """Drop whole objects from the end until the text fits the budget (KTD9).

    Args:
        entries: The result entries, each with an ``objects`` list; trimmed in
            place. An entry emptied by trimming is removed.
        assemble: ``assemble(reasons) -> projection``, rebuilt after each drop.
        reasons: Truncation reasons so far; ``TRUNCATED_SIZE`` is added on the
            first drop.

    Returns:
        The final projection.
    """
    max_chars = int(settings.MCP_MAX_RESULT_CHARS)
    while True:
        projection = assemble(reasons)
        if len(to_text(projection)) <= max_chars:
            return projection
        holder = next((e for e in reversed(entries) if e['objects']), None)
        if holder is None:
            return projection
        holder['objects'].pop()
        if not holder['objects']:
            entries.remove(holder)
        if TRUNCATED_SIZE not in reasons:
            reasons.append(TRUNCATED_SIZE)


def project_lookup(
    classified: Classified, response: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project the identifier tool's answer (R1, R5-R9; AE1, AE2, AE3, AE5, AE7).

    Args:
        classified: The classified identifiers
            (``chat_mcp.identifiers.classify_identifiers``).
        response: The ``lookup_objects(inputs=classified.lookup_inputs(),
            detail='full')`` response, or ``None`` when no identifier was
            looked up.

    Returns:
        ``requested``, ``objects_found``, ``objects_returned``, ``truncated``,
        ``truncation`` (when truncated: ``reasons`` and ``full_results`` with
        a ``curl``), ``results`` (one per distinct identifier, in order: its
        ``position`` in the caller's list, canonical ``identifier``, ``kind``,
        ``status``, a ``reason`` when it lists no object, the resolved ``tns``
        record for a TNS name, and ``objects``), and the
        ``status_meanings``, ``provenance_notes`` and ``tns_notes`` of what
        the summaries show.
    """
    provenance_sets = (response or {}).get('provenance_sets') or {}
    api_results = iter((response or {}).get('results') or [])
    entries: list[dict[str, Any]] = []
    found = 0
    for ident in classified.identifiers:
        if ident.lookup_input() is None:
            entries.append({
                'position': ident.index,
                'identifier': None,
                'kind': RESULT_KINDS[ident.kind],
                'status': (
                    STATUS_PRECISION_LOST if ident.kind == KIND_PRECISION_LOST
                    else STATUS_UNRECOGNIZED
                ),
                'reason': ident.reason,
                'objects': [],
            })
            continue
        entry, count = _lookup_entry(ident, next(api_results), provenance_sets)
        entries.append(entry)
        found += count

    reasons: list[str] = []
    remaining = int(settings.MCP_MAX_OBJECTS)
    kept = []
    for entry in entries:
        objects = entry['objects']
        if objects and remaining <= 0:
            if TRUNCATED_OBJECTS not in reasons:
                reasons.append(TRUNCATED_OBJECTS)
            continue
        if len(objects) > remaining:
            del objects[remaining:]
            if TRUNCATED_OBJECTS not in reasons:
                reasons.append(TRUNCATED_OBJECTS)
        remaining -= len(objects)
        kept.append(entry)
    entries = kept

    def assemble(reasons: list[str]) -> dict[str, Any]:
        summaries = [obj for entry in entries for obj in entry['objects']]
        returned = len(summaries)
        cut = list(reasons)
        if returned < found and not cut:
            cut.append(TRUNCATED_API)
        projection: dict[str, Any] = {
            'requested': classified.requested,
            'objects_found': found,
            'objects_returned': returned,
            'truncated': bool(cut),
        }
        if cut:
            projection['truncation'] = {
                'reasons': cut, 'full_results': _lookup_request(classified),
            }
        projection['results'] = entries
        projection.update(_notes(summaries, provenance_sets))
        return projection

    return _fit(entries, assemble, reasons)


def _cone_request(ra: float, dec: float, radius: float) -> dict[str, str]:
    """The API cone search that pages the full set, in ingest order (R2, R7)."""
    page_size = int(settings.API_MAX_OBJECTS_PER_POSITION)
    query = urlencode({
        'ra': repr(float(ra)), 'dec': repr(float(dec)),
        'radius_arcsec': repr(float(radius)), 'detail': 'full', 'page_size': page_size,
    })
    return {
        'note': (
            f'This request to the public API returns results in ingest order '
            f'(oldest first), not nearest first, {page_size} per page; follow '
            'next_cursor in each response for the next page.'
        ),
        'url': f'{_base_url()}/api/cone?{query}',
    }


def project_near_position(response: dict[str, Any]) -> dict[str, Any]:
    """Project the near-position tool's answer (R2, R5-R8; AE4).

    Args:
        response: The ``cone_search(..., detail='full', order=ORDER_NEAREST)``
            response.

    Returns:
        ``center`` (ra, dec), ``radius_arcsec``, ``order``, ``status``, a
        ``reason`` when nothing is within the radius, ``objects_total``,
        ``objects_returned``, ``truncated``, ``truncation`` (when truncated:
        ``reasons`` and ``full_results`` with the cone ``url``), ``objects``
        (summaries, nearest first, each with ``separation_arcsec``), and the
        notes of what they show.
    """
    provenance_sets = response.get('provenance_sets') or {}
    result = response['results'][0]
    normalized = result['normalized']
    ra, dec = normalized['ra'], normalized['dec']
    radius = float(normalized['radius_arcsec'])
    total = int(result.get('total') or 0)
    objects = [_summary(o, provenance_sets) for o in result['objects']]
    reasons: list[str] = []
    cap = int(settings.MCP_MAX_OBJECTS)
    if len(objects) > cap:
        del objects[cap:]
        reasons.append(TRUNCATED_OBJECTS)
    entry: dict[str, Any] = {'objects': objects}

    def assemble(reasons: list[str]) -> dict[str, Any]:
        returned = len(entry['objects'])
        cut = list(reasons)
        if returned < total and not cut:
            cut.append(TRUNCATED_API)
        projection: dict[str, Any] = {
            'center': {'ra': ra, 'dec': dec},
            'radius_arcsec': radius,
            'order': 'nearest_first',
            'status': result['status'],
        }
        if result['status'] == InputStatus.NO_RUBIN_OBJECT:
            projection['reason'] = (
                f'No transient seen by this service lies within {radius:g} arcsec '
                f'of RA {ra:g}, Dec {dec:g} (degrees). The service holds only '
                'alerts its brokers delivered, so this is not evidence that Rubin '
                'saw nothing there.'
            )
        projection.update({
            'objects_total': total,
            'objects_returned': returned,
            'truncated': bool(cut),
        })
        if cut:
            projection['truncation'] = {
                'reasons': cut, 'full_results': _cone_request(ra, dec, radius),
            }
        projection['objects'] = entry['objects']
        projection.update(_notes(entry['objects'], provenance_sets))
        return projection

    return _fit([entry], assemble, reasons)


def project_coverage(
    describe: dict[str, Any],
    status: dict[str, Any],
    *,
    latest_ingest: datetime | None,
) -> dict[str, Any]:
    """Project the coverage tool's answer (R3).

    Args:
        describe: The ``describe_service()`` body.
        status: The ``service_status(...)`` body.
        latest_ingest: When the newest alert reached the service, or ``None``
            when there is none or it could not be read.

    Returns:
        The service version and database state, the catalogs and releases,
        crossmatch radius and rule, the per-broker reliability cuts, TNS
        resolution state and snapshot epoch, the TNS and cone radii, the
        latest alert ingest time, and what the service does not answer.
    """
    return {
        'service_version': status.get('service_version'),
        'database': status.get('database'),
        'catalogs': [
            {'name': cat['name'], 'release': cat['release']}
            for cat in describe['catalogs']
        ],
        'crossmatch_radius_arcsec': describe['crossmatch']['radius_arcsec'],
        'crossmatch_rule': describe['crossmatch']['description'],
        'reliability_cuts': describe['reliability_cuts'],
        'tns_resolution': status.get('tns_resolution'),
        'tns_snapshot_epoch': status.get('tns_snapshot_epoch'),
        'tns_match_radius_arcsec': describe['tns']['default_radius_arcsec'],
        'max_search_radius_arcsec': describe['limits']['max_cone_radius_arcsec'],
        'latest_alert_ingest': (
            latest_ingest.isoformat() if latest_ingest is not None else None
        ),
        'latest_alert_ingest_note': (
            'When the newest alert reached this service. If it is hours or days '
            'old, Rubin or its brokers have paused alerts or ingest has stalled, '
            'and objects alerted since then are not in the service yet.'
        ),
        'does_not_answer': list(_DOES_NOT_ANSWER),
    }
