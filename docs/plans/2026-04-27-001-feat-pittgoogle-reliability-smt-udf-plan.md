---
title: 'feat: Add server-side reliability filter for Pitt-Google via SMT JS UDF'
type: feat
status: active
date: 2026-04-27
origin: docs/brainstorms/2026-04-27-standardize-broker-reliability-filter-brainstorm.md
---

# feat: Add server-side reliability filter for Pitt-Google via SMT JS UDF

## Overview

Apply the broker filter standard `reliability >= 0.6` (defined in `scimma_crossmatch_service_design.md` §2.2) to the Pitt-Google ingest path. The filter runs **server-side at Google Cloud Pub/Sub** as a JavaScript Single Message Transform (SMT) User-Defined Function (UDF) attached to our subscription, so alerts that fail are never delivered to our subscriber. The threshold is configurable via a broker-agnostic env var, `MIN_DIASOURCE_RELIABILITY` (default `0.6`), wired through Docker Compose and the Helm chart.

This plan also covers the planning-time discovery that `pittgoogle.pubsub.Subscription.touch()` is **create-only-if-missing** for existing subscriptions and silently ignores `smt_javascript_udf` on a no-op touch — so the consumer must additionally call the underlying `google.cloud.pubsub_v1.SubscriberClient.update_subscription()` to keep the UDF in sync after threshold changes.

---

## Problem Frame

The brainstorm (see origin) standardized broker filtering on `reliability >= 0.6` of the latest diaSource. ANTARES and Lasair filter expressions are managed outside this codebase. Pitt-Google is the only broker whose filter lives in this repo; today its only filter is a Pub/Sub *attribute* filter requiring `diaObject_diaObjectId` (`crossmatch/brokers/pittgoogle/consumer.py:88`), with no quality check.

Pub/Sub's older `attribute_filter` cannot inspect message body content, but the newer Single Message Transform (SMT) feature — exposed by `pittgoogle.pubsub.Subscription.touch(smt_javascript_udf=...)` — runs JavaScript UDFs that can read the JSON payload. Moving the filter to an SMT UDF keeps Pitt-Google symmetric with ANTARES and Lasair as a true upstream filter and saves Pub/Sub ingress on alerts that fail (see origin: `docs/brainstorms/2026-04-27-standardize-broker-reliability-filter-brainstorm.md`).

---

## Requirements Trace

