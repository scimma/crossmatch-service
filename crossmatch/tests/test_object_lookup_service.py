"""U4: object lookup by ID and the batch lookup framework (service layer).

Covers R1, R2, R7, R8, R10, R11, R12, R18, R19, R28 and AE1, AE4 against
``api.lookup``. The HTTP adapters and response conformance are covered by
``test_object_lookup_view``.
"""

from datetime import timedelta

import pytest
from django.db import ProgrammingError
from django.test import override_settings
from django.utils import timezone

from api.contract import InputStatus, ObjectStatus, ReadTimeCatalogOutcome
from api.errors import InvalidQuery
from api.lookup import BEST_GUESS_PROVENANCE_SET, get_object, lookup_objects
from core import provenance
from core.models import (
    Alert,
    CatalogSearchOutcome,
    ObjectCrossmatchRecord,
    TnsAssociation,
)
from tests.factories import (
    AlertDeliveryFactory,
    AlertFactory,
    CatalogMatchFactory,
    ObjectCrossmatchRecordFactory,
    ProvenanceSetFactory,
)

MATCHED = Alert.Status.MATCHED
UNKNOWN_ID = 4_242_424_242


def _id(object_id):
    return {'kind': 'id', 'diaObjectId': object_id}


def _catalog_names():
    return [cat['name'] for cat in provenance.catalog_releases()]


def _only_result(body):
    assert body['count'] == 1
    assert len(body['results']) == 1
    return body['results'][0]


def _only_object(result):
    assert len(result['objects']) == 1
    return result['objects'][0]


@pytest.mark.django_db
def test_get_object_matched_returns_status_outcomes_provenance_and_matches():
    record = ObjectCrossmatchRecordFactory()
    alert = record.alert
    match = CatalogMatchFactory(alert=alert, catalog_name='gaia_dr3')

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId)

    assert body['provenance'] == provenance.service_provenance()
    assert body['detail'] == 'matches'
    result = _only_result(body)
    assert result['kind'] == 'id'
    assert result['status'] == ObjectStatus.COINCIDENT_SOURCES
    obj = _only_object(result)
    assert obj['diaObjectId'] == alert.lsst_diaObject_diaObjectId
    assert obj['diaObjectId_str'] == str(alert.lsst_diaObject_diaObjectId)
    assert obj['status'] == ObjectStatus.COINCIDENT_SOURCES
    crossmatch = obj['crossmatch']
    assert crossmatch['provenance'] == 'recorded'
    assert crossmatch['catalog_outcomes'] == {
        name: CatalogSearchOutcome.SEARCHED for name in _catalog_names()
    }
    key = crossmatch['provenance_set']
    assert key == record.provenance_set.content_hash
    assert key in body['provenance_sets']
    assert obj['matches'] == [{
        'catalog_name': 'gaia_dr3',
        'catalog_source_id': match.catalog_source_id,
        'separation_arcsec': 0.5,
        'provenance_set': key,
    }]


@pytest.mark.django_db
def test_get_object_unknown_id_is_not_in_service():
    body = get_object(dia_object_id=UNKNOWN_ID)

    result = _only_result(body)
    assert result['status'] == ObjectStatus.NOT_IN_SERVICE
    assert _only_object(result) == {
        'diaObjectId': UNKNOWN_ID,
        'diaObjectId_str': str(UNKNOWN_ID),
        'status': ObjectStatus.NOT_IN_SERVICE,
    }
    assert body['provenance_sets'] == {}


@pytest.mark.django_db
def test_get_object_malformed_id_is_a_request_error_naming_the_param():
    with pytest.raises(InvalidQuery) as exc_info:
        get_object(dia_object_id='12x')
    assert exc_info.value.param == 'diaObjectId'


