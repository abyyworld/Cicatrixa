"""Transactional email via Resend's HTTP API — no SMTP, just an API key."""
import os

import httpx

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_ADDR = os.environ.get("MAIL_FROM", "Cicatrixa <noreply@cicatrixa.com>")


def available() -> bool:
    return bool(RESEND_API_KEY)


def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    if not available():
        return False
    payload = {"from": FROM_ADDR, "to": [to], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    try:
        r = httpx.post("https://api.resend.com/emails",
                       headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                       json=payload, timeout=15)
        return r.status_code < 300
    except Exception:
        return False


def _wrap(title: str, body_html: str) -> str:
    return f"""<div style="font-family:ui-monospace,Menlo,monospace;background:#F4EFE6;
padding:32px;color:#1C1917">
<div style="max-width:480px;margin:0 auto;background:#FAF6EE;border:1px solid #1C1917;
border-radius:2px;padding:28px 30px">
<div style="font-size:20px;font-style:italic;font-family:Georgia,serif;margin-bottom:4px">
Cicatrixa</div>
<div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#6B6257;
margin-bottom:22px">{title}</div>
{body_html}
</div></div>"""


def send_verification(to: str, verify_url: str) -> bool:
    html = _wrap("verify your account", f"""
<p style="font-size:14px;line-height:1.6">Confirm this address to finish setting up your
Cicatrixa account.</p>
<a href="{verify_url}" style="display:inline-block;margin-top:8px;background:#B0261C;
color:#F4EFE6;text-decoration:none;padding:10px 20px;border-radius:2px;font-size:12.5px;
font-weight:600;letter-spacing:.08em;text-transform:uppercase">Verify email →</a>
<p style="font-size:12px;color:#6B6257;margin-top:20px">Or paste this link:<br>{verify_url}</p>""")
    return send(to, "Verify your Cicatrixa account", f"Verify your account: {verify_url}", html)


def send_service_failed(to: str, project_name: str, service_name: str, project_url: str) -> bool:
    html = _wrap("chart update", f"""
<p style="font-size:14px;line-height:1.6"><b>{service_name}</b> in project
<b>{project_name}</b> went from live to failed. The last known-good container keeps serving
where possible, but the newest deploy did not come up healthy.</p>
<a href="{project_url}" style="display:inline-block;margin-top:8px;background:#B0261C;
color:#F4EFE6;text-decoration:none;padding:10px 20px;border-radius:2px;font-size:12.5px;
font-weight:600;letter-spacing:.08em;text-transform:uppercase">View the chart →</a>""")
    return send(to, f"⚠ {project_name}/{service_name} needs attention",
               f"{service_name} in {project_name} went live -> failed. {project_url}", html)
