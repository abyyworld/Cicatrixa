"""Verification evidence and the levels it does — and does not — support.

The product's trust position is that it refuses to guess. A fix recorded as
verified when it wasn't poisons the record permanently and silently, because
nothing downstream ever re-derives it.

So the level is never *asserted* by a caller. It is *computed* from evidence
collected in the same run, by level_for(). assert_supported() exists so a
caller that tries to persist a level above its evidence raises instead of
writing.

The rule that does the work: a green suite is not verification. If no test
executes the lines the patch changed, the suite passing says only that the
patch broke nothing it already covered — which is exactly the state a
confidently wrong patch produces.
"""
import difflib
import json
import os
import re
from dataclasses import dataclass, field

# Ordered weakest → strongest. Comparison is by index, so order matters.
LEVELS = (
    "unverified_no_coverage",
    "verified_existing_tests",
    "verified_reproduction",
    "verified_merged_stable",
)
UNVERIFIED = LEVELS[0]


def rank(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError:
        raise ValueError(f"unknown verification level: {level!r}")


@dataclass
class Evidence:
    """Everything observed about one fix. Absent evidence is False/empty, never
    None-as-maybe — 'we did not look' and 'we looked and it was not there' both
    mean the level cannot be claimed."""

    suite_ran: bool = False
    suite_passed: bool = False
    # Line numbers in the post-fix file, per repo-relative path.
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    covered_lines: dict[str, set[int]] = field(default_factory=dict)
    # A generated test that provably failed before the patch and passes after.
    repro_failed_before: bool = False
    repro_passed_after: bool = False
    merged: bool = False
    stable_after_merge: bool = False

    def covered_changed_lines(self) -> dict[str, set[int]]:
        """The intersection that matters: changed lines a test actually executed."""
        out = {}
        for path, lines in self.changed_lines.items():
            hit = lines & self.covered_lines.get(path, set())
            if hit:
                out[path] = hit
        return out

    def exercises_the_change(self) -> bool:
        return bool(self.covered_changed_lines())


def level_for(evidence: Evidence) -> str:
    """The only way to obtain a verification level."""
    if evidence.merged and evidence.stable_after_merge and evidence.suite_passed \
            and evidence.exercises_the_change():
        return "verified_merged_stable"
    if evidence.repro_failed_before and evidence.repro_passed_after \
            and evidence.suite_ran and evidence.suite_passed:
        # A reproduction is self-evidently coverage of the change: it failed on
        # the old code and passes on the new one.
        return "verified_reproduction"
    if evidence.suite_ran and evidence.suite_passed and evidence.exercises_the_change():
        return "verified_existing_tests"
    return UNVERIFIED


def assert_supported(level: str, evidence: Evidence) -> None:
    """Raise unless `level` is at or below what the evidence supports."""
    supported = level_for(evidence)
    if rank(level) > rank(supported):
        raise ValueError(
            f"cannot record {level!r}: the evidence supports at most {supported!r} "
            f"(suite_ran={evidence.suite_ran}, suite_passed={evidence.suite_passed}, "
            f"changed lines exercised={evidence.exercises_the_change()}, "
            f"repro={evidence.repro_failed_before and evidence.repro_passed_after})")


# ---------- changed lines ----------

def changed_lines(before: str, after: str) -> set[int]:
    """1-based line numbers in `after` that are new or modified.

    Deletions are deliberately not represented: a deleted line cannot be
    covered by a test, so counting it would only ever weaken the intersection.
    """
    out: set[int] = set()
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


# ---------- test discovery ----------

_TEST_FILE_RE = re.compile(r"^(test_.*|.*_test)\.py$")
_CONFIG_FILES = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages",
              ".tox", ".mypy_cache", ".pytest_cache", "build", "dist"}


def detect_pytest(workdir: str) -> dict | None:
    """Return a test plan for a Python repo with a pytest suite, else None.

    Python-only on purpose. A structural notion of "the test that covers this
    call site" does not port to node/go without per-ecosystem work, and a
    half-right answer here writes a wrong verification level.
    """
    test_files: list[str] = []
    for root, dirs, names in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in names:
            if _TEST_FILE_RE.match(name):
                test_files.append(os.path.relpath(os.path.join(root, name), workdir))
    if not test_files:
        return None

    configured = [f for f in _CONFIG_FILES
                  if os.path.exists(os.path.join(workdir, f))]
    requirements = [f for f in ("requirements-dev.txt", "requirements-test.txt",
                                "dev-requirements.txt", "test-requirements.txt")
                    if os.path.exists(os.path.join(workdir, f))]
    return {
        "runner": "pytest",
        "test_files": sorted(test_files),
        "config_files": configured,
        "dev_requirements": requirements,
    }


# ---------- suite execution ----------

# `coverage` rather than pytest-cov: one dependency, and `coverage json` gives a
# line-level executed map we can intersect directly.
INSTALL_CMD = "pip install --quiet --disable-pip-version-check pytest coverage"


def suite_command(plan: dict, source_dirs: list[str] | None = None,
                  install: bool = True) -> str:
    """The shell command run inside the container. Returns coverage as JSON on
    a marker-delimited final line so one exec yields both outcomes.

    `install` is False when pytest and coverage are already present — the test
    suite uses that to exercise this exact command locally.
    """
    include = ""
    if source_dirs:
        include = " --include=" + ",".join(f"{d}/*" for d in sorted(source_dirs))
    return (
        (f"{INSTALL_CMD} >/dev/null 2>&1 || true; " if install else "") +
        f"python -m coverage run{include} -m pytest -q; "
        "rc=$?; "
        "echo '---CX-COVERAGE---'; "
        "python -m coverage json -o - 2>/dev/null || echo '{}'; "
        "exit $rc"
    )


COVERAGE_MARKER = "---CX-COVERAGE---"


def parse_output(output: str) -> tuple[str, dict[str, set[int]]]:
    """Split combined output into (pytest text, executed lines per file)."""
    head, _, tail = output.partition(COVERAGE_MARKER)
    covered: dict[str, set[int]] = {}
    tail = tail.strip()
    if tail:
        start = tail.find("{")
        if start != -1:
            try:
                data = json.loads(tail[start:])
                for path, info in (data.get("files") or {}).items():
                    executed = info.get("executed_lines") or []
                    covered[os.path.normpath(path)] = set(executed)
            except (json.JSONDecodeError, AttributeError):
                covered = {}
    return head.strip(), covered


def collect(workdir: str, changed: dict[str, str], run, install: bool = True) -> Evidence:
    """Run the suite and build Evidence.

    `changed` maps repo-relative path -> (before, after) source, as a 2-tuple.
    `run` is a callable (command: str) -> (exit_code: int, output: str) that
    executes inside the built image. Injected so the rules above are testable
    without a Docker daemon.
    """
    evidence = Evidence()
    for path, (before, after) in changed.items():
        lines = changed_lines(before, after)
        if lines:
            evidence.changed_lines[os.path.normpath(path)] = lines

    plan = detect_pytest(workdir)
    if not plan:
        # No suite: unverified_no_coverage, which is a legitimate terminal state.
        return evidence

    source_dirs = sorted({os.path.dirname(p) for p in evidence.changed_lines
                          if os.path.dirname(p)})
    exit_code, output = run(suite_command(plan, source_dirs, install=install))
    evidence.suite_ran = True
    evidence.suite_passed = (exit_code == 0)
    _, covered = parse_output(output)
    evidence.covered_lines = covered
    return evidence
