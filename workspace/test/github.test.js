// workspace/github.js against a fake GitHub (globalThis.fetch replaced), with a real
// RSA key for the App so the JWT is checked the way GitHub checks it.
import assert from "node:assert/strict";
import { createHmac, generateKeyPairSync, verify } from "node:crypto";
import { beforeEach, test } from "node:test";

import * as gh from "../github.js";

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const PEM = privateKey.export({ type: "pkcs1", format: "pem" });
const SETTINGS = { gh_app_id: "4242", gh_app_slug: "cicatrixa-test", gh_app_pem: PEM,
                   gh_app_client_id: "Iv1.client", gh_app_client_secret: "client-secret",
                   gh_app_webhook_secret: "hook-secret" };

// ---------- a fake GitHub ----------

let calls;
let routes;
beforeEach(() => { calls = []; routes = []; });

function on(method, pattern, reply) {
  routes.push({ method, pattern, reply });
}

const jsonRes = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json" } });

globalThis.fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  const method = init.method || "GET";
  const headers = new Headers(init.headers);
  const call = { method, url, headers, body: init.body };
  calls.push(call);
  for (const r of routes) {
    const target = `${url.origin}${url.pathname}`;
    const m = typeof r.pattern === "string" ? (target === r.pattern ? [target] : null)
      : target.match(r.pattern);
    if (r.method === method && m) return r.reply(call, m);
  }
  return jsonRes({ message: "Not Found" }, 404);
};

function decodeJWT(token) {
  const [h, p, s] = token.split(".");
  const ok = verify("RSA-SHA256", Buffer.from(`${h}.${p}`), publicKey, Buffer.from(s, "base64url"));
  return { ok, header: JSON.parse(Buffer.from(h, "base64url")),
           payload: JSON.parse(Buffer.from(p, "base64url")) };
}

const bearer = (call) => (call.headers.get("authorization") || "").replace(/^Bearer /, "");

// ---------- the App ----------

test("the manifest is the platform's, field for field", () => {
  const m = gh.buildManifest("https://app.cicatrixa.com/", "Cicatrixa");
  assert.equal(JSON.stringify(m), JSON.stringify({
    name: "Cicatrixa",
    url: "https://app.cicatrixa.com",
    hook_attributes: { url: "https://app.cicatrixa.com/api/webhooks/github", active: true },
    redirect_url: "https://app.cicatrixa.com/admin/github/callback",
    callback_urls: ["https://app.cicatrixa.com/connect/github/callback"],
    request_oauth_on_install: true,
    public: true,
    default_permissions: { contents: "write", metadata: "read", pull_requests: "write",
                           members: "read" },
    default_events: ["push", "pull_request"],
  }));
});

test("the manifest form goes to the account's or the organisation's new-App page", () => {
  assert.equal(gh.manifestFormURL("gh app:1.2.x"),
               "https://github.com/settings/apps/new?state=gh%20app%3A1.2.x");
  assert.equal(gh.manifestFormURL("s", "acme-inc"),
               "https://github.com/organizations/acme-inc/settings/apps/new?state=s");
});

test("the App JWT is RS256, verifies with the App's public key, and lives under 10 minutes", () => {
  const now = Date.UTC(2026, 9, 21, 12, 0, 0);
  const { ok, header, payload } = decodeJWT(gh.appJWT(SETTINGS, now));
  assert.ok(ok, "signature verifies");
  assert.deepEqual(header, { alg: "RS256", typ: "JWT" });
  assert.equal(payload.iss, "4242");
  assert.equal(payload.iat, now / 1000 - 60);
  assert.equal(payload.exp, now / 1000 + 540);
  assert.ok(payload.exp - payload.iat <= 600);
  assert.throws(() => gh.appJWT({ gh_app_id: "1" }), /not configured/);
});

test("an installation token is minted with the JWT, and sent with GitHub's headers", async () => {
  on("POST", "https://api.github.com/app/installations/77/access_tokens", (call) => {
    assert.ok(decodeJWT(bearer(call)).ok);
    return jsonRes({ token: "ghs_inst77", expires_at: "2026-10-21T13:00:00Z" }, 201);
  });
  assert.equal(await gh.installationToken(SETTINGS, 77), "ghs_inst77");
  const h = calls[0].headers;
  assert.equal(h.get("accept"), "application/vnd.github+json");
  assert.equal(h.get("x-github-api-version"), "2022-11-28");
  assert.equal(h.get("user-agent"), "cicatrixa");
});

test("the manifest code becomes the App's settings, without any credential sent", async () => {
  on("POST", "https://api.github.com/app-manifests/abc123/conversions", () => jsonRes({
    id: 99, slug: "cicatrixa-x", name: "Cicatrixa X", pem: PEM, webhook_secret: "wh",
    client_id: "Iv1.x", client_secret: "cs" }, 201));
  assert.deepEqual(await gh.exchangeManifestCode("abc123"), {
    gh_app_id: "99", gh_app_slug: "cicatrixa-x", gh_app_name: "Cicatrixa X", gh_app_pem: PEM,
    gh_app_webhook_secret: "wh", gh_app_client_id: "Iv1.x", gh_app_client_secret: "cs" });
  assert.equal(calls[0].headers.get("authorization"), null);
  await assert.rejects(gh.exchangeManifestCode("used"), /refused the App code/);
  await assert.rejects(gh.exchangeManifestCode("../x"), /bad manifest code/);
});

