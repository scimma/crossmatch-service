"""Template tags for the web frontend footer.

Only footer *content* that is not a live-config seam field lives here. The app
version is deliberately NOT a template tag: it is read through the single
live-config seam and passed in per-view context (KTD2), keeping every
``settings`` read behind that one trust boundary. This mirrors Astrodash's
``astrodash_tags.py``.
"""

from django import template
from django.conf import settings

register = template.Library()


@register.simple_tag(name='support_email')
def support_email() -> str:
    """Return the footer "Contact us" mailto address.

    Returns:
        ``settings.SUPPORT_EMAIL`` (overridable per deployment).
    """
    return settings.SUPPORT_EMAIL
