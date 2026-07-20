# YC Application — Fall 2026 (FINAL DRAFT)

Company: **Cicatrixa** (from *cicatrix* — the scar left after a wound heals).
Live at **https://cicatrixa.com** · GitHub org: Cicatrixa · Domain + brand secured.

> ⏰ Deadline: **July 27, 2026, 8pm PT** (decision by Aug 28). Submit by July 26.
> Batch runs Oct–Dec in San Francisco.

---

## One-liner (≤ 50 chars)

**"Hosting where apps fix themselves."** (34 chars)

Backups:
- "Self-healing app hosting for $3/month." (38)
- "The $3 host with an SRE that never sleeps." (42)

## What does your company do?

Cicatrixa is a hosting platform where deployed apps heal themselves. Point it at
a GitHub repo — no Dockerfile, no config — and the AI works out how to build it,
wires a subdomain with HTTPS, and verifies the deploy actually serves before
calling it live. Then it stays on call: every push redeploys, a watchdog restarts
crashed containers within a minute, and when the crash is a real code bug, an AI
medic reads the live container logs and source, writes the fix, verifies the
patched build, commits it to GitHub, and redeploys — the owner finds a commit,
not a page.

It costs $2.99/month for 0.25 vCPU, 1 GB RAM, and 5 GB storage — subdomain,
HTTPS, one-click Postgres, and the medic included. That's cheaper than Heroku's
cheapest dyno, for a host that doesn't just run your app but keeps it alive.

## The pitch in one paragraph

Every indie developer and small team has the same story: the side project or
client app that went down on a weekend and stayed down until someone noticed.
Big companies solve this with on-call rotations; everyone else just has downtime.
We sell the thing only big companies had — an SRE watching production — as a
$2.99 line item on a hosting bill. The hosting is the distribution: because the
apps run on us, we already have the logs, the source, the build pipeline, and
the deploy rails, so "AI that fixes your app" needs zero integration work from
the user. Competitors selling AI debugging as a separate tool have to beg for
that access; we have it the moment you deploy.

## Why now

- Frontier models crossed the threshold where they can reliably go from a
  traceback + source to a *correct, verified* patch — that wasn't true 18 months ago.
- The indie/solo-builder population is exploding (AI codegen means far more
  deployed apps per developer), and none of those apps have anyone on call.
- The cheap-PaaS tier is stagnant: Heroku Eco $5, Render $7, Railway ~$5 — all
  of them page *you* when your app dies. Nobody competes on what happens after
  the crash.

## "Why not just ask a strong model?" — the objection to nail

A strong model is not on call. Between "a model that can debug" and "a platform
that heals itself" sits everything that isn't intelligence:

1. **Nobody asks at 3am.** The crash has to be noticed and acted on while the
   human is asleep. A chat window has no pager. Our watchdog does.
2. **Context assembly is the hard 80%.** The model needs the live container
   logs, the right slice of source, the deploy history, and a place to build.
   Because we're the host, we have all of it already — no agent to install,
   no permissions dance.
3. **Verification is enforced by the harness, not the prompt.** The medic's
   patch must match the real file verbatim, build cleanly, and pass the smoke
   test before the fix is ever offered or shipped. A hallucinated patch can't
   reach production.
4. **Deploy rails.** A model can't rebuild a container, swap traffic, and roll
   back. We own the last mile, which is where the actual risk lives.

The intelligence is a commodity we buy; the product is the closed loop around
it — plus the hosting margin that pays for it.

## Competitors & the edge

- **Cheap PaaS** (Heroku, Render, Railway, Fly.io): run your app, email you when
  it dies. None auto-fix. We match their price and add the part that matters.
- **AI SRE copilots** (Resolve.ai, Cleric, Traversal): sell to enterprises with
  existing on-call teams; they investigate and *explain*, a human ships the fix.
  We serve the 100× larger population that has no on-call team at all.
- **Sentry Seer / Copilot autofix**: patch from a stack trace inside the dev
  workflow — no live logs, no deploy loop, no verification against the running app.

