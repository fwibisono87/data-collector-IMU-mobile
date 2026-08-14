# Emit-tick drift: why phones produce different row counts over the same window

Findings from session `1786677865027` (2026-08-14, 3 phones, 9m35s). Investigation only —
no fix is included in this changeset.

## What was observed

The three phones' CSVs cover the **same wall-clock window** — spans agree to within 30 ms —
but hold different numbers of rows:

| Role | Rows | Span | row_hz | true_sensor_hz | held rows |
|---|---|---|---|---|---|
| waist | 57,317 | 575.901 s | 99.53 | 99.07 | 0.29% |
| chest | 56,173 | 575.890 s | 97.54 | 91.57 | 3.36% |
| thigh_right | 50,569 | 575.871 s | 87.81 | 79.50 | 4.51% |

Rows are emitted by a `Timer.periodic` at 100 Hz in
`mobile_node/lib/services/internal_sensor_manager.dart`, independent of sensor freshness.
A perfect run would be 57,590 rows. thigh_right is 12% short.

**This is not packet loss.** Sequence numbers are a complete 0..N-1 run on all three
devices — no gaps, no duplicates, `packets_dropped_no_writer` and `csv_write_failures` both
zero. The missing rows were never emitted by the phone.

## The timer is healthy; the losses are discrete stalls

`dt` is tightly centred on the 10 ms period for every device — median exactly 10.0, with
9/10/11 ms dominating the histogram. There is no systematic slip. What differs is how often
a tick is missed outright:

| Role | intervals ≥20 ms | est. ticks never emitted |
|---|---|---|
| waist | 0.41% | 823 (1.4%) |
| chest | 1.57% | 3,245 (5.5%) |
| thigh_right | 6.58% | 8,238 (14.0%) |

## The losses are concentrated at the start of the session

Stalls (≥20 ms) per quarter of the session:

| Role | Q1 | Q2 | Q3 | Q4 |
|---|---|---|---|---|
| chest | 509 | 252 | 102 | 20 |
| thigh_right | 2,619 | 669 | 21 | 17 |
| waist | 102 | 93 | 21 | 21 |

Per 10-second bucket, thigh_right loses ~350–430 ticks per bucket (≈35–40%) steadily
through the first ~3 minutes, then runs essentially clean for the rest of the session. That
average of ~35% over ~180 s of a 576 s session accounts for the observed 12% shortfall
almost exactly.

The shape matters: this is **not** thermal throttling (which worsens over time) and not a
uniformly slow handset (which would lose ticks evenly). Something contends with the emit
timer early in a session and then stops.

## What this rules in

Two candidates fit both the timing and the per-device ordering:

1. **Startup contention on the main isolate.** Session start does file creation, header
   writes, buffer activation and — critically — `RecoveryUploader` work for any pending
   rescue file. A background upload that completes a few minutes in would produce exactly
   this decay. `local_session_recorder.dart` now serialises its I/O through `_ioChain` with
   a coalesced periodic flush, which should reduce main-isolate blocking; that landed in
   this changeset but **has not yet been measured against a fresh session**.

2. **Handset variation.** The 2510DRA23E is dual-sourced. The ranking by emit-tick loss
   (thigh_right ≫ chest ≫ waist) is the same as the ranking by `true_sensor_hz`
   (79.5 / 91.6 / 99.1) and by held-row percentage. The device whose sensor HAL runs at a
   lower ODR is also the device whose timer slips most, which points at the whole handset
   being slower rather than at the sensor alone.

These are not exclusive, and the data here cannot separate them.

## How to settle it

- Re-run a session on the merged build and compare tick loss in the first 3 minutes against
  the numbers above. The serialised recorder I/O is the one variable that changed.
- Log `RecoveryUploader` start/finish timestamps per session and check whether the loss
  window closes when the upload completes. This is the cheapest discriminator.
- Swap roles between handsets. If the loss follows the physical phone rather than the role,
  it is handset variation; if it follows the role, it is something in the session-start path
  specific to that device's workload.
- Capture `dumpsys sensorservice` per handset to confirm which part (TDK icm4n607 vs Bosch
  bmi3xy) is in each unit, and record it alongside the device id.

## Consequence for analysis, regardless of cause

Row counts differ between devices over the same window, and no fixed rate describes any of
these files. `timestamp_ms` is the authoritative axis. This is why CSV filenames no longer
carry a rate token and why `<session>_<role>_timing.json` states `row_hz`, `true_hz` and the
measured span explicitly — see the sidecar's own `note` field.
