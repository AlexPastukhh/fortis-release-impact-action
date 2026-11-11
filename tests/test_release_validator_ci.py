"""Exact-HEAD, trusted-diff, no-network test coverage for the shared CI adapter."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_FILE = ROOT / "tools/release/ci_entry.py"
spec = importlib.util.spec_from_file_location("ci_entry_test", ADAPTER_FILE)
import sys
sys.path.insert(0, str(ADAPTER_FILE.parent))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)

HAS_DEPS = (
    importlib.util.find_spec("yaml") is not None
    and importlib.util.find_spec("jsonschema") is not None
)


class ReleaseImpactCIAdapterTests(unittest.TestCase):
    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "local-only@example.invalid")
        self.git("config", "user.name", "Fortis offline fixture")
        for f, content in {
            "Dockerfile": "FROM scratch\n",
            "src/worker.py": "v = 1\n",
            "docs/README.md": "Documentation\n",
            "release/targets.yaml": (
                "schemaVersion: 1\n"
                "repository: test-project\n"
                "buildUnits:\n"
                "  - id: shared-image\n"
                "    artifactType: docker-image\n"
                "    recipe: Dockerfile\n"
                "    inputs: [Dockerfile, src/**]\n"
                "targets:\n"
                "  - id: api\n"
                "    buildUnit: shared-image\n"
                "    deployment:\n"
                "      kind: docker-compose-service\n"
                "      procedure: docs/README.md\n"
                "      environments: [local]\n"
                "    verification: [tests]\n"
                "  - id: worker\n"
                "    buildUnit: shared-image\n"
                "    deployment:\n"
                "      kind: docker-compose-service\n"
                "      procedure: docs/README.md\n"
                "      environments: [local]\n"
                "    verification: [tests]\n"
                "noDeploymentPaths: [docs/**]\n"
            ),
        }.items():
            path = self.repo / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "src/worker.py").write_text("v = 2\n")
        self.git("add", ".")
        self.git("commit", "-qm", "change source")
        self.head = self.git("rev-parse", "HEAD")
        self.body = (
            ci.START + "\n```yaml\n"
            "releaseImpact:\n"
            "  schemaVersion: 1\n"
            "  classification: deployment\n"
            "  reason: Shared Docker image is used by both API and worker targets.\n"
            "  targets:\n"
            "    - id: api\n"
            "      actions: [image-rebuild]\n"
            "    - id: worker\n"
            "      actions: [image-rebuild]\n"
            "  dependencies:\n"
            "    pullRequests: []\n"
            "    targets: []\n"
            "  verification: [tests]\n"
            "```\n" + ci.END
        )
        self.event = {
            "action": "synchronize",
            "repository": {"full_name": "owner/test-project"},
            "pull_request": {
                "number": 15,
                "state": "open",
                "draft": False,
                "head": {"sha": self.head,
                         "repo": {"full_name": "owner/test-project"}},
                "base": {"sha": self.base, "ref": "main",
                         "repo": {"full_name": "owner/test-project"}},
                "body": self.body,
            }
        }
        self.event_file = Path(self.tmp.name) / "event.json"
        self.write_event()

    def write_event(self):
        self.event_file.write_text(json.dumps(self.event))

    def info(self):
        return ci.read_event(self.event_file, event_name="pull_request",
                             repository="owner/test-project", head_sha=self.head)

    def test_valid_event_metadata(self):
        self.assertEqual(self.info(), (self.base, self.body, 15))

    def test_actual_git_diff_provenance(self):
        self.assertEqual(ci.changed_paths(self.repo, self.base, self.head),
                         ["src/worker.py"])

    def test_renamed_runtime_file_includes_removed_source(self):
        self.git("mv", "src/worker.py", "docs/worker.py")
        self.git("commit", "-qm", "move runtime source to docs")
        moved_sha = self.git("rev-parse", "HEAD")
        paths = ci.changed_paths(self.repo, self.head, moved_sha)
        self.assertEqual(paths, ["docs/worker.py", "src/worker.py"])

    def test_wrong_head_rejected(self):
        self.event["pull_request"]["head"]["sha"] = self.base
        self.write_event()
        with self.assertRaisesRegex(ci.ValidationError, "PR HEAD SHA"):
            self.info()

    def test_draft_and_fork_rejected(self):
        self.event["pull_request"]["draft"] = True
        self.write_event()
        with self.assertRaisesRegex(ci.ValidationError, "non-draft"):
            self.info()
        self.event["pull_request"]["draft"] = False
        self.event["pull_request"]["head"]["repo"]["full_name"] = "untrusted/fork"
        self.write_event()
        with self.assertRaisesRegex(ci.ValidationError, "cross-repository"):
            self.info()

    def test_wrong_repository_rejected(self):
        with self.assertRaisesRegex(ci.ValidationError, "repository mismatch"):
            ci.read_event(self.event_file, event_name="pull_request",
                          repository="different/test-project", head_sha=self.head)

    def test_missing_merge_base_rejected(self):
        with self.assertRaises((ci.ValidationError, subprocess.CalledProcessError)):
            ci.changed_paths(self.repo, "f"*40, self.head)

    def test_untrusted_changed_paths_not_loaded_from_event(self):
        self.event["changedFiles"] = ["docs/README.md"]
        self.write_event()
        self.assertEqual(ci.changed_paths(self.repo, *self.info()[:1], self.head),
                         ["src/worker.py"])

    @unittest.skipUnless(HAS_DEPS, "PyYAML/jsonschema installed by GitHub CI")
    def test_full_ci_accepts_both_targets_in_shared_build(self):
        result = ci.run(self.event_file, repository="owner/test-project",
                        event_name="pull_request", repo_root=self.repo)
        self.assertEqual(result["affectedTargets"], ["api", "worker"])
        self.assertEqual(result["headSha"], self.head)
        self.assertEqual(result["suggestedLabels"],
                         ["build:api", "build:worker", "release:image-rebuild"])

    @unittest.skipUnless(HAS_DEPS, "PyYAML/jsonschema installed by GitHub CI")
    def test_full_ci_fails_with_omitted_shared_target(self):
        self.event["pull_request"]["body"] = self.body.replace(
            "    - id: worker\n      actions: [image-rebuild]\n", ""
        )
        self.write_event()
        with self.assertRaisesRegex(ci.ValidationError, "worker"):
            ci.run(self.event_file, repository="owner/test-project",
                   event_name="pull_request", repo_root=self.repo)

    @unittest.skipUnless(HAS_DEPS, "PyYAML/jsonschema installed by GitHub CI")
    def test_full_ci_fails_without_pr_metadata_block(self):
        self.event["pull_request"]["body"] = "Just normal PR text"
        self.write_event()
        with self.assertRaisesRegex(ci.ValidationError, "release-impact block"):
            ci.run(self.event_file, repository="owner/test-project",
                   event_name="pull_request", repo_root=self.repo)


if __name__ == "__main__":
    unittest.main()
