---
title: LSDB 0.11 Upgrade (pandas 3) - Plan
type: chore
date: 2026-09-24
topic: lsdb-0-11-upgrade
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-brainstorm
execution: code
---

# LSDB 0.11 Upgrade (pandas 3) - Plan

## Goal Capsule

- Objective: The public crossmatch service runs on lsdb 0.11.0 and pandas 3 in PROD, and every change this causes in what it publishes has been identified and explained before PROD moves.
- Product authority: This plan. Active scope is the lsdb 0.11 / pandas 3 dependency upgrade of the app and its Dask cluster, validated with the existing replay tool. The replay tool itself is prior work (`docs/plans/2026-09-24-0552-feat-crossmatch-replay-tool-plan.md`) and is not changed here beyond runbook wording.
- Means: exact-pin the forced dependency set, add pandas 3 coverage tests for the crossmatch and ingest paths, then gate PROD with the replay (Key Decisions; KTD1-KTD6).
- Open blockers: None.
- Stop conditions: stop and report if pip resolution needs any direct pin beyond lsdb, hats, nested-pandas, pandas and the transitive db-dtypes (R2), or if a pandas 3 failure cannot be fixed without changing published payload semantics.
- Execution profile: the pipeline implements U1-U4 and opens a PR on the fork. The Rollout Contract steps (PROD export, replays, tag, DEV/PROD rollout, PROD MR) are maintainer-run after merge.
- Who finishes: maintainer merges upstream, tags, and runs the Rollout Contract.

---

## Product Contract

Product Contract preservation: unchanged in scope. Planning resolved the three deferred questions (KTD1, KTD4, KTD5); R13's "recorded alert" is realized as schema-faithful fixtures because retention has nulled stored broker payloads (KTD4).

### Summary

Move the app and its Dask cluster, in lockstep, from lsdb 0.10.4 to 0.11.0, which brings hats 0.11.0, nested-pandas 0.7.x and pandas 3. The three modules that use pandas get a targeted pandas 3 review and new coercion tests. The upgrade ships as a new release (assumed 0.14.0) and rolls to DEV. A replay baseline taken on 0.13.1 before DEV moves is compared with a candidate taken after. PROD is promoted only when every difference in the replay report is explained in the PROD promotion MR.

### Problem Frame

The stack is one minor version behind LSDB (0.10.4; 0.11.0 was released 2026-09-21), and the user asked to move to the latest release. Unlike the 0.10 upgrade, this is not a pin bump. lsdb 0.11.0 requires `hats>=0.11,<0.12` and `nested-pandas>=0.7,<0.8`, and both of those require `pandas>=3`. The current nested-pandas 0.6.10 caps `pandas<2.4`, so the lsdb and pandas moves cannot be separated. (PyPI metadata, checked 2026-09-24.)

pandas 3 changes behavior the service depends on: copy-on-write becomes the default, strings default to a new `str` dtype, and missing-value handling in string columns changes. pandas is used in three modules on the crossmatch path: `crossmatch/matching/payload.py` (converting catalog values to the JSON the public receives), `crossmatch/tasks/crossmatch.py` (building the alert frame in the shared compute step), and `crossmatch/matching/catalog.py`. A pandas 3 behavior change there could alter published values without any error.

Rubin alerts are paused, possibly for up to six weeks from 2026-09-24, so no live batch can validate the upgrade. The replay tool (v0.13.1, live on DEV and PROD) was built to be this upgrade's pre-PROD gate.

### Key Decisions

