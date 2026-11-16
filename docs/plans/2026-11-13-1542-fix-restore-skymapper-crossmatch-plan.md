---
title: Restore SkyMapper DR4 Crossmatching - Plan
type: fix
date: 2026-11-13
topic: restore-skymapper-crossmatch
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# Restore SkyMapper DR4 Crossmatching - Plan

## Goal Capsule

- Objective: New Rubin alerts on DEV and PROD are crossmatched against SkyMapper DR4 again and published with SkyMapper matches identical in identity and values to those published before the outage.
- Product authority: This plan, from scimma/crossmatch-service#135. Active scope is restoring SkyMapper going forward. Detecting silently dead catalogs and backfilling the gap are not active scope.
- Open blockers: None.
- Means: a one-value gitops change to the SkyMapper URL, landed on DEV through main and on PROD through a tag cut from PROD's current tag (KTD1, KTD2). The app's own defaults follow in one PR (KTD3).
- Execution profile: the pipeline implements U1 and opens one app PR on scimma upstream from the `bot` fork. It prepares the U2 and U3 gitops branches locally and never pushes them. U4's cluster steps (gitops pushes, PROD tag, the replay runs, `kubectl apply`) are the maintainer's, run from exact commands. The agent does read-only cluster checks and the U4 comparison.
- Stop conditions: stop and report if the DEV replay shows SkyMapper skipped, or shows any SkyMapper match differing from what PROD published before the outage, other than an edge difference KTD4 attributes to the new margin. Also stop if the PROD promotion diff touches anything other than the SkyMapper URL.
- Who finishes: the maintainer merges the app PR and both gitops MRs, pushes the PROD tag, and applies the four PROD Applications. The agent records the post-night result on #135.
- Product Contract preservation: unchanged in meaning. R6's list of places to change is extended in U1 to every place the old URL appears in the app repo, which planning found (KTD3).

---

## Product Contract

### Summary

Point SkyMapper DR4 at its new data.lsdb.io collection, `https://data.lsdb.io/hats/skymapper_dr4`, on DEV first and then PROD. This is a gitops value change, so no new image is needed, and PROD gets it in a promotion that carries only this change. A small separate app PR moves the app's own default and docs to the same URL.

### Problem Frame

On 2026-10-27, data.lsdb.io rebuilt SkyMapper DR4 as a HATS collection, and the catalog URL the service is configured with, `https://data.lsdb.io/hats/skymapper_dr4/catalog`, now returns 404. The crossmatch treats a failed catalog read as transient, so every batch since then has logged a warning, skipped SkyMapper and completed with the other catalogs.

On PROD, 103,421 of 192,192 objects (about 54%) were crossmatched between 2026-10-29 13:20 UTC and 2026-11-13 with SkyMapper skipped. The last SkyMapper match was published at 2026-10-30 07:43 UTC. Each affected object's crossmatch record marks SkyMapper `skipped_read_failure`, and its published payload is flagged partial with `catalogs_skipped: [skymapper_dr4]`. API users can therefore see the gap, but Hopskotch subscribers simply received fewer matches. No dashboard or alert surfaced it.

Today the URL reaches DEV and PROD through gitops (`crossmatch.skymapper_hats_url` in `apps/crossmatch-service/values.yaml`). The app default in `crossmatch/project/settings.py` is used only by local development and by images run without that value.

### Key Decisions

- **Restore going forward only; no backfill.** The 103,421 affected objects stay marked partial, which the API already reports. (session-settled: user-approved - chosen over backfilling the matches into the database and API, and over backfilling and publishing them to Hopskotch: smallest change, ships through gitops alone, and a backfill can follow on request.) Governs R1, R6.
- **PROD gets the fix now, in a promotion carrying only this change.** It does not wait for the 0.19.0 promotion. (session-settled: user-approved - chosen over riding with the 0.19.0 promotion: PROD regains SkyMapper as soon as possible, with no image change.) Governs R3, R4.
- **The release label stays "SkyMapper DR4", and no new provenance set starts.** On 2026-11-13, 60 recent PROD SkyMapper matches were recomputed against the new collection with the app's own loader. All 60 returned the same `object_id`, identical payload values and the same separation. Provenance hashes the catalog name and release, not the URL. Governs R2.
- **This is restoration only.** Detecting a silently dead catalog is its own issue and plan, and #134 (the LSDB 0.11.2 upgrade) stays a version bump only. (session-settled: user-approved - chosen over detection only, and over restoration plus detection in one plan: fix the outage now.) Governs R7.

