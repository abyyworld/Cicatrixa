#!/usr/bin/env bash
# Deploy the Cicatrixa platform to the server.
# Usage: SERVER=root@169.58.36.128 ./deploy.sh
set -euo pipefail
SERVER="${SERVER:-root@169.58.36.128}"
DEST=/root/cicatrixa-platform
HERE="$(cd "$(dirname "$0")" && pwd)"

rsync -az --delete --exclude .env "$HERE/" "$SERVER:$DEST/"

ssh "$SERVER" "set -euo pipefail; cd $DEST; "'
  [ -f .env ] || cp .env.example .env

  # A placeholder BASE_DOMAIN is the classic silent outage: every Traefik router rule
  # is built from it, so the real hostname matches nothing and the site never answers.
  eval "$(grep -E "^(BASE_DOMAIN|BASE_URL)=" .env || true)"
  case "${BASE_DOMAIN:-}" in
    ""|*nip.io|*example.com|localhost)
      echo "refusing to deploy: BASE_DOMAIN=\"${BASE_DOMAIN:-}\" is a placeholder." >&2
      echo "set BASE_DOMAIN and BASE_URL in '"$DEST"'/.env first." >&2
      exit 1 ;;
  esac
  echo "→ BASE_DOMAIN=$BASE_DOMAIN  BASE_URL=$BASE_URL"

  # The compose file joins healnet (the self-heal demo network) as external. If the demo
  # stack has never been started, "external: true" aborts the whole up — including the
  # website. Creating it is idempotent and harmless.
  docker network inspect healnet >/dev/null 2>&1 || docker network create healnet

  docker compose build control
  docker compose up -d --remove-orphans
  docker compose ps

  # "compose said OK" is not "the site loads". Verify through Traefik on the real host
  # rule, which is what actually broke last time.
  echo "→ verifying http://$BASE_DOMAIN/healthz through Traefik ..."
  for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
             -H "Host: app.$BASE_DOMAIN" http://127.0.0.1/healthz || true)
    case "$code" in
      200|301|302|308) echo "✓ Traefik answered $code after ${i}s"; exit 0 ;;
    esac
    sleep 1
  done
  echo "✗ no healthy response from Traefik after 30s (last code: ${code:-none})" >&2
  docker compose logs --tail=60 traefik control >&2
  exit 1
'
