// workspace/plan.js against the platform's own Python (platform/control/app/ai.py,
// engine.py, main.py): the same repos must give the same tree, prompt, plan and log,
// byte for byte. The Python side runs the real engine._run_pipeline up to the build,
// with git, the database and OpenAI replaced; the JS side reads the same directories
// through a fake GitHub. No network. The parity half needs a python3 (>= 3.10); set
// CX_PYTHON to pick one (then it must work), otherwise it is skipped when python3 is
// missing. The rest runs everywhere.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync,
         writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  API_NAME_RE, EXCLUDED_DIRS, KEY_FILES, PLAN_INSTRUCTIONS, RESERVED_SLUGS, aiPlan, analyze,
  archContext, envKey, heuristicPlan, isApiName, jsonFrom, makeSlug, rootFiles, serviceName,
  serviceURL, siblingContext, snapshotTree,
} from "../plan.js";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CONTROL = join(REPO, "platform", "control");
const SHA = "0123456789abcdef0123456789abcdef01234567";
const work = mkdtempSync(join(tmpdir(), "cx-plan-"));
after(() => rmSync(work, { recursive: true, force: true }));

// ---------- fixture repos ----------

function writeRepo(name, files) {
  const dir = join(work, "repos", name);
  for (const [path, content] of Object.entries(files)) {
    const full = join(dir, ...path.split("/"));
    mkdirSync(dirname(full), { recursive: true });
    writeFileSync(full, content);
  }
  return dir;
}

// What GitHub's recursive tree lists: every blob, committed node_modules included.
function listFiles(dir, prefix = "") {
  const out = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix + e.name;
    if (e.isDirectory()) out.push(...listFiles(join(dir, e.name), `${rel}/`));
    else out.push(rel);
  }
  return out;
}

// workspace/github.js's shape, served from a directory. `sizes` is what the tree
// reports ({path: bytes}); `refuse` lists paths readFile refuses as over the cap (413),
// as github.js does for a file whose size the tree did not give.
function fixtureGh(dir, { truncated = false, hide = [], calls = [], sizes, refuse = [] } = {}) {
  return {
    async repoInfo(token, full) {
      calls.push(["repoInfo", full]);
      return { full_name: full, default_branch: "trunk", private: false };
    },
    async branchHead(token, full, branch) {
      calls.push(["branchHead", full, branch]);
      return SHA;
    },
    async getTree(token, full, sha) {
      calls.push(["getTree", full, sha]);
      const paths = listFiles(dir).filter((p) => !hide.includes(p));
      return sizes ? { paths, truncated, sizes: new Map(Object.entries(sizes)) }
        : { paths, truncated };
    },
    async readFile(token, full, path, ref) {
      calls.push(["readFile", path, ref]);
      if (refuse.includes(path)) throw Object.assign(new Error(`${path} is over 1 MB`), { status: 413 });
      const p = join(dir, ...path.split("/"));
      try {
        if (!statSync(p).isFile()) return null;
      } catch {
        return null;
      }
      // Python's open() keeps a byte-order mark; so does this.
      return new TextDecoder("utf-8", { ignoreBOM: true }).decode(readFileSync(p));
    },
  };
}

const pkg = (o) => JSON.stringify(o, null, 2) + "\n";
const lines = (n, word) => Array.from({ length: n }, (_, i) => `${word} ${i}`).join("\r\n");
// A key file whose 4000th character is an emoji (a surrogate pair in JS), with CRLF
// line ends that the platform's text-mode read turns into \n before counting.
const longFile = (tag) => `${tag}\r\n` + "x".repeat(3995 - tag.length) + "😀" + "é\r" +
                          lines(200, tag);

