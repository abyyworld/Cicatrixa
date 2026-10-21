// End-to-end account flows through api/auth.js, called the way Vercel calls it after
// vercel.json's rewrite, against a real libSQL database file. Resend is replaced by a
// recorder; Better Auth's origin and rate-limit checks stay on.
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { createClient } from "@libsql/client";

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
const mailsTo = (to) => outbox.filter((m) => m.to[0] === to);
// What the sign-up page sends (site/accounts.js).
const signUp = (c, email, password = "correct horse battery") =>
  c.post("/sign-up/email", { email, password, name: email.split("@")[0],
                             callbackURL: "/login?verified=1" });
const signIn = (c, email, password = "correct horse battery") =>
  c.post("/sign-in/email", { email, password });
// The emailed reset (or finish-signing-up) link: its landing hands over the token.
async function tokenFrom(c, mail) {
  const landing = await c.get(linkIn(mail).replace(BASE, ""));
  assert.equal(landing.status, 302);
  const target = new URL(landing.location);
  assert.equal(target.pathname, "/reset-password");
  return target.searchParams.get("token");
}

// The forgot-password flow end to end: request, emailed link, new password.
async function resetPassword(c, email, newPassword) {
  assert.equal((await c.post("/request-password-reset", {
    email, redirectTo: `${BASE}/reset-password` })).status, 200);
  const landing = await c.get(linkIn(lastMailTo(email)).replace(BASE, ""));
  const token = new URL(landing.location).searchParams.get("token");
  assert.equal((await c.post("/reset-password", { newPassword, token })).status, 200);
}

