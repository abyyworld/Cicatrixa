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

import { mailOn, sendResetPasswordEmail, sendVerificationEmail } from "./email.js";

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

export function authOptions(env = process.env, client = databaseClient(env)) {
  const base = baseURL(env);
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
      sendResetPassword: ({ user, url }) => sendResetPasswordEmail(env, user.email, url),
    },
    emailVerification: {
      sendOnSignUp: verify,
      // An unverified person who tries to log in gets a fresh link.
      sendOnSignIn: verify,
      autoSignInAfterVerification: true,
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
  if (!cached) cached = betterAuth(authOptions(env));
  return cached;
}
