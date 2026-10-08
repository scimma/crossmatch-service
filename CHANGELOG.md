# Changelog

All notable changes to this application will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project (mostly) adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

- `Added` for new features.
- `Changed` for changes in existing functionality.
- `Deprecated` for soon-to-be removed features.
- `Removed` for now removed features.
- `Fixed` for any bug fixes.
- `Security` in case of vulnerabilities.

## [Unreleased]

### Fixed

- The TNS snapshot refresh no longer fails on a quiet hour. TNS serves the hourly delta for an hour with no new or changed objects as a file holding only its time-range line, with no header row; the parser treated that as a garbled export, so the refresh logged `tns_refresh_failed`, the snapshot epoch stopped advancing, and after `TNS_SNAPSHOT_MAX_AGE_SECONDS` TNS-name lookups answered "TNS unavailable" until the next full re-download. Such a file now parses to no records and the refresh advances the epoch; a file with content beyond that line but no header still fails.

## [0.16.0] - 2026-10-07

### Added

- Chat connector: a public, read-only MCP (Model Context Protocol) endpoint at `POST /mcp` lets researchers ask Claude.ai or ChatGPT about Rubin transients and get this service's answer in the chat, with no login. It serves three tools that answer from the same lookup code as the API: `lookup_rubin_transients` (TNS names and `diaObjectId`s, up to 100 identifiers per call), `search_rubin_transients_near_position` (RA/Dec in degrees, default radius 10 arcsec, at most `API_MAX_CONE_RADIUS_ARCSEC`, nearest first), and `describe_crossmatch_service` (catalogs and releases, crossmatch radius, brokers and reliability cuts, TNS snapshot freshness, newest alert, and service status). Each answer summarizes at most 20 objects, with their coincident sources grouped by catalog, the stored TNS association, provenance basis, and TNS, Lasair and ANTARES links; an oversized answer returns the first part, says it is truncated and how many results exist, and carries a ready-to-run API request for the full set. Every tool declares itself read-only, and an unanswerable question returns an explicit reason (unknown TNS name, no Rubin object for a TNS name, not in the service, crossmatch pending, TNS unavailable, service unavailable). The JSON-RPC transport is hand-written (no new dependency), with an optional signed `Mcp-Session-Id`, an `Origin` check, and one `mcp tool call` log line per call (tool, inputs, result counts, truncation, outcome, latency).
- Connector setup docs: `/llms.txt`, `/api-docs.md`, and the HTML `/api-docs` page share a "Connect from a chat assistant" section built by the docs builder, with the absolute connector URL, the paid-plan requirement, and short Claude.ai (custom connector) and ChatGPT (Developer mode, including the workspace-admin note) setup steps. `GET /api/describe` gains an additive `mcp` block naming the endpoint's path, method, transport, authentication (`none`), and tools, documented in the OpenAPI document.
- New settings for the chat connector: `MCP_MAX_IDENTIFIERS` (default 100), `MCP_MAX_OBJECTS` (20), `MCP_MATCHES_PER_CATALOG` (3), and `MCP_MAX_RESULT_CHARS` (30000) bound each answer; `MCP_API_BASE_URL` (`https://crossmatch.scimma.org`) is the host in the ready-to-run API request; `LASAIR_OBJECT_URL_TEMPLATE` (`https://lasair.lsst.ac.uk/objects/{diaObjectId}/`) and `ANTARES_OBJECT_URL_TEMPLATE` (`https://antares.noirlab.edu/loci?search={diaObjectId}`) build the broker links; `MCP_ALLOWED_ORIGINS` (empty) lists browser origins allowed to call `/mcp`; `MCP_SESSION_MAX_AGE_SECONDS` (86400) bounds a session ID's life; and `MCP_CLIENT_IP_HEADER` (`X-Real-Ip`), `MCP_PROVIDER_CIDRS` (`160.79.104.0/21`, Anthropic's egress range), `MCP_SESSION_RATE` (2/s burst 10 per provider session), `MCP_IP_RATE` (2/s burst 10 per other client IP), `MCP_PROVIDER_RATE` (10/s burst 50 for a provider range's session-less calls), `MCP_MAX_CONCURRENT` (6 tool calls in flight cluster-wide), and `MCP_MAX_CONCURRENT_PER_KEY` (2 per session or client) limit tool calls on the shared cache; a limited call is a tool error with a retry-after, not an HTTP 429. Each `CROSSMATCH_CATALOGS` entry gains an optional `key_columns` list, the few catalog values a chat answer shows per coincident source; each must be one of the catalog's `payload_columns`, checked at startup.
- Lookups at `detail=matches` and `detail=full` include a per-object `tns` block: the stored TNS association as of the snapshot it came from (`checked: false` or `null` means the object has not been checked against TNS, not that it has no TNS counterpart). The field is additive and documented in the OpenAPI document; no existing field changes.

### Changed

- The cone-search code gains an internal nearest-first listing order used by the chat connector; the public `GET /api/cone` order and paging are unchanged.
- The API request guard (per-request budget and database-unavailable handling) is refactored into a reusable `run_guarded` helper shared by the API views and the MCP tools, with no change in behavior.
- Deploy note: the gitops change routes `/mcp` to the web pods through its own Ingress and Traefik middleware (a generous per-IP ceiling; the fine-grained limits are the app's), rendered only when the API is public; passes the MCP settings to the web tier (set `MCP_API_BASE_URL` to the DEV host on DEV, and add OpenAI's published ChatGPT connector egress ranges to `MCP_PROVIDER_CIDRS`); and wires `TNS_BOT_*` credentials into celery-worker and celery-beat as optional secret refs, so it can deploy before the TNS secret is sealed. Until the maintainer seals TNS bot credentials on each cluster, TNS-name lookups answer "TNS unavailable". Confirm the Lasair and ANTARES link templates against the live sites before launch. The default cache now sets one-second Valkey socket and connect timeouts, so a stalled Valkey raises and the MCP limits fail open instead of hanging web threads; an unresolvable Valkey host can still take several seconds per call because name lookup is outside those timeouts.

### Fixed

- The `RefreshTnsSnapshot` periodic task now stays enabled across restarts: the ingest consumers' `locked_init` no longer turns it off, so the TNS snapshot keeps refreshing and TNS-name lookups stay available once credentials are present.

## [0.15.0] - 2026-10-01

### Added

- Object lookup by `diaObjectId`: `GET /api/objects/<diaObjectId>` for one object and `POST /api/lookup` for an ordered batch of tagged inputs (`id`, `position`, or `tns`). Every input produces exactly one result, in input order, echoing the input as sent; a malformed input is reported as `invalid_input` in its own result without failing the rest. Each object reports a fixed status code (`not_in_service`, `crossmatch_pending`, `coincident_sources`, `no_coincident_source`, `not_searched`), its per-catalog search outcomes, and its coincident sources. IDs are accepted as JSON integers or decimal strings, and every object carries `diaObjectId_str` beside the integer `diaObjectId` for JavaScript clients.
- Position queries: `GET /api/cone` pages every Rubin object the service has seen within a radius of a position (matched or not), bounded across pages by an `as_of` limit on ingest time (a bound, not a snapshot: an alert whose ingest commits mid-walk with an ingest time at or before `as_of` can still appear on a later page), and `position` inputs in `POST /api/lookup` search many positions in one request.
- TNS-name lookup: `GET /api/tns/<name>` (and `tns` inputs in `POST /api/lookup`) resolves a TNS designation such as `2026abc` or `SN 2026abc` against the local TNS snapshot and returns the Rubin objects around it; a name absent from a current snapshot reports `tns_name_not_found`, and a stale or missing snapshot reports `resolver_unavailable`.
- Per-object crossmatch provenance: from release 0.15.0 on, each crossmatch records, per object, the crossmatch radius, catalog releases, per-broker reliability cuts, per-catalog search outcome (checked against each catalog's HATS coverage map), and the brokers that had delivered the object. Lookups report it with each object and match, through a shared `provenance_sets` map; objects crossmatched before recording started report `not_recorded`, name the release that started recording, and are offered the current settings as a labeled best guess. The published Hopskotch payload is unchanged.
- Catalog-property filters and counts on the new queries: catalog-qualified `_min`/`_max` bounds (for example `gaia_dr3.parallax_over_error_min`) plus generic `catalog`, `separation_arcsec_max`, and `reliability_min`/`_max` filters mark every result `qualifies: true|false` with a reason instead of dropping inputs, and `response=count` returns counts by status and qualification without objects.
- `GET /api/status` (service and database availability, version, TNS-resolution availability; always `200`) and `GET /api/describe` (catalogs with releases, what `catalog_source_id` means per catalog, filterable fields with units, per-request limits, detail levels, and the reliability-cut table).
- Agent-facing documentation: a complete OpenAPI 3.1 document at `/openapi.json` (every operation, parameter, response field, status value, and error code, with units), `/llms.txt`, and the API reference as Markdown at `/api-docs.md`, all rendered from the same builders as the HTML `/api-docs` page.
- Every successful API response carries a service-level `provenance` block (service version, contract version, crossmatch radius, catalog releases, and per-broker reliability cuts), and every API error carries a fixed `code`, a `message`, the offending `param` or `params`, and `retryable`.
- New settings: `API_REQUEST_BUDGET_SECONDS` (default 5 s), `API_MAX_IDS` (1000), `API_MAX_POSITIONS` (200), `API_MAX_CONE_RADIUS_ARCSEC` (60), `API_MAX_OBJECTS_PER_POSITION` (100), and `API_MAX_OBJECTS_PER_REQUEST` (5000) bound each API request; `GAIA_RELEASE`, `DES_RELEASE`, `DELVE_RELEASE`, and `SKYMAPPER_RELEASE` label the catalog releases in service; `GAIA_FOOTPRINT_MOC_ORDER`, `DES_FOOTPRINT_MOC_ORDER`, `DELVE_FOOTPRINT_MOC_ORDER`, and `SKYMAPPER_FOOTPRINT_MOC_ORDER` report each catalog's coverage-map resolution; and `ANTARES_DECLARED_MIN_RELIABILITY` / `LASAIR_DECLARED_MIN_RELIABILITY`, each with a matching `_AS_OF` date, declare the reliability cuts those brokers enforce before alerts arrive (unset reports `not_declared`).
- Migration `0011` adds the provenance-set and per-object crossmatch-record tables. Migration `0012` builds an index on the lowercased TNS object name with `CREATE INDEX CONCURRENTLY`; if it is interrupted, drop the INVALID `core_tns_name_lower_idx` index on `tns_objects` and restart one ingest consumer to re-apply it.

### Changed

- `GET /api/recent-crossmatches` gains the response contract without changing any existing field: pages add the `provenance` block, an `as_of` pin, and `diaObjectId_str` beside each integer `diaObjectId`, and a page's `next_cursor` now pins the walk to alerts ingested at or before `as_of` (cursors issued before the upgrade keep working and continue the walk). Error bodies keep the `error` string with the same text and add `code`, `message`, `param`, and `retryable`. The endpoint now runs under the per-request budget, answering `400 query_too_expensive` when a request overruns it and `503 service_unavailable` with `Retry-After` when the database is unreachable; its page-size defaults are unchanged, so a very large page at `detail=full` can overrun the budget and should be requested smaller.
- `GET /api/recent-crossmatches` now returns `400 unsupported_parameter` for a filter-named parameter or `response`, which it previously ignored, so unfiltered results are never mistaken for filtered ones; every other unknown parameter is still ignored.
- The web tier runs gunicorn with threaded workers (`--worker-class gthread`, `WEB_THREADS`, default 4) and an explicit `--timeout` (`WEB_TIMEOUT`, default 30 s), so `/healthz` and the HTML pages are not queued behind slow API requests, and sets `DATABASE_CONNECT_TIMEOUT` (default 3 s) for the web tier only, so an unreachable database fails fast into a structured `503`.
- Deploy note: before promoting to PROD, the gitops repo needs one shared environment block for every provenance input (`MIN_DIASOURCE_RELIABILITY`, `CROSSMATCH_RADIUS_ARCSEC`, the HATS URLs, the catalog release overrides, and the declared ANTARES and Lasair cuts) included in the Pitt-Google consumer, Celery worker, beat, and web containers, so recorded and reported provenance agree; and a stricter edge rate limit for `api/lookup`, `api/cone`, and `api/tns`. Migrations `0011` and `0012` apply when the ingest consumers start.

## [0.14.0] - 2026-09-25

### Changed

- Upgrade LSDB from 0.10.4 to 0.11.0, which moves hats to 0.11.0, nested-pandas to 0.7.2, and pandas from 2.3.3 to 3.0.6 (lsdb 0.11's hats and nested-pandas require pandas 3); db-dtypes moves transitively from 1.6.0 to 1.7.1. numpy, pyarrow and dask are unchanged. The app and the Dask cluster must run the same image; validate with the replay before promoting to PROD (see `docs/runbooks/crossmatch-replay.md`).
- The crossmatch replay runbook no longer sets `CROSSMATCH_REPLAY_ENABLED` or `--image-tag` on the command line: the gitops chart now enables replay in the DEV pods only and sets `APP_VERSION` on every app pod, so a replay against PROD is refused regardless of what the operator types and each snapshot records the deployed image tag automatically.

## [0.13.1] - 2026-09-24

### Fixed

- `replay_export_sample` no longer hashes and sorts every qualifying row: each coverage category now walks its index from a seed-derived starting diaObjectId (wrapping around), so an export takes seconds on production-sized tables instead of running for many minutes. The same seed over the same data still reproduces the sample. A new `--statement-timeout` (default 120 s) cancels any single query that runs longer, so an export can never hold a long query on the PROD database.

## [0.13.0] - 2026-09-24

### Added

- Crossmatch replay tool for validating dependency upgrades without live alerts: `replay_export_sample` exports a read-only coverage sample of historical alerts (PROD), `replay_run` replays it through the production crossmatch compute step on the Dask cluster without writing or publishing anything (DEV only, gated by the new `CROSSMATCH_REPLAY_ENABLED` setting), and `replay_compare` diffs two snapshots into a grouped Markdown report. See `docs/runbooks/crossmatch-replay.md`.

### Changed

- `crossmatch_batch` now delegates its in-memory work to a shared `compute_crossmatch` step and persists through callbacks at the same points as before; batch behavior, write order, and metrics are unchanged. The Dask version-alignment check is callable without the startup fail-fast (`check_cluster_alignment`); the Celery startup guard behaves as before.

## [0.12.0] - 2026-08-19

### Added

- TNS (Transient Name Service) cross-link on published matches: when an alert's position coincides with a known TNS object (within `TNS_MATCH_RADIUS_ARCSEC`), the payload gains a `tns` block (name, object-page link, classification, redshift, separation, objid) plus a `tns_checked` / `tns_snapshot_epoch` enrichment indicator. Backed by a periodically refreshed local snapshot of TNS's public objects (new tables via migration `0010`); best-effort, so a missing or stale snapshot never blocks a crossmatch batch. Deploy note: the refresh task stays disabled until the TNS bot credentials (`TNS_BOT_ID` / `TNS_BOT_NAME` / `TNS_BOT_API_KEY`) are provisioned as a per-cluster secret.

## [0.11.0] - 2026-08-12

### Changed

- Upgraded `lsdb` to 0.10.4 (with aligned `hats` 0.10.4 and `nested-pandas` 0.6.10) across all pin sites; no serialization-critical package moved.

### Added

- Cluster-side lsdb/hats version guard in `core/dask.py`. `distributed`'s `get_versions()` omits lsdb, so an app/cluster skew was previously silent; the guard queries workers via `client.run` and fails fast on drift. Deploy note: the Dask cluster image tag must advance in lockstep with the app, or the guard CrashLoops on skew.

## [0.10.1] - 2026-08-10

### Added

- Footer reports (and, for a tagged release, links) the running version by wiring `APP_VERSION` from the deployed image tag, instead of showing `0.0.0`.

### Changed

- NSF acknowledgment wording updated to "findings and conclusions".

## [0.10.0] - 2026-08-10

### Added

- Web frontend (Django MVC) served alongside the API, deployed at `crossmatch[-dev].scimma.org/`.

## [0.9.1] - 2026-08-10

### Fixed

- Hardened database migration application at deploy time so migrations apply cleanly under the startup advisory lock (robust `locked_init`).

## [0.9.0] - 2026-07-22

### Added

- Alert payload retention: a periodic sweep ages out stored alert payloads past a configurable grace period.

## [0.8.0] - 2026-07-21

### Fixed

- Recover crossmatch batches killed mid-run: an interrupted batch (e.g. worker kill) is returned to a retryable state instead of being stranded.

## [0.7.0] - 2026-07-21

### Fixed

- Single-catalog resilience: one catalog failing (no spatial overlap or a read error) no longer discards the whole batch's matches; the batch continues across the remaining catalogs.

## [0.6.2] - 2026-07-20

### Fixed

- DES Y6 Gold and DELVE DR3 Gold catalog access over S3.

## [0.6.1] - 2026-07-20

### Fixed

- Correct accounting of asynchronous Hopskotch delivery so publish success and failure are tracked accurately.

## [0.6.0] - 2026-07-14

### Added

- Pagination for the recent-crossmatch read-model API.

## [0.5.0] - 2026-07-13

### Added

- Recent-crossmatch API endpoint exposing recent matches to the public science community.

## [0.4.0] - 2026-07-13

### Added

- Scientist-facing read model backing the public API.

## [0.3.3] - 2026-07-10

### Fixed

- Treat a catalog-read `FileNotFoundError` as a transient error and retry it.
- Recover batches stuck in QUEUED using `queued_at` timing.

## [0.3.2] - 2026-07-10

### Fixed

- Retry transient catalog-read errors during crossmatch.

## [0.3.1] - 2026-07-10

### Fixed

- Recycle broker-consumer database connections to avoid stale or closed-connection errors.

## [0.3.0] - 2026-07-06

### Added

- Application metrics instrumentation and a monitoring dashboard.

## [0.2.0] - 2026-06-30

### Added

- pytest-django + factory_boy test harness covering the crossmatch to notify pipeline (notify transition, MATCHED/notify ordering and atomicity, fail-loud crossmatch, payload/catalog validation, ingest idempotency, batch dispatch thresholds, stuck-QUEUED recovery).
- CI: a pytest workflow (builds the image, runs against Postgres) and a lock-drift guard.
- `requirements.lock` as the reproducible dependency source of truth.

### Changed

- Crossmatch now fails loud on catalog open/compute errors (the batch reverts to INGESTED and retries) instead of silently zero-matching.

## [0.1.3] - 2026-06-29

### Fixed

- Race where a notify could fire before the batch reached MATCHED.

## [0.1.2] - 2026-06-29

### Fixed

- Correct the NOTIFIED status transition.

## [0.1.1] - 2026-06-29

### Fixed

- Pin `hats` to 0.9.0 to match the Dask cluster and avoid version skew.

## [0.1.0] - 2026-06-12

Initial release of the crossmatch service.

### Added

- Alert ingestion and normalization from the ANTARES, Lasair, and Pitt-Google brokers, including auto-creation of the local Hopskotch topic in development.
- LSDB crossmatch (lsdb 0.9.0) of alert coordinates against HATS catalogs on a remote Dask cluster: Gaia DR3, DES Y6 Gold, DELVE DR3 Gold, and SkyMapper DR4.
- Catalog-specific published payloads with per-catalog column mapping.
- Match publication over Hopskotch (Kafka) via hop-client.
- Fail-fast Dask version check to catch app/cluster version skew.
- Kubernetes/Helm deployment charts and a GHCR container-image build/publish pipeline.
- `structlog` structured logging.

### Changed

- Version-pinned Python dependencies.
- Adopted Valkey (replacing RabbitMQ) for Celery, with a `VALKEY_SERVICE` / `VALKEY_PORT` env contract harmonized across Docker, Kubernetes, and Django settings.

### Fixed

- Register the correct Celery task modules (`tasks.crossmatch`, `tasks.schedule`) and remove the stale `tasks/tasks.py`, resolving unregistered-task errors in the workers and beat.
- Postgres init race condition on startup.
- diaSourceId reliability filtering.

[Unreleased]: https://github.com/scimma/crossmatch-service/compare/v0.16.0...HEAD
[0.16.0]: https://github.com/scimma/crossmatch-service/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/scimma/crossmatch-service/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/scimma/crossmatch-service/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/scimma/crossmatch-service/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/scimma/crossmatch-service/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/scimma/crossmatch-service/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/scimma/crossmatch-service/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/scimma/crossmatch-service/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/scimma/crossmatch-service/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/scimma/crossmatch-service/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/scimma/crossmatch-service/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/scimma/crossmatch-service/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/scimma/crossmatch-service/compare/v0.6.2...v0.7.0
[0.6.2]: https://github.com/scimma/crossmatch-service/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/scimma/crossmatch-service/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/scimma/crossmatch-service/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/scimma/crossmatch-service/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/scimma/crossmatch-service/compare/v0.3.3...v0.4.0
[0.3.3]: https://github.com/scimma/crossmatch-service/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/scimma/crossmatch-service/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/scimma/crossmatch-service/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/scimma/crossmatch-service/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/scimma/crossmatch-service/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/scimma/crossmatch-service/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/scimma/crossmatch-service/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/scimma/crossmatch-service/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/scimma/crossmatch-service/releases/tag/v0.1.0
