"""Filters and counts for the new queries (R13, R14, R15; KTD9, KTD10).

The filterable properties come from a declarative registry beside
``payload_columns``: each ``CROSSMATCH_CATALOGS`` entry's ``filter_columns``
(column -> unit) and ``derived_filter_columns`` (a ratio of two payload
columns, e.g. Gaia ``parallax_over_error``), validated at settings import.
``filter_parameters`` turns the registry, read live, into the public filter
parameters, which the parser, the OpenAPI document, and ``api/describe`` share:

* ``<catalog>.<column>_min`` / ``_max``: inclusive bounds on one catalog
  property, the column lowercased (``gaia_dr3.parallax_over_error_min``,
  ``des_y6_gold.dnf_z_max``). A match filter.
* ``catalog``: only sources in this catalog count. A match filter.
* ``separation_arcsec_max``: only sources within this separation, arcsec
  (inclusive). A match filter.
* ``reliability_min`` / ``reliability_max``: inclusive bounds on the object's
  stored LSST reliability. An object filter.

An object qualifies when it is in the service, meets every object filter, and,
when any match filter is present, has at least one current-version source (of
a MATCHED or NOTIFIED object) that satisfies every match filter; objects with
no coincident source do not qualify. Match filters apply within one catalog's
source, so filters naming more than one catalog are rejected with
``filters_span_catalogs``.

Qualification runs in one SQL statement over the candidate set the inputs
already bound (IDs, cone members); no JSONB expression indexes are needed. A
stored value is read only when its JSON type is a number, through one canonical
``double precision`` cast, so a null or non-numeric value never qualifies and
never raises.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection

from api.contract import (
    ErrorCode,
    InputStatus,
    ObjectStatus,
    QualifiesReason,
    _parse_number,
)
from api.errors import ApiError, InvalidQuery
from api.guard import check_deadline, sql_phase
from core.models import Alert, CatalogMatch

#: ``response`` values of the new queries (KTD10).
RESPONSE_OBJECTS = 'objects'
RESPONSE_COUNT = 'count'
RESPONSE_MODES = (RESPONSE_OBJECTS, RESPONSE_COUNT)

#: The generic filter names (R13).
CATALOG_FILTER = 'catalog'
SEPARATION_MAX_FILTER = 'separation_arcsec_max'
RELIABILITY_MIN_FILTER = 'reliability_min'
RELIABILITY_MAX_FILTER = 'reliability_max'
GENERIC_FILTERS = (
    CATALOG_FILTER, SEPARATION_MAX_FILTER, RELIABILITY_MIN_FILTER,
    RELIABILITY_MAX_FILTER,
)

#: ``FilterParam.kind`` values.
KIND_COLUMN = 'column'
KIND_CATALOG = 'catalog'
KIND_SEPARATION = 'separation'
KIND_RELIABILITY = 'reliability'
MATCH_FILTER_KINDS = (KIND_COLUMN, KIND_CATALOG, KIND_SEPARATION)

_BOUND_SUFFIXES = ('_min', '_max')

#: The reasons an object entry can carry; ``counts.objects`` reports each.
OBJECT_QUALIFIES_REASONS = (
    QualifiesReason.MEETS_FILTERS.value,
    QualifiesReason.OBJECT_FILTERS_NOT_MET.value,
    QualifiesReason.NO_SOURCE_MEETS_FILTERS.value,
    QualifiesReason.NOT_IN_SERVICE.value,
    QualifiesReason.CROSSMATCH_PENDING.value,
    QualifiesReason.NO_COINCIDENT_SOURCE.value,
    QualifiesReason.NOT_SEARCHED.value,
)

_CROSSMATCHED = (Alert.Status.MATCHED.value, Alert.Status.NOTIFIED.value)


@dataclass(frozen=True)
class FilterField:
    """One filterable catalog property.

    Attributes:
        catalog: The catalog name.
        name: The public, lowercased property name.
        unit: The unit.
        column: The stored column (upstream-native case) or ``None`` when
            derived.
        numerator: A derived property's numerator column.
        denominator: A derived property's denominator column.
        description: What the property is.
    """

    catalog: str
    name: str
    unit: str
    column: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    description: str = ''

    def sql(self) -> tuple[str, list[Any]]:
        """The property's value on match row ``m`` as SQL, null when unusable.

        Returns:
            ``(expression, params)``.
        """
        if self.column is not None:
            return _as_double(self.column)
        num_sql, num_params = _as_double(self.numerator)
        den_sql, den_params = _as_double(self.denominator)
        return f'({num_sql} / NULLIF({den_sql}, 0))', num_params + den_params


@dataclass(frozen=True)
class FilterParam:
    """One public filter parameter.

    Attributes:
        name: The parameter name.
        kind: ``column``, ``catalog``, ``separation``, or ``reliability``.
        bound: ``min`` or ``max`` for a bound, else ``None``.
        description: What the parameter does (with its unit).
        field: The catalog property of a ``column`` parameter.
    """

    name: str
    kind: str
    bound: str | None
    description: str
    field: FilterField | None = None

    @property
    def is_match_filter(self) -> bool:
        """Whether the parameter filters coincident sources (vs the object)."""
        return self.kind in MATCH_FILTER_KINDS

    @property
    def unit(self) -> str | None:
        """The unit of the parameter's value, if it has one."""
        if self.field is not None:
            return self.field.unit
        return {KIND_SEPARATION: 'arcsec', KIND_RELIABILITY: 'probability'}.get(self.kind)


