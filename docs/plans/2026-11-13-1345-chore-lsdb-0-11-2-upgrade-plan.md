---
title: LSDB 0.11.2 Upgrade - Plan
type: chore
date: 2026-11-13
topic: lsdb-0-11-2-upgrade
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# LSDB 0.11.2 Upgrade - Plan

## Goal Capsule

- Objective: The public crossmatch service runs on the current LSDB patch releases in PROD, and every change this causes in what it publishes has been identified and explained before PROD moves.
- Means: a version-only bump to lsdb 0.11.2, hats 0.11.2 and nested-pandas 0.7.3 (with the pyarrow move they force), shipped as its own release and gated by the existing replay.
- Product authority: This plan, from scimma/crossmatch-service issue #133. Active scope is the dependency upgrade of the app and its Dask cluster. Adopting new LSDB features is not in scope.
- Open blockers: None for planning or the PR. The DEV rollout waits on 0.19.0 reaching PROD (R9).
- Stop conditions: stop and report if pip resolution forces a direct pin change beyond lsdb, hats and nested-pandas (KTD1), if the suite or live check fails in a way that cannot be fixed without changing published payload semantics or Euclid's footprint (R3, AE1), or if a forced dask/distributed move cannot be made in lockstep with the Dask image (a forced move that can is recorded per R2 and AE2, not a stop).
- Execution profile: one PR. The pipeline implements U1-U3 and opens the PR on scimma upstream from the `bot` fork. The Rollout Contract (export, replays, tag, DEV/PROD rollout, PROD MR) is maintainer-run after merge.
- Who finishes: the maintainer merges upstream, tags 0.20.0 once 0.19.0 is on PROD, and runs the Rollout Contract.
- Product Contract preservation: Product Contract unchanged; the Deferred to Planning questions are resolved in KTD1-KTD4.

---

## Product Contract

### Summary

Move the app and its Dask cluster from lsdb 0.11.0 to 0.11.2, hats 0.11.0 to 0.11.2, and nested-pandas 0.7.2 to 0.7.3, with pyarrow moving from 24 to 26 because nested-pandas 0.7.3 requires it. Nothing in the crossmatch code path changes. The upgrade ships as release 0.20.0 and rolls out after 0.19.0 is on PROD, with PROD gated by a before/after replay on DEV whose every difference is explained in the PROD promotion MR.

### Problem Frame

Issue #133 reports new LSDB releases: lsdb 0.11.1 (2026-10-22) and 0.11.2 (2026-11-13), hats 0.11.1 and 0.11.2, and nested-pandas 0.7.3. The service pins lsdb 0.11.0 (v0.14.0, 2026-09-25). The upstream changes include an S3 read fix in hats 0.11.1, which matters now that Euclid DR1 is read from IRSA's S3 bucket, and new validation of column names and skymap files in hats 0.11.2, which touches the skymap-derived coverage map Euclid depends on.

This is not a pure patch bump. lsdb 0.11.2 requires `hats>=0.11.2` and `nested-pandas>=0.7.3`, and nested-pandas 0.7.3 requires `pyarrow>=26`, while the lock holds pyarrow 24.0.0. pyarrow sits under every catalog read and is not covered by the startup version-skew guard, which compares only lsdb and hats beyond the Dask serialization set. A behavior change there could alter published values without any error.

Two other releases are in flight. DEV runs 0.19.0 (Babamul and Euclid DR1) and is waiting for its first Rubin night to measure Euclid's batch cost. PROD still runs 0.17.0.

### Key Decisions

- **Version bump only; no new LSDB features.** lsdb 0.11.1 and 0.11.2 add an outer crossmatch, a top-level `lsdb.crossmatch`, and `CrossMatchStream` (one anchor crossmatched with several catalogs); none is adopted, so every output difference traces to the libraries. (session-settled: user-approved - chosen over also measuring CrossMatchStream and over rewriting the batch on it: smallest, most attributable change.) Governs R1, R2, R3.
- **Roll out only after 0.19.0 is on PROD, as its own release.** Euclid is measured on DEV at 0.19.0 and promoted first; this upgrade then runs its own DEV, replay, PROD cycle, so each PROD move changes one thing. (session-settled: user-approved - chosen over upgrading DEV after the Euclid night but before PROD, which would take PROD from 0.17 to 0.20 in one step, and over upgrading DEV now, which would confound the Euclid measurement.) Governs R9, R10.
- **The replay gates PROD, as for 0.11.0.** pyarrow moving two major versions is the reason a silent value change is plausible; only the replay catches one. (session-settled: user-approved - chosen over tests plus one DEV night and over tests only.) Governs R6, R7, R8.
- **pyarrow moves because it is forced; nothing else moves unless forced.** Carries the 0.11.0 upgrade's "only forced dependencies" rule to this release. Governs R2.
- **A later 0.11.x patch is taken if it ships before PROD promotion.** It is a re-pin in a patch release plus a fresh candidate replay, as the 0.11.0 plan provided. Governs R11.

