"""
Reproducer: writes a *failing test* that reproduces the diagnosed bug, then
proves it fails by running it in an ephemeral container against the current
(buggy) source. No patch is attempted until this test exists and fails —
that is what separates "LLM guesses a patch" from engineering.
"""
import logging
import os
import time
from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

import sandbox
from config import APP_SRC, MAX_REPRO_ATTEMPTS, MODEL
from diagnostician import Diagnosis
from source_index import SourceIndex

logger = logging.getLogger(__name__)


class GeneratedTest(BaseModel):
    file_name: str = Field(
        description="Test file name, e.g. test_repro_discount_crash.py (must start with test_)"
    )
    test_code: str = Field(description="Complete pytest file content")
    rationale: str = Field(description="Why this test reproduces the diagnosed bug")


@dataclass
class Reproduction:
    test_path: str        # relative to app source root, e.g. tests/test_repro_x.py
    test_code: str
    verified_failing: bool
    output: str           # pytest output from the verification run


SYSTEM = """You are the Reproducer in an autonomous self-healing system. Given a
root-cause diagnosis and the source code, write a single pytest file that:
1. FAILS on the current buggy code (by asserting the *correct* behavior — the bug
   will make it raise or return the wrong value),
2. Will PASS once the bug is properly fixed,
3. Imports the application code directly (unit-level, no network, no running server),
4. Is deterministic and self-contained.
The test runs with CWD at the app source root, so import modules by their file
names (e.g. `from main import create_order`). Do not test the bug's presence —
test the correct behavior its fix will restore."""


class Reproducer:
    def __init__(self):
        self._client = AsyncOpenAI()

    async def reproduce(self, diagnosis: Diagnosis, index: SourceIndex) -> Reproduction:
        feedback = ""
        last: Reproduction | None = None
        for attempt in range(1, MAX_REPRO_ATTEMPTS + 1):
            gen = await self._generate(diagnosis, index, feedback)
            rel_path = os.path.join("tests", gen.file_name)
            self._write_test(rel_path, gen.test_code)

            exit_code, output = await sandbox.run_tests(target=rel_path)
            last = Reproduction(
                test_path=rel_path,
                test_code=gen.test_code,
                verified_failing=(exit_code == 1),
                output=output,
            )
            if last.verified_failing:
                logger.info(f"Reproduced: {rel_path} fails on current code (attempt {attempt})")
                return last

            os.remove(os.path.join(APP_SRC, rel_path))
            if exit_code == 0:
                feedback = (
                    "Your previous test PASSED on the buggy code, so it does not "
                    f"reproduce the bug. Output:\n{output}\n"
                    "Write a test that exercises the exact crashing input from the diagnosis."
                )
            else:
                feedback = (
                    f"Your previous test errored (pytest exit {exit_code}) instead of failing "
                    f"cleanly — likely an import or collection problem. Output:\n{output}\n"
                    "Fix the test file itself."
                )
            logger.warning(f"Reproduction attempt {attempt} not a clean failure (exit {exit_code})")
        return last

    async def _generate(self, diagnosis: Diagnosis, index: SourceIndex, feedback: str) -> GeneratedTest:
        prompt = (
            f"## Diagnosis\n"
            f"Root cause: {diagnosis.root_cause}\n"
            f"Affected file: {diagnosis.affected_file}\n"
            f"Buggy code:\n```\n{diagnosis.buggy_code}\n```\n"
            f"Reproduction hint: {diagnosis.reproduction_hint}\n\n"
            f"## Source code\n{index.format_for_llm([diagnosis.affected_file])}\n\n"
        )
        if feedback:
            prompt += f"## Feedback on your previous attempt\n{feedback}\n\n"
        prompt += "Write the failing test."
        resp = await self._client.responses.parse(
            model=MODEL,
            instructions=SYSTEM,
            input=prompt,
            text_format=GeneratedTest,
        )
        return resp.output_parsed

    def _write_test(self, rel_path: str, code: str):
        full = os.path.join(APP_SRC, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(code)
        logger.info(f"Wrote reproduction test: {rel_path}")
