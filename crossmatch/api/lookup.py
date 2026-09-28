"""Object lookup by ID and the batch lookup framework (R1, R2, R7, R8; KTD1-KTD5).

``lookup_objects`` answers one ordered list of tagged inputs; ``get_object`` is
the single-ID form of the same query. Both return a JSON-native dict: the
service-level provenance envelope, the ``provenance_sets`` the results refer
to, and one result per input, in input order, echoing the input exactly as sent
(duplicates included).

The framework has three parts, so new input kinds plug in without touching the
rest:

* ``InputKind``: one kind of input (``id`` here; ``position`` and ``tns`` are
  registered by ``api.positions``, imported at the end of this module). A kind
  parses and validates one raw input and resolves all of its parsed inputs in
  set-based queries, returning each input's ``status`` and ``objects``. Kinds
  with the same ``group`` are resolved together, in one call, so they can
  share one query and one per-request object budget in input order.
* Per-entry validation: a malformed input becomes an ``invalid_input`` result
  naming the offending field; it never fails the request (R28). Only
  request-level problems (not a list, empty, over the per-request maximum, a
  bad ``detail`` or shared ``radius_arcsec``) raise ``InvalidQuery``.
* ``build_objects``: the shared object builder. Every kind that finds Rubin
  objects turns their IDs into object entries through it, so object status
  (the status resolution in the plan's High-Level Technical Design), R10's
  object fields, per-catalog search outcomes, matches, and provenance are the
  same whichever input found the object.

Every entry point also takes filters and ``response`` (``api.filters``,
KTD9, KTD10): with filters, every result and object is marked ``qualifies``
with a reason and nothing is dropped; ``response=count`` answers the same
inputs and filters with counts instead of objects, uncapped by the listing
limits.

Matches are read only for MATCHED and NOTIFIED objects, so partial rows left by
a reverted batch never surface on a pending object (KTD9.5). Queries run in
``api.guard.sql_phase`` blocks and long loops call ``check_deadline``, so a
guarded view stays inside the request budget (KTD12).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from api.contract import (
    InputStatus,
    ObjectStatus,
    ReadTimeCatalogOutcome,
    dia_object_id_fields,
    envelope,
    parse_dia_object_id,
    parse_radius_arcsec,
)
from api.errors import InvalidQuery
from api.filters import (
    RESPONSE_COUNT,
    FilterSpec,
    apply_filters,
    count_body,
    parse_filters,
    parse_response_mode,
)
from api.guard import check_deadline, sql_phase
from api.service import DEFAULT_DETAIL, DETAIL_LEVELS, _load_matches
from core import provenance
from core.log import get_logger
from core.models import (
    Alert,
    AlertDelivery,
    CatalogMatch,
    CatalogSearchOutcome,
    ObjectCrossmatchRecord,
)
from matching.payload import _to_json_scalar

logger = get_logger(__name__)

#: Key of the best-guess entry in ``provenance_sets``: current settings offered
#: for objects whose provenance was not recorded, labeled as a guess (R19).
BEST_GUESS_PROVENANCE_SET = 'best_guess_current_settings'

#: ``basis`` of a ``provenance_sets`` entry.
BASIS_RECORDED = 'recorded'
BASIS_BEST_GUESS = BEST_GUESS_PROVENANCE_SET

#: ``crossmatch.provenance`` of an object.
PROVENANCE_RECORDED = 'recorded'
PROVENANCE_NOT_RECORDED = 'not_recorded'

#: Top-level fields of a ``POST api/lookup`` body.
LOOKUP_BODY_FIELDS = ('inputs', 'detail', 'radius_arcsec', 'filters', 'response')

_BEST_GUESS_DESCRIPTION = (
    'A best guess, not a record: the current service settings, offered for '
    'objects crossmatched without recorded provenance. The settings in effect '
    'when those objects were crossmatched may have differed.'
)

_CROSSMATCHED = (Alert.Status.MATCHED, Alert.Status.NOTIFIED)


class InputError(Exception):
    """One batch input is malformed; reported in its own result (R28).

    Attributes:
        message: Why the input is malformed.
        param: The offending field, e.g. ``inputs[3].diaObjectId``.
    """

    def __init__(self, message: str, param: str) -> None:
        """Build the error.

        Args:
            message: Why the input is malformed.
            param: The offending field.
        """
        super().__init__(message)
        self.message = message
        self.param = param


@dataclass
class ParsedInput:
    """One request input after per-entry validation.

    Attributes:
        index: Position in the request's input list.
        raw: The input exactly as sent (echoed as ``input``).
        kind: The kind as sent, when it is a string; else ``None``.
        value: The kind-specific parsed value (e.g. the int ID).
        normalized: The JSON-native normalized form (echoed as ``normalized``).
        error: Set when the input is malformed.
    """

    index: int
    raw: Any
    kind: str | None
    value: Any = None
    normalized: dict[str, Any] | None = None
    error: InputError | None = None


@dataclass
class LookupContext:
    """State shared by every input kind while one request is answered.

    Attributes:
        detail: The detail level.
        radius_arcsec: The request's shared radius, for position-like inputs
            that carry none; ``None`` when not given.
        provenance_sets: The response's ``provenance_sets`` map, filled as
            objects are built.
        object_budget: Objects the request may still list (the per-request
            total, KTD12); kinds whose inputs list a variable number of
            objects spend it in input order.
        truncated: Set when the per-request total cut any input's objects.
        filters: The request's filters, if any.
        count_only: ``response=count``: kinds list every object of each input,
            uncapped, and the response carries counts instead of results.
        candidates: ``{input index: every object ID within the radius}``,
            recorded by the cone kinds when filters or counts need objects
            beyond the listed ones.
    """

    detail: str
    radius_arcsec: float | None = None
    provenance_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    object_budget: int = 0
    truncated: bool = False
    filters: FilterSpec | None = None
    count_only: bool = False
    candidates: dict[int, list[int]] = field(default_factory=dict)

    @property
    def needs_candidates(self) -> bool:
        """Whether kinds must find every object of an input, not only the listed."""
        return self.count_only or self.filters is not None


class InputKind:
    """One kind of batch input (KTD3). Subclass and register in ``INPUT_KINDS``.

    Attributes:
        name: The ``kind`` tag.
        fields: The fields an input of this kind may carry besides ``kind``.
        max_setting: The settings name of the per-request maximum of inputs of
            this kind. Kinds naming the same setting share that maximum.
        group: Kinds with the same group are resolved together: ``resolve``
            of the group's first kind receives every valid input of the group,
            in request order. Empty means the kind is its own group.
        fixed_objects: The number of objects every input of this kind lists,
            reserved from the per-request object budget before resolution;
            ``None`` when the number varies (the kind spends the budget).
    """

    name: str = ''
    fields: tuple[str, ...] = ()
    max_setting: str = ''
    group: str = ''
    fixed_objects: int | None = None

    def parse(
        self, raw: dict[str, Any], param: str, ctx: LookupContext,
    ) -> tuple[Any, dict[str, Any]]:
        """Validate one input of this kind.

        Args:
            raw: The input object (already known to be a dict of this kind with
                no unknown fields).
            param: The input's parameter path, e.g. ``inputs[3]``.
            ctx: The request context (e.g. the shared ``radius_arcsec``).

        Returns:
            ``(value, normalized)``: the parsed value handed to ``resolve`` and
            the JSON-native normalized form echoed in the result.

        Raises:
            InputError: If the input is malformed.
        """
        raise NotImplementedError

    def resolve(
        self, entries: list[ParsedInput], ctx: LookupContext,
    ) -> dict[int, dict[str, Any]]:
        """Answer every valid input of this kind in set-based queries.

        Args:
            entries: The valid inputs of this kind (or of its group), in
                request order.
            ctx: The request context; object entries come from
                ``build_objects(ids, ctx)``.

        Returns:
            ``{index: result_fields}``, where ``result_fields`` holds at least
            ``status`` and ``objects`` and may add kind-specific fields.
        """
        raise NotImplementedError


class IdInput(InputKind):
    """``{"kind": "id", "diaObjectId": <int or decimal string>}`` (R1, R2, KTD4)."""

    name = 'id'
    fields = ('diaObjectId',)
    max_setting = 'API_MAX_IDS'
    fixed_objects = 1

    def parse(
        self, raw: dict[str, Any], param: str, ctx: LookupContext,
    ) -> tuple[Any, dict[str, Any]]:
        """Parse the ID; see ``InputKind.parse``."""
        id_param = f'{param}.diaObjectId'
        if 'diaObjectId' not in raw:
            raise InputError('diaObjectId is required', id_param)
        try:
            object_id = parse_dia_object_id(raw['diaObjectId'], param=id_param)
        except InvalidQuery as exc:
            raise InputError(exc.message, id_param) from None
        return object_id, {'kind': self.name, **dia_object_id_fields(object_id)}

    def resolve(
        self, entries: list[ParsedInput], ctx: LookupContext,
    ) -> dict[int, dict[str, Any]]:
        """Resolve the IDs; each input's status is its object's status."""
        objects = build_objects([entry.value for entry in entries], ctx)
        return {
            entry.index: {
                'status': objects[entry.value]['status'],
                'objects': [objects[entry.value]],
            }
            for entry in entries
        }


#: The input kinds, by ``kind`` tag. ``api.positions`` registers ``position``
#: and ``tns``.
INPUT_KINDS: dict[str, InputKind] = {IdInput.name: IdInput()}


def lookup_objects(
    *,
    inputs: Any,
    detail: str | None = None,
    radius_arcsec: Any = None,
    filters: Any = None,
    response: Any = None,
) -> dict[str, Any]:
    """Answer an ordered list of tagged inputs, one result per input (R2, R5, R7).

    Args:
        inputs: The request's input list, as decoded from JSON. Each entry is a
            tagged object such as ``{"kind": "id", "diaObjectId": 123}``.
        detail: ``ids`` | ``position`` | ``matches`` (default) | ``full``.
        radius_arcsec: Shared search radius in arcsec for position and TNS
            inputs that carry none; at most ``API_MAX_CONE_RADIUS_ARCSEC``.
        filters: ``{filter name: value}`` (``api.filters``); JSON numbers.
        response: ``objects`` (default) or ``count``.

    Returns:
        The enveloped response: ``provenance``, ``detail``, ``count`` (number
        of results), ``provenance_sets``, ``truncated`` (whether the
        per-request object total cut any result), ``filters`` when filtered,
        and ``results``, one per input in input order; or, for
        ``response=count``, the counts body of ``api.filters.count_body``.

    Raises:
        InvalidQuery: If ``inputs`` is not a non-empty list, exceeds a
            per-request maximum, or ``detail``, ``radius_arcsec``, a filter,
            or ``response`` is invalid.
        ApiError: ``filters_span_catalogs`` (see ``api.filters``).
    """
    detail = _check_detail(detail)
    radius = (
        parse_radius_arcsec(radius_arcsec, param='radius_arcsec')
        if radius_arcsec is not None else None
    )
    spec = parse_filters(filters, allow_string=False)
    mode = parse_response_mode(response)
    if not isinstance(inputs, list):
        raise InvalidQuery('inputs must be a JSON array of input objects', param='inputs')
    if not inputs:
        raise InvalidQuery('inputs must contain at least one input', param='inputs')
    _check_request_size(inputs)
    ctx = _context(detail, spec, mode, radius_arcsec=radius)
    parsed = [_parse_input(index, raw, ctx) for index, raw in enumerate(inputs)]
    return _answer(parsed, ctx)


def get_object(
    *,
    dia_object_id: Any,
    detail: str | None = None,
    filters: Any = None,
    response: Any = None,
) -> dict[str, Any]:
    """Look up one object by ``diaObjectId`` (R1).

    The response has the same shape as ``lookup_objects`` with one result,
    whose ``input`` echoes ``dia_object_id`` as given.

    Args:
        dia_object_id: The ID, as an int or decimal string.
        detail: As for ``lookup_objects``.
        filters: ``{filter name: value}`` from the query string.
        response: ``objects`` (default) or ``count``.

    Returns:
        The enveloped response with one result, or the counts body.

    Raises:
        InvalidQuery: If the ID is malformed (``param`` ``diaObjectId``) or
            ``detail``, a filter, or ``response`` is invalid.
        ApiError: ``filters_span_catalogs``.
    """
    detail = _check_detail(detail)
    spec = parse_filters(filters, allow_string=True)
    mode = parse_response_mode(response)
    object_id = parse_dia_object_id(dia_object_id, param='diaObjectId')
    kind = INPUT_KINDS[IdInput.name]
    entry = ParsedInput(
        index=0,
        raw=dia_object_id,
        kind=kind.name,
        value=object_id,
        normalized={'kind': kind.name, **dia_object_id_fields(object_id)},
    )
    return _answer([entry], _context(detail, spec, mode))


def _context(
    detail: str,
    spec: FilterSpec | None,
    mode: str,
    *,
    radius_arcsec: float | None = None,
) -> LookupContext:
    """The request context; a count lists objects at detail ``ids`` only."""
    count_only = mode == RESPONSE_COUNT
    return LookupContext(
        detail='ids' if count_only else detail,
        radius_arcsec=radius_arcsec,
        filters=spec,
        count_only=count_only,
    )


def parse_lookup_body(body: Any) -> dict[str, Any]:
    """Map a decoded ``POST api/lookup`` body to ``lookup_objects`` kwargs.

    Args:
        body: The decoded JSON body.

    Returns:
        The keyword arguments for ``lookup_objects``.

    Raises:
        InvalidQuery: If the body is not an object (``param`` ``body``), has an
            unknown field (``param`` names it), or has no ``inputs``.
    """
    if not isinstance(body, dict):
        raise InvalidQuery('request body must be a JSON object', param='body')
    for key in body:
        if key not in LOOKUP_BODY_FIELDS:
            raise InvalidQuery(
                f'unknown field {key!r}; allowed fields: {", ".join(LOOKUP_BODY_FIELDS)}',
                param=str(key),
            )
    if 'inputs' not in body:
        raise InvalidQuery('inputs is required', param='inputs')
    return {key: body[key] for key in LOOKUP_BODY_FIELDS if key in body}


def _check_detail(detail: Any) -> str:
    """Return the detail level, defaulted, or raise ``InvalidQuery`` naming it."""
    if detail is None:
        return DEFAULT_DETAIL
    if not isinstance(detail, str) or detail not in DETAIL_LEVELS:
        raise InvalidQuery(
            f'detail must be one of {DETAIL_LEVELS}, got {detail!r}', param='detail'
        )
    return detail


def _raw_kind(raw: Any) -> str | None:
    """The ``kind`` an input was sent with, when it is a string."""
    if isinstance(raw, dict) and isinstance(raw.get('kind'), str):
        return raw['kind']
    return None


def _max_inputs(kind: InputKind) -> int:
    """The live per-request maximum of inputs of one kind."""
    return int(getattr(settings, kind.max_setting))


def _check_request_size(inputs: list[Any]) -> None:
    """Enforce the per-request maximums before any input is parsed (KTD12).

    Kinds naming the same ``max_setting`` share that maximum (``position`` and
    ``tns`` are both position searches). The whole list is capped by the sum
    of the maximums of the kinds it uses (the largest single maximum when it
    uses none), so malformed entries, which have no valid kind, cannot make a
    request larger than a well-formed one.

    Raises:
        InvalidQuery: If a maximum is exceeded (``param`` ``inputs``).
    """
    counts: dict[str, int] = {}
    kinds_by_setting: dict[str, list[str]] = {}
    for raw in inputs:
        kind = _raw_kind(raw)
        if kind in INPUT_KINDS:
            setting = INPUT_KINDS[kind].max_setting
            counts[setting] = counts.get(setting, 0) + 1
            names = kinds_by_setting.setdefault(setting, [])
            if kind not in names:
                names.append(kind)
    if counts:
        total_max = sum(int(getattr(settings, setting)) for setting in counts)
    else:
        total_max = max(_max_inputs(kind) for kind in INPUT_KINDS.values())
    if len(inputs) > total_max:
        raise InvalidQuery(
            f'inputs has {len(inputs)} entries; the maximum is {total_max}',
            param='inputs',
        )
    for setting, count in counts.items():
        limit = int(getattr(settings, setting))
        if count > limit:
            names = ', '.join(repr(name) for name in kinds_by_setting[setting])
            raise InvalidQuery(
                f'inputs has {count} inputs of kind {names}; the maximum is {limit}',
                param='inputs',
            )


def _parse_input(index: int, raw: Any, ctx: LookupContext) -> ParsedInput:
    """Validate one input; a malformed one carries its error instead of raising."""
    param = f'inputs[{index}]'
    entry = ParsedInput(index=index, raw=raw, kind=_raw_kind(raw))
    try:
        if not isinstance(raw, dict):
            raise InputError('each input must be a JSON object with a kind', param)
        if 'kind' not in raw:
            raise InputError('kind is required', f'{param}.kind')
        kind = INPUT_KINDS.get(entry.kind) if entry.kind is not None else None
        if kind is None:
            raise InputError(
                f'kind must be one of {sorted(INPUT_KINDS)}, got {raw["kind"]!r}',
                f'{param}.kind',
            )
        for key in raw:
            if key != 'kind' and key not in kind.fields:
                raise InputError(
                    f'unknown field {key!r} for kind {kind.name!r}', f'{param}.{key}'
                )
        entry.value, entry.normalized = kind.parse(raw, param, ctx)
    except InputError as exc:
        entry.error = exc
    return entry


def _answer(parsed: list[ParsedInput], ctx: LookupContext) -> dict[str, Any]:
    """Resolve the valid inputs group by group and assemble results in order.

    Before resolution the per-request object total is reduced by the objects
    that fixed-size kinds (one per ID) will list; the rest is the budget that
    variable-size kinds spend in input order.
    """
    by_group: dict[str, list[ParsedInput]] = {}
    reserved = 0
    for entry in parsed:
        if entry.error is None:
            kind = INPUT_KINDS[entry.kind]
            by_group.setdefault(kind.group or kind.name, []).append(entry)
            reserved += kind.fixed_objects or 0
    ctx.object_budget = max(0, int(settings.API_MAX_OBJECTS_PER_REQUEST) - reserved)
    resolved: dict[int, dict[str, Any]] = {}
    for entries in by_group.values():
        resolved.update(INPUT_KINDS[entries[0].kind].resolve(entries, ctx))

    results = []
    for entry in parsed:
        result: dict[str, Any] = {
            'index': entry.index,
            'kind': entry.kind,
            'input': entry.raw,
            'normalized': entry.normalized,
        }
        if entry.error is not None:
            result['status'] = InputStatus.INVALID_INPUT.value
            result['objects'] = []
            result['error'] = {
                'message': entry.error.message,
                'param': entry.error.param,
            }
        else:
            result.update(resolved[entry.index])
        results.append(result)

    return finish_response(results, ctx, {
        'detail': ctx.detail,
        'count': len(results),
        'provenance_sets': ctx.provenance_sets,
        'truncated': ctx.truncated,
        'results': results,
    })


def finish_response(
    results: list[dict[str, Any]], ctx: LookupContext, body: dict[str, Any],
) -> dict[str, Any]:
    """Apply the request's filters and response mode, and envelope the body.

    Args:
        results: The resolved results (also inside ``body``).
        ctx: The request context.
        body: The listing body.

    Returns:
        The enveloped listing, with ``filters`` when filtered, or the counts
        body for ``response=count``.
    """
    if ctx.filters is not None:
        apply_filters(results, ctx.candidates, ctx.filters)
    if ctx.count_only:
        return envelope(count_body(results, ctx.filters))
    if ctx.filters is not None:
        body['filters'] = ctx.filters.echo
    return envelope(body)


def _iso(value: Any) -> str | None:
    """An aware datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None


