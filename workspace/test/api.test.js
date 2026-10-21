// The workspace end to end through api/workspace.js, called the way Vercel calls it
// after vercel.json's rewrites, against a real libSQL database file. Users are made
// through api/auth.js (sign-up, emailed link) as in accounts/test/flow.test.js.
// GitHub, OpenAI and Resend are fakes behind globalThis.fetch; the GitHub fake checks
// the App's JWT with the public half of a key generated here, and only answers an
// installation token for the repos of its own installation.
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, verify } from "node:crypto";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { createClient } from "@libsql/client";

const BASE = "http://localhost:3000";
const SECRET = "test-secret-that-is-long-enough-0123456789";
const dir = mkdtempSync(join(tmpdir(), "cx-workspace-"));
Object.assign(process.env, {
  TURSO_DATABASE_URL: `file:${join(dir, "workspace.db")}`,
  BETTER_AUTH_SECRET: SECRET,
  BETTER_AUTH_URL: BASE,
  RESEND_API_KEY: "re_test",
  MAIL_FROM: "Cicatrixa <noreply@cicatrixa.com>",
  ADMIN_EMAILS: "boss@example.com, chief@example.com,sleepy@example.com",
});
delete process.env.OPENAI_API_KEY;
delete process.env.AI_MODEL;

const db = createClient({ url: process.env.TURSO_DATABASE_URL });
const rows = async (sql, args = []) => {
  const rs = await db.execute({ sql, args });
  return rs.rows.map((r) => Object.fromEntries(rs.columns.map((c, i) => [c, r[i]])));
};

// ---------- the fake world ----------

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const PEM = privateKey.export({ type: "pkcs1", format: "pem" });
const APP = { id: 4242, slug: "cicatrixa-test", name: "Cicatrixa Test", pem: PEM,
              webhook_secret: "hook-secret", client_id: "Iv1.test", client_secret: "cs-test" };

const sha = (c) => c.repeat(40);
const world = {
  manifestCodes: new Set(["mcode-1", "mcode-2"]),
  conversions: 0,
  // id → {account, repos, created_at}
  installations: new Map(),
  // OAuth code → {me, visible: [installation ids], memberships: {org: answer | "error"}}
  people: new Map(),
  // full → {default_branch, private, branches: {name: sha}, trees: {sha: [paths]}, files: {path: text}}
  repos: new Map(),
  deleted: [],
  openai: [],          // replies to hand out, in order: object (JSON reply) or a number (status)
  openaiRequests: [],
  gate: null,          // a promise the tree endpoint waits on, to watch a stream mid-run
};

function installation(id, login, type, accountId, repos, created = "2026-10-01T00:00:00Z") {
  world.installations.set(id, { account: { login, type, id: accountId }, repos, created_at: created });
}

function person(code, me, visible, memberships = {}) {
  world.people.set(code, { me, visible, memberships });
}

function repo(full, { branch = "main", head = sha("a"), files = {}, extra = [] } = {}) {
  world.repos.set(full, { default_branch: branch, private: true, branches: { [branch]: head },
                          trees: { [head]: [...Object.keys(files), ...extra] }, files });
}

const jsonRes = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json" } });
const notFound = () => jsonRes({ message: "Not Found" }, 404);

function appJWTok(headers) {
  const token = (headers.get("authorization") || "").replace(/^Bearer /, "");
  const [h, p, s] = token.split(".");
  if (!s) return false;
  if (!verify("RSA-SHA256", Buffer.from(`${h}.${p}`), publicKey, Buffer.from(s, "base64url"))) {
    return false;
  }
  const claims = JSON.parse(Buffer.from(p, "base64url"));
  return String(claims.iss) === String(APP.id) && claims.exp * 1000 > Date.now();
}

function installationFor(headers) {
  const m = (headers.get("authorization") || "").match(/^Bearer ghs_(\d+)$/);
  return m ? world.installations.get(Number(m[1])) : null;
}

const personFor = (headers) => {
  const m = (headers.get("authorization") || "").match(/^Bearer ghu_(.+)$/);
  return m ? world.people.get(m[1]) : null;
};

const instJSON = (id) => ({ id, ...world.installations.get(id) });

async function github(method, url, headers, body) {
  const p = url.pathname;
  let m;
  if (url.host === "github.com" && p === "/login/oauth/access_token" && method === "POST") {
    const form = new URLSearchParams(body);
    if (form.get("client_id") !== APP.client_id || form.get("client_secret") !== APP.client_secret) {
      return jsonRes({ error: "incorrect_client_credentials" });
    }
    return world.people.has(form.get("code")) ? jsonRes({ access_token: `ghu_${form.get("code")}` })
      : jsonRes({ error: "bad_verification_code" });
  }
  if ((m = p.match(/^\/app-manifests\/([^/]+)\/conversions$/)) && method === "POST") {
    world.conversions++;
    if (headers.get("authorization")) return jsonRes({ message: "unexpected credential" }, 400);
    if (!world.manifestCodes.delete(m[1])) return notFound();
    return jsonRes({ id: APP.id, slug: APP.slug, name: APP.name, pem: APP.pem,
                     webhook_secret: APP.webhook_secret, client_id: APP.client_id,
                     client_secret: APP.client_secret }, 201);
  }
  if ((m = p.match(/^\/app\/installations\/(\d+)(\/access_tokens)?$/))) {
    if (!appJWTok(headers)) return jsonRes({ message: "A JSON web token could not be decoded" }, 401);
    const id = Number(m[1]);
    if (!world.installations.has(id)) return notFound();
    if (m[2] && method === "POST") return jsonRes({ token: `ghs_${id}`, expires_at: "2099-01-01T00:00:00Z" }, 201);
    if (method === "GET" && !m[2]) return jsonRes(instJSON(id));
    if (method === "DELETE" && !m[2]) {
      world.deleted.push(id);
      return new Response(null, { status: 204 });
    }
  }
  if (p === "/installation/repositories" && method === "GET") {
    const inst = installationFor(headers);
    if (!inst) return jsonRes({ message: "Bad credentials" }, 401);
    const page = Number(url.searchParams.get("page") || 1);
    const per = Number(url.searchParams.get("per_page") || 30);
    const list = inst.repos.map((full) => ({ full_name: full, private: true, pushed_at: "2026-10-20T00:00:00Z",
                                             default_branch: world.repos.get(full)?.default_branch || "main" }));
    return jsonRes({ total_count: list.length, repositories: list.slice((page - 1) * per, page * per) });
  }
  if ((m = p.match(/^\/repos\/([^/]+\/[^/]+)(\/.*)?$/)) && method === "GET") {
    const inst = installationFor(headers);
    const r = world.repos.get(m[1]);
    if (!inst || !r || !inst.repos.includes(m[1])) return notFound();
    const rest = m[2] || "";
    if (!rest) return jsonRes({ full_name: m[1], default_branch: r.default_branch, private: r.private });
    let n;
    if ((n = rest.match(/^\/commits\/(.+)$/))) {
      const head = r.branches[decodeURIComponent(n[1])];
      return head ? jsonRes({ sha: head }) : jsonRes({ message: "No commit found for SHA" }, 422);
    }
    if ((n = rest.match(/^\/git\/trees\/(.+)$/))) {
      if (world.gate) await world.gate;
      const paths = r.trees[n[1]];
      if (!paths) return notFound();
      return jsonRes({ sha: n[1], truncated: false, tree: [
        ...paths.map((path) => ({ path, type: "blob" })),
        ...[...new Set(paths.filter((x) => x.includes("/")).map((x) => x.split("/")[0]))]
          .map((path) => ({ path, type: "tree" }))] });
    }
    if ((n = rest.match(/^\/contents\/(.+)$/))) {
      const path = n[1].split("/").map(decodeURIComponent).join("/");
      if (headers.get("accept") !== "application/vnd.github.raw+json") return jsonRes({ message: "raw please" }, 415);
      if (url.searchParams.get("ref") !== r.branches[r.default_branch] &&
          !Object.values(r.branches).includes(url.searchParams.get("ref"))) return notFound();
      return path in r.files ? new Response(r.files[path]) : notFound();
    }
    return notFound();
  }
  const who = personFor(headers);
  if (p === "/user" && method === "GET") return who ? jsonRes(who.me) : jsonRes({ message: "Bad credentials" }, 401);
  if (p === "/user/installations" && method === "GET" && who) {
    const page = Number(url.searchParams.get("page") || 1);
    const list = who.visible.map(instJSON);
    return jsonRes({ total_count: list.length, installations: list.slice((page - 1) * 100, page * 100) });
  }
  if ((m = p.match(/^\/user\/memberships\/orgs\/([^/]+)$/)) && who) {
    const answer = who.memberships[m[1]];
    if (answer === "error") return jsonRes({ message: "Server Error" }, 500);
    return answer ? jsonRes({ ...answer, organization: { login: m[1] } }) : notFound();
  }
  return notFound();
}