- **Target lsdb 0.11.0 now; adopt a 0.11.x patch if one ships before the PROD promotion.** Uses the window while alerts are paused; a patch is a re-pin plus a fresh candidate replay. (session-settled: user-approved - chosen over freezing at 0.11.0 and over waiting for a patch: build in the window, keep the option to take early fixes.) Governs R1, R10.
- **Move only the dependencies lsdb 0.11 forces.** numpy, pyarrow and dask/distributed stay pinned unless tests or the replay show a problem, so each replay difference traces to fewer causes. Bumping dask is a documented escalation, not a default. (session-settled: user-approved - chosen over also bumping dask/distributed and over refreshing the whole stack: smallest, most attributable change.) Governs R1, R2.
- **Targeted pandas 3 review and tests before relying on the replay.** Review the three pandas-using modules for the known pandas 3 changes and add unit tests for payload coercion. The replay then confirms nothing else moved. (session-settled: user-approved - chosen over a replay-driven-only approach and over a full line-by-line audit.) Governs R4, R5, R13.
- **The replay does not cover TNS for this upgrade.** DEV has no TNS credentials, so both snapshots flag TNS as not current. The TNS code's pandas use is covered by existing unit tests under pandas 3, and the PROD MR says so. (session-settled: user-approved - chosen over provisioning DEV TNS credentials before the baseline and over treating them as nice-to-have: does not block on an ops task.) Governs R8, R9.
- **The replay gates PROD; the report and explanations go in the gitops PROD promotion MR.** Images are built only from release tags, so the app PR merges before any candidate replay can exist. (session-settled: user-approved - chosen over building a pre-release image to gate the merge and over a committed report doc: no new build path, and PROD is still gated.) Governs R9, R11, R12.
- **Pass means every difference is listed and explained, not zero differences.** Carried from the replay tool's plan. (session-settled: user-directed - chosen over requiring identical output: pandas 3 may legitimately change representations.) Governs R9.
- **Replays run only on DEV, sampling PROD history.** Carried from the replay tool's plan. (session-settled: user-approved - chosen over DEV-only history and over replaying in either environment.) Governs R6, R7.

<!-- ce-section: work-relationships -->
### How This Work Fits Together

This plan covers the lsdb 0.11 upgrade. The broader breakdown below is the current understanding, not a committed roadmap.

- Crossmatch replay tool (`docs/plans/2026-09-24-0552-feat-crossmatch-replay-tool-plan.md`): Enables this upgrade's PROD gate; delivered as v0.13.1.
  - DEV TNS credentials: Still to decide; would let later upgrades' replays cover TNS.
  - Tests for the replay's comparability-flag branches: Can proceed independently of this upgrade.

### Actors

- A1. Maintainer: merges upstream, tags the release, pushes gitops changes and the PROD tag, and approves the PROD promotion MR.
- A2. Implementing agent: bumps pins, does the pandas 3 review and tests, runs the replays on DEV, and drafts the explanations.

### Key Flows

- F1. Upgrade and validate
  - **Trigger:** The plan is picked up for implementation.
  - **Actors:** A1, A2
  - **Steps:**
    1. Bump the pins, do the pandas 3 review and tests, and merge the app PR.
    2. Export a sample on PROD and take the baseline replay on DEV at 0.13.1.
    3. Tag and build the release, then roll it to DEV (Dask first).
    4. Take the candidate replay and compare.
    5. Explain every difference in the PROD promotion MR, then promote.
  - **Covered by:** R1-R13

### Requirements

**Dependency upgrade**

- R1. lsdb is upgraded from 0.10.4 to 0.11.0, with hats, nested-pandas and pandas moved to the versions it requires, exact-pinned at every pin site: `crossmatch/requirements.base.txt`, the regenerated `crossmatch/requirements.lock`, and the local Dask services' `EXTRA_PIP_PACKAGES` in `docker/docker-compose.yaml`.
- R2. No other direct pin changes unless pip resolution, tests, or the replay prove it necessary; any such change (including a dask/distributed bump) is recorded with its reason.
- R3. The app and the Dask cluster run identical lsdb, hats, nested-pandas and pandas versions; the existing startup version checks pass on DEV and PROD after the rollout.

**pandas 3 readiness**

