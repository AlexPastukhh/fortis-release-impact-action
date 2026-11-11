"""Fail-closed pure V1 labels projection: no GitHub writes or PR code."""
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/release"))
from labels_v1 import project_labels, ValidationError


class LabelProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("Dockerfile", "src/app.py", "docs/README.md", "release/targets.yaml"):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("static fixture", encoding="utf-8")
        self.catalog = {
            "schemaVersion": 1, "repository": "sample",
            "buildUnits": [{"id": "shared-image", "artifactType": "docker-image",
                            "recipe": "Dockerfile", "inputs": ["Dockerfile", "src/**"]}],
            "targets": [
                {"id": ident, "buildUnit": "shared-image",
                 "deployment": {"kind": "docker-compose-service",
                                "procedure": "docs/README.md",
                                "environments": ["local"]}, "verification": ["tests"]}
                for ident in ("api", "worker")
            ],
            "noDeploymentPaths": ["docs/**"],
        }
        self.impact = {
            "releaseImpact": {
                "schemaVersion": 1, "classification": "deployment",
                "reason": "Both services share a Docker image that must be rebuilt.",
                "targets": [
                    {"id": "api", "actions": ["image-rebuild"]},
                    {"id": "worker", "actions": ["image-rebuild"]}],
                "dependencies": {"pullRequests": [], "targets": []},
                "verification": ["tests"]}}

    def test_deployment_shared_build_unit_labels_each_target_only_once(self):
        result = project_labels(self.impact, self.catalog, ["src/app.py"])
        self.assertEqual(result["suggestedLabels"],
                         ["build:api", "build:worker", "release:image-rebuild"])
        self.assertEqual(result["affectedTargets"], ["api", "worker"])

    def test_no_deployment_has_only_no_deployment_label(self):
        value = deepcopy(self.impact)
        value["releaseImpact"].update(classification="no-deployment",
                                      targets=[], reason="README update only, no runtime.")
        result = project_labels(value, self.catalog, ["docs/README.md"])
        self.assertEqual(result["suggestedLabels"], ["release:no-deployment"])

    def test_mixed_actions_keep_all_necessary_review_signals(self):
        value = deepcopy(self.impact)
        value["releaseImpact"]["targets"][0]["actions"] = [
            "image-rebuild", "migration-required", "unknown"]
        result = project_labels(value, self.catalog, ["src/app.py"])
        self.assertEqual(result["suggestedLabels"],
                         ["build:api", "build:worker", "release:image-rebuild",
                          "release:migration-required", "release:review-required"])

    def test_no_deployment_cannot_hide_runtime_changes(self):
        value = deepcopy(self.impact)
        value["releaseImpact"].update(classification="no-deployment", targets=[])
        with self.assertRaisesRegex(ValidationError, "affected targets"):
            project_labels(value, self.catalog, ["src/app.py"])

    def test_missing_affected_target_fails_closed(self):
        value = deepcopy(self.impact)
        value["releaseImpact"]["targets"].pop()
        with self.assertRaisesRegex(ValidationError, "worker"):
            project_labels(value, self.catalog, ["src/app.py"])

    def test_unknown_classification_never_generates_labels(self):
        value = deepcopy(self.impact)
        value["releaseImpact"]["classification"] = "unknown"
        with self.assertRaisesRegex(ValidationError, "explicit review"):
            project_labels(value, self.catalog, ["src/app.py"])

    def test_unclassified_files_fail_closed(self):
        with self.assertRaisesRegex(ValidationError, "unclassified"):
            project_labels(self.impact, self.catalog, ["untracked.py"])

    def test_unknown_declared_target_fails_closed(self):
        value = deepcopy(self.impact)
        value["releaseImpact"]["targets"][0]["id"] = "not-in-catalog"
        with self.assertRaisesRegex(ValidationError, "unknown target"):
            project_labels(value, self.catalog, ["src/app.py"])

    def test_oversized_github_target_label_fails_closed(self):
        long_id = "a" * 45
        catalog = deepcopy(self.catalog)
        catalog["targets"][0]["id"] = long_id
        value = deepcopy(self.impact)
        value["releaseImpact"]["targets"][0]["id"] = long_id
        with self.assertRaisesRegex(ValidationError, "label name limit"):
            project_labels(value, catalog, ["src/app.py"])

    def test_catalog_mutation_forces_all_targets_to_be_listed(self):
        result = project_labels(self.impact, self.catalog, ["release/targets.yaml"])
        self.assertEqual(result["suggestedLabels"],
                         ["build:api", "build:worker", "release:image-rebuild"])

    def test_no_labels_imply_deployed_or_production_authority(self):
        result = project_labels(self.impact, self.catalog, ["src/app.py"])
        self.assertFalse(any("deployed" in x or "production" in x
                             for x in result["suggestedLabels"]))

    def test_extra_declared_target_retained_conservatively(self):
        # Validator permits deliberately conservative over-declaration.
        result = project_labels(self.impact, self.catalog, ["src/app.py"])
        self.assertIn("build:worker", result["suggestedLabels"])


if __name__ == "__main__":
    unittest.main()
