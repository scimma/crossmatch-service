"""TNS association wired into the crossmatch batch (plan U7).

The Dask/LSDB path is mocked at its two seams (lsdb.from_dataframe and
crossmatch_alerts), the same pattern as test_crossmatch_catalog_skip; the TNS
snapshot is seeded directly in the DB.
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest
from django.test import override_settings
from django.utils import timezone

import tasks.crossmatch as crossmatch_mod
from core.healpix import radec_to_ipix
from core.models import (
    Alert,
    Notification,
    TnsAssociation,
    TnsObject,
    TnsSnapshotMeta,
)
from tasks.crossmatch import _build_tns_associations, crossmatch_batch
from tests.factories import AlertFactory

ARCSEC = 1.0 / 3600.0

ONE_CATALOG = [{
    "name": "cat_a", "hats_url": "x", "source_id_column": "source_id",
    "ra_column": "ra", "dec_column": "dec", "payload_columns": ["mag"],
}]
TWO_CATALOGS = [
    {"name": "cat_a", "hats_url": "x", "source_id_column": "source_id",
     "ra_column": "ra", "dec_column": "dec", "payload_columns": ["mag"]},
    {"name": "cat_b", "hats_url": "y", "source_id_column": "source_id",
     "ra_column": "ra", "dec_column": "dec", "payload_columns": ["mag"]},
]


def _match_row(alert):
    return pd.DataFrame([{
        "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
        "source_id": "cat-0", "_dist_arcsec": 0.4,
        "ra": 180.0, "dec": -30.0, "mag": 18.2,
    }])


@pytest.fixture(autouse=True)
def _mock_lsdb(monkeypatch):
    monkeypatch.setattr(crossmatch_mod.lsdb, "from_dataframe", lambda *a, **k: MagicMock())


def _seed_current_snapshot(near_alert=True):
    TnsSnapshotMeta.objects.create(pk=1, last_refresh_epoch=timezone.now())
    ra, dec = (180.0, -30.0 + 0.3 * ARCSEC) if near_alert else (0.0, 0.0)
    return TnsObject.objects.create(
        objid=4242, name="2024xyz", name_prefix="SN", ra_deg=ra, dec_deg=dec,
        type="SN Ia", redshift=0.05, healpix_ipix=radec_to_ipix(ra, dec),
    )


# --- End-to-end through crossmatch_batch -------------------------------------

@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_matched_alert_gets_tns_block(monkeypatch):
    """AE1: an alert within the radius of a TNS object carries a tns block."""
    alert = AlertFactory(status=Alert.Status.QUEUED)  # (180, -30)
    _seed_current_snapshot(near_alert=True)
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert))

    crossmatch_batch([str(alert.uuid)])

    payload = Notification.objects.get(alert=alert).payload
    assert payload["tns_checked"] is True
    assert payload["tns"]["objid"] == 4242
    assert payload["tns"]["name"] == "2024xyz"
    assert payload["tns"]["url"] == "https://www.wis-tns.org/object/2024xyz"
    assert payload["tns"]["classification"] == "SN Ia"
    # Persisted for the API full level.
    assert TnsAssociation.objects.get(alert=alert).objid == 4242


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=TWO_CATALOGS)
def test_tns_block_fans_across_catalog_matches(monkeypatch):
    """One alert's association rides every catalog-match notification it produces."""
    alert = AlertFactory(status=Alert.Status.QUEUED)
    _seed_current_snapshot(near_alert=True)
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert))

    crossmatch_batch([str(alert.uuid)])

    notifications = Notification.objects.filter(alert=alert)
    assert notifications.count() == 2
    for notification in notifications:
        assert notification.payload["tns"]["objid"] == 4242


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_no_snapshot_publishes_without_tns_and_completes(monkeypatch):
    """AE4: with no snapshot the batch publishes no tns block and still MATCHES."""
    alert = AlertFactory(status=Alert.Status.QUEUED)  # no snapshot seeded
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert))

    crossmatch_batch([str(alert.uuid)])

    payload = Notification.objects.get(alert=alert).payload
    assert "tns" not in payload
    assert payload["tns_checked"] is False
    alert.refresh_from_db()
    assert alert.status == Alert.Status.MATCHED
    assert TnsAssociation.objects.get(alert=alert).checked is False


@pytest.mark.django_db
@override_settings(CROSSMATCH_CATALOGS=ONE_CATALOG)
def test_checked_no_counterpart_has_indicator_no_block(monkeypatch):
    """AE2: a checked alert with no nearby TNS object gets checked=True, no block."""
    alert = AlertFactory(status=Alert.Status.QUEUED)
    _seed_current_snapshot(near_alert=False)  # only object is far away
    monkeypatch.setattr(crossmatch_mod, "crossmatch_alerts", lambda *a, **k: _match_row(alert))

    crossmatch_batch([str(alert.uuid)])

    payload = Notification.objects.get(alert=alert).payload
    assert "tns" not in payload
    assert payload["tns_checked"] is True
    assoc = TnsAssociation.objects.get(alert=alert)
    assert assoc.checked is True and assoc.objid is None


# --- Direct unit tests of the association helper -----------------------------

def _clean_df(alert):
    return pd.DataFrame([{
        "uuid": str(alert.uuid),
        "lsst_diaObject_diaObjectId": alert.lsst_diaObject_diaObjectId,
        "ra_deg": alert.ra_deg, "dec_deg": alert.dec_deg,
    }])


@pytest.mark.django_db
def test_build_associations_stale_snapshot_is_not_checked():
    alert = AlertFactory()
    # Epoch older than the default max age (7200s) -> stale.
    TnsSnapshotMeta.objects.create(
        pk=1, last_refresh_epoch=timezone.now() - timedelta(hours=3)
    )
    TnsObject.objects.create(
        objid=1, name="x", ra_deg=alert.ra_deg, dec_deg=alert.dec_deg,
        healpix_ipix=radec_to_ipix(alert.ra_deg, alert.dec_deg),
    )
    enrichment = _build_tns_associations(_clean_df(alert))
    info = enrichment[alert.lsst_diaObject_diaObjectId]
    assert info["tns"] is None and info["tns_checked"] is False
    assert TnsAssociation.objects.get(alert=alert).checked is False


@pytest.mark.django_db
def test_build_associations_upserts_on_rerun():
    """Re-running the association updates the alert's row rather than duplicating."""
    alert = AlertFactory()
    _seed_current_snapshot(near_alert=False)  # far object -> no match, checked
    _build_tns_associations(_clean_df(alert))
    _build_tns_associations(_clean_df(alert))
    assert TnsAssociation.objects.filter(alert=alert).count() == 1
