// The online workspace's HTTP API on app.cicatrixa.com: connect GitHub (the platform's
// own GitHub App flow, at the platform's own URLs), turn repos into projects with
// reserved <slug>.cicatrixa.com addresses, and analyse each repo through the GitHub API
// into a run plan. Nothing is built or run here — there is no Docker host yet — so a
// project's best state is "planned", and the platform deploys it after the import.
//
// One Vercel function (api/workspace.js) hands every request here with its original
// URL. Routes, by the path after /api/workspace/ (DESIGN "HTTP API"):
//   JSON  GET me, repos, projects, projects/:id, admin
//         POST projects, projects/:id/services/:sid/analyze, projects/:id/delete,
//              github/disconnect, admin/claim, admin/github-app
//   pages GET connect/github/{start,authorize,callback,setup}, admin/github/callback
//   hook  POST webhooks/github
import { randomUUID } from "node:crypto";

import { baseURL } from "../accounts/auth.js";
import { mailOn } from "../accounts/email.js";
import * as github from "./github.js";
import { analyze, envKey, makeSlug, serviceName, serviceURL } from "./plan.js";
import { readState, signState } from "./secrets.js";
import * as store from "./store.js";

export const LIMITS = { services: 5, analysesPerDay: 20 };
export const BASE_DOMAIN = "cicatrixa.com";
// An analysis runs inside a 60 s function; one that started this long ago is dead.
export const STALE_MS = 90_000;
const DEFAULT_ADMINS = "kenny09077@gmail.com,annolieberto@gmail.com";
const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

// ---------- small helpers ----------

const NO_STORE = { "cache-control": "no-store" };

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json; charset=utf-8", ...NO_STORE } });
}

const fail = (status, error) => json({ error }, status);

function redirect(location) {
  return new Response(null, { status: 303, headers: { location, ...NO_STORE } });
}

// What a page may be sent back with in ?error=. Only these codes, never a sentence: the
// pages turn a code into their own words, so a link from anyone cannot put its text
// under Cicatrixa's banner (site/workspace.js has the sentences, and the platform's
// main.py sends the same codes).
export const ERROR_CODES = Object.freeze(["app_missing", "state", "no_code", "not_yours",
  "not_installed", "taken", "org_request", "github", "admin_only", "app_exists", "app_state",
  "app_failed", "signin"]);

function withError(page, code) {
  if (!ERROR_CODES.includes(code)) throw new Error(`unknown error code ${code}`);
  return redirect(`${page}?${new URLSearchParams({ error: code })}`);
}

export function adminEmails(env = process.env) {
  const raw = (env.ADMIN_EMAILS || "").trim() || DEFAULT_ADMINS;
  return raw.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
}

// An address named in ADMIN_EMAILS, confirmed by its emailed link. Not yet an admin: the
// confirmation proves someone who reads the inbox clicked, not that they chose the
// password — whoever signed up first did, and anyone can sign up with any address.
export function adminAddress(user, env = process.env) {
  return Boolean(user?.emailVerified && user.email &&
                 adminEmails(env).includes(user.email.toLowerCase()));
}

// isAdmin: an admin address whose password was set through an emailed reset link
// (store.passwordProof, recorded by accounts/auth.js), so the password holder reads the
// inbox. adminPending: an admin address without that proof yet; POST admin/claim mails
// it a reset link. import_online applies the same rule to the platform's is_admin.
async function adminStatus(c) {
  c.adminStatus ??= (async () => {
    if (!adminAddress(c.user, c.env)) return { isAdmin: false, adminPending: false };
    const proved = Boolean(await store.passwordProof(c.client, c.user.id));
    return { isAdmin: proved, adminPending: !proved };
  })();
  return c.adminStatus;
}

const isAdmin = async (c) => (await adminStatus(c)).isAdmin;

// The route: what follows /api/workspace/, or the page path itself.
export function routeKey(pathname) {
  const p = pathname.replace(/\/+$/, "");
  if (p.startsWith("/api/workspace/")) return p.slice("/api/workspace/".length);
  if (p === "/api/webhooks/github") return "webhooks/github";
  return p.replace(/^\/+/, "");
}

const clock = (ms) => new Date(ms).toISOString().slice(11, 19);    // HH:MM:SS, UTC

