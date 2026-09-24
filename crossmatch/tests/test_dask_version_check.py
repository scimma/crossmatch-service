"""Off-boundary lsdb/hats version guard in core/dask.py.

distributed.get_versions() reports a fixed package set that does NOT include lsdb
or hats, so adding them to _VERSION_CHECK_PACKAGES is a silent no-op — every role
reports None and None != None is false. These cover the separate guard that asks
each worker for its real lsdb/hats version via client.run and fails fast on skew,
which is the only thing that catches an app/cluster lsdb drift (matches would
otherwise (de)serialize incorrectly with no pickle-layer error).
"""

from unittest import mock

from core import dask as dask_mod


def _fake_client(worker_results):
    """A stand-in Dask client whose .run(func) returns a preset {worker: result}."""
    client = mock.Mock()
    client.run.return_value = worker_results
    return client


def test_aligned_workers_report_no_drift():
    client = _fake_client({
        'tcp://w1': {'lsdb': '0.10.4', 'hats': '0.10.4'},
        'tcp://w2': {'lsdb': '0.10.4', 'hats': '0.10.4'},
    })
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': '0.10.4', 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert drift == []


def test_lsdb_skew_flags_drift():
    # A worker still on the old lsdb while the client (app) moved to 0.10.4.
    client = _fake_client({'tcp://w1': {'lsdb': '0.9.0', 'hats': '0.10.4'}})
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': '0.10.4', 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert len(drift) == 1
    assert drift[0]['package'] == 'lsdb'
    assert drift[0]['client_version'] == '0.10.4'
    assert drift[0]['worker_versions'] == {'tcp://w1': '0.9.0'}


def test_hats_skew_flags_drift():
    client = _fake_client({'tcp://w1': {'lsdb': '0.10.4', 'hats': '0.9.0'}})
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': '0.10.4', 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert [d['package'] for d in drift] == ['hats']


def test_worker_missing_import_flags_drift():
    # A worker that cannot import lsdb reports None -> counts as drift, not a crash.
    client = _fake_client({'tcp://w1': {'lsdb': None, 'hats': '0.10.4'}})
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': '0.10.4', 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert [d['package'] for d in drift] == ['lsdb']
    assert drift[0]['worker_versions'] == {'tcp://w1': None}


def test_no_workers_answered_flags_drift():
    # client.run returns {} (workers dropped in the wait->run window). Comparing
    # the client against zero workers must NOT read as aligned -> fail closed.
    client = _fake_client({})
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': '0.10.4', 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert {d['package'] for d in drift} == {'lsdb', 'hats'}
    assert all(d['worker_versions'] == {} for d in drift)


def test_client_cannot_import_engine_flags_drift():
    # The app image itself cannot import lsdb (client_ver None). A broken app
    # build must fail fast, not read as aligned just because a worker also lacks it.
    client = _fake_client({'tcp://w1': {'lsdb': None, 'hats': '0.10.4'}})
    with mock.patch.object(dask_mod, '_package_versions_local',
                           return_value={'lsdb': None, 'hats': '0.10.4'}):
        drift = dask_mod._check_off_boundary_versions(client)
    assert [d['package'] for d in drift] == ['lsdb']
    assert drift[0]['client_version'] is None


def test_package_versions_local_reports_installed_lsdb_and_hats():
    versions = dask_mod._package_versions_local()
    assert set(versions) == {'lsdb', 'hats'}
    # Both are installed in the image, so neither is None.
    assert versions['lsdb'] is not None
    assert versions['hats'] is not None


def test_package_versions_local_maps_missing_package_to_none(monkeypatch):
    # Proves the import-failure branch: a package that cannot be imported -> None,
    # not an exception (so client.run does not fail on a worker missing the package).
    monkeypatch.setattr(dask_mod, '_OFF_BOUNDARY_PACKAGES',
                        ('lsdb', 'definitely_not_a_real_pkg_zzz'))
    versions = dask_mod._package_versions_local()
    assert versions['lsdb'] is not None
    assert versions['definitely_not_a_real_pkg_zzz'] is None


# --- Shared alignment check (replay tool plan U2, KTD4) -----------------------
#
# check_cluster_alignment is the same check the worker's startup guard runs, but
# it returns drift instead of calling _fail_fast (which SIGTERMs the parent
# process -- under `kubectl exec` that would hit the operator's shell).


def _aligned_client():
    client = mock.Mock()
    client.get_versions.return_value = {
        'client': {'packages': {'numpy': '2.4.2'}},
        'scheduler': {'packages': {'numpy': '2.4.2'}},
        'workers': {'tcp://w1': {'packages': {'numpy': '2.4.2'}}},
    }
    client.run.return_value = {'tcp://w1': {'lsdb': '0.10.4', 'hats': '0.10.4'}}
    client.scheduler_info.return_value = {'workers': {'tcp://w1': {}}}
    return client