@pytest.mark.django_db
def test_ae1_four_ids_return_four_statuses_in_input_order():
    pending = AlertFactory(status=Alert.Status.INGESTED)
    no_match = ObjectCrossmatchRecordFactory().alert
    gaia = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(
        alert=gaia, catalog_name='gaia_dr3', catalog_payload={'parallax': 3.2},
    )

    body = lookup_objects(
        inputs=[
            _id(UNKNOWN_ID),
            _id(pending.lsst_diaObject_diaObjectId),
            _id(no_match.lsst_diaObject_diaObjectId),
            _id(gaia.lsst_diaObject_diaObjectId),
        ],
        detail='full',
    )

    assert body['count'] == 4
    statuses = [r['status'] for r in body['results']]
    assert statuses == [
        ObjectStatus.NOT_IN_SERVICE,
        ObjectStatus.CROSSMATCH_PENDING,
        ObjectStatus.NO_COINCIDENT_SOURCE,
        ObjectStatus.COINCIDENT_SOURCES,
    ]
    assert [r['index'] for r in body['results']] == [0, 1, 2, 3]
    gaia_obj = _only_object(body['results'][3])
    assert gaia_obj['matches'][0]['catalog_name'] == 'gaia_dr3'
    assert gaia_obj['matches'][0]['catalog_payload'] == {'parallax': 3.2}


@pytest.mark.django_db
def test_ae4_unrecorded_object_reports_not_recorded_with_labeled_best_guess():
    july = AlertFactory(status=MATCHED)
    CatalogMatchFactory(alert=july, catalog_name='gaia_dr3')

    body = get_object(dia_object_id=july.lsst_diaObject_diaObjectId)

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.COINCIDENT_SOURCES
    crossmatch = obj['crossmatch']
    assert crossmatch['provenance'] == 'not_recorded'
    assert crossmatch['provenance_set'] is None
    assert crossmatch['recording_release'] == provenance.PROVENANCE_RECORDING_RELEASE
    assert crossmatch['catalog_outcomes'] == {
        name: ReadTimeCatalogOutcome.NOT_RECORDED for name in _catalog_names()
    }
    assert crossmatch['best_guess_provenance_set'] == BEST_GUESS_PROVENANCE_SET
    best_guess = body['provenance_sets'][BEST_GUESS_PROVENANCE_SET]
    assert best_guess['basis'] == 'best_guess_current_settings'
    assert 'best guess' in best_guess['description']
    current = provenance.service_provenance()
    assert best_guess['crossmatch_radius_arcsec'] == current['crossmatch_radius_arcsec']
    assert best_guess['catalogs'] == current['catalogs']
    assert best_guess['reliability_cuts'] == current['reliability_cuts']
    # The match carries no recorded provenance set.
    assert obj['matches'][0]['provenance_set'] is None


@pytest.mark.django_db
def test_ae4_recorded_object_reports_recorded_radius_release_and_cuts():
    recorded_cuts = [{
        'broker': 'pittgoogle', 'min_reliability': 0.3,
        'enforced_by': 'service', 'status': 'service_setting', 'as_of': None,
    }]
    pset = ProvenanceSetFactory(
        crossmatch_radius_arcsec=2.5,
        catalogs=[{'name': 'gaia_dr3', 'release': 'DR3-recorded'}],
        reliability_cuts=recorded_cuts,
    )
    record = ObjectCrossmatchRecordFactory(
        provenance_set=pset, catalog_outcomes={'gaia_dr3': 'searched'},
    )
    CatalogMatchFactory(alert=record.alert, catalog_name='gaia_dr3')

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)

    obj = _only_object(_only_result(body))
    key = obj['crossmatch']['provenance_set']
    assert 'recording_release' not in obj['crossmatch']
    assert body['provenance_sets'] == {key: {
        'basis': 'recorded',
        'crossmatch_radius_arcsec': 2.5,
        'catalogs': [{'name': 'gaia_dr3', 'release': 'DR3-recorded'}],
        'reliability_cuts': recorded_cuts,
    }}
    assert obj['matches'][0]['provenance_set'] == key
    assert obj['crossmatch']['match_version'] == 1
    assert obj['crossmatch']['crossmatched_at'] == record.crossmatched_at.isoformat()


@pytest.mark.django_db
def test_every_catalog_skipped_or_outside_footprint_is_not_searched():
    names = _catalog_names()
    outcomes = {name: CatalogSearchOutcome.OUTSIDE_FOOTPRINT.value for name in names}
    outcomes[names[0]] = CatalogSearchOutcome.SKIPPED_READ_FAILURE.value
    record = ObjectCrossmatchRecordFactory(catalog_outcomes=outcomes)

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.NOT_SEARCHED
    assert obj['crossmatch']['catalog_outcomes'] == outcomes


