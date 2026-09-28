"""Bodies of the status and describe endpoints (R30, R22, R23; KTD16).

``service_status`` builds ``GET api/status``: whether the database answers,
the service and contract versions, and whether TNS-name resolution is
available, judged by the snapshot-currency helper the resolver itself uses
(``TnsSnapshotMeta.current_epoch``). ``check_database`` runs the status checks
and raises on a database error; the view turns that into ``database:
unavailable`` so the endpoint always answers 200.

``describe_service`` builds ``GET api/describe``: the vocabulary and limits an
agent must respect, from the provenance builder (``core/provenance.py``), the
filter registry (``api/filters.py``), and the settings ceilings, all read at
call time. It needs no database, and it never opens a LSDB/HATS catalog: the
coverage-map resolution it reports is the configured ``footprint_moc_order``.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import connection

from api.filters import (
    GENERIC_FILTERS,
    RESPONSE_COUNT,
    RESPONSE_MODES,
    RESPONSE_OBJECTS,
    FilterField,
    filter_fields,
    filter_parameters,
)
from api.guard import current_guard, sql_phase
from api.service import DEFAULT_DETAIL, DETAIL_LEVELS
from core import provenance
from core.db import set_statement_timeout
from core.models import TnsSnapshotMeta

#: Statement timeout of the status checks, seconds. A database that cannot
#: answer a trivial query this fast is reported unavailable.
STATUS_CHECK_TIMEOUT_SECONDS = 1.0

#: ``database`` values of the status body.
DATABASE_OK = 'ok'
DATABASE_UNAVAILABLE = 'unavailable'
DATABASE_STATES = (DATABASE_OK, DATABASE_UNAVAILABLE)

#: ``tns_resolution`` values of the status body.
TNS_AVAILABLE = 'available'
TNS_UNAVAILABLE = 'unavailable'
TNS_RESOLUTION_STATES = (TNS_AVAILABLE, TNS_UNAVAILABLE)

#: What each detail level returns; levels are cumulative.
DETAIL_LEVEL_DESCRIPTIONS = {
    'ids': (
        'diaObjectId, status, and the crossmatch block (per-catalog search '
        'outcomes and recorded provenance).'
    ),
    'position': (
        'ids plus the object position (ra, dec, degrees), reliability, '
        'ingest_time, event_time, and delivering brokers.'
    ),
    'matches': (
        'position plus the coincident sources: catalog_name, '
        'catalog_source_id, and separation_arcsec.'
    ),
    'full': (
        'matches plus each source as published on Hopskotch: the catalog '
        'payload columns (lowercased keys) and the TNS enrichment.'
    ),
}

#: What each ``response`` mode returns (KTD10).
RESPONSE_MODE_DESCRIPTIONS = {
    RESPONSE_OBJECTS: 'The listing of results and objects (default).',
    RESPONSE_COUNT: (
        'Counts by input status, object status, and qualifies for the same '
        'inputs and filters, without objects and uncapped by the listing limits.'
    ),
}

#: What ``catalog_source_id`` means, per catalog in service by default. A
#: catalog not listed here is described from its configured source-id column.
_SOURCE_ID_MEANINGS = {
    'gaia_dr3': (
        'The Gaia DR3 source_id: the unique identifier of the Gaia source, a '
        '64-bit integer served as a decimal string.'
    ),
    'des_y6_gold': (
        'The DES Y6 Gold COADD_OBJECT_ID: the unique identifier of the coadd '
        'object in the DES Y6 Gold catalog, served as a decimal string.'
    ),
    'delve_dr3_gold': (
        'The DELVE DR3 Gold COADD_OBJECT_ID: the unique identifier of the coadd '
        'object in the DELVE DR3 Gold catalog, served as a decimal string. Do not '
        'assume it names the same source as an equal DES COADD_OBJECT_ID.'
    ),
    'skymapper_dr4': (
        'The SkyMapper DR4 object_id: the unique identifier of the object in '
        'the SkyMapper DR4 master table, served as a decimal string.'
    ),
}

_NEAREST_RULE = (
    'For each object and each catalog, the crossmatch keeps only the nearest '
    'catalog source within the crossmatch radius, so an object has at most one '
    'coincident source per catalog; other sources inside the radius are not '
    'reported. A coincident source is not a host association.'
)


def check_database() -> datetime | None:
    """Run the status checks: a trivial query and TNS snapshot currency.

    Runs under the active request guard, with the statement timeout lowered to
    ``STATUS_CHECK_TIMEOUT_SECONDS`` (or the time remaining, if less).

    Returns:
        The TNS snapshot epoch when the snapshot is current, else ``None``.

    Raises:
        django.db.DatabaseError: If the database cannot be reached or does not
            answer in time.
    """
    with sql_phase():
        guard = current_guard()
        timeout = STATUS_CHECK_TIMEOUT_SECONDS
        if guard is not None:
            timeout = min(timeout, guard.remaining())
        set_statement_timeout(timeout)
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return TnsSnapshotMeta.current_epoch()


def service_status(*, database_ok: bool, snapshot_epoch: datetime | None) -> dict[str, Any]:
    """The ``api/status`` body (R30).

    Args:
        database_ok: Whether the database answered the status checks.
        snapshot_epoch: The current TNS snapshot epoch, or ``None`` when the
            snapshot is stale, absent, or could not be read.

    Returns:
        A JSON-native dict with ``provenance``, ``database``,
        ``service_version``, ``contract_version``, ``tns_resolution``, and
        ``tns_snapshot_epoch``.
    """
    available = database_ok and snapshot_epoch is not None
    return {
        'provenance': provenance.service_provenance(),
        'database': DATABASE_OK if database_ok else DATABASE_UNAVAILABLE,
        'service_version': provenance.service_version(),
        'contract_version': provenance.CONTRACT_VERSION,
        'tns_resolution': TNS_AVAILABLE if available else TNS_UNAVAILABLE,
        'tns_snapshot_epoch': snapshot_epoch.isoformat() if available else None,
    }


def healpix_resolution_arcmin(order: int) -> float:
    """The mean side of a HEALPix pixel at ``order``, in arcmin.

    Args:
        order: The HEALPix order (0..29).

    Returns:
        ``sqrt(4 pi / (12 * 4**order))`` in arcmin, rounded to 0.1.
    """
    radians = math.sqrt(4 * math.pi / (12 * 4 ** order))
    return round(math.degrees(radians) * 60, 1)


def _coverage_map(order: int | None) -> dict[str, Any]:
    """A catalog's coverage-map resolution and what it means for ``searched``."""
    if order is None:
        note = (
            'The coverage-map resolution of this catalog is not configured. '
            'Searched means the position was inside the catalog footprint as '
            'given by its HATS coverage map; objects in footprint holes or near '
            'edges may be recorded as searched.'
        )
        return {'footprint_moc_order': None, 'resolution_arcmin': None, 'note': note}
    resolution = healpix_resolution_arcmin(order)
    note = (
        f'Searched means the position was inside the catalog footprint at the '
        f'resolution of its HATS coverage map (HEALPix order {order}, pixels '
        f'about {resolution:g} arcmin across). Objects in footprint holes or '
        f'near edges smaller than that may be recorded as searched even though '
        f'the catalog has no data there.'
    )
    return {
        'footprint_moc_order': order,
        'resolution_arcmin': resolution,
        'note': note,
    }