const D = {};
D.nodeNext = writeRepo("node-next", {
  "package.json": pkg({ name: "web", scripts: { dev: "next dev", build: "next build", start: "next start" },
                        dependencies: { next: "14.2.0", react: "18.3.1" } }),
  "next.config.js": "module.exports = {};\n",
  "pages/index.js": "export default () => 'hi';\n",
  "public/favicon.ico": "ico",
});
D.nodeDevNext = writeRepo("node-dev-next", {
  "package.json": pkg({ name: "x", devDependencies: { next: "14" } }),
});
D.nodeStartBuild = writeRepo("node-start-build", {
  "package.json": pkg({ scripts: { start: "node server.js", build: "tsc" }, dependencies: { express: "4" } }),
  "server.js": "require('express')().listen(process.env.PORT);\n",
  "src/index.ts": "export {};\n",
});
D.nodePlain = writeRepo("node-plain", {
  "package.json": pkg({ name: "plain", dependencies: { express: "4" } }),
  "index.js": "console.log('hi');\n",
});
D.nodeEmptyStart = writeRepo("node-empty-start", {
  "package.json": pkg({ scripts: { start: "", build: "vite build" } }),
});
D.nodeBadJson = writeRepo("node-bad-json", {
  "package.json": '{ "name": broken,\n',
  "index.js": "",
});
D.nodeCrlf = writeRepo("node-crlf", {
  "package.json": '{\r\n  "scripts": {\r\n    "start": "node app.js"\r\n  }\r\n}\r\n',
  "app.js": "a\rb\r\nc\n",
});
D.pyReqMain = writeRepo("py-req-main", { "requirements.txt": "fastapi\n", "main.py": "app = None\n" });
D.pyProjectApp = writeRepo("py-pyproject-app", { "pyproject.toml": "[project]\nname='x'\n", "app.py": "" });
D.pyReqServerRun = writeRepo("py-req-server-run", {
  "requirements.txt": "flask\n", "server.py": "", "run.py": "",
});
D.pyProjectRun = writeRepo("py-pyproject-run", { "pyproject.toml": "", "run.py": "" });
D.pyReqOnly = writeRepo("py-req-only", { "requirements.txt": "" });
D.go = writeRepo("go", { "go.mod": "module x\n", "main.go": "package main\n", "cmd/tool/main.go": "" });
D.static = writeRepo("static", {
  "index.html": "<!doctype html><title>hi</title>\n", "css/site.css": "body{}\n", "js/app.js": "",
});
D.ownDockerfile = writeRepo("own-dockerfile", {
  "Dockerfile": "FROM nginx:alpine\nEXPOSE 8080\n", "index.html": "<p>hi</p>\n",
  "docker-compose.yml": "services: {}\n",
});
// os.path.exists is true for a directory too, so the platform takes this for Go.
D.dirGoMod = writeRepo("dir-named-go-mod", { "go.mod/README": "not a module\n", "index.html": "" });
D.deep = writeRepo("deep", {
  "root.txt": "root\n", "a/a.txt": "a\n", "a/b/b.txt": "bee\r\nline2\n", "a/b/c/c.txt": "c\n",
  "a/b/c/d/deep.txt": "d\n", "node_modules/x/index.js": "module.exports = 1;\n",
  "a/node_modules/y.js": "", "src/__pycache__/m.pyc": "", "src/m.py": "", ".next/cache/x": "",
  "venv/bin/python": "", ".venv/lib/x": "", "target/debug/app": "", "z/target/x": "",
  "z/z.txt": "", "only-excluded/node_modules/q.js": "",
});
{
  const files = { "README.md": "many\n" };
  const names = ["Zeta", "_under", "-dash", "10", "9", "a b", "émile", "ｆull", "😀moji", "~t"];
  for (let i = 0; i < 45; i++) files[`lib/f${String(i).padStart(2, "0")}.js`] = "";
  for (const n of names) files[`lib/${n}.js`] = "";
  D.manyFiles = writeRepo("many-files", files);
}
{
  const files = { "index.html": "" };
  const dirs = [".next", ".venv", "__pycache__", "Zeta", "node_modules", "target", "venv"];
  for (let i = 0; i < 25; i++) dirs.push(`d${String(i).padStart(2, "0")}`);
  for (const d of dirs) files[`${d}/file.txt`] = "";
  files["d00/inner/y.txt"] = "";
  files["d00/inner/deeper/x.txt"] = "";
  D.manySubdirs = writeRepo("many-subdirs", files);
}
{
  const files = { "a.txt": "", "b.txt": "", "c.txt": "", "d.txt": "", "e.txt": "" };
  for (let d = 0; d < 15; d++) {
    for (let f = 0; f < 30; f++) files[`p${String(d).padStart(2, "0")}/file-number-${f}.txt`] = "";
  }
  for (let f = 0; f < 10; f++) files[`p00/sub/s${f}.txt`] = "";
  D.huge = writeRepo("huge", files);
}
{
  const files = {};
  for (const n of ["requirements.txt", "pyproject.toml", "Gemfile", "Procfile", "main.py", "app.py",
                   "server.py", "README.md", "Makefile", "index.html", "docker-compose.yml",
                   "vite.config.ts"]) {
    files[n] = longFile(n);
  }
  D.keyFilesLong = writeRepo("key-files-long", files);
}
D.unicode = writeRepo("unicode", {
  "é.txt": "", "z.txt": "", "Z.txt": "", "😀.txt": "", "ｆ.txt": "", "_x": "", "-y": "",
  "README.md": Buffer.concat([Buffer.from("bad bytes: "),
    Buffer.from([0xff, 0xfe, 0xc3, 0x28, 0xe2, 0x82, 0x41, 0xed, 0xa0, 0x80, 0xc0, 0xaf]),
    Buffer.from("\n﻿mid-file mark\n")]),
  "index.js": "﻿console.log(1)\n",
});

// ---------- the cases ----------

const SIB_WEB = { name: "web", slug: "shop", port: 3000, public_url: "https://shop.cicatrixa.com",
                  env_key: "WEB" };
const SIB_DB = { name: "worker", slug: "shop-worker", port: null,
                 public_url: "https://shop-worker.cicatrixa.com", env_key: "WORKER" };
const fence = (o) => "```json\n" + JSON.stringify(o, null, 1) + "\n```";
const PLAN_NEXT = fence({ dockerfile: "FROM node:20-slim\nCMD [\"npm\",\"start\"]\n", port: 3000,
                          health_path: "/api/health", build_args: { VITE_API_URL: "https://shop-api.cicatrixa.com" },
                          api_prefixes: null, strip_prefix: false, notes: "next app" });
const NEED = JSON.stringify({ need_files: ["a/b/b.txt", "missing.txt", "../etc/passwd", "/root.txt", "a/",
                                           "a/b/../a.txt", "node_modules/x/index.js", "./z/z.txt",
                                           "ninth-is-never-read"] });

const pipelines = [];
for (const [key, dir] of Object.entries(D)) {
  pipelines.push({ id: `heuristics ${key}`, dir, repo: `acme/${key}`, branch: "main" });
}
pipelines.push(
  { id: "heuristics api bridge default", dir: D.nodePlain, repo: "acme/orders-api", branch: "main",
    siblings: [SIB_WEB, SIB_DB] },
  { id: "heuristics api name without siblings", dir: D.nodePlain, repo: "acme/orders-api", branch: "dev" },
  { id: "heuristics siblings, not an api", dir: D.go, repo: "acme/Front.End", branch: "main",
    siblings: [SIB_WEB] },
  { id: "ai plan", dir: D.nodeNext, repo: "acme/web", branch: "main", siblings: [SIB_WEB, SIB_DB],
    ai: true, replies: [PLAN_NEXT] },
  { id: "ai need_files", dir: D.deep, repo: "acme/deep", branch: "main", ai: true,
    replies: [NEED, 'Sure.\n{"port": 5000, "dockerfile": null, "api_prefixes": "/api", "notes": "deep"}\nok'] },
  { id: "ai own Dockerfile", dir: D.ownDockerfile, repo: "acme/shop-backend", branch: "main", ai: true,
    siblings: [SIB_WEB],
    replies: ['{"dockerfile": null, "port": 8080, "health_path": "/", "api_prefixes": ["/api", "v1", 3, "/graphql"], "notes": "its own"}'] },
  { id: "ai empty dockerfile string", dir: D.ownDockerfile, repo: "acme/x", branch: "main", ai: true,
    replies: ['{"dockerfile": "", "port": 80}'] },
  { id: "ai float port", dir: D.pyReqMain, repo: "acme/py", branch: "main", ai: true,
    replies: ['{"port": 8000.0, "dockerfile": "FROM x\\n", "notes": "n"}'] },
  { id: "ai string port", dir: D.pyReqMain, repo: "acme/py", branch: "main", ai: true,
    replies: ['{"port": "8000", "notes": "n"}'] },
  { id: "ai asks twice", dir: D.deep, repo: "acme/deep", branch: "main", ai: true, replies: [NEED, NEED] },
  { id: "ai garbage", dir: D.static, repo: "acme/s", branch: "main", ai: true,
    replies: ["I can't help with that."] },
  { id: "ai http error", dir: D.static, repo: "acme/s", branch: "main", ai: true, replies: [null] },
  { id: "ai api name, no bridge default", dir: D.go, repo: "acme/orders-api", branch: "main", ai: true,
    siblings: [SIB_WEB], replies: ['{"port": 8080, "dockerfile": "FROM golang\\n", "notes": "go api"}'] },
  { id: "ai need_files string", dir: D.static, repo: "acme/s", branch: "main", ai: true,
    replies: ['{"need_files": "index.html"}', '{"port": 80, "dockerfile": null, "notes": "static"}'] },
  { id: "ai need_files dict", dir: D.static, repo: "acme/s", branch: "main", ai: true,
    replies: ['{"need_files": {"index.html": 1, "css/site.css": 2}}', '{"port": 80, "notes": ""}'] },
  { id: "ai need_files non-string", dir: D.static, repo: "acme/s", branch: "main", ai: true,
    replies: ['{"need_files": ["index.html", 7, "js/app.js"]}', '{"port": 80}'] },
  { id: "ai empty need_files", dir: D.static, repo: "acme/s", branch: "main", ai: true,
    replies: ['{"need_files": [], "port": 81, "notes": "x"}'] },
  { id: "ai long key files", dir: D.keyFilesLong, repo: "acme/long", branch: "main", ai: true,
    replies: ['{"port": 8000, "dockerfile": "FROM python\\n", "notes": "long"}'] },
  { id: "ai huge tree", dir: D.huge, repo: "acme/huge", branch: "main", ai: true,
    replies: ['{"port": 80, "dockerfile": "FROM nginx\\n", "notes": "huge"}'] },
  { id: "ai unicode", dir: D.unicode, repo: "acme/u", branch: "main", ai: true,
    replies: ['{"need_files": ["README.md", "😀.txt"]}', '{"port": 3000, "notes": "ünï 😀"}'] },
);