def build_objects(ids: Sequence[int], ctx: LookupContext) -> dict[int, dict[str, Any]]:
    """Build the object entry of every given ``diaObjectId`` (R8, R10, R11, R18, R19).

    Set-based: a fixed number of queries whatever the number of IDs. Repeated
    IDs are resolved once. Records the provenance sets the objects refer to in
    ``ctx.provenance_sets``.

    Args:
        ids: The IDs, as ints.
        ctx: The request context.

    Returns:
        ``{diaObjectId: object_entry}`` with an entry for every given ID,
        including ``not_in_service`` entries for IDs the service has never seen.
    """
    unique_ids = list(dict.fromkeys(int(i) for i in ids))
    detail = ctx.detail

    with sql_phase():
        alerts = {
            int(row['lsst_diaObject_diaObjectId']): row
            for row in Alert.objects.filter(
                lsst_diaObject_diaObjectId__in=unique_ids
            ).values(
                'lsst_diaObject_diaObjectId', 'status', 'ra_deg', 'dec_deg',
                'reliability', 'ingest_time', 'event_time',
            )
        }
    crossmatched = [oid for oid, row in alerts.items() if row['status'] in _CROSSMATCHED]

    records: dict[int, ObjectCrossmatchRecord] = {}
    record_by_version: dict[tuple[int, int], ObjectCrossmatchRecord] = {}
    has_matches: set[int] = set()
    matches: dict[int, list[dict[str, Any]]] = {}
    if crossmatched:
        with sql_phase():
            for record in (
                ObjectCrossmatchRecord.objects.filter(alert_id__in=crossmatched)
                .select_related('provenance_set')
                .order_by('alert_id', 'match_version')
            ):
                oid = int(record.alert_id)
                records[oid] = record  # ordered by version: the last is current
                record_by_version[(oid, int(record.match_version))] = record
            has_matches = {
                int(oid) for oid in CatalogMatch.objects.filter(alert_id__in=crossmatched)
                .values_list('alert_id', flat=True).distinct()
            }
        if detail in ('matches', 'full'):
            with sql_phase():
                matches = _load_matches(crossmatched, detail, include_match_version=True)

    brokers: dict[int, list[str]] = {}
    if detail != 'ids' and alerts:
        with sql_phase():
            for oid, broker in AlertDelivery.objects.filter(
                alert_id__in=list(alerts)
            ).values_list('alert_id', 'broker'):
                brokers.setdefault(int(oid), []).append(broker)

    catalogs_in_service = [cat['name'] for cat in provenance.catalog_releases()]
    objects: dict[int, dict[str, Any]] = {}
    for oid in unique_ids:
        check_deadline()
        obj: dict[str, Any] = dia_object_id_fields(oid)
        row = alerts.get(oid)
        if row is None:
            obj['status'] = ObjectStatus.NOT_IN_SERVICE.value
            objects[oid] = obj
            continue

        is_crossmatched = row['status'] in _CROSSMATCHED
        record = records.get(oid)
        if not is_crossmatched:
            status = ObjectStatus.CROSSMATCH_PENDING
        elif oid in has_matches:
            status = ObjectStatus.COINCIDENT_SOURCES
        elif record is not None and not any(
            outcome == CatalogSearchOutcome.SEARCHED
            for outcome in record.catalog_outcomes.values()
        ):
            status = ObjectStatus.NOT_SEARCHED
        else:
            status = ObjectStatus.NO_COINCIDENT_SOURCE
        obj['status'] = status.value

        if detail != 'ids':
            obj['ra'] = _to_json_scalar(row['ra_deg'])
            obj['dec'] = _to_json_scalar(row['dec_deg'])
            obj['reliability'] = _to_json_scalar(row['reliability'])
            obj['ingest_time'] = _iso(row['ingest_time'])
            obj['event_time'] = _iso(row['event_time'])
            obj['brokers'] = sorted(brokers.get(oid, []))

        obj['crossmatch'] = (
            _crossmatch_block(record, catalogs_in_service, ctx)
            if is_crossmatched else None
        )

        if detail in ('matches', 'full'):
            entries = []
            for match in matches.get(oid, []) if is_crossmatched else []:
                version = match.pop('match_version')
                matched_record = record_by_version.get((oid, version))
                match['provenance_set'] = (
                    _provenance_set_key(matched_record, ctx)
                    if matched_record is not None else None
                )
                entries.append(match)
            obj['matches'] = entries
        objects[oid] = obj
    return objects


