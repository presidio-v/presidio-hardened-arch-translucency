# ADR-0002: `pat observe` is single-shot (cron/launchd), not a daemon

* Status: accepted — extended (not reversed) by v0.9.0 daemon mode (see Consequences)
* Date: 2026-06-10
* Decision ref: D2 (PRESIDIO-REQ.md, v0.8.0 Design Decisions)

## Context

`pat observe` records workload measurements into the rolling store for
`pat optimize` to read back. Recurring collection is inherent to the use case, so
the process model was an open question: should `observe` own a long-running
poller (a `--duration/--interval` foreground loop or a background daemon), or take
one measurement and exit while something external handles the schedule?

A long-running poller owns state, must survive crashes and restarts, complicates
testing (timers, signals, partial writes), and duplicates schedulers the host
already provides (cron, launchd, systemd, Kubernetes CronJob).

## Decision

We will make `pat observe` strictly single-shot: it takes one measurement (or one
Prometheus scrape), appends it to the store, and exits. It is not a daemon and not
a foreground polling loop. Users schedule recurring collection externally. The
earlier `--duration/--interval` framing is superseded by this decision.

## Consequences

- The tool stays stateless, crash-safe, and trivially testable — each invocation
  is a pure "measure → append → exit".
- Scheduling is delegated to mature, host-native schedulers rather than reinvented.
- Users must set up a cron/launchd/CronJob entry themselves; there is a small
  onboarding cost for the convenience of not owning a process.
- **v0.9.0 extension, not reversal.** `pat observe daemon install` now *writes*
  the launchd/systemd unit for the user. This is opt-in convenience on top of the
  host scheduler — the scheduler still fires single-shot `pat observe` on an
  interval; observe itself never becomes a long-running process. The single-shot
  invariant of this ADR is preserved.
