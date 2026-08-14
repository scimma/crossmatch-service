"""U3 / AE1-AE6: the recent-crossmatch query/service layer.

Covers the window filter on both timestamp fields, matches-only inner join,
grouping by object, the four detail levels, current-match_version dedup, and
keyset (cursor) paging: per-page size clamping, following ``next_cursor`` to
exhaustion, tie handling across a page boundary, and decoded-cursor
re-validation. ``ingest_time`` is ``auto_now_add`` so it cannot be set at
construction; tests that pin it use an explicit ``.update()`` after the factory
builds the row (mirroring how production stamps it on ingest).
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from api.pagination import Cursor, encode_cursor
from api.service import InvalidQuery, recent_crossmatches
from core.models import TnsAssociation
from tests.factories import AlertFactory, CatalogMatchFactory, set_ingest_time


@pytest.mark.django_db
def test_matches_only_excludes_objects_without_matches():
    """AE2: an alert in the window but with no CatalogMatch is not returned."""
    now = timezone.now()
    matched = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=matched)
    AlertFactory(event_time=now)  # no match -> excluded

    result = recent_crossmatches(time_field='event_time', detail='ids')

    returned = {o['diaObjectId'] for o in result['objects']}
    assert returned == {matched.lsst_diaObject_diaObjectId}
    assert result['count'] == 1


@pytest.mark.django_db
def test_window_excludes_alerts_outside_span_event_time():
    """AE1: only alerts whose event_time falls in [start, end) are returned."""
    now = timezone.now()
    inside = AlertFactory(event_time=now - timedelta(hours=2))
    CatalogMatchFactory(alert=inside)
    outside = AlertFactory(event_time=now - timedelta(hours=30))
    CatalogMatchFactory(alert=outside)

    result = recent_crossmatches(
        start=now - timedelta(hours=12), end=now, time_field='event_time',
        detail='ids',
    )

    returned = {o['diaObjectId'] for o in result['objects']}
    assert returned == {inside.lsst_diaObject_diaObjectId}


@pytest.mark.django_db
def test_window_filters_on_ingest_time_by_default():
    """AE1: the default time_field is ingest_time, independent of event_time."""
    now = timezone.now()
    # event_time is old, but ingest_time is recent -> inside the default window.
    recent_ingest = AlertFactory(event_time=now - timedelta(days=10))
    CatalogMatchFactory(alert=recent_ingest)
    set_ingest_time(recent_ingest, now - timedelta(hours=1))

    # event_time is recent, but ingest_time is old -> outside the window.
    old_ingest = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=old_ingest)
    set_ingest_time(old_ingest, now - timedelta(days=5))

    result = recent_crossmatches(detail='ids')  # ingest_time default, 12h

    returned = {o['diaObjectId'] for o in result['objects']}
    assert returned == {recent_ingest.lsst_diaObject_diaObjectId}
    assert result['time_field'] == 'ingest_time'


@pytest.mark.django_db
def test_detail_ids_returns_only_object_ids():
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=alert)

    result = recent_crossmatches(time_field='event_time', detail='ids')

    obj = result['objects'][0]
    assert obj == {'diaObjectId': alert.lsst_diaObject_diaObjectId}
    assert 'ra' not in obj and 'matches' not in obj


@pytest.mark.django_db
def test_detail_position_adds_object_coordinates():
    """AE3: position level adds the alert object's ra/dec (not source coords)."""
    now = timezone.now()
    alert = AlertFactory(event_time=now, ra_deg=12.5, dec_deg=-45.0)
    CatalogMatchFactory(alert=alert, source_ra_deg=99.0, source_dec_deg=99.0)

    result = recent_crossmatches(time_field='event_time', detail='position')

    obj = result['objects'][0]
    assert obj['ra'] == 12.5
    assert obj['dec'] == -45.0
    assert 'matches' not in obj


@pytest.mark.django_db
def test_detail_matches_lists_catalog_source_and_separation():
    """AE4: matches level lists catalog_name, catalog_source_id, separation."""
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='42',
        match_distance_arcsec=0.75,
    )

    result = recent_crossmatches(time_field='event_time', detail='matches')

    matches = result['objects'][0]['matches']
    assert len(matches) == 1
    assert matches[0] == {
        'catalog_name': 'gaia_dr3',
        'catalog_source_id': '42',
        'separation_arcsec': 0.75,
    }


