#!/usr/bin/env python3
"""Pure, fail-closed V1 admission and label reconciliation PREVIEW.

Inputs MUST originate from independently authenticated GitHub API reads in a
trusted default-branch publisher; caller must independently re-validate the
exact checked-out PR HEAD, catalog, body and full git diff with ci_entry.run.
No API client, network access, workflow automation, credentials or writes here.
"""
from __future__ import annotations

import re

from validate import SHA, ValidationError, _check, _dict, _list

REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MANAGED_RELEASE = frozenset({
    "release:no-deployment", "release:image-rebuild",
    "release:config-deploy", "release:windows-update",
    "release:package-build", "release:migration-required",
    "release:review-required",
})
STICKY_REVIEW = "release:review-required"
MAX_LABELS = 250
MAX_NAME = 50


def _repository(value: str, where: str) -> str:
    _check(type(value) is str and len(value) <= 202 and
           REPOSITORY.fullmatch(value) is not None, where + ": invalid repository")
    return value


def _sha(value: str, where: str) -> str:
    _check(type(value) is str and SHA.fullmatch(value) is not None,
           where + ": invalid SHA")
    return value


def _int(value, where: str):
    _check(type(value) is int and value > 0, where + ": positive integer required")
    return value


def _full_name(record, where: str):
    return _repository(_dict(record, where).get("full_name"), where)


def _pr_info(pr, repository: str, expected_number: int):
    pr = _dict(pr, "GitHub pull request")
    _check(_int(pr.get("number"), "PR number") == expected_number, "PR number drift")
    _check(pr.get("state") == "open" and pr.get("draft") is False and
           pr.get("merged") is not True and pr.get("merged_at") is None,
           "PR must be open, unmerged and non-draft")
    head = _dict(pr.get("head"), "PR head")
    base = _dict(pr.get("base"), "PR base")
    _check(_full_name(head.get("repo"), "PR head repository") == repository and
           _full_name(base.get("repo"), "PR base repository") == repository,
           "cross-repository PR is not eligible")
    _check(base.get("ref") == "main", "PR target is not main")
    head_sha = _sha(head.get("sha"), "PR head SHA")
    base_sha = _sha(base.get("sha"), "PR base SHA")
    body = pr.get("body")
    _check(type(body) is str and len(body) <= 20000,
           "PR body missing or oversized")
    return {"number": expected_number, "headSha": head_sha,
            "baseSha": base_sha, "body": body}


def admit_successful_workflow_run(event: dict, trusted_run: dict,
                                  current_pr: dict, *,
                                  repository: str, workflow_id: int) -> dict:
    """Verify GitHub webhook AND separately fetched trusted run/PR snapshots.

    Workflow IDs MUST be resolved from the trusted default-branch workflow
    configuration, not copied from event data or an untrusted PR artifact.
    No ambiguities are resolved by guesswork, including missing PR linkage.
    """
    repository = _repository(repository, "expected repository")
    workflow_id = _int(workflow_id, "expected workflow ID")
    event = _dict(event, "workflow_run event")
    _check(event.get("action") == "completed", "workflow run is not completed")
    trigger = _dict(event.get("workflow_run"), "event.workflow_run")
    run = _dict(trusted_run, "trusted GitHub workflow run")
    run_id = _int(trigger.get("id"), "event run ID")
    _check(_int(run.get("id"), "trusted run ID") == run_id,
           "event run ID mismatch")
    for where, entry in (("event run", trigger), ("trusted run", run)):
        _check(entry.get("status") == "completed" and
               entry.get("conclusion") == "success" and
               entry.get("event") == "pull_request",
               where + ": only successful pull_request run is eligible")
        _check(_int(entry.get("workflow_id"), where + " workflow ID") == workflow_id,
               where + ": unexpected workflow ID")
        _check(_full_name(entry.get("repository"), where + " repository") == repository,
               where + ": wrong repository")
        _check(_full_name(entry.get("head_repository"),
                          where + " head repository") == repository,
               where + ": fork/cross-repo event")
    head_sha = _sha(run.get("head_sha"), "trusted workflow run HEAD")
    _check(_sha(trigger.get("head_sha"), "event workflow HEAD") == head_sha,
           "workflow run HEAD differs between event and API")
    links = _list(run.get("pull_requests"), "trusted run PR links")
    event_links = _list(trigger.get("pull_requests"), "event PR links")
    _check(len(links) == len(event_links) == 1,
           "exactly one supported PR link required")
    number = _int(_dict(links[0], "trusted run PR link").get("number"),
                  "trusted run PR number")
    _check(_int(_dict(event_links[0], "event run PR link").get("number"),
                "event run PR number") == number, "PR linkage differs")
    pr = _pr_info(current_pr, repository, number)
    _check(pr["headSha"] == head_sha, "stale PR HEAD; no publication allowed")
    return {
        "repository": repository,
        "pullRequest": number,
        "headSha": head_sha,
        "baseSha": pr["baseSha"],
        "body": pr["body"],
        "workflowRun": run_id,
    }