### Actors

- A1. Maintainer: merges the gitops MR and the app PR, pushes gitops, and runs the PROD promotion.
- A2. Implementing agent: prepares the gitops branch and the app PR, and verifies on the clusters.

### Key Flows

- F1. Restore SkyMapper
  - **Trigger:** The plan is picked up for implementation.
  - **Actors:** A1, A2
  - **Steps:**
    1. Change the SkyMapper URL in gitops and roll it to DEV.
    2. Verify on DEV.
    3. Promote only this change to PROD and verify.
    4. Land the app default and docs change.
  - **Covered by:** R1-R7

### Requirements

**Cluster fix**

- R1. DEV and PROD read SkyMapper DR4 from `https://data.lsdb.io/hats/skymapper_dr4`, delivered by the gitops `crossmatch.skymapper_hats_url` value, with no app image change.
- R2. The SkyMapper release label and every published SkyMapper match field (`object_id` as `catalog_source_id`, payload columns, separation) are unchanged from before the outage.
- R3. DEV changes first. PROD changes only after DEV shows SkyMapper read through the real Dask compute path and succeeding, not skipped. Either a live DEV crossmatch batch or the DEV replay in R5 counts.
- R4. The PROD promotion changes only the SkyMapper URL. No other gitops change made since PROD's current tag reaches PROD with it.

**Verification**

- R5. On each cluster, after the change, a crossmatch batch completes with SkyMapper among the succeeded catalogs, and SkyMapper `CatalogMatch` rows are created again. When no alerts flow on DEV, the check is a DEV replay of a PROD sample of alerts from before 2026-10-29 (`replay_export_sample`, then `replay_run`), run through the DEV Dask cluster. DEV's Dask cluster runs the same lsdb 0.11.0 as PROD. The replay writes nothing to the database or Hopskotch, and it passes when all three hold:
  - SkyMapper is not skipped;
  - every sampled alert that had a SkyMapper match gets the same `object_id` and payload;
  - every sampled alert that had no SkyMapper match still has none.

  Opening the catalog and building its coverage map in the worker does not count on its own, because it reads only metadata.

**App and docs**

- R6. The app default for `SKYMAPPER_HATS_URL` in `crossmatch/project/settings.py`, the default in `scripts/dump_catalog_columns.py`, the URL in `docs/references/skymapper_dr4-columns.md` and the design doc's SkyMapper URL all name the new collection. This lands in its own app PR with a CHANGELOG entry under `[Unreleased]`.
- R7. The open LSDB upgrade PR, scimma/crossmatch-service#134, is not modified. Issue #135 is closed by the app PR, and the cluster fix is recorded on the issue.

### Acceptance Examples

- AE1. **Covers R3, R5.** Given DEV has the new URL and no Rubin alerts are flowing, when the agent checks DEV, then a DEV replay of a pre-outage PROD sample shows SkyMapper succeeded with matches identical to those published before the outage. The PROD promotion may proceed on that evidence, and the first live DEV batch is confirmed afterwards. A worker that only opens the catalog and builds its coverage map is not enough.
- AE2. **Covers R4.** Given gitops main also carries Babamul and Euclid values added after PROD's current tag, when the PROD promotion is prepared, then the change PROD receives touches only the SkyMapper URL.
- AE3. **Covers R2.** Given a SkyMapper match is published after the fix, when compared with a pre-outage match for the same source, then `catalog_source_id` and every payload field have the same name and meaning.

