# Session output contract

This document describes every artifact the backend (and the browser export) leaves
behind for one finished session, precisely enough that the app can be rebuilt without
changing what an analyst receives.

The contract is **executable**: `master_backend/tests/contract/test_output_contract.py`
drives a real session through `IoManager → SessionManager → FinalizeJob →
IntegrityValidator → export/upload/cameras` with a frozen clock, then compares the
complete output against
`master_backend/tests/contract/golden/session_snapshot.json`. The snapshot is the
authority; this document explains it.

Seventeen subjects are already recorded. Their files on the SSD are research data:
names, metadata lines, headers, column order, number formatting and JSON key trees
must not drift. If a change is intentional, re-read this document, then regenerate the
golden file:

```sh
UPDATE_GOLDEN=1 master_backend/.venv/bin/python -m pytest master_backend/tests/contract -q
master_backend/.venv/bin/python -m pytest master_backend/tests -q
```

The golden file is normalised for exactly two things — the session id and values that
genuinely differ between two runs (`VOLATILE`, see §12). Treat a diff in any other
value as a real change to the contract.

---

## 1. Storage layout

All paths are rooted at the environment variables the app reads at import time.

```
$SSD_PATH/                                    # SSD_PATH (default ./data)
├── backend_audit.jsonl                       # one process-wide JSONL audit log
├── .sessions/
│   └── <session_id>.state.json               # terminal session ledger
└── Data_Riset_IMU/
    ├── <subject>_<tag>/                      # session folder (spaces → _)
    │   ├── <session_id>_<role>_sensor_data.csv
    │   ├── <session_id>_chest_sensor_data_late.csv      # only if a late tail arrived
    │   ├── <session_id>_late_delivery.json
    │   ├── <session_id>_integrity_report.json
    │   ├── <session_id>_connectivity.json
    │   ├── <session_id>_sampling.json
    │   ├── <session_id>_<role>_timing.json
    │   ├── <session_id>_cameras.json
    │   ├── <session_id>_consolidated.csv
    │   ├── <session_id>_<role>_consolidated.csv
    │   ├── <session_id>_consolidation.json
    │   ├── <session_id>_consolidated_validation.json
    │   └── <session_id>_bundle.zip
    └── _pending_cameras/
        └── <session_id>_cameras.json          # camera anchors that arrived before the folder
$RESCUE_PATH/                                  # rescue fallback when the SSD write fails
    └── Data_Riset_IMU/<subject>_<tag>/
        └── <session_id>_<role>_sensor_data_rescue.csv
            # legacy sessions may instead carry <session_id>_<role>_rescue.csv;
            # every parser accepts both
$RECOVERY_PATH/<session_id>/                   # phone-side rescue uploads
    ├── <device_id>.csv
    └── <device_id>.info.json
```

`<session_id>` is `str(int(time.time() * 1000))` at START — epoch milliseconds, e.g.
`1700000000000`. `<role>` is the role the device registered with (`chest`, `waist`,
`thigh_right`, …). The file writers deliberately emit **no** sampling-tier token:
legacy files may still contain `_75hz_`, and every filename→role parser must strip it
(`csv_schema.strip_tier_token`).

The contract scenario produces 23 files (asserted by
`test_every_artifact_type_is_produced`):

```
<sid>.state.json                    <sid>_chest_consolidated.csv
<sid>_bundle.zip                    <sid>_chest_sensor_data.csv
<sid>_cameras.json                  <sid>_chest_sensor_data_late.csv
<sid>_chest_timing.json             <sid>_connectivity.json
<sid>_consolidated.csv              <sid>_consolidated_validation.json
<sid>_consolidation.json            <sid>_integrity_report.json
<sid>_late_delivery.json            <sid>_sampling.json
<sid>_thigh_right_consolidated.csv  <sid>_thigh_right_sensor_data.csv
<sid>_thigh_right_timing.json       <sid>_waist_consolidated.csv
<sid>_waist_sensor_data.csv         <sid>_waist_timing.json
<waist-device-id>.csv               <waist-device-id>.info.json
backend_audit.jsonl
```

---

## 2. CSV data files

Writer: `master_backend/app/io_manager.py` (`_format_row`, `DeviceWriter`), header and
metadata constants from `master_backend/app/csv_schema.py`.

### 2.1 Names

