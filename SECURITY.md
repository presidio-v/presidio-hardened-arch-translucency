# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.6.x   | :white_check_mark: |
| 0.5.x   | :white_check_mark: |
| 0.4.x   | :white_check_mark: |
| < 0.4   | :x:                |

## Reporting a Vulnerability

Please report security vulnerabilities by opening a private GitHub Security Advisory
(via the "Security" tab → "Report a vulnerability") rather than a public issue.

Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive an acknowledgement within 5 business days. We aim to release a patch
within 30 days of a confirmed vulnerability.

## Security Features

This toolkit ships with the following Presidio security hardening:

| Feature | Description |
|---|---|
| **Input sanitization** | All CLI parameters are bounds-checked and type-validated before use |
| **Secure logging** | Recommendations logged without sensitive data; user input never echoed raw |
| **CVE/dependency audit** | `pip-audit` check runs on every invocation (skippable via `--skip-audit`) |
| **Security event logging** | Structured audit log entry emitted for every recommendation |
| **Output sanitization** | Rich markup prevents injection via user-supplied layer names |
| **Dependabot** | Automated dependency updates configured in `.github/dependabot.yml` |
| **CodeQL** | Static analysis via `.github/workflows/codeql.yml` |

## Dependency Security

Dependencies are pinned in `pyproject.toml` and monitored via:
- GitHub Dependabot (automated PRs for updates)
- `pip-audit` on every CLI run
- CodeQL static analysis on every push and weekly schedule

## Known Limitations (v0.4.0)

- The simulation model uses calibrated coefficients, not live telemetry.
  Production use should be validated against actual cluster metrics.
- `pip-audit` requires a network connection; it gracefully skips when offline.

## Software Development Lifecycle

This repository is developed under the Presidio hardened-family SDLC. The public report
— scope, standards mapping, threat-model gates, and supply-chain controls — is at
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