### Actors

- A1. Maintainer: merges upstream, tags the release, pushes gitops changes and the PROD tag, and approves the PROD promotion MR.
- A2. Implementing agent: bumps pins, runs the checks, runs the replays on DEV, and drafts the explanations.

### Key Flows

- F1. Upgrade, validate, promote
  - **Trigger:** The plan is picked up for implementation.
  - **Actors:** A1, A2
  - **Steps:**
    1. Bump the pins, run the suite and the live catalog check, and merge the app PR (R1-R5). This can happen while 0.19.0 is still on its way to PROD.
    2. Once 0.19.0 is on PROD, export a PROD sample and take the baseline replay on DEV at 0.19.0 (R6, R9).
    3. Tag 0.20.0 and roll it to DEV with the Dask and app images in lockstep (R10).
    4. Take the candidate replay and compare (R7).
    5. Explain every difference in the PROD promotion MR, then promote (R8, R10).
  - **Covered by:** R1-R11

### Requirements

**Dependency upgrade**

- R1. lsdb, hats and nested-pandas are exact-pinned at 0.11.2, 0.11.2 and 0.7.3 at every pin site: `crossmatch/requirements.base.txt`, the regenerated `crossmatch/requirements.lock`, and the local Dask services' `EXTRA_PIP_PACKAGES` in `docker/docker-compose.yaml`.
- R2. pyarrow moves to the version the resolver selects within `>=26`; any other direct or transitive version change is recorded with the requirement that forced it.
- R3. The crossmatch code path is unchanged; code changes are limited to what the new versions require to keep current behavior (for example, an API deprecation or a new validation error).

**Pre-merge checks**

- R4. The full test suite passes in-container on the new versions.
- R5. Each configured catalog (Gaia DR3, DES Y6 Gold, DELVE DR3 Gold, SkyMapper DR4, Euclid DR1 MER Deep and Wide) opens on the new stack from the app image with its payload columns present, and Euclid's skymap-derived coverage map still builds under hats 0.11.2's new skymap and column validation.

**Replay gate**

- R6. A PROD sample is exported, and a baseline snapshot is taken on DEV at 0.19.0 before either DEV image tag moves.
- R7. A candidate snapshot is taken from the same sample after the celery workers log `Dask cluster verified` on 0.20.0, and the comparison report is produced, including the Euclid keep-all-matches catalogs compared source by source.
- R8. Every output-difference group and every comparability flag in the report is explained in the gitops PROD promotion MR, and a flag caused by a transient catalog read is resolved by re-running that replay.

**Release and rollout**

- R9. DEV moves to 0.20.0 only after 0.19.0 is live on PROD.
- R10. The upgrade ships as release 0.20.0 per the CHANGELOG convention, and DEV and PROD roll out with the app and Dask image tags in lockstep, Dask first, per `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`. PROD promotes only after R8 is satisfied.
- R11. If a newer lsdb 0.11.x ships before PROD promotion, it is adopted in a patch release and the candidate replay is re-run against it.

### Acceptance Examples

- AE1. **Covers R5.** Given hats 0.11.2 rejects the Euclid Wide skymap or a requested column, when the live catalog check runs, then the PR does not merge until the cause is fixed without changing which sources count as inside Euclid's footprint, or the user decides otherwise.
- AE2. **Covers R2.** Given pip resolution also forces a dask or numpy change, when the implementer records it, then the reason appears in the commit, the CHANGELOG entry and the PROD MR.
- AE3. **Covers R8.** Given the report shows a `type or null representation changed` group on a Euclid flux column, when the PROD MR is prepared, then that group has a written explanation, or the upgrade does not promote.
- AE4. **Covers R9.** Given the PR is merged and 0.20.0 is tagged while PROD is still on 0.17.0, when the maintainer considers the DEV rollout, then DEV stays on 0.19.0 until 0.19.0 is on PROD.

