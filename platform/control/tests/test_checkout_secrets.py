"""A checkout is not private: the AI reads files in it, it is the build context (the
static plan's `COPY . /usr/share/nginx/html` serves it to anyone), and the medic's
test runs get a copy. So no credential may land in it, and git's own directory is
never read, written, built or copied on anyone's behalf."""
import base64
import io
import os
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import engine, medic

TOKEN = "ghs_SECRETTOKEN123"


def git(*args, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.fixture
def github(tmp_path):
    """A repo served over plain HTTP the way GitHub serves one to an installation
    token: only with `Authorization: Basic x-access-token:<token>`."""
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    (src / "index.html").write_text("<h1>hi</h1>\n")
    git("add", "-A", cwd=src)
    git("commit", "-qm", "first", cwd=src)
    git("clone", "-q", "--bare", str(src), str(tmp_path / "srv" / "acme" / "site.git"))
    wanted = "Basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        """git's smart HTTP protocol (shallow clones need it), by git http-backend."""

        def do_GET(self):
            self.serve()

        def do_POST(self):
            self.serve()

        def serve(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != wanted:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
                self.end_headers()
                return
            path, _, query = self.path.partition("?")
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            cgi = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env={
                **os.environ, "GIT_PROJECT_ROOT": str(tmp_path / "srv"),
                "GIT_HTTP_EXPORT_ALL": "1", "REQUEST_METHOD": self.command,
                "PATH_INFO": path, "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                "GIT_PROTOCOL": self.headers.get("Git-Protocol", "")})
            head, _, payload = cgi.stdout.partition(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n") if line)
            self.send_response(int(headers.pop("Status", "200").split()[0]))
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"127.0.0.1:{server.server_address[1]}"
    yield {"credentialed": f"http://x-access-token:{TOKEN}@{base}/acme/site.git",
           "clean": f"http://{base}/acme/site.git", "seen": seen}
    server.shutdown()


def every_byte_under(root) -> bytes:
    out = b""
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                with open(path, "rb") as f:
                    out += f.read()
    return out


# ---------- the clone ----------

def test_a_clone_leaves_the_token_nowhere_in_the_checkout(github, tmp_path):
    work = tmp_path / "work"
    r = engine.clone_repo(github["credentialed"], "main", str(work))
    assert r.returncode == 0, r.stderr
    assert (work / "index.html").read_text() == "<h1>hi</h1>\n"
    assert TOKEN.encode() not in every_byte_under(work)
    assert TOKEN not in r.stdout + r.stderr
    origin = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                            capture_output=True, text=True).stdout.strip()
    assert origin == github["clean"]


def test_the_token_is_what_lets_git_in(github, tmp_path):
    """The server above really checks: without the header nothing is cloned, and
    git does not stop to ask anyone for a password."""
    r = engine.clone_repo(github["clean"], "main", str(tmp_path / "work"))
    assert r.returncode != 0
    assert set(github["seen"]) == {None}


def test_a_full_clone_for_the_medic_can_push_with_the_token_handed_to_each_command(
        github, tmp_path):
    work = tmp_path / "push"
    assert engine.clone_repo(github["credentialed"], "main", str(work), depth=None).returncode == 0
    _url, env = engine.split_credentials(github["credentialed"])
    fetch = subprocess.run(["git", "-C", str(work), "fetch", "origin"], env=env,
                           capture_output=True, text=True)
    assert fetch.returncode == 0, fetch.stderr
    assert TOKEN.encode() not in every_byte_under(work)


def test_a_url_without_a_token_is_left_alone(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    url, env = engine.split_credentials("https://github.com/acme/site.git")
    assert url == "https://github.com/acme/site.git" and "GIT_CONFIG_COUNT" not in env


def test_the_header_is_scoped_to_the_host_and_added_to_any_git_config_already_set(
        monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    url, env = engine.split_credentials(f"https://x-access-token:{TOKEN}@github.com/a/b.git")
    assert url == "https://github.com/a/b.git"
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"]) == ("2", "safe.directory")
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraHeader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_1"].split()[-1]).decode() == \
        f"x-access-token:{TOKEN}"


# ---------- what the AI may name ----------

@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "work"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(f"url = https://x-access-token:{TOKEN}@github.com/a/b\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "notes").symlink_to(root / ".git")
    (root / "Dockerfile").write_text("FROM nginx:alpine\nCOPY . /usr/share/nginx/html\n")
    return root


@pytest.mark.parametrize("path", [".git/config", "/.git/config", "src/../.git/config",
                                  "notes/config", ".GIT/config", "../outside", ".git"])
def test_the_ai_never_reads_gits_own_files_or_leaves_the_checkout(checkout, path):
    assert engine._make_reader(str(checkout), lambda _l: None)(path) is None
    assert engine.inside_checkout(str(checkout), path) is None


def test_the_ai_still_reads_the_code(checkout):
    assert engine._make_reader(str(checkout), lambda _l: None)("src/app.py") == "print('hi')\n"


def test_the_medic_offers_no_patch_to_gits_own_files(checkout):
    """Apply runs git add, commit and push in the clone next: a patch to its config
    (core.fsmonitor, hooksPath) would run code in the control plane."""
    service = {"name": "web", "slug": "web"}
    patches = [{"file": ".git/config", "find": "url", "replace": "x"},
               {"file": "notes/config", "find": "url", "replace": "x"},
               {"file": "src/app.py", "find": "hi", "replace": "hello"}]
    kept = medic._verify_patches(patches, service, {"web": str(checkout)})
    assert [p["file"] for p in kept] == ["src/app.py"]


# ---------- what gets built and copied ----------

def names_in(archive) -> list[str]:
    archive.seek(0)
    return tarfile.open(fileobj=archive).getnames()


def test_the_build_context_never_carries_git_whatever_the_dockerignore_says(checkout):
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "x.js").write_text("")
    (checkout / ".dockerignore").write_text(
        "# comment\nnode_modules\nDockerfile\n!.git\n!.git/config\n")
    with engine._build_context(str(checkout), "Dockerfile") as context:
        names = names_in(context)
    assert not [n for n in names if engine._in_git_dir(n)]
    assert "Dockerfile" in names                      # always sent, as docker-py does
    assert "src/app.py" in names and "node_modules/x.js" not in names
    assert "notes" in names                           # a symlink, not what it points at
    assert not [n for n in names if n.startswith("notes/")]


def test_the_ai_dockerfile_is_sent_though_the_repo_ignores_dotfiles(checkout):
    (checkout / ".cx.Dockerfile").write_text("FROM scratch\n")
    (checkout / ".dockerignore").write_text(".*\n")
    with engine._build_context(str(checkout), ".cx.Dockerfile") as context:
        names = names_in(context)
    assert ".cx.Dockerfile" in names and not [n for n in names if engine._in_git_dir(n)]


def test_a_build_sends_that_context_not_the_directory(checkout, monkeypatch):
    seen = {}

    class API:
        def build(self, **kw):
            seen.update(kw, names=names_in(kw["fileobj"]))
            return iter([{"stream": "Successfully built abc\n"}])

    monkeypatch.setattr(engine, "dock", lambda: type("D", (), {"api": API()})())
    engine._build(str(checkout), "Dockerfile", "cx-web:abc", lambda _l: None)
    assert "path" not in seen and seen["custom_context"] is True
    assert seen["dockerfile"] == "Dockerfile"
    assert "src/app.py" in seen["names"]
    assert not [n for n in seen["names"] if engine._in_git_dir(n)]


def test_the_medics_test_copy_has_no_git(checkout, monkeypatch):
    seen = {}

    class Container:
        def put_archive(self, path, data):
            seen["names"] = tarfile.open(fileobj=io.BytesIO(data.read())).getnames()
        def start(self): pass
        def wait(self, timeout): return {"StatusCode": 0}
        def logs(self): return b"ok"
        def remove(self, force): pass

    class Containers:
        def create(self, image, **kw):
            return Container()

    monkeypatch.setattr(engine, "dock", lambda: type("D", (), {"containers": Containers()})())
    engine.test_runner("img", str(checkout))("pytest")
    root = str(checkout).lstrip("/")
    assert root + "/src/app.py" in seen["names"]
    assert not [n for n in seen["names"] if engine._in_git_dir(n[len(root):])]


# ---------- a repo's links: never read or written through ----------
#
# A repo decides what each of its names is, links included, and the clone writes them
# as committed: `.cx.Dockerfile -> /data/cicatrixa.db` or `README.md -> /proc/self/environ`
# is one commit away. cx-control runs as root and holds every API key in its
# environment, the platform's database and every other tenant's checkout.

@pytest.fixture
def outside(tmp_path):
    """Files beside the checkouts that no repo may reach: the platform's database and
    another tenant's checkout."""
    data = tmp_path / "data"
    (data / "work" / "other-tenant").mkdir(parents=True)
    (data / "cicatrixa.db").write_text("PLATFORM DATABASE")
    (data / "work" / "other-tenant" / "secret.env").write_text("DB_PASSWORD=hunter2\n")
    return data


def repo_with(tmp_path, files: dict, links: dict):
    """A git repo committing `files` (name -> text) and `links` (name -> target)."""
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    for name, text in files.items():
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text(text)
    for name, target in links.items():
        os.symlink(str(target), src / name)
    git("add", "-A", cwd=src)
    git("commit", "-qm", "first", cwd=src)
    return src


def a_service(slug="site"):
    from app import db
    uid = db.q("INSERT INTO users(email,pw_hash,created_at) VALUES('a@b.c','x',?)",
               (db.now(),)).lastrowid
    db.q("INSERT INTO github_connections(user_id,kind,installation_id,created_at) "
         "VALUES(?,'app',1,?)", (uid, db.now()))
    pid = db.q("INSERT INTO projects(user_id,name,slug,created_at) VALUES(?,?,?,?)",
               (uid, slug, slug, db.now())).lastrowid
    sid = db.q("INSERT INTO services(project_id,name,slug,repo_full,branch,created_at) "
               "VALUES(?,?,?,?,?,?)", (pid, slug, slug, f"a/{slug}", "main",
                                       db.now())).lastrowid
    service = dict(db.one("SELECT * FROM services WHERE id=?", (sid,)))
    service.update(user_id=uid, project_slug=slug)
    return service, db.one("SELECT * FROM projects WHERE id=?", (pid,))


class Built(Exception):
    pass


def capture_builds(monkeypatch) -> list[dict]:
    """engine.dock() whose API records every build context it is sent, then stops."""
    builds = []

    class API:
        def build(self, **kw):
            kw["fileobj"].seek(0)
            tar = tarfile.open(fileobj=kw["fileobj"])
            members = {m.name: m for m in tar.getmembers()}
            df = members.get(kw["dockerfile"])
            builds.append({"dockerfile": kw["dockerfile"], "names": sorted(members),
                           "is_file": bool(df and df.isfile()),
                           "text": tar.extractfile(df).read().decode() if df and df.isfile()
                           else None})
            raise Built()

    monkeypatch.setattr(engine, "dock", lambda: type("D", (), {"api": API()})())
    return builds


def test_a_committed_cx_dockerfile_link_is_never_written_through(
        fresh_db, tmp_path, outside, monkeypatch):
    """The review's PoC: a repo with no Dockerfile commits `.cx.Dockerfile` as a link to
    a file outside its checkout. The plan's Dockerfile used to be written through it
    (open(..., "w")), over that file. Now it never enters the checkout: the build gets
    it as its own context entry, and the repo's link stays out of that context."""
    from app import ai, gh, metrics
    victim = outside / "cicatrixa.db"
    src = repo_with(tmp_path, {"index.html": "<h1>hi</h1>\n"}, {".cx.Dockerfile": victim})
    monkeypatch.setattr(engine, "WORK_ROOT", str(outside / "work"))
    monkeypatch.setattr(gh, "clone_url", lambda conn, repo: str(src))
    monkeypatch.setattr(metrics, "check_deploy_quota", lambda owner, s: None)
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "")                # the heuristic plan
    builds = capture_builds(monkeypatch)
    service, project = a_service()
    with pytest.raises(Built):
        engine._run_pipeline(service, project, 1, lambda line: None)
    assert victim.read_text() == "PLATFORM DATABASE"
    [build] = builds
    assert build["dockerfile"] == engine.PLAN_DOCKERFILE == ".cx.Dockerfile"
    assert build["is_file"] and build["text"].startswith("FROM nginx:alpine")
    assert build["names"].count(".cx.Dockerfile") == 1 and "index.html" in build["names"]
    # Kept beside the checkout, not in it; the repo's link is still just a link there.
    kept = engine.plan_dockerfile_path("site")
    assert os.path.dirname(kept) == engine.WORK_ROOT and not kept.startswith(
        os.path.join(engine.WORK_ROOT, "site") + os.sep)
    assert engine.read_plan_dockerfile("site") == build["text"]
    assert os.path.islink(os.path.join(engine.WORK_ROOT, "site", ".cx.Dockerfile"))


def test_the_plan_dockerfile_goes_with_its_checkout(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "WORK_ROOT", str(tmp_path / "work"))
    engine._save_plan_dockerfile("site", "FROM scratch\n")
    assert engine.read_plan_dockerfile("site") == "FROM scratch\n"
    assert os.listdir(engine.WORK_ROOT) == ["site.cx.Dockerfile"]      # no temp file left
    service, _ = a_service()
    monkeypatch.setattr(engine, "dock", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(engine, "_existing_containers", lambda slug: [])
    monkeypatch.setattr(engine.gh, "revoke_repo", lambda repo: None)
    engine.delete_service(service)
    assert engine.read_plan_dockerfile("site") is None


def test_the_medic_builds_with_the_plan_dockerfile_and_copies_nothing_into_the_clone(
        fresh_db, tmp_path, outside, monkeypatch):
    """Its fix flow copied the cached plan with shutil.copy onto `.cx.Dockerfile` in a
    fresh clone — through the repo's link, when it commits one — and then committed
    the copy into the customer's repo with `git add -A`."""
    import json
    from app import db, gh
    victim = outside / "cicatrixa.db"
    src = repo_with(tmp_path, {"index.html": "<h1>hi</h1>\n"}, {".cx.Dockerfile": victim})
    monkeypatch.setattr(engine, "WORK_ROOT", str(outside / "work"))
    monkeypatch.setattr(medic, "PUSH_ROOT", str(tmp_path / "push"))
    monkeypatch.setattr(gh, "clone_url", lambda conn, repo: str(src))
    engine._save_plan_dockerfile("site", "FROM nginx:alpine\nCOPY . /usr/share/nginx/html\n")
    service, project = a_service()
    mid = db.q("INSERT INTO chat_messages(project_id,role,kind,data,created_at) "
               "VALUES(?,'agent','fix',?,?)", (project["id"], json.dumps({
                   "service_id": service["id"], "commit_message": "fix greeting",
                   "patches": [{"file": "index.html", "find": "hi", "replace": "hello"}]}),
                   db.now())).lastrowid
    builds, committed = [], []

    def fake_build(workdir, dockerfile, tag, log, buildargs=None, dockerfile_text=None):
        with engine._build_context(workdir, dockerfile, dockerfile_text) as context:
            context.seek(0)
            tar = tarfile.open(fileobj=context)
            member = tar.getmember(dockerfile)
            builds.append((dockerfile, member.isfile() and tar.extractfile(member).read()))
        return ""

    def fake_deliver(clone, *a):
        committed.extend(subprocess.run(["git", "-C", clone, "show", "--name-only",
                                         "--format=", "HEAD"], capture_output=True,
                                        text=True).stdout.split())
        return "pushed", None

    monkeypatch.setattr(engine, "_build", fake_build)
    monkeypatch.setattr(engine, "dock", lambda: type("D", (), {"images": type(
        "I", (), {"remove": staticmethod(lambda *a, **k: None)})()})())
    monkeypatch.setattr(medic, "_record_fix", lambda *a: None)
    monkeypatch.setattr(medic, "_verify_fix", lambda *a: ("unverified", None))
    monkeypatch.setattr(medic, "_deliver", fake_deliver)
    sid, text = medic.apply_fix_sync(project["id"], mid)
    assert sid == service["id"], text
    assert victim.read_text() == "PLATFORM DATABASE"
    assert builds == [(".cx.Dockerfile", b"FROM nginx:alpine\nCOPY . /usr/share/nginx/html\n")]
    assert committed == ["index.html"]


def test_the_ai_snapshot_reads_no_key_file_through_a_link_out_of_the_checkout(
        tmp_path, outside, monkeypatch):
    """The review's PoC: these key files go into the AI's prompt on every deploy, and
    through the medic into its replies to the repo's owner."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-secret-123")
    src = repo_with(tmp_path, {"src/app.js": "console.log(1)\n", "package.json": "{}\n"},
                    {"README.md": "/proc/self/environ",
                     "requirements.txt": outside / "work" / "other-tenant" / "secret.env",
                     "Procfile": ".git/config",
                     "index.js": "src/app.js",                  # inside: still read
                     "server.js": "src"})                       # a directory: nothing to read
    work = str(tmp_path / "work")
    assert engine.clone_repo(str(src), "main", work).returncode == 0
    tree, files = engine._snapshot(work)
    assert set(files) == {"package.json", "index.js"}
    assert files["index.js"] == "console.log(1)\n"
    assert "README.md" in tree.split("\n")                      # listed, never read
    for name in ("README.md", "requirements.txt", "Procfile", "server.js"):
        assert engine.read_checkout_file(work, name) is None
        assert engine._make_reader(work, lambda _l: None)(name) is None


def test_a_heuristic_plan_reads_no_package_json_out_of_the_checkout(tmp_path, outside):
    (outside / "next.json").write_text('{"dependencies": {"next": "15"}}')
    root = tmp_path / "co"
    root.mkdir()
    (root / "package.json").symlink_to(outside / "next.json")
    plan = engine.ai.heuristic_plan(str(root))
    assert plan["notes"] == "heuristic: node app" and "next" not in plan["dockerfile"]
    (root / "package.json").unlink()
    (root / "package.json").write_text('{"dependencies": {"next": "15"}}')
    assert "next" in engine.ai.heuristic_plan(str(root))["dockerfile"]


def test_a_link_out_of_the_checkout_cannot_tell_whether_its_target_exists(tmp_path, outside):
    """The plan shows in the deploy log; were a link's target looked up, a link into
    another tenant's checkout would tell whether a file exists there."""
    plans = []
    for target in (outside / "work" / "other-tenant" / "secret.env",
                   outside / "work" / "other-tenant" / "nothing-here"):
        root = tmp_path / f"co{len(plans)}"
        root.mkdir()
        (root / "requirements.txt").symlink_to(target)
        plans.append(engine.ai.heuristic_plan(str(root)))
    assert plans[0] == plans[1] and plans[0]["notes"] == "heuristic: python app"


def test_the_integration_audit_greps_only_files_in_the_checkout(tmp_path, outside):
    (outside / "leak.js").write_text("fetch('https://api.other-tenant.example')\n")
    root = tmp_path / "co"
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.js").write_text("fetch('https://api.foreign.example')\n")
    (root / "src" / "leak.js").symlink_to(outside / "leak.js")
    (root / "src" / "alias.js").symlink_to(root / "src" / "api.js")
    urls = ["https://api.foreign.example", "https://api.other-tenant.example"]
    assert engine._grep_urls(str(root), urls) == {"https://api.foreign.example": ["src/api.js"]}


def test_a_dockerignore_that_links_out_of_the_checkout_is_not_read(checkout, outside):
    (outside / "patterns").write_text("src\n")
    (checkout / ".dockerignore").symlink_to(outside / "patterns")
    with engine._build_context(str(checkout), "Dockerfile") as context:
        assert "src/app.py" in names_in(context)


def test_the_plan_dockerfile_is_the_one_entry_of_its_name_whatever_the_repo_has(checkout):
    (checkout / ".cx.Dockerfile").mkdir()
    (checkout / ".cx.Dockerfile" / "x").write_text("x")
    with engine._build_context(str(checkout), ".cx.Dockerfile", "FROM scratch\n") as context:
        context.seek(0)
        tar = tarfile.open(fileobj=context)
        names = tar.getnames()
        assert [n for n in names if n.startswith(".cx.Dockerfile")] == [".cx.Dockerfile"]
        assert tar.extractfile(".cx.Dockerfile").read() == b"FROM scratch\n"
