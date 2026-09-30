"""Knobs that decide whether one customer can hurt every other customer, and
whether the cx-plain escape hatch actually covers the apps it is meant to."""
import importlib

import pytest


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("BASE_DOMAIN", "cicatrixa.com")
    monkeypatch.setenv("BASE_URL", "https://app.cicatrixa.com")
    from app import engine as e
    return e


def _service(slug="shop"):
    return {"slug": slug, "project_id": 1, "user_id": 7, "name": slug}


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import engine as e
    return importlib.reload(e)


def test_user_apps_redirect_to_https_by_default(engine, monkeypatch):
    e = _reload(monkeypatch, BASE_URL="https://app.cicatrixa.com")
    labels = e._labels(_service(), 3000)
    assert labels["traefik.http.routers.cx-shop.middlewares"] == "cx-to-https"


def test_the_plain_http_escape_hatch_reaches_user_apps(engine, monkeypatch):
    """It used to cover only the dashboard: user apps hard-coded cx-to-https, so a
    certificate outage redirected every customer to a :443 that could not answer."""
    e = _reload(monkeypatch, BASE_URL="https://app.cicatrixa.com", HTTPS_REDIRECT_MW="cx-plain")
    labels = e._labels(_service(), 3000)
    assert labels["traefik.http.routers.cx-shop.middlewares"] == "cx-plain"


def test_builds_are_memory_capped(engine, monkeypatch):
    e = _reload(monkeypatch, BUILD_MEM_MB="1536")
    seen = {}

    class API:
        def build(self, **kw):
            seen.update(kw)
            return iter([{"stream": "Successfully built abc\n"}])

    class Client:
        api = API()

    monkeypatch.setattr(e, "dock", lambda: Client())
    e._build("/tmp", "Dockerfile", "cx-shop:abc", lambda _l: None)
    cap = 1536 * 1024 * 1024
    assert seen["container_limits"] == {"memory": cap, "memswap": cap}


def test_build_concurrency_is_tunable_and_never_zero(engine, monkeypatch):
    assert _reload(monkeypatch, BUILD_CONCURRENCY="1").BUILD_CONCURRENCY == 1
    assert _reload(monkeypatch, BUILD_CONCURRENCY="0").BUILD_CONCURRENCY == 1


def test_the_ai_is_told_the_host_architecture(monkeypatch):
    from app import ai
    monkeypatch.setattr(ai, "HOST_ARCH", "arm64")
    ctx = ai._arch_context()
    assert "linux/arm64" in ctx
    assert "Never use --platform" in ctx
    assert "exec format error" in ctx


def test_python_fallback_prefers_wheels(tmp_path):
    """On a slim image with no compiler, an sdist with no wheel for this
    architecture fails to build; --prefer-binary picks a wheel when one exists."""
    from app import ai
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "main.py").write_text("app = None\n")
    plan = ai.heuristic_plan(str(tmp_path))
    assert "--prefer-binary" in plan["dockerfile"]
