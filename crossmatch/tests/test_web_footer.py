"""Tests for the shared footer (U2): NSF acknowledgment and links (AE4)."""

from django.urls import reverse


NSF_AWARDS = ['OAC-1841625', 'OAC-1934752', 'OAC-2311355', 'AST-2432428']


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
