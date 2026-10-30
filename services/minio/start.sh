#!/bin/sh
# Serves the S3 API on $PORT. The console needs a second port, so it is only enabled when browser
# access is on: Render exposes a single HTTP port per service.
set -e
export MINIO_ROOT_USER="${MINIO_ROOT_USER:-$MINIO_ACCESS_KEY}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-$MINIO_SECRET_KEY}"
mkdir -p /data
if [ "$MINIO_BROWSER" = "off" ]; then
  exec minio server /data --address ":${PORT:-9000}" --quiet
fi
exec minio server /data --address ":${PORT:-9000}" --console-address ":9001"
