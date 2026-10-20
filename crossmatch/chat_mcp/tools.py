"""The MCP tool registry: four question-shaped, read-only tools (R1-R4, KTD5).

Each tool is data: a name, a title, the read-only annotations, and a
description and input schema built from settings at call time (the limits are
read when asked, like the API maximums, KTD9). Each also carries two steps:

* ``parse`` validates the arguments without touching the database and
  raises ``ToolArgumentError`` with a message naming the limit or argument;
  the endpoint answers that as an ``isError`` result.
* ``run`` calls one API function (``lookup_objects``, ``cone_search``,
  ``describe_service`` with the status checks, or ``summarize_window``)
  under the request guard and
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
from django.utils import timezone as django_timezone

from api.contract import ErrorCode, parse_position, parse_radius_arcsec
from api.discovery import check_database, describe_service, service_status
from api.errors import ApiError, InvalidQuery
from api.guard import RequestGuard, run_guarded, sql_phase
from api.lookup import lookup_objects
from api.positions import ORDER_NEAREST, cone_search
from api.summary import (
    BROKER_PARAM,
    CATALOG_MATCH_PARAM,
    CATALOG_MATCH_VALUES,
    CATALOG_PARAM,
    END_PARAM,
    LAST_NIGHTS_PARAM,
    NIGHT_PARAM,
    START_PARAM,
    TOP_N_PARAM,
    WINDOW_PARAMS,
    broker_names,
    parse_summary_params,
    summarize_window,
)
from chat_mcp.identifiers import Classified, classify_identifiers
from chat_mcp.projection import (
    project_coverage,
    project_lookup,
    project_near_position,
    project_summary,
)
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


# --- summarize_rubin_transients_by_night ---

#: The arguments the summary tool passes to ``parse_summary_params``.
_SUMMARY_ARGUMENTS = (
    NIGHT_PARAM, LAST_NIGHTS_PARAM, START_PARAM, END_PARAM,
    BROKER_PARAM, CATALOG_PARAM, CATALOG_MATCH_PARAM, TOP_N_PARAM,
)


def _now() -> datetime:
    """The query moment, read once per call (a seam for tests)."""
    return django_timezone.now()


def _max_nights() -> int:
    return int(settings.SUMMARY_MAX_NIGHTS)


def _catalog_names() -> list[str]:
    return [str(cat['name']) for cat in settings.CROSSMATCH_CATALOGS]


def _summary_description() -> str:
    max_nights = _max_nights()
    return (
        'Count the Rubin Observatory transients this crossmatch service received '
        'over one Rubin observing night or a span of nights: how many distinct '
        'Rubin objects (not alerts or broker deliveries), per night, per broker '
        '(ANTARES, Lasair, Pitt-Google; give broker as antares, lasair or '
        'pittgoogle), their catalog match outcomes, the reliability '
        'distribution, and the most reliable objects. A night is a Rubin dayObs '
        '(YYYY-MM-DD, the UTC date of a time minus 12 hours), and objects fall '
        'into nights by their observation time. For "last night" or "tonight" '
        'call with no window; for "this week" or "last week" use last_nights=7; '
        'for longer periods such as "last month", split them into spans of '
        f'at most {max_nights} nights and add the results. Never convert nights to the '
        "user's local time zone; Rubin observes from Chile and nights are named "
        f'in UTC. At most {max_nights} nights per call. Catalog names: Gaia DR3 '
        'is gaia_dr3, DES is des_y6_gold, DELVE is delve_dr3_gold, SkyMapper is '
        'skymapper_dr4; catalog_match=without reports each no-match reason '
        'separately, and those buckets must not be summed into one total. Only '
        f'the top_n (default {int(settings.SUMMARY_DEFAULT_TOP_N)}, at most '
        f'{int(settings.MCP_MAX_OBJECTS)}) most reliable objects are listed, not '
        'every object; pass their diaObjectIds as strings to '
        'lookup_rubin_transients for coincident sources. The answer names its '
        'unit, clock and window, and carries an API request that reproduces it. '
        f'{_NOT_ANSWERED}'
    )


def _summary_schema() -> dict[str, Any]:
    max_nights = _max_nights()
    max_objects = int(settings.MCP_MAX_OBJECTS)
    return {
        'type': 'object',
        'properties': {
            NIGHT_PARAM: {
                'type': 'string', 'pattern': r'^\d{4}-\d{2}-\d{2}$',
                'description': (
                    'One Rubin night as its dayObs date, YYYY-MM-DD (UTC). '
                    'Omit every window argument for the latest night.'
                ),
            },
            LAST_NIGHTS_PARAM: {
                'type': 'integer', 'minimum': 1, 'maximum': max_nights,
                'description': (
                    f'The N most recent Rubin nights, 1 to {max_nights} '
                    '(7 for a week).'
                ),
            },
            START_PARAM: {
                'type': 'string',
                'description': (
                    'Span start, ISO-8601 UTC (inclusive); give with end. The '
                    f'span may touch at most {max_nights} nights.'
                ),
            },
            END_PARAM: {
                'type': 'string',
                'description': 'Span end, ISO-8601 UTC (exclusive); give with start.',
            },
            BROKER_PARAM: {
                'type': 'string', 'enum': broker_names(),
                'description': 'Only objects this broker delivered.',
            },
            CATALOG_PARAM: {
                'type': 'string', 'enum': _catalog_names(),
                'description': (
                    'Only objects with (or, with catalog_match=without, '
                    'without) a coincident source in this catalog.'
                ),
            },
            CATALOG_MATCH_PARAM: {
                'type': 'string', 'enum': list(CATALOG_MATCH_VALUES),
                'description': 'with (default) or without; needs catalog.',
            },
            TOP_N_PARAM: {
                'type': 'integer', 'minimum': 1, 'maximum': max_objects,
                'default': int(settings.SUMMARY_DEFAULT_TOP_N),
                'description': (
                    f'How many most reliable objects to list, 1 to {max_objects}.'
                ),
            },
        },
    }


def _summary_retry(exc: ApiError) -> str:
    """A retry shape for a rejected summary argument, by the parameter named."""
    named = exc.params or ([exc.param] if exc.param is not None else [])
    max_nights = _max_nights()
    if exc.code == ErrorCode.FILTERS_SPAN_CATALOGS:
        return 'Retry with a single catalog.'
    forms = {
        'span' if name in (START_PARAM, END_PARAM) else name
        for name in named if name in WINDOW_PARAMS
    }
    if len(forms) > 1:
        return 'Retry with only one of night, last_nights, or start and end.'
    if TOP_N_PARAM in named:
        return f'Retry with top_n from 1 to {int(settings.MCP_MAX_OBJECTS)}.'
    if LAST_NIGHTS_PARAM in named:
        return f'Retry with last_nights from 1 to {max_nights}.'
    if START_PARAM in named or END_PARAM in named:
        return (
            f'Retry with a start and end touching at most {max_nights} nights and '
            f'ending by the latest begun night, or with last_nights (1 to {max_nights}).'
        )
    if NIGHT_PARAM in named:
        return (
            'Retry with night as a begun dayObs date (YYYY-MM-DD), or with no '
            'window for the latest night.'
        )
    return 'Retry with one of the values listed in the message.'


def _parse_summary(arguments: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
    """Validate the window, filters and ``top_n`` with the API's own parser.

    Returns:
        The summary arguments and the query moment they were checked at, so
        ``run`` summarizes the same window.

    Raises:
        ToolArgumentError: For any argument the API rejects, naming the limit
            and how to retry.
    """
    params = {name: arguments.get(name) for name in _SUMMARY_ARGUMENTS}
    now = _now()
    try:
        parse_summary_params(params, now=now)
    except ApiError as exc:
        raise ToolArgumentError(f'{exc.message}. {_summary_retry(exc)}') from None
    return params, now


def _run_summary(
    guard: RequestGuard, parsed: tuple[dict[str, Any], datetime],
) -> RunResult:
    """Summarize the window with one ``summarize_window`` call and project it.

    It runs at REPEATABLE READ, so every number comes from one snapshot (KTD4).
    """
    params, now = parsed

    def work() -> dict[str, Any]:
        return project_summary(summarize_window(params, now=now))

    projection = run_guarded(guard, work, isolation='repeatable_read')
    return projection, {
        'objects_total': projection['objects_total'],
        'top_returned': projection['top_returned'],
        'nights': len(projection['window']['nights']),
    }


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
        Tool(
            name='summarize_rubin_transients_by_night',
            title='Summarize Rubin transients by night',
            description=_summary_description,
            input_schema=_summary_schema,
            parse=_parse_summary,
            run=_run_summary,
        ),
    )
}
