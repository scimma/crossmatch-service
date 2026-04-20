"""Verify Dask cluster version alignment and connect a Client per Celery worker.

When DASK_SCHEDULER_ADDRESS is set:

  - At master startup (`worker_init`, fires once before any fork), verify that
    the client's Python and key package versions match the Dask scheduler and
    at least one registered worker. Drift or timeout exits the master process
    non-zero so the pod surfaces as CrashLoopBackOff in Kubernetes.
    `WorkerShutdown` and an unhandled exception from a per-fork signal handler
    are both swallowed by Celery's billiard wrapper and result in an infinite
    respawn loop, so the check must run in the master where `sys.exit` works.

  - In each forked worker process (`worker_process_init`), construct a
    `dask.distributed.Client(address)`. The Client registers itself as the
    default scheduler for that process so LSDB `.compute()` calls offload to
    the cluster. Versions are already verified by this point; constructing the
    Client here is safe and correct.

See scimma_crossmatch_service_design.md §7.4 for why exact version pinning is
required and docs/plans/2026-04-20-001-feat-fail-fast-dask-version-check-plan.md
for the design.
"""

import os
import signal
import sys
import time

from celery.signals import worker_init, worker_process_init
from django.conf import settings

from core.log import get_logger

logger = get_logger(__name__)

# Packages compared across client / scheduler / workers. Names are dict keys
# under versions[<role>]['packages'] as produced by distributed.versions —
# all lowercase. Includes Dask's defaults plus numpy and pandas, which §7.4
# identifies as the actual root cause of cross-version pickle failures.
_VERSION_CHECK_PACKAGES = (
    'python',
    'distributed',
    'dask',
    'msgpack',
    'cloudpickle',
    'toolz',
    'tornado',
    'numpy',
    'pandas',
)

_BACKOFF_INITIAL = 1.0   # seconds
_BACKOFF_MAX = 10.0      # seconds


def _fail_fast():
    """Exit the celery master non-zero AND tear down the parent process.

    sys.exit alone doesn't exit the container in dev mode: the parent is
    `watchmedo auto-restart`, which keeps running after celery exits, so the
    bash entrypoint stays blocked and the container appears "up" with celery
    dead. SIGTERM to the parent kills watchmedo (dev) or bash (prod), which
    propagates to container exit and triggers docker compose's restart cap or
    K8s CrashLoopBackOff.
    """
    try:
        os.kill(os.getppid(), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    sys.exit(1)


@worker_init.connect
def verify_dask_versions(**kwargs):
    """Verify Dask cluster versions match. Runs once in the Celery master.

    On failure calls `_fail_fast()` which signals the parent process and exits.
    Running this in the master (not per-fork via `worker_process_init`) is what
    lets us actually exit the container — billiard catches exceptions and exit
    calls inside forked children, leading to an infinite respawn loop.
    """
    address = settings.DASK_SCHEDULER_ADDRESS
    if not address:
        logger.info('No DASK_SCHEDULER_ADDRESS set, using local Dask scheduler')
        return

    timeout = settings.DASK_VERSION_CHECK_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    started = time.monotonic()

    logger.info('Verifying Dask cluster version alignment',
                address=address, timeout_seconds=timeout)

    client = _connect_with_retry(address, deadline, started)
    if client is None:
        elapsed = time.monotonic() - started
        logger.error('Dask version check timed out',
                     scheduler_address=address,
                     reason='scheduler unreachable',
                     timeout_seconds=timeout,
                     elapsed_seconds=round(elapsed, 1))
        _fail_fast()

    try:
        if not _wait_for_worker(client, deadline, address, timeout, started):
            _fail_fast()

        drifted = _check_versions(client)
        if drifted:
            logger.error('Dask version drift detected',
                         scheduler_address=address,
                         drifted_packages=drifted)
            _fail_fast()

        worker_count = len(client.scheduler_info(n_workers=-1).get('workers', {}))
        elapsed = time.monotonic() - started
        logger.info('Dask cluster verified',
                    scheduler_address=address,
                    worker_count=worker_count,
                    elapsed_seconds=round(elapsed, 1))
    finally:
        # Don't leave a dangling connection in the master — each forked child
        # will create its own Client in connect_dask_scheduler() below.
        try:
            client.close()
        except Exception:
            pass


@worker_process_init.connect
def connect_dask_scheduler(**kwargs):
    """Construct the per-process Dask Client.

    Versions are already verified by `verify_dask_versions` in the master, so
    this just establishes the per-process Client that registers itself as the
    default scheduler for LSDB `.compute()` calls in this fork.
    """
    address = settings.DASK_SCHEDULER_ADDRESS
    if not address:
        return  # Local Dask; already logged in verify_dask_versions

    from dask.distributed import Client
    Client(address)


def _connect_with_retry(address, deadline, started):
    """Try to construct a Client until the deadline. Return Client or None on timeout."""
    from dask.distributed import Client
    backoff = _BACKOFF_INITIAL
    while True:
        try:
            return Client(address)
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            elapsed = time.monotonic() - started
            logger.debug('Waiting for Dask scheduler',
                         scheduler_address=address,
                         elapsed_seconds=round(elapsed, 1),
                         remaining_seconds=round(remaining, 1),
                         error=str(exc))
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, _BACKOFF_MAX)


def _wait_for_worker(client, deadline, address, timeout, started):
    """Block until ≥1 worker registers or the deadline elapses. Return True on success."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        elapsed = time.monotonic() - started
        logger.error('Dask version check timed out',
                     scheduler_address=address,
                     reason='no workers registered',
                     timeout_seconds=timeout,
                     elapsed_seconds=round(elapsed, 1))
        return False
    try:
        client.wait_for_workers(n_workers=1, timeout=remaining)
        return True
    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.error('Dask version check timed out',
                     scheduler_address=address,
                     reason='no workers registered',
                     timeout_seconds=timeout,
                     elapsed_seconds=round(elapsed, 1),
                     error=str(exc))
        return False


def _check_versions(client):
    """Return a list of drift records, one per mismatched package. Empty list = aligned."""
    versions = client.get_versions(check=False)
    client_pkgs = versions.get('client', {}).get('packages', {})
    scheduler_pkgs = versions.get('scheduler', {}).get('packages', {})
    workers = versions.get('workers', {}) or {}

    drifted = []
    for pkg in _VERSION_CHECK_PACKAGES:
        client_ver = client_pkgs.get(pkg)
        scheduler_ver = scheduler_pkgs.get(pkg)
        worker_versions = {
            addr: w.get('packages', {}).get(pkg)
            for addr, w in workers.items()
        }

        scheduler_drifted = scheduler_ver is not None and scheduler_ver != client_ver
        worker_drifted = any(v != client_ver for v in worker_versions.values())

        if scheduler_drifted or worker_drifted:
            drifted.append({
                'package': pkg,
                'client_version': client_ver,
                'scheduler_version': scheduler_ver,
                'worker_versions': worker_versions,
            })
    return drifted
