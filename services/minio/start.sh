#!/bin/sh
# Serves the S3 API on $PORT. On Render, MINIO_CONSOLE_PROXY=on puts a local reverse proxy on that
# public port and routes /console/ to MinIO's private console listener.
set -e
export MINIO_ROOT_USER="${MINIO_ROOT_USER:-$MINIO_ACCESS_KEY}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-$MINIO_SECRET_KEY}"
mkdir -p /data
if [ "$MINIO_CONSOLE_PROXY" = "on" ]; then
  export MINIO_BROWSER=on
  export MINIO_BROWSER_REDIRECT_URL="${MINIO_BROWSER_REDIRECT_URL:-https://${RENDER_EXTERNAL_HOSTNAME:-edge-cdn-minio.onrender.com}/console}"
  minio server /data --address "127.0.0.1:9000" --console-address "127.0.0.1:9001" --quiet &
  minio_pid=$!
  trap 'kill "$minio_pid" 2>/dev/null || true' INT TERM
  exec /minio-proxy
fi
if [ "$MINIO_BROWSER" = "off" ]; then
  exec minio server /data --address ":${PORT:-9000}" --quiet
fi
exec minio server /data --address ":${PORT:-9000}" --console-address ":9001"
