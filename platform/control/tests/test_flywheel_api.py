"""The read side. The rule that matters: a break_observation is tenant-scoped
and may carry customer specifics, so one account must never see another's."""
import pytest

from app import db, flywheel, verify


@pytest.fixture
def two_tenants(fresh_db):
    acme = db.q("INSERT INTO users (email,pw_hash,is_admin,created_at) VALUES (?,?,?,?)",
                ("acme@x.com", "x", 0, db.now())).lastrowid
    beta = db.q("INSERT INTO users (email,pw_hash,is_admin,created_at) VALUES (?,?,?,?)",
                ("beta@x.com", "x", 0, db.now())).lastrowid
    for user, pkg in ((acme, "openai"), (beta, "requests")):
        oid = flywheel.observe(user)
        flywheel.set_root_cause(oid, vendor_package=pkg, symbol_path=f"{pkg}.thing")
    return acme, beta


def test_an_account_sees_only_its_own_observations(two_tenants):
    acme, beta = two_tenants
    mine = flywheel.observations(user_id=acme)
    assert len(mine) == 1
    assert mine[0]["vendor_package"] == "openai"
    assert all(o["vendor_package"] != "requests" for o in mine)


def test_the_unscoped_read_returns_every_tenant(two_tenants):
    assert len(flywheel.observations()) == 2      # admin-only at the route layer


def test_the_observation_payload_is_an_explicit_allow_list(two_tenants):
    """A column added later must be opted in, not leak by default."""
    acme, _ = two_tenants
    payload = flywheel.observations(user_id=acme)[0]
    assert "verification_evidence" not in payload   # can be large and repo-specific
    assert "pr_branch" not in payload
    assert set(payload) == {
        "id", "trigger", "vendor_package", "vendor_version_from", "vendor_version_to",
        "symbol_path", "break_kind", "call_site_fingerprint", "library_lookup_result",
        "matched_transform_id", "verification_level", "pr_number", "pr_url",
        "pr_state", "human_commits_on_pr", "merged_at", "created_at"}


def test_the_limit_is_clamped(two_tenants):
    assert len(flywheel.observations(limit=0)) == 1        # floors at 1
    assert len(flywheel.observations(limit=100000)) == 2   # ceiling does not error


def test_summary_scopes_to_one_account(two_tenants):
    acme, _ = two_tenants
    assert flywheel.summary(30, user_id=acme)["observations"] == 1
    assert flywheel.summary(30)["observations"] == 2
    assert flywheel.summary(30)["tenants"] == 2


def test_summary_counts_verified_observations(two_tenants):
    acme, _ = two_tenants
    oid = flywheel.observe(acme)
    flywheel.set_verification(oid, verify.Evidence(
        suite_ran=True, suite_passed=True,
        changed_lines={"a.py": {1}}, covered_lines={"a.py": {1}}))
    s = flywheel.summary(30, user_id=acme)
    assert s["observations"] == 2 and s["verified_observations"] == 1


def test_summary_carries_both_rates(two_tenants):
    s = flywheel.summary(30)
    assert "hit_rate" in s["library"]
    assert "unattended_merge_rate" in s["pull_requests"]
    assert s["transforms_in_library"] == 0


def test_the_transform_listing_exposes_no_customer_code(two_tenants):
    acme, beta = two_tenants
    for user in (acme, beta):
        oid = flywheel.observe(user)
        db.q("UPDATE break_observation SET verification_level='verified_reproduction' "
             "WHERE id=?", (oid,))
    flywheel.insert_transform(
        vendor_package="openai", symbol_path="openai.ChatCompletion.create",
        break_kind="symbol_moved", match_pattern={"node": "Call"},
        rewrite_ref="transforms.openai_v1.move_it",
        supporting_observations=flywheel.supporting_from_ids(
            [r["id"] for r in db.all_(
                "SELECT id FROM break_observation "
                "WHERE verification_level='verified_reproduction'")]))
    listed = flywheel.transforms()
    assert len(listed) == 1
    assert "match_pattern" not in listed[0]      # structural, but not needed by readers
    for value in listed[0].values():
        if isinstance(value, str):
            assert "\n" not in value
