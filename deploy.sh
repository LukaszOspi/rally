#!/usr/bin/env bash
# Sync this directory to the Hetzner box and rebuild the stack there.
# The server is aarch64, so images are always built on the server itself.
set -euo pipefail

HOST="${RALLY_HOST:-fuerst-wiacek}"
REMOTE="${RALLY_REMOTE_DIR:-/opt/rally-finder}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Syncing $HERE -> $HOST:$REMOTE"
ssh "$HOST" "mkdir -p $REMOTE"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude 'DOCUMENTS' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  "$HERE/" "$HOST:$REMOTE/"

echo "==> Building and starting containers"
ssh "$HOST" "cd $REMOTE && docker compose up -d --build"

echo "==> Waiting for the API to report healthy"
for i in $(seq 1 30); do
  if ssh "$HOST" "docker exec rally-api python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health',timeout=4)\"" >/dev/null 2>&1; then
    echo "    healthy after ${i}s"
    break
  fi
  sleep 1
  [ "$i" = 30 ] && { echo "    API did not become healthy"; ssh "$HOST" "docker logs rally-api --tail 40"; exit 1; }
done

echo "==> Reloading Caddy"
ssh "$HOST" "cd /opt/infra && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile" \
  || echo "    (Caddy reload failed — check /opt/infra/Caddyfile)"

echo "==> Status"
ssh "$HOST" "docker ps --filter name=rally- --format '    {{.Names}}  {{.Status}}'"
echo "Done. https://rally.ospi.link"