### Success Criteria

- PROD publishes SkyMapper matches for new alerts within the first Rubin night after promotion.
- Batches on DEV and PROD no longer log `catalogs_skipped=['skymapper_dr4']`.
- After the first Rubin night following the PROD promotion, the implementing agent checks the PROD worker logs and the latest SkyMapper `CatalogMatch.created_at`, and records the result on scimma/crossmatch-service#135.

### Scope Boundaries

- Backfilling the 103,421 partial objects, in the database or on Hopskotch.
- Detecting or alerting on a catalog that fails every read. This is a separate issue.
- Any change to #134 or to the crossmatch code path.
- Changing how a read failure is classified (transient versus permanent).
- Adopting the collection's nested light-curve photometry in the payload.

### Dependencies / Assumptions

- data.lsdb.io keeps serving the collection at `https://data.lsdb.io/hats/skymapper_dr4` (collection.properties: `hats_primary_table_url=skymapper_dr4_lightcurves`, `default_margin=skymapper_dr4_lightcurves_5arcs`, created 2026-10-27).
- PROD's image (0.17.0, lsdb 0.11.0) can open the collection URL. This was verified on lsdb 0.11.2 only, so it must be confirmed on DEV's or PROD's actual image before PROD changes.
- The old URL pointed at a bare catalog, for which `lsdb.open_catalog` loads no margin. DELVE, also configured as a bare catalog path, opens with `margin=False`. The collection loads a 5 arcsec margin by default, so alerts near a partition edge can now get SkyMapper matches that were never published before. This is an accepted gain, not a behavior change needing sign-off, and the CHANGELOG entry says so.
- The gitops repo is pushed by the maintainer. DEV ArgoCD tracks gitops main, and PROD is pinned to a gitops tag per `docs/prod-promotion-runbook.md` in the gitops repo.

### Outstanding Questions

**Deferred to Planning**

Both are resolved in the Planning Contract:
- the PROD promotion path is KTD2, with branch and tag naming in U3;
- the replay sample and its comparison are KTD4 and U4. The sample uses the runbook's default categories, and the comparison keeps only alerts crossmatched before 2026-10-29.

### Sources / Research

- scimma/crossmatch-service#135: the outage report.
- PROD evidence (2026-11-13):
  - per-catalog last `CatalogMatch.created_at`;
  - `ObjectCrossmatchRecord.catalog_outcomes` counts;
  - worker log lines `Catalog skipped after transient read failure catalog=skymapper_dr4`.
