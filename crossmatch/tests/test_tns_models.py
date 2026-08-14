"""Model tests for the TNS snapshot / association tables (plan U1)."""

import pytest
from django.db import IntegrityError, transaction

from core.models import Alert, TnsAssociation, TnsObject, TnsSnapshotMeta
from tests.factories import AlertFactory


@pytest.mark.django_db
def test_tns_object_created_with_healpix():
    obj = TnsObject.objects.create(
        objid=1001,
        name="2024xyz",
        name_prefix="SN",
        ra_deg=180.0,
        dec_deg=-30.0,
        type="SN Ia",
        redshift=0.05,
        healpix_ipix=123456789,
    )
    assert obj.pk is not None
    assert TnsObject.objects.get(objid=1001).healpix_ipix == 123456789


@pytest.mark.django_db
def test_tns_object_objid_is_unique():
    TnsObject.objects.create(objid=2002, name="a", ra_deg=10.0, dec_deg=10.0)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TnsObject.objects.create(objid=2002, name="b", ra_deg=20.0, dec_deg=20.0)


@pytest.mark.django_db
def test_tns_object_classification_and_redshift_nullable():
    """An unclassified AT with no redshift is a valid row (Covers AE5, storage side)."""
    obj = TnsObject.objects.create(
        objid=3003, name="2024aaa", name_prefix="AT", ra_deg=1.0, dec_deg=1.0
    )
    assert obj.type is None
    assert obj.redshift is None


@pytest.mark.django_db
def test_tns_association_one_per_alert():
    alert = AlertFactory()
    TnsAssociation.objects.create(alert=alert, checked=True, objid=42, name="2024xyz",
                                  separation_arcsec=0.3)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TnsAssociation.objects.create(alert=alert, checked=True)


@pytest.mark.django_db
def test_tns_association_checked_but_no_match():
    """A checked alert with no counterpart stores checked=True and null match fields."""
    alert = AlertFactory()
    assoc = TnsAssociation.objects.create(alert=alert, checked=True)
    assert assoc.checked is True
    assert assoc.objid is None
    assert assoc.name is None
    assert assoc.separation_arcsec is None


@pytest.mark.django_db
def test_tns_association_cascades_on_alert_delete():
    """The association is reclaimed with its alert (bounds table growth)."""
    alert = AlertFactory()
    TnsAssociation.objects.create(alert=alert, checked=True)
    assert TnsAssociation.objects.count() == 1
    alert.delete()
    assert TnsAssociation.objects.count() == 0


@pytest.mark.django_db
def test_tns_snapshot_meta_holds_epoch():
    from django.utils import timezone

    now = timezone.now()
    meta = TnsSnapshotMeta.objects.create(last_refresh_epoch=now)
    assert TnsSnapshotMeta.objects.get(pk=meta.pk).last_refresh_epoch == now
