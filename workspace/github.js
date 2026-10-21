// GitHub for the online workspace: a port of platform/control/app/gh.py, so the same
// GitHub App, the same URLs and the same proof of ownership work here today and on
// the platform after the move (DESIGN: "Leaving Vercel").
//
// Every call is made with a credential: the App's JWT, an installation token, or the
// person's one-time OAuth token. Vercel's outbound addresses are shared, and GitHub
// gives anonymous requests 60 an hour per address. The one exception is the manifest
// conversion, which GitHub defines as unauthenticated (the code itself is the secret).
//
// `settings` is the object of decrypted cx_setting values (store.getSettings).
import { createHmac, createSign, timingSafeEqual } from "node:crypto";

const API = "https://api.github.com";
const TIMEOUT_MS = 15_000;
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
// The largest repo file the analysis reads. GitHub's raw media type serves up to 100 MB,
// and an analysis reads up to two dozen key files at once in a function that shares its
// memory with other people's requests; the plan only ever uses the first 8000 characters.
export const MAX_FILE_BYTES = 1024 * 1024;

export class GitHubError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function headers(auth, accept = "application/vnd.github+json") {
  const h = { Accept: accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cicatrixa" };
  if (auth) h.Authorization = `Bearer ${auth}`;
  return h;
}

// The body as text, refused (413) past maxBytes: by its Content-Length before anything
// is read, and by counting when GitHub sends none, so no more than that is ever held.
async function cappedText(res, maxBytes, what) {
  const tooBig = () => new GitHubError(`${what} is over ${Math.round(maxBytes / 1024 / 1024)} MB`,
                                       413);
  if (Number(res.headers.get("content-length")) > maxBytes) {
    await res.body?.cancel().catch(() => {});
    throw tooBig();
  }
  if (!res.body) return "";
  const reader = res.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel().catch(() => {});
      throw tooBig();
    }
    chunks.push(value);
  }
  // As res.text() decodes: UTF-8, a leading byte-order mark dropped.
  return new TextDecoder().decode(Buffer.concat(chunks));
}

