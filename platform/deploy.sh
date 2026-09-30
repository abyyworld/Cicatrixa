#!/usr/bin/env bash
# Deploy the Cicatrixa platform to an existing server (a fresh one: bootstrap.sh).
# Usage: SERVER=root@<ip> ./deploy.sh
#
# Needs root SSH: it rsyncs to /root/cicatrixa-platform. On Oracle, whose images
# refuse root logins, enable it once — see docs/RUNBOOK.md.
set -euo pipefail
# No default. The old default was a VPS that has since died, and providers
# reassign IPs: a bare ./deploy.sh would have shipped the platform to whoever
# holds that address now.
SERVER="${SERVER:?set SERVER=root@<ip> — the box to deploy to}"
DEST=/root/cicatrixa-platform
HERE="$(cd "$(dirname "$0")" && pwd)"

rsync -az --delete --exclude .env "$HERE/" "$SERVER:$DEST/"

rc=0
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
  # TRUSTED certificate. On a redeploy that certificate predates this run, so it
  # proves DNS and the stack, not the firewall: the check from the laptop after
  # this block is what proves that. Exit 0 live, 2 pending DNS, 1 broken.
  # (No apostrophes anywhere in this block: it is one single-quoted ssh argument.)
  BASE_DOMAIN="$BASE_DOMAIN" ./verify-public.sh
' || rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

# The box has checked itself; it cannot see its own firewall the way a visitor
# does, and a certificate issued weeks ago keeps being served after :80/:443 are
# closed. This laptop can: one real request from outside.
# Check the host THIS box serves — read from its .env, not assumed — and send the
# request to THIS box when SERVER is an address. Otherwise the laptop's DNS picks
# the machine, and a different box (production, or a stale cached one) could
# answer "ok" for this one. Written for macOS's bash 3.2: no empty-array expansion.
BOX_DOMAIN="$(ssh "$SERVER" "grep -E '^BASE_DOMAIN=' $DEST/.env | tail -1 | cut -d= -f2-" 2>/dev/null || true)"
if [ -z "$BOX_DOMAIN" ]; then
  echo "✗ could not read BASE_DOMAIN from $DEST/.env on the box" >&2
  exit 1
fi
TARGET="${SERVER#*@}"
PIN=""
case "$TARGET" in
  ""|*[!0-9.]*) ;;                                  # a hostname or ssh alias: use DNS
  *) PIN="app.$BOX_DOMAIN:443:$TARGET" ;;           # an IPv4 address: go straight to it
esac
echo "→ from here: https://app.$BOX_DOMAIN/healthz${PIN:+  (pinned to $TARGET)}"
if body="$(curl -sS --max-time 20 ${PIN:+--resolve "$PIN"} "https://app.$BOX_DOMAIN/healthz" 2>&1)" \
   && printf '%s' "$body" | grep -q '"ok"'; then
  echo "✓ reachable from the internet: $body"
else
  echo "✗ the box says it is up, but it is NOT reachable from here: $body" >&2
  echo "  check the provider firewall / security list for 80 and 443." >&2
  exit 1
fi
