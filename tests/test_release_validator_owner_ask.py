"""Offline one-operation, owner-gated ASK intent tests; never touch GitHub."""
from __future__ import annotations

import copy
import unittest

import test_release_validator_ci as fixture_tests
from test_release_validator_ci import HAS_DEPS
from test_release_validator_required_ci import FakeRequiredCI
from owner_ask_label_v1 import (
    _expect_ask_policy, _selected_labels,
    prepare_owner_label_ask, submit_one_owner_label_ask,
)
from validate import ValidationError


class CapabilitiesReader:
    def __init__(self):
        self.policy = "ASK"
        self.publisher = "NOT_ACTIVE"
        self.calls = []

    def post(self, route, payload):
        self.calls.append((route, payload))
        assert route == "/v1/workspace/capabilities" and payload == {}
        return {"githubReleaseLabelWrites": {
            "policy": self.policy,
            "commands": ["add_pr_release_labels", "remove_pr_release_label",
                         "create_release_label"],
            "scope": "registered-open-same-repo-task-PR-only",
            "unknownEffect": "STOP",
            "automaticPublisher": self.publisher,
        }}


class FakeOwnerAskGateway(FakeRequiredCI):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.gateway = CapabilitiesReader()
        self.ask_calls = []
        self.reply_state = "PENDING"

    def add_pr_release_labels(self, repo, number, head, base, body_sha,
                              labels_sha, labels, *, request_id, reason):
        self.ask_calls.append(("add", repo, number, head, base,
                               body_sha, labels_sha, labels, request_id, reason))
        return {"requestId": request_id, "state": self.reply_state}

    def remove_pr_release_label(self, repo, number, head, base, body_sha,
                                labels_sha, name, *, request_id, reason):
        self.ask_calls.append(("remove", repo, number, head, base,
                               body_sha, labels_sha, name, request_id, reason))
        return {"requestId": request_id, "state": self.reply_state}


