# Runbook — "cicatrixa.com won't load"

## The outage of 2026-08-22

**Symptom:** browser says *"cicatrixa.com took too long to respond."* Vercel reports the
deployment Ready. Cloudflare reports **0 requests / 0 unique visitors** over 24h.

Vercel's *Ready* is a build status, not a domain status. The screen that would have shown this
is **Vercel → Project → Settings → Domains**, where `cicatrixa.com` reads *Invalid
Configuration* (or is absent). A finished build and a domain that points at you are
independent facts; only the first was true.

**Diagnosis:**

```
cicatrixa.com        A  169.58.36.128   ttl=300
www.cicatrixa.com    A  169.58.36.128
*.cicatrixa.com      A  169.58.36.128
NS: razvan / reza .ns.cloudflare.com
```

The A record answers with the **raw origin IP**, not a Cloudflare edge IP (`104.x` / `172.67.x`).
That means the records are **DNS-only (grey cloud)** — which is also why Cloudflare's analytics
show zero traffic: requests never reach Cloudflare, they go straight to the VPS.

So the browser connects to `169.58.36.128`, that box does not answer, and the request times out.
**The green Vercel deployment is irrelevant to `cicatrixa.com`, because the domain was never
pointed at Vercel.** Two independent problems wearing one symptom:

1. the apex points at the wrong origin, and
2. that origin is down.

## Fix 1 — get the marketing site loading (5 minutes, no server access needed)

Cloudflare → `cicatrixa.com` → **DNS** → Records.

| action | type | name | value | proxy |
|---|---|---|---|---|
| **edit** the existing apex record | A | `@` (`cicatrixa.com`) | `76.76.21.21` | **DNS only** (grey) |
| **edit/add** | CNAME | `www` | `cname.vercel-dns.com` | **DNS only** (grey) |
| **add** (so the platform keeps working) | A | `app` | `169.58.36.128` | DNS only |
| **leave alone** | A | `*` | `169.58.36.128` | DNS only |

The `app` row matters: today `app.cicatrixa.com` resolves only through the wildcard, so it would
follow the wildcard anywhere you later move it. Add the explicit record before touching the apex.

Verify with `dig +short cicatrixa.com` — it must stop returning `169.58.36.128`, and
`dig +short www.cicatrixa.com` must return Vercel's CNAME target rather than the wildcard's A.

Then in Vercel → the project → **Settings → Domains** → add `cicatrixa.com` and
`www.cicatrixa.com`. Use whatever record values that page shows if they differ from the two
above — Vercel is the source of truth for its own targets. Wait for both to read *Valid
Configuration*; TTL is 300s so propagation is minutes.

Keep the records **grey-clouded** until it resolves. Turning the orange cloud on afterwards is
fine, but then set **SSL/TLS → Overview → Full (strict)** first, or you get a redirect loop.

Order matters: add the domain in Vercel *before* flipping DNS if you can, so the certificate is
already issued when traffic arrives.

## Fix 2 — bring the platform back up (`app.cicatrixa.com`)

`platform/recover.sh` automates all of this. From your laptop:

```bash
SERVER=root@169.58.36.128 ./platform/recover.sh
```

It first establishes whether the box is reachable at all, and only then repairs the
stack: disk, a stale demo container holding `:80`, the missing `healnet` network, a
placeholder `BASE_DOMAIN`, then verifies `/healthz` over both HTTP and HTTPS.

**2026-08-23: stage 1 fails.** `ping`, `ssh`, `:80` and `:443` all time out — 100%
packet loss. Port 22 not answering means this is not a Docker, Traefik or config
problem; it is the host or the network in front of it. Nothing in this repo, and no
command run remotely, can reach it. Go to the VPS provider's console and check, in
this order: instance powered on, **unpaid invoice or suspension**, public IP still
`169.58.36.128`, security group still allowing 22/80/443. If the console says the
instance is running, use its web console / VNC / KVM to get a shell without SSH.

The manual sequence, once you have any shell:

```bash
ssh root@169.58.36.128

# 1. Is it even alive, and is it out of disk? (a full disk looks exactly like a hang)
uptime; df -h /; docker ps -a

# 2. Is Traefik listening on 80/443?
ss -lntp | grep -E ':80|:443'

# 3. Why is it not?
cd /root/cicatrixa-platform
docker compose ps
docker compose logs --tail=200 traefik
docker compose logs --tail=200 control

# 4. Reclaim disk if df said >90%
docker system prune -af --volumes=false

# 5. The single most likely config fault: .env never cut over from the nip.io bootstrap
grep -E 'BASE_DOMAIN|BASE_URL' .env
#   must be:  BASE_DOMAIN=cicatrixa.com
#             BASE_URL=https://app.cicatrixa.com

# 6. Bring it up (deploy.sh now creates healnet, refuses placeholder .env, and verifies)
SERVER=root@169.58.36.128 ./deploy.sh
```

From your laptop, prove it end to end:

```bash
curl -I  http://app.cicatrixa.com/healthz      # expect 301 -> https
curl -sI https://app.cicatrixa.com/healthz     # expect 200
curl -s  https://cicatrixa.com | head -5       # expect the marketing HTML from Vercel
```

## What each check proves

| check | green means | red means |
|---|---|---|
| `df -h /` under 90% | not a disk-fill hang | prune images; builds have been piling up |
| `ss -lntp` shows :80 and :443 | Traefik bound the ports | Traefik crashed or lost a port race — see §Two stacks |
| `docker compose ps` all Up | containers are running | read the logs of whichever is not |
| `/healthz` → 200 | control plane is serving and its event loop is not blocked | Traefik is up but the backend is hung or crash-looping |
| Cloudflare requests > 0 | traffic is reaching *somewhere* | still DNS — nothing is arriving at all |

## Two stacks, one server

`/root/Cicatrixa` (the self-heal demo) and `/root/cicatrixa-platform` (the product) used to
both want the container name `traefik` and ports 80 / 8080 / 9000. Running `docker compose up`
in the demo directory would take the website down with a port-already-allocated error.

The platform's Traefik is now named **`cx-traefik`**, and the demo stack only publishes its
ports when you ask for them:

```bash
cd /root/Cicatrixa
docker compose up -d                      # safe: no host ports, platform Traefik routes it
docker compose --profile standalone up -d # local dev only, never on the server
```

## Preventing the next one

- `deploy.sh` now refuses to deploy with a placeholder `BASE_DOMAIN`, creates the `healnet`
  network if missing, and curls `/healthz` through Traefik before declaring success.
- `HTTPS_REDIRECT_MW=cx-plain` in `.env` keeps plain HTTP serving if a certificate ever fails
  to issue, so a TLS problem degrades instead of blacking the site out. The redirect is also a
  302 now, not a 301 — a permanent redirect to a dead `:443` is cached by browsers forever, so
  flipping the switch afterwards would not rescue anyone who had already visited.
- Uptime check: point any monitor at `https://app.cicatrixa.com/healthz` and
  `https://cicatrixa.com`.