@pytest.mark.django_db
def test_one_searched_catalog_with_no_match_is_no_coincident_source():
    names = _catalog_names()
    outcomes = {name: CatalogSearchOutcome.OUTSIDE_FOOTPRINT.value for name in names}
    outcomes[names[-1]] = CatalogSearchOutcome.SEARCHED.value
    record = ObjectCrossmatchRecordFactory(catalog_outcomes=outcomes)

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)

    assert _only_result(body)['status'] == ObjectStatus.NO_COINCIDENT_SOURCE


@pytest.mark.django_db
def test_catalog_added_after_record_is_not_in_service_at_crossmatch():
    names = _catalog_names()
    older = {name: CatalogSearchOutcome.SEARCHED.value for name in names[:-1]}
    older['retired_catalog'] = CatalogSearchOutcome.SEARCHED.value
    record = ObjectCrossmatchRecordFactory(catalog_outcomes=older)

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)

    outcomes = _only_object(_only_result(body))['crossmatch']['catalog_outcomes']
    assert outcomes[names[-1]] == ReadTimeCatalogOutcome.NOT_IN_SERVICE_AT_CROSSMATCH
    # A catalog recorded then but no longer in service keeps its recorded outcome.
    assert outcomes['retired_catalog'] == CatalogSearchOutcome.SEARCHED
    assert list(outcomes)[: len(names)] == names


@pytest.mark.django_db
def test_mixed_batch_returns_one_entry_per_input_in_order_with_duplicates():
    pending = AlertFactory(status=Alert.Status.QUEUED)
    unmatched = ObjectCrossmatchRecordFactory().alert
    matched = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=matched)
    matched_id = matched.lsst_diaObject_diaObjectId

    inputs = [
        _id(matched_id),
        {'kind': 'id', 'diaObjectId': 'not-a-number'},
        _id(UNKNOWN_ID),
        _id(str(matched_id)),
        _id(pending.lsst_diaObject_diaObjectId),
        _id(unmatched.lsst_diaObject_diaObjectId),
        _id(matched_id),
    ]
    body = lookup_objects(inputs=inputs)

    results = body['results']
    assert body['count'] == len(inputs) == len(results)
    assert [r['index'] for r in results] == list(range(len(inputs)))
    assert [r['input'] for r in results] == inputs
    assert [r['status'] for r in results] == [
        ObjectStatus.COINCIDENT_SOURCES,
        InputStatus.INVALID_INPUT,
        ObjectStatus.NOT_IN_SERVICE,
        ObjectStatus.COINCIDENT_SOURCES,
        ObjectStatus.CROSSMATCH_PENDING,
        ObjectStatus.NO_COINCIDENT_SOURCE,
        ObjectStatus.COINCIDENT_SOURCES,
    ]
    # The string form echoes as sent and normalizes to the same object.
    assert results[3]['input'] == {'kind': 'id', 'diaObjectId': str(matched_id)}
    assert results[3]['normalized'] == results[0]['normalized'] == {
        'kind': 'id', 'diaObjectId': matched_id, 'diaObjectId_str': str(matched_id),
    }
    assert results[0]['objects'] == results[6]['objects'] == results[3]['objects']
    invalid = results[1]
    assert invalid['normalized'] is None
    assert invalid['objects'] == []
    assert invalid['error']['param'] == 'inputs[1].diaObjectId'


@pytest.mark.django_db
def test_id_above_2_pow_53_round_trips_as_string():
    big = 2**53 + 1
    alert = AlertFactory(lsst_diaObject_diaObjectId=big, status=MATCHED)

    body = lookup_objects(inputs=[_id(str(big))])

    result = _only_result(body)
    assert result['input'] == {'kind': 'id', 'diaObjectId': str(big)}
    assert result['normalized']['diaObjectId_str'] == str(big)
    obj = _only_object(result)
    assert obj['diaObjectId'] == big == alert.lsst_diaObject_diaObjectId
    assert obj['diaObjectId_str'] == str(big)


