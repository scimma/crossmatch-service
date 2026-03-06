#!/bin/bash

set -euo pipefail

## Initialize Django database and static files
##
cd "${APP_ROOT_DIR:-/opt}/crossmatch"
bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT} --timeout=0
bash entrypoints/wait-for-it.sh ${VALKEY_SERVICE:-redis}:${VALKEY_PORT:-6379} --timeout=0

echo "Running initialization script..."
bash entrypoints/django_init.sh
echo "Django database initialization complete."

# Start Lasair alert ingest
cd "${APP_ROOT_DIR:-/opt}/crossmatch"

python manage.py run_lasair_ingest
