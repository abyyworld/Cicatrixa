// How Cicatrixa works out how to run a repo, read through the GitHub API instead of a
// clone. A port of the analysis half of the platform's deploy pipeline:
// platform/control/app/ai.py (heuristic_plan, build_plan and the prompt it sends) and
// engine.py (_snapshot, KEY_FILES and the post-processing in _run_pipeline), plus the
// slug rules from main.py. When the platform runs on a Docker host it re-plans every
// repo itself, so what matters here is that a user sees the same plan the box will
// make: the same tree, the same key files, the same prompt, byte for byte.
// workspace/test/plan.test.js checks that against the Python code itself.

import { MAX_FILE_BYTES } from "./github.js";

// ---------- what the platform reads (engine.py) ----------

export const KEY_FILES = Object.freeze([
  "package.json", "requirements.txt", "pyproject.toml", "go.mod", "Gemfile",
  "Dockerfile", "docker-compose.yml", "Procfile", "next.config.js",
  "next.config.mjs", "vite.config.js", "vite.config.ts", "main.py", "app.py",
  "server.py", "index.js", "server.js", "app.js", "README.md", "Makefile",
  "nuxt.config.ts", "angular.json", "composer.json", "index.html",
]);

export const EXCLUDED_DIRS = Object.freeze([
  ".git", "node_modules", "__pycache__", ".next", "venv", ".venv", "target",
]);
const EXCLUDED = new Set(EXCLUDED_DIRS);

// main.py RESERVED_SLUGS: names a user's app may never take on the shared domain.
export const RESERVED_SLUGS = Object.freeze(new Set([
  "app", "www", "api", "admin", "demo", "healer", "traefik", "mail", "status", "docs",
]));

export const API_NAME_RE = /(api|backend|server|graphql|rest)/iu;
export const DEFAULT_MODEL = "gpt-5.1-codex-mini";   // platform/docker-compose.yml's AI_MODEL
export const BASE_DOMAIN = "cicatrixa.com";
const OPENAI_URL = "https://api.openai.com/v1/responses";
// A Vercel function gets 60 s. The AI must stop with room left to fall back to the
// heuristics and write the result to Turso, or the user is left with nothing.
const AI_BUDGET_MS = 60_000 - 8_000;
const ASK_TIMEOUT_MS = 180_000;                      // ai.py _ask: timeout=180

// ---------- Python semantics the port depends on ----------

// Python slices strings by code point; JS by UTF-16 unit, which would cut an emoji in
// half at the 4000/6000/40000-character limits and change the prompt.
function cut(s, n) {
  if (s.length <= n) return s;
  let i = 0;
  for (let count = 0; i < s.length && count < n; count++) {
    i += s.codePointAt(i) > 0xffff ? 2 : 1;
  }
  return s.slice(0, i);
}

// Python's sorted() orders str by code point; JS's default sort by UTF-16 unit puts
// astral characters (emoji) before U+E000-U+FFFF.
function byCodePoint(a, b) {
  let i = 0;
  while (i < a.length && i < b.length) {
    const x = a.codePointAt(i);
    const y = b.codePointAt(i);
    if (x !== y) return x - y;
    i += x > 0xffff ? 2 : 1;
  }
  return (a.length - i) - (b.length - i);
}

// The platform reads files with open() in text mode: universal newlines turn \r\n and
// a lone \r into \n before anything is cut or sent.
const pyText = (s) => s.replace(/\r\n?/g, "\n");

// Python truthiness: empty lists and dicts are false, which JS's are not.
function truthy(v) {
  if (Array.isArray(v)) return v.length > 0;
  if (v && typeof v === "object") return Object.keys(v).length > 0;
  return Boolean(v);
}

// list(v) for the values JSON can hold: a str iterates its characters, a dict its keys.
function pyList(v) {
  if (Array.isArray(v)) return v;
  if (typeof v === "string") return Array.from(v);
  if (v && typeof v === "object") return Object.keys(v);
  throw new TypeError(`${typeof v} is not iterable`);
}

const isDict = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

// str.strip(): Python's whitespace, which is not quite JS's \s (it has \x1c-\x1f and
// \x85, not ﻿).
const PY_WS = "[\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029" +
              "\\u202f\\u205f\\u3000]";
