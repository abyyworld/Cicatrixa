#!/usr/bin/env bash
# A new server's first boot, run by cloud-init: install the platform, then keep
# checking until DNS points here and a trusted certificate is served. Nobody has
# to SSH in or re-run anything once DNS is set.
#
# When you create the instance, paste this into the provider's cloud-init box
# (Oracle: Show advanced options → Management → Paste cloud-init script;
# Hetzner: Cloud config):
#
#   #!/bin/bash
#   curl -fsSL --retry 10 --retry-all-errors -o /root/first-boot.sh \
#     https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/first-boot.sh
#   BASE_DOMAIN=cicatrixa.com ADMIN_EMAILS=you@example.com bash /root/first-boot.sh
#
# No API keys in there. The cloud's metadata service hands user data to any
# process on the box that asks, customer containers included. Add keys over SSH
# once it is up (the log's last lines say how).
#
# Watch it:      ssh ubuntu@<ip> sudo tail -f /var/log/cicatrixa-first-boot.log
# Run it again:  sudo bash /root/first-boot.sh   (BASE_DOMAIN is read from .env)
set -uo pipefail

export HOME="${HOME:-/root}"     # cloud-init and systemd-run leave it unset; git and docker want it
RAW="${RAW:-https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform}"
DEST="${DEST:-/root/cicatrixa-platform}"
LOG="${LOG:-/var/log/cicatrixa-first-boot.log}"
GO_LIVE_HOURS="${GO_LIVE_HOURS:-72}"    # how long to wait for DNS before giving up
DNS_STEP="${DNS_STEP:-60}"
# A failed certificate request counts against Let's Encrypt's limit of 5 failed
# validations per hostname per hour, and every Traefik restart makes one. Fifteen
# minutes apart stays under it however long DNS takes to settle.
CERT_RETRY="${CERT_RETRY:-900}"
UNIT=cicatrixa-go-live
APT_LOCK_CONF=/etc/apt/apt.conf.d/90cicatrixa-lock-timeout

log() { printf '%s  %s\n' "$(date -u '+%F %T')" "$*"; }

public_ip() {
  curl -4 -fsS --max-time 10 https://api.ipify.org 2>/dev/null \
    || curl -4 -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true
}

# Oracle's images (and AWS's) answer a root login with "Please login as the user
# ubuntu" and hang up. deploy.sh, recover.sh and the backup all need root, since
# the platform lives in /root. The default user's key becomes root's; sshd's
# default, prohibit-password, already allows key-only root logins.
enable_root_ssh() {
  grep -q 'Please login as the user' /root/.ssh/authorized_keys 2>/dev/null || return 0
  local home
  for home in /home/ubuntu /home/opc /home/debian /home/admin; do
    if [ -s "$home/.ssh/authorized_keys" ]; then
      install -m600 "$home/.ssh/authorized_keys" /root/.ssh/authorized_keys
      log "root SSH: enabled with the key of ${home##*/}"
      return 0
    fi
  done
  log "root SSH: still disabled — found no default user's key to give root"
}

# First boot is when Ubuntu runs its own package jobs. Wait them out rather than
# fail Docker's install on a held lock.
wait_for_apt() {
  [ -d "$(dirname "$APT_LOCK_CONF")" ] || return 0
  printf 'DPkg::Lock::Timeout "600";\n' > "$APT_LOCK_CONF"
  local _
  for _ in $(seq 1 120); do
    pgrep -x 'apt|apt-get|dpkg|unattended-upgr' >/dev/null || return 0
    sleep 5
  done
}

running_apps() {
  docker ps --filter label=traefik.enable=true --format '{{.Names}}' 2>/dev/null \
    | grep -vxE 'cx-control|cx-traefik' || true
}

done_message() {
  local domain="$1"
  log "LIVE: https://app.$domain"
  cat <<EOF

  Check from your laptop:   curl -sS https://app.$domain/healthz     (expect {"ok":true})

  Add API keys over SSH, never in cloud-init user data. As root on this box:
    curl -fsSL $RAW/bootstrap.sh | BASE_DOMAIN=$domain OPENAI_API_KEY=sk-... bash
  It keeps everything else in .env. Then back the database up off the box:
  docs/RUNBOOK.md, "Back up off the box".
EOF
}

