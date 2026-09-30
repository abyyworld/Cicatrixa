#!/usr/bin/env bash
# Prove that app.<BASE_DOMAIN> works from the internet, not just from this box.
#
# Run it on the server, from the platform directory (bootstrap.sh, deploy.sh and
# recover.sh all call it). Exit codes:
#   0  public: DNS points here and a TRUSTED certificate is being served
#   2  pending: the stack is up, but DNS does not point at this box yet
#   1  broken: the stack is down, or DNS is right and the certificate never issued
#
# Why a trusted certificate is the test: a curl to 127.0.0.1 proves only that
# the containers started — it goes around every cloud firewall, security list
# and DNS mistake there is. Let's Encrypt can only issue a certificate after
# reaching :80 on this box through public DNS, so a certificate issued DURING
# THIS RUN proves the internet could reach the box a moment ago. One issued
# earlier (already in acme.json) proves only that DNS and the stack are right:
# a firewall closed since then would not show. The script says which case it
# is, and deploy.sh / recover.sh follow up with a real check from outside.
#
# VERIFY_ALLOW_RESTART=1 lets it restart Traefik once to retry a certificate
# request that failed before DNS was ready. Only bootstrap.sh sets it: on a box
# with customer apps, restarting Traefik drops every one of them.
set -uo pipefail

cd "$(dirname "$0")"
if [ -z "${BASE_DOMAIN:-}" ] && [ -f .env ]; then
  BASE_DOMAIN="$(grep -E '^BASE_DOMAIN=' .env | tail -1 | cut -d= -f2-)"
fi
if [ -z "${BASE_DOMAIN:-}" ]; then
  echo "verify-public: BASE_DOMAIN is not set and not in .env" >&2
  exit 1
fi

HOST="app.$BASE_DOMAIN"
START="$(date +%s)"
TIMEOUT="${VERIFY_TIMEOUT:-180}"     # seconds to wait for a trusted certificate
STEP="${VERIFY_STEP:-5}"
RESTART_AFTER="${VERIFY_RESTART_AFTER:-60}"
ALLOW_RESTART="${VERIFY_ALLOW_RESTART:-0}"

# When the certificate being served was issued (its notBefore), as epoch seconds,
# or empty if that cannot be read.
cert_issued_at() {
  command -v openssl >/dev/null 2>&1 || return 0
  local nb
  nb="$(echo | openssl s_client -connect 127.0.0.1:443 -servername "$HOST" 2>/dev/null \
          | openssl x509 -noout -startdate 2>/dev/null | cut -d= -f2)"
  [ -n "$nb" ] && date -d "$nb" +%s 2>/dev/null || true
}

say() { printf '  %s\n' "$*"; }

# ── 1. is the stack up at all? ──────────────────────────────────────────────
up=""
for _ in $(seq 1 12); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
            -H "Host: $HOST" http://127.0.0.1/healthz 2>/dev/null || true)"
  case "$code" in 200|30[0-9]) up="$code"; break ;; esac
  sleep "$STEP"
done
if [ -z "$up" ]; then
  echo "✗ Traefik is not answering for $HOST on this box — the stack itself is down." >&2
  docker compose ps >&2 2>/dev/null || true
  docker compose logs --tail 40 traefik control >&2 2>/dev/null || true
  exit 1
fi
say "stack is up (Traefik answered $up for $HOST on 127.0.0.1)"

# ── 2. does DNS point here? ─────────────────────────────────────────────────
ip="$(curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null \
      || curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true)"
resolved="$(getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1; exit}')"

if [ -n "$ip" ] && [ "$resolved" != "$ip" ]; then
  echo
  echo "… PENDING DNS — $HOST resolves to '${resolved:-nothing}', not this box ($ip)."
  echo
  echo "  In your DNS provider, set these two records, proxy OFF (DNS only):"
  echo "    A   app.$BASE_DOMAIN   $ip"
  echo "    A   *.$BASE_DOMAIN     $ip     (the projects people deploy)"
  echo
  echo "  Leave $BASE_DOMAIN and www alone if a marketing site is hosted elsewhere."
  echo "  If '${resolved:-}' is a Cloudflare address, the record is proxied: switch it to DNS only."
  echo "  Once it resolves, re-run this script (or bootstrap.sh) — it is safe to repeat."
  exit 2
