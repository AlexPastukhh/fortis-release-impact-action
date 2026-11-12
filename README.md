# Fortis Release Impact Action

A standalone, **public and read-only** GitHub composite action to validate the
machine-readable release impact of pull requests against the repository's
`release/targets.yaml` catalog. It runs no deployment operations and does not
modify PRs, labels, GitHub Projects, environments or repository content.

This repository publishes the reusable **validator implementation**. The
normative [Fortis release planning contract](https://github.com/AlexPastukhh/fortis-container/blob/main/docs/RELEASE-PLANNING-CONTRACT.md)
and cross-repository Use Cases remain with `fortis-container`. The included
version-1 JSON schemas are immutable execution snapshots of that contract, not
a separate policy authority. Change them only in a separately reviewed,
test-covered release process; do not independently fork the business rules.

## Caller workflow

Use an **immutable reviewed commit SHA** of this action, not `@main`, in each
participating project's `.github/workflows/release-impact.yml`:

```yaml
name: Fortis Release Impact
on:
  pull_request:
    branches: [main]
    types: [opened, synchronize, reopened, edited, ready_for_review]
permissions:
  contents: read
jobs:
  release-impact:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - uses: AlexPastukhh/fortis-release-impact-action@<PINNED_40_CHARACTER_COMMIT_SHA>
```

The existing project-specific CI stays in place. This action validates only
open non-draft PRs **from the same repository to `main`** and checks out
the exact event PR HEAD. It rejects forks, missing metadata, stale SHAs,
unclassified changed files and unknown release-impact classifications.
It compares the full merge-base diff with `release/targets.yaml`, without
interpreting git rename detection as proof that a runtime source file vanished.

The PR body must contain exactly one `releaseImpact` YAML object between
`<!-- fortis-release-impact:start -->` and
`<!-- fortis-release-impact:end -->`, according to the v1 schemas.

## Developer tests

```sh
python -m pip install -r tools/release/requirements.txt
python -m unittest discover -s tests -p 'test_release_validator*.py' -v
```

The tests create temporary local Git fixtures and require no GitHub token or
network calls. `action.yml` is the single public entry point. Source code
was initially separated from the reviewed `fortis-container` action at
`6bde4c84b9625719bc7f87b1397758636d6c85c2`. During migration,
the legacy private action is a pinned historical compatibility version;
it must not be independently maintained as a second current validator.

This public code is intentionally limited to the generic validator, two
versioned schemas, minimal dependencies and offline tests. It contains no
Fortis Gateway credentials, private deployment controllers or owner routes.


## Label projection (source candidate; **no automation installed**)

The release CI adapter now also emits `suggestedLabels` from the **same
semantically validated** PR HEAD, changed file set and catalog. The one
versioned pure implementation is `tools/release/labels_v1.py`:

- `release:no-deployment` when the entire PR is explicitly classified as
  safe to accept without a runtime update.
- `build:<target-id>` for **every declared affected target** in deployment PRs.
- `release:image-rebuild`, `release:config-deploy`, `release:windows-update`,
  `release:package-build`, `release:migration-required` as declared.
- `release:review-required` if an explicitly declared target action is
  `unknown`. A wholly `unknown` PR fails Release Impact CI instead.

The projection never writes labels and does not imply a release is approved,
installed or deployed. Its output is an unprivileged, **non-authoritative
planning hint** until an independently trusted publisher revalidates the
current PR HEAD and writes labels with restricted permissions.

Do not turn the current `pull_request` job into a privileged job, and never
treat an artifact or log from a PR-controlled workflow as permission to
mutate GitHub. A follow-up owner-reviewed publisher must run only trusted
code, avoid executing PR source, enforce same-repository exact SHA and
revalidate source/catalog semantics before changing managed `build:` and
`release:` labels. Do not accidentally erase unrelated human labels.


## Trusted label admission and reconciliation (source-only preview)

`tools/release/label_admission_v1.py` is an **offline, pure-function,
no-write proposal** for the next stage, not an active label publisher.
It is intentionally separate from the unprivileged PR validation job.
It assumes a future trusted caller has independently fetched:

1. the completed successful `pull_request` workflow run via GitHub's
   authenticated API, and resolved the allowed workflow ID from reviewed
   default-branch configuration (not from PR source); **verify the exact
   `run_attempt` from both the webhook and a fresh API read**;
2. **the complete paginated job inventory from the same authenticated run
   and attempt**, with a trusted list of required job names from reviewed
   default-branch configuration. Reject any missing, duplicate, queued,
   running, cancelled, skipped or failed job, even when the overall workflow
   claims `completed/success`. PR #73 revealed a real workflow/job status
   discrepancy, so the workflow summary alone is not sufficient admission.
   The caller must construct `job_inventory = {run_id, run_attempt,
   complete: true, jobs: [{id, name, status, conclusion}, ...]}` only after
   fetching every page; `complete` must not be inferred from partial data;
3. the exact current PR object via GitHub's authenticated API, including
   head/base SHAs, repository identities, open/non-draft state and bounded
   body; the workflow-run event must link exactly one matching PR;
4. the checked-out exact PR HEAD and full Git merge-base diff, and has
   **recomputed** the Release Impact under the pinned trusted validator,
   never using an artifact or log from a PR-controlled job as authority;
5. the complete existing issue/PR labels and repository label definitions.

The admission module rejects failed/forked/unknown/stale runs, multiple or
missing PR links, mismatched SHA/base/body and unvalidated evidence. It
computes `add` and `remove` lists **without applying them**. Only reserved
`build:` and enumerated `release:` labels are managed; all unrelated labels
are preserved. An existing `release:review-required` is sticky and can be
cleared only by owner review. Undefined repository labels block publication.

A future writer must request fresh PR and label snapshots **before each
mutation**, make bounded idempotent GitHub calls via the agreed Fortis
authorization route, and independently read back the result. GitHub's label
REST API does not supply an atomic PR-HEAD compare-and-swap operation:
therefore racing updates, ambiguous writes or missing permissions are
`STOP` conditions, not automatic retries or release approval.

### Trusted label preview — source-only integration

`tools/release/trusted_label_preview_v1.py` joins those pure checks into a
**no-write** preview: independently read PR/run/job records and the *latest*
run attempt, admit only successful complete jobs, construct a minimal PR event,
re-run the pinned trusted validator over an exact-HEAD **data-only** checkout
with complete Git history, then produce minimal add/remove label deltas.

The caller must fetch GitHub records through an authenticated, reviewed
read-only API client and pin the expected workflow ID and job names from the
**trusted default branch**. A prior successful attempt is insufficient if a
newer rerun fails. The checkout must contain data only: do not execute any
PR scripts, dependencies, workflows, artifacts or uploaded code, especially
not in a privileged `workflow_run` context. The preview itself has **no
GitHub auth, no external calls, no secret handling and no mutation**.
`writeEnabled: false` is an explicit non-authorization marker.

### Fortis Gateway read-only snapshot collector (source-only)

`tools/release/gateway_snapshot_v1.py` is a **no-write** adapter for an
injected, already-authenticated **typed Fortis Gateway** GitHub client. It
collects the latest run attempt from a bounded, fully paginated workflow-run
inventory (20/page; incomplete inventories STOP), independently reads the
exact-attempt workflow and complete CI jobs, the open PR's body/provenance
from `get_pr_release_impact_evidence`, the full label context, PR metadata
and paginated repository/PR label definitions. Cross-checks bind their
repository, PR number, run attempt, HEAD/base SHA and body SHA-256. It then
builds a synthetic event from authenticated records — **not a claim of an
independently received GitHub webhook**.

`collect_gateway_snapshot(...)` returns data only, with
`snapshotOnly: true`. `preview_gateway_snapshot(snapshot, repo_root=...)`
passes these inputs to the previous trusted validator, which requires a
separate **exact PR HEAD data-only checkout with full Git history**. This
module never fetches Git objects or executes checkout scripts on its own.
The caller must pin expected workflow ID/path and job names from reviewed
configuration, not PR source or an artifact.

### All-required-CI admission (source-only, read-only)

`tools/release/required_ci_v1.py` now adds a separate **mandatory two-gate
preflight for the initial fortis-approvals pilot**. It requires the
`Source CI` and `Fortis Release Impact` workflow identities to be pinned
by reviewed default-branch configuration: workflow ID, workflow path and
the exact required job names. The PR, its changed workflow files and
artifacts must not supply or reduce this list.

The collector reads the **complete bounded run history** through the
authenticated typed Gateway, selects the highest run number for each
required workflow on the exact PR HEAD, re-reads each latest attempt and
all its jobs, and checks same-repository, run ID, workflow identity,
SHA/branch/base, PR linkage and `completed/success` for **every job**.
It rechecks PR body/label fingerprints and the latest workflow attempts.
A newer failed rerun supersedes an older successful attempt; missing,
queued, incomplete, duplicate or unexpectedly named CI jobs deny admission.
If the bounded listing is truncated, it stops rather than claiming success.

`collect_all_required_ci(...)` reports only
`ALL_REQUIRED_CI_ADMITTED_READ_ONLY` with `writeEnabled: false`.
`preview_with_all_required_ci(...)` then invokes the already trusted
exact-HEAD source/catalog validator to return a no-write label proposal.
The caller still has to provide a separate full-history, data-only
exact-HEAD checkout. No GitHub workflow, API writer or new permission is
installed by merging this module.

**Real fail-closed case:** fortis-approvals PR #73 had a green Release
Impact run but Source CI attempt 2 concluded failure, with its
`audit-projections` job still queued. A previous Source CI success
must NEVER authorize labels for that PR.

### Bounded Gateway owner-ASK intent (source-only, not active)

`tools/release/owner_ask_label_v1.py` defines a **two-stage, inert-by-default**
boundary: `prepare_owner_label_ask(...)` performs only authenticated Gateway
READs, recomputes the release plan from **full-history exact-PR-HEAD
data-only Git objects**, admits both reviewed mandatory CI workflows and
their latest job attempts, and then reads fresh PR/label fingerprints.
It builds **one** narrow `add_pr_release_labels` intent (1–5
explicitly selected calculated labels) or **one**
`remove_pr_release_label` intent (one selected calculated stale label).
Arbitrary PR-controlled additions, unknown build labels,
`release:review-required` removal, unapproved label definitions, stale
metadata or partial/failed Source CI are rejected. The returned record is
`PREPARED_OWNER_ASK_ONLY`, never proof of a GitHub write.

`submit_one_owner_label_ask(...)` is an **explicit, optional trusted-caller
function**: it requires an independently generated **stable canonical UUID
recorded before the call**, an auditable reason, an explicit operator intent,
and authenticated live Gateway capabilities confirming policy **ASK**,
`unknownEffect: STOP`, `automaticPublisher: NOT_ACTIVE` and the
registered open same-repository task-PR scope. It recomputes everything
immediately before sending **at most one** existing typed Gateway
`add_pr_release_labels` or `remove_pr_release_label` request. It
does not approve the request, execute GitHub REST directly, poll, issue a
second request, generate a fresh UUID after a failure, or assert success.
A `PENDING` response means **owner WebAuthn approval is still needed**.
Any unexpected state or uncertain transport outcome is **STOP**: inspect the
**original UUID** using Gateway status and durable receipts, never blind
retry. Even a Gateway `EXECUTED` state requires independent exact PR/labels
readback before claiming a label has been published.

**This module is not connected to a workflow or agent trigger**, is not
permitted to create autonomous approvals, and has no built-in credential
or GitHub API client. Do **not** invoke its submission function from the
unprivileged `pull_request` CI or a write-capable `workflow_run` without
a separate reviewed principal, runner isolation, owner-controlled policy,
durable caller journal and live pilot. The existing provider write path
still has no atomic PR-SHA CAS: races or unknown effects never authorize
another operation or a deployment. Source merge by itself changes neither
the production Gateway nor the active permissions.

This is a **read-only planning preflight, not authorization to publish**.
A future writer must separately pass owner-reviewed Gateway ASK/receipt
admission, independently re-evaluate every required CI gate, use fresh PR
and labels fingerprints before every bounded write, and STOP on any race,
timeout, unknown effect or missing provider access.

The next separate security gate is a trusted default-branch caller, explicit
read-only GitHub permissions and exact-HEAD pilot, followed by an independently
reviewed Gateway-based bounded ASK/receipt writer with fresh PR/label
fingerprints for **each** operation. Never grant the untrusted PR workflow
GitHub-write or owner credentials.

**Not implemented here:** a GitHub API client, token handling, a
`workflow_run`/privileged workflow, Gateway label grants, live label writes,
recovery/reconciliation, GitHub Projects auto-add or any deployment. Do not
wire this offline module to a write-capable GitHub workflow before a separate
owner-approved security review, explicit provider access checks, negative
tests and exact-HEAD pilot. GitHub's current secure-use guidance cautions
against processing untrusted code and artifacts in privileged event contexts.
