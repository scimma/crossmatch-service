#!/bin/env bash
set -e

echo "Begin Django initialization..."

echo "Apply database migrations..."
python manage.py migrate
# echo "Provision database..."
# python manage.py createcachetable
echo "Collect static files..."
python manage.py collectstatic --no-input
echo "Initializing periodic tasks..."
python manage.py initialize_periodic_tasks

echo "Create Django superuser..."
while [[ "$SUCCESS" != "true" ]]; do
  regex=".*That username is already taken*"
  set +e
  ERR_MSG="$(python manage.py createsuperuser --no-input 2>&1 > /dev/null | grep -v "registering new views" )"
  set -e
  if [[ "$ERR_MSG" == "" ]]; then
    echo "superuser created successfully"
    SUCCESS="true"
  fi
  if [[ "$ERR_MSG" =~ $regex ]]; then
    echo "superuser already exists"
    SUCCESS="true"
  fi
  if [[ "$SUCCESS" == "true" ]]; then
    touch /tmp/superuser_created
  else
    echo "Error: $ERR_MSG"
    SUCCESS="false"
    sleep 2
  fi
done

echo "Django initialization complete."
