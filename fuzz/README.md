# Fuzzing

Coverage-guided fuzzing of `presidio_arch_translucency` with Atheris.

The harness targets the calibration untrusted-input boundary in
`calibrate.py`: the CLI observation parsers (`parse_observation`,
`parse_energy_observation`) and the fail-closed readers of the on-disk fit
record `~/.pat/model.json` (`commitment_of`, `verify_commitment`,
`training_commitment_status`, and friends).

## Run

    uv pip install --system -e ".[fuzz]"
    python fuzz/fuzz_calibration_record.py            # until a crash or Ctrl-C

Time-boxed, as CI runs it:

    python fuzz/fuzz_calibration_record.py -atheris_runs=25000 -max_total_time=60

The harness raises `AssertionError` explicitly (no bare `assert` statements),
so its property checks fire regardless of `-O`/`PYTHONOPTIMIZE`; any unexpected
exception propagates and Atheris records the crash with a reproducer.

## Gotchas

- **No macOS wheel:** Atheris ships Linux-only wheels, so this runs in the
  `fuzz` CI job on `ubuntu-latest`, never on a developer Mac.
- **No cp310 wheel:** Atheris 3.x dropped Python 3.10, so the job pins Python
  3.12 (the extra's `python_version >= "3.11"` marker keeps `>=3.10` resolves
  clean). The test matrix is untouched.
- **Editable installs can shadow the package:** an `-e` checkout can win over
  the installed distribution on `sys.path`; verify the `calibrate` import
  resolves to the real target so coverage exercises the code under test.

## Why the literal `import atheris` matters

OpenSSF Scorecard's Fuzzing check greps sources for the literal `import
atheris` string and its dynamic-analysis check needs the harness to import and
drive the real target module. Keep that import line intact and keep the harness
calling the installed package, not a local re-implementation.
