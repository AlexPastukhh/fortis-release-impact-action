"""Offline tests: authenticated typed Gateway reads -> trusted label preview."""
from __future__ import annotations

import copy
import hashlib
import unittest

import test_release_validator_ci as fixture_tests
from test_release_validator_ci import HAS_DEPS
from gateway_snapshot_v1 import collect_gateway_snapshot, preview_gateway_snapshot
from validate import ValidationError


class FakeTypedGateway:
    """Projection-only fake: no write methods, no network or credentials."""

    def __init__(self, fixture):
        self.calls = []
        self.repo = "owner/test-project"
        self.labels = ["bug", "build:old", "release:review-required"]
        self.definitions = [
            "bug", "build:old", "release:review-required", "build:api",
            "build:worker", "release:image-rebuild"]
        self.body = fixture.body
        self.pr = {
            "number": 15, "state": "open", "draft": False, "merged": False,
            "head": {"ref": "task/release-test", "sha": fixture.head},
            "base": {"ref": "main", "sha": fixture.base},
        }
        self.run = {
            "id": 91234, "attempt": 1, "workflowId": 45678,
            "workflowPath": ".github/workflows/release-impact.yml",
            "event": "pull_request", "status": "completed",
            "conclusion": "success", "headSha": fixture.head,
            "headBranch": "task/release-test",
            "repository": self.repo, "headRepository": self.repo,
            "pullRequests": [{"number": 15, "headSha": fixture.head,
                              "baseSha": fixture.base}],
        }
        self.job = {"id": 44, "name": "release-impact",
                    "status": "completed", "conclusion": "success"}
        self.paged_runs = None
        self.run_attempt = 1

    def list_workflow_runs(self, repo, *, page, per_page):
        self.calls.append(("runs", repo, page, per_page))
        assert per_page == 20
        if self.paged_runs is not None:
            rows = self.paged_runs
        else:
            rows = [{
                "id": self.run["id"], "runAttempt": self.run_attempt,
                "headSha": self.run["headSha"], "status": self.run["status"],
                "conclusion": self.run["conclusion"],
            }]
        offset = (page - 1) * per_page
        return {"operation": "list_workflow_runs", "totalCount": len(rows),
                "items": copy.deepcopy(rows[offset:offset + per_page])}

    def get_workflow_run(self, repo, run_id, attempt):
        self.calls.append(("run", repo, run_id, attempt))
        return {"operation": "get_workflow_run",
                "data": {"run": copy.deepcopy(self.run)}}

    def list_workflow_jobs(self, repo, run_id, attempt, *, page, per_page):
        self.calls.append(("jobs", repo, run_id, attempt, page, per_page))
        return {"operation": "list_workflow_jobs", "data": {
            "run": copy.deepcopy(self.run), "items": [copy.deepcopy(self.job)],
            "total": 1, "page": page, "perPage": per_page, "complete": True}}

    def get_pr_release_impact_evidence(self, repo, number, sha):
        self.calls.append(("evidence", repo, number, sha))
        return {
            "operation": "get_pr_release_impact_evidence", "number": number,
            "headSha": self.run["headSha"],
            "baseSha": self.run["pullRequests"][0]["baseSha"],
            "headRepository": self.repo, "baseRepository": self.repo,
            "headBranch": self.pr["head"]["ref"], "baseBranch": "main",
            "body": self.body,
            "bodySha256": hashlib.sha256(self.body.encode()).hexdigest(),
            "state": "open", "draft": False, "merged": False,
        }

    def get_pr_release_label_context(self, repo, number):
        self.calls.append(("label-context", repo, number))
        return {"operation": "get_pr_release_label_context", "number": number,
                "headSha": self.run["headSha"],
                "baseSha": self.run["pullRequests"][0]["baseSha"],
                "branch": self.pr["head"]["ref"],
                "bodySha256": hashlib.sha256(self.body.encode()).hexdigest(),
                "labelsSha256": "a" * 64, "labels": sorted(self.labels)}

    def get_pull_request(self, repo, number):
        self.calls.append(("pr", repo, number))
        return {"operation": "get_pull_request",
                "pullRequest": copy.deepcopy(self.pr)}

    def list_pr_labels(self, repo, number, *, page, per_page):
        self.calls.append(("pr-labels", repo, number, page))
        return self._labels(self.labels, page, per_page, "list_pr_labels",
                            number=number)

    def list_repository_labels(self, repo, *, page, per_page):
        self.calls.append(("defs", repo, page))
        return self._labels(self.definitions, page, per_page,
                            "list_repository_labels")

    @staticmethod
    def _labels(labels, page, per_page, operation, **extra):
        offset = (page - 1) * per_page
        subset = labels[offset:offset + per_page]
        return {"operation": operation, "page": page, "perPage": per_page,
                "items": [{"name": x} for x in subset],
                "hasNextPage": len(subset) == per_page, **extra}


class GatewaySnapshotTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_tests.ReleaseImpactCIAdapterTests("test_valid_event_metadata")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.client = FakeTypedGateway(fixture)
        self.args = {
            "repository": "owner/test-project", "run_id": 91234,
            "pull_request": 15, "workflow_id": 45678,
            "workflow_path": ".github/workflows/release-impact.yml",
            "required_job_names": ["release-impact"],
        }

    def collect(self):
        return collect_gateway_snapshot(self.client, **self.args)

    def test_snapshot_binds_independent_gateway_sources(self):
        snap = self.collect()
        self.assertTrue(snap["snapshotOnly"])
        self.assertEqual(snap["latest_run_attempt"], 1)
        self.assertEqual(snap["current_pr"]["body"], self.fixture.body)
        self.assertEqual(snap["job_inventory"]["jobs"][0]["name"],
                         "release-impact")
        self.assertIn("release:review-required", snap["existing_labels"])
        self.assertEqual(self.client.calls[-1][0], "label-context")
        self.assertFalse(hasattr(self.client, "add_pr_release_labels"))

    @unittest.skipUnless(HAS_DEPS, "GitHub CI installs yaml/jsonschema")
    def test_live_validator_composition_no_writes(self):
        plan = preview_gateway_snapshot(self.collect(), repo_root=self.fixture.repo)
        self.assertEqual(plan["status"], "PREVIEW_ONLY")
        self.assertEqual(plan["add"],
                         ["build:api", "build:worker", "release:image-rebuild"])
        self.assertEqual(plan["remove"], ["build:old"])
        self.assertEqual(plan["preservedHumanLabels"], ["bug"])
        self.assertTrue(plan["preservedReviewFlag"])
        self.assertIs(plan["writeEnabled"], False)

    def test_latest_rerun_failure_or_attempt_mismatch_stops(self):
        self.client.run_attempt = 2
        with self.assertRaisesRegex(ValidationError, "run latest attempt"):
            self.collect()
        self.client.run_attempt = 1
        self.client.run["conclusion"] = "failure"
        with self.assertRaisesRegex(ValidationError, "run latest attempt"):
            self.collect()

    def test_queued_or_cancelled_jobs_stop(self):
        for status, conclusion in (("queued", None), ("in_progress", None),
                                   ("completed", "cancelled")):
            with self.subTest(status=status):
                self.client.job["status"] = status
                self.client.job["conclusion"] = conclusion
                with self.assertRaisesRegex(ValidationError, "job not completed"):
                    self.collect()

    def test_context_or_branch_drift_stops(self):
        self.client.pr["base"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValidationError, "head/base SHA drift"):
            self.collect()
        self.client.pr["base"]["sha"] = self.fixture.base
        self.client.pr["head"]["ref"] = "main"
        self.client.run["headBranch"] = "task/release-test"
        with self.assertRaisesRegex(ValidationError, "branch/state drift"):
            self.collect()

    def test_labels_must_match_context(self):
        self.client.labels.append("release:no-deployment")
        original = self.client.get_pr_release_label_context
        def stale_context(repo, n):
            d = original(repo, n)
            d["labels"] = ["bug"]
            return d
        self.client.get_pr_release_label_context = stale_context
        with self.assertRaisesRegex(ValidationError, "label inventory drift"):
            self.collect()
    def test_duplicate_labels_stop(self):
        self.client.labels = ["bug", "bug"]
        with self.assertRaisesRegex(ValidationError, "duplicate label"):
            self.collect()

    def test_missing_run_from_bounded_listing_stops(self):
        self.client.paged_runs = [
            {"id": 5000 + i, "runAttempt": 1, "headSha": "a" * 40,
             "status": "completed", "conclusion": "success"}
            for i in range(43)]
        with self.assertRaisesRegex(ValueError, "missing from bounded"):
            self.collect()
        self.assertEqual([c for c in self.client.calls if c[0] == "runs"][-1][2],
                         3)

    def test_pagination_with_many_runs_finds_exact_latest_attempt(self):
        prefix = [{"id": 5000 + i, "runAttempt": 1, "headSha": "a" * 40,
                   "status": "completed", "conclusion": "success"}
                  for i in range(27)]
        prefix.append({"id": 91234, "runAttempt": 1,
                       "headSha": self.fixture.head, "status": "completed",
                       "conclusion": "success"})
        self.client.paged_runs = prefix
        self.assertEqual(self.collect()["latest_run_attempt"], 1)
        self.assertEqual([c for c in self.client.calls if c[0] == "runs"][-1][2], 2)

    def test_oversized_job_list_or_incomplete_pagination_stops(self):
        original = self.client.list_workflow_jobs
        def incomplete(*args, **kwargs):
            d = original(*args, **kwargs)
            d["data"]["complete"] = False
            return d
        self.client.list_workflow_jobs = incomplete
        with self.assertRaisesRegex(ValidationError, "completeness"):
            self.collect()

    def test_digest_mismatch_stops(self):
        original = self.client.get_pr_release_impact_evidence
        def wrong(*args, **kwargs):
            d = original(*args, **kwargs)
            d["bodySha256"] = "b" * 64
            return d
        self.client.get_pr_release_impact_evidence = wrong
        with self.assertRaisesRegex(ValidationError, "description digest mismatch"):
            self.collect()


if __name__ == "__main__":
    unittest.main()
