"""
Ephemeral container runner: executes pytest in a throwaway container with the
(possibly patched) app source bind-mounted from the host.

A dedicated test-runner image (app image + pytest/httpx) is built once and
reused, so test runs are fast and never hit the network.
"""
import asyncio
import io
import logging
import threading

import docker

from config import APP_IMAGE, HOST_APP_SRC

logger = logging.getLogger(__name__)

_client = docker.from_env()

TEST_IMAGE = "orderservice:testrunner"
TEST_DOCKERFILE = f"FROM {APP_IMAGE}\nRUN pip install --no-cache-dir pytest httpx\n"
_image_lock = threading.Lock()
_image_ready = False


def _ensure_test_image():
    global _image_ready
    with _image_lock:
        if _image_ready:
            return
        try:
            _client.images.get(TEST_IMAGE)
        except docker.errors.ImageNotFound:
            logger.info(f"Building test-runner image {TEST_IMAGE} (one-time)")
            _client.images.build(
                fileobj=io.BytesIO(TEST_DOCKERFILE.encode()), tag=TEST_IMAGE, rm=True
            )
        _image_ready = True


async def run_tests(target: str = "tests/") -> tuple[int, str]:
    """Run pytest against `target` inside an ephemeral container.
    Returns (exit_code, combined output)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_tests_sync, target)


def _run_tests_sync(target: str) -> tuple[int, str]:
    _ensure_test_image()
    container = _client.containers.run(
        TEST_IMAGE,
        command=["python", "-m", "pytest", target, "-q", "--tb=short"],
        volumes={HOST_APP_SRC: {"bind": "/app", "mode": "rw"}},
        working_dir="/app",
        network_disabled=True,
        detach=True,
    )
    try:
        result = container.wait(timeout=120)
        exit_code = result.get("StatusCode", 1)
        output = container.logs().decode("utf-8", errors="replace")
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    logger.info(f"Ephemeral test run [{target}] exit={exit_code}")
    return exit_code, output
