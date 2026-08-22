# 🩹 Self-Healing Production System

**An autonomous agent pipeline that keeps a live deployed app running with zero human intervention** — it detects crashes, diagnoses the root cause, *proves* the bug with a failing test, patches until the suite is green, and ships the fix through a real canary deploy. All on its own.

```
 crash ──▶ Watchdog ──▶ Diagnostician ──▶ Reproducer ──▶ Fixer ──▶ [Gate] ──▶ Deployer ──▶ healed
            logs +        LLM root-         failing        patch     one-click   Traefik
            health        cause from        test in        until     approve     weighted
            checks        indexed src       ephemeral      green     (optional)  canary
                                            container
```

## Why this is different

Most "LLM fixes your bug" demos guess a patch from a stack trace and hope. This system **refuses to patch blind**:

1. **Watchdog** tails container logs + polls health endpoints; detects error spikes (sliding window) and crashes.
2. **Diagnostician** (LLM reasoning model) gets the traceback + a token-efficient index of the source and returns a *structured* root-cause diagnosis — file, exact buggy code, fix hypothesis, reproduction input.
3. **Reproducer** — *the step nobody else does* — spins an **ephemeral container** and writes a **failing test** that reproduces the bug. If it can't make the bug fail deterministically, the pipeline stops. No repro, no patch.
4. **Fixer** iterates patches until (a) the reproduction test passes and (b) the **entire existing suite stays green** — both verified in ephemeral containers, never on the live app.
5. **Deployer** builds the patched image and ships it via **Traefik native weighted routing**: 20% canary traffic, watches error rate + logs for the watch window, then **auto-promotes to 100% or auto-rolls-back**. The old version stays at weight 0 as an instant rollback path.
6. **Optional human gate** (`APPROVAL_MODE`): `off` = fully autonomous (default), `dashboard` = one-click Approve/Reject on the verified diff right in the live dashboard.

Every stage streams live to an SSE dashboard.

## Quickstart

Requirements: Docker + Docker Compose, an OpenAI API key. One env var, two commands:

```bash
cp .env.example .env       # set OPENAI_API_KEY — that's the only required setting

# run from the repo root. The profile + overlay publish the host ports; they exist
# so this stack can also run on the production server, where the Cicatrixa platform's
# own Traefik already owns :80/:8080/:9000 and routes the demo through its file provider.
docker compose --profile standalone \
  -f docker-compose.yml -f docker-compose.standalone.yml up --build
```

- **http://localhost:9000** — self-healer dashboard (watch the pipeline live)
- **http://localhost** — the target app, behind Traefik weighted routing
- **http://localhost:8080** — Traefik dashboard (see the canary weights shift in real time)

## Run the demo

The target app is a small order API with a seeded bug: `discount_pct=100` causes an integer division by zero. Trigger it three times to trip the spike detector:

```bash
for i in 1 2 3; do curl -s -X POST http://localhost/trigger-bug; echo; done
```

Then watch **localhost:9000**. In a few minutes you'll see, end to end:

1. Crash detected (traceback captured from live logs)
2. Root cause pinned to the exact line in `app/main.py`
3. A new failing test appear in `app/tests/` — verified failing in a throwaway container
4. A minimal patch, verified: repro test passes, full suite green
5. A canary at 20% traffic on Traefik, then auto-promotion to 100%

Try the fixed endpoint: `curl -X POST http://localhost/trigger-bug` → no more 500.

## Human-in-the-loop (optional)

```bash
# .env — pick one
APPROVAL_MODE=off        # fully autonomous (default)
APPROVAL_MODE=dashboard  # verified diff + one-click Approve/Reject in the healer UI
```

Rejection (or timeout) rolls back all changes — fail safe.

## Architecture notes

- **Test-first patching is the core invariant.** The Fixer cannot run before a failing test exists. This turns "LLM guessed a patch" into "regression-tested engineering" and leaves a permanent test artifact behind after every heal.
- **Nothing is verified on the live app.** Repro and suite runs happen in ephemeral containers with the candidate source bind-mounted.
- **Canary, not big-bang.** Traefik's file provider watches `traefik/dynamic/dynamic.yml`; the Deployer edits weights (100/0 → 80/20 → 0/100) and Traefik hot-reloads. Rollback is a weight flip, not a redeploy.
- **After promotion the watchdog re-arms on the new primary** — the loop is continuous, not one-shot.
- Config is all env vars (`healer/config.py`); the LLM defaults to `gpt-5.1` (override with `HEALER_MODEL`) with Pydantic-validated structured outputs via the Responses API.

## Repo layout

```
app/          target service (FastAPI) + baseline test suite + seeded bug
healer/       the five agents + control plane
  watchdog.py       log tailing, health polling, spike detection
  diagnostician.py  structured root-cause via LLM
  reproducer.py     failing-test writer + ephemeral verification
  fixer.py          patch loop, suite gate, unified diff, rollback
  deployer.py       image build, canary, weight shifting, promote/rollback
  main.py           orchestrator + SSE dashboard + one-click approval gate
traefik/      static config + watched dynamic weights
```
