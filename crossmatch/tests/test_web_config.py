"""Tests for the web frontend's live-config seam (U3).

The seam is the single trust boundary (R11/KTD2): it exposes only named
display fields and must never leak a secret setting onto a public page (R14).
Also covers the unset (R12/AE1), version (AE3), lowercasing (AE2/KTD6), and
section-read-failure (AE5) behaviors.
"""

import importlib.metadata
from unittest import mock

import pytest
from django.test import override_settings

from web import config


SECRET_SETTINGS = [
    'SECRET_KEY',
    'DATABASE_PASSWORD',
    'HOPSKOTCH_USERNAME',
    'HOPSKOTCH_PASSWORD',
    'ANTARES_API_KEY',
    'ANTARES_API_SECRET',
]


def _all_values(obj):
    """Flatten every scalar value reachable from a seam result for scanning."""
    out = []
    if isinstance(obj, dict):
        for value in obj.values():
            out.extend(_all_values(value))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            out.extend(_all_values(item))
    else:
        out.append(obj)
    return out


@override_settings(
    SECRET_KEY='super-secret-key-value',
    DATABASE_PASSWORD='db-pw-value',
    HOPSKOTCH_USERNAME='hop-user-value',
    HOPSKOTCH_PASSWORD='hop-pw-value',
    ANTARES_API_KEY='antares-key-value',
    ANTARES_API_SECRET='antares-secret-value',
)
def test_seam_never_exposes_secret_settings():
    """Covers R11: assembled seam output contains no secret setting values."""
    combined = {
        'service': config.service_config(),
        'catalogs': config.catalogs(),
        'brokers': config.brokers(),
    }
    values = [str(v) for v in _all_values(combined)]
    blob = '\n'.join(values)
    for secret_value in [
        'super-secret-key-value',
        'db-pw-value',
        'hop-user-value',
        'hop-pw-value',
        'antares-key-value',
        'antares-secret-value',
    ]:
        assert secret_value not in blob


@override_settings(HOPSKOTCH_TOPIC='')
def test_empty_hopskotch_topic_is_not_configured():
    """Covers AE1/R12: empty topic -> NOT_CONFIGURED, not an empty string."""
    cfg = config.service_config()
    assert cfg['hopskotch_topic'] is config.NOT_CONFIGURED
    assert not cfg['hopskotch_topic']


def test_lsdb_version_read_from_package_metadata():
    """Covers AE3: version comes from importlib.metadata, not a constant."""
    with mock.patch.object(
        importlib.metadata, 'version', return_value='9.9.9-test'
    ) as m:
        assert config.lsdb_version() == '9.9.9-test'
    m.assert_called_once_with('lsdb')


def test_lsdb_version_missing_package_is_not_configured():
    with mock.patch.object(
        importlib.metadata,
        'version',
        side_effect=importlib.metadata.PackageNotFoundError,
    ):
        assert config.lsdb_version() is config.NOT_CONFIGURED


@override_settings(
    CROSSMATCH_CATALOGS=[
        {
            'name': 'des_y6_gold',
            'source_id_column': 'COADD_OBJECT_ID',
            'ra_column': 'RA',
            'dec_column': 'DEC',
            'payload_columns': ['WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R'],
        }
    ]
)
def test_catalog_columns_are_lowercased():
    """Covers AE2/KTD6: published columns match the lowercased Hopskotch keys."""
    cats = config.catalogs()
    assert cats[0]['published_columns'] == ['wavg_mag_psf_g', 'wavg_mag_psf_r']


@override_settings(CROSSMATCH_CATALOGS=[])
def test_empty_catalog_list_yields_empty():
    """Covers the empty-catalog state (design finding): [] not an error."""
    assert config.catalogs() == []


def test_catalog_read_failure_is_section_unavailable():
    """Covers AE5: a read error degrades the section, never raises."""
    with override_settings(CROSSMATCH_CATALOGS=object()):
        # object() is not iterable -> the seam catches and degrades.
        result = config.catalogs()
    assert isinstance(result, config.SectionUnavailable)
    assert not result
