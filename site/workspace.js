// Signed-in pages (dashboard, connect GitHub, new project, project, admin): static pages
// that talk to workspace/routes.js at /api/workspace/* and to Better Auth at /api/auth/*
// for the session. As in accounts.js, every page names itself in <body data-page="…">.
// Everything that comes from the server or the URL goes in through textContent or DOM
// nodes, never innerHTML: a repo name, a project name or a log line can hold anything,
// and none of it may become markup.
//
// Nothing here can be built or run yet, so no page says an app is live: the best a
// project gets is "planned", and the platform deploys it once it has a server.
(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const BASE_DOMAIN = "cicatrixa.com";
  const OFFLINE = "Can't reach the server. Check your connection and try again.";

  // ---------- requests ----------

  // GET without a body, POST JSON with one. The API refuses a POST that is not JSON or
  // whose Origin is not ours (CSRF); fetch sends Origin on every POST by itself.
  async function call(url, body) {
    const res = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? { accept: "application/json" }
        : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body or not JSON */ }
    return { ok: res.ok, status: res.status, data };
  }
  const api = (path, body) => call(`/api/workspace/${path}`, body);
  const auth = (path, body) => call(`/api/auth/${path}`, body);

  function problem(r) {
    if (typeof r.data?.error === "string" && r.data.error) return r.data.error;
    if (r.status === 429) return "Too many tries. Wait a minute, then try again.";
    return "Something went wrong. Try again.";
  }

  // ---------- DOM ----------

  // h("a", {class: "x", href: "/y"}, "text", child…): attributes only, children as
  // nodes or text nodes. Nothing is ever parsed as HTML.
  function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
    el.append(...kids.flat().filter((kid) => kid !== undefined && kid !== null && kid !== false));
    return el;
  }

  function show(target, text) {
    const el = typeof target === "string" ? $(target) : target;
    if (!el) return;
    el.textContent = text || "";
    el.hidden = !text;
  }

  // The page could not be shown at all.
  function fatal(text) {
    $("page").hidden = true;
    show("fatal", text);
  }

  // Leaving the page aborts its requests: that is not a failure to report.
  let leaving = false;
  addEventListener("pagehide", () => { leaving = true; });
  // Back or Forward can restore a page from the browser's memory as it was left: a
  // button still disabled, a stale project list, or a signed-in page after logging out.
  // Load it again instead, which checks the session first.
  addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });
  function go(url) {
    leaving = true;
    location.assign(url);
  }

  // Disable the button while it works; a throw is the network failing.
  async function busy(button, errorTarget, run) {
    button.disabled = true;
    show(errorTarget, "");
    try { await run(); } catch { show(errorTarget, OFFLINE); }
    finally { if (!leaving) button.disabled = false; }
  }

  // A dialog holds the keyboard while it is open: the page behind it is inert, Tab and
  // Shift+Tab cycle through the dialog's own controls, Escape closes it, and focus goes
  // back to the button that opened it. Otherwise a keyboard user tabs out to links hidden
  // behind the overlay, and after closing has lost their place on the page.
  const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden])," +
                    " select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

  function modal(overlay) {
    const box = overlay.querySelector(".modal") || overlay;
    let opener = null;
    let behind = [];

    // What Tab can reach: visible controls, and of a radio group only its checked one
    // (the arrow keys move inside a group, as everywhere else).
    function tabbable() {
      return [...box.querySelectorAll(FOCUSABLE)].filter((el) => !el.closest("[hidden]") &&
        !(el.type === "radio" && !el.checked &&
          box.querySelector(`input[type=radio][name="${CSS.escape(el.name)}"]:checked`)));
    }

    function close() {
      if (overlay.hidden) return;
      overlay.hidden = true;
      for (const el of behind) el.inert = false;
      behind = [];
      if (opener?.isConnected) opener.focus();
      opener = null;
    }

    overlay.addEventListener("click", (event) => {
      if (event.target === overlay || event.target.closest("[data-close]")) close();
    });
    document.addEventListener("keydown", (event) => {
      if (overlay.hidden) return;
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (event.key !== "Tab") return;
      const items = tabbable();
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const at = document.activeElement;
      const inside = box.contains(at);
      if (event.shiftKey ? (at === first || !inside) : (at === last || !inside)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    });
    return {
      open() {
        opener = document.activeElement;
        overlay.hidden = false;
        behind = [...document.body.children].filter((el) => el !== overlay && !el.inert);
        for (const el of behind) el.inert = true;
        (overlay.querySelector("[data-close]") || tabbable()[0])?.focus();
      },
      close,
    };
  }

  // ---------- statuses ----------

  // Online: queued → analyzing → planned | failed. The platform's own names stay styled
  // for the day it takes over.
  const STATUSES = new Set(["queued", "analyzing", "planned", "failed",
                            "new", "deploying", "live", "degraded", "stopped"]);

  function setPill(el, status) {
    el.className = STATUSES.has(status) ? `pill ${status}` : "pill";
    el.textContent = status || "unknown";
  }
  const pill = (status) => { const el = h("span"); setPill(el, status); return el; };

  // Same rule as workspace/routes.js projectStatus.
  function projectStatus(services) {
    const st = services.map((s) => s.status);
    if (st.includes("analyzing")) return "analyzing";
    if (st.includes("failed")) return "failed";
    if (st.length && st.every((s) => s === "planned")) return "planned";
    return "queued";
  }

  const repoOf = (s) => s.repoFull ?? s.repo_full ?? "";

  // ?error= is a short code, never a sentence: anyone can send a link to these pages
  // with any text in the URL, and none of it may appear under Cicatrixa's own banner
  // (a fake "reinstall the App at …" would read as ours). workspace/routes.js redirects
  // with these codes; "reconnect" is this script's own (newProject); a confirmation link
  // that failed brings one of Better Auth's, in capitals. Anything else is the generic
  // sentence.
  const ERRORS = {
    app_missing: "GitHub connections aren't open yet.",
    state: "That GitHub link was started from another account or has expired. Start again here.",
    no_code: "GitHub did not send an authorization. If the App is already installed, " +
             "use “Already installed? Connect it”.",
    not_yours: "That installation is not on your GitHub account or on an organisation you " +
               "administer.",
    not_installed: "The App is not installed on your GitHub account or on an organisation " +
                   "you administer. Install it first.",
    taken: "That GitHub installation is already connected to another Cicatrixa account.",
    org_request: "GitHub sent your request to the organisation's owners. Once one of them " +
                 "approves it, that owner connects it from their own Cicatrixa account — or " +
                 "install the App on your own GitHub account instead.",
    github: "GitHub didn't answer as expected. Try again in a minute.",
    admin_only: "Only an admin can do that.",
    app_exists: "A GitHub App is already set up.",
    app_state: "That App creation link was started from another account or has expired. " +
               "Start again.",
    app_failed: "GitHub didn't finish creating the App. Try again.",
    signin: "Sign in first.",
    reconnect: "Cicatrixa can't reach your GitHub installation any more — it may have been " +
               "uninstalled. Connect GitHub again.",
  };
  const LINK_ERRORS = {
    INVALID_TOKEN: "That link has expired or was already used.",
    TOKEN_EXPIRED: "That link has expired.",
    USER_NOT_FOUND: "That link has expired or was already used.",
    INVALID_USER: "That link has expired or was already used.",
  };
  const GENERIC = "Something went wrong. Try again.";

  // Own keys only: "constructor" or "__proto__" in the URL must not find Object's.
  const pick = (table, key) => (Object.hasOwn(table, key) ? table[key] : undefined);
  function urlError() {
    const e = (params.get("error") || "").trim();
    if (!e) return "";
    return pick(ERRORS, e) ?? pick(LINK_ERRORS, e.toUpperCase()) ?? GENERIC;
  }

  // ---------- session and masthead ----------

  function toLogin() {
    const q = new URLSearchParams();
    if (/^\/dashboard(\.html)?$/.test(location.pathname)) {
      // A confirmation link that failed lands on /dashboard?error=… signed out.
      if (params.get("error")) q.set("error", params.get("error"));
    } else {
      q.set("next", location.pathname + location.search);
    }
    const qs = String(q);
    location.replace(qs ? `/login?${qs}` : "/login");
  }

  function masthead(me) {
    $("who").textContent = me.user.email;
    $("who").title = me.user.email;
    // An admin address that has not proved its password yet still gets the link: /admin
    // is where it confirms (admin/claim).
    $("admin-link").hidden = !(me.user.isAdmin || me.user.adminPending);
    for (const a of document.querySelectorAll("#links a")) {
      const here = a.pathname === location.pathname.replace(/\.html$/, "");
      if (here) a.setAttribute("aria-current", "page");
    }
    $("links").hidden = false;
    const out = $("signout");
    out.hidden = false;
    out.addEventListener("click", async () => {
      out.disabled = true;
      try {
        if ((await auth("sign-out", {})).ok) return go("/login");
      } catch { /* offline: say so below */ }
      out.disabled = false;
      show("fatal", "Could not log out. Try again.");
    });
  }

  // The session decides who is signed in; /me says what they have. Nothing on the page
  // shows until both answer, so a signed-out visitor never sees a flash of it.
  async function session() {
    let s;
    let me;
    try {
      [s, me] = await Promise.all([auth("get-session"), api("me")]);
    } catch {
      if (!leaving) fatal(OFFLINE);
      return null;
    }
    if (me.status === 401 || (s.ok && !s.data?.user)) {
      toLogin();
      return null;
    }
    if (!me.ok || !me.data?.user) {
      fatal(problem(me));
      return null;
    }
    masthead(me.data);
    return me.data;
  }

  // ---------- dashboard ----------

  function meter(el, used, limit) {
    el.querySelector(".v").textContent = `${used} / ${limit}`;
    const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
    const fill = el.querySelector(".fill");
    fill.style.width = `${pct}%`;
    fill.classList.toggle("warn", pct >= 70 && pct < 90);
    fill.classList.toggle("crit", pct >= 90);
  }

  function projectCard(p) {
    const services = p.services || [];
    return h("a", { class: "card proj", href: `/project?id=${encodeURIComponent(p.id)}` },
      h("div", { class: "grow" },
        h("div", { class: "name", text: p.name }),
        h("div", { class: "svcs mono muted",
                   text: services.map((s) => `${s.name} (${repoOf(s)})`).join(" · ") })),
      // The address is reserved, not serving: text, never a link.
      h("span", { class: "addr mono muted", title: "reserved — live when deploying opens",
                  text: `${p.slug}.${BASE_DOMAIN}` }),
      pill(p.status));
  }

  async function dashboard(me) {
    const r = await api("projects");
    if (!r.ok) return fatal(problem(r));
    const projects = r.data?.projects || [];
    const gh = me.github;

    const error = urlError();
    if (error) show("error", error);
    else if (params.get("deleted")) show("notice", "✔ Project deleted.");
    else if (params.get("github") === "connected") show("notice", "✔ GitHub connected.");
    else if (params.get("welcome")) show("notice", "✔ Email confirmed — welcome to Cicatrixa.");

    const login = gh?.accountLogin ? `@${gh.accountLogin} · ` : "";
    $("gh-status").textContent = gh ? `GitHub connected (${login}App install #${gh.installationId})`
      : "GitHub not connected yet";
    // Until an admin has created the GitHub App, nobody's repo can be read: say so instead.
    if (!me.app?.configured) $("notice-how").textContent = "GitHub connections open shortly.";
    $("disconnect").hidden = !gh;
    $("connect").hidden = Boolean(gh);

    meter($("meter-services"), me.usage?.services ?? 0, me.limits?.services ?? 5);
    if (typeof me.usage?.analysesToday === "number") {
      meter($("meter-analyses"), me.usage.analysesToday, me.limits?.analysesPerDay ?? 20);
    } else {
      $("meter-analyses").hidden = true;
    }

    $("projects").append(...projects.map(projectCard));
    if (!projects.length) {
      $("empty-why").textContent = gh ? "Pick a repo and Cicatrixa works out how to run it."
        : "Connect your GitHub account to get started.";
      $("empty-go").textContent = gh ? "Create your first project" : "Connect GitHub";
      $("empty-go").href = gh ? "/projects/new" : "/connect/github";
      $("empty").hidden = false;
    }

    const dc = modal($("dc-overlay"));
    $("disconnect").addEventListener("click", dc.open);
    $("dc-go").addEventListener("click", () => busy($("dc-go"), "dc-error", async () => {
      const revoke = document.querySelector("input[name=dc_revoke]:checked")?.value === "1";
      const res = await api("github/disconnect", { revoke });
      if (!res.ok) return show("dc-error", problem(res));
      go("/dashboard");
    }));

    $("page").hidden = false;
  }

  // ---------- connect GitHub ----------

  // Once, when this tab is visible again: they came back from GitHub's tab.
  function onReturn(run) {
    document.addEventListener("visibilitychange", function back() {
      if (document.hidden) return;
      document.removeEventListener("visibilitychange", back);
      run();
    });
  }

  // Coming back to this tab is not the same as having finished on GitHub: they may only
  // be rereading the hint. Move on once the connection exists (a change of repos needs no
  // new one); until then keep the hint up and wait for the next return.
  function whenConnected(changing) {
    onReturn(async function back() {
      if (changing) return go("/projects/new");
      let r = null;
      try { r = await api("me"); } catch { /* offline: try again on the next return */ }
      if (r?.status === 401) return toLogin();
      if (r?.ok && r.data?.github) return go("/projects/new");
      onReturn(back);
    });
  }

  function connect(me) {
    show("error", urlError());
    const gh = me.github;
    if (gh) {
      const org = gh.accountType === "Organization" ? " (organisation)" : "";
      $("conn-who").textContent = `${gh.accountLogin ? `@${gh.accountLogin}` : "your account"}${org}` +
        ` · App install #${gh.installationId}`;
      $("connected").hidden = false;
    }
    if (me.app?.configured) {
      $("app-card").hidden = false;
      if (gh) {
        // /connect/github/start sends a connected account to its installation's settings.
        $("install-btn").textContent = "Change which repos ↗";
        $("authorize-line").hidden = true;
      }
      let waiting = false;
      $("install-btn").addEventListener("click", () => {
        $(gh ? "change-hint" : "install-hint").hidden = false;
        if (waiting) return;          // a second click must not stack a second listener
        waiting = true;
        whenConnected(Boolean(gh));
      });
    } else {
      $("noapp-card").hidden = false;
      const who = me.user.isAdmin ? "admin" : me.user.adminPending ? "pending" : "user";
      $(`noapp-${who}`).hidden = false;
    }
    $("page").hidden = false;
  }

  // ---------- new project ----------

  async function newProject(me) {
    if (!me.app?.configured || !me.github) return location.replace("/connect/github");
    if (params.get("github") === "connected") {
      show("notice", "✔ GitHub connected. Check the repos that belong to this project.");
    }
    show("error", urlError());
    $("add-repos").href = `https://github.com/apps/${encodeURIComponent(me.app.slug)}/installations/new`;
    const used = me.usage?.services ?? 0;
    const limit = me.limits?.services ?? 5;
    $("quota").textContent = `${used} of ${limit} services used`;

    const list = $("repolist");
    const rows = new Map();         // lowercase full name → row
    let seq = 0;
    let listFailed = false;

    const NONE_PICKED = "Pick at least one repository.";
    function repoToggled() {
      if ($("error").textContent === NONE_PICKED) show("error", "");
      let primaryStillValid = false;
      for (const row of rows.values()) {
        const cb = row.querySelector("input[type=checkbox]");
        const radio = row.querySelector("input[type=radio]");
        row.querySelector(".primary-pick").hidden = !cb.checked;
        if (!cb.checked) radio.checked = false;
        if (cb.checked && radio.checked) primaryStillValid = true;
      }
      if (!primaryStillValid) {
        const first = [...rows.values()].find((r) => r.querySelector("input[type=checkbox]").checked);
        if (first) first.querySelector("input[type=radio]").checked = true;
      }
    }

    function repoRow(repo) {
      const n = ++seq;
      const cb = h("input", { type: "checkbox", name: "repos", value: repo.full_name, id: `cb-${n}` });
      cb.addEventListener("change", repoToggled);
      const radio = h("input", { type: "radio", name: "primary", value: repo.full_name, id: `pr-${n}` });
      return h("div", { class: "repo-row", "data-name": repo.full_name.toLowerCase() },
        cb,
        h("label", { class: "plain", for: `cb-${n}` }, repo.full_name, repo.private ? " 🔒" : ""),
        h("span", { class: "primary-pick mono muted", hidden: true },
          radio, h("label", { class: "plain inline", for: `pr-${n}`, text: "main" })));
    }

    function applyFilter() {
      const q = $("filter-input").value.trim().toLowerCase();
      for (const row of rows.values()) row.hidden = !row.dataset.name.includes(q);
    }

    const REFRESH = "↻ refresh";
    async function load(feedback) {
      const btn = $("refresh-btn");
      btn.disabled = true;
      btn.textContent = "↻ loading…";
      let r = null;
      try { r = await api("repos"); } catch { /* offline */ }
      $("repo-loading").hidden = true;
      const done = (label) => {
        btn.textContent = label;
        if (label === REFRESH) btn.disabled = false;
        else setTimeout(() => { btn.textContent = REFRESH; btn.disabled = false; }, 2000);
      };
      if (r && (r.status === 409 || r.status === 503)) {
        // Not connected (any more), or connections are not open: that page says why, by
        // code — it never shows text taken from its URL.
        const code = r.status === 503 ? "app_missing" : "reconnect";
        return location.replace(`/connect/github?error=${code}`);
      }
      if (!r || !r.ok) {
        listFailed = true;
        show("error", r ? problem(r) : OFFLINE);
        $("empty-msg").hidden = rows.size > 0;
        return done(REFRESH);
      }
      if (listFailed) { listFailed = false; show("error", ""); }
      const fresh = new Map((r.data?.repos || []).map((repo) => [repo.full_name.toLowerCase(), repo]));
      let added = 0;
      let removed = 0;
      // Drop repos no longer granted to the GitHub App.
      for (const [key, row] of rows) {
        if (!fresh.has(key)) { row.remove(); rows.delete(key); removed++; }
      }
      for (const [key, repo] of fresh) {
        if (!rows.has(key)) { const row = repoRow(repo); rows.set(key, row); list.append(row); added++; }
      }
      $("empty-msg").hidden = rows.size > 0;
      applyFilter();
      repoToggled();
      if (added || removed) $("gh-hint").hidden = true;
      if (!feedback) return done(REFRESH);
      const parts = [added && `${added} added`, removed && `${removed} removed`].filter(Boolean);
      done(parts.length ? `↻ ${parts.join(", ")}` : "↻ up to date");
    }

    $("filter-input").addEventListener("input", applyFilter);
    $("refresh-btn").addEventListener("click", () => load(true));
    $("refresh-now").addEventListener("click", (event) => {
      event.preventDefault();
      $("gh-hint").hidden = true;
      load(true);
    });
    let waiting = false;
    $("add-repos").addEventListener("click", () => {
      $("gh-hint").hidden = false;
      if (waiting) return;
      waiting = true;
      onReturn(() => { waiting = false; load(false); });
    });

    $("form").addEventListener("submit", (event) => {
      event.preventDefault();
      const repos = [...list.querySelectorAll("input[name=repos]:checked")].map((cb) => cb.value);
      if (!repos.length) return show("error", NONE_PICKED);
      const body = { repos,
                     primary: list.querySelector("input[name=primary]:checked")?.value || repos[0] };
      const name = $("name").value.trim();
      const branch = $("branch").value.trim();
      if (name) body.name = name;
      if (branch) body.branch = branch;
      busy($("deploy-btn"), "error", async () => {
        const r = await api("projects", body);
        if (!r.ok || !r.data?.project?.id) {
          show("error", problem(r));
          $("error").scrollIntoView({ block: "nearest" });
          return;
        }
        go(`/project?id=${encodeURIComponent(r.data.project.id)}&run=1`);
      });
    });

    $("page").hidden = false;
    await load(false);
  }

  // ---------- project ----------

  // Lines of NDJSON as they arrive; the whole text at once where a browser has no
  // readable body stream.
  async function readLines(res, onLine) {
    const reader = typeof res.body?.getReader === "function" ? res.body.getReader() : null;
    if (!reader) {
      for (const line of (await res.text()).split("\n")) if (line.trim()) onLine(line);
      return;
    }
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (line.trim()) onLine(line);
      }
    }
    buf += decoder.decode();
    if (buf.trim()) onLine(buf);
  }

  function setLog(term, text) {
    term.textContent = text || "";
    term.scrollTop = term.scrollHeight;
  }

  // Follow the log unless the reader scrolled up to read something.
  function appendLog(term, line) {
    const following = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    term.append(term.firstChild ? `\n${line}` : line);
    if (following) term.scrollTop = term.scrollHeight;
  }

  const STATE_COPY = {
    planned: ["Planned · deploys automatically when deploying opens.",
              "Cicatrixa read every repo and wrote the plan below. Nothing is built or run yet; " +
              "when our build servers open, this project deploys on its own and we email you."],
    analyzing: ["Analyzing — reading your repos through GitHub.",
                "Cicatrixa reads each repo's files, works out how to run it and writes the " +
                "Dockerfile. Nothing is built or run yet."],
    queued: ["Queued — waiting to be analyzed.",
             "Each repo is read through GitHub and planned in turn. Nothing is built or run yet."],
    failed: ["An analysis failed.",
             "The log says why. Fix it in the repo, then re-analyze. Nothing is built or run yet."],
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function project() {
    const id = params.get("id");
    if (!id) return location.replace("/dashboard");
    const first = await api(`projects/${encodeURIComponent(id)}`);
    if (first.status === 404) return fatal("No such project. It may have been deleted.");
    if (!first.ok || !first.data?.project) return fatal(problem(first));
    let proj = first.data.project;
    const cards = new Map();        // service id → {el, f: fields, svc, streaming}
    let running = false;
    let watching = false;

    const current = () => proj.services.map((s) => cards.get(s.id)?.svc || s);

    function fill(c, svc) {
      c.svc = svc;
      const { f } = c;
      f.name.textContent = svc.name;
      f.main.hidden = !svc.isPrimary || proj.services.length < 2;
      f.repo.textContent = `${repoOf(svc)} @ ${svc.branch}${svc.sha ? ` · ${svc.sha.slice(0, 8)}` : ""}`;
      f.addr.textContent = `${svc.slug}.${BASE_DOMAIN}`;
      setPill(f.pill, svc.status);
      show(f.error, svc.status === "failed" ? (svc.error || "The analysis failed.") : "");
      const plan = svc.plan && typeof svc.plan === "object" ? svc.plan : null;
      f.plan.hidden = !plan;
      f["df-wrap"].hidden = !plan;
      if (plan) {
        f.notes.textContent = plan.notes || "—";
        f.port.textContent = plan.port ?? "—";
        f.health.textContent = plan.health_path || "/";
        const bridge = Array.isArray(plan.api_prefixes) ? plan.api_prefixes.join(", ") : "";
        f.bridge.textContent = bridge ? `${bridge} served on sibling domains` : "";
        f.bridge.hidden = !bridge;
        f["bridge-k"].hidden = !bridge;
        const df = typeof plan.dockerfile === "string" && plan.dockerfile.trim() ? plan.dockerfile : "";
        f.dockerfile.textContent = df;
        f.dockerfile.hidden = !df;
        f["own-df"].hidden = Boolean(df);
      }
      f.log.dataset.empty = svc.status === "queued" ? "waiting to be analyzed…" : "no log yet";
      if (!c.streaming && typeof svc.log === "string") setLog(f.log, svc.log);
    }

    function card(svc) {
      const el = $("svc-tpl").content.firstElementChild.cloneNode(true);
      const f = {};
      for (const node of el.querySelectorAll("[data-f]")) f[node.dataset.f] = node;
      const c = { el, f, svc, streaming: false };
      f.run.addEventListener("click", () => runQueue([svc.id]));
      cards.set(svc.id, c);
      fill(c, svc);
      return el;
    }

    function header() {
      const services = current();
      const status = projectStatus(services);
      document.title = `${proj.name} — Cicatrixa`;
      $("pname").textContent = proj.name;
      setPill($("statuspill"), status);
      const n = services.length;
      $("pcount").textContent = `${n} service${n === 1 ? "" : "s"} · ${proj.slug}.${BASE_DOMAIN}`;
      const [head, text] = STATE_COPY[status] || STATE_COPY.queued;
      $("state-head").textContent = head;
      $("state-text").textContent = text;
      $("state-note").classList.toggle("bad", status === "failed");
      $("run-all").disabled = running;
      $("delete").disabled = running;
      for (const c of cards.values()) c.f.run.disabled = running || c.svc.status === "analyzing";
    }

    // The stored truth, for every card not streaming right now.
    async function reload() {
      let r;
      try { r = await api(`projects/${encodeURIComponent(proj.id)}`); } catch { return false; }
      if (r.status === 404) { go("/dashboard"); return false; }
      if (!r.ok || !r.data?.project) return false;
      proj = r.data.project;
      for (const s of proj.services) {
        const c = cards.get(s.id);
        if (c && !c.streaming) fill(c, s);
      }
      header();
      return true;
    }

    // "next" goes on to the next service in the queue, "stop" ends the queue.
    async function analyzeOne(sid) {
      const c = cards.get(sid);
      if (!c) return "next";
      c.streaming = true;
      fill(c, { ...c.svc, status: "analyzing", error: null });
      setLog(c.f.log, "");
      header();
      let res;
      try {
        res = await fetch(`/api/workspace/projects/${encodeURIComponent(proj.id)}/services/` +
                          `${encodeURIComponent(sid)}/analyze`, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "application/x-ndjson" },
          body: "{}",
          credentials: "same-origin",
        });
      } catch {
        c.streaming = false;
        await reload();
        show("error", OFFLINE);
        return "stop";
      }
      if (!res.ok) {
        let data = null;
        try { data = await res.json(); } catch { /* not JSON */ }
        c.streaming = false;
        await reload();            // e.g. 409: it is being analyzed elsewhere
        if (res.status === 401) { toLogin(); return "stop"; }
        show(c.f.error, problem({ status: res.status, data }));
        // A 409 for a service another tab is analyzing concerns that service only. Out
        // of analyses for today (429), GitHub not connected (409) or connections closed
        // (503): every other service would be refused too.
        const elsewhere = res.status === 409 && cards.get(sid).svc.status === "analyzing";
        return elsewhere || ![409, 429, 503].includes(res.status) ? "next" : "stop";
      }
      let end = null;
      try {
        await readLines(res, (line) => {
          let m;
          try { m = JSON.parse(line); } catch { return; }
          if (m?.type === "log" && typeof m.line === "string") appendLog(c.f.log, m.line);
          else if (m?.type === "done" && m.service) end = { service: m.service };
          else if (m?.type === "error") end = { error: String(m.error || "The analysis failed.") };
        });
      } catch { /* the stream broke: what was stored is the truth */ }
      c.streaming = false;
      if (end?.service) fill(c, end.service);
      else if (end?.error) fill(c, { ...c.svc, status: "failed", error: end.error, log: c.f.log.textContent });
      else await reload();
      header();
      return "next";
    }

    async function runQueue(ids) {
      if (running) return;
      running = true;
      show("error", "");
      header();
      try {
        for (const sid of ids) if (await analyzeOne(sid) === "stop") break;
      } finally {
        running = false;
        header();
      }
      watch();
    }

    // Another tab (or a function still finishing) is analyzing: follow it from Turso.
    async function watch() {
      if (watching) return;
      watching = true;
      for (let i = 0; i < 40 && !running && current().some((s) => s.status === "analyzing"); i++) {
        await sleep(4000);
        if (!running) await reload();
      }
      watching = false;
    }

    $("services").append(...proj.services.map(card));
    header();

    $("run-all").addEventListener("click", () => runQueue(proj.services.map((s) => s.id)));
    const del = modal($("del-overlay"));
    $("delete").addEventListener("click", del.open);
    $("del-go").addEventListener("click", () => busy($("del-go"), "del-error", async () => {
      const r = await api(`projects/${encodeURIComponent(proj.id)}/delete`, {});
      if (!r.ok) return show("del-error", problem(r));
      go("/dashboard?deleted=1");
    }));

    // run=1 comes from creating the project; drop it so a reload does not ask again.
    if (params.has("run")) {
      history.replaceState(null, "", `${location.pathname}?id=${encodeURIComponent(proj.id)}`);
    }
    $("page").hidden = false;
    const queued = proj.services.filter((s) => s.status === "queued").map((s) => s.id);
    if (queued.length) runQueue(queued);
    else watch();
  }

  // ---------- admin ----------

  // An address on the admin list with a confirmed email is not proof enough: anyone can
  // sign up with that address, with a password of their own, and wait for its owner to
  // click the confirmation link. So admin/claim emails that address a link to set the
  // password once; whoever sets it holds both the inbox and the password, and is admin
  // from their next login.
  function claimAdmin(me) {
    show("claim-error", urlError());
    $("claim-to").textContent = me.user.email;
    $("claim-btn").addEventListener("click", () => busy($("claim-btn"), "claim-error", async () => {
      const r = await api("admin/claim", {});
      if (!r.ok) return show("claim-error", problem(r));
      $("claim-sent-to").textContent = me.user.email;
      $("claim-ask").hidden = true;
      $("claim-sent").hidden = false;
    }));
    $("claim").hidden = false;
  }

  async function admin(me) {
    if (!me.user.isAdmin) {
      if (me.user.adminPending) return claimAdmin(me);
      $("denied").hidden = false;
      return;
    }
    const r = await api("admin");
    if (r.status === 403) { $("denied").hidden = false; return; }
    if (!r.ok || !r.data) return fatal(problem(r));
    const a = r.data;
    const c = a.counts || {};

    $("base-url").textContent = location.origin;
    $("ai-state").textContent = a.aiOn ? "on" : "off — set OPENAI_API_KEY";
    $("mail-state").textContent = a.mailOn ? "on" : "off — set RESEND_API_KEY";
    const error = urlError();
    if (error) show("error", error);
    else if (params.get("github") === "created") {
      show("notice", "✔ GitHub App created. Everyone can connect GitHub now.");
    }

    const stats = [
      ["users", c.users, `${c.verifiedUsers ?? 0} verified`],
      ["projects", c.projects],
      ["services", c.services, `${c.planned ?? 0} planned`],
      ["github connections", c.connections],
    ];
    $("stats").append(...stats.map(([label, value, sub]) => h("div", {},
      h("div", { class: "lbl-caps", text: label }), h("div", { class: "v", text: String(value ?? 0) }),
      sub && h("div", { class: "sub", text: sub }))));

    if (a.app?.configured) {
      setPill($("app-pill"), "live");
      $("app-pill").textContent = `configured — ${a.app.slug}`;
      $("hook-url").textContent = `${location.origin}/api/webhooks/github`;
      $("app-settings").textContent = `github.com/settings/apps/${a.app.slug}`;
      $("app-on").hidden = false;
    } else {
      setPill($("app-pill"), "failed");
      $("app-pill").textContent = "not configured";
      $("app-off").hidden = false;
      const form = $("app-form");
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        busy(form.querySelector("button[type=submit]"), "error", async () => {
          const res = await api("admin/github-app",
                                { name: $("app-name").value.trim(), org: $("app-org").value.trim() });
          if (!res.ok) return show("error", problem(res));
          // GitHub's manifest flow: POST the manifest to GitHub, which creates the App and
          // comes back to /admin/github/callback. Only ever to github.com.
          let action;
          try { action = new URL(res.data.action); } catch { action = null; }
          if (!action || action.origin !== "https://github.com" || typeof res.data.manifest !== "string") {
            return show("error", "The server sent back an unexpected answer. Try again.");
          }
          const field = h("input", { type: "hidden", name: "manifest" });
          field.value = res.data.manifest;
          const post = h("form", { method: "post", action: action.href, hidden: true }, field);
          document.body.append(post);
          leaving = true;
          post.submit();
        });
      });
    }
    $("page").hidden = false;
  }

  // ---------- start ----------

  const pages = { dashboard, connect, "new-project": newProject, project, admin };

  (async () => {
    const page = pages[document.body.dataset.page];
    if (!page) return;
    const me = await session();
    if (!me) return;
    try {
      await page(me);
    } catch (err) {
      if (leaving) return;
      console.error(err);
      fatal("This page could not load. Check your connection, then reload.");
    }
  })();
})();
