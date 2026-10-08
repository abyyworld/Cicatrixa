# Cicatrixa — project brief

Paste-able context for a fresh chat. Read this first.

## What Cicatrixa is

**AI-operated hosting that heals itself.** Users sign up, connect GitHub, pick a repo —
the platform clones it, works out how to run it, writes a Dockerfile if there isn't one,
builds a container, routes a subdomain to it, smoke-tests it, and then keeps it alive:
when a container dies or a deploy breaks, an LLM agent diagnoses it, proves the bug with a
failing test, patches until the suite is green, and ships through a weighted canary.

Pricing: **$2.99/mo**, 14-day free trial, referral invites (+20% quota per subscribed friend).
Owner: kenny09077@gmail.com. Cloudflare account: Annolieberto@gmail.com.

## The two things in this repo (do not confuse them)

| | `site/` + `vercel.json` | `platform/` |
|---|---|---|
| what | static marketing page (one 44KB `index.html`) | the real product: FastAPI control plane + Traefik |
| runs on | **Vercel** (static) | **any machine with Docker** — none live as of 2026-10-08 |
| serves | `cicatrixa.com`, `www.` | `app.cicatrixa.com`, `*.cicatrixa.com` user apps, `demo.` |
| deploy | git push → Vercel | free, any machine: `platform/tunnel.sh` (Cloudflare Tunnel); new box: paste `platform/first-boot.sh`'s stub into cloud-init; by hand: `platform/bootstrap.sh`; existing box: `SERVER=root@<ip> ./platform/deploy.sh` |

There is a **third**, older thing: the repo root (`docker-compose.yml`, `app/`, `healer/`,
`traefik/`) is the original **self-heal demo** — a seeded-bug FastAPI order service plus the
five healing agents. It is the YC demo, not the product. On the server it lives at
`/root/Cicatrixa` and the platform's Traefik mounts its `traefik/dynamic/` as a file provider.

## Live topology

Cloudflare DNS, zone `85494004e4d82090c4c4f618664caace`, registrar Cloudflare, expires 2027-07-17.
Nothing in this repo can change a DNS record — there is no Cloudflare API call anywhere in it.
**Check reality before trusting this table:** `dig +short app.cicatrixa.com`.

```
                 RECORD TODAY (2026-09-30)                  TARGET
  cicatrixa.com  CNAME → cname.vercel-dns.com  (Vercel) ✓   unchanged
  www            CNAME → cname.vercel-dns.com  (Vercel) ✓   unchanged
  app            CNAME → cname.vercel-dns.com  (Vercel) ✗   A → <platform box>, DNS only
                                                              or tunnel: CNAME → <id>.cfargotunnel.com, proxied
  *              A     → 169.58.36.128  (dead box)      ✗   A → <platform box>, DNS only
                                                              or tunnel: CNAME → <id>.cfargotunnel.com, proxied
```

