---
title: Wire the deployed image tag into a server-rendered footer version
date: 2026-08-10
category: design-patterns
module: crossmatch/web
problem_type: design_pattern
component: rails_view
severity: medium
applies_when:
  - surfacing the deployed build/image version in a server-rendered page
  - linking a version string to a GitHub (or other) release page
  - image tags and git release tags differ by a leading 'v' prefix
  - wiring one config value end to end through settings, container env, and Helm/gitops
  - a page must degrade to plain text for dev/sha/sentinel (non-release) builds
tags: [app-version, deploy-version, django-template, template-filter, semver, release-link, helm, docker-compose]
---

# Wire the deployed image tag into a server-rendered footer version

## Context

The public web frontend's footer carried a version string that was, in practice,
a lie. The value flowed from `APP_VERSION` in Django settings, which fell back to
a hardcoded `0.0.0` when nothing set it — and nothing did. Every deployment, on
DEV and PROD alike, rendered `0.0.0` in the footer regardless of which image was
actually running.

What we wanted was modest but genuinely useful on a public page: show the version
of the build that is actually serving the request, and — when that version
corresponds to a real, shipped GitHub release — make it a link straight to the
release notes. The friction was two-fold. First, there was no live wiring from
the deployed artifact (the container image tag) down to the rendered footer.
Second, the "make it a link" half hides a trap: the deployed image tags and the
GitHub release tags are spelled differently, so a naive link template produces a
404 for exactly the releases you most want to link.

## Guidance

The fix is a single source of truth — the deployed image tag — threaded through
four seams: the Helm chart injects it as an env var, Django settings read that
env var, a template filter decides whether it earns a link, and the footer
template renders one branch or the other.

**1. The version comes from the environment, with a sentinel fallback.**
`crossmatch/project/settings.py:10`:

```python
APP_VERSION = os.getenv('APP_VERSION', '0.0.0')
```

`0.0.0` is deliberately a value that will *never* be treated as a real release
(see the filter below), so an unset environment degrades to honest plain text
rather than a fabricated link.

**2. The Helm chart injects the running image tag.** In the `web.env` block,
`kubernetes/charts/crossmatch-service/templates/_helpers.yaml:163-164`:

```yaml
- name: APP_VERSION
  value: {{ .Values.common.image.tag | quote }}
```

The tag the pod runs *is* the version the footer reports. There is no separate
version file, constant, or build-time string to keep in sync — drift is
structurally impossible because both the running code and the footer derive from
the same `common.image.tag`. (The deployed gitops chart lives in a separate repo
and mirrors this same injection; the in-repo chart above is the reference copy.)

**3. Local compose defaults to a non-release value.**
`docker/docker-compose.yaml`:

```yaml
APP_VERSION: "${APP_VERSION:-dev}"
```

`dev` is intentionally not a semver, so local runs show `dev` as plain text and
never masquerade as a release — while still letting a developer export
`APP_VERSION=0.10.1` to exercise the linked path.

**4. The filter gates the link on a full semver and prepends the `v`.** This is
the load-bearing piece. `crossmatch/web/templatetags/web_tags.py`:

```python
_SEMVER_RE = re.compile(r'^\d+\.\d+\.\d+$')
_RELEASE_TAG_BASE = 'https://github.com/scimma/crossmatch-service/releases/tag/'

@register.filter(name='release_url')
def release_url(value: Any) -> str:
    text = str(value)
    if text == '0.0.0' or not _SEMVER_RE.match(text):
        return ''
    return f'{_RELEASE_TAG_BASE}v{text}'
```

The image tag is `X.Y.Z` (no leading `v`); the GitHub release tag is `vX.Y.Z`.
The filter reconciles the two spellings by prepending `v` when it builds the URL
— and it does so *only* for a string matching the full-semver regex and not the
`0.0.0` sentinel. Everything else — a local `dev` build, a CI `sha-<sha>` build,
the unset `0.0.0`, or a partial version like `0.10` — returns an empty string.

**5. The footer renders link-or-plain-text on that result.**
`crossmatch/web/templates/web/_footer.html`:

```django
{% if service.app_version %}
<span class="footer-sep">//</span>
{% with rel_url=service.app_version|release_url %}
{% if rel_url %}<a href="{{ rel_url }}">{{ service.app_version }}</a>{% else %}{{ service.app_version }}{% endif %}
{% endwith %}
{% endif %}
```

The displayed text is always the raw version; only whether it is wrapped in an
`<a>` depends on `release_url` being non-empty. (The version reaches the template
as `service.app_version` from per-view context routed through the app's single
live-config seam, not a direct settings read in the template.)

