#!/usr/bin/env python3
"""Read-only, fail-closed typed Fortis Gateway evidence collector.

An injected trusted GitHubClient must authenticate every READ via Fortis
Gateway. This module does NOT create a token, make raw HTTP calls, execute
PR code, mutate GitHub or grant publisher authority. The returned snapshot
feeds trusted_label_preview_v1 ONLY with a data-only exact-HEAD checkout.
"""
from __future__ import annotations

import hashlib

from label_admission_v1 import (_check_completed_jobs, _int, _repository,
                                 _sha, _dict, _list, _check)
from trusted_label_preview_v1 import preview_trusted_labels

RUN_PAGE_SIZE = 20
LABEL_PAGE_SIZE = 50
MAX_RUN_PAGES = 25
MAX_LABEL_PAGES = 6
MAX_JOBS = 100


def _latest_run(github, repo: str, run_id: int) -> dict:
    """Find the latest attempt in an independently read bounded run inventory.

    A run-attempt endpoint can return an old successful attempt even when a
    newer rerun failed. A missing/incomplete listing is never permission.
    """
    total = None
    for page in range(1, MAX_RUN_PAGES + 1):
        data = _dict(github.list_workflow_runs(repo, page=page,
                                               per_page=RUN_PAGE_SIZE),
                     "trusted run listing")
        _check(data.get("operation") == "list_workflow_runs",
               "unexpected run listing operation")
        count = data.get("totalCount")
        _check(type(count) is int and 0 <= count <= RUN_PAGE_SIZE * MAX_RUN_PAGES,
               "unbounded run listing")
        if total is None:
            total = count
        _check(total == count, "run inventory changed during pagination")
        rows = _list(data.get("items"), "run listing page")
        offset = (page - 1) * RUN_PAGE_SIZE
        _check(len(rows) == min(RUN_PAGE_SIZE, max(0, total - offset)),
               "incomplete run listing page")
        matches = [entry for entry in rows
                   if type(entry) is dict and entry.get("id") == run_id]
        _check(len(matches) <= 1, "duplicate workflow run")
        if matches:
            return matches[0]
        if offset + len(rows) >= total:
            break
    raise ValueError("target workflow run missing from bounded latest-attempt inventory")


def _complete_jobs(github, repo: str, run: dict) -> dict:
    records = []
    expected_total = None
    for page in range(1, 3):
        data = _dict(github.list_workflow_jobs(
            repo, run["id"], run["attempt"], page=page, per_page=MAX_JOBS),
            "trusted jobs listing")
        _check(data.get("operation") == "list_workflow_jobs",
               "unexpected jobs listing operation")
        batch = _dict(data.get("data"), "jobs listing data")
        _check(batch.get("run") == run, "jobs page run provenance drift")
        count = batch.get("total")
        _check(type(count) is int and 0 < count <= MAX_JOBS,
               "unbounded jobs inventory")
        if expected_total is None:
            expected_total = count
        _check(expected_total == count and batch.get("page") == page and
               batch.get("perPage") == MAX_JOBS, "jobs page inconsistency")
        rows = _list(batch.get("items"), "jobs inventory items")
        offset = (page - 1) * MAX_JOBS
        _check(len(rows) == min(MAX_JOBS, max(0, count - offset)),
               "incomplete jobs inventory page")
        records.extend({"id": job.get("id"), "name": job.get("name"),
                        "status": job.get("status"),
                        "conclusion": job.get("conclusion")}
                       for job in rows)
        _check(batch.get("complete") is (offset + len(rows) >= count),
               "jobs inventory completeness flag inconsistent")
        if batch["complete"]:
            return {"run_id": run["id"], "run_attempt": run["attempt"],
                    "complete": True, "jobs": records}
    raise ValueError("incomplete jobs pagination")