const JSON_TEXTS = [
  '```json\n{"port": 1}\n```', '```\n{"a": {"b": 1}}\n```', 'prefix {"port": 3} suffix }',
  "no json here", '```json\n{"port": 1}\n```\n```json\n{"port": 2}\n```',
  '```json  {"x": 1} ```', '```json\x1c{"x": 1}\x1c```\n{"y": 2}',
  '```json﻿{"x": 1}﻿```\n{"y": 2}', '{"port": 3000.0}', '{"a": 1, "a": 2}',
  '{"need_files": ["x"]} trailing {', "}{", "", '```json\n[1, 2]\n```\n{"z": 1}',
];
const PORT_TEXTS = ['{"port": 3000}', '{"port": 3000.0}', '{"port": 3e3}', '{"port": "3000"}',
                    '{"port": -1}', '{"port": 0}', "{}", '{"port": 12345678901234567890}',
                    '{"port": null}', '{"port": [3000]}', '{"port": 3000, "port": 3000.5}'];
const SLUG_NAMES = ["My App", "  --Hello--World--  ", "", "---", "app", "API", "x".repeat(50),
                    `${"a".repeat(39)}-b`, `${"a".repeat(39)}--b`, "İstanbul Café", "Ünïcødé ☃ 😀 rocket",
                    "foo_bar.baz", "Kelvin", "admin", "docs", "Straße", "ǅemo", "a--b"];
const SLUG_TAKEN = [[], ["my-app", "my-app-2"], ["app-2"], ["admin-2", "admin-3"]];
const REPOS = ["acme/My.Repo", "acme/", "acme/---", "Org/Very-Long-Repository-Name-That-Exceeds",
               "single", "a/b/c", "acme/repo_with_underscores", "acme/İ", "acme/ﬁle"];
const KEY_NAMES = ["web", "orders-api", "my.service 2", "---", "", "ünï", "a__b", "x9"];
const API_NAMES = ["orders-api", "web", "BACKEND", "graphql-gw", "rest", "Server", "frontend", "reſt",
                   "apİ", "", "capital"];
const SIBLING_SETS = [[], [SIB_WEB], [SIB_WEB, SIB_DB, { ...SIB_DB, port: 0, name: "z" }]];

// ---------- the Python side ----------

