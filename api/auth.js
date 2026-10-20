// Vercel function for every /api/auth/* request (vercel.json rewrites them here).
// Better Auth answers them: sign-up, sign-in, sign-out, get-session, verify-email,
// request-password-reset, reset-password, send-verification-email.
import { databaseClient, getAuth } from "../accounts/auth.js";
import { ensureSchema } from "../accounts/schema.js";

let ready;

// vercel.json sends /api/auth/<path> here as /api/auth?__auth_path=<path>. Put the path
// back where Better Auth looks for it. If Vercel already handed over the original URL,
// only the helper parameter is dropped.
export function originalRequest(request, body) {
  const url = new URL(request.url);
  const path = url.searchParams.get("__auth_path");
  url.searchParams.delete("__auth_path");
  if (path !== null && !url.pathname.startsWith("/api/auth/")) {
    url.pathname = `/api/auth/${path.replace(/^\/+/, "")}`;
  }
  return new Request(url, { method: request.method, headers: request.headers, body,
                            redirect: "manual" });
}

async function handle(request) {
  ready ??= ensureSchema(databaseClient()).catch((err) => { ready = undefined; throw err; });
  await ready;
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer();
  return getAuth().handler(originalRequest(request, body));
}

export const GET = handle;
export const POST = handle;