- The equivalence probe: 60 PROD matches recomputed against the collection through `matching/catalog.py` on lsdb 0.11.2, with 60/60 identical.
- `crossmatch/project/settings.py` (`SKYMAPPER_HATS_URL`, the `skymapper_dr4` catalog entry); `crossmatch/core/models.py` (`CatalogSearchOutcome.SKIPPED_READ_FAILURE`, `ObjectCrossmatchRecord`).
- The gitops repo: `apps/crossmatch-service/values.yaml` (`crossmatch.skymapper_hats_url`), `templates/_helpers.yaml` (`SKYMAPPER_HATS_URL` in `provenance.env`), `docs/prod-promotion-runbook.md`.
- `docs/runbooks/crossmatch-replay.md`: the export, `replay_run` and copy-out commands U4 reuses.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **DEV gets the change through an ordinary MR to gitops main, editing only `crossmatch.skymapper_hats_url` in `apps/crossmatch-service/values.yaml`.** DEV's ArgoCD tracks main, and neither `values-dev.yaml` nor `values-prod.yaml` overrides this key. The release label `crossmatch.skymapper_release` stays "SkyMapper DR4". `CONTRACT_REF` and the env contract check are untouched, because no environment variable is added or removed. Governs R1, R3.
- KTD2. **PROD gets a tag on a branch cut from `prod-2026-11-12`, PROD's current tag, carrying the same one-line change.** The runbook's normal path tags a commit on main, but main now carries Babamul, Euclid and DEV-only changes since `prod-2026-11-12` (`git diff prod-2026-11-12..main` touches `_helpers.yaml`, `statefulset.yaml`, `values.yaml` and `values-dev.yaml`). A tag on main would break R4. The tag commit is not on main, so later promotions from main stay correct only because KTD1 puts the same change on main first. The `targetRevision` bump of the four `argocd-apps/*-prod.yaml` files goes to main in its own MR, as every previous promotion did (`promote(prod): point PROD apps at prod-...`). Governs R4.
- KTD3. **The app PR moves every place the old URL appears, not only those R6 names.** The old URL also appears in `docker/docker-compose.yaml` (two services), `docker/.env.example`, `kubernetes/charts/crossmatch-service/values.yaml` and `kubernetes/dev-overrides.yaml.example`. Leaving any of them would put a dead default back into local development or a fresh chart install. The CHANGELOG notes the edge-match gain (Dependencies / Assumptions) and that existing `docker/.env` files setting the old URL must be updated by hand. Governs R6.
- KTD4. **The DEV replay gate compares the replay's SkyMapper output against PROD's stored matches with a one-off script that is never committed.** A replay sample holds only alert positions, and `replay_compare` compares two snapshots, not a snapshot against stored data. Steps:
  1. The maintainer exports a sample on PROD (read-only) and runs `replay_run` on DEV after KTD1 is live.
  2. The agent copies the snapshot out.
  3. The agent pulls the stored SkyMapper matches and outcomes for the sampled alerts from PROD in a read-only query, keeping only alerts whose crossmatch record shows SkyMapper searched and not `skipped_read_failure` (crossmatched before 2026-10-29).
  4. It checks the pass condition R5 states: identical source ids and payloads where PROD stored a match, and no match where PROD recorded none.
  5. It reads R5 together with the accepted margin gain (Dependencies / Assumptions). The old bare catalog loaded no margin and the collection loads 5 arcsec, so a difference counts as that accepted gain, not a failure, when the replay's SkyMapper source lies in a different HATS partition than the one holding the alert's position. That covers both a new match for an alert that had none and a nearer neighbour replacing a stored match. Such differences are listed separately in the result. Every other difference fails the gate.
  6. It reports the post-filter counts: alerts with a stored SkyMapper match, and alerts searched with no match.

  This mirrors the probe that established equivalence on 2026-11-13, now on DEV's own Dask runtime (lsdb 0.11.0, the same as PROD). Governs R3, R5, AE1. (session-settled: user-approved - chosen over a metadata-only coverage-map check: only the real compute path exercises the partition reads that failed.)
- KTD5. **The agent runs no cluster-mutating command.** The replay commands are the maintainer's, given verbatim. The export is a read-only transaction on PROD, and `replay_run` refuses outside DEV and writes nothing. The agent's own cluster access is read-only: `kubectl get`, `logs`, and `exec` of read-only Django shell queries.

### Assumptions

- PROD's 0.17.0 image and DEV's 0.19.0 image both pin lsdb 0.11.0 and hats 0.11.0 (checked in `v0.17.0`'s `requirements.base.txt`, and PROD's celery worker runs the 0.17.0 image), so a passing DEV replay covers PROD's read path. The DEV replay is the only check on lsdb 0.11.0 against the collection; the earlier probe used 0.11.2.
- No Rubin alerts are guaranteed to flow before the PROD promotion. The replay is therefore the gate (AE1), and the first live DEV and PROD batches are confirmed afterwards.
- A tag on a commit outside main is fetched by PROD's ArgoCD, because ArgoCD resolves tags against the upstream repo, not against branches.

---

## Implementation Units

### U1. Move the app's SkyMapper default to the collection URL

