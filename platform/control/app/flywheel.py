"""The migration-transform library: what broke, and the reusable fix for it.

Two records. `break_observation` is one per incident, tenant-scoped, and may
contain customer specifics. `transform` is promoted, reusable, tenant-agnostic
and contains **zero customer code** — insert_transform enforces that on write
rather than trusting callers, because a single leak is permanent.

The point of the pair is that the library gets cheaper over time: lookup()
runs *before* the model is asked to generate anything, and records hit or miss.
That hit rate is the measurement of whether the flywheel is turning at all.
"""
import json
import re
import time

from . import db, verify

TRIGGERS = ("crash", "ci_failure", "dependency_bump", "drift_watch")
BREAK_KINDS = ("signature_change", "symbol_removed", "symbol_moved",
               "return_shape_change", "default_changed", "behaviour_change", "unknown")
LOOKUP_RESULTS = ("hit", "miss")
PR_STATES = ("open", "merged", "closed")
CONFIDENCE_TIERS = ("candidate", "provisional", "established")

# Promotion bar. Enforced here so a transform cannot exist without it, even
# though nothing promotes automatically yet.
MIN_SUPPORTING_OBSERVATIONS = 2
MIN_DISTINCT_TENANTS = 2
MIN_SUPPORTING_LEVEL = "verified_reproduction"


def _one_of(value, allowed, field):
    if value not in allowed:
        raise ValueError(f"{field} must be one of {allowed}, got {value!r}")
    return value


# ---------- hook 1: incident ingested ----------

def observe(user_id: int, *, project_id=None, service_id=None, trigger="crash") -> int:
    """Create the record the moment an incident enters. Nulls are expected —
    almost nothing is known yet, and a record that waits for certainty is a
    record that never gets written."""
    _one_of(trigger, TRIGGERS, "trigger")
    cur = db.q("INSERT INTO break_observation (user_id, project_id, service_id, "
               "trigger, created_at, updated_at) VALUES (?,?,?,?,?,?)",
               (user_id, project_id, service_id, trigger, db.now(), db.now()))
    return cur.lastrowid


