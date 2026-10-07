"""The MCP tool registry: three question-shaped, read-only tools (R1-R4, KTD5).

Each tool is data: a name, a title, the read-only annotations, and a
description and input schema built from settings at call time (the limits are
read when asked, like the API maximums, KTD9). Each also carries two steps:

* ``parse`` validates the arguments without touching the database and
  raises ``ToolArgumentError`` with a message naming the limit or argument;
  the endpoint answers that as an ``isError`` result.
* ``run`` calls one API function (``lookup_objects``, ``cone_search``, or
  ``describe_service`` with the status checks) under the request guard and
  projects its return value (``chat_mcp.projection``). It returns the
  projection and the counts for the ``mcp tool call`` log line, and raises
  ``ApiError`` when the guard or the API refuses the call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import DatabaseError
from django.db.models import Max

from api.contract import parse_position, parse_radius_arcsec
from api.discovery import check_database, describe_service, service_status
from api.errors import ApiError, InvalidQuery
from api.guard import RequestGuard, run_guarded, sql_phase
from api.lookup import lookup_objects
from api.positions import ORDER_NEAREST, cone_search
from chat_mcp.identifiers import Classified, classify_identifiers
from chat_mcp.projection import project_coverage, project_lookup, project_near_position
from core.models import Alert

#: ``radius_arcsec`` of the near-position tool when the caller gives none.
DEFAULT_RADIUS_ARCSEC = 10.0

#: Every tool only reads this service's own store (R4).
READ_ONLY_ANNOTATIONS = {
    'readOnlyHint': True,
    'destructiveHint': False,
    'idempotentHint': True,
    'openWorldHint': False,
}

_CATALOGS = 'Gaia DR3, DES Y6 Gold, DELVE DR3 Gold and SkyMapper DR4'
_NOT_ANSWERED = (
    'It does not answer light curves, photometry history, classifications or '
    'host-galaxy association; point the user to the TNS, Lasair and ANTARES '
    'links in the answer for those.'
)

#: What a tool's ``run`` returns: the projection, and counts for the log line.
RunResult = tuple[dict[str, Any], dict[str, Any]]


class ToolArgumentError(Exception):
    """A tool's arguments are invalid; ``message`` names the limit or argument."""

    def __init__(self, message: str) -> None:
        """Build the error.

        Args:
            message: What is wrong, naming the argument and any limit.
        """
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Tool:
    """One MCP tool.

    Attributes:
        name: The tool name the client calls.
        title: A human-readable name for the client's UI.
        description: Builds the description from current settings.
        input_schema: Builds the JSON Schema of the arguments.
        parse: Validates the arguments; raises ``ToolArgumentError``.
        run: Runs the parsed call under a guard; raises ``ApiError``.
    """

    name: str
    title: str
    description: Callable[[], str]
    input_schema: Callable[[], dict[str, Any]]
    parse: Callable[[dict[str, Any]], Any]
    run: Callable[[RequestGuard, Any], RunResult]

    def definition(self) -> dict[str, Any]:
        """The tool's ``tools/list`` entry.

        Returns:
            ``name``, ``title``, ``description``, ``inputSchema`` and
            ``annotations``.
        """
        return {
            'name': self.name,
            'title': self.title,
            'description': self.description(),
            'inputSchema': self.input_schema(),
            'annotations': dict(READ_ONLY_ANNOTATIONS),
        }


def _max_radius() -> float:
    return float(settings.API_MAX_CONE_RADIUS_ARCSEC)


def _default_radius() -> float:
    return min(DEFAULT_RADIUS_ARCSEC, _max_radius())


# --- lookup_rubin_transients ---