- **Goal:** every default in the app repo names `https://data.lsdb.io/hats/skymapper_dr4`, and the change is recorded in the CHANGELOG.
- **Requirements:** R6, R7; KTD3.
- **Dependencies:** none.
- **Files:**
  - `crossmatch/project/settings.py`
  - `docker/docker-compose.yaml`
  - `docker/.env.example`
  - `kubernetes/charts/crossmatch-service/values.yaml`
  - `kubernetes/dev-overrides.yaml.example`
  - `scripts/dump_catalog_columns.py`
  - `docs/references/skymapper_dr4-columns.md` (configured URL and resolved catalog root)
  - `scimma_crossmatch_service_design.md` (both SkyMapper URL mentions)
  - `CHANGELOG.md`
  - this plan file
- **Approach:**
  1. Replace the old URL at each site, without touching surrounding lines.
  2. In the column reference, set "Resolved catalog root" to the collection's primary table, `https://data.lsdb.io/hats/skymapper_dr4/skymapper_dr4_lightcurves`.
  3. Add an `[Unreleased]` `Fixed` entry: SkyMapper DR4 moved to a collection on data.lsdb.io; matches are identical; edge matches are possible now that a 5 arcsec margin loads; local `.env` files that set the old URL need updating; DEV and PROD are fixed through gitops.
- **Test expectation:** none for the defaults themselves; this is a configuration change. Verification below replaces it.
- **Verification:**
  - The in-container suite passes.
  - A worker container started with no `SKYMAPPER_HATS_URL` override opens SkyMapper and matches probe alerts. This reuses the scratchpad probe, never committed.
  - A search of the repo finds `skymapper_dr4/catalog` only in historical records left unchanged: `docs/plans/`, `docs/brainstorms/` and the untracked `docs/grounding/`. It appears nowhere in code, config, docker, kubernetes, scripts, the design doc or the column reference.

### U2. Prepare the gitops DEV change on main

- **Goal:** a local gitops branch from `upstream/main` that changes only `crossmatch.skymapper_hats_url`, ready for the maintainer to push and merge.
- **Requirements:** R1, R3; KTD1.
- **Dependencies:** none. It can run in parallel with U1.
- **Files:** `apps/crossmatch-service/values.yaml` in the gitops repo.
- **Approach:**
  1. Create branch `fix/skymapper-collection-url` from the fetched `upstream/main` and change the one value.
  2. Run `helm template` with the DEV and PROD overlays to confirm the rendered `SKYMAPPER_HATS_URL` changed and nothing else moved.
  3. Commit with a `fix(crossmatch-service):` subject, then stop. The agent does not push.
- **Test expectation:** none -- a single values change. The rendered-manifest diff is the check.
- **Verification:** `helm template` diffs for DEV and PROD show exactly one changed env value in each workload that receives `provenance.env`.

### U3. Prepare the PROD promotion branches

- **Goal:** two local gitops branches.
  - A tag-source branch cut from `prod-2026-11-12` with the same one-line change.
  - A main-based promote branch that points the four PROD Applications at the new tag.
- **Requirements:** R4, AE2; KTD2.
- **Dependencies:** U2, so main and the tag carry the same value.
- **Files:** `apps/crossmatch-service/values.yaml` on the tag-source branch; `argocd-apps/crossmatch-service-prod.yaml`, `dask-prod.yaml`, `oauth2-proxy-prod.yaml` and `monitoring-prod.yaml` on the promote branch.
- **Approach:**
  1. `prod/skymapper-url` from `prod-2026-11-12` with the one-line change.
  2. Name the tag `prod-2026-11-13`, or the next free `-N` suffix on the day the maintainer tags.
  3. `promote/prod-<tag>` from `upstream/main`, editing only `targetRevision` in the four files.
  4. Confirm `git diff prod-2026-11-12..prod/skymapper-url` is exactly one line.
  5. Stop. The agent creates no tag and pushes nothing.
- **Test expectation:** none -- branch preparation.
- **Verification:** the tag-source diff from `prod-2026-11-12` is the single URL line. The promote branch diff is four `targetRevision` lines.

