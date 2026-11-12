"""Offline, no-write pilot of authenticated CI -> independently checked labels."""
from __future__ import annotations

import copy
import unittest

import test_release_validator_ci as fixture_tests
from test_release_validator_ci import HAS_DEPS
from trusted_label_preview_v1 import preview_trusted_labels
from validate import ValidationError


class TrustedPublisherPreviewTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_tests.ReleaseImpactCIAdapterTests("test_valid_event_metadata")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        repo = "owner/test-project"
        self.pr = copy.deepcopy(fixture.event["pull_request"])
        self.pr["merged"] = False
        self.pr["merged_at"] = None
        self.run = {
            "id": 91234, "run_attempt": 1, "workflow_id": 45678,
            "status": "completed", "conclusion": "success",
            "event": "pull_request", "head_sha": fixture.head,
            "repository": {"full_name": repo},
            "head_repository": {"full_name": repo},
            "pull_requests": [{"number": 15}],
        }
        self.event = {"action": "completed",
                      "workflow_run": copy.deepcopy(self.run)}
        self.jobs = {
            "run_id": 91234, "run_attempt": 1, "complete": True,
            "jobs": [{"id": 121, "name": "release-impact",
                      "status": "completed", "conclusion": "success"}],
        }
        self.kwargs = {
            "event": self.event, "trusted_run": self.run,
            "current_pr": self.pr, "job_inventory": self.jobs,
            "latest_run_attempt": 1, "repository": repo,
            "workflow_id": 45678, "required_job_names": ["release-impact"],
            "repo_root": fixture.repo,
            "existing_labels": ["bug", "build:stale", "release:review-required"],
            "available_labels": ["build:api", "build:worker",
                                 "release:image-rebuild"],
        }

    def preview(self):
        return preview_trusted_labels(**self.kwargs)

    @unittest.skipUnless(HAS_DEPS, "GitHub CI installs yaml/jsonschema")
    def test_full_preview_rechecks_actual_git_diff_and_preserves_human(self):
        plan = self.preview()
        self.assertEqual(plan["status"], "PREVIEW_ONLY")
        self.assertEqual(plan["suggestedLabels"], [
            "build:api", "build:worker", "release:image-rebuild"])
        self.assertEqual(plan["add"], [
            "build:api", "build:worker", "release:image-rebuild"])
        self.assertEqual(plan["remove"], ["build:stale"])
        self.assertEqual(plan["preservedHumanLabels"], ["bug"])
        self.assertTrue(plan["preservedReviewFlag"])
        self.assertEqual(plan["validation"]["changedFiles"], 1)
        self.assertEqual(plan["workflowRunAttempt"], 1)
        self.assertIs(plan["writeEnabled"], False)

    @unittest.skipUnless(HAS_DEPS, "GitHub CI installs yaml/jsonschema")
    def test_repeat_preview_is_idempotent(self):
        self.kwargs["existing_labels"] = [
            "bug", "build:api", "build:worker", "release:image-rebuild"]
        plan = self.preview()
        self.assertEqual((plan["add"], plan["remove"]), ([], []))

    @unittest.skipUnless(HAS_DEPS, "GitHub CI installs yaml/jsonschema")
    def test_checked_out_wrong_head_is_denied(self):
        self.fixture.git("checkout", "-q", self.fixture.base)
        with self.assertRaisesRegex(ValidationError, "checked-out source"):
            self.preview()

    @unittest.skipUnless(HAS_DEPS, "GitHub CI installs yaml/jsonschema")
    def test_pr_body_omits_runtime_target_and_fails_independent_check(self):
        self.pr["body"] = self.pr["body"].replace(
            "    - id: worker\n      actions: [image-rebuild]\n", "")
        with self.assertRaisesRegex(ValidationError, "worker"):
            self.preview()

    def test_older_successful_run_does_not_override_failed_rerun(self):
        self.kwargs["latest_run_attempt"] = 2
        with self.assertRaisesRegex(ValidationError, "stale workflow attempt"):
            self.preview()
    def test_running_or_queued_ci_job_blocks_preview(self):
        for value in ("in_progress", "queued"):
            with self.subTest(value=value):
                self.jobs["jobs"][0]["status"] = value
                self.jobs["jobs"][0]["conclusion"] = None
                with self.assertRaisesRegex(ValidationError, "job not completed"):
                    self.preview()

    def test_partial_job_inventory_blocks_preview(self):
        self.jobs["complete"] = False
        with self.assertRaisesRegex(ValidationError, "pages incomplete"):
            self.preview()

    def test_fork_or_stale_pr_head_blocks_preview(self):
        self.pr["head"]["repo"]["full_name"] = "other/test-project"
        with self.assertRaisesRegex(ValidationError, "cross-repository"):
            self.preview()
        self.pr["head"]["repo"]["full_name"] = "owner/test-project"
        self.pr["head"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValidationError, "stale PR HEAD"):
            self.preview()

    def test_failed_run_denies_preview_even_if_jobs_pass(self):
        self.run["conclusion"] = "failure"
        with self.assertRaisesRegex(ValidationError, "successful"):
            self.preview()


if __name__ == "__main__":
    unittest.main()