const outbox = [];
const unexpected = [];
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  const method = init.method || "GET";
  const headers = new Headers(init.headers);
  if (url.href === "https://api.resend.com/emails") {
    outbox.push(JSON.parse(init.body));
    return jsonRes({ id: "x" });
  }
  if (url.href === "https://api.openai.com/v1/responses") {
    world.openaiRequests.push({ headers, body: JSON.parse(init.body) });
    const reply = world.openai.shift();
    if (typeof reply === "number" || reply === undefined) return jsonRes({ error: "down" }, reply || 500);
    return jsonRes({ output: [{ type: "message", content: [{ type: "output_text", text: reply }] }] });
  }
  if (url.host === "api.github.com" || url.host === "github.com") {
    if (url.host === "api.github.com" && headers.get("x-github-api-version") !== "2022-11-28") {
      unexpected.push(`no API version header: ${url}`);
    }
    return github(method, url, headers, init.body);
  }
  unexpected.push(`unexpected fetch: ${method} ${url}`);
  return new Response("no network in tests", { status: 599 });
};

const auth = await import("../../api/auth.js");
const ws = await import("../../api/workspace.js");
const { buildManifest } = await import("../github.js");
const { readState, signState, decryptSetting } = await import("../secrets.js");

// ---------- people ----------

