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
# Ask the box, over one connection, for the domain it serves and the address the
# internet sees for it. The request below is pinned to THAT address — not to
# SERVER, which may be a VPN, Tailscale or LAN address that never passes the
# provider firewall this check exists to see. Laptop-side code avoids bash-4-only
# features: it runs under macOS's /bin/bash 3.2.
remote='d=$(grep -E "^BASE_DOMAIN=" '"$DEST"'/.env 2>/dev/null | tail -1 | cut -d= -f2-); echo "D=$d"; i=$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null); echo "I=$i"'
sshrc=0
info="$(ssh -o ConnectTimeout=15 "$SERVER" "$remote" 2>&1)" || sshrc=$?
if [ "$sshrc" -ne 0 ]; then
  echo "✗ could not reach $SERVER over ssh after the deploy (exit $sshrc): $info" >&2
  exit 1
fi
BOX_DOMAIN="$(printf '%s\n' "$info" | sed -n 's/^D=//p' | tail -1)"
PUBLIC_IP="$(printf '%s\n' "$info" | sed -n 's/^I=//p' | tail -1)"
if [ -z "$BOX_DOMAIN" ]; then
  echo "✗ BASE_DOMAIN is not set in $DEST/.env on the box" >&2
  exit 1
fi
PIN=""
case "$PUBLIC_IP" in
  ""|*[!0-9.]*) PUBLIC_IP="" ;;                        # unknown: fall back to DNS
  *) PIN="app.$BOX_DOMAIN:443:$PUBLIC_IP" ;;
esac
# The pin skips DNS, so check the visitor's DNS path on its own.
if [ -n "$PUBLIC_IP" ] && command -v dig >/dev/null 2>&1; then
  seen="$(dig +short "app.$BOX_DOMAIN" A 2>/dev/null | tail -1)"
  if [ "$seen" != "$PUBLIC_IP" ]; then
    echo "! app.$BOX_DOMAIN resolves to '${seen:-nothing}' from here, not this box ($PUBLIC_IP)" >&2
  fi
fi
echo "→ from here: https://app.$BOX_DOMAIN/healthz${PIN:+  (pinned to $PUBLIC_IP, the public address of the box)}"
if body="$(curl -sS --max-time 20 ${PIN:+--resolve "$PIN"} "https://app.$BOX_DOMAIN/healthz" 2>&1)" \
   && printf '%s' "$body" | grep -q '"ok"'; then
  echo "✓ reachable from the internet: $body"
else
  echo "✗ the box says it is up, but it is NOT reachable from here: $body" >&2
  echo "  check the provider firewall / security list for 80 and 443." >&2
  exit 1
fi