test("installationAccount and revokeInstallation use the App JWT; revoking is best-effort",
  async () => {
    on("GET", "https://api.github.com/app/installations/5", (call) => {
      assert.ok(decodeJWT(bearer(call)).ok);
      return jsonRes({ id: 5, account: { login: "acme", type: "Organization" } });
    });
    on("DELETE", "https://api.github.com/app/installations/5", (call) => {
      assert.ok(decodeJWT(bearer(call)).ok);
      return new Response(null, { status: 204 });
    });
    assert.deepEqual(await gh.installationAccount(SETTINGS, 5), { login: "acme", type: "Organization" });
    assert.equal(await gh.revokeInstallation(SETTINGS, 5), true);
    assert.equal(await gh.revokeInstallation(SETTINGS, 6), false);
  });

// ---------- repositories ----------

test("repositories are listed page by page, in the platform's shape", async () => {
  on("POST", /\/app\/installations\/1\/access_tokens$/, () => jsonRes({ token: "ghs_1" }));
  const all = Array.from({ length: 130 }, (_, i) => ({
    full_name: `ada/r${i}`, private: i % 2 === 0, default_branch: i === 3 ? "trunk" : "main",
    pushed_at: "2026-10-01T00:00:00Z", owner: { login: "ada" } }));
  on("GET", "https://api.github.com/installation/repositories", (call) => {
    assert.equal(bearer(call), "ghs_1");
    assert.equal(call.url.searchParams.get("per_page"), "100");
    const page = Number(call.url.searchParams.get("page"));
    return jsonRes({ total_count: 130, repositories: all.slice((page - 1) * 100, page * 100) });
  });
  const repos = await gh.listRepos(SETTINGS, 1);
  assert.equal(repos.length, 130);
  assert.deepEqual(repos[3], { full_name: "ada/r3", private: false, default_branch: "trunk",
                               pushed_at: "2026-10-01T00:00:00Z" });
  assert.equal(calls.filter((c) => c.url.pathname === "/installation/repositories").length, 2);
});

test("a repo the token cannot see is 'not found or no access'", async () => {
  on("GET", "https://api.github.com/repos/ada/app", () => jsonRes({
    full_name: "ada/app", default_branch: "dev", private: true }));
  assert.deepEqual(await gh.repoInfo("ghs_1", "ada/app"),
                   { full_name: "ada/app", default_branch: "dev", private: true });
  await assert.rejects(gh.repoInfo("ghs_1", "eve/secret"), /not found or no access/);
  await assert.rejects(gh.repoInfo("ghs_1", "../../user"), /not a repository name/);
});

test("nothing is ever sent without a credential", async () => {
  await assert.rejects(gh.repoInfo("", "ada/app"), /no GitHub credential/);
  await assert.rejects(gh.readFile(undefined, "ada/app", "package.json", "main"), /no GitHub credential/);
  assert.equal(calls.length, 0);
});

test("branch head, tree and files are read through the API", async () => {
  on("GET", "https://api.github.com/repos/ada/app/commits/feature%2Fx", () => jsonRes({ sha: "f".repeat(40) }));
  on("GET", `https://api.github.com/repos/ada/app/git/trees/${"f".repeat(40)}`, (call) => {
    assert.equal(call.url.searchParams.get("recursive"), "1");
    return jsonRes({ truncated: false, tree: [
      { path: "src", type: "tree" }, { path: "src/index.js", type: "blob" },
      { path: "package.json", type: "blob" }, { path: "vendor/lib", type: "commit" }] });
  });
  on("GET", "https://api.github.com/repos/ada/app/contents/src/index.js", (call) => {
    assert.equal(call.headers.get("accept"), "application/vnd.github.raw+json");
    assert.equal(call.url.searchParams.get("ref"), "f".repeat(40));
    return new Response("console.log('hi')\n");
  });
  assert.equal(await gh.branchHead("ghs_1", "ada/app", "feature/x"), "f".repeat(40));
  await assert.rejects(gh.branchHead("ghs_1", "ada/app", "nope"), /branch nope not found/);
  assert.deepEqual(await gh.getTree("ghs_1", "ada/app", "f".repeat(40)),
                   { paths: ["src/index.js", "package.json"], truncated: false });
  assert.equal(await gh.readFile("ghs_1", "ada/app", "src/index.js", "f".repeat(40)),
               "console.log('hi')\n");
  assert.equal(await gh.readFile("ghs_1", "ada/app", "missing.txt", "main"), null);
});

test("a file path from the model cannot climb out of the repo", async () => {
  for (const p of ["../../../user", "src/../../x", "/etc/passwd", "a//b", ".", "a\\..\\b", ""]) {
    assert.equal(await gh.readFile("ghs_1", "ada/app", p, "main"), null, p);
  }
  assert.equal(calls.length, 0);
});

// ---------- proving whose installation it is ----------