@pytest.mark.django_db
def test_detail_full_reconstructs_published_payload():
    """AE5: full level reconstructs the published payload via the shared builder;
    ra/dec are the catalog source coords, not the object position."""
    now = timezone.now()
    alert = AlertFactory(event_time=now, ra_deg=1.0, dec_deg=2.0)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='42',
        match_distance_arcsec=0.75, source_ra_deg=1.11, source_dec_deg=2.22,
        catalog_payload={'phot_g_mean_mag': 18.3},
    )

    result = recent_crossmatches(time_field='event_time', detail='full')

    match = result['objects'][0]['matches'][0]
    assert match == {
        'diaObjectId': alert.lsst_diaObject_diaObjectId,
        'ra': 1.11,
        'dec': 2.22,
        'catalog_name': 'gaia_dr3',
        'catalog_source_id': '42',
        'separation_arcsec': 0.75,
        'catalog_payload': {'phot_g_mean_mag': 18.3},
        # The shared builder always emits the coverage keys. A stored match has
        # no batch context, so the read-model reports full coverage (partial
        # False); the real per-batch skip set rides the published Hopskotch payload.
        'catalogs_skipped': [],
        'partial': False,
        # No TNS association persisted for this alert -> indicator present,
        # no tns block (plan U8).
        'tns_checked': False,
        'tns_snapshot_epoch': None,
    }


@pytest.mark.django_db
def test_multiple_catalogs_group_under_one_object():
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=alert, catalog_name='gaia_dr3', catalog_source_id='1')
    CatalogMatchFactory(alert=alert, catalog_name='des_y6_gold', catalog_source_id='2')

    result = recent_crossmatches(time_field='event_time', detail='matches')

    assert result['count'] == 1
    catalogs = {m['catalog_name'] for m in result['objects'][0]['matches']}
    assert catalogs == {'gaia_dr3', 'des_y6_gold'}


@pytest.mark.django_db
def test_only_current_match_version_is_returned():
    """A re-matched (object, catalog, source) surfaces once, at the highest
    match_version, not once per version."""
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='7',
        match_distance_arcsec=0.9, match_version=1,
    )
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='7',
        match_distance_arcsec=0.4, match_version=2,
    )

    result = recent_crossmatches(time_field='event_time', detail='matches')

    matches = result['objects'][0]['matches']
    assert len(matches) == 1
    assert matches[0]['separation_arcsec'] == 0.4  # the current version


@pytest.mark.django_db
def test_empty_window_returns_no_objects():
    result = recent_crossmatches(time_field='event_time', detail='matches')
    assert result['count'] == 0
    assert result['objects'] == []


def _seed_matched_objects(count, base_time, spacing=timedelta(minutes=1)):
    """Create ``count`` matched alerts at strictly-decreasing event_times.

    Returns the diaObjectIds in newest-first order (the order a walk should
    reconstruct).
    """
    ids_newest_first = []
    for i in range(count):
        alert = AlertFactory(event_time=base_time - i * spacing)
        CatalogMatchFactory(alert=alert)
        ids_newest_first.append(alert.lsst_diaObject_diaObjectId)
    return ids_newest_first


def _walk(page_size, **kwargs):
    """Follow next_cursor to exhaustion; return the concatenated diaObjectIds."""
    collected = []
    cursor = None
    pages = 0
    while True:
        result = recent_crossmatches(page_size=page_size, cursor=cursor, **kwargs)
        collected.extend(o['diaObjectId'] for o in result['objects'])
        cursor = result['next_cursor']
        pages += 1
        if cursor is None:
            break
        assert pages < 1000, 'walk did not terminate'
    return collected


@pytest.mark.django_db
def test_first_page_echoes_page_size_and_emits_cursor():
    """AE1: no cursor, more than a page of objects -> a full page plus a cursor."""
    now = timezone.now()
    _seed_matched_objects(5, now)

    result = recent_crossmatches(time_field='event_time', detail='ids', page_size=2)

    assert len(result['objects']) == 2
    assert result['page_size'] == 2
    assert result['count'] == 2
    assert result['next_cursor'] is not None