let ipCounter = 0;
function client() {
  const ip = `10.1.0.${++ipCounter}`;
  const jar = new Map();
  async function send(handler, url, method, { body, origin = BASE, contentType, headers = {} } = {}) {
    const h = new Headers({ "x-forwarded-for": ip, ...headers });
    if (origin) h.set("origin", origin);
    if (body !== undefined) h.set("content-type", contentType || "application/json");
    if (jar.size) h.set("cookie", [...jar].map(([k, v]) => `${k}=${v}`).join("; "));
    const res = await handler(new Request(url, {
      method, headers: h,
      body: body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body) }));
    for (const c of res.headers.getSetCookie()) {
      const [pair] = c.split(";");
      const [k, v] = pair.split("=");
      if (v === "" || /max-age=0/i.test(c)) jar.delete(k); else jar.set(k, v);
    }
    return res;
  }
  async function authCall(method, path, body) {
    const [p, query] = path.split("?");
    const url = new URL(`${BASE}/api/auth`);
    url.searchParams.set("__auth_path", p.replace(/^\/api\/auth\//, ""));
    for (const [k, v] of new URLSearchParams(query || "")) url.searchParams.set(k, v);
    return send(method === "GET" ? auth.GET : auth.POST, url, method, { body });
  }
  // What Vercel hands api/workspace.js after vercel.json's rewrites.
  function rewritten(path) {
    const [p, query] = path.split("?");
    const url = new URL(`${BASE}/api/workspace`);
    const key = p.startsWith("/api/workspace/") ? p.slice("/api/workspace/".length)
      : p === "/api/webhooks/github" ? "webhooks/github" : p.slice(1);
    url.searchParams.set("__path", key);
    for (const [k, v] of new URLSearchParams(query || "")) url.searchParams.append(k, v);
    return url;
  }
  async function call(method, path, body, opts = {}) {
    const res = await send(method === "GET" ? ws.GET : ws.POST, rewritten(path), method,
                           { body, ...opts });
    let data = null;
    if ((res.headers.get("content-type") || "").startsWith("application/json")) {
      data = await res.clone().json();
    }
    return { status: res.status, data, location: res.headers.get("location"), res };
  }
  return {
    jar, authCall,
    get: (path, opts) => call("GET", `/api/workspace/${path}`, undefined, opts),
    post: (path, body = {}, opts) => call("POST", `/api/workspace/${path}`, body, opts),
    page: (path) => call("GET", path),
    raw: call,
  };
}

const linkIn = (mail) => mail.text.match(/https?:\/\/\S+/)[0];

async function user(email) {
  const c = client();
  const r = await c.authCall("POST", "/sign-up/email", {
    email, password: "correct horse battery", name: email.split("@")[0], callbackURL: "/dashboard" });
  assert.equal(r.status, 200);
  const mail = [...outbox].reverse().find((m) => m.to[0] === email);
  assert.equal((await c.authCall("GET", linkIn(mail).replace(BASE, ""))).status, 302);
  const s = await (await c.authCall("GET", "/get-session")).json();
  c.id = s.user.id;
  c.email = email;
  return c;
}

const ndjson = async (res) => (await res.text()).trim().split("\n").map((l) => JSON.parse(l));
const query = (location) => Object.fromEntries(new URL(location, BASE).searchParams);

// GitHub sends the person back to the callback.
const callback = (c, params) => c.page(`/connect/github/callback?${new URLSearchParams(params)}`);
const ghState = (c) => signState(SECRET, `gh:${c.id}`);

const people = [];
for (const n of ["ada", "bob", "carl", "dana", "erin", "fred", "gina", "hana", "boss", "chief", "sleepy"]) {
  people.push(await user(`${n}@example.com`));
}
const [ada, bob, carl, dana, erin, fred, gina, hana, boss, chief, sleepy] = people;

// ---------- before the App exists ----------

test("signed out: JSON says sign in, GitHub's pages send to login", async () => {
  const anon = client();
  for (const path of ["me", "repos", "projects", "admin"]) {
    const r = await anon.get(path);
    assert.equal(r.status, 401, path);
    assert.deepEqual(r.data, { error: "Sign in first." });
  }
  assert.equal((await anon.post("projects", { repos: ["ada/x"] })).status, 401);
  for (const path of ["/connect/github/start", "/connect/github/authorize",
                      "/connect/github/callback?code=x&state=y"]) {
    const r = await anon.page(path);
    assert.equal(r.status, 303);
    assert.equal(r.location, "/login?next=%2Fconnect%2Fgithub");
  }
  assert.equal((await anon.page("/admin/github/callback?code=x")).location, "/login?next=%2Fadmin");
  assert.equal((await anon.get("nothing-here")).status, 404);
  assert.equal((await ada.get("projects", { headers: {} })).status, 200);
  assert.equal((await ada.raw("GET", "/api/workspace/github/disconnect")).status, 405);
});

test("before the App is created: nothing to connect, and webhooks are not trusted", async () => {
  const me = await ada.get("me");
  assert.equal(me.status, 200);
  assert.deepEqual(me.data, {
    user: { id: ada.id, email: "ada@example.com", name: "ada", isAdmin: false },
    github: null, app: { configured: false, slug: null }, aiOn: false, mailOn: true,
    baseDomain: "cicatrixa.com", limits: { services: 5, analysesPerDay: 20 },
    usage: { services: 0, analysesToday: 0 } });
  assert.equal((await ada.get("repos")).status, 503);
  const start = await ada.page("/connect/github/start");
  assert.equal(new URL(start.location, BASE).pathname, "/connect/github");
  assert.match(query(start.location).error, /not set up yet/);
  assert.match(query((await ada.page("/connect/github/authorize")).location).error, /not set up yet/);
  const body = '{"action":"deleted","installation":{"id":1}}';
  const r = await client().raw("POST", "/api/webhooks/github", body, { headers: {
    "x-github-event": "installation",
    "x-hub-signature-256": `sha256=${createHmac("sha256", "").update(body).digest("hex")}` } });
  assert.equal(r.status, 401);
});

test("admin endpoints refuse everyone but a verified admin address", async () => {
  assert.equal((await boss.get("me")).data.user.isAdmin, true);
  assert.equal((await ada.get("admin")).status, 403);
  assert.equal((await ada.post("admin/github-app", {})).status, 403);
  // An admin's address that was never confirmed is not an admin.
  await db.execute({ sql: `update "user" set emailVerified = 0 where id = ?`, args: [sleepy.id] });
  assert.equal((await sleepy.get("me")).data.user.isAdmin, false);
  assert.equal((await sleepy.get("admin")).status, 403);
  assert.equal((await sleepy.post("admin/github-app", {})).status, 403);
  const cb = await sleepy.page(`/admin/github/callback?code=mcode-1&state=${
    encodeURIComponent(signState(SECRET, `ghapp:${sleepy.id}`))}`);
  assert.match(query(cb.location).error, /Only an admin/);
  assert.equal(world.conversions, 0);
  const ok = await boss.get("admin");
  assert.equal(ok.status, 200);
  assert.deepEqual(ok.data.app, { configured: false, slug: null, name: null });
  assert.equal(ok.data.counts.users, 11);
  assert.equal(ok.data.counts.verifiedUsers, 10);
  assert.equal(ok.data.aiOn, false);
  assert.equal(ok.data.mailOn, true);
});

test("CSRF: a mutation without our Origin, or without a JSON body, is refused", async () => {
  const cases = [
    { origin: null },
    { origin: "https://evil.example" },
    { origin: "https://app.cicatrixa.com.evil.example" },
    { contentType: "text/plain" },
    { contentType: "application/x-www-form-urlencoded", body: "repos=ada%2Fshop-web" },
    { contentType: "multipart/form-data; boundary=x", body: "--x--" },
  ];
  for (const opts of cases) {
    const { body = { repos: ["ada/shop-web"] }, ...rest } = opts;
    for (const path of ["projects", "github/disconnect", "admin/github-app"]) {
      const r = await boss.post(path, body, rest);
      assert.equal(r.status, 403, `${path} ${JSON.stringify(opts)}`);
    }
  }
  // Our own origins pass, including the site's apex.
  assert.equal((await ada.post("github/disconnect", {}, { origin: "https://cicatrixa.com" })).status, 200);
  assert.equal((await ada.post("github/disconnect", "not json")).status, 400);
  assert.equal((await ada.post("github/disconnect", [1])).status, 400);
});

// ---------- creating the GitHub App (manifest flow) ----------

test("the manifest flow: our state only, secrets stored encrypted, a second App refused", async () => {
  const bad = await boss.post("admin/github-app", { org: "not an org!" });
  assert.equal(bad.status, 400);
  const org = await boss.post("admin/github-app", { org: "acme-inc" });
  assert.match(org.data.action, /^https:\/\/github\.com\/organizations\/acme-inc\/settings\/apps\/new\?state=/);

  const r = await boss.post("admin/github-app", { name: "Cicatrixa Test" });
  assert.equal(r.status, 200);
  const action = new URL(r.data.action);
  assert.equal(`${action.origin}${action.pathname}`, "https://github.com/settings/apps/new");
  const state = action.searchParams.get("state");
  assert.equal(readState(SECRET, state, "ghapp:"), boss.id);
  assert.equal(typeof r.data.manifest, "string");
  assert.deepEqual(JSON.parse(r.data.manifest), buildManifest(BASE, "Cicatrixa Test"));

  const cb = (c, params) => c.page(`/admin/github/callback?${new URLSearchParams(params)}`);
  // Another admin's state, an install state, or none: nothing is exchanged.
  for (const s of [signState(SECRET, `ghapp:${chief.id}`), signState(SECRET, `gh:${boss.id}`), ""]) {
    const x = await cb(boss, { code: "mcode-1", state: s });
    assert.equal(new URL(x.location, BASE).pathname, "/admin");
    assert.match(query(x.location).error, /not started from this account/);
  }
  assert.match(query((await cb(chief, { code: "mcode-1", state })).location).error,
               /not started from this account/);
  assert.match(query((await cb(boss, { state })).location).error, /did not send/);
  assert.equal(world.conversions, 0);

  const done = await cb(boss, { code: "mcode-1", state });
  assert.equal(done.location, "/admin?github=created");
  const stored = Object.fromEntries((await rows("select key, value from cx_setting"))
    .map((x) => [x.key, x.value]));
  assert.equal(stored.gh_app_id, "4242");
  assert.equal(stored.gh_app_slug, "cicatrixa-test");
  assert.equal(stored.gh_app_name, "Cicatrixa Test");
  assert.equal(stored.gh_app_client_id, "Iv1.test");
  for (const [key, plain] of [["gh_app_pem", PEM], ["gh_app_webhook_secret", "hook-secret"],
                              ["gh_app_client_secret", "cs-test"]]) {
    assert.match(stored[key], /^enc1:/, key);
    assert.ok(!stored[key].includes(plain) && !stored[key].includes("PRIVATE"), key);
    assert.equal(decryptSetting(SECRET, stored[key]), plain, key);
  }
  assert.deepEqual((await ada.get("me")).data.app, { configured: true, slug: "cicatrixa-test" });
  assert.deepEqual((await boss.get("admin")).data.app,
                   { configured: true, slug: "cicatrixa-test", name: "Cicatrixa Test" });

  // A second App would strand every installation of the first.
  const again = await boss.post("admin/github-app", {});
  assert.equal(again.status, 409);
  const before = world.conversions;
  const second = await cb(boss, { code: "mcode-2", state: signState(SECRET, `ghapp:${boss.id}`) });
  assert.match(query(second.location).error, /already set up/);
  assert.equal(world.conversions, before, "the second code is never exchanged");
  assert.equal((await rows("select value from cx_setting where key = 'gh_app_id'"))[0].value, "4242");
});

// ---------- connecting GitHub ----------

installation(100, "ada", "User", 1, ["ada/shop-web", "ada/shop-api", "ada/broken"]);
installation(200, "bob", "User", 2, ["bob/private"]);
installation(300, "acme", "Organization", 50, ["acme/site"]);
installation(400, "erin", "User", 5, ["erin/old"], "2026-10-01T00:00:00Z");
installation(401, "erin-labs", "Organization", 51, ["erin-labs/new"], "2026-10-05T00:00:00Z");
installation(500, "fred", "User", 6, ["fred/api", "fred/web", "fred/a", "fred/b", "fred/c", "fred/d"]);
installation(600, "gina", "User", 7, ["gina/site", "gina/blog"]);
installation(700, "hana", "User", 8, ["hana/api-server", "hana/front"]);

person("ada-code", { id: 1, login: "ada" }, [100, 200, 300], { acme: { role: "member", state: "active" } });
person("bob-code", { id: 2, login: "bob" }, [200]);
person("carl-code", { id: 3, login: "carl" }, [300], { acme: { role: "admin", state: "active" } });
person("dana-code", { id: 4, login: "dana" }, [300], { acme: { role: "admin", state: "active" } });
person("dana-flaky", { id: 4, login: "dana" }, [300], { acme: "error" });
person("erin-code", { id: 5, login: "erin" }, [400, 401], { "erin-labs": { role: "admin", state: "active" } });
person("fred-code", { id: 6, login: "fred" }, [500]);
person("gina-code", { id: 7, login: "gina" }, [600]);
person("hana-code", { id: 8, login: "hana" }, [700]);

test("connect starts at GitHub's install page with a state only this account can use", async () => {
  const r = await ada.page("/connect/github/start");
  const url = new URL(r.location);
  assert.equal(`${url.origin}${url.pathname}`, "https://github.com/apps/cicatrixa-test/installations/new");
  assert.equal(readState(SECRET, url.searchParams.get("state"), "gh:"), ada.id);
  assert.equal(readState(SECRET, url.searchParams.get("state"), "ghapp:"), null);

  const a = new URL((await ada.page("/connect/github/authorize")).location);
  assert.equal(`${a.origin}${a.pathname}`, "https://github.com/login/oauth/authorize");
  assert.equal(a.searchParams.get("client_id"), "Iv1.test");
  assert.equal(a.searchParams.get("redirect_uri"), `${BASE}/connect/github/callback`);
  assert.equal(readState(SECRET, a.searchParams.get("state"), "gh:"), ada.id);
});

test("a callback started by another account binds nothing", async () => {
  for (const state of [ghState(bob), signState(SECRET, `ghapp:${ada.id}`), "", "junk"]) {
    const r = await callback(ada, { code: "ada-code", installation_id: "100", state });
    assert.match(query(r.location).error, /not started from this account/);
  }
  assert.equal((await ada.get("me")).data.github, null);
});

test("no OAuth code, or one GitHub refuses, binds nothing", async () => {
  const none = await callback(ada, { installation_id: "100", setup_action: "install", state: ghState(ada) });
  assert.equal(new URL(none.location, BASE).pathname, "/connect/github");
  assert.match(query(none.location).error, /GitHub did not send an authorization/);
  const setup = await ada.page(`/connect/github/setup?${new URLSearchParams({ installation_id: "100",
                                                                               state: ghState(ada) })}`);
  assert.match(query(setup.location).error, /did not send an authorization/);
  const refused = await callback(ada, { code: "made-up", installation_id: "100", state: ghState(ada) });
  assert.match(query(refused.location).error, /did not accept the authorization/);
  assert.equal((await ada.get("me")).data.github, null);
});

test("someone else's installation is refused, even when the person can see it", async () => {
  // ada collaborates on bob's repo and is a plain member of acme: she sees both.
  for (const iid of ["200", "300", "999"]) {
    const r = await callback(ada, { code: "ada-code", installation_id: iid, state: ghState(ada) });
    assert.match(query(r.location).error, /not on your own GitHub account/, iid);
  }
  // An organisation whose membership check fails is no proof either.
  const flaky = await callback(dana, { code: "dana-flaky", installation_id: "300", state: ghState(dana) });
  assert.match(query(flaky.location).error, /not on your own GitHub account/);
  assert.equal((await rows("select count(*) as c from cx_github"))[0].c, 0);
});

test("the person's own installation binds, and the callback lands on /projects/new", async () => {
  const r = await callback(ada, { code: "ada-code", installation_id: "100", setup_action: "install",
                                  state: ghState(ada) });
  assert.equal(r.status, 303);
  assert.equal(r.location, "/projects/new?github=connected");
  assert.deepEqual((await ada.get("me")).data.github,
                   { installationId: 100, accountLogin: "ada", accountType: "User" });
  const [row] = await rows("select * from cx_github where userId = ?", [ada.id]);
  assert.equal(row.provedBy, "ada");
  // Coming back with the same installation needs no new proof.
  assert.equal((await callback(ada, { installation_id: "100", state: ghState(ada) })).location, "/projects/new");
  // Connected: start goes to GitHub's settings page for that installation.
  assert.equal((await ada.page("/connect/github/start")).location,
               "https://github.com/settings/installations/100");
});

test("an organisation's admin binds its installation; then nobody else can", async () => {
  const r = await callback(carl, { code: "carl-code", installation_id: "300", state: ghState(carl) });
  assert.equal(r.location, "/projects/new?github=connected");
  assert.deepEqual((await carl.get("me")).data.github,
                   { installationId: 300, accountLogin: "acme", accountType: "Organization" });
  assert.equal((await carl.page("/connect/github/start")).location,
               "https://github.com/organizations/acme/settings/installations/300");
  // dana administers acme too, but 300 is carl's now.
  const taken = await callback(dana, { code: "dana-code", installation_id: "300", state: ghState(dana) });
  assert.match(query(taken.location).error, /already connected to another account/);
  const none = await callback(dana, { code: "dana-code", state: ghState(dana) });
  assert.match(query(none.location).error, /Install it first/);
  assert.equal((await dana.get("me")).data.github, null);
});

test("without an installation id, the newest one the person controls is bound", async () => {
  const r = await callback(erin, { code: "erin-code", state: ghState(erin) });
  assert.equal(r.location, "/projects/new?github=connected");
  assert.deepEqual((await erin.get("me")).data.github,
                   { installationId: 401, accountLogin: "erin-labs", accountType: "Organization" });
});

// ---------- repositories and projects ----------

for (const [c, code, iid] of [[fred, "fred-code", 500], [gina, "gina-code", 600], [hana, "hana-code", 700]]) {
  test(`${c.email} connects`, async () => {
    const r = await callback(c, { code, installation_id: String(iid), state: ghState(c) });
    assert.equal(r.location, "/projects/new?github=connected");
  });
}

const NEXT_PKG = JSON.stringify({ scripts: { build: "next build", start: "next start" },
                                  dependencies: { next: "14.2.0", react: "18.3.1" } });
repo("ada/shop-web", { head: sha("a"), files: { "package.json": NEXT_PKG, "next.config.js": "module.exports = {}\n",
                                                "pages/index.js": "export default () => null\n" },
                       extra: ["node_modules/next/index.js", "public/logo.svg"] });
repo("ada/shop-api", { branch: "trunk", head: sha("b"),
                       files: { "requirements.txt": "fastapi\nuvicorn\n", "main.py": "from fastapi import FastAPI\napp = FastAPI()\n" } });
repo("ada/broken", { head: sha("c"), files: { "index.html": "<h1>hi</h1>\n" } });
repo("bob/private", { head: sha("d"), files: { "go.mod": "module x\n" } });
for (const n of ["api", "web", "a", "b", "c", "d"]) repo(`fred/${n}`, { head: sha("e"), files: { "index.html": "x" } });
repo("gina/site", { head: sha("f"), files: { "index.html": "<p>site</p>\n" } });
repo("gina/blog", { head: sha("1"), files: { "go.mod": "module blog\n", "main.go": "package main\n" } });
repo("hana/api-server", { head: sha("2"), files: {
  "package.json": JSON.stringify({ scripts: { start: "node src/server.js" }, dependencies: { express: "4" } }),
  "src/server.js": "require('./config')\n", "src/config.js": "module.exports = { port: process.env.PORT }\n" } });
repo("hana/front", { head: sha("3"), files: { "index.html": "<script src=app.js></script>\n", "app.js": "fetch('/api/x')\n" } });

test("repos lists the installation's repositories in the platform's shape", async () => {
  const r = await ada.get("repos");
  assert.equal(r.status, 200);
  assert.deepEqual(r.data.repos.map((x) => x.full_name), ["ada/shop-web", "ada/shop-api", "ada/broken"]);
  assert.deepEqual(r.data.repos[1], { full_name: "ada/shop-api", private: true, default_branch: "trunk",
                                      pushed_at: "2026-10-20T00:00:00Z" });
  const none = await bob.get("repos");
  assert.equal(none.status, 409);
  assert.match(none.data.error, /Connect GitHub first/);
});

let shop;
test("a project: primary first, slugs by the platform's rules, branch from each repo", async () => {
  const r = await ada.post("projects", { repos: ["ada/shop-api", "ada/shop-web", "Ada/Shop-Web"],
                                         primary: "ada/shop-web" });
  assert.equal(r.status, 200);
  shop = r.data.project;
  assert.equal(shop.name, "shop-web");
  assert.equal(shop.slug, "shop-web");
  assert.equal(shop.status, "queued");
  assert.equal(shop.url, "https://shop-web.cicatrixa.com");
  assert.deepEqual(shop.services.map((s) => [s.name, s.slug, s.repoFull, s.branch, s.isPrimary, s.status, s.url]), [
    ["shop-web", "shop-web", "ada/shop-web", "main", true, "queued", "https://shop-web.cicatrixa.com"],
    ["shop-api", "shop-web-shop-api", "ada/shop-api", "trunk", false, "queued",
     "https://shop-web-shop-api.cicatrixa.com"]]);
  const list = await ada.get("projects");
  assert.deepEqual(list.data.projects.map((p) => p.id), [shop.id]);
  assert.equal((await ada.get("me")).data.usage.services, 2);
});

test("a repo the installation cannot reach is refused, and nothing is created", async () => {
  for (const repos of [["bob/private"], ["ada/shop-web", "bob/private"], ["ada/missing"]]) {
    const r = await ada.post("projects", { repos });
    assert.equal(r.status, 403, repos.join());
    assert.match(r.data.error, /cannot reach/);
  }
  assert.equal((await ada.get("projects")).data.projects.length, 1);
});

test("bad project requests are refused", async () => {
  for (const body of [{}, { repos: [] }, { repos: "ada/shop-web" }, { repos: ["not a repo"] },
                      { repos: ["../../x"] }, { repos: ["ada/a", "ada/b", "ada/c", "ada/d", "ada/e", "ada/f"] },
                      { repos: ["ada/broken"], branch: "a b" }, { repos: ["ada/broken"], branch: "../x" },
                      { repos: ["ada/broken"], name: 5 }]) {
    assert.equal((await ada.post("projects", body)).status, 400, JSON.stringify(body));
  }
  assert.equal((await dana.post("projects", { repos: ["acme/site"] })).status, 409, "not connected");
});

test("reserved names and taken slugs get the next free suffix; 5 services at most", async () => {
  const api = await fred.post("projects", { repos: ["fred/api"] });
  assert.equal(api.data.project.slug, "api-2", "api is reserved");
  const admin = await fred.post("projects", { repos: ["fred/web"], name: "Admin", branch: "main" });
  assert.equal(admin.data.project.name, "Admin");
  assert.equal(admin.data.project.slug, "admin-2");
  const taken = await fred.post("projects", { repos: ["fred/a"], name: "Shop Web" });
  assert.equal(taken.data.project.slug, "shop-web-2", "ada's project has shop-web");
  const tooMany = await fred.post("projects", { repos: ["fred/b", "fred/c", "fred/d"] });
  assert.equal(tooMany.status, 409);
  assert.match(tooMany.data.error, /Service limit reached: 3\/5 used, tried to add 3/);
  const two = await fred.post("projects", { repos: ["fred/b", "fred/c"] });
  assert.deepEqual(two.data.project.services.map((s) => s.slug), ["b", "b-c"]);
  assert.equal((await fred.post("projects", { repos: ["fred/d"] })).status, 409);
  // A slug someone takes between the check and the insert is picked again.
  const { makeSlug } = await import("../plan.js");
  assert.equal(await makeSlug("shop web", async (s) => ["shop-web", "shop-web-2"].includes(s)), "shop-web-3");
});

test("another account's project is not found", async () => {
  assert.equal((await bob.get(`projects/${shop.id}`)).status, 404);
  assert.equal((await bob.post(`projects/${shop.id}/delete`)).status, 404);
  assert.equal((await bob.post(`projects/${shop.id}/services/${shop.services[0].id}/analyze`)).status, 404);
  assert.equal((await ada.get("projects/no-such-id")).status, 404);
});

// ---------- analysis ----------

const STAMP = /^\[\d\d:\d\d:\d\d\] /;

test("analysis streams the platform's log and stores a heuristic plan", async () => {
  const [web] = shop.services;
  const res = (await ada.post(`projects/${shop.id}/services/${web.id}/analyze`)).res;
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type"), /^application\/x-ndjson/);
  const events = await ndjson(res);
  const logs = events.filter((e) => e.type === "log").map((e) => e.line);
  for (const l of logs) assert.match(l, STAMP);
  const text = logs.map((l) => l.replace(STAMP, ""));
  assert.deepEqual(text.slice(0, 4), [
    "⟦shop-web⟧ ⇣ reading ada/shop-web@main",
    `⟦shop-web⟧   at commit ${sha("a").slice(0, 10)}`,
    "⟦shop-web⟧ 🧠 analyzing repository (heuristics)",
    "⟦shop-web⟧   plan: heuristic: node app (port 3000)"]);
  assert.match(text.at(-1), /^⟦shop-web⟧ ✔ planned — deploys automatically when deploying opens/);
  const done = events.at(-1);
  assert.equal(done.type, "done");
  assert.equal(done.service.status, "planned");
  assert.equal(done.service.sha, sha("a"));
  assert.equal(done.service.plan.port, 3000);
  assert.match(done.service.plan.dockerfile, /npx","next","start"/);
  assert.equal(done.service.log, logs.join("\n"));

  const [row] = await rows("select * from cx_service where id = ?", [web.id]);
  assert.equal(row.status, "planned");
  assert.equal(JSON.parse(row.plan).port, 3000);
  assert.equal(row.log, logs.join("\n"));
  const detail = (await ada.get(`projects/${shop.id}`)).data.project;
  assert.equal(detail.services[0].plan.health_path, "/");
  assert.equal(detail.status, "queued", "one planned, one queued");
});

test("the API sibling gets the heuristic bridge, and the project becomes planned", async () => {
  const api = shop.services[1];
  const events = await ndjson((await ada.post(`projects/${shop.id}/services/${api.id}/analyze`)).res);
  const text = events.filter((e) => e.type === "log").map((e) => e.line.replace(STAMP, ""));
  assert.ok(text.includes("⟦shop-api⟧   plan: heuristic: python app (port 8000)"), text.join("\n"));
  assert.ok(text.includes("⟦shop-api⟧   api bridge: /api will be served on sibling domains"));
  assert.deepEqual(events.at(-1).service.plan.api_prefixes, ["/api"]);
  const p = (await ada.get(`projects/${shop.id}`)).data.project;
  assert.equal(p.status, "planned");
  assert.deepEqual(p.services.map((s) => s.status), ["planned", "planned"]);
});

test("the log really streams: the first lines arrive before the analysis ends", async () => {
  const [site] = (await gina.post("projects", { repos: ["gina/site"] })).data.project.services;
  const project = (await gina.get("projects")).data.projects[0];
  let release;
  world.gate = new Promise((r) => { release = r; });
  try {
    const res = (await gina.post(`projects/${project.id}/services/${site.id}/analyze`)).res;
    const reader = res.body.getReader();
    const { value } = await reader.read();
    const first = JSON.parse(new TextDecoder().decode(value).split("\n")[0]);
    assert.match(first.line, /⇣ reading gina\/site@main$/, "no prefix with one service");
    // Mid-run, the service is analyzing in Turso.
    assert.equal((await rows("select status from cx_service where id = ?", [site.id]))[0].status, "analyzing");
    assert.equal((await gina.get(`projects/${project.id}`)).data.project.status, "analyzing");
    // A second run of the same service now is refused.
    assert.equal((await gina.post(`projects/${project.id}/services/${site.id}/analyze`)).status, 409);
    release();
    let rest = "";
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      rest += new TextDecoder().decode(chunk.value);
    }
    const last = JSON.parse(rest.trim().split("\n").at(-1));
    assert.equal(last.type, "done");
    assert.equal(last.service.plan.notes, "heuristic: static site");
  } finally {
    world.gate = null;
  }
});

test("the AI plan: same prompt as the platform, files it asks for, bridge from its answer", async () => {
  process.env.OPENAI_API_KEY = "sk-test";
  try {
    assert.equal((await hana.get("me")).data.aiOn, true);
    const project = (await hana.post("projects", { repos: ["hana/api-server", "hana/front"] })).data.project;
    const [api, front] = project.services;
    world.openai.push('{"need_files": ["src/config.js", "nope.txt"]}');
    world.openai.push("Here you go:\n```json\n" + JSON.stringify({
      dockerfile: "FROM node:20-slim\nWORKDIR /app\nCOPY . .\nRUN npm install\nCMD [\"npm\",\"start\"]\n",
      port: 8080, health_path: "/api/health", build_args: null, api_prefixes: ["/api", "nope", 3],
      strip_prefix: false, notes: "express API reading PORT" }) + "\n```");
    const events = await ndjson((await hana.post(`projects/${project.id}/services/${api.id}/analyze`)).res);
    const text = events.filter((e) => e.type === "log").map((e) => e.line.replace(STAMP, ""));
    assert.ok(text.includes("⟦api-server⟧ 🧠 analyzing repository (AI)"), text.join("\n"));
    assert.ok(text.includes("⟦api-server⟧   🔎 AI reading src/config.js"));
    assert.ok(!text.some((l) => l.includes("AI reading nope.txt")));
    assert.ok(text.includes("⟦api-server⟧   plan: express API reading PORT (port 8080)"));
    assert.ok(text.includes("⟦api-server⟧   api bridge: /api will be served on sibling domains"));
    const service = events.at(-1).service;
    assert.equal(service.status, "planned");
    assert.equal(service.plan.port, 8080);
    assert.deepEqual(service.plan.api_prefixes, ["/api"]);

    const [first, second] = world.openaiRequests.splice(0);
    assert.equal(first.headers.get("authorization"), "Bearer sk-test");
    assert.equal(first.body.model, "gpt-5.1-codex-mini");
    assert.equal(first.body.max_output_tokens, 4000);
    assert.match(first.body.instructions, /^You are the deployment engine of a hosting platform/);
    assert.ok(first.body.input.startsWith("Build host: linux/amd64."));
    assert.ok(first.body.input.includes("Sibling services in this project:\n- front: public " +
      `https://${front.slug}.cicatrixa.com, internal http://${front.slug}:? (env keys FRONT_URL / FRONT_INTERNAL_URL)`));
    assert.ok(first.body.input.includes("Repo has Dockerfile: False"));
    assert.ok(first.body.input.includes("--- package.json ---"));
    assert.ok(second.body.input.includes("Requested files:\n--- src/config.js ---\nmodule.exports"));
    assert.ok(second.body.input.includes("--- nope.txt ---\n(not found)"));

    // OpenAI down: the heuristics still give a plan, and the log says so.
    world.openai.push(500);
    const fallback = await ndjson((await hana.post(`projects/${project.id}/services/${front.id}/analyze`)).res);
    const lines = fallback.filter((e) => e.type === "log").map((e) => e.line.replace(STAMP, ""));
    assert.ok(lines.includes("⟦front⟧   AI plan unavailable — using heuristics"), lines.join("\n"));
    assert.equal(fallback.at(-1).service.plan.notes, "heuristic: static site");
    // front now sees api-server's port in its sibling context.
    assert.ok(world.openaiRequests[0].body.input.includes(`internal http://${api.slug}:8080`));
    world.openaiRequests.length = 0;
  } finally {
    delete process.env.OPENAI_API_KEY;
  }
});

test("a repo that cannot be read fails the analysis, and says why", async () => {
  const project = (await ada.post("projects", { repos: ["ada/broken"], branch: "gone" })).data.project;
  const [svc] = project.services;
  assert.equal(svc.branch, "gone");
  const events = await ndjson((await ada.post(`projects/${project.id}/services/${svc.id}/analyze`)).res);
  const last = events.at(-1);
  assert.equal(last.type, "error");
  assert.match(last.error, /branch gone not found/);
  assert.match(events.at(-2).line, /✖ analysis failed: .*branch gone not found/);
  const [row] = await rows("select * from cx_service where id = ?", [svc.id]);
  assert.equal(row.status, "failed");
  assert.match(row.error, /branch gone not found/);
  assert.match(row.log, /⇣ reading ada\/broken@gone/);
  assert.equal(row.plan, null);
  assert.equal((await ada.get(`projects/${project.id}`)).data.project.status, "failed");
  await ada.post(`projects/${project.id}/delete`);
});

test("the 90 s rule: a running analysis is not started twice; a dead one may be re-run", async () => {
  const [web] = shop.services;
  const at = (s) => new Date(Date.now() - s * 1000).toISOString();
  await db.execute({ sql: "update cx_service set status = 'analyzing', updatedAt = ? where id = ?",
                     args: [at(10), web.id] });
  const before = (await rows("select count(*) as c from cx_analysis where userId = ?", [ada.id]))[0].c;
  const busy = await ada.post(`projects/${shop.id}/services/${web.id}/analyze`);
  assert.equal(busy.status, 409);
  assert.equal((await rows("select count(*) as c from cx_analysis where userId = ?", [ada.id]))[0].c, before,
               "a refused run does not count against the cap");
  assert.equal((await ada.get(`projects/${shop.id}`)).data.project.services[0].status, "analyzing");

  // The function was killed at 60 s: after 90 s nobody is running it.
  await db.execute({ sql: "update cx_service set updatedAt = ? where id = ?", args: [at(91), web.id] });
  const stale = (await ada.get(`projects/${shop.id}`)).data.project;
  assert.equal(stale.services[0].status, "failed");
  assert.match(stale.services[0].error, /stopped before it finished/);
  const events = await ndjson((await ada.post(`projects/${shop.id}/services/${web.id}/analyze`)).res);
  assert.equal(events.at(-1).type, "done");
  assert.equal(events.at(-1).service.status, "planned");
});

test("20 analyses a day at most", async () => {
  const project = (await gina.get("projects")).data.projects[0];
  const [site] = project.services;
  await db.execute({ sql: "delete from cx_analysis where userId = ?", args: [gina.id] });
  const add = (n, hoursAgo) => db.batch(Array.from({ length: n }, (_, i) => ({
    sql: "insert into cx_analysis (id, userId, at) values (?, ?, ?)",
    args: [`old-${hoursAgo}-${i}`, gina.id, new Date(Date.now() - hoursAgo * 3600_000).toISOString()] })), "write");
  await add(19, 2);
  await add(5, 25);                         // more than a day ago: these do not count
  assert.equal((await gina.get("me")).data.usage.analysesToday, 19);
  const ok = await gina.post(`projects/${project.id}/services/${site.id}/analyze`);
  assert.equal(ok.status, 200);
  await ok.res.text();
  const capped = await gina.post(`projects/${project.id}/services/${site.id}/analyze`);
  assert.equal(capped.status, 429);
  assert.match(capped.data.error, /20 analyses in 24 hours/);
  assert.equal((await rows("select status from cx_service where id = ?", [site.id]))[0].status, "planned",
               "a refused run leaves the service as it was");
});

// ---------- delete, disconnect, webhook ----------

test("deleting a project removes its services and frees its slugs", async () => {
  const project = (await gina.post("projects", { repos: ["gina/blog"] })).data.project;
  assert.equal(project.slug, "blog");
  assert.equal((await gina.post(`projects/${project.id}/delete`, {}, { origin: "https://evil.example" })).status, 403);
  const r = await gina.post(`projects/${project.id}/delete`);
  assert.deepEqual(r.data, { ok: true });
  assert.equal((await rows("select count(*) as c from cx_service where projectId = ?", [project.id]))[0].c, 0);
  assert.equal((await gina.get(`projects/${project.id}`)).status, 404);
  assert.equal((await gina.post(`projects/${project.id}/delete`)).status, 404);
  assert.equal((await gina.post("projects", { repos: ["gina/blog"] })).data.project.slug, "blog");
});

test("disconnecting forgets the installation; with revoke it is also removed on GitHub", async () => {
  const plain = await fred.post("github/disconnect", {});
  assert.deepEqual(plain.data, { ok: true });
  assert.equal((await fred.get("me")).data.github, null);
  assert.deepEqual(world.deleted, []);
  assert.equal((await fred.get("repos")).status, 409);

  assert.deepEqual((await erin.post("github/disconnect", { revoke: true })).data, { ok: true });
  assert.deepEqual(world.deleted, [401]);
  assert.equal((await erin.get("me")).data.github, null);
  // Projects stay: they are imported with their slugs and planned again on the box.
  assert.equal((await fred.get("projects")).data.projects.length, 4);
});

test("GitHub's installation.deleted webhook removes the binding; bad signatures are refused", async () => {
  const hook = (body, { event = "installation", secret = "hook-secret", sig } = {}) =>
    client().raw("POST", "/api/webhooks/github", body, { origin: null, headers: {
      "x-github-event": event,
      "x-hub-signature-256": sig ?? `sha256=${createHmac("sha256", secret).update(body).digest("hex")}` } });
  const deleted = JSON.stringify({ action: "deleted", installation: { id: 300, account: { login: "acme" } } });
  assert.equal((await hook(deleted, { secret: "wrong" })).status, 401);
  assert.equal((await hook(deleted, { sig: "" })).status, 401);
  assert.equal((await hook(deleted, { sig: "sha256=00" })).status, 401);
  assert.ok((await carl.get("me")).data.github, "still bound after refused hooks");

  const push = JSON.stringify({ ref: "refs/heads/main", repository: { full_name: "acme/site" } });
  assert.deepEqual((await hook(push, { event: "push" })).data, { ok: true });
  const created = JSON.stringify({ action: "created", installation: { id: 300 } });
  assert.deepEqual((await hook(created)).data, { ok: true });
  assert.ok((await carl.get("me")).data.github);

  const r = await hook(deleted);
  assert.equal(r.status, 200);
  assert.deepEqual(r.data, { ok: true, removed: 1 });
  assert.equal((await carl.get("me")).data.github, null);
  // Now dana, acme's other admin, can connect it.
  const again = await callback(dana, { code: "dana-code", installation_id: "300", state: ghState(dana) });
  assert.equal(again.location, "/projects/new?github=connected");
});

// ---------- routing ----------

test("vercel.json sends every workspace URL to the function, and keeps what was there", async () => {
  const cfg = JSON.parse(readFileSync(new URL("../../vercel.json", import.meta.url), "utf8"));
  assert.equal(cfg.outputDirectory, "site");
  assert.equal(cfg.cleanUrls, true);
  assert.deepEqual(cfg.functions, { "api/auth.js": { maxDuration: 15 },
                                    "api/workspace.js": { maxDuration: 60 } });
  assert.deepEqual(cfg.redirects[0], { source: "/", has: [{ type: "host", value: "app.cicatrixa.com" }],
                                       destination: "/dashboard", permanent: false });
  const rewrites = Object.fromEntries(cfg.rewrites.map((r) => [r.source, r.destination]));
  assert.equal(rewrites["/api/auth/:path*"], "/api/auth?__auth_path=:path*");
  assert.equal(rewrites["/api/workspace/:path*"], "/api/workspace?__path=:path*");
  for (const p of ["connect/github/start", "connect/github/authorize", "connect/github/callback",
                   "connect/github/setup", "admin/github/callback"]) {
    assert.equal(rewrites[`/${p}`], `/api/workspace?__path=${p}`, p);
  }
  assert.equal(rewrites["/api/webhooks/github"], "/api/workspace?__path=webhooks/github");
  // App pages on the apex and www go to app., where the session cookie lives.
  const away = cfg.redirects.filter((r) => r.destination.startsWith("https://app.cicatrixa.com/"));
  for (const r of away) {
    assert.equal(r.has.length, 1);
    assert.ok(["cicatrixa.com", "www.cicatrixa.com"].includes(r.has[0].value), r.has[0].value);
    assert.equal(r.permanent, false);
  }
  const sources = away.map((r) => r.source);
  for (const page of ["dashboard", "login", "signup", "forgot-password", "reset-password", "project", "admin"]) {
    assert.equal(sources.filter((s) => new RegExp(`[(|]${page}[|)]`).test(s)).length, 2, page);
  }
  assert.equal(sources.filter((s) => s === "/projects/:path*").length, 2);
  assert.equal(sources.filter((s) => s === "/connect/:path*").length, 2);
  assert.ok(!sources.some((s) => /demo/.test(s) || s === "/"));
});

test("the rewritten URL and the original URL reach the same route", async () => {
  const { originalRequest } = ws;
  const cases = [
    ["https://app.cicatrixa.com/api/workspace?__path=projects/abc/delete", "/api/workspace/projects/abc/delete"],
    ["https://app.cicatrixa.com/api/workspace?__path=connect/github/callback&code=1&state=s",
     "/connect/github/callback"],
    ["https://app.cicatrixa.com/api/workspace?__path=admin/github/callback&code=1", "/admin/github/callback"],
    ["https://app.cicatrixa.com/api/workspace?__path=webhooks/github", "/api/webhooks/github"],
    ["https://app.cicatrixa.com/connect/github/start?__path=connect/github/start", "/connect/github/start"],
  ];
  for (const [given, path] of cases) {
    const url = new URL(originalRequest(new Request(given)).url);
    assert.equal(url.pathname, path);
    assert.equal(url.searchParams.has("__path"), false);
  }
  const cb = new URL(originalRequest(new Request(cases[1][0])).url);
  assert.equal(cb.searchParams.get("code"), "1");
  // The original URL itself (no rewrite) works too.
  const direct = await ada.raw("GET", "/api/workspace/me");
  assert.equal(direct.status, 200);
  const res = await ws.GET(new Request(`${BASE}/api/workspace/me`, {
    headers: { cookie: [...ada.jar].map(([k, v]) => `${k}=${v}`).join("; ") } }));
  assert.equal((await res.json()).user.email, "ada@example.com");
});

test("no request went anywhere but the fakes", () => {
  assert.deepEqual(unexpected, []);
});
