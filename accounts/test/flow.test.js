// End-to-end account flows through api/auth.js, called the way Vercel calls it after
// vercel.json's rewrite, against a real libSQL database file. Resend is replaced by a
// recorder; Better Auth's origin and rate-limit checks stay on.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const BASE = "http://localhost:3000";
const dir = mkdtempSync(join(tmpdir(), "cx-accounts-"));
Object.assign(process.env, {
  TURSO_DATABASE_URL: `file:${join(dir, "accounts.db")}`,
  BETTER_AUTH_SECRET: "test-secret-that-is-long-enough-0123456789",
  BETTER_AUTH_URL: BASE,
  RESEND_API_KEY: "re_test",
  MAIL_FROM: "Cicatrixa <noreply@cicatrixa.com>",
});

const outbox = [];
let resendStatus = 200;
const realFetch = globalThis.fetch;
globalThis.fetch = async (url, init) => {
  if (String(url) === "https://api.resend.com/emails") {
    const mail = JSON.parse(init.body);
    if (resendStatus !== 200) {
      return new Response('{"message":"The cicatrixa.com domain is not verified"}',
                          { status: resendStatus });
    }
    outbox.push(mail);
    return new Response('{"id":"x"}', { status: 200 });
  }
  return realFetch(url, init);
};

const { GET, POST } = await import("../../api/auth.js");

