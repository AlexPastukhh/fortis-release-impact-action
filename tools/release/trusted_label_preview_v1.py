#!/usr/bin/env python3
"""No-write trusted Release Labels publisher preview (v1).

Caller supplies independently authenticated GitHub PR/run/jobs/label snapshots,
the latest run attempt, and a full-history checkout at the exact PR HEAD.
The validator is imported from this reviewed action, NEVER from PR source.
No GitHub client, credentials, webhook handler, workflow or write capability.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Load reviewed sibling modules before any PR checkout can shadow them on
# sys.path. An eventual trusted caller must also use an isolated interpreter.
_TRUSTED_CODE = str(Path(__file__).resolve().parent)
if sys.path[0] != _TRUSTED_CODE:
    sys.path.insert(0, _TRUSTED_CODE)

import json
import tempfile

import ci_entry
from label_admission_v1 import _int, admit_successful_workflow_run, plan_labels
from validate import _check


def _synthetic_pr_event(admission: dict) -> dict:
    """Minimal trusted API metadata for the existing exact-HEAD validator."""
    repo = admission["repository"]
    return {
        "action": "synchronize",
        "repository": {"full_name": repo},
        "pull_request": {
            "number": admission["pullRequest"],
            "state": "open", "draft": False,
            "head": {"sha": admission["headSha"], "repo": {"full_name": repo}},
            "base": {"sha": admission["baseSha"], "ref": "main",
                     "repo": {"full_name": repo}},
            "body": admission["body"],
        },
    }


def preview_trusted_labels(*, event: dict, trusted_run: dict,
                           current_pr: dict, job_inventory: dict,
                           latest_run_attempt: int,
                           repository: str, workflow_id: int,
                           required_job_names: list[str], repo_root: Path,
                           existing_labels: list[str],
                           available_labels: list[str]) -> dict:
    """Fail closed; recompute from source without any external mutation.

    latest_run_attempt MUST be read independently from the live GitHub run;
    a prior successful attempt must not win over a later failed rerun.
    This function does not authenticate its inputs; the trusted caller MUST.
    """
    attempt = _int(latest_run_attempt, "latest GitHub run attempt")
    _check(_int(trusted_run.get("run_attempt"), "trusted run attempt") == attempt,
           "stale workflow attempt; newer rerun must be evaluated")
    admission = admit_successful_workflow_run(
        event, trusted_run, current_pr, repository=repository,
        workflow_id=workflow_id, job_inventory=job_inventory,
        required_job_names=required_job_names)
    # Only a constructed subset of independently read PR metadata reaches
    # the trusted validator. No artifacts, PR scripts or workflow files run.
    payload = _synthetic_pr_event(admission)
    with tempfile.TemporaryDirectory(prefix="fortis-label-preview-") as temp:
        event_file = Path(temp) / "trusted-pr.json"
        event_file.write_text(json.dumps(payload, ensure_ascii=False),
                              encoding="utf-8")
        checked = ci_entry.run(event_file, repository=repository,
                               event_name="pull_request", repo_root=repo_root)
    plan = plan_labels(admission, checked, current_pr,
                       existing_labels, available_labels)
    # No mutation or permission to publish: a future caller must use a new
    # GitHub/Gateway snapshot, strict intent UUID and post-effect readback.
    return {
        **plan,
        "workflowRun": admission["workflowRun"],
        "workflowRunAttempt": admission["workflowRunAttempt"],
        "validation": {
            "status": checked["status"],
            "classification": checked["classification"],
            "affectedTargets": checked["affectedTargets"],
            "changedFiles": checked["changedFiles"],
        },
        "writeEnabled": False,
    }
