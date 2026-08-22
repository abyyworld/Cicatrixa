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
      echo "set BASE_DOMAIN and BASE_URL in $PWD/.env first." >&2
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
  # rule, which is what actually broke last time. Only 200 counts: :80 redirects to
  # :443, so accepting a 3xx here would greenlight precisely the failure we are
  # guarding against — a redirect pointing at a port that cannot answer.
  echo "→ verifying https://app.$BASE_DOMAIN/healthz through Traefik ..."
  ok_http="" ; ok_https=""
  for i in $(seq 1 45); do
    if [ -z "$ok_https" ]; then
      c=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 \
            --resolve "app.$BASE_DOMAIN:443:127.0.0.1" \
            "https://app.$BASE_DOMAIN/healthz" 2>/dev/null || true)
      if [ "$c" = "200" ]; then
        ok_https=ok
        echo "  ✓ :443 terminated TLS and reached cx-control (${i}s)"
      fi
    fi
    if [ -z "$ok_http" ]; then
      c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
            -H "Host: app.$BASE_DOMAIN" http://127.0.0.1/healthz 2>/dev/null || true)
      case "$c" in
        200|301|302|308) ok_http=ok; echo "  ✓ :80 answered $c (${i}s)" ;;
      esac
    fi
    if [ -n "$ok_http" ] && [ -n "$ok_https" ]; then
      echo "✓ deploy verified"
      exit 0
    fi
    sleep 1
  done
  echo "✗ unhealthy after 45s — :80 ${ok_http:-FAILED}, :443 ${ok_https:-FAILED}" >&2
  echo "  :443 failing alone usually means the certificate never issued — check that the A" >&2
  echo "  record for app.$BASE_DOMAIN reaches this box and that :80 accepts inbound traffic." >&2
  docker compose logs --tail=80 traefik control >&2
  exit 1
'
