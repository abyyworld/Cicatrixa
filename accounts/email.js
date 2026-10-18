// Transactional email through Resend's HTTP API (no SDK, no SMTP): the verification
// link and the password-reset link. Same look as platform/control/app/mailer.py.

const RESEND_URL = "https://api.resend.com/emails";

export function mailOn(env = process.env) {
  return Boolean(env.RESEND_API_KEY);
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function wrap(eyebrow, bodyHtml) {
  // Colours are written out, not CSS variables: email clients do not support
  // prefers-color-scheme reliably.
  return `<div style="font-family:ui-monospace,Menlo,Consolas,monospace;background:#0A0F0C;padding:36px 20px;color:#E4EEE7">
<div style="max-width:460px;margin:0 auto;background:#101713;border:1px solid #1F2B24;border-radius:14px;padding:32px 34px">
<div style="font-size:18px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px"><span style="color:#FF5257">cica</span><span style="color:#40D967">trixa</span></div>
<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#74857B;margin-bottom:24px">${eyebrow}</div>
${bodyHtml}
<div style="margin-top:30px;padding-top:18px;border-top:1px solid #1F2B24;font-size:11.5px;color:#74857B">Cicatrixa</div>
</div></div>`;
}

function button(url, label) {
  return `<a href="${escapeHtml(url)}" style="display:inline-block;background:#40D967;color:#06130A;text-decoration:none;padding:11px 22px;border-radius:8px;font-size:12.5px;font-weight:600;letter-spacing:.04em">${label}</a>
<p style="font-size:12px;color:#74857B;margin-top:20px;word-break:break-all">Or paste this link:<br>${escapeHtml(url)}</p>`;
}

// Throws when Resend refuses. Better Auth logs the error and still answers the request
// as usual (so the page cannot tell), which puts Resend's own reason in the function's
// log on Vercel: "domain is not verified" there means adding Resend's DNS records.
export async function send(env, { to, subject, text, html }) {
  if (!mailOn(env)) {
    console.warn(`accounts: RESEND_API_KEY is not set — "${subject}" was not sent`);
    return false;
  }
  const from = env.MAIL_FROM || "Cicatrixa <noreply@cicatrixa.com>";
  const res = await fetch(RESEND_URL, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from, to: [to], subject, text, html }),
  });
  if (!res.ok) {
    const reason = (await res.text()).slice(0, 300);
    console.error(`accounts: Resend refused "${subject}" (${res.status}): ${reason}`);
    throw new Error(`email not sent (Resend ${res.status})`);
  }
  return true;
}

export function sendVerificationEmail(env, to, url) {
  return send(env, {
    to,
    subject: "Confirm your email for Cicatrixa",
    text: `Confirm your email to finish creating your Cicatrixa account (link works for an hour): ${url}`,
    html: wrap("confirm your email", `
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 22px">Confirm this address to
finish creating your Cicatrixa account. The link works for an hour.</p>
${button(url, "Confirm my email →")}
<p style="font-size:12.5px;color:#74857B;margin:18px 0 0">Didn't sign up? Ignore this email —
nothing happens without the link.</p>`),
  });
}

export function sendResetPasswordEmail(env, to, url) {
  return send(env, {
    to,
    subject: "Reset your Cicatrixa password",
    text: `Choose a new Cicatrixa password (link works once, for an hour): ${url}`,
    html: wrap("reset your password", `
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 22px">Someone asked to reset
the password for this Cicatrixa account. The link below works once and expires in an hour.</p>
${button(url, "Choose a new password →")}
<p style="font-size:12.5px;color:#74857B;margin:18px 0 0">Didn't ask for this? Ignore this
email — your password stays as it is.</p>`),
  });
}