const PY_STRIP = new RegExp(`^${PY_WS}+|${PY_WS}+$`, "gu");
const FENCED = new RegExp("```(?:json)?" + PY_WS + "*(\\{[\\s\\S]*?\\})" + PY_WS + "*```", "u");

function pyStr(v) {
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  return typeof v === "number" ? String(v) : JSON.stringify(v);
}

// ---------- the file tree (engine._snapshot) ----------

function newDir() {
  return { dirs: new Map(), files: [] };
}

// engine._snapshot over the blob paths of a git tree. os.walk goes top-down, depth
// first; the platform keeps at most 20 subdirectories and 40 sorted files per
// directory, lists files only down to `a/b/file`, and stops once it has more than
// 400 lines. os.walk takes subdirectories in filesystem order, which is arbitrary;
// here they are alphabetical. A directory exists in a checkout when anything is
// under it, even if all of that is excluded (a/node_modules/x.js makes `a`), so it
// still takes one of the 20 places.
export function snapshotTree(paths) {
  const root = newDir();
  for (const path of paths) {
    const parts = path.split("/");
    let node = root;
    for (const seg of parts.slice(0, -1)) {
      let next = node.dirs.get(seg);
      if (!next) node.dirs.set(seg, next = newDir());
      node = next;
    }
    node.files.push(parts[parts.length - 1]);
  }
  const lines = [];
  let full = false;
  const walk = (node, prefix, depth) => {
    if (depth > 2) return;
    for (const f of node.files.slice().sort(byCodePoint).slice(0, 40)) lines.push(prefix + f);
    if (lines.length > 400) {
      full = true;
      return;
    }
    const subdirs = [...node.dirs.keys()].sort(byCodePoint).filter((d) => !EXCLUDED.has(d));
    for (const d of subdirs.slice(0, 20)) {
      walk(node.dirs.get(d), `${prefix}${d}/`, depth + 1);
      if (full) return;
    }
  };
  walk(root, "", 0);
  return lines.join("\n");
}

// The root of a repo as heuristicPlan sees it. `texts` maps root file names to their
// contents. Like os.path.exists, has() is also true for a directory of that name.
export function rootFiles(paths, texts = new Map()) {
  const names = new Set();
  for (const p of paths) {
    const slash = p.indexOf("/");
    names.add(slash === -1 ? p : p.slice(0, slash));
  }
  return {
    has: (name) => names.has(name),
    text: (name) => (texts.has(name) ? texts.get(name) : null),
  };
}

// ---------- the deterministic plan (ai.heuristic_plan) ----------

// `files` is {has(name), text(name)} over the repo root. Every Dockerfile string is
// the platform's, byte for byte. Where Python would crash on a package.json whose
// "scripts" or "dependencies" is not an object (null, a list), this treats it as {}.
export function heuristicPlan(files) {
  const has = (...names) => names.some((n) => files.has(n));

  if (has("package.json")) {
    let pkg;
    try {
      const raw = files.text("package.json");
      pkg = raw === null || raw === undefined ? {} : JSON.parse(raw);
    } catch {
      pkg = {};
    }
    if (!isDict(pkg)) pkg = {};
    const deps = isDict(pkg.dependencies) ? pkg.dependencies : {};
    const devDeps = isDict(pkg.devDependencies) ? pkg.devDependencies : {};
    const scripts = isDict(pkg.scripts) ? pkg.scripts : {};
    let df;
    if (Object.hasOwn(deps, "next") || Object.hasOwn(devDeps, "next")) {
      df = "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\n" +
           "COPY . .\nRUN npm run build\nEXPOSE 3000\nENV HOST=0.0.0.0 PORT=3000\n" +
           'CMD ["npx","next","start","-H","0.0.0.0","-p","3000"]\n';
    } else {
      const cmd = truthy(scripts.start) ? '["npm","start"]' : '["node","index.js"]';
      const buildLine = Object.hasOwn(scripts, "build") ? "RUN npm run build\n" : "";
      df = "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\nRUN npm install\n" +
           `COPY . .\n${buildLine}EXPOSE 3000\nENV HOST=0.0.0.0 PORT=3000\nCMD ${cmd}\n`;
    }
    return { dockerfile: df, port: 3000, health_path: "/", notes: "heuristic: node app" };
  }

  if (has("requirements.txt", "pyproject.toml")) {
    const req = has("requirements.txt")
      ? "COPY requirements.txt ./\nRUN pip install --no-cache-dir --prefer-binary -r requirements.txt\n"
      : "COPY pyproject.toml ./\nRUN pip install --no-cache-dir --prefer-binary .\n";
    const entry = ["main.py", "app.py", "server.py", "run.py"].find((f) => files.has(f)) || "main.py";
    const mod = entry.slice(0, -3);
    const df = "FROM python:3.12-slim\nWORKDIR /app\n" + req + "COPY . .\nEXPOSE 8000\n" +
               "RUN pip install --no-cache-dir uvicorn || true\n" +
               `CMD ["sh","-c","uvicorn ${mod}:app --host 0.0.0.0 --port 8000 || python ${entry}"]\n`;
    return { dockerfile: df, port: 8000, health_path: "/", notes: "heuristic: python app" };
  }

  if (has("go.mod")) {
    const df = "FROM golang:1.22-alpine AS build\nWORKDIR /src\nCOPY . .\n" +
               "RUN go build -o /bin/app ./...\nFROM alpine\nCOPY --from=build /bin/app /bin/app\n" +
               'EXPOSE 8080\nENV PORT=8080\nCMD ["/bin/app"]\n';
    return { dockerfile: df, port: 8080, health_path: "/", notes: "heuristic: go app" };
  }

  // default: static site
  const df = "FROM nginx:alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n";
  return { dockerfile: df, port: 80, health_path: "/", notes: "heuristic: static site" };
}