def _lookup_description() -> str:
    return (
        'Tell the user about specific Rubin Observatory transients, by TNS name or '
        'Rubin diaObjectId. For each one, reports the catalog sources from '
        f'{_CATALOGS} that coincide with its position (separation and key values '
        'such as magnitudes, parallax, star/galaxy class and photometric '
        'redshift), its status in this crossmatch service, the alert brokers '
        'that delivered it, its stored TNS association, and links to its TNS, '
        'Lasair and ANTARES pages. Identifiers are strings: TNS names such as '
        '2026abc, AT 2026abc or SN2026abc, or diaObjectIds of up to 19 digits '
        'such as 3068394420283588661 (always send IDs as strings). Up to '
        f'{int(settings.MCP_MAX_IDENTIFIERS)} identifiers per call; at most '
        f'{int(settings.MCP_MAX_OBJECTS)} objects are summarized, and a '
        'truncated answer says so and carries an API request for the full set. '
        'An identifier with no object here gets a reason (unknown TNS name, no '
        'Rubin object at that TNS position, not in this service, crossmatch '
        f'pending, TNS unavailable). {_NOT_ANSWERED}'
    )


def _lookup_schema() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'identifiers': {
                'type': 'array',
                'items': {'type': 'string'},
                'minItems': 1,
                'maxItems': int(settings.MCP_MAX_IDENTIFIERS),
                'description': (
                    'TNS names (2026abc, AT 2026abc, SN2026abc) or Rubin '
                    'diaObjectIds as decimal strings.'
                ),
            },
        },
        'required': ['identifiers'],
    }


def _parse_lookup(arguments: dict[str, Any]) -> Classified:
    """Classify the ``identifiers`` argument (KTD6).

    Raises:
        ToolArgumentError: If it is missing, not an array, empty, or longer
            than ``MCP_MAX_IDENTIFIERS``.
    """
    values = arguments.get('identifiers')
    if not isinstance(values, list) or not values:
        raise ToolArgumentError(
            'identifiers must be a non-empty array of strings (TNS names or '
            'diaObjectIds).'
        )
    maximum = int(settings.MCP_MAX_IDENTIFIERS)
    if len(values) > maximum:
        raise ToolArgumentError(
            f'identifiers accepts at most {maximum} identifiers per call, got '
            f'{len(values)}. Split the list across calls, or use the public API '
            '(POST /api/lookup) for larger sets.'
        )
    return classify_identifiers(values)


def _run_lookup(guard: RequestGuard, classified: Classified) -> RunResult:
    """Look up the identifiers with one ``lookup_objects`` call and project it."""
    inputs = classified.lookup_inputs()

    def work() -> dict[str, Any]:
        response = lookup_objects(inputs=inputs, detail='full') if inputs else None
        return project_lookup(classified, response)

    projection = run_guarded(guard, work)
    return projection, {
        'identifiers': classified.requested,
        'objects_found': projection['objects_found'],
        'objects_returned': projection['objects_returned'],
    }


# --- search_rubin_transients_near_position ---

def _near_description() -> str:
    return (
        'Find the Rubin Observatory transients this crossmatch service has seen '
        'within a radius of a sky position, nearest first, each with the same '
        'summary as lookup_rubin_transients: coincident sources from '
        f'{_CATALOGS}, status, delivering brokers, stored TNS association, and '
        'TNS, Lasair and ANTARES links. ra_deg and dec_deg are ICRS degrees (RA '
        '0 to 360, Dec -90 to 90). radius_arcsec defaults to '
        f'{_default_radius():g} and is at most {_max_radius():g} arcsec. At most '
        f'{int(settings.MCP_MAX_OBJECTS)} objects are summarized; a truncated '
        'answer says so and carries an API request for the full set. An empty '
        'answer means no transient alerted to this service lies there, not that '
        'Rubin saw nothing. It searches Rubin transients, not the catalogs '
        f'themselves. {_NOT_ANSWERED}'
    )


def _near_schema() -> dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'ra_deg': {
                'type': 'number', 'minimum': 0, 'maximum': 360,
                'description': 'Right ascension, ICRS degrees.',
            },
            'dec_deg': {
                'type': 'number', 'minimum': -90, 'maximum': 90,
                'description': 'Declination, ICRS degrees.',
            },
            'radius_arcsec': {
                'type': 'number', 'exclusiveMinimum': 0, 'maximum': _max_radius(),
                'default': _default_radius(),
                'description': (
                    f'Search radius in arcsec; default {_default_radius():g}, '
                    f'at most {_max_radius():g}.'
                ),
            },
        },
        'required': ['ra_deg', 'dec_deg'],
    }


