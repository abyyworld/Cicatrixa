"""LLM brain of the deploy engine (OpenAI Responses API, heuristic fallbacks)."""
import json
import os
import re

import httpx

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("AI_MODEL", "gpt-5.1-codex-mini")


def available() -> bool:
    return bool(OPENAI_API_KEY)


def _ask(instructions: str, prompt: str, max_tokens: int = 4000) -> str:
    r = httpx.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={"model": MODEL, "instructions": instructions, "input": prompt,
              "max_output_tokens": max_tokens},
        timeout=180,
    )
    r.raise_for_status()
    data = r.json()
    # collect assistant text across output items
    parts = []
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text"):
                parts.append(c.get("text", ""))
    return "\n".join(parts).strip()


def _json_from(text: str) -> dict | None:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            raw = text[start:end + 1]
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


PLAN_INSTRUCTIONS = """You are the deployment engine of a hosting platform. Given a repository
snapshot, produce a plan to run it as a single Docker container serving HTTP.
Reply with ONLY a JSON object:
{
 "dockerfile": "<complete Dockerfile text, or null if the repo's own Dockerfile should be used>",
 "port": <int, the TCP port the app will listen on inside the container>,
 "health_path": "<HTTP path expected to return <500 — for APIs use a real route (e.g. /api/health), not '/'>",
 "build_args": {"NAME": "value", ...} or null,
 "api_prefixes": ["/api"] or null,
 "strip_prefix": false,
 "notes": "<one line: what the app is and how you chose to run it>"
}
Rules:
- If the repo has a working Dockerfile, set dockerfile to null and report its port (EXPOSE or code).
- Otherwise write a production-ready Dockerfile. Bind to 0.0.0.0. Prefer official slim base images.
- Static sites (plain html / built assets): use nginx:alpine, port 80.
- Node: detect the start script / framework (next start needs a build step first).
- Python: uvicorn/gunicorn for ASGI/WSGI, `python main.py` style otherwise.
- Never publish host ports; the platform routes by internal port only.

Multi-service projects (when "Sibling services" is listed):
- The platform BRIDGES API paths: if this service is the project's API/backend, set
  "api_prefixes" to the URL prefixes it serves (usually ["/api"]) — the platform will route
  those paths from the sibling frontends' domains to this container. Set "strip_prefix": true
  only if the app does NOT expect the prefix in its routes.
- If this service is a frontend that calls a sibling API: prefer relative "/api/..." requests
  (they are bridged, no CORS). If its code needs an absolute base URL baked in at build time,
  find the env var it reads (e.g. VITE_API_URL) and supply it in "build_args" with the
  sibling's public URL, declaring a matching ARG in the Dockerfile you write.

If the snapshot is not enough to decide (you must know how the code reads its API base URL,
its routes, or its config), reply instead with ONLY:
{"need_files": ["relative/path", ...]}   (max 8 files) — you will receive their contents."""


def _sibling_context(siblings: list[dict] | None) -> str:
    if not siblings:
        return ""
    lines = [f"- {s['name']}: public {s['public_url']}, internal http://{s['slug']}:"
             f"{s['port'] or '?'} (env keys {s['env_key']}_URL / {s['env_key']}_INTERNAL_URL)"
             for s in siblings]
    return "Sibling services in this project:\n" + "\n".join(lines) + "\n\n"


def _ask_with_files(instructions: str, prompt: str, read_file, rounds: int = 2):
    """Ask; if the model requests files, read them and ask again (deep analysis)."""
    for round_no in range(rounds):
        data = _json_from(_ask(instructions, prompt))
        needed = (data or {}).get("need_files")
        if not needed or not read_file or round_no == rounds - 1:
            return data
        chunks = []
        for path in list(needed)[:8]:
            content = read_file(path)
            chunks.append(f"--- {path} ---\n{content if content is not None else '(not found)'}")
        prompt += "\n\nRequested files:\n" + "\n".join(chunks)[:40000] + \
                  "\n\nNow reply with the full plan JSON only."
    return None


def build_plan(tree: str, files: dict[str, str], has_dockerfile: bool,
               siblings: list[dict] | None = None, read_file=None) -> dict | None:
    if not available():
        return None
    blob = "\n".join(f"--- {p} ---\n{c[:4000]}" for p, c in files.items())
    prompt = (_sibling_context(siblings) + f"Repository file tree:\n{tree[:6000]}\n\n"
              f"Repo has Dockerfile: {has_dockerfile}\n\nKey files:\n{blob[:40000]}")
    try:
        plan = _ask_with_files(PLAN_INSTRUCTIONS, prompt, read_file)
        if plan and isinstance(plan.get("port"), int):
            return plan
    except Exception:
        pass
    return None


