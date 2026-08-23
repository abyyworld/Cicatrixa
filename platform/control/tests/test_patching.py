"""Patches are applied to a customer's repository and pushed to their branch.
An ambiguous patch must be refused, never guessed at."""
import pytest

from app import patching

DUPLICATED = '''def total(items):
    return sum(i.price for i in items)

def subtotal(items):
    return sum(i.price for i in items)
'''


def test_a_unique_match_applies_once():
    src = "x = 1\ny = 2\n"
    patch = {"find": "x = 1", "replace": "x = 42"}
    assert patching.check(src, patch) is patching.OK
    assert patching.apply(src, patch) == "x = 42\ny = 2\n"


def test_a_duplicated_match_is_refused():
    """The regression: str.replace would have rewritten BOTH functions, silently
    editing a call site the model never looked at."""
    patch = {"find": "return sum(i.price for i in items)",
             "replace": "return sum(i.price * i.qty for i in items)"}
    reason = patching.check(DUPLICATED, patch)
    assert reason is not patching.OK
    assert "appears 2 times" in reason
    with pytest.raises(ValueError):
        patching.apply(DUPLICATED, patch)


def test_a_duplicated_match_never_corrupts_the_second_site():
    patch = {"find": "return sum(i.price for i in items)", "replace": "return 0"}
    try:
        result = patching.apply(DUPLICATED, patch)
    except ValueError:
        return  # refused, which is the point
    pytest.fail(f"ambiguous patch was applied: {result!r}")


def test_a_missing_match_is_refused():
    patch = {"find": "not in the file", "replace": "anything"}
    assert "no longer in the file" in patching.check("x = 1\n", patch)


def test_a_missing_replace_key_is_refused_rather_than_deleting():
    """`patch.get("replace") or ""` turned a malformed patch into a deletion of
    every occurrence of `find`."""
    patch = {"find": "x = 1"}
    reason = patching.check("x = 1\n", patch)
    assert reason is not patching.OK
    assert "deletion must say so explicitly" in reason


def test_a_null_replace_is_refused():
    assert patching.check("x = 1\n", {"find": "x = 1", "replace": None}) is not patching.OK


def test_an_explicit_empty_replace_is_a_legitimate_deletion():
    patch = {"find": "debug_print()\n", "replace": ""}
    assert patching.check("debug_print()\nreal_code()\n", patch) is patching.OK
    assert patching.apply("debug_print()\nreal_code()\n", patch) == "real_code()\n"


def test_an_empty_find_is_refused():
    # "" is in every string and str.count("") is len+1 — a guaranteed corruption.
    assert patching.check("anything", {"find": "", "replace": "x"}) is not patching.OK
