#!/usr/bin/env python3
"""Bounded offline validation for Fortis build targets and PR Release Impact.

Source-only implementation: CI must install the declared PyYAML/jsonschema
dependencies and pin PR HEAD/changed-file evidence; no GitHub API/write here.
"""
from __future__ import annotations

import argparse
import fnmatch
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path

MAX_DOCUMENT = 128 * 1024
MAX_PR_BODY = 20_000
SHA = re.compile(r"[0-9a-f]{40}\Z")
ID = re.compile(r"[a-z][a-z0-9-]{0,99}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
START = "<!-- fortis-release-impact:start -->"
END = "<!-- fortis-release-impact:end -->"


class ValidationError(Exception):
    """Fail closed with an explanatory, non-secret validation failure."""


def _check(ok: bool, message: str):
    if not ok:
        raise ValidationError(message)


def _dict(value, where):
    _check(type(value) is dict, where + ": expected mapping")
    return value


def _list(value, where):
    _check(type(value) is list, where + ": expected list")
    return value


def _name(value, pattern, where):
    _check(type(value) is str and pattern.fullmatch(value) is not None, where + ": invalid name")
    return value


def _path(value: str, where: str, *, glob: bool = False):
    _check(type(value) is str and 0 < len(value) <= 240, where + ": invalid path")
    _check(not value.startswith("/") and not value.startswith("~") and
           not any(c in value for c in "\\:\x00\r\n") and
           all(part not in ("", ".", "..") for part in value.split("/")) and
           all(part != ".git" for part in value.split("/")),
           where + ": path is not a safe relative POSIX path")
    if not glob:
        _check(not any(c in value for c in "*?[]"), where + ": literal path required")
    return value


def _inside(root: Path, path: Path):
    _check(path.is_relative_to(root), "path escapes repository root")


def _present_file(root: Path, name: str, where: str):
    path = root / _path(name, where)
    _check(not path.is_symlink() and path.is_file(), where + ": file does not exist")
    _inside(root, path.resolve(strict=True))


def _glob_matches(root: Path, pattern: str, where: str):
    pattern = _path(pattern, where, glob=True)
    # Reject nested glob syntax with ambiguous implementation semantics.
    _check("{" not in pattern and "}" not in pattern, where + ": unsupported glob")
    found = False
    # Path.glob("src/**") visits directories but not necessarily leaf files.
    # Expand a trailing /** into actual descendant files, recursively.
    candidates = root.glob(pattern)
    if pattern.endswith("/**"):
        base = root / pattern[:-3]
        candidates = itertools.chain(candidates, base.rglob("*") if base.is_dir() else ())
    count = 0
    for p in candidates:
        count += 1
        _check(count <= 200000, where + ": glob matched too many entries")
        _check(not p.is_symlink(), where + ": symlink input")
        _inside(root, p.resolve(strict=True))
        if p.is_file():
            found = True
    _check(found, where + ": source glob matched no files")
    return pattern


def _unique(items: list[dict], field: str, where: str):
    ids = []
    for item in items:
        _dict(item, where)
        ids.append(_name(item.get(field), ID, where + "." + field))
    _check(len(ids) == len(set(ids)), where + ": duplicate IDs")
    return ids


def validate_catalog(data: dict, root: Path, repository: str):
    """Validate catalog semantic invariants beyond the JSON schema."""
    root = root.resolve(strict=True)
    _name(repository, REPO, "repository")
    data = _dict(data, "catalog")
    _check(data.get("schemaVersion") == 1, "catalog: unsupported schemaVersion")
    _check(data.get("repository") == repository, "catalog: repository does not match")
    units = _list(data.get("buildUnits"), "buildUnits")
    targets = _list(data.get("targets"), "targets")
    _check(len(units) <= 150 and len(targets) <= 200, "catalog: too many entries")
    unit_ids = _unique(units, "id", "buildUnits")
    target_ids = _unique(targets, "id", "targets")
    for unit in units:
        where = "buildUnits." + unit["id"]
        inputs = _list(unit.get("inputs"), where + ".inputs")
        _check(0 < len(inputs) <= 120 and len(inputs) == len(set(inputs)), where + ": invalid inputs")
        for pattern in inputs:
            _glob_matches(root, pattern, where + ".inputs")
        if unit.get("recipe") is not None:
            _present_file(root, unit["recipe"], where + ".recipe")
    for target in targets:
        where = "targets." + target["id"]
        uid = target.get("buildUnit")
        direct = target.get("inputs")
        _check(bool(uid) != bool(direct), where + ": exactly one of buildUnit or inputs is required")
        if uid:
            _check(uid in unit_ids, where + ": unknown buildUnit")
        else:
            direct = _list(direct, where + ".inputs")
            _check(0 < len(direct) <= 120 and len(direct) == len(set(direct)), where + ": invalid direct inputs")
            for pattern in direct:
                _glob_matches(root, pattern, where + ".inputs")
        deployment = _dict(target.get("deployment"), where + ".deployment")
        kind = deployment.get("kind")
        _check(kind in {"docker-compose-service", "owner-controlled", "windows-source",
                        "windows-application", "configuration", "package", "none"},
               where + ": invalid deployment kind")
        _present_file(root, deployment.get("procedure"), where + ".deployment.procedure")
        envs = _list(deployment.get("environments"), where + ".deployment.environments")
        _check(bool(envs) and len(envs) == len(set(envs)), where + ": environments must be unique")
        for env in envs:
            _name(env, ID, where + ".environment")
        _check(bool(_list(target.get("verification"), where + ".verification")),
               where + ": verification is mandatory")
        for dep in target.get("dependsOn", []):
            dep = _dict(dep, where + ".dependsOn")
            _name(dep.get("repository"), REPO, where + ".dependsOn.repository")
            _name(dep.get("target"), ID, where + ".dependsOn.target")
            _check(not (dep["repository"] == repository and dep["target"] == target["id"]),
                   where + ": self dependency")
            if dep["repository"] == repository:
                _check(dep["target"] in target_ids, where + ": missing local dependency")
    # External dependencies cannot be verified from the local catalog alone.
    local = {t["id"]: [d["target"] for d in t.get("dependsOn", [])
                        if d["repository"] == repository] for t in targets}
    visiting, visited = set(), set()

    def visit(node):
        _check(node not in visiting, "catalog: cycle in local target dependencies")
        if node in visited:
            return
        visiting.add(node)
        for nxt in local[node]:
            visit(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in local:
        visit(node)
    safe = _list(data.get("noDeploymentPaths", []), "noDeploymentPaths")
    _check(len(safe) <= 100 and len(safe) == len(set(safe)), "noDeploymentPaths: invalid list")
    for pattern in safe:
        _glob_matches(root, pattern, "noDeploymentPaths")
    return {"buildUnits": len(units), "targets": len(targets), "noDeploymentPaths": len(safe)}


def _matches(path: str, pattern: str) -> bool:
    # Conservative approximation: '*' may match '/', so over-classification
    # blocks unsafe omissions rather than creating a false no-deployment.
    return fnmatch.fnmatchcase(path, pattern)


def validate_impact(data: dict, catalog: dict, changed_files: list[str]):
    """Validate PR semantics against one source-pinned catalog and changed paths."""
    data = _dict(data, "impact")
    info = _dict(data.get("releaseImpact"), "releaseImpact")
    _check(info.get("schemaVersion") == 1, "impact: unsupported schemaVersion")
    classification = info.get("classification")
    _check(classification in ("deployment", "no-deployment", "unknown"), "impact: invalid classification")
    _check(type(info.get("reason")) is str and 10 <= len(info["reason"]) <= 2000,
           "impact: meaningful reason is required")
    changed_files = _list(changed_files, "changedFiles")
    _check(bool(changed_files) and len(changed_files) <= 2000 and
           len(set(changed_files)) == len(changed_files), "changedFiles: empty, duplicated or too large")
    for path in changed_files:
        _path(path, "changedFiles", glob=False)
    declared = _list(info.get("targets"), "releaseImpact.targets")
    _check(len(declared) <= 200, "impact: too many targets")
    declared_ids = _unique(declared, "id", "releaseImpact.targets")
    available = {t["id"]: t for t in catalog["targets"]}
    units = {u["id"]: u for u in catalog["buildUnits"]}
    for target in declared:
        _check(target["id"] in available, "impact: unknown target " + target["id"])
        actions = _list(target.get("actions"), "impact.actions")
        _check(bool(actions) and len(actions) == len(set(actions)), "impact: invalid actions")
        _check(set(actions) <= {"image-rebuild", "config-deploy", "windows-update",
                               "package-build", "migration-required", "unknown"},
               "impact: unknown action")
        if "image-rebuild" in actions:
            uid = available[target["id"]].get("buildUnit")
            _check(uid in units and units[uid]["artifactType"] == "docker-image",
                   "impact: image-rebuild requires a Docker image buildUnit")
    _dict(info.get("dependencies"), "impact.dependencies")
    _check(bool(_list(info.get("verification"), "impact.verification")),
           "impact: verification is mandatory")
    affected, unmatched = set(), []
    for name in changed_files:
        hits = set()
        for target in catalog["targets"]:
            patterns = target.get("inputs", [])
            uid = target.get("buildUnit")
            if uid:
                patterns = units[uid]["inputs"]
            if any(_matches(name, pattern) for pattern in patterns):
                hits.add(target["id"])
        affected.update(hits)
        if name == "release/targets.yaml":
            # A catalog edit changes the applicability contract itself.
            affected.update(available)
        elif not hits and not any(_matches(name, p) for p in catalog.get("noDeploymentPaths", [])):
            unmatched.append(name)
    _check(not unmatched, "impact: unclassified changed files: " + ", ".join(unmatched[:8]))
    if classification == "no-deployment":
        _check(not declared_ids and not affected, "impact: no-deployment with affected targets")
    elif classification == "deployment":
        _check(bool(declared_ids), "impact: deployment requires targets")
        missing = sorted(affected - set(declared_ids))
        _check(not missing, "impact: affected targets missing: " + ", ".join(missing))
        _check(bool(affected), "impact: claimed deployment without matched source inputs")
    else:
        raise ValidationError("impact: unknown requires explicit review; cannot pass release gate")
    return {"classification": classification, "affectedTargets": sorted(affected),
            "changedFiles": len(changed_files)}


def _json_no_duplicates(pairs):
    data = {}
    for k, v in pairs:
        _check(k not in data, "JSON: duplicate key: " + k)
        data[k] = v
    return data


def load_data(text: str, input_format: str):
    _check(len(text.encode("utf-8")) <= MAX_DOCUMENT, "document exceeds input size bound")
    if input_format == "json":
        return json.loads(text, object_pairs_hook=_json_no_duplicates)
    try:
        import yaml
    except ImportError as exc:
        raise ValidationError("PyYAML missing: install tools/release/requirements.txt") from exc

    try:
        for event in yaml.parse(text):
            _check(not isinstance(event, yaml.AliasEvent) and not getattr(event, "anchor", None),
                   "YAML anchors and aliases are forbidden")
    except yaml.YAMLError as exc:
        raise ValidationError("invalid YAML") from exc

    class UniqueSafeLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                _check(type(key) is str and key not in result, "YAML: duplicate or non-string mapping key")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        return yaml.load(text, Loader=UniqueSafeLoader)
    except yaml.YAMLError as exc:
        raise ValidationError("invalid YAML") from exc


def validate_schema(data: dict, path: Path):
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise ValidationError("jsonschema missing: install tools/release/requirements.txt") from exc
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: str(e.path))
    _check(not errors, "JSON Schema: " + "; ".join(e.message for e in errors[:5]))


def extract_pr_impact(body: str):
    _check(len(body) <= MAX_PR_BODY, "PR body is too large")
    _check(body.count(START) == 1 and body.count(END) == 1, "PR: one release-impact block required")
    a = body.index(START) + len(START)
    b = body.index(END)
    _check(a < b, "PR: invalid release-impact marker order")
    fenced = body[a:b].strip()
    match = re.fullmatch(r"```yaml\n([\s\S]{1,60000}?)\n```", fenced)
    _check(match is not None, "PR: expected one fenced yaml block")
    return match.group(1)


def _read(path: Path, limit=MAX_DOCUMENT):
    _check(path.is_file() and not path.is_symlink() and path.stat().st_size <= limit,
           "input file absent, symlink or over limit")
    return path.read_text(encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=("catalog", "impact"))
    ap.add_argument("--repo-root", required=True, type=Path)
    ap.add_argument("--repository", required=True)
    ap.add_argument("--catalog", default="release/targets.yaml")
    ap.add_argument("--input-format", choices=("yaml", "json"), default="yaml")
    ap.add_argument("--pr-body", type=Path)
    ap.add_argument("--changed-files-json", type=Path)
    ap.add_argument("--expected-head-sha")
    args = ap.parse_args(argv)
    try:
        root = args.repo_root.resolve(strict=True)
        _check((root / ".git").exists(), "repo-root must be a Git checkout")
        here = Path(__file__).resolve().parents[2]
        schema_dir = here / "docs" / "schemas"
        catalog_text = _read(root / _path(args.catalog, "catalog"))
        catalog = load_data(catalog_text, args.input_format)
        validate_schema(catalog, schema_dir / "build-target-catalog-v1.schema.json")
        info = validate_catalog(catalog, root, args.repository)
        if args.mode == "impact":
            _check(args.pr_body is not None and args.changed_files_json is not None and
                   args.expected_head_sha is not None, "impact requires body, changed files and exact head SHA")
            _check(type(args.expected_head_sha) is str and SHA.fullmatch(args.expected_head_sha),
                   "invalid expected PR head SHA")
            checked = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                     capture_output=True, text=True, timeout=8, check=True).stdout.strip()
            _check(checked == args.expected_head_sha, "checked-out HEAD differs from exact PR HEAD")
            content = extract_pr_impact(_read(args.pr_body, MAX_PR_BODY))
            impact = load_data(content, args.input_format)
            validate_schema(impact, schema_dir / "release-impact-v1.schema.json")
            changed = json.loads(_read(args.changed_files_json), object_pairs_hook=_json_no_duplicates)
            info = validate_impact(impact, catalog, changed)
        print(json.dumps({"status": "PASS", "mode": args.mode, **info}, sort_keys=True))
        return 0
    except (ValidationError, ValueError, OSError, json.JSONDecodeError,
            subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("RELEASE_VALIDATION_FAILED: " + str(exc)[:1200], file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