def _complete_label_names(github, repo: str, pr_number: int | None) -> list[str]:
    values: list[str] = []
    for page in range(1, MAX_LABEL_PAGES + 1):
        if pr_number is None:
            data = github.list_repository_labels(
                repo, page=page, per_page=LABEL_PAGE_SIZE)
            expected = "list_repository_labels"
        else:
            data = github.list_pr_labels(
                repo, pr_number, page=page, per_page=LABEL_PAGE_SIZE)
            expected = "list_pr_labels"
        data = _dict(data, "typed GitHub label page")
        _check(data.get("operation") == expected and
               data.get("page") == page and
               data.get("perPage") == LABEL_PAGE_SIZE and
               (pr_number is None or data.get("number") == pr_number),
               "unexpected label page provenance")
        rows = _list(data.get("items"), "typed label records")
        _check(len(rows) <= LABEL_PAGE_SIZE, "oversized label page")
        values.extend(_dict(item, "typed label").get("name") for item in rows)
        _check(len(values) <= 250 and
               all(type(name) is str and 0 < len(name) <= 50 for name in values) and
               len(set(values)) == len(values),
               "invalid or duplicate label names")
        has_next = data.get("hasNextPage")
        _check(type(has_next) is bool, "missing label pagination marker")
        if not has_next:
            return sorted(values)
        _check(len(rows) == LABEL_PAGE_SIZE,
               "label page claims continuation without full page")
    raise ValueError("label pagination exceeds strict limit")