def _as_double(column: str) -> tuple[str, list[Any]]:
    """The one canonical read of a stored payload value as ``double precision``.

    Payload keys are stored lowercased. The value is cast only when its JSON
    type is a number (``CASE`` guarantees the cast is not evaluated
    otherwise), so a null, string, or missing value reads as SQL null.
    """
    key = column.lower()
    return (
        "(CASE WHEN jsonb_typeof(m.catalog_payload -> %s) = 'number' "
        "THEN (m.catalog_payload ->> %s)::double precision END)",
        [key, key],
    )


def filter_fields() -> list[FilterField]:
    """The filterable catalog properties, from live settings (R13).

    Returns:
        Every catalog's ``filter_columns`` then ``derived_filter_columns``, in
        configured order.
    """
    fields = []
    for cat in settings.CROSSMATCH_CATALOGS:
        name = cat['name']
        for column, unit in (cat.get('filter_columns') or {}).items():
            fields.append(FilterField(
                catalog=name, name=column.lower(), unit=unit, column=column,
                description=f'{name} catalog column {column}, as stored with the match',
            ))
        for derived, spec in (cat.get('derived_filter_columns') or {}).items():
            fields.append(FilterField(
                catalog=name, name=derived.lower(), unit=spec['unit'],
                numerator=spec['numerator'], denominator=spec['denominator'],
                description=(
                    f'{name} {spec["numerator"]} / {spec["denominator"]}, computed '
                    'from the stored values (signed; unusable, so never '
                    'qualifying, when either is missing or the denominator is 0)'
                ),
            ))
    return fields


def filter_parameters() -> list[FilterParam]:
    """Every public filter parameter, from live settings (R13).

    Returns:
        The generic filters, then ``<catalog>.<column>_min`` and ``_max`` of
        every filterable catalog property.
    """
    params = [
        FilterParam(
            CATALOG_FILTER, KIND_CATALOG, None,
            'Match filter: only coincident sources from this catalog count. It '
            'names a catalog, so it cannot be combined with a filter on another '
            'catalog.',
        ),
        FilterParam(
            SEPARATION_MAX_FILTER, KIND_SEPARATION, 'max',
            'Match filter: only coincident sources at most this far from the '
            'object count, arcsec (inclusive).',
        ),
        FilterParam(
            RELIABILITY_MIN_FILTER, KIND_RELIABILITY, 'min',
            "Object filter: inclusive minimum of the object's stored LSST "
            'reliability; an object with none does not qualify.',
        ),
        FilterParam(
            RELIABILITY_MAX_FILTER, KIND_RELIABILITY, 'max',
            "Object filter: inclusive maximum of the object's stored LSST "
            'reliability; an object with none does not qualify.',
        ),
    ]
    for field in filter_fields():
        for bound, word in (('min', 'minimum'), ('max', 'maximum')):
            params.append(FilterParam(
                f'{field.catalog}.{field.name}_{bound}', KIND_COLUMN, bound,
                f'Match filter: inclusive {word} of {field.description}; unit: '
                f'{field.unit}. A null or non-numeric stored value never qualifies.',
                field=field,
            ))
    return params


def is_filter_param(name: str) -> bool:
    """Whether a parameter name has the form of a filter.

    A generic filter name, a catalog-qualified name (containing ``.``), or a
    name ending in ``_min`` or ``_max``. Such a name that is not a known filter
    is an error, never silently ignored.
    """
    return name in GENERIC_FILTERS or '.' in name or name.endswith(_BOUND_SUFFIXES)


