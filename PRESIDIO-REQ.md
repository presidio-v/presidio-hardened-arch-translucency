# Presidio-Hardened Toolkit: presidio-hardened-arch-translucency

## Overview
Build a production-ready Python CLI tool named `presidio-hardened-arch-translucency` that implements MVP 0.1.0 of the "architectural translucency" concept (defined 20 years ago by Vladimir Stantchev). It analyzes where replication should be applied (new Docker container vs. Kubernetes Pod vs. multi-node Deployment with replicas) to maximize performance (throughput ω(δ) and response time) for a given workload.

Architectural translucency is the ability to monitor and control non-functional properties (especially performance) architecture-wide in a cross-layered way. It states that the same measure (e.g. replication) has different implications on throughput and response time when applied at different layers. Key layers for Docker/K8s: container level (new container), Pod level, Deployment/ReplicaSet level, or cluster-node level. The goal is to choose the layer that gives the highest performance gain with the lowest overhead.

Target: Docker and Kubernetes deployments in cloud-native environments.

Users run `pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container` and receive a layer recommendation with estimated throughput/response-time improvement.

## Mandatory Presidio Security Extensions
- Input sanitization for all workload parameters (bounds checking, type validation, rejection of malformed inputs)
- Secure logging of replication recommendations (no sensitive data, no secrets in log output)
- On-run CVE/dependency check for Docker/K8s client libraries (pip-audit or safety check on startup)
- Security event logging ("Presidio architectural-translucency recommendation applied")
- Rate-limit / abuse guard on CLI invocations (configurable max calls per session)
- Strict output sanitization: recommendations never echo raw user input without escaping
- Full GitHub security files: SECURITY.md, .github/dependabot.yml, .github/workflows/codeql.yml + pytest workflow

## Technical Requirements
- Python 3.9+
- Modern pyproject.toml + hatchling/uv + Typer CLI
- src/presidio_arch_translucency/ layout
- Simple CLI: `pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container`
- Returns recommendation + estimated throughput/response-time improvement
- Basic simulation model based on the original equations from the papers (ω(δ) = f(ι(δ)), response time = 1/ω)
- 80%+ test coverage with pytest
- README.md with side-by-side examples and clear reference to the architectural translucency concept
- LICENSE = MIT
- Version = 0.1.0

## Workflow Rules (always follow)
1. First create or update PRESIDIO-REQ.md from this template (adapt for the specific toolkit).
2. Manually remove or comment out the final "Deliver the complete working project ready for GitHub publish." line.
3. Implement file-by-file in logical order.
4. After every major section run validation commands (ruff format . && ruff check . --fix && pytest) and fix all issues automatically.
5. When complete, reply exactly: "BUILD COMPLETE – ready for publish"

<!-- Deliver the complete working project ready for GitHub publish. -->
