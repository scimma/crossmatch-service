"""The hand-built OpenAPI 3.1 document for the public API (R22, R27; KTD14).

The document is a plain dict assembled per request from the contract codes
(``api/contract.py``, KTD2) and the provenance builder (``core/provenance.py``,
KTD8), so its enums cannot drift from the codes the service emits and its
configuration-derived text (radius, catalog releases, cuts, version) always
describes the running service. It is served at ``/openapi.json``; the test
suite validates it with ``openapi-spec-validator`` and validates every API
response against it (the ``openapi_validate`` fixture in ``conftest.py``).

No runtime dependency: validation tooling is dev-only.

Schema components shared by every operation live under
``components/schemas``; each operation adds its path item in ``_paths``.
Operation IDs match the planned MCP tool names (KTD1).

Every numeric schema carries ``x-unit`` (U8): a physical unit (``deg``,
``arcsec``, ``arcmin``, ``s``, ``h``), ``count``, ``probability``,
``dimensionless``, or, for numbers that are not quantities, ``identifier``,
``index``, ``version``, or ``HEALPix order``.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from api.contract import (
    CATALOG_OUTCOME_VALUES,
    CODE_DESCRIPTIONS,
    CONTRACT_VERSION,
    ErrorCode,
    InputStatus,
    ObjectStatus,
    QualifiesReason,
)
from api.discovery import (
    COINCIDENCE_CAVEAT,
    DATABASE_STATES,
    DETAIL_LEVEL_DESCRIPTIONS,
    NEAREST_SOURCE_RULE,
    NOT_IN_SERVICE_CAVEAT,
    PAGING_CAVEAT,
    RESPONSE_MODE_DESCRIPTIONS,
    STATUS_CHECK_TIMEOUT_SECONDS,
    TNS_RESOLUTION_STATES,
    catalog_source_id_meanings,
    position_conventions,
)
from api.filters import (
    KIND_CATALOG,
    OBJECT_QUALIFIES_REASONS,
    RESPONSE_COUNT,
    RESPONSE_MODES,
    FilterParam,
    filter_parameters,
)
from api.lookup import (
    BASIS_BEST_GUESS,
    BASIS_RECORDED,
    PROVENANCE_NOT_RECORDED,
    PROVENANCE_RECORDED,
)
from api.positions import (
    TRUNCATION_PER_INPUT,
    TRUNCATION_PER_REQUEST,
    TRUNCATION_VALUES,
)
from api.service import DEFAULT_WINDOW_HOURS
from core import provenance
from core.models import CutEnforcedBy, CutStatus

_REF = '#/components/schemas/'

#: ``x-unit`` values.
DEG = 'deg'
ARCSEC = 'arcsec'
COUNT = 'count'


def _ref(name: str) -> dict[str, str]:
    """A JSON reference to a shared schema component."""
    return {'$ref': _REF + name}


def _code_enum(values: list[str], summary: str) -> dict[str, Any]:
    """A string enum whose description defines every value.

    Args:
        values: The code values, in contract order.
        summary: One line introducing the enum.

    Returns:
        The schema, with a Markdown list of ``value``: meaning.
    """
    lines = [summary, '']
    lines.extend(f'- `{value}`: {CODE_DESCRIPTIONS[value]}' for value in values)
    return {'type': 'string', 'enum': list(values), 'description': '\n'.join(lines)}


def _source_id_description() -> str:
    """What ``catalog_source_id`` means, per catalog in service (R23), live."""
    meanings = catalog_source_id_meanings()
    lines = [
        'Source identifier in the catalog named by catalog_name, as a string; '
        'unique only within one catalog_name. Per catalog in service:',
        '',
    ]
    lines.extend(
        f"- `{cat['name']}` (column {cat['source_id_column']}): "
        f"{meanings[str(cat['name'])]}"
        for cat in settings.CROSSMATCH_CATALOGS
    )
    return '\n'.join(lines)


def _separation_schema() -> dict[str, Any]:
    """A match's separation from its Rubin object."""
    return {
        'type': 'number',
        'x-unit': ARCSEC,
        'description': (
            'Angular separation between the Rubin object position and the '
            'catalog source position, arcsec; at most the crossmatch radius.'
        ),
    }


def cuts_text(cuts: list[dict[str, Any]]) -> str:
    """The reliability cut per broker as one sentence fragment (R20, R23).

    Args:
        cuts: ``provenance.reliability_cuts()`` output.

    Returns:
        E.g. ``antares: not declared (broker); pittgoogle: 0.6 (enforced by
        service)``.
    """
    parts = []
    for cut in cuts:
        if cut['min_reliability'] is None:
            value = 'not declared'
        else:
            value = f"{cut['min_reliability']}"
        where = f"enforced by {cut['enforced_by']}, {cut['status']}"
        if cut['as_of']:
            where += f", as of {cut['as_of']}"
        parts.append(f"{cut['broker']}: {value} ({where})")
    return '; '.join(parts)


def _json(schema: dict[str, Any], description: str) -> dict[str, Any]:
    """A response object with an ``application/json`` body."""
    return {
        'description': description,
        'content': {'application/json': {'schema': schema}},
    }


def _provenance_schema(current: dict[str, Any]) -> dict[str, Any]:
    """The service-level provenance component, described with live values."""
    radius = current['crossmatch_radius_arcsec']
    radius_text = f'{radius} arcsec' if radius is not None else 'not configured'
    releases = ', '.join(
        f"{c['name']} ({c['release']})" for c in current['catalogs']
    ) or 'none'
    return {
        'type': 'object',
        'description': (
            'Service-level provenance carried by every response (R17), read '
            'from the live service configuration. On this deployment the '
            f'crossmatch radius is {radius_text}, the catalogs in service '
            f'are: {releases}, and the minimum LSST reliability per broker is: '
            f"{cuts_text(current['reliability_cuts'])}."
        ),
        'required': [
            'service_version',
            'contract_version',
            'crossmatch_radius_arcsec',
            'catalogs',
            'reliability_cuts',
        ],
        'additionalProperties': False,
        'properties': {
            'service_version': {
                'type': ['string', 'null'],
                'description': 'Deployed service version; null if unset.',
            },
            'contract_version': {
                'type': 'string',
                'description': 'Version of this response contract.',
            },
            'crossmatch_radius_arcsec': {
                'type': ['number', 'null'],
                'x-unit': ARCSEC,
                'description': (
                    'Crossmatch radius in arcsec: a catalog source within this '
                    'angular separation of the object position is coincident.'
                ),
            },
            'catalogs': {
                'type': 'array',
                'description': 'Catalogs in service, with their releases.',
                'items': _ref('CatalogRelease'),
            },
            'reliability_cuts': {
                'type': 'array',
                'description': (
                    'The reliability cut per broker and where it is enforced '
                    '(R20). Broker-enforced cuts are declared configuration, '
                    'not observed by this service.'
                ),
                'items': _ref('ReliabilityCut'),
            },
        },
        'example': current,
    }


