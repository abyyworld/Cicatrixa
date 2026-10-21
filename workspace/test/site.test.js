// The signed-in pages' script, site/workspace.js, run against each real page's HTML in a
// small fake browser (node:vm): which elements exist and which start hidden come from the
// page itself, the API answers come from a fake fetch. What it pins down:
//   - ?error= shows only fixed sentences: every code workspace/routes.js redirects with
//     has its own, and any other text in the URL (a phishing sentence, "constructor")
//     shows the generic one — never the URL's own words;
//   - the admin proof: an admin address without it sees the Admin link and a "Confirm
//     it's you" box that POSTs admin/claim; nobody else does;
//   - the Connect tab moves on only once the connection exists, however often the
//     visitor comes back to it before finishing on GitHub;
//   - the dashboard says what is true before the GitHub App exists;
//   - the static copy fixed with these: the App's real permissions, no running platform
//     on the sign-up and log-in pages, the move-to-a-server order on /admin.
// Focus inside the modal dialogs needs a real browser; the Playwright run checks it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { ERROR_CODES } from "../routes.js";

const SITE = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "site");
const SCRIPT = readFileSync(join(SITE, "workspace.js"), "utf8");
const read = (page) => readFileSync(join(SITE, page), "utf8");

// The SHARED CONTRACT's sentences, word for word.
const SENTENCES = {
  app_missing: "GitHub connections aren't open yet.",
  state: "That GitHub link was started from another account or has expired. Start again here.",
  no_code: "GitHub did not send an authorization. If the App is already installed, use " +
           "“Already installed? Connect it”.",
  not_yours: "That installation is not on your GitHub account or on an organisation you administer.",
  not_installed: "The App is not installed on your GitHub account or on an organisation you " +
                 "administer. Install it first.",
  taken: "That GitHub installation is already connected to another Cicatrixa account.",
  org_request: "GitHub sent your request to the organisation's owners. Once one of them approves " +
               "it, that owner connects it from their own Cicatrixa account — or install the App " +
               "on your own GitHub account instead.",
  github: "GitHub didn't answer as expected. Try again in a minute.",
  admin_only: "Only an admin can do that.",
  app_exists: "A GitHub App is already set up.",
  app_state: "That App creation link was started from another account or has expired. Start again.",
  app_failed: "GitHub didn't finish creating the App. Try again.",
  signin: "Sign in first.",
};
const GENERIC = "Something went wrong. Try again.";

// ---------- a fake browser, just big enough for these pages ----------

class FakeElement {
  constructor(doc, id, { tag = "div", hidden = false, text = "" } = {}) {
    Object.assign(this, { doc, id, tagName: tag.toUpperCase(), hidden, textContent: text,
                          title: "", href: "", value: "", disabled: false, inert: false });
    this.attrs = {};
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this.kids = [];
    this.parts = new Map();
    this.classList = { toggle() {}, add() {}, remove() {}, contains: () => false };
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  // Parts the page reaches by selector inside an element (a meter's ".v" and ".fill").
  querySelector(sel) {
    if (!this.parts.has(sel)) this.parts.set(sel, new FakeElement(this.doc, `${this.id} ${sel}`));
    return this.parts.get(sel);
  }
  querySelectorAll() { return []; }
  append(...kids) { this.kids.push(...kids); }
  contains(el) { return el === this; }
  closest() { return null; }
  focus() { this.doc.activeElement = this; }
  click() {
    const event = { target: this, preventDefault() {} };
    for (const fn of [...(this.listeners.click || [])]) fn(event);
  }
}

// Every element with an id, as the page's HTML starts it: tag, hidden or not, and its
// text when it holds no markup.
function elementsOf(html) {
  const found = [];
  for (const m of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*)>/gi)) {
    const id = m[2].match(/\bid="([^"]+)"/)?.[1];
    if (!id) continue;
    const bare = m[2].replace(/"[^"]*"/g, '""');
    const after = html.slice(m.index + m[0].length);
    const text = after.split("<")[0].replace(/\s+/g, " ").trim();
    found.push({ id, tag: m[1], hidden: /(^|\s)hidden(?=[\s=]|$)/.test(bare), text });
  }
  return found;
}

const json = (status, data) => ({
  ok: status >= 200 && status < 300, status, body: null,
  json: async () => data, text: async () => JSON.stringify(data),
});

// GET me as workspace/routes.js answers it; `user` merges into the default user.
const meData = ({ user, ...over } = {}) => ({
  user: { id: "u1", email: "ada@example.com", name: "ada", isAdmin: false, adminPending: false,
          ...user },
  github: null, app: { configured: true, slug: "cicatrixa-test" }, aiOn: false, mailOn: true,
  baseDomain: "cicatrixa.com", limits: { services: 5, analysesPerDay: 20 },
  usage: { services: 0, analysesToday: 0 }, ...over,
});

