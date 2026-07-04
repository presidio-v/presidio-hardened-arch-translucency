# v0.18.0 Third-Party Audit — Remediation Status

**Date:** 2026-07-02
**Audit:** `presidio-third-party-audits/arch-translucency-third-party-security-audit-v0-18-0.md`
(Codex, release-gate review; verdict at audit time: NOT APPROVED FOR RELEASE)
**Remediation:** same day, this repo. Release decision remains with the founder
(delta audit recommended before tagging, per the audit's conclusion).

## Finding-by-finding status

| # | Finding | Severity | Status | Remediation |
|---|---|---|---|---|
| 1 | v0.18.0 artifacts ship as v0.17.0 | P1 | **Fixed** | `pyproject.toml` + `__init__.__version__` → `0.18.0`; CHANGELOG promoted from `[Unreleased]` to `[0.18.0] - 2026-07-02`. `pat --version` reports 0.18.0; `build_training_run_reading` emits `source_version: 0.18.0`. Artifacts must be regenerated at release (`uv build`) — auditor's three-command check applies. |
| 2 | Training numeric inputs accept `nan`/`inf` | P1 | **Fixed** | `security.sanitize_bounded_number` (finite + bounded, rejects `bool`) applied to every float option of `train-analyze` / `train-what-if` / `train-evidence-emit`; library-level `_require_positive_finite` / `_require_degree` guards in `training.py` (`TrainingDomainError`) protect API callers independently of the CLI. Adversarial probes now exit 2 (analyze/what-if) / 1 (evidence-emit) with a one-line error, no traceback. Regression tests added (`nan`, `inf`, `-inf`, zero, negative). |
| 3 | `training-run@1` library validation incomplete | P1 | **Fixed** | `build_training_run_reading` now enforces the full contract: strategy ∈ {data, fsdp, tensor, pipeline} (pinned by test to `training.VALID_STRATEGIES`); `run_id` non-blank, ≤512 chars, no control characters; all numerics coerced fail-closed (`TypeError`/`ValueError`/`OverflowError` → `EvidenceProducerError`; non-integral floats rejected — no silent truncation; `bool` rejected); `degree`/`device_count` ≥ 1, counters ≥ 0. Security log carries `run_id_sha256_16` digest, never the raw `run_id`. Library- and CLI-level regression tests added. |
| 4 | `train-what-if` ignores `max_degree` | P2 | **Fixed** | `evaluate_strategy` rejects `degree` outside `[1, max_degree]` (library-level guard, per the auditor's preference); CLI maps it to exit 2. Probe `--strategy pipeline --degree 999` now fails closed. |
| 5 | CI `pip-audit` advisory, not a release gate | P2 | **Fixed** | `ci.yml`: `continue-on-error: ${{ github.event_name == 'pull_request' }}` — blocking on push (main + release tags), advisory on contributor PRs. Matches the SECURITY.md release-gate description. |
| 6 | Documentation still v0.17-focused | P3 | **Fixed** | README: headline → v0.18.0, new "ML training parallelism" section (all three commands + evidence/parents), roadmap row added. SECURITY.md: supported versions → main/0.18.x, `training-run@1` trust-boundary subsection, audit-history link. PRESIDIO-REQ: Training Arc section + roadmap row updated with audit/remediation status. |

## Verification after remediation

- `ruff check .` — passed; `ruff format --check` — passed.
- Full test suite: **779 passed, 7 skipped** (14 new audit-regression tests in
  `tests/test_training.py`).
- Adversarial re-probes (all fail-closed, no tracebacks):
  - `train-analyze -s nan|inf` → exit 2
  - `train-what-if --strategy pipeline --degree 999` → exit 2
  - `train-evidence-emit -s inf` → exit 1, no JSON emitted
  - `train-evidence-emit --run-id $'bad\nrun'` → exit 1, no JSON emitted
- `pat --version` → `0.18.0`.

## Out of scope of this remediation (recorded)

- **Artifact regeneration + wheel smoke** (`uv build`, fresh-venv install,
  metadata + `source_version` check) — release-time step, founder-run with the
  delta audit.
- **presidio-evidence conformance** — the audit reviewed the *vendored*
  evidence surface in this repo only, not the `presidio-evidence` project.
  The parents-bearing `training-run@1` golden vector and the
  `workshop-attestation@1` vector are owed by `presidio-evidence` (gated on
  its Rust lane's first green run or founder waiver) and should be covered by
  a separate short conformance audit once appended.