def check_fresh_pr(admission: dict, fresh_pr: dict):
    """Verify HEAD, base and PR body again immediately before *future* writes.

    A second read reduces stale-run races but GitHub label APIs have no PR
    HEAD compare-and-swap. A future writer needs post-write readback and must
    STOP on races or ambiguous mutations rather than blindly retrying.
    """
    a = _dict(admission, "admission")
    repo = _repository(a.get("repository"), "admitted repository")
    number = _int(a.get("pullRequest"), "admitted PR")
    new = _pr_info(fresh_pr, repo, number)
    _check(new["headSha"] == a.get("headSha"), "PR HEAD changed")
    _check(new["baseSha"] == a.get("baseSha"), "PR base changed")
    _check(new["body"] == a.get("body"), "PR metadata changed")
    return True


def _labels(values, where: str):
    values = _list(values, where)
    _check(len(values) <= MAX_LABELS, where + ": too many labels")
    names = set()
    for name in values:
        _check(type(name) is str and 0 < len(name) <= MAX_NAME,
               where + ": invalid label name")
        _check(name not in names, where + ": duplicate label")
        names.add(name)
    return names


def _managed(name: str) -> bool:
    # Preserve unrelated human labels and unrecognized release-prefixed labels.
    return name.startswith("build:") or name in MANAGED_RELEASE


def plan_labels(admission: dict, checked_result: dict, current_pr: dict,
                existing_labels: list[str], available_labels: list[str]) -> dict:
    """Calculate additions/removals from independently revalidated impact.

    No writes here. Managed namespaces are reserved for the automation; the
    safety-review flag is sticky and must only be cleared by an owner.
    Missing GitHub label definitions BLOCK publication; never invent labels.
    """
    check_fresh_pr(admission, current_pr)
    result = _dict(checked_result, "trusted source validator result")
    _check(result.get("status") == "PASS" and
           result.get("repository") == admission["repository"] and
           result.get("headSha") == admission["headSha"] and
           result.get("pullRequest") == admission["pullRequest"],
           "validated evidence does not match admitted exact PR HEAD")
    classification = result.get("classification")
    _check(classification in ("deployment", "no-deployment"),
           "unresolved impact cannot be labelled")
    suggested = _labels(result.get("suggestedLabels"), "suggested labels")
    _check(bool(suggested) and all(_managed(s) for s in suggested),
           "unapproved or missing suggested managed label")
    if classification == "no-deployment":
        _check(suggested == {"release:no-deployment"} and
               result.get("affectedTargets") == [],
               "invalid no-deployment label evidence")
    else:
        _check("release:no-deployment" not in suggested,
               "deployment cannot carry no-deployment label")
        _check(bool(suggested & set(MANAGED_RELEASE - {"release:no-deployment"})),
               "deployment has no release action evidence")
        _check(any(s.startswith("build:") for s in suggested),
               "deployment without build target labels")
        affected = _list(result.get("affectedTargets"), "validated affected targets")
        _check(all(type(t) is str and "build:" + t in suggested for t in affected),
               "validated affected target omitted from labels")
    existing = _labels(existing_labels, "existing labels")
    available = _labels(available_labels, "available repository labels")
    missing = sorted(suggested - available)
    stale = sorted(n for n in existing if _managed(n) and
                   n not in suggested and n != STICKY_REVIEW)
    additions = sorted(suggested - existing)
    # Review-required may not be silently removed by automation, even when
    # later PR metadata becomes no-deployment.
    return {
        "status": "BLOCKED_MISSING_LABELS" if missing else "PREVIEW_ONLY",
        "repository": admission["repository"],
        "pullRequest": admission["pullRequest"],
        "headSha": admission["headSha"],
        "suggestedLabels": sorted(suggested),
        "add": additions,
        "remove": stale,
        "missingDefinitions": missing,
        "preservedHumanLabels": sorted(n for n in existing if not _managed(n)),
        "preservedReviewFlag": STICKY_REVIEW in existing and STICKY_REVIEW not in suggested,
    }