@pytest.mark.django_db
@override_settings(API_MAX_IDS=2)
def test_batch_over_the_id_maximum_is_rejected_naming_inputs():
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=[_id(1), _id(2), _id(3)])
    assert exc_info.value.param == 'inputs'
    assert '2' in exc_info.value.message


@pytest.mark.django_db
@override_settings(API_MAX_IDS=2)
def test_batch_at_the_id_maximum_is_served():
    body = lookup_objects(inputs=[_id(1), _id(2)])
    assert body['count'] == 2


@pytest.mark.django_db
@override_settings(API_MAX_IDS=2)
def test_malformed_entries_count_toward_the_request_size_bound():
    inputs = [_id(1), _id(2)] + ['junk'] * 5
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=inputs)
    assert exc_info.value.param == 'inputs'


@pytest.mark.django_db
def test_pending_object_with_leftover_match_rows_shows_no_matches():
    # A reverted batch can leave CatalogMatch rows behind on an object that is
    # back to QUEUED; they must not surface (KTD9.5).
    alert = AlertFactory(status=Alert.Status.QUEUED)
    CatalogMatchFactory(alert=alert)

    for detail in ('ids', 'matches', 'full'):
        body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail=detail)
        obj = _only_object(_only_result(body))
        assert obj['status'] == ObjectStatus.CROSSMATCH_PENDING
        assert obj['crossmatch'] is None
        assert obj.get('matches', []) == []


@pytest.mark.django_db
def test_live_brokers_are_reported_apart_from_brokers_at_crossmatch():
    record = ObjectCrossmatchRecordFactory(brokers=['antares'])
    alert = record.alert
    AlertDeliveryFactory(alert=alert, broker='lasair')
    AlertDeliveryFactory(alert=alert, broker='antares')

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail='position')

    obj = _only_object(_only_result(body))
    assert obj['brokers'] == ['antares', 'lasair']
    assert obj['crossmatch']['brokers_at_crossmatch'] == ['antares']


@pytest.mark.django_db
def test_detail_position_reports_r10_object_fields_without_matches():
    event_time = timezone.now() - timedelta(days=3)
    alert = AlertFactory(
        status=MATCHED, ra_deg=10.5, dec_deg=-45.25, reliability=0.91,
        event_time=event_time,
    )
    alert.refresh_from_db()
    CatalogMatchFactory(alert=alert)

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail='position')

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.COINCIDENT_SOURCES
    assert obj['ra'] == 10.5
    assert obj['dec'] == -45.25
    assert obj['reliability'] == 0.91
    assert obj['ingest_time'] == alert.ingest_time.isoformat()
    assert obj['event_time'] == event_time.isoformat()
    assert obj['brokers'] == []
    assert 'matches' not in obj


@pytest.mark.django_db
def test_detail_ids_reports_status_and_outcomes_only():
    alert = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=alert)

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail='ids')

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.COINCIDENT_SOURCES
    assert set(obj) == {'diaObjectId', 'diaObjectId_str', 'status', 'crossmatch'}


@pytest.mark.django_db
def test_detail_full_returns_the_published_payload_with_provenance_set():
    record = ObjectCrossmatchRecordFactory()
    CatalogMatchFactory(
        alert=record.alert, catalog_name='des_y6_gold', catalog_payload={'dnf_z': 0.4},
    )

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId, detail='full')

    match = _only_object(_only_result(body))['matches'][0]
    assert match['catalog_payload'] == {'dnf_z': 0.4}
    assert match['diaObjectId'] == record.alert.lsst_diaObject_diaObjectId
    assert match['provenance_set'] == record.provenance_set.content_hash


@pytest.mark.django_db
def test_shared_provenance_set_appears_once():
    pset = ProvenanceSetFactory()
    a = ObjectCrossmatchRecordFactory(provenance_set=pset).alert
    b = ObjectCrossmatchRecordFactory(provenance_set=pset).alert

    body = lookup_objects(
        inputs=[_id(a.lsst_diaObject_diaObjectId), _id(b.lsst_diaObject_diaObjectId)],
    )

    assert list(body['provenance_sets']) == [pset.content_hash]


