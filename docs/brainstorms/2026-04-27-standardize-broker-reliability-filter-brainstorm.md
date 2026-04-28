# Brainstorm: Standardize Broker Alert Filtering on `reliability >= 0.6`

**Date:** 2026-04-27
**Status:** Decisions captured; design doc updated; ready for `/ce-plan`

## Problem

Today the three configured brokers apply mismatched (or no) filters before alerts reach our ingest services:

- **ANTARES** (filter supplied to ANTARES): SNR + boolean-flag criteria; no reliability check.
- **Lasair** (filter created on Lasair web UI): `latestR > 0.6`, where `latestR` is the Lasair-side alias for the LSST `reliability` field on the latest diaSource.
- **Pitt-Google** (consumer in this repo): no quality filter; only an attribute filter requiring `diaObject_diaObjectId`.

We want one shared rule: every broker delivers only alerts whose **latest diaSource** has `reliability >= 0.6`. Reliability is the LSST DM real/bogus score added in February 2024 to the Alert Production pipeline by `lsst.meas.transiNet.RBTransiNetTask` (RBTransiNet ML model, "RB" = Real/Bogus). It populates the `reliability` column on the transformed DiaSource catalog and APDB DiaSource table. The field was previously called `spuriousness` and renamed via ticket DM-39378.

## Decisions

1. **Threshold**: `reliability >= 0.6` on the latest diaSource for a diaObject. Initial standard; admits candidates while rejecting the majority of artifacts.
2. **Env var**: `MIN_DIASOURCE_RELIABILITY`, default `0.6`. Broker-agnostic name so future broker clients added to this codebase consume the same variable.
3. **ANTARES filter (supplied upstream)**: target state is the single rule `latest diaSource reliability >= 0.6`. Existing SNR + flag criteria are removed from the design doc. The supplied ANTARES filter expression must be re-issued upstream to match. Operational follow-up.
4. **Lasair filter (Lasair web UI)**: change `latestR > 0.6` to `latestR >= 0.6` to align operator. The existing `reliability_moderate` filter must be re-created on the Lasair web UI to match. Operational follow-up. Topic name (`lasair_366SCiMMA_reliability_moderate`) stays — "moderate" still describes the threshold range.
5. **Pitt-Google filter (this repo)**: server-side at Google Cloud Pub/Sub via a JavaScript Single Message Transform (SMT) User-Defined Function (UDF), attached to the subscription via `pittgoogle.pubsub.Subscription.touch(smt_javascript_udf=...)`. The UDF reads `data.diaSource.reliability` from the JSON payload and drops messages that fail the predicate before they reach our subscriber. The threshold is interpolated from `MIN_DIASOURCE_RELIABILITY` into the UDF source at subscription-touch time. This aligns Pitt-Google with ANTARES and Lasair as a true upstream filter and saves ingress on alerts that fail. The earlier framing — that Pub/Sub cannot inspect message body content — was true of the older `attribute_filter` mechanism but does not apply to SMT UDFs.

## What changes (this brainstorm)

- **`scimma_crossmatch_service_design.md`** — updated in this brainstorm:
  - New §2.2 "Broker Filter Standard" defining the rule, reliability provenance, and the `MIN_DIASOURCE_RELIABILITY` env var.
  - §2.1 A (ANTARES filter) rewritten to a single reliability rule.
  - §2.1 B2 (Lasair filter) operator changed `>` → `>=`; rationale text updated.
  - §2.1 B3 (Pitt-Google ingest) gains a reliability-filter bullet.
  - §4.4 (Pitt-Google interface) consumer code example, Filtering paragraph, and env-var table updated.
  - "Comparison with ANTARES filter" paragraph removed — filters are now identical.
  - §10 Resolved entry for Lasair refreshed.

## What changes (next: `/ce-plan` + execution)

- **`crossmatch/brokers/pittgoogle/consumer.py`**: build the SMT JavaScript UDF source from `MIN_DIASOURCE_RELIABILITY`, pass it to `subscription.touch(smt_javascript_udf=...)` at startup. No Python predicate in `msg_callback` is needed (the UDF drops failing messages before delivery). Planning needs to verify the SMT UDF return convention for "drop this message" against Google's spec — pittgoogle docs do not show a concrete example. Planning also needs to confirm whether `touch()` updates the UDF on existing subscriptions or only sets it at creation time; if the latter, threshold changes will require subscription re-creation as a deploy step.
- **`docker/.env.example`, `docker/.env`**: add `MIN_DIASOURCE_RELIABILITY=0.6`.
- **`docker/docker-compose.yaml`**: wire `MIN_DIASOURCE_RELIABILITY` onto the ingest service env.
- **`kubernetes/charts/crossmatch-service/templates/_helpers.yaml`**: add `MIN_DIASOURCE_RELIABILITY` to the standard env list with a default.
- **Operational follow-ups**: re-create the Lasair `reliability_moderate` UI filter at `>= 0.6`; re-issue the ANTARES filter to use the single reliability rule.

## Out of scope

- Changing the threshold value (it is an env var; no code change required to adjust later).
- Logic for selecting "latest diaSource" within an alert envelope: in current LSST alert envelopes the alert's primary `diaSource` is the latest detection by construction. If that ever changes, `consumer.py` will need explicit selection logic — flagged for future review but not pre-emptively implemented.
- Telemetry or metrics on filter drop rates. Worth adding later if drop volumes need observability.
