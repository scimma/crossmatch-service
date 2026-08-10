"""Template tags for the web frontend footer.

Only footer *content* that is not a live-config seam field lives here. The app
version is deliberately NOT a template tag: it is read through the single
live-config seam and passed in per-view context (KTD2), keeping every
``settings`` read behind that one trust boundary. This mirrors Astrodash's
``astrodash_tags.py``.
"""

from typing import Any

from django import template
from django.conf import settings

from web import config

register = template.Library()


@register.simple_tag(name='support_email')
def support_email() -> str:
    """Return the footer "Contact us" mailto address.

    Returns:
        ``settings.SUPPORT_EMAIL`` (overridable per deployment).
    """
    return settings.SUPPORT_EMAIL


@register.filter(name='is_configured')
def is_configured(value: Any) -> bool:
    """Whether a seam value is a real configured value (vs. an unset marker).

    Templates must not branch on plain truthiness for seam fields: a validly
    configured ``0`` / ``0.0`` (e.g. ``MIN_DIASOURCE_RELIABILITY = 0.0``, "admit
    everything") is falsy and would render as "not configured", a wrong fact on
    a public page (R12). This distinguishes the seam's ``NOT_CONFIGURED`` /
    ``SectionUnavailable`` sentinels from a falsy-but-set value.

    Args:
        value: A scalar field from the live-config seam.

    Returns:
        ``False`` only when ``value`` is the unset/unavailable sentinel.
    """
    return value is not config.NOT_CONFIGURED and not isinstance(
        value, config.SectionUnavailable
    )