# The part that runs in the background until the site is live. It is a function
# so bash has read all of it before running any: a re-run of bootstrap.sh
# replaces this file underneath it.
go_live() {
  cd "$DEST" || { log "go-live: $DEST is missing"; return 1; }
  local domain host ip resolved seen="-" rc pause allow deadline
  domain="$(grep -E '^BASE_DOMAIN=' .env | tail -1 | cut -d= -f2-)"
  host="app.$domain"
  ip="$(public_ip)"
  deadline=$(( $(date +%s) + GO_LIVE_HOURS * 3600 ))
  log "go-live: waiting for $host to point at ${ip:-this box}"

  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ -n "$ip" ] || ip="$(public_ip)"
    resolved="$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1; exit}')"
    if [ -n "$ip" ] && [ "$resolved" != "$ip" ]; then
      if [ "$resolved" != "$seen" ]; then      # say it once per change, not every minute
        log "waiting for DNS: $host → ${resolved:-nothing}, but this box is $ip."
        log "  In Cloudflare: A app → $ip and A * → $ip, both DNS only (grey cloud)."
        seen="$resolved"
      fi
      sleep "$DNS_STEP"
      continue
    fi

    log "DNS: $host → ${resolved:-?} — checking for a trusted certificate"
    # A restart drops every customer app, so it is only allowed while there are none.
    allow=1; [ -n "$(running_apps)" ] && allow=0
    VERIFY_ALLOW_RESTART="$allow" ./verify-public.sh
    rc=$?
    if [ "$rc" -eq 0 ]; then
      done_message "$domain"
      return 0
    fi
    pause="$CERT_RETRY"
    [ "$rc" -eq 2 ] && pause="$DNS_STEP"    # DNS changed back between the two checks
    seen="-"
    log "not live yet (verify-public exit $rc) — trying again in ${pause}s"
    sleep "$pause"
  done

  log "GAVE UP after ${GO_LIVE_HOURS}h: $host still is not live. The reason is above."
  log "  Once fixed:  cd $DEST && ./verify-public.sh"
  return 1
}

first_boot() {
  [ "$(id -u)" -eq 0 ] || { echo "run this as root (or with sudo)." >&2; return 1; }
  if [ -z "${BASE_DOMAIN:-}" ] && [ -f "$DEST/.env" ]; then
    BASE_DOMAIN="$(grep -E '^BASE_DOMAIN=' "$DEST/.env" | tail -1 | cut -d= -f2-)"
  fi
  [ -n "${BASE_DOMAIN:-}" ] || { log "set BASE_DOMAIN, e.g. BASE_DOMAIN=cicatrixa.com"; return 1; }
  export BASE_DOMAIN

  log "first boot: installing the platform for app.$BASE_DOMAIN"
  enable_root_ssh
  local v
  for v in OPENAI_API_KEY RESEND_API_KEY STRIPE_SECRET_KEY STRIPE_WEBHOOK_SECRET; do
    if [ -n "${!v:-}" ]; then
      log "!! $v is set. If it came from cloud-init user data, any container on this box"
      log "   can read it from the metadata service — rotate it and add the new one over SSH."
    fi
  done
  wait_for_apt

  local boot rc
  boot="$(mktemp)"
  if ! curl -fsSL --retry 10 --retry-all-errors -o "$boot" "$RAW/bootstrap.sh"; then
    log "FAILED: could not download $RAW/bootstrap.sh"
    rm -f "$boot" "$APT_LOCK_CONF"
    return 1
  fi
  bash "$boot"
  rc=$?
  rm -f "$boot" "$APT_LOCK_CONF"

  if [ "$rc" -eq 0 ]; then
    done_message "$BASE_DOMAIN"
    return 0
  fi
  if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx cx-control \
     || [ ! -x "$DEST/verify-public.sh" ]; then
    log "FAILED: the stack did not come up (bootstrap exit $rc). The reason is above."
    log "  Fix it, then run again:  sudo bash /root/first-boot.sh"
    return 1
  fi

  # Up, but not reachable by name yet — almost always DNS that is not set or not
  # propagated. Hand over to a background unit so cloud-init can finish.
  local self
  self="$(readlink -f "$0" 2>/dev/null || true)"
  [ -f "$self" ] || self="$DEST/first-boot.sh"
  log "the stack is up; app.$BASE_DOMAIN is not live yet (exit $rc)."
  log "Checking every minute in the background, for up to ${GO_LIVE_HOURS}h — nothing to do on this box."
  if command -v systemd-run >/dev/null; then
    systemctl stop "$UNIT" >/dev/null 2>&1 || true
    systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
    systemd-run --quiet --unit="$UNIT" --description="Cicatrixa: go live once DNS points here" \
      --setenv=HOME=/root --setenv=DEST="$DEST" --setenv=LOG="$LOG" --setenv=RAW="$RAW" \
      /bin/bash "$self" go-live
  else
    DEST="$DEST" LOG="$LOG" RAW="$RAW" nohup setsid /bin/bash "$self" go-live >/dev/null 2>&1 &
  fi
}

mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
case "${1:-}" in
  go-live) go_live ;;
  *)       first_boot ;;
esac