FIX_INSTRUCTIONS = """You are the self-healing deploy engine of a hosting platform. A container
build or startup failed. Diagnose the root cause from the logs and produce a corrected plan.
Reply with ONLY a JSON object:
{"diagnosis": "<1-2 sentences root cause>",
 "dockerfile": "<complete corrected Dockerfile, or null to keep the current one>",
 "port": <int>, "health_path": "<path>",
 "build_args": {"NAME": "value"} or null}
If you need to inspect source files to find the real cause (config, entry point, router
setup), reply instead with ONLY: {"need_files": ["relative/path", ...]} (max 8) — you will
receive their contents and can then answer with the corrected plan."""


def fix_plan(dockerfile: str, error_log: str, tree: str, files: dict[str, str],
             siblings: list[dict] | None = None, read_file=None) -> dict | None:
    if not available():
        return None
    blob = "\n".join(f"--- {p} ---\n{c[:3000]}" for p, c in files.items())
    prompt = (_sibling_context(siblings) +
              f"Current Dockerfile:\n{dockerfile}\n\nFailure log (tail):\n{error_log[-12000:]}"
              f"\n\nFile tree:\n{tree[:4000]}\n\nKey files:\n{blob[:20000]}")
    try:
        return _ask_with_files(FIX_INSTRUCTIONS, prompt, read_file)
    except Exception:
        return None


CHAT_INSTRUCTIONS = """You are the on-call physician for a user's deployed project on the
Cicatrixa hosting platform. The user talks to you in a chat. You are given the project's
services (status, ports, container logs, recent deploy logs) and their repositories.

Investigate what the user asks — an error check, a suspected bug, a question, or a change
they want made. Ground every claim in the logs and code you were shown; never invent errors.
When a concrete CODE change would fix a real problem — or implement what the user explicitly
asked for — propose it as exact patches: if the user approves, the platform commits them to
the user's GitHub repository and redeploys, so patches must be minimal, correct, and
self-contained.

Reply with ONLY a JSON object:
{"reply": "<what you found / did, plain language, a few sentences — this is shown in chat>",
 "service": "<name of the service the patches apply to>" | null,
 "patches": [{"file": "relative/path", "find": "<exact literal text>", "replace": "<new text>"}] | null,
 "commit_message": "<one-line commit message>" | null}
- "find" must be an EXACT substring of the current file; it is literally replaced (all
  occurrences). Max 8 patches, one service per fix.
- Patch "file" paths are relative to that service's repository root — do NOT prefix them
  with the service name.
- No real problem or no code fix warranted -> patches: null and say so in "reply".
To read more source files first, reply ONLY: {"need_files": ["<service-name>/relative/path",
...]} (max 8) — paths are prefixed with the service name."""


def chat_agent(context: str, read_file=None) -> dict | None:
    if not available():
        return None
    try:
        return _ask_with_files(CHAT_INSTRUCTIONS, context, read_file, rounds=3)
    except Exception:
        return None


INTEGRATION_INSTRUCTIONS = """You are the integration engineer of a hosting platform. A frontend
service was deployed, but its built JS bundle calls an API at a foreign absolute URL instead of
its sibling API service on this platform.

The platform bridges the sibling API onto this frontend's own domain: requests to the listed
bridge prefix (e.g. /api/...) are routed to the API container (the prefix is stripped if noted).
So the correct fix is almost always: make the frontend call RELATIVE paths under the bridge
prefix — same origin, no CORS, no baked hostnames.

You may patch the frontend's files before rebuild. IMPORTANT: if the Dockerfile ships
prebuilt assets (a committed dist/ or build/ directory), patching src/ changes nothing —
patch the built asset files directly (replacing a quoted URL string inside minified JS is
safe), or patch both. "Files containing each URL" below tells you exactly where the foreign
URLs live. Reply with ONLY a JSON object:
{"diagnosis": "<1-2 sentences>",
 "patches": [{"file": "src/x.js", "find": "<exact literal text>", "replace": "<new text>"}],
 "build_args": {"NAME": "value"} or null}
- "find" must be an EXACT substring of the file (it will be literally replaced, all occurrences).
- Patch the API base constant / axios baseURL / fetch prefix to the bridge prefix (e.g. "/api"),
  or to an env-var read with that default. Max 6 patches, keep them minimal.
- If the foreign URL is legitimately external (a CDN, an auth provider, a third-party API),
  reply {"ok": true, "diagnosis": "<why no change is needed>"}.
If you need to see source files first, reply ONLY: {"need_files": ["path", ...]} (max 8)."""


