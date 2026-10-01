"""U1 / R17, R20, R27; KTD7, KTD8: the single provenance builder.

Service-level provenance (version, contract version, radius, catalog releases,
per-broker reliability cuts) is built from live settings by
``core/provenance.py`` alone, so the API, the docs, and the web tier cannot
drift from the running service. Covers the settings-import validation of the
catalog ``release`` labels and the declared broker cuts, too.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from core import provenance
from core.models import CutEnforcedBy, CutStatus

APP_ROOT = Path(__file__).resolve().parent.parent

UNDECLARED = {
    'ANTARES_DECLARED_MIN_RELIABILITY': None,
    'ANTARES_DECLARED_MIN_RELIABILITY_AS_OF': None,
    'LASAIR_DECLARED_MIN_RELIABILITY': None,
    'LASAIR_DECLARED_MIN_RELIABILITY_AS_OF': None,
}


def _cut(prov, broker):
    return next(c for c in prov['reliability_cuts'] if c['broker'] == broker)


def _import_settings(**env):
    """Import ``project.settings`` in a fresh interpreter with ``env`` applied.

    A subprocess keeps the import-time checks honest (they run exactly as at
    process start) without re-executing the settings module in this process.
    """
    full_env = {**os.environ, **env}
    return subprocess.run(
        [
            sys.executable,
            '-c',
            'import json, project.settings as s; '
            'print(json.dumps({'
            '"releases": [c["release"] for c in s.CROSSMATCH_CATALOGS], '
            '"lasair": [s.LASAIR_DECLARED_MIN_RELIABILITY, '
            's.LASAIR_DECLARED_MIN_RELIABILITY_AS_OF]}))',
        ],
        cwd=APP_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
    )


# --- service-level provenance (R17) -------------------------------------------


@override_settings(CROSSMATCH_RADIUS_ARCSEC=2.0)
def test_radius_read_from_live_settings():
    """R27: an overridden radius is reported with no code change."""
    assert provenance.service_provenance()['crossmatch_radius_arcsec'] == 2.0


@override_settings(APP_VERSION='9.8.7')
def test_service_and_contract_version():
    prov = provenance.service_provenance()
    assert prov['service_version'] == '9.8.7'
    assert prov['contract_version'] == provenance.CONTRACT_VERSION


def test_catalog_releases_follow_configured_catalogs():
    prov = provenance.service_provenance()
    assert [c['name'] for c in prov['catalogs']] == [
        c['name'] for c in settings.CROSSMATCH_CATALOGS
    ]
    assert all(isinstance(c['release'], str) and c['release'] for c in prov['catalogs'])


@override_settings(
    CROSSMATCH_CATALOGS=[
        {'name': 'gaia_dr3', 'release': 'Gaia DR3', 'hats_url': 's3://secret-bucket'}
    ]
)
def test_catalog_entries_expose_only_name_and_release():
    """Only the allowlisted fields leave the builder (never the HATS URL)."""
    assert provenance.catalog_releases() == [{'name': 'gaia_dr3', 'release': 'Gaia DR3'}]


@override_settings(CROSSMATCH_CATALOGS=[{'name': 'gaia_dr3'}])
def test_catalog_without_release_fails_loudly_at_read_time():
    with pytest.raises(ImproperlyConfigured, match='gaia_dr3'):
        provenance.catalog_releases()


def test_output_round_trips_through_json_without_custom_encoder():
    prov = provenance.service_provenance()
    assert json.loads(json.dumps(prov)) == prov


@override_settings(
    LASAIR_DECLARED_MIN_RELIABILITY=0.7,
    LASAIR_DECLARED_MIN_RELIABILITY_AS_OF=date(2026, 9, 1),
)
def test_date_valued_as_of_is_emitted_as_iso_string():
    prov = provenance.service_provenance()
    assert _cut(prov, 'lasair')['as_of'] == '2026-09-01'
    assert json.loads(json.dumps(prov)) == prov


# --- per-broker reliability cuts (R20) ----------------------------------------


@override_settings(MIN_DIASOURCE_RELIABILITY=0.75, **UNDECLARED)
def test_pittgoogle_cut_is_enforced_by_service_setting():
    cut = _cut(provenance.service_provenance(), 'pittgoogle')
    assert cut == {
        'broker': 'pittgoogle',
        'min_reliability': 0.75,
        'enforced_by': CutEnforcedBy.SERVICE.value,
        'status': CutStatus.SERVICE_SETTING.value,
        'as_of': None,
    }


@override_settings(**UNDECLARED)
def test_undeclared_antares_cut_reports_not_declared():
    cut = _cut(provenance.service_provenance(), 'antares')
    assert cut == {
        'broker': 'antares',
        'min_reliability': None,
        'enforced_by': CutEnforcedBy.BROKER.value,
        'status': CutStatus.NOT_DECLARED.value,
        'as_of': None,
    }


@override_settings(
    **{
        **UNDECLARED,
        'LASAIR_DECLARED_MIN_RELIABILITY': 0.6,
        'LASAIR_DECLARED_MIN_RELIABILITY_AS_OF': '2026-09-15',
    }
)
def test_declared_lasair_cut_reports_value_date_and_broker_enforcement():
    prov = provenance.service_provenance()
    assert _cut(prov, 'lasair') == {
        'broker': 'lasair',
        'min_reliability': 0.6,
        'enforced_by': 'broker',
        'status': 'declared',
        'as_of': '2026-09-15',
    }
    # The other broker-enforced cut is untouched by Lasair's declaration.
    assert _cut(prov, 'antares')['status'] == 'not_declared'


@override_settings(
    **{**UNDECLARED, 'ANTARES_DECLARED_MIN_RELIABILITY': 0.6}
)
def test_declared_value_without_as_of_date_is_not_reported_as_declared():
    """R20 requires a stated date; a half-declared cut is never asserted."""
    cut = _cut(provenance.service_provenance(), 'antares')
    assert cut['status'] == 'not_declared'
    assert cut['min_reliability'] is None


def test_every_broker_reported_once_in_fixed_order():
    brokers = [c['broker'] for c in provenance.service_provenance()['reliability_cuts']]
    assert brokers == ['antares', 'lasair', 'pittgoogle']


# --- web tier reads through the builder (KTD8) --------------------------------


def test_web_config_reads_provenance_through_builder():
    from web import config

    with mock.patch.object(
        provenance, 'crossmatch_radius_arcsec', return_value=3.5
    ), mock.patch.object(
        provenance, 'service_version', return_value='1.2.3-built'
    ), mock.patch.object(
        provenance, 'service_min_reliability', return_value=0.42
    ):
        cfg = config.service_config()
    assert cfg['crossmatch_radius_arcsec'] == 3.5
    assert cfg['app_version'] == '1.2.3-built'
    assert cfg['min_diasource_reliability'] == 0.42


@pytest.mark.parametrize(
    'setting',
    [
        'CROSSMATCH_RADIUS_ARCSEC',
        'MIN_DIASOURCE_RELIABILITY',
        'APP_VERSION',
        'ANTARES_DECLARED_MIN_RELIABILITY',
        'ANTARES_DECLARED_MIN_RELIABILITY_AS_OF',
        'LASAIR_DECLARED_MIN_RELIABILITY',
        'LASAIR_DECLARED_MIN_RELIABILITY_AS_OF',
    ],
)
def test_builder_is_only_reader_of_provenance_settings_in_api_and_web(setting):
    """KTD8: the API and web tiers never read provenance settings themselves."""
    read = re.compile(rf"settings\s*,\s*['\"]{setting}['\"]|settings\.{setting}\b")
    offenders = [
        str(path.relative_to(APP_ROOT))
        for pkg in ('api', 'web')
        for path in (APP_ROOT / pkg).rglob('*.py')
        if read.search(path.read_text())
    ]
    assert offenders == []


# --- settings import-time validation (KTD7, KTD8) -----------------------------


def test_default_catalogs_all_carry_a_release():
    assert all(c.get('release') for c in settings.CROSSMATCH_CATALOGS)


def test_catalog_release_is_env_overridable():
    result = _import_settings(GAIA_RELEASE='Gaia DR3 (custom build)')
    assert result.returncode == 0, result.stderr
    releases = json.loads(result.stdout.strip().splitlines()[-1])['releases']
    assert 'Gaia DR3 (custom build)' in releases


def test_catalog_entry_without_release_raises_at_import():
    result = _import_settings(GAIA_RELEASE='  ')
    assert result.returncode != 0
    assert 'ImproperlyConfigured' in result.stderr
    assert 'gaia_dr3' in result.stderr


def test_validate_catalog_releases_rejects_missing_key():
    from project.settings import _validate_catalog_releases

    with pytest.raises(ImproperlyConfigured, match='des_y6_gold'):
        _validate_catalog_releases([{'name': 'des_y6_gold'}])


def test_declared_cut_env_parsed_with_iso_date():
    result = _import_settings(
        LASAIR_DECLARED_MIN_RELIABILITY='0.6',
        LASAIR_DECLARED_MIN_RELIABILITY_AS_OF='2026-09-15',
    )
    assert result.returncode == 0, result.stderr
    lasair = json.loads(result.stdout.strip().splitlines()[-1])['lasair']
    assert lasair == [0.6, '2026-09-15']


def test_unset_declared_cut_env_is_none():
    env = {k: '' for k in UNDECLARED}
    result = _import_settings(**env)
    assert result.returncode == 0, result.stderr
    lasair = json.loads(result.stdout.strip().splitlines()[-1])['lasair']
    assert lasair == [None, None]


@pytest.mark.parametrize(
    'env',
    [
        # value without a date
        {'LASAIR_DECLARED_MIN_RELIABILITY': '0.6'},
        # date without a value
        {'ANTARES_DECLARED_MIN_RELIABILITY_AS_OF': '2026-09-15'},
        # not an ISO date
        {
            'LASAIR_DECLARED_MIN_RELIABILITY': '0.6',
            'LASAIR_DECLARED_MIN_RELIABILITY_AS_OF': '15/09/2026',
        },
        # outside [0, 1]
        {
            'ANTARES_DECLARED_MIN_RELIABILITY': '1.5',
            'ANTARES_DECLARED_MIN_RELIABILITY_AS_OF': '2026-09-15',
        },
        # nan
        {
            'ANTARES_DECLARED_MIN_RELIABILITY': 'nan',
            'ANTARES_DECLARED_MIN_RELIABILITY_AS_OF': '2026-09-15',
        },
    ],
)
def test_invalid_declared_cut_raises_at_import(env):
    result = _import_settings(**{**{k: '' for k in UNDECLARED}, **env})
    assert result.returncode != 0
    assert 'ImproperlyConfigured' in result.stderr