| File | Producer | Notes |
|---|---|---|
| `<sid>_<role>_sensor_data.csv` | `IoManager._open_writer_for` | one per registered device; opened at START |
| `<sid>_<role>_sensor_data_late.csv` | `IoManager.write_late` | created only when a packet arrives inside the late window |
| `<sid>_<role>_sensor_data_rescue.csv` | `IoManager._activate_rescue_writer` | only if the SSD writer raises; legacy name `<sid>_<role>_rescue.csv` |
| `<sid>_consolidated.csv` | `export.export_consolidate` | union of all sources, deduped |
| `<sid>_<role>_consolidated.csv` | `upload.merge_csv_sources_per_role` | same union, bucketed per role |
| `<sid>_merged.csv` | `upload.recovery_merge` | manual recovery-screen merge (not part of the automatic flow) |

Pinned by `test_device_csv_name_header_and_cell_format` (name, metadata, header,
formats) and `test_rows_are_sorted_and_the_sequence_gap_is_visible`.

### 2.2 Metadata line

Exactly one `#` line before the header. Built by `csv_schema.metadata_line`, which
emits keys **in this order**, skipping empty values, then appends `extra`:

```
session_id, schema_version, subject, operator, role, device_id,
nominal_hz, device_model, app_version
```

`schema_version` is always `2`. Commas and whitespace inside a value are replaced with
`_` (the parser and the Flutter uploader both read `(\w+)=([^,\s]+)`), so a value can
never truncate the line.

The actual lines emitted today:

| File | Metadata line |
|---|---|
| main CSV | `# session_id=<sid>,schema_version=2,subject=<subject>,operator=<operator>` |
| late CSV | the main line **plus** `,late_delivery=1` |
| consolidated | `# session_id=<sid>,source=consolidate` |
| recovery merge | `# session_id=<sid>,subject=<subject>,session_tag=<tag>,source=recovery_merge` |
| backend restart recovery | `# session_id=<sid>,source=backend_restart_recovery` |

If preflight checks were red at START, the main line additionally carries
`preflight_failed=<check>;<check>` — an extra designed to travel with the data.

The rescue CSV a phone uploads is stored **verbatim** as
`$RECOVERY_PATH/<sid>/<device_id>.csv`; the backend never rewrites its metadata. The
phone's format is:

```
# session_id=<sid>,role=<role>,device_id=<uuid>,subject=<subject>,
  session_tag=<tag>,operator=<op>,start_epoch_ms=<ms>,source=local_node,schema_version=2
```

### 2.3 Header

Byte-for-byte `CSV_HEADER_V2` (with its trailing newline), 14 columns in this order:

```
timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,
label_id,label_name,sequence_number,device_id,acc_ts_ms,gyro_ts_ms,sample_kind
```

### 2.4 Row format

`io_manager._format_row`:

| # | Column | Type / format |
|---|---|---|
| 0 | `timestamp_ms` | integer epoch ms (the authoritative time axis) |
| 1–3 | `acc_x_g`,`acc_y_g`,`acc_z_g` | `%.6f` |
| 4–6 | `gyro_x_degs`,`gyro_y_degs`,`gyro_z_degs` | `%.6f` |
| 7 | `label_id` | integer |
| 8 | `label_name` | raw string, unquoted (labels are slugs; a comma would break the CSV) |
| 9 | `sequence_number` | integer, from the phone |
| 10 | `device_id` | UUID string |
| 11 | `acc_ts_ms` | integer, or **empty** when the packet carries `0` |
| 12 | `gyro_ts_ms` | integer, or **empty** when the packet carries `0` |
| 13 | `sample_kind` | `0` fresh / `1` held; **empty** for `schema_version < 2` |

Rules that matter:

* `0` sensor-clock stamps are written as an empty field, never as `0`
  (`pkt.acc_ts_ms if pkt.acc_ts_ms else ""`).
* A pre-v2 phone writes an empty `sample_kind` — unknown provenance, not "fresh".
* Floats are always six decimals, negatives included; no thousands separators, no
  scientific notation.
* The label is resolved from the label timeline by packet timestamp, so a row gets the
  label that was active when the sample was taken, not when it was written.
* `label_name` is unquoted. `csv_schema.parse_row` splits on `,` and tolerates v1 rows
  (11 columns) by padding to 14.
* On close (`DeviceWriter.close`) rows are rewritten in timestamp order **only if** any
  row arrived out of order; an already-sorted file is left untouched. Stable sort keeps
  arrival order for equal timestamps. `reordered` is reported in `state.json`.
