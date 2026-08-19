# Project: SCiMMA Rubin Crossmatch Service

## What this is
Processes an alert stream of transient sources from the Vera C. Rubin Observatory: it
crossmatches each alert's coordinates against astronomical source catalogs and publishes
the matches for the public astro community. The core is a Django 6 app (MVC: API server +
web frontend) with asynchronous work run on Celery; the actual crossmatch runs on a remote
Dask cluster via LSDB against HATS catalogs (Gaia DR3, DES Y6 Gold, DELVE DR3 Gold,
SkyMapper DR4). Alerts arrive from multiple brokers (ANTARES, Lasair, Pitt-Google) and
matches are published over Hopskotch (Kafka, via hop-client). Active development.

## Layout
- `crossmatch/` — the Django project (single package; run with `manage.py`)
  - `core/` — models, structured logging, Dask client, k8s helpers
  - `brokers/` — alert ingestion + normalization (antares, lasair, pittgoogle)
  - `matching/` — LSDB crossmatch (`catalog.py` loads/validates HATS catalogs; `payload.py` builds the per-match payload)
  - `tasks/` — Celery tasks (`crossmatch.py` batch crossmatch; `schedule.py` periodic)
  - `notifier/` — publishing matches (`impl_hopskotch.py`, `impl_http.py`)
  - `project/` — Django `settings.py` and Celery config
- `docker/` — `docker-compose.yaml` dev stack + `Dockerfile` (primary way to run locally)
- `kubernetes/` — deployment manifests
- `scripts/` — standalone host-side scripts (e.g. `check_payload.py`, `dump_catalog_columns.py`)
- `docs/` — `developer.md` (how to run); `brainstorms/` (requirements docs); `plans/` (implementation plans); `references/` (per-catalog column references); `solutions/` (documented solutions to past problems — bugs, conventions, dev-experience — organized by category with YAML frontmatter `module`/`tags`/`problem_type`; relevant when implementing or debugging in a documented area)
- `CONCEPTS.md` — shared domain vocabulary (entities, named processes, status concepts); relevant when orienting to the codebase or discussing domain concepts

## Conventions
- Python deps are **version-pinned** in `crossmatch/requirements.base.txt` and must stay aligned with the remote Dask cluster's Python + library versions — see Don't, below.
- Django 6 (`>=6.0,<6.1`); the app is run via `manage.py`, not a bare package.
- Logging via **structlog** (`from core.log import get_logger; logger = get_logger(__name__)`), not `print` and not the stdlib `logging` module directly.
- Type hints and docstrings are used on new functions/tasks (see `tasks/crossmatch.py`, `matching/`) — follow the surrounding style; they are not lint-enforced.
- No linter is configured in-repo. If you run one ad hoc, scope it to files you changed.
- `CHANGELOG.md` (Keep a Changelog format) must be updated whenever a new version is tagged:
  before the tag, rename the accumulated `[Unreleased]` section to the new version and date
  (`## [X.Y.Z] - YYYY-MM-DD`) and start a fresh empty `[Unreleased]` above it. Land ongoing
  changes under `[Unreleased]` as you go so the release step is just a rename.

## Domain notes
- Alert coordinates are RA/Dec in **degrees** (`ra_deg`, `dec_deg`); crossmatch radius and
  match separation are in **arcsec** (`CROSSMATCH_RADIUS_ARCSEC`, `_dist_arcsec`/`dist_arcsec`).
- `diaObjectId` is a **64-bit integer** — carry it as int64 and coerce explicitly (`int(...)`)
  before JSON; never let it round-trip through a float.
- HATS catalog column names are **case-inconsistent across surveys**: Gaia/SkyMapper lowercase,
  DES/DELVE UPPERCASE; SkyMapper coordinates use a J2000 suffix (`raj2000`/`dej2000`). Published
  payload keys are lowercased (J2000 suffix preserved). Authoritative column lists live in
  `docs/references/<catalog>-columns.md`.
- (Fill in if/when they apply: flux/magnitude systems, wavelength/frequency units, time scale.
  The matching path is purely positional today, so none are asserted here.)

