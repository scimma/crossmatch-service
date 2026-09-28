"""The single builder of service-level provenance (R17, R20, R27; KTD8).

Every surface that reports the service's configuration-derived facts -- the API
envelope, the OpenAPI document, the agent-facing docs, the web tier
(``web/config.py``), and the crossmatch task when it records provenance -- calls
this module rather than reading the settings itself, so the facts cannot drift
between surfaces or from the running service.

Settings are read at call time so ``@override_settings`` applies. Output is
JSON-native (str, float, None, lists, and dicts of those): it round-trips
through ``json.dumps`` with no custom encoder and can be stored in a
``JSONField`` as-is.

This module lives in ``core`` so the Celery worker can use it without
importing the API layer; the codes it emits are the stored ``TextChoices`` in
``core.models``.
"""

from __future__ import annotations

import datetime
import math
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.log import get_logger
from core.models import CutEnforcedBy, CutStatus

logger = get_logger(__name__)

# Version of the API response contract (codes, envelope, error shape). Bumped
# by hand when the contract changes; independent of the service version.
CONTRACT_VERSION = '1.0.0'

# Broker identifiers as stored in ``AlertDelivery.broker``, in reporting order.
_BROKER_ENFORCED = ('antares', 'lasair')
_SERVICE_ENFORCED = 'pittgoogle'


def _json_float(value: Any) -> float | None:
    """Coerce a numeric setting to a finite ``float``, or ``None`` if unset.

    Args:
        value: The setting value (``None``, int, float, or numeric string).

    Returns:
        A finite float, or ``None`` for an unset or non-finite value.
    """
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _iso_date(value: Any) -> str | None:
    """Render an as-of date as an ISO ``YYYY-MM-DD`` string.

    Args:
        value: A ``date``, an ISO date string, or ``None``/empty.

    Returns:
        The ISO date string, or ``None`` when unset.
    """
    if value in (None, ''):
        return None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    return datetime.date.fromisoformat(str(value)).isoformat()


def service_version() -> str | None:
    """The deployed service version (``APP_VERSION``), or ``None`` if unset."""
    value = getattr(settings, 'APP_VERSION', None)
    if value is None or str(value).strip() == '':
        return None
    return str(value)


def crossmatch_radius_arcsec() -> float | None:
    """The configured crossmatch radius in arcsec, or ``None`` if unset."""
    return _json_float(getattr(settings, 'CROSSMATCH_RADIUS_ARCSEC', None))


def service_min_reliability() -> float | None:
    """The reliability cut this service enforces itself (Pitt-Google path)."""
    return _json_float(getattr(settings, 'MIN_DIASOURCE_RELIABILITY', None))


def catalog_releases() -> list[dict[str, str]]:
    """The catalogs in service with their configured release labels (KTD7).

    Only ``name`` and ``release`` leave this function; the rest of each
    catalog entry (HATS URL, columns) is not provenance.

    Returns:
        ``[{'name': ..., 'release': ...}, ...]`` in configured order.

    Raises:
        ImproperlyConfigured: If an entry has no ``release`` label. Settings
            import already rejects that; this catches a runtime override.
    """
    result = []
    for cat in getattr(settings, 'CROSSMATCH_CATALOGS', None) or []:
        release = cat.get('release')
        if not isinstance(release, str) or not release.strip():
            raise ImproperlyConfigured(
                f"catalog {cat.get('name')!r} has no 'release' label"
            )
        result.append({'name': str(cat['name']), 'release': release})
    return result


def _declared_cut(broker: str) -> dict[str, Any]:
    """One broker-enforced cut, as declared by the maintainer (R20).

    A value without an as-of date is reported as ``not_declared``: R20 only
    allows asserting a broker's cut together with the date it was declared.
    Settings import rejects that combination; this guards a runtime override.
    """
    prefix = broker.upper()
    value = _json_float(getattr(settings, f'{prefix}_DECLARED_MIN_RELIABILITY', None))
    as_of = _iso_date(getattr(settings, f'{prefix}_DECLARED_MIN_RELIABILITY_AS_OF', None))
    declared = value is not None and as_of is not None
    if not declared and (value is not None or as_of is not None):
        logger.warning('provenance_incomplete_declared_cut', broker=broker)
    return {
        'broker': broker,
        'min_reliability': value if declared else None,
        'enforced_by': CutEnforcedBy.BROKER.value,
        'status': (CutStatus.DECLARED if declared else CutStatus.NOT_DECLARED).value,
        'as_of': as_of if declared else None,
    }


def reliability_cuts() -> list[dict[str, Any]]:
    """The reliability cut per broker and where it is enforced (R20).

    Returns:
        One entry per broker (``antares``, ``lasair``, ``pittgoogle``), each with
        ``broker``, ``min_reliability``, ``enforced_by``, ``status``, and
        ``as_of``.
    """
    cuts = [_declared_cut(broker) for broker in _BROKER_ENFORCED]
    cuts.append({
        'broker': _SERVICE_ENFORCED,
        'min_reliability': service_min_reliability(),
        'enforced_by': CutEnforcedBy.SERVICE.value,
        'status': CutStatus.SERVICE_SETTING.value,
        'as_of': None,
    })
    return cuts


def service_provenance() -> dict[str, Any]:
    """Service-level provenance carried by every API response (R17).

    Returns:
        A JSON-native dict with ``service_version``, ``contract_version``,
        ``crossmatch_radius_arcsec``, ``catalogs`` (name and release), and
        ``reliability_cuts`` (per broker).
    """
    return {
        'service_version': service_version(),
        'contract_version': CONTRACT_VERSION,
        'crossmatch_radius_arcsec': crossmatch_radius_arcsec(),
        'catalogs': catalog_releases(),
        'reliability_cuts': reliability_cuts(),
    }
