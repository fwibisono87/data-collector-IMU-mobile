/* eslint-disable */
/**
 * GENERATED from master_backend/app/api/schema.json — DO NOT EDIT BY HAND.
 * Regenerate: `npm run gen:api`. `npm run check:api` fails when this file is stale.
 */

export type Code = string;
export type Details = {
  [k: string]: unknown;
} | null;
export type Message = string;
export type Ok = boolean;
export type ReqId = string;
export type Type = 'ack';
export type ActiveRefId = string | null;
export type CamId = string;
export type DeviceKey = string;
export type AtMs = number;
export type Confidence = number;
export type DxPx = number;
export type DyPx = number;
export type Score = number;
export type ShiftPct = number;
export type Verdict = 'OK' | 'CHECK' | 'MOVED' | 'NO_SIGNAL' | 'MISSING' | 'RESOLUTION_CHANGED';
export type DroppedFrames = number;
export type Error = string | null;
export type Fps = number | null;
export type Height = number | null;
export type Label = string;
export type Recording = boolean;
export type State = 'opening' | 'live' | 'lost' | 'error';
export type Width = number | null;
export type Backend = 'dshow' | 'v4l2' | 'fake';
export type DeviceKey1 = string;
export type Label1 = string;
export type CamId1 = string | null;
export type DeviceKey2 = string;
/**
 * @maxItems 5
 */
export type Cameras =
  | []
  | [CameraSelectionItem]
  | [CameraSelectionItem, CameraSelectionItem]
  | [CameraSelectionItem, CameraSelectionItem, CameraSelectionItem]
  | [CameraSelectionItem, CameraSelectionItem, CameraSelectionItem, CameraSelectionItem]
  | [CameraSelectionItem, CameraSelectionItem, CameraSelectionItem, CameraSelectionItem, CameraSelectionItem];
export type Cameras1 = Camera[];
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ClientMessage".
 */
export type ClientMessage = WatchMsg | UnwatchMsg | PingMsg;
export type RefId = string;
export type ReqId1 = string | null;
export type Type1 = 'calibration.watch';
export type ReqId2 = string | null;
export type Type2 = 'calibration.unwatch';
export type ReqId3 = string;
export type Type3 = 'ping';
export type AtMs1 = number;
export type CamId2 = string;
export type Confidence1 = number;
export type DeviceKey3 = string;
export type DxPx1 = number;
export type DyPx1 = number;
export type Score1 = number;
export type ShiftPct1 = number;
export type Verdict1 = 'OK' | 'CHECK' | 'MOVED' | 'NO_SIGNAL' | 'MISSING' | 'RESOLUTION_CHANGED';
export type AtMs2 = number;
export type Cameras2 = CompareCamera[];
export type RefId1 = string;
export type Devices = CameraDevice[];
export type FreeBytes = number;
export type Path = string;
export type Operator = string;
export type SessionTag = string;
export type Subject = string;
export type UpdatedAtMs = number;
export type Operator1 = string | null;
export type SessionTag1 = string | null;
export type Subject1 = string | null;
export type Type4 = 'calibration.drift';
export type SessionId = string;
export type Detail = string | null;
export type EndedAtMs = number | null;
export type Name = 'close_writers' | 'cameras' | 'validate' | 'bundle' | 'package' | 'late_window';
export type StartedAtMs = number | null;
export type Status = 'pending' | 'running' | 'done' | 'failed' | 'skipped';
export type Steps = FinalizeStep[];
export type Type5 = 'finalize';
export type Code1 = string;
export type Message1 = string;
export type Subject2 = string | null;
export type LabelId = number;
export type LabelId1 = number;
export type SinceMs = number;
export type DeviceId = string;
export type Role = string;
export type Rows = number;
/**
 * @minItems 3
 * @maxItems 3
 */
export type Acc = [number, number, number];
/**
 * @minItems 3
 * @maxItems 3
 */
