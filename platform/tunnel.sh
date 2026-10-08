#!/usr/bin/env bash
# Put the platform on the internet from any machine with Docker — for free.
#
# No server to rent, no public IP, no port to open, no certificate to issue.
# cx-tunnel (cloudflared) dials OUT to Cloudflare, which already runs this
# domain's DNS; Cloudflare sends app.<domain> and every <project>.<domain> back
# down that connection to cx-traefik, and TLS ends at Cloudflare's edge on the
# certificate it already holds for the domain. A home PC behind a router works.
#
# First run. The token comes from the Cloudflare dashboard — docs/RUNBOOK.md,
# "Free: Cloudflare Tunnel", has the clicks:
#
#   BASE_DOMAIN=cicatrixa.com CLOUDFLARE_TUNNEL_TOKEN=eyJ... ./tunnel.sh
#
# Every run after that (update, restart, after a reboot):
#
#   git pull && ./tunnel.sh
#   ./tunnel.sh stop          # take it offline, keep every account and project
#
# ADMIN_EMAILS is required: the address you will sign up with yourself. Without
# it, whoever signs up first on the live site would be the admin.
# Also accepted on any run, written into .env and kept: OPENAI_API_KEY,
# RESEND_API_KEY, MAIL_FROM, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, AI_MODEL.
#
# The machine IS the server: while it sleeps or is off, the site shows a
# Cloudflare error. Customer containers run on it, so prefer one that holds
# nothing else you care about. Runs under macOS's /bin/bash 3.2.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

case "${1:-start}" in
  stop)
    docker compose down
    # Customer apps, databases and test runs are not part of the compose project.
    # Stop them too — offline means their code stops running here — but keep the
    # apps and databases: the watchdog starts them again after the next ./tunnel.sh.
    ids="$(docker ps -q --filter label=cx.service; docker ps -q --filter label=cx.database
           docker ps -q --filter label=cx.test)"
    [ -z "$ids" ] || docker stop $ids >/dev/null
    exit 0 ;;
  start) ;;
  *) echo "usage: $0 [start|stop]" >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null; then
  echo "Docker is not installed on this machine — that is the only thing missing." >&2
  case "$(uname -s)" in
    Darwin) echo "  Docker Desktop: https://docs.docker.com/desktop/install/mac-install/" >&2 ;;
    *)      echo "  curl -fsSL https://get.docker.com | sh" >&2 ;;
  esac
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running (or this user may not use it)." >&2
  case "$(uname -s)" in
    Darwin) echo "  open -a Docker      then run this again" >&2 ;;
    *)      echo "  sudo systemctl start docker    — or run this with sudo" >&2 ;;
  esac
  exit 1
fi

touch .env
chmod 600 .env
# A value from .env, with one layer of quotes taken off the way compose reads it.
current() {
  grep -E "^$1=" .env | tail -1 | cut -d= -f2- | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" || true
}
# Replace or add KEY=VALUE in .env, via a temp file so values holding / or &
# (URLs, keys) need no escaping.
upsert() {
  local t; t="$(mktemp)"
  grep -v "^$1=" .env > "$t" || true
  printf '%s=%s\n' "$1" "$2" >> "$t"
  mv "$t" .env
}

BASE_DOMAIN="${BASE_DOMAIN:-$(current BASE_DOMAIN)}"
case "$BASE_DOMAIN" in
  ""|localhost|*nip.io|*example.com)
    echo "set BASE_DOMAIN — the domain whose DNS is on Cloudflare, e.g. BASE_DOMAIN=cicatrixa.com" >&2
    echo "(for a run with no domain at all, use ./local.sh)" >&2
    exit 1 ;;
esac

# People paste the whole install line the dashboard shows
# ("cloudflared service install eyJ..."); keep only the token out of it.
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-$(current CLOUDFLARE_TUNNEL_TOKEN)}"
TOKEN="$(printf '%s\n' "$TOKEN" | tr ' \t' '\n\n' | grep -E '^eyJ' | tail -1 || true)"
if [ -z "$TOKEN" ]; then
  echo "set CLOUDFLARE_TUNNEL_TOKEN — the long string starting with eyJ that the" >&2
  echo "Cloudflare dashboard shows when you create the tunnel (docs/RUNBOOK.md)." >&2
  exit 1
fi

PASSED_ADMIN_EMAILS="${ADMIN_EMAILS:-}"
ADMIN_EMAILS="${ADMIN_EMAILS:-$(current ADMIN_EMAILS)}"
ADMIN_EMAILS="$(printf '%s' "$ADMIN_EMAILS" | tr -d ' \t\r')"   # as the app reads it
case "$ADMIN_EMAILS" in *@*) ;; *) ADMIN_EMAILS="" ;; esac
if [ -z "$ADMIN_EMAILS" ]; then
  echo "set ADMIN_EMAILS — the address you will sign up with yourself. Without it the" >&2
  echo "first account made on the live site, by anyone, would be the admin." >&2
  exit 1
