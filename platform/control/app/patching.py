"""Safe application of find/replace patches to a source file.

Split out of medic.py so the rules are testable without importing docker,
httpx or the rest of the control plane.

The rule that matters: a patch is applied only when its `find` text occurs
*exactly once* in the file. `str.replace` rewrites every occurrence, so a
`find` that matches twice silently edits a second site the model never saw
— on a customer's repository, pushed to their branch. Ambiguity is refused,
not guessed at.
"""

# A patch is a dict: {"file": str, "find": str, "replace": str}
# `replace` may be "" (a deliberate deletion) but must be present.

OK = None


def check(src: str, patch: dict) -> str | None:
    """Return None if the patch can be applied safely, else the reason it cannot."""
    find = patch.get("find")
    if not find:
        return "patch has no 'find' text"
    if "replace" not in patch or patch["replace"] is None:
        # Distinguished from an explicit "": a missing key used to be coerced to
        # "" further down, turning a malformed patch into a silent deletion.
        return "patch has no 'replace' text (a deletion must say so explicitly)"
    count = src.count(find)
    if count == 0:
        return "the code to replace is no longer in the file"
    if count > 1:
        return (f"the code to replace appears {count} times — ambiguous, "
                "refusing to guess which one was meant")
    return OK


def apply(src: str, patch: dict) -> str:
    """Apply a patch that has already passed check(). Replaces exactly one site."""
    reason = check(src, patch)
    if reason is not None:
        raise ValueError(reason)
    return src.replace(patch["find"], patch["replace"], 1)