def _provenance_set_key(record: ObjectCrossmatchRecord, ctx: LookupContext) -> str:
    """Add a record's provenance set to ``ctx.provenance_sets``; return its key."""
    pset = record.provenance_set
    key = pset.content_hash
    if key not in ctx.provenance_sets:
        ctx.provenance_sets[key] = {
            'basis': BASIS_RECORDED,
            'crossmatch_radius_arcsec': _to_json_scalar(pset.crossmatch_radius_arcsec),
            'catalogs': pset.catalogs,
            'reliability_cuts': pset.reliability_cuts,
        }
    return key


def _best_guess_key(ctx: LookupContext) -> str:
    """Add the labeled best-guess provenance set (current settings); return its key."""
    if BEST_GUESS_PROVENANCE_SET not in ctx.provenance_sets:
        current = provenance.service_provenance()
        ctx.provenance_sets[BEST_GUESS_PROVENANCE_SET] = {
            'basis': BASIS_BEST_GUESS,
            'description': _BEST_GUESS_DESCRIPTION,
            'crossmatch_radius_arcsec': current['crossmatch_radius_arcsec'],
            'catalogs': current['catalogs'],
            'reliability_cuts': current['reliability_cuts'],
        }
    return BEST_GUESS_PROVENANCE_SET


def _crossmatch_block(
    record: ObjectCrossmatchRecord | None,
    catalogs_in_service: list[str],
    ctx: LookupContext,
) -> dict[str, Any]:
    """What one crossmatched object's crossmatch searched, and under what provenance.

    With a record, every catalog in service reports its recorded outcome, or
    ``not_in_service_at_crossmatch`` when the record predates it; catalogs the
    record names that are no longer in service keep their recorded outcome.
    Without one, every catalog in service reports ``not_recorded``, the release
    that started recording is named, and the current settings are offered as a
    labeled best guess (R19, KTD5.7).

    Args:
        record: The object's current crossmatch record, if any.
        catalogs_in_service: Catalog names in service now, in configured order.
        ctx: The request context.

    Returns:
        The object's ``crossmatch`` block.
    """
    if record is None:
        return {
            'provenance': PROVENANCE_NOT_RECORDED,
            'match_version': None,
            'crossmatched_at': None,
            'provenance_set': None,
            'brokers_at_crossmatch': None,
            'catalog_outcomes': {
                name: ReadTimeCatalogOutcome.NOT_RECORDED.value
                for name in catalogs_in_service
            },
            'recording_release': provenance.PROVENANCE_RECORDING_RELEASE,
            'best_guess_provenance_set': _best_guess_key(ctx),
        }
    recorded = record.catalog_outcomes
    outcomes = {
        name: str(recorded.get(
            name, ReadTimeCatalogOutcome.NOT_IN_SERVICE_AT_CROSSMATCH.value
        ))
        for name in catalogs_in_service
    }
    for name, outcome in recorded.items():
        outcomes.setdefault(str(name), str(outcome))
    return {
        'provenance': PROVENANCE_RECORDED,
        'match_version': int(record.match_version),
        'crossmatched_at': _iso(record.crossmatched_at),
        'provenance_set': _provenance_set_key(record, ctx),
        'brokers_at_crossmatch': [str(b) for b in record.brokers],
        'catalog_outcomes': outcomes,
    }


# The position and TNS kinds build on this module and register themselves in
# INPUT_KINDS when imported; importing them here registers them whichever of
# the two modules is imported first.
from api import positions as _positions  # noqa: E402,F401