// Loads `page` (a file in site/) at `url` and runs workspace.js on it. `answer(method, path,
// body)` returns [status, data] for /api/*; unknown paths are a test failure.
async function open(page, url, answer) {
  const html = read(page);
  const doc = {
    hidden: false, title: "", activeElement: null, listeners: {}, byId: new Map(),
    body: null,
    getElementById(id) { return this.byId.get(id) || null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    createElement(tag) { return new FakeElement(doc, "", { tag }); },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event = {}) {
      for (const fn of [...(this.listeners[type] || [])]) fn({ type, ...event });
    },
  };
  for (const e of elementsOf(html)) doc.byId.set(e.id, new FakeElement(doc, e.id, e));
  doc.body = new FakeElement(doc, "body", { tag: "body" });
  doc.body.dataset.page = html.match(/<body data-page="([^"]+)"/)[1];
  doc.body.children = [];

  const u = new URL(url, "https://app.cicatrixa.com");
  const nav = [];
  const calls = [];
  const errors = [];
  const fetch = async (path, init = {}) => {
    const method = init.method || "GET";
    const body = init.body === undefined ? undefined : JSON.parse(init.body);
    calls.push({ method, path, body, headers: init.headers || {} });
    const got = answer(method, path, body);
    if (!got) throw new Error(`unexpected request ${method} ${path}`);
    return json(...got);
  };
  const context = vm.createContext({
    document: doc, fetch, URL, URLSearchParams, TextDecoder, setTimeout, clearTimeout,
    CSS: { escape: (s) => s },
    console: { ...console, error: (...a) => errors.push(a.join(" ")) },
    history: { replaceState() {} },
    addEventListener() {},
    location: {
      search: u.search, pathname: u.pathname, origin: u.origin, href: u.href,
      assign: (to) => nav.push(["assign", to]), replace: (to) => nav.push(["replace", to]),
      reload: () => nav.push(["reload"]),
    },
  });
  vm.runInContext(SCRIPT, context, { filename: "site/workspace.js" });
  await settle();
  const $ = (id) => {
    const el = doc.getElementById(id);
    assert.ok(el, `${page} has no #${id}`);
    return el;
  };
  return { doc, $, nav, calls, errors, visible: (id) => !$(id).hidden };
}

async function settle() {
  for (let i = 0; i < 30; i++) await new Promise((resolve) => setImmediate(resolve));
}

// The answers every signed-in page needs, plus `more` for the page's own requests.
function signedIn(me, more = () => null) {
  return (method, path, body) => {
    if (path === "/api/auth/get-session") return [200, { user: { id: "u1", email: me.user.email } }];
    if (method === "GET" && path === "/api/workspace/me") return [200, me];
    return more(method, path, body);
  };
}

// ---------- ?error= ----------

test("every code workspace/routes.js redirects with shows its own sentence", async () => {
  assert.deepEqual([...ERROR_CODES].sort(), Object.keys(SENTENCES).sort(),
                   "routes.js and the SHARED CONTRACT list the same codes");
  for (const code of ERROR_CODES) {
    const p = await open("connect/github.html", `/connect/github?error=${code}`, signedIn(meData()));
    assert.equal(p.$("error").textContent, SENTENCES[code], code);
    assert.ok(p.visible("error"), code);
    assert.deepEqual(p.errors, []);
  }
});

test("text in ?error= that is not a code is never shown, on every page that has a banner",
  async () => {
    const phishing = "Your Cicatrixa App install is outdated. Reinstall it at " +
                     "github.com/apps/cicatrixa-v2";
    const pages = [
      ["connect/github.html", "/connect/github", () => null],
      ["dashboard.html", "/dashboard", (m, path) => (path === "/api/workspace/projects"
        ? [200, { projects: [] }] : null)],
      ["admin.html", "/admin", (m, path) => (path === "/api/workspace/admin"
        ? [200, { app: { configured: false }, counts: {}, aiOn: false, mailOn: true }] : null)],
    ];
    const admin = meData({ user: { isAdmin: true } });
    for (const [page, path, more] of pages) {
      for (const value of [phishing, "constructor", "__proto__", "toString", "hasOwnProperty",
                           "APP_MISSING", "state ", "not_yours.", "<b>x</b>"]) {
        const p = await open(page, `${path}?${new URLSearchParams({ error: value })}`,
                             signedIn(page === "admin.html" ? admin : meData(), more));
        const shown = p.$("error").textContent;
        assert.ok(shown === GENERIC || (value === "state " && shown === SENTENCES.state),
                  `${page} ?error=${JSON.stringify(value)} showed ${JSON.stringify(shown)}`);
        assert.deepEqual(p.errors, [], page);
      }
    }
  });