@pytest.mark.django_db
def test_default_page_size_used_when_omitted():
    now = timezone.now()
    _seed_matched_objects(3, now)

    result = recent_crossmatches(time_field='event_time', detail='ids')

    assert result['page_size'] == 1000  # RECENT_CROSSMATCH_DEFAULT_PAGE_SIZE
    # Default (1000) exceeds the 3 seeded objects, so all three come back at once.
    assert result['count'] == 3
    assert result['next_cursor'] is None


@pytest.mark.django_db
def test_follow_cursor_to_exhaustion_covers_set_once_in_order():
    """AE2: the union of pages equals the full distinct set, each id once, in
    newest-first order; the final page has next_cursor None."""
    now = timezone.now()
    expected = _seed_matched_objects(7, now)

    walked = _walk(page_size=2, time_field='event_time', detail='ids')

    assert walked == expected  # order preserved, no dupes, no skips


@pytest.mark.django_db
def test_tie_on_time_field_split_across_page_boundary():
    """AE2 tie case: objects sharing one event_time split across a page boundary
    are neither dropped nor duplicated (exercises the diaObjectId tiebreaker)."""
    now = timezone.now()
    shared = now - timedelta(minutes=5)
    # Four objects at the SAME event_time, plus one newer and one older.
    newer = AlertFactory(event_time=now)
    CatalogMatchFactory(alert=newer)
    tied = []
    for _ in range(4):
        a = AlertFactory(event_time=shared)
        CatalogMatchFactory(alert=a)
        tied.append(a.lsst_diaObject_diaObjectId)
    older = AlertFactory(event_time=now - timedelta(minutes=10))
    CatalogMatchFactory(alert=older)

    walked = _walk(page_size=2, time_field='event_time', detail='ids')

    # The four tied ids sort by diaObjectId ASC among themselves.
    expected = (
        [newer.lsst_diaObject_diaObjectId]
        + sorted(tied)
        + [older.lsst_diaObject_diaObjectId]
    )
    assert walked == expected
    assert len(walked) == len(set(walked)) == 6


@override_settings(RECENT_CROSSMATCH_MAX_PAGE_SIZE=2)
@pytest.mark.django_db
def test_page_size_above_max_clamps_not_rejects():
    """AE3: a page_size above the operator max is served at the max, not 400."""
    now = timezone.now()
    _seed_matched_objects(3, now)

    result = recent_crossmatches(time_field='event_time', detail='ids', page_size=100)

    assert result['page_size'] == 2
    assert result['count'] == 2
    assert result['next_cursor'] is not None


@pytest.mark.django_db
def test_invalid_detail_raises():
    with pytest.raises(InvalidQuery):
        recent_crossmatches(detail='everything')


@pytest.mark.django_db
def test_invalid_time_field_raises():
    with pytest.raises(InvalidQuery):
        recent_crossmatches(time_field='created_at')


@pytest.mark.django_db
def test_non_positive_page_size_raises():
    with pytest.raises(InvalidQuery):
        recent_crossmatches(page_size=0)
    with pytest.raises(InvalidQuery):
        recent_crossmatches(page_size=-5)


@pytest.mark.django_db
def test_end_before_start_raises():
    now = timezone.now()
    with pytest.raises(InvalidQuery):
        recent_crossmatches(start=now, end=now - timedelta(hours=1))


@pytest.mark.django_db
def test_window_span_over_max_raises():
    now = timezone.now()
    with pytest.raises(InvalidQuery):
        recent_crossmatches(start=now - timedelta(days=365), end=now)


@pytest.mark.django_db
def test_full_detail_skips_row_with_null_source_coords():
    """A stored match with null source coords must not 500 the whole full
    response: the unbuildable row is skipped, the valid one survives."""
    now = timezone.now()
    alert = AlertFactory(event_time=now)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='good',
        source_ra_deg=1.0, source_dec_deg=2.0,
    )
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='bad',
        source_ra_deg=None, source_dec_deg=None,
    )

    result = recent_crossmatches(time_field='event_time', detail='full')

    matches = result['objects'][0]['matches']
    assert [m['catalog_source_id'] for m in matches] == ['good']