// ---------- the AI plan (ai.build_plan) ----------

// ai.py PLAN_INSTRUCTIONS, verbatim.
export const PLAN_INSTRUCTIONS = `You are the deployment engine of a hosting platform. Given a repository
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
- Python: uvicorn/gunicorn for ASGI/WSGI, \`python main.py\` style otherwise.
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
{"need_files": ["relative/path", ...]}   (max 8 files) — you will receive their contents.`;

// ai._arch_context. Contabo's VPS are x86, so the box this plan is for is amd64; the
// architecture Vercel's function runs on says nothing about it.
export function archContext(arch = "amd64") {
  return `Build host: linux/${arch}. Images are built and run on this architecture ` +
         "with no emulation. Never use --platform, never download binaries built for " +
         `another architecture, and prefer dependencies that ship ${arch} builds. ` +
         "'exec format error' in a log means something in the image targets the wrong " +
         "architecture.\n\n";
}

// ai._sibling_context. siblings: [{name, slug, port, public_url, env_key}].
export function siblingContext(siblings) {
  if (!truthy(siblings)) return "";
  const lines = siblings.map((s) =>
    `- ${s.name}: public ${s.public_url}, internal http://${s.slug}:` +
    `${truthy(s.port) ? s.port : "?"} (env keys ${s.env_key}_URL / ${s.env_key}_INTERNAL_URL)`);
  return "Sibling services in this project:\n" + lines.join("\n") + "\n\n";
}

// Numbers whose JSON text was not an integer literal ("port": 3000.0 or 3e3): Python's
// json gives a float for those, and build_plan refuses a plan whose port is not an int.
const nonIntegerPort = new WeakSet();

// ai._json_from: the first ```json fenced object, else everything from the first "{"
// to the last "}"; null when that is not JSON.
export function jsonFrom(text) {
  const m = FENCED.exec(text);
  let raw = m ? m[1] : null;
  if (raw === null) {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    if (start !== -1 && end > start) raw = text.slice(start, end + 1);
  }
  if (raw === null) return null;
  try {
    return JSON.parse(raw, function (key, value, context) {
      if (key === "port" && typeof value === "number" && context?.source !== undefined &&
          !/^-?\d+$/.test(context.source)) {
        nonIntegerPort.add(this);
      }
      return value;
    });
  } catch {
    return null;
  }
}

function hasIntPort(plan) {
  return Number.isInteger(plan.port) && !nonIntegerPort.has(plan);
}