def _update(observation_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = db.now()
    cols = ", ".join(f"{k}=?" for k in fields)
    db.q(f"UPDATE break_observation SET {cols} WHERE id=?",
         (*fields.values(), observation_id))


def get(observation_id: int):
    return db.one("SELECT * FROM break_observation WHERE id=?", (observation_id,))


# ---------- hook 2: root cause determined ----------

def set_root_cause(observation_id: int, *, vendor_package=None, version_from=None,
                   version_to=None, symbol_path=None, break_kind="unknown"):
    """`unknown` is a legitimate break_kind and the common one. Forcing a guess
    here would put noise into the only field that makes transforms selectable."""
    _one_of(break_kind, BREAK_KINDS, "break_kind")
    _update(observation_id, vendor_package=vendor_package,
            vendor_version_from=version_from, vendor_version_to=version_to,
            symbol_path=symbol_path, break_kind=break_kind)


# ---------- hook 3: library lookup, BEFORE generation ----------

def lookup(observation_id: int, *, vendor_package=None, symbol_path=None,
           break_kind=None, fingerprint=None) -> tuple[str, int | None]:
    """Query the library and record hit/miss. Must run before the model is asked
    to generate a patch — that ordering is the entire measurement. Returns
    (result, transform_id)."""
    row = None
    if vendor_package and symbol_path:
        sql = ("SELECT * FROM transform WHERE vendor_package=? AND symbol_path=? "
               "AND promoted_at IS NOT NULL")
        args = [vendor_package, symbol_path]
        if break_kind and break_kind != "unknown":
            sql += " AND break_kind=?"
            args.append(break_kind)
        sql += " ORDER BY promoted_at DESC LIMIT 1"
        row = db.one(sql, tuple(args))

    result = "hit" if row else "miss"
    _update(observation_id, library_lookup_result=result,
            matched_transform_id=row["id"] if row else None,
            call_site_fingerprint=fingerprint)
    return result, (row["id"] if row else None)


# ---------- hook 4: patch generated ----------

def set_fix(observation_id: int, *, fingerprint=None):
    _update(observation_id, call_site_fingerprint=fingerprint)


# ---------- hook 5: verification run ----------

def set_verification(observation_id: int, evidence: verify.Evidence):
    """The level is computed from the evidence, never passed in. assert_supported
    is redundant with level_for by construction and is called anyway: this is the
    one write where being wrong is silent and permanent."""
    level = verify.level_for(evidence)
    verify.assert_supported(level, evidence)
    _update(observation_id, verification_level=level,
            verification_evidence=json.dumps({
                "suite_ran": evidence.suite_ran,
                "suite_passed": evidence.suite_passed,
                "changed_lines": {k: sorted(v) for k, v in evidence.changed_lines.items()},
                "covered_changed_lines": {k: sorted(v) for k, v
                                          in evidence.covered_changed_lines().items()},
                "repro_failed_before": evidence.repro_failed_before,
                "repro_passed_after": evidence.repro_passed_after,
            }))
    return level


# ---------- hooks 6 and 7: pull request lifecycle ----------

def set_pr_opened(observation_id: int, *, repo_full: str, number: int, url: str,
                  branch: str):
    _update(observation_id, pr_repo_full=repo_full, pr_number=number, pr_url=url,
            pr_branch=branch, pr_state="open", pr_opened_at=db.now())


def set_pr_closed(observation_id: int, *, state: str, merged_at: float | None = None):
    _one_of(state, PR_STATES, "pr_state")
    _update(observation_id, pr_state=state,
            merged_at=merged_at if state == "merged" else None)


def find_by_pr(repo_full: str, number: int):
    return db.one("SELECT * FROM break_observation WHERE pr_repo_full=? AND pr_number=?",
                  (repo_full, number))


def find_by_pr_branch(repo_full: str, branch: str):
    return db.one("SELECT * FROM break_observation WHERE pr_repo_full=? AND pr_branch=? "
                  "AND pr_state='open' ORDER BY id DESC LIMIT 1", (repo_full, branch))


# ---------- hook 8: a human pushed to our PR branch ----------

def add_human_commits(observation_id: int, count: int):
    """The kill metric. A PR that merged untouched is one we did not need a human
    for; a PR a human had to push to is one we got wrong. Without this the
    unattended rate is fiction."""
    if count <= 0:
        return
    db.q("UPDATE break_observation SET human_commits_on_pr = human_commits_on_pr + ?, "
         "updated_at=? WHERE id=?", (count, db.now(), observation_id))


# ---------- the metric ----------

def unattended_merge_rate(since: float, until: float | None = None) -> dict:
    """Of the PRs opened in the window, what share merged with no human commits.

    Windowed on pr_opened_at, not merged_at: the denominator is 'PRs we opened',
    so a PR that is still open counts against us rather than being invisible.
    """
    until = db.now() if until is None else until
    row = db.one(
        "SELECT COUNT(*) AS opened, "
        "  SUM(CASE WHEN pr_state='merged' THEN 1 ELSE 0 END) AS merged, "
        "  SUM(CASE WHEN pr_state='merged' AND human_commits_on_pr = 0 "
        "      THEN 1 ELSE 0 END) AS unattended "
        "FROM break_observation "
        "WHERE pr_opened_at IS NOT NULL AND pr_opened_at >= ? AND pr_opened_at < ?",
        (since, until))
    opened = row["opened"] or 0
    merged = row["merged"] or 0
    unattended = row["unattended"] or 0
    return {
        "window_start": since, "window_end": until,
        "prs_opened": opened, "prs_merged": merged, "merged_unattended": unattended,
        "unattended_merge_rate": (unattended / opened) if opened else None,
        "unattended_share_of_merged": (unattended / merged) if merged else None,
    }


def library_hit_rate(since: float, until: float | None = None) -> dict:
    """Is the flywheel turning? A rising hit rate is the only evidence that the
    library is worth more than the model that filled it."""
    until = db.now() if until is None else until
    row = db.one(
        "SELECT COUNT(*) AS looked_up, "
        "  SUM(CASE WHEN library_lookup_result='hit' THEN 1 ELSE 0 END) AS hits "
        "FROM break_observation "
        "WHERE library_lookup_result IS NOT NULL AND created_at >= ? AND created_at < ?",
        (since, until))
    looked_up = row["looked_up"] or 0
    hits = row["hits"] or 0
    return {"lookups": looked_up, "hits": hits,
            "hit_rate": (hits / looked_up) if looked_up else None}


# ---------- the transform table, and what may not enter it ----------

_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_VERSION_RANGE = re.compile(r"^[0-9A-Za-z.,<>=!*~^ |()-]{0,120}$")
# Anything that looks like source rather than a name. Newlines are the giveaway,
# but a single-line snippet is possible too.
_SOURCE_MARKERS = re.compile(
    r"(\bdef\b|\bclass\b|\bimport\b|\breturn\b|\blambda\b|=>|;|\{|\}|#|\"\"\"|''')")
MAX_PATTERN_LEAF = 120


def _reject_source(value: str, field: str):
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} contains a newline — customer source must never "
                         f"reach the transform table")
    if _SOURCE_MARKERS.search(value):
        raise ValueError(f"{field} looks like source code, not a structural "
                         f"description: {value[:60]!r}")
    if len(value) > MAX_PATTERN_LEAF:
        raise ValueError(f"{field} is {len(value)} chars; structural descriptions "
                         f"are short, source is long")