The apex and `www` belong to Vercel and must stay there. `app` and `*` need a Linux box running
Docker. Both records already exist, so they are **edited**, not added: Cloudflare will not create
an A record beside an existing CNAME of the same name. Then remove `app.cicatrixa.com` from the
Vercel project so nothing else claims it. In tunnel mode the two records are instead **deleted**
and replaced by proxied CNAMEs to the tunnel (`app` is made by the tunnel's route, `*` by hand) —
`docs/RUNBOOK.md`, "Free: Cloudflare Tunnel".

## Hosting the platform — what can and cannot run it

The control plane drives a local Docker daemon through `/var/run/docker.sock` (engine, dbprovision,
medic, metrics, watchdog, main) to build and run customer containers, keeps SQLite in WAL mode on
a persistent volume, runs always-on loops, and routes wildcard `*.cicatrixa.com` through Traefik.
So it needs **a host with Docker** — a Linux box with root, or in tunnel mode any machine running
Docker (Docker Desktop on Mac/Windows included). Researched and fact-checked 2026-09-30:

- **Firebase / Cloud Run / Functions / App Hosting: cannot run it, at any price.** No Docker daemon
  (gVisor sandbox, no privileged mode), no lock-safe disk for SQLite (GCS FUSE has no locking; NFS
  mounts are forced no-lock), loops stall after a response, and wildcard routing needs a ~$18/mo load
  balancer. Anything server-side on Firebase also needs the Blaze plan, i.e. a card. The only Google
  route is a rewrite to per-customer Cloud Run services + Firestore — not worth it.
- **Oracle Cloud Always Free, Ampere A1: the only free host that runs it unchanged.** 2 OCPU / 12 GB
  ARM64 (halved from 4/24 on 2026-06-15), 200 GB disk, card required but not charged. Risks: Oracle's
  Cloud Services Agreement limits use to "internal business operations" and bars "service bureau"
  use — selling hosting on it is plausibly a breach, and Oracle has disabled and deleted Always Free
  instances in 2026. Treat it as a stopgap and back up `/data/cicatrixa.db` off the box. Its images
  refuse root SSH, which `deploy.sh` / `recover.sh` need — `first-boot.sh` fixes it at first boot. Customer
  repos that assume x86 (amd64-only binaries, npm lockfiles missing arm64 optional deps) will fail
  with "exec format error"; `ai.py` tells the model the host architecture to reduce this.
- **A ~€5–6/mo x86 VPS (the smallest Hetzner cloud plan, or similar): the clean answer.** No ToS
  risk, no ARM surprises. Hetzner renamed and repriced its plans in 2026 — check before quoting one.
- **Any machine you already have + Cloudflare Tunnel (`platform/tunnel.sh`, added 2026-10-08): $0.**
  `cx-tunnel` (cloudflared, compose profile `tunnel`) dials out to Cloudflare, which already runs the
  domain's DNS and terminates TLS at its edge, and forwards `app.` and `*.` to `cx-traefik:80`. No
  public IP, open port or Let's Encrypt. The catch is the machine: it must stay on, and customer
  code runs on it and can reach its LAN. Zero Trust's Free plan may ask for a card; it is not charged.
- GCP e2-micro (1 GB RAM), AWS (credits, time-limited), Azure (12 months) and every PaaS without a
  Docker socket were rejected.

`docs/RUNBOOK.md` has the exact steps for a new box of either kind.

On the box, in `/root/cicatrixa-platform` (the repo's `platform/` directory):
- **cx-traefik** (v3.6) — :80 and :443 public (in tunnel mode every port is on loopback, and
  cx-tunnel reaches :80 over `cxnet`); :8080 dashboard and :9000 healer UI bound to
  127.0.0.1 only (reach them over an SSH tunnel — Docker-published ports bypass the host firewall).
  Docker provider on `cxnet`, file provider on `DEMO_DYNAMIC_DIR`. Let's Encrypt HTTP-01 via resolver `le`.
- **cx-control** — FastAPI on :8090, SQLite at `/data/cicatrixa.db` (volume `cx-data`).
  User app containers run on `cxnet` with **no published host ports** — Traefik routes by label,
  so port collisions are impossible.

## Control plane map (`platform/control/app/`)

| file | job |
|---|---|
| `main.py` | routes, auth cookies, signup + email code, forgot/reset password, SSE, webhooks (GitHub push, Stripe) |
| `engine.py` | the deploy pipeline: clone → analyze → Dockerfile → build → run → port-detect → label → smoke test. Blue/green; old container serves until the new one verifies |
| `ai.py` | OpenAI Responses API: writes Dockerfiles, diagnoses failures, smoke-test verdicts. Falls back to node/python/go/static heuristics with no API key |
| `watchdog.py` | 3 loops: GitHub poll (180s), container health (60s, restart ×2 then rebuild), metrics (60s) |
| `medic.py` | chat medic — investigates, proposes patches, verifies them against the real file before offering Apply |
| `patching.py` | find/replace application. A patch applies only if `find` occurs **exactly once** — ambiguity is refused, never guessed |
| `verify.py` | verification evidence and the levels it supports. `level_for()` computes the level; callers never assert one. A green suite with no coverage of the changed lines is `unverified_no_coverage` |
| `flywheel.py` | `break_observation` (per incident, tenant-scoped) + `transform` (promoted, tenant-agnostic, **zero customer code** — enforced on write). Library lookup fires *before* generation; unattended-merge-rate and hit-rate queries live here |
| `fingerprint.py` | libcst structural hash of a call site — identifiers stripped, literals bucketed. Matches the same break across repos without storing anyone's source. Python only |
| `db.py` | SQLite schema + versioned migrations (`schema_version` in `settings`, explicit up/down, raises on failure). `conn()` is thread-local |
| `auth.py` `gh.py` `billing.py` `invites.py` `referrals.py` `mailer.py` `metrics.py` `dbprovision.py` `bus.py` | scrypt+signed cookies / GitHub App+PAT / Stripe / invite gate / referral quota / Resend / cgroup usage / on-demand Postgres / SSE pub-sub |

Conventions: blocking work goes through `asyncio.to_thread`; background threads reach the loop
via `asyncio.run_coroutine_threadsafe`. Keep it that way — a blocking call in an `async def`
freezes every request in the process.

## Config that actually matters

`platform/.env` on the server (never committed, `.gitignore`d):
`BASE_DOMAIN` and `BASE_URL` build **every Traefik Host rule and every cookie's Secure flag**
(`HTTPS_ENABLED = BASE_URL.startswith("https://")`). Get these wrong and nothing routes.

## Failure modes seen so far

1. **Domain pointed at the VPS while the site lives on Vercel** → browser timeout, Cloudflare
   reports 0 requests (DNS-only records bypass Cloudflare entirely). Fixed 2026-08-23 by pointing
   the apex and `www` at Vercel in the Cloudflare dashboard.
2. **`.env` never cut over from the `<ip>.nip.io` bootstrap value** → no router matches the real
   host; HTTPS falls back to Traefik's self-signed cert.
3. **Two stacks fighting** — the demo stack and the platform stack both wanted the container name
   `traefik` and ports 80/8080/9000. Running `docker compose up` in `/root/Cicatrixa` used to take
   the website down. The platform's Traefik is now `cx-traefik` and the demo no longer publishes
   those ports when the platform is present.
4. **Disk fill** from per-deploy images — `engine._prune_images` keeps only the live tag per slug;
   build cache still needs an occasional `docker system prune`.

## Rules of thumb

- Never point `cicatrixa.com` (apex) at the VPS again — it belongs to Vercel.
- Never publish host ports on user containers; route by Traefik label.
- Tunnel mode (`COMPOSE_PROFILES=tunnel` in `.env`) sets `HTTPS_REDIRECT_MW=cx-tunnel`: Traefik sees
  plain HTTP from the tunnel even for HTTPS visitors, so its own redirect would loop, and its :80
  entrypoint overwrites X-Forwarded-Proto with `http` — `cx-tunnel` puts back `https` (else apps
  that force HTTPS loop forever) and sends `Cloudflare-CDN-Cache-Control: no-store` (nothing can
  purge Cloudflare's cache after a redeploy). No compress there: it holds response headers, and
  Cloudflare cuts a response with none after ~100 s. Cloudflare's **Always Use HTTPS** does the
  redirect, and sign-in depends on it (cookies are Secure). Anything that may outlast ~100 s in a
  request goes in a background task; SSE streams send a comment at once and every 20 s.
  `verify-public.sh` checks the visitor's path in this mode — out through public DNS to Cloudflare
  and back through the tunnel — instead of waiting for a Let's Encrypt certificate.
- Admin is never granted for typing an address. An `ADMIN_EMAILS` address becomes admin when its
  emailed code is entered; otherwise `docker exec cx-control python -m app.promote <email>`, which
  needs a shell on the server, creates or takes over the account with a random password (ending
  every session on it) and prints a one-time link to set one. Without `ADMIN_EMAILS`, the first
  account on a fresh instance is the admin — on a public instance, whoever signs up first;
  `tunnel.sh` refuses to run without it. A code dies after 5 wrong guesses; an account gets one
  new code a minute (whether or not the email went out) and 25 wrong guesses an hour.
- Sessions and pending cookies, like reset links, carry a tag of the password hash
  (`auth.make_session`), so a new password ends every older one. Every token signed with the key
  has its own prefix (`session:`, `pending:`, `reset:`, `gh:`) — none may be accepted as another.
  A pending cookie never opens an already-verified account.
- A GitHub installation is bound to an account only with proof. The `gh:` state must name the
  signed-in account (no link from elsewhere binds anything), and GitHub's OAuth code (the App asks
  for authorization on install; `/connect/github/authorize` for existing installations) must show
  the installation is on the person's own account or an organisation they administer
  (`gh.installations_user_controls` — seeing it is not enough: collaborators and members can, and
  an installation token reaches all its repos). One bound to another account is always refused.
- Customer code never gets the platform's `/data` volume: it holds cicatrixa.db (the signing key,
  password hashes, the GitHub App key) and every tenant's checkout. The medic's test runs get a
  COPY of their checkout (`engine.test_runner`, `put_archive`).
- A deploy candidate starts with `traefik.enable=false`; only after it answers does a routed copy
  start, and the old container retire. `cx-retry` hands a request that cannot connect (a routed
  copy still starting) to another container. Known gap: requests sent to the OLD container while
  it stops gracefully can wait a few seconds — Traefik only drops it once it has exited.
- Password reset links are signed tokens bound to the current password hash (`auth.make_reset`):
  setting a new password kills every link issued for the old one, so no table tracks them. With
  email off or failing, `/admin` → "reset pw" makes a link to hand over manually, and `/admin`
  shows Resend's last error — "domain is not verified" there means nobody can get a signup code.
- Verify with `platform/verify-public.sh` on the box, never "compose said OK", never `curl -k`
  (`-k` accepts Traefik's self-signed fallback, so it passes on a box nobody can reach). It
  passes only on a **trusted** certificate. A certificate issued *during the run* proves Let's
  Encrypt reached :80 through public DNS; one issued earlier proves only DNS and the stack, and
  the script says which. Only a request from **outside** the box sees today's firewall — so
  `deploy.sh` and `recover.sh` end with one from the laptop. Keep it that way.
- No router with `tls.certresolver` may name the apex or `www`. Traefik turns every `Host()` in
  a rule into ONE certificate order; the apex's challenge lands on Vercel, fails, and fails the
  whole order — `app.` included. The control-plane routers match `app.<domain>` only
  (a test in `tests/test_engine_config.py` enforces it).
- User-app routers use the fixed middlewares `cx-app-web` (:80; a chain over `HTTPS_REDIRECT_MW`
  and `cx-retry`) and `cx-app-tls` (:443). They are defined on **cx-traefik's** labels, never on
  cx-control: Traefik drops a stopped or starting container's labels, and every app went down with
  the control plane. Never bake the switch's value into app labels: labels freeze when a
  container is created, so the switch would stop reaching running apps.
- `BASE_URL` must be `https://app.cicatrixa.com`, never the apex: it builds the GitHub App
  callback, referral invite links and the Stripe checkout return URL. Point those at the static
  Vercel page and GitHub install, invites and billing all break silently.
- The healer rewrites `traefik/dynamic/dynamic.yml` wholesale on every canary step
  (`healer/deployer.py:_write_weights`). Anything hand-edited into that file must also be
  emitted there or it survives only until the next heal.
- Secrets live only in `platform/.env` on the server. `.env.example` is the template.
  Never in cloud-init user data: the cloud's metadata service (169.254.169.254) hands it to
  any process that asks, and customer containers can reach it.
- Never let a caller pick a verification level. `verify.level_for(evidence)` decides;
  `assert_supported()` raises on anything higher. One dishonest record is permanent and
  silent, because nothing downstream re-derives it.
- Promotion to `transform` needs ≥2 observations across ≥2 tenants at `verified_reproduction`
  or above. `insert_transform` enforces it; nothing promotes automatically yet.
- New schema goes in `db.MIGRATIONS` as a `Migration` with a real `down`. Never in the
  legacy block — that one still swallows errors and only exists to reach parity on old DBs.
