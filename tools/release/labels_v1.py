#!/usr/bin/env python3
"""Pure V1 GitHub label projection from validated Release Impact evidence.

This module performs NO network calls and NEVER creates, adds or removes labels.
A separate trusted publisher must re-validate exact PR HEAD, catalog and diff
and must restrict its GitHub token to minimal PR-label permissions.
"""
from __future__ import annotations

from validate import ID, ValidationError, _check, _dict, validate_impact

MAX_LABEL_NAME = 50
MAX_LABELS = 250
RELEASE_PREFIX = "release:"
BUILD_PREFIX = "build:"
RELEASE_ACTIONS = frozenset({
    "image-rebuild", "config-deploy", "windows-update",
    "package-build", "migration-required", "unknown",
})


def project_labels(impact: dict, catalog: dict, changed_files: list[str]) -> dict:
    """Return conservative labels only AFTER semantic source-impact validation.

    Labels are projections for planning and human review, never authorization
    to build, deploy or mark a target as installed. Extra targets explicitly
    declared in the validated metadata are retained conservatively.
    """
    verified = validate_impact(impact, catalog, changed_files)
    info = _dict(impact.get("releaseImpact"), "releaseImpact")
    classification = verified["classification"]
    _check(classification in ("deployment", "no-deployment"),
           "labels: only validated deployment/no-deployment can be projected")
    declared = info["targets"]
    labels = set()
    if classification == "no-deployment":
        _check(not declared and not verified["affectedTargets"],
               "labels: no-deployment cannot include targets")
        labels.add("release:no-deployment")
    else:
        _check(bool(declared), "labels: deployment must have targets")
        for target in declared:
            target_id = target["id"]
            _check(type(target_id) is str and ID.fullmatch(target_id) is not None,
                   "labels: invalid target ID")
            label = BUILD_PREFIX + target_id
            _check(len(label) <= MAX_LABEL_NAME,
                   "labels: target ID exceeds GitHub label name limit")
            labels.add(label)
            for action in target["actions"]:
                _check(action in RELEASE_ACTIONS, "labels: unrecognized action")
                # 'unknown' is a review signal, never deployment approval.
                if action == "unknown":
                    labels.add("release:review-required")
                else:
                    labels.add(RELEASE_PREFIX + action)
    _check(0 < len(labels) <= MAX_LABELS, "labels: invalid total label count")
    _check(all(len(x) <= MAX_LABEL_NAME for x in labels),
           "labels: GitHub label name limit exceeded")
    return {
        "classification": classification,
        "affectedTargets": verified["affectedTargets"],
        "changedFiles": verified["changedFiles"],
        "suggestedLabels": sorted(labels),
    }
