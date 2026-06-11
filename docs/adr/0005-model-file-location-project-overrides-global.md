# ADR-0005: Model file at `~/.pat/model.json`; project-local `.pat-model.json` overrides global

* Status: accepted
* Date: 2026-06-10
* Decision ref: D5 (PRESIDIO-REQ.md, v0.8.0 Design Decisions)

## Context

`pat calibrate` writes fitted parameters that `pat analyze` and the other analysis
commands read back. Where those parameters live matters: a single global file is
simple but cannot represent a calibration that is specific to one project or
repository, while a project-local-only file loses the convenience of a
machine-wide default. Earlier notes were inconsistent about whether there was one
location or two, so the resolution order needed to be locked.

The shipped loader (`model._model_search_paths`) already checked a project-local
file before a global one; the decision was to ratify that behaviour rather than
diverge from it.

## Decision

We will support two locations and resolve them in a fixed order:

1. **Project-local** `.pat-model.json` in the current working directory
2. **Global** `~/.pat/model.json`

The loader checks project-local first and falls back to global — **project-local
overrides global**. `pat calibrate` writes the global file by default.

## Consequences

- A repository can carry its own calibration (commit or gitignore
  `.pat-model.json` as the team prefers) that overrides the machine default
  without touching it.
- Running `pat analyze` from inside a project directory transparently picks up the
  project's calibration; running it elsewhere falls back to the global fit.
- Two possible sources means a surprising result is always explained by "which
  file won" — the fixed, documented precedence keeps that debuggable.
- This precedence composes with the per-layer scheme (ADR-0004): layer selection
  happens *within* whichever file the precedence above selects.
