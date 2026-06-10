# CLAUDE.md — presidio-hardened-arch-translucency

See `PRESIDIO-REQ.md` for requirements and `SECURITY.md` for security policy.

## Verification

```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check . && .venv/bin/python -m pytest tests/ -x -q --tb=short
```
