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
| runs on | **Vercel** (static) | **VPS 169.58.36.128** (Docker Compose) |
| serves | `cicatrixa.com`, `www.` | `app.cicatrixa.com`, `*.cicatrixa.com` user apps, `demo.` |
| deploy | git push → Vercel | `SERVER=root@169.58.36.128 ./platform/deploy.sh` |

There is a **third**, older thing: the repo root (`docker-compose.yml`, `app/`, `healer/`,
`traefik/`) is the original **self-heal demo** — a seeded-bug FastAPI order service plus the
five healing agents. It is the YC demo, not the product. On the server it lives at
`/root/Cicatrixa` and the platform's Traefik mounts its `traefik/dynamic/` as a file provider.

## Live topology

Cloudflare DNS, zone `85494004e4d82090c4c4f618664caace`, registrar Cloudflare, expires 2027-07-17.
Nothing in this repo can change a DNS record — there is no Cloudflare API call anywhere in it.
**Check reality before trusting this table:** `dig +short cicatrixa.com`.

```
                     TARGET                    AS OF 2026-08-22
  cicatrixa.com      Vercel                    169.58.36.128   ← wrong, this is the outage
  www                Vercel                    169.58.36.128   ← via the wildcard
  app                169.58.36.128             169.58.36.128   ← via the wildcard, no explicit record
  *                  169.58.36.128             169.58.36.128   ✓
  demo               169.58.36.128             169.58.36.128   ✓
```

The apex belongs to Vercel and the wildcard to the VPS. Until the first two rows are cut over in
the Cloudflare dashboard by hand, `cicatrixa.com` reaches the VPS and the Vercel build — however
green — serves nobody. `docs/RUNBOOK.md` has the exact records.

VPS 169.58.36.128, `/root/cicatrixa-platform`:
- **traefik v3.6** — :80 :443 :8080 (dashboard) :9000 (healer UI). Docker provider on `cxnet`,
  file provider on the demo's dynamic dir. Let's Encrypt HTTP-01 via resolver `le`.
- **cx-control** — FastAPI on :8090, SQLite at `/data/cicatrixa.db` (volume `cx-data`).
  User app containers run on `cxnet` with **no published host ports** — Traefik routes by label,
  so port collisions are impossible.

## Control plane map (`platform/control/app/`)

| file | job |
|---|---|
| `main.py` | routes, auth cookies, SSE, webhooks (GitHub push, Stripe) |
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
   reports 0 requests (DNS-only records bypass Cloudflare entirely). Open as of 2026-08-22 —
   it needs a human in the Cloudflare dashboard. See `docs/RUNBOOK.md`.
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
- Verify a deploy by hitting `/healthz` through Traefik **over HTTPS**, not by "compose said OK".
  A 3xx from `:80` proves nothing — a redirect to a `:443` that cannot answer is the outage.
- `BASE_URL` must be `https://app.cicatrixa.com`, never the apex: it builds the GitHub App
  callback, referral invite links and the Stripe checkout return URL. Point those at the static
  Vercel page and GitHub install, invites and billing all break silently.
- The healer rewrites `traefik/dynamic/dynamic.yml` wholesale on every canary step
  (`healer/deployer.py:_write_weights`). Anything hand-edited into that file must also be
  emitted there or it survives only until the next heal.
- Secrets live only in `platform/.env` on the server. `.env.example` is the template.
- Never let a caller pick a verification level. `verify.level_for(evidence)` decides;
  `assert_supported()` raises on anything higher. One dishonest record is permanent and
  silent, because nothing downstream re-derives it.
- Promotion to `transform` needs ≥2 observations across ≥2 tenants at `verified_reproduction`
  or above. `insert_transform` enforces it; nothing promotes automatically yet.
- New schema goes in `db.MIGRATIONS` as a `Migration` with a real `down`. Never in the
  legacy block — that one still swallows errors and only exists to reach parity on old DBs.