* Consolidation de-duplicates on `(device_id, sequence_number)` and sorts by
  `timestamp` then insertion order. Duplicates dropped are reported per role.

The scenario's chest stream has a deliberate 20-sequence hole (300–319) and a late
packet with sequence 620; the regular 10 ms cadence, `sample_kind=1` on every 97th row
and empty stamps on every 50th row are frozen in the golden file.

---

## 3. Stop-time reports

Written by `integrity_validator.IntegrityValidator.run`, into the folder of the first
main CSV. All three are pretty-printed JSON (`indent=2`) with no trailing newline.
Pinned by `test_json_report_top_level_keys`.

> **Which version survives.** `run()` also runs once more after consolidation
> (`export._revalidate_consolidated`), over the consolidated per-role files. That second
> pass **overwrites** this report, `connectivity.json`, `sampling.json` and every
> `timing.json`. The golden file therefore represents the post-consolidation version:
> `validation_scope: "consolidated_sources"` and `csv_path`/`csv` pointing at
> `<sid>_<role>_consolidated.csv`. At STOP, before any consolidation, the same files say
> `validation_scope: "main_csv_at_stop"` and point at the main CSVs.

### 3.1 `<sid>_integrity_report.json`

Top-level keys:

```
session_id, status, validated_at_ms, validation_scope, label_timeline,
session_start_ms, session_end_ms, devices, cross_device_checks,
analysis_ready, analysis_ready_reasons
```

* `status`: `PASS` / `PARTIAL` / `FAIL`, worst across devices.
* `label_timeline`: ordered `{timestamp_ms, label_id, label_name}`; the first entry is
  the default `0/"0"` stamped at START, later ones stamped when SET_LABEL arrived.
* `devices[]` — per device:
  `device_id, role, csv_path, row_count, csv_sha256, status, reasons,
  first_packet_ts, offline_intervals, packets_received, offline_interval_count,
  offline_total_ms, telemetry_gaps, telemetry_gap_count, telemetry_gap_total_ms,
  rows_reordered, packets_dropped_no_writer, csv_write_failures,
  rows_lost_after_failover, analysis_ready`, plus, when measurable:
  `sampling`, `coverage`, `invalid_rows`, `label_aware_sequence_gaps`, and
  `issue` only for the zero-rows / no-CSV failure modes.
  * `sampling`: `nominal_hz, true_sensor_hz, held_row_pct, reference_hz,
    first_timestamp_ms, last_timestamp_ms, dt_ms, sequence`.
  * `dt_ms`: `median, mean, p95, max, min, non_positive`.
  * `sequence`: `present, missing, missing_pct, duplicates, min, max, largest_gap,
    gaps[]`; each gap is
    `{start_ms, end_ms, duration_ms, missing, previous_sequence, next_sequence}`.
  * `coverage`: `expected_start_ms, first_timestamp_ms, last_timestamp_ms,
    session_end_ms, leading_gap_ms, trailing_gap_ms, accepted_gap_ms`.
  * `label_aware_sequence_gaps[]` (gaps re-classified after writing): the gap fields
    above plus `start_label_id, end_label_id, nonzero_label_overlap,
    nonzero_transition, zero_transition_ms, waived`.
* `cross_device_checks`: `device_count, expected_device_count, role_uniqueness,
  scheduled_start_ms, max_start_drift_ms, start_drift_threshold_ms, start_drift_ok,
  all_devices_completed, missing_devices_intervals, telemetry_gaps`.
* `analysis_ready` + `analysis_ready_reasons`: the human-readable reason a device is
  not analysis-ready (rate failure, sequence gap over a label, coverage gap, …).

Frozen scenario result: `status: "FAIL"`, `analysis_ready: false`, reasons
`"chest: sampling-rate acceptance failed"` and
`"chest: 10 ms gap overlaps non-zero label"`. Chest's `sequence` is
`present 601, missing 20, missing_pct 3.2206119162640903, largest_gap 20`, one gap
from `previous_sequence 299` to `next_sequence 320`.

### 3.2 `<sid>_connectivity.json`

```
session_id, devices[]
```

Each device: `device_id, role, intervals, total_offline_ms, telemetry_gaps,
total_telemetry_gap_ms`. `intervals` are the WebSocket disconnect spans; `telemetry_gaps`
are the data-side gaps. This is the machine-readable answer to "why PARTIAL?".