// One process runs every case and prints the results as JSON.
const HARNESS = String.raw`
import ast, inspect, json, os, re, shutil, subprocess, sys, tempfile, types

CONTROL = sys.argv[1]
sys.path.insert(0, CONTROL)
WORK = tempfile.mkdtemp(prefix="cx-plan-parity-")
os.environ["WORK_ROOT"] = os.path.join(WORK, "work")
os.environ["DB_PATH"] = os.path.join(WORK, "unused.db")
for k in ("OPENAI_API_KEY", "AI_MODEL", "BASE_DOMAIN", "BASE_URL"):
    os.environ.pop(k, None)

# ai.py and engine.py are what is under test; third-party modules they import at the
# top are stubbed when this python lacks them (none is called here).
def stub(name, **attrs):
    try:
        __import__(name)
    except ImportError:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

stub("httpx")
stub("jwt")
stub("docker", DockerClient=type("DockerClient", (), {}))

import app  # noqa: E402
metrics = types.ModuleType("app.metrics")
metrics.check_deploy_quota = lambda owner, service: None
sys.modules["app.metrics"] = metrics
app.metrics = metrics
from app import ai, engine  # noqa: E402
ai.HOST_ARCH = "amd64"

# os.walk takes subdirectories in filesystem order; the port takes them alphabetically.
_walk = os.walk
def sorted_walk(top, *a, **k):
    for dirpath, dirnames, filenames in _walk(top, *a, **k):
        dirnames.sort()
        yield dirpath, dirnames, filenames
os.walk = sorted_walk

# main.py needs FastAPI; its slug helpers do not, so they are lifted out of its source.
tree = ast.parse(open(os.path.join(CONTROL, "app", "main.py"), encoding="utf-8").read())
picked = [n for n in tree.body
          if (isinstance(n, ast.FunctionDef) and n.name in ("make_slug", "_service_name"))
          or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "RESERVED_SLUGS"
                                               for t in n.targets))]
assert len(picked) == 3, picked
TAKEN = set()
class FakeDB:
    @staticmethod
    def one(sql, params=()):
        return {"x": 1} if params and params[0] in TAKEN else None
main = {"re": re, "db": FakeDB}
exec(compile(ast.Module(body=picked, type_ignores=[]), "main.py", "exec"), main)

excluded = ast.literal_eval(re.search(r"d not in (\([^)]*\))", inspect.getsource(engine._snapshot)).group(1))

SHA = "0123456789abcdef0123456789abcdef01234567"

class Stop(Exception):
    pass

def pipeline(job):
    fixture = job["dir"]
    replies = list(job.get("replies") or [])
    asks = []
    ai.OPENAI_API_KEY = "sk-test" if job.get("ai") else ""

    def fake_ask(instructions, prompt, max_tokens=4000):
        asks.append({"instructions": instructions, "prompt": prompt, "max_tokens": max_tokens})
        reply = replies.pop(0)
        if reply is None:
            raise RuntimeError("HTTP 500")
        return reply

    def fake_run(args, **kwargs):
        if args[:2] == ["git", "clone"]:
            shutil.copytree(fixture, args[-1], dirs_exist_ok=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
        raise AssertionError(args)

    captured = {}
    def capture(service, siblings, plan):
        captured["plan"] = json.loads(json.dumps(plan))
        raise Stop()

    ai._ask = fake_ask
    engine.subprocess = types.SimpleNamespace(run=fake_run)
    engine.db = types.SimpleNamespace(one=lambda sql, params=(): {"id": 1, "kind": "app"},
                                      q=lambda *a, **k: None)
    engine.gh = types.SimpleNamespace(clone_url=lambda connection, repo: fixture)
    engine._sibling_info = lambda service: job.get("siblings") or []
    engine._build_args = capture
    log = []
    service = {"id": 1, "project_id": 1, "name": main["_service_name"](job["repo"]), "slug": "svc",
               "repo_full": job["repo"], "branch": job["branch"]}
    try:
        engine._run_pipeline(service, {"id": 1, "user_id": 1, "slug": "svc"}, 1, log.append)
    except Stop:
        pass
    return {"log": log, "plan": captured.get("plan"), "asks": asks}

def build_plan(text):
    ai.OPENAI_API_KEY = "sk-test"
    ai._ask = lambda instructions, prompt, max_tokens=4000: text
    return ai.build_plan("tree", {}, False) is not None

def run(job):
    kind = job["kind"]
    if kind == "consts":
        return {"PLAN_INSTRUCTIONS": ai.PLAN_INSTRUCTIONS, "KEY_FILES": engine.KEY_FILES,
                "EXCLUDED_DIRS": list(excluded), "RESERVED_SLUGS": sorted(main["RESERVED_SLUGS"]),
                "arch": ai._arch_context(), "MODEL": ai.MODEL, "API_NAME_RE": engine.API_NAME_RE.pattern}
    if kind == "pipeline":
        return pipeline(job)
    if kind == "snapshot":
        tree, files = engine._snapshot(job["dir"])
        return {"tree": tree, "files": files}
    if kind == "heuristic":
        return ai.heuristic_plan(job["dir"])
    if kind == "json_from":
        return ai._json_from(job["text"])
    if kind == "build_plan":
        return build_plan(job["text"])
    if kind == "sibling_context":
        return ai._sibling_context(job["siblings"])
    if kind == "make_slug":
        TAKEN.clear()
        TAKEN.update(job["taken"])
        return main["make_slug"](job["name"])
    if kind == "service_name":
        return main["_service_name"](job["repo"])
    if kind == "env_key":
        return engine._env_key(job["name"])
    if kind == "is_api":
        return engine._is_api_name(job["name"])
    raise ValueError(kind)

out = []
for job in json.load(sys.stdin):
    try:
        out.append({"ok": run(job)})
    except Exception as exc:
        out.append({"error": f"{type(exc).__name__}: {exc}"})
shutil.rmtree(WORK, ignore_errors=True)
json.dump(out, sys.stdout)
`;

const jobs = [{ kind: "consts" }];
const at = (job) => jobs.push(job) - 1;
const idx = {
  pipelines: pipelines.map((p) => at({ kind: "pipeline", ...p })),
  snapshots: Object.values(D).map((dir) => at({ kind: "snapshot", dir })),
  heuristics: Object.values(D).map((dir) => at({ kind: "heuristic", dir })),
  json: JSON_TEXTS.map((text) => at({ kind: "json_from", text })),
  ports: PORT_TEXTS.map((text) => at({ kind: "build_plan", text })),
  siblings: SIBLING_SETS.map((siblings) => at({ kind: "sibling_context", siblings })),
  slugs: SLUG_TAKEN.flatMap((taken) => SLUG_NAMES.map((name) => [name, taken,
                                                                 at({ kind: "make_slug", name, taken })])),
  names: REPOS.map((repo) => at({ kind: "service_name", repo })),
  keys: KEY_NAMES.map((name) => at({ kind: "env_key", name })),
  apis: API_NAMES.map((name) => at({ kind: "is_api", name })),
};

function runPython() {
  const python = process.env.CX_PYTHON || "python3";
  const script = join(work, "harness.py");
  writeFileSync(script, HARNESS);
  // -I: nothing from the environment or the current directory; -B: no .pyc files in
  // the repo; -X utf8: open() reads UTF-8 whatever the locale, as on the box.
  const r = spawnSync(python, ["-I", "-B", "-X", "utf8", script, CONTROL],
                      { input: JSON.stringify(jobs), encoding: "utf8", maxBuffer: 256 << 20 });
  const why = r.error ? r.error.message : r.status !== 0 ? r.stderr.trim().split("\n").pop() : "";
  if (why) {
    if (process.env.CX_PYTHON) {
      rmSync(work, { recursive: true, force: true });
      throw new Error(`CX_PYTHON=${python} cannot run the harness: ${why}`);
    }
    return { skip: `no usable python3 for the parity checks (${why})` };
  }
  return { results: JSON.parse(r.stdout) };
}

const { results, skip } = runPython();
const py = (i) => {
  const r = results[i];
  if (r.error) throw new Error(`python: ${r.error}`);
  return r.ok;
};

// ---------- the JS side of a pipeline case ----------

function fakeOpenAI(replies, bodies = []) {
  const queue = [...replies];
  const fetchImpl = async (url, init) => {
    assert.equal(String(url), "https://api.openai.com/v1/responses");
    bodies.push({ ...JSON.parse(init.body), authorization: init.headers.Authorization });
    const reply = queue.shift();
    if (reply === null || reply === undefined) return new Response("{}", { status: 500 });
    return Response.json({ output: [{ type: "reasoning", content: null },
                                     { type: "message", content: [{ type: "output_text", text: reply }] }] });
  };
  return { fetchImpl, bodies };
}

