"""The library's two invariants: no customer source ever reaches the shared
transform table, and no transform exists without the evidence to justify it."""
import json

import pytest

from app import db, flywheel, verify


@pytest.fixture
def tenants(fresh_db):
    a = db.q("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
             ("a@example.com", "x", db.now())).lastrowid
    b = db.q("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
             ("b@example.com", "x", db.now())).lastrowid
    return a, b


def _observation(user_id, level="verified_reproduction", **kw):
    oid = flywheel.observe(user_id, **kw)
    db.q("UPDATE break_observation SET verification_level=? WHERE id=?", (level, oid))
    return oid


# ---------- record lifecycle ----------

def test_an_incident_records_before_anything_is_known(tenants):
    a, _ = tenants
    oid = flywheel.observe(a, trigger="crash")
    row = flywheel.get(oid)
    assert row["user_id"] == a
    assert row["vendor_package"] is None          # nulls are expected at ingest
    assert row["break_kind"] == "unknown"
    assert row["verification_level"] == "unverified_no_coverage"
    assert row["human_commits_on_pr"] == 0


def test_drift_watch_is_accepted_though_nothing_emits_it_yet(tenants):
    a, _ = tenants
    assert flywheel.get(flywheel.observe(a, trigger="drift_watch"))["trigger"] == "drift_watch"


def test_an_unknown_trigger_is_rejected(tenants):
    a, _ = tenants
    with pytest.raises(ValueError, match="trigger must be"):
        flywheel.observe(a, trigger="vibes")