- R4. The three pandas-using modules (`matching/payload.py`, `tasks/crossmatch.py`, `matching/catalog.py`) are reviewed for copy-on-write, default string dtype, and missing-value changes, and any code the review shows would change behavior is fixed.
- R5. Payload coercion has unit tests covering pandas 3 inputs: the `str` dtype, `pd.NA`, NaN and NaT in object and extension columns, and numpy scalars from pandas 3 frames. They pass under pandas 3, and published values stay JSON-native with diaObjectId an exact integer.
- R13. Before the DEV rollout, the alert-ingest path is exercised under pandas 3: each broker client (ANTARES, Pitt-Google, Lasair) decodes a representative recorded alert without error, through the consumer's normal decoding. The PROD promotion MR states that ingest was covered this way, not by the replay. antares-client and pittgoogle-client use pandas internally, and alerts are paused, so neither the replay nor live traffic reaches this path before PROD.

**Replay validation**

- R6. A PROD sample is exported with `replay_export_sample` and kept outside the pod before the upgrade reaches DEV. This is the first real PROD export.
- R7. A baseline snapshot is taken on DEV at 0.13.1 before either DEV image tag moves, and a candidate snapshot is taken from the same sample after the celery worker logs `Dask cluster verified` on the upgraded stack.
- R8. The comparison report is produced. Any comparability break other than the expected "TNS not current" flags is resolved by re-running the affected replay before proceeding; a skipped catalog, for example, is a transient read failure.
- R9. Every output-difference group and every flag in the report is explained in the gitops PROD promotion MR. That includes a statement that TNS was covered by unit tests, not by the replay.

**Release and rollout**

- R10. The upgrade ships as a new release (assumed 0.14.0) per the CHANGELOG convention. If a 0.11.x patch ships before PROD promotion, it is adopted as a patch release and the candidate replay is re-run.
- R11. DEV and PROD roll out with the app and Dask image tags in lockstep, Dask first, per `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`. PROD promotes only after R9 is satisfied.
- R12. The replay runbook and the two upgrade convention docs say that replay explanations are recorded in the PROD promotion MR, not in the app PR.

### Acceptance Examples

- AE1. **Covers R8.** Given a candidate replay whose report shows `skymapper_dr4` skipped, when the maintainer reviews it, then that replay is re-run and the rerun's report is the one explained in the PROD MR.
- AE2. **Covers R9.** Given a report with a `type or null representation changed` group for a DES column, when the PROD MR is prepared, then that group has a written explanation (for example, "pandas 3 returns `str` dtype; the payload value is unchanged in JSON") or the upgrade does not promote.
- AE3. **Covers R10.** Given lsdb 0.11.1 is released after the DEV rollout but before PROD promotion, when the maintainer continues, then the pins move to 0.11.1 in a patch release, DEV rolls to it, and a new candidate replay replaces the old one in the PROD MR.
- AE4. **Covers R2.** Given the suite fails only because dask 2026.1.2 mishandles a pandas 3 frame, when the implementer resolves it, then dask/distributed move to a compatible version in the same change, and the reason is recorded in the commit and the PROD MR.

### Success Criteria

- PROD runs lsdb 0.11.x with pandas 3, with the version checks passing on the first try and no crash-looping pods.
- The PROD promotion MR holds a replay report whose every group and flag has an explanation a reviewer can check.

### Scope Boundaries

- Provisioning DEV or PROD TNS credentials.
- Tests for the replay's comparability-flag branches.
- A pre-release (sha-tagged) image build path.
- Any change to `CROSSMATCH_BATCH_MAX_SIZE`.
- Adopting new lsdb 0.11 or pandas 3 features.

### Dependencies / Assumptions

