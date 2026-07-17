"""
Fixer: patches the diagnosed file until the reproduction test passes AND the
full test suite stays green. Iterates with test output as feedback, up to
MAX_FIX_ATTEMPTS. Produces a unified diff for the human gate / dashboard.
"""
import difflib
import logging
import os
from dataclasses import dataclass, field

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

import sandbox
from config import APP_SRC, MAX_FIX_ATTEMPTS, MODEL
from diagnostician import Diagnosis
from reproducer import Reproduction
from source_index import SourceIndex

logger = logging.getLogger(__name__)


class Patch(BaseModel):
    path: str = Field(description="Relative path of the file being rewritten")
    new_content: str = Field(description="The complete corrected file content")
    explanation: str = Field(description="What was changed and why it fixes the root cause")


@dataclass
class FixResult:
    success: bool
    attempts: int
    diff: str = ""
    explanation: str = ""
    test_output: str = ""
    original_contents: dict[str, str] = field(default_factory=dict)  # path -> pre-patch content

    def rollback(self):
        """Restore all patched files to their pre-fix content."""
        for rel_path, content in self.original_contents.items():
            with open(os.path.join(APP_SRC, rel_path), "w") as f:
                f.write(content)
        logger.info("Fixer changes rolled back")


SYSTEM = """You are the Fixer in an autonomous self-healing system. You receive a
root-cause diagnosis, a failing reproduction test, and the source code. Rewrite the
affected file so that:
1. The reproduction test passes,
2. The entire existing test suite stays green,
3. The change is minimal and targeted at the root cause — no refactors, no style
   changes, no new dependencies.
Return the COMPLETE corrected file content, not a fragment."""


class Fixer:
    def __init__(self):
        self._client = AsyncOpenAI()

    async def fix(self, diagnosis: Diagnosis, repro: Reproduction, index: SourceIndex) -> FixResult:
        original = self._read(diagnosis.affected_file)
        if original is None:
            return FixResult(success=False, attempts=0,
                             test_output=f"Affected file not found: {diagnosis.affected_file}")

        feedback = ""
        result = FixResult(success=False, attempts=0,
                           original_contents={diagnosis.affected_file: original})
        for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
            result.attempts = attempt
            patch = await self._generate(diagnosis, repro, feedback)
            target = patch.path if self._read(patch.path) is not None else diagnosis.affected_file
            if target not in result.original_contents:
                result.original_contents[target] = self._read(target) or ""
            self._write(target, patch.new_content)

            # Gate 1: the reproduction test must now pass
            repro_exit, repro_out = await sandbox.run_tests(target=repro.test_path)
            if repro_exit != 0:
                feedback = (
                    f"Your patch did NOT make the reproduction test pass.\n"
                    f"Pytest output:\n{repro_out}"
                )
                logger.warning(f"Fix attempt {attempt}: repro test still failing")
                continue

            # Gate 2: the full suite must stay green
            suite_exit, suite_out = await sandbox.run_tests(target="tests/")
            if suite_exit != 0:
                feedback = (
                    f"Your patch fixed the reproduction test but BROKE the full suite.\n"
                    f"Pytest output:\n{suite_out}"
                )
                logger.warning(f"Fix attempt {attempt}: full suite broken")
                continue

            result.success = True
            result.explanation = patch.explanation
            result.test_output = suite_out
            result.diff = self._diff(target, result.original_contents[target], patch.new_content)
            logger.info(f"Fix verified on attempt {attempt}: repro passes, suite green")
            return result

        result.rollback()
        result.test_output = feedback
        logger.error(f"Fixer gave up after {MAX_FIX_ATTEMPTS} attempts; changes rolled back")
        return result

    async def _generate(self, diagnosis: Diagnosis, repro: Reproduction, feedback: str) -> Patch:
        current = self._read(diagnosis.affected_file) or ""
        prompt = (
            f"## Diagnosis\n"
            f"Root cause: {diagnosis.root_cause}\n"
            f"Fix hypothesis: {diagnosis.fix_hypothesis}\n\n"
            f"## Failing reproduction test ({repro.test_path})\n"
            f"```python\n{repro.test_code}\n```\n\n"
            f"## Current content of {diagnosis.affected_file}\n"
            f"```\n{current}\n```\n\n"
        )
        if feedback:
            prompt += f"## Feedback on your previous patch\n{feedback}\n\n"
        prompt += "Produce the corrected file."
        resp = await self._client.responses.parse(
            model=MODEL,
            instructions=SYSTEM,
            input=prompt,
            text_format=Patch,
        )
        return resp.output_parsed

    def _read(self, rel_path: str) -> str | None:
        full = os.path.join(APP_SRC, rel_path)
        if not os.path.isfile(full):
            return None
        with open(full) as f:
            return f.read()

    def _write(self, rel_path: str, content: str):
        with open(os.path.join(APP_SRC, rel_path), "w") as f:
            f.write(content)

    @staticmethod
    def _diff(path: str, before: str, after: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
