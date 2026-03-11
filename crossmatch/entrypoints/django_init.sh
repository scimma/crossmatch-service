#!/bin/env bash
set -e

echo "Begin Django initialization..."

python manage.py locked_init

echo "Django initialization complete."