def _components(current: dict[str, Any]) -> dict[str, Any]:
    """The shared schema components."""
    return {
        'schemas': {
            'DiaObjectId': {
                'type': 'integer',
                'format': 'int64',
                'x-unit': 'identifier',
                'minimum': 0,
                'description': (
                    'Rubin diaObjectId, a 64-bit integer. JavaScript JSON '
                    'parsers lose precision above 2^53; read diaObjectId_str '
                    'there instead.'
                ),
            },
            'DiaObjectIdStr': {
                'type': 'string',
                'pattern': '^[0-9]+$',
                'description': 'The same diaObjectId as a decimal string.',
            },
            'ObjectStatus': _code_enum(
                ObjectStatus.values,
                'What the service knows about one Rubin object. Exactly one '
                'per object.',
            ),
            'InputStatus': _code_enum(
                InputStatus.values,
                'The outcome of one request input (an ID, a position, or a TNS '
                'name). Each object inside the input carries its own '
                'ObjectStatus.',
            ),
            'CatalogOutcome': _code_enum(
                CATALOG_OUTCOME_VALUES,
                'How one catalog was searched for one crossmatched object. '
                '"No coincident source" applies only to catalogs with outcome '
                '`searched`.',
            ),
            'ErrorCode': _code_enum(ErrorCode.values, 'Request-level error code.'),
            'QualifiesReason': _code_enum(
                QualifiesReason.values,
                'Why an object or input does or does not qualify under the '
                "request's filters. Reasons that restate a status reuse its code.",
            ),
            'CutEnforcedBy': _code_enum(
                CutEnforcedBy.values, "Where a broker's reliability cut is enforced."
            ),
            'CutStatus': _code_enum(
                CutStatus.values,
                "How the reported value of a broker's reliability cut is known.",
            ),
            'CatalogRelease': {
                'type': 'object',
                'required': ['name', 'release'],
                'additionalProperties': False,
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': 'Catalog name as used in catalog_name.',
                    },
                    'release': {
                        'type': 'string',
                        'description': 'Catalog release in service.',
                    },
                },
            },
            'ReliabilityCut': {
                'type': 'object',
                'required': [
                    'broker', 'min_reliability', 'enforced_by', 'status', 'as_of',
                ],
                'additionalProperties': False,
                'properties': {
                    'broker': {
                        'type': 'string',
                        'description': 'Broker identifier (e.g. antares).',
                    },
                    'min_reliability': {
                        'type': ['number', 'null'],
                        'x-unit': 'probability',
                        'minimum': 0,
                        'maximum': 1,
                        'description': (
                            'Minimum LSST real/bogus reliability of the latest '
                            'diaSource; null when not declared.'
                        ),
                    },
                    'enforced_by': _ref('CutEnforcedBy'),
                    'status': _ref('CutStatus'),
                    'as_of': {
                        'type': ['string', 'null'],
                        'format': 'date',
                        'description': (
                            'ISO date the maintainer declared a broker-enforced '
                            'cut; null otherwise.'
                        ),
                    },
                },
            },
            'Provenance': _provenance_schema(current),
            'Envelope': {
                'type': 'object',
                'description': 'Fields every successful response carries.',
                'required': ['provenance'],
                'properties': {'provenance': _ref('Provenance')},
            },
            'Error': {
                'type': 'object',
                'description': (
                    'A request-level error. `error` repeats `message` for '
                    'clients of the original error body.'
                ),
                'required': ['error', 'code', 'message', 'retryable'],
                'additionalProperties': False,
                'properties': {
                    'error': {'type': 'string', 'description': 'Same as message.'},
                    'code': _ref('ErrorCode'),
                    'message': {'type': 'string'},
                    'param': {
                        'type': 'string',
                        'description': 'The offending parameter.',
                    },
                    'params': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'The offending parameters, when several.',
                    },
                    'retryable': {
                        'type': 'boolean',
                        'description': 'Whether retrying the same request can help.',
                    },
                },
            },
            **_recent_crossmatches_schemas(),
            **_lookup_schemas(),
            **_filter_schemas(),
            **_discovery_schemas(),
        },
    }


def _recent_crossmatches_schemas() -> dict[str, Any]:
    """Schemas of the recent-crossmatches response: its original fields plus the
    contract additions (``provenance``, ``as_of``), which change none of them."""
    source_id = _source_id_description()
    positions = position_conventions()
    return {
        'MatchSummary': {
            'type': 'object',
            'description': f'A coincident source. {NEAREST_SOURCE_RULE}',
            'required': ['catalog_name', 'catalog_source_id', 'separation_arcsec'],
            'properties': {
                'catalog_name': {
                    'type': 'string',
                    'description': 'The catalog the source is from.',
                },
                'catalog_source_id': {
                    'type': 'string',
                    'description': source_id,
                },
                'separation_arcsec': _separation_schema(),
            },
        },
        'PublishedMatch': {
            'type': 'object',
            'description': (
                'A match built by the same payload builder as the Hopskotch '
                'stream. ra/dec here are the catalog source position. '
                + NEAREST_SOURCE_RULE
            ),
            'required': [
                'diaObjectId', 'ra', 'dec', 'catalog_name', 'catalog_source_id',
                'separation_arcsec', 'catalog_payload', 'catalogs_skipped',
                'partial', 'tns_checked', 'tns_snapshot_epoch',
            ],
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'ra': {
                    'type': 'number',
                    'x-unit': DEG,
                    'description': f'Catalog source RA, degrees. {positions}',
                },
                'dec': {
                    'type': 'number',
                    'x-unit': DEG,
                    'description': f'Catalog source Dec, degrees. {positions}',
                },
                'catalog_name': {
                    'type': 'string',
                    'description': 'The catalog the source is from.',
                },
                'catalog_source_id': {'type': 'string', 'description': source_id},
                'separation_arcsec': _separation_schema(),
                'catalog_payload': {
                    'type': ['object', 'null'],
                    'description': 'Raw catalog columns, lowercased keys.',
                },
                'catalogs_skipped': {'type': 'array', 'items': {'type': 'string'}},
                'partial': {'type': 'boolean'},
                'tns_checked': {'type': 'boolean'},
                'tns_snapshot_epoch': {'type': ['string', 'null']},
                'tns': {'type': 'object'},
            },
        },
        'RecentObject': {
            'type': 'object',
            'description': 'One object; fields grow with the detail level.',
            'required': ['diaObjectId', 'diaObjectId_str'],
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'diaObjectId_str': _ref('DiaObjectIdStr'),
                'ra': {
                    'type': ['number', 'null'],
                    'x-unit': DEG,
                    'description': 'Object RA, degrees (ICRS).',
                },
                'dec': {
                    'type': ['number', 'null'],
                    'x-unit': DEG,
                    'description': 'Object Dec, degrees (ICRS).',
                },
                'matches': {
                    'type': 'array',
                    'items': {
                        'anyOf': [_ref('MatchSummary'), _ref('PublishedMatch')],
                    },
                },
            },
        },
        'RecentCrossmatchesPage': {
            'type': 'object',
            'required': [
                'provenance', 'window', 'time_field', 'detail', 'page_size',
                'count', 'next_cursor', 'objects', 'as_of',
            ],
            'properties': {
                'provenance': _ref('Provenance'),
                'window': {
                    'type': 'object',
                    'required': ['start', 'end'],
                    'properties': {
                        'start': {'type': 'string', 'format': 'date-time'},
                        'end': {'type': 'string', 'format': 'date-time'},
                    },
                },
                'time_field': {'type': 'string', 'enum': ['ingest_time', 'event_time']},
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                },
                'page_size': {'type': 'integer', 'x-unit': COUNT, 'minimum': 1},
                'count': {
                    'type': 'integer',
                    'x-unit': COUNT,
                    'minimum': 0,
                    'description': 'Objects on this page, not a total.',
                },
                'next_cursor': {
                    'type': ['string', 'null'],
                    'description': (
                        'Opaque token for the next page; null when the window '
                        'is exhausted. It pins the window, time_field, detail, '
                        'and as_of.'
                    ),
                },
                'objects': {'type': 'array', 'items': _ref('RecentObject')},
                'as_of': {
                    'type': 'string',
                    'format': 'date-time',
                    'description': (
                        'The walk is pinned to alerts ingested at or before this '
                        'time, on every page of one walk (set by the first page; '
                        'a cursor issued before this field existed pins at the '
                        'request that presents it). ' + PAGING_CAVEAT + ' Here '
                        'that means an object in the pinned set whose first '
                        'match lands mid-walk can appear on a later page.'
                    ),
                },
            },
        },
    }