async function jsPipeline(job) {
  const { fetchImpl, bodies } = fakeOpenAI(job.replies || []);
  const log = [];
  const result = await analyze({
    token: "ghs_test", repoFull: job.repo, branch: job.branch, siblings: job.siblings || [],
    env: job.ai ? { OPENAI_API_KEY: "sk-test" } : {}, gh: fixtureGh(job.dir),
    log: (line) => log.push(line), now: Date.now(), fetchImpl,
  });
  return { log, bodies, ...result };
}

const FALLBACK = "  AI plan unavailable — using heuristics";

// ---------- parity ----------

test("constants are the platform's", { skip }, () => {
  const c = py(0);
  assert.equal(PLAN_INSTRUCTIONS, c.PLAN_INSTRUCTIONS);
  assert.deepEqual([...KEY_FILES], c.KEY_FILES);
  assert.deepEqual([...EXCLUDED_DIRS], c.EXCLUDED_DIRS);
  assert.deepEqual([...RESERVED_SLUGS].sort(), c.RESERVED_SLUGS);
  assert.equal(archContext(), c.arch);
  assert.equal(API_NAME_RE.source, c.API_NAME_RE);
  assert.equal("gpt-5.1-codex-mini", c.MODEL);
});

test("snapshot tree matches engine._snapshot byte for byte", { skip }, () => {
  Object.values(D).forEach((dir, i) => {
    assert.equal(snapshotTree(listFiles(dir)), py(idx.snapshots[i]).tree, dir);
  });
  // The walk did hit every limit it has.
  const huge = py(idx.snapshots[Object.keys(D).indexOf("huge")]).tree.split("\n");
  assert.ok(huge.length > 400 && huge.length < 450, String(huge.length));
  const many = py(idx.snapshots[Object.keys(D).indexOf("manyFiles")]).tree.split("\n");
  assert.equal(many.filter((l) => l.startsWith("lib/")).length, 40);
  const subdirs = py(idx.snapshots[Object.keys(D).indexOf("manySubdirs")]).tree;
  assert.ok(subdirs.includes("Zeta/file.txt") && subdirs.includes("d18/file.txt") &&
            !subdirs.includes("d19/") && subdirs.includes("d00/inner/y.txt") &&
            !subdirs.includes("deeper"));
});

test("heuristic plans match ai.heuristic_plan", { skip }, () => {
  Object.values(D).forEach((dir, i) => {
    const paths = listFiles(dir);
    const texts = new Map();
    if (paths.includes("package.json")) texts.set("package.json", readFileSync(join(dir, "package.json"), "utf8"));
    assert.deepEqual(heuristicPlan(rootFiles(paths, texts)), py(idx.heuristics[i]), dir);
  });
});

for (const [n, job] of pipelines.entries()) {
  test(`pipeline parity: ${job.id}`, { skip }, async () => {
    const want = py(idx.pipelines[n]);
    const got = await jsPipeline(job);
    // The platform clones; this reads through the API and says so.
    const wantLog = want.log.map((l) => l.replace(/^⇣ cloning /, "⇣ reading "));
    const viaHeuristics = want.plan.notes?.startsWith?.("heuristic:") ?? false;
    assert.equal(got.log.includes(FALLBACK), Boolean(job.ai) && viaHeuristics);
    assert.equal(got.usedAI, Boolean(job.ai) && !viaHeuristics);
    assert.deepEqual(got.log.filter((l) => l !== FALLBACK), wantLog);
    assert.deepEqual(got.plan, want.plan);
    assert.equal(got.sha, SHA);
    assert.equal(got.bodies.length, want.asks.length);
    got.bodies.forEach((body, i) => {
      assert.equal(body.instructions, want.asks[i].instructions);
      assert.equal(body.input, want.asks[i].prompt);
      assert.equal(body.max_output_tokens, want.asks[i].max_tokens);
      assert.equal(body.model, "gpt-5.1-codex-mini");
      assert.equal(body.authorization, "Bearer sk-test");
    });
  });
}

test("the parity cases cover what they are meant to", { skip }, () => {
  const byId = Object.fromEntries(pipelines.map((p, n) => [p.id, py(idx.pipelines[n])]));
  const need = byId["ai need_files"];
  assert.equal(need.asks.length, 2);
  assert.match(need.asks[1].prompt, /--- missing.txt ---\n\(not found\)/);
  assert.match(need.asks[1].prompt, /--- \.\.\/etc\/passwd ---\n\(not found\)/);
  assert.match(need.asks[1].prompt, /--- node_modules\/x\/index.js ---\nmodule.exports = 1;/);
  assert.ok(!need.asks[1].prompt.includes("ninth-is-never-read"));
  assert.ok(need.log.includes("  🔎 AI reading /root.txt"));
  assert.deepEqual(need.plan.api_prefixes, ["/"]);
  assert.ok(byId["ai own Dockerfile"].log.includes("  using repo's own Dockerfile"));
  assert.deepEqual(byId["ai own Dockerfile"].plan.api_prefixes, ["/api", "/graphql"]);
  assert.deepEqual(byId["heuristics api bridge default"].plan.api_prefixes, ["/api"]);
  assert.match(byId["ai long key files"].asks[0].prompt, /Key files:\n[\s\S]{39000,}/);
  assert.match(byId["ai plan"].asks[0].prompt, /Sibling services in this project:\n- web: public/);
  assert.equal(byId["heuristics dirGoMod"].plan.notes, "heuristic: go app");
});

test("_json_from parity", { skip }, () => {
  JSON_TEXTS.forEach((text, i) => assert.deepEqual(jsonFrom(text), py(idx.json[i]), JSON.stringify(text)));
});

test("build_plan accepts exactly the plans whose port is an int", { skip }, async () => {
  for (const [i, text] of PORT_TEXTS.entries()) {
    const { fetchImpl } = fakeOpenAI([text]);
    const plan = await aiPlan({ tree: "tree", files: {}, hasDockerfile: false, apiKey: "k", fetchImpl });
    assert.equal(plan !== null, py(idx.ports[i]), text);
  }
});

test("sibling context parity", { skip }, () => {
  SIBLING_SETS.forEach((s, i) => assert.equal(siblingContext(s), py(idx.siblings[i])));
});

