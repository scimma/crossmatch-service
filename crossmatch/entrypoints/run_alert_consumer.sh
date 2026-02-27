#!/bin/bash

set -euo pipefail

# Migrations should be created manually by developers and committed with the source code repo.
# Set the MAKE_MIGRATIONS env var to a non-empty string to create migration scripts
# after changes are made to the Django ORM models.
if [ "$MAKE_MIGRATIONS" == "true" ]; then
  echo "Generating database migration scripts..."
  python manage.py makemigrations --no-input
  exit 0
fi

## Initialize Django database and static files
##
cd "${APP_ROOT_DIR:-/opt}/crossmatch"
bash entrypoints/wait-for-it.sh ${DATABASE_HOST}:${DATABASE_PORT} --timeout=0

echo "Running initialization script..."
bash entrypoints/django_init.sh
echo "Django database initialization complete."

# Start alert consumer
cd "${APP_ROOT_DIR:-/opt}/crossmatch"

python manage.py run_alert_consumer