def _parse_near(arguments: dict[str, Any]) -> tuple[float, float, float]:
    """Validate the position and radius with the API's own parsers.

    Raises:
        ToolArgumentError: If a coordinate is missing, not a number, or out of
            range, or the radius is not positive or above the cone maximum.
    """
    radius = arguments.get('radius_arcsec')
    try:
        ra, dec = parse_position(
            arguments.get('ra_deg'), arguments.get('dec_deg'),
            ra_param='ra_deg', dec_param='dec_deg',
        )
        radius = parse_radius_arcsec(_default_radius() if radius is None else radius)
    except InvalidQuery as exc:
        raise ToolArgumentError(exc.message) from None
    return ra, dec, radius


def _run_near(guard: RequestGuard, parsed: tuple[float, float, float]) -> RunResult:
    """Search the cone nearest first with one ``cone_search`` call and project it."""
    ra, dec, radius = parsed

    def work() -> dict[str, Any]:
        response = cone_search(
            ra=ra, dec=dec, radius_arcsec=radius, detail='full',
            page_size=int(settings.MCP_MAX_OBJECTS), order=ORDER_NEAREST,
        )
        return project_near_position(response)

    projection = run_guarded(guard, work)
    return projection, {
        'ra_deg': ra,
        'dec_deg': dec,
        'radius_arcsec': radius,
        'objects_found': projection['objects_total'],
        'objects_returned': projection['objects_returned'],
    }


# --- describe_crossmatch_service ---

def _describe_description() -> str:
    return (
        'Describe what this Rubin transient crossmatch service covers and its '
        f'current state: the catalogs and releases ({_CATALOGS}), the crossmatch '
        'radius and rule, the alert brokers (ANTARES, Lasair, Pitt-Google) and '
        'their reliability cuts, TNS snapshot freshness, when the newest alert '
        'arrived, and whether the service is up. Use it to explain the '
        'service\'s scope, or why a transient is absent. Takes no arguments.'
    )


def _no_arguments(arguments: dict[str, Any]) -> None:
    """The coverage tool takes no arguments; any given are ignored."""
    return None


def _status_reads() -> tuple[datetime | None, datetime | None]:
    """The status checks and the newest alert's ingest time."""
    epoch = check_database()
    with sql_phase():
        latest = Alert.objects.aggregate(latest=Max('ingest_time'))['latest']
    return epoch, latest


def _run_describe(guard: RequestGuard, _parsed: None) -> RunResult:
    """Project ``describe_service`` with the status checks, as ``api/status`` does.

    A database that fails or does not answer in time is reported as
    unavailable in the answer, not as a tool error, like the status endpoint
    (``describe_service`` itself needs no database).
    """
    describe = describe_service()
    try:
        epoch, latest = run_guarded(guard, _status_reads, map_unavailable=False)
        status = service_status(database_ok=True, snapshot_epoch=epoch)
    except (DatabaseError, ApiError):
        latest = None
        status = service_status(database_ok=False, snapshot_epoch=None)
    return project_coverage(describe, status, latest_ingest=latest), {}


TOOLS: dict[str, Tool] = {
    tool.name: tool for tool in (
        Tool(
            name='lookup_rubin_transients',
            title='Look up Rubin transients',
            description=_lookup_description,
            input_schema=_lookup_schema,
            parse=_parse_lookup,
            run=_run_lookup,
        ),
        Tool(
            name='search_rubin_transients_near_position',
            title='Search Rubin transients near a position',
            description=_near_description,
            input_schema=_near_schema,
            parse=_parse_near,
            run=_run_near,
        ),
        Tool(
            name='describe_crossmatch_service',
            title='Describe the crossmatch service',
            description=_describe_description,
            input_schema=lambda: {'type': 'object', 'properties': {}},
            parse=_no_arguments,
            run=_run_describe,
        ),
    )
}