def collect_gateway_snapshot(github, *, repository: str, run_id: int,
                             pull_request: int, workflow_id: int,
                             workflow_path: str,
                             required_job_names: list[str]) -> dict:
    """Collect a snapshot from authenticated typed Gateway READs only.

    Trusted caller pins workflow identity and required jobs from reviewed
    default-branch config. Snapshot success is NOT permission to write:
    any future mutation must independently re-read all preconditions.
    """
    repo = _repository(repository, "trusted repository")
    name = repo.split("/")[1]
    run_id = _int(run_id, "trusted run ID")
    number = _int(pull_request, "trusted PR number")
    workflow_id = _int(workflow_id, "trusted workflow ID")
    _check(type(workflow_path) is str and
           workflow_path.startswith(".github/workflows/") and
           workflow_path.endswith(".yml") and
           ".." not in workflow_path,
           "invalid trusted workflow path")
    _check(type(required_job_names) is list and
           0 < len(required_job_names) <= 50 and
           all(type(x) is str for x in required_job_names) and
           len(set(required_job_names)) == len(required_job_names),
           "invalid trusted required jobs")
    listed = _latest_run(github, name, run_id)
    attempt = _int(listed.get("runAttempt"), "latest workflow attempt")
    _check(listed.get("headSha") is not None, "missing workflow run SHA")
    endpoint = _dict(github.get_workflow_run(name, run_id, attempt),
                     "trusted exact-attempt workflow run")
    _check(endpoint.get("operation") == "get_workflow_run",
           "unexpected run read operation")
    run = _dict(_dict(endpoint.get("data"), "run data").get("run"),
                "typed GitHub run")
    _check(run.get("id") == run_id and run.get("attempt") == attempt and
           run.get("workflowId") == workflow_id and
           run.get("workflowPath") == workflow_path and
           run.get("headSha") == listed["headSha"] and
           run.get("event") == "pull_request" and
           run.get("status") == listed.get("status") == "completed" and
           run.get("conclusion") == listed.get("conclusion") == "success" and
           run.get("repository") == repo and run.get("headRepository") == repo,
           "run latest attempt/source/workflow mismatch")
    links = _list(run.get("pullRequests"), "typed run PR links")
    _check(len(links) == 1 and links[0].get("number") == number and
           links[0].get("headSha") == run.get("headSha"),
           "run PR linkage invalid")
    job_inventory = _complete_jobs(github, name, run)
    _check_completed_jobs(job_inventory, run_id, attempt, required_job_names)
    raw = _dict(github.get_pr_release_impact_evidence(
        name, number, run["headSha"]), "authenticated PR impact evidence")
    context = _dict(github.get_pr_release_label_context(
        name, number), "authenticated label context")
    short = _dict(github.get_pull_request(name, number),
                  "authenticated short PR")["pullRequest"]
    _check(raw.get("operation") == "get_pr_release_impact_evidence" and
           context.get("operation") == "get_pr_release_label_context" and
           raw.get("number") == context.get("number") == number,
           "PR evidence identity mismatch")
    _check(raw.get("headSha") == context.get("headSha") == run["headSha"] ==
           short["head"]["sha"] == links[0]["headSha"] and
           raw.get("baseSha") == context.get("baseSha") ==
           short["base"]["sha"] == links[0]["baseSha"],
           "PR head/base SHA drift")
    _check(raw.get("headRepository") == raw.get("baseRepository") == repo and
           raw.get("headBranch") == context.get("branch") ==
           short["head"]["ref"] == run.get("headBranch") and
           raw.get("baseBranch") == short["base"]["ref"] == "main" and
           short["state"] == raw.get("state") == "open" and
           short["draft"] is raw.get("draft") is False and
           short["merged"] is raw.get("merged") is False,
           "PR repository/branch/state drift")
    body = raw.get("body")
    _check(type(body) is str and 0 < len(body.encode("utf-8")) <= 20000 and
           hashlib.sha256(body.encode("utf-8")).hexdigest() ==
           raw.get("bodySha256") == context.get("bodySha256"),
           "PR description digest mismatch")
    existing = _complete_label_names(github, name, number)
    available = _complete_label_names(github, name, None)
    _check(existing == sorted(context.get("labels", [])),
           "PR label inventory drift")
    # Final compare narrows the window for a read-only preview. This is not
    # GitHub CAS, so the later writer must do fresh reads before each ASK.
    _check(github.get_pr_release_label_context(name, number) == context,
           "PR metadata/labels changed during reads")

    trusted_run = {
        "id": run_id, "run_attempt": attempt, "workflow_id": workflow_id,
        "status": run["status"], "conclusion": run["conclusion"],
        "event": run["event"], "head_sha": run["headSha"],
        "repository": {"full_name": repo},
        "head_repository": {"full_name": repo},
        "pull_requests": [{"number": number}],
    }
    current_pr = {
        "number": number, "state": short["state"], "draft": short["draft"],
        "merged": short["merged"], "merged_at": None,
        "head": {"sha": raw["headSha"], "ref": raw["headBranch"],
                 "repo": {"full_name": repo}},
        "base": {"sha": raw["baseSha"], "ref": "main",
                 "repo": {"full_name": repo}},
        "body": body,
    }
    # The event below is an authenticated *synthetic* trigger generated by
    # the reader, NOT a claim that GitHub sent an independent webhook.
    event = {"action": "completed", "workflow_run": dict(trusted_run)}
    return {
        "event": event, "trusted_run": trusted_run,
        "current_pr": current_pr, "job_inventory": job_inventory,
        "latest_run_attempt": attempt, "repository": repo,
        "workflow_id": workflow_id,
        "required_job_names": list(required_job_names),
        "existing_labels": existing, "available_labels": available,
        "snapshotOnly": True,
    }


def preview_gateway_snapshot(snapshot: dict, *, repo_root) -> dict:
    """Independent exact-head source recomputation; never performs writes."""
    snap = _dict(snapshot, "trusted Gateway snapshot")
    _check(snap.get("snapshotOnly") is True, "untrusted snapshot marker")
    return preview_trusted_labels(
        event=snap["event"], trusted_run=snap["trusted_run"],
        current_pr=snap["current_pr"], job_inventory=snap["job_inventory"],
        latest_run_attempt=snap["latest_run_attempt"],
        repository=snap["repository"], workflow_id=snap["workflow_id"],
        required_job_names=snap["required_job_names"],
        repo_root=repo_root, existing_labels=snap["existing_labels"],
        available_labels=snap["available_labels"])
