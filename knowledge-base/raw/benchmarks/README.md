# Benchmark Captures

Dated snapshots of `pat demo` and `pat what-if` results.
Each file is immutable — add a new dated file for each run.

## File naming

`YYYY-MM-DD-<scenario>.md`

## Index

*(no entries yet — add after first pat demo run)*

## What to capture per run

```markdown
# pat demo / pat what-if result
# Date: YYYY-MM-DD | Machine: <host description> | Docker version: X.Y
# Command: pat demo --replicas N --requests M --concurrency C [flags]

## Measured results table
[paste CLI output]

## HPA projection panel
[paste CLI output]

## Cost analysis panel
[paste CLI output]

## Notable observations
[any surprises, environment notes, deviations from model predictions]
```

## Why this matters

The `pat demo` results are the primary empirical basis for calibrating the model
parameters (α, β) per layer. If measured throughput deviates significantly from
model predictions, parameters need updating. See `raw/replication-model.md` §Calibration.
