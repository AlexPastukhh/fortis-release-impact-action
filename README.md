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
