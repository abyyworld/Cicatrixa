#!/usr/bin/env bash
# One-shot recovery for the Cicatrixa platform VPS. Run from your laptop:
#
#     SERVER=root@<ip> ./platform/recover.sh
#
# Stage 1 works out whether the box is reachable at all and says which layer is
# broken. Stage 2 only runs if SSH answers, and then diagnoses and repairs the
# stack: disk, stale demo containers holding :80, the healnet network, a
# placeholder BASE_DOMAIN. It ends with verify-public.sh on the box (a trusted
# certificate through Traefik; exit 0 live, 2 DNS pending, 1 broken) and then
# one request to https://app.<domain>/healthz from this machine.
#
# Needs root SSH (the platform lives in /root). On Oracle see docs/RUNBOOK.md.
set -uo pipefail

# No default: providers reassign IPs, and the old default is a dead box's.
SERVER="${SERVER:?set SERVER=root@<ip> — the box to recover}"
HOST="${SERVER#*@}"
DOMAIN="${DOMAIN:-cicatrixa.com}"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "stage 1 — is $HOST reachable at all?"

icmp=no; ssh_tcp=no; http=no; https=no
ping -c 3 -W 3 "$HOST" >/dev/null 2>&1 && icmp=yes
for probe in 22:ssh_tcp 80:http 443:https; do
  port=${probe%%:*}; var=${probe##*:}
  if command -v nc >/dev/null 2>&1 && nc -z -G 5 -w 5 "$HOST" "$port" >/dev/null 2>&1; then
    printf -v "$var" yes
  fi
done
echo "  ping   : $icmp"
echo "  tcp/22 : $ssh_tcp"
echo "  tcp/80 : $http"
echo "  tcp/443: $https"

if [ "$ssh_tcp" = no ]; then
  say "verdict — the machine is not reachable. Nothing on this box can be fixed from here."
  cat <<'TXT'
Port 22 does not answer, so this is not a Docker, Traefik or config problem.
It is the host or the network in front of it. In your VPS provider's console:

  1. Is the instance POWERED ON? (maintenance reboots do not always bring it back)
  2. Any UNPAID INVOICE or suspension notice? This is the most common cause of a
     server going completely dark with no warning.
  3. Is the public IP still the one this script probed? If it was reassigned, DNS
     is pointing at someone else's machine.
  4. Does the security group / firewall still allow 22, 80 and 443 inbound?

If the console says the instance is running, use its web console / VNC / KVM to
get a shell without SSH, then re-run the checks in docs/RUNBOOK.md by hand.

Your public website is NOT affected by this — cicatrixa.com and www are served
by Vercel and do not touch this machine. What is down is app.cicatrixa.com and
every deployed user app.
TXT
  exit 1
fi

say "stage 2 — SSH answers, repairing the stack"
# Ship the current check first: a box installed before verify-public.sh existed
# does not have it, and the stage below ends by running it.
scp -q -o ConnectTimeout=15 "$(cd "$(dirname "$0")" && pwd)/verify-public.sh" \
    "$SERVER:/root/cicatrixa-platform/verify-public.sh" 2>/dev/null \
  || echo "  (could not copy verify-public.sh — the check below says so if it is missing)"
ssh -o ConnectTimeout=15 "$SERVER" 'bash -s' -- "$DOMAIN" <<'REMOTE'
set -uo pipefail
DOMAIN="$1"
DEST=/root/cicatrixa-platform
step() { printf '\n-- %s\n' "$*"; }

step "host"
uptime
echo "memory:"; free -h 2>/dev/null | head -2

step "disk — a full disk looks exactly like a hang"
df -h /
used=$(df --output=pcent / 2>/dev/null | tr -dc '0-9')
if [ -n "${used:-}" ] && [ "$used" -ge 85 ]; then
  echo "!! / is ${used}% full — reclaiming docker space"
  docker system prune -af --volumes=false || true
  df -h /
fi

step "docker"
if ! docker info >/dev/null 2>&1; then
  echo "!! docker is not responding — starting it"
  systemctl start docker || true
  sleep 5
fi
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' || true

step "port :80 — the demo stack used to steal it from the platform"
if docker ps --format '{{.Names}}' | grep -qx traefik; then
  echo "!! a container literally named 'traefik' (the old demo stack) is running."
  echo "   stopping it so the platform's cx-traefik can bind :80/:443"
  docker stop traefik && docker rm traefik
fi

step "healnet — 'external: true' aborts the whole stack when it is missing"
docker network inspect healnet >/dev/null 2>&1 || docker network create healnet

if [ ! -d "$DEST" ]; then
  echo "!! $DEST does not exist — run platform/deploy.sh first"
  exit 1
fi
cd "$DEST"

step "config"
if [ ! -f .env ]; then
  echo "!! no .env — copying the template"
  cp .env.example .env
fi
cur_domain=$(grep -E '^BASE_DOMAIN=' .env | cut -d= -f2-)
cur_url=$(grep -E '^BASE_URL=' .env | cut -d= -f2-)
echo "  BASE_DOMAIN=$cur_domain"
echo "  BASE_URL=$cur_url"
case "$cur_domain" in
  ""|*nip.io|localhost|*example.com)
    echo "!! placeholder BASE_DOMAIN — every Traefik router rule is built from this,"
    echo "   so the real hostname matches nothing. Correcting it (backup: .env.bak)"
    cp .env .env.bak
    sed -i "s|^BASE_DOMAIN=.*|BASE_DOMAIN=$DOMAIN|" .env
    sed -i "s|^BASE_URL=.*|BASE_URL=https://app.$DOMAIN|" .env
    grep -E '^BASE_(DOMAIN|URL)=' .env
    ;;