fi
[ -n "$ip" ] && say "DNS: $HOST → $ip (this box)"
[ -z "$ip" ] && say "could not learn this box's public IP — skipping the DNS comparison"

# ── 3. is a TRUSTED certificate being served? ───────────────────────────────
restarted=""
waited=0
last=""
while [ "$waited" -lt "$TIMEOUT" ]; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
            --resolve "$HOST:443:127.0.0.1" "https://$HOST/healthz" 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$code" = "200" ]; then
    issued="$(cert_issued_at)"
    echo
    # Let's Encrypt backdates notBefore by about an hour, so "fresh" allows two.
    if [ -n "$issued" ] && [ "$issued" -ge $((START - 7200)) ]; then
      echo "✓ $HOST is live: DNS points here and Let's Encrypt issued a trusted certificate"
      echo "  just now, which it can only do after reaching this box on :80 from the internet."
    elif [ -n "$issued" ]; then
      echo "✓ $HOST: DNS points here and the stack serves its trusted certificate."
      echo "  That certificate was issued earlier ($(date -u -d "@$issued" '+%F %H:%M UTC')), so this"
      echo "  does NOT prove the internet can reach the box right now — check from outside:"
    else
      echo "✓ $HOST: DNS points here and a trusted certificate is being served"
      echo "  (its issue date could not be read). Confirm from outside:"
    fi
    echo
    echo "  From your laptop (the only check that sees the firewall as a visitor does):"
    echo "    curl -sS https://$HOST/healthz      # expect {\"ok\":true}"
    exit 0
  fi
  case "$rc" in
    60|35|51|58|77) last="certificate not trusted yet (Traefik is still on its self-signed fallback)" ;;
    7)              last="connection refused on :443" ;;
    28)             last="timed out on :443" ;;
    0)              last="HTTP $code from /healthz" ;;
    *)              last="curl exit $rc" ;;
  esac
  # A certificate request that failed before DNS was ready is not retried until
  # Traefik reloads, and one restart re-triggers it. Only when allowed: on a box
  # with customer apps a restart drops them all.
  if [ "$ALLOW_RESTART" = "1" ] && [ -z "$restarted" ] && [ "$waited" -ge "$RESTART_AFTER" ]; then
    say "no trusted certificate after ${waited}s — restarting Traefik once to retry issuance"
    docker compose restart traefik >/dev/null 2>&1 || true
    restarted=1
  fi
  sleep "$STEP"
  waited=$((waited + STEP))
done

echo >&2
echo "✗ $HOST resolves here but is not serving a trusted certificate after ${TIMEOUT}s." >&2
echo "  last result: $last" >&2
echo >&2
echo "  Let's Encrypt must reach this box on :80 from the internet. In order:" >&2
echo "   1. the provider's firewall / security list allows TCP 80 and 443 from 0.0.0.0/0" >&2
echo "   2. the DNS record is DNS only (grey cloud), not proxied" >&2
echo "   3. what Traefik says — the log names every domain in the failed order; any" >&2
echo "      name there that is NOT pointed at this box (e.g. an apex on Vercel) fails" >&2
echo "      the whole certificate:  docker compose logs traefik | grep -iE 'acme|certificate'" >&2
echo "   4. Let's Encrypt rate limits (too many failed attempts pause issuance for an hour)" >&2
if [ "$ALLOW_RESTART" != "1" ]; then
  echo "   5. a request that failed before DNS was ready is only retried when Traefik" >&2
  echo "      reloads; if nothing else is hosted here yet: docker compose restart traefik" >&2
fi
exit 1
