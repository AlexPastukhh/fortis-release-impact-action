"""No-write negative tests for mandatory Source CI + Release Impact admission."""
from __future__ import annotations

import copy
import unittest

import test_release_validator_ci as fixture_tests
from test_release_validator_ci import HAS_DEPS
from test_release_validator_gateway_snapshot import FakeTypedGateway
from gateway_snapshot_v1 import collect_gateway_snapshot
from required_ci_v1 import collect_all_required_ci, preview_with_all_required_ci
from validate import ValidationError


class FakeRequiredCI(FakeTypedGateway):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.source = copy.deepcopy(self.run)
        self.source.update({
            "id": 91235, "workflowId": 56789,
            "workflowPath": ".github/workflows/source-ci.yml",
        })
        self.source_jobs = [
            {"id": 51, "name": "offline-tests",
             "status": "completed", "conclusion": "success"},
            {"id": 52, "name": "audit-projections",
             "status": "completed", "conclusion": "success"},
        ]
        self.source_run_attempt = 1
        self.run_count = 0
        self.newer_source = None
        self.newer_after = 2

    def _listing(self, run, name, run_number):
        return {
            "id": run["id"], "runAttempt": (
                self.source_run_attempt if name == "Source CI" else 1),
            "runNumber": run_number,
            "name": name, "event": run["event"],
            "status": run["status"], "conclusion": run["conclusion"],
            "headSha": run["headSha"], "headBranch": run["headBranch"],
        }

    def list_workflow_runs(self, repo, *, page, per_page):
        self.calls.append(("runs", repo, page, per_page))
        self.run_count += 1
        rows = [self._listing(self.source, "Source CI", 4),
                self._listing(self.run, "Fortis Release Impact", 5)]
        if (self.newer_source is not None and
                self.run_count >= self.newer_after):
            rows.append(copy.deepcopy(self.newer_source))
        offset = (page - 1) * per_page
        return {"operation": "list_workflow_runs", "totalCount": len(rows),
                "items": copy.deepcopy(rows[offset:offset+per_page])}

    def get_workflow_run(self, repo, run_id, attempt):
        self.calls.append(("run", repo, run_id, attempt))
        data = self.source if run_id == self.source["id"] else self.run
        return {"operation": "get_workflow_run",
                "data": {"run": copy.deepcopy(data)}}

    def list_workflow_jobs(self, repo, run_id, attempt, *, page, per_page):
        self.calls.append(("jobs", repo, run_id, attempt, page, per_page))
        if run_id == self.source["id"]:
            run = self.source
            jobs = self.source_jobs
        else:
            run = self.run
            jobs = [self.job]
        offset = (page-1)*per_page
        return {"operation": "list_workflow_jobs", "data": {
            "run": copy.deepcopy(run),
            "items": copy.deepcopy(jobs[offset:offset+per_page]),
            "total": len(jobs), "page": page, "perPage": per_page,
            "complete": offset+len(jobs[offset:offset+per_page]) >= len(jobs),
        }}