test("makeSlug, serviceName, envKey, isApiName parity", { skip }, async () => {
  for (const [name, taken, i] of idx.slugs) {
    const set = new Set(taken);
    assert.equal(await makeSlug(name, async (s) => set.has(s)), py(i), `${name} / ${taken}`);
  }
  REPOS.forEach((repo, i) => assert.equal(serviceName(repo), py(idx.names[i]), repo));
  KEY_NAMES.forEach((name, i) => assert.equal(envKey(name), py(idx.keys[i]), name));
  API_NAMES.forEach((name, i) => assert.equal(isApiName(name), py(idx.apis[i]), name));
});

// ---------- aiPlan ----------

const BASE = { tree: "package.json\nsrc/index.js", hasDockerfile: true, apiKey: "sk-test",
               files: new Map([["package.json", '{"name":"x"}'], ["Dockerfile", "FROM node\n"]]) };

test("aiPlan: the prompt, the request and the plan", async () => {
  const { fetchImpl, bodies } = fakeOpenAI(['{"port": 8080, "notes": "ok"}']);
  const plan = await aiPlan({ ...BASE, siblings: [SIB_WEB], fetchImpl, model: "m-1" });
  assert.deepEqual(plan, { port: 8080, notes: "ok" });
  assert.equal(bodies.length, 1);
  const [body] = bodies;
  assert.equal(body.model, "m-1");
  assert.equal(body.max_output_tokens, 4000);
  assert.equal(body.instructions, PLAN_INSTRUCTIONS);
  assert.ok(body.input.startsWith("Build host: linux/amd64. Images are built"));
  assert.ok(body.input.includes("Never use --platform"));
  assert.ok(body.input.includes("'exec format error'"));
  assert.ok(body.input.includes("Repo has Dockerfile: True\n\n"));
  assert.ok(body.input.includes("- web: public https://shop.cicatrixa.com, internal http://shop:3000 " +
                                "(env keys WEB_URL / WEB_INTERNAL_URL)"));
  assert.ok(body.input.endsWith("Key files:\n--- package.json ---\n{\"name\":\"x\"}\n--- Dockerfile ---\nFROM node\n"));
  const arm = fakeOpenAI(['{"port": 1}']);
  await aiPlan({ ...BASE, hasDockerfile: false, arch: "arm64", fetchImpl: arm.fetchImpl });
  assert.ok(arm.bodies[0].input.startsWith("Build host: linux/arm64."));
  assert.ok(arm.bodies[0].input.includes("Repo has Dockerfile: False\n\n"));
});

test("aiPlan: need_files reads up to 8 files, '(not found)' for missing ones", async () => {
  const asked = Array.from({ length: 10 }, (_, i) => `f${i}.js`);
  const { fetchImpl, bodies } = fakeOpenAI([JSON.stringify({ need_files: asked }),
                                            "```json\n{\"port\": 3000}\n```"]);
  const reads = [];
  const log = [];
  const plan = await aiPlan({
    ...BASE, fetchImpl, log: (l) => log.push(l),
    readFile: async (p) => { reads.push(p); return p === "f1.js" ? "one" : p === "f2.js" ? "" : null; },
  });
  assert.deepEqual(plan, { port: 3000 });
  assert.deepEqual(reads, asked.slice(0, 8));
  assert.deepEqual(log, ["  🔎 AI reading f1.js", "  🔎 AI reading f2.js"]);
  assert.equal(bodies.length, 2);
  const second = bodies[1].input;
  assert.ok(second.startsWith(bodies[0].input + "\n\nRequested files:\n--- f0.js ---\n(not found)\n" +
                              "--- f1.js ---\none\n--- f2.js ---\n\n--- f3.js ---\n(not found)"));
  assert.ok(second.endsWith("--- f7.js ---\n(not found)\n\nNow reply with the full plan JSON only."));
});

test("aiPlan: no plan without an integer port, or when the model keeps asking", async () => {
  for (const reply of ['{"port": "3000"}', '{"port": 3000.5}', '{"port": 3000.0}', '{"port": true}',
                       '{"notes": "no port"}', "not json", '{"need_files": ["a"]}']) {
    const { fetchImpl } = fakeOpenAI([reply, reply]);
    assert.equal(await aiPlan({ ...BASE, fetchImpl, readFile: async () => null }), null, reply);
  }
  // Two rounds at most, like the platform.
  const { fetchImpl, bodies } = fakeOpenAI(['{"need_files": ["a"]}', '{"need_files": ["b"]}', '{"port": 1}']);
  assert.equal(await aiPlan({ ...BASE, fetchImpl, readFile: async () => "x" }), null);
  assert.equal(bodies.length, 2);
});

test("aiPlan: errors and a missing key give null", async () => {
  const { fetchImpl, bodies } = fakeOpenAI([null]);
  assert.equal(await aiPlan({ ...BASE, fetchImpl }), null);
  assert.equal(bodies.length, 1);
  let called = false;
  assert.equal(await aiPlan({ ...BASE, apiKey: "", fetchImpl: async () => { called = true; } }), null);
  assert.equal(called, false);
  const bad = async () => Response.json({ output: [{ content: [{ type: "output_text", text: 5 }] }] });
  assert.equal(await aiPlan({ ...BASE, fetchImpl: bad }), null);
  const throws = async () => { throw new TypeError("fetch failed"); };
  assert.equal(await aiPlan({ ...BASE, fetchImpl: throws }), null);
});

test("aiPlan: gives up at the deadline even if fetch never answers", async () => {
  let signal;
  const hang = (url, init) => { signal = init.signal; return new Promise(() => {}); };
  const t0 = Date.now();
  assert.equal(await aiPlan({ ...BASE, fetchImpl: hang, deadlineMs: Date.now() + 80 }), null);
  const took = Date.now() - t0;
  assert.ok(took >= 70 && took < 2000, `took ${took} ms`);
  assert.equal(signal.aborted, true);
  // Already past: no request at all.
  let called = false;
  const spy = async () => { called = true; return new Response("{}"); };
  assert.equal(await aiPlan({ ...BASE, fetchImpl: spy, deadlineMs: Date.now() - 1 }), null);
  assert.equal(called, false);
});

test("aiPlan: uses globalThis.fetch when no fetchImpl is given", async () => {
  const real = globalThis.fetch;
  const { fetchImpl, bodies } = fakeOpenAI(['{"port": 9000}']);
  globalThis.fetch = fetchImpl;
  try {
    assert.deepEqual(await aiPlan(BASE), { port: 9000 });
  } finally {
    globalThis.fetch = real;
  }
  assert.equal(bodies.length, 1);
});