export type Gyro = [number, number, number];
export type TsMs = number;
export type TMs = number;
export type Type6 = 'live';
export type Code2 = string;
export type CreatedAtMs = number;
export type Id = string;
export type Level = 'info' | 'warn' | 'error';
export type Message2 = string;
export type Resolved = boolean;
export type SessionId1 = string | null;
export type ZipPath = string;
export type AppVersion = string;
export type Buffering = boolean;
export type DeviceId1 = string;
export type Model = string;
export type OfflineSinceMs = number | null;
export type Online = boolean;
export type Packets = number;
export type RateHz = number;
export type Role1 = string;
export type TelemetryAgeMs = number | null;
export type TrueHzAvg = number;
export type Blockers = Issue[];
export type Warnings = Issue[];
export type CamIdAtCapture = string;
export type DeviceKey4 = string;
export type File = string;
export type FrameArrivalMs = number;
export type Height1 = number;
export type Label2 = string;
export type SkewMs = number;
export type Width1 = number;
export type Cameras3 = ReferenceCamera[];
export type CreatedAtMs1 = number;
export type Name1 = string;
export type RefId2 = string;
export type TriggerMs = number;
export type Name2 = string | null;
export type ActiveRefId1 = string | null;
export type References = Reference[];
export type Name3 = string;
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ServerMessage".
 */
export type ServerMessage = SnapshotMsg | LiveMsg | FinalizeMsg | DriftMsg | AckMsg;
export type BuildId = string;
export type Cameras4 = Camera[];
export type LastSessionId = string | null;
export type Notices = Notice[];
export type Phones1 = Phone[];
export type ServerTimeMs = number;
export type Id1 = number;
export type Name4 = string;
export type SinceMs1 = number;
export type Operator2 = string;
export type ScheduledStartMs = number;
export type SessionId2 = string;
export type SessionTag2 = string;
export type StartedAtMs1 = number;
export type Subject3 = string;
export type State1 = 'IDLE' | 'RECORDING' | 'FINALIZING' | 'VALIDATING' | 'ERROR';
export type Type7 = 'snapshot';
export type Cameras5 = string[];
export type DurationS = number | null;
export type EndedAtMs1 = number | null;
export type Bytes = number;
export type Kind = string;
export type Name5 = string;
export type Files = SessionFile[];
export type Finalize = FinalizeStep[];
export type Folder = string;
export type IntegrityReport = {
  [k: string]: unknown;
} | null;
export type Late = LateRows[];
export type Notices1 = number;
export type Operator3 = string;
export type OutstandingUploads = number;
export type Phones2 = string[];
export type SessionId3 = string;
export type SessionTag3 = string;
export type StartedAtMs2 = number;
export type Subject4 = string;
export type Complete = boolean;
export type DeviceId2 = string;
export type ReceivedBytes = number;
export type Role2 = string;
export type TotalBytes = number | null;
export type Verified = boolean;
export type Uploads = UploadStatus[];
export type Verdict2 = 'PASS' | 'PARTIAL' | 'FAIL' | 'UNKNOWN' | 'NONE';
export type ZipBytes = number | null;
export type ZipPath1 = string | null;
export type Next = string | null;
export type Cameras6 = string[];
export type DurationS1 = number | null;
export type EndedAtMs2 = number | null;
export type Folder1 = string;
export type Notices2 = number;
export type Operator4 = string;
export type OutstandingUploads1 = number;
export type Phones3 = string[];
export type SessionId4 = string;
export type SessionTag4 = string;
export type StartedAtMs3 = number;
export type Subject5 = string;
export type Verdict3 = 'PASS' | 'PARTIAL' | 'FAIL' | 'UNKNOWN' | 'NONE';
export type ZipBytes1 = number | null;
export type ZipPath2 = string | null;
export type Sessions = SessionSummary[];
export type ScheduledStartMs1 = number;
export type SessionId5 = string;
export type Warnings1 = Issue[];
export type Reason = string | null;
export type SessionId6 = string;

/**
 * Generated from master_backend/app/api/schema.py. Do not edit by hand; run python -m master_backend.app.api.export_schema.
 */
