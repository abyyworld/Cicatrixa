# Accounts: sign-up, email confirmation, login, password reset

These run on **Vercel** (the same project that serves the marketing site), keep users in the
**Turso** database `cicatrixa-prod`, and send email through **Resend**. All three are free, and
none of them needs a server of ours.

| Piece | File |
|---|---|
| Better Auth config (passwords, sessions, rate limits, email hooks) | `accounts/auth.js` |
| The emails (confirm address, reset password) | `accounts/email.js` |
| Database tables, created on the first request | `accounts/schema.js` |
| The Vercel function that answers `/api/auth/*` | `api/auth.js` |
| Pages: `/signup` `/login` `/forgot-password` `/reset-password` `/dashboard` | `site/*.html`, `site/accounts.js`, `site/app.css` |
| Routing (`app.cicatrixa.com/` → `/dashboard`, `/api/auth/*` → the function) | `vercel.json` |
| Tests | `accounts/test/flow.test.js` (`npm test`) |

What a person sees: sign up → "check your inbox" → the email's link confirms the address and
signs them in → dashboard. Logging in before confirming sends a fresh link. "Forgot your
password?" emails a link that works once for an hour; using it ends every other session.
Passwords are hashed by Better Auth (scrypt); nobody can read them back.

Deploying projects still needs the platform (`platform/`) on a machine with Docker. Until then
the dashboard says it opens soon.

## Turning it on (one time)

1. **Database.** `cicatrixa-prod` exists in the Turso org `akbarjuraev` (listed in
   free-database-servers' `INVENTORY.md`). Its URL and token are the first two values below.
   To make a new token yourself: `turso db tokens create cicatrixa-prod`.
2. **Email.** Sign up at [resend.com](https://resend.com/signup) → **Domains → Add domain →
   `cicatrixa.com`** → add the DNS records it shows in Cloudflare (DNS only, grey cloud), or
   use its "Auto configure" button for Cloudflare → wait for *Verified* → **API Keys → Create**.
   Until the domain is verified, Resend only delivers to your own address.
3. **Vercel → the project → Settings → Environment Variables** (Production and Preview):

   | Name | Value |
   |---|---|
   | `TURSO_DATABASE_URL` | `libsql://cicatrixa-prod-akbarjuraev….turso.io` |
   | `TURSO_AUTH_TOKEN` | the database token |
   | `BETTER_AUTH_SECRET` | 32+ random characters: `openssl rand -base64 32` |
   | `BETTER_AUTH_URL` | `https://app.cicatrixa.com` |
   | `RESEND_API_KEY` | `re_…` from step 2 |
   | `MAIL_FROM` | optional; default `Cicatrixa <noreply@cicatrixa.com>` |

4. **Vercel → Settings → Domains:** make sure `app.cicatrixa.com` is listed. In Cloudflare DNS,
   `app` is a CNAME to `cname.vercel-dns.com` (DNS only).
5. Merge this branch into `main`. Vercel deploys it. Then open `https://app.cicatrixa.com`,
   sign up with your own email, and click the link.

Without `RESEND_API_KEY`, accounts still work, but unconfirmed: nobody could ever receive a
link, so nobody could log in otherwise. Password reset then has no way to reach anyone.

## When something is wrong

Vercel → the project → **Logs**, filter by `/api/auth`:

| Log line | Meaning |
|---|---|
| `Resend refused … domain is not verified` | finish step 2's DNS records |
| `RESEND_API_KEY is not set` | step 3 |
| `TURSO_DATABASE_URL is not set`, or a libsql auth error | step 3's first two values |
| Better Auth: `BETTER_AUTH_SECRET` missing | step 3 |

If Turso archived the database after a long idle stretch (free plan),
`turso group unarchive default`.

## Limits that matter

- Resend free: 3,000 emails a month, 100 a day.
- Turso free: 5 GB, 500M row reads and 10M row writes a month.
- Vercel Hobby is for **non-commercial** use. Once Cicatrixa takes payments, the project needs
  Vercel Pro, or these files move to wherever the platform runs.
- Rate limits (per IP, per minute, stored in the database): 5 sign-ins, 5 sign-ups,
  3 reset requests, 3 resends, 5 password resets.

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

## Moving these users into the platform later

When `platform/` runs on a real host, its control plane should take over `app.cicatrixa.com`.
These users carry over. Better Auth's `user` table has the email and `emailVerified`; the
`account` row with `providerId = 'credential'` holds the password hash as
`<salt hex>:<key hex>` (scrypt, N=16384, r=16, p=1, 64-byte key, salt used as text), which Python
can check with `hashlib.scrypt`. Import them, or point the platform's login at this database.