## Commands
- Launch local dev stack: `docker compose -f docker/docker-compose.yaml up -d --build`
- Run tests: **pytest / pytest-django inside a container**, against the compose Postgres
  (`django-db`), per `docs/developer.md`. The app image does not ship pytest, so install the
  dev deps first. One-off (does not start consumers/celery/valkey/kafka; reuses a running
  `django-db`; pytest-django uses its own `test_` database, so dev data is untouched):
  `docker compose --env-file docker/.env -f docker/docker-compose.yaml run --rm --no-deps celery-worker sh -c 'pip install -q -r requirements.dev.txt && python -m pytest'`.
  When a worker container is already up: `docker exec crossmatch-celery-worker-1 sh -c 'cd /opt/crossmatch && python -m pytest'`.
  Config is `crossmatch/pytest.ini` (`testpaths = tests brokers`); subset with a path or `-k`.
- Query the dev database: it's the `django-db` compose service, not published to a host port —
  reach it with `docker compose -f docker/docker-compose.yaml exec django-db psql -U crossmatch_service_admin -d scimma_crossmatch_service`.
  See `docs/solutions/developer-experience/query-dev-database-via-docker-exec.md`.
- Verify the payload helper (no app/Django needed): `python scripts/check_payload.py`
  (in a venv with numpy + pandas).

## Gotchas
- **numpy/pandas scalars are not JSON-native.** Catalog values come off the crossmatch
  DataFrame as numpy scalars (int16/32/64, float32/64, `np.bool_`) and pandas null sentinels
  (None/NaN/NaT/`pd.NA`); a Django `JSONField` and `json.dumps` reject them, and `nan` emits an
  invalid `NaN` token. Coerce at the boundary via `matching/payload.py`. Full rationale:
  `docs/solutions/design-patterns/coerce-numpy-pandas-scalars-to-json.md`.
- **`lsdb.open_catalog(url)` loads only the catalog's DEFAULT columns.** Use
  `columns="all"` to introspect the full schema, and pass an explicit `columns=[...]` to load a
  subset. `matching/catalog.py` validates requested columns up front so a bad name fails loudly.
- **`crossmatch(suffix_method='overlapping_columns')` silently suffixes colliding columns.** A
  requested catalog column that shares a name with an alert column (`uuid`,
  `lsst_diaObject_diaObjectId`, `ra_deg`, `dec_deg`) gets `_catalog`-appended and breaks payload
  mapping; `matching/catalog.py` guards this with `_ALERT_COLUMNS`.
- **Build match rows per-row defensively** in `tasks/crossmatch.py` — wrap each row, not the whole
  loop, so one bad row doesn't discard a whole catalog's matches (the batch transitions to MATCHED
  unconditionally afterward, making that loss permanent).
- **No spatial overlap is normal, not an error.** A catalog whose footprint misses the batch
  raises "Catalogs do not overlap"; the loop logs and continues (e.g. DES Y6 Gold yields no
  matches outside its southern footprint).

## Git workflow
- **Fork-based repo.** `origin` is the maintainer's fork
  (`github.com/skoranda/crossmatch-service`); `upstream` is canonical
  (`github.com/scimma/crossmatch-service`) and is what pull requests target. Confirm with
  `git remote -v`.
- **Never commit to `main`.** Create a branch first and commit there, so the work can be
  pushed to `origin` and opened as a pull request against `upstream`. If a commit lands on
  `main` by mistake, move it onto a branch: `git branch <name> && git reset --hard HEAD~1`.
- **Pushing feature branches to `origin` is allowed**, and so is **opening a pull request on
  `origin` from the pushed branch** (base `origin/main`). This project rule overrides the
  global "never push to remote" default, but only for `origin`
  (`git@github.com:skoranda/crossmatch-service.git`) — push the branch, then `gh pr create`
  against `origin`.
- **Still off-limits for Claude:** pushing to `upstream`, pushing to (or force-pushing) `main`
  on any remote, the PR against `upstream`, and every merge — those stay with the maintainer.

## Don't
- Add or upgrade a dependency without re-pinning every pin site and aligning the Dask cluster's
  Python/library versions — version skew silently breaks distributed (de)serialization. See
  `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` and the fail-fast
  Dask version check in `core/dask.py`.
- Reformat or churn files you weren't asked to change.
- Push to `upstream`, push/force-push `main`, open a PR against `upstream`, or merge anything —
  those stay with the maintainer. (Pushing feature branches and opening PRs on `origin` is fine;
  see Git workflow.)