@pytest.mark.django_db
def test_empty_window_has_null_cursor():
    """AE4: an empty window -> no objects, next_cursor null, count 0."""
    result = recent_crossmatches(time_field='event_time', detail='matches', page_size=5)
    assert result['count'] == 0
    assert result['objects'] == []
    assert result['next_cursor'] is None


@pytest.mark.django_db
def test_cursor_resumes_same_query_with_derived_context():
    """A cursor built for a query resumes that query when start/end/time_field/
    detail are omitted (the service derives them from the cursor)."""
    now = timezone.now()
    expected = _seed_matched_objects(4, now)

    first = recent_crossmatches(time_field='event_time', detail='ids', page_size=2)
    # Second page passes ONLY the cursor + page_size (no window/time_field/detail).
    second = recent_crossmatches(page_size=2, cursor=first['next_cursor'])

    assert second['time_field'] == 'event_time'
    assert second['detail'] == 'ids'
    got = [o['diaObjectId'] for o in first['objects']] + [
        o['diaObjectId'] for o in second['objects']
    ]
    assert got == expected


@pytest.mark.django_db
def test_paging_preserves_nested_matches_across_detail_levels():
    """R13: page size counts objects and each object still carries its nested
    matches, for every detail level."""
    now = timezone.now()
    for i in range(3):
        alert = AlertFactory(event_time=now - timedelta(minutes=i))
        CatalogMatchFactory(alert=alert, catalog_name='gaia_dr3', catalog_source_id=str(i))

    for detail in ('position', 'matches', 'full'):
        walked_objs = []
        cursor = None
        while True:
            result = recent_crossmatches(
                time_field='event_time', detail=detail, page_size=1, cursor=cursor
            )
            assert result['count'] == len(result['objects']) <= 1
            walked_objs.extend(result['objects'])
            cursor = result['next_cursor']
            if cursor is None:
                break
        assert len(walked_objs) == 3
        if detail in ('matches', 'full'):
            assert all('matches' in o for o in walked_objs)


@pytest.mark.django_db
def test_decoded_cursor_time_field_revalidated_against_allowlist():
    """Security (KTD3): an unsigned cursor carrying an out-of-allowlist time_field
    is rejected before the keyset predicate interpolates it into the ORM."""
    now = timezone.now()
    bad = encode_cursor(
        Cursor(
            time_field_value=now,
            dia_object_id=1,
            start=now - timedelta(hours=1),
            end=now,
            time_field='created_at; DROP',  # not in TIME_FIELDS
            detail='ids',
        )
    )
    with pytest.raises(InvalidQuery):
        recent_crossmatches(cursor=bad)


@pytest.mark.django_db
def test_decoded_cursor_detail_revalidated_against_allowlist():
    """Security (KTD3): a cursor carrying an out-of-allowlist detail is rejected,
    symmetric with the time_field re-validation."""
    now = timezone.now()
    bad = encode_cursor(
        Cursor(
            time_field_value=now,
            dia_object_id=1,
            start=now - timedelta(hours=1),
            end=now,
            time_field='event_time',
            detail='everything',  # not in DETAIL_LEVELS
        )
    )
    with pytest.raises(InvalidQuery):
        recent_crossmatches(cursor=bad)


@pytest.mark.django_db
def test_empty_cursor_string_rejected_not_served_as_page_one():
    """An explicit empty cursor is malformed -> InvalidQuery, not a silent page 1."""
    with pytest.raises(InvalidQuery):
        recent_crossmatches(cursor='')


@pytest.mark.django_db
def test_naive_timestamp_cursor_does_not_500():
    """A crafted cursor with naive timestamps is handled (coerced to aware), not a
    TypeError: the window comparison must not mix naive and aware datetimes."""
    import base64
    import json
    payload = {'t': '2026-07-14T01:05:53', 'i': 1, 's': '2026-07-14T01:00:00',
               'e': '2026-07-14T02:00:00', 'f': 'event_time', 'd': 'ids'}
    raw = json.dumps(payload).encode('utf-8')
    token = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')

    result = recent_crossmatches(cursor=token)  # must not raise TypeError

    assert result['objects'] == []
    assert result['next_cursor'] is None


