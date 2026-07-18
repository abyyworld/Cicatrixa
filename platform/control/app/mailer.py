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


def _wrap(eyebrow: str, body_html: str) -> str:
    """Same dark system as the app: near-black surface, red/green split wordmark,
    monospace type. Colors are hardcoded (not CSS vars) since email clients don't
    reliably support prefers-color-scheme."""
    return f"""<div style="font-family:ui-monospace,Menlo,Consolas,monospace;
background:#0A0F0C;padding:36px 20px;color:#E4EEE7">
<div style="max-width:460px;margin:0 auto;background:#101713;border:1px solid #1F2B24;
border-radius:14px;padding:32px 34px;overflow:hidden">
<div style="font-size:18px;font-weight:600;letter-spacing:-.02em;margin-bottom:6px">
<span style="color:#FF5257">cica</span><span style="color:#40D967">trixa</span></div>
<div style="font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#74857B;
margin-bottom:24px">{eyebrow}</div>
{body_html}
<div style="margin-top:30px;padding-top:18px;border-top:1px solid #1F2B24;
font-size:11.5px;color:#74857B">Cicatrixa · AI-operated hosting</div>
</div></div>"""


def send_verification_code(to: str, code: str) -> bool:
    digits = "".join(
        f'<td style="width:40px;height:52px;text-align:center;vertical-align:middle;'
        f'background:#0C1210;border:1px solid #2C7A45;border-radius:8px;'
        f'font-size:24px;font-weight:600;color:#40D967;letter-spacing:0">{d}</td>'
        f'<td style="width:8px"></td>'
        for d in code
    )
    html = _wrap("verify your email", f"""
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 22px">
Enter this code to finish signing in to Cicatrixa. It expires in 10 minutes.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 22px">
<tr>{digits}</tr>
</table>
<p style="font-size:12.5px;color:#74857B;margin:0">Didn't request this? You can ignore this
email — nothing happens without the code.</p>""")
    return send(to, f"{code} is your Cicatrixa verification code",
               f"Your Cicatrixa verification code is {code}. It expires in 10 minutes.", html)


def send_service_failed(to: str, project_name: str, service_name: str, project_url: str) -> bool:
    html = _wrap("deploy alert", f"""
<p style="font-size:14.5px;line-height:1.6;color:#E4EEE7;margin:0 0 22px">
<b>{service_name}</b> in project <b>{project_name}</b> went from live to failed. The last
known-good container keeps serving where possible, but the newest deploy did not come up
healthy.</p>
<a href="{project_url}" style="display:inline-block;background:#40D967;color:#06130A;
text-decoration:none;padding:11px 22px;border-radius:8px;font-size:12.5px;font-weight:600;
letter-spacing:.04em">View the project →</a>""")
    return send(to, f"⚠ {project_name}/{service_name} needs attention",
               f"{service_name} in {project_name} went live -> failed. {project_url}", html)
