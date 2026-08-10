"""Tests for the shared footer: NSF acknowledgment (AE4) and deploy version (U1)."""

from django.test import override_settings
from django.urls import reverse

from web.templatetags.web_tags import release_url


NSF_AWARDS = ['OAC-1841625', 'OAC-1934752', 'OAC-2311355', 'AST-2432428']

RELEASE_BASE = 'https://github.com/scimma/crossmatch-service/releases/tag/'


def test_footer_renders_nsf_awards_and_disclaimer(client):
    """Covers AE4: four NSF awards, each an award-search link, plus the disclaimer."""
    body = client.get(reverse('web:home')).content.decode()
    for award in NSF_AWARDS:
        assert award in body
    # Each award id links to its NSF award-search page.
    for award_id in ['1841625', '1934752', '2311355', '2432428']:
        assert f'nsf.gov/awardsearch/showAward?AWD_ID={award_id}' in body
    assert (
        'do not necessarily reflect the views of the National Science Foundation'
        in body
    )


def test_footer_has_contact_issues_and_scimma_links(client):
    """The inline link row includes contact + report-issues and ends with SCiMMA."""
    body = client.get(reverse('web:home')).content.decode()
    assert 'mailto:' in body  # Contact us
    assert 'github.com/scimma/crossmatch-service/issues' in body  # Report issues
    assert 'scimma.org' in body  # participating institutions end in SCiMMA


# --- U1: deploy version in the footer --------------------------------------


def test_release_url_semver_gets_v_prefixed_release_link():
    """Covers AE1 / R2: a semver tag links to its release, with the `v` added."""
    assert release_url('0.10.0') == f'{RELEASE_BASE}v0.10.0'


def test_release_url_non_release_builds_are_empty():
    """Covers AE2 / R3: dev and CI (sha) builds get no link."""
    assert release_url('dev') == ''
    assert release_url('sha-6489fb8') == ''


def test_release_url_zero_sentinel_is_empty():
    """Covers R3 / KTD3: the 0.0.0 unset sentinel does not link (no broken tag)."""
    assert release_url('0.0.0') == ''


def test_release_url_major_minor_is_empty():
    """A non-full-semver like 0.10 is not treated as a release tag."""
    assert release_url('0.10') == ''


@override_settings(APP_VERSION='0.10.0')
def test_footer_links_release_for_semver_version(client):
    """Covers AE1 / R2: the footer version links to releases/tag/v0.10.0."""
    body = client.get(reverse('web:home')).content.decode()
    assert f'{RELEASE_BASE}v0.10.0' in body
    assert '>0.10.0</a>' in body


@override_settings(APP_VERSION='sha-abcdef0')
def test_footer_plain_text_for_non_release_version(client):
    """Covers AE2 / R3: a CI (sha) build shows the version as plain text, no link.

    Uses a distinctive value ('sha-abcdef0') so the positive assertion actually
    proves the plain-text branch rendered it -- a value like 'dev' would false-
    positive on 'device-width' in the base template's viewport meta tag.
    """
    body = client.get(reverse('web:home')).content.decode()
    assert 'sha-abcdef0' in body
    assert f'{RELEASE_BASE}' not in body


@override_settings(APP_VERSION='0.0.0')
def test_footer_plain_text_for_zero_sentinel(client):
    """The unset 0.0.0 default renders plain text, not a releases/tag/v0.0.0 link."""
    body = client.get(reverse('web:home')).content.decode()
    assert '0.0.0' in body
    assert f'{RELEASE_BASE}' not in body


@override_settings(APP_VERSION='1.2.3')
def test_footer_version_reflects_the_setting_live(client):
    """Covers R1: the footer shows whatever APP_VERSION is set to (env-sourced), not a constant."""
    body = client.get(reverse('web:home')).content.decode()
    assert '1.2.3' in body
    assert f'{RELEASE_BASE}v1.2.3' in body