function fakeOAuth({ me, installations, memberships = {}, code = "good-code" }) {
  on("POST", "https://github.com/login/oauth/access_token", (call) => {
    const form = new URLSearchParams(call.body);
    assert.equal(form.get("client_id"), "Iv1.client");
    assert.equal(form.get("client_secret"), "client-secret");
    assert.equal(call.headers.get("accept"), "application/json");
    return form.get("code") === code ? jsonRes({ access_token: "ghu_person" })
      : jsonRes({ error: "bad_verification_code" });
  });
  on("GET", "https://api.github.com/user", (call) => {
    assert.equal(bearer(call), "ghu_person");
    return jsonRes(me);
  });
  on("GET", "https://api.github.com/user/installations", (call) => {
    const page = Number(call.url.searchParams.get("page"));
    return jsonRes({ installations: installations.slice((page - 1) * 100, page * 100) });
  });
  on("GET", /^https:\/\/api\.github\.com\/user\/memberships\/orgs\/(.+)$/, (call, m) => {
    const answer = memberships[m[1]];
    if (answer === "error") return jsonRes({ message: "boom" }, 500);
    return answer ? jsonRes(answer) : jsonRes({ message: "Not Found" }, 404);
  });
}

const inst = (id, login, type, accountId) => ({ id, account: { login, type, id: accountId },
                                                created_at: `2026-10-0${id % 9 + 1}T00:00:00Z` });

test("a personal installation counts only when it is the person's own account", async () => {
  fakeOAuth({ me: { id: 1, login: "ada" },
              installations: [inst(11, "ada", "User", 1), inst(12, "bob", "User", 2)] });
  const result = await gh.installationsUserControls(SETTINGS, "good-code");
  assert.equal(result.me.login, "ada");
  assert.deepEqual([...result.controlled.keys()], [11]);
});

test("an organisation counts only for an active admin; a member, a pending admin or an " +
     "unanswered check does not", async () => {
  fakeOAuth({
    me: { id: 1, login: "ada" },
    installations: [inst(21, "acme", "Organization", 50), inst(22, "guild", "Organization", 51),
                    inst(23, "pending", "Organization", 52), inst(24, "flaky", "Organization", 53),
                    inst(25, "hidden", "Organization", 54)],
    memberships: { acme: { role: "admin", state: "active" }, guild: { role: "member", state: "active" },
                   pending: { role: "admin", state: "pending" }, flaky: "error" },
  });
  const result = await gh.installationsUserControls(SETTINGS, "good-code");
  assert.deepEqual([...result.controlled.keys()], [21]);
});

test("installations are read past the first page", async () => {
  const many = Array.from({ length: 101 }, (_, i) => inst(1000 + i, "bob", "User", 2));
  many.push(inst(5000, "ada", "User", 1));
  fakeOAuth({ me: { id: 1, login: "ada" }, installations: many });
  const result = await gh.installationsUserControls(SETTINGS, "good-code");
  assert.deepEqual([...result.controlled.keys()], [5000]);
});

test("a code GitHub does not accept, or no client secret, proves nothing", async () => {
  fakeOAuth({ me: { id: 1, login: "ada" }, installations: [inst(11, "ada", "User", 1)] });
  assert.equal(await gh.installationsUserControls(SETTINGS, "stale-code"), null);
  assert.equal(await gh.installationsUserControls(SETTINGS, ""), null);
  assert.equal(await gh.installationsUserControls({ ...SETTINGS, gh_app_client_secret: "" },
                                                  "good-code"), null);
});

test("the authorize URL comes back to the platform's callback", () => {
  const url = new URL(gh.authorizeURL(SETTINGS, "st.1.x", "https://app.cicatrixa.com"));
  assert.equal(url.origin + url.pathname, "https://github.com/login/oauth/authorize");
  assert.equal(url.searchParams.get("client_id"), "Iv1.client");
  assert.equal(url.searchParams.get("state"), "st.1.x");
  assert.equal(url.searchParams.get("redirect_uri"), "https://app.cicatrixa.com/connect/github/callback");
  assert.equal(gh.authorizeURL({}, "s", "https://app.cicatrixa.com"), null);
});

// ---------- webhooks ----------

test("a webhook is trusted only with a valid signature from a configured secret", () => {
  const body = Buffer.from('{"action":"deleted"}');
  const sig = `sha256=${createHmac("sha256", "hook-secret").update(body).digest("hex")}`;
  assert.equal(gh.verifyWebhook("hook-secret", body, sig), true);
  assert.equal(gh.verifyWebhook("hook-secret", body.toString(), sig), true);
  assert.equal(gh.verifyWebhook("hook-secret", Buffer.from('{"action":"created"}'), sig), false);
  assert.equal(gh.verifyWebhook("other", body, sig), false);
  assert.equal(gh.verifyWebhook("hook-secret", body, sig.slice(0, -2)), false);
  assert.equal(gh.verifyWebhook("hook-secret", body, sig.replace("sha256=", "sha1=")), false);
  assert.equal(gh.verifyWebhook("hook-secret", body, null), false);
  assert.equal(gh.verifyWebhook("", body, sig), false, "no secret, no trust");
});
