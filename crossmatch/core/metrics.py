"""Golden-signal Prometheus instruments for the crossmatch service (U6).

Two exposition contexts share the same instrument definitions:

* Broker consumers (``run_*_ingest.py``) are single long-running processes.
  They call :func:`start_metrics_server`, which serves the default in-process
  registry, and emit the ingest counter + last-success gauge through
  ``brokers.ingest_alert``.

* Celery prefork workers fork child processes, so a single in-process registry
  would report only one child. When ``PROMETHEUS_MULTIPROC_DIR`` is set the
  instruments transparently use ``prometheus_client`` multiprocess mode: each
  child writes to the shared directory and the worker parent serves the
  aggregated view (see :func:`render_metrics` / :func:`start_metrics_server`).
  ``crossmatch/project/celery.py`` starts the parent server in ``worker_init``
  and cleans up dead children in ``worker_process_shutdown``.

The instruments are defined once at import; multiprocess vs. single-process is
decided at server/exposition time by the presence of ``PROMETHEUS_MULTIPROC_DIR``,
so the same module works unchanged in both contexts.
"""

import os
import time
from typing import Tuple

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
    multiprocess,
    start_http_server,
)

# --- Golden-signal instruments ---------------------------------------------

#: Alerts accepted by ``ingest_alert`` per broker. ``result`` is ``new`` for a
#: first delivery or ``duplicate`` for a repeat delivery already recorded.
ALERTS_INGESTED = Counter(
    "crossmatch_alerts_ingested_total",
    "Alerts ingested, by broker and result (new|duplicate).",
    ["broker", "result"],
)

#: Unix timestamp of the most recent successful ingest per broker. A freshness
#: signal: staleness means a consumer has stopped making progress. ``max`` mode
#: takes the latest value across processes under multiprocess exposition.
INGEST_LAST_SUCCESS = Gauge(
    "crossmatch_alert_last_success_timestamp_seconds",
    "Unix time of the last successful ingest, per broker.",
    ["broker"],
    multiprocess_mode="max",
)

#: Crossmatch batches completed by the Celery worker, by ``result``
#: (completed|failed).
CROSSMATCH_BATCHES = Counter(
    "crossmatch_batches_total",
    "Crossmatch batches processed, by result (completed|failed).",
    ["result"],
)

#: Catalog matches written, by catalog. Emitted per catalog in crossmatch_batch.
CROSSMATCH_MATCHES = Counter(
    "crossmatch_matches_total",
    "Catalog matches written, by catalog.",
    ["catalog"],
)

#: Catalogs skipped in a batch after their reads persistently failed (retries
#: exhausted), by catalog. A sustained rate means that catalog's source is down
#: and its matches are being silently dropped from published crossmatches.
CATALOG_SKIPS = Counter(
    "crossmatch_catalog_skips_total",
    "Catalogs skipped after read failure, by catalog.",
    ["catalog"],
)

#: Per-object crossmatch record writes that did not happen, by ``reason``
#: (build_failed|missing_table|write_failed). The batch still commits MATCHED;
#: the affected objects read as provenance "not recorded".
CROSSMATCH_RECORD_FAILURES = Counter(
    "crossmatch_record_failures_total",
    "Per-object crossmatch record writes skipped or failed, by reason.",
    ["reason"],
)

#: Notifications published to a destination, by ``result`` (success|failure).
NOTIFICATIONS_PUBLISHED = Counter(
    "crossmatch_notifications_published_total",
    "Notifications published, by result (success|failure).",
    ["result"],
)


# --- Emission helpers -------------------------------------------------------


def record_ingest_success(broker: str, result: str) -> None:
    """Record one successfully processed alert for ``broker``.

    Increments the ingest counter under ``result`` (``new`` or ``duplicate``)
    and advances the last-success gauge to now. Called only after the ingest
    completes without error, so a failed ingest never advances the gauge.

    Args:
        broker: Broker name (e.g. ``antares``, ``lasair``, ``pittgoogle``).
        result: ``new`` for a first delivery, ``duplicate`` for a repeat.
    """
    ALERTS_INGESTED.labels(broker=broker, result=result).inc()
    INGEST_LAST_SUCCESS.labels(broker=broker).set(time.time())


def _multiprocess_registry() -> CollectorRegistry:
    """A registry that aggregates the multiprocess metric files on disk."""
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def render_metrics() -> Tuple[str, bytes]:
    """Render the current metrics as Prometheus text.

    Uses the multiprocess-aggregating registry when ``PROMETHEUS_MULTIPROC_DIR``
    is set (Celery workers), otherwise the default in-process registry
    (consumers).

    Returns:
        A ``(content_type, payload)`` pair suitable for an HTTP response.
    """
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        payload = generate_latest(_multiprocess_registry())
    else:
        payload = generate_latest()
    return CONTENT_TYPE_LATEST, payload


def start_metrics_server(port: int = None) -> int:
    """Start a background HTTP server exposing ``/metrics``.

    In multiprocess mode the server aggregates across all worker children; in
    single-process mode it serves the default registry. Non-blocking: the
    server runs on a daemon thread.

    Args:
        port: TCP port to listen on. Defaults to the ``METRICS_PORT`` env var,
            or 9110 if unset.

    Returns:
        The port the server bound to.
    """
    if port is None:
        port = int(os.environ.get("METRICS_PORT", "9110"))
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        start_http_server(port, registry=_multiprocess_registry())
    else:
        start_http_server(port)
    return port
