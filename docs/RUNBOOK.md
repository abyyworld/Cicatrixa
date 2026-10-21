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

> **Done 2026-08-23 — kept as history.** Do not re-apply this table: its `app` row points at the
> old box, which is dead. For a new box see "Standing up the platform on a new box" below.

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
| **Smallest Hetzner x86 cloud plan** (or any comparable VPS) | ~€5–6/mo incl. IPv4 — check hetzner.com/cloud, plans and prices changed in 2026 | x86 | none — the clean answer |
| **Oracle Cloud Always Free, A1** | $0, card on file | ARM64 | Oracle's terms bar "service bureau" use; instances were disabled and deleted in 2026. Stopgap only — back up the database off the box |
| **Any machine you already have + Cloudflare Tunnel** (`platform/tunnel.sh`) | $0 — Cloudflare's Zero Trust sign-up may ask for a card on its Free plan, but does not charge it | whatever it is | it must stay on and awake; customer code runs on it; no public IP or open port needed |

### Free: Cloudflare Tunnel, on any machine with Docker

The domain's DNS is already on Cloudflare, so Cloudflare can carry the traffic too. `cx-tunnel`
(cloudflared) dials **out** from the machine; Cloudflare sends `app.cicatrixa.com` and every
`<project>.cicatrixa.com` back down that connection to `cx-traefik`, and serves HTTPS on the
certificate it already holds for the domain. No public IP, no port forwarding, no firewall rule,
no Let's Encrypt — a home PC behind a router works, and so does an Oracle box with nothing opened.

What it costs you instead: **the machine is the server.** Asleep, off, or offline, the site shows
a Cloudflare error. Customer containers run on it and can reach its local network — use a
machine or VM that holds nothing else you care about, not your everyday laptop. On Apple Silicon
or a Raspberry Pi, repos that assume x86 can fail with "exec format error", as on Oracle.

**1. Create the tunnel** — Cloudflare dashboard → **Zero Trust** → **Networks → Tunnels** →
*Create a tunnel* → **Cloudflared** → name it `cicatrixa`. (Menu names move; search the dashboard
for "Tunnels" if these are gone. A first visit to Zero Trust asks you to pick a plan: **Free**.)
The next screen shows install commands containing a long string starting with `eyJ` — that is the
token. Copy any of those commands; `tunnel.sh` takes the token out of whatever you paste.

**2. Clear the two old records** — Cloudflare → `cicatrixa.com` → **DNS**: **delete** `app`
(CNAME → Vercel) and `*` (A → the dead `169.58.36.128`). Cloudflare will not add a record next to
an existing one of the same name. Leave `cicatrixa.com` and `www` alone — they are Vercel's.

**3. Give the tunnel its two routes** — back in the tunnel, *Public hostnames* (newer dashboards:
*Published application routes*) → add both, service type **HTTP**, URL **`cx-traefik:80`**:

| subdomain | domain | service |
|---|---|---|
| `app` | `cicatrixa.com` | HTTP `cx-traefik:80` |
| `*` | `cicatrixa.com` | HTTP `cx-traefik:80` |

The `app` route creates its DNS record for you. The `*` one does not (the dashboard warns about it):

**4. Add the wildcard record by hand** — DNS → add **CNAME** `*` → `<tunnel-id>.cfargotunnel.com`,
**Proxied** (orange cloud). The tunnel ID is on the tunnel's page, and `tunnel.sh` prints the exact
target if it is missing.

**5. Turn on HTTPS everywhere** — **SSL/TLS → Edge Certificates → Always Use HTTPS: On.** Sign-in
cookies are HTTPS-only, so without it anyone who types `http://` gets a login that never logs in.
It only affects proxied records, so the Vercel site is untouched.

**6. Vercel** → the project → **Settings → Domains** → remove `app.cicatrixa.com`.

**7. On the machine** (Docker installed and running — Docker Desktop on Mac/Windows; on Windows
run this inside WSL):

```bash
git clone https://github.com/abyyworld/cicatrixa.git && cd cicatrixa/platform
BASE_DOMAIN=cicatrixa.com CLOUDFLARE_TUNNEL_TOKEN='eyJ...' ADMIN_EMAILS=you@example.com ./tunnel.sh
```