// ---------- analyze ----------

test("analyze: the platform's log lines, heuristics", async () => {
  const log = [];
  const calls = [];
  const r = await analyze({ token: "t", repoFull: "acme/web", branch: "main", gh: fixtureGh(D.nodePlain, { calls }),
                            env: {}, log: (l) => log.push(l) });
  assert.deepEqual(log, ["⇣ reading acme/web@main", "  at commit 0123456789",
                         "🧠 analyzing repository (heuristics)", "  plan: heuristic: node app (port 3000)"]);
  assert.equal(r.usedAI, false);
  assert.equal(r.sha, SHA);
  assert.equal(r.branch, "main");
  assert.deepEqual(r.plan.api_prefixes, []);
  assert.match(r.plan.dockerfile, /CMD \["node","index.js"\]/);
  // Only root key files that exist are read, at the commit.
  assert.deepEqual(calls.filter((c) => c[0] === "readFile").sort(),
                   [["readFile", "index.js", SHA], ["readFile", "package.json", SHA]]);
});

test("analyze: falls back to the heuristics when the AI fails, and says so", async () => {
  const log = [];
  const { fetchImpl } = fakeOpenAI([null]);
  const r = await analyze({ token: "t", repoFull: "acme/orders-api", branch: "main", gh: fixtureGh(D.pyReqMain),
                            env: { OPENAI_API_KEY: "sk" }, siblings: [SIB_WEB], log: (l) => log.push(l),
                            fetchImpl });
  assert.deepEqual(log, ["⇣ reading acme/orders-api@main", "  at commit 0123456789",
                         "🧠 analyzing repository (AI)", FALLBACK, "  plan: heuristic: python app (port 8000)"]);
  assert.equal(r.usedAI, false);
  // The platform's bridge default is for when there is no AI at all, not when it failed.
  assert.deepEqual(r.plan.api_prefixes, []);
});

test("analyze: the repo's own Dockerfile", async () => {
  const log = [];
  const { fetchImpl, bodies } = fakeOpenAI(['{"dockerfile": null, "port": 8080, "notes": "own"}']);
  const r = await analyze({ token: "t", repoFull: "acme/site", branch: "main", gh: fixtureGh(D.ownDockerfile),
                            env: { OPENAI_API_KEY: "sk", AI_MODEL: "gpt-x" }, log: (l) => log.push(l), fetchImpl });
  assert.equal(r.usedAI, true);
  assert.equal(r.plan.dockerfile, null);
  assert.equal(bodies[0].model, "gpt-x");
  assert.ok(bodies[0].input.includes("Repo has Dockerfile: True"));
  assert.ok(bodies[0].input.includes("--- Dockerfile ---\nFROM nginx:alpine\nEXPOSE 8080\n"));
  assert.deepEqual(log.slice(3), ["  using repo's own Dockerfile", "  plan: own (port 8080)"]);
  // The heuristics always write their own Dockerfile, as on the platform.
  const h = await analyze({ token: "t", repoFull: "acme/site", branch: "main", gh: fixtureGh(D.ownDockerfile), env: {} });
  assert.match(h.plan.dockerfile, /^FROM nginx:alpine\nCOPY \. /);
});

test("analyze: api bridge default with heuristics, siblings and an API name", async () => {
  const log = [];
  const r = await analyze({ token: "t", repoFull: "acme/orders-api", branch: "main", gh: fixtureGh(D.go),
                            env: {}, siblings: [SIB_WEB], log: (l) => log.push(l) });
  assert.deepEqual(r.plan.api_prefixes, ["/api"]);
  assert.equal(log.at(-1), "  api bridge: /api will be served on sibling domains");
  const alone = await analyze({ token: "t", repoFull: "acme/orders-api", branch: "main", gh: fixtureGh(D.go), env: {} });
  assert.deepEqual(alone.plan.api_prefixes, []);
  const front = await analyze({ token: "t", repoFull: "acme/storefront", branch: "main", gh: fixtureGh(D.go),
                                env: {}, siblings: [SIB_WEB] });
  assert.deepEqual(front.plan.api_prefixes, []);
  const named = await analyze({ token: "t", repoFull: "acme/storefront", name: "shop-backend", branch: "main",
                                gh: fixtureGh(D.go), env: {}, siblings: [SIB_WEB] });
  assert.deepEqual(named.plan.api_prefixes, ["/api"]);
});

test("analyze: the AI gets until 52 s after the request started", async () => {
  let called = false;
  const fetchImpl = async () => { called = true; return new Response("{}", { status: 500 }); };
  const log = [];
  const r = await analyze({ token: "t", repoFull: "acme/s", branch: "main", gh: fixtureGh(D.static),
                            env: { OPENAI_API_KEY: "sk" }, now: Date.now() - 53_000, fetchImpl,
                            log: (l) => log.push(l) });
  assert.equal(called, false);
  assert.ok(log.includes(FALLBACK));
  assert.equal(r.plan.notes, "heuristic: static site");
  // A clock works too: here 60 s pass between the start and the AI call.
  let t = Date.now();
  const clock = () => { const v = t; t += 60_000; return v; };
  const r2 = await analyze({ token: "t", repoFull: "acme/s", branch: "main", gh: fixtureGh(D.static),
                             env: { OPENAI_API_KEY: "sk" }, now: clock, fetchImpl });
  assert.equal(called, false);
  assert.equal(r2.usedAI, false);
});

test("analyze: the default branch when none is set", async () => {
  const log = [];
  const calls = [];
  const r = await analyze({ token: "t", repoFull: "acme/s", branch: "", gh: fixtureGh(D.static, { calls }),
                            env: {}, log: (l) => log.push(l) });
  assert.equal(r.branch, "trunk");
  assert.equal(log[0], "⇣ reading acme/s@trunk");
  assert.deepEqual(calls.find((c) => c[0] === "branchHead"), ["branchHead", "acme/s", "trunk"]);
});