- R1. The Pitt-Google consumer applies `reliability >= 0.6` to the latest diaSource of every alert it forwards, with the threshold sourced from `MIN_DIASOURCE_RELIABILITY` (default `0.6`).
- R2. Alerts whose latest diaSource has `reliability` missing, null, or below threshold are dropped *before* delivery to our subscriber (server-side at Pub/Sub), not after.
- R3. The threshold env var name is `MIN_DIASOURCE_RELIABILITY` (broker-agnostic, per origin decision #2), so future broker clients added under `crossmatch/brokers/<broker>/` consume the same variable.
- R4. Threshold changes flow through redeploys without requiring operators to delete / recreate the subscription (preserves backlog and ack-deadline state).
- R5. The new env var is wired through both Docker Compose (local dev) and the Helm chart (cluster deploy) with the documented default `0.6`.

---

## Scope Boundaries

- Operational re-issue of the ANTARES filter expression at `lsst_diaSource_reliability >= 0.6` — **manual broker-side work**, not in this plan.
- Re-creation of the Lasair `reliability_moderate` UI filter at `latestR >= 0.6` — **manual broker-side work**, not in this plan.
- Any change to the catalog payload columns work on the same branch — **separate PI-review thread**, tracked in `docs/brainstorms/2026-04-27-payload-columns-by-keyword-brainstorm.md`.
- Logic to select "the latest diaSource" within an alert envelope — current LSST envelopes deliver the latest detection as the primary `diaSource` by construction. Flagged for future review only if that contract changes (origin: Out of scope #2).
- Telemetry / metrics on filter drop rates — worth adding later if drop volumes need observability (origin: Out of scope #3).
- Changing the threshold value beyond `0.6` — already supported by the env var; no code change required to adjust later.

---

## Context & Research

### Relevant Code and Patterns

- `crossmatch/brokers/pittgoogle/consumer.py` — current Pitt-Google consumer; `consume_alerts()` at line 61 sets up `Topic` + `Subscription` and calls `subscription.touch(attribute_filter=...)` at line 88. Reconnect loop with exponential backoff already in place; the new SMT-attach logic must live inside that loop so a failed `update_subscription` triggers a reconnect.
- `crossmatch/project/settings.py:194-200` — Pitt-Google settings block (`PITTGOOGLE_TOPIC`, `PITTGOOGLE_SUBSCRIPTION`, `PITTGOOGLE_PUBLISHER_PROJECT`). Pattern for new setting: `os.environ.get(...)` for strings; `float(os.getenv(..., 'default'))` for numbers (see `CROSSMATCH_RADIUS_ARCSEC` at line 27).
- `docker/docker-compose.yaml:118-120` — existing `PITTGOOGLE_*` env wiring on the ingest-bearing service; extend the same block.
- `docker/.env.example:52-54` and `docker/.env:49-51` — existing `PITTGOOGLE_*` defaults; extend with the new var.
- `kubernetes/charts/crossmatch-service/templates/_helpers.yaml` — env-block pattern: each broker has a `{{- define "<broker>.env" -}}` block (see `pittgoogle.env` block). Broker-agnostic vars belong in a new shared block, not under `pittgoogle.env`, since the variable is intentionally broker-agnostic per origin decision #2.
- `kubernetes/charts/crossmatch-service/values.yaml` — pattern for adding a new top-level config block (see `pittgoogle_consumer:`). A broker-agnostic `broker_filter:` block at the top level fits the variable's scope.

### Institutional Learnings

- `docs/solutions/` does not exist in this repo, so no prior captured learnings are applicable.

### External References

- **Google Cloud Pub/Sub — Single Message Transforms (UDFs) overview**: https://cloud.google.com/pubsub/docs/smts/udfs-overview — defines UDF function signature `function fn(message, metadata)`, return contract (`return null` to drop, `return message` to pass through), and sandbox limits (20 KB code, 500 ms exec, ECMAScript built-ins only, no `require` / Node APIs).
- **Google Cloud Pub/Sub — MessageTransform REST reference**: https://cloud.google.com/pubsub/docs/reference/rest/v1/MessageTransform — schema for the `message_transforms` field used by `update_subscription`.
- **pittgoogle-client `Subscription.touch()` source**: https://github.com/mwvgroup/pittgoogle-client/blob/main/pittgoogle/pubsub.py — confirmed via source read that on existing subscriptions the `smt_javascript_udf` argument is silently ignored with a warning.
- The LSST alert payload from the `lsst-alerts-json` topic is JSON-serialized at the publisher. Whether the SMT UDF receives `message.data` as base64 (default) or as a UTF-8 string (`STRING_MODE`) must be verified before writing the parse line — see Open Questions § Deferred to Implementation.

---

## Key Technical Decisions

- **Filter location: server-side via SMT JS UDF.** Carried from origin decision #5. Aligns Pitt-Google with ANTARES/Lasair as a true upstream filter and saves ingress on dropped alerts.
- **UDF source lives inline in `crossmatch/brokers/pittgoogle/consumer.py` as a Python f-string.** The UDF is ~15 lines of JS; a separate `.js` file would add a packaging concern (loading a sibling file at runtime) without commensurate benefit. Inline keeps the threshold interpolation point obvious next to the only call site.
- **Always-update via underlying `SubscriberClient.update_subscription()` after every `touch()`.** Because pittgoogle's `touch()` is create-only-if-missing and silently ignores SMT on existing subscriptions, a redeploy with a new threshold would otherwise leave the old UDF in place. Always calling `update_subscription` with the desired `message_transforms` plus `update_mask=FieldMask(paths=["message_transforms"])` keeps the subscription's UDF in sync and preserves backlog and ack-deadline state. Threshold changes flow through redeploys with no operator intervention.
- **`MIN_DIASOURCE_RELIABILITY` lives at the top level of `crossmatch/project/settings.py`, not inside the Pitt-Google block.** It is broker-agnostic by design (origin decision #2). Place it in a new "Broker filter standard" comment block above the per-broker blocks.
- **Helm: new top-level `broker_filter:` block in `values.yaml`, new `{{- define "broker_filter.env" -}}` block in `_helpers.yaml`.** Same broker-agnostic rationale as above; do not nest it under `pittgoogle_consumer:`.

---

## Open Questions

### Resolved During Planning

- **UDF return convention for "drop this message"**: `return null;` drops, `return message;` passes through. Source: Google Cloud Pub/Sub SMT UDF docs.
- **Sandbox limits**: 20 KB code, 500 ms execution per message, ECMAScript built-ins only. UDF source size is trivially within the 20 KB limit. Execution time is dominated by `JSON.parse` on the alert envelope; LSST DPDD alerts can be hundreds of KB with long `prvDiaSources` and embedded cutout data, so the 500 ms claim is **not asserted but deferred for verification on first deploy** (see Open Questions § Deferred to Implementation).
- **Whether `touch()` updates SMT on existing subscriptions**: No — it is create-only-if-missing and silently warns if SMT args are passed for an existing subscription. The plan addresses this with an explicit `update_subscription()` call after `touch()`.
- **UDF source location**: inline Python f-string in `crossmatch/brokers/pittgoogle/consumer.py`.
- **Float interpolation format**: `repr(threshold)` (lossless), not `f"{threshold:g}"` (silent truncation past 6 sig figs).

### Deferred to Implementation

- **Operational observability + rollout safety** — deferred to a separate operational PR, not this plan. The gap is real: today the consumer has no visibility into UDF errors or drop rates, the rollout procedure is implicit, and no canary or rollback recipe is documented. The follow-up PR should add Cloud Monitoring alerts on `subscription/udf_error_count` (per-minute SMT errors) and `subscription/num_undelivered_messages`; capture a pre-change ingest-rate baseline + post-change comparison window; and document the rollback recipe (redeploy old `MIN_DIASOURCE_RELIABILITY`; verify via `gcloud pubsub subscriptions describe <name> --format='value(message_transforms)'`). Until that PR ships, the first deploy of this plan inherits a known silent-drop risk class — flagged here so operators are aware before promoting.
- **Exact unit-test mocking strategy for `pittgoogle.pubsub.Subscription`**: the tests need to verify both the `touch()` call and the subsequent `update_subscription()` call. The implementer will decide whether to mock at the `pittgoogle.pubsub.Subscription` class level, the underlying `google.cloud.pubsub_v1.SubscriberClient`, or both. The repo has no existing test for `consumer.py` to mirror; pick the cleanest seam.
- **Exact Pub/Sub IAM scope for `update_subscription`**: the existing service account already has permission to create subscriptions via `touch()`, which implies sufficient permissions; verify on first deploy by watching for `PermissionDenied` exceptions and adjust the service-account role if needed.
- **Verify SMT `message.data` encoding on `lsst-alerts-json` before writing the UDF body.** Google's SMT docs are read by reviewers as both "base64-encoded by default" and "UTF-8 when transforming"; the difference is real (base64 needs `atob` before `JSON.parse`; UTF-8 / `STRING_MODE` does not). Test path: attach a passthrough UDF (`function probe(message, metadata) { console.log(message.data.slice(0, 16)); return message; }`) to a non-prod subscription on the same topic, send one real alert, inspect the logged prefix — base64 alert JSON typically starts with `eyJ` (the encoded `{"`), raw JSON starts with `{"diaSource"` or similar. Pick the matching parse path (and configure `STRING_MODE` on the subscription if we want the simpler form). Ship behind this verification, not before it.
- **Verify SMT UDF execution-time budget on representative LSST envelopes.** SMT UDFs have a 500 ms hard limit per message. The dominant cost is `JSON.parse` on the alert payload, which can be hundreds of KB on alerts with long `prvDiaSources`. Measure parse latency on the largest expected payload (use the same probe UDF as the encoding test, or a synthetic large alert) before promoting to production. If the budget is tight, fall back to a `STRING_MODE` subscription with attribute-promoted reliability and switch to `attribute_filter`.
- **Verify `update_subscription` server-side idempotency on first deploy.** The plan relies on calling `update_subscription` with identical `message_transforms` being a no-op (no audit-log noise, no transform restart). This is plausible from REST-PUT semantics but not authoritatively documented. Watch GCP audit logs after the first redeploy of an unchanged threshold to confirm; if the call is observably non-idempotent, gate the call behind a "current matches desired" check inside `_ensure_smt_udf`.

---

## Implementation Units

- U1. **Add `MIN_DIASOURCE_RELIABILITY` setting**

**Goal:** Expose the broker-agnostic threshold as a Django setting available to any broker client in this repo.

**Requirements:** R1, R3.

**Dependencies:** None.

**Files:**
- Modify: `crossmatch/project/settings.py`

**Approach:**
- Add a new "Broker filter standard" section before the per-broker blocks (above the `# ANTARES streaming consumer` block at line ~178).
- Read the env var, cast, **and bound-check to `[0.0, 1.0]`** at settings import. Reliability is bounded by definition; out-of-range values (including `nan`, `inf`, negative, `> 1`) would either interpolate pathologically into the SMT UDF (causing JS `ReferenceError` at runtime → silent 100% drop) or produce a filter that admits or rejects everything. Fail fast at import:
  ```python
  from django.core.exceptions import ImproperlyConfigured

  _min_reliability = float(os.getenv('MIN_DIASOURCE_RELIABILITY', '0.6'))
  if not (0.0 <= _min_reliability <= 1.0):
      raise ImproperlyConfigured(
          f"MIN_DIASOURCE_RELIABILITY must be a finite float in [0.0, 1.0]; got {_min_reliability!r}"
      )
  MIN_DIASOURCE_RELIABILITY = _min_reliability
  ```
  The chained inequality also rejects `float('nan')` because `nan` comparisons return `False`.
- Add a one-line comment pointing at the design doc section: `# Broker filter standard — see scimma_crossmatch_service_design.md §2.2`.

**Patterns to follow:**
- `CROSSMATCH_RADIUS_ARCSEC` in `crossmatch/project/settings.py:27` for the `float(os.getenv(..., 'default'))` idiom.

**Test scenarios:**
- Test expectation: none — pure config addition; behaviour is exercised through U3.

**Verification:**
- `from project import settings; settings.MIN_DIASOURCE_RELIABILITY == 0.6` in a Django shell with no env override.
- Setting `MIN_DIASOURCE_RELIABILITY=0.75` yields `0.75`. A non-numeric value raises `ValueError`; out-of-range values (`-1`, `2`, `inf`, `nan`) raise `ImproperlyConfigured` (intended fail-fast).

---

- U2. **Add the SMT JavaScript UDF builder**

**Goal:** Provide a function that returns the JS UDF source string with the threshold interpolated, ready to attach to a Pub/Sub subscription.

**Requirements:** R1, R2.

**Dependencies:** U1.

**Files:**
- Modify: `crossmatch/brokers/pittgoogle/consumer.py`
- Test: `crossmatch/brokers/pittgoogle/test_consumer.py` (create — no existing test file for this module)

**Approach:**
- Add a module-level helper, `_build_reliability_udf(threshold: float) -> str`, that returns a JS source string. The UDF:
  - Wraps body in `try { ... } catch (e) { return null; }` so a malformed payload drops the message rather than failing the whole transform.
  - **Verify `message.data` encoding before writing the parse line.** Google's SMT UDF runtime can deliver `message.data` either as base64 (default) or as a UTF-8 string when the subscription is configured in `STRING_MODE`. The plan's docs research surfaced both readings of the spec, so the implementation must verify against the live `lsst-alerts-json` subscription before committing to one. See Open Questions § Deferred to Implementation. If base64: `const data = atob(message.data); const payload = JSON.parse(data);`. If UTF-8 / `STRING_MODE`: `const payload = JSON.parse(message.data);` directly.
  - Reads `payload?.diaSource?.reliability` and checks `typeof score === "number" && score >= ${threshold}`.
  - `return null` (drop) on missing/null/below-threshold; `return message` (pass through) otherwise.
- Interpolate the numeric threshold with `repr(threshold)` (not `f"{...:g}"`). `repr()` is lossless for any IEEE-754 double — `f"{...:g}"` silently truncates to ~6 significant figures, which is fine for the documented values (`0.6`, `0.75`, `0.9`) but would silently corrupt any future threshold with more precision. Trade-off: `repr(0.1+0.2)` emits `'0.30000000000000004'`, which is valid JS and behaves correctly. The bounds check in U1 already rejects pathological values (`nan`, `inf`, out-of-range), so `repr()` only ever sees a finite float in `[0, 1]`.
- Add a module-level constant for the UDF function name (e.g. `_UDF_FUNCTION_NAME = "reliabilityFilter"`) so U3 can reference the same name when building the `JavaScriptUDF` proto.
- Keep the UDF code under the SMT 20 KB code-size limit — trivially satisfied by a ~15-line filter. The 500 ms execution-time limit is *not* trivially satisfied because parse cost scales with alert size; verify on first deploy (Open Questions § Deferred).

**Patterns to follow:**
- Other helper-style functions in `crossmatch/brokers/pittgoogle/consumer.py` use module-level `_underscore`-prefixed names (e.g. `_msg_callback`); follow the same convention.

**Test scenarios:**
- Happy path: `_build_reliability_udf(0.6)` returns a non-empty string containing `"0.6"` and a parse statement (whatever the encoding-verification step picks) and `return null` and `return message`.
- Happy path: the returned source declares a function whose name matches the **value** of `_UDF_FUNCTION_NAME` (e.g. `function reliabilityFilter(message, metadata) { ... }` if `_UDF_FUNCTION_NAME = "reliabilityFilter"`). The function uses the `function name(...)` named-declaration form, not arrow-function or `const name = function(...)` syntax — pittgoogle's first-create path parses the function name from the source via regex `function\s+([a-zA-Z0-9_]+)\s*\(`, so the source style must match.
- Edge case: `_build_reliability_udf(0.75)` interpolates `"0.75"` (no trailing zero); `_build_reliability_udf(0.9)` interpolates `"0.9"`.
- Edge case: `_build_reliability_udf(0.0)` (boundary value) interpolates `"0.0"` and produces a syntactically valid UDF.
- Edge case: returned source byte length is well under 20 KB (sanity check on the SMT limit).
- Predicate happy path (executable JS test, run via Node or `pytest` + `js2py`): with the UDF source from `_build_reliability_udf(0.6)`, a synthetic message whose payload `diaSource.reliability` is `0.7` returns the message; `0.5` returns `null`; exactly `0.6` returns the message (boundary, `>=`); `0.0` returns `null`.
- Predicate edge case: payload with `reliability: "0.7"` (string instead of number) returns `null` — `typeof === "number"` rejects strings; document this as intended behavior.
- Predicate edge case: payload with `reliability: null` returns `null`; payload with no `diaSource` field returns `null`; payload that is not a JSON object returns `null` (caught by `try`/`catch`).
- Predicate edge case: payload with `reliability: NaN` returns `null` (`>=` is false for NaN).

**Verification:**
- The returned string parses as valid JavaScript when run through any JS parser (manual check during development; not a test gate).
- The function name in the source matches `_UDF_FUNCTION_NAME` so U3's `JavaScriptUDF(function_name=...)` proto agrees.

---

- U3. **Attach SMT UDF at consumer startup with always-update**

**Goal:** On every consumer process startup, ensure the Pub/Sub subscription has the current SMT UDF attached, regardless of whether the subscription is being created for the first time or already exists.

**Requirements:** R1, R2, R4.

**Dependencies:** U1, U2.

**Files:**
- Modify: `crossmatch/brokers/pittgoogle/consumer.py`
- Test: `crossmatch/brokers/pittgoogle/test_consumer.py` (extend — created in U2)

**Approach:**
- Inside `consume_alerts()`'s reconnect loop:
  1. Build the UDF source via `_build_reliability_udf(settings.MIN_DIASOURCE_RELIABILITY)`.
  2. Pass it to `subscription.touch(attribute_filter=..., smt_javascript_udf=udf_source)` as before — this still creates the subscription with the UDF on first run.
  3. Then unconditionally call a new helper, `_ensure_smt_udf(subscription, udf_source)`, that uses the underlying `google.cloud.pubsub_v1.SubscriberClient` (accessible as `subscription.client` — a documented `@property` on pittgoogle's `Subscription`) to call `update_subscription`. Use the proto form so the kwarg types are explicit:
     ```python
     from google.protobuf.field_mask_pb2 import FieldMask
     from google.pubsub_v1.types import (
         JavaScriptUDF, MessageTransform, Subscription as SubscriptionProto,
     )

     subscription.client.update_subscription(
         subscription=SubscriptionProto(
             name=subscription.path,
             message_transforms=[
                 MessageTransform(
                     javascript_udf=JavaScriptUDF(
                         code=udf_source,
                         function_name=_UDF_FUNCTION_NAME,
                     ),
                 ),
             ],
         ),
         update_mask=FieldMask(paths=["message_transforms"]),
     )
     ```
     This is the canonical SDK call shape — `update_mask` requires a `FieldMask` proto, not a Python list. Setting an identical `message_transforms` is observed-idempotent server-side; flagged in Open Questions § Deferred for verification on first deploy (audit-log noise).
- Log the threshold being applied at startup so it is visible in operations: e.g. `logger.info("Attaching Pitt-Google reliability filter", min_reliability=settings.MIN_DIASOURCE_RELIABILITY)`.
- Failures from `update_subscription` (e.g. `PermissionDenied`, transient network error) bubble up to the existing `except Exception` block in the reconnect loop, which logs and backs off — same recovery path as a `touch()` failure today.
- Do not add a Python predicate in `_msg_callback`. The SMT UDF is the only filter for reliability; a defensive client-side check would re-introduce duplication explicitly rejected during the brainstorm.

**Execution note:** Implement test-first for `_ensure_smt_udf` — the always-update behaviour is the load-bearing change here, and the test verifies it is invoked even when the subscription already exists.

**Patterns to follow:**
- The existing reconnect loop with exponential backoff in `consume_alerts()` (lines 79–99). Slot the new logic before `consumer.stream()` so failures retry with the same cadence.
- `pittgoogle.pubsub.Subscription` source (read in U2 research) for how to access `subscription.client`.

**Test scenarios:**
- Happy path (new subscription): mock pittgoogle so `touch()` does the create path; assert `_ensure_smt_udf` is then called and that `update_subscription` is invoked with the expected `message_transforms` payload.
- Happy path (existing subscription): mock pittgoogle so `touch()` hits the "already exists" path and silently ignores the UDF arg; assert `_ensure_smt_udf` is still called and `update_subscription` is invoked — this is the regression that motivated the always-update decision.
- Threshold interpolation: with `settings.MIN_DIASOURCE_RELIABILITY=0.75`, the UDF source passed to `update_subscription` contains `0.75`.
- Error path: `update_subscription` raises `PermissionDenied`; the exception propagates and the reconnect loop sleeps for `_BACKOFF_INITIAL` seconds and retries — verifies we did not silently swallow IAM errors.
- Integration: `_msg_callback` does **not** check `reliability` (negative test — the filter must not be duplicated client-side). Run through a synthetic alert with `reliability < 0.6` and confirm the callback ingests it (because the test environment bypasses the real SMT UDF).
- Logging: at consumer start, the configured threshold is logged once.

**Verification:**
- A consumer process started against a fresh subscription has the SMT UDF attached on first run.
- A consumer process started against an existing subscription with a stale UDF (e.g. previous threshold `0.5`) updates the SMT to the current threshold without recreating the subscription.

---

- U4. **Wire `MIN_DIASOURCE_RELIABILITY` through deployment manifests**

**Goal:** Make the new env var available to the consumer in both local-dev (Docker Compose) and cluster (Helm) deployments, with the documented default `0.6`.

**Requirements:** R3, R5.

**Dependencies:** U1 (the setting must exist for the env var to have an effect).

**Files:**
- Modify: `docker/.env.example`
- Modify: `docker/.env`
- Modify: `docker/docker-compose.yaml`
- Modify: `kubernetes/charts/crossmatch-service/values.yaml`
- Modify: `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`

**Approach:**
- **Docker Compose:**
  - Add `MIN_DIASOURCE_RELIABILITY=0.6` to `docker/.env.example` and `docker/.env` near the existing `PITTGOOGLE_*` block, with a section comment indicating broker-agnostic scope.
  - In `docker/docker-compose.yaml`, add `MIN_DIASOURCE_RELIABILITY: "${MIN_DIASOURCE_RELIABILITY:-0.6}"` to the env block of every service that may run a broker consumer (today: the ingest-bearing service near the existing `PITTGOOGLE_*` lines around line 118; verify and extend to the worker/celery-beat services if they share the same env mount).
- **Helm chart:**
  - In `kubernetes/charts/crossmatch-service/values.yaml`, add a new top-level block `broker_filter:` with `min_diasource_reliability: "0.6"`. Keep at the top level (alongside `antares_consumer`, `pittgoogle_consumer`, `crossmatch:`, etc.) — the value is broker-agnostic.
  - In `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`, add a new `{{- define "broker_filter.env" -}}` block emitting the env var: `- name: MIN_DIASOURCE_RELIABILITY` / `value: {{ .Values.broker_filter.min_diasource_reliability | quote }}`.
  - Reference the new block from the Pitt-Google consumer Deployment / StatefulSet (the only consumer in this codebase that consumes the variable today). ANTARES and Lasair StatefulSets do not need it — those broker filters live outside this codebase. If a future broker consumer in this repo needs the same filter, include the block there too.
  - Pin `pittgoogle_consumer.replicaCount: 1` (today's default) and add an inline values comment noting that the consumer must run at `replicas: 1` so subscription provisioning is single-writer. Briefly note the rolling-deploy crossover: during a rolling restart a window of seconds exists where the terminating and starting pod can both call `update_subscription`; Pub/Sub's last-write-wins on `message_transforms` makes the new pod's value the eventual state.
- **Cast precision:** the value is quoted in YAML and re-parsed as a float in `settings.py`. `"0.6"` → `0.6` is exact. No floating-point representation concerns at the manifest level.

**Patterns to follow:**
- For docker compose: the existing `PITTGOOGLE_TOPIC: "${PITTGOOGLE_TOPIC:-lsst-alerts-json}"` line at `docker/docker-compose.yaml:118`.
- For Helm helpers: the existing `{{- define "pittgoogle.env" -}}` block in `_helpers.yaml`.
- For Helm values: the existing top-level `pittgoogle_consumer:` block in `values.yaml`.

**Test scenarios:**
- Test expectation: none — config-only changes. Behaviour verified end-to-end by U3 tests against the Python setting and by deploy-time smoke checks (see Verification).

**Verification:**
- `docker compose config` (or equivalent) shows `MIN_DIASOURCE_RELIABILITY=0.6` on the consumer service.
- `helm template ./kubernetes/charts/crossmatch-service` shows `MIN_DIASOURCE_RELIABILITY` env entry on the Pitt-Google consumer Deployment with value `"0.6"`.
- Setting `MIN_DIASOURCE_RELIABILITY=0.75` in `docker/.env` overrides the compose default and reaches `settings.MIN_DIASOURCE_RELIABILITY` inside the running consumer.

---

## System-Wide Impact

- **Interaction graph:** the Pitt-Google consumer entrypoint (`crossmatch/brokers/pittgoogle/consumer.py`, run via `crossmatch/entrypoints/run_pittgoogle_ingest.sh` and `crossmatch/project/management/commands/run_pittgoogle_ingest.py`) is the only Python caller affected. The ANTARES and Lasair consumers are untouched. The downstream `ingest_alert`/`normalize_pittgoogle` chain is unchanged.
- **Error propagation:** `update_subscription` failures bubble through the existing `except Exception` reconnect loop; behaviour is the same as a `touch()` failure today. No new error class introduced.
- **State lifecycle risks:** the always-update path means an SMT UDF deployment "owns" the subscription's `message_transforms`. If an operator changes the UDF out-of-band (e.g. via `gcloud pubsub subscriptions update`), the next consumer redeploy will overwrite it. This is intended; document briefly in the operational note below.
- **API surface parity:** `MIN_DIASOURCE_RELIABILITY` is broker-agnostic by design. The plan deliberately puts it at the top level of `settings.py` and at the top level of `values.yaml` so future broker clients consume the same variable without adding a new one.
- **Integration coverage:** the U3 "existing subscription" test scenario (see U3 → Test scenarios → "Happy path (existing subscription)") is the load-bearing integration check — without it, threshold changes would silently no-op against a deployed subscription.
- **Unchanged invariants:** the Pub/Sub `attribute_filter` `attributes:diaObject_diaObjectId` set at subscription creation is **not** changed. It remains the immutable attribute filter; the SMT UDF is a separate, mutable transform layer. The `msg_callback` ingest path (`normalize_pittgoogle` → `ingest_alert` → UPSERT into `alerts` → enqueue Celery task) is unchanged.

---

## Alternative Approaches Considered

- **File an upstream pittgoogle PR adding `update=True` to `Subscription.touch()`.** Would let the consumer go back to a single-line `touch()` call. Rejected for now — upstream review and release cadence is outside our control and the always-update workaround is a small local cost (~25 lines). Worth revisiting as a follow-up if pittgoogle's API direction aligns.
- **Move subscription provisioning to a Helm `pre-install`/`pre-upgrade` Job.** Cleaner separation of lifecycles (provisioning is a deploy-time singleton; the consumer becomes read-only). Rejected for this plan because it adds new Helm template surface and a new Docker entrypoint or management command for the one-shot job. Worth considering if we ever need replicas > 1 or want to decouple subscription state from consumer pod lifecycle.
- **Manage the subscription out-of-band via Terraform / `gcloud` in CI.** Removes the runtime write capability from the consumer entirely (smaller IAM blast radius). Rejected because the project does not currently use IaC for GCP subscriptions; introducing it for one resource adds a tooling dependency. Reasonable next step if the team adopts Terraform for GCP more broadly.

The chosen "always-update via `SubscriberClient.update_subscription` inside the consumer reconnect loop" is the lowest-friction option given the current tooling and the single-replica constraint on the Pitt-Google consumer Deployment.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| pittgoogle's `touch()` silently ignores `smt_javascript_udf` on existing subscriptions, so a naive port would leave threshold changes ineffective. | Always-update via `SubscriberClient.update_subscription()` after every `touch()` (U3). The U3 test scenario "existing subscription" guards the regression. |
| Service account may lack `pubsub.subscriptions.update` permission. | `pubsub.subscriptions.create` and `pubsub.subscriptions.update` are **separate** IAM permissions, both included in `roles/pubsub.editor` and above but not in `roles/pubsub.subscriber`. The plan prescribes a custom role with the minimum permissions, IAM-Condition-scoped to the single configured subscription resource — see Documentation / Operational Notes § "IAM scoping for `update_subscription`". Verify on first deploy by watching for `PermissionDenied`. |
| UDF execution time hits the 500 ms SMT limit. | Dominant cost is `JSON.parse` on alerts that can run into hundreds of KB. The plan does **not** assert the limit is "trivially satisfied"; instead the implementer measures parse latency on representative LSST envelopes during the encoding-verification step (Open Questions § Deferred). If the budget is tight, options include subscribing to a slimmer projection of the alert (if Pitt-Google offers one) or moving the reliability filter to a Pub/Sub server-side `attribute_filter` against a publish-time-promoted attribute. |
| Malformed alert payload causes UDF to throw, dropping the message. | Intended: a malformed alert cannot satisfy the predicate. The UDF wraps body in `try { ... } catch (e) { return null; }` so the failure mode is "drop", not "kill the transform". |
| LSST alert envelope semantics change so the alert's primary `diaSource` is no longer the latest detection. | Out of scope for this plan; the brainstorm flagged the same risk. The UDF would need explicit selection logic at that point; for now the assumption is documented in the design doc and origin. |
| Multi-replica race on `update_subscription` during rolling deploy. | The consumer Deployment is pinned to `replicas: 1` in `values.yaml` (codified, not just defaulted). During a rolling deploy a brief window exists where the old and new pod both issue `update_subscription`; Pub/Sub's last-write-wins semantics on the `message_transforms` field mean the new pod's value wins after rollover. Document explicitly in U4. |

---

## Documentation / Operational Notes

- **Threshold-change operations:** changing `MIN_DIASOURCE_RELIABILITY` and redeploying is sufficient; the consumer's always-update path will rewrite the SMT UDF on startup. No subscription deletion required.
- **Out-of-band SMT changes:** if anyone edits the subscription's `message_transforms` outside this codebase (e.g. via `gcloud pubsub subscriptions update`), the next consumer process startup will overwrite it with the value derived from `MIN_DIASOURCE_RELIABILITY`. Treat the consumer as the source of truth.
- **IAM scoping for `update_subscription`:** the consumer service account needs `pubsub.subscriptions.update` (the always-update path) on top of the existing `pubsub.subscriptions.create`/`consume`. The plan prescribes a **resource-scoped custom IAM role** rather than the broad `roles/pubsub.editor`:
  - Permissions: `pubsub.subscriptions.create`, `pubsub.subscriptions.update`, `pubsub.subscriptions.get`, `pubsub.subscriptions.consume` (the existing read/ack-deadline operations the consumer already uses), `pubsub.topics.attachSubscription` if creating subscriptions on a topic owned by another project.
  - Scope via an IAM **Condition** binding the role to the resource `projects/<GOOGLE_CLOUD_PROJECT>/subscriptions/<PITTGOOGLE_SUBSCRIPTION>`. This prevents a compromised service-account key from mutating other subscriptions in the same GCP project (e.g. broker-side subscriptions or other tenants).
  - Confirm the binding is applied before the first deploy that includes the always-update path; surface `PermissionDenied` as a fast-fail in the consumer reconnect loop.
- **Operational follow-ups (carried from origin, NOT in scope of this plan):** re-issue the ANTARES filter expression at `lsst_diaSource_reliability >= 0.6`; re-create the Lasair `reliability_moderate` UI filter at `latestR >= 0.6`.

---

## Sources & References

- **Origin document:** `docs/brainstorms/2026-04-27-standardize-broker-reliability-filter-brainstorm.md`
- **Design doc (target state):** `scimma_crossmatch_service_design.md` §2.2 (broker filter standard) and §4.4 (Pitt-Google interface).
- Related code: `crossmatch/brokers/pittgoogle/consumer.py`, `crossmatch/project/settings.py`, `docker/docker-compose.yaml`, `kubernetes/charts/crossmatch-service/templates/_helpers.yaml`, `kubernetes/charts/crossmatch-service/values.yaml`.
- External docs:
  - https://cloud.google.com/pubsub/docs/smts/udfs-overview
  - https://cloud.google.com/pubsub/docs/smts/smts-overview
  - https://cloud.google.com/pubsub/docs/reference/rest/v1/MessageTransform
  - https://mwvgroup.github.io/pittgoogle-client/api-reference/pubsub.html
  - https://github.com/mwvgroup/pittgoogle-client/blob/main/pittgoogle/pubsub.py (source-read for `Subscription.touch()` semantics)
