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

[Unreleased]: https://github.com/scimma/crossmatch-service/compare/v0.11.0...HEAD
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
