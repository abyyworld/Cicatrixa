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
# Also accepted on any run, written into .env and kept: OPENAI_API_KEY,
# ADMIN_EMAILS, RESEND_API_KEY, MAIL_FROM, STRIPE_SECRET_KEY,
# STRIPE_WEBHOOK_SECRET, AI_MODEL. Pass ADMIN_EMAILS (your own address) on the
# first run: without it, whoever signs up first on the live site is the admin.
#
# The machine IS the server: while it sleeps or is off, the site shows a
# Cloudflare error. Customer containers run on it, so prefer one that holds
# nothing else you care about. Runs under macOS's /bin/bash 3.2.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

case "${1:-start}" in
  stop)  docker compose down; exit 0 ;;
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
current() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
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

echo "── Settings"
upsert BASE_DOMAIN "$BASE_DOMAIN"
# The app host, never the apex: it builds the GitHub App callback, invite links
# and the Stripe return URL, and the apex is the marketing site on Vercel.
upsert BASE_URL "https://app.$BASE_DOMAIN"
upsert CLOUDFLARE_TUNNEL_TOKEN "$TOKEN"
upsert COMPOSE_PROFILES tunnel
# Traefik sees plain HTTP from the tunnel even when the visitor used HTTPS, so its
# own redirect would send every request back to Cloudflare in a loop. Cloudflare's
# "Always Use HTTPS" does that job at the edge instead (verify-public.sh checks it).
upsert HTTPS_REDIRECT_MW cx-plain
for v in OPENAI_API_KEY AI_MODEL ADMIN_EMAILS RESEND_API_KEY MAIL_FROM \
         STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET; do
  if [ -n "${!v:-}" ]; then
    upsert "$v" "${!v}"
    echo "  set $v from what you passed"
  fi
done

# Nothing needs a port open to the network: the tunnel reaches Traefik over the
# cxnet network. So Traefik's ports bind to loopback only, away from the ports a
# laptop usually has taken already. Chosen once, then kept in .env.
taken() { command -v lsof >/dev/null && lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
pick() {  # pick <var> <port>...
  local var="$1" p; shift
  [ -n "$(current "$var")" ] && return 0
  for p in "$@"; do
    if ! taken "$p"; then upsert "$var" "127.0.0.1:$p"; return 0; fi
  done
  upsert "$var" "127.0.0.1:$1"
}
pick CX_HTTP_PORT 8000 8001 8002 8880
pick CX_HTTPS_PORT 8443 8444 8445
pick CX_TRAEFIK_PORT 8081 8082 8083
pick CX_HEALER_PORT 9001 9002 9003

# Traefik watches this directory for the self-heal demo's router; the compose
# default is a server path. An empty directory is fine.
if [ -z "$(current DEMO_DYNAMIC_DIR)" ]; then
  upsert DEMO_DYNAMIC_DIR "$(cd "$HERE/.." && pwd)/traefik/dynamic"
fi
mkdir -p "$(current DEMO_DYNAMIC_DIR)"
echo "  .env is set for https://app.$BASE_DOMAIN through a Cloudflare tunnel"

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
     if [ -n "$(current ADMIN_EMAILS)" ]; then
       echo "  Sign up at https://app.$BASE_DOMAIN/signup as $(current ADMIN_EMAILS) to be the admin."
     else
       echo "  Sign up at https://app.$BASE_DOMAIN/signup NOW: with no ADMIN_EMAILS set, the"
       echo "  first account on a new install is the admin, whoever makes it."
     fi
     echo "  Keep this machine on and awake: it is the server." ;;
  2) echo "  The stack is running here. Finish what is listed above in the Cloudflare"
     echo "  dashboard, then run ./tunnel.sh again — it is safe to repeat." ;;
  *) echo "  Something above is broken; fix it and run ./tunnel.sh again." ;;
esac
exit "$rc"
