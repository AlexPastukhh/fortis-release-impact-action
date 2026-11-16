#!/usr/bin/env python3
"""One bounded owner-ASK Release Labels intent, inert unless explicitly invoked.

Pure read-only planning rechecks exact-head GitHub evidence, BOTH required CI
workflows and data-only source catalog. An optional *explicit* one-shot helper
submits the plan through the existing typed Fortis Gateway ASK boundary.
No automatic trigger, owner approval, token, raw HTTP client, retries or writes
to GitHub from this module. Owner/WebAuthn permission stays in Gateway.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from gateway_snapshot_v1 import collect_gateway_snapshot
from label_admission_v1 import STICKY_REVIEW, MANAGED_RELEASE
from required_ci_v1 import collect_all_required_ci, preview_with_all_required_ci
from validate import ValidationError, _check, _dict, _list

BUILD = re.compile(r"build:[a-z][a-z0-9-]{0,43}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ASK_OPS = frozenset(("add_pr_release_labels", "remove_pr_release_label"))


def _valid_managed(name: str) -> bool:
    return (type(name) is str and name != STICKY_REVIEW and
            (name in MANAGED_RELEASE or BUILD.fullmatch(name) is not None))


def _selected_labels(mode: str, selected: list[str], plan: dict) -> tuple[str, dict]:
    _check(mode in ("add", "remove"), "explicit add/remove choice required")
    names = _list(selected, "owner-selected Release Labels")
    _check(1 <= len(names) <= (5 if mode == "add" else 1) and
           len(set(names)) == len(names) and
           all(_valid_managed(name) for name in names),
           "invalid owner-selected managed labels")
    key = "add" if mode == "add" else "remove"
    candidates = _list(plan.get(key), "independently computed label delta")
    _check(set(names) <= set(candidates),
           "selected labels not in independently recomputed plan")
    if mode == "add":
        return "add_pr_release_labels", {"labels": sorted(names)}
    return "remove_pr_release_label", {"name": names[0]}


def _fresh_context(github, repo: str, number: int, plan: dict,
                   snapshot: dict) -> dict:
    """Re-read *all* bounded label preconditions after exact-source validation.

    This cannot remove GitHub's TOCTOU window; Gateway performs additional
    pre-write checks and post-effect readback under a single ASK receipt.
    """
    context = _dict(github.get_pr_release_label_context(repo, number),
                    "fresh Gateway label context")
    pr = _dict(snapshot["current_pr"], "bound PR")
    body = pr.get("body")
    _check(type(body) is str and 0 < len(body.encode("utf-8")) <= 20000,
           "invalid bound PR body")
    expected_body_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _check(context.get("operation") == "get_pr_release_label_context" and
           context.get("number") == number and
           context.get("headSha") == plan["headSha"] ==
           _dict(pr.get("head"), "bound PR head").get("sha") and
           context.get("baseSha") ==
           _dict(pr.get("base"), "bound PR base").get("sha") and
           context.get("branch") ==
           _dict(pr.get("head"), "bound PR head").get("ref") and
           context.get("bodySha256") == expected_body_digest and
           sorted(context.get("labels", [])) == snapshot["existing_labels"],
           "PR SHA/body/labels changed after preview")
    for key in ("bodySha256", "labelsSha256"):
        _check(type(context.get(key)) is str and
               HEX64.fullmatch(context[key]) is not None,
               "invalid Gateway SHA-256 proof")
    return context


def prepare_owner_label_ask(github, *, repository: str, run_id: int,
                            pull_request: int, workflow_id: int,
                            workflow_path: str, required_job_names: list[str],
                            required_workflows: list[dict], repo_root: Path,
                            mode: str, selected_labels: list[str]) -> dict:
    """Use reviewed trusted code and data-only PR checkout to propose ONE ASK.

    All inputs must originate in trusted owner-reviewed configuration and an
    authenticated typed Gateway client, never a PR artifact or untrusted code.
    The caller's explicit label selection is limited to independently
    recomputed deltas. This function has only READ side effects.
    """
    snapshot = collect_gateway_snapshot(
        github, repository=repository, run_id=run_id,
        pull_request=pull_request, workflow_id=workflow_id,
        workflow_path=workflow_path, required_job_names=required_job_names)
    plan = preview_with_all_required_ci(
        github, snapshot=snapshot, required_workflows=required_workflows,
        repo_root=repo_root)
    _check(plan.get("status") == "PREVIEW_ONLY" and
           plan.get("allRequiredCI") == "ALL_REQUIRED_CI_ADMITTED_READ_ONLY" and
           plan.get("writeEnabled") is False and
           plan.get("missingDefinitions") == [],
           "missing definitions, unresolved source or incomplete CI: STOP")
    _check(plan.get("repository") == repository and
           plan.get("pullRequest") == pull_request,
           "trusted release plan changed PR identity")
    operation, delta = _selected_labels(mode, selected_labels, plan)
    # Re-run every trusted CI gate after exact-source recomputation; a later
    # failed rerun is never hidden by an old success.
    gate = collect_all_required_ci(
        github, snapshot=snapshot, required_workflows=required_workflows)
    _check(gate.get("status") == "ALL_REQUIRED_CI_ADMITTED_READ_ONLY" and
           gate.get("headSha") == plan["headSha"],
           "required CI changed after source recomputation")
    context = _fresh_context(
        github, repository.split("/")[1], pull_request, plan, snapshot)
    params = {
        "number": pull_request, "expectedHeadSha": context["headSha"],
        "expectedBaseSha": context["baseSha"],
        "expectedBodySha256": context["bodySha256"],
        "expectedLabelsSha256": context["labelsSha256"], **delta,
    }
    return {
        "status": "PREPARED_OWNER_ASK_ONLY",
        "repository": repository, "pullRequest": pull_request,
        "headSha": plan["headSha"], "operation": operation,
        "params": params, "requiredCI": gate["requiredCI"],
        "suggestedLabels": plan["suggestedLabels"],
        "ownerApprovalRequired": True, "gatewayPolicyRequired": "ASK",
        "autoSubmit": False, "writeEnabled": False,
    }


def _expect_ask_policy(github) -> None:
    """Read live Gateway capabilities before even creating an owner request."""
    gateway = getattr(github, "gateway", None)
    _check(gateway is not None and callable(getattr(gateway, "post", None)),
           "trusted Gateway capabilities reader required")
    cap = _dict(gateway.post("/v1/workspace/capabilities", {}),
                "authenticated Gateway capabilities")
    writes = _dict(cap.get("githubReleaseLabelWrites"),
                   "live release label policy")
    _check(writes.get("policy") == "ASK" and
           writes.get("unknownEffect") == "STOP" and
           writes.get("automaticPublisher") == "NOT_ACTIVE" and
           writes.get("scope") == "registered-open-same-repo-task-PR-only" and
           set(writes.get("commands", [])) == {
               "create_release_label", "add_pr_release_labels",
               "remove_pr_release_label"},
           "Gateway owner-ASK policy or scope not verified")



def _canonical_uuid(value) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _native_identity(reply: dict, original_id: str) -> dict:
    """Keep the existing coordinator's IDs, never manufacture or retry one."""
    message = 'Gateway native intent identity unknown: STOP, inspect original intent'
    _check(reply.get('intentId') == original_id, message)
    ids = reply.get('attemptIds')
    _check(type(ids) is list and len(ids) <= 1024 and
           all(_canonical_uuid(value) for value in ids) and
           len(set(ids)) == len(ids), message)
    active = reply.get('requestId')
    last = reply.get('lastAttemptId')
    if ids:
        _check(ids[0] == original_id and last == ids[-1] and active in ids, message)
    else:
        _check(active is None and last is None and reply.get('state') != 'PENDING', message)
    next_action = _dict(reply.get('nextAction'), message)
    _check(next_action.get('intentId') == original_id and
           type(next_action.get('kind')) is str and
           next_action.get('kind') in {'NONE', 'FIX_PRECONDITION', 'RETRY_INTENT',
               'WAIT_BACKOFF', 'RETRY_READ', 'WAIT_OWNER', 'WAIT_EXISTING_REQUEST'}, message)
    return dict(requestId=active, originalRequestId=original_id, intentId=original_id,
                attemptIds=list(ids), lastAttemptId=last,
                coordinatorNextAction={'kind': next_action['kind'], 'intentId': original_id})


