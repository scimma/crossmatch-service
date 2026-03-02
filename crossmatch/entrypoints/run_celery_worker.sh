#!/bin/bash

set -euo pipefail

QUEUES=$1

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${REDIS_SERVICE:-redis}:${REDIS_PORT:-6379} --timeout=0

# Start worker
if [[ $DEV_MODE == "true" ]]; then
    watchmedo auto-restart --directory=./ --pattern=*.py --recursive -- \
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
