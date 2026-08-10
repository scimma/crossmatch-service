# Residual review findings - feat/web-frontend

Recorded 2026-08-10 after the `ce-code-review` pass on the web frontend
(commits 29bfc92, f96551b, 77ab825, 2a8f8b0, 99ed5a6). The confirmed,
in-scope findings were fixed in 2a8f8b0 (simplify pass) and 99ed5a6 (review
fixes). The items below were surfaced but intentionally not changed in this
branch; they are recorded here for the maintainer.

## Pre-existing / out-of-scope

- **P2 - `SECRET_KEY` vs `DJANGO_SECRET_KEY` env-name mismatch (chart-wide).**
  `crossmatch/project/settings.py` reads the signing key from
  `DJANGO_SECRET_KEY`, but the Helm chart injects it as `SECRET_KEY` (in
  `django.env`, and now consistently in `web.yaml`). The names never match, so
  every workload falls back to the compiled-in `django-dummy-secret`. This
  pre-dates the web frontend and spans all workloads; fixing it changes the
  effective signing key for every pod, so it is a deliberate maintainer
  decision, not a web-frontend change. No impact on the web pod today: it
  renders no auth, no meaningful signed sessions or cookies. Fix direction:
  rename the injected var to `DJANGO_SECRET_KEY` (chart-wide) or change the
  settings read to `SECRET_KEY`, then confirm the running pods no longer use the
  dummy key.

- **P3 - duplicate `CommonMiddleware`.** `settings.py` MIDDLEWARE lists
  `django.middleware.common.CommonMiddleware` twice (pre-existing; CommonMiddleware
  is idempotent). Left untouched to avoid churning unrelated pre-existing code.

## Deferred by the plan

- **`APP_VERSION` default `'0.0.0'` is truthy**, so the footer always renders a
  version link to a nonexistent `releases/tag/0.0.0`. The plan defers
  app-version sourcing to build/deploy time (Outstanding Questions). Revisit
  before the version link is meaningful in PROD.

## Low-value / convention-consistent

- **P3 - bootstrap-icons CDN stylesheet lacks SRI `integrity`.** The other CDN
  tags (Bootstrap CSS/JS, jQuery, Popper) carry `integrity` + `crossorigin`; the
  bootstrap-icons stylesheet does not. This matches the Astrodash reference's
  convention. Low impact (public page, no secrets, CSS-only). Add the published
  SRI hash for `bootstrap-icons@1.11.0` if hardening the CDN surface.

- **P3 - production static path is smoke-verified, not unit-tested.** The
  automated suite exercises the dev finders path (`WHITENOISE_USE_FINDERS=true`);
  the production path (`false` + `collectstatic` into `STATIC_ROOT`) was verified
  manually via `docker compose up` but has no automated test. A moved/removed
  static asset would pass CI yet 404 in prod. Consider a collectstatic+serve
  test with `WHITENOISE_USE_FINDERS=false` against a temp `STATIC_ROOT`.

- **Residual template duplication.** The `is_configured` filter (99ed5a6)
  unified the sentinel-check logic across the six config-or-unset sites, but the
  `<span class="config-unset">...</span>` else-markup still repeats per site
  (the rendering varies: `<code>` wrapping, an "arcsec" suffix, different
  messages). Left as-is; a single inclusion tag would obscure that variation.

## Out-of-repo follow-up (gitops)

- The DEV/PROD ingress currently routes only `api/` to this workload. The new
  page paths (`/`, `/catalogs`, `/brokers`, `/consuming`, `/api-docs`,
  `/static/...`) must be added to the ingress in the gitops repo, and the routed
  hostname must appear in the web pod's `DJANGO_ALLOWED_HOSTS`
  (`web.allowed_hosts`, default `crossmatch.scimma.org`; the DEV overlay must
  override to `crossmatch-dev.scimma.org`) or Django answers 400 DisallowedHost.