// Rejects once the deadline passes even if fetchImpl ignores its signal.
function beforeDeadline(promise, ms, ctl) {
  let timer;
  const expired = new Promise((_, reject) => {
    timer = setTimeout(() => {
      ctl.abort();
      reject(new Error("AI deadline passed"));
    }, Math.max(0, ms));
  });
  return Promise.race([promise, expired]).finally(() => clearTimeout(timer));
}

// ai._ask: one Responses API call; the reply is every output_text/text part joined.
async function ask(opts, instructions, prompt, maxTokens = 4000) {
  const { apiKey, model, fetchImpl, deadlineMs, now } = opts;
  const left = Math.min(deadlineMs - now(), ASK_TIMEOUT_MS);
  if (!(left > 0)) throw new Error("AI deadline passed");
  const ctl = new AbortController();
  return beforeDeadline((async () => {
    const res = await fetchImpl(OPENAI_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, instructions, input: prompt, max_output_tokens: maxTokens }),
      signal: ctl.signal,
    });
    if (!res.ok) throw new Error(`OpenAI answered ${res.status}`);
    const data = await res.json();
    if (!isDict(data)) throw new TypeError("OpenAI reply is not an object");
    const output = data.output === undefined ? [] : data.output;
    if (!Array.isArray(output)) throw new TypeError("OpenAI output is not a list");
    const parts = [];
    for (const item of output) {
      if (!isDict(item)) throw new TypeError("OpenAI output item is not an object");
      const content = truthy(item.content) ? item.content : [];
      if (!Array.isArray(content)) throw new TypeError("OpenAI content is not a list");
      for (const c of content) {
        if (!isDict(c)) throw new TypeError("OpenAI content part is not an object");
        if (c.type === "output_text" || c.type === "text") {
          const text = c.text === undefined ? "" : c.text;
          if (typeof text !== "string") throw new TypeError("OpenAI text is not a string");
          parts.push(text);
        }
      }
    }
    return parts.join("\n").replace(PY_STRIP, "");
  })(), left, ctl);
}

// ai._ask_with_files: if the model asks for files, read them and ask again.
async function askWithFiles(opts, instructions, prompt, readFile, rounds = 2) {
  for (let round = 0; round < rounds; round++) {
    const data = jsonFrom(await ask(opts, instructions, prompt));
    const needed = data === null ? undefined : data.need_files;
    if (!truthy(needed) || !readFile || round === rounds - 1) return data;
    const wanted = pyList(needed).slice(0, 8);
    // Read in parallel (each is a GitHub request inside a 60 s budget), but log and
    // fail in request order, as the platform's one-by-one reads do.
    const reads = wanted.map((p) => Promise.resolve().then(() => readFile(p))
      .then((v) => ({ v }), (e) => ({ e })));
    const chunks = [];
    for (let i = 0; i < wanted.length; i++) {
      const { v, e } = await reads[i];
      if (e) throw e;
      const found = v !== null && v !== undefined;
      if (found) opts.log(`  🔎 AI reading ${wanted[i]}`);
      chunks.push(`--- ${wanted[i]} ---\n${found ? v : "(not found)"}`);
    }
    prompt += "\n\nRequested files:\n" + cut(chunks.join("\n"), 40000) +
              "\n\nNow reply with the full plan JSON only.";
  }
  return null;
}

function entriesOf(files) {
  if (!files) return [];
  if (files instanceof Map || Array.isArray(files)) return [...files];
  return Object.entries(files);
}

// ai.build_plan. `files` maps key-file names to their text in KEY_FILES order (a Map,
// or an object). `readFile(path)` resolves to the text (already cut) or null.
// `deadlineMs` is an absolute epoch time; the plan is abandoned when it passes.
// Returns the plan, or null for anything short of a plan with an integer port.
export async function aiPlan({ tree, files, hasDockerfile, siblings, readFile, apiKey,
                               model = DEFAULT_MODEL, arch = "amd64", deadlineMs = Infinity,
                               fetchImpl, log = () => {}, now = Date.now }) {
  if (!apiKey) return null;
  const blob = entriesOf(files).map(([p, c]) => `--- ${p} ---\n${cut(c, 4000)}`).join("\n");
  const prompt = archContext(arch) + siblingContext(siblings) +
                 `Repository file tree:\n${cut(tree, 6000)}\n\n` +
                 `Repo has Dockerfile: ${hasDockerfile ? "True" : "False"}\n\n` +
                 `Key files:\n${cut(blob, 40000)}`;
  const opts = { apiKey, model: model || DEFAULT_MODEL, deadlineMs, now, log,
                 fetchImpl: fetchImpl || ((...a) => globalThis.fetch(...a)) };
  try {
    const plan = await askWithFiles(opts, PLAN_INSTRUCTIONS, prompt, readFile);
    if (truthy(plan) && isDict(plan) && hasIntPort(plan)) return plan;
  } catch {
    // build_plan swallows every failure: the caller falls back to the heuristics.
  }
  return null;
}

