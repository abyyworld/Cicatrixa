#!/usr/bin/env bash
# Where is app.cicatrixa.com actually pointed, and is anything serving it?
#
# Answers the question without needing anybody: DNS first, then what answers on
# :80 and :443, then — only if a key gets you in — what the server thinks it is
# running. Every check prints what it saw, so a wrong answer is diagnosable
# rather than just red.
#
#   ./status.sh                          # uses BASE_DOMAIN from .env, or cicatrixa.com
#   DOMAIN=example.com ./status.sh
#   SERVER=root@169.58.36.128 ./status.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/.env" ] && eval "$(grep -E '^(BASE_DOMAIN|SERVER)=' "$HERE/.env" || true)"
DOMAIN="${DOMAIN:-${BASE_DOMAIN:-cicatrixa.com}}"
SERVER="${SERVER:-root@169.58.36.128}"
HOST_ONLY="${SERVER#*@}"

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red()   { printf '\033[31m%s\033[0m\n' "$1"; }
warn()  { printf '\033[33m%s\033[0m\n' "$1"; }

echo "domain: $DOMAIN    server: $SERVER"
echo

# ── 1. DNS ────────────────────────────────────────────────────────────────
# The whole outage was this line disagreeing with the one below it: the app
# subdomain resolving to a static host that has no control plane on it.
server_ip="$(getent hosts "$HOST_ONLY" 2>/dev/null | awk '{print $1; exit}')"
[ -z "$server_ip" ] && server_ip="$HOST_ONLY"
echo "── DNS"
for host in "$DOMAIN" "www.$DOMAIN" "app.$DOMAIN"; do
  answer="$(dig +short "$host" A | tr '\n' ' ' | sed 's/ $//')"
  answer="${answer:-(no A record)}"
  if [ "$host" = "app.$DOMAIN" ]; then
    case " $answer " in
      *" $server_ip "*) green  "  $host → $answer  (the server)" ;;
      *"(no A record)"*) red   "  $host → $answer" ;;
      *)                 red   "  $host → $answer  (NOT the server — $server_ip)" ;;
    esac
  else
    echo "  $host → $answer"
  fi
done
echo

# ── 2. What answers ───────────────────────────────────────────────────────
# Asked twice: through DNS as a visitor sees it, and straight at the server's
# IP with the Host header set, which is what Traefik routes on. When those two
# disagree, the platform is up and the name is pointed somewhere else.
probe() {  # probe <url> <host-header|"">
  local url="$1" host="${2:-}" args=(-sS -m 15 -o /dev/null -w '%{http_code}')
  [ -n "$host" ] && args+=(-H "Host: $host" --resolve "$host:443:$server_ip" --resolve "$host:80:$server_ip")
  curl "${args[@]}" "$url" 2>/dev/null || echo "---"
}
echo "── What answers"
for scheme in http https; do
  code="$(probe "$scheme://app.$DOMAIN/login" "")"
  case "$code" in
    200|30[0-9]) green "  $scheme://app.$DOMAIN/login → $code" ;;
    ---|000)     red   "  $scheme://app.$DOMAIN/login → no answer" ;;
    *)           warn  "  $scheme://app.$DOMAIN/login → $code" ;;
  esac
done
direct="$(probe "http://$server_ip/login" "app.$DOMAIN")"
case "$direct" in
  200|30[0-9]) green "  the server itself, asked for app.$DOMAIN → $direct (control plane is up)" ;;
  ---|000)     red   "  the server itself → no answer on :80 (platform is down, or the box is)" ;;
  *)           warn  "  the server itself, asked for app.$DOMAIN → $direct" ;;
esac
echo

# ── 3. The server's own account of itself ─────────────────────────────────
echo "── On the server"
if ssh -o BatchMode=yes -o ConnectTimeout=8 "$SERVER" true 2>/dev/null; then
  ssh "$SERVER" 'cd /root/cicatrixa-platform 2>/dev/null || cd /root/Cicatrixa/platform 2>/dev/null || { echo "  (no platform checkout found)"; exit 0; }
    docker compose ps --format "  {{.Name}}  {{.State}}  {{.Status}}" 2>/dev/null || docker ps --format "  {{.Names}}  {{.Status}}"
    echo "  ---"
    grep -E "^(BASE_DOMAIN|BASE_URL)=" .env 2>/dev/null | sed "s/^/  /" || echo "  (no .env)"'
else
  warn "  no SSH from here (no key, or the box is unreachable)"
  echo "     if the box is alive, this is a key problem; if it is not, that is the outage."
fi
echo

cat <<'NOTE'
── What each answer means
  app.<domain> resolves somewhere other than the server
      → the name is pointed at the static site. Point the A record at the
        server, and remove the hostname from the Vercel project.
  the server answers on :80 but the public name does not
      → the platform is fine; it is only DNS.
  the server does not answer at all
      → bring it back:  SERVER=root@<ip> ./deploy.sh
        Any Docker host will do; nothing in the stack is tied to that machine.
NOTE
