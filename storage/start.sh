#!/bin/sh
set -eu

: "${AWS_ACCESS_KEY_ID:?Run scripts/configure_storage.py first}"
: "${AWS_SECRET_ACCESS_KEY:?Run scripts/configure_storage.py first}"
: "${WEED_ADMIN_PASSWORD:?Run scripts/configure_storage.py first}"
: "${WEED_ADMIN_READONLY_PASSWORD:?Run scripts/configure_storage.py first}"

exec /entrypoint.sh "$@"
