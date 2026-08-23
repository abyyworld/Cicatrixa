#!/usr/bin/env bash
# One-shot recovery for the Cicatrixa platform VPS. Run from your laptop:
#
#     SERVER=root@169.58.36.128 ./platform/recover.sh
#
# Stage 1 works out whether the box is reachable at all and says which layer is
# broken. Stage 2 only runs if SSH answers, and then diagnoses and repairs the
# stack: disk, stale demo containers holding :80, the healnet network, a
# placeholder BASE_DOMAIN, and finally verifies /healthz over HTTP and HTTPS.
set -uo pipefail

SERVER="${SERVER:-root@169.58.36.128}"
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

step "verifying through Traefik"
ok80=FAILED; ok443=FAILED
for i in $(seq 1 45); do
  if [ "$ok443" = FAILED ]; then
    c=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
          --resolve "app.$DOMAIN:443:127.0.0.1" "https://app.$DOMAIN/healthz" 2>/dev/null)
    [ "$c" = 200 ] && ok443=ok && echo "  :443 -> 200 (${i}s)"
  fi
  if [ "$ok80" = FAILED ]; then
    c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
          -H "Host: app.$DOMAIN" http://127.0.0.1/healthz 2>/dev/null)
    case "$c" in 200|301|302|308) ok80=ok; echo "  :80  -> $c (${i}s)" ;; esac
  fi
  [ "$ok80" = ok ] && [ "$ok443" = ok ] && break
  sleep 1
done

echo
echo "RESULT: :80 $ok80, :443 $ok443"
if [ "$ok80" = ok ] && [ "$ok443" = ok ]; then
  echo "✓ app.$DOMAIN is serving. Redeploy each user project once so its containers"
  echo "  get the HTTPS Traefik labels (they are baked in at container creation)."
else
  echo "✗ still not serving — logs follow"
  docker compose logs --tail=80 traefik control
  if [ "$ok80" = ok ] && [ "$ok443" = FAILED ]; then
    echo
    echo "Only :443 failed, so the certificate never issued. Let's Encrypt validates"
    echo "over HTTP-01 on :80 — confirm app.$DOMAIN resolves to THIS box and that :80"
    echo "is open inbound from the internet."
  fi
fi
REMOTE
