"""A record marked verified when it wasn't poisons the asset permanently and
silently. These are the tests that make that impossible."""
import pytest

from app import verify


def ev(**kw) -> verify.Evidence:
    return verify.Evidence(**kw)


# ---------- the central rule ----------

def test_a_green_suite_alone_is_not_verification():
    """The suite passes, but no test executes the changed lines. That is exactly
    what a confidently wrong patch looks like."""
    e = ev(suite_ran=True, suite_passed=True,
           changed_lines={"pay.py": {10, 11}},
           covered_lines={"pay.py": {1, 2, 3}})     # covers the file, not the change
    assert verify.level_for(e) == "unverified_no_coverage"


def test_covering_the_changed_lines_earns_verified_existing_tests():
    e = ev(suite_ran=True, suite_passed=True,
           changed_lines={"pay.py": {10, 11}},
           covered_lines={"pay.py": {9, 10, 11}})
    assert verify.level_for(e) == "verified_existing_tests"


def test_one_covered_changed_line_is_enough():
    e = ev(suite_ran=True, suite_passed=True,
           changed_lines={"pay.py": {10, 11, 12}},
           covered_lines={"pay.py": {12}})
    assert verify.level_for(e) == "verified_existing_tests"


def test_coverage_in_a_different_file_does_not_count():
    e = ev(suite_ran=True, suite_passed=True,
           changed_lines={"pay.py": {10}},
           covered_lines={"other.py": {10}})
    assert verify.level_for(e) == "unverified_no_coverage"


def test_a_failing_suite_is_never_verification():
    e = ev(suite_ran=True, suite_passed=False,
           changed_lines={"pay.py": {10}}, covered_lines={"pay.py": {10}})
    assert verify.level_for(e) == "unverified_no_coverage"


def test_no_suite_at_all_is_unverified_not_an_error():
    """A legitimate terminal state, not a failure to work around."""
    assert verify.level_for(ev()) == "unverified_no_coverage"


# ---------- reproduction ----------

def test_a_reproduction_that_failed_before_and_passes_after_outranks_coverage():
    e = ev(suite_ran=True, suite_passed=True,
           repro_failed_before=True, repro_passed_after=True)
    assert verify.level_for(e) == "verified_reproduction"


def test_a_repro_that_never_failed_first_proves_nothing():
    """No repro, no patch. A test that passed on the buggy code too is not one."""
    e = ev(suite_ran=True, suite_passed=True,
           repro_failed_before=False, repro_passed_after=True)
    assert verify.level_for(e) == "unverified_no_coverage"


def test_a_repro_cannot_rescue_a_broken_suite():
    e = ev(suite_ran=True, suite_passed=False,
           repro_failed_before=True, repro_passed_after=True)
    assert verify.level_for(e) == "unverified_no_coverage"


def test_merged_and_stable_is_the_top_level():
    e = ev(suite_ran=True, suite_passed=True, merged=True, stable_after_merge=True,
           changed_lines={"pay.py": {10}}, covered_lines={"pay.py": {10}})
    assert verify.level_for(e) == "verified_merged_stable"


def test_merged_without_stability_does_not_reach_the_top():
    e = ev(suite_ran=True, suite_passed=True, merged=True, stable_after_merge=False,
           changed_lines={"pay.py": {10}}, covered_lines={"pay.py": {10}})
    assert verify.level_for(e) == "verified_existing_tests"


# ---------- the guard ----------

@pytest.mark.parametrize("claimed", [
    "verified_existing_tests", "verified_reproduction", "verified_merged_stable",
])
def test_no_level_can_be_recorded_above_its_evidence(claimed):
    """The requirement, directly: a caller cannot write a level the run did not earn."""
    with pytest.raises(ValueError, match="cannot record"):
        verify.assert_supported(claimed, ev())


def test_recording_at_or_below_the_evidence_is_allowed():
    e = ev(suite_ran=True, suite_passed=True,
           changed_lines={"pay.py": {10}}, covered_lines={"pay.py": {10}})
    verify.assert_supported("verified_existing_tests", e)
    verify.assert_supported("unverified_no_coverage", e)   # downgrading is fine


def test_an_unknown_level_is_rejected():
    with pytest.raises(ValueError, match="unknown verification level"):
        verify.assert_supported("definitely_fine_trust_me", ev())


def test_levels_are_strictly_ordered():
    assert [verify.rank(x) for x in verify.LEVELS] == [0, 1, 2, 3]


# ---------- changed-line computation ----------

