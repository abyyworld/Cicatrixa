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
