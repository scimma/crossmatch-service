#!/bin/env bash

set -euo pipefail

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${VALKEY_SERVICE:-redis}:${VALKEY_PORT:-6379} --timeout=0

celery -A project flower \
    --port=${FLOWER_PORT:-5555} \
    --url_prefix=${FLOWER_URL_PREFIX:-} \
    --loglevel ${FLOWER_LOG_LEVEL:-WARNING}

