#!/bin/sh
# Starts a cache service. Without REDIS_URL it runs its own private Redis in the container,
# so each cache stays independent where only one managed Redis is available for free.
set -e
if [ -z "$REDIS_URL" ]; then
  redis-server --port 6379 --bind 127.0.0.1 --dir /tmp --save "" --appendonly no \
    --maxmemory "${REDIS_MAXMEMORY:-64mb}" --maxmemory-policy allkeys-lru --daemonize yes
  export REDIS_URL=redis://127.0.0.1:6379/0
fi
exec uvicorn "$1" --host 0.0.0.0 --port "${PORT:-8000}" --no-access-log --log-level warning