test("a confirmation link that failed still says the link is the problem", async () => {
  const projects = (m, path) => (path === "/api/workspace/projects" ? [200, { projects: [] }] : null);
  for (const [code, sentence] of [["INVALID_TOKEN", "That link has expired or was already used."],
                                  ["invalid_token", "That link has expired or was already used."],
                                  ["TOKEN_EXPIRED", "That link has expired."],
                                  ["USER_NOT_FOUND", "That link has expired or was already used."]]) {
    const p = await open("dashboard.html", `/dashboard?error=${code}`, signedIn(meData(), projects));
    assert.equal(p.$("error").textContent, sentence, code);
  }
});

test("a lost connection sends the new-project page to Connect with a code, not a sentence",
  async () => {
    const connected = meData({ github: { installationId: 7, accountLogin: "ada", accountType: "User" } });
    for (const [status, code] of [[409, "reconnect"], [503, "app_missing"]]) {
      const p = await open("projects/new.html", "/projects/new", signedIn(connected, (m, path) =>
        (path === "/api/workspace/repos" ? [status, { error: "Some sentence from the API." }] : null)));
      assert.deepEqual(p.nav, [["replace", `/connect/github?error=${code}`]]);
      const c = await open("connect/github.html", `/connect/github?error=${code}`, signedIn(meData()));
      assert.notEqual(c.$("error").textContent, GENERIC);
      assert.ok(!c.$("error").textContent.includes("Some sentence"));
    }
  });

// ---------- admin proof ----------

test("an admin address without the password proof sees Admin and a claim box", async () => {
  const me = meData({ user: { adminPending: true } });
  const posts = [];
  const p = await open("admin.html", "/admin", signedIn(me, (method, path, body) => {
    if (method === "POST" && path === "/api/workspace/admin/claim") {
      posts.push(body);
      return [200, { ok: true }];
    }
    return null;
  }));
  assert.ok(p.visible("admin-link"), "the nav shows Admin");
  assert.ok(p.visible("claim") && !p.visible("denied") && !p.visible("page"));
  assert.ok(!p.calls.some((c) => c.path === "/api/workspace/admin"), "no admin data asked for");
  assert.equal(p.$("claim-to").textContent, "ada@example.com");
  assert.ok(!p.visible("claim-sent"));

  p.$("claim-btn").click();
  await settle();
  assert.deepEqual(posts, [{}]);
  const post = p.calls.find((c) => c.path === "/api/workspace/admin/claim");
  assert.equal(post.headers["content-type"], "application/json", "JSON, so the CSRF check passes");
  assert.ok(p.visible("claim-sent") && !p.visible("claim-ask"));
  assert.equal(p.$("claim-sent-to").textContent, "ada@example.com");
  assert.ok(!p.visible("claim-error"));
});

test("a refused claim says why and lets them try again", async () => {
  const me = meData({ user: { adminPending: true } });
  const p = await open("admin.html", "/admin", signedIn(me, (method, path) =>
    (path === "/api/workspace/admin/claim"
      ? [429, { error: "Too many emails asked for from here. Try again in a minute." }] : null)));
  p.$("claim-btn").click();
  await settle();
  assert.equal(p.$("claim-error").textContent,
               "Too many emails asked for from here. Try again in a minute.");
  assert.ok(p.visible("claim-ask") && !p.visible("claim-sent"));
  assert.equal(p.$("claim-btn").disabled, false);
});

test("nobody else gets the Admin link or the claim box", async () => {
  const p = await open("admin.html", "/admin", signedIn(meData()));
  assert.ok(!p.visible("admin-link"));
  assert.ok(p.visible("denied") && !p.visible("claim") && !p.visible("page"));

  const a = await open("admin.html", "/admin", signedIn(meData({ user: { isAdmin: true } }),
    (m, path) => (path === "/api/workspace/admin"
      ? [200, { app: { configured: true, slug: "cx" }, counts: { users: 1 }, aiOn: true, mailOn: true }]
      : null)));
  assert.ok(a.visible("admin-link") && a.visible("page") && !a.visible("claim"));
});

test("before the App exists, a pending admin is told to confirm on the admin page", async () => {
  const noApp = { app: { configured: false, slug: null } };
  for (const [user, shown] of [[{ isAdmin: true }, "noapp-admin"],
                               [{ adminPending: true }, "noapp-pending"],
                               [{}, "noapp-user"]]) {
    const p = await open("connect/github.html", "/connect/github",
                         signedIn(meData({ ...noApp, user })));
    for (const id of ["noapp-admin", "noapp-pending", "noapp-user"]) {
      assert.equal(p.visible(id), id === shown, `${JSON.stringify(user)}: #${id}`);
    }
  }
});

