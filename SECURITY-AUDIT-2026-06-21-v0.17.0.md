# Third-Party Security Audit Remediation — v0.17.0

Date: 2026-06-21

Source report: `/Users/vstantch/projects/presidio-third-party-audits/third-party-security-audit-presidio-hardened-arch-translucency-20260621-204853.md`

Scope: `presidio-hardened-arch-translucency` v0.17.0 evidence arc (`evidence_producer`, `pat evidence-emit`, optional `[evidence]` extra, docs, tests, and release packaging).

Verdict from third-party report: low risk / approve for release. No Critical or High findings. Observations were low or informational.

## Findings Assessed

| Severity | Finding | Assessment | Remediation |
|---|---|---|---|
| Low | New optional `cryptography` dependency requires supply-chain attention | Valid. The evidence extra was optional and could be missed by the CI audit job. | Bounded the evidence extra to `cryptography>=49.0.0,<50.0.0`, kept it lock-pinned in `uv.lock`, and changed CI `security-audit` to install `.[audit,evidence]` before `pip-audit`. |
| Low | Evidence emission trusts the local observation store | Accepted as a design boundary, not a code vulnerability. The sidecar signs what the local host presents, so the host and store permissions are the trust boundary. | Documented the v0.17 evidence trust boundary in `SECURITY.md` and added a CLI regression that `pat evidence-emit` reads the private default store while preserving `~/.pat` `0700` and `observations.db` `0600`. |
| Info | Ed25519 key format should be clarified | Valid. The docs said lowercase hex, but `bytes.fromhex()` would accept uppercase and whitespace. | Enforced exactly 64 lowercase hex characters representing a raw 32-byte Ed25519 seed, clarified docs, and added regression tests for uppercase, whitespace, and short keys. |
| Info | Uncommitted tree at audit time | Release-process note. | This remediation is committed before the signed `v0.17.0` tag is created. |
| Carry | Docker base-image digest pinning remains a prior supply-chain task | Not introduced by v0.17.0. | Left tracked as an ongoing known limitation in `SECURITY.md`. |

## Release Gate

Before tagging v0.17.0, run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
/Users/vstantch/.local/bin/uv lock --check
/Users/vstantch/.local/bin/uv run --extra evidence --extra audit pip-audit --progress-spinner=off
.venv/bin/python -m pytest tests/ -x -q --tb=short
```
