"""Tests for the web frontend pages (U1, U4-U7).

Covers the shared shell (navbar, footer, no auth UI, responsive collapse,
static serving, branded 404), navigation, the config-driven Catalogs and
Brokers pages (lowercasing, accessible markup, unset states), the Consuming
page's subscribe recipe, and the hand-written API reference.
"""

from unittest import mock

from django.test import override_settings
from django.urls import reverse

import pytest


# --- U1: base shell and static pipeline -------------------------------------


def test_home_returns_200_with_navbar_and_footer(client):
    """GET / renders 200 with the navbar brand and the footer include."""
    resp = client.get(reverse('web:home'))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'SCiMMA Crossmatch' in body  # navbar wordmark
    assert 'National Science Foundation' in body  # footer NSF line (include)


def test_shell_renders_no_auth_control(client):
    """The rendered shell contains no login/logout/auth affordance (R1/R16)."""
    body = client.get(reverse('web:home')).content.decode().lower()
    assert 'login' not in body
    assert 'log in' not in body
    assert 'sign in' not in body
    assert 'logout' not in body


def test_navbar_collapses_behind_toggler(client):
    """The navbar uses Bootstrap's responsive collapse with an aria-expanded toggler."""
    body = client.get(reverse('web:home')).content.decode()
    assert 'navbar-toggler' in body
    assert 'aria-expanded' in body
    assert 'navbar-collapse' in body


def test_static_asset_is_served(client):
    """A static asset under /static/web/... resolves (WHITENOISE_USE_FINDERS)."""
    resp = client.get('/static/web/logo/crossmatch-logo.png')
    assert resp.status_code == 200


def test_unknown_url_renders_branded_404(client):
    """An unknown URL renders the branded 404 in the site shell, not Django's default."""
    resp = client.get('/no-such-page')
    assert resp.status_code == 404
    body = resp.content.decode()
    assert '404' in body
    assert 'SCiMMA Crossmatch' in body  # rendered inside the base shell


# --- U4: navigation ---------------------------------------------------------


@pytest.mark.parametrize('page', ['home', 'catalogs', 'brokers', 'consuming', 'api'])
def test_every_page_links_to_all_five_pages(client, page):
    """Every page's navbar links to all five topic pages (R5)."""
    body = client.get(reverse(f'web:{page}')).content.decode()
    for target in ['home', 'catalogs', 'brokers', 'consuming', 'api']:
        assert f'href="{reverse(f"web:{target}")}"' in body


def test_home_cards_link_to_topic_pages(client):
    """Home renders entry-point cards resolving to each topic-page URL (R6)."""
    body = client.get(reverse('web:home')).content.decode()
    for target in ['catalogs', 'brokers', 'consuming', 'api']:
        assert f'href="{reverse(f"web:{target}")}"' in body


# --- U5: catalogs page ------------------------------------------------------


@override_settings(
    CROSSMATCH_CATALOGS=[
        {
            'name': 'des_y6_gold',
            'source_id_column': 'COADD_OBJECT_ID',
            'ra_column': 'RA',
            'dec_column': 'DEC',
            'payload_columns': ['WAVG_MAG_PSF_G', 'WAVG_MAG_PSF_R'],
        }
    ],
    CROSSMATCH_RADIUS_ARCSEC=1.0,
)
def test_catalog_columns_render_lowercased(client):
    """Each catalog renders its published columns lowercased, not upstream case (AE2)."""
    body = client.get(reverse('web:catalogs')).content.decode()
    assert 'wavg_mag_psf_g' in body
    assert 'wavg_mag_psf_r' in body
    assert 'WAVG_MAG_PSF_G' not in body  # no upstream-native case leaked


@override_settings(
    CROSSMATCH_CATALOGS=[
        {
            'name': 'gaia_dr3',
            'source_id_column': 'source_id',
            'ra_column': 'ra',
            'dec_column': 'dec',
            'payload_columns': ['phot_g_mean_mag'],
        }
    ]
)
def test_catalog_table_is_accessible(client):
    """Each catalog table has scoped header cells and a caption (R7 a11y)."""
    body = client.get(reverse('web:catalogs')).content.decode()
    assert '<caption' in body
    assert 'scope="col"' in body
    assert 'scope="row"' in body


@override_settings(CROSSMATCH_RADIUS_ARCSEC=2.5)
def test_catalogs_page_shows_radius_and_version(client):
    """The page shows the crossmatch radius and the installed LSDB version."""
    body = client.get(reverse('web:catalogs')).content.decode()
    assert '2.5' in body
    # LSDB is installed in the test image; assert the seam's version reaches the
    # page unconditionally (no `if version:` guard -- that would silently no-op
    # the AE3 assertion if the version were ever falsy).
    from web import config

    version = config.lsdb_version()
    assert version
    assert str(version) in body


