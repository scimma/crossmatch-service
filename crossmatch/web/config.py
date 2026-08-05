"""Live configuration seam for the informational web frontend.

Every web page reads the deployed service's facts through this one module. It
maps an explicit allowlist of display fields from ``django.conf.settings`` (plus
the installed LSDB version, read from package metadata) into plain context
values -- it never returns or iterates the settings module, so a secret setting
(``SECRET_KEY``, the database password, the broker/Hopskotch credentials) can
never reach a template even if a page is edited carelessly. This is the single
trust boundary behind R11/KTD2; it matters because the pages are publicly
viewable with no authentication (R14).

Settings are read at call time (mirroring ``api/service.py``) so tests can use
``@override_settings``. Unset values become ``NOT_CONFIGURED`` (R12); a section
whose read fails becomes ``SectionUnavailable`` (AE5) rather than raising.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from core.log import get_logger

logger = get_logger(__name__)


class _NotConfigured:
    """Sentinel for a value unset on this deployment (R12).

    Falsy so templates can branch with ``{% if value %}`` and render a
    "not configured" state instead of a blank or malformed value.
    """

    _instance: '_NotConfigured | None' = None

    def __new__(cls) -> '_NotConfigured':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return 'NOT_CONFIGURED'


NOT_CONFIGURED = _NotConfigured()


@dataclass(frozen=True)
class SectionUnavailable:
    """A config section whose live read failed at render time (AE5).

    Templates render a graceful "temporarily unavailable" message for the
    affected section while the rest of the page still renders. Falsy for the
    same reason as ``NOT_CONFIGURED``.
    """

    reason: str

    def __bool__(self) -> bool:
        return False


def _present(value: Any) -> Any:
    """Return ``value`` unless it is unset (``None``/empty), else NOT_CONFIGURED."""
    if value is None:
        return NOT_CONFIGURED
    if isinstance(value, str) and value.strip() == '':
        return NOT_CONFIGURED
    return value


def lsdb_version() -> Any:
    """Installed LSDB version from package metadata, or NOT_CONFIGURED.

    This is the client-side pin installed on *this* deployment; the crossmatch
    itself runs on the remote Dask cluster (see R7 / KTD deployment notes).
    """
    try:
        return importlib.metadata.version('lsdb')
    except importlib.metadata.PackageNotFoundError:
        return NOT_CONFIGURED


def catalogs() -> Any:
    """Crossmatched catalogs with their published (lowercased) columns.

    Published column names are lowercased to match the keys actually published
    over Hopskotch (``col.lower()`` in ``matching/payload.py``), not the
    upstream-native case stored in settings (R7/AE2/KTD6). Returns an empty list
    when no catalogs are configured, or ``SectionUnavailable`` on a read error.
    """
    try:
        raw = getattr(settings, 'CROSSMATCH_CATALOGS', []) or []
        return [
            {
                'name': cat.get('name'),
                'source_id_column': cat.get('source_id_column'),
                'ra_column': cat.get('ra_column'),
                'dec_column': cat.get('dec_column'),
                'published_columns': [
                    str(c).lower() for c in cat.get('payload_columns', [])
                ],
            }
            for cat in raw
        ]
    except Exception as exc:  # noqa: BLE001 -- degrade the section, never 500
        logger.warning('web_config_catalogs_unavailable', error=str(exc))
        return SectionUnavailable(reason='catalog configuration could not be read')


def brokers() -> Any:
    """Upstream brokers and their configured topics (unset -> NOT_CONFIGURED)."""
    try:
        return [
            {
                'name': 'ANTARES',
                'topic': _present(getattr(settings, 'ANTARES_TOPIC', None)),
            },
            {
                'name': 'Lasair',
                'topic': _present(getattr(settings, 'LASAIR_TOPIC', None)),
            },
            {
                'name': 'Pitt-Google',
                'topic': _present(getattr(settings, 'PITTGOOGLE_TOPIC', None)),
            },
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning('web_config_brokers_unavailable', error=str(exc))
        return SectionUnavailable(reason='broker configuration could not be read')


def service_config() -> dict[str, Any]:
    """The scalar display facts, each read from a single named setting.

    Only these named fields are exposed; the settings module is never passed to
    a template, so secrets in the same module cannot leak (R11).
    """
    return {
        'crossmatch_radius_arcsec': _present(
            getattr(settings, 'CROSSMATCH_RADIUS_ARCSEC', None)
        ),
        'min_diasource_reliability': _present(
            getattr(settings, 'MIN_DIASOURCE_RELIABILITY', None)
        ),
        'hopskotch_broker_url': _present(
            getattr(settings, 'HOPSKOTCH_BROKER_URL', None)
        ),
        'hopskotch_topic': _present(getattr(settings, 'HOPSKOTCH_TOPIC', None)),
        'app_version': _present(getattr(settings, 'APP_VERSION', None)),
        'lsdb_version': lsdb_version(),
    }