@pytest.mark.parametrize('bad_input, param', [
    ('123', 'inputs[0]'),
    (123, 'inputs[0]'),
    (None, 'inputs[0]'),
    ({'diaObjectId': 1}, 'inputs[0].kind'),
    ({'kind': 5, 'diaObjectId': 1}, 'inputs[0].kind'),
    ({'kind': 'bogus', 'diaObjectId': 1}, 'inputs[0].kind'),
    ({'kind': 'id'}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': 1.0}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': True}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': -1}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': 2**63}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': ' 12'}, 'inputs[0].diaObjectId'),
    ({'kind': 'id', 'diaObjectId': 1, 'extra': 2}, 'inputs[0].extra'),
])
@pytest.mark.django_db
def test_malformed_entry_is_invalid_input_not_a_request_error(bad_input, param):
    good = AlertFactory(status=MATCHED)

    body = lookup_objects(inputs=[bad_input, _id(good.lsst_diaObject_diaObjectId)])

    bad, ok = body['results']
    assert bad['status'] == InputStatus.INVALID_INPUT
    assert bad['input'] == bad_input
    assert bad['normalized'] is None
    assert bad['objects'] == []
    assert bad['error']['param'] == param
    assert bad['error']['message']
    assert ok['status'] == ObjectStatus.NO_COINCIDENT_SOURCE


@pytest.mark.django_db
def test_invalid_input_echoes_the_kind_it_was_sent_with():
    body = lookup_objects(inputs=[{'kind': 'bogus'}, {'kind': 7}, 'x'])
    assert [r['kind'] for r in body['results']] == ['bogus', None, None]


@pytest.mark.parametrize('inputs', [None, {}, 'abc', 5])
@pytest.mark.django_db
def test_inputs_not_a_list_is_a_request_error(inputs):
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=inputs)
    assert exc_info.value.param == 'inputs'


@pytest.mark.django_db
def test_empty_inputs_is_a_request_error():
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=[])
    assert exc_info.value.param == 'inputs'


@pytest.mark.parametrize('detail', ['bogus', '', 5])
@pytest.mark.django_db
def test_bad_detail_is_a_request_error_naming_detail(detail):
    with pytest.raises(InvalidQuery) as exc_info:
        lookup_objects(inputs=[_id(1)], detail=detail)
    assert exc_info.value.param == 'detail'


@pytest.mark.django_db
def test_lookup_reaches_objects_whose_payload_was_reclaimed():
    # R12: retention nulls Alert.payload; lookups still find the object.
    alert = ObjectCrossmatchRecordFactory(alert__payload=None).alert
    CatalogMatchFactory(alert=alert)

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId)

    assert _only_result(body)['status'] == ObjectStatus.COINCIDENT_SOURCES


@pytest.mark.django_db
def test_ids_resolve_in_a_fixed_number_of_queries(django_assert_num_queries):
    # Set-based resolution: the query count does not grow with the batch
    # (alerts, records, match existence, matches, TNS associations for the
    # matches, the per-object TNS associations, deliveries),
    # plus the SAVEPOINT and RELEASE around the record read.
    def batch(n):
        alerts = []
        for _ in range(n):
            alert = ObjectCrossmatchRecordFactory().alert
            CatalogMatchFactory(alert=alert)
            AlertDeliveryFactory(alert=alert)
            alerts.append(_id(alert.lsst_diaObject_diaObjectId))
        return alerts + [_id(UNKNOWN_ID), {'kind': 'bogus'}]

    small, large = batch(2), batch(25)
    with django_assert_num_queries(9):
        lookup_objects(inputs=small, detail='full')
    with django_assert_num_queries(9):
        lookup_objects(inputs=large, detail='full')


# --- U2: the per-object TNS block (R5, R10, KTD7, AE7) ---


_NO_TNS_MATCH = {
    'name': None,
    'name_prefix': None,
    'classification': None,
    'redshift': None,
    'separation_arcsec': None,
    'url': None,
}


