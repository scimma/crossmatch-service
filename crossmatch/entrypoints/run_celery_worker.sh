#!/bin/bash

set -euo pipefail

QUEUES=$1

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${VALKEY_SERVICE:-redis}:${VALKEY_PORT:-6379} --timeout=0

# Start worker
if [[ $DEV_MODE == "true" ]]; then
    # --no-restart-on-command-exit: watchmedo only restarts celery on file
    # changes, not when celery exits on its own. Without this flag, a fail-fast
    # exit (e.g., the Dask version-drift check in core/dask.py) gets silently
    # respawned inside the same container, masking the failure.
    watchmedo auto-restart --directory=./ --pattern=*.py --recursive \
        --no-restart-on-command-exit -- \
    celery -A project worker \
        --queues $QUEUES \
        --loglevel ${CELERY_LOG_LEVEL:-DEBUG} \
        --concurrency ${CELERY_CONCURRENCY:-4}
else
    celery -A project worker \
        --queues $QUEUES \
        --loglevel ${CELERY_LOG_LEVEL:-INFO} \
        --concurrency ${CELERY_CONCURRENCY:-4}
fi
