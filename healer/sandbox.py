"""
Ephemeral container runner: executes a command in a throwaway container
with the (possibly patched) app source bind-mounted from the host.
Used by the Reproducer to verify the failing test and by the Fixer to
run the test suite against candidate patches.
"""
import asyncio
import logging

import docker

from config import APP_IMAGE, HOST_APP_SRC

logger = logging.getLogger(__name__)

_client = docker.from_env()

# test deps installed at runtime so the production image stays lean
# (httpx is needed by fastapi.testclient)
TEST_CMD = "pip install -q pytest httpx && python -m pytest {target} -q --tb=short"


async def run_tests(target: str = "tests/", image: str = APP_IMAGE) -> tuple[int, str]:
    """Run pytest against `target` inside an ephemeral container.
    Returns (exit_code, combined output)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_tests_sync, target, image)


def _run_tests_sync(target: str, image: str) -> tuple[int, str]:
    container = _client.containers.run(
        image,
        command=["sh", "-c", TEST_CMD.format(target=target)],
        volumes={HOST_APP_SRC: {"bind": "/app", "mode": "rw"}},
        working_dir="/app",
        detach=True,
    )
    try:
        result = container.wait(timeout=180)
        exit_code = result.get("StatusCode", 1)
        output = container.logs().decode("utf-8", errors="replace")
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    logger.info(f"Ephemeral test run [{target}] exit={exit_code}")
    return exit_code, output
