#!/bin/env bash

set -euo pipefail

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0
bash entrypoints/wait-for-it.sh ${MESSAGE_BROKER_HOST}:${MESSAGE_BROKER_PORT:-5672} --timeout=0

celery \
    --broker=amqp://${RABBITMQ_DEFAULT_USER}:${RABBITMQ_DEFAULT_PASS}@${MESSAGE_BROKER_HOST}:${MESSAGE_BROKER_PORT}// \
    flower \
    --port=${FLOWER_PORT:-5555} \
    --url_prefix=${FLOWER_URL_PREFIX:-} \
    --loglevel ${FLOWER_LOG_LEVEL:-WARNING}