class AllRequiredCITests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_tests.ReleaseImpactCIAdapterTests(
            "test_valid_event_metadata")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.client = FakeRequiredCI(fixture)
        self.required = [
            {"name": "Source CI", "workflowId": 56789,
             "workflowPath": ".github/workflows/source-ci.yml",
             "requiredJobs": ["offline-tests", "audit-projections"]},
            {"name": "Fortis Release Impact", "workflowId": 45678,
             "workflowPath": ".github/workflows/release-impact.yml",
             "requiredJobs": ["release-impact"]},
        ]

    def collect(self):
        return collect_gateway_snapshot(
            self.client, repository="owner/test-project", run_id=91234,
            pull_request=15, workflow_id=45678,
            workflow_path=".github/workflows/release-impact.yml",
            required_job_names=["release-impact"])

    def check(self, **overrides):
        kw={"snapshot": self.collect(), "required_workflows": self.required}
        kw.update(overrides)
        return collect_all_required_ci(self.client, **kw)

    def test_both_required_workflows_are_admitted_read_only(self):
        result = self.check()
        self.assertEqual(result["status"], "ALL_REQUIRED_CI_ADMITTED_READ_ONLY")
        self.assertEqual(
            [w["name"] for w in result["requiredCI"]],
            ["Fortis Release Impact", "Source CI"])
        self.assertEqual(result["pullRequest"], 15)
        self.assertFalse(result["writeEnabled"])
        self.assertFalse(hasattr(self.client, "add_pr_release_labels"))

    @unittest.skipUnless(HAS_DEPS, "CI installs yaml/jsonschema")
    def test_valid_source_and_both_ci_produce_only_preview(self):
        snap = self.collect()
        result = preview_with_all_required_ci(
            self.client, snapshot=snap, required_workflows=self.required,
            repo_root=self.fixture.repo)
        self.assertEqual(result["status"], "PREVIEW_ONLY")
        self.assertEqual(result["allRequiredCI"],
                         "ALL_REQUIRED_CI_ADMITTED_READ_ONLY")
        self.assertTrue(result["add"])
        self.assertFalse(result["writeEnabled"])

    def test_cannot_omit_source_ci(self):
        with self.assertRaisesRegex(ValidationError, "both Source CI"):
            self.check(required_workflows=self.required[1:])

    def test_duplicate_or_wrong_workflow_config_fails(self):
        self.required[0]["name"] = "Fortis Release Impact"
        with self.assertRaisesRegex(ValidationError, "duplicate mandatory"):
            self.check()
        self.required[0]["name"] = "Source CI"
        self.required[0]["workflowId"] = 123
        with self.assertRaisesRegex(ValidationError, "identity/head/attempt"):
            self.check()

    def test_old_success_cannot_hide_latest_failed_attempt(self):
        self.client.source_run_attempt = 2
        self.client.source["attempt"] = 2
        self.client.source["conclusion"] = "failure"
        with self.assertRaisesRegex(ValidationError,
                                    "latest required CI workflow not successful: Source CI"):
            self.check()

    def test_source_job_queued_despite_green_workflow_fails(self):
        for status, conclusion in (("queued", None), ("in_progress", None),
                                   ("completed", "cancelled")):
            with self.subTest(status=status):
                self.client.source_jobs[1]["status"] = status
                self.client.source_jobs[1]["conclusion"] = conclusion
                with self.assertRaisesRegex(ValidationError, "job not completed"):
                    self.check()

    def test_missing_required_workflow_blocks(self):
        original = self.client.list_workflow_runs
        def only_release(*args, **kwargs):
            page = original(*args, **kwargs)
            page["items"] = [
                x for x in page["items"] if x["name"] != "Source CI"]
            page["totalCount"] = len(page["items"])
            return page
        self.client.list_workflow_runs = only_release
        with self.assertRaisesRegex(ValidationError, "mandatory CI workflow run missing"):
            self.check()

    def test_missing_or_unexpected_source_job_blocks(self):
        self.client.source_jobs.pop()
        with self.assertRaisesRegex(ValidationError, "required GitHub job missing"):
            self.check()
        self.client.source_jobs.append(
            {"id": 77, "name": "untrusted-added-job",
             "status": "completed", "conclusion": "success"})
        with self.assertRaisesRegex(ValidationError, "unexpected GitHub job name"):
            self.check()

    def test_source_ci_wrong_pr_base_blocks(self):
        self.client.source["pullRequests"][0]["baseSha"] = "f" * 40
        with self.assertRaisesRegex(ValidationError,
                                    "linked to different PR/base"):
            self.check()

    def test_source_ci_wrong_path_or_fork_blocks(self):
        self.client.source["workflowPath"] = ".github/workflows/evil.yml"
        with self.assertRaisesRegex(ValidationError, "identity/head/attempt"):
            self.check()
        self.client.source["workflowPath"] = ".github/workflows/source-ci.yml"
        self.client.source["headRepository"] = "other/test-project"
        with self.assertRaisesRegex(ValidationError, "identity/head/attempt"):
            self.check()

    def test_newer_pending_run_blocks_even_if_old_run_passed(self):
        self.client.newer_source = {
            "id": 99999, "runAttempt": 1, "runNumber": 6,
            "name": "Source CI", "event": "pull_request",
            "status": "in_progress", "conclusion": None,
            "headSha": self.fixture.head, "headBranch": "task/release-test",
        }
        with self.assertRaisesRegex(ValidationError,
                                    "latest required CI workflow not successful"):
            self.check()

    def test_newer_run_race_during_gate_stops(self):
        snap = self.collect()
        self.client.newer_source = {
            "id": 99999, "runAttempt": 1, "runNumber": 6,
            "name": "Source CI", "event": "pull_request",
            "status": "completed", "conclusion": "success",
            "headSha": self.fixture.head, "headBranch": "task/release-test",
        }
        # One listing already happened for collect(), first gate listing is
        # call 2. Keep it unseen until gate re-reads run inventory at call 3.
        self.client.newer_after = 3
        with self.assertRaisesRegex(ValidationError,
                                    "new required CI run or rerun appeared"):
            collect_all_required_ci(self.client, snapshot=snap,
                                    required_workflows=self.required)

    def test_duplicate_run_number_denied(self):
        original = self.client.list_workflow_runs
        def duplicate(*args, **kwargs):
            page = original(*args, **kwargs)
            source = copy.deepcopy(page["items"][0])
            source["id"] = 123456
            page["items"].append(source)
            page["totalCount"] += 1
            return page
        self.client.list_workflow_runs = duplicate
        with self.assertRaisesRegex(ValidationError,
                                    "ambiguous duplicate workflow run number"):
            self.check()

    def test_changed_pr_fingerprint_denied(self):
        original = self.client.get_pr_release_label_context
        def changed(repo, n):
            result = original(repo, n)
            # The first contexts occur during the initial snapshot; the next
            # context must not silently accept metadata changed after that.
            if self.client.run_count >= 2:
                result["bodySha256"] = "f" * 64
            return result
        self.client.get_pr_release_label_context = changed
        with self.assertRaisesRegex(ValidationError,
                                    "PR fingerprint changed"):
            self.check()

    def test_changed_pr_branch_denied(self):
        original = self.client.get_pr_release_label_context
        def changed(repo, n):
            result = original(repo, n)
            if self.client.run_count >= 2:
                result["branch"] = "task/other-branch"
            return result
        self.client.get_pr_release_label_context = changed
        with self.assertRaisesRegex(ValidationError,
                                    "PR fingerprint changed"):
            self.check()

    def test_run_inventory_truncated_denied(self):
        snap = self.collect()
        original = self.client.list_workflow_runs
        def excessive(*args, **kwargs):
            page = original(*args, **kwargs)
            page["totalCount"] = 501
            return page
        self.client.list_workflow_runs = excessive
        with self.assertRaisesRegex(ValidationError, "bounded safe limit"):
            collect_all_required_ci(self.client, snapshot=snap,
                                    required_workflows=self.required)


if __name__ == "__main__":
    unittest.main()
