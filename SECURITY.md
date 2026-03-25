# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

**Do not file a public GitHub issue for security vulnerabilities.**

Please report security vulnerabilities to the maintainers via one of:

1. **GitHub Security Advisories** — use the "Report a vulnerability" button on the
   [Security tab](../../security/advisories/new) of this repository.
2. **Email** — send details to the repository owner (visible on the GitHub profile).

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive an acknowledgement within 72 hours and a resolution timeline
within 7 days for confirmed vulnerabilities.

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

## Known Limitations (v0.3.0)

- The simulation model uses calibrated coefficients, not live telemetry.
  Production use should be validated against actual cluster metrics.
- `pip-audit` requires a network connection; it gracefully skips when offline.