### Success Criteria

- PROD runs lsdb 0.11.2 (or a later 0.11.x per R11) with the startup version checks passing on the first try and no crash-looping pods.
- The PROD promotion MR holds a replay report whose every group and flag has an explanation a reviewer can check.

### Scope Boundaries

- Adopting CrossMatchStream, `lsdb.crossmatch`, outer crossmatch, or any other new LSDB feature; measuring CrossMatchStream for the batch is a possible later follow-up.
- hats-import, which the service does not use.
- Adding pyarrow or nested-pandas to the startup version-skew guard.
- Any change to `CROSSMATCH_BATCH_MAX_SIZE`.
- The 0.19.0 PROD promotion itself and its open items (Euclid dataset DOIs, PROD consumer CPU requests).

### Dependencies / Assumptions

- The Dask scheduler and workers on DEV and PROD run the crossmatch-service image at the same tag as the app, so pyarrow and nested-pandas are aligned by image tag lockstep rather than by the skew guard.
- The replay tool handles `match_mode: all` catalogs source by source (`crossmatch/replay/compare.py`).
- PyPI metadata (checked 2026-11-13): lsdb 0.11.2 requires `hats>=0.11.2,<0.12` and `nested-pandas>=0.7.3,<0.8`; nested-pandas 0.7.3 requires `pyarrow>=26`, `pandas>=3,<4` and `fsspec>=2026.2.0` (the lock holds fsspec 2026.4.0); hats 0.11.2 requires `numpy>=2.3,<3` (the lock holds 2.4.2).
- Whether dask 2026.1.2 and db-dtypes work correctly with pyarrow 26 is unverified until the suite and the replay run.
- If the 0.19.0 PROD promotion stalls, this rollout stalls with it; the PR and tag can still proceed.

### Outstanding Questions

All Deferred to Planning questions are resolved in the Planning Contract: lock moves (KTD1), lsdb API changes (KTD2), the live check (KTD3), and the stale version comments (KTD4). The exact pyarrow 26.x is an execution-time result of the resolver.

### Sources / Research

- scimma/crossmatch-service issue #133.
- `docs/plans/2026-09-24-1207-chore-lsdb-0-11-upgrade-plan.md`: the previous upgrade's shape, replay gate and rollout contract.
- `docs/runbooks/crossmatch-replay.md`: the export, baseline, candidate and compare flow.
- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` and `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`: pin-site and rollout conventions.
- `crossmatch/core/dask.py`: the startup skew guard (`_VERSION_CHECK_PACKAGES`, `_OFF_BOUNDARY_PACKAGES = ('lsdb', 'hats')`).
- `crossmatch/matching/catalog.py`: `_skymap_moc`, the skymap-derived coverage map affected by hats 0.11.2's skymap validation.
- Upstream release notes: lsdb v0.11.1 and v0.11.2, hats v0.11.1 and the v0.11.1...v0.11.2 compare ("Validate column names and skymap files", "Validate npix leaf catalogs"), nested-pandas v0.7.3 ("Update tensor null behavior with pyarrow 26 changes", "Fix to work with pandas 3.1").
- `docs/grounding/lsdb-0-11-2-upgrade.md` (untracked): grounding dossier with file:line quotes for the pin sites, skew guard and prior upgrade.
- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` "LSDB 0.10.4 -> 0.11.0 upgrade" example: the `ResolutionImpossible` / `--upgrade-package` pattern KTD1 follows.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Move only lsdb, hats and nested-pandas in `requirements.base.txt`; let pip-compile move pyarrow transitively.** pyarrow has no direct pin today and stays without one; recompile without `--upgrade`, and if resolution fails on a locked package, upgrade only that package with `--upgrade-package` (the 0.11.0 db-dtypes pattern). numpy, pandas, dask/distributed and fsspec already satisfy the new requirements and are not expected to move; if the resolver does force one, it is recorded per R2 and AE2. Governs R1, R2 (instantiates the "version bump only" Key Decision).
- KTD2. **No code change is expected on the crossmatch path.** The lsdb v0.11.0...v0.11.2 diff for the calls this service makes (`lsdb.open_catalog` with `columns="all"` and an explicit list, `lsdb.from_dataframe`, `Catalog.crossmatch` with the default KD-tree algorithm and `how="inner"`) adds the `outer` mode, a `load_extension` API and clearer coordinate-column errors; the inner path keeps the same semantics and the default margin threshold. hats 0.11.2's source changes are the validation module (#773, #782), which this service never calls; `get_healpix_from_path` in `hats/io/paths.py`, now matching leaf pixel directories; and remote directory reads in `hats/io/file_io/file_io.py`, now listing every non-directory entry rather than only `is_file()` entries. `read_skymap` is untouched. The two read-path changes affect remote catalogs (data.lsdb.io, IRSA S3), so U3's live check is their proof. Any fix the suite or live check does surface is limited by R3. Governs R3.
- KTD3. **The live catalog check (R5) runs as a throwaway script against the locally built branch image, not as committed code.** It opens each configured catalog through `matching/catalog.py`'s own loader with the app's settings, asserts the payload columns, builds `catalog_moc` for each (exercising `_skymap_moc` for both Euclid builds), and crossmatches a small set of alert positions inside EDF-South/EDF-Fornax and inside each other footprint. Its result goes in the PR body. Rationale: the same probe ran for Euclid's PR and needs network access to data.lsdb.io and the IRSA bucket, which CI does not have.
- KTD4. **Fix the stale hats 0.10.4 comments in `docker/Dockerfile` and `.github/workflows/lock-drift.yml` in the pin commit, wording them version-neutrally.** They describe how the lock protects transitive pins and will drift again on every upgrade if they name a version.
- KTD5. **The CHANGELOG entry lands under `[Unreleased]`; 0.20.0 is cut later by the release PR.** Matches the project convention and R10, since the release must wait for 0.19.0 on PROD.

