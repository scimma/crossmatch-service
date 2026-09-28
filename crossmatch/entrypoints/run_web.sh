#!/bin/env bash

set -euo pipefail

cd "${APP_ROOT_DIR:-/opt}/crossmatch"

bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT:-5432} --timeout=0

# Collect the web frontend's static assets (logo, css) into STATIC_ROOT so
# WhiteNoise serves them from this pod (no nginx). Idempotent; --no-input avoids
# the interactive overwrite prompt.
python manage.py collectstatic --no-input

# Threaded workers (gthread) so /healthz and the HTML pages are not queued behind
# slow API requests; WEB_WORKERS x WEB_THREADS is the pod's request concurrency
# and its Postgres connection ceiling (sized in settings.py's API cost-bound
# block). --timeout is the worker-heartbeat backstop, well above the per-request
# API budget (API_REQUEST_BUDGET_SECONDS). DATABASE_CONNECT_TIMEOUT is exported
# here, not globally, so only the web tier fails fast on an unreachable database.
export DATABASE_CONNECT_TIMEOUT=${DATABASE_CONNECT_TIMEOUT:-3}

gunicorn project.wsgi:application \
    --bind 0.0.0.0:${WEB_PORT:-8000} \
    --workers ${WEB_WORKERS:-2} \
    --worker-class gthread \
    --threads ${WEB_THREADS:-4} \
    --timeout ${WEB_TIMEOUT:-30} \
    --log-level ${WEB_LOG_LEVEL:-info}