def _filterable_field(field: FilterField) -> dict[str, Any]:
    """One filterable catalog property and its filter parameter names."""
    return {
        'name': field.name,
        'unit': field.unit,
        'description': field.description,
        'column': field.column,
        'derived_from': (
            {'numerator': field.numerator, 'denominator': field.denominator}
            if field.column is None else None
        ),
        'parameters': [
            f'{field.catalog}.{field.name}_min', f'{field.catalog}.{field.name}_max',
        ],
    }


def _catalogs() -> list[dict[str, Any]]:
    """Each catalog in service, in configured order."""
    releases = {c['name']: c['release'] for c in provenance.catalog_releases()}
    fields_by_catalog: dict[str, list[dict[str, Any]]] = {}
    for field in filter_fields():
        fields_by_catalog.setdefault(field.catalog, []).append(_filterable_field(field))
    result = []
    for cat in settings.CROSSMATCH_CATALOGS:
        name = str(cat['name'])
        column = str(cat['source_id_column'])
        meaning = _SOURCE_ID_MEANINGS.get(name) or (
            f'The {releases[name]} {column} column: the identifier of the source '
            'in that catalog, served as a string.'
        )
        result.append({
            'name': name,
            'release': releases[name],
            'catalog_source_id': {
                'column': column,
                'description': (
                    f'{meaning} Identifiers are unique only within one '
                    'catalog_name.'
                ),
            },
            'filterable_fields': fields_by_catalog.get(name, []),
            'coverage_map': _coverage_map(cat.get('footprint_moc_order')),
        })
    return result