### U4. Run the gates and roll out (maintainer-run, agent-verified)

- **Goal:** DEV, then PROD, read SkyMapper again, with the replay gate passed before PROD moves.
- **Requirements:** R3, R4, R5, AE1, AE3; KTD4, KTD5; Success Criteria.
- **Dependencies:** U2 merged and synced on DEV, U3 branches prepared. U1 can merge at any point.
- **Files:** none committed. The comparison script and the replay files live in the session scratchpad.
- **Approach:**
  1. The maintainer pushes U2 and merges its MR. The agent confirms, read-only, that the DEV ArgoCD app is Synced at the merge commit and that the celery worker's `SKYMAPPER_HATS_URL` is the collection.
  2. The maintainer runs `replay_export_sample --seed skymapper-135` on PROD and `replay_run` on DEV, per `docs/runbooks/crossmatch-replay.md`. The agent supplies the exact commands with the PROD and DEV kubeconfigs and copies the files out.
  3. The agent runs the KTD4 comparison and reports pass or fail against R5.
  4. On pass, the maintainer pushes `prod/skymapper-url`, tags and pushes the tag to the upstream remote, pushes and merges the promote MR, and runs the four `kubectl apply` commands from the runbook against PROD.
  5. The agent confirms, read-only, that the PROD apps are Synced and Healthy at the new tag and that the PROD worker's `SKYMAPPER_HATS_URL` is the collection.
  6. After the first Rubin night, the agent checks the PROD batch logs and the latest SkyMapper `CatalogMatch.created_at`, and comments the result on #135.
- **Test expectation:** none -- operational rollout. The replay comparison is the test.
- **Verification:** every pass condition of R5 holds for the sampled alerts crossmatched before 2026-10-29. PROD apps are Synced and Healthy at the new tag. After the first night, a new SkyMapper match exists on PROD and no batch logs `catalogs_skipped=['skymapper_dr4']`.

---

## Verification Contract

| Gate | How | Proves |
|---|---|---|
| App suite | In-container pytest per the project CLAUDE.md, with `-e BABAMUL_ENABLED=false` | U1 changes nothing else |
| App default opens | Scratchpad probe from the branch image with no URL override | R6 |
| Render diff | `helm template` with DEV and PROD values, before versus after | KTD1, U2 |
| Promotion diff | `git diff prod-2026-11-12..prod/skymapper-url` is one line | R4, AE2 |
| DEV replay gate | `replay_run` on DEV plus the KTD4 comparison against PROD stored matches | R3, R5, AE1 |
| Cluster state | Read-only `kubectl get applications` and an env check on both clusters | R1 |
| Post-night | PROD logs and the last SkyMapper match time, recorded on #135 | Success Criteria |

---

## Definition of Done

- The U1 app PR is open on scimma upstream with "Closes #135", and CI is decided.
- The U2 and U3 gitops branches exist locally with the diffs Verification names. The maintainer has exact push, tag, MR and apply commands.
- After the maintainer's steps, U4's DEV gate has passed, PROD runs at the new tag, and the post-night result is recorded on #135.
- No probe script, replay file or comparison output is committed.

---

## Rollout Contract (maintainer)

The agent supplies the exact commands for each step at the time, with kubeconfig paths and the remote names checked against `git remote -v`.

1. Push `fix/skymapper-collection-url` to the gitops fork, open the MR to upstream main, and merge it. DEV syncs.
2. Export the PROD sample and run the DEV replay; hand the files to the agent for the comparison.
3. On pass: push `prod/skymapper-url`, then tag and push `prod-2026-11-13[-N]` to the remote whose URL is `https://gitlab.com/ncsa-caps-rse/crossmatch-service-k8s-gitops.git`.
4. Push `promote/prod-<tag>`, merge its MR, and `kubectl apply` the four `argocd-apps/*-prod.yaml` files against PROD, `crossmatch-service` and `dask` together.
5. Merge the U1 app PR at any time. It changes no cluster behavior.
