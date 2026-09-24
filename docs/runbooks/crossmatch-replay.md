# Crossmatch Replay: Operational Runbook

Validate a crossmatch stack change (an lsdb, hats, nested-pandas, pandas, numpy or
dask upgrade) before PROD without live alerts. You replay a fixed sample of
historical PROD alerts on DEV before and after the change, then compare the two
snapshots. The upgrade passes when **every difference in the report is listed and
explained in the upgrade pull request**. Plan of record:
`docs/plans/2026-09-24-0552-feat-crossmatch-replay-tool-plan.md`.

## What the app ships

| Command | Where it runs | What it does |
|---|---|---|
| `replay_export_sample` | PROD (read-only) | Selects a coverage sample of historical alerts and writes a JSON file |
| `replay_run` | DEV only | Replays the sample through the same crossmatch compute step production uses, on the Dask cluster, and writes a snapshot |
| `replay_compare` | Anywhere | Compares a baseline and a candidate snapshot and writes a Markdown report |

`replay_run` writes nothing to the database and publishes nothing: it calls the
crossmatch compute step without the persistence callbacks `crossmatch_batch` uses,
and production metrics are not incremented.

## Rules

- **Never run `replay_run` on PROD.** It would put load on the PROD Dask cluster. It
  refuses unless `CROSSMATCH_REPLAY_ENABLED=true`, which nothing sets on PROD.
- **Take the baseline before the DEV rollout** of the change under test, and the
  candidate after it. Both run on DEV against the same sample file.
- **Write every file to a path, then `kubectl cp` it out.** The app's structured logs
  go to stdout, so never pipe a command's output into a file.

## 1. Export the sample on PROD

```bash
kubectl -n crossmatch-service exec celery-worker-0 -- \
  python manage.py replay_export_sample --output /tmp/replay-sample.json --seed <upgrade-name>
kubectl -n crossmatch-service cp celery-worker-0:/tmp/replay-sample.json ./replay-sample.json
```

The export runs in a Postgres `READ ONLY` transaction. It takes up to
`--per-category` alerts (default 250) from each category:

- matched in each configured catalog;
- terminal alerts with no match;
- alerts outside the DES footprint (dec above +10);
- matches whose stored catalog values contain nulls.

Alerts in several categories appear once. The same `--seed` over the same data
reproduces the sample. The command lists any category it could not fill; the
replay can still run, but that path is not exercised.

Keep the sample file with the upgrade's notes, so later upgrades can reuse it.

## 2. Baseline replay on DEV (before the rollout)

Switch `kubectl` to the DEV cluster, then:

```bash
kubectl -n crossmatch-service cp ./replay-sample.json celery-worker-0:/tmp/replay-sample.json
kubectl -n crossmatch-service exec celery-worker-0 -- env CROSSMATCH_REPLAY_ENABLED=true \
  python manage.py replay_run --sample /tmp/replay-sample.json \
  --output /tmp/replay-baseline.json --image-tag <current DEV image tag>
kubectl -n crossmatch-service cp celery-worker-0:/tmp/replay-baseline.json ./replay-baseline.json
```

`replay_run` first runs the same client/cluster version-alignment check the Celery
worker runs at startup. It refuses if the scheduler address is unset, if any
compared package drifts (including lsdb and hats), or if the image tag is unknown.
The celery-worker pod has no `APP_VERSION` today, so pass `--image-tag` with the tag
the DEV overlay pins.

A replay of about 2,000 alerts takes a few minutes, like a production batch.

## 3. Roll the change out to DEV, then take the candidate

Roll the upgrade to DEV the usual way. For a cluster-aligned upgrade, roll the Dask
cluster first (`docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`).
**Wait until the celery-worker pod is running and its startup check logged `Dask
cluster verified`**. A replay started mid-rollout refuses on version drift. Then
repeat step 2 with `--output /tmp/replay-candidate.json` and the new image tag.

## 4. Compare and explain

The rollout restarted the pod, so `/tmp` no longer holds the baseline; copy both
snapshots in from your local copies:

```bash
kubectl -n crossmatch-service cp ./replay-baseline.json celery-worker-0:/tmp/replay-baseline.json
kubectl -n crossmatch-service cp ./replay-candidate.json celery-worker-0:/tmp/replay-candidate.json
kubectl -n crossmatch-service exec celery-worker-0 -- python manage.py replay_compare \
  /tmp/replay-baseline.json /tmp/replay-candidate.json --output /tmp/replay-report.md
kubectl -n crossmatch-service cp celery-worker-0:/tmp/replay-report.md ./replay-report.md
```

The comparison has no database or Dask dependency, so any container built from the
app image works too. The report lists, in order:

1. **Comparability.** Anything that makes the snapshots incomparable: a different
   sample, crossmatch radius or catalog configuration, a catalog skipped after read
   failures (re-run that replay), a TNS snapshot that was not current, or a hosted
   catalog build that changed between runs.
2. **Run context differences.** Every run-context value that differs: package
   versions (these are the change under test), image tag, TNS epoch.
3. **Output differences**, grouped by kind, catalog and field, with counts, the
   largest numeric change, and examples:
   - `match present only in baseline` / `match present only in candidate`
   - `matched source changed`
   - `type or null representation changed` (for example `1` versus `1.0`, `null`
     versus a missing key)
   - `value changed`
   - `TNS block changed`, or `TNS block changed (TNS snapshot drift)` when the TNS
     objects near that alert changed between the runs

Paste the report into the upgrade pull request and explain every group there. The
PR is the record of the judgment; the tool does not decide pass or fail.

## Known gaps

- **TNS on DEV.** TNS enrichment is exercised only when DEV has a current TNS
  snapshot, which needs the TNS bot credentials provisioned on DEV. Until then every
  report flags TNS as not current, and the TNS half of the comparison covers nothing.
- **Gitops follow-ups** (maintainer):
  - set `CROSSMATCH_REPLAY_ENABLED=true` in the DEV overlay only, so the `env`
    prefix above becomes unnecessary;
  - pass `APP_VERSION` to the celery-worker pod, so `--image-tag` becomes optional.

## Local development

The dev compose stack sets `CROSSMATCH_REPLAY_ENABLED=true` on `celery-worker`. To
replay against real catalogs locally, start the local Dask profile and point a
worker container at it:

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yaml --profile dask-scheduler \
  up -d dask-scheduler dask-worker
docker compose --env-file docker/.env -f docker/docker-compose.yaml run --rm --no-deps \
  -e DASK_SCHEDULER_ADDRESS=tcp://dask-scheduler:8786 celery-worker sh -c \
  'python manage.py replay_export_sample --output /tmp/s.json --per-category 5 &&
   python manage.py replay_run --sample /tmp/s.json --output /tmp/a.json --image-tag dev'
```
