#!/bin/bash

set -euo pipefail

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${MESSAGE_BROKER_HOST}:${MESSAGE_BROKER_PORT} --timeout=0

# Start worker
if [[ $DEV_MODE == "true" ]]; then
    watchmedo auto-restart --directory=./ --pattern=*.py --recursive -- \
    celery -A project beat --loglevel ${CELERY_LOG_LEVEL:-DEBUG}
else
    celery -A project beat --loglevel ${CELERY_LOG_LEVEL:-INFO}
fi