### Assumptions

- pip-compile is run with Python 3.12 (matching the lock header and the `python:3.12.12` base image) inside a container, because the host Python environments are broken.
- The local Dask services in `docker/docker-compose.yaml` install their packages from `EXTRA_PIP_PACKAGES` and do not pin pyarrow; pip resolves pyarrow there from nested-pandas 0.7.3's floor, so they get pyarrow 26.x too. DEV and PROD Dask run the app image, so the lock governs them.
- The live check can reach data.lsdb.io and the IRSA S3 bucket from this machine, as it did for the Euclid PR.

### Risks

- **pyarrow 24 to 26 changes a value representation.** Mitigation: the suite (payload coercion tests) catches type-level breaks; the replay (R6-R8) catches value-level changes before PROD.
- **dask 2026.1.2 misbehaves with pyarrow 26.** dask declares `pyarrow>=16`, so resolution will not flag it. Mitigation: the suite plus the live crossmatch; if dask must move, it moves in lockstep with the Dask image and is recorded per R2 and AE2, never silently.
- **A transient catalog read failure in the live check.** Mitigation: re-run, as the replay runbook already does; a repeatable failure is a finding.

---

## Implementation Units

### U1. Move the pins and regenerate the lock

- **Goal:** lsdb 0.11.2, hats 0.11.2 and nested-pandas 0.7.3 are pinned at every pin site and the lock resolves them with pyarrow 26.x.
- **Requirements:** R1, R2; KTD1, KTD4.
- **Dependencies:** none.
- **Files:** `crossmatch/requirements.base.txt`, `crossmatch/requirements.lock`, `docker/docker-compose.yaml`, `docker/Dockerfile`, `.github/workflows/lock-drift.yml`, `CHANGELOG.md`, `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md`.
- **Approach:**
  1. Update the three pins and the explanatory comment above them in `requirements.base.txt` (lsdb 0.11.2 requires `hats>=0.11.2,<0.12` and `nested-pandas>=0.7.3,<0.8`; nested-pandas 0.7.3 requires `pyarrow>=26`).
  2. Regenerate `requirements.lock` per KTD1 and record every package that moved.
  3. Update both `EXTRA_PIP_PACKAGES` lines in `docker/docker-compose.yaml`.
  4. Reword the stale comments per KTD4.
  5. Add the `[Unreleased]` CHANGELOG entry naming every moved package and the replay-before-PROD requirement (KTD5).
  6. Add a short "LSDB 0.11.0 -> 0.11.2" example to the pin-upgrade convention doc naming what the lock diff moved.
