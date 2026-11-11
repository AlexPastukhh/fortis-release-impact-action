#!/usr/bin/env python3
"""Read-only GitHub Actions adapter for the Fortis v1 release-impact validator.

Trust only the runner-provided event payload and the checked-out Git objects;
never trust a PR-provided changed-path list or execute code from PR content.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from validate import (
    MAX_PR_BODY,
    ValidationError,
    _check,
    _dict,
    _name,
    REPO,
    SHA,
    START,
    END,
    extract_pr_impact,
    load_data,
    validate_catalog,
    validate_impact,
    validate_schema,
)

MAX_EVENT = 1024 * 1024
MAX_PATHS = 2000
ALLOWED_ACTIONS = {"opened", "synchronize", "reopened", "edited", "ready_for_review"}


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, timeout=25, check=True,
    )
    return proc.stdout


def read_event(event_file: Path, *, event_name: str, repository: str, head_sha: str):
    """Read and bind GitHub's source metadata to exact same-repository PR SHA."""
    _check(event_name == "pull_request", "only pull_request events may pass")
    _check(event_file.is_file() and event_file.stat().st_size <= MAX_EVENT,
           "GitHub event file missing or oversized")
    event = _dict(json.loads(event_file.read_text(encoding="utf-8")), "GitHub event")
    _check(event.get("action") in ALLOWED_ACTIONS, "unsupported PR event action")
    source = _dict(event.get("repository"), "event.repository")
    _check(source.get("full_name") == repository, "event repository mismatch")
    pr = _dict(event.get("pull_request"), "event.pull_request")
    _check(type(pr.get("number")) is int and 1 <= pr["number"] <= 1000000,
           "invalid PR number")
    _check(pr.get("state") == "open" and not pr.get("draft", False),
           "only open and non-draft PRs are admitted")
    base = _dict(pr.get("base"), "PR base")
    head = _dict(pr.get("head"), "PR head")
    _check(base.get("ref") == "main", "release impact checks only PRs targeting main")
    _check(_dict(base.get("repo"), "base repo").get("full_name") == repository,
           "base repository mismatch")
    _check(_dict(head.get("repo"), "head repo").get("full_name") == repository,
           "cross-repository/fork PR requires a separately reviewed CI contract")
    _check(head.get("sha") == head_sha and SHA.fullmatch(head_sha) is not None,
           "PR HEAD SHA does not match trusted checked-out source")
    base_sha = base.get("sha")
    _check(type(base_sha) is str and SHA.fullmatch(base_sha) is not None,
           "invalid PR base SHA")
    body = pr.get("body")
    _check(type(body) is str and 0 < len(body) <= MAX_PR_BODY,
           "PR body absent or oversized")
    return base_sha, body, pr["number"]


def changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    """Compute the complete PR diff from Git objects, never from user input."""
    _check(_git(repo, "rev-parse", "HEAD").decode().strip() == head_sha,
           "checked-out Git HEAD differs from PR event HEAD")
    for sha in (base_sha, head_sha):
        _check(_git(repo, "cat-file", "-t", sha).strip() == b"commit",
               "required base/head commit missing: use full history checkout")
    ancestor = _git(repo, "merge-base", base_sha, head_sha).decode().strip()
    _check(SHA.fullmatch(ancestor) is not None, "no safe merge base")
    # --no-renames returns BOTH source and destination paths for moves.
    # Otherwise moving runtime source into a docs-only allowlist can hide
    # the deleted runtime file from impact classification.
    raw = _git(repo, "diff", "--no-ext-diff", "--no-renames",
               "--name-only", "-z", ancestor, head_sha)
    _check(raw.endswith(b"\0") or not raw, "invalid NUL-separated git diff")
    try:
        paths = [name.decode("utf-8") for name in raw.split(b"\0") if name]
    except UnicodeDecodeError as exc:
        raise ValidationError("changed filename is not UTF-8") from exc
    _check(bool(paths) and len(paths) <= MAX_PATHS and len(paths) == len(set(paths)),
           "empty, excessive or duplicate changed path list")
    return paths


def run(event_file: Path, *, repository: str, event_name: str,
        repo_root: Path) -> dict:
    _check("/" in repository and len(repository.split("/")) == 2,
           "GITHUB_REPOSITORY must be owner/repository")
    owner, repo_name = repository.split("/")
    _name(owner, REPO, "owner")
    _name(repo_name, REPO, "repository")
    root = repo_root.resolve(strict=True)
    _check((root / ".git").exists(), "GitHub checkout missing")
    head_sha = _git(root, "rev-parse", "HEAD").decode().strip()
    _check(SHA.fullmatch(head_sha) is not None, "invalid Git HEAD")
    base_sha, body, number = read_event(
        event_file, event_name=event_name, repository=repository, head_sha=head_sha,
    )
    paths = changed_paths(root, base_sha, head_sha)
    catalog_path = root / "release/targets.yaml"
    _check(catalog_path.is_file() and not catalog_path.is_symlink() and
           catalog_path.stat().st_size <= 128 * 1024,
           "release/targets.yaml missing, symlink or oversized")
    catalog = load_data(catalog_path.read_text(encoding="utf-8"), "yaml")
    schema_dir = Path(__file__).resolve().parents[2] / "docs/schemas"
    validate_schema(catalog, schema_dir / "build-target-catalog-v1.schema.json")
    validate_catalog(catalog, root, repo_name)
    impact = load_data(extract_pr_impact(body), "yaml")
    validate_schema(impact, schema_dir / "release-impact-v1.schema.json")
    result = validate_impact(impact, catalog, paths)
    return {"status": "PASS", "repository": repository, "pullRequest": number,
            "headSha": head_sha, **result}


def main() -> int:
    try:
        event = Path(os.environ["GITHUB_EVENT_PATH"])
        root = Path(os.environ["GITHUB_WORKSPACE"])
        result = run(
            event, repository=os.environ["GITHUB_REPOSITORY"],
            event_name=os.environ["GITHUB_EVENT_NAME"], repo_root=root,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValidationError, KeyError, ValueError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print("FORTIS_RELEASE_IMPACT_FAILED: " + str(exc)[:1000], file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
