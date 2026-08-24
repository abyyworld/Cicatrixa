#!/usr/bin/env bash
# Run the whole platform on this machine. No server, no domain, no certificates.
#
# The control plane already defaults to BASE_DOMAIN=localhost and only adds its
# HTTPS router when BASE_URL is https — so nothing here is a special mode, it is
# the same stack with the public parts left off. Projects you deploy come up at
# <slug>.localhost, which resolves without any DNS on every current browser.
#
#   ./local.sh          # start it
#   ./local.sh stop     # stop it, keep the data
#   ./local.sh reset    # stop it and delete the data volume
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

case "${1:-start}" in
  stop)  docker compose down; exit 0 ;;
  reset) docker compose down -v; echo "data volume deleted."; exit 0 ;;
  start) ;;
  *) echo "usage: $0 [start|stop|reset]" >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null; then
  echo "Docker is not installed on this machine — that is the only thing missing." >&2
  case "$(uname -s)" in
    Darwin) cat >&2 <<'MAC'

  On a Mac, either:
    Docker Desktop   https://docs.docker.com/desktop/install/mac-install/   (simplest)
    or, from the terminal:
      brew install --cask docker && open -a Docker

  Then run this again.
MAC
      ;;
    *) echo "  curl -fsSL https://get.docker.com | sh" >&2 ;;
  esac
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is installed but not running." >&2
  case "$(uname -s)" in
    Darwin) echo "  open -a Docker      (wait for the whale in the menu bar, then run this again)" >&2 ;;
    *) echo "  sudo systemctl start docker" >&2 ;;
  esac
  exit 1
fi

# The compose file joins the self-heal demo's network as external, because on the
# server that stack owns it. Nothing creates it on a laptop, and "external: true"
# aborts the whole up when it is missing.
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet >/dev/null

# :80 and :443 are privileged, and on a laptop they are often taken as well —
# by another Docker stack, by nginx, by anything. Rather than failing to start,
# move to ports nobody fights over. Override with CX_HTTP_PORT=... if you want.
taken() { command -v lsof >/dev/null && lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if [ -z "${CX_HTTP_PORT:-}" ]; then
  if taken 80; then
    CX_HTTP_PORT=8000
    echo "note: something already has :80, so the platform is on :$CX_HTTP_PORT."
  else
    CX_HTTP_PORT=80
  fi
fi
export CX_HTTP_PORT
export CX_HTTPS_PORT="${CX_HTTPS_PORT:-8443}"
export CX_TRAEFIK_PORT="${CX_TRAEFIK_PORT:-8081}"
export CX_HEALER_PORT="${CX_HEALER_PORT:-9001}"
SITE="http://localhost"
[ "$CX_HTTP_PORT" = "80" ] || SITE="http://localhost:$CX_HTTP_PORT"

if [ ! -f .env ]; then
  cat > .env <<ENV
# Written by local.sh — a machine, not a server.
BASE_DOMAIN=localhost
BASE_URL=$SITE
# No certificate can be issued for localhost, so the redirect to :443 must not
# be armed: with it, every page would bounce to a port nothing answers on.
HTTPS_REDIRECT_MW=cx-plain
# Everything below is optional. Without a key the deploy engine falls back to
# its deterministic node/python/go/static heuristics, which cover most repos.
OPENAI_API_KEY=
ADMIN_EMAILS=
ENV
  echo "wrote .env for a local run (BASE_DOMAIN=localhost)."
fi


docker compose build control
docker compose up -d --remove-orphans

printf 'waiting for the control plane'
for _ in $(seq 1 60); do
  if docker compose exec -T control python -c "
import sys, urllib.request
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3).status == 200 else 1)
" >/dev/null 2>&1; then
    echo
    echo
    echo "  Cicatrixa is running on this machine."
    echo "    control plane   $SITE"
    if [ "$CX_HTTP_PORT" = "80" ]; then
      echo "    your projects   http://<slug>.localhost"
    else
      echo "    your projects   http://<slug>.localhost:$CX_HTTP_PORT"
    fi
    echo "    traefik         http://localhost:$CX_TRAEFIK_PORT  (basic-auth)"
    echo
    echo "  Sign up at $SITE/signup — the first account is yours."
    echo "  Connect GitHub with a fine-grained PAT (Contents: read + Metadata):"
    echo "  the GitHub App flow needs a public callback URL, so it is the one"
    echo "  thing a local run cannot do."
    exit 0
  fi
  printf '.'
  sleep 2
done

echo
echo "the control plane did not come up. What it said:" >&2
docker compose logs --tail 40 control >&2
exit 1