// ---------- connect: coming back to the tab ----------

test("coming back to the Connect tab moves on only once GitHub is connected", async () => {
  let github = null;
  let meCalls = 0;
  const p = await open("connect/github.html", "/connect/github", (method, path) => {
    if (path === "/api/auth/get-session") return [200, { user: { id: "u1" } }];
    if (path === "/api/workspace/me") { meCalls++; return [200, meData({ github })]; }
    return null;
  });
  const back = async () => {
    p.doc.hidden = false;
    p.doc.dispatch("visibilitychange");
    await settle();
  };
  p.$("install-btn").click();
  p.$("install-btn").click();                      // twice: still one listener
  assert.ok(p.visible("install-hint"));
  const before = meCalls;

  await back();                                    // only glancing back
  assert.equal(meCalls, before + 1);
  assert.deepEqual(p.nav, []);
  assert.ok(p.visible("install-hint"), "the hint stays up");

  await back();                                    // still not finished on GitHub
  assert.equal(meCalls, before + 2);
  assert.deepEqual(p.nav, []);

  github = { installationId: 9, accountLogin: "ada", accountType: "User" };
  await back();                                    // finished: now it moves on
  assert.deepEqual(p.nav, [["assign", "/projects/new"]]);
  await back();
  assert.equal(p.nav.length, 1, "and listens no more");
});

test("changing repos of an existing connection moves on at the first return", async () => {
  const gh = { installationId: 9, accountLogin: "ada", accountType: "User" };
  const p = await open("connect/github.html", "/connect/github", signedIn(meData({ github: gh })));
  p.$("install-btn").click();
  assert.ok(p.visible("change-hint") && !p.visible("install-hint"));
  p.doc.dispatch("visibilitychange");
  await settle();
  assert.deepEqual(p.nav, [["assign", "/projects/new"]]);
});

// ---------- dashboard ----------

test("the dashboard claims to read repos only once the GitHub App exists", async () => {
  const projects = (m, path) => (path === "/api/workspace/projects" ? [200, { projects: [] }] : null);
  const on = await open("dashboard.html", "/dashboard", signedIn(meData(), projects));
  assert.match(on.$("notice-how").textContent, /^Cicatrixa already reads your repo/);
  const off = await open("dashboard.html", "/dashboard",
                         signedIn(meData({ app: { configured: false, slug: null } }), projects));
  assert.equal(off.$("notice-how").textContent, "GitHub connections open shortly.");
  assert.deepEqual([...on.errors, ...off.errors], []);
});

// ---------- static copy ----------

test("the marketing page names the GitHub App's real permissions", () => {
  const html = read("index.html");
  assert.ok(!/repo read/i.test(html), "no 'repo read' left");
  assert.equal(html.match(/read &amp; write/g)?.length, 2);
  assert.equal(html.match(/org members/g)?.length, 2);
  const connect = read("connect/github.html");
  assert.match(connect, /GitHub will ask for read &amp; write access to code/);
  assert.match(connect, /Change which repositories Cicatrixa can access/);
});

test("sign-up and log-in claim no running platform", () => {
  const signup = read("signup.html");
  const login = read("login.html");
  assert.ok(!login.includes("never noticed you were gone"));
  assert.ok(!signup.includes("Every account starts with 14 days free"));
  assert.match(signup, /trial starts when deploying opens/);
});

test("/admin lists the move to a server in the runbook's order, ending with --notify", () => {
  const term = read("admin.html").match(/<pre class="term">([\s\S]*?)<\/pre>/)[1];
  const order = ["import_online --dry-run", "import_online\n", "move app and * to the box",
                 "VERIFY_ALLOW_RESTART=1 ./verify-public.sh", "docker restart cx-control",
                 "import_online --notify"];
  const at = order.map((step) => term.indexOf(step));
  assert.ok(at.every((i) => i >= 0), JSON.stringify(at));
  assert.deepEqual([...at].sort((a, b) => a - b), at, "in this order");
  assert.match(read("admin.html"), /docs\/RUNBOOK\.md<\/code>, section “Leaving Vercel”/);
});

test("the demo page leads a signed-in visitor back to the dashboard", () => {
  const demo = read("demo.html");
  const masthead = demo.match(/<header class="masthead">([\s\S]*?)<\/header>/)[1];
  assert.match(masthead, /href="\/dashboard"/);
  assert.ok(!demo.includes("analysed today"));
  assert.ok(!demo.includes("already reads your repo"));
});