function parsePlan(text) {
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}

// What the pages see. A service left "analyzing" by a function that was killed (the
// 60 s limit) shows as failed, so nobody waits on it; it may be run again.
export function serviceView(s, { detail = false, now = Date.now() } = {}) {
  let { status, error } = s;
  if (status === "analyzing" && Date.parse(s.updatedAt) <= now - STALE_MS) {
    status = "failed";
    error = "The analysis stopped before it finished. Run it again.";
  }
  const view = {
    id: s.id, name: s.name, slug: s.slug, repoFull: s.repoFull, branch: s.branch,
    isPrimary: Boolean(s.isPrimary), status, sha: s.sha || null, error: error || null,
    url: serviceURL(s.slug), plan: parsePlan(s.plan), createdAt: s.createdAt,
    updatedAt: s.updatedAt,
  };
  if (detail) view.log = s.log || "";
  return view;
}

export function projectStatus(services) {
  const st = services.map((s) => s.status);
  if (st.includes("analyzing")) return "analyzing";
  if (st.includes("failed")) return "failed";
  if (st.length && st.every((x) => x === "planned")) return "planned";
  return "queued";
}

function projectView(p, opts) {
  const services = p.services.map((s) => serviceView(s, opts));
  return { id: p.id, name: p.name, slug: p.slug, status: projectStatus(services),
           createdAt: p.createdAt, url: serviceURL(p.slug), services };
}

// ---------- JSON endpoints ----------

async function me(c) {
  const [settings, conn, services, analyses, admin] = await Promise.all([
    c.settings(), store.getConnection(c.client, c.user.id),
    store.countUserServices(c.client, c.user.id), store.countAnalyses(c.client, c.user.id),
    adminStatus(c)]);
  return json({
    user: { id: c.user.id, email: c.user.email, name: c.user.name, isAdmin: admin.isAdmin,
            adminPending: admin.adminPending },
    github: conn ? { installationId: conn.installationId, accountLogin: conn.accountLogin,
                     accountType: conn.accountType } : null,
    app: { configured: github.appConfigured(settings), slug: settings.gh_app_slug || null },
    aiOn: Boolean(c.env.OPENAI_API_KEY), mailOn: mailOn(c.env), baseDomain: BASE_DOMAIN,
    limits: LIMITS, usage: { services, analysesToday: analyses },
  });
}

// The App and this account's installation, or the JSON refusal to send instead.
async function connected(c) {
  const settings = await c.settings();
  if (!github.appConfigured(settings)) {
    return { refusal: fail(503, "GitHub connections are not open yet.") };
  }
  const conn = await store.getConnection(c.client, c.user.id);
  if (!conn) return { refusal: fail(409, "Connect GitHub first.") };
  return { settings, conn };
}

async function repos(c) {
  const { settings, conn, refusal } = await connected(c);
  if (refusal) return refusal;
  try {
    return json({ repos: await c.gh.listRepos(settings, conn.installationId) });
  } catch (err) {
    if (err.status === 404) {
      // The App was uninstalled and the webhook missed: the binding is gone.
      await store.deleteConnection(c.client, c.user.id);
      return fail(409, "The GitHub App is no longer installed there. Connect GitHub again.");
    }
    return fail(502, `Could not list repositories: ${err.message}`);
  }
}

async function listProjects(c) {
  const projects = await store.listProjects(c.client, c.user.id);
  return json({ projects: projects.map((p) => projectView(p)) });
}

