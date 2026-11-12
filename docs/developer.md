# Developer documentation

## Requirements

- Docker with Compose plugin "docker compose"

## Dependencies

`crossmatch/requirements.base.txt` is the human-edited source of truth — add,
remove, or re-pin top-level dependencies there. `crossmatch/requirements.lock`
is the fully-pinned transitive lock compiled from it with `pip-compile`
(pip-tools). The image build installs from the lock (`docker/Dockerfile`), so
the lock — not `requirements.base.txt` — is what determines the versions that
actually ship. This is what keeps the `hats==0.9.0` pin (and the rest of the
tree) reproducible across rebuilds.

After editing `requirements.base.txt`, regenerate the lock:

```bash
cd crossmatch
pip install pip-tools
pip-compile --strip-extras --output-file=requirements.lock requirements.base.txt
```

Compile under Python 3.12 to match the image. The `lock-drift` GitHub Actions
workflow recompiles on every change to either file and fails the build if the
committed lock does not match — i.e. if `requirements.base.txt` was edited but
the lock was not regenerated.

## Launch local deployment

If you wish to override any of the environment variables, create a `.env` file in the `/docker` directory:

```bash
echo 'DEV_MODE = true' >> docker/.env
```

Launch the local deployment by navigating to the root directory of your repo clone and executing:

```bash
docker compose -f docker/docker-compose.yaml up -d --build
```

## Testing

## Virtual environment

You may want to use a Python virtual environment when developing code or when testing. In general it is possible to `docker exec` into a running container where the environment is already configured, but sometimes it is convenient to run a script on your host machine. In these cases, you can create a virtual environment and install the requirements locally.

```bash
# Create the venv once
$ python -m venv .venv
# Thereafter, source the activate script prior to running Python scripts that require the project dependencies
$ source .venv/bin/activate
(.venv) 
# Install/update all dependencies 
$ pip install -r crossmatch/requirements.base.txt 
```

## Unit tests

Tests run with `pytest` (via `pytest-django`) in-container, against the compose
`django-db` Postgres service. Bring up the stack, then run the suite:

```bash
docker compose -f docker/docker-compose.yaml up -d
# test deps come from crossmatch/requirements.dev.txt (pytest, pytest-django, factory_boy)
docker exec -it crossmatch-celery-worker-1 sh -c 'cd /opt/crossmatch && python -m pytest'
```

Config lives in `crossmatch/pytest.ini` (`DJANGO_SETTINGS_MODULE`, test paths).
Run a subset with a path or `-k`, e.g. `python -m pytest tests/test_dispatch_notifications.py`.
Tests that depend on commit semantics (the dispatch/ordering tests) use
`@pytest.mark.django_db(transaction=True)` and require Postgres — they will not
behave on SQLite.

## Crossmatch replay (dependency-upgrade validation)

Unit tests mock LSDB, so they cannot show whether a dependency upgrade changes
what the service publishes. The replay commands re-run a sample of historical
alerts through the real crossmatch compute step and diff the results:
`replay_export_sample` (PROD, read-only), `replay_run` (DEV only), and
`replay_compare`. The code lives in `crossmatch/replay/`. The operator flow,
including the local Dask profile for trying it against real catalogs, is in
`docs/runbooks/crossmatch-replay.md`.

## Adding or measuring a catalog

A catalog is one `CROSSMATCH_CATALOGS` entry in `crossmatch/project/settings.py`,
plus its `*_HATS_URL` (and `*_RELEASE`) in compose, the env example, the in-repo
chart, `kubernetes/dev-overrides.yaml.example` and `deploy-contract.yaml`. Generate
its column reference with `scripts/dump_catalog_columns.py` and pick
`payload_columns` from it (see
`docs/solutions/conventions/catalog-specific-payload-columns.md`). Set
`match_mode: 'all'` with `max_matches` only for a catalog that should keep every
source within the radius.

Rolling a new catalog out (the Euclid catalogs are the worked example):

1. **Gitops, DEV.** Add the catalog's `*_hats_url` and `*_release` values to
   `apps/crossmatch-service` and deliver them with the other catalogs through the
   `provenance.env` helper. Only then bump `CONTRACT_REF` past the app commit that
   added them to `deploy-contract.yaml`: the contract check fails on contract
   variables the render does not deliver.
2. **DEV.** Deploy the image. The first batch records a new provenance set. Watch
   the worker logs for `crossmatch_match_cap_reached` and for
   `Catalog coverage map built from its skymap`.
3. **Measure one Rubin night** whose batches include alerts in the catalog's
   footprint: `crossmatch_batch` runtime against the 480 s soft and 600 s hard
   limits (a full 100k batch has measured 3.0-4.3 min), peak Dask worker memory
   against its limit, and matches actually landing in the footprint.
4. **PROD.** Promote with the gitops `docs/prod-promotion-runbook.md`.
5. **Euclid Wide.** Today's Euclid-overlapping alerts sit in the Euclid Deep
   Fields, so the DEV night reads few of Wide's large (about 1 GiB) partitions.
   Repeat step 3 the first time PROD batches contain alerts in Wide-only coverage.
   If Wide threatens the time limits, set `EUCLID_DR1_MER_WIDE_ENABLED=false` in
   the PROD gitops values while the cause is investigated.
6. The App dashboard's "matches / alert" panel counts matches, not objects, so it
   rises once a catalog keeps several sources per alert.

## Continuous integration

The `Tests` GitHub Actions workflow (`.github/workflows/test.yml`) runs the
suite on every pull request and on pushes to `main`. It builds the application
image (so the suite runs against the same pinned dependency tree that ships) and
runs `pytest` inside it against a Postgres service. Make it a required status
check in branch protection so a failing test blocks merge (a repo-settings step,
not in this repo).