def submit_one_owner_label_ask(github, *, request_id: str, reason: str,
                               operator_confirmed: bool = False,
                               **prepare_kwargs) -> dict:
    """One typed owner call; preserve the accepted native intent continuation.

    Record the supplied original UUID BEFORE invoking. The existing Repo
    coordinator owns any safely authorized attempts and its durable journal.
    Unknown outcomes require original intent/attempt readback, never blind retry.
    """
    _check(operator_confirmed is True,
           "explicit operator confirmation required before ASK submission")
    _check(_canonical_uuid(request_id),
           "stable canonical owner request UUID required")
    _check(type(reason) is str and 10 <= len(reason.strip()) <= 500,
           "auditable owner ASK reason required")
    _expect_ask_policy(github)
    prepared = prepare_owner_label_ask(github, **prepare_kwargs)
    repo = prepared["repository"].split("/")[1]
    params = prepared["params"]
    if prepared["operation"] == "add_pr_release_labels":
        reply = github.add_pr_release_labels(
            repo, params["number"], params["expectedHeadSha"],
            params["expectedBaseSha"], params["expectedBodySha256"],
            params["expectedLabelsSha256"], params["labels"],
            request_id=request_id, reason=reason)
    else:
        reply = github.remove_pr_release_label(
            repo, params["number"], params["expectedHeadSha"],
            params["expectedBaseSha"], params["expectedBodySha256"],
            params["expectedLabelsSha256"], params["name"],
            request_id=request_id, reason=reason)
    _check(type(reply) is dict,
           "Gateway ASK response unknown: STOP, inspect original request ID")
    if "intentId" in reply:
        identity = _native_identity(reply, request_id)
    else:
        _check(reply.get("requestId") == request_id,
               "Gateway ASK response unknown: STOP, inspect original request ID")
        identity = {"requestId": request_id}
    state = reply.get("state")
    # No confirmation of a GitHub write here. Owner approval, execution,
    # receipt, and exact-label readback are independent operator actions.
    return {
        **identity,
        "status": ("PENDING_OWNER_APPROVAL" if state == "PENDING"
                   else "STOP_INSPECT_ORIGINAL_REQUEST"),
        "gatewayState": state, "ownerApprovalRequired": True,
        "automaticRetryAllowed": False, "writeConfirmed": False,
    }