function cleanBranch(value) {
  if (value === undefined || value === null) return "";
  if (typeof value !== "string") return null;
  const b = value.trim();
  if (b.length > 200 || b.includes("..") || /[\s~^:?*[\\\x00-\x1f\x7f]/.test(b)) return null;
  return b;
}

// Port of main.py create_project: primary first, name defaults to the primary's
// service name, the primary service takes the project's slug and the others
// <project slug>-<service name>, all through the platform's make_slug.
async function createProject(c) {
  const body = c.body;
  if (!Array.isArray(body.repos)) return fail(400, "Pick at least one repository.");
  // GitHub names are case-insensitive: "Ada/Shop" and "ada/shop" are one repo.
  const seen = new Set();
  let wanted = body.repos.map((r) => (typeof r === "string" ? r.trim() : ""))
    .filter((r) => !seen.has(r.toLowerCase()) && seen.add(r.toLowerCase()));
  if (!wanted.length || wanted.some((r) => !REPO_RE.test(r))) {
    return fail(400, "Pick at least one repository.");
  }
  if (wanted.length > LIMITS.services) {
    return fail(400, `A project has at most ${LIMITS.services} services.`);
  }
  if (body.name !== undefined && typeof body.name !== "string") return fail(400, "Bad project name.");
  const branch = cleanBranch(body.branch);
  if (branch === null) return fail(400, "That is not a branch name.");
  const primary = wanted.find((r) => typeof body.primary === "string" &&
                                     r.toLowerCase() === body.primary.trim().toLowerCase()) || wanted[0];
  wanted = [primary, ...wanted.filter((r) => r !== primary)];

  const { settings, conn, refusal } = await connected(c);
  if (refusal) return refusal;
  const overLimit = async () => {
    const current = await store.countUserServices(c.client, c.user.id);
    return current + wanted.length > LIMITS.services &&
      fail(409, `Service limit reached: ${current}/${LIMITS.services} used, tried to add ` +
                `${wanted.length}. Delete a project to make room.`);
  };
  // Early, to spare GitHub the calls; insertProject is what enforces it.
  const early = await overLimit();
  if (early) return early;

  // Every repo must be one this account's installation can read: a typed name is not
  // access, and the platform will clone with the same installation.
  let token;
  try {
    token = await c.gh.installationToken(settings, conn.installationId);
  } catch (err) {
    return fail(502, `GitHub did not give access: ${err.message}`);
  }
  const infos = [];
  for (const full of wanted) {
    try {
      infos.push(await c.gh.repoInfo(token, full));
    } catch (err) {
      if (err.status === 404) {
        return fail(403, `Cicatrixa cannot reach ${full}. Add it to the GitHub App's ` +
                         "repositories, then try again.");
      }
      return fail(502, `Could not read ${full} on GitHub: ${err.message}`);
    }
  }

  const name = (body.name || "").trim().slice(0, 80) || serviceName(infos[0].full_name);
  for (let attempt = 0; ; attempt++) {
    const chosen = new Set();
    const isTaken = async (slug) => chosen.has(slug) || store.slugTaken(c.client, slug);
    const project = { id: randomUUID(), userId: c.user.id, name };
    project.slug = await makeSlug(name, isTaken);
    chosen.add(project.slug);
    const services = [];
    for (const [i, info] of infos.entries()) {
      const sname = serviceName(info.full_name);
      const slug = i === 0 ? project.slug : await makeSlug(`${project.slug}-${sname}`, isTaken);
      chosen.add(slug);
      services.push({ id: randomUUID(), name: sname, slug, repoFull: info.full_name,
                      branch: branch || info.default_branch, isPrimary: i === 0 });
    }
    let stored;
    try {
      stored = await store.insertProject(c.client, project, services,
                                         { maxServices: LIMITS.services });
    } catch (err) {
      // Someone took a slug between the check and the insert: pick again.
      if (attempt < 3 && /UNIQUE/i.test(String(err.message))) continue;
      throw err;
    }
    // Another request of this account's filled the room while GitHub answered.
    if (!stored) return (await overLimit()) || fail(409, "Service limit reached. Try again.");
    return json({ project: projectView(await store.getProject(c.client, c.user.id, project.id),
                                       { detail: true }) });
  }
}

async function projectDetail(c, projectId) {
  const project = await store.getProject(c.client, c.user.id, projectId);
  if (!project) return fail(404, "No such project.");
  return json({ project: projectView(project, { detail: true }) });
}

async function deleteProject(c, projectId) {
  if (!await store.deleteProject(c.client, c.user.id, projectId)) {
    return fail(404, "No such project.");
  }
  return json({ ok: true });
}

// Streams NDJSON: {"type":"log","line":…} per line, then {"type":"done","service":…}
// or {"type":"error","error":…}. Every state change is written to Turso as it
// happens, so a buffered response or a killed function still leaves the truth there.
async function analyzeService(c, projectId, serviceId) {
  const project = await store.getProject(c.client, c.user.id, projectId);
  const service = project?.services.find((s) => s.id === serviceId);
  if (!service) return fail(404, "No such service.");
  const { settings, conn, refusal } = await connected(c);
  if (refusal) return refusal;

  const analysisId = randomUUID();
  if (!await store.recordAnalysis(c.client, { id: analysisId, userId: c.user.id },
                                  LIMITS.analysesPerDay)) {
    return fail(429, `That is ${LIMITS.analysesPerDay} analyses in 24 hours, the most for now. ` +
                     "Try again later.");
  }
  const startedAt = Date.now();
  if (!await store.claimService(c.client, service.id, startedAt, STALE_MS)) {
    await store.forgetAnalysis(c.client, analysisId);
    return fail(409, "This service is already being analyzed. Wait for it to finish.");
  }

  const prefix = project.services.length > 1 ? `⟦${service.name}⟧ ` : "";
  const siblings = project.services.filter((s) => s.id !== service.id).map((s) => ({
    name: s.name, slug: s.slug, port: Number.isInteger(parsePlan(s.plan)?.port)
      ? parsePlan(s.plan).port : null,
    public_url: serviceURL(s.slug), env_key: envKey(s.name) }));

  const encoder = new TextEncoder();
  let closed = false;
  let controller;
  const send = (obj) => {
    if (closed) return;
    try { controller.enqueue(encoder.encode(`${JSON.stringify(obj)}\n`)); } catch { closed = true; }
  };
  const lines = [];
  let saving = Promise.resolve();
  let lastSave = 0;
  const log = (line) => {
    const stamped = `[${clock(Date.now())}] ${prefix}${line}`;
    lines.push(stamped);
    send({ type: "log", line: stamped });
    if (Date.now() - lastSave >= 1000) {
      lastSave = Date.now();
      const text = lines.join("\n");
      saving = saving.then(() => store.saveLog(c.client, service.id, text)).catch(() => {});
    }
  };

  async function run() {
    let result;
    try {
      const token = await c.gh.installationToken(settings, conn.installationId);
      result = await c.analyze({ token, repoFull: service.repoFull, branch: service.branch,
                                 siblings, env: c.env, gh: c.gh, log, now: startedAt,
                                 name: service.name });
    } catch (err) {
      const why = String(err?.message || err).slice(0, 500);
      log(`✖ analysis failed: ${why}`);
      await saving;
      await store.finishService(c.client, service.id, { status: "failed", log: lines.join("\n"),
                                                        error: why });
      send({ type: "error", error: why });
      return;
    }
    log(`✔ planned — deploys automatically when deploying opens (${serviceURL(service.slug)})`);
    await saving;
    await store.finishService(c.client, service.id, { status: "planned", sha: result.sha,
                                                      plan: result.plan, log: lines.join("\n") });
    send({ type: "done", service: serviceView(await store.getService(c.client, service.id),
                                              { detail: true }) });
  }

  const stream = new ReadableStream({
    start(ctrl) {
      controller = ctrl;
      // Only Turso failing gets here. The service then stays "analyzing" and is shown
      // as stopped once STALE_MS has passed, so it can be run again.
      run().catch((err) => {
        console.error("workspace: could not store an analysis:", err?.message || err);
        send({ type: "error", error: "The result could not be saved. Run it again in a minute." });
      }).finally(() => {
        if (closed) return;
        closed = true;
        try { ctrl.close(); } catch { /* the reader went away */ }
      });
    },
    // The visitor left: the analysis still finishes and is stored.
    cancel() { closed = true; },
  });
  return new Response(stream, { headers: {
    "content-type": "application/x-ndjson; charset=utf-8", ...NO_STORE,
    "x-accel-buffering": "no" } });
}

async function disconnect(c) {
  const conn = await store.getConnection(c.client, c.user.id);
  if (conn && c.body.revoke === true) {
    const settings = await c.settings();
    if (github.appConfigured(settings)) await c.gh.revokeInstallation(settings, conn.installationId);
  }
  await store.deleteConnection(c.client, c.user.id);
  return json({ ok: true });
}

async function admin(c) {
  if (!await isAdmin(c)) return fail(403, "Admins only.");
  const settings = await c.settings();
  return json({
    app: { configured: github.appConfigured(settings), slug: settings.gh_app_slug || null,
           name: settings.gh_app_name || null },
    counts: await store.counts(c.client),
    aiOn: Boolean(c.env.OPENAI_API_KEY), mailOn: mailOn(c.env),
  });
}

// An admin address claims its admin rights: Better Auth emails the account's own address
// a password-reset link, and the password set through it is the proof (adminStatus).
// The request goes through Better Auth's handler, not auth.api, so its own limit on
// /request-password-reset (per visitor address) applies; a direct call skips the limiter.
async function claimAdmin(c) {
  const { isAdmin: already, adminPending } = await adminStatus(c);
  if (already) return fail(409, "This account is already an admin.");
  if (!adminPending) return fail(403, "Admins only.");
  const base = baseURL(c.env);
  const headers = new Headers({ "content-type": "application/json" });
  for (const name of ["origin", "x-forwarded-for", "x-real-ip", "user-agent"]) {
    const value = c.request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const res = await c.auth.handler(new Request(`${base}/api/auth/request-password-reset`, {
    method: "POST", headers,
    body: JSON.stringify({ email: c.user.email, redirectTo: `${base}/reset-password` }),
  }));
  if (res.status === 429) {
    return fail(429, "Too many emails asked for from here. Try again in a minute.");
  }
  if (!res.ok) {
    console.error(`workspace: admin claim: Better Auth answered ${res.status}`);
    return fail(502, "The email could not be sent. Try again in a minute.");
  }
  return json({ ok: true });
}

// The page POSTs a hidden form to `action` with field manifest = `manifest` (a JSON
// string): GitHub's manifest flow creates the App, then comes back to
// /admin/github/callback with a code and our state.
async function createApp(c) {
  if (!await isAdmin(c)) return fail(403, "Admins only.");
  if (github.appConfigured(await c.settings())) {
    return fail(409, "A GitHub App is already set up. A second one would strand every " +
                     "installation of the first.");
  }
  const name = typeof c.body.name === "string" && c.body.name.trim() ? c.body.name.trim()
    : "Cicatrixa";
  if (!/^[A-Za-z0-9][A-Za-z0-9 ._-]{0,33}$/.test(name)) {
    return fail(400, "An App name is up to 34 letters, digits, spaces, dots, dashes or underscores.");
  }
  const org = typeof c.body.org === "string" ? c.body.org.trim() : "";
  if (org && !/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$/.test(org)) {
    return fail(400, "That is not a GitHub organisation name.");
  }
  const state = signState(c.env.BETTER_AUTH_SECRET, `ghapp:${c.user.id}`);
  return json({ action: github.manifestFormURL(state, org || undefined),
                manifest: JSON.stringify(github.buildManifest(baseURL(c.env), name)) });
}

// ---------- pages GitHub sends people to ----------

const LOGIN_FOR_CONNECT = `/login?${new URLSearchParams({ next: "/connect/github" })}`;

async function connectStart(c) {
  if (!c.user) return redirect(LOGIN_FOR_CONNECT);
  const settings = await c.settings();
  if (!github.appConfigured(settings)) return withError("/connect/github", "app_missing");
  const conn = await store.getConnection(c.client, c.user.id);
  if (conn) {
    // Already installed: GitHub's installation settings page has the repo picker.
    // The fresh-install page has no way forward for an App that is installed.
    return redirect(conn.accountType === "Organization"
      ? `https://github.com/organizations/${encodeURIComponent(conn.accountLogin)}/settings/installations/${conn.installationId}`
      : `https://github.com/settings/installations/${conn.installationId}`);
  }
  const state = signState(c.env.BETTER_AUTH_SECRET, `gh:${c.user.id}`);
  return redirect(`https://github.com/apps/${encodeURIComponent(settings.gh_app_slug)}` +
                  `/installations/new?state=${encodeURIComponent(state)}`);
}

async function connectAuthorize(c) {
  if (!c.user) return redirect(LOGIN_FOR_CONNECT);
  const url = github.authorizeURL(await c.settings(),
                                  signState(c.env.BETTER_AUTH_SECRET, `gh:${c.user.id}`),
                                  baseURL(c.env));
  return url ? redirect(url) : withError("/connect/github", "app_missing");
}

// Exact port of main.py connect_setup. Nothing in this URL can be taken on trust:
// installation_id can be typed, and the ids are sequential; binding someone else's
// hands over their private repos. So the state must be one this same signed-in
// account started, GitHub's OAuth code must show the installation is on an account
// they control, and one already bound to another account is refused.
async function connectCallback(c) {
  if (!c.user) return redirect(LOGIN_FOR_CONNECT);
  const params = c.url.searchParams;
  const refuse = (code) => withError("/connect/github", code);

  // An organisation member asked for an install: GitHub sent the request to the owners
  // and there is no installation yet. Binding anything here (a personal installation
  // the code happens to show) would not be what they asked for, and a plain member can
  // never connect the organisation's installation: an owner does, from their own account.
  // It binds nothing, so it needs no state.
  if (params.get("setup_action") === "request") return refuse("org_request");
  if (readState(c.env.BETTER_AUTH_SECRET, params.get("state"), "gh:") !== c.user.id) {
    return refuse("state");
  }
  const raw = params.get("installation_id");
  const wanted = raw && /^\d{1,15}$/.test(raw) ? Number(raw) : null;
  if (wanted && await store.installationOwner(c.client, wanted) === c.user.id) {
    return redirect("/projects/new");                                // already theirs
  }
  if (!params.get("code")) return refuse("no_code");
  const proof = await c.gh.installationsUserControls(await c.settings(), params.get("code"));
  if (!proof) return refuse("github");
  const { me: person, controlled } = proof;

  const boundElsewhere = async (iid) => {
    const owner = await store.installationOwner(c.client, iid);
    return owner !== null && owner !== c.user.id;
  };
  let chosen;
  if (wanted) {
    if (!controlled.has(wanted)) return refuse("not_yours");
    chosen = wanted;
  } else {
    const free = [];
    for (const iid of controlled.keys()) if (!await boundElsewhere(iid)) free.push(iid);
    if (!free.length) return refuse("not_installed");
    const created = (iid) => controlled.get(iid).created_at || "";
    chosen = free.reduce((a, b) => (created(b) > created(a) ? b : a));
  }
  if (await boundElsewhere(chosen)) return refuse("taken");
  const account = controlled.get(chosen).account || {};
  try {
    await store.bindInstallation(c.client, { userId: c.user.id, installationId: chosen,
                                             accountLogin: account.login || "",
                                             accountType: account.type || "",
                                             provedBy: person?.login || "" });
  } catch (err) {
    if (/UNIQUE/i.test(String(err.message))) return refuse("taken");   // lost a race
    throw err;
  }
  return redirect("/projects/new?github=connected");
}

// GitHub comes back here after the admin creates the App from /admin.
async function adminCallback(c) {
  if (!c.user) return redirect(`/login?${new URLSearchParams({ next: "/admin" })}`);
  const refuse = (code) => withError("/admin", code);
  if (!await isAdmin(c)) return refuse("admin_only");
  const params = c.url.searchParams;
  if (readState(c.env.BETTER_AUTH_SECRET, params.get("state"), "ghapp:") !== c.user.id) {
    return refuse("app_state");
  }
  // The new App, if GitHub made one, is not stored: a second would strand every
  // installation of the first.
  if (github.appConfigured(await c.settings())) return refuse("app_exists");
  if (!params.get("code")) return refuse("app_failed");
  let values;
  try {
    values = await c.gh.exchangeManifestCode(params.get("code"));
  } catch (err) {
    console.error("workspace: GitHub did not hand over the App:", err.message);
    return refuse("app_failed");
  }
  try {
    await store.saveAppSettings(c.client, c.env.BETTER_AUTH_SECRET, values);
  } catch (err) {
    if (/UNIQUE|constraint/i.test(String(err.message))) return refuse("app_exists");
    throw err;
  }
  return redirect("/admin?github=created");
}

// Only installation removals matter here: pushes are picked up at import, which plans
// every repo again on the box.
async function webhook(c) {
  const raw = Buffer.from(await c.request.arrayBuffer());
  const settings = await c.settings();
  if (!github.verifyWebhook(settings.gh_app_webhook_secret, raw,
                            c.request.headers.get("x-hub-signature-256"))) {
    return fail(401, "bad signature");
  }
  let payload;
  try { payload = JSON.parse(raw.toString("utf8")); } catch { return json({ ok: true }); }
  if (c.request.headers.get("x-github-event") === "installation" &&
      payload?.action === "deleted" && Number.isInteger(payload?.installation?.id)) {
    const removed = await store.deleteInstallation(c.client, payload.installation.id);
    return json({ ok: true, removed });
  }
  return json({ ok: true });
}

// ---------- dispatch ----------

const ROUTES = [
  // [method, pattern, handler, kind]; kind: json (session, JSON), page, hook
  ["GET", /^me$/, me, "json"],
  ["GET", /^repos$/, repos, "json"],
  ["GET", /^projects$/, listProjects, "json"],
  ["POST", /^projects$/, createProject, "json"],
  ["GET", /^projects\/([\w-]{1,64})$/, projectDetail, "json"],
  ["POST", /^projects\/([\w-]{1,64})\/services\/([\w-]{1,64})\/analyze$/, analyzeService, "json"],
  ["POST", /^projects\/([\w-]{1,64})\/delete$/, deleteProject, "json"],
  ["POST", /^github\/disconnect$/, disconnect, "json"],
  ["GET", /^admin$/, admin, "json"],
  ["POST", /^admin\/claim$/, claimAdmin, "json"],
  ["POST", /^admin\/github-app$/, createApp, "json"],
  ["GET", /^connect\/github\/start$/, connectStart, "page"],
  ["GET", /^connect\/github\/authorize$/, connectAuthorize, "page"],
  ["GET", /^connect\/github\/(?:callback|setup)$/, connectCallback, "page"],
  ["GET", /^admin\/github\/callback$/, adminCallback, "page"],
  ["POST", /^webhooks\/github$/, webhook, "hook"],
];

function trustedOrigins(auth, env) {
  const list = auth?.options?.trustedOrigins;
  return Array.isArray(list) ? list : [baseURL(env)];
}

// A JSON mutation needs a JSON body and an Origin we trust. A form or a script on
// another site can send neither: a cross-site JSON POST must pass a CORS preflight,
// which this API never grants.
function csrfOK(request, origins) {
  const type = (request.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
  const origin = request.headers.get("origin");
  return type === "application/json" && Boolean(origin) && origins.includes(origin);
}

async function sessionUser(auth, request) {
  try {
    return (await auth.api.getSession({ headers: request.headers }))?.user || null;
  } catch (err) {
    console.error("workspace: session check failed:", err.message);
    return null;
  }
}

// deps: {env, client, auth, gh?, analyze?} — gh and analyze are injectable for tests.
export async function handleWorkspace(request, { env = process.env, client, auth,
                                                 gh = github, analyze: analyzeImpl = analyze }) {
  const url = new URL(request.url);
  const key = routeKey(url.pathname);
  const matches = ROUTES.map(([method, re, fn, kind]) => ({ method, m: key.match(re), fn, kind }))
    .filter((r) => r.m);
  if (!matches.length) return fail(404, "Not found.");
  const route = matches.find((r) => r.method === request.method);
  if (!route) return fail(405, "Method not allowed.");

  let settingsP;
  const c = {
    request, url, env, client, auth, gh, analyze: analyzeImpl, body: {},
    settings: () => (settingsP ??= store.getSettings(client, env.BETTER_AUTH_SECRET)),
  };
  const args = route.m.slice(1);
  try {
    if (route.kind === "hook") return await route.fn(c, ...args);
    if (route.kind === "json" && request.method === "POST") {
      if (!csrfOK(request, trustedOrigins(auth, env))) {
        return fail(403, "Refused: this request did not come from Cicatrixa's own pages.");
      }
      try {
        c.body = await request.json();
      } catch {
        return fail(400, "The request body is not JSON.");
      }
      if (!c.body || typeof c.body !== "object" || Array.isArray(c.body)) {
        return fail(400, "The request body must be a JSON object.");
      }
    }
    c.user = await sessionUser(auth, request);
    if (route.kind === "json" && !c.user) return fail(401, "Sign in first.");
    return await route.fn(c, ...args);
  } catch (err) {
    console.error(`workspace: ${request.method} ${key} failed:`, err?.message || err);
    if (route.kind === "page") {
      return key.startsWith("admin/") ? withError("/admin", "app_failed")
        : withError("/connect/github", "github");
    }
    return fail(500, "Something went wrong. Try again.");
  }
}