def _lookup_schemas() -> dict[str, Any]:
    """Schemas of the object lookups (get_object, lookup_objects; U4)."""
    recording_release = provenance.PROVENANCE_RECORDING_RELEASE
    provenance_set_ref = {
        'type': ['string', 'null'],
        'description': (
            'Key into the response provenance_sets map of the provenance '
            'recorded for this crossmatch; null when not recorded.'
        ),
    }
    with_provenance_set = {
        'type': 'object',
        'required': ['provenance_set'],
        'properties': {'provenance_set': provenance_set_ref},
    }
    radius_max = float(settings.API_MAX_CONE_RADIUS_ARCSEC)
    radius_max_text = f'At most {radius_max:g} arcsec.'
    tns_radius = float(settings.TNS_MATCH_RADIUS_ARCSEC)
    ra_schema = {
        'type': 'number',
        'x-unit': DEG,
        'minimum': 0,
        'maximum': 360,
        'description': 'Right ascension, degrees (ICRS).',
    }
    dec_schema = {
        'type': 'number',
        'x-unit': DEG,
        'minimum': -90,
        'maximum': 90,
        'description': 'Declination, degrees (ICRS).',
    }
    radius_schema = {
        'type': 'number',
        'x-unit': ARCSEC,
        'exclusiveMinimum': 0,
        'maximum': radius_max,
    }
    tns_name_schema = {
        'type': 'string',
        'maxLength': 64,
        'description': (
            'A TNS name such as 2026abc, SN 2026abc, or AT2026abc. Whitespace '
            'and an SN/AT prefix are ignored and case does not matter.'
        ),
    }
    cone_caveat = (
        'Position searches cover every Rubin object the service has seen, '
        'crossmatched or not; an object whose position could not be indexed '
        'at ingest (null HEALPix index) is not reachable by position. '
        + COINCIDENCE_CAVEAT
    )
    cone_counts = {
        'total': {
            'type': ['integer', 'null'],
            'x-unit': COUNT,
            'minimum': 0,
            'description': (
                'Rubin objects within the radius; null when no search ran.'
            ),
        },
        'truncated': {
            'type': 'boolean',
            'description': 'Whether objects lists fewer than total.',
        },
        'truncation': {
            'description': (
                'Why objects was cut: '
                f'`{TRUNCATION_PER_INPUT}` (the per-input maximum of '
                f'{_int_setting("API_MAX_OBJECTS_PER_POSITION")} objects; run '
                'a cone search to page the rest) or '
                f'`{TRUNCATION_PER_REQUEST}` (the per-request total of '
                f'{_int_setting("API_MAX_OBJECTS_PER_REQUEST")} objects, spent '
                'in input order). Null when not cut.'
            ),
            'anyOf': [
                {'type': 'null'},
                {'type': 'string', 'enum': list(TRUNCATION_VALUES)},
            ],
        },
    }
    qualification = {
        'qualifies': {
            'type': 'boolean',
            'description': (
                'Present only when the request has filters: whether this '
                'qualifies. Nothing is dropped for not qualifying.'
            ),
        },
        'qualifies_reason': {
            **_ref('QualifiesReason'),
            'description': 'Present only when the request has filters.',
        },
    }
    qualifying_total = {
        'qualifying_total': {
            'type': ['integer', 'null'],
            'x-unit': COUNT,
            'minimum': 0,
            'description': (
                'Present only when the request has filters: qualifying Rubin '
                'objects among all objects within the radius (listed or not); '
                'null when no search ran.'
            ),
        },
    }
    filters_echo = {
        'filters': {
            **_ref('Filters'),
            'description': 'Present only when the request has filters: the '
            'filters applied, normalized.',
        },
    }
    truncated_top = {
        'type': 'boolean',
        'description': (
            'Whether the per-request object total cut any result; those '
            f'results carry truncation `{TRUNCATION_PER_REQUEST}`.'
        ),
    }
    return {
        'IdInput': {
            'type': 'object',
            'description': 'Look up one Rubin object by diaObjectId.',
            'required': ['kind', 'diaObjectId'],
            'additionalProperties': False,
            'properties': {
                'kind': {'const': 'id'},
                'diaObjectId': {
                    'description': (
                        'The diaObjectId as a JSON integer or a decimal string. '
                        'Send a string from JavaScript clients, which lose '
                        'precision above 2^53.'
                    ),
                    'oneOf': [
                        _ref('DiaObjectId'),
                        _ref('DiaObjectIdStr'),
                    ],
                },
            },
        },
        'PositionInput': {
            'type': 'object',
            'description': (
                'Search one position for every Rubin object the service has '
                'seen within the radius.'
            ),
            'required': ['kind', 'ra', 'dec'],
            'additionalProperties': False,
            'properties': {
                'kind': {'const': 'position'},
                'ra': ra_schema,
                'dec': dec_schema,
                'radius_arcsec': {
                    **radius_schema,
                    'description': (
                        'Search radius, arcsec. Required unless the request '
                        'carries a shared radius_arcsec. ' + radius_max_text
                    ),
                },
            },
        },
        'TnsInput': {
            'type': 'object',
            'description': (
                'Look up a TNS name: the name is resolved through the TNS '
                'snapshot and answered as a position search around the TNS '
                'position.'
            ),
            'required': ['kind', 'name'],
            'additionalProperties': False,
            'properties': {
                'kind': {'const': 'tns'},
                'name': tns_name_schema,
                'radius_arcsec': {
                    **radius_schema,
                    'description': (
                        'Search radius around the TNS position, arcsec. '
                        'Default: the request radius_arcsec, else the TNS '
                        f'association radius ({tns_radius:g} arcsec). '
                        + radius_max_text
                    ),
                },
            },
        },
        'LookupInput': {
            'description': (
                'One tagged input, discriminated by kind. An entry that does '
                'not match is not a request error: it gets its own result with '
                'status invalid_input.'
            ),
            'oneOf': [_ref('IdInput'), _ref('PositionInput'), _ref('TnsInput')],
        },
        'LookupRequest': {
            'type': 'object',
            'required': ['inputs'],
            'additionalProperties': False,
            'properties': {
                'inputs': {
                    'type': 'array',
                    'minItems': 1,
                    'description': (
                        'The inputs, answered one result per input in this '
                        'order; duplicates are answered twice. Per-request '
                        'maximums (currently: IDs '
                        f'{_int_setting("API_MAX_IDS")}; positions and TNS '
                        f'names together {_int_setting("API_MAX_POSITIONS")}) '
                        'apply; over a maximum the request is a 400 naming '
                        'inputs. A position or TNS input lists at most '
                        f'{_int_setting("API_MAX_OBJECTS_PER_POSITION")} '
                        'objects, and the request at most '
                        f'{_int_setting("API_MAX_OBJECTS_PER_REQUEST")} objects '
                        'in all, spent in input order.'
                    ),
                    'items': _ref('LookupInput'),
                },
                'radius_arcsec': {
                    **radius_schema,
                    'description': (
                        'Shared search radius, arcsec, for position and TNS '
                        'inputs that carry none. ' + radius_max_text
                    ),
                },
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                    'description': 'Cumulative detail level (default matches).',
                },
                'filters': {
                    **_ref('Filters'),
                    'description': (
                        'Filters, as {name: value}. With filters every result '
                        'and object is marked qualifies with a '
                        'qualifies_reason; nothing is dropped.'
                    ),
                },
                'response': _response_mode_schema(),
            },
        },
        'ProvenanceSet': {
            'type': 'object',
            'description': (
                'The conditions a crossmatch ran under. basis `recorded`: '
                'written when the object was crossmatched (R18). basis '
                f'`{BASIS_BEST_GUESS}`: the current settings, offered for '
                'objects without recorded provenance and labeled as a best '
                'guess, not a record (R19).'
            ),
            'required': [
                'basis', 'crossmatch_radius_arcsec', 'catalogs', 'reliability_cuts',
            ],
            'additionalProperties': False,
            'properties': {
                'basis': {'type': 'string', 'enum': [BASIS_RECORDED, BASIS_BEST_GUESS]},
                'description': {'type': 'string'},
                'crossmatch_radius_arcsec': {
                    'type': ['number', 'null'],
                    'x-unit': ARCSEC,
                    'description': 'Crossmatch radius, arcsec.',
                },
                'catalogs': {'type': 'array', 'items': _ref('CatalogRelease')},
                'reliability_cuts': {
                    'type': 'array',
                    'items': _ref('ReliabilityCut'),
                },
            },
        },
        'Crossmatch': {
            'type': 'object',
            'description': (
                'What the crossmatch of this object searched, and under which '
                'provenance. Without a record (provenance '
                f'`{PROVENANCE_NOT_RECORDED}`) every catalog reports '
                '`not_recorded`; recording started with service release '
                f'{recording_release}, but a missing record does not assert '
                'that the object predates it.'
            ),
            'required': [
                'provenance', 'match_version', 'crossmatched_at', 'provenance_set',
                'brokers_at_crossmatch', 'catalog_outcomes',
            ],
            'additionalProperties': False,
            'properties': {
                'provenance': {
                    'type': 'string',
                    'enum': [PROVENANCE_RECORDED, PROVENANCE_NOT_RECORDED],
                },
                'match_version': {
                    'type': ['integer', 'null'],
                    'x-unit': 'version',
                    'description': (
                        'Version of the stored crossmatch result; it increases '
                        'when the object is crossmatched again.'
                    ),
                },
                'crossmatched_at': {
                    'type': ['string', 'null'],
                    'format': 'date-time',
                    'description': 'When the crossmatch result was committed.',
                },
                'provenance_set': provenance_set_ref,
                'brokers_at_crossmatch': {
                    'type': ['array', 'null'],
                    'items': {'type': 'string'},
                    'description': (
                        'Brokers that had delivered the object when it was '
                        'crossmatched. A broker that delivered it later is in '
                        'the object brokers list only.'
                    ),
                },
                'catalog_outcomes': {
                    'type': 'object',
                    'description': (
                        'Search outcome per catalog: every catalog in service '
                        'now, plus any recorded catalog no longer in service.'
                    ),
                    'additionalProperties': _ref('CatalogOutcome'),
                },
                'recording_release': {
                    'type': 'string',
                    'description': (
                        'Present when not recorded: the service release that '
                        'started recording provenance.'
                    ),
                },
                'best_guess_provenance_set': {
                    'type': 'string',
                    'description': (
                        'Present when not recorded: key of the labeled '
                        'best-guess entry in provenance_sets.'
                    ),
                },
            },
        },
        'LookupMatch': {
            'description': (
                'A coincident source (raw catalog values at detail full). '
                f'{NEAREST_SOURCE_RULE} {COINCIDENCE_CAVEAT}'
            ),
            'anyOf': [
                {'allOf': [_ref('MatchSummary'), with_provenance_set]},
                {'allOf': [_ref('PublishedMatch'), with_provenance_set]},
            ],
        },
        'LookupObject': {
            'type': 'object',
            'description': (
                'One Rubin object. An object not in the service carries only '
                'its ID and status. Position, reliability, times, and brokers '
                'appear from detail position; matches and tns from detail '
                'matches.'
            ),
            'required': ['diaObjectId', 'diaObjectId_str', 'status'],
            'additionalProperties': False,
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'diaObjectId_str': _ref('DiaObjectIdStr'),
                'status': _ref('ObjectStatus'),
                'ra': {
                    'type': ['number', 'null'],
                    'x-unit': DEG,
                    'description': 'Object RA, degrees (ICRS).',
                },
                'dec': {
                    'type': ['number', 'null'],
                    'x-unit': DEG,
                    'description': 'Object Dec, degrees (ICRS).',
                },
                'reliability': {
                    'type': ['number', 'null'],
                    'x-unit': 'probability',
                    'description': (
                        'LSST real/bogus reliability stored when the object '
                        'was first seen. Alerts below the per-broker cut in '
                        'provenance.reliability_cuts never reach the service.'
                    ),
                },
                'ingest_time': {
                    'type': 'string',
                    'format': 'date-time',
                    'description': 'When the service first ingested the object.',
                },
                'event_time': {
                    'type': 'string',
                    'format': 'date-time',
                    'description': 'Observation time of the first ingested alert.',
                },
                'brokers': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Every broker that has delivered the object.',
                },
                'crossmatch': {
                    'description': 'Null until the object is crossmatched.',
                    'anyOf': [{'type': 'null'}, _ref('Crossmatch')],
                },
                'matches': {
                    'type': 'array',
                    'description': (
                        'Coincident sources; empty unless status is '
                        'coincident_sources.'
                    ),
                    'items': _ref('LookupMatch'),
                },
                'tns': {
                    'description': (
                        'The stored TNS association; null when the object has '
                        'none (crossmatched before TNS association was added, '
                        'or not yet crossmatched).'
                    ),
                    'anyOf': [{'type': 'null'}, _ref('ObjectTns')],
                },
                'separation_arcsec': {
                    'type': 'number',
                    'x-unit': ARCSEC,
                    'minimum': 0,
                    'description': (
                        'Present on position and TNS results only: angular '
                        'separation of the object position from the searched '
                        'position, arcsec.'
                    ),
                },
                **qualification,
            },
        },
        'IdResult': {
            'type': 'object',
            'description': 'The result of an id input: its object and that object\'s status.',
            'required': ['index', 'kind', 'input', 'normalized', 'status', 'objects'],
            'additionalProperties': False,
            'properties': {
                'index': {
                    'type': 'integer',
                    'x-unit': 'index',
                    'minimum': 0,
                    'description': 'Position of the input in the request, from 0.',
                },
                'kind': {'const': 'id'},
                'input': {'description': 'The input exactly as sent.'},
                'normalized': {
                    'type': 'object',
                    'required': ['kind', 'diaObjectId', 'diaObjectId_str'],
                    'additionalProperties': False,
                    'properties': {
                        'kind': {'const': 'id'},
                        'diaObjectId': _ref('DiaObjectId'),
                        'diaObjectId_str': _ref('DiaObjectIdStr'),
                    },
                },
                'status': _ref('ObjectStatus'),
                'objects': {
                    'type': 'array',
                    'minItems': 1,
                    'maxItems': 1,
                    'items': _ref('LookupObject'),
                },
                **qualification,
            },
        },
        'TnsRecord': {
            'type': 'object',
            'description': 'The TNS object a name resolved to, from the TNS snapshot.',
            'required': [
                'objid', 'name', 'name_prefix', 'ra', 'dec', 'classification',
                'redshift', 'url',
            ],
            'additionalProperties': False,
            'properties': {
                'objid': {
                    'type': 'integer',
                    'x-unit': 'identifier',
                    'description': 'TNS internal object id.',
                },
                'name': {
                    'type': 'string',
                    'description': 'Bare TNS designation, as TNS stores it.',
                },
                'name_prefix': {
                    'type': ['string', 'null'],
                    'description': 'TNS name prefix, e.g. SN or AT.',
                },
                'ra': {
                    'type': 'number',
                    'x-unit': DEG,
                    'description': 'TNS RA, degrees, as TNS reports it.',
                },
                'dec': {
                    'type': 'number',
                    'x-unit': DEG,
                    'description': 'TNS Dec, degrees, as TNS reports it.',
                },
                'classification': {
                    'type': ['string', 'null'],
                    'description': 'TNS classification, e.g. SN Ia; null if none.',
                },
                'redshift': {
                    'type': ['number', 'null'],
                    'x-unit': 'dimensionless',
                    'description': 'TNS redshift; null if none.',
                },
                'url': {'type': 'string', 'description': 'The TNS object page.'},
            },
        },
        'ObjectTns': {
            'type': 'object',
            'description': (
                'The TNS association stored when the object was crossmatched, '
                'as of the TNS snapshot it was checked against '
                '(snapshot_epoch). It is not refreshed: a TNS object named '
                'after that snapshot is not shown, so null name fields mean '
                'no TNS object within the association radius as of that '
                'snapshot, not that none exists now. checked is false, with a '
                'null snapshot_epoch, when no TNS snapshot was current at '
                'crossmatch.'
            ),
            'required': [
                'checked', 'snapshot_epoch', 'name', 'name_prefix',
                'classification', 'redshift', 'separation_arcsec', 'url',
            ],
            'additionalProperties': False,
            'properties': {
                'checked': {
                    'type': 'boolean',
                    'description': (
                        'Whether the object was checked against a current '
                        'TNS snapshot.'
                    ),
                },
                'snapshot_epoch': {
                    'type': ['string', 'null'],
                    'format': 'date-time',
                    'description': (
                        'Epoch of the TNS snapshot the object was checked '
                        'against; null unless checked.'
                    ),
                },
                'name': {
                    'type': ['string', 'null'],
                    'description': (
                        'Bare TNS designation of the associated TNS object; '
                        'null if none.'
                    ),
                },
                'name_prefix': {
                    'type': ['string', 'null'],
                    'description': 'TNS name prefix, e.g. SN or AT.',
                },
                'classification': {
                    'type': ['string', 'null'],
                    'description': 'TNS classification, e.g. SN Ia; null if none.',
                },
                'redshift': {
                    'type': ['number', 'null'],
                    'x-unit': 'dimensionless',
                    'description': 'TNS redshift; null if none.',
                },
                'separation_arcsec': {
                    'type': ['number', 'null'],
                    'x-unit': ARCSEC,
                    'minimum': 0,
                    'description': (
                        'Angular separation of the Rubin object position from '
                        'the TNS object position, arcsec; null if none.'
                    ),
                },
                'url': {
                    'type': ['string', 'null'],
                    'description': 'The TNS object page; null if none.',
                },
            },
        },
        'PositionResult': {
            'type': 'object',
            'description': (
                'The result of a position input: every Rubin object the '
                'service has seen within the radius, each with its own '
                'status, in (ingest_time, diaObjectId) order. '
                + cone_caveat
            ),
            'required': [
                'index', 'kind', 'input', 'normalized', 'status', 'objects',
                'total', 'truncated', 'truncation',
            ],
            'additionalProperties': False,
            'properties': {
                'index': {
                    'type': 'integer',
                    'x-unit': 'index',
                    'minimum': 0,
                    'description': 'Position of the input in the request, from 0.',
                },
                'kind': {'const': 'position'},
                'input': {'description': 'The input exactly as sent.'},
                'normalized': {
                    'type': 'object',
                    'required': ['kind', 'ra', 'dec', 'radius_arcsec'],
                    'additionalProperties': False,
                    'properties': {
                        'kind': {'const': 'position'},
                        'ra': {
                            'type': 'number',
                            'x-unit': DEG,
                            'description': 'RA, degrees (ICRS).',
                        },
                        'dec': {
                            'type': 'number',
                            'x-unit': DEG,
                            'description': 'Dec, degrees (ICRS).',
                        },
                        'radius_arcsec': {
                            'type': 'number',
                            'x-unit': ARCSEC,
                            'description': 'The radius searched, arcsec.',
                        },
                    },
                },
                'status': _code_enum(
                    [InputStatus.NO_RUBIN_OBJECT.value, InputStatus.OBJECTS_FOUND.value],
                    'Whether any Rubin object lies within the radius.',
                ),
                'objects': {'type': 'array', 'items': _ref('LookupObject')},
                **cone_counts,
                **qualification,
                **qualifying_total,
            },
        },
        'TnsResult': {
            'type': 'object',
            'description': (
                'The result of a TNS input. When the name resolves, it is '
                'answered as a position search around the TNS position and '
                'echoes the TNS record and snapshot epoch; an unknown name '
                'reports tns_name_not_found with the epoch, and a stale or '
                'absent snapshot reports resolver_unavailable. ' + cone_caveat
            ),
            'required': [
                'index', 'kind', 'input', 'normalized', 'status', 'objects',
                'total', 'truncated', 'truncation', 'tns', 'tns_snapshot_epoch',
            ],
            'additionalProperties': False,
            'properties': {
                'index': {
                    'type': 'integer',
                    'x-unit': 'index',
                    'minimum': 0,
                    'description': 'Position of the input in the request, from 0.',
                },
                'kind': {'const': 'tns'},
                'input': {'description': 'The input exactly as sent.'},
                'normalized': {
                    'type': 'object',
                    'required': ['kind', 'name', 'radius_arcsec'],
                    'additionalProperties': False,
                    'properties': {
                        'kind': {'const': 'tns'},
                        'name': {
                            'type': 'string',
                            'description': (
                                'The bare designation, lowercased, without '
                                'SN/AT prefix or whitespace; matched against '
                                'the lowercased TNS name.'
                            ),
                        },
                        'radius_arcsec': {
                            'type': 'number',
                            'x-unit': ARCSEC,
                            'description': 'The radius searched, arcsec.',
                        },
                    },
                },
                'status': _code_enum(
                    [
                        InputStatus.NO_RUBIN_OBJECT.value,
                        InputStatus.OBJECTS_FOUND.value,
                        InputStatus.TNS_NAME_NOT_FOUND.value,
                        InputStatus.RESOLVER_UNAVAILABLE.value,
                    ],
                    'The outcome of the TNS input.',
                ),
                'objects': {'type': 'array', 'items': _ref('LookupObject')},
                **cone_counts,
                'tns': {
                    'description': 'The TNS record; null unless the name resolved.',
                    'anyOf': [{'type': 'null'}, _ref('TnsRecord')],
                },
                'tns_snapshot_epoch': {
                    'type': ['string', 'null'],
                    'format': 'date-time',
                    'description': (
                        'Epoch of the TNS snapshot the name was resolved '
                        'against (R21); null when resolver_unavailable.'
                    ),
                },
                **qualification,
                **qualifying_total,
            },
        },
        'InvalidInputResult': {
            'type': 'object',
            'description': (
                'A malformed input. Only this entry fails; the rest of the '
                'request is answered.'
            ),
            'required': [
                'index', 'kind', 'input', 'normalized', 'status', 'objects', 'error',
            ],
            'additionalProperties': False,
            'properties': {
                'index': {
                    'type': 'integer',
                    'x-unit': 'index',
                    'minimum': 0,
                    'description': 'Position of the input in the request, from 0.',
                },
                'kind': {
                    'type': ['string', 'null'],
                    'description': 'The kind as sent, when it was a string.',
                },
                'input': {'description': 'The input exactly as sent.'},
                'normalized': {'type': 'null'},
                'status': {'const': 'invalid_input'},
                'objects': {'type': 'array', 'maxItems': 0},
                'error': {
                    'type': 'object',
                    'required': ['message', 'param'],
                    'additionalProperties': False,
                    'properties': {
                        'message': {'type': 'string'},
                        'param': {
                            'type': 'string',
                            'description': 'The offending field, e.g. inputs[3].diaObjectId.',
                        },
                    },
                },
                **qualification,
            },
        },
        'LookupResult': {
            'description': 'One result per input, in input order.',
            'oneOf': [
                _ref('IdResult'), _ref('PositionResult'), _ref('TnsResult'),
                _ref('InvalidInputResult'),
            ],
        },
        'LookupResponse': {
            'type': 'object',
            'required': [
                'provenance', 'detail', 'count', 'provenance_sets', 'truncated',
                'results',
            ],
            'additionalProperties': False,
            'properties': {
                'truncated': truncated_top,
                'provenance': _ref('Provenance'),
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                },
                'count': {
                    'type': 'integer',
                    'x-unit': COUNT,
                    'minimum': 0,
                    'description': 'Number of results (equal to the number of inputs).',
                },
                'provenance_sets': {
                    'type': 'object',
                    'description': (
                        'Provenance sets referenced by the results, keyed by '
                        'the keys objects and matches carry.'
                    ),
                    'additionalProperties': _ref('ProvenanceSet'),
                },
                'results': {'type': 'array', 'items': _ref('LookupResult')},
                **filters_echo,
            },
        },
        'ConeSearchResponse': {
            'type': 'object',
            'description': (
                'One page of a single cone search: the lookup response shape '
                'with one position result, whose objects are this page.'
            ),
            'required': [
                'provenance', 'detail', 'count', 'provenance_sets', 'truncated',
                'results', 'as_of', 'page_size', 'next_cursor',
            ],
            'additionalProperties': False,
            'properties': {
                'provenance': _ref('Provenance'),
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                },
                'count': {'const': 1, 'description': 'Number of results.'},
                'provenance_sets': {
                    'type': 'object',
                    'additionalProperties': _ref('ProvenanceSet'),
                },
                'truncated': {
                    'const': False,
                    'description': 'Always false: a single cone pages instead.',
                },
                'results': {
                    'type': 'array',
                    'minItems': 1,
                    'maxItems': 1,
                    'items': _ref('PositionResult'),
                },
                'as_of': {
                    'type': 'string',
                    'format': 'date-time',
                    'description': (
                        'The object set is pinned to objects ingested at or '
                        'before this time, on every page of one walk. '
                        + PAGING_CAVEAT
                    ),
                },
                'page_size': {'type': 'integer', 'x-unit': COUNT, 'minimum': 1},
                'next_cursor': {
                    'type': ['string', 'null'],
                    'description': 'Opaque token for the next page; null on the last.',
                },
                **filters_echo,
            },
        },
    }


