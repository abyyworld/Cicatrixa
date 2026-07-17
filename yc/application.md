# YC Application — Working Draft

Name: **Cicatrixa** (from *cicatrix* — the scar left after a wound heals; the
product leaves a permanent regression test behind after every fix). Domain
secured: cicatrixa.com. GitHub org: Cicatrixa.

> ⚠️ Timing: YC now runs four batches a year. It's mid-July 2026 — check
> ycombinator.com/apply TODAY for the next deadline (Fall '26 applications
> historically close in early August). You may have ~2–3 weeks, which changes
> the checklist below from "nice pace" to "sprint."

---

## The one-liner (≤ 50 chars)

**"AI SRE that proves the fix before shipping it."** (46 chars)

Backups:
- "Fixes production bugs with tests, not guesses."
- "Self-healing infrastructure for production apps."

## What does your company do? (long answer)

When production breaks at 3am, an engineer gets paged, reads logs, reproduces the
bug, writes a fix, waits for CI, and ships — a 2–6 hour loop that burns your best
people. Cicatrixa closes that loop autonomously: it watches your logs and health
checks, diagnoses the root cause from your source code, and — this is the part
nobody else does — **writes a failing test that reproduces the bug in a sandbox
before it writes a single line of fix**. Only when that test exists does it patch,
and it iterates until the reproduction test passes and your entire existing suite
stays green. Then it ships through a real canary deploy (a slice of live traffic,
auto-promote or auto-rollback) and leaves the regression test in your repo forever.

Every other "AI fixes your bug" tool patches from a stack trace and hopes. A stack
trace tells you where the code died, not what correct behavior is. A failing test
is the only machine-checkable definition of a bug — it converts LLM guessing into
verifiable engineering. That invariant ("no repro, no patch") is our moat-in-miniature
and our brand.

## Why now

- Incident response is the last unautomated stage of the DevOps pipeline: CI/CD
  automated shipping, observability automated *seeing*, nothing automated *fixing*.
- Frontier reasoning models crossed the threshold where they can reliably write a
  targeted failing test from a traceback + source — that wasn't true 18 months ago.
- On-call burnout is a top-3 attrition driver for infra teams; downtime costs are
  board-visible. Buyers already pay for Datadog/PagerDuty/Sentry — we sit on top
  of that spend and close the loop they open.

## "Why not just ask a strong model?" — the objection to nail

Every reviewer will think it: engineers already have Claude/GPT in their terminal;
why pay for another debugging app? The answer, which should appear near-verbatim
in the application and on the landing page:

**A strong model is not on call.** Between "a model that can debug" and "a
production system that heals itself" sits everything that isn't intelligence:

1. **Nobody asks at 3am.** The incident has to be *noticed*, triaged against noise,
   and acted on while the human is asleep. A chat window has no pager.
2. **Context assembly is the hard 80%.** The model needs the traceback, the right
   slice of source, recent deploys, and a place to run code. Engineers do this by
   hand every time; we do it automatically every time.
3. **A sandbox with a burden of proof.** Ad-hoc model use produces a plausible
   patch. Our pipeline cannot ship anything that lacks a previously-failing,
   now-passing test plus a green suite. The invariant is enforced by the harness,
   not by prompt discipline.
4. **Deploy rails.** A model can't canary itself onto 20% of traffic and roll
   itself back. We own the last mile, which is where the actual risk lives.

The intelligence is a commodity component we buy; the product is the closed loop
around it. Nobody says "why do you need CI, you can run tests by hand" — we are
CI for incident response.

## Competitors & the edge

Resolve.ai, Cleric, Traversal (AI SRE copilots — investigate and *explain*, a
human still writes and ships the fix); Sentry Seer / GitHub Copilot autofix
(patch from stack trace — no reproduction, no deploy loop); Datadog Bits /
incident.io (summarize and route).

The edge, stated as one sentence: **they stop at a diagnosis or a guessed patch;
we stop at healed traffic, and every fix we ship carries a machine-checkable
proof.** Trust is the product, not the patch. Teams will never auto-merge a
patch a model "thinks" is right; they will happily auto-merge one that arrives
with a red-then-green test, a green suite, and a canary behind it — that's the
same bar they hold humans to. We automate the evidence, not just the edit.

