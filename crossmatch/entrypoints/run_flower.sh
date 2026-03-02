#!/bin/env bash

set -euo pipefail

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${REDIS_SERVICE:-redis}:${REDIS_PORT:-6379} --timeout=0

celery \
    --broker=redis://${REDIS_SERVICE:-redis}:${REDIS_PORT:-6379}/${REDIS_BROKER_DB:-0} \
    flower \
    --port=${FLOWER_PORT:-5555} \
    --url_prefix=${FLOWER_URL_PREFIX:-} \
    --loglevel ${FLOWER_LOG_LEVEL:-WARNING}