The edge in one sentence: **because the apps run on us, the AI has production
access on day zero, and every fix ships with build + smoke-test verification —
they stop at a suggestion; we stop at healed traffic.**

Honest scoping (say it before a partner does): the medic owns the code-defect
slice; infra faults are handled by the dumber-but-reliable layers (restart,
rebuild, redeploy). That layering — cheap reflexes first, expensive intelligence
only when reflexes fail — is also the unit-economics answer.

## How do people use it? (all of this is live today)

1. Get an invite (invite-only while we scale the fleet) → sign up, 6-digit
   email verification.
2. Connect GitHub (GitHub App or PAT), pick one repo or several — each becomes
   its own service with its own subdomain; siblings get auto-wired env vars and
   an /api bridge (no CORS config, ever).
3. Deploy. The AI writes the Dockerfile if the repo has none, detects the port,
   smoke-tests, and gives an AI verdict on the deploy log — all streaming live.
4. One-click Postgres, wired into every service as DATABASE_URL.
5. Every git push redeploys (webhook + poll). Watchdog restarts/rebuilds crashed
   containers. Chat with the medic about any bug; approve its verified diff and
   it commits + redeploys.

## Business model

$2.99/mo per project (0.25 vCPU / 1 GB RAM / 5 GB storage, Postgres and medic
included). Server cost at current density: a $6/mo 8 GB VPS hosts ~7 paying
projects — infrastructure gross margin ~65% before AI spend; AI spend is
per-incident and small (one medic fix ≈ a few cents of tokens). Upsell path:
bigger tiers, team seats, then "bring your own infra" — the medic and deploy
brain as a control plane over the customer's cloud, at SaaS pricing. Land with
indie hackers and agencies (dozens of small client apps, no on-call), expand
upward.

## Progress (all real, all verifiable at cicatrixa.com)

- Platform built and shipped **in 3 days** (July 17–19, 2026): multi-tenant
  control plane, AI build engine, watchdogs, chat medic, Postgres provisioning,
  quotas (per-user + instance-wide), invite system, admin panel, email flows.
- Live on cicatrixa.com with automatic HTTPS per project subdomain.
- The full loop has run for real: the medic diagnosed a genuine production
  ZeroDivisionError from live logs, committed the fix to GitHub
  (`9230c72`), redeployed, and the endpoint went 500 → 200 — captured on video.
- Adversarial test fixtures (deliberately hostile repos: wrong README, broken
  Dockerfile, foreign API baked into inline scripts) found and fixed 4 real
  engine bugs — kept as a standing regression suite.
- Invite-only: [UPDATE AT SUBMIT: N users, M deployments, K medic fixes —
  pull live numbers from /admin].

## Demo assets

- 60-sec YC demo: raw screen capture of the real product, founder voiceover
  (see `yc/demo_voiceover.md`, cut at `demo/yc_demo.mp4`).
- 2-min brand film for the landing page: `demo/CICATRIXA_COMMERCIAL.mp4`.
- Partner walkthrough: demo account available on request (invite-gated).

## Founder section — fill in personally

- Who you are, what you've built before, why this problem is yours.
- If applying solo, say so plainly and cover it: shipping velocity above is the
  evidence (this platform went idea → live paying-ready product in 72 hours).
- Equity/incorporation: answer honestly; YC handles Delaware C-corp post-accept.

## Submission checklist (final week)

- [x] Narrative locked: self-healing hosting at $2.99/mo.
- [ ] Pricing visible on landing page.
- [ ] Admin stats page → real funnel numbers for the Progress line.
- [ ] YC demo video recorded (voiceover over raw takes).
- [ ] Founder video (30–60s, phone, plain).
- [ ] 10–20 real invites out; every active user counts.
- [ ] ToS/privacy stubs live (collecting emails).
- [ ] Server hardening + nightly DB backup (protect the traction data).
- [ ] Submit **July 26** — not at the deadline.