### 3.3 `<sid>_sampling.json`

```
session_id, generated_at_ms, schema_version (2), thresholds, devices[]
```

`thresholds`: `rate_partial_frac, rate_fail_frac, seq_gap_partial_pct,
seq_gap_fail_pct, max_drift_ms` (`0.9, 0.7, 0.1, 1.0, 100` unless overridden by
`INTEGRITY_*` env vars).

Each device is the full `sampling_analysis.analyse_rows` output plus a verdict:
`device_id, role, rows, first_timestamp_ms, last_timestamp_ms, span_s, nominal_hz,
reference_hz, true_sensor_hz, held_row_pct, acc_run_length_hist, dt_ms, sequence,
invalid_rows, declared, verdict, reasons`.

`declared` = `{held_row_pct_declared, agreement_pct}`: what the phone *said* about
`sample_kind=1` versus what was detected by comparing consecutive six-value samples.
In the golden: `agreement_pct: 100.0`.

`held_row_pct` is **percent of all rows whose full six-value sample repeats the previous
row** (100 × held / total rows). It is a detected repetition rate, not the fraction of
`sample_kind=1` rows — hence ~99.8 % on a stream with only ~6 deliberately repeated
rows. Do not re-interpret it as "ZOH duty cycle".

### 3.4 `<sid>_<role>_timing.json`

One per role. Keys: `session_id, role, device_id, csv, first_ts_ms, last_ts_ms,
span_s, rows, row_hz, true_hz, held_pct, note`.

* `csv` is a bare filename — `<sid>_<role>_consolidated.csv` after revalidation,
  the main CSV at STOP.
* `row_hz` = rows / span; `true_hz` = distinct hardware readings / span (lower by the
  held fraction); `held_pct` = detected repetition percentage.
* `note` is a fixed warning that `timestamp_ms` is the authoritative axis and time must
  not be reconstructed from a fixed rate.

Frozen values: chest `row_hz 100.16666666666667`, `true_hz 99.16666666666667`,
`span_s 6.0`, `rows 601`; waist `rows 620`, `span_s 6.19`; thigh_right `rows 600`,
`true_hz 99.1652754590985`.

### 3.5 `<sid>_consolidated_validation.json`

Written by `export.export_consolidate` via `integrity_validator.validate_consolidated`
(sequence-gap check only):

```
session_id, status, validated_at_ms, validation_scope, thresholds, roles[]
```