fi
OWNER="${ADMIN_EMAILS%%,*}"

echo "── Settings"
upsert BASE_DOMAIN "$BASE_DOMAIN"
# The app host, never the apex: it builds the GitHub App callback, invite links
# and the Stripe return URL, and the apex is the marketing site on Vercel.
upsert BASE_URL "https://app.$BASE_DOMAIN"
upsert CLOUDFLARE_TUNNEL_TOKEN "$TOKEN"
upsert COMPOSE_PROFILES tunnel
# Traefik sees plain HTTP from the tunnel even when the visitor used HTTPS, so its
# own redirect would send every request back to Cloudflare in a loop. Cloudflare's
# "Always Use HTTPS" does that job at the edge instead (verify-public.sh checks it),
# and cx-tunnel tells apps the request was HTTPS (docker-compose.yml).
upsert HTTPS_REDIRECT_MW cx-tunnel
upsert ADMIN_EMAILS "$ADMIN_EMAILS"
[ -z "$PASSED_ADMIN_EMAILS" ] || echo "  set ADMIN_EMAILS from what you passed"
for v in OPENAI_API_KEY AI_MODEL RESEND_API_KEY MAIL_FROM \
         STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET; do
  if [ -n "${!v:-}" ]; then
    upsert "$v" "${!v}"
    echo "  set $v from what you passed"
  fi
done

# Nothing needs a port open to the network: the tunnel reaches Traefik over the
# cxnet network. So Traefik's ports bind to loopback only, each on a free port
# Docker picks ("127.0.0.1:" — no fixed number to collide with whatever this
# machine already runs). `docker compose port traefik 80` says which.
for v in CX_HTTP_PORT CX_HTTPS_PORT CX_TRAEFIK_PORT CX_HEALER_PORT; do
  [ -n "$(current "$v")" ] || upsert "$v" "127.0.0.1:"
done

# Traefik watches this directory for the self-heal demo's router; the compose
# default is a server path. An empty directory is fine — but one this machine
# cannot create (a /root/... copied from .env.example) is replaced, not fatal.
dyn="$(current DEMO_DYNAMIC_DIR)"
if [ -z "$dyn" ] || ! mkdir -p "$dyn" 2>/dev/null; then
  dyn="$(cd "$HERE/.." && pwd)/traefik/dynamic"
  upsert DEMO_DYNAMIC_DIR "$dyn"
  mkdir -p "$dyn"
fi
echo "  .env is set for https://app.$BASE_DOMAIN through a Cloudflare tunnel"

# Compose prefers a variable in its environment over the same one in .env. Hand it
# what was just written — above all the token taken OUT of a pasted install line,
# which would otherwise reach cloudflared whole — and drop anything that would
# put Traefik's ports back on the network.
export BASE_DOMAIN BASE_URL="https://app.$BASE_DOMAIN" CLOUDFLARE_TUNNEL_TOKEN="$TOKEN" \
       COMPOSE_PROFILES=tunnel HTTPS_REDIRECT_MW=cx-tunnel ADMIN_EMAILS DEMO_DYNAMIC_DIR="$dyn"
unset CX_HTTP_PORT CX_HTTPS_PORT CX_TRAEFIK_PORT CX_HEALER_PORT

# The compose file joins the self-heal demo's network as external; nothing
# creates it on a machine where the demo has never run.
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet >/dev/null

echo "── Starting"
docker compose build control
docker compose up -d --remove-orphans

printf 'waiting for the control plane'
for _ in $(seq 1 60); do
  if docker compose exec -T control python -c "
import sys, urllib.request
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3).status == 200 else 1)
" >/dev/null 2>&1; then
    echo
    break
  fi
  printf '.'
  sleep 2
done

echo "── Is it reachable from the internet?"
rc=0
./verify-public.sh || rc=$?
echo
case "$rc" in
  0) echo "  Cicatrixa is live: https://app.$BASE_DOMAIN"
     # A shell on this machine is the proof of who the admin is: anyone on the
     # web can type your address. promote makes (or takes back) the account and
     # prints a one-time link to choose its password — no signing up first.
     echo "  Make your admin account, on this machine:"
     echo "    docker exec cx-control python -m app.promote $OWNER"
     if [ -n "$(current RESEND_API_KEY)" ]; then
       echo "  (Or sign up as $OWNER: entering the emailed code makes it the admin.)"
     fi
     echo "  Keep this machine on and awake: it is the server." ;;
  2) echo "  The stack is running here. Finish what is listed above in the Cloudflare"
     echo "  dashboard, then run ./tunnel.sh again — it is safe to repeat." ;;
  *) echo "  Something above is broken; fix it and run ./tunnel.sh again." ;;
esac
exit "$rc"