esac

step "bringing the stack up"
docker compose up -d --remove-orphans
docker compose ps

step "verifying from the internet's point of view"
# A trusted certificate or nothing: the old check used curl -k and passed on
# Traefik's self-signed fallback, i.e. on a box the internet could not reach.
# No BASE_DOMAIN override: the script reads .env, which is what the stack runs
# with — including when the box's domain differs from the one passed in.
if [ ! -f ./verify-public.sh ]; then
  echo "!! verify-public.sh is missing on the box — run platform/deploy.sh to ship the current tree"
  exit 1
fi
bash ./verify-public.sh
rc=$?
if [ "$rc" -eq 0 ]; then
  echo "Redeploy each user project once so its containers get the HTTPS Traefik"
  echo "labels — they are baked in at container creation."
fi
exit "$rc"
REMOTE
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"

say "from here — the one check the box cannot do for itself"
DEST=/root/cicatrixa-platform
# Ask the box, over one connection, for the domain it serves and the address the
# internet sees for it. The request below is pinned to THAT address — not to
# SERVER, which may be a VPN, Tailscale or LAN address that never passes the
# provider firewall this check exists to see. Laptop-side code avoids bash-4-only
# features: it runs under macOS's /bin/bash 3.2.
# A box in tunnel mode (tunnel.sh) has no public address to pin to: visitors
# reach it through Cloudflare, so the request below goes the way theirs do.
remote='d=$(grep -E "^BASE_DOMAIN=" '"$DEST"'/.env 2>/dev/null | tail -1 | cut -d= -f2-); echo "D=$d"; i=$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null); echo "I=$i"; t=$(grep -E "^COMPOSE_PROFILES=" '"$DEST"'/.env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\042\047" | tr ", " "\n\n" | grep -cx tunnel); echo "T=$t"'
sshrc=0
info="$(ssh -o ConnectTimeout=15 "$SERVER" "$remote" 2>&1)" || sshrc=$?
if [ "$sshrc" -ne 0 ]; then
  echo "✗ could not reach $SERVER over ssh after the deploy (exit $sshrc): $info" >&2
  exit 1
fi
BOX_DOMAIN="$(printf '%s\n' "$info" | sed -n 's/^D=//p' | tail -1)"
PUBLIC_IP="$(printf '%s\n' "$info" | sed -n 's/^I=//p' | tail -1)"
TUNNEL="$(printf '%s\n' "$info" | sed -n 's/^T=//p' | tail -1)"
[ "${TUNNEL:-0}" = "0" ] || PUBLIC_IP=""
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
  if [ "${TUNNEL:-0}" = "0" ]; then
    echo "  check the provider firewall / security list for 80 and 443." >&2
  else
    echo "  the box is in tunnel mode: on it, ./verify-public.sh says what Cloudflare is missing." >&2
  fi
  exit 1
fi