def _limits() -> dict[str, Any]:
    """The per-request maximums and budget (KTD12), read live."""
    return {
        'request_budget_seconds': float(settings.API_REQUEST_BUDGET_SECONDS),
        'max_ids': int(settings.API_MAX_IDS),
        'max_positions': int(settings.API_MAX_POSITIONS),
        'max_cone_radius_arcsec': float(settings.API_MAX_CONE_RADIUS_ARCSEC),
        'max_objects_per_position': int(settings.API_MAX_OBJECTS_PER_POSITION),
        'max_objects_per_request': int(settings.API_MAX_OBJECTS_PER_REQUEST),
        'recent_crossmatches': {
            'default_page_size': int(settings.RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE),
            'max_page_size': int(settings.RECENT_CROSSMATCH_MAX_PAGE_SIZE),
            'max_window_hours': int(settings.RECENT_CROSSMATCH_MAX_WINDOW_HOURS),
        },
    }


def describe_service() -> dict[str, Any]:
    """The ``api/describe`` body (KTD16; R22, R23).

    Returns:
        A JSON-native dict with ``provenance``, ``catalogs``, ``crossmatch``,
        ``tns``, ``limits``, ``detail_levels``, ``default_detail``,
        ``response_modes``, ``generic_filters``, ``reliability_cuts``, and
        ``provenance_recording_release``.
    """
    generic = {p.name: p for p in filter_parameters() if p.name in GENERIC_FILTERS}
    return {
        'provenance': provenance.service_provenance(),
        'catalogs': _catalogs(),
        'crossmatch': {
            'radius_arcsec': provenance.crossmatch_radius_arcsec(),
            'nearest_source_per_catalog': True,
            'description': _NEAREST_RULE,
        },
        'tns': {
            'default_radius_arcsec': float(settings.TNS_MATCH_RADIUS_ARCSEC),
            'max_radius_arcsec': float(settings.API_MAX_CONE_RADIUS_ARCSEC),
        },
        'limits': _limits(),
        'detail_levels': [
            {'name': level, 'description': DETAIL_LEVEL_DESCRIPTIONS[level]}
            for level in DETAIL_LEVELS
        ],
        'default_detail': DEFAULT_DETAIL,
        'response_modes': [
            {'name': mode, 'description': RESPONSE_MODE_DESCRIPTIONS[mode]}
            for mode in RESPONSE_MODES
        ],
        'generic_filters': [
            {
                'name': param.name,
                'kind': param.kind,
                'bound': param.bound,
                'unit': param.unit,
                'is_match_filter': param.is_match_filter,
                'description': param.description,
            }
            for param in (generic[name] for name in GENERIC_FILTERS)
        ],
        'reliability_cuts': provenance.reliability_cuts(),
        'provenance_recording_release': provenance.PROVENANCE_RECORDING_RELEASE,
    }
