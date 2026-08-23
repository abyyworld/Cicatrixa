"""Hook 8 is the one that decides whether the kill metric is real. These test
the parsing and counting, not FastAPI's routing."""
import json

import pytest

from app import db, flywheel, gh

AGENT_EMAIL = "agent@cicatrixa.dev"


@pytest.fixture
def observation(fresh_db):
    uid = db.q("INSERT INTO users (email, pw_hash, created_at) VALUES (?,?,?)",
               ("a@example.com", "x", db.now())).lastrowid
    oid = flywheel.observe(uid)
    flywheel.set_pr_opened(oid, repo_full="acme/api", number=4,
                           url="https://github.com/acme/api/pull/4",
                           branch="cicatrixa/fix-abc1234567")
    return oid


def push_event(repo, branch, emails):
    return json.dumps({
        "ref": f"refs/heads/{branch}",
        "repository": {"full_name": repo},
        "after": "deadbeef",
        "commits": [{"author": {"email": e}} for e in emails],
    }).encode()


def pr_event(action, repo, number, branch, merged, merged_at=None):
    return json.dumps({
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {"number": number, "merged": merged,
                         "state": "closed" if merged else "open",
                         "head": {"ref": branch}, "merged_at": merged_at},
    }).encode()


# ---------- parsing ----------

def test_push_authors_are_extracted_and_lowercased():
    body = push_event("acme/api", "main", ["Dev@Example.COM", AGENT_EMAIL])
    assert gh.push_commit_authors(body) == ["dev@example.com", AGENT_EMAIL]


def test_push_authors_on_garbage_returns_empty():
    assert gh.push_commit_authors(b"not json") == []


def test_a_merged_pull_request_reads_as_merged():
    pr = gh.parse_pull_request(pr_event("closed", "acme/api", 4, "b", True,
                                        "2026-08-23T01:02:03Z"))
    assert pr["state"] == "merged" and pr["number"] == 4
    assert pr["repo_full"] == "acme/api"


def test_a_pull_request_closed_without_merging_is_not_merged():
    assert gh.parse_pull_request(
        pr_event("closed", "acme/api", 4, "b", False))["state"] == "open"


def test_malformed_pull_request_payload_is_ignored():
    assert gh.parse_pull_request(b'{"action":"closed"}') is None


# ---------- counting ----------

def _count(body, repo, branch):
    """Mirrors main._count_human_commits without importing FastAPI."""
    obs = flywheel.find_by_pr_branch(repo, branch)
    if not obs:
        return 0
    human = [e for e in gh.push_commit_authors(body) if e and e != AGENT_EMAIL]
    if human:
        flywheel.add_human_commits(obs["id"], len(human))
    return len(human)


def test_our_own_commits_do_not_count_as_human(observation):
    body = push_event("acme/api", "cicatrixa/fix-abc1234567", [AGENT_EMAIL, AGENT_EMAIL])
    assert _count(body, "acme/api", "cicatrixa/fix-abc1234567") == 0
    assert flywheel.get(observation)["human_commits_on_pr"] == 0


def test_a_human_push_to_our_pr_branch_is_counted(observation):
    body = push_event("acme/api", "cicatrixa/fix-abc1234567",
                      ["dev@example.com", AGENT_EMAIL, "other@example.com"])
    assert _count(body, "acme/api", "cicatrixa/fix-abc1234567") == 2
    assert flywheel.get(observation)["human_commits_on_pr"] == 2


def test_a_push_to_an_unrelated_branch_is_ignored(observation):
    body = push_event("acme/api", "main", ["dev@example.com"])
    assert _count(body, "acme/api", "main") == 0
    assert flywheel.get(observation)["human_commits_on_pr"] == 0


def test_a_push_to_the_same_branch_in_another_repo_is_ignored(observation):
    body = push_event("other/repo", "cicatrixa/fix-abc1234567", ["dev@example.com"])
    assert _count(body, "other/repo", "cicatrixa/fix-abc1234567") == 0


def test_a_touched_pr_is_excluded_from_the_unattended_rate(observation):
    """End to end: human pushes, PR merges, the metric must not credit it."""
    _count(push_event("acme/api", "cicatrixa/fix-abc1234567", ["dev@example.com"]),
           "acme/api", "cicatrixa/fix-abc1234567")
    flywheel.set_pr_closed(observation, state="merged", merged_at=db.now())
    stats = flywheel.unattended_merge_rate(since=0)
    assert stats["prs_merged"] == 1
    assert stats["merged_unattended"] == 0
    assert stats["unattended_merge_rate"] == 0.0
