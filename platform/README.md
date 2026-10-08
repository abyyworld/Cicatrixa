# Cicatrixa Platform

Multi-tenant, AI-operated hosting: users sign up, connect GitHub, pick a repo — the AI
clones it, figures out how to run it, builds a container, routes a subdomain to it,
verifies the deploy, and keeps it alive.

```
signup ─▶ connect GitHub ─▶ pick repo ─▶ AI deploy pipeline ─▶ live at <slug>.<domain>
                                          clone → analyze → Dockerfile → build →
                                          run → port-detect → route → smoke test
                                          (failures: AI diagnoses, patches, retries)
```

## Running it

Five ways in, and two of them need no server:

```bash
./local.sh                                   # this machine, no domain, no certificates
BASE_DOMAIN=cicatrixa.com CLOUDFLARE_TUNNEL_TOKEN=eyJ... ADMIN_EMAILS=you@example.com ./tunnel.sh
                                             # this machine, ON the internet, free — no public IP
first-boot.sh                                # a new box: paste its stub into cloud-init at creation
BASE_DOMAIN=cicatrixa.com ./bootstrap.sh     # a fresh box, run on the box itself
SERVER=root@<ip> ./deploy.sh                 # a box that already has a checkout
./status.sh                                  # where is the domain pointed, and what answers
```

`first-boot.sh` is the hands-off version of `bootstrap.sh`. Paste the three lines at its
top into the provider's cloud-init box when you create the instance (Oracle:
*Show advanced options → Management*). It enables root SSH on images that refuse it,
runs `bootstrap.sh`, and if DNS is not pointed yet it keeps checking in the background
and goes live once it is. Progress is in `/var/log/cicatrixa-first-boot.log`. Never put
API keys in cloud-init: the metadata service hands user data to every container on the box.

`tunnel.sh` is the free answer to "we have no server": any machine with Docker
serves `app.<domain>` and every project subdomain through a Cloudflare Tunnel. It
dials out, so there is no port to open and no certificate to issue — Cloudflare
terminates TLS for the domain it already runs DNS for. The Cloudflare clicks (create
the tunnel, two routes to `cx-traefik:80`, one wildcard record, Always Use HTTPS) are in
`docs/RUNBOOK.md`, "Free: Cloudflare Tunnel". The machine has to stay on: it is the server.

`local.sh` is the answer to "the server is broken": the control plane, the deploy
engine and the healer all run on your own machine, projects come up at
`<slug>.localhost`, and nothing is tied to a host you no longer have. The one thing
it cannot do is the GitHub App flow, which needs a public callback URL — connect with
a fine-grained PAT instead.

`bootstrap.sh` turns a rebuilt server into one command and a DNS record. It installs
Docker if the box has none, writes the `.env`, starts the stack, and then checks the
thing that actually matters — that the real hostname answers *through Traefik* —
rather than trusting that compose said OK. It prints the A records to set when it
is done.

What genuinely cannot be server-independent: building and running other people's
containers, holding :80 and :443, and keeping a health loop alive. That is what this
product does, so it needs a Docker host somewhere — your laptop counts.

## Which host serves what

- `cicatrixa.com` and `www.cicatrixa.com` are the marketing site on **Vercel**. The
  server never asks for their certificates: Traefik orders every `Host()` in a router
  as one certificate, and the apex's challenge would land on Vercel and fail the whole
  order, `app.` included.
- **`app.cicatrixa.com`** and `*.cicatrixa.com` (the projects people deploy) are served
  by the server, through `cx-traefik`.
- **`app.cicatrixa.com` must reach the server** — an A record to its IP, or a proxied
  CNAME to its Cloudflare tunnel (`tunnel.sh`) — not Vercel. Vercel has no
  control plane, so anything it serves on that hostname is a placeholder standing in
  front of the product: signup, login, the dashboard, GitHub connect and billing all
  live in `control/`.

When the control-plane host is down, the fix is to bring it back — not to point the app
subdomain at the static site. A placeholder there is indistinguishable from the product
being gone, and it silently swallows every "launch app" link on the marketing page. If a
holding page is unavoidable, put it on a hostname the product does not use, and take it
down in the same change that brings the host back.

To restore the app subdomain after an outage:

```bash
SERVER=root@<ip> ./deploy.sh          # brings up cx-traefik + cx-control, verifies :80
dig +short app.cicatrixa.com          # the server's A record (or, tunnel mode, Cloudflare's addresses) — not Vercel's
curl -sI https://app.cicatrixa.com/login   # 200 from cx-control, not the static site
```

## Architecture

- **`control/`** — FastAPI control plane (`cx-control`): landing + dashboard UI, auth
  (scrypt + signed cookies), SQLite (`/data` volume), GitHub integration, deploy engine,
  watchdogs, SSE live logs.
- **Traefik v3.6** — docker provider routes `Host(<slug>.<BASE_DOMAIN>)` to project
  containers by label. User apps run on the internal `cxnet` network with **no host
  ports published**, so port collisions cannot happen. File provider keeps serving the
  self-heal demo (`demo.<BASE_DOMAIN>`, weighted canary) and the authenticated
  dashboards on `:8080` / `:9000`.
- **AI engine** (`app/ai.py`, OpenAI Responses API): writes Dockerfiles for repos that
  lack them, diagnoses failed builds/boots and retries with a corrected plan (max 3
  attempts), and issues a post-deploy smoke-test verdict. Deterministic heuristics
  (node/python/go/static) cover the no-API-key case.
- **Watchdogs**:
  - GitHub push webhook (`/api/webhooks/github`) → instant redeploy.
  - Poll loop (3 min) compares HEAD SHA → redeploy — safety net / PAT connections.
  - Health loop (60 s): restarts dead containers (×2), then rebuilds from source.
  - Deploys are blue/green: the old container serves until the new one is verified.

## GitHub connect

Preferred: a **GitHub App** — admins create it in one click from `/admin` (manifest
flow pre-fills everything; credentials are exchanged and stored automatically). Users
then hit "Install & choose repos" and use GitHub's native repo picker; push webhooks
flow with no per-repo setup. Fallback: paste a PAT (fine-grained `Contents: read` +
`Metadata`, or classic `repo`).

## Tests

```bash
cd platform/control
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Kept out of the runtime image on purpose — `requirements.txt` is what ships.

## Deploy

```bash
cp .env.example .env      # set BASE_DOMAIN / BASE_URL / OPENAI_API_KEY / ADMIN_EMAILS
SERVER=root@<ip> ./deploy.sh
```

Until a real domain exists, `BASE_DOMAIN=<ip>.nip.io` gives working wildcard
subdomains for free. When the real domain arrives: point a wildcard `A` record
(`*.domain` and `domain`) at the server, update `BASE_DOMAIN`/`BASE_URL` in `.env`,
update `DEMO_ROUTER_RULE` in the demo's `.env`, and recreate the GitHub App URLs from
`/admin`.
