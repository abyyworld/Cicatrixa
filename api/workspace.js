// Vercel function for the online workspace (workspace/routes.js): GitHub connect at the
// platform's own URLs, projects, repo analysis, admin, and GitHub's webhook. vercel.json
// rewrites /api/workspace/<path>, /connect/github/*, /admin/github/callback and
// /api/webhooks/github here. One function, because Vercel's Hobby plan allows few.
import { databaseClient, getAuth } from "../accounts/auth.js";
import { ensureSchema } from "../accounts/schema.js";
import { handleWorkspace } from "../workspace/routes.js";
import { ensureWorkspaceSchema } from "../workspace/store.js";

let client;
let ready;

// Where each rewrite came from, by the __path vercel.json gives it.
function originalPath(path) {
  const p = path.replace(/^\/+/, "");
  if (p.startsWith("connect/") || p === "admin/github/callback") return `/${p}`;
  if (p === "webhooks/github") return "/api/webhooks/github";
  return `/api/workspace/${p}`;
}

// Put the original URL back, as api/auth.js does. If Vercel already handed over the
// original URL, only the helper parameter is dropped.
export function originalRequest(request, body) {
  const url = new URL(request.url);
  const path = url.searchParams.get("__path");
  url.searchParams.delete("__path");
  if (path !== null && (url.pathname === "/api/workspace" || url.pathname === "/api/workspace/")) {
    url.pathname = originalPath(path);
  }
  return new Request(url, { method: request.method, headers: request.headers, body,
                            redirect: "manual" });
}

async function handle(request) {
  client ??= databaseClient();
  // Accounts' tables first: the workspace's reference "user".
  ready ??= ensureSchema(client).then(() => ensureWorkspaceSchema(client))
    .catch((err) => { ready = undefined; throw err; });
  await ready;
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer();
  return handleWorkspace(originalRequest(request, body), { env: process.env, client,
                                                           auth: getAuth() });
}

export const GET = handle;
export const POST = handle;
