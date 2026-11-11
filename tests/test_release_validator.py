"""No-network semantic and fail-closed tests for shared Fortis release rules."""
import importlib.util
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_validation", ROOT / "tools/release/validate.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)


class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        for name in ["Dockerfile", "src/app.py", "docs/README.md", "release/targets.yaml"]:
            p = self.repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("source\n")
        self.catalog = {
            "schemaVersion": 1,
            "repository": "sample",
            "buildUnits": [
                {"id": "app-image", "artifactType": "docker-image",
                 "recipe": "Dockerfile", "inputs": ["Dockerfile", "src/**"]}
            ],
            "targets": [
                {"id": target, "buildUnit": "app-image",
                 "deployment": {"kind": "docker-compose-service", "procedure": "docs/README.md",
                                "environments": ["local"]}, "verification": ["health"]}
                for target in ("api", "worker")
            ],
            "noDeploymentPaths": ["docs/**"],
        }
        self.impact = {
            "releaseImpact": {
                "schemaVersion": 1,
                "classification": "deployment",
                "reason": "Shared application source changed; both services require the new image.",
                "targets": [
                    {"id": "api", "actions": ["image-rebuild"]},
                    {"id": "worker", "actions": ["image-rebuild"]},
                ],
                "dependencies": {"pullRequests": [], "targets": []},
                "verification": ["tests"],
            }
        }

    def test_catalog_valid(self):
        self.assertEqual(rv.validate_catalog(self.catalog, self.repo, "sample")["targets"], 2)

    def test_catalog_repository_mismatch_rejected(self):
        with self.assertRaisesRegex(rv.ValidationError, "repository does not match"):
            rv.validate_catalog(self.catalog, self.repo, "other")

    def test_duplicate_unit_rejected(self):
        v = deepcopy(self.catalog)
        v["buildUnits"].append(deepcopy(v["buildUnits"][0]))
        with self.assertRaisesRegex(rv.ValidationError, "duplicate IDs"):
            rv.validate_catalog(v, self.repo, "sample")

    def test_missing_recipe_rejected(self):
        v = deepcopy(self.catalog)
        v["buildUnits"][0]["recipe"] = "missing/Dockerfile"
        with self.assertRaisesRegex(rv.ValidationError, "does not exist"):
            rv.validate_catalog(v, self.repo, "sample")

    def test_traversal_glob_rejected(self):
        v = deepcopy(self.catalog)
        v["buildUnits"][0]["inputs"] = ["../secret/**"]
        with self.assertRaisesRegex(rv.ValidationError, "safe relative"):
            rv.validate_catalog(v, self.repo, "sample")

    def test_symlink_input_rejected(self):
        external = Path(self.tmp.name).parent / (Path(self.tmp.name).name + "-outside")
        external.write_text("outside")
        self.addCleanup(external.unlink)
        (self.repo / "src/escaped.py").symlink_to(external)
        with self.assertRaisesRegex(rv.ValidationError, "symlink input"):
            rv.validate_catalog(self.catalog, self.repo, "sample")

    def test_local_dependency_cycle_rejected(self):
        v = deepcopy(self.catalog)
        v["targets"][0]["dependsOn"] = [{"repository": "sample", "target": "worker"}]
        v["targets"][1]["dependsOn"] = [{"repository": "sample", "target": "api"}]
        with self.assertRaisesRegex(rv.ValidationError, "cycle"):
            rv.validate_catalog(v, self.repo, "sample")

    def test_shared_image_requires_both_targets(self):
        rv.validate_catalog(self.catalog, self.repo, "sample")
        result = rv.validate_impact(self.impact, self.catalog, ["src/app.py"])
        self.assertEqual(result["affectedTargets"], ["api", "worker"])

    def test_shared_image_cannot_omit_worker(self):
        v = deepcopy(self.impact)
        v["releaseImpact"]["targets"] = v["releaseImpact"]["targets"][:1]
        with self.assertRaisesRegex(rv.ValidationError, "worker"):
            rv.validate_impact(v, self.catalog, ["src/app.py"])

    def test_docs_only_requires_no_deployment_explicit(self):
        v = deepcopy(self.impact)
        v["releaseImpact"].update(classification="no-deployment", targets=[])
        self.assertEqual(rv.validate_impact(v, self.catalog, ["docs/README.md"])["classification"],
                         "no-deployment")

    def test_docs_and_runtime_cannot_be_no_deployment(self):
        v = deepcopy(self.impact)
        v["releaseImpact"].update(classification="no-deployment", targets=[])
        with self.assertRaisesRegex(rv.ValidationError, "affected targets"):
            rv.validate_impact(v, self.catalog, ["src/app.py", "docs/README.md"])

    def test_unclassified_file_fails_closed(self):
        (self.repo / "new-component.py").write_text("new\n")
        with self.assertRaisesRegex(rv.ValidationError, "unclassified"):
            rv.validate_impact(self.impact, self.catalog, ["new-component.py"])

    def test_catalog_edit_requires_complete_impact(self):
        v = deepcopy(self.impact)
        v["releaseImpact"]["targets"] = v["releaseImpact"]["targets"][:1]
        with self.assertRaisesRegex(rv.ValidationError, "worker"):
            rv.validate_impact(v, self.catalog, ["release/targets.yaml"])

    def test_unknown_never_passes(self):
        v = deepcopy(self.impact)
        v["releaseImpact"]["classification"] = "unknown"
        with self.assertRaisesRegex(rv.ValidationError, "explicit review"):
            rv.validate_impact(v, self.catalog, ["src/app.py"])

    def test_wrong_image_action_rejected(self):
        v = deepcopy(self.catalog)
        v["buildUnits"][0]["artifactType"] = "application-package"
        with self.assertRaisesRegex(rv.ValidationError, "Docker image"):
            rv.validate_impact(self.impact, v, ["src/app.py"])

    def test_bad_json_duplicate_key_rejected(self):
        with self.assertRaisesRegex(rv.ValidationError, "duplicate key"):
            rv.load_data('{"schemaVersion":1,"schemaVersion":2}', "json")

    def test_pr_impact_markers(self):
        body = (
            "Intro\n" + rv.START + "\n```yaml\nreleaseImpact: {}\n```\n" +
            rv.END + "\nEnd"
        )
        self.assertEqual(rv.extract_pr_impact(body), "releaseImpact: {}")

    def test_duplicate_pr_marker_rejected(self):
        body = rv.START + rv.START + rv.END
        with self.assertRaisesRegex(rv.ValidationError, "one release-impact"):
            rv.extract_pr_impact(body)

    def test_empty_changed_files_rejected(self):
        with self.assertRaisesRegex(rv.ValidationError, "empty"):
            rv.validate_impact(self.impact, self.catalog, [])


if __name__ == "__main__":
    unittest.main()
