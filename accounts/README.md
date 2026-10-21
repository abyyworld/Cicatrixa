# The online workspace: accounts, GitHub, planned projects

Until the platform (`platform/`) runs on a machine with Docker, `app.cicatrixa.com` is this: a
**Vercel** function pair beside the marketing site, users and projects in the **Turso** database
`cicatrixa-prod`, emails through **Resend**. All three are free, and none needs a server of ours.

What a person can do today: sign up → confirm their email → connect GitHub (the platform's own
GitHub App) → pick repos → Cicatrixa reads each repo through the GitHub API, works out how to run
it and writes the Dockerfile, live in a log → the project is **planned** and keeps its
`<name>.cicatrixa.com` address. Nothing is built or run: there is no Docker host. When there is
one, a single command moves everyone over and the planned projects deploy on their own
(docs/RUNBOOK.md, "Leaving Vercel"). `/demo` shows the recorded self-heal run.

| Piece | File |
|---|---|
| Better Auth config (passwords, sessions, rate limits, email hooks) | `accounts/auth.js` |
| The emails (confirm address, reset password) | `accounts/email.js` |
| Account tables, created on the first request | `accounts/schema.js` |
| `/api/auth/*` (sign-up, login, reset…) | `api/auth.js` |
| Workspace API: GitHub connect, repos, projects, analysis, admin, webhook | `workspace/routes.js`, served by `api/workspace.js` |
| GitHub App calls (port of `platform/control/app/gh.py`) | `workspace/github.js` |
| Run plan: file tree, heuristics, AI prompt (port of `ai.py` + `engine.py`'s analysis) | `workspace/plan.js` |
| Workspace tables (`cx_*`) and queries | `workspace/store.js` |
| Encrypted settings, signed state tokens | `workspace/secrets.js` |
| Pages: `/signup` `/login` `/forgot-password` `/reset-password` | `site/*.html`, `site/accounts.js` |
| Pages: `/dashboard` `/connect/github` `/projects/new` `/project?id=` `/admin` | `site/…`, `site/workspace.js` |
| Public page: `/demo` (the recorded self-heal run) | `site/demo.html`, `site/media/` |
| Routing: rewrites to the two functions, `app.` root → `/dashboard`, app pages on `www`/apex → `app.` | `vercel.json` |
| Tests | `accounts/test/`, `workspace/test/` (`npm test`) |

## Turning it on (one time)

1. **Database.** `cicatrixa-prod` exists in the Turso org `akbarjuraev` (listed in
   free-database-servers' `INVENTORY.md`). Tables create themselves on the first request.
2. **Email.** Resend → **Domains → `cicatrixa.com`** (DNS records in Cloudflare, DNS only) →
   *Verified* → **API Keys → Create**. Until the domain is verified, Resend only delivers to your
   own address.
3. **Vercel → the project → Settings → Environment Variables** (Production):

   | Name | Value |
   |---|---|
   | `TURSO_DATABASE_URL` | `libsql://cicatrixa-prod-akbarjuraev….turso.io` |
   | `TURSO_AUTH_TOKEN` | the database token |
   | `BETTER_AUTH_SECRET` | 32+ random characters. **Never change it**: it also encrypts the GitHub App's keys in the database |
   | `BETTER_AUTH_URL` | `https://app.cicatrixa.com` |
   | `RESEND_API_KEY` | `re_…` from step 2 |
   | `MAIL_FROM` | optional; default `Cicatrixa <noreply@cicatrixa.com>` |
   | `OPENAI_API_KEY` | optional: the AI writes the plans (else built-in rules for Node, Python, Go, static) |
   | `AI_MODEL` | optional; default `gpt-5.1-codex-mini`, as on the platform |
   | `ADMIN_EMAILS` | optional; comma-separated. Default: the owner's two addresses in `CLAUDE.md` |

4. **Vercel → Settings → Domains:** `app.cicatrixa.com` is listed; in Cloudflare DNS, `app` is a
   CNAME to Vercel (DNS only).
5. **Become admin** — once: log in with an `ADMIN_EMAILS` address → **Admin** → **Confirm it's
   you** → open the emailed link and set your password. A confirmed address alone is not enough:
   anyone can sign up with your address first and get you to click its confirmation link, so admin
   also needs a password set through an emailed reset link (`cx_setting` row `pw_proof:<userId>`,
   written by `onPasswordReset`).
6. **The GitHub App** — once: **Admin** → **Create the GitHub App** → GitHub asks you to confirm →
   done. Its keys are stored in Turso (`cx_setting`), the secret ones encrypted with
   `BETTER_AUTH_SECRET`. This is the very App the platform uses later, at the same URLs, so nobody
   reconnects. If the name "Cicatrixa" is taken on GitHub, type another in the form. Until this
   step, the Connect page says GitHub connections open shortly.

Without `RESEND_API_KEY`, accounts still work, but unconfirmed, and nobody is admin.

## When something is wrong

Vercel → the project → **Logs**, filter by `/api/auth` or `/api/workspace`:

| What you see | Meaning |
|---|---|
| `Resend refused … domain is not verified` | finish step 2's DNS records |
| `RESEND_API_KEY is not set` | step 3 |
| `TURSO_DATABASE_URL is not set`, or a libsql auth error | step 3's first two values |
| Better Auth: `BETTER_AUTH_SECRET` missing | step 3 |
| Connect page: "GitHub connections open shortly" | step 5 has not been done |
| No **Admin** link for you | your address is not in `ADMIN_EMAILS` (or the default), or not confirmed |
| A plan says `heuristic: …` | no `OPENAI_API_KEY`, or the AI did not answer in time — the log says which |

If Turso archived the database after a long idle stretch (free plan),
`turso group unarchive default`.

## Limits that matter

- Per user: 5 services, 20 analyses a day. Rate limits on sign-in/sign-up/reset are per IP, per
  minute, stored in the database (5 sign-ins, 5 sign-ups, 3 reset requests, 3 resends, 5 resets).
- An analysis must finish inside one Vercel function call (60 s); the AI gets about 50 s of it,
  then the built-in rules take over.
- Resend free: 3,000 emails a month, 100 a day. Turso free: 5 GB, 500M row reads, 10M writes a month.
- Vercel Hobby is for **non-commercial** use. Once Cicatrixa takes payments, the project needs
  Vercel Pro, or app.cicatrixa.com moves to the platform (the RUNBOOK section below).

## Upgrading Better Auth

`accounts/schema.js` is the SQL Better Auth 1.7.7 wants for these options. After changing either,
print what it wants now against an empty database and add any new table or column as its own
`create … if not exists` / `alter table … add column` line:

```js
import { getMigrations } from "better-auth/db/migration";
import { createClient } from "@libsql/client";
import { authOptions } from "./accounts/auth.js";
const client = createClient({ url: "file:/tmp/empty.db" });
const env = { TURSO_DATABASE_URL: "file:/tmp/empty.db", BETTER_AUTH_SECRET: "x".repeat(40), RESEND_API_KEY: "x" };
console.log(await (await getMigrations(authOptions(env, client))).compileMigrations());
```

## Moving to a real server

docs/RUNBOOK.md, **"Leaving Vercel"**, step by step. In short: the box's `platform/.env` gets
the Turso URL and token and `BETTER_AUTH_SECRET` (to read this database) plus `RESEND_API_KEY`,
`MAIL_FROM`, `OPENAI_API_KEY`, `AI_MODEL` and `ADMIN_EMAILS` (to keep what people had here);
`docker exec cx-control python -m app.import_online` brings every account (Better Auth password
hashes are accepted as they are), the GitHub App, every connection and every planned project;
`app` and `*` move to the box; restarting cx-control deploys the projects within the box's
quotas; `import_online --notify` emails their owners. Until the import has run, the box refuses
sign-ups, and it never merges an online account into an existing box account unless told to
(`--link-existing`).