def _local_versions():
    return mock.patch.object(dask_mod, '_package_versions_local',
                             return_value={'lsdb': '0.10.4', 'hats': '0.10.4'})


def test_check_cluster_alignment_returns_client_and_no_drift():
    client = _aligned_client()
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=client), \
            mock.patch.object(dask_mod, '_fail_fast') as fail_fast, \
            _local_versions():
        got, drift = dask_mod.check_cluster_alignment('tcp://sched:8786', timeout=5)
    assert got is client
    assert drift == []
    fail_fast.assert_not_called()
    client.close.assert_not_called()


def test_check_cluster_alignment_reports_lsdb_skew_without_fail_fast():
    client = _aligned_client()
    client.run.return_value = {'tcp://w1': {'lsdb': '0.9.0', 'hats': '0.10.4'}}
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=client), \
            mock.patch.object(dask_mod, '_fail_fast') as fail_fast, \
            _local_versions():
        _, drift = dask_mod.check_cluster_alignment('tcp://sched:8786', timeout=5)
    assert [d['package'] for d in drift] == ['lsdb']
    fail_fast.assert_not_called()


def test_check_cluster_alignment_raises_when_scheduler_unreachable():
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=None), \
            mock.patch.object(dask_mod, '_fail_fast') as fail_fast:
        try:
            dask_mod.check_cluster_alignment('tcp://sched:8786', timeout=5)
        except dask_mod.DaskAlignmentError as exc:
            assert exc.reason == 'scheduler unreachable'
        else:
            raise AssertionError('expected DaskAlignmentError')
    fail_fast.assert_not_called()


def test_check_cluster_alignment_closes_client_when_no_workers():
    client = _aligned_client()
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=client), \
            mock.patch.object(dask_mod, '_wait_for_worker', return_value=False):
        try:
            dask_mod.check_cluster_alignment('tcp://sched:8786', timeout=5)
        except dask_mod.DaskAlignmentError as exc:
            assert exc.reason == 'no workers registered'
        else:
            raise AssertionError('expected DaskAlignmentError')
    client.close.assert_called_once()


def test_verify_dask_versions_still_fails_fast_on_drift(settings):
    settings.DASK_SCHEDULER_ADDRESS = 'tcp://sched:8786'
    client = _aligned_client()
    client.run.return_value = {'tcp://w1': {'lsdb': '0.9.0', 'hats': '0.10.4'}}
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=client), \
            mock.patch.object(dask_mod, '_fail_fast') as fail_fast, \
            _local_versions():
        dask_mod.verify_dask_versions()
    fail_fast.assert_called_once()
    client.close.assert_called_once()


def test_verify_dask_versions_passes_aligned_cluster(settings):
    settings.DASK_SCHEDULER_ADDRESS = 'tcp://sched:8786'
    client = _aligned_client()
    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=client), \
            mock.patch.object(dask_mod, '_fail_fast') as fail_fast, \
            _local_versions():
        dask_mod.verify_dask_versions()
    fail_fast.assert_not_called()
    client.close.assert_called_once()


def test_verify_dask_versions_fails_fast_when_scheduler_unreachable(settings):
    settings.DASK_SCHEDULER_ADDRESS = 'tcp://sched:8786'

    class _Exit(Exception):
        pass

    with mock.patch.object(dask_mod, '_connect_with_retry', return_value=None), \
            mock.patch.object(dask_mod, '_fail_fast', side_effect=_Exit):
        try:
            dask_mod.verify_dask_versions()
        except _Exit:
            pass
        else:
            raise AssertionError('expected _fail_fast')


def test_cluster_package_versions_reports_client_and_workers():
    client = mock.Mock()
    client.get_versions.return_value = {
        'client': {'packages': {'python': '3.12.8', 'dask': '2026.1.2'}},
        'scheduler': {'packages': {'python': '3.12.8', 'dask': '2026.1.2'}},
        'workers': {'tcp://w1': {'packages': {'python': '3.12.8',
                                              'dask': '2026.1.2'}}},
    }
    client.run.return_value = {
        'tcp://w1': {'lsdb': '0.10.4', 'hats': '0.10.4',
                     'nested_pandas': '0.6.10'},
    }
    with mock.patch.object(
        dask_mod, '_context_versions_local',
        return_value={'lsdb': '0.10.4', 'hats': '0.10.4', 'nested_pandas': '0.6.10'},
    ):
        versions = dask_mod.cluster_package_versions(client)
    assert versions['client']['nested_pandas'] == '0.6.10'
    assert versions['client']['dask'] == '2026.1.2'
    assert versions['scheduler']['python'] == '3.12.8'
    assert versions['workers']['tcp://w1']['nested_pandas'] == '0.6.10'
    assert versions['workers']['tcp://w1']['lsdb'] == '0.10.4'


def test_context_versions_local_includes_nested_pandas():
    versions = dask_mod._context_versions_local()
    assert set(versions) == {'lsdb', 'hats', 'nested_pandas'}
    assert versions['nested_pandas']
