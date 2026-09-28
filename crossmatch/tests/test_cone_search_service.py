"""U5: position inputs -- single and batched cone searches (service layer).

Covers R4, R5, R7, R8, R12, R16 and AE2 against ``api.positions`` and the
``position`` kind of ``api.lookup``. The HTTP adapters and response
conformance are covered by ``test_cone_search_view``.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from api.contract import InputStatus, ObjectStatus
from api.errors import InvalidQuery
from api.lookup import lookup_objects
from api.positions import cone_search
from core.healpix import radec_to_ipix
from core.models import Alert
from tests.factories import (
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
    set_ingest_time,
)

ARCSEC = 1.0 / 3600.0


def alert_at(ra, dec, **kwargs):
    """An alert at (ra, dec) with its HEALPix index set, as ingest does."""
    return AlertFactory(
        ra_deg=ra, dec_deg=dec, healpix_ipix=radec_to_ipix(ra, dec), **kwargs
    )


def matched_at(ra, dec):
    """A crossmatched object with a Gaia match at (ra, dec)."""
    record = ObjectCrossmatchRecordFactory(
        alert=alert_at(ra, dec, status=Alert.Status.MATCHED)
    )
    CatalogMatchFactory(alert=record.alert, catalog_name='gaia_dr3')
    return record.alert


def position(ra, dec, radius=None):
    entry = {'kind': 'position', 'ra': ra, 'dec': dec}
    if radius is not None:
        entry['radius_arcsec'] = radius
    return entry


def oid(alert):
    return alert.lsst_diaObject_diaObjectId


def ids_of(result):
    return [obj['diaObjectId'] for obj in result['objects']]


def only_result(body):
    assert len(body['results']) == 1
    return body['results'][0]


@pytest.mark.django_db
def test_empty_position_reports_no_rubin_object():
    # Covers AE2: an object 3 arcsec away is outside a 2 arcsec search.
    alert_at(150.0, 2.0 + 3 * ARCSEC)

    body = lookup_objects(inputs=[position(150.0, 2.0, 2.0)])

    result = only_result(body)
    assert result['status'] == InputStatus.NO_RUBIN_OBJECT
    assert result['objects'] == []
    assert result['total'] == 0
    assert result['truncated'] is False
    assert result['normalized'] == {
        'kind': 'position', 'ra': 150.0, 'dec': 2.0, 'radius_arcsec': 2.0,
    }


@pytest.mark.django_db
def test_single_empty_cone_reports_no_rubin_object():
    body = cone_search(ra='150.0', dec='2.0', radius_arcsec='2')

    result = only_result(body)
    assert result['status'] == InputStatus.NO_RUBIN_OBJECT
    assert result['objects'] == []
    assert body['next_cursor'] is None


@pytest.mark.django_db
def test_cone_with_pending_and_matched_objects_reports_each_status():
    pending = alert_at(150.0, 2.0)
    matched = matched_at(150.0 + 1 * ARCSEC, 2.0)

    body = lookup_objects(inputs=[position(150.0, 2.0, 5.0)])

    result = only_result(body)
    assert result['status'] == InputStatus.OBJECTS_FOUND
    assert result['total'] == 2
    statuses = {obj['diaObjectId']: obj['status'] for obj in result['objects']}
    assert statuses == {
        oid(pending): ObjectStatus.CROSSMATCH_PENDING,
        oid(matched): ObjectStatus.COINCIDENT_SOURCES,
    }
    by_id = {obj['diaObjectId']: obj for obj in result['objects']}
    assert by_id[oid(pending)]['separation_arcsec'] == pytest.approx(0.0, abs=1e-6)
    assert by_id[oid(matched)]['separation_arcsec'] == pytest.approx(1.0, rel=1e-3)
    assert by_id[oid(matched)]['matches'][0]['catalog_name'] == 'gaia_dr3'


@pytest.mark.django_db
def test_cone_straddling_ra_zero_finds_both_sides():
    west = alert_at(359.999, 10.0)
    east = alert_at(0.001, 10.0)

    body = lookup_objects(inputs=[position(0.0, 10.0, 10.0)])

    assert sorted(ids_of(only_result(body))) == sorted([oid(west), oid(east)])


@pytest.mark.django_db
def test_cone_at_the_pole_finds_objects_on_both_sides():
    # Two objects on opposite meridians, each 10 arcsec from the south pole.
    a = alert_at(10.0, -90.0 + 10 * ARCSEC)
    b = alert_at(190.0, -90.0 + 10 * ARCSEC)
    alert_at(100.0, -90.0 + 40 * ARCSEC)  # outside

    body = lookup_objects(inputs=[position(0.0, -90.0, 15.0)])

    assert sorted(ids_of(only_result(body))) == sorted([oid(a), oid(b)])


@pytest.mark.django_db
def test_cover_candidate_outside_the_radius_is_excluded_from_objects_and_totals():
    inside = alert_at(80.0, 20.0 + 9.9 * ARCSEC)
    outside = alert_at(80.0, 20.0 + 10.1 * ARCSEC)
    # The outside object is a cover candidate: it shares a coarse cover pixel.
    from core.healpix import cone_cover_ranges
    ranges = cone_cover_ranges(80.0, 20.0, 10.0)
    assert any(lo <= outside.healpix_ipix <= hi for lo, hi in ranges)

    body = lookup_objects(inputs=[position(80.0, 20.0, 10.0)])

    result = only_result(body)
    assert ids_of(result) == [oid(inside)]
    assert result['total'] == 1
    assert result['truncated'] is False


@pytest.mark.django_db
@override_settings(API_MAX_OBJECTS_PER_POSITION=1)
def test_cover_candidate_outside_the_radius_does_not_count_toward_the_cap():
    # With a cap of one, a counted outside candidate would truncate the result.
    inside = alert_at(80.0, 20.0 + 9.9 * ARCSEC)
    alert_at(80.0, 20.0 + 10.1 * ARCSEC)

    result = only_result(lookup_objects(inputs=[position(80.0, 20.0, 10.0)]))

    assert ids_of(result) == [oid(inside)]
    assert result['truncated'] is False
    assert result['total'] == 1


@pytest.mark.django_db
def test_object_without_healpix_index_is_not_reachable_by_position():
    AlertFactory(ra_deg=150.0, dec_deg=2.0)  # healpix_ipix left NULL

    body = lookup_objects(inputs=[position(150.0, 2.0, 5.0)])

    assert only_result(body)['status'] == InputStatus.NO_RUBIN_OBJECT


@pytest.mark.django_db
def test_position_search_reaches_the_whole_archive():
    # R12: an object ingested long ago is still found.
    old = alert_at(150.0, 2.0)
    set_ingest_time(old, timezone.now() - timedelta(days=3650))

    result = only_result(lookup_objects(inputs=[position(150.0, 2.0, 1.0)]))

    assert ids_of(result) == [oid(old)]


@pytest.mark.django_db
def test_single_cone_pages_to_completion_under_a_pinned_as_of():
    base = timezone.now() - timedelta(hours=1)
    alerts = []
    for k in range(5):
        alert = alert_at(210.0, -10.0 + k * ARCSEC)
        set_ingest_time(alert, base + timedelta(seconds=k))
        alerts.append(alert)

    first = cone_search(ra=210.0, dec=-10.0, radius_arcsec=10.0, page_size=2)
    seen = ids_of(only_result(first))
    assert only_result(first)['total'] == 5
    as_of = first['as_of']
    cursor = first['next_cursor']
    assert cursor is not None

    # An object ingested mid-walk is outside the pinned set.
    late = alert_at(210.0, -10.0 + 0.5 * ARCSEC)

    while cursor is not None:
        page = cone_search(cursor=cursor, page_size=2)
        assert page['as_of'] == as_of
        result = only_result(page)
        assert result['status'] == InputStatus.OBJECTS_FOUND
        seen.extend(ids_of(result))
        cursor = page['next_cursor']

    assert seen == [oid(a) for a in alerts]
    assert oid(late) not in seen
    # A fresh search sees it.
    fresh = cone_search(ra=210.0, dec=-10.0, radius_arcsec=10.0)
    assert oid(late) in ids_of(only_result(fresh))


@pytest.mark.django_db
def test_cone_cursor_conflicting_with_explicit_parameters_is_rejected():
    for k in range(3):
        alert_at(210.0, -10.0 + k * ARCSEC)
    first = cone_search(ra=210.0, dec=-10.0, radius_arcsec=10.0, page_size=1)

    # Repeating the pinned values is fine; changing one is not.
    cone_search(cursor=first['next_cursor'], ra=210.0, dec=-10.0,
                radius_arcsec=10.0, page_size=1)
    with pytest.raises(InvalidQuery) as exc_info:
        cone_search(cursor=first['next_cursor'], radius_arcsec=20.0)
    assert exc_info.value.param == 'radius_arcsec'
    with pytest.raises(InvalidQuery) as exc_info:
        cone_search(cursor=first['next_cursor'], detail='full')
    assert exc_info.value.param == 'detail'


@pytest.mark.django_db
@override_settings(API_MAX_OBJECTS_PER_POSITION=2)
def test_batched_position_over_its_cap_is_truncated_with_a_total():
    base = timezone.now() - timedelta(hours=1)
    alerts = []
    for k in range(4):
        alert = alert_at(30.0, 30.0 + k * ARCSEC)
        set_ingest_time(alert, base + timedelta(seconds=k))
        alerts.append(alert)

    result = only_result(lookup_objects(inputs=[position(30.0, 30.0, 10.0)]))

    assert result['status'] == InputStatus.OBJECTS_FOUND
    assert result['truncated'] is True
    assert result['truncation'] == 'per_input_limit'
    assert result['total'] == 4
    assert ids_of(result) == [oid(a) for a in alerts[:2]]


@pytest.mark.django_db
@override_settings(API_MAX_OBJECTS_PER_REQUEST=5, API_MAX_OBJECTS_PER_POSITION=3)
def test_batch_over_the_request_total_truncates_in_input_order():
    for k in range(3):
        alert_at(30.0, 30.0 + k * ARCSEC)
        alert_at(60.0, 30.0 + k * ARCSEC)
        alert_at(90.0, 30.0 + k * ARCSEC)
    known = alert_at(120.0, 30.0)

    body = lookup_objects(inputs=[
        position(30.0, 30.0, 10.0),
        {'kind': 'id', 'diaObjectId': oid(known)},
        position(60.0, 30.0, 10.0),
        position(90.0, 30.0, 10.0),
    ])

    first, by_id, second, third = body['results']
    assert body['truncated'] is True
    # The ID input takes one object of the budget of five; the positions share
    # the rest in input order.
    assert len(by_id['objects']) == 1
    assert len(first['objects']) == 3 and first['truncated'] is False
    assert len(second['objects']) == 1 and second['truncated'] is True
    assert second['truncation'] == 'per_request_limit' and second['total'] == 3
    assert third['objects'] == [] and third['truncated'] is True
    assert third['truncation'] == 'per_request_limit' and third['total'] == 3
    assert third['status'] == InputStatus.OBJECTS_FOUND


@pytest.mark.django_db
def test_batch_without_truncation_says_so():
    body = lookup_objects(inputs=[position(30.0, 30.0, 10.0)])
    assert body['truncated'] is False


@pytest.mark.django_db
def test_batched_positions_return_exactly_what_single_cones_return():
    centers = [(0.0, 0.0), (0.0, 0.002), (359.999, 45.0), (180.0, -89.99), (75.0, 12.0)]
    for k, (ra, dec) in enumerate(centers):
        for j in range(3):
            alert_at((ra + j * 2 * ARCSEC) % 360.0, dec + (j - 1) * 4 * ARCSEC)
    queries = [(ra, dec, 8.0) for ra, dec in centers] + [(0.0, 0.001, 20.0)]

    batch = lookup_objects(inputs=[position(*q) for q in queries])

    for query, result in zip(queries, batch['results']):
        single = only_result(cone_search(ra=query[0], dec=query[1], radius_arcsec=query[2]))
        assert ids_of(result) == ids_of(single), query
        assert result['total'] == single['total']
        assert result['status'] == single['status']


@pytest.mark.django_db
def test_shared_radius_applies_to_positions_without_their_own():
    near = alert_at(150.0, 2.0 + 3 * ARCSEC)

    body = lookup_objects(
        inputs=[position(150.0, 2.0), position(150.0, 2.0, 1.0)], radius_arcsec=5.0,
    )

    wide, narrow = body['results']
    assert wide['normalized']['radius_arcsec'] == 5.0
    assert ids_of(wide) == [oid(near)]
    assert narrow['status'] == InputStatus.NO_RUBIN_OBJECT


@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_single_cone_radius_above_the_maximum_is_rejected():
    with pytest.raises(InvalidQuery) as exc_info:
        cone_search(ra=10.0, dec=10.0, radius_arcsec=61.0)
    assert exc_info.value.param == 'radius_arcsec'


@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_shared_radius_above_the_maximum_is_rejected():
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=[position(10.0, 10.0)], radius_arcsec=61.0)
    assert exc_info.value.param == 'radius_arcsec'


@pytest.mark.parametrize('entry, param', [
    ({'kind': 'position', 'ra': 10.0, 'dec': 95.0, 'radius_arcsec': 2.0}, 'inputs[1].dec'),
    ({'kind': 'position', 'ra': 400.0, 'dec': 5.0, 'radius_arcsec': 2.0}, 'inputs[1].ra'),
    ({'kind': 'position', 'ra': '10', 'dec': 5.0, 'radius_arcsec': 2.0}, 'inputs[1].ra'),
    ({'kind': 'position', 'ra': True, 'dec': 5.0, 'radius_arcsec': 2.0}, 'inputs[1].ra'),
    ({'kind': 'position', 'dec': 5.0, 'radius_arcsec': 2.0}, 'inputs[1].ra'),
    ({'kind': 'position', 'ra': 10.0, 'dec': 5.0}, 'inputs[1].radius_arcsec'),
    ({'kind': 'position', 'ra': 10.0, 'dec': 5.0, 'radius_arcsec': 0}, 'inputs[1].radius_arcsec'),
    ({'kind': 'position', 'ra': 10.0, 'dec': 5.0, 'radius_arcsec': 61}, 'inputs[1].radius_arcsec'),
    ({'kind': 'position', 'ra': 10.0, 'dec': 5.0, 'radius_arcsec': 2, 'r': 1}, 'inputs[1].r'),
])
@pytest.mark.django_db
@override_settings(API_MAX_CONE_RADIUS_ARCSEC=60)
def test_malformed_position_fails_only_its_entry(entry, param):
    found = alert_at(150.0, 2.0)

    body = lookup_objects(inputs=[
        position(150.0, 2.0, 1.0), entry, {'kind': 'id', 'diaObjectId': oid(found)},
    ])

    good, bad, by_id = body['results']
    assert good['status'] == InputStatus.OBJECTS_FOUND
    assert bad['status'] == InputStatus.INVALID_INPUT
    assert bad['error']['param'] == param
    assert bad['input'] == entry
    assert by_id['status'] == ObjectStatus.CROSSMATCH_PENDING


@pytest.mark.django_db
@override_settings(API_MAX_POSITIONS=2)
def test_batch_over_the_position_maximum_is_rejected():
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=[position(1.0, 1.0, 1.0)] * 3)
    assert exc_info.value.param == 'inputs'


@pytest.mark.django_db
def test_duplicate_positions_are_answered_twice_in_order():
    found = alert_at(150.0, 2.0)
    inputs = [position(150.0, 2.0, 1.0), position(10.0, 10.0, 1.0), position(150.0, 2.0, 1.0)]

    body = lookup_objects(inputs=inputs)

    assert [r['index'] for r in body['results']] == [0, 1, 2]
    assert [r['input'] for r in body['results']] == inputs
    assert ids_of(body['results'][0]) == ids_of(body['results'][2]) == [oid(found)]
    assert body['results'][1]['status'] == InputStatus.NO_RUBIN_OBJECT