def filter_query_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """The filter-shaped parameters of a query string (last value of each)."""
    return {name: params.get(name) for name in params if is_filter_param(name)}


def parse_response_mode(value: Any) -> str:
    """The ``response`` mode, defaulted to ``objects``.

    Raises:
        InvalidQuery: If the value is not ``objects`` or ``count``.
    """
    if value is None:
        return RESPONSE_OBJECTS
    if not isinstance(value, str) or value not in RESPONSE_MODES:
        raise InvalidQuery(
            f'response must be one of {RESPONSE_MODES}, got {value!r}', param='response'
        )
    return value


@dataclass(frozen=True)
class Bound:
    """One inclusive bound on a catalog property."""

    field: FilterField
    bound: str
    value: float


@dataclass(frozen=True)
class FilterSpec:
    """A request's parsed filters.

    Attributes:
        echo: The normalized filters, echoed as ``filters`` in the response.
        catalog: The one catalog the match filters name, if any.
        catalog_filter: Whether ``catalog`` was given.
        bounds: Bounds on catalog properties.
        separation_max: ``separation_arcsec_max``, if given.
        reliability_min: ``reliability_min``, if given.
        reliability_max: ``reliability_max``, if given.
    """

    echo: dict[str, Any]
    catalog: str | None
    catalog_filter: bool
    bounds: tuple[Bound, ...]
    separation_max: float | None
    reliability_min: float | None
    reliability_max: float | None

    @property
    def has_match_filters(self) -> bool:
        """Whether any filter applies to coincident sources."""
        return self.catalog_filter or bool(self.bounds) or self.separation_max is not None