The behavior is pinned by `crossmatch/tests/test_web_footer.py`: the filter unit
tests `test_release_url_semver_gets_v_prefixed_release_link` (semver →
`.../releases/tag/v0.10.0`), `test_release_url_non_release_builds_are_empty`
(`dev`, `sha-6489fb8` → `''`), `test_release_url_zero_sentinel_is_empty`, and
`test_release_url_major_minor_is_empty` (`0.10` → `''`); and the rendered-footer
tests `test_footer_links_release_for_semver_version`,
`test_footer_plain_text_for_non_release_version`,
`test_footer_plain_text_for_zero_sentinel`, and
`test_footer_version_reflects_the_setting_live` (via `@override_settings`,
proving the footer reflects whatever `APP_VERSION` is set to, not a constant).

## Why This Matters

- **Single source of truth, no drift.** The footer version *is* the image tag the
  pod runs (`_helpers.yaml` → `os.getenv` in `settings.py`). There is no
  hand-maintained version constant or `VERSION` file that can fall out of step
  with what is actually deployed. Bump the image tag and the footer follows for
  free.

- **The semver gate prevents shipping a 404.** Because image tags omit the `v`
  that release tags carry, the only correct link is one that prepends `v` — and
  only for values that are genuinely releases. Gating on `_SEMVER_RE` and
  excluding `0.0.0` means a link is emitted *only* when a matching
  `releases/tag/vX.Y.Z` actually exists. A naive `releases/tag/{{ version }}`
  would have linked `0.0.0`, `dev`, and CI builds straight into 404s on a public
  page.

- **Plain-text fallback keeps non-release builds honest.** `dev`, `sha-...`, and
  the `0.0.0` sentinel still *display* — you can see what is running — but they
  are visibly not links, which correctly signals "this is not a shipped release."
  The page never claims more than it can back up.

## When to Apply

- Any server-rendered application that wants to surface (and optionally link) the
  version it is currently running.
- Especially when the identifier you deploy under and the identifier your
  release/VCS lives under differ by a prefix or format — image tag `X.Y.Z` vs.
  git tag `vX.Y.Z` is the canonical case, but the same holds for any
  `deployed-id` → `canonical-url` transform.

The key moves are transferable:

1. Derive the displayed version from the deployment artifact itself (image tag,
   build arg) rather than a separate file, so it cannot drift.
2. Read it through one env var with a sentinel default that is *guaranteed* not to
   validate as a real release.
3. Put the "does this earn a link, and how do I spell the URL" decision in one
   tested pure function, gated on a strict pattern.
4. Have the view/template render link-or-plain-text purely on that function's
   result.

## Examples

- **Footer version, before → after:** the string was effectively the hardcoded
  `0.0.0` fallback (nothing ever set `APP_VERSION`), so every environment showed
  `0.0.0`. After wiring, the value is env-sourced from the deployed image tag
  (`_helpers.yaml` → `settings.py`), so the footer reflects the actual build.

- **Shipped release → linked:** with `APP_VERSION=0.10.1`, `release_url` returns
  `https://github.com/scimma/crossmatch-service/releases/tag/v0.10.1` (note the
  prepended `v`), and the footer renders `<a href=".../v0.10.1">0.10.1</a>`.

- **Non-release builds → plain text, no link:** `dev` (local compose default),
  `sha-abc1234` (CI build), and `0.0.0` (unset sentinel) all yield `''` from
  `release_url`, so the footer prints the string bare with no `<a>` wrapper —
  and `0.10` (partial semver) is treated the same, never producing a
  `releases/tag/v0.10` link that would 404.

## Related

- `docs/residual-review-findings/feat-web-frontend.md` — this learning resolves
  the deferred finding recorded there (the `APP_VERSION` default `0.0.0` was
  truthy, so the footer linked to a nonexistent `releases/tag/0.0.0`; sourcing was
  deferred to build/deploy time). Shipped under git tag `v0.10.1` (GitHub serves
  the `releases/tag/v0.10.1` page for the tag, which is what the footer links to).
- `docs/solutions/conventions/argocd-apps-applied-manually.md` — adjacent
  deploy/GitOps context for the chart `web.env` injection (ArgoCD Applications are
  applied by hand).
- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` —
  weak parallel only: keeping one value consistent across multiple sites
  (compose `web` env + Helm `web.env`), a different domain from dependency pinning.
- No GitHub issue tracks deploy-version display; issue #91 ("Update footer
  disclaimer") is footer-adjacent but concerns disclaimer text, not the version.