`validation_scope` is `"consolidated_per_role"` — note the **different string** from the
integrity report's `"consolidated_sources"`. Each role: `role, path, rows, status,
reasons, sequence` (same `sequence` shape as above). Frozen: overall `FAIL`, chest
`missing 20 / 3.2206 %`.

### 3.6 `<sid>_consolidation.json`

```
session_id, consolidated_at_ms, per_role, per_role_files, path, rows, sources,
source_files, duplicates_dropped, validation
```

* `path`, `rows` (1821 in the golden), `sources` (`main/late/recovery` counts),
  `source_files[]` (`{path, label, rows}`) and `duplicates_dropped` come from the
  session-wide merge.
* `per_role` maps role → `{path, rows, sources, source_files, duplicates_dropped}`;
  `per_role_files` lists the files in role order.
* `validation` is the same document written to
  `<sid>_consolidated_validation.json` (duplicated deliberately).
* `revalidated` is **not** stored here; it goes into the ledger and the route response.

---

## 4. Late delivery

Writer: `io_manager.write_late` + `finalize_late`, triggered when a phone reconnects
inside `LATE_ACCEPT_SEC` after STOP.

* `<sid>_<role>_sensor_data_late.csv` — only devices that actually sent a late packet.
  Same header and columns; metadata is the main line + `,late_delivery=1`.
* `<sid>_late_delivery.json`:

```json
{
  "session_id": "<sid>",
  "devices": {
    "<device_id>": {
      "path": "<absolute path>",
      "rows": 1,
      "sha256": "<hex>",
      "reordered": 0,
      "rows_appended": 1
    }
  }
}
```

`rows` is the file's row count after close; `rows_appended` is how many packets this
window wrote. The summary is written only if at least one device sent something, and
only if the late window was armed by a real `close_session`.

---

## 5. Phone rescue upload

Route: `POST /upload/csv` (`master_backend/app/upload.py`).

* Uploads are chunked with `x-offset`, `x-total`, `x-complete: 1`, `x-sha256`.
* Stored as `$RECOVERY_PATH/<sid>/<device_id>.csv` (bytes appended verbatim,
  `fsync`ed) and `<device_id>.info.json`.
* `<device_id>.info.json` keys:
  `session_id, device_id, role, subject, session_tag, operator, total_bytes,
  received_bytes, sha256, sha256_verified, complete, done, start_epoch_ms,
  updated_at_ms, state` — plus `corrupt_attempts` after a failed hash.
  `state` is `receiving`, `verified` or `corrupt`; `complete` is only true after the
  final size **and** SHA-256 match. A mismatch deletes the bytes and resets the
  transfer so a retry starts at 0.
* A verified recovery file is a consolidation source with label `recovery`; it is
  deduped against the live stream on `(device_id, sequence_number)`.
* The recovery directory is also exported as `data/recovery/<device_id>.csv` in both
  bundles. The `.info.json` sidecar is **not** exported.

---

## 6. Backend bundle (`<sid>_bundle.zip`)

Builder: `export._build_bundle` / `build_session_bundle`. Written to
`<sid>_bundle.zip.tmp` then `os.replace`d, so a truncated archive never looks finished.
Built automatically by the finalize job (unless `FINALIZE_AUTO_BUNDLE` disables it) and
on demand by `POST /export/<sid>/bundle`.

Entries:

```
bundle_manifest.json
data/<every session file with the <sid>_ prefix>     # never a previous bundle
data/recovery/<device_id>.csv                         # each verified recovery CSV
```

Concretely, the golden archive contains `data/` copies of the cameras, sensor, late,
timing, connectivity, sampling, integrity, late-delivery, consolidation, consolidated,
consolidated-validation files and both consolidated per-role files, plus
`data/recovery/<waist>.csv`.

`bundle_manifest.json`:

```
session_id, built_at_ms, entries, lifecycle, contains_video, note
```

* `entries` lists every archive member **except** the manifest, in write order.
* `lifecycle` is the ledger record at build time (`_ledger_record`).
* `contains_video` is always `false`: footage is recorded by the browser via
  MediaRecorder and never reaches the backend.
* `note` explains that and points at the dashboard's "Recover buffered video" screen.

Downloading the bundle returns `{session_id, contains_video: false, path, entries, size}`.
The HTTP endpoint's filename is `<sid>_bundle.zip`.

> **Oddity frozen:** if consolidation and bundle run after STOP (the contract scenario
> does), the automatic finalize-time bundle is replaced by this later one. Its
> `bundle_manifest.lifecycle` therefore reflects the post-consolidation ledger,
> including `revalidated_after_consolidation_ms`.

---

## 7. Session ledger (`$SSD_PATH/.sessions/<sid>.state.json`)

Writer: `session_ledger.SessionLedger.write` (atomic replace), populated by
`SessionManager`. This is the dashboard's and bundle manifest's source of truth.

Top-level keys (golden): `session_id, subject_name, session_tag, operator, state,
terminal, stop_reason, scheduled_start_ms, recording_started_ms, finalized_at_ms,
updated_at_ms, startup_in_progress, preflight_failed, label_timeline, devices,
file_results, finalize, integrity_report, revalidated_after_consolidation_ms`.

* `state` is `IDLE` after a completed session (the manager returns to idle);
  `terminal` is `true`; `stop_reason` is the operator/automation reason.
* `devices[]`: `device_id, role, model, app_version, packets, first_packet_ts,
  offline_intervals, substate` (`FINALIZED`, …).
* `file_results`: device_id → `{path, rows, sha256, reordered}`.
* `finalize`: `{session_id, started_ms, finished_ms, steps[], failed[]}` where each step
  is `{step, state, started_ms, ended_ms, detail}`. Today's steps are
  `close_writers`, `validate`, `bundle`.
* `integrity_report` is the full report object (same as the JSON sidecar) and is
  rewritten by revalidation; `revalidated_after_consolidation_ms` records when.

> **Oddity frozen:** `finalize.finished_ms` is `0` even for a successful finalize,
> because the ledger snapshot is persisted before `finished_ms` is set. Do not "fix"
> this in the golden file without deciding it is a real change — dashboards must not
> assume it is populated.

---

## 8. Audit log (`$SSD_PATH/backend_audit.jsonl`)

Writer: `audit_logger.audit`, opened by `main.py` at startup (append mode, flushed at
least once a second, immediately for `ERROR`). One JSON object per line:

```json
{"ts_ms": 1700000000000, "level": "INFO", "event": "csv_opened", "detail": {...}}
```

Event names observed for one complete session include: `audit_log_opened`,
`state_transition`, `csv_opened`, `camera_mark`, `session_finalizing`,
`finalize_complete`, `late_delivery_started`, `late_delivery_finalized`,
`recovery_upload_complete`, `session_consolidated`, `session_bundled`.

The contract freezes the **line count** (22 in the scenario) and the **key set of each
event type** (`ts_ms, level, event, detail`), not the values: audit details embed
absolute storage paths and content-derived bundle sizes that legitimately differ per
run. See §12.

---

## 9. Browser-built ZIP (end-of-session modal)

Builder: `master_frontend/src/components/EndSessionModal.tsx` (`buildStreamEntries`,
`handleDownload`). Streamed to a real file handle by `streamZipToDisk`; nothing is
assembled in the JS heap. This archive is the one the operator saves from the
dashboard and it is the **only** place video appears.

Filename: `<subject>_<tag>_<sid>.zip`, with runs of whitespace in subject/tag replaced
by `_`; empty values default to `subject` / `session`.

Entries:

| Path | Source |
|---|---|
| `videos/<sid>_<camId>_video_sync.<webm\|mp4>` | one per MediaRecorder byte segment; later runs get `_part2`, `_part3`, … |
| `data/<backend manifest file name>` | every manifest file whose `kind` is a data kind (see below), re-pulled at click time |
| `data/recovery/<device_id>.csv` | each complete, hash-verified recovery upload |
| `data/manifest.json` | the backend `ExportManifest` snapshot (`JSON.stringify(m, null, 2)`) |
| `data/export_error.txt` | instead of the manifest/data files when the backend is unreachable |
| `cameras.json` | browser-built camera anchors **plus** IndexedDB `camera_events` |
| `missed_cameras.txt` | only when a camera never produced footage; one line per camera |
| `video_report.json` | per-video finalize evidence; written last |

Data kinds included: `main, csv, late, rescue, merged, consolidated, integrity,
connectivity, late_summary, consolidation, consolidated_validation, timing`
(`export_client.isDataKind`). Note what this means:

* The backend session bundle (`kind: "other"`) is **not** nested inside the ZIP.
* The backend's `_cameras.json` (`kind: "cameras"`) is **not** included; the root
  `cameras.json` is the browser's richer record (see below).
* The ledger `.state.json` lives outside the session folder and is not included.

`cameras.json`:

```json
{
  "session_id": "<sid>",
  "cameras": [{
    "session_id": "<sid>", "cam_id": "cam1", "device_id": "<uuid>",
    "browser_label": "Front", "mime": "video/webm",
    "file": "videos/<sid>_cam1_video_sync.webm",
    "files": ["videos/<sid>_cam1_video_sync.webm", "..._part2.webm"],
    "started_at_ms": 0, "flash_at_ms": 0, "stopped_at_ms": 0
  }],
  "camera_events": [{
    "key": "<uuid>", "sessionId": "<sid>", "camId": "cam1",
    "type": "started|stopped|track_ended|write_error",
    "atMs": 0, "detail": "optional"
  }]
}
```

`file` is the legacy single-file field (first segment); `files` always lists every
segment so a recorder restart is never mistaken for one stacked file.

`video_report.json`:

```json
{
  "session_id": "<sid>",
  "videos": [{
    "path": "videos/<sid>_cam1_video_sync.webm",
    "camId": "cam1",
    "ok": true,
    "bytesWritten": 1234567,
    "durationMs": 6000,
    "ebmlHeaders": 1,
    "error": "only when ok is false"
  }],
  "healthy": true
}
```

`videos[]` items are `FinalizeResult` (`webm_seekable.ts`) plus `path` and `camId`.
`healthy` is `every(v => v.ok && v.ebmlHeaders === 1)` — exactly one EBML header per
file is the definition of "seekable, single recording". `durationMs` is `null` when the
duration could not be recovered; failed finalizes still stream the raw bytes with
`ok: false` and an `error`.

Should the modal fail before saving, `data/export_error.txt` says so and the archive is
video-only; the CSVs remain on the backend and on the recovery screen.

---

## 10. What is frozen, intentionally

These are current behaviour, not aspirations. A rebuild must reproduce them or
regenerate the golden file after an explicit decision:

1. **`status: FAIL` for an otherwise complete session.** A 20-sequence hole (3.22 %)
   exceeds the 1 % fail threshold and overlaps non-zero labels, so chest fails and the
   session is not analysis-ready even though every other device is `PASS`.
2. **Post-consolidation reports overwrite stop-time reports**, changing
   `validation_scope`, `csv_path`/`csv`, and row counts. The final numbers describe the
   consolidated files (chest 601, waist 620, thigh_right 600).
3. **Two different scope strings**: `consolidated_sources` (integrity report) and
   `consolidated_per_role` (consolidated validation).
4. **`finalize.finished_ms: 0`** in the persisted ledger (see §7).
5. **`held_row_pct` means detected repetition**, not fraction of `sample_kind=1`.
6. **`label_name` is unquoted** in CSV; a comma in a label would corrupt the row.
7. **Empty, not `0`,** for unknown sensor-clock stamps and pre-v2 `sample_kind`.
8. **No rate token in filenames**; the measured rates live only in the timing sidecar.
9. **Metadata key order** as listed in §2.2, including `schema_version` immediately
   after `session_id`.
10. **`(device_id, sequence_number)` is the dedup key** for both late delivery and
    consolidation; timestamps alone are not unique.
11. **The backend bundle never contains video**, and never nests a previous bundle.
12. **The browser ZIP omits the backend bundle and backend camera anchors**, and adds
    its own `cameras.json` / `video_report.json`.
13. **Rescue CSVs are stored byte-for-byte**, including their phone-written metadata
    line and original row order.

---

## 11. Format-level tests (fast failure with a precise message)

Before the golden diff, `test_output_contract.py` asserts the invariants that matter
most, so a regenerated golden file cannot silently bless a broken format:

| Test | Pins |
|---|---|
| `test_every_artifact_type_is_produced` | the complete file inventory |
| `test_device_csv_name_header_and_cell_format` | file name, metadata keys/order, header, 14 columns, `%.6f`, empty stamps, both sample kinds |
| `test_rows_are_sorted_and_the_sequence_gap_is_visible` | monotonic timestamps, the exact chest sequence set, report gap |
| `test_json_report_top_level_keys` | every report's top-level tree, camera and late-device nested keys |
| `test_label_timeline_is_frozen` | label ids, names and transition timestamps |
| `test_scenario_is_deterministic` | two independent runs produce identical normalised snapshots |
| `test_outputs_match_golden` | the entire snapshot, with a 400-line unified diff on failure |

---

## 12. Volatile values

`VOLATILE` in the contract test contains exactly one rule:

```python
VOLATILE = [
    (r"^/", "<volatile>"),   # absolute storage paths
]
```

Why: `SSD_PATH`, `RESCUE_PATH` and `RECOVERY_PATH` are per-run temporary directories in
the test. Every absolute path in a report, ledger, consolidation summary, late-delivery
summary or `.info.json` therefore differs between runs. The rule replaces the whole
value with `<volatile>` only when the value **starts with `/`**; no substring rewriting,
no blanket normalisation.

The session id is replaced separately, everywhere, with `<SID>`
(`normalise(..., session_id=...)`). This is not a volatility workaround — it keeps the
golden file readable and lets one session id change without touching 2 800 lines. The
replacement applies to dict keys as well, because zip entry names and some CSV-derived
keys embed it.

The JSONL audit log freezes only `line_count` and the sorted key set per event type.
Its detail values embed absolute paths **and** content-derived bundle sizes
(`session_bundled`), which legitimately differ per run; freezing them would require
normalising most of every line and would hide real shape changes. The format itself
(one JSON object per line, four top-level keys) is still enforced.

Nothing else is normalised. If two runs differ in any other value, that is either a bug
or a missing `VOLATILE` entry — add it only after confirming the difference is inherent
to the environment, never to make a failing test pass.

---

## 13. Changing the contract

1. Read this document and the golden file; decide whether the change is intended.
2. Change the code, and update this document.
3. Regenerate:
   `UPDATE_GOLDEN=1 master_backend/.venv/bin/python -m pytest master_backend/tests/contract -q`
4. Review the golden diff like production data: it is the diff an analyst would see.
5. Run the whole suite: `master_backend/.venv/bin/python -m pytest master_backend/tests -q`.
6. If a value is newly volatile because a test path moved, justify it in the commit.