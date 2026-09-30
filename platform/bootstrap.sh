#!/usr/bin/env bash
# Bring the platform up on a machine that has nothing on it yet.
#
# Run it ON the box (ssh in first). It installs Docker if it is missing, writes
# the .env from what you pass in, starts the stack, and then checks the thing
# that actually matters — that the real hostname answers through Traefik, not
# that compose said OK.
#
#   curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
#     | BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... bash
#
# or, from a checkout:
#   BASE_DOMAIN=cicatrixa.com ./bootstrap.sh
#
# A rebuilt server is then one command and a DNS record, which is the whole
# point: nothing in this stack is tied to the machine it last ran on.
set -euo pipefail

REPO="${REPO:-https://github.com/abyyworld/cicatrixa.git}"
DEST="${DEST:-/root/cicatrixa-platform}"
BASE_DOMAIN="${BASE_DOMAIN:-}"
BASE_URL="${BASE_URL:-}"

[ "$(id -u)" -eq 0 ] || { echo "run this as root (or with sudo)." >&2; exit 1; }
if [ -z "$BASE_DOMAIN" ]; then
  echo "set BASE_DOMAIN — the domain this platform serves, e.g. BASE_DOMAIN=cicatrixa.com" >&2
  exit 1
fi
case "$BASE_DOMAIN" in
  *nip.io|*example.com|localhost)
    echo "BASE_DOMAIN=$BASE_DOMAIN is a placeholder. Use ./local.sh for a machine with no domain." >&2
    exit 1 ;;
esac
# The app host, not the apex. BASE_URL builds the GitHub App callback, invite
# links and the Stripe return URL; the apex is usually a marketing site on a
# static host, and pointing those at it breaks all three without an error.
BASE_URL="${BASE_URL:-https://app.$BASE_DOMAIN}"

echo "── Docker"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  echo "  already here: $(docker --version)"
else
  echo "  installing…"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

echo "── Source"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch --depth 1 origin main && git -C "$DEST" reset --hard origin/main
else
  # The platform is a subdirectory of the repository, so the checkout lands
  # beside it and the compose file is run from platform/.
  #
  # $DEST is replaced wholesale on every run, and .env lives inside it — so it is
  # carried across explicitly. Without this, re-running the script (which it tells
  # you to do once DNS resolves) silently wiped every API key and Stripe secret.
  kept=""
  if [ -f "$DEST/.env" ]; then
    kept="$(mktemp)"
    cp -p "$DEST/.env" "$kept"
  fi
  rm -rf "$DEST.src"
  git clone --depth 1 "$REPO" "$DEST.src"
  rm -rf "$DEST"
  mv "$DEST.src/platform" "$DEST"
  rm -rf "$DEST/../cicatrixa-src"
  mv "$DEST.src" "$DEST/../cicatrixa-src"
  if [ -n "$kept" ]; then
    mv "$kept" "$DEST/.env"
  fi
fi
cd "$DEST"

echo "── Settings"
if [ -f .env ]; then
  echo "  keeping the .env already here"
else
  cat > .env <<ENV
BASE_DOMAIN=$BASE_DOMAIN
BASE_URL=$BASE_URL
OPENAI_API_KEY=${OPENAI_API_KEY:-}
AI_MODEL=${AI_MODEL:-gpt-5.1-codex-mini}
ADMIN_EMAILS=${ADMIN_EMAILS:-}
RESEND_API_KEY=${RESEND_API_KEY:-}
MAIL_FROM=${MAIL_FROM:-Cicatrixa <noreply@$BASE_DOMAIN>}
STRIPE_SECRET_KEY=${STRIPE_SECRET_KEY:-}
STRIPE_WEBHOOK_SECRET=${STRIPE_WEBHOOK_SECRET:-}
ENV
  echo "  wrote .env for $BASE_DOMAIN"
fi
chmod 600 .env

# Traefik watches this directory for the self-heal demo's router. The compose
# default is the old server's path; on a fresh box point it at an empty one.
mkdir -p "$DEST/traefik/dynamic"
grep -q '^DEMO_DYNAMIC_DIR=' .env || printf '\nDEMO_DYNAMIC_DIR=%s\n' "$DEST/traefik/dynamic" >> .env

# The demo stack owns this network on a full server; the compose file joins it
# as external and will not start without it.
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet >/dev/null

echo "── Starting"
docker compose build control
docker compose up -d --remove-orphans
docker compose ps

echo "── Is it reachable from the internet?"
# Not "did compose say OK", and not a curl to 127.0.0.1 — both pass on a box the
# internet cannot reach. verify-public.sh only goes green once a trusted
# certificate is being served, which requires Let's Encrypt to have reached :80
# through public DNS. It exits 2, not 1, when the only thing missing is DNS.
./verify-public.sh
