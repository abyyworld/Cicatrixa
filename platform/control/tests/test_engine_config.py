"""Knobs that decide whether one customer can hurt every other customer, and
whether the cx-plain escape hatch actually covers the apps it is meant to."""
import importlib

import pytest


@pytest.fixture
def engine(monkeypatch):
    """Tests here reload app.engine under different env. The reloaded module would
    otherwise outlive the test — module globals are read at import — so teardown
    restores the env FIRST and then rebuilds the module from it."""
    from app import engine as e
    monkeypatch.setenv("BASE_DOMAIN", "cicatrixa.com")
    monkeypatch.setenv("BASE_URL", "https://app.cicatrixa.com")
    yield e
    monkeypatch.undo()
    importlib.reload(e)


def _service(slug="shop"):
    return {"slug": slug, "project_id": 1, "user_id": 7, "name": slug}


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import engine as e
    return importlib.reload(e)


def test_user_apps_use_the_shared_middleware_not_a_frozen_value(engine, monkeypatch):
    """Labels are fixed when a container is created. If the switch's current value
    were baked in, flipping HTTPS_REDIRECT_MW later would do nothing for the apps
    already running — the ones a certificate outage is actually hurting."""
    for switch in ("cx-to-https", "cx-plain"):
        e = _reload(monkeypatch, BASE_URL="https://app.cicatrixa.com", HTTPS_REDIRECT_MW=switch)
        labels = e._labels(_service(), 3000)
        assert labels["traefik.http.routers.cx-shop.middlewares"] == "cx-app-web"


def test_the_shared_middleware_is_defined_on_the_control_plane():
    """cx-app-web only exists if docker-compose.yml defines it; a router pointing at
    an undefined middleware is dropped by Traefik, taking the app offline."""
    import pathlib
    compose = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    assert "traefik.http.middlewares.cx-app-web.chain.middlewares=" in compose


def test_no_certificate_order_includes_the_apex():
    """The apex lives on Vercel. Any certresolver router whose rule names it makes
    Let's Encrypt validate it, fail, and fail the whole order — app. included."""
    import pathlib, re
    compose = (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    secure = re.findall(r'routers\.([\w-]+)\.tls\.certresolver', compose)
    assert secure, "expected at least one certresolver router"
    for router in secure:
        rule = re.search(rf'routers\.{re.escape(router)}\.rule=(.*)"', compose).group(1)
        hosts = re.findall(r"Host\(`([^`]+)`\)", rule)
        assert hosts == ["app.${BASE_DOMAIN}"], f"{router} orders a cert for {hosts}"


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