def integration_fix(suspicious: list[str], bridge_prefix: str, strip: bool,
                    siblings: list[dict], tree: str, files: dict[str, str],
                    read_file=None, url_locations: dict | None = None) -> dict | None:
    if not available():
        return None
    blob = "\n".join(f"--- {p} ---\n{c[:3000]}" for p, c in files.items())
    strip_note = ("the prefix is STRIPPED before reaching the API (bridge /api/x -> API /x)"
                  if strip else "the prefix is passed through unchanged")
    locations = "\n".join(f"  {u} -> {paths}" for u, paths in (url_locations or {}).items())
    prompt = (_sibling_context(siblings) +
              f"Bridge prefix on this frontend's domain: {bridge_prefix} ({strip_note})\n"
              f"Foreign API URLs found in the built bundle: {suspicious}\n"
              f"Files containing each URL (repo-relative):\n{locations or '  (none found)'}\n\n"
              f"File tree:\n{tree[:6000]}\n\nKey files:\n{blob[:25000]}")
    try:
        return _ask_with_files(INTEGRATION_INSTRUCTIONS, prompt, read_file)
    except Exception:
        return None


def probe_paths(tree: str, files: dict[str, str], run_log: str, status: int) -> list[str]:
    """The app answered >=400 at its health path — read the code and name real routes."""
    if not available():
        return []
    blob = "\n".join(f"--- {p} ---\n{c[:3000]}" for p, c in files.items())
    prompt = (f"A freshly deployed app answered HTTP {status} at its probe path. Find real "
              f"HTTP routes it serves (health checks, API index, pages) from its code.\n\n"
              f"File tree:\n{tree[:5000]}\n\nKey files:\n{blob[:25000]}\n\n"
              f"Container log (tail):\n{run_log[-3000:]}\n\n"
              'Reply with ONLY JSON: {"paths": ["/api/health", ...]} — up to 4, most likely first.')
    try:
        data = _json_from(_ask("You analyze web service code and list its HTTP routes. "
                               "JSON only.", prompt))
        paths = [p for p in (data or {}).get("paths", []) if isinstance(p, str)
                 and p.startswith("/")]
        return paths[:4]
    except Exception:
        return []


def smoke_verdict(url: str, status: int, body_snippet: str, logs: str) -> str:
    if not available():
        return ""
    try:
        return _ask(
            "You verify freshly deployed web apps. In 1-2 sentences say whether the app looks "
            "healthy and correctly served, or what looks wrong. Plain text only.",
            f"GET {url} -> HTTP {status}\nBody (first 1500 chars):\n{body_snippet[:1500]}\n"
            f"Container logs (tail):\n{logs[-3000:]}",
            max_tokens=1000,
        )
    except Exception:
        return ""


# ---------- deterministic fallback when no AI key / AI fails ----------

def heuristic_plan(root: str) -> dict:
    import os.path as op

    def has(*names):
        return any(op.exists(op.join(root, n)) for n in names)

    if has("package.json"):
        try:
            pkg = json.load(open(op.join(root, "package.json")))
        except Exception:
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        build = '"build" in scripts' if "build" in pkg.get("scripts", {}) else ""
        if "next" in deps:
            df = ("FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\n"
                  "COPY . .\nRUN npm run build\nEXPOSE 3000\nENV HOST=0.0.0.0 PORT=3000\n"
                  'CMD ["npx","next","start","-H","0.0.0.0","-p","3000"]\n')
        else:
            start = pkg.get("scripts", {}).get("start")
            cmd = '["npm","start"]' if start else '["node","index.js"]'
            build_line = "RUN npm run build\n" if build else ""
            df = ("FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\n"
                  f"COPY . .\n{build_line}EXPOSE 3000\nENV HOST=0.0.0.0 PORT=3000\nCMD {cmd}\n")
        return {"dockerfile": df, "port": 3000, "health_path": "/",
                "notes": "heuristic: node app"}
    if has("requirements.txt", "pyproject.toml"):
        req = ("COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n"
               if has("requirements.txt") else
               "COPY pyproject.toml ./\nRUN pip install --no-cache-dir .\n")
        entry = next((f for f in ("main.py", "app.py", "server.py", "run.py")
                      if op.exists(op.join(root, f))), "main.py")
        mod = entry[:-3]
        df = ("FROM python:3.12-slim\nWORKDIR /app\n" + req + "COPY . .\nEXPOSE 8000\n"
              "RUN pip install --no-cache-dir uvicorn || true\n"
              f'CMD ["sh","-c","uvicorn {mod}:app --host 0.0.0.0 --port 8000 || python {entry}"]\n')
        return {"dockerfile": df, "port": 8000, "health_path": "/",
                "notes": "heuristic: python app"}
    if has("go.mod"):
        df = ("FROM golang:1.22-alpine AS build\nWORKDIR /src\nCOPY . .\n"
              "RUN go build -o /bin/app ./...\nFROM alpine\nCOPY --from=build /bin/app /bin/app\n"
              'EXPOSE 8080\nENV PORT=8080\nCMD ["/bin/app"]\n')
        return {"dockerfile": df, "port": 8080, "health_path": "/", "notes": "heuristic: go app"}
    # default: static site
    df = ("FROM nginx:alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n")
    return {"dockerfile": df, "port": 80, "health_path": "/", "notes": "heuristic: static site"}