- The replay tool at v0.13.1 is deployed on DEV and PROD, with `CROSSMATCH_REPLAY_ENABLED` on DEV only and `APP_VERSION` on every app pod (scimma/crossmatch-service#108, #110, #111).
- numpy 2.4.2 satisfies pandas 3 (`>=1.26`) and hats 0.11 (`>=2.3,<3`); pyarrow 24.0.0 satisfies all declared requirements.
- pip-compile must move `db-dtypes` (pulled in by pittgoogle-client) from 1.6.0, which requires `pandas<3.0.0`, to 1.7.x, which allows `pandas<4`. This is a forced transitive move permitted by R2.
- dask 2026.1.2's metadata permits pandas 3 (`pandas>=2.0`). Whether it works correctly with pandas 3 is unverified until tests and the replay run (see AE4).
- The v0.13.1 export has run at DEV scale (6 s on 1.9M alerts); its first PROD run is part of R6 and is bounded by its 120 s per-query timeout.
- Hosted catalog reads, especially SkyMapper on `data.lsdb.io`, fail transiently at times; re-running a replay is the remedy.

### Outstanding Questions

**Deferred to Planning**

All deferred questions are resolved in the Planning Contract: exact pins and transitive moves (KTD1), lsdb API changes (KTD2), the release number (KTD6), and the R13 fixture source (KTD4).

### Sources / Research

- `docs/plans/2026-09-24-0552-feat-crossmatch-replay-tool-plan.md`: the replay tool this upgrade uses as its gate.
- `docs/runbooks/crossmatch-replay.md`: the export, baseline, rollout, candidate and compare flow.
- `docs/plans/2026-08-11-001-chore-lsdb-0-10-upgrade-plan.md`: the previous lsdb upgrade's shape.
- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md` and `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`: pin-site and rollout conventions, both updated by R12.
- `docs/solutions/design-patterns/coerce-numpy-pandas-scalars-to-json.md`: the coercion boundary R5 tests.
- `crossmatch/requirements.base.txt` (lsdb, hats, nested-pandas, numpy, pandas, dask pins) and `docker/docker-compose.yaml` (the local Dask services' `EXTRA_PIP_PACKAGES`).
- `.github/workflows/build-image.yml`: images are built on `v*.*.*` tags; a manual run produces only a `sha-` tag, which motivates the PROD-gate decision.
- PyPI metadata (checked 2026-09-24):
  - lsdb 0.11.0 requires `hats>=0.11,<0.12` and `nested-pandas>=0.7,<0.8`;
  - hats 0.11.0 and nested-pandas 0.7.2 require `pandas>=3`; nested-pandas 0.6.10 requires `pandas<2.4`;
  - pandas 3.0.6 requires `numpy>=1.26`;
  - dask 2026.1.2 declares `pandas>=2.0` and `pyarrow>=16`.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Exact pins: lsdb 0.11.0, hats 0.11.0, nested-pandas 0.7.2, pandas 3.0.6; db-dtypes 1.6.0 -> 1.7.1 transitively.** A dry pip-compile over the current lock fails (`ResolutionImpossible`) because the locked db-dtypes 1.6.0 (via pittgoogle-client) requires `pandas<3`; `pip-compile --upgrade-package db-dtypes` resolves, and the lock diff is exactly those five lines. numpy, pyarrow and dask/distributed do not move, which satisfies the "only forced deps" decision. Governs R1, R2.
- KTD2. **No app code changes are forced by the lsdb API.** Inspected in containers: `from_dataframe` and `Catalog.crossmatch` signatures are identical between 0.10.4 and 0.11.0. `open_catalog` made `search_filter` keyword-only and added `storage_options`; the app calls `open_catalog(url, columns=...)`, which is unaffected. Governs R1, R4.
- KTD3. **pandas 3 review targets three behaviors in three modules, and the review is proven by tests, not only by reading.** In `matching/payload.py`, `_to_json_scalar` handles None/NaN/NaT/`pd.NA` through `pd.isna`, and numpy scalars through isinstance checks; it must also handle pandas 3's `str` dtype values. The review confirms whether those arrive as Python `str` (passes through) or as another object (falls to `str(value)`). In `tasks/crossmatch.py`, the frame build (`astype(str)` on uuid, `dropna`) and `itertuples` rows. In `matching/catalog.py`, columns from `.compute()`. Fix only what a test shows changes behavior. Governs R4, R5.
- KTD4. **R13 uses schema-faithful fixtures, driven through each client's own decoding.** Recorded alerts are unavailable: retention nulls `Alert.payload` after the grace period (DEV 3 days, PROD 30), and alerts have been paused longer than that. Fixtures are built in the shape each client consumes. ANTARES: stream messages carry no alert records, and `locus.alerts` is fetched lazily from the ANTARES REST API (`antares_client.models._list_resources`), so the test decodes a zlib+bson locus fixture through `antares_client.stream._parse_message` (what `StreamingClient.iter()` uses), which also parses the lightcurve CSV with pandas. `relationships.alerts` holds links only, because a `data` entry fails the client's schema. The test patches `_list_resources` to return alerts loaded from a JSON:API alerts fixture, so no network call is made. Pitt-Google: a `pittgoogle.Alert` built from an lsst-alerts-json payload, read via `alert.dict`. Lasair: JSON through `normalize_lasair`. Each test ends in `ingest_alert` so the whole ingest path runs under pandas 3. Governs R13.
- KTD5. **A persistent catalog-identity flag is explained, not re-run.** The replay records hats catalog properties, and hats moves 0.10.4 -> 0.11.0, so the "catalog build differs" flag may appear on every candidate. When it survives a re-run and URLs and row counts are unchanged, the PROD MR explains it as the hats version change. This refines R8's re-run remedy for this one flag and is written into the runbook (U4). Governs R8, R12.
- KTD6. **Release 0.14.0.** pandas 3 is a runtime-level change, and minor bumps are the project's convention for feature-level releases. The CHANGELOG entry lands under `[Unreleased]` in U1, and the release rename happens at tag time per convention. Governs R10.

### Assumptions

- Hands-off `lfg` run: the scoping confirmation was skipped; inferred planning bets are the KTDs above.
- The in-container test run uses an image rebuilt from the new lock (`docker compose build celery-worker`); the local compose Dask services install their `EXTRA_PIP_PACKAGES` at start.

### Sequencing

U1 first (pins), then U2 and U3 against the new pins (both need pandas 3 installed), then U4 (docs). The Rollout Contract follows the merge.

---

## Implementation Units

### U1. Bump the forced dependency set

**Goal:** The app, the lock, and the local Dask services pin lsdb 0.11.0, hats 0.11.0, nested-pandas 0.7.2 and pandas 3.0.6, with db-dtypes moved transitively.

**Requirements:** R1, R2, R3; KTD1, KTD6.

**Dependencies:** None.

**Files:**
- `crossmatch/requirements.base.txt` (modify)
- `crossmatch/requirements.lock` (regenerate)
- `docker/docker-compose.yaml` (modify the two `EXTRA_PIP_PACKAGES` strings)
- `CHANGELOG.md` (`[Unreleased]` Changed entry)

**Approach:**
1. Edit the four pins in `requirements.base.txt` and update its lsdb/hats/nested-pandas comment block to the 0.11 constraints.
2. Regenerate the lock with `pip-compile --strip-extras --upgrade-package db-dtypes --output-file=requirements.lock requirements.base.txt` under Python 3.12. Confirm that the lock diff is exactly the five expected lines.
3. Update both compose `EXTRA_PIP_PACKAGES` strings (dask-scheduler, dask-worker) to the new lsdb, hats, nested-pandas and pandas versions; numpy stays 2.4.2.
4. Rebuild the dev image and run the full suite under pandas 3; record every failure for U2/U3 rather than patching around it.

**Patterns to follow:** `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md`; the 0.10.4 upgrade commit.

**Test scenarios:**
- Test expectation: none new in this unit -- the existing suite run under the new pins is the check; failures route to U2/U3.

**Verification:** `pip-compile` succeeds; the lock diff is the five expected lines; the container reports pandas 3.0.6 and lsdb 0.11.0; the lock-drift CI check passes.

### U2. pandas 3 readiness of the crossmatch path

**Goal:** Published payloads and the compute step behave the same under pandas 3, proven by tests.

**Requirements:** R4, R5; KTD2, KTD3.

**Dependencies:** U1.

**Files:**
- `crossmatch/matching/payload.py` (modify only if a test shows a change)
- `crossmatch/tasks/crossmatch.py`, `crossmatch/matching/catalog.py` (modify only if needed)
- `crossmatch/tests/test_payload.py` (extend)
- `crossmatch/tests/test_crossmatch_compute.py`, `crossmatch/tests/test_crossmatch_tns.py` (extend)

**Approach:**
1. Review the three modules for copy-on-write chained assignment, `str`-dtype assumptions, and missing-value handling (KTD3).
2. Add coercion tests that take values out of real pandas 3 frames and series via `itertuples`/`getattr`, the same way the compute step reads them.
3. Fix `_to_json_scalar` (or the call site) only where a test shows a behavior change, keeping the payload contract: JSON-native values, missing values as null, diaObjectId an exact int.

**Patterns to follow:** `docs/solutions/design-patterns/coerce-numpy-pandas-scalars-to-json.md`; existing `tests/test_payload.py`.

**Test scenarios:**
- A `str`-dtype column value (pandas 3 default for strings) coerces to a Python `str` equal to the original.
- A `pd.NA` in a `str`-dtype column and in a nullable `Int64` column coerces to `None`.
- NaN in a float64 column and NaT in a datetime column coerce to `None`.
- A numpy `int64`, `float32` and `bool_` read from a pandas 3 frame row coerce to `int`, `float` and `bool`.
- A diaObjectId above 2^53 in an int64 column survives the frame build and `itertuples` as the exact `int`.
- `compute_crossmatch` with a mocked catalog result containing a `str`-dtype and a `pd.NA` value produces the same published payload as the pandas 2 expectation, written as a literal.
- `_compute_tns_enrichment` over a pandas 3 frame with two alerts (one near a TNS object, one not) returns the expected enrichment; this proves the per-row `itertuples` loop under pandas 3.
- Uuid values in the alert frame remain strings after the build (the `astype(str)` path under pandas 3).

**Verification:** New and existing tests pass under pandas 3; any production edit is covered by a test that failed before it.

### U3. pandas 3 readiness of alert ingest

**Goal:** Each broker client decodes an alert into our canonical format under pandas 3 before the upgrade reaches DEV.

**Requirements:** R13; KTD4.

**Dependencies:** U1.

**Files:**
- `crossmatch/tests/fixtures/brokers/` (new: antares locus message and JSON:API alerts document, pittgoogle lsst-alerts-json payload, lasair JSON)
- `crossmatch/tests/test_broker_decoding.py` (new)

**Approach:**
1. For ANTARES, decode a zlib+bson locus fixture with `antares_client.stream._parse_message`; the fixture includes a lightcurve CSV and links-only `relationships.alerts`. Patch `antares_client.models._list_resources` to return alerts loaded from a JSON:API alerts fixture, then read `locus.alerts[0].properties` as the consumer does (KTD4).
2. For Pitt-Google, build a `pittgoogle.Alert` from a JSON payload the way the consumer receives it, and read `alert.dict`.
3. For Lasair, parse the JSON message.
4. Feed each through its `normalize_*` and `ingest_alert`, and assert the canonical fields and a created `Alert` row.

**Execution note:** Start from each client's decoding entry point as the consumer calls it. A test that bypasses the client and hands a dict straight to `normalize_*` does not satisfy R13.

**Patterns to follow:** `tests/test_ingest.py`; the consumers in `brokers/*/consumer.py`.

**Test scenarios:**
- An ANTARES fixture message decodes via antares-client to a locus whose newest alert's properties normalize to the expected diaObjectId, ra, dec and event_time, and `ingest_alert` creates one Alert.
- An ANTARES fixture's lightcurve decodes without error under pandas 3 into a DataFrame with the expected columns and row count (string columns arrive as pandas 3 `str` dtype).
- The ANTARES test makes no network call: the patched `_list_resources` is called once for the locus's alerts.
- A Pitt-Google fixture decodes via `pittgoogle.Alert` to `alert.dict`, normalizes, and ingests.
- A Lasair JSON fixture normalizes and ingests.
- A diaObjectId above 2^53 in each fixture arrives in the Alert row exactly.

**Verification:** The tests pass under pandas 3 and exercise the real client decoding.

### U4. Runbook and conventions: where replay explanations go

**Goal:** The docs say the upgrade's replay explanations go in the gitops PROD promotion MR, and how to handle a persistent catalog-identity flag.

**Requirements:** R12; KTD5.

**Dependencies:** None.

**Files:**
- `docs/runbooks/crossmatch-replay.md`
- `docs/solutions/conventions/dependency-pin-upgrade-pattern-2026-05-12.md`
- `docs/solutions/conventions/lockstep-dask-cluster-aligned-upgrade-rollout.md`

**Approach:**
1. Replace "explain in the upgrade PR" wording with "in the gitops PROD promotion MR", and note why: images are built only from release tags, so the app PR merges before a candidate replay exists.
2. Add the KTD5 rule for a persistent catalog-identity flag after a hats version change.
3. Name the non-replay coverage the MR must state (TNS via unit tests, ingest via fixture tests).

**Test expectation:** none -- documentation only.

**Verification:** No doc still says to record replay explanations in the app PR.

---

## Rollout Contract (maintainer-run, after the PR merges)

Each step uses `docs/runbooks/crossmatch-replay.md`; nothing here is pipeline-executed.

1. **Export (R6):** run `replay_export_sample` on PROD; copy the file out and keep it.
2. **Baseline (R7):** copy the sample to DEV and replay on 0.13.1 before any DEV tag moves.
3. **Release (R10):** move `[Unreleased]` to `[0.14.0]`, merge, tag `v0.14.0`, and wait for the image build.
4. **DEV rollout (R11):** gitops MR bumping DEV app and Dask to 0.14.0; pause app auto-sync, roll Dask first, then resume; wait for `Dask cluster verified`.
5. **Candidate and compare (R7, R8):** replay the same sample; compare; re-run for any transient break; apply KTD5 to a persistent identity flag.
6. **PROD MR (R9, R11):** PROD overlays to 0.14.0, the report, and an explanation for every group and flag, with coverage statements for TNS (unit tests) and ingest (U3 fixtures); cut the PROD tag; apply `dask-prod` first, then the other three Applications; verify.
7. **Patch (R10):** if lsdb 0.11.x ships before step 6, re-pin in a patch release and redo steps 3-6 for DEV and the candidate.

## Verification Contract

| Gate | Command or check | Applies to |
|---|---|---|
| Lock resolves and drift is clean | `pip-compile --strip-extras --upgrade-package db-dtypes ...` produces the five-line lock diff; lock-drift CI passes | U1 |
| Full suite under pandas 3 | `docker compose --env-file docker/.env -f docker/docker-compose.yaml build celery-worker`, then `... run --rm --no-deps celery-worker sh -c 'pip install -q -r requirements.dev.txt && python -m pytest'` | U1-U3 |
| Formatting | `black` on new files only (existing files are not black-formatted; do not reformat them) | U2, U3 |
| Migrations | `python manage.py makemigrations --check` reports no changes | all |

## Definition of Done

- U1-U4 are complete, with the new tests passing under pandas 3 in-container and CI green on the PR.
- The lock diff is limited to the five KTD1 lines.
- No doc still says replay explanations belong in the app PR.
- The PR body lists the Rollout Contract as the maintainer's remaining steps.
- No experimental or abandoned code remains in the diff.