class OwnerAskTests(unittest.TestCase):
    def setUp(self):
        fixture = fixture_tests.ReleaseImpactCIAdapterTests(
            "test_valid_event_metadata")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.client = FakeOwnerAskGateway(fixture)
        self.arguments = {
            "repository": "owner/test-project", "run_id": 91234,
            "pull_request": 15, "workflow_id": 45678,
            "workflow_path": ".github/workflows/release-impact.yml",
            "required_job_names": ["release-impact"],
            "required_workflows": [
                {"name": "Source CI", "workflowId": 56789,
                 "workflowPath": ".github/workflows/source-ci.yml",
                 "requiredJobs": ["offline-tests", "audit-projections"]},
                {"name": "Fortis Release Impact", "workflowId": 45678,
                 "workflowPath": ".github/workflows/release-impact.yml",
                 "requiredJobs": ["release-impact"]},
            ],
            "repo_root": self.fixture.repo,
            "mode": "add",
            "selected_labels": ["build:api"],
        }
        self.uuid = "6e2b01d7-0dfd-4b29-a5aa-df6d0a58c1cf"

    def prepare(self, **kwargs):
        return prepare_owner_label_ask(self.client,
                                       **{**self.arguments, **kwargs})

    def submit(self, **kwargs):
        params = {"request_id": self.uuid,
                  "reason": "Owner-requested reviewed single ASK preview",
                  "operator_confirmed": True,
                  **self.arguments}
        params.update(kwargs)
        return submit_one_owner_label_ask(self.client, **params)

    def test_only_computed_managed_labels_may_be_selected(self):
        plan = {"add": ["build:api", "release:image-rebuild"],
                "remove": ["build:old", "release:review-required"]}
        self.assertEqual(_selected_labels("add", ["build:api"], plan),
                         ("add_pr_release_labels", {"labels": ["build:api"]}))
        self.assertEqual(_selected_labels("remove", ["build:old"], plan),
                         ("remove_pr_release_label", {"name": "build:old"}))
        for mode, names in (
            ("add", ["release:review-required"]),
            ("remove", ["release:review-required"]),
            ("add", ["release:no-deployment"]),
            ("add", ["human:bug"]),
            ("add", ["build:BAD"]),
            ("add", ["build:api"]*2),
            ("add", ["build:api"]*6),
            ("remove", ["build:old", "build:api"]),
            ("unknown", ["build:api"]),
            ("add", [])):
            with self.subTest(mode=mode, names=names):
                with self.assertRaises(ValidationError):
                    _selected_labels(mode, names, plan)

    def test_gateway_must_keep_ask_and_publisher_inactive(self):
        _expect_ask_policy(self.client)
        self.client.gateway.policy = "ALLOW"
        with self.assertRaisesRegex(ValidationError, "policy"):
            _expect_ask_policy(self.client)
        self.client.gateway.policy = "ASK"
        self.client.gateway.publisher = "ACTIVE"
        with self.assertRaisesRegex(ValidationError, "policy"):
            _expect_ask_policy(self.client)

    def test_request_must_be_explicit_canonical_uuid(self):
        with self.assertRaisesRegex(ValidationError, "explicit operator"):
            self.submit(operator_confirmed=False)
        with self.assertRaisesRegex(ValidationError, "canonical"):
            self.submit(request_id="bad-uuid")
        with self.assertRaisesRegex(ValidationError, "auditable"):
            self.submit(reason="")

    def test_denied_gateway_policy_cannot_submit(self):
        self.client.gateway.policy = "ALLOW"
        with self.assertRaisesRegex(ValidationError, "policy"):
            self.submit()
        self.assertFalse(self.client.ask_calls)

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_prepares_only_one_ask_without_mutations(self):
        proposal = self.prepare()
        self.assertEqual(proposal["status"], "PREPARED_OWNER_ASK_ONLY")
        self.assertEqual(proposal["operation"], "add_pr_release_labels")
        self.assertEqual(proposal["params"]["labels"], ["build:api"])
        self.assertEqual(proposal["params"]["expectedHeadSha"],
                         self.fixture.head)
        self.assertEqual(len(proposal["params"]["expectedLabelsSha256"]), 64)
        self.assertEqual(len(proposal["requiredCI"]), 2)
        self.assertTrue(proposal["ownerApprovalRequired"])
        self.assertIs(proposal["writeEnabled"], False)
        self.assertIs(proposal["autoSubmit"], False)
        self.assertEqual(self.client.ask_calls, [])

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_submit_one_owner_ask_returns_pending_not_written(self):
        result = self.submit()
        self.assertEqual(result["requestId"], self.uuid)
        self.assertEqual(result["status"], "PENDING_OWNER_APPROVAL")
        self.assertFalse(result["writeConfirmed"])
        self.assertFalse(result["automaticRetryAllowed"])
        self.assertEqual(len(self.client.ask_calls), 1)
        sent = self.client.ask_calls[0]
        self.assertEqual((sent[0], sent[1], sent[2]),
                         ("add", "test-project", 15))
        self.assertEqual(sent[7], ["build:api"])
        self.assertEqual(sent[8], self.uuid)

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_gateway_nonpending_or_unknown_state_stops_no_retry(self):
        self.client.reply_state = "UNCERTAIN"
        result = self.submit()
        self.assertEqual(result["status"], "STOP_INSPECT_ORIGINAL_REQUEST")
        self.assertEqual(result["gatewayState"], "UNCERTAIN")
        self.assertFalse(result["automaticRetryAllowed"])
        self.assertEqual(len(self.client.ask_calls), 1)

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_removal_is_one_managed_label_never_review_flag(self):
        proposal = self.prepare(mode="remove", selected_labels=["build:old"])
        self.assertEqual(proposal["operation"], "remove_pr_release_label")
        self.assertEqual(proposal["params"]["name"], "build:old")
        result = self.submit(mode="remove", selected_labels=["build:old"])
        self.assertEqual(result["status"], "PENDING_OWNER_APPROVAL")
        self.assertEqual(len(self.client.ask_calls), 1)
        self.assertEqual(self.client.ask_calls[0][0], "remove")

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_missing_definitions_stop_before_ask(self):
        self.client.definitions.remove("build:api")
        with self.assertRaisesRegex(ValidationError, "missing definitions"):
            self.submit()
        self.assertEqual(self.client.ask_calls, [])

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_stale_label_context_stops_before_ask(self):
        original = self.client.get_pr_release_label_context
        count = 0
        def stale(repo, n):
            nonlocal count
            count += 1
            row = original(repo, n)
            if count >= 4:
                row["labelsSha256"] = "f" * 64
                row["labels"].append("release:no-deployment")
            return row
        self.client.get_pr_release_label_context = stale
        with self.assertRaises(ValidationError):
            self.submit()
        self.assertEqual(self.client.ask_calls, [])

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_failed_source_ci_stops_before_ask(self):
        self.client.source["conclusion"] = "failure"
        with self.assertRaisesRegex(
                ValidationError, "latest required CI workflow not successful"):
            self.submit()
        self.assertEqual(self.client.ask_calls, [])

    @unittest.skipUnless(HAS_DEPS, "CI installs YAML/jsonschema")
    def test_untrusted_label_selection_stops(self):
        with self.assertRaisesRegex(
                ValidationError, "selected labels not in"):
            self.submit(selected_labels=["release:no-deployment"])
        self.assertEqual(self.client.ask_calls, [])


if __name__ == "__main__":
    unittest.main()