def test_root_cause_fills_the_vendor_fields(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    flywheel.set_root_cause(oid, vendor_package="openai", version_from="0.28.1",
                            version_to="1.0.0", symbol_path="openai.ChatCompletion.create",
                            break_kind="symbol_moved")
    row = flywheel.get(oid)
    assert row["vendor_package"] == "openai"
    assert row["symbol_path"] == "openai.ChatCompletion.create"
    assert row["break_kind"] == "symbol_moved"


def test_a_version_that_is_not_knowable_stays_null(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    flywheel.set_root_cause(oid, vendor_package="requests", symbol_path="requests.get")
    row = flywheel.get(oid)
    assert row["vendor_version_from"] is None and row["vendor_version_to"] is None
    assert row["break_kind"] == "unknown"        # not forced into a guess


def test_an_unknown_break_kind_is_rejected(tenants):
    a, _ = tenants
    with pytest.raises(ValueError, match="break_kind must be"):
        flywheel.set_root_cause(flywheel.observe(a), break_kind="probably_bad")


# ---------- lookup: the flywheel measurement ----------

def test_lookup_on_an_empty_library_is_a_miss(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    result, tid = flywheel.lookup(oid, vendor_package="openai",
                                  symbol_path="openai.ChatCompletion.create",
                                  break_kind="symbol_moved", fingerprint="abc123")
    assert (result, tid) == ("miss", None)
    row = flywheel.get(oid)
    assert row["library_lookup_result"] == "miss"
    assert row["call_site_fingerprint"] == "abc123"


def test_lookup_finds_a_promoted_transform(tenants):
    a, b = tenants
    tid = flywheel.insert_transform(
        vendor_package="openai", symbol_path="openai.ChatCompletion.create",
        break_kind="symbol_moved", match_pattern={"node": "Call", "func": "Attribute"},
        rewrite_ref="transforms.openai_v1.move_chat_completion",
        supporting_observations=flywheel.supporting_from_ids(
            [_observation(a), _observation(b)]))
    oid = flywheel.observe(a)
    result, found = flywheel.lookup(oid, vendor_package="openai",
                                    symbol_path="openai.ChatCompletion.create",
                                    break_kind="symbol_moved")
    assert (result, found) == ("hit", tid)


def test_hit_rate_is_measurable(tenants):
    a, b = tenants
    flywheel.insert_transform(
        vendor_package="openai", symbol_path="openai.ChatCompletion.create",
        break_kind="symbol_moved", match_pattern={"node": "Call"},
        rewrite_ref="transforms.openai_v1.move_chat_completion",
        supporting_observations=flywheel.supporting_from_ids(
            [_observation(a), _observation(b)]))
    flywheel.lookup(flywheel.observe(a), vendor_package="openai",
                    symbol_path="openai.ChatCompletion.create", break_kind="symbol_moved")
    flywheel.lookup(flywheel.observe(a), vendor_package="requests",
                    symbol_path="requests.get", break_kind="unknown")
    stats = flywheel.library_hit_rate(since=0)
    assert stats["lookups"] == 2 and stats["hits"] == 1 and stats["hit_rate"] == 0.5


# ---------- verification is computed, never asserted ----------

def test_verification_level_is_derived_from_evidence(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    e = verify.Evidence(suite_ran=True, suite_passed=True,
                        changed_lines={"pay.py": {7}}, covered_lines={"pay.py": {7}})
    assert flywheel.set_verification(oid, e) == "verified_existing_tests"
    row = flywheel.get(oid)
    assert row["verification_level"] == "verified_existing_tests"
    assert json.loads(row["verification_evidence"])["covered_changed_lines"] == {"pay.py": [7]}


def test_a_green_suite_that_misses_the_change_is_recorded_unverified(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    e = verify.Evidence(suite_ran=True, suite_passed=True,
                        changed_lines={"pay.py": {7}}, covered_lines={"pay.py": {1}})
    assert flywheel.set_verification(oid, e) == "unverified_no_coverage"


# ---------- PR lifecycle and the kill metric ----------

def test_pr_lifecycle_and_unattended_merge_rate(tenants):
    a, _ = tenants
    clean, touched, still_open = (flywheel.observe(a) for _ in range(3))
    for oid, n in ((clean, 1), (touched, 2), (still_open, 3)):
        flywheel.set_pr_opened(oid, repo_full="acme/api", number=n,
                               url=f"https://github.com/acme/api/pull/{n}",
                               branch=f"cicatrixa/fix-{n}")
    flywheel.set_pr_closed(clean, state="merged", merged_at=db.now())
    flywheel.add_human_commits(touched, 2)
    flywheel.set_pr_closed(touched, state="merged", merged_at=db.now())

    stats = flywheel.unattended_merge_rate(since=0)
    assert stats["prs_opened"] == 3
    assert stats["prs_merged"] == 2
    assert stats["merged_unattended"] == 1        # only the untouched one counts
    assert stats["unattended_merge_rate"] == pytest.approx(1 / 3)
    assert stats["unattended_share_of_merged"] == pytest.approx(0.5)


def test_an_open_pr_counts_against_the_rate_rather_than_vanishing(tenants):
    a, _ = tenants
    flywheel.set_pr_opened(flywheel.observe(a), repo_full="acme/api", number=1,
                           url="u", branch="b")
    assert flywheel.unattended_merge_rate(since=0)["unattended_merge_rate"] == 0.0


def test_the_window_excludes_prs_opened_outside_it(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    flywheel.set_pr_opened(oid, repo_full="acme/api", number=1, url="u", branch="b")
    db.q("UPDATE break_observation SET pr_opened_at=? WHERE id=?", (1000.0, oid))
    assert flywheel.unattended_merge_rate(since=5000.0)["prs_opened"] == 0
    assert flywheel.unattended_merge_rate(since=0, until=2000.0)["prs_opened"] == 1


def test_a_pr_is_findable_by_number_and_by_branch(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    flywheel.set_pr_opened(oid, repo_full="acme/api", number=7, url="u",
                           branch="cicatrixa/fix-abc")
    assert flywheel.find_by_pr("acme/api", 7)["id"] == oid
    assert flywheel.find_by_pr_branch("acme/api", "cicatrixa/fix-abc")["id"] == oid


def test_human_commits_accumulate_across_pushes(tenants):
    a, _ = tenants
    oid = flywheel.observe(a)
    flywheel.add_human_commits(oid, 1)
    flywheel.add_human_commits(oid, 3)
    flywheel.add_human_commits(oid, 0)
    assert flywheel.get(oid)["human_commits_on_pr"] == 4


# ---------- no customer source may reach the transform table ----------

CUSTOMER_SOURCE = [
    "def charge(order):\n    return order.total * 1.2\n",
    "--- a/pay.py\n+++ b/pay.py\n@@ -1 +1 @@\n-x\n+y\n",
    "import openai; openai.api_key = SECRET",
    "return sum(i.price for i in items)",
    "class Billing: pass",
    "# internal: acme corp proprietary",
]


@pytest.mark.parametrize("blob", CUSTOMER_SOURCE)
def test_source_cannot_enter_through_rewrite_ref(tenants, blob):
    a, b = tenants
    with pytest.raises(ValueError):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path="openai.ChatCompletion.create",
            break_kind="symbol_moved", match_pattern={"node": "Call"},
            rewrite_ref=blob,
            supporting_observations=flywheel.supporting_from_ids(
                [_observation(a), _observation(b)]))


@pytest.mark.parametrize("blob", CUSTOMER_SOURCE)
def test_source_cannot_enter_through_the_match_pattern(tenants, blob):
    a, b = tenants
    for pattern in ({"node": blob}, {"args": [{"value": blob}]}, {blob: "Call"}):
        with pytest.raises(ValueError):
            flywheel.insert_transform(
                vendor_package="openai", symbol_path="openai.ChatCompletion.create",
                break_kind="symbol_moved", match_pattern=pattern,
                rewrite_ref="transforms.openai_v1.move_it",
                supporting_observations=flywheel.supporting_from_ids(
                    [_observation(a), _observation(b)]))


@pytest.mark.parametrize("blob", CUSTOMER_SOURCE)
def test_source_cannot_enter_through_symbol_path_or_package(tenants, blob):
    a, b = tenants
    supporting = flywheel.supporting_from_ids([_observation(a), _observation(b)])
    with pytest.raises(ValueError):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path=blob, break_kind="symbol_moved",
            match_pattern={"node": "Call"}, rewrite_ref="transforms.x.y",
            supporting_observations=supporting)
    with pytest.raises(ValueError):
        flywheel.insert_transform(
            vendor_package=blob, symbol_path="openai.X.create", break_kind="symbol_moved",
            match_pattern={"node": "Call"}, rewrite_ref="transforms.x.y",
            supporting_observations=supporting)


def test_a_rewrite_ref_must_be_a_callable_name_not_a_diff(tenants):
    """Storing a textual diff is the dead end: it is worthless against code it
    has never seen."""
    a, b = tenants
    supporting = flywheel.supporting_from_ids([_observation(a), _observation(b)])
    for bad in ("just_a_name", "-old_line\n+new_line", "a/b.py"):
        with pytest.raises(ValueError, match="rewrite_ref"):
            flywheel.insert_transform(
                vendor_package="openai", symbol_path="openai.X.create",
                break_kind="symbol_moved", match_pattern={"node": "Call"},
                rewrite_ref=bad, supporting_observations=supporting)


def test_nothing_in_a_stored_transform_resembles_source(tenants):
    a, b = tenants
    flywheel.insert_transform(
        vendor_package="openai", applies_to_version_range=">=1.0,<2.0",
        symbol_path="openai.ChatCompletion.create", break_kind="symbol_moved",
        match_pattern={"node": "Call", "func": {"node": "Attribute", "attr": "create"}},
        rewrite_ref="transforms.openai_v1.move_chat_completion",
        supporting_observations=flywheel.supporting_from_ids(
            [_observation(a), _observation(b)]))
    row = db.one("SELECT * FROM transform LIMIT 1")
    for key in row.keys():
        value = row[key]
        if isinstance(value, str):
            assert "\n" not in value, f"{key} contains a newline"
            assert "def " not in value and "import " not in value, f"{key} smells like code"


# ---------- the promotion bar ----------

def test_a_single_observation_can_never_create_a_transform(tenants):
    a, _ = tenants
    with pytest.raises(ValueError, match="needs 2 observations"):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path="openai.X.create",
            break_kind="symbol_moved", match_pattern={"node": "Call"},
            rewrite_ref="transforms.x.y",
            supporting_observations=flywheel.supporting_from_ids([_observation(a)]))


def test_two_observations_from_one_tenant_are_not_enough(tenants):
    a, _ = tenants
    with pytest.raises(ValueError, match="distinct tenants"):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path="openai.X.create",
            break_kind="symbol_moved", match_pattern={"node": "Call"},
            rewrite_ref="transforms.x.y",
            supporting_observations=flywheel.supporting_from_ids(
                [_observation(a), _observation(a)]))


def test_unverified_observations_never_promote_at_any_count(tenants):
    a, b = tenants
    many = [_observation(a, level="unverified_no_coverage") for _ in range(5)]
    many += [_observation(b, level="unverified_no_coverage") for _ in range(5)]
    with pytest.raises(ValueError, match="needs 2 observations"):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path="openai.X.create",
            break_kind="symbol_moved", match_pattern={"node": "Call"},
            rewrite_ref="transforms.x.y",
            supporting_observations=flywheel.supporting_from_ids(many))


def test_verified_existing_tests_is_below_the_promotion_bar(tenants):
    a, b = tenants
    with pytest.raises(ValueError, match="needs 2 observations"):
        flywheel.insert_transform(
            vendor_package="openai", symbol_path="openai.X.create",
            break_kind="symbol_moved", match_pattern={"node": "Call"},
            rewrite_ref="transforms.x.y",
            supporting_observations=flywheel.supporting_from_ids(
                [_observation(a, level="verified_existing_tests"),
                 _observation(b, level="verified_existing_tests")]))


def test_merged_stable_observations_clear_the_bar(tenants):
    a, b = tenants
    assert flywheel.insert_transform(
        vendor_package="openai", symbol_path="openai.X.create",
        break_kind="symbol_moved", match_pattern={"node": "Call"},
        rewrite_ref="transforms.x.y",
        supporting_observations=flywheel.supporting_from_ids(
            [_observation(a, level="verified_merged_stable"),
             _observation(b, level="verified_reproduction")])) > 0


# ---------- PR mode ----------

def test_pr_mode_defaults_on_but_a_service_can_opt_out(tenants):
    a, _ = tenants
    pid = db.q("INSERT INTO projects (user_id, name, slug, created_at) VALUES (?,?,?,?)",
               (a, "p", "p", db.now())).lastrowid
    sid = db.q("INSERT INTO services (project_id, name, slug, repo_full, created_at) "
               "VALUES (?,?,?,?,?)", (pid, "api", "api", "acme/api", db.now())).lastrowid
    service = db.one("SELECT * FROM services WHERE id=?", (sid,))
    assert flywheel.pr_mode_enabled(service) is True        # NULL inherits the default

    db.set_setting("pr_mode_default", "0")
    assert flywheel.pr_mode_enabled(db.one("SELECT * FROM services WHERE id=?", (sid,))) is False

    db.q("UPDATE services SET pr_mode=1 WHERE id=?", (sid,))
    assert flywheel.pr_mode_enabled(db.one("SELECT * FROM services WHERE id=?", (sid,))) is True
    # an explicit choice survives the default moving back
    db.set_setting("pr_mode_default", "1")
    db.q("UPDATE services SET pr_mode=0 WHERE id=?", (sid,))
    assert flywheel.pr_mode_enabled(db.one("SELECT * FROM services WHERE id=?", (sid,))) is False
