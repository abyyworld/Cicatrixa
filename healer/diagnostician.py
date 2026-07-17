"""
Diagnostician: CrashEvent + indexed source → structured root-cause diagnosis.
Uses an OpenAI reasoning model with a Pydantic-validated structured output.
"""
import logging

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from config import MODEL
from source_index import SourceIndex
from watchdog import CrashEvent

logger = logging.getLogger(__name__)


class Diagnosis(BaseModel):
    root_cause: str = Field(
        description="Plain-English explanation of the underlying defect, not just the symptom"
    )
    affected_file: str = Field(
        description="Relative path (within the indexed source) of the file containing the defect"
    )
    buggy_code: str = Field(
        description="The exact snippet of code believed to be at fault, verbatim from the source"
    )
    fix_hypothesis: str = Field(
        description="Concrete description of the change that would fix the defect"
    )
    reproduction_hint: str = Field(
        description="Precise input/call that triggers the bug (function, arguments, endpoint, payload)"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this diagnosis, 0-1")


SYSTEM = """You are the Diagnostician in an autonomous self-healing production system.
You receive a crash report (traceback + recent error log lines) from a live service
and its full indexed source code. Identify the true root cause — the defect in the
code, not the symptom in the logs. Be precise: quote the exact buggy code verbatim,
name the exact file, and describe an input that deterministically triggers the crash
so a failing test can be written from your reproduction hint."""


class Diagnostician:
    def __init__(self):
        self._client = AsyncOpenAI()

    async def diagnose(self, event: CrashEvent, index: SourceIndex) -> Diagnosis:
        prompt = (
            f"A production container ({event.container_name}) is crashing.\n\n"
            f"## Traceback\n```\n{event.traceback}\n```\n\n"
            f"## Recent error log lines\n```\n" + "\n".join(event.error_lines) + "\n```\n\n"
            f"## Source code\n{index.format_for_llm()}\n\n"
            "Diagnose the root cause."
        )
        resp = await self._client.responses.parse(
            model=MODEL,
            instructions=SYSTEM,
            input=prompt,
            text_format=Diagnosis,
        )
        diagnosis = resp.output_parsed
        logger.info(
            f"Diagnosis ({diagnosis.confidence:.0%}): {diagnosis.affected_file} — {diagnosis.root_cause}"
        )
        return diagnosis
