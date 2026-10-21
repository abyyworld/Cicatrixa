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