// ---------- names (engine.py, main.py) ----------

export function envKey(name) {
  return String(name).replace(/[^A-Za-z0-9]+/gu, "_").toUpperCase().replace(/^_+|_+$/g, "") ||
         "SERVICE";
}

// Lowercased first because Python's re.I also matches "İ" (U+0130) to "i", which
// JS's case folding does not.
export function isApiName(name) {
  return API_NAME_RE.test(String(name || "").toLowerCase());
}

// main._service_name: the repo's name, as a slug, at most 24 characters.
export function serviceName(repo) {
  const last = String(repo).split("/").pop();
  return last.toLowerCase().replace(/[^a-z0-9-]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 24) ||
         "app";
}

// main.make_slug. The cut comes after the strip, so a name can end in "-"; the first
// suffix is -2. `isTaken(slug)` may be async: it must cover projects and services.
export async function makeSlug(name, isTaken = async () => false) {
  const base = String(name).toLowerCase().replace(/[^a-z0-9-]+/gu, "-")
    .replace(/^-+|-+$/g, "").slice(0, 40) || "app";
  let slug = base;
  for (let n = 2; RESERVED_SLUGS.has(slug) || await isTaken(slug); n++) slug = `${base}-${n}`;
  return slug;
}

export function serviceURL(slug) {
  return `https://${slug}.${BASE_DOMAIN}`;
}

// ---------- one service's analysis (engine._run_pipeline, up to the build) ----------

// engine._make_reader's confinement: the path is taken relative to the repo root, and
// anything that leaves it is not read. Python raises on a NUL byte (os.path), which
// ends the AI plan; so does this.
function insideRepo(relpath) {
  if (relpath.includes("\0")) throw new Error("embedded null byte");
  const out = [];
  for (const seg of relpath.replace(/^\/+/, "").split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      if (!out.length) return null;
      out.pop();
      continue;
    }
    out.push(seg);
  }
  return out.length ? out.join("/") : null;
}

