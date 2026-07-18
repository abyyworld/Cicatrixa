"""GitHub integration: GitHub App (manifest flow, installation tokens) + PAT fallback."""
import hashlib
import hmac
import json
import time

import httpx
import jwt

from . import db

API = "https://api.github.com"
_token_cache: dict[int, tuple[str, float]] = {}  # installation_id -> (token, expires_at)


# ---------- GitHub App credentials (created once via manifest flow) ----------

def app_configured() -> bool:
    return bool(db.setting("gh_app_id") and db.setting("gh_app_pem"))


def app_slug() -> str | None:
    return db.setting("gh_app_slug")


def build_manifest(base_url: str) -> dict:
    """Manifest for one-click GitHub App creation from the admin page."""
    return {
        "name": db.setting("gh_app_name", "cicatrixa-deploy"),
        "url": base_url,
        "hook_attributes": {"url": f"{base_url}/api/webhooks/github", "active": True},
        "redirect_url": f"{base_url}/admin/github/callback",
        "callback_urls": [f"{base_url}/connect/github/callback"],
        "setup_url": f"{base_url}/connect/github/setup",
        "setup_on_update": False,
        "public": True,
        "default_permissions": {"contents": "read", "metadata": "read"},
        "default_events": ["push"],
    }


def exchange_manifest_code(code: str) -> dict:
    """Convert the temporary manifest code into permanent app credentials."""
    r = httpx.post(f"{API}/app-manifests/{code}/conversions",
                   headers={"Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    db.set_setting("gh_app_id", str(data["id"]))
    db.set_setting("gh_app_slug", data["slug"])
    db.set_setting("gh_app_pem", data["pem"])
    db.set_setting("gh_app_webhook_secret", data["webhook_secret"] or "")
    db.set_setting("gh_app_client_id", data.get("client_id", ""))
    db.set_setting("gh_app_client_secret", data.get("client_secret", ""))
    return data


def _app_jwt() -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": db.setting("gh_app_id")}
    return jwt.encode(payload, db.setting("gh_app_pem"), algorithm="RS256")


def installation_token(installation_id: int) -> str:
    cached = _token_cache.get(installation_id)
    if cached and cached[1] > time.time() + 120:
        return cached[0]
    r = httpx.post(f"{API}/app/installations/{installation_id}/access_tokens",
                   headers={"Authorization": f"Bearer {_app_jwt()}",
                            "Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    expires = time.mktime(time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _token_cache[installation_id] = (data["token"], expires)
    return data["token"]


# ---------- per-connection helpers (App installation or PAT) ----------

def conn_token(connection) -> str:
    if connection["kind"] == "app":
        return installation_token(connection["installation_id"])
    return connection["pat_token"] or ""


def _get(token: str, path: str, params: dict | None = None):
    headers = {"Accept": "application/vnd.github+json"}
    if token:  # empty token -> anonymous access (public repos only)
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"{API}{path}", params=params, timeout=30, headers=headers)
    r.raise_for_status()
    return r.json()


def list_repos(connection) -> list[dict]:
    token = conn_token(connection)
    repos: list[dict] = []
    if connection["kind"] == "app":
        page = 1
        while True:
            data = _get(token, "/installation/repositories",
                        {"per_page": 100, "page": page})
            repos += data.get("repositories", [])
            if len(repos) >= data.get("total_count", 0) or not data.get("repositories"):
                break
            page += 1
    else:
        page = 1
        while page <= 5:
            batch = _get(token, "/user/repos",
                         {"per_page": 100, "page": page, "sort": "pushed"})
            repos += batch
            if len(batch) < 100:
                break
            page += 1
    return [{"full_name": r["full_name"], "private": r["private"],
             "default_branch": r.get("default_branch", "main"),
             "pushed_at": r.get("pushed_at", "")} for r in repos]


def viewer_login(token: str) -> str:
    try:
        return _get(token, "/user").get("login", "")
    except httpx.HTTPStatusError:
        return ""


def head_sha(connection, repo_full: str, branch: str) -> str | None:
    try:
        data = _get(conn_token(connection), f"/repos/{repo_full}/commits/{branch}",
                    {"per_page": 1})
        return data.get("sha")
    except Exception:
        return None


def default_branch(connection, repo_full: str) -> str:
    try:
        return _get(conn_token(connection), f"/repos/{repo_full}").get("default_branch", "main")
    except Exception:
        return "main"


def clone_url(connection, repo_full: str) -> str:
    token = conn_token(connection)
    if not token:
        return f"https://github.com/{repo_full}.git"
    return f"https://x-access-token:{token}@github.com/{repo_full}.git"


# ---------- webhooks ----------

def verify_webhook(secret: str, signature: str | None, body: bytes) -> bool:
    if not secret:
        return True  # no secret configured yet — accept (poll watchdog still validates)
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], digest)


def parse_push(body: bytes) -> tuple[str, str, str] | None:
    """Return (repo_full, branch, head_sha) for a push event, else None."""
    try:
        payload = json.loads(body)
        ref = payload.get("ref", "")
        if not ref.startswith("refs/heads/"):
            return None
        return (payload["repository"]["full_name"], ref[len("refs/heads/"):],
                payload.get("after", ""))
    except Exception:
        return None
