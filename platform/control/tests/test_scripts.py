"""The setup scripts, run for real against a stub `docker`: what .env holds when the
stack first starts.

A box that takes over the online workspace must refuse sign-ups from its very first
`docker compose up` (main.signups_wait_for_import keys on TURSO_DATABASE_URL). Started
without it, it took them on its IP address — the VPS's :80 fallback router answers any
host name — until someone edited .env, and with email off confirmed them on the spot:
an account sitting on an address whose owner is still online."""
import os
import shutil
import subprocess

import pytest

PLATFORM = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONLINE = {"TURSO_DATABASE_URL": "libsql://cicatrixa-prod-test.aws-us-east-1.turso.io",
          "TURSO_AUTH_TOKEN": "turso-token", "BETTER_AUTH_SECRET": "better-auth-secret",
          "RESEND_API_KEY": "re_test", "MAIL_FROM": "Cicatrixa <noreply@cicatrixa.com>"}

pytestmark = pytest.mark.skipif(not shutil.which("bash") or not shutil.which("git"),
                                reason="needs bash and git")


@pytest.fixture
def stub(tmp_path):
    """A `docker` that answers yes to everything and, at `compose up`, keeps the .env
    the stack would have started with."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\necho "$*" >> "$STUB_LOG"\n'
                      'if [ "$1 $2" = "compose up" ]; then cp .env "$STUB_ENV_AT_UP"; fi\n'
                      "exit 0\n")
    docker.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path),
            "STUB_LOG": str(tmp_path / "docker.log"),
            "STUB_ENV_AT_UP": str(tmp_path / "env-at-up")}


def platform_copy(dest):
    """This checkout's platform/, with verify-public.sh (the network check) passing."""
    shutil.copytree(PLATFORM, dest, ignore=shutil.ignore_patterns("__pycache__", ".env"))
    (dest / "verify-public.sh").write_text("#!/bin/sh\nexit 0\n")


def env_file(path) -> dict:
    lines = open(path).read().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={
        **os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


@pytest.mark.skipif(os.geteuid() != 0, reason="bootstrap.sh runs as root")
def test_bootstrap_starts_the_stack_with_the_online_workspace_already_in_env(tmp_path, stub):
    repo = tmp_path / "repo"
    platform_copy(repo / "platform")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "platform", cwd=repo)
    dest = tmp_path / "box" / "cicatrixa-platform"
    dest.parent.mkdir()
    env = {**stub, "BASE_DOMAIN": "cicatrixa.com", "ADMIN_EMAILS": "owner@example.com",
           "REPO": str(repo), "DEST": str(dest), **ONLINE}
    r = subprocess.run(["bash", str(repo / "platform" / "bootstrap.sh")], env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    at_up = env_file(stub["STUB_ENV_AT_UP"])
    assert {k: at_up.get(k) for k in ONLINE} == ONLINE
    # A re-run that passes none of them keeps them; one that passes a new value takes it.
    env = {k: v for k, v in env.items() if k not in ONLINE}
    r = subprocess.run(["bash", str(dest / "bootstrap.sh")], env={
        **env, "TURSO_AUTH_TOKEN": "rotated"}, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    at_up = env_file(stub["STUB_ENV_AT_UP"])
    assert at_up["TURSO_DATABASE_URL"] == ONLINE["TURSO_DATABASE_URL"]
    assert at_up["TURSO_AUTH_TOKEN"] == "rotated"


def test_tunnel_starts_the_stack_with_the_online_workspace_already_in_env(tmp_path, stub):
    here = tmp_path / "platform"
    platform_copy(here)
    env = {**stub, "BASE_DOMAIN": "cicatrixa.com", "ADMIN_EMAILS": "owner@example.com",
           "CLOUDFLARE_TUNNEL_TOKEN": "eyJtest", **ONLINE}
    r = subprocess.run(["bash", str(here / "tunnel.sh")], env=env, cwd=here,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    at_up = env_file(stub["STUB_ENV_AT_UP"])
    assert {k: at_up.get(k) for k in ONLINE} == ONLINE
