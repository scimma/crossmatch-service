"""Template tags for the web frontend footer.

Only footer *content* that is not a live-config seam field lives here. The app
version is deliberately NOT a template tag: it is read through the single
live-config seam and passed in per-view context (KTD2), keeping every
``settings`` read behind that one trust boundary. This mirrors Astrodash's
``astrodash_tags.py``.
"""

import re
from typing import Any

from django import template
from django.conf import settings

from web import config

register = template.Library()

# A GitHub release for a shipped version lives at releases/tag/vX.Y.Z. The
# deployed image tags omit the leading `v` (0.10.0), while the git release tags
# carry it (v0.10.0), so the `v` is added when building the link.
_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')
_RELEASE_TAG_BASE = 'https://github.com/scimma/crossmatch-service/releases/tag/'


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


@register.filter(name='release_url')
def release_url(value: Any) -> str:
    """GitHub release URL for a semver deploy version, else an empty string.

    A real release tag gets a link; anything else -- a local/``dev`` build, a CI
    ``sha-<sha>`` build, the ``0.0.0`` unset sentinel, or a non-full-semver like
    ``0.10`` -- returns an empty string so the footer renders the version as
    plain text with no broken link (R2/R3, KTD2/KTD3). The leading ``v`` that the
    git release tags carry (but the image tags omit) is prepended here.

    Args:
        value: The deploy version string (or any seam value; coerced defensively).

    Returns:
        ``https://github.com/scimma/crossmatch-service/releases/tag/v<version>``
        for a semver that is not the ``0.0.0`` sentinel, else ``''``.
    """
    text = str(value)
    if text == '0.0.0' or not _SEMVER_RE.match(text):
        return ''
    return f'{_RELEASE_TAG_BASE}v{text}'
