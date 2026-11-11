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
