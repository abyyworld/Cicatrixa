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

## Standing up the platform on a new box

The old VPS (169.58.36.128) is gone. A new one is one command plus two DNS records.
**Firebase cannot host this** — see "Hosting the platform" in `CLAUDE.md` for why.

### Pick a box

| | cost | arch | catch |
|---|---|---|---|
| **Hetzner CX22** (or any €4–5 VPS) | ~€4/mo | x86 | none — the clean answer |
| **Oracle Cloud Always Free, A1** | $0, card on file | ARM64 | Oracle's terms bar "service bureau" use; instances were disabled and deleted in 2026. Stopgap only — back up the database off the box |

### Oracle only — the console clicks that matter

1. Sign up at oracle.com/cloud/free. The **home region is permanent**; pick one near your users.
2. Recommended: upgrade to **Pay As You Go** (Billing → Upgrade), then **Budgets → $1/month alert**.
   PAYG is exempt from the Always Free idle-reclaim rule (under 20% CPU, network and memory for
   7 days can reclaim a free instance — a quiet platform looks exactly like that). Stay inside the
   free shape below and it bills $0.
3. **Networking → VCN wizard → "Create VCN with Internet Connectivity".** Then the public subnet's
   **Default Security List → Add Ingress Rules**: `0.0.0.0/0` TCP **80**, and `0.0.0.0/0` TCP **443**.
   Do not open 8080 or 9000.
4. **Compute → Create instance**: image **Canonical Ubuntu 24.04** (not Oracle Linux — Docker's
   installer rejects it), shape **Ampere VM.Standard.A1.Flex, 2 OCPU / 12 GB** — nothing larger,
   public subnet, "Assign a public IPv4 address" on, your SSH key. "Out of host capacity" means try
   another availability domain, or retry later.

You do **not** need to touch the Ubuntu image's iptables rules. Docker publishes 80/443 through the
`FORWARD` chain, ahead of the image's `REJECT` rule; the security list above is what decides. (Never
run `netfilter-persistent reload` once Docker is running — it wipes Docker's chains.)

### Every box — DNS first, then one command

Set DNS **before** running the script, so the certificate can issue on the first try.
Cloudflare → DNS, both **DNS only** (grey cloud):

```
A   app   <new-ip>
A   *     <new-ip>
```

Leave `cicatrixa.com` and `www` on Vercel. Remove `app.cicatrixa.com` from the Vercel project's
domains if it is attached there. Then:

```bash
# Hetzner and most VPSes log you in as root:
ssh root@<new-ip>
curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
  | BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... ADMIN_EMAILS=hello@cicatrixa.com bash

# Oracle logs you in as ubuntu. The variables go AFTER sudo — sudo drops any
# set before it, and the script would stop with "set BASE_DOMAIN":
ssh ubuntu@<new-ip>
curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
  | sudo BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... ADMIN_EMAILS=hello@cicatrixa.com bash
```

`BASE_URL` now defaults to `https://app.cicatrixa.com`. It ends by running `verify-public.sh`:

| result | meaning |
|---|---|
| `✓ … is live` (exit 0) | DNS points here **and** a trusted certificate is being served — only possible if Let's Encrypt reached the box on :80 from the internet |
| `… PENDING DNS` (exit 2) | the stack is up; DNS does not point here yet. It prints the records. Re-run the same command once it resolves — `.env` and its keys are kept |
| `✗` (exit 1) | stack down, or DNS is right and no certificate after 3 minutes — it lists what to check |

It restarts Traefik once if no certificate has issued after a minute, because a request that failed
before DNS was ready is not retried until Traefik reloads. Last step, from your laptop:
`curl -sS https://app.cicatrixa.com/healthz` — the one thing the server cannot check about itself is
a firewall that allows :80 but blocks :443.

### Back up off the box

The database is the `cx-data` volume. On Oracle especially, keep a copy elsewhere. `sqlite3`'s
online backup is consistent while the app is writing; copying the file is not (WAL mode):

```bash
ssh root@<ip> 'docker exec cx-control python -c "import sqlite3;s=sqlite3.connect(\"/data/cicatrixa.db\");d=sqlite3.connect(\"/data/backup.db\");s.backup(d)" && docker exec cx-control cat /data/backup.db' > cicatrixa-$(date +%F).db
```

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