def parse_filters(raw: Any, *, allow_string: bool) -> FilterSpec | None:
    """Parse and validate a request's filters (R13, R14; KTD9 step 2).

    Args:
        raw: ``{filter name: value}``: the ``filters`` body object, or the
            filter-shaped query parameters.
        allow_string: Accept decimal strings for numbers (query strings).

    Returns:
        The parsed filters, or ``None`` when there are none.

    Raises:
        InvalidQuery: If ``raw`` is not an object (``param`` ``filters``), a
            name is unknown or a value invalid (``param`` names it).
        ApiError: ``filters_span_catalogs`` when the match filters name more
            than one catalog (``params`` lists the parameters naming them).
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidQuery('filters must be a JSON object of filter: value', param='filters')
    if not raw:
        return None
    known = {param.name: param for param in filter_parameters()}
    catalogs = [cat['name'] for cat in settings.CROSSMATCH_CATALOGS]
    echo: dict[str, Any] = {}
    named: dict[str, list[str]] = {}
    bounds: list[Bound] = []
    values: dict[str, float] = {}
    for name, value in raw.items():
        param = known.get(name) if isinstance(name, str) else None
        if param is None:
            raise InvalidQuery(
                f'unknown filter {name!r}; filters are the generic '
                f'{", ".join(GENERIC_FILTERS)} and <catalog>.<column>_min|_max '
                'for the documented catalog columns (see the OpenAPI document)',
                param=str(name),
            )
        if param.kind == KIND_CATALOG:
            if not isinstance(value, str) or value not in catalogs:
                raise InvalidQuery(
                    f'catalog must be one of {catalogs}, got {value!r}', param=name
                )
            echo[name] = value
            named.setdefault(value, []).append(name)
            continue
        number = _parse_number(value, name, allow_string)
        if param.kind == KIND_SEPARATION and number < 0:
            raise InvalidQuery(f'{name} must not be negative, got {value!r}', param=name)
        echo[name] = number
        if param.kind == KIND_COLUMN:
            bounds.append(Bound(param.field, param.bound, number))
            named.setdefault(param.field.catalog, []).append(name)
        else:
            values[name] = number
    if len(named) > 1:
        conflicting = [name for names in named.values() for name in names]
        raise ApiError(
            "match filters apply within one catalog's source, but these filters "
            f'name {len(named)} catalogs ({", ".join(named)}): '
            f'{", ".join(conflicting)}',
            code=ErrorCode.FILTERS_SPAN_CATALOGS,
            status=400,
            params=conflicting,
        )
    return FilterSpec(
        echo=echo,
        catalog=next(iter(named), None),
        catalog_filter=CATALOG_FILTER in echo,
        bounds=tuple(bounds),
        separation_max=values.get(SEPARATION_MAX_FILTER),
        reliability_min=values.get(RELIABILITY_MIN_FILTER),
        reliability_max=values.get(RELIABILITY_MAX_FILTER),
    )


@dataclass(frozen=True)
class Qualification:
    """What the filters found for one object in the service.

    Attributes:
        object_ok: The object meets every object filter.
        match_ok: A current source of the crossmatched object satisfies every
            match filter (``True`` when there are no match filters).
    """

    object_ok: bool
    match_ok: bool


def qualify_objects(ids: Iterable[int], spec: FilterSpec) -> dict[int, Qualification]:
    """Evaluate the filters for a candidate set of objects, in one statement.

    A source counts only when it is its (object, catalog, source) row's latest
    match version, the version responses list, and only for MATCHED and
    NOTIFIED objects (KTD9 step 5).

    Args:
        ids: The candidate ``diaObjectId`` values.
        spec: The filters.

    Returns:
        ``{diaObjectId: Qualification}`` for every candidate in the service;
        IDs not in the service are absent.
    """
    unique = list(dict.fromkeys(int(i) for i in ids))
    if not unique:
        return {}

    object_terms, object_params = [], []
    if spec.reliability_min is not None:
        object_terms.append('a.reliability >= %s')
        object_params.append(spec.reliability_min)
    if spec.reliability_max is not None:
        object_terms.append('a.reliability <= %s')
        object_params.append(spec.reliability_max)
    object_sql = (
        f"COALESCE(({' AND '.join(object_terms)}), FALSE)" if object_terms else 'TRUE'
    )

    match_sql, match_params = 'TRUE', []
    if spec.has_match_filters:
        terms, match_params = [], []
        if spec.catalog is not None:
            terms.append('m.catalog_name = %s')
            match_params.append(spec.catalog)
        if spec.separation_max is not None:
            terms.append('m.match_distance_arcsec <= %s')
            match_params.append(spec.separation_max)
        for bound in spec.bounds:
            value_sql, value_params = bound.field.sql()
            terms.append(f"{value_sql} {'>=' if bound.bound == 'min' else '<='} %s")
            match_params.extend(value_params + [bound.value])
        matches = CatalogMatch._meta.db_table
        match_sql = f'''(a.status IN (%s, %s) AND EXISTS (
            SELECT 1 FROM {matches} m
            WHERE m.lsst_diaobject_diaobjectid = a.lsst_diaobject_diaobjectid
              AND NOT EXISTS (
                  SELECT 1 FROM {matches} n
                  WHERE n.lsst_diaobject_diaobjectid = m.lsst_diaobject_diaobjectid
                    AND n.catalog_name = m.catalog_name
                    AND n.catalog_source_id = m.catalog_source_id
                    AND n.match_version > m.match_version
              )
              {''.join(f' AND {term}' for term in terms)}
        ))'''
        match_params = [*_CROSSMATCHED, *match_params]

    sql = f'''
        SELECT a.lsst_diaobject_diaobjectid, {object_sql}, {match_sql}
        FROM {Alert._meta.db_table} a
        WHERE a.lsst_diaobject_diaobjectid = ANY(%s::bigint[])
    '''
    with sql_phase():
        with connection.cursor() as cursor:
            cursor.execute(sql, [*object_params, *match_params, unique])
            rows = cursor.fetchall()
    return {
        int(object_id): Qualification(bool(object_ok), bool(match_ok))
        for object_id, object_ok, match_ok in rows
    }


def qualifies(qualification: Qualification | None, spec: FilterSpec) -> bool:
    """Whether an object qualifies (R14)."""
    return (
        qualification is not None
        and qualification.object_ok
        and (qualification.match_ok or not spec.has_match_filters)
    )


def object_qualification(
    status: str, qualification: Qualification | None, spec: FilterSpec,
) -> tuple[bool, str]:
    """``(qualifies, qualifies_reason)`` of one object entry.

    Args:
        status: The object's ``ObjectStatus``.
        qualification: The object's ``Qualification``; ``None`` when it is
            not in the service.
        spec: The filters.

    Returns:
        The mark and its ``QualifiesReason``.
    """
    if qualification is None or status == ObjectStatus.NOT_IN_SERVICE:
        return False, QualifiesReason.NOT_IN_SERVICE.value
    if not qualification.object_ok:
        return False, QualifiesReason.OBJECT_FILTERS_NOT_MET.value
    if qualifies(qualification, spec):
        return True, QualifiesReason.MEETS_FILTERS.value
    if status == ObjectStatus.COINCIDENT_SOURCES:
        return False, QualifiesReason.NO_SOURCE_MEETS_FILTERS.value
    # crossmatch_pending, no_coincident_source, not_searched: the status says why.
    return False, str(status)


def _result_candidates(
    result: dict[str, Any], candidates: Mapping[int, list[int]],
) -> list[int]:
    """Every object ID a result stands for: its cone members, else its objects."""
    if result['index'] in candidates:
        return candidates[result['index']]
    return [int(obj['diaObjectId']) for obj in result['objects']]


def apply_filters(
    results: list[dict[str, Any]],
    candidates: Mapping[int, list[int]],
    spec: FilterSpec,
) -> None:
    """Mark every result and object entry ``qualifies`` with a reason (KTD9 step 4).

    Nothing is dropped. An ID result carries its object's mark. A position or
    TNS result carries ``qualifying_total``, the qualifying objects among all
    objects within its radius (listed or not), and qualifies when that is
    positive; an input with no search run or no object reports its status as
    the reason. A malformed input never qualifies (``invalid_input``).

    Args:
        results: The response results, marked in place.
        candidates: ``{result index: every object ID within the radius}`` for
            position and TNS results whose listing may be capped.
        spec: The filters.
    """
    all_ids = [i for result in results for i in _result_candidates(result, candidates)]
    quals = qualify_objects(all_ids, spec)
    for result in results:
        check_deadline()
        for obj in result['objects']:
            obj['qualifies'], obj['qualifies_reason'] = object_qualification(
                obj['status'], quals.get(int(obj['diaObjectId'])), spec,
            )
        if result['status'] == InputStatus.INVALID_INPUT:
            result['qualifies'] = False
            result['qualifies_reason'] = QualifiesReason.INVALID_INPUT.value
        elif 'total' not in result:  # an ID input: its one object
            (obj,) = result['objects']
            result['qualifies'] = obj['qualifies']
            result['qualifies_reason'] = obj['qualifies_reason']
        elif result['total'] is None or not result['total']:
            result['qualifying_total'] = None if result['total'] is None else 0
            result['qualifies'] = False
            result['qualifies_reason'] = result['status']
        else:
            count = sum(
                qualifies(quals.get(i), spec)
                for i in _result_candidates(result, candidates)
            )
            result['qualifying_total'] = count
            result['qualifies'] = count > 0
            result['qualifies_reason'] = (
                QualifiesReason.MEETS_FILTERS if count
                else QualifiesReason.NO_OBJECT_MEETS_FILTERS
            ).value


def count_body(
    results: list[dict[str, Any]], spec: FilterSpec | None,
) -> dict[str, Any]:
    """The ``response=count`` body for fully resolved results (R15; KTD10).

    Args:
        results: The results, listing every object of each input (uncapped),
            marked by ``apply_filters`` when ``spec`` is set.
        spec: The filters, or ``None``.

    Returns:
        ``response``, ``filters`` (the echo, or null), and ``counts``:
        ``inputs`` (per input, duplicates included) and ``objects`` (distinct
        ``diaObjectId`` values), each with ``total``, ``by_status`` (every
        status, zero-filled), and ``qualifying`` (null without filters);
        ``objects`` adds ``by_qualifies_reason`` (null without filters).
    """
    input_statuses = dict.fromkeys(ObjectStatus.values + InputStatus.values, 0)
    object_statuses = dict.fromkeys(ObjectStatus.values, 0)
    reasons = dict.fromkeys(OBJECT_QUALIFIES_REASONS, 0) if spec is not None else None
    objects: dict[int, dict[str, Any]] = {}
    for result in results:
        input_statuses[result['status']] += 1
        for obj in result['objects']:
            objects.setdefault(int(obj['diaObjectId']), obj)
    qualifying_objects = 0
    for obj in objects.values():
        object_statuses[obj['status']] += 1
        if spec is not None:
            reasons[obj['qualifies_reason']] += 1
            qualifying_objects += bool(obj['qualifies'])
    return {
        'response': RESPONSE_COUNT,
        'filters': spec.echo if spec is not None else None,
        'counts': {
            'inputs': {
                'total': len(results),
                'by_status': input_statuses,
                'qualifying': (
                    sum(bool(result['qualifies']) for result in results)
                    if spec is not None else None
                ),
            },
            'objects': {
                'total': len(objects),
                'by_status': object_statuses,
                'qualifying': qualifying_objects if spec is not None else None,
                'by_qualifies_reason': reasons,
            },
        },
    }