// One request to api.github.com. Errors name the endpoint and status, never a token.
// raw: the body as text (at most maxBytes when given) instead of JSON.
async function call(method, path, auth, { accept, body, params, raw = false, maxBytes } = {}) {
  if (!auth) throw new GitHubError("no GitHub credential for this request", 0);
  const url = new URL(API + path);
  for (const [k, v] of Object.entries(params || {})) url.searchParams.set(k, String(v));
  let res;
  try {
    res = await fetch(url, {
      method, headers: headers(auth, accept),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    throw new GitHubError(`GitHub did not answer (${err.name === "TimeoutError"
      ? "timed out" : "network error"})`, 0);
  }
  if (!res.ok) {
    let why = "";
    try { why = (await res.json()).message || ""; } catch { /* not JSON */ }
    throw new GitHubError(`GitHub said ${res.status} for ${method} ${url.pathname}` +
                          (why ? `: ${String(why).slice(0, 200)}` : ""), res.status);
  }
  if (res.status === 204) return null;
  if (raw && maxBytes) return cappedText(res, maxBytes, url.pathname);
  return raw ? res.text() : res.json();
}

function checkRepo(full) {
  if (typeof full !== "string" || !REPO_RE.test(full)) {
    throw new GitHubError(`not a repository name: ${String(full).slice(0, 100)}`, 400);
  }
  return full;
}

const b64url = (buf) => Buffer.from(buf).toString("base64url");

// ---------- the App itself ----------

export function appConfigured(settings) {
  return Boolean(settings?.gh_app_id && settings?.gh_app_pem);
}

// Exactly gh.py build_manifest. Never point base at the apex: it is the Vercel site.
export function buildManifest(baseURL, name = "Cicatrixa") {
  const base = baseURL.replace(/\/+$/, "");
  return {
    name,
    url: base,
    hook_attributes: { url: `${base}/api/webhooks/github`, active: true },
    redirect_url: `${base}/admin/github/callback`,
    callback_urls: [`${base}/connect/github/callback`],
    // GitHub then sends the installer back with an OAuth code: the only proof of
    // whose installation it is (routes.js connectCallback).
    request_oauth_on_install: true,
    public: true,
    default_permissions: { contents: "write", metadata: "read", pull_requests: "write",
                           members: "read" },
    default_events: ["push", "pull_request"],
  };
}

export function manifestFormURL(state, org) {
  const where = org ? `organizations/${encodeURIComponent(org)}/settings/apps/new`
                    : "settings/apps/new";
  return `https://github.com/${where}?state=${encodeURIComponent(state)}`;
}

// GitHub's manifest code → the App's permanent credentials, as setting keys.
export async function exchangeManifestCode(code) {
  if (!code || !/^[A-Za-z0-9_-]{1,200}$/.test(code)) throw new GitHubError("bad manifest code", 400);
  let res;
  try {
    res = await fetch(`${API}/app-manifests/${code}/conversions`, {
      method: "POST", headers: headers(null), signal: AbortSignal.timeout(TIMEOUT_MS) });
  } catch {
    throw new GitHubError("GitHub did not answer the App creation", 0);
  }
  if (!res.ok) {
    throw new GitHubError(`GitHub refused the App code (${res.status}); codes work once, ` +
                          "for an hour", res.status);
  }
  const data = await res.json();
  if (!data.id || !data.pem || !data.slug) throw new GitHubError("GitHub's answer had no App", 502);
  return {
    gh_app_id: String(data.id),
    gh_app_slug: data.slug,
    gh_app_name: data.name || "Cicatrixa",
    gh_app_pem: data.pem,
    gh_app_webhook_secret: data.webhook_secret || "",
    gh_app_client_id: data.client_id || "",
    gh_app_client_secret: data.client_secret || "",
  };
}

// RS256, backdated 60 s for clock drift, 9 minutes of life (GitHub's limit is 10).
export function appJWT(settings, now = Date.now()) {
  if (!appConfigured(settings)) throw new GitHubError("GitHub App not configured", 503);
  const t = Math.floor(now / 1000);
  const head = b64url(JSON.stringify({ alg: "RS256", typ: "JWT" }));
  const body = b64url(JSON.stringify({ iat: t - 60, exp: t + 540, iss: settings.gh_app_id }));
  const signature = createSign("RSA-SHA256").update(`${head}.${body}`).sign(settings.gh_app_pem);
  return `${head}.${body}.${b64url(signature)}`;
}

// Minted per request: a serverless function keeps no cache worth trusting.
export async function installationToken(settings, installationId) {
  const data = await call("POST", `/app/installations/${Number(installationId)}/access_tokens`,
                          appJWT(settings));
  if (!data?.token) throw new GitHubError("GitHub gave no installation token", 502);
  return data.token;
}

export async function installationAccount(settings, installationId) {
  const data = await call("GET", `/app/installations/${Number(installationId)}`, appJWT(settings));
  return { login: data?.account?.login || "", type: data?.account?.type || "" };
}

// Best-effort, like gh.py: the binding is removed here whatever GitHub answers.
export async function revokeInstallation(settings, installationId) {
  try {
    await call("DELETE", `/app/installations/${Number(installationId)}`, appJWT(settings));
    return true;
  } catch {
    return false;
  }
}

// ---------- repositories (installation token) ----------

export async function listRepos(settings, installationId) {
  const token = await installationToken(settings, installationId);
  const repos = [];
  for (let page = 1; page <= 50; page++) {
    const data = await call("GET", "/installation/repositories", token,
                            { params: { per_page: 100, page } });
    const batch = data?.repositories || [];
    repos.push(...batch);
    if (!batch.length || repos.length >= (data.total_count || 0)) break;
  }
  return repos.map((r) => ({ full_name: r.full_name, private: Boolean(r.private),
                             default_branch: r.default_branch || "main",
                             pushed_at: r.pushed_at || "" }));
}

export async function repoInfo(token, full) {
  try {
    const r = await call("GET", `/repos/${checkRepo(full)}`, token);
    return { full_name: r.full_name, default_branch: r.default_branch || "main",
             private: Boolean(r.private) };
  } catch (err) {
    if (err.status === 404 || err.status === 403) {
      throw new GitHubError(`${full}: not found or no access`, 404);
    }
    throw err;
  }
}

// The commit a branch points at. The sha media type makes GitHub answer the bare SHA and
// compute no diff: the JSON commit carries its stats, and a head commit too big to diff
// fails with a 422 that would read as "branch not found". With it, 404 and 422 ("No
// commit found") both mean the branch does not exist; 409 is a repository with no commits.
export async function branchHead(token, full, branch) {
  let text;
  try {
    text = await call("GET", `/repos/${checkRepo(full)}/commits/${encodeURIComponent(branch)}`,
                      token, { accept: "application/vnd.github.sha", raw: true, maxBytes: 1024 });
  } catch (err) {
    if (err.status === 404 || err.status === 422) {
      throw new GitHubError(`${full}: branch ${branch} not found`, 404);
    }
    if (err.status === 409) {
      throw new GitHubError(`${full} is empty: push a first commit, then run the analysis again`,
                            409);
    }
    throw err;
  }
  const sha = String(text || "").trim();
  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(sha)) {
    throw new GitHubError(`${full}@${branch}: GitHub gave no commit`, 502);
  }
  return sha;
}

// Every blob's path, and its size in bytes where GitHub gives one, so the analysis can
// pass over files too big to read (MAX_FILE_BYTES) without asking for them.
export async function getTree(token, full, sha) {
  const r = await call("GET", `/repos/${checkRepo(full)}/git/trees/${encodeURIComponent(sha)}`,
                       token, { params: { recursive: 1 } });
  const blobs = (r?.tree || []).filter((e) => e.type === "blob");
  const sizes = new Map();
  for (const e of blobs) if (Number.isFinite(e.size)) sizes.set(e.path, e.size);
  return { paths: blobs.map((e) => e.path), sizes, truncated: Boolean(r?.truncated) };
}

// A file's text, or null when it does not exist. The path may come from the model
// (need_files), so it is confined to the repo: no "..", ".", empty or absolute parts
// (fetch would resolve "../.." into another API endpoint). A file over MAX_FILE_BYTES
// is refused with a GitHubError of status 413, and never held whole.
export async function readFile(token, full, path, ref) {
  const parts = String(path).replace(/^\.\/+/, "").split("/");
  if (!parts.length || parts.some((p) => !p || p === "." || p === ".." || p.includes("\\"))) {
    return null;
  }
  try {
    return await call("GET", `/repos/${checkRepo(full)}/contents/${parts.map(encodeURIComponent)
      .join("/")}`, token, { accept: "application/vnd.github.raw+json", raw: true,
                             maxBytes: MAX_FILE_BYTES, params: ref ? { ref } : {} });
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

// ---------- proving whose installation it is ----------

// For an installation that already exists: GitHub's OAuth page, which comes back to
// the callback with a code and no installation_id.
export function authorizeURL(settings, state, baseURL) {
  if (!settings?.gh_app_client_id) return null;
  const q = new URLSearchParams({ client_id: settings.gh_app_client_id, state,
                                  redirect_uri: `${baseURL.replace(/\/+$/, "")}/connect/github/callback` });
  return `https://github.com/login/oauth/authorize?${q}`;
}

// Port of gh.installations_user_controls: trade the OAuth code for the person's own
// token (used here once, never stored) and return this App's installations on an
// account they control — their own, or an organisation they administer.
// → {me, controlled: Map<installationId, installation>} or null when the code is no good.
//
// Seeing an installation is not enough: /user/installations lists every one the person
// can read anything through (a single repo as a collaborator, plain org membership),
// and an installation token reaches ALL of its repos.
export async function installationsUserControls(settings, code) {
  const clientId = settings?.gh_app_client_id;
  const clientSecret = settings?.gh_app_client_secret;
  if (!(code && clientId && clientSecret)) return null;
  try {
    const res = await fetch("https://github.com/login/oauth/access_token", {
      method: "POST",
      headers: { Accept: "application/json", "User-Agent": "cicatrixa",
                 "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret, code }).toString(),
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const token = res.status === 200 ? (await res.json()).access_token : null;
    if (!token) return null;
    const me = await call("GET", "/user", token);
    const controlled = new Map();
    for (let page = 1; page <= 50; page++) {
      const found = (await call("GET", "/user/installations", token,
                                { params: { per_page: 100, page } }))?.installations || [];
      for (const inst of found) {
        const account = inst.account || {};
        if (account.type === "User") {
          if (account.id === me.id) controlled.set(inst.id, inst);
        } else if (account.type === "Organization" && account.login) {
          let m;
          try {
            m = await call("GET", `/user/memberships/orgs/${encodeURIComponent(account.login)}`, token);
          } catch {
            continue;           // no answer is no proof
          }
          if (m?.role === "admin" && m?.state === "active") controlled.set(inst.id, inst);
        }
      }
      if (found.length < 100) break;
    }
    return { me, controlled };
  } catch {
    return null;
  }
}

// ---------- webhooks ----------

// Unlike gh.py, no secret means no: an unsigned POST is never trusted.
export function verifyWebhook(secret, rawBody, signatureHeader) {
  if (!secret || typeof signatureHeader !== "string" || !signatureHeader.startsWith("sha256=")) {
    return false;
  }
  const body = typeof rawBody === "string" ? Buffer.from(rawBody, "utf8") : Buffer.from(rawBody);
  const want = Buffer.from(createHmac("sha256", secret).update(body).digest("hex"), "utf8");
  const given = Buffer.from(signatureHeader.slice(7), "utf8");
  return given.length === want.length && timingSafeEqual(given, want);
}