def _filter_value_schema(param: FilterParam) -> dict[str, Any]:
    """The value schema of one filter parameter, described with its unit."""
    if param.kind == KIND_CATALOG:
        names = [cat['name'] for cat in settings.CROSSMATCH_CATALOGS]
        return {'type': 'string', 'enum': names, 'description': param.description}
    schema: dict[str, Any] = {'type': 'number', 'description': param.description}
    if param.unit is not None:
        schema['x-unit'] = param.unit
    return schema


def _filter_schemas() -> dict[str, Any]:
    """Schemas of the filters and of the ``response=count`` body (U6)."""
    status_counts = {
        'type': 'object',
        'description': 'Count per status; every status is present, zero-filled.',
        'additionalProperties': {'type': 'integer', 'x-unit': COUNT, 'minimum': 0},
    }
    qualifying = {
        'type': ['integer', 'null'],
        'x-unit': COUNT,
        'minimum': 0,
        'description': 'How many qualify under the filters; null without filters.',
    }
    return {
        'Filters': {
            'type': 'object',
            'description': (
                'Filters (R13, R14). Bounds are inclusive. Catalog property '
                'filters are named <catalog>.<column>_min|_max with the column '
                'lowercased; they, catalog, and separation_arcsec_max are match '
                'filters, and an object qualifies only if one of its current '
                'coincident sources satisfies every match filter, so match '
                'filters may name only one catalog (else 400 '
                'filters_span_catalogs). reliability_min|_max are object '
                'filters. A null or non-numeric stored value never qualifies. '
                'An unknown filter name is a 400 naming it.'
            ),
            'additionalProperties': False,
            'properties': {
                param.name: _filter_value_schema(param) for param in filter_parameters()
            },
        },
        'CountResponse': {
            'type': 'object',
            'description': (
                'The response=count body (R15): counts for the same inputs and '
                'filters as the listing, without objects and uncapped by the '
                'listing limits.'
            ),
            'required': ['provenance', 'response', 'filters', 'counts'],
            'additionalProperties': False,
            'properties': {
                'provenance': _ref('Provenance'),
                'response': {'const': RESPONSE_COUNT},
                'filters': {
                    'description': 'The filters applied, normalized; null when none.',
                    'anyOf': [{'type': 'null'}, _ref('Filters')],
                },
                'counts': {
                    'type': 'object',
                    'required': ['inputs', 'objects'],
                    'additionalProperties': False,
                    'properties': {
                        'inputs': {
                            'type': 'object',
                            'description': (
                                'Per input, duplicates included. by_status keys '
                                'are ObjectStatus (id inputs) and InputStatus '
                                'values.'
                            ),
                            'required': ['total', 'by_status', 'qualifying'],
                            'additionalProperties': False,
                            'properties': {
                                'total': {
                                    'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                                },
                                'by_status': status_counts,
                                'qualifying': qualifying,
                            },
                        },
                        'objects': {
                            'type': 'object',
                            'description': (
                                'Per distinct Rubin object the inputs reach '
                                '(IDs, and every object within each radius). '
                                'by_status keys are ObjectStatus values.'
                            ),
                            'required': [
                                'total', 'by_status', 'qualifying',
                                'by_qualifies_reason',
                            ],
                            'additionalProperties': False,
                            'properties': {
                                'total': {
                                    'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                                },
                                'by_status': status_counts,
                                'qualifying': qualifying,
                                'by_qualifies_reason': {
                                    'type': ['object', 'null'],
                                    'description': (
                                        'Count per QualifiesReason, zero-filled '
                                        f'({", ".join(OBJECT_QUALIFIES_REASONS)}); '
                                        'null without filters.'
                                    ),
                                    'additionalProperties': {
                                        'type': 'integer', 'x-unit': COUNT,
                                        'minimum': 0,
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def _discovery_schemas() -> dict[str, Any]:
    """Schemas of the status and describe bodies (U7, KTD16)."""
    nullable_order = {
        'type': ['integer', 'null'],
        'x-unit': 'HEALPix order',
        'minimum': 0,
        'maximum': 29,
    }
    detail_lines = '; '.join(
        f'{name}: {text}' for name, text in DETAIL_LEVEL_DESCRIPTIONS.items()
    )
    mode_lines = '; '.join(
        f'{name}: {text}' for name, text in RESPONSE_MODE_DESCRIPTIONS.items()
    )
    named = {
        'type': 'object',
        'required': ['name', 'description'],
        'additionalProperties': False,
        'properties': {
            'name': {'type': 'string'},
            'description': {'type': 'string'},
        },
    }
    return {
        'ServiceStatus': {
            'type': 'object',
            'description': (
                'Service availability (R30). Always returned with 200: a '
                'database that cannot be reached, or does not answer a trivial '
                f'query within {STATUS_CHECK_TIMEOUT_SECONDS:g} s, is reported '
                'as database: unavailable, not as an error.'
            ),
            'required': [
                'provenance', 'database', 'service_version', 'contract_version',
                'tns_resolution', 'tns_snapshot_epoch',
            ],
            'additionalProperties': False,
            'properties': {
                'provenance': _ref('Provenance'),
                'database': {
                    'type': 'string',
                    'enum': list(DATABASE_STATES),
                    'description': (
                        'ok: the database answered. unavailable: it did not; '
                        'queries will fail with 503 service_unavailable until '
                        'it recovers.'
                    ),
                },
                'service_version': {
                    'type': ['string', 'null'],
                    'description': 'Deployed service version; null if unset.',
                },
                'contract_version': {
                    'type': 'string',
                    'description': 'Version of the response contract.',
                },
                'tns_resolution': {
                    'type': 'string',
                    'enum': list(TNS_RESOLUTION_STATES),
                    'description': (
                        'available: the TNS snapshot is current, so TNS-name '
                        'inputs resolve. unavailable: the snapshot is stale or '
                        'absent, or the database is unavailable; TNS-name inputs '
                        'report resolver_unavailable.'
                    ),
                },
                'tns_snapshot_epoch': {
                    'type': ['string', 'null'],
                    'format': 'date-time',
                    'description': (
                        'When the current TNS snapshot was last refreshed; null '
                        'unless tns_resolution is available.'
                    ),
                },
            },
        },
        'FilterableField': {
            'type': 'object',
            'description': 'One filterable catalog property.',
            'required': [
                'name', 'unit', 'description', 'column', 'derived_from', 'parameters',
            ],
            'additionalProperties': False,
            'properties': {
                'name': {
                    'type': 'string',
                    'description': 'Public property name (lowercased).',
                },
                'unit': {'type': 'string', 'description': 'Unit of the value.'},
                'description': {'type': 'string'},
                'column': {
                    'type': ['string', 'null'],
                    'description': (
                        'The stored catalog column (upstream-native case); null '
                        'for a derived property.'
                    ),
                },
                'derived_from': {
                    'type': ['object', 'null'],
                    'description': (
                        'For a derived property, the numerator / denominator '
                        'columns it is computed from; null otherwise.'
                    ),
                    'required': ['numerator', 'denominator'],
                    'additionalProperties': False,
                    'properties': {
                        'numerator': {'type': 'string'},
                        'denominator': {'type': 'string'},
                    },
                },
                'parameters': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'The filter parameters (_min, _max) on this property.',
                },
            },
        },
        'CatalogDescription': {
            'type': 'object',
            'description': 'One catalog in service.',
            'required': [
                'name', 'release', 'catalog_source_id', 'filterable_fields',
                'coverage_map',
            ],
            'additionalProperties': False,
            'properties': {
                'name': {
                    'type': 'string',
                    'description': 'Catalog name as used in catalog_name.',
                },
                'release': {'type': 'string', 'description': 'Release in service.'},
                'catalog_source_id': {
                    'type': 'object',
                    'description': 'What catalog_source_id means for this catalog.',
                    'required': ['column', 'description'],
                    'additionalProperties': False,
                    'properties': {
                        'column': {
                            'type': 'string',
                            'description': 'The catalog column it is read from.',
                        },
                        'description': {'type': 'string'},
                    },
                },
                'filterable_fields': {
                    'type': 'array',
                    'items': _ref('FilterableField'),
                },
                'coverage_map': {
                    'type': 'object',
                    'description': (
                        'The resolution at which catalog outcome searched is '
                        'decided: the configured max HEALPix order of the '
                        "catalog's HATS coverage map. Objects in footprint holes "
                        'or near edges smaller than a pixel may be recorded as '
                        'searched.'
                    ),
                    'required': ['footprint_moc_order', 'resolution_arcmin', 'note'],
                    'additionalProperties': False,
                    'properties': {
                        'footprint_moc_order': {
                            **nullable_order,
                            'description': 'HEALPix order; null if not configured.',
                        },
                        'resolution_arcmin': {
                            'type': ['number', 'null'],
                            'x-unit': 'arcmin',
                            'description': (
                                'Mean pixel side at that order, arcmin; null if '
                                'not configured.'
                            ),
                        },
                        'note': {'type': 'string'},
                    },
                },
            },
        },
        'ServiceDescription': {
            'type': 'object',
            'description': (
                'The catalogs, vocabulary, and per-request limits of the '
                'service (KTD16), read from the live configuration.'
            ),
            'required': [
                'provenance', 'catalogs', 'crossmatch', 'tns', 'limits',
                'detail_levels', 'default_detail', 'response_modes',
                'generic_filters', 'reliability_cuts',
                'provenance_recording_release', 'mcp',
            ],
            'additionalProperties': False,
            'properties': {
                'provenance': _ref('Provenance'),
                'catalogs': {'type': 'array', 'items': _ref('CatalogDescription')},
                'mcp': {
                    'type': 'object',
                    'description': (
                        'The public, read-only MCP endpoint for chat '
                        'assistants (R14). Its tools answer from the same '
                        'lookups as this API.'
                    ),
                    'required': [
                        'path', 'method', 'transport', 'authentication',
                        'description', 'tools',
                    ],
                    'additionalProperties': False,
                    'properties': {
                        'path': {
                            'type': 'string',
                            'description': 'Site-relative path of the endpoint.',
                        },
                        'method': {
                            'type': 'string',
                            'description': 'HTTP method: POST.',
                        },
                        'transport': {
                            'type': 'string',
                            'description': (
                                'streamable_http: MCP Streamable HTTP, JSON-RPC '
                                'over POST answered with a JSON body.'
                            ),
                        },
                        'authentication': {
                            'type': 'string',
                            'description': (
                                'none: no login; connect with the URL alone.'
                            ),
                        },
                        'description': {'type': 'string'},
                        'tools': {
                            'type': 'array',
                            'description': 'The tools tools/list serves, in order.',
                            'items': {
                                'type': 'object',
                                'required': ['name', 'title'],
                                'additionalProperties': False,
                                'properties': {
                                    'name': {'type': 'string'},
                                    'title': {'type': 'string'},
                                },
                            },
                        },
                    },
                },
                'crossmatch': {
                    'type': 'object',
                    'required': [
                        'radius_arcsec', 'nearest_source_per_catalog', 'description',
                    ],
                    'additionalProperties': False,
                    'properties': {
                        'radius_arcsec': {
                            'type': ['number', 'null'],
                            'x-unit': ARCSEC,
                            'description': 'Crossmatch radius, arcsec.',
                        },
                        'nearest_source_per_catalog': {
                            'type': 'boolean',
                            'description': (
                                'True: only the nearest source per catalog '
                                'within the radius is kept.'
                            ),
                        },
                        'description': {'type': 'string'},
                    },
                },
                'tns': {
                    'type': 'object',
                    'required': ['default_radius_arcsec', 'max_radius_arcsec'],
                    'additionalProperties': False,
                    'properties': {
                        'default_radius_arcsec': {
                            'type': 'number',
                            'x-unit': ARCSEC,
                            'description': (
                                'Default radius around a TNS position, arcsec.'
                            ),
                        },
                        'max_radius_arcsec': {
                            'type': 'number',
                            'x-unit': ARCSEC,
                            'description': 'Largest radius a request may set, arcsec.',
                        },
                    },
                },
                'limits': {
                    'type': 'object',
                    'description': (
                        'Per-request maximums and the request budget (KTD12). A '
                        'request over a maximum is a 400 naming the parameter; '
                        'one over the budget is a 400 query_too_expensive.'
                    ),
                    'required': [
                        'request_budget_seconds', 'max_ids', 'max_positions',
                        'max_cone_radius_arcsec', 'max_objects_per_position',
                        'max_objects_per_request', 'recent_crossmatches',
                    ],
                    'additionalProperties': False,
                    'properties': {
                        'request_budget_seconds': {'type': 'number', 'x-unit': 's'},
                        'max_ids': {'type': 'integer', 'x-unit': COUNT, 'minimum': 0},
                        'max_positions': {
                            'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                        },
                        'max_cone_radius_arcsec': {'type': 'number', 'x-unit': ARCSEC},
                        'max_objects_per_position': {
                            'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                        },
                        'max_objects_per_request': {
                            'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                        },
                        'recent_crossmatches': {
                            'type': 'object',
                            'required': [
                                'default_page_size', 'max_page_size',
                                'max_window_hours',
                            ],
                            'additionalProperties': False,
                            'properties': {
                                'default_page_size': {
                                    'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                                },
                                'max_page_size': {
                                    'type': 'integer', 'x-unit': COUNT, 'minimum': 0,
                                },
                                'max_window_hours': {
                                    'type': 'integer', 'x-unit': 'h', 'minimum': 0,
                                },
                            },
                        },
                    },
                },
                'detail_levels': {
                    'type': 'array',
                    'description': f'Cumulative detail levels. {detail_lines}',
                    'items': named,
                },
                'default_detail': {'type': 'string'},
                'response_modes': {
                    'type': 'array',
                    'description': f'Values of response. {mode_lines}',
                    'items': named,
                },
                'generic_filters': {
                    'type': 'array',
                    'description': (
                        'Filters not tied to a catalog property; catalog '
                        'property filters are listed per catalog.'
                    ),
                    'items': {
                        'type': 'object',
                        'required': [
                            'name', 'kind', 'bound', 'unit', 'is_match_filter',
                            'description',
                        ],
                        'additionalProperties': False,
                        'properties': {
                            'name': {'type': 'string'},
                            'kind': {'type': 'string'},
                            'bound': {'type': ['string', 'null'], 'enum': ['min', 'max', None]},
                            'unit': {'type': ['string', 'null']},
                            'is_match_filter': {'type': 'boolean'},
                            'description': {'type': 'string'},
                        },
                    },
                },
                'reliability_cuts': {
                    'type': 'array',
                    'items': _ref('ReliabilityCut'),
                },
                'provenance_recording_release': {
                    'type': 'string',
                    'description': (
                        'The service release that started recording per-object '
                        'crossmatch provenance; objects crossmatched earlier '
                        'report it as not recorded.'
                    ),
                },
            },
        },
    }


def _response_mode_schema() -> dict[str, Any]:
    """The ``response`` parameter's schema (KTD10)."""
    return {
        'type': 'string',
        'enum': list(RESPONSE_MODES),
        'description': (
            'objects (default): the listing. count: counts by input status, '
            'object status, and qualifies for the same inputs and filters, '
            'without objects (CountResponse).'
        ),
    }


def _filter_query_params() -> list[dict[str, Any]]:
    """The filter and ``response`` query parameters of a GET query."""
    params = [
        _query_param(param.name, _filter_value_schema(param), param.description)
        for param in filter_parameters()
    ]
    params.append(_query_param(
        'response', _response_mode_schema(), _response_mode_schema()['description'],
    ))
    return params


def _int_setting(name: str) -> int:
    """A per-request maximum, read live (R27)."""
    return int(getattr(settings, name))


def _query_param(name: str, schema: dict[str, Any], description: str) -> dict[str, Any]:
    """An optional query parameter."""
    return {
        'name': name,
        'in': 'query',
        'required': False,
        'schema': schema,
        'description': description,
    }


def _paths() -> dict[str, Any]:
    """Path items for every documented operation."""
    radius_param_schema = {
        'type': 'number',
        'x-unit': ARCSEC,
        'exclusiveMinimum': 0,
        'maximum': float(settings.API_MAX_CONE_RADIUS_ARCSEC),
    }
    return {
        '/openapi.json': {
            'get': {
                'operationId': 'get_openapi',
                'summary': 'This OpenAPI document.',
                'description': (
                    'The OpenAPI 3.1 document for every operation, built per '
                    'request from the live configuration, so its radius, '
                    'catalogs, reliability cuts, limits, and version describe '
                    'the running service. Numeric fields name their unit in '
                    'x-unit. Start here, or at /llms.txt.'
                ),
                'responses': {
                    '200': _json(
                        {
                            'type': 'object',
                            'required': ['openapi', 'info', 'paths'],
                        },
                        'The OpenAPI 3.1 document.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                },
            },
        },
        '/api/objects/{diaObjectId}': {
            'get': {
                'operationId': 'get_object',
                'summary': 'Look up one Rubin object by diaObjectId.',
                'description': (
                    'What the service knows about one object: its status, '
                    'per-catalog search outcomes, provenance, and coincident '
                    'sources. The response has the lookup_objects shape with '
                    'one result. ' + NOT_IN_SERVICE_CAVEAT
                ),
                'parameters': [
                    {
                        'name': 'diaObjectId',
                        'in': 'path',
                        'required': True,
                        'schema': _ref('DiaObjectIdStr'),
                        'description': 'The diaObjectId, as a decimal string.',
                    },
                    _query_param(
                        'detail',
                        {'type': 'string', 'enum': ['ids', 'position', 'matches', 'full']},
                        'Cumulative detail level (default matches).',
                    ),
                    *_filter_query_params(),
                ],
                'responses': {
                    '200': _json(
                        {'oneOf': [_ref('LookupResponse'), _ref('CountResponse')]},
                        'The object, or counts for response=count.',
                    ),
                    '400': _json(
                        _ref('Error'),
                        'Invalid diaObjectId, detail, filter, or response; '
                        'filters_span_catalogs; or query_too_expensive.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
                },
            },
        },
        '/api/lookup': {
            'post': {
                'operationId': 'lookup_objects',
                'summary': 'Look up a list of tagged inputs in one request.',
                'description': (
                    'Read-only and idempotent. Every input produces exactly one '
                    'result, in input order, echoing the input as sent and its '
                    'normalized form. A malformed input is reported as '
                    'invalid_input in its own result and does not fail the '
                    'others; request-level problems are a 400 naming the '
                    'parameter. ' + NOT_IN_SERVICE_CAVEAT
                ),
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _ref('LookupRequest')}},
                },
                'responses': {
                    '200': _json(
                        {'oneOf': [_ref('LookupResponse'), _ref('CountResponse')]},
                        'One result per input, or counts for response=count.',
                    ),
                    '400': _json(
                        _ref('Error'),
                        'Invalid request: body, Content-Type, inputs, detail, '
                        'a filter, response, or an unknown field; '
                        'filters_span_catalogs; or query_too_expensive.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
                },
            },
        },
        '/api/cone': {
            'get': {
                'operationId': 'cone_search',
                'summary': 'Search one position for every Rubin object within a radius.',
                'description': (
                    'Every Rubin object the service has seen within the radius '
                    'of the position, crossmatched or not, across the whole '
                    'archive, each with its own status. A position with no '
                    'object reports no_rubin_object. Objects whose position '
                    'could not be indexed at ingest (null HEALPix index) are '
                    'not reachable by position search; look them up by '
                    'diaObjectId. Paged in (ingest_time, diaObjectId) order: '
                    'follow next_cursor until null. The first page pins the '
                    'object set with as_of. ' + PAGING_CAVEAT + ' '
                    + COINCIDENCE_CAVEAT
                ),
                'parameters': [
                    _query_param(
                        'ra',
                        {'type': 'number', 'x-unit': DEG, 'minimum': 0, 'maximum': 360},
                        'Center RA, degrees (ICRS). Required without cursor.',
                    ),
                    _query_param(
                        'dec',
                        {'type': 'number', 'x-unit': DEG, 'minimum': -90, 'maximum': 90},
                        'Center Dec, degrees (ICRS). Required without cursor.',
                    ),
                    _query_param(
                        'radius_arcsec', radius_param_schema,
                        'Radius, arcsec. Required without cursor. '
                        f'At most {radius_param_schema["maximum"]:g} arcsec.',
                    ),
                    _query_param(
                        'detail',
                        {'type': 'string', 'enum': ['ids', 'position', 'matches', 'full']},
                        'Cumulative detail level (default matches).',
                    ),
                    _query_param(
                        'page_size',
                        {'type': 'integer', 'x-unit': COUNT, 'minimum': 1},
                        'Objects per page (default '
                        f'{_int_setting("API_MAX_OBJECTS_PER_POSITION")}); '
                        'clamped to '
                        f'{_int_setting("API_MAX_OBJECTS_PER_REQUEST")}.',
                    ),
                    _query_param(
                        'cursor',
                        {'type': 'string'},
                        'Opaque next_cursor from a prior page; pins ra, dec, '
                        'radius_arcsec, detail, and as_of. Filters are not '
                        'pinned: pass them on every page. Not allowed with '
                        'response=count. ' + PAGING_CAVEAT,
                    ),
                    *_filter_query_params(),
                ],
                'responses': {
                    '200': _json(
                        {'oneOf': [_ref('ConeSearchResponse'), _ref('CountResponse')]},
                        'One page, or counts for response=count.',
                    ),
                    '400': _json(
                        _ref('Error'),
                        'Invalid parameter, filter, or cursor (param names it), '
                        'filters_span_catalogs, or query_too_expensive.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
                },
            },
        },
        '/api/tns/{name}': {
            'get': {
                'operationId': 'resolve_tns',
                'summary': 'Look up a TNS name as a position search around it.',
                'description': (
                    'Resolves the name through the TNS snapshot and returns '
                    'every Rubin object within the radius of the TNS '
                    'position, echoing the TNS record and snapshot epoch. The '
                    'response has the lookup_objects shape with one result. '
                    'Not paged: at most '
                    f'{_int_setting("API_MAX_OBJECTS_PER_POSITION")} objects, '
                    'with truncated and total when capped. When the snapshot '
                    'is stale or absent the result is resolver_unavailable '
                    '(still a 200). A coincident source is not a host '
                    'association, and the absence of one is not evidence of a '
                    'hostless transient.'
                ),
                'parameters': [
                    {
                        'name': 'name',
                        'in': 'path',
                        'required': True,
                        'schema': {'type': 'string', 'maxLength': 64},
                        'description': 'The TNS name, e.g. 2026abc or SN 2026abc.',
                    },
                    _query_param(
                        'radius_arcsec', radius_param_schema,
                        'Radius around the TNS position, arcsec (default '
                        f'{float(settings.TNS_MATCH_RADIUS_ARCSEC):g}); at most '
                        f'{radius_param_schema["maximum"]:g}.',
                    ),
                    _query_param(
                        'detail',
                        {'type': 'string', 'enum': ['ids', 'position', 'matches', 'full']},
                        'Cumulative detail level (default matches).',
                    ),
                    *_filter_query_params(),
                ],
                'responses': {
                    '200': _json(
                        {'oneOf': [_ref('LookupResponse'), _ref('CountResponse')]},
                        'One result, or counts for response=count.',
                    ),
                    '400': _json(
                        _ref('Error'),
                        'Invalid name, radius_arcsec, detail, filter, or '
                        'response; filters_span_catalogs; or query_too_expensive.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
                },
            },
        },
        '/api/status': {
            'get': {
                'operationId': 'service_status',
                'summary': 'Service availability, version, and TNS-resolution availability.',
                'description': (
                    'Always 200 for a GET: an unreachable or unresponsive '
                    'database is reported as database: unavailable rather than '
                    'as an error. tns_resolution reports whether TNS-name inputs '
                    'can be resolved now (the TNS snapshot is current).'
                ),
                'responses': {
                    '200': _json(_ref('ServiceStatus'), 'The service status.'),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                },
            },
        },
        '/api/describe': {
            'get': {
                'operationId': 'describe_service',
                'summary': 'Catalogs, vocabulary, and per-request limits.',
                'description': (
                    'The catalogs in service (release, what catalog_source_id '
                    'means, filterable fields with units, coverage-map '
                    'resolution), the crossmatch and TNS radii, per-request '
                    'maximums and budget, detail levels, response modes, '
                    'generic filters, the broker reliability cut table, and '
                    'the provenance recording release. Read from the live '
                    'configuration; needs no database.'
                ),
                'responses': {
                    '200': _json(_ref('ServiceDescription'), 'The description.'),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                },
            },
        },
        '/api/recent-crossmatches': {
            'get': {
                'operationId': 'recent_crossmatches',
                'summary': 'Crossmatches for objects with alerts in a time window.',
                'description': (
                    'One keyset page of matched objects whose alert falls in the '
                    'window, newest first. Matches-only: an object with no '
                    'catalog match is not listed. Follow next_cursor until null; '
                    'the walk is pinned by as_of. Does not filter or count: a '
                    'filter-named parameter (<catalog>.<column>_min|_max, '
                    'catalog, separation_arcsec_max, reliability_min|_max, or '
                    'any name with a dot or ending _min/_max) or response is a '
                    '400 unsupported_parameter; other unknown parameters are '
                    'ignored. Runs under the per-request budget of '
                    f'{float(settings.API_REQUEST_BUDGET_SECONDS):g} s: a large page at '
                    'detail=full can exceed it (400 query_too_expensive); use a '
                    'smaller page_size.'
                ),
                'parameters': [
                    _query_param(
                        'start',
                        {'type': 'string', 'format': 'date-time'},
                        'Window start (inclusive), ISO-8601; naive means UTC. '
                        f'Default: end minus {DEFAULT_WINDOW_HOURS} hours. The '
                        'window may span at most '
                        f'{_int_setting("RECENT_CROSSMATCH_MAX_WINDOW_HOURS")} '
                        'hours.',
                    ),
                    _query_param(
                        'end',
                        {'type': 'string', 'format': 'date-time'},
                        'Window end (exclusive), ISO-8601. Default: now.',
                    ),
                    _query_param(
                        'time_field',
                        {'type': 'string', 'enum': ['ingest_time', 'event_time']},
                        'Which alert timestamp the window filters on.',
                    ),
                    _query_param(
                        'detail',
                        {'type': 'string', 'enum': ['ids', 'position', 'matches', 'full']},
                        'Cumulative detail level (default matches).',
                    ),
                    _query_param(
                        'page_size',
                        {'type': 'integer', 'x-unit': COUNT, 'minimum': 1},
                        'Maximum objects on the page (default '
                        f'{_int_setting("RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE")}); '
                        'clamped to the operator maximum of '
                        f'{_int_setting("RECENT_CROSSMATCH_MAX_PAGE_SIZE")}.',
                    ),
                    _query_param(
                        'cursor',
                        {'type': 'string'},
                        'Opaque next_cursor from a prior page; pins start, end, '
                        'time_field, and detail.',
                    ),
                ],
                'responses': {
                    '200': _json(_ref('RecentCrossmatchesPage'), 'One page.'),
                    '400': _json(
                        _ref('Error'),
                        'invalid_parameter (a bad start, end, time_field, '
                        'detail, page_size, or cursor, or a cursor conflict); '
                        'unsupported_parameter (a filter or response parameter, '
                        'which this operation does not support); or '
                        'query_too_expensive. error repeats message, as in the '
                        'original error-only body.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
                },
            },
        },
    }


def build_document() -> dict[str, Any]:
    """Build the OpenAPI 3.1 document from live settings.

    Returns:
        The JSON-native document.
    """
    current = provenance.service_provenance()
    version = current['service_version'] or 'unknown'
    return {
        'openapi': '3.1.0',
        'info': {
            'title': 'SCiMMA Rubin Crossmatch Service API',
            'version': CONTRACT_VERSION,
            'description': (
                'Read-only queries over Rubin alert objects and their catalog '
                f'crossmatches. Service version {version}; contract version '
                f'{CONTRACT_VERSION}. {position_conventions()}\n\n'
                f'{NEAREST_SOURCE_RULE}\n\n{COINCIDENCE_CAVEAT}\n\n'
                f'{NOT_IN_SERVICE_CAVEAT}\n\n{PAGING_CAVEAT}\n\n'
                'Access is public and unauthenticated; authentication may be '
                'required in a future release.'
            ),
        },
        'paths': _paths(),
        'components': _components(current),
    }