`ADMIN_EMAILS` is your own address (comma-separate several); `tunnel.sh` will not start
without it. Typing an address into the signup form does **not** make anyone admin — with email
off, anyone could type yours. Make your admin account on the machine instead:

```bash
docker exec cx-control python -m app.promote you@example.com
```

It creates the account (or takes it back, if someone registered your address first: their
password and sessions go) and prints a one-time link, valid for an hour, to choose its password.
With `RESEND_API_KEY` set, signing up as an `ADMIN_EMAILS` address and entering the emailed code
works too.

Add `OPENAI_API_KEY=sk-...` (and `RESEND_API_KEY`, Stripe keys) on the same line when you have
them; everything is kept in `platform/.env` (mode 600, never committed). It ends with
`verify-public.sh`, which in this mode checks the visitor's own path — out to Cloudflare through
public DNS and back in through the tunnel — then that `http://` redirects and that a project
subdomain arrives too:

| result | meaning |
|---|---|
| `✓ … is live through the Cloudflare tunnel` (exit 0) | make your admin account as above (the script prints the command); everyone else signs up at `https://app.cicatrixa.com/signup` |
| `… PENDING` (exit 2) | the stack runs here; a DNS record or route is missing, Always Use HTTPS is off, or the tunnel has not connected — it says which, with the tunnel ID filled in |
| `✗` (exit 1) | stack down, tunnel not connected (a wrong token shows in the tunnel's log it prints), or a route pointing somewhere other than `cx-traefik:80` |

What tunnel mode does for every app, so nobody has to: it tells apps the request was HTTPS
(Traefik only sees plain HTTP from the tunnel, and an app that forces HTTPS would otherwise
redirect forever), and it tells Cloudflare not to cache them (nothing here can purge its cache
after a redeploy). The visitor's address reaches apps as the `CF-Connecting-IP` header.

Updating later, or after a reboot (`restart: unless-stopped` brings it back on its own once
Docker starts): `git pull && ./tunnel.sh`. Taking it offline: `./tunnel.sh stop`. Moving it to a
real server later is the A-record path below; delete the tunnel's records first.

Back up the database off the machine, as on any box (same command as below, minus the ssh):

```bash
f=cicatrixa-$(date +%F).db
docker exec cx-control python -c "import sqlite3;s=sqlite3.connect(\"file:/data/cicatrixa.db?mode=ro\",uri=True);d=sqlite3.connect(\"/data/backup.db\");s.backup(d);d.close()" \
  && docker exec cx-control cat /data/backup.db > "$f.tmp" \
  && [ "$(head -c 15 "$f.tmp")" = "SQLite format 3" ] && mv "$f.tmp" "$f" && echo "saved $f" \
  || { echo "backup FAILED" >&2; rm -f "$f.tmp"; }
```

### Payments (any box)

The 14-day trial needs nothing. To take the $2.99 subscription, Stripe has to tell the platform
when someone pays — without the webhook, people can pay and never be credited:

1. Stripe dashboard → **Developers → API keys**: the secret key → `STRIPE_SECRET_KEY`.
2. **Developers → Webhooks → Add endpoint**: URL `https://app.cicatrixa.com/api/webhooks/stripe`,
   events **`checkout.session.completed`** and **`invoice.paid`**. Its signing secret
   (`whsec_…`) → `STRIPE_WEBHOOK_SECRET`.
3. Put both in `platform/.env` (or pass them to `tunnel.sh` / `bootstrap.sh`, which write them
   there) and re-run the same script.

Each invoice counts once, whichever of the two events arrives first and however often Stripe
retries it.

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
   public subnet, "Assign a public IPv4 address" on, your SSH key, boot volume **100 GB** (the
   free allowance is 200 GB in all). "Out of host capacity" means try another availability
   domain, or retry later. Under **Show advanced options → Management → Paste cloud-init script**,
   paste the three lines from [Hands-off](#hands-off-paste-this-when-you-create-the-instance)
   below — then the box installs itself and steps 5 and the bootstrap command are done for you.

5. **Let root log in with your key** (skip if you pasted the cloud-init script). Oracle's
   images refuse root SSH, and `deploy.sh`, `recover.sh` and the backup below all need it (the
   platform lives in `/root`):

   ```bash
   ssh ubuntu@<new-ip> 'sudo install -m600 ~ubuntu/.ssh/authorized_keys /root/.ssh/authorized_keys'
   ssh root@<new-ip> true && echo "root login works"
   ```

   This replaces cloud-init's "please log in as ubuntu" entry; Ubuntu's default
   `PermitRootLogin prohibit-password` already allows key-only root logins. After this, use
   `root@<new-ip>` everywhere below, exactly as on any other VPS.

You do **not** need to touch the Ubuntu image's iptables rules. Docker publishes 80/443 through the
`FORWARD` chain, ahead of the image's `REJECT` rule; the security list above is what decides.
But anything that **re-applies** `/etc/iptables/rules.v4` while Docker is running wipes Docker's
chains and takes every app offline from outside: `netfilter-persistent start|restart|reload`,
`systemctl restart netfilter-persistent`, and an `apt upgrade` of `netfilter-persistent` or
`iptables-persistent` (its install script restarts the service). The fix is
`systemctl restart docker`, then check from outside: `curl -sS https://app.cicatrixa.com/healthz`.

### Hands-off: paste this when you create the instance

Any provider with a cloud-init / "user data" box (Oracle, Hetzner, most others) can install the
platform on first boot. Paste exactly this:

```bash
#!/bin/bash
curl -fsSL --retry 10 --retry-all-errors -o /root/first-boot.sh \
  https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/first-boot.sh
BASE_DOMAIN=cicatrixa.com ADMIN_EMAILS=you@example.com bash /root/first-boot.sh
```

**No API keys in it.** The metadata service at 169.254.169.254 hands user data to any process
on the box that asks, and customer containers can reach it. Add keys over SSH afterwards (below).

`first-boot.sh` gives root your SSH key where the image refuses root logins, runs
`bootstrap.sh`, and then — if DNS does not point at the box yet — keeps checking every minute
in the background (unit `cicatrixa-go-live`, for up to 72 h). Once DNS points here it gets the
certificate and the site goes live with nobody logged in. Set the DNS records below as soon
as the console shows the instance's public IP; the order does not matter any more.

```bash
ssh ubuntu@<new-ip> sudo tail -f /var/log/cicatrixa-first-boot.log    # watch it
http://<new-ip>/healthz        # in a browser, before DNS: {"ok":true} means 80 is open and the stack is up
```

It ends with `LIVE: https://app.cicatrixa.com`, or `FAILED:` / `GAVE UP` and the reason.
Then add the keys, as root on the box (`.env` keeps everything else):

```bash
curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
  | BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... bash
```

### Every box — DNS first, then one command

Set DNS **before** running the script, so the certificate can issue on the first try.
Both records already exist — **edit** them, don't add new ones (Cloudflare will not put an A
record next to an existing CNAME of the same name). Cloudflare → DNS:

| record | today (2026-09-30) | change to |
|---|---|---|
| `app` | CNAME → `cname.vercel-dns.com` | type **A**, content `<new-ip>`, **DNS only** (grey cloud) |
| `*` | A → `169.58.36.128` (the dead box) | A → `<new-ip>`, **DNS only** |

Leave `cicatrixa.com` and `www` on Vercel — the platform no longer answers for them or asks for
their certificates. In Vercel → Settings → Domains, **remove `app.cicatrixa.com`** so nothing else
claims it. Then:

```bash
# Hetzner and most VPSes log you in as root:
ssh root@<new-ip>
curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
  | BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... ADMIN_EMAILS=you@example.com bash

# Oracle logs you in as ubuntu. The variables go AFTER sudo — sudo drops any
# set before it, and the script would stop with "set BASE_DOMAIN":
ssh ubuntu@<new-ip>
curl -fsSL https://raw.githubusercontent.com/abyyworld/cicatrixa/main/platform/bootstrap.sh \
  | sudo BASE_DOMAIN=cicatrixa.com OPENAI_API_KEY=sk-... ADMIN_EMAILS=you@example.com bash
```

`BASE_URL` defaults to `https://app.cicatrixa.com`. Re-running is safe: `.env` is kept, and any
setting you pass on the re-run is written into it. It ends by running `verify-public.sh`:

| result | meaning |
|---|---|
| `✓ … is live` (exit 0) | DNS points here **and** Let's Encrypt issued a trusted certificate during this run — which needs it to have reached the box on :80 from the internet. On a later re-run the certificate predates the run and the script says so: it then proves DNS and the stack, not the firewall |
| `… PENDING DNS` (exit 2) | the stack is up; DNS does not point here yet. It prints the records. Re-run the same command once it resolves — `.env` and its keys are kept |
| `✗` (exit 1) | stack down, or DNS is right and no certificate after 3 minutes — it lists what to check |

If no certificate has issued after a minute it restarts Traefik once, because a request that failed
before DNS was ready is not retried until Traefik reloads — but only while no customer app is
running, since a restart drops them all. `deploy.sh` and `recover.sh` never restart it. Last step, from your laptop:
`curl -sS https://app.cicatrixa.com/healthz` — the one thing the server cannot check about itself is
a firewall that allows :80 but blocks :443.

### Back up off the box

The database is the `cx-data` volume. On Oracle especially, keep a copy elsewhere. `sqlite3`'s
online backup is consistent while the app is writing; copying the file is not (WAL mode). The source
is opened read-only, so a missing database fails the backup instead of producing an empty one:

```bash
f=cicatrixa-$(date +%F).db
ssh root@<ip> 'docker exec cx-control python -c "import sqlite3;s=sqlite3.connect(\"file:/data/cicatrixa.db?mode=ro\",uri=True);d=sqlite3.connect(\"/data/backup.db\");s.backup(d);d.close()" && docker exec cx-control cat /data/backup.db' > "$f.tmp" \
  && [ "$(head -c 15 "$f.tmp")" = "SQLite format 3" ] && mv "$f.tmp" "$f" && echo "saved $f" \
  || { echo "backup FAILED" >&2; rm -f "$f.tmp"; }
```

## Leaving Vercel

Until there is a box, `app.cicatrixa.com` is the online workspace on Vercel (`accounts/`,
`workspace/`, Turso database `cicatrixa-prod`): people sign up, connect GitHub through the same
GitHub App the platform uses, and plan projects that wait for a server. One command on the box
brings all of it over — every account (passwords work unchanged, nobody resets anything), the
GitHub App, every GitHub connection and every planned project — and the projects then deploy on
their own. Do it in this order, in one sitting: the online workspace keeps taking changes until
`app` has moved.

Why this order. The box takes **no sign-ups** while `TURSO_DATABASE_URL` is set and no import has
committed: an account made there first would sit on an address whose owner is still online (the
box answers on any host name once `*` points at it — a VPS on its IP address before that — and
with email off it confirms sign-ups without proof). So it has `TURSO_DATABASE_URL` from its very
first start (step 1). Neither `app` nor `*` moves before the import, and the import never merges
an online account into one already on the box unless you name its address (step 4). Nothing
imported deploys before step 7's `--start`, however often cx-control restarts on the way. The
certificate for `app.` is proved before any customer app runs, because only then may
`verify-public.sh` restart Traefik. And people hear that their projects deploy only once they
really have started, on the box the email links to.

**1. Bring the box up with the online workspace's address, DNS untouched** — any way above
(`bootstrap.sh`, the cloud-init stub, or `tunnel.sh`), with Vercel's `TURSO_DATABASE_URL` (the
Vercel project's **Settings → Environment Variables**) passed to it, so that its very first start
already refuses sign-ups. Skip its DNS step: `app` stays on Vercel and `*` where it is (tunnel
mode: create the tunnel and run `tunnel.sh`, but add no routes and change no records yet).

- `bootstrap.sh` or `tunnel.sh`: pass the rest of step 2's values on the same line too; both write
  them into `.env` before the first start (`tunnel.sh`: add `CLOUDFLARE_TUNNEL_TOKEN=eyJ...`):

  ```bash
  BASE_DOMAIN=cicatrixa.com ADMIN_EMAILS=kenny09077@gmail.com,annolieberto@gmail.com \
    TURSO_DATABASE_URL=libsql://cicatrixa-prod-akbarjuraev.<region>.turso.io \
    TURSO_AUTH_TOKEN=eyJ... BETTER_AUTH_SECRET=... OPENAI_API_KEY=sk-... \
    RESEND_API_KEY=re_... MAIL_FROM='Cicatrixa <noreply@cicatrixa.com>' ./bootstrap.sh
  ```

- The cloud-init stub: add only `TURSO_DATABASE_URL=libsql://...` to its last line, beside
  `BASE_DOMAIN`. It is not a secret — nothing reads the database without its token — and the
  token and every key stay out of user data (any container on the box can read it); they go in
  over SSH at step 2.

Started without `TURSO_DATABASE_URL`, the box would take sign-ups until step 3 — a VPS's `:80`
answers on its IP address for any host name — and with email off confirm them on the spot. Step 4
lists any account made that way as "already has an account here" and merges none of them.

The script then ends `PENDING` (exit 2). That is expected here; step 6 finishes it.

Let's Encrypt allows **5 failed authorizations per hostname per hour**, and each start of Traefik
while `app` still points at Vercel spends one on `app.cicatrixa.com` (its challenge lands on
Vercel). So do not restart the stack over and over before step 5. Tunnel mode has no Let's
Encrypt.

**2. Give the box what Vercel has** — the values in the Vercel project's **Settings → Environment
Variables**, in `platform/.env` on the box (`/root/cicatrixa-platform/.env`): whatever step 1 did
not already write there (after the cloud-init stub, all but `TURSO_DATABASE_URL`):

```bash
TURSO_DATABASE_URL=libsql://cicatrixa-prod-akbarjuraev.<region>.turso.io
TURSO_AUTH_TOKEN=eyJ...
BETTER_AUTH_SECRET=...
RESEND_API_KEY=re_...
MAIL_FROM=Cicatrixa <noreply@cicatrixa.com>
OPENAI_API_KEY=sk-...
AI_MODEL=gpt-5.1-codex-mini
ADMIN_EMAILS=kenny09077@gmail.com,annolieberto@gmail.com
```

- `RESEND_API_KEY` and `MAIL_FROM`: the same key and the same verified sending domain — the people
  coming over had email online. `OPENAI_API_KEY` and `AI_MODEL`: Vercel's, if it has them, so the
  box plans with the AI as the workspace did.
- `BETTER_AUTH_SECRET` must be the very value Vercel has: the GitHub App's private key and secrets
  are stored encrypted with it, and the import stops if it cannot read them. If Vercel hides the
  Turso token, make a new one (`turso db tokens create cicatrixa-prod`). Put a value that contains
  `$` in single quotes.
- `ADMIN_EMAILS` must be set here: online it defaults to the owner's two addresses, the box has no
  default. An address in it comes over as admin only if it confirmed its email online **and** its
  password was last set through an emailed reset link (online, `/admin` offers to send that link to
  an admin address that has not proved itself yet). Otherwise the import says so; then
  `docker exec cx-control python -m app.promote you@example.com` — **after** the import, not before
  (an account made here first is not merged, step 4).
- Without `RESEND_API_KEY` the import warns loudly: nobody could reset a forgotten password
  themselves, sign-ups here would be confirmed without proof, and step 8 refuses to run.

**3. Start the stack with them** — this also runs the migration the import needs:

```bash
cd /root/cicatrixa-platform && docker compose up -d
```

Until step 4 commits, the box's sign-up page says sign-ups open shortly and makes no account.
Restarting cx-control here, or at any step before 7, deploys nothing: imported projects wait for
step 7's `--start`.

**4. Import** — first see what it would do (`--dry-run` writes nothing), then do it. It emails
nobody:

```bash
docker exec cx-control python -m app.import_online --dry-run
docker exec cx-control python -m app.import_online
```

| it prints | meaning |
|---|---|
| `⚠ RESEND_API_KEY is empty here` | step 2 is not done: add the key, `docker compose up -d` (it deploys nothing yet), run it again |
| `GitHub App: id … copied` | the App's keys are on the box; nobody connects GitHub again |
| `✗ someone@…: already has an account here (made here …, email …, N projects, GitHub …)` | an account with that address was made on the box, and nothing shows it is the same person. It is **not merged**: that person, their connection and their projects stay out. If the account listed is really theirs (yours, made with `app.promote`, say), run the import again with `--link-existing someone@example.com` (`a@x,b@y` for more) — it merges only the addresses it names, and of those only accounts that confirmed the address on both sides; the account here keeps its own password. Name only accounts you know are theirs: one somebody else made here with their address sits in the same list |
| `new admins: …` | addresses in the box's `ADMIN_EMAILS` that confirmed their email online and set their password through an emailed link. `! … is in ADMIN_EMAILS but its password was never set …`: not admin — `docker exec cx-control python -m app.promote you@example.com` |
| `✗ … installation N is connected to another account here` | the box already binds installation N to another account, and one installation belongs to one account, so the owner cannot connect it from the Connect page either. Find out which account really owns it (online, this person does). If the binding here is stale, remove it — `docker exec cx-control python -c "from app import db; db.q('DELETE FROM github_connections WHERE installation_id=N')"` — and run the import again. Until then their projects are held back |
| `held back (they wait …)`, then `✗ shop (shop) of …: …` | projects that will not deploy at step 7 whatever the limits: their owner has no GitHub connection here, or is an account made here with no trial left. From step 7 on, the first restart of cx-control after that changes deploys them |
| `− someone@…: installation N was disconnected online since; removed here` | on a later run: its owner pressed Disconnect online after an earlier run. The connection the import brought over goes too, and their projects are held back until they connect GitHub here (a connection made on the box is never removed) |
| `capacity: …` with `✗` lines | more waits to deploy than this box's limits allow: the instance's service count or RAM, or an account's quota (the default runs 2 services at once). Set them in `/admin` (instance limits; quotas per user) after step 5 and **before step 7**. What does not fit is not failed: it waits, and a later restart starts it |
| `✗ shop (shop): shop.cicatrixa.com is taken here; not imported` | a project on the box already has that address. The owner makes the project again, under another name |
| `import refused, nothing was written: … different GitHub App` | the box made its own App. One App per instance: installations made on one mean nothing to the other |
| `import refused, nothing was written: … schema version` | cx-control has not started with this code yet: step 3 |

Running it again is safe. It adds what is new; carries over a password reset and an email confirmed
online since (for accounts still on their online password — one chosen here is never overwritten);
removes projects deleted online that never deployed here (one that has deployed is kept and named)
and GitHub connections it brought over that were disconnected online since; and never brings back a
project deleted here.

**5. Move `app` and `*` to the box** — then nothing points at Vercel's workspace any more:

- VPS: Cloudflare → DNS: **edit** `app` (CNAME → Vercel) to type **A**, content `<box-ip>`,
  **DNS only**, and **edit** `*` to **A** `<box-ip>`, **DNS only** (the table in "Every box").
- Tunnel: "Free: Cloudflare Tunnel", steps 2–5: delete both records, add the `app` and `*` routes,
  add the wildcard record, turn on Always Use HTTPS.
- Vercel → the project → **Settings → Domains** → remove `app.cicatrixa.com`. Leave
  `cicatrixa.com` and `www`: their `/signup`, `/login` and `/dashboard` redirect to `app.`, which
  is now the box. The GitHub App's callback and webhook URLs are `app.cicatrixa.com` ones, so
  GitHub follows on its own.

Once `dig +short app.cicatrixa.com` answers with the box (tunnel: Cloudflare's addresses), run the
import once more — it brings over whatever changed online until the move:

```bash
docker exec cx-control python -m app.import_online
```

**6. Verify, while no customer app runs yet:**

```bash
cd /root/cicatrixa-platform && VERIFY_ALLOW_RESTART=1 ./verify-public.sh
```

Exit 0: `app.cicatrixa.com` serves a trusted certificate. `VERIFY_ALLOW_RESTART=1` lets it restart
Traefik once, to retry the certificate request that failed while `app` pointed at Vercel — safe
only now: nothing imported has deployed yet (nothing starts them before step 7's `--start`: not a
restart of cx-control, not a push to their repos), and later a restart would drop every customer
app. If it reports Let's Encrypt refusing
"too many failed authorizations", wait out the hour and run it again. Do not go on until it
passes: the next steps send people here to sign in.

**7. Let the imported projects deploy, and restart cx-control — they start:**

```bash
docker exec cx-control python -m app.import_online --start
docker restart cx-control && docker logs -f cx-control
```

`--start` records that `app` is here and verified, and lists what waits and what is held back, as
the import did. Until it has run, no start of cx-control deploys anything imported (each one logs
`import: N imported service(s) wait for ...`). From the restart on, as it starts it deploys every
imported service that has never been deployed — for owners allowed to deploy (an imported
account's 14-day trial started at the import), with a GitHub connection, within the account's and
the instance's limits. What does not fit stays waiting, with
`import: <slug> waits: <why>` in the log; raise the limit in `/admin` and restart again to start it.

**8. Tell people:**

```bash
docker exec cx-control python -m app.import_online --notify
```

It emails each owner whose imported projects have started deploying (a project counts once step 7
admitted it, though builds run two at a time) and records each email that went out. Run it again
later, after a limit was raised and a restart started more, or if a send failed (Resend allows 2 a
second, and 100 a day on the free plan): it sends only what is still owed. `--dry-run` lists who it
would email. It refuses without `RESEND_API_KEY`.

**9. Check from your laptop** — the one check that sees the firewall the way a visitor does:

```bash
curl -sS https://app.cicatrixa.com/healthz            # {"ok":true}
```

People sign in at `https://app.cicatrixa.com/login` with the email and password they already
have; online sessions do not carry over, so everyone signs in once. Links the online accounts
emailed earlier (confirm, reset) stop working once `app` has moved — "Forgot your password?" sends
a new one. Once it is all live, empty `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` and
`BETTER_AUTH_SECRET` in `.env` and `docker compose up -d`: nothing reads them again.

## Fix 2 — bring the platform back up (`app.cicatrixa.com`)

`platform/recover.sh` automates all of this. From your laptop:

```bash
SERVER=root@<ip> ./platform/recover.sh
```

It first establishes whether the box is reachable at all, and only then repairs the
stack: disk, a stale demo container holding `:80`, the missing `healnet` network, a
placeholder `BASE_DOMAIN`, then runs `verify-public.sh` on the box (a trusted certificate through
Traefik — exit 0 live, 2 DNS pending, 1 broken) and one request to `https://app.<domain>/healthz`
from your laptop, the only check that sees the firewall the way a visitor does.

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
curl -I  http://app.cicatrixa.com/healthz      # expect 302 -> https
curl -sI https://app.cicatrixa.com/healthz     # expect 200
curl -s  https://cicatrixa.com | head -5       # expect the marketing HTML from Vercel
```

## "Nobody can sign up" / "I forgot my password"

Signup, login and password reset all live in the control plane, so start with whether one is
answering at all:

| symptom | cause | fix |
|---|---|---|
| `app.cicatrixa.com/signup` is a 404, or the marketing page | `app` still points at Vercel — no control plane anywhere | bring one up: "Free: Cloudflare Tunnel" or a box above |
| the sign-up page says sign-ups open shortly | `TURSO_DATABASE_URL` is set in `platform/.env` and no import has committed: sign-ups wait for the online accounts ("Leaving Vercel") | finish "Leaving Vercel" step 4; on a box that takes no online workspace, empty `TURSO_DATABASE_URL` and `docker compose up -d` |
| signup works, then "enter your code" and no email ever arrives | `RESEND_API_KEY` is set but Resend refuses to send — usually the sending domain is not verified, which only lets the Resend account's owner receive mail | `/admin` shows Resend's last answer; verify the domain at resend.com/domains, or remove the key (then signups skip the code) |
| you are not admin | an address in `ADMIN_EMAILS` becomes admin only once its emailed code is entered — with email off, nothing proves it is yours | on the server: `docker exec cx-control python -m app.promote you@example.com`, then open the link it prints to set the password |
| "Connect GitHub" comes back with an error | the App asks for authorization when it is installed; that authorization is how Cicatrixa learns the installation is on your own account or an organisation you administer | install from the Connect page and press **Authorize** on GitHub; if it was installed already, use **Already installed? Connect it**. An organisation's installation must be connected by one of its owners |
| someone forgot their password | — | "Forgot your password?" on the login page emails a one-hour, one-use link. With email off or failing: `/admin` → **reset pw** next to the user, and send them the link yourself |

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
