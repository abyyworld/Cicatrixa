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
BASE_URL="${BASE_URL:-https://$BASE_DOMAIN}"

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
  rm -rf "$DEST.src"
  git clone --depth 1 "$REPO" "$DEST.src"
  rm -rf "$DEST"
  mv "$DEST.src/platform" "$DEST"
  mv "$DEST.src" "$DEST/../cicatrixa-src" 2>/dev/null || true
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

# The demo stack owns this network on a full server; the compose file joins it
# as external and will not start without it.
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet >/dev/null

echo "── Starting"
docker compose build control
docker compose up -d --remove-orphans
docker compose ps

echo "── Does the hostname actually answer?"
ip="$(curl -fsS -m 10 https://api.ipify.org 2>/dev/null || echo "")"
ok=""
for _ in $(seq 1 30); do
  code="$(curl -sS -m 10 -o /dev/null -w '%{http_code}' -H "Host: $BASE_DOMAIN" http://127.0.0.1/login || true)"
  case "$code" in 200|30[0-9]) ok="$code"; break ;; esac
  sleep 2
done

echo
if [ -n "$ok" ]; then
  echo "  the platform answers for $BASE_DOMAIN through Traefik ($ok)."
  echo
  where="${ip:-the IP of this machine}"
  echo "  Point these at $where, and the certificate issues on the first request:"
  echo "    A   $BASE_DOMAIN        ${ip:-<ip>}"
  echo "    A   www.$BASE_DOMAIN    ${ip:-<ip>}"
  echo "    A   app.$BASE_DOMAIN    ${ip:-<ip>}"
  echo "    A   *.$BASE_DOMAIN      ${ip:-<ip>}     (the projects people deploy)"
  echo
  echo "  If the domain is attached to a static host such as Vercel, remove it there:"
  echo "  whichever answers DNS wins, and a static host has no control plane on it."
else
  echo "  the platform did not answer for $BASE_DOMAIN. What it said:" >&2
  docker compose logs --tail 40 control >&2
  exit 1
fi
