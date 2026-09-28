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
)
from api.lookup import (
    BASIS_BEST_GUESS,
    BASIS_RECORDED,
    PROVENANCE_NOT_RECORDED,
    PROVENANCE_RECORDED,
)
from core import provenance
from core.models import CutEnforcedBy, CutStatus

_REF = '#/components/schemas/'


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
            f'crossmatch radius is {radius_text} and the catalogs in service '
            f'are: {releases}.'
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
            'LegacyError': {
                'type': 'object',
                'description': 'The original error body: a reason string only.',
                'required': ['error'],
                'properties': {'error': {'type': 'string'}},
            },
            **_recent_crossmatches_schemas(),
            **_lookup_schemas(),
        },
    }


def _recent_crossmatches_schemas() -> dict[str, Any]:
    """Schemas describing the existing recent-crossmatches response as-is."""
    return {
        'MatchSummary': {
            'type': 'object',
            'required': ['catalog_name', 'catalog_source_id', 'separation_arcsec'],
            'properties': {
                'catalog_name': {'type': 'string'},
                'catalog_source_id': {
                    'type': 'string',
                    'description': "Source identifier in the named catalog.",
                },
                'separation_arcsec': {
                    'type': 'number',
                    'description': 'Object-to-source angular separation, arcsec.',
                },
            },
        },
        'PublishedMatch': {
            'type': 'object',
            'description': (
                'A match built by the same payload builder as the Hopskotch '
                'stream. ra/dec here are the catalog source position.'
            ),
            'required': [
                'diaObjectId', 'ra', 'dec', 'catalog_name', 'catalog_source_id',
                'separation_arcsec', 'catalog_payload', 'catalogs_skipped',
                'partial', 'tns_checked', 'tns_snapshot_epoch',
            ],
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'ra': {'type': 'number', 'description': 'Source RA, degrees.'},
                'dec': {'type': 'number', 'description': 'Source Dec, degrees.'},
                'catalog_name': {'type': 'string'},
                'catalog_source_id': {'type': 'string'},
                'separation_arcsec': {'type': 'number'},
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
            'required': ['diaObjectId'],
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'ra': {
                    'type': ['number', 'null'],
                    'description': 'Object RA, degrees.',
                },
                'dec': {
                    'type': ['number', 'null'],
                    'description': 'Object Dec, degrees.',
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
                'window', 'time_field', 'detail', 'page_size', 'count',
                'next_cursor', 'objects',
            ],
            'properties': {
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
                'page_size': {'type': 'integer', 'minimum': 1},
                'count': {
                    'type': 'integer',
                    'minimum': 0,
                    'description': 'Objects on this page, not a total.',
                },
                'next_cursor': {'type': ['string', 'null']},
                'objects': {'type': 'array', 'items': _ref('RecentObject')},
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
        'LookupInput': {
            'description': (
                'One tagged input, discriminated by kind. An entry that does '
                'not match is not a request error: it gets its own result with '
                'status invalid_input.'
            ),
            'oneOf': [_ref('IdInput')],
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
                        f'{_int_setting("API_MAX_IDS")}) apply; over a maximum '
                        'the request is a 400 naming inputs.'
                    ),
                    'items': _ref('LookupInput'),
                },
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                    'description': 'Cumulative detail level (default matches).',
                },
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
                'match_version': {'type': ['integer', 'null']},
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
                'A coincident source (raw catalog values at detail full). A '
                'coincident source is not a host association.'
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
                'appear from detail position; matches from detail matches.'
            ),
            'required': ['diaObjectId', 'diaObjectId_str', 'status'],
            'additionalProperties': False,
            'properties': {
                'diaObjectId': _ref('DiaObjectId'),
                'diaObjectId_str': _ref('DiaObjectIdStr'),
                'status': _ref('ObjectStatus'),
                'ra': {'type': ['number', 'null'], 'description': 'Object RA, degrees (ICRS).'},
                'dec': {'type': ['number', 'null'], 'description': 'Object Dec, degrees (ICRS).'},
                'reliability': {
                    'type': ['number', 'null'],
                    'description': (
                        'LSST real/bogus reliability stored when the object '
                        'was first seen.'
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
            },
        },
        'IdResult': {
            'type': 'object',
            'description': 'The result of an id input: its object and that object\'s status.',
            'required': ['index', 'kind', 'input', 'normalized', 'status', 'objects'],
            'additionalProperties': False,
            'properties': {
                'index': {'type': 'integer', 'minimum': 0},
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
                'index': {'type': 'integer', 'minimum': 0},
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
            },
        },
        'LookupResult': {
            'description': 'One result per input, in input order.',
            'oneOf': [_ref('IdResult'), _ref('InvalidInputResult')],
        },
        'LookupResponse': {
            'type': 'object',
            'required': ['provenance', 'detail', 'count', 'provenance_sets', 'results'],
            'additionalProperties': False,
            'properties': {
                'provenance': _ref('Provenance'),
                'detail': {
                    'type': 'string',
                    'enum': ['ids', 'position', 'matches', 'full'],
                },
                'count': {
                    'type': 'integer',
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
            },
        },
    }


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
    return {
        '/openapi.json': {
            'get': {
                'operationId': 'get_openapi',
                'summary': 'This OpenAPI document.',
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
                    'one result. not_in_service is not evidence that the object '
                    'failed a reliability cut or does not exist in Rubin.'
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
                ],
                'responses': {
                    '200': _json(_ref('LookupResponse'), 'The object.'),
                    '400': _json(_ref('Error'), 'Invalid diaObjectId or detail.'),
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
                    'parameter.'
                ),
                'requestBody': {
                    'required': True,
                    'content': {'application/json': {'schema': _ref('LookupRequest')}},
                },
                'responses': {
                    '200': _json(_ref('LookupResponse'), 'One result per input.'),
                    '400': _json(
                        _ref('Error'),
                        'Invalid request: body, Content-Type, inputs, detail, '
                        'or an unknown field; or query_too_expensive.',
                    ),
                    '405': _json(_ref('Error'), 'Method not allowed.'),
                    '503': _json(_ref('Error'), 'Service unavailable; see Retry-After.'),
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
                    'catalog match is not listed. Follow next_cursor until null.'
                ),
                'parameters': [
                    _query_param(
                        'start',
                        {'type': 'string', 'format': 'date-time'},
                        'Window start (inclusive), ISO-8601; naive means UTC. '
                        'Default: end minus 12 hours.',
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
                        {'type': 'integer', 'minimum': 1},
                        'Maximum objects on the page; clamped to the operator '
                        'maximum.',
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
                    '400': _json(_ref('LegacyError'), 'Invalid parameter.'),
                    '405': _json(_ref('LegacyError'), 'Method not allowed.'),
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
                f'{CONTRACT_VERSION}. Coordinates are RA/Dec in degrees; '
                'separations and radii are in arcsec. A coincident source is '
                'not a host association, and the absence of one is not '
                'evidence of a hostless transient. Access is public and '
                'unauthenticated; authentication may be required in a future '
                'release.'
            ),
        },
        'paths': _paths(),
        'components': _components(current),
    }
