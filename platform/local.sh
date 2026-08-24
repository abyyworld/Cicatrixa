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

command -v docker >/dev/null || { echo "Docker is not installed. https://docs.docker.com/get-docker/" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running — start Docker Desktop, or 'sudo systemctl start docker'." >&2; exit 1; }

# The compose file joins the self-heal demo's network as external, because on the
# server that stack owns it. Nothing creates it on a laptop, and "external: true"
# aborts the whole up when it is missing.
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet >/dev/null

if [ ! -f .env ]; then
  cat > .env <<'ENV'
# Written by local.sh — a machine, not a server.
BASE_DOMAIN=localhost
BASE_URL=http://localhost
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

# :80 is what Traefik needs, and something else often has it on a laptop.
if command -v lsof >/dev/null && lsof -nP -iTCP:80 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "warning: something is already listening on :80 — Traefik will fail to start." >&2
  echo "         stop it, or set a different port in docker-compose.yml." >&2
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
    echo "    control plane   http://localhost"
    echo "    your projects   http://<slug>.localhost"
    echo "    traefik         http://localhost:8080  (basic-auth)"
    echo
    echo "  Sign up at http://localhost/signup — the first account is yours."
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
