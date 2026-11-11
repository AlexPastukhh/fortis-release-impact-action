"""No-network security tests for a prospective trusted label publisher."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/release"))
from label_admission_v1 import (
    ValidationError, admit_successful_workflow_run, check_fresh_pr, plan_labels,
)

REPO = "AlexPastukhh/tunnel"
HEAD = "a" * 40
BASE = "b" * 40
BODY = "<!-- fortis-release-impact:start -->\n..."
WORKFLOW_ID = 123456


def run_record():
    return {
        "id": 789123, "workflow_id": WORKFLOW_ID,
        "status": "completed", "conclusion": "success",
        "event": "pull_request", "head_sha": HEAD,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "pull_requests": [{"number": 23}],
    }


def pull_request():
    return {
        "number": 23, "state": "open", "merged": False,
        "merged_at": None, "draft": False, "body": BODY,
        "head": {"sha": HEAD, "repo": {"full_name": REPO}},
        "base": {"sha": BASE, "ref": "main", "repo": {"full_name": REPO}},
    }


def impact():
    return {
        "status": "PASS", "repository": REPO,
        "pullRequest": 23, "headSha": HEAD,
        "classification": "deployment", "affectedTargets": ["api"],
        "suggestedLabels": ["build:api", "release:image-rebuild"],
    }


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.trusted = run_record()
        self.event = {"action": "completed", "workflow_run": copy.deepcopy(self.trusted)}
        self.pr = pull_request()

    def admit(self):
        return admit_successful_workflow_run(
            self.event, self.trusted, self.pr,
            repository=REPO, workflow_id=WORKFLOW_ID)

    def test_valid_admission_records_exact_head_and_pr(self):
        a = self.admit()
        self.assertEqual((a["repository"], a["pullRequest"], a["headSha"],
                          a["baseSha"], a["workflowRun"]),
                         (REPO, 23, HEAD, BASE, 789123))
        self.assertTrue(check_fresh_pr(a, self.pr))

    def test_failed_or_pending_run_cannot_trigger(self):
        for status, conclusion in [("in_progress", None), ("completed", "failure"),
                                   ("completed", "cancelled")]:
            with self.subTest(status=status, conclusion=conclusion):
                self.trusted["status"], self.trusted["conclusion"] = status, conclusion
                with self.assertRaisesRegex(ValidationError, "successful"):
                    self.admit()

    def test_cannot_pretend_push_run_was_pr(self):
        self.trusted["event"] = "push"
        with self.assertRaisesRegex(ValidationError, "pull_request"):
            self.admit()

    def test_wrong_workflow_id_rejected(self):
        self.trusted["workflow_id"] = WORKFLOW_ID + 1
        with self.assertRaisesRegex(ValidationError, "workflow ID"):
            self.admit()

    def test_wrong_webhook_run_id_rejected(self):
        self.event["workflow_run"]["id"] = 789124
        with self.assertRaisesRegex(ValidationError, "run ID mismatch"):
            self.admit()

    def test_untrusted_webhook_cannot_invent_success(self):
        self.trusted["conclusion"] = "failure"
        with self.assertRaisesRegex(ValidationError, "successful"):
            self.admit()

    def test_event_head_sha_mismatch_rejected(self):
        self.event["workflow_run"]["head_sha"] = "c"*40
        with self.assertRaisesRegex(ValidationError, "HEAD differs"):
            self.admit()

    def test_stale_pr_head_rejected(self):
        self.pr["head"]["sha"] = "c"*40
        with self.assertRaisesRegex(ValidationError, "stale PR HEAD"):
            self.admit()

    def test_draft_and_closed_or_merged_rejected(self):
        for changes in ({"draft": True}, {"state": "closed"},
                        {"merged": True}, {"merged_at": "2026-11-11T00:00:00Z"}):
            with self.subTest(changes=changes):
                pr = copy.deepcopy(self.pr)
                pr.update(changes)
                self.pr = pr
                with self.assertRaisesRegex(ValidationError, "open, unmerged"):
                    self.admit()
                self.pr = pull_request()

    def test_fork_head_or_run_head_repo_rejected(self):
        self.pr["head"]["repo"]["full_name"] = "Other/tunnel"
        with self.assertRaisesRegex(ValidationError, "cross-repository"):
            self.admit()
        self.pr = pull_request()
        self.trusted["head_repository"]["full_name"] = "Other/tunnel"
        with self.assertRaisesRegex(ValidationError, "fork/cross-repo"):
            self.admit()

    def test_non_main_target_rejected(self):
        self.pr["base"]["ref"] = "other"
        with self.assertRaisesRegex(ValidationError, "not main"):
            self.admit()

    def test_missing_or_multiple_pr_linkage_rejected(self):
        for links in ([], [{"number": 23}, {"number": 42}]):
            self.trusted["pull_requests"] = links
            with self.assertRaisesRegex(ValidationError, "exactly one"):
                self.admit()

    def test_linkage_mismatch_rejected(self):
        self.trusted["pull_requests"][0]["number"] = 42
        with self.assertRaisesRegex(ValidationError, "PR linkage"):
            self.admit()

    def test_webhook_run_id_must_be_positive_not_bool(self):
        self.event["workflow_run"]["id"] = True
        with self.assertRaisesRegex(ValidationError, "positive integer"):
            self.admit()

    def test_second_read_detects_metadata_races(self):
        a = self.admit()
        for mutation in ("sha", "base", "body"):
            with self.subTest(mutation=mutation):
                fresh = copy.deepcopy(self.pr)
                if mutation == "sha":
                    fresh["head"]["sha"] = "c"*40
                elif mutation == "base":
                    fresh["base"]["sha"] = "d"*40
                else:
                    fresh["body"] += " edit"
                with self.assertRaisesRegex(ValidationError, "changed"):
                    check_fresh_pr(a, fresh)

    def test_label_plan_minimal_delta_and_preserves_human(self):
        a = self.admit()
        plan = plan_labels(
            a, impact(), self.pr,
            ["bug", "build:legacy", "release:no-deployment",
             "release:review-required", "release:unknown-team-label"],
            ["build:api", "release:image-rebuild"])
        self.assertEqual(plan["status"], "PREVIEW_ONLY")
        self.assertEqual(plan["add"], ["build:api", "release:image-rebuild"])
        self.assertEqual(plan["remove"], ["build:legacy", "release:no-deployment"])
        self.assertEqual(plan["preservedHumanLabels"],
                         ["bug", "release:unknown-team-label"])
        self.assertTrue(plan["preservedReviewFlag"])

    def test_repeat_label_plan_is_idempotent(self):
        a = self.admit()
        plan = plan_labels(a, impact(), self.pr,
                           ["build:api", "release:image-rebuild"],
                           ["build:api", "release:image-rebuild"])
        self.assertEqual((plan["add"], plan["remove"]), ([], []))

    def test_missing_label_definitions_block_future_publisher(self):
        a = self.admit()
        plan = plan_labels(a, impact(), self.pr, [],
                           ["release:image-rebuild"])
        self.assertEqual(plan["status"], "BLOCKED_MISSING_LABELS")
        self.assertEqual(plan["missingDefinitions"], ["build:api"])

    def test_no_deployment_must_be_only_label(self):
        a = self.admit()
        safe = impact()
        safe.update(classification="no-deployment", affectedTargets=[],
                    suggestedLabels=["release:no-deployment"])
        plan = plan_labels(a, safe, self.pr, ["bug", "build:old"],
                           ["release:no-deployment"])
        self.assertEqual(plan["add"], ["release:no-deployment"])
        self.assertEqual(plan["remove"], ["build:old"])
        safe["suggestedLabels"].append("build:api")
        with self.assertRaisesRegex(ValidationError, "invalid no-deployment"):
            plan_labels(a, safe, self.pr, [], ["release:no-deployment", "build:api"])

    def test_cannot_accept_forged_validator_evidence(self):
        a = self.admit()
        for key, value in (("status", "FAIL"), ("headSha", "c"*40),
                           ("repository", "Other/tunnel"), ("pullRequest", 24)):
            with self.subTest(key=key):
                result = impact()
                result[key] = value
                with self.assertRaisesRegex(ValidationError, "validated evidence"):
                    plan_labels(a, result, self.pr, [], ["build:api", "release:image-rebuild"])

    def test_unknown_label_names_cannot_be_generated(self):
        a = self.admit()
        data = impact()
        data["suggestedLabels"].append("admin:approved")
        with self.assertRaisesRegex(ValidationError, "unapproved"):
            plan_labels(a, data, self.pr, [], data["suggestedLabels"])

    def test_conflicting_no_deployment_and_deployment_rejected(self):
        a = self.admit()
        data = impact()
        data["suggestedLabels"].append("release:no-deployment")
        with self.assertRaisesRegex(ValidationError, "cannot carry"):
            plan_labels(a, data, self.pr, [], data["suggestedLabels"])

    def test_missing_affected_target_label_rejected(self):
        a = self.admit()
        data = impact()
        data["affectedTargets"] = ["worker"]
        with self.assertRaisesRegex(ValidationError, "affected target omitted"):
            plan_labels(a, data, self.pr, [], data["suggestedLabels"])

    def test_oversized_input_and_duplicate_labels_rejected(self):
        a = self.admit()
        with self.assertRaisesRegex(ValidationError, "duplicate label"):
            plan_labels(a, impact(), self.pr, ["bug", "bug"],
                        ["build:api", "release:image-rebuild"])
        with self.assertRaisesRegex(ValidationError, "invalid label"):
            plan_labels(a, impact(), self.pr, ["Z"*51],
                        ["build:api", "release:image-rebuild"])

    def test_owner_review_flag_is_never_auto_removed(self):
        a = self.admit()
        plan = plan_labels(a, impact(), self.pr, ["release:review-required"],
                           ["build:api", "release:image-rebuild"])
        self.assertEqual(plan["remove"], [])
        self.assertTrue(plan["preservedReviewFlag"])


if __name__ == "__main__":
    unittest.main()
