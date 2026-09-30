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

  # "compose said OK" is not "the site loads", and a curl to 127.0.0.1 is not
  # either: it goes around every firewall and DNS mistake there is. This used to
  # check :443 with curl -k, which accepted the self-signed fallback certificate
  # and so passed on a box nobody could reach. verify-public.sh accepts only a
  # TRUSTED certificate, which can only be issued after the CA reached this box on
  # :80 through public DNS. Exit 0 live, 2 pending DNS, 1 broken, passed through.
  # (No apostrophes anywhere in this block: it is one single-quoted ssh argument.)
  BASE_DOMAIN="$BASE_DOMAIN" ./verify-public.sh
'