@override_settings(RECENT_CROSSMATCH_MAX_WINDOW_HOURS=1)
@pytest.mark.django_db
def test_decoded_cursor_window_span_revalidated():
    """Security (KTD3): a cursor pinning an over-span window is rejected."""
    now = timezone.now()
    bad = encode_cursor(
        Cursor(
            time_field_value=now,
            dia_object_id=1,
            start=now - timedelta(days=30),
            end=now,
            time_field='event_time',
            detail='ids',
        )
    )
    with pytest.raises(InvalidQuery):
        recent_crossmatches(cursor=bad)


@pytest.mark.django_db
def test_cursor_conflict_with_explicit_param_rejected():
    """AE5: presenting a cursor with a conflicting explicit param -> InvalidQuery."""
    now = timezone.now()
    _seed_matched_objects(3, now)
    first = recent_crossmatches(time_field='event_time', detail='ids', page_size=1)

    with pytest.raises(InvalidQuery):
        recent_crossmatches(
            cursor=first['next_cursor'], time_field='ingest_time'  # conflicts
        )


@pytest.mark.django_db
def test_open_event_time_window_walk_is_read_committed():
    """R11 carve-out: on an open event_time window, a mid-walk insert whose
    event_time falls BELOW the current cursor is read-committed (may appear); the
    walk stays duplicate-free either way. This characterizes, not forbids, the
    late arrival."""
    now = timezone.now()
    first_batch = _seed_matched_objects(3, now)  # newest-first

    # Page 1 (newest). Then insert an older alert BELOW where the cursor now sits.
    p1 = recent_crossmatches(time_field='event_time', detail='ids', page_size=2)
    seen = [o['diaObjectId'] for o in p1['objects']]

    late = AlertFactory(event_time=now - timedelta(hours=1))  # below the cursor
    CatalogMatchFactory(alert=late)

    # Continue the walk.
    cursor = p1['next_cursor']
    while cursor is not None:
        page = recent_crossmatches(page_size=2, cursor=cursor)
        seen.extend(o['diaObjectId'] for o in page['objects'])
        cursor = page['next_cursor']

    # Duplicate-free is the hard guarantee; the original set is fully covered.
    assert len(seen) == len(set(seen))
    assert set(first_batch).issubset(set(seen))
    # The late arrival being included is the read-committed behavior (not asserted
    # as required); it must never cause a duplicate, which the check above covers.


# --- TNS enrichment on the full level (plan U8, AE1 API side) ----------------

@pytest.mark.django_db
def test_detail_full_includes_tns_block_from_association():
    """AE1 (API side): a persisted TNS association surfaces as a tns block on full."""
    now = timezone.now()
    alert = AlertFactory(event_time=now, ra_deg=1.0, dec_deg=2.0)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='42',
        match_distance_arcsec=0.75, source_ra_deg=1.11, source_dec_deg=2.22,
        catalog_payload={'phot_g_mean_mag': 18.3},
    )
    TnsAssociation.objects.create(
        alert=alert, checked=True, snapshot_epoch=now,
        objid=4242, name='2024xyz', name_prefix='SN', type='SN Ia',
        redshift=0.05, separation_arcsec=0.3,
    )

    result = recent_crossmatches(time_field='event_time', detail='full')
    match = result['objects'][0]['matches'][0]

    assert match['tns_checked'] is True
    assert match['tns_snapshot_epoch'] == now.isoformat()
    assert match['tns'] == {
        'objid': 4242,
        'name': '2024xyz',
        'classification': 'SN Ia',
        'redshift': 0.05,
        'separation_arcsec': 0.3,
        'url': 'https://www.wis-tns.org/object/2024xyz',
    }


@pytest.mark.django_db
def test_detail_full_checked_no_match_has_indicator_no_block():
    """A checked-but-unmatched alert reports checked=True with no tns block."""
    now = timezone.now()
    alert = AlertFactory(event_time=now, ra_deg=1.0, dec_deg=2.0)
    CatalogMatchFactory(
        alert=alert, catalog_name='gaia_dr3', catalog_source_id='42',
        match_distance_arcsec=0.75, source_ra_deg=1.11, source_dec_deg=2.22,
        catalog_payload={},
    )
    TnsAssociation.objects.create(alert=alert, checked=True, snapshot_epoch=now)

    result = recent_crossmatches(time_field='event_time', detail='full')
    match = result['objects'][0]['matches'][0]

    assert match['tns_checked'] is True
    assert 'tns' not in match
