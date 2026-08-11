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
