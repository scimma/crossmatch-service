"""factory_boy factories for the alert -> CatalogMatch -> Notification graph.

The Alert FKs on CatalogMatch / Notification use ``to_field='lsst_diaObject_diaObjectId'``
(not the uuid pk); SubFactory wiring below resolves that relation correctly so tests build
graphs the same way production does.
"""

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from core import provenance
from core.models import (
    Alert,
    AlertDelivery,
    CatalogMatch,
    CatalogSearchOutcome,
    Notification,
    ObjectCrossmatchRecord,
    ProvenanceSet,
)


class AlertFactory(DjangoModelFactory):
    class Meta:
        model = Alert

    lsst_diaObject_diaObjectId = factory.Sequence(lambda n: 9_000_000_000 + n)
    lsst_diaSource_diaSourceId = factory.Sequence(lambda n: 8_000_000_000 + n)
    ra_deg = 180.0
    dec_deg = -30.0
    event_time = factory.LazyFunction(timezone.now)
    schema_version = 1
    payload = factory.LazyFunction(dict)
    status = Alert.Status.INGESTED


class AlertDeliveryFactory(DjangoModelFactory):
    class Meta:
        model = AlertDelivery

    alert = factory.SubFactory(AlertFactory)
    broker = "antares"


class CatalogMatchFactory(DjangoModelFactory):
    class Meta:
        model = CatalogMatch

    alert = factory.SubFactory(AlertFactory)
    catalog_name = "gaia_dr3"
    catalog_source_id = factory.Sequence(lambda n: str(7_000_000_000 + n))
    match_distance_arcsec = 0.5
    source_ra_deg = 180.0
    source_dec_deg = -30.0
    catalog_payload = factory.LazyFunction(dict)
    match_version = 1


class NotificationFactory(DjangoModelFactory):
    class Meta:
        model = Notification

    alert = factory.SubFactory(AlertFactory)
    destination = "hopskotch"
    payload = factory.LazyFunction(dict)
    state = Notification.State.PENDING


class ProvenanceSetFactory(DjangoModelFactory):
    """A provenance set built from the live settings, as the crossmatch task does."""

    class Meta:
        model = ProvenanceSet

    content_hash = factory.Sequence(lambda n: f'{n:064x}')
    crossmatch_radius_arcsec = factory.LazyFunction(provenance.crossmatch_radius_arcsec)
    catalogs = factory.LazyFunction(provenance.catalog_releases)
    reliability_cuts = factory.LazyFunction(provenance.reliability_cuts)


def _all_searched():
    return {
        cat['name']: CatalogSearchOutcome.SEARCHED.value
        for cat in provenance.catalog_releases()
    }


class ObjectCrossmatchRecordFactory(DjangoModelFactory):
    """A per-object crossmatch record; by default every catalog in service searched."""

    class Meta:
        model = ObjectCrossmatchRecord

    alert = factory.SubFactory(AlertFactory, status=Alert.Status.MATCHED)
    match_version = 1
    provenance_set = factory.SubFactory(ProvenanceSetFactory)
    catalog_outcomes = factory.LazyFunction(_all_searched)
    brokers = factory.LazyFunction(lambda: ['antares'])
    crossmatched_at = factory.LazyFunction(timezone.now)


def set_ingest_time(alert, when):
    """Force an Alert's ``ingest_time`` to ``when``.

    ``Alert.ingest_time`` is ``auto_now_add`` so Django ignores any value passed
    at construction; tests that need to pin it use an explicit UPDATE afterward,
    mirroring how production stamps it on ingest.
    """
    Alert.objects.filter(pk=alert.pk).update(ingest_time=when)


def make_alert_with_notifications(status, notification_states, destination="hopskotch"):
    """Build one Alert at ``status`` plus one Notification per entry in
    ``notification_states`` (each a Notification.State), all on ``destination``.

    Returns (alert, [notifications]).
    """
    alert = AlertFactory(status=status)
    notifications = [
        NotificationFactory(alert=alert, destination=destination, state=state)
        for state in notification_states
    ]
    return alert, notifications