Honest scoping (say this before a partner does): many incidents are infra/config/
data problems, not code bugs. The AI SRE copilots chase that whole surface; we
deliberately own the code-defect slice end-to-end, because it's the slice where
proof is possible and full autonomy is therefore earnable. Beachhead, not ceiling.

## How do people use it? (distribution answer — also the honest roadmap)

- **Today (prototype/demo):** docker-compose sidecar next to a containerized app.
- **Product (what customers actually install):**
  1. Connect an alert source — Sentry / Datadog / CloudWatch webhook (5 min).
  2. Install the GitHub App (repo read + PR write).
  3. On incident: we reproduce in **our** sandboxed cloud runners, and the output
     lands as a **pull request in their repo**: failing test + minimal fix + canary
     config. The PR *is* the human gate — approve = merge = their existing CD ships it.
  4. Autonomy is a dial: PR-only → auto-merge-on-green → full closed loop for
     teams that earn confidence.
- **Customers never run our Docker stack.** Zero infra change, no agent in their
  prod, no socket access. That's the difference between a demo and a product.

## Business model

Per-repo/per-seat SaaS with usage-based incident pricing. Anchor: one prevented
sev-2 (~eng-hours + downtime) >> $500/mo. Land with on-call-heavy Series A–C
startups (10–100 engineers, Sentry/Datadog already installed), expand to platform
teams. Later: enterprise self-hosted runners.

## Progress

Working end-to-end system: crash detection → root-cause diagnosis → sandboxed
failing-test reproduction → verified patch (repro passes + full suite green) →
Traefik weighted-canary deploy with auto-promote/auto-rollback → live dashboard.
[By application time this line should also say: "N design partners, M real bugs
healed in real repos" — see checklist.]

---

## 60-second demo video script

One unbroken screen recording, three windows tiled: terminal, healer dashboard
(localhost:9000), Traefik dashboard (localhost:8080). Voiceover, no music.

- **0:00–0:08** — "This is a live order API in production. I'm going to break it."
  `curl -X POST localhost/trigger-bug` ×3 → three 500s on screen.
- **0:08–0:20** — Dashboard: watchdog stage lights up, real traceback captured.
  "Our agent caught the crash from the logs. Now watch what it does *before*
  writing any fix."
- **0:20–0:35** — Reproducer stage: failing test appears. "It wrote a failing test
  and proved the bug in a sandboxed container. No reproduction, no patch — that's
  the rule. This is what every other AI-fixes-bugs tool skips."
- **0:35–0:48** — Fixer: diff on screen, "repro test passes, full suite green."
  Deployer: cut to Traefik weights flipping 100/0 → 80/20 → 0/100. "Verified fix,
  shipped as a canary to real traffic, auto-promoted."
- **0:48–0:60** — `curl -X POST localhost/trigger-bug` → 200. "Bug found, proven,
  fixed, tested, and deployed. Zero human intervention, under N minutes. We're
  Cicatrixa — the AI SRE that proves the fix before shipping it."

Record 5+ takes; the run is nondeterministic. Keep the best. Never demo live.

## Pre-application checklist (in priority order)

1. **Rehearse the loop until it's boring.** Run end-to-end 10+ times; fix flakes.
2. **Add 2–3 more seeded bug types** (unhandled None, off-by-one in pagination,
   bad exception swallowing) — proves generality in 30 extra seconds of video.
3. **Kill the "toy repo" objection:** run the pipeline against a real OSS FastAPI
   project with a real historical bug re-introduced. Screenshot it.
4. **Design partners (the single highest-leverage item):** DM 20 infra/platform
   engineers you know; offer to heal one real bug free. Target: 3–5 "yes, we'd
   pilot" — named logos or quotes go straight into the application.
5. **Record the demo video** (script above) + founder video (30s, plain, why you).
6. **Ship the landing page** (`site/index.html` in this repo) to Vercel/Netlify
   with a waitlist form; put the demo video on it. Post to HN/Twitter — waitlist
   count is a traction line.
7. **Fill the application** from this doc. Short declarative sentences. No hype
   words YC filters ("revolutionary", "platform", "ecosystem").