export interface IMUCollectorDashboardAPI {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "AckMsg".
 */
export interface AckMsg {
  error?: ErrorBody | null;
  ok: Ok;
  req_id: ReqId;
  type?: Type;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ErrorBody".
 */
export interface ErrorBody {
  code: Code;
  details: Details;
  message: Message;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CalibrationState".
 */
export interface CalibrationState {
  active_ref_id: ActiveRefId;
  [k: string]: unknown;
}
/**
 * One selected camera slot (cam1..cam5).
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Camera".
 */
export interface Camera {
  cam_id: CamId;
  device_key: DeviceKey;
  drift: Drift | null;
  dropped_frames: DroppedFrames;
  error: Error;
  fps: Fps;
  height: Height;
  label: Label;
  recording: Recording;
  state: State;
  width: Width;
  [k: string]: unknown;
}
/**
 * Result of the calibration comparison against the active reference.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Drift".
 */
export interface Drift {
  at_ms: AtMs;
  confidence: Confidence;
  dx_px: DxPx;
  dy_px: DyPx;
  score: Score;
  shift_pct: ShiftPct;
  verdict: Verdict;
  [k: string]: unknown;
}
/**
 * A device the camera backend can open.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CameraDevice".
 */
export interface CameraDevice {
  backend: Backend;
  device_key: DeviceKey1;
  label: Label1;
  [k: string]: unknown;
}
/**
 * One selected device. ``cam_id`` is optional: slots are assigned in list order.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CameraSelectionItem".
 */
export interface CameraSelectionItem {
  cam_id?: CamId1;
  device_key: DeviceKey2;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CameraSelectionRequest".
 */
export interface CameraSelectionRequest {
  cameras: Cameras;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CameraSelectionResponse".
 */
export interface CameraSelectionResponse {
  cameras: Cameras1;
  [k: string]: unknown;
}
/**
 * Start receiving ``calibration.drift``. ``ref_id`` may be a real id or "active".
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "WatchMsg".
 */
export interface WatchMsg {
  ref_id: RefId;
  req_id?: ReqId1;
  type?: Type1;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "UnwatchMsg".
 */
export interface UnwatchMsg {
  req_id?: ReqId2;
  type?: Type2;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "PingMsg".
 */
export interface PingMsg {
  req_id: ReqId3;
  type?: Type3;
  [k: string]: unknown;
}
/**
 * A Drift verdict attributed to one camera.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CompareCamera".
 */
export interface CompareCamera {
  at_ms: AtMs1;
  cam_id: CamId2;
  confidence: Confidence1;
  device_key: DeviceKey3;
  dx_px: DxPx1;
  dy_px: DyPx1;
  score: Score1;
  shift_pct: ShiftPct1;
  verdict: Verdict1;
  [k: string]: unknown;
}
/**
 * Result of ``GET /api/calibration/compare`` and the ``calibration.drift`` message.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "CompareResult".
 */
export interface CompareResult {
  at_ms: AtMs2;
  cameras: Cameras2;
  ref_id: RefId1;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "DeviceList".
 */
export interface DeviceList {
  devices: Devices;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "DiskInfo".
 */
export interface DiskInfo {
  free_bytes: FreeBytes;
  path: Path;
  [k: string]: unknown;
}
/**
 * Server-side copy of the Setup form, so every browser shows the same values.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Draft".
 */
export interface Draft {
  operator: Operator;
  session_tag: SessionTag;
  subject: Subject;
  updated_at_ms: UpdatedAtMs;
  [k: string]: unknown;
}
/**
 * ``PUT /api/session/draft``. ``None`` means "keep the current value".
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "DraftUpdate".
 */
export interface DraftUpdate {
  operator?: Operator1;
  session_tag?: SessionTag1;
  subject?: Subject1;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "DriftMsg".
 */
export interface DriftMsg {
  data: CompareResult;
  type?: Type4;
  [k: string]: unknown;
}
/**
 * The documented ``{}`` body of dismiss / open-folder / activate / deactivate.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "EmptyResponse".
 */
export interface EmptyResponse {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ErrorResponse".
 */
export interface ErrorResponse {
  error: ErrorBody;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "FinalizeMsg".
 */
export interface FinalizeMsg {
  data: FinalizeProgress;
  type?: Type5;
  [k: string]: unknown;
}
/**
 * Finalize progress for one session; also the payload of the ``finalize`` WS message.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "FinalizeProgress".
 */
export interface FinalizeProgress {
  session_id: SessionId;
  steps: Steps;
  [k: string]: unknown;
}
/**
 * One step of the finalize job. ``detail`` is human-readable error/extra text.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "FinalizeStep".
 */
export interface FinalizeStep {
  detail: Detail;
  ended_at_ms: EndedAtMs;
  name: Name;
  started_at_ms: StartedAtMs;
  status: Status;
  [k: string]: unknown;
}
/**
 * One actionable problem or warning. ``subject`` identifies what it is about
 * (device_id, cam_id or a form field name).
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Issue".
 */
export interface Issue {
  code: Code1;
  message: Message1;
  subject?: Subject2;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LabelRequest".
 */
export interface LabelRequest {
  label_id: LabelId;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LabelResponse".
 */
export interface LabelResponse {
  label_id: LabelId1;
  since_ms: SinceMs;
  [k: string]: unknown;
}
/**
 * Rows that arrived inside the late window for one device.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LateRows".
 */
export interface LateRows {
  device_id: DeviceId;
  role: Role;
  rows: Rows;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LiveData".
 */
export interface LiveData {
  phones: Phones;
  t_ms: TMs;
  [k: string]: unknown;
}
export interface Phones {
  [k: string]: LiveSample;
}
/**
 * Latest IMU sample of one phone. ``acc`` is in g, ``gyro`` in deg/s.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LiveSample".
 */
export interface LiveSample {
  acc: Acc;
  gyro: Gyro;
  ts_ms: TsMs;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "LiveMsg".
 */
export interface LiveMsg {
  data: LiveData;
  type?: Type6;
  [k: string]: unknown;
}
/**
 * A server-side notice. Unresolved & undismissed notices are in the snapshot.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Notice".
 */
export interface Notice {
  code: Code2;
  created_at_ms: CreatedAtMs;
  id: Id;
  level: Level;
  message: Message2;
  resolved: Resolved;
  session_id: SessionId1;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "PackageResponse".
 */
export interface PackageResponse {
  zip_path: ZipPath;
  [k: string]: unknown;
}
/**
 * One connected phone and its health.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Phone".
 */
export interface Phone {
  app_version: AppVersion;
  buffering: Buffering;
  device_id: DeviceId1;
  model: Model;
  offline_since_ms: OfflineSinceMs;
  online: Online;
  packets: Packets;
  rate_hz: RateHz;
  role: Role1;
  telemetry_age_ms: TelemetryAgeMs;
  true_hz_avg: TrueHzAvg;
  [k: string]: unknown;
}
/**
 * What START needs (blockers) and what it merely records (warnings).
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Readiness".
 */
export interface Readiness {
  blockers: Blockers;
  warnings: Warnings;
  [k: string]: unknown;
}
/**
 * ``reference.json`` plus the URLs the dashboard renders thumbnails from.
 *
 * ``thumb_urls`` maps ``device_key`` to the full-resolution still URL
 * (``/api/calibration/references/<ref_id>/<key_slug>.jpg``); ``cameras[].file``
 * stays the on-disk filename from the frozen-ish calibration layout.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Reference".
 */
export interface Reference {
  cameras: Cameras3;
  created_at_ms: CreatedAtMs1;
  name: Name1;
  ref_id: RefId2;
  thumb_urls?: ThumbUrls;
  trigger_ms: TriggerMs;
  [k: string]: unknown;
}
/**
 * One still inside a calibration reference set (CALIBRATION.md).
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ReferenceCamera".
 */
export interface ReferenceCamera {
  cam_id_at_capture: CamIdAtCapture;
  device_key: DeviceKey4;
  file: File;
  frame_arrival_ms: FrameArrivalMs;
  height: Height1;
  label: Label2;
  skew_ms: SkewMs;
  width: Width1;
  [k: string]: unknown;
}
export interface ThumbUrls {
  [k: string]: string;
}
/**
 * ``POST /api/calibration/references``; the name is generated when absent.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ReferenceCreate".
 */
export interface ReferenceCreate {
  name?: Name2;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ReferenceList".
 */
export interface ReferenceList {
  active_ref_id: ActiveRefId1;
  references: References;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "ReferenceRename".
 */
export interface ReferenceRename {
  name: Name3;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SnapshotMsg".
 */
export interface SnapshotMsg {
  data: Snapshot;
  type?: Type7;
  [k: string]: unknown;
}
/**
 * The whole dashboard state. Always sent whole; the dashboard never patches.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "Snapshot".
 */
export interface Snapshot {
  build_id: BuildId;
  calibration: CalibrationState;
  cameras: Cameras4;
  disk: DiskInfo;
  draft: Draft;
  finalize: FinalizeProgress | null;
  last_session_id: LastSessionId;
  notices: Notices;
  phones: Phones1;
  readiness: Readiness;
  server_time_ms: ServerTimeMs;
  session: SessionInfo | null;
  state: State1;
  [k: string]: unknown;
}
/**
 * The session that is recording or finalising.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionInfo".
 */
export interface SessionInfo {
  label: SessionLabel;
  operator: Operator2;
  scheduled_start_ms: ScheduledStartMs;
  session_id: SessionId2;
  session_tag: SessionTag2;
  started_at_ms: StartedAtMs1;
  subject: Subject3;
  [k: string]: unknown;
}
/**
 * The label that is active right now.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionLabel".
 */
export interface SessionLabel {
  id: Id1;
  name: Name4;
  since_ms: SinceMs1;
  [k: string]: unknown;
}
/**
 * A SessionSummary plus everything the Done view shows.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionDetail".
 */
export interface SessionDetail {
  cameras: Cameras5;
  duration_s: DurationS;
  ended_at_ms: EndedAtMs1;
  files: Files;
  finalize: Finalize;
  folder: Folder;
  integrity_report: IntegrityReport;
  late: Late;
  notices: Notices1;
  operator: Operator3;
  outstanding_uploads: OutstandingUploads;
  phones: Phones2;
  session_id: SessionId3;
  session_tag: SessionTag3;
  started_at_ms: StartedAtMs2;
  subject: Subject4;
  uploads: Uploads;
  verdict: Verdict2;
  zip_bytes: ZipBytes;
  zip_path: ZipPath1;
  [k: string]: unknown;
}
/**
 * One file in the session folder, as listed in the Done/Sessions view.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionFile".
 */
export interface SessionFile {
  bytes: Bytes;
  kind: Kind;
  name: Name5;
  [k: string]: unknown;
}
/**
 * Phone rescue upload progress for one device.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "UploadStatus".
 */
export interface UploadStatus {
  complete: Complete;
  device_id: DeviceId2;
  received_bytes: ReceivedBytes;
  role: Role2;
  total_bytes: TotalBytes;
  verified: Verified;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionList".
 */
export interface SessionList {
  next: Next;
  sessions: Sessions;
  [k: string]: unknown;
}
/**
 * One row of the Sessions history (and the head of SessionDetail).
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "SessionSummary".
 */
export interface SessionSummary {
  cameras: Cameras6;
  duration_s: DurationS1;
  ended_at_ms: EndedAtMs2;
  folder: Folder1;
  notices: Notices2;
  operator: Operator4;
  outstanding_uploads: OutstandingUploads1;
  phones: Phones3;
  session_id: SessionId4;
  session_tag: SessionTag4;
  started_at_ms: StartedAtMs3;
  subject: Subject5;
  verdict: Verdict3;
  zip_bytes: ZipBytes1;
  zip_path: ZipPath2;
  [k: string]: unknown;
}
/**
 * ``POST /api/session/start`` — the body is empty today.
 *
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "StartRequest".
 */
export interface StartRequest {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "StartResponse".
 */
export interface StartResponse {
  scheduled_start_ms: ScheduledStartMs1;
  session_id: SessionId5;
  warnings: Warnings1;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "StopRequest".
 */
export interface StopRequest {
  reason?: Reason;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `IMUCollectorDashboardAPI`'s JSON-Schema
 * via the `definition` "StopResponse".
 */
export interface StopResponse {
  session_id: SessionId6;
  [k: string]: unknown;
}