@pytest.mark.django_db
@pytest.mark.parametrize('detail', ['matches', 'full'])
def test_matched_tns_association_is_shown_on_the_object(detail):
    epoch = timezone.now() - timedelta(hours=2)
    alert = ObjectCrossmatchRecordFactory().alert
    CatalogMatchFactory(alert=alert)
    TnsAssociation.objects.create(
        alert=alert, checked=True, snapshot_epoch=epoch,
        objid=4242, name='2026xyz', name_prefix='SN', type='SN Ia',
        redshift=0.031, separation_arcsec=0.4,
    )

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail=detail)

    assert _only_object(_only_result(body))['tns'] == {
        'checked': True,
        'snapshot_epoch': epoch.isoformat(),
        'name': '2026xyz',
        'name_prefix': 'SN',
        'classification': 'SN Ia',
        'redshift': 0.031,
        'separation_arcsec': 0.4,
        'url': 'https://www.wis-tns.org/object/2026xyz',
    }


@pytest.mark.django_db
def test_checked_with_no_tns_match_shows_checked_and_null_name_fields():
    epoch = timezone.now()
    alert = ObjectCrossmatchRecordFactory().alert
    TnsAssociation.objects.create(alert=alert, checked=True, snapshot_epoch=epoch)

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId)

    assert _only_object(_only_result(body))['tns'] == {
        'checked': True, 'snapshot_epoch': epoch.isoformat(), **_NO_TNS_MATCH,
    }


@pytest.mark.django_db
def test_crossmatched_with_no_current_snapshot_shows_not_checked():
    # tasks/crossmatch.py writes this row whenever no TNS snapshot is current.
    alert = ObjectCrossmatchRecordFactory().alert
    TnsAssociation.objects.create(alert=alert, checked=False)

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId)

    assert _only_object(_only_result(body))['tns'] == {
        'checked': False, 'snapshot_epoch': None, **_NO_TNS_MATCH,
    }


@pytest.mark.django_db
def test_crossmatched_before_the_tns_feature_shows_null_tns():
    alert = ObjectCrossmatchRecordFactory().alert

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail='full')

    assert _only_object(_only_result(body))['tns'] is None


@pytest.mark.django_db
def test_object_with_no_coincident_source_still_shows_its_tns_association():
    # The per-match tns block at full cannot carry it: there are no matches.
    alert = ObjectCrossmatchRecordFactory().alert
    TnsAssociation.objects.create(
        alert=alert, checked=True, snapshot_epoch=timezone.now(),
        objid=7, name='2026abc', separation_arcsec=0.2,
    )

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail='full')

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.NO_COINCIDENT_SOURCE
    assert obj['matches'] == []
    assert obj['tns']['name'] == '2026abc'


@pytest.mark.django_db
@pytest.mark.parametrize('detail', ['ids', 'position'])
def test_no_tns_block_below_detail_matches(detail):
    alert = ObjectCrossmatchRecordFactory().alert
    TnsAssociation.objects.create(
        alert=alert, checked=True, snapshot_epoch=timezone.now(),
        objid=7, name='2026abc', separation_arcsec=0.2,
    )

    body = get_object(dia_object_id=alert.lsst_diaObject_diaObjectId, detail=detail)

    assert 'tns' not in _only_object(_only_result(body))


class _PgError(Exception):
    """Stands in for the driver error a Django DatabaseError wraps."""

    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _break_record_reads(monkeypatch, sqlstate):
    def fail(*args, **kwargs):
        error = ProgrammingError(f'record read failed ({sqlstate})')
        error.__cause__ = _PgError(sqlstate)
        raise error

    monkeypatch.setattr(ObjectCrossmatchRecord.objects, 'filter', fail)


@pytest.mark.django_db
def test_missing_record_table_reads_as_not_recorded(monkeypatch):
    record = ObjectCrossmatchRecordFactory()
    _break_record_reads(monkeypatch, '42P01')

    body = get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)

    obj = _only_object(_only_result(body))
    assert obj['status'] == ObjectStatus.NO_COINCIDENT_SOURCE
    assert obj['crossmatch']['provenance'] == 'not_recorded'
    assert obj['crossmatch']['best_guess_provenance_set'] == BEST_GUESS_PROVENANCE_SET


@pytest.mark.django_db
def test_other_record_read_errors_still_propagate(monkeypatch):
    record = ObjectCrossmatchRecordFactory()
    _break_record_reads(monkeypatch, '42501')  # insufficient_privilege

    with pytest.raises(ProgrammingError):
        get_object(dia_object_id=record.alert.lsst_diaObject_diaObjectId)
