// Account pages: talks to Better Auth at /api/auth (api/auth.js). Every page names
// itself in <body data-page="…">; dynamic text goes in with textContent only.
(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);

  async function api(path, body) {
    const res = await fetch(`/api/auth${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    return { ok: res.ok, status: res.status, data };
  }

  const MESSAGES = {
    INVALID_EMAIL_OR_PASSWORD: "Wrong email or password.",
    EMAIL_NOT_VERIFIED: "Confirm your email first — we've just sent you a new link.",
    PASSWORD_TOO_SHORT: "Password must be at least 8 characters.",
    PASSWORD_TOO_LONG: "Password must be at most 128 characters.",
    INVALID_EMAIL: "That doesn't look like an email.",
    INVALID_TOKEN: "That link has expired or was already used.",
    TOKEN_EXPIRED: "That link has expired.",
    USER_ALREADY_EXISTS: "An account with that email already exists. Log in instead.",
  };

  function problem(result) {
    if (result.status === 429) return "Too many tries. Wait a minute, then try again.";
    const code = result.data?.code;
    return MESSAGES[code] || result.data?.message || "Something went wrong. Try again.";
  }

  function show(id, text) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
  }

  // Disable the button while a request is in flight; report anything thrown.
  function onSubmit(form, run) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      show("error", "");
      try { await run(new FormData(form)); }
      catch { show("error", "Can't reach the server. Check your connection and try again."); }
      finally { button.disabled = false; }
    });
  }

  // A path on this site only: anything else could send people to another host.
  function safeNext(value) {
    return value && value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\")
      ? value : "/dashboard";
  }

  const linkError = () => {
    const e = params.get("error");
    return e ? (MESSAGES[e.toUpperCase()] || "That link has expired or was already used.") : "";
  };

  const pages = {
    signup() {
      onSubmit($("form"), async (f) => {
        const email = String(f.get("email")).trim();
        const r = await api("/sign-up/email", {
          email, password: f.get("password"), name: email.split("@")[0],
          callbackURL: "/dashboard?welcome=1",
        });
        if (!r.ok) return show("error", problem(r));
        if (r.data?.token) return location.assign("/dashboard?welcome=1");   // email is off
        $("form").hidden = true;
        $("sent-to").textContent = email;
        $("sent").hidden = false;
      });
    },

    login() {
      if (params.get("reset")) show("notice", "Password changed — log in with the new one.");
      show("error", linkError() && `${linkError()} Log in and we'll send you a new one.`);
      onSubmit($("form"), async (f) => {
        const r = await api("/sign-in/email", {
          email: String(f.get("email")).trim(), password: f.get("password"),
          // Where the fresh confirmation link lands if the email is not confirmed yet.
          rememberMe: true, callbackURL: "/dashboard?welcome=1",
        });
        if (!r.ok) return show("error", problem(r));
        location.assign(safeNext(params.get("next")));
      });
    },

    forgot() {
      onSubmit($("form"), async (f) => {
        const email = String(f.get("email")).trim();
        const r = await api("/request-password-reset",
                            { email, redirectTo: `${location.origin}/reset-password` });
        if (!r.ok) return show("error", problem(r));
        $("form").hidden = true;
        $("sent-to").textContent = email;
        $("sent").hidden = false;     // the same answer whether or not the account exists
      });
    },

    reset() {
      const token = params.get("token");
      if (!token || params.get("error")) {
        $("form").hidden = true;
        $("expired").hidden = false;
        return;
      }
      onSubmit($("form"), async (f) => {
        const r = await api("/reset-password", { newPassword: f.get("password"), token });
        if (!r.ok) {
          if (["INVALID_TOKEN", "TOKEN_EXPIRED"].includes(r.data?.code)) {
            $("form").hidden = true;
            $("expired").hidden = false;
            return;
          }
          return show("error", problem(r));
        }
        location.assign("/login?reset=1");
      });
    },

    async dashboard() {
      const r = await api("/get-session");
      if (!r.ok || !r.data?.user) {
        const e = params.get("error");
        return location.replace(e ? `/login?error=${encodeURIComponent(e)}` : "/login");
      }
      $("email").textContent = r.data.user.email;
      $("notify-email").textContent = r.data.user.email;
      if (params.get("welcome")) show("notice", "Email confirmed — welcome to Cicatrixa.");
      $("account").hidden = false;
      $("signout").addEventListener("click", async () => {
        await api("/sign-out", {});
        location.assign("/login");
      });
    },
  };

  pages[document.body.dataset.page]?.();
})();