// Everything the platform does between "cloning" and "build attempt 1/3", through the
// GitHub API: gh is {repoInfo, branchHead, getTree, readFile} (workspace/github.js).
// One difference: a file over MAX_FILE_BYTES is not read (logged "skipped … (over
// 1 MB)") and counts as not found, where the platform reads its clone whole. Its size
// comes from the tree when GitHub gives one; otherwise readFile refuses it (status 413).
// log(line) gets the platform's lines without the time stamp. `now` is the time the
// request started (epoch ms) or a clock; the AI is given until 52 s after it unless
// deadlineMs says otherwise. `name` is the service's name (default: from the repo).
// Returns {sha, branch, plan, usedAI}; throws when GitHub cannot be read.
export async function analyze({ token, repoFull, branch, siblings = [], env = process.env, gh,
                                log = () => {}, now = Date.now, name, deadlineMs, fetchImpl }) {
  const clock = typeof now === "function" ? now : Date.now;
  const startedAt = typeof now === "number" ? now : clock();
  const deadline = deadlineMs ?? startedAt + AI_BUDGET_MS;
  const serviceNameOf = name || serviceName(repoFull);

  if (!branch) branch = (await gh.repoInfo(token, repoFull)).default_branch || "main";
  log(`⇣ reading ${repoFull}@${branch}`);
  const sha = await gh.branchHead(token, repoFull, branch);
  log(`  at commit ${String(sha).slice(0, 10)}`);

  const { paths, sizes, truncated } = await gh.getTree(token, repoFull, sha);
  const blobs = new Set(paths);
  const tree = snapshotTree(paths);
  const view = rootFiles(paths);

  // A file at the commit, or null when it is not there or too big to read; SKIPPED
  // when it was too big, so the caller can say so.
  const SKIPPED = Symbol("skipped");
  const tooBig = (path) => sizes instanceof Map && sizes.get(path) > MAX_FILE_BYTES;
  const readAt = async (path) => {
    if (tooBig(path)) return SKIPPED;
    try {
      return await gh.readFile(token, repoFull, path, sha);
    } catch (err) {
      if (err?.status === 413) return SKIPPED;
      throw err;
    }
  };
  const skipped = (path) => log(`  skipped ${path} (over ${MAX_FILE_BYTES / 1024 / 1024} MB)`);

  // Key files that exist at the root, read at the commit. GitHub cuts the recursive
  // tree of a huge repo short, and then a root file may be missing from it: ask for
  // the ones the plan depends on directly.
  const texts = new Map();
  const probe = truncated ? [...KEY_FILES, "run.py"].filter((n) => !view.has(n)) : [];
  const reads = [...KEY_FILES.filter((n) => blobs.has(n)), ...probe];
  const contents = await Promise.all(reads.map(readAt));
  reads.forEach((n, i) => {
    if (contents[i] === SKIPPED) {
      // It is in the repo, so has() stays true, as on the platform; its text is unread.
      skipped(n);
      if (probe.includes(n)) blobs.add(n);
      return;
    }
    if (contents[i] === null || contents[i] === undefined) return;
    texts.set(n, pyText(contents[i]));
    blobs.add(n);
  });
  const files = rootFiles([...blobs], texts);
  const keyFiles = new Map();
  for (const n of KEY_FILES) if (texts.has(n)) keyFiles.set(n, cut(texts.get(n), 6000));
  const hasDockerfile = files.has("Dockerfile");

  // engine._make_reader: what the model asks for with need_files, first 8000 chars.
  const readFile = async (relpath) => {
    if (typeof relpath !== "string") throw new TypeError("need_files lists a non-path");
    const rel = insideRepo(relpath);
    if (rel === null) return null;
    if (!blobs.has(rel)) {
      // Not a file in the tree: a directory, or nothing. Only a cut-short tree can
      // be missing a real file.
      if (!truncated || paths.some((p) => p.startsWith(`${rel}/`))) return null;
    }
    const text = await readAt(rel);
    if (text === SKIPPED) {
      skipped(rel);
      return null;
    }
    return text === null || text === undefined ? null : cut(pyText(text), 8000);
  };

  const apiKey = env.OPENAI_API_KEY || "";
  const aiOn = Boolean(apiKey);
  log("🧠 analyzing repository" + (aiOn ? " (AI)" : " (heuristics)"));
  let plan = null;
  if (aiOn) {
    plan = await aiPlan({ tree, files: keyFiles, hasDockerfile, siblings, readFile, apiKey,
                          model: env.AI_MODEL || DEFAULT_MODEL, arch: "amd64",
                          deadlineMs: deadline, fetchImpl, log, now: clock });
    if (!plan) log("  AI plan unavailable — using heuristics");
  }
  const usedAI = Boolean(plan);
  if (!plan) plan = heuristicPlan(files);

  if (hasDockerfile && !truthy(plan.dockerfile)) {
    plan.dockerfile = null;
    log("  using repo's own Dockerfile");
  }
  if (truthy(plan.notes)) log(`  plan: ${pyStr(plan.notes)} (port ${pyStr(plan.port)})`);
  // bridge default when heuristics only: name says this is the project's API
  if ((plan.api_prefixes === undefined || plan.api_prefixes === null) && truthy(siblings) &&
      isApiName(serviceNameOf) && !aiOn) {
    plan.api_prefixes = ["/api"];
  }
  const prefixes = plan.api_prefixes;
  let list = [];
  if (truthy(prefixes)) {
    try { list = pyList(prefixes); } catch { list = []; }   // a number: Python would crash
  }
  plan.api_prefixes = list.filter((p) => typeof p === "string" && p.startsWith("/"));
  if (plan.api_prefixes.length) {
    log(`  api bridge: ${plan.api_prefixes.join(", ")} will be served on sibling domains`);
  }
  return { sha, branch, plan, usedAI };
}