test("analyze: a tree GitHub cut short still finds the root files", async () => {
  const calls = [];
  const gh = fixtureGh(D.nodeNext, { truncated: true, hide: ["package.json"], calls });
  const r = await analyze({ token: "t", repoFull: "acme/web", branch: "main", gh, env: {} });
  assert.equal(r.plan.notes, "heuristic: node app");
  assert.match(r.plan.dockerfile, /npx","next"/);
  assert.ok(calls.some((c) => c[0] === "readFile" && c[1] === "run.py"));
  // Not cut short: nothing outside the tree is asked for.
  const calls2 = [];
  await analyze({ token: "t", repoFull: "acme/web", branch: "main", env: {},
                  gh: fixtureGh(D.nodeNext, { hide: ["package.json"], calls: calls2 }) });
  assert.ok(!calls2.some((c) => c[0] === "readFile" && c[1] === "package.json"));
});

test("analyze: what the model may read stays inside the repo", async () => {
  const calls = [];
  const { fetchImpl, bodies } = fakeOpenAI([
    JSON.stringify({ need_files: ["../../etc/passwd", "a/", "a/b/b.txt", "/a/a.txt", "a/b/../../root.txt", "nope"] }),
    '{"port": 1}',
  ]);
  const log = [];
  await analyze({ token: "t", repoFull: "acme/deep", branch: "main", gh: fixtureGh(D.deep, { calls }),
                  env: { OPENAI_API_KEY: "sk" }, fetchImpl, log: (l) => log.push(l) });
  const read = calls.filter((c) => c[0] === "readFile").map((c) => c[1]);
  assert.ok(read.includes("a/b/b.txt") && read.includes("a/a.txt") && read.includes("root.txt"));
  assert.ok(!read.some((p) => p.includes("..") || p === "a" || p === "nope" || p.includes("passwd")));
  assert.match(bodies[1].input, /--- a\/b\/b.txt ---\nbee\nline2\n/);   // CRLF as the platform reads it
  assert.deepEqual(log.filter((l) => l.includes("🔎")),
                   ["  🔎 AI reading a/b/b.txt", "  🔎 AI reading /a/a.txt", "  🔎 AI reading a/b/../../root.txt"]);
});

// Not in D: the parity runs (fixed when the file loads) cover what the platform does,
// and it reads files of any size.
const BIG = writeRepo("big", {
  "package.json": pkg({ scripts: { start: "node server.js" } }), "README.md": "huge\n",
  "index.html": "<p>huge</p>\n", "server.js": "require('http')\n", "docs/huge.txt": "huge\n",
  "docs/small.txt": "small\n",
});

test("analyze: files over 1 MB are not read, and the log says so", async () => {
  const calls = [];
  const log = [];
  const { fetchImpl, bodies } = fakeOpenAI([
    JSON.stringify({ need_files: ["docs/huge.txt", "docs/small.txt", "index.html"] }), '{"port": 3000}']);
  // README.md and docs/huge.txt are too big by the tree; index.html has no size there,
  // and readFile refuses it.
  const gh = fixtureGh(BIG, { calls, refuse: ["index.html"],
                                sizes: { "README.md": 5_000_000, "docs/huge.txt": 1024 * 1024 + 1,
                                         "package.json": 1024 * 1024, "server.js": 20 } });
  const r = await analyze({ token: "t", repoFull: "acme/big", branch: "main", gh,
                            env: { OPENAI_API_KEY: "sk" }, fetchImpl, log: (l) => log.push(l) });
  const read = calls.filter((c) => c[0] === "readFile").map((c) => c[1]);
  assert.ok(!read.includes("README.md") && !read.includes("docs/huge.txt"), read.join());
  assert.ok(read.includes("package.json"), "exactly 1 MB is read");
  assert.deepEqual(log.filter((l) => l.includes("skipped")), [
    "  skipped README.md (over 1 MB)", "  skipped index.html (over 1 MB)",
    "  skipped docs/huge.txt (over 1 MB)", "  skipped index.html (over 1 MB)"]);
  // What was not read is not sent, and a file asked for is "(not found)".
  assert.ok(!bodies[0].input.includes("--- README.md ---"));
  assert.ok(!bodies[0].input.includes("--- index.html ---"));
  assert.ok(bodies[0].input.includes("--- server.js ---\nrequire('http')\n"));
  assert.match(bodies[1].input, /--- docs\/huge.txt ---\n\(not found\)\n--- docs\/small.txt ---\nsmall\n/);
  assert.equal(r.plan.port, 3000);
  // Without the AI the heuristics still see the files are there.
  const h = await analyze({ token: "t", repoFull: "acme/big", branch: "main", env: {},
                            gh: fixtureGh(BIG, { sizes: { "package.json": 5_000_000 } }) });
  assert.equal(h.plan.notes, "heuristic: node app");
  assert.match(h.plan.dockerfile, /CMD \["node","index.js"\]/, "package.json was not read");
});

test("analyze: GitHub failures fail the analysis", async () => {
  const gh = { ...fixtureGh(D.static), branchHead: async () => { throw new Error("acme/s: branch x not found"); } };
  await assert.rejects(analyze({ token: "t", repoFull: "acme/s", branch: "x", gh, env: {} }), /branch x not found/);
});

// ---------- the rest ----------

test("heuristicPlan survives package.json shapes that crash the platform", () => {
  for (const raw of ['{"scripts": null, "dependencies": null}', "[]", "null", '"x"',
                     '{"scripts": ["build"], "devDependencies": [1]}']) {
    const plan = heuristicPlan({ has: (n) => n === "package.json", text: () => raw });
    assert.equal(plan.notes, "heuristic: node app", raw);
    assert.match(plan.dockerfile, /CMD \["node","index.js"\]\n$/);
    assert.ok(!plan.dockerfile.includes("RUN npm run build"));
  }
});

test("snapshotTree sorts by code point and keeps the platform's limits", () => {
  assert.equal(snapshotTree(["ｆ.txt", "😀.txt", "b/x", "a"]), "a\nｆ.txt\n😀.txt\nb/x");
  assert.equal(snapshotTree(["a/b/c/d.txt", "a/b/c.txt", "node_modules/x", "a/.git/y"]), "a/b/c.txt");
  assert.equal(snapshotTree([]), "");
});

test("makeSlug and friends", async () => {
  assert.equal(await makeSlug("My Shop!"), "my-shop");
  assert.equal(await makeSlug("admin"), "admin-2");
  assert.equal(await makeSlug("", async (s) => s === "app-2"), "app-3");
  assert.equal(await makeSlug(`${"a".repeat(39)}-b`), `${"a".repeat(39)}-`);
  assert.equal(serviceName("acme/My_Repo.js"), "my-repo-js");
  assert.equal(serviceURL("shop"), "https://shop.cicatrixa.com");
  assert.equal(envKey("orders-api"), "ORDERS_API");
  assert.equal(isApiName("orders-api"), true);
  assert.equal(isApiName("web"), false);
});