@override_settings(CROSSMATCH_CATALOGS=[])
def test_empty_catalogs_shows_explicit_state(client):
    """With no catalogs configured the page shows an explicit state, not a blank area."""
    body = client.get(reverse('web:catalogs')).content.decode().lower()
    assert 'no catalogs' in body


@override_settings(MIN_DIASOURCE_RELIABILITY=0.0)
def test_zero_reliability_renders_value_not_unset(client):
    """A validly-configured 0.0 threshold renders the value, not 'not configured' (R12).

    0.0 ("admit everything") is falsy, so a plain ``{% if %}`` guard would
    mis-render it as unset; the seam value is present and must display.
    """
    body = client.get(reverse('web:brokers')).content.decode()
    # If 0.0 fell through to the unset state, the value would be absent entirely.
    assert '0.0' in body


def test_lsdb_version_read_failure_degrades_page(client):
    """A non-PackageNotFound metadata error degrades the version, page still renders (AE5).

    ``service_config()`` runs on every page and is unguarded, so an uncaught
    error from the version read would 500 the whole site; it must degrade.
    """
    import importlib.metadata

    with mock.patch.object(
        importlib.metadata, 'version', side_effect=RuntimeError('boom')
    ):
        resp = client.get(reverse('web:catalogs'))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'not available' in body  # the version section degraded
    assert 'National Science Foundation' in body  # rest of the page still rendered


def test_section_unavailable_renders_at_the_page(client):
    """A section read failure shows 'temporarily unavailable' at the page (AE5, consumer level)."""
    from web import config

    unavailable = config.SectionUnavailable(reason='forced')
    with mock.patch.object(config, 'catalogs', return_value=unavailable):
        body = client.get(reverse('web:catalogs')).content.decode()
    assert 'temporarily unavailable' in body.lower()
    assert 'National Science Foundation' in body  # rest of the page still rendered


# --- U6: brokers & consuming pages ------------------------------------------


@override_settings(MIN_DIASOURCE_RELIABILITY=0.6)
def test_brokers_page_lists_brokers_and_threshold(client):
    """The Brokers page lists the three brokers and the reliability threshold (R8)."""
    body = client.get(reverse('web:brokers')).content.decode()
    assert 'ANTARES' in body
    assert 'Lasair' in body
    assert 'Pitt-Google' in body
    assert '0.6' in body


def test_brokers_table_is_accessible(client):
    """The broker/topic table has scoped headers and a caption (R7)."""
    body = client.get(reverse('web:brokers')).content.decode()
    assert '<caption' in body
    assert 'scope="col"' in body
    assert 'scope="row"' in body


@override_settings(ANTARES_TOPIC='')
def test_brokers_unset_topic_shows_not_configured(client):
    """A broker whose topic is unset shows the 'not configured' marker."""
    body = client.get(reverse('web:brokers')).content.decode().lower()
    assert 'not configured' in body


@override_settings(
    HOPSKOTCH_BROKER_URL='kafka://kafka.scimma.org',
    HOPSKOTCH_TOPIC='scimma-crossmatch.rubin-lsdb',
)
def test_consuming_page_shows_live_topic_and_example(client):
    """The Consuming page renders the live broker/topic and a hop-client example (R9)."""
    body = client.get(reverse('web:consuming')).content.decode()
    assert 'scimma-crossmatch.rubin-lsdb' in body
    assert 'hop subscribe' in body
    assert 'kafka://kafka.scimma.org/scimma-crossmatch.rubin-lsdb' in body


@override_settings(HOPSKOTCH_TOPIC='')
def test_consuming_page_unset_topic_state(client):
    """With an unset topic the Consuming page shows 'not configured', no dangling URL (AE1)."""
    body = client.get(reverse('web:consuming')).content.decode().lower()
    assert 'not configured' in body
    assert 'hop subscribe' not in body


# --- U7: API reference page -------------------------------------------------


def test_api_page_documents_params_and_errors(client):
    """The API page documents every query param and a success + error response (R10)."""
    body = client.get(reverse('web:api')).content.decode()
    for param in ['start', 'end', 'time_field', 'detail', 'page_size', 'cursor']:
        assert param in body
    assert 'next_cursor' in body
    assert 'objects' in body
    assert '400' in body
    assert '405' in body
    assert 'method not allowed' in body


def test_api_docs_url_does_not_collide_with_api_prefix():
    """The API-docs page lives at a path outside the api/ JSON prefix."""
    url = reverse('web:api')
    assert url == '/api-docs'
    assert not url.startswith('/api/')
