#!/usr/bin/env python3
"""Offline read-only ALL-required-CI gate for Fortis Release Labels.

The trusted caller supplies a reviewed two-workflow config and injects an
authenticated typed Gateway client. Nothing in an untrusted PR, a CI artifact,
or a workflow result may remove either required gate. No writes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from gateway_snapshot_v1 import (
    RUN_PAGE_SIZE, MAX_RUN_PAGES, _complete_jobs, preview_gateway_snapshot,
)
from label_admission_v1 import (
    _check, _check_completed_jobs, _dict, _int, _list, _repository, _sha,
)

REQUIRED_WORKFLOW_NAMES = frozenset(("Source CI", "Fortis Release Impact"))


def _reviewed_workflows(required_workflows: list[dict]) -> dict[str, dict]:
    specs = _list(required_workflows, "reviewed required workflows")
    _check(len(specs) == len(REQUIRED_WORKFLOW_NAMES),
           "both Source CI and Fortis Release Impact are mandatory")
    by_name = {}
    ids = set()
    paths = set()
    for raw in specs:
        spec = _dict(raw, "reviewed CI workflow specification")
        _check(set(spec) == {"name", "workflowId", "workflowPath", "requiredJobs"},
               "unexpected trusted CI workflow configuration")
        name = spec.get("name")
        _check(type(name) is str and name in REQUIRED_WORKFLOW_NAMES and
               name not in by_name, "missing or duplicate mandatory CI workflow")
        workflow_id = _int(spec.get("workflowId"), "trusted CI workflow ID")
        path = spec.get("workflowPath")
        _check(type(path) is str and path.startswith(".github/workflows/") and
               path.endswith((".yml", ".yaml")) and
               ".." not in path and len(path) <= 180,
               "invalid reviewed CI workflow path")
        _check(workflow_id not in ids and path not in paths,
               "duplicate trusted workflow identity")
        jobs = _list(spec.get("requiredJobs"), "trusted CI jobs")
        _check(0 < len(jobs) <= 50 and
               all(type(job) is str and 0 < len(job) <= 128 for job in jobs) and
               len(set(jobs)) == len(jobs),
               "invalid required CI jobs")
        by_name[name] = spec
        ids.add(workflow_id)
        paths.add(path)
    _check(set(by_name) == REQUIRED_WORKFLOW_NAMES,
           "required Source CI or Release Impact workflow missing")
    return by_name


def _all_workflow_runs(github, repo_name: str) -> list[dict]:
    """Require the complete bounded run inventory, not a partial search."""
    rows = []
    seen_ids = set()
    total = None
    for page in range(1, MAX_RUN_PAGES + 1):
        raw = _dict(github.list_workflow_runs(
            repo_name, page=page, per_page=RUN_PAGE_SIZE),
            "typed GitHub workflow inventory")
        _check(raw.get("operation") == "list_workflow_runs",
               "unexpected workflow inventory response")
        count = raw.get("totalCount")
        _check(type(count) is int and 0 <= count <= MAX_RUN_PAGES * RUN_PAGE_SIZE,
               "workflow inventory exceeds bounded safe limit")
        if total is None:
            total = count
        _check(count == total, "workflow inventory changed during pagination")
        batch = _list(raw.get("items"), "workflow inventory page")
        offset = (page - 1) * RUN_PAGE_SIZE
        _check(len(batch) == min(RUN_PAGE_SIZE, max(0, total - offset)),
               "incomplete workflow inventory page")
        for entry in batch:
            entry = _dict(entry, "typed GitHub workflow listing entry")
            ident = _int(entry.get("id"), "workflow run ID")
            _check(ident not in seen_ids, "workflow inventory duplicate run ID")
            seen_ids.add(ident)
            rows.append(entry)
        if len(rows) == total:
            return rows
    raise ValueError("incomplete bounded workflow inventory")


def _latest_named_runs(rows: list[dict], head_sha: str,
                       by_name: dict[str, dict]) -> dict[str, dict]:
    selected = {}
    for name in REQUIRED_WORKFLOW_NAMES:
        matches = []
        for row in rows:
            if row.get("headSha") == head_sha and row.get("name") == name:
                _check(row.get("event") == "pull_request",
                       "unexpected event for required CI workflow")
                matches.append(row)
        _check(bool(matches), "mandatory CI workflow run missing: " + name)
        numbered = {}
        for row in matches:
            number = _int(row.get("runNumber"), "workflow run number")
            _check(number not in numbered, "ambiguous duplicate workflow run number")
            numbered[number] = row
        selected[name] = numbered[max(numbered)]
    return selected

def collect_all_required_ci(github, *, snapshot: dict,
                            required_workflows: list[dict]) -> dict:
    """Verify each mandatory workflow and its latest successful CI jobs.

    The snapshot MUST be from authenticated collect_gateway_snapshot reads.
    IDs, paths and job names MUST be pinned in reviewed default-branch config.
    These checks do not authorize a future mutation.
    """
    snap = _dict(snapshot, "authenticated PR/Release Impact snapshot")
    _check(snap.get("snapshotOnly") is True,
           "Gateway evidence snapshot required")
    repo = _repository(snap.get("repository"), "trusted repository")
    repo_name = repo.split("/")[1]
    pr = _dict(snap.get("current_pr"), "current PR snapshot")
    number = _int(pr.get("number"), "PR number")
    head = _dict(pr.get("head"), "PR head")
    base = _dict(pr.get("base"), "PR base")
    head_sha = _sha(head.get("sha"), "PR HEAD SHA")
    base_sha = _sha(base.get("sha"), "PR base SHA")
    _check(pr.get("state") == "open" and pr.get("draft") is False and
           pr.get("merged") is False and base.get("ref") == "main" and
           _dict(head.get("repo"), "head repo").get("full_name") == repo and
           _dict(base.get("repo"), "base repo").get("full_name") == repo,
           "invalid current PR provenance")
    reviewed = _reviewed_workflows(required_workflows)
    run_rows = _all_workflow_runs(github, repo_name)
    latest = _latest_named_runs(run_rows, head_sha, reviewed)
    summaries = []
    for name in sorted(REQUIRED_WORKFLOW_NAMES):
        listing = latest[name]
        spec = reviewed[name]
        ident = _int(listing.get("id"), "latest CI run ID")
        attempt = _int(listing.get("runAttempt"), "latest CI attempt")
        _check(listing.get("status") == "completed" and
               listing.get("conclusion") == "success",
               "latest required CI workflow not successful: " + name)
        fetched = _dict(github.get_workflow_run(repo_name, ident, attempt),
                        "authenticated CI workflow")
        _check(fetched.get("operation") == "get_workflow_run",
               "unexpected exact-run operation")
        run = _dict(_dict(fetched.get("data"), "exact run payload").get("run"),
                    "exact CI workflow run")
        _check(run.get("id") == ident and run.get("attempt") == attempt and
               run.get("workflowId") == spec["workflowId"] and
               run.get("workflowPath") == spec["workflowPath"] and
               run.get("status") == listing["status"] == "completed" and
               run.get("conclusion") == listing["conclusion"] == "success" and
               run.get("event") == "pull_request" and
               run.get("headSha") == listing.get("headSha") == head_sha and
               run.get("headBranch") == listing.get("headBranch") ==
               head.get("ref") and
               run.get("repository") == run.get("headRepository") == repo,
               "required CI workflow identity/head/attempt mismatch: " + name)
        links = _list(run.get("pullRequests"), "required CI PR links")
        _check(len(links) == 1 and
               _dict(links[0], "required CI PR link").get("number") == number and
               links[0].get("headSha") == head_sha and
               links[0].get("baseSha") == base_sha,
               "required CI run linked to different PR/base: " + name)
        jobs = _complete_jobs(github, repo_name, run)
        _check_completed_jobs(jobs, ident, attempt, spec["requiredJobs"])
        if name == "Fortis Release Impact":
            admitted = _dict(snap.get("trusted_run"), "prior Release Impact")
            _check(admitted.get("id") == ident and
                   admitted.get("run_attempt") == attempt and
                   snap.get("workflow_id") == spec["workflowId"] and
                   snap.get("required_job_names") == spec["requiredJobs"],
                   "Release Impact run differs from authenticated snapshot")
        summaries.append({
            "name": name, "runId": ident, "attempt": attempt,
            "workflowId": spec["workflowId"],
            "workflowPath": spec["workflowPath"],
            "completedJobs": sorted(spec["requiredJobs"]),
        })
    # Recheck PR label/body fingerprint and latest attempts after all jobs.
    # GitHub lacks atomic compare-and-swap; a writer needs its own fresh reads.
    fresh = _dict(github.get_pr_release_label_context(repo_name, number),
                  "fresh PR label fingerprint")
    body = pr.get("body")
    _check(type(body) is str and 0 < len(body.encode("utf-8")) <= 20000,
           "invalid PR description in CI snapshot")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _check(fresh.get("operation") == "get_pr_release_label_context" and
           fresh.get("number") == number and
           fresh.get("branch") == head.get("ref") and
           fresh.get("headSha") == head_sha and
           fresh.get("baseSha") == base_sha and
           fresh.get("bodySha256") == digest and
           sorted(_list(fresh.get("labels"), "fresh labels")) ==
           snap.get("existing_labels"),
           "PR fingerprint changed during required CI admission")
    new_runs = _latest_named_runs(
        _all_workflow_runs(github, repo_name), head_sha, reviewed)
    _check(all(new_runs[name].get("id") == latest[name]["id"] and
               new_runs[name].get("runAttempt") == latest[name]["runAttempt"] and
               new_runs[name].get("status") == latest[name]["status"] and
               new_runs[name].get("conclusion") == latest[name]["conclusion"]
               for name in REQUIRED_WORKFLOW_NAMES),
           "new required CI run or rerun appeared during admission")
    return {
        "status": "ALL_REQUIRED_CI_ADMITTED_READ_ONLY",
        "repository": repo, "pullRequest": number, "headSha": head_sha,
        "requiredCI": sorted(summaries, key=lambda item: item["name"]),
        "writeEnabled": False,
    }


def preview_with_all_required_ci(github, *, snapshot: dict,
                                 required_workflows: list[dict],
                                 repo_root: Path) -> dict:
    """Source revalidation + every trusted CI gate; still only a preview."""
    gate = collect_all_required_ci(
        github, snapshot=snapshot, required_workflows=required_workflows)
    plan = preview_gateway_snapshot(snapshot, repo_root=repo_root)
    _check(plan.get("headSha") == gate["headSha"] and
           plan.get("writeEnabled") is False,
           "source preview and required CI admission disagree")
    return {**plan, "requiredCI": gate["requiredCI"],
            "allRequiredCI": gate["status"], "writeEnabled": False}
