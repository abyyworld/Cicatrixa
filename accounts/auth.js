// Accounts for app.cicatrixa.com: sign-up, email verification, login, password reset.
//
// Better Auth (open-source library) keeps users, sessions and password hashes in our
// Turso database; Resend sends the emails. It runs as one Vercel function
// (api/auth.js) beside the marketing site, so it needs no server of its own.
// The platform (platform/) still needs a Docker host to deploy anything; when it has
// one, it can take these users over — accounts/README.md says how.
import { createClient } from "@libsql/client";
import { LibsqlDialect } from "@libsql/kysely-libsql";
import { betterAuth } from "better-auth";

import { recordPasswordProof } from "../workspace/store.js";
import { mailOn, sendFinishSignUpEmail, sendResetPasswordEmail,
         sendVerificationEmail } from "./email.js";

const SITE_ORIGINS = [
  "https://cicatrixa.com",
  "https://www.cicatrixa.com",
  "https://app.cicatrixa.com",
];

// Where links in emails point, and what the session cookie is issued for.
export function baseURL(env = process.env) {
  if (env.BETTER_AUTH_URL) return env.BETTER_AUTH_URL.replace(/\/+$/, "");
  if (env.VERCEL_URL) return `https://${env.VERCEL_URL}`;   // a preview deployment
  return "http://localhost:3000";
}

export function databaseClient(env = process.env) {
  if (!env.TURSO_DATABASE_URL) {
    throw new Error("TURSO_DATABASE_URL is not set (accounts/README.md: Vercel environment variables)");
  }
  return createClient({ url: env.TURSO_DATABASE_URL, authToken: env.TURSO_AUTH_TOKEN });
}

// Asks Better Auth's own handler to email `email` a password link, as the visitor who sent
// `request`: its per-visitor limit on /request-password-reset (3 a minute) then counts these
// too, which auth.api would skip. Its answer is generic whether or not anything was sent.
async function requestPasswordLink(auth, base, email, request) {
  const headers = new Headers({ "content-type": "application/json" });
  for (const name of ["origin", "x-forwarded-for", "x-real-ip", "user-agent"]) {
    const value = request?.headers.get(name);
    if (value) headers.set(name, value);
  }
  const res = await auth.handler(new Request(`${base}/api/auth/request-password-reset`, {
    method: "POST", headers,
    body: JSON.stringify({ email, redirectTo: `${base}/reset-password` }),
  }));
  if (!res.ok) console.error(`accounts: no password link for a repeated sign-up (${res.status})`);
}

// `self` returns the Better Auth instance built from these options (getAuth): two hooks
// below act through it. Options built only to read the schema never call them.
export function authOptions(env = process.env, client = databaseClient(env), self = () => null) {
  const base = baseURL(env);
  const instance = () => {
    const auth = self();
    if (!auth) throw new Error("accounts: these options have no Better Auth instance (getAuth)");
    return auth;
  };
  // Without a mail key nobody could ever receive a verification link, so nobody
  // could log in. Then accounts work unverified, as the platform does with mail off.
  const verify = mailOn(env);
  const trusted = [...new Set([base, ...SITE_ORIGINS,
                               ...(env.VERCEL_URL ? [`https://${env.VERCEL_URL}`] : [])])];
  return {
    appName: "Cicatrixa",
    baseURL: base,
    basePath: "/api/auth",
    secret: env.BETTER_AUTH_SECRET,
    trustedOrigins: trusted,
    database: { dialect: new LibsqlDialect({ client }), type: "sqlite" },
    emailAndPassword: {
      enabled: true,
      requireEmailVerification: verify,
      minPasswordLength: 8,
      maxPasswordLength: 128,
      resetPasswordTokenExpiresIn: 60 * 60,
      // A reset is how someone takes an account back: end every other session.
      revokeSessionsOnPasswordReset: true,
      // An unconfirmed account's link is how its address is claimed (onExistingUserSignUp
      // below, or forgot-password before confirming), so it reads as finishing the sign-up.
      sendResetPassword: ({ user, url }) => (user.emailVerified
        ? sendResetPasswordEmail(env, user.email, url)
        : sendFinishSignUpEmail(env, user.email, url)),
      // A sign-up for an address that already has an account (Better Auth answers it as
      // if it were new, so nobody learns which addresses have one). If that account was
      // never confirmed, whoever made it may not read the inbox: anyone can sign up first
      // with someone else's address and their own password and wait for its owner. So the
      // inbox gets a link to set the password; setting it replaces the first one, confirms
      // the address and ends every session (onPasswordReset, revokeSessionsOnPasswordReset).
      // A confirmed account gets nothing new: its owner logs in, or resets the password.
      onExistingUserSignUp: async ({ user }, request) => {
        if (user.emailVerified) return;
        await requestPasswordLink(instance(), base, user.email, request);
      },
      // The password was just chosen by someone holding an emailed link: the proof an
      // ADMIN_EMAILS address needs before it is an admin (workspace/routes.js
      // adminStatus). A confirmation link is not enough, because whoever signed up chose
      // that password. Better Auth calls this after storing the new password and before
      // ending the old sessions, so it must not throw: a failed write only means the
      // person claims again.
      onPasswordReset: async ({ user }) => {
        try {
          await recordPasswordProof(client, user.id);
        } catch (err) {
          console.error("accounts: could not record a password reset:", err?.message || err);
        }
        // The link proves the inbox as well as a confirmation link does, so the person
        // who finished signing up through it logs in straight away. If this write fails,
        // logging in mails a confirmation link, as for any unconfirmed account.
        if (user.emailVerified) return;
        try {
          const { internalAdapter } = await instance().$context;
          await internalAdapter.updateUser(user.id, { emailVerified: true });
        } catch (err) {
          console.error("accounts: could not confirm the address after a reset:",
                        err?.message || err);
        }
      },
    },
    emailVerification: {
      sendOnSignUp: verify,
      // An unverified person who tries to log in gets a fresh link.
      sendOnSignIn: verify,
      // A confirmation link proves who reads the inbox, not who chose the password, so it
      // opens no session: it lands on /login?verified=1 (the pages' callbackURL), and the
      // person logs in with their own password. Signing in whoever clicked would sign the
      // inbox's owner in to an account someone else holds the password to.
      autoSignInAfterVerification: false,
      expiresIn: 60 * 60,
      sendVerificationEmail: ({ user, url }) => sendVerificationEmail(env, user.email, url),
    },
    session: {
      expiresIn: 60 * 60 * 24 * 30,
      updateAge: 60 * 60 * 24,
    },
    // Stored in the database: a serverless function forgets anything kept in memory
    // between requests, so in-memory limits would limit nothing.
    rateLimit: {
      enabled: true,
      storage: "database",
      window: 60,
      max: 100,
      customRules: {
        "/sign-in/email": { window: 60, max: 5 },
        "/sign-up/email": { window: 60, max: 5 },
        "/request-password-reset": { window: 60, max: 3 },
        "/send-verification-email": { window: 60, max: 3 },
        "/reset-password": { window: 60, max: 5 },
      },
    },
    advanced: {
      useSecureCookies: base.startsWith("https://"),
      ipAddress: { ipAddressHeaders: ["x-forwarded-for", "x-real-ip"] },
    },
    telemetry: { enabled: false },
  };
}

let cached;

// One instance per warm function.
export function getAuth(env = process.env) {
  if (!cached) {
    const auth = betterAuth(authOptions(env, databaseClient(env), () => auth));
    cached = auth;
  }
  return cached;
}