- **Patterns to follow:** the 0.14.0 CHANGELOG entry and the 0.11.0 example in the convention doc.
- **Test expectation:** none in this unit -- pure pin and doc change; U2 proves it.
- **Verification:** the regenerated lock matches what lock-drift CI would produce; the lock diff is limited to lsdb, hats, nested-pandas, pyarrow and any package the resolver forced, each named in the CHANGELOG.

### U2. Prove the suite on the new stack

- **Goal:** the full test suite passes on an image rebuilt from the new lock, with any version-driven fix confined to R3.
- **Requirements:** R3, R4; KTD2.
- **Dependencies:** U1.
- **Files:** only if a failure requires it -- the failing module under `crossmatch/` and its test under `crossmatch/tests/`.
- **Approach:** rebuild the compose image so it installs the new lock, then run the suite in-container per the project CLAUDE.md. Confirm the running container reports the new lsdb, hats, nested-pandas and pyarrow versions before trusting a green run. A failure is diagnosed against the upstream changelogs; a fix that would alter published values stops the run.
- **Execution note:** smoke-first: confirm installed versions inside the container before reading test results.
- **Test scenarios:**
  - Existing payload coercion tests pass under pyarrow 26 (numpy scalars, `pd.NA`, NaN, `str` dtype values stay JSON-native; diaObjectId stays an exact int).
  - Existing catalog validation, crossmatch-call and compute tests pass unchanged (the inner crossmatch call shape is unchanged per KTD2).
  - If a fix is needed, a test reproducing the failure under the new versions is added beside it.
- **Verification:** suite green on the rebuilt image; `makemigrations --check` clean; no change to the crossmatch call or payload code unless a failure required it.

### U3. Live catalog check on the new stack

- **Goal:** every configured catalog opens and crossmatches on the new stack, and Euclid's skymap-derived coverage still builds.
- **Requirements:** R5; AE1; KTD3.
- **Dependencies:** U1, U2.
- **Files:** none committed; the probe script lives in the session scratchpad and its output is summarized in the PR body.
- **Approach:** run the KTD3 probe from the locally built image with network access, recording per catalog: opened, payload columns present, margin present, coverage map area, and match counts for the probe positions.
- **Test scenarios:**
  - Covers AE1. Both Euclid builds open anonymously, their coverage maps build from the skymap with the same areas measured for the Euclid PR (about 114 deg2 Deep, 2468 deg2 Wide), and a Deep crossmatch returns several matches for some EDF alerts.
  - Gaia DR3, DES Y6 Gold, DELVE DR3 Gold and SkyMapper DR4 open with all payload columns and return nearest matches for positions in their footprints.
- **Verification:** all six catalogs pass; any difference from the Euclid PR's areas is explained or stops the run.

---

## Verification Contract

| Gate | How | Proves |
|---|---|---|
| Lock drift | `.github/workflows/lock-drift.yml` on the PR | `requirements.lock` matches `requirements.base.txt` |
| Test suite | In-container pytest per the project CLAUDE.md "Run tests", on an image rebuilt from the new lock | R3, R4 |
| Migrations | `python manage.py makemigrations --check` in the same container | No model drift |
| Live catalog check | KTD3 probe from the branch image | R5, AE1 |
| CI | Upstream PR checks green (a fork PR showing no checks is not green) | Build and lint gates |
| Replay gate | Maintainer-run after merge, per the Rollout Contract | R6-R8 |

---

## Definition of Done

- U1-U3 are complete and the PR is open on scimma upstream with "Closes #133".
- The lock diff, the installed versions and the live-check results are stated in the PR body, with every moved package named.
- The PR body carries the Rollout Contract below for the maintainer.
- No throwaway probe code, experiment, or dead-end change remains in the diff.

---

## Rollout Contract (maintainer, after merge)

1. Wait until 0.19.0 is live on PROD (R9).
2. Export a PROD sample with `replay_export_sample` and take the baseline replay on DEV at 0.19.0, per `docs/runbooks/crossmatch-replay.md` (R6).
3. Release 0.20.0: rename `[Unreleased]`, merge the release PR, tag `v0.20.0` (R10).
4. Roll DEV to 0.20.0, Dask first, app and Dask tags in lockstep; wait for `Dask cluster verified` (R10).
5. Take the candidate replay and compare; re-run any replay with a transient catalog flag (R7, R8).
6. Explain every difference group and flag in the gitops PROD promotion MR, then promote PROD (R8, R10).
7. If a newer lsdb 0.11.x ships before step 6, re-pin in a patch release and redo steps 3-6 (R11).