def _check_pattern(node, field="match_pattern"):
    """Walk a libcst matcher spec and reject any leaf that could be source."""
    if isinstance(node, str):
        _reject_source(node, field)
    elif isinstance(node, dict):
        for key, val in node.items():
            _reject_source(str(key), f"{field} key")
            _check_pattern(val, field)
    elif isinstance(node, list):
        for item in node:
            _check_pattern(item, field)
    elif isinstance(node, (int, float, bool)) or node is None:
        pass
    else:
        raise ValueError(f"{field} contains an unsupported type: {type(node).__name__}")


def eligible_for_promotion(supporting: list[dict]) -> tuple[bool, str]:
    """>=2 observations, across >=2 distinct tenants, at verified_reproduction or
    above. Never from one observation at any level; never from
    unverified_no_coverage at any count."""
    strong = [s for s in supporting
              if verify.rank(s.get("level", verify.UNVERIFIED))
              >= verify.rank(MIN_SUPPORTING_LEVEL)]
    if len(strong) < MIN_SUPPORTING_OBSERVATIONS:
        return False, (f"needs {MIN_SUPPORTING_OBSERVATIONS} observations at "
                       f"{MIN_SUPPORTING_LEVEL} or above, has {len(strong)}")
    tenants = {s.get("user_id") for s in strong if s.get("user_id") is not None}
    if len(tenants) < MIN_DISTINCT_TENANTS:
        return False, (f"needs {MIN_DISTINCT_TENANTS} distinct tenants, has "
                       f"{len(tenants)}")
    return True, ""


def insert_transform(*, vendor_package: str, symbol_path: str, break_kind: str,
                     match_pattern: dict, rewrite_ref: str,
                     supporting_observations: list[dict],
                     applies_to_version_range: str | None = None,
                     confidence_tier: str = "candidate",
                     promoted_at: float | None = None) -> int:
    """Create a transform. Every field is shape-checked, because this table is
    tenant-agnostic and shared: a leak here is visible to every other customer.

    rewrite_ref stores the *name of a codemod callable*, not a diff. A stored
    textual diff is worthless against code it has never seen — that is the dead
    end this design exists to avoid.
    """
    if not _PACKAGE.match(vendor_package or ""):
        raise ValueError(f"vendor_package is not a package name: {vendor_package!r}")
    if not _DOTTED.match(symbol_path or ""):
        raise ValueError(f"symbol_path is not a dotted symbol: {symbol_path!r}")
    if not _DOTTED.match(rewrite_ref or "") or "." not in (rewrite_ref or ""):
        raise ValueError(f"rewrite_ref must be a dotted name of a codemod callable "
                         f"in transforms/, not code: {rewrite_ref!r}")
    _one_of(break_kind, BREAK_KINDS, "break_kind")
    _one_of(confidence_tier, CONFIDENCE_TIERS, "confidence_tier")
    if applies_to_version_range is not None \
            and not _VERSION_RANGE.match(applies_to_version_range):
        raise ValueError(f"applies_to_version_range is not a version spec: "
                         f"{applies_to_version_range!r}")
    if not isinstance(match_pattern, dict):
        raise ValueError("match_pattern must be a structural matcher spec (dict)")
    _check_pattern(match_pattern)

    ok, why = eligible_for_promotion(supporting_observations)
    if not ok:
        raise ValueError(f"refusing to create a transform: {why}")

    cur = db.q(
        "INSERT INTO transform (vendor_package, applies_to_version_range, symbol_path, "
        "break_kind, match_pattern, rewrite_ref, supporting_observations, "
        "confidence_tier, promoted_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (vendor_package, applies_to_version_range, symbol_path, break_kind,
         json.dumps(match_pattern), rewrite_ref,
         json.dumps([{"id": s.get("id"), "level": s.get("level")}
                     for s in supporting_observations]),
         confidence_tier, promoted_at if promoted_at is not None else db.now(),
         db.now()))
    return cur.lastrowid


def supporting_from_ids(observation_ids: list[int]) -> list[dict]:
    """Build the supporting-observation list from real records, so the promotion
    bar is checked against what was actually verified rather than what a caller
    claims."""
    out = []
    for oid in observation_ids:
        row = get(oid)
        if row:
            out.append({"id": row["id"], "level": row["verification_level"],
                        "user_id": row["user_id"]})
    return out


# ---------- PR mode ----------

def pr_mode_enabled(service) -> bool:
    """Per-service opt-in. NULL inherits the platform default, so flipping the
    default never silently changes a service someone deliberately set."""
    value = service["pr_mode"] if "pr_mode" in service.keys() else None
    if value is not None:
        return bool(value)
    return db.setting("pr_mode_default", "1") == "1"


def time_window(days: int) -> float:
    return time.time() - days * 86400