// Signed up, confirmed, and logged in with the password: the link alone opens no session.
async function verifiedUser(email, password = "correct horse battery") {
  const c = client();
  await signUp(c, email, password);
  const r = await c.get(linkIn(lastMailTo(email)).replace(BASE, ""));
  assert.equal(r.status, 302);
  assert.equal((await signIn(c, email, password)).status, 200);
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

// The link proves who reads the inbox, not who chose the password (accounts/auth.js).
test("opening the link confirms the email but signs nobody in; the person logs in", async () => {
  const c = client();
  await signUp(c, "bob@example.com");
  const r = await c.get(linkIn(lastMailTo("bob@example.com")).replace(BASE, ""));
  assert.equal(r.status, 302);
  assert.equal(r.location, "/login?verified=1");
  assert.equal(c.jar.size, 0, "no session cookie");
  assert.equal((await c.get("/get-session")).data, null);
  assert.equal((await signIn(c, "bob@example.com")).status, 200);
  const s = await c.get("/get-session");
  assert.equal(s.data.user.email, "bob@example.com");
  assert.equal(s.data.user.emailVerified, true);
});

test("a confirmation link that failed lands on the log-in page with Better Auth's code",
  async () => {
    const r = await client().get("/verify-email?token=not-a-token&callbackURL=%2Flogin%3Fverified%3D1");
    assert.equal(r.status, 302);
    assert.equal(r.location, "/login?verified=1&error=INVALID_TOKEN");
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

// workspace/routes.js makes an ADMIN_EMAILS address an admin only with this proof.
test("a password set through a reset link is recorded as the proof an admin needs", async () => {
  const db = createClient({ url: process.env.TURSO_DATABASE_URL });
  const proof = async (id) => (await db.execute({
    sql: "select value from cx_setting where key = ?", args: [`pw_proof:${id}`] })).rows[0]?.[0];
  const c = await verifiedUser("pat@example.com");
  const id = (await c.get("/get-session")).data.user.id;
  assert.equal(await proof(id), undefined, "a confirmation link is no proof");
  await resetPassword(c, "pat@example.com", "chosen from the link");
  assert.match(await proof(id), /^\d{4}-\d\d-\d\dT/);
});

test("a proof that cannot be written still resets the password and ends old sessions",
  async () => {
    const db = createClient({ url: process.env.TURSO_DATABASE_URL });
    const elsewhere = await verifiedUser("quin@example.com");
    await db.execute("create table if not exists cx_setting (key text primary key, " +
                     "value text not null, updatedAt text not null)");
    await db.execute("create trigger no_proofs before insert on cx_setting " +
                     "begin select raise(abort, 'cx_setting is read-only'); end");
    const errors = [];
    const original = console.error;
    console.error = (...a) => errors.push(a.join(" "));
    try {
      await resetPassword(client(), "quin@example.com", "a new one entirely");
    } finally {
      console.error = original;
      await db.execute("drop trigger no_proofs");
    }
    assert.ok(errors.some((e) => e.includes("could not record a password reset")));
    assert.equal((await elsewhere.get("/get-session")).data, null);
    assert.equal((await client().post("/sign-in/email", { email: "quin@example.com",
                                                          password: "a new one entirely" })).status, 200);
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

test("signing up again with a confirmed address gives nothing away and sends nothing",
  async () => {
    await verifiedUser("gus@example.com");
    const before = outbox.length;
    const r = await signUp(client(), "gus@example.com", "someone else entirely");
    assert.equal(r.status, 200);
    assert.equal(r.data.token, null);
    assert.equal(outbox.length, before);
    assert.equal((await client().post("/sign-in/email", {
      email: "gus@example.com", password: "someone else entirely" })).status, 401);
  });

// ---------- an address someone else signed up with first ----------

// The squat: someone signs up with another person's address and their own password. A
// second sign-up for that unconfirmed address mails the inbox a link to set the password.
test("a second sign-up for an unconfirmed address mails it a link to set the password",
  async () => {
    const squatter = client();
    await signUp(squatter, "lee@example.com", "the squatter's password");
    const owner = client();
    const r = await signUp(owner, "lee@example.com", "the owner's own password");
    assert.equal(r.status, 200);
    assert.equal(r.data.token, null);
    const mail = lastMailTo("lee@example.com");
    assert.equal(mail.subject, "Finish creating your Cicatrixa account");
    assert.match(mail.text, /^Finish creating your Cicatrixa account: set your password/);
    assert.match(mail.html, /Set my password/);
    assert.match(linkIn(mail), /^http:\/\/localhost:3000\/api\/auth\/reset-password\/\w+\?callbackURL=/);
    assert.equal(owner.jar.size, 0, "no session for either");
    assert.equal(squatter.jar.size, 0);
  });

test("a sign-up answers alike for a new, an unconfirmed and a confirmed address", async () => {
  await verifiedUser("max@example.com");
  await signUp(client(), "ned@example.com");
  const shape = (r) => ({ status: r.status, token: r.data.token,
                          keys: Object.keys(r.data.user).sort(),
                          emailVerified: r.data.user.emailVerified, email: r.data.user.email });
  const fresh = shape(await signUp(client(), "new@example.com"));
  const unconfirmed = shape(await signUp(client(), "ned@example.com"));
  const confirmed = shape(await signUp(client(), "max@example.com"));
  assert.deepEqual({ ...unconfirmed, email: "new@example.com" }, fresh);
  assert.deepEqual({ ...confirmed, email: "new@example.com" }, fresh);
});

// The PoC's attack, both ways round: whichever link the owner opens, the squatter never has
// a session on the account once its owner uses it, and the owner's password is the one.
test("the squatter of an address loses it to the inbox's owner, whichever link is opened",
  async () => {
    const db = createClient({ url: process.env.TURSO_DATABASE_URL });
    for (const confirmFirst of [false, true]) {
      const email = confirmFirst ? "mia@example.com" : "nia@example.com";
      const squatter = client();
      await signUp(squatter, email, "the squatter's password");
      const confirm = lastMailTo(email);
      const owner = client();
      await signUp(owner, email, "the owner's own password");
      const finish = lastMailTo(email);
      assert.equal(finish.subject, "Finish creating your Cicatrixa account");

      if (confirmFirst) {
        // The owner opens the squatter's confirmation link first: confirmed, signed in to
        // nothing, and the owner's password does not work yet.
        assert.equal((await owner.get(linkIn(confirm).replace(BASE, ""))).location,
                     "/login?verified=1");
        assert.equal((await owner.get("/get-session")).data, null);
        assert.equal((await signIn(owner, email, "the owner's own password")).status, 401);
        // The squatter can now log in, but only until the owner sets a password.
        assert.equal((await signIn(squatter, email, "the squatter's password")).status, 200);
      } else {
        assert.equal((await signIn(squatter, email, "the squatter's password")).status, 403);
      }

      // The owner sets the password through the finish link and logs in straight away.
      const token = await tokenFrom(owner, finish);
      assert.equal((await owner.post("/reset-password",
                                     { newPassword: "the owner's own password", token })).status, 200);
      assert.equal((await signIn(owner, email, "the owner's own password")).status, 200);
      const me = (await owner.get("/get-session")).data.user;
      assert.equal(me.emailVerified, true);
      // The password proof too: an ADMIN_EMAILS address taken back this way is admin at once.
      assert.equal((await db.execute({ sql: "select count(*) from cx_setting where key = ?",
                                       args: [`pw_proof:${me.id}`] })).rows[0][0], 1);

      // The squatter is out for good: no session, no password, and the old confirmation
      // link signs nobody in.
      assert.equal((await squatter.get("/get-session")).data, null);
      assert.equal((await signIn(client(), email, "the squatter's password")).status, 401);
      const late = client();
      assert.equal((await late.get(linkIn(confirm).replace(BASE, ""))).status, 302);
      assert.equal(late.jar.size, 0);
      // One credential row, holding the owner's password: what import_online copies.
      const rows = (await db.execute({ sql: "select count(*) from account where userId = ?",
                                       args: [me.id] })).rows;
      assert.equal(Number(rows[0][0]), 1);
    }
  });

test("finish-signing-up links count against the reset limit of the visitor who signs up",
  async () => {
    await signUp(client(), "oli@example.com");
    const before = mailsTo("oli@example.com").length;
    const c = client();
    const errors = [];
    const original = console.error;
    console.error = (...a) => errors.push(a.join(" "));
    const codes = [];
    try {
      for (let i = 0; i < 4; i++) codes.push((await signUp(c, "oli@example.com")).status);
    } finally {
      console.error = original;
    }
    assert.deepEqual(codes, [200, 200, 200, 200], "the answer never changes");
    assert.equal(mailsTo("oli@example.com").length - before, 3);
    assert.ok(errors.some((e) => e.includes("no password link for a repeated sign-up (429)")));
  });

test("forgot password before confirming: the link finishes the sign-up and confirms",
  async () => {
    const c = client();
    await signUp(c, "pia@example.com", "first password here");
    assert.equal((await c.post("/request-password-reset", {
      email: "pia@example.com", redirectTo: `${BASE}/reset-password` })).status, 200);
    const mail = lastMailTo("pia@example.com");
    assert.equal(mail.subject, "Finish creating your Cicatrixa account");
    const token = await tokenFrom(c, mail);
    assert.equal((await c.post("/reset-password", { newPassword: "chosen from the link",
                                                    token })).status, 200);
    assert.equal((await signIn(c, "pia@example.com", "first password here")).status, 401);
    assert.equal((await signIn(c, "pia@example.com", "chosen from the link")).status, 200);
    assert.equal((await c.get("/get-session")).data.user.emailVerified, true);
  });

test("a reset whose address cannot be marked confirmed still sets the password", async () => {
  const db = createClient({ url: process.env.TURSO_DATABASE_URL });
  await signUp(client(), "rae@example.com");
  await client().post("/request-password-reset", {
    email: "rae@example.com", redirectTo: `${BASE}/reset-password` });
  const c = client();
  const token = await tokenFrom(c, lastMailTo("rae@example.com"));
  await db.execute("create trigger no_confirming before update of emailVerified on \"user\" " +
                   "begin select raise(abort, 'user is read-only'); end");
  const errors = [];
  const original = console.error;
  console.error = (...a) => errors.push(a.join(" "));
  let reset;
  try {
    reset = await c.post("/reset-password", { newPassword: "a password from the link", token });
  } finally {
    console.error = original;
    await db.execute("drop trigger no_confirming");
  }
  assert.equal(reset.status, 200);
  assert.ok(errors.some((e) => e.includes("could not confirm the address after a reset")));
  // Unconfirmed still: logging in mails a confirmation link, and after it the password works.
  const before = outbox.length;
  assert.equal((await signIn(c, "rae@example.com", "a password from the link")).data.code,
               "EMAIL_NOT_VERIFIED");
  assert.equal(outbox.length, before + 1);
  await c.get(linkIn(lastMailTo("rae@example.com")).replace(BASE, ""));
  assert.equal((await signIn(c, "rae@example.com", "a password from the link")).status, 200);
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