let ipCounter = 0;
// Each test acts from its own address, so the per-IP rate limits only bite where a
// test means them to.
function client(ip = `10.0.0.${++ipCounter}`) {
  const jar = new Map();
  async function call(method, path, body, { origin = BASE, headers = {} } = {}) {
    // What Vercel hands the function: /api/auth?__auth_path=<path> plus the query.
    const [p, query] = path.split("?");
    const url = new URL(`${BASE}/api/auth`);
    url.searchParams.set("__auth_path", p.replace(/^\/api\/auth\//, ""));
    for (const [k, v] of new URLSearchParams(query || "")) url.searchParams.set(k, v);
    const h = new Headers({ "x-forwarded-for": ip, ...headers });
    if (origin) h.set("origin", origin);
    if (body !== undefined) h.set("content-type", "application/json");
    if (jar.size) h.set("cookie", [...jar].map(([k, v]) => `${k}=${v}`).join("; "));
    const res = await (method === "GET" ? GET : POST)(new Request(url, {
      method, headers: h, body: body === undefined ? undefined : JSON.stringify(body),
    }));
    for (const c of res.headers.getSetCookie()) {
      const [pair] = c.split(";");
      const [k, v] = pair.split("=");
      if (v === "" || /max-age=0/i.test(c)) jar.delete(k); else jar.set(k, v);
    }
    let data = null;
    try { data = await res.clone().json(); } catch { /* redirect or empty */ }
    return { status: res.status, data, location: res.headers.get("location"), res };
  }
  return {
    jar,
    get: (path, opts) => call("GET", path, undefined, opts),
    post: (path, body = {}, opts) => call("POST", path, body, opts),
  };
}

const linkIn = (mail) => mail.text.match(/https?:\/\/\S+/)[0];
const lastMailTo = (to) => [...outbox].reverse().find((m) => m.to[0] === to);
const signUp = (c, email, password = "correct horse battery") =>
  c.post("/sign-up/email", { email, password, name: email.split("@")[0],
                             callbackURL: "/dashboard?welcome=1" });

async function verifiedUser(email, password = "correct horse battery") {
  const c = client();
  await signUp(c, email, password);
  const r = await c.get(linkIn(lastMailTo(email)).replace(BASE, ""));
  assert.equal(r.status, 302);
  return c;
}

test("sign-up emails a confirmation link and gives no session until it is opened", async () => {
  const c = client();
  const r = await signUp(c, "ada@example.com");
  assert.equal(r.status, 200);
  assert.equal(r.data.token, null);
  const mail = lastMailTo("ada@example.com");
  assert.equal(mail.subject, "Confirm your email for Cicatrixa");
  assert.equal(mail.from, "Cicatrixa <noreply@cicatrixa.com>");
  assert.match(linkIn(mail), /^http:\/\/localhost:3000\/api\/auth\/verify-email\?token=/);
  assert.equal((await c.get("/get-session")).data, null);
});

test("opening the link confirms the email, signs in, and lands on the dashboard", async () => {
  const c = client();
  await signUp(c, "bob@example.com");
  const r = await c.get(linkIn(lastMailTo("bob@example.com")).replace(BASE, ""));
  assert.equal(r.status, 302);
  assert.equal(r.location, "/dashboard?welcome=1");
  const s = await c.get("/get-session");
  assert.equal(s.data.user.email, "bob@example.com");
  assert.equal(s.data.user.emailVerified, true);
});

test("logging in before confirming is refused and sends a fresh link", async () => {
  const c = client();
  await signUp(c, "cy@example.com");
  const before = outbox.length;
  const r = await c.post("/sign-in/email", { email: "cy@example.com",
                                             password: "correct horse battery" });
  assert.equal(r.status, 403);
  assert.equal(r.data.code, "EMAIL_NOT_VERIFIED");
  assert.equal(outbox.length, before + 1);
});

test("a confirmed user logs in; a wrong password does not", async () => {
  await verifiedUser("dee@example.com");
  const c = client();
  assert.equal((await c.post("/sign-in/email", { email: "dee@example.com",
                                                 password: "not it" })).status, 401);
  const ok = await c.post("/sign-in/email", { email: "dee@example.com",
                                              password: "correct horse battery" });
  assert.equal(ok.status, 200);
  assert.equal((await c.get("/get-session")).data.user.email, "dee@example.com");
});

test("forgot password: emailed link, new password works, old one and old sessions do not",
  async () => {
    const elsewhere = await verifiedUser("eve@example.com");
    const c = client();
    const r = await c.post("/request-password-reset", {
      email: "eve@example.com", redirectTo: `${BASE}/reset-password` });
    assert.equal(r.status, 200);
    const mail = lastMailTo("eve@example.com");
    assert.equal(mail.subject, "Reset your Cicatrixa password");

    const landing = await c.get(linkIn(mail).replace(BASE, ""));
    assert.equal(landing.status, 302);
    const target = new URL(landing.location);
    assert.equal(target.pathname, "/reset-password");
    const token = target.searchParams.get("token");
    assert.ok(token);

    assert.equal((await c.post("/reset-password", { newPassword: "a brand new one",
                                                    token })).status, 200);
    assert.equal((await c.post("/reset-password", { newPassword: "and again please",
                                                    token })).data.code, "INVALID_TOKEN");
    assert.equal((await c.post("/sign-in/email", { email: "eve@example.com",
                                                   password: "correct horse battery" })).status, 401);
    assert.equal((await c.post("/sign-in/email", { email: "eve@example.com",
                                                   password: "a brand new one" })).status, 200);
    assert.equal((await elsewhere.get("/get-session")).data, null);
  });

test("forgot password answers the same for an unknown email, and sends nothing", async () => {
  const before = outbox.length;
  const r = await client().post("/request-password-reset", {
    email: "nobody@example.com", redirectTo: `${BASE}/reset-password` });
  assert.equal(r.status, 200);
  assert.equal(outbox.length, before);
});

test("a reset link may only send people back to this site", async () => {
  await verifiedUser("fay@example.com");
  const r = await client().post("/request-password-reset", {
    email: "fay@example.com", redirectTo: "https://evil.example/steal" });
  assert.equal(r.status, 403);
});

test("signing up again with a taken address gives nothing away", async () => {
  await verifiedUser("gus@example.com");
  const r = await signUp(client(), "gus@example.com", "someone else entirely");
  assert.equal(r.status, 200);
  assert.equal((await client().post("/sign-in/email", {
    email: "gus@example.com", password: "someone else entirely" })).status, 401);
});

test("a form posted from another site is refused", async () => {
  const r = await client().post("/sign-up/email", {
    email: "hal@example.com", password: "correct horse battery", name: "hal" },
    { origin: "https://evil.example" });
  assert.equal(r.status, 403);
});

test("guessing passwords is cut off after 5 tries a minute", async () => {
  await verifiedUser("ivy@example.com");
  const c = client();
  const codes = [];
  for (let i = 0; i < 7; i++) {
    codes.push((await c.post("/sign-in/email", { email: "ivy@example.com",
                                                 password: `guess ${i}` })).status);
  }
  assert.deepEqual(codes.slice(0, 5), [401, 401, 401, 401, 401]);
  assert.equal(codes[6], 429);
});

test("a short password is refused", async () => {
  const r = await signUp(client(), "jo@example.com", "short");
  assert.equal(r.data.code, "PASSWORD_TOO_SHORT");
});

test("when Resend refuses, the reason is logged and sign-up still answers", async () => {
  const errors = [];
  const original = console.error;
  console.error = (...a) => errors.push(a.join(" "));
  resendStatus = 403;
  try {
    const r = await signUp(client(), "kim@example.com");
    assert.equal(r.status, 200);
  } finally {
    resendStatus = 200;
    console.error = original;
  }
  assert.ok(errors.some((e) => e.includes("not verified")));
});

test("Vercel's rewritten URL and the original URL both reach the right endpoint", async () => {
  const { originalRequest } = await import("../../api/auth.js");
  const rewritten = originalRequest(new Request(
    "https://app.cicatrixa.com/api/auth?__auth_path=reset-password/abc&callbackURL=%2Fx"));
  assert.equal(new URL(rewritten.url).pathname, "/api/auth/reset-password/abc");
  assert.equal(new URL(rewritten.url).searchParams.get("callbackURL"), "/x");
  assert.equal(new URL(rewritten.url).searchParams.has("__auth_path"), false);
  const original = originalRequest(new Request(
    "https://app.cicatrixa.com/api/auth/get-session?__auth_path=get-session"));
  assert.equal(new URL(original.url).pathname, "/api/auth/get-session");
});