def test_changed_lines_finds_a_modified_line():
    before = "a = 1\nb = 2\nc = 3\n"
    after = "a = 1\nb = 99\nc = 3\n"
    assert verify.changed_lines(before, after) == {2}


def test_changed_lines_finds_inserted_lines():
    assert verify.changed_lines("a = 1\n", "a = 1\nb = 2\nc = 3\n") == {2, 3}


def test_a_pure_deletion_yields_no_changed_lines():
    # Nothing new exists to be covered, so there is nothing to intersect.
    assert verify.changed_lines("a = 1\nb = 2\n", "a = 1\n") == set()


def test_an_unchanged_file_yields_nothing():
    assert verify.changed_lines("a = 1\n", "a = 1\n") == set()


# ---------- discovery ----------

def test_a_repo_with_no_tests_is_not_detected(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    assert verify.detect_pytest(str(tmp_path)) is None


def test_a_pytest_repo_is_detected(tmp_path):
    (tmp_path / "main.py").write_text("def f(): return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_f(): assert True\n")
    plan = verify.detect_pytest(str(tmp_path))
    assert plan["runner"] == "pytest"
    assert "tests/test_main.py" in plan["test_files"]


def test_the_suffix_naming_convention_is_detected(tmp_path):
    (tmp_path / "main_test.py").write_text("def test_x(): assert True\n")
    assert verify.detect_pytest(str(tmp_path))["test_files"] == ["main_test.py"]


def test_vendored_directories_are_not_mistaken_for_a_suite(tmp_path):
    """node_modules and .venv ship their own tests; finding those would claim a
    suite the customer does not have."""
    vendored = tmp_path / "node_modules" / "pkg"
    vendored.mkdir(parents=True)
    (vendored / "test_vendor.py").write_text("def test_v(): assert True\n")
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "test_dep.py").write_text("def test_d(): assert True\n")
    assert verify.detect_pytest(str(tmp_path)) is None


# ---------- collection, with the container runner injected ----------

def _repo(tmp_path):
    (tmp_path / "pay.py").write_text("def total(x):\n    return x\n")
    (tmp_path / "test_pay.py").write_text("from pay import total\ndef test_t(): total(1)\n")
    return str(tmp_path)


def test_collect_records_a_pass_and_the_covered_lines(tmp_path):
    workdir = _repo(tmp_path)
    output = ('1 passed\n' + verify.COVERAGE_MARKER +
              '\n{"files": {"pay.py": {"executed_lines": [1, 2]}}}')
    e = verify.collect(workdir, {"pay.py": ("def total(x):\n    return x\n",
                                            "def total(x):\n    return x * 2\n")},
                       run=lambda cmd: (0, output))
    assert e.suite_ran and e.suite_passed
    assert e.changed_lines == {"pay.py": {2}}
    assert e.covered_changed_lines() == {"pay.py": {2}}
    assert verify.level_for(e) == "verified_existing_tests"


def test_collect_on_a_repo_without_tests_never_runs_anything(tmp_path):
    (tmp_path / "pay.py").write_text("x = 1\n")

    def explode(cmd):
        pytest.fail("the suite must not run when no tests were detected")

    e = verify.collect(str(tmp_path), {"pay.py": ("x = 1\n", "x = 2\n")}, run=explode)
    assert not e.suite_ran
    assert verify.level_for(e) == "unverified_no_coverage"


def test_collect_records_a_failing_suite_honestly(tmp_path):
    workdir = _repo(tmp_path)
    e = verify.collect(workdir, {"pay.py": ("a\n", "b\n")},
                       run=lambda cmd: (1, "1 failed\n" + verify.COVERAGE_MARKER + "\n{}"))
    assert e.suite_ran and not e.suite_passed
    assert verify.level_for(e) == "unverified_no_coverage"


def test_unparseable_coverage_degrades_to_no_coverage_not_a_crash(tmp_path):
    """A truncated or garbled coverage report must never be read as coverage."""
    workdir = _repo(tmp_path)
    e = verify.collect(workdir, {"pay.py": ("a\n", "b\n")},
                       run=lambda cmd: (0, "1 passed\n" + verify.COVERAGE_MARKER +
                                        "\n{not json at all"))
    assert e.suite_passed
    assert e.covered_lines == {}
    assert verify.level_for(e) == "unverified_no_coverage"


def test_missing_coverage_marker_degrades_safely(tmp_path):
    workdir = _repo(tmp_path)
    e = verify.collect(workdir, {"pay.py": ("a\n", "b\n")},
                       run=lambda cmd: (0, "1 passed, coverage never ran"))
    assert e.covered_lines == {}
    assert verify.level_for(e) == "unverified_no_coverage"
