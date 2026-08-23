"""End-to-end against a real pytest suite under real coverage.

The unit tests mock the container runner, so they prove the rules but not the
command. This runs verify.suite_command() verbatim through a shell against a
throwaway repo, so the coverage JSON shape and the changed-line intersection
are exercised for real.
"""
import shutil
import subprocess

import pytest

from app import verify

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="needs a shell")


def local_runner(workdir):
    """Stands in for engine.test_runner(): same contract, no Docker."""
    def run(command):
        r = subprocess.run(["sh", "-lc", command], cwd=workdir,
                           capture_output=True, text=True, timeout=180)
        return r.returncode, r.stdout + r.stderr
    return run


def _write(tmp_path, pay_src, test_src):
    (tmp_path / "pay.py").write_text(pay_src)
    (tmp_path / "test_pay.py").write_text(test_src)
    return str(tmp_path)


COVERED_TEST = "from pay import total\n\n\ndef test_total():\n    assert total(2, 3) == 6\n"
UNRELATED_TEST = "from pay import unrelated\n\n\ndef test_u():\n    assert unrelated() == 1\n"


def test_a_fix_covered_by_the_suite_reaches_verified_existing_tests(tmp_path):
    before = "def total(a, b):\n    return a + b\n\n\ndef unrelated():\n    return 1\n"
    after = "def total(a, b):\n    return a * b\n\n\ndef unrelated():\n    return 1\n"
    workdir = _write(tmp_path, after, COVERED_TEST)

    evidence = verify.collect(workdir, {"pay.py": (before, after)},
                              run=local_runner(workdir), install=False)

    assert evidence.suite_ran and evidence.suite_passed
    assert evidence.changed_lines == {"pay.py": {2}}
    # line 2 is the return we changed, and the test executes it
    assert evidence.covered_changed_lines() == {"pay.py": {2}}
    assert verify.level_for(evidence) == "verified_existing_tests"


def test_a_green_suite_that_never_touches_the_fix_stays_unverified(tmp_path):
    """The failure mode that matters: the suite passes, coverage is real, but
    nothing exercises the changed line."""
    before = "def total(a, b):\n    return a + b\n\n\ndef unrelated():\n    return 1\n"
    after = "def total(a, b):\n    return a * b\n\n\ndef unrelated():\n    return 1\n"
    workdir = _write(tmp_path, after, UNRELATED_TEST)

    evidence = verify.collect(workdir, {"pay.py": (before, after)},
                              run=local_runner(workdir), install=False)

    assert evidence.suite_passed, "the suite itself is green"
    assert evidence.covered_lines.get("pay.py"), "coverage really ran"
    assert evidence.covered_changed_lines() == {}, "but not on the changed line"
    assert verify.level_for(evidence) == "unverified_no_coverage"
    with pytest.raises(ValueError):
        verify.assert_supported("verified_existing_tests", evidence)


def test_a_failing_suite_is_recorded_as_unverified(tmp_path):
    before = "def total(a, b):\n    return a + b\n"
    after = "def total(a, b):\n    return a - b\n"     # breaks the test
    workdir = _write(tmp_path, after, COVERED_TEST)

    evidence = verify.collect(workdir, {"pay.py": (before, after)},
                              run=local_runner(workdir), install=False)

    assert evidence.suite_ran and not evidence.suite_passed
    assert verify.level_for(evidence) == "unverified_no_coverage"
