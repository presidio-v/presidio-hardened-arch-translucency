# Contributing to presidio-hardened-arch-translucency

Thanks for your interest. This project is held to a stricter bar than a typical library — the
checklist below is what a change needs to clear before it can be merged.

## Reporting a security vulnerability

**Do not open a public issue for a security vulnerability.** Use the private reporting
process in [SECURITY.md](SECURITY.md) — GitHub Security Advisories, via the repository's
"Security" tab, or contact security@presidio-group.eu. You will get an acknowledgement
within 5 business days.

## Reporting bugs and requesting features

Open a [GitHub issue](https://github.com/presidio-v/presidio-hardened-arch-translucency/issues). Search existing issues first.
For a bug, include:

- the installed version (`pip show presidio_arch_translucency`) and language-runtime version
- what you expected to happen, and what happened instead
- a minimal reproduction if you can produce one

Please strip any secrets, credentials, or personal data from anything you paste into a
public issue.

## New to the project?

Issues labelled [`good first issue`](https://github.com/presidio-v/presidio-hardened-arch-translucency/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
are scoped to be approachable without deep knowledge of the codebase and are a good place to
start.

## How changes are made

All changes go through a pull request against `main`; changes are not landed by direct push.
Every PR must pass the required status checks (CI lint + tests, CodeQL) before it can merge, and
`main` is being placed under branch protection to enforce this and the code-owner review below.

1. Fork the repository and create a branch off `main`.
2. Make your change, with tests (see the test policy below).
3. Run the local verification block until it is clean.
4. Update `CHANGELOG.md` under `## [Unreleased]`.
5. Open a PR describing what changed and why.

## Code review

Every pull request — including a maintainer's own — is reviewed before it merges. The project
requires an approving review from a code owner ([CODEOWNERS](.github/CODEOWNERS)) who is
**not** the author of the change; this is enforced by branch protection on `main` (required
review, code-owner review, stale-approval dismissal, and last-push re-approval; admins are
included) once the two-person review gate is activated.

What a reviewer confirms before approving:

- **Tests** — new or changed functionality ships with tests; bug fixes include a regression
  test; the coverage floors hold.
- **Security reasoning** — for changes to a security-sensitive area (see below), the PR
  explains the reasoning, and no existing default is weakened without an explicit rationale.
- **Compatibility** — changes to the public API surface, event/record shapes, or exception
  types follow [SEMVER.md](SEMVER.md); breaking changes are called out.
- **Style and scope** — the linter is clean, the change is focused, and `CHANGELOG.md` is
  updated.

Reviewers approve via GitHub's review flow. A change that needs rework is returned with
specific requested changes rather than merged with caveats.

## Requirements for acceptable contributions

A change is merged when it meets all of the following.

### Style

Formatting and linting are enforced by the project linter and are not a matter of taste — CI
rejects anything that does not conform.

This project uses **ruff** for both linting and formatting; its configuration
lives in `pyproject.toml` under `[tool.ruff]`: line length 88, target version
`py310`, and the lint rule sets `E`, `F`, `I`, `UP`, `B`, and **`S`** (the bandit
security rules), with `S101` ignored only for test asserts. CI runs
`ruff format --check .` and `ruff check .` and rejects anything that does not
conform.

Each module uses a single consistent import (or include) style. Do not mix conventions for
the same dependency within one module.

### Tests

**Test policy: any change that adds or modifies functionality must ship with tests in the
same pull request.** Bug fixes must include a regression test that fails before the fix and
passes after it. This is enforced in review, and by the coverage gate.

- a coverage floor is enforced in CI: **`--cov-fail-under=80`** with branch coverage
  enabled (`[tool.pytest.ini_options].addopts` and `[tool.coverage.run] branch = true`
  in `pyproject.toml`)

### Security-sensitive changes

This project's security controls are the product. If your change touches any of the
security-sensitive modules, then the reviewer bar above applies in full:

- `scaler.py`, `hpa.py`, `hpa_patch.py` — Kubernetes-manifest emission (the actuation boundary; name validation, no raw-input echo)
- `security.py` — input sanitisation, secure logging, the on-run CVE audit
- `evidence_producer.py` — canonical JSON, hashing, and Ed25519/HMAC signing
- `cloud.py`, `cloud_azure.py`, `cloud_gcp.py` — cloud-credential handling for pricing
- `prometheus.py`, `otlp.py`, `pushgateway.py`, `observe.py` — network ingress/egress and the hash-chained observation store

- explain the security reasoning in the PR description, not only the mechanics
- do not weaken a default. New controls are opt-in; relaxations of existing controls need
  an explicit rationale
- never re-implement cryptographic primitives — call a vetted standard library or crypto
  dependency instead
- functions that produce a stable serialized or digest output are byte-stability contracts.
  Changing their output for existing input is a breaking change even if no signature changes

### Public API and compatibility

The public API surface and what counts as a breaking change are defined in
[SEMVER.md](SEMVER.md). Read it before changing anything exported from the public API, and
note that event/record shapes and exception types are part of the contract that downstream
consumers depend on.

### Dependencies

New runtime dependencies are a high bar for a security-focused library and need justification
in the PR. Prefer the standard library. Optional functionality belongs in an optional
dependency group rather than the core dependency set.

## Local verification

Run this before opening a PR, and fix anything it reports:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m ruff check . \
  && .venv/bin/python -m ruff format --check . \
  && .venv/bin/python -m pytest tests/ -x -q --tb=short
```

These are the project's actual commands (the `[dev]` extra pins `ruff`,
`pytest`, and `pytest-cov`). The coverage floor and multi-version matrix come
from CI, which runs the suite on Python 3.10, 3.11, and 3.12; a change must pass
on all three.

CI runs the test suite across every supported runtime version. A change must pass on all of
them.

## Commit messages

Write in the imperative mood ("add TTL bound", not "added" or "adds"). Explain *why* the
change is being made where that is not obvious from the diff.

## Licensing and Developer Certificate of Origin (DCO)

The project is MIT licensed, and contributions are accepted under the same
terms (inbound = outbound).

To assert that you have the right to submit your contribution, every commit must
be **signed off** under the [Developer Certificate of Origin](https://developercertificate.org/)
1.1. Signing off means adding a `Signed-off-by` line to the commit message with
your real name and email:

```
Signed-off-by: Jane Developer <jane@example.com>
```

`git commit -s` adds this line for you. By signing off you certify the DCO —
in short, that you wrote the change or otherwise have the right to submit it
under the project's MIT license. Pull requests whose commits are not signed off
will be asked to amend before merge.

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
