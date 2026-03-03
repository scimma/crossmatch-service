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

### Added

- `structlog` added to Python dependencies.

### Fixed

- Replaced broken `rabbitmq.env` Helm helper (referenced non-existent `Values.rabbitmq.*`) with `valkey.env` injecting `VALKEY_SERVICE` / `VALKEY_PORT`.
- Updated all four container entrypoints and `docker-compose.yaml` to use `VALKEY_SERVICE` / `VALKEY_PORT`, harmonising env var naming across Docker, Kubernetes, and Django settings.
- Removed four dead `rabbitmq_*` variable lines from `celery.py`.
- Fixed `CELERY_IMPORTS` to reference `tasks.crossmatch` and `tasks.schedule` instead of the deleted `tasks.tasks` module, resolving `Received unregistered task of type 'crossmatch_alert'` errors in Celery workers.
- Fixed `initialize_periodic_tasks` management command to import `periodic_tasks` from `tasks.schedule` and write the correct task path (`tasks.schedule.query_heroic`) to the database, resolving `KeyError: 'tasks.tasks.query_heroic'` in celery-beat.

### Changed

- Deleted stale `tasks/tasks.py` (superseded by `tasks/crossmatch.py` and `tasks/schedule.py`).

## [0.0.0]

### Added

### Fixed

### Changed
