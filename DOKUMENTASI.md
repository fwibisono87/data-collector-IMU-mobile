# Dokumentasi Teknis — IMU Telemetry System

> Dokumen referensi teknis (gaya JavaDoc/API reference) untuk seluruh komponen kode dalam repositori ini.
> Ditulis dalam Bahasa Indonesia. Nama kelas, fungsi, variabel, dan istilah teknis kode tetap
> dalam bahasa aslinya (Inggris) agar dapat dicocokkan langsung dengan source code.

## Daftar Isi

1. [Ringkasan Proyek](#1-ringkasan-proyek)
2. [Arsitektur Sistem](#2-arsitektur-sistem)
3. [Kontrak Data Bersama (`shared_contracts/`)](#3-kontrak-data-bersama-shared_contracts)
4. [Backend — `master_backend/`](#4-backend--master_backend)
5. [Frontend — `master_frontend/`](#5-frontend--master_frontend)
6. [Aplikasi Mobile — `mobile_node/`](#6-aplikasi-mobile--mobile_node)
7. [Alat Bantu — `tools/device_simulator.py`](#7-alat-bantu--toolsdevice_simulatorpy)
8. [Alur Protokol End-to-End](#8-alur-protokol-end-to-end)
9. [Menjalankan Proyek](#9-menjalankan-proyek)
10. [Kode Usang / Tidak Terpakai](#10-kode-usang--tidak-terpakai)

---

## 1. Ringkasan Proyek

**IMU Telemetry System** adalah platform pengumpulan data multi-perangkat untuk merekam aliran
sensor inersia (**IMU** — *Inertial Measurement Unit*: akselerometer & giroskop) dari beberapa
ponsel secara bersamaan, dikoordinasikan dari satu dashboard operator secara *real-time*.

Sistem terbagi menjadi 4 komponen utama:

| Komponen | Teknologi | Peran |
|---|---|---|
| `mobile_node/` | Flutter (Dart) | Klien mobile: membaca sensor IMU bawaan ponsel, mengirim data ke backend, menyimpan sesi untuk pemulihan (*resume*) bila terputus |
| `master_backend/` | Python (FastAPI + WebSocket) | Hub WebSocket pusat: mesin status sesi (*state machine*), deduplikasi paket, penulisan CSV, audit log, mDNS discovery |
| `master_frontend/` | Next.js (TypeScript/React) | Dashboard operator: koneksi, *preflight check*, grafik langsung, pelabelan aktivitas, laporan integritas, rekaman multi-kamera |
| `shared_contracts/` | Protobuf (`.proto`) | Skema data bersama lintas backend, frontend, dan mobile |

Alat bantu tambahan: `tools/device_simulator.py` — mensimulasikan satu perangkat mobile untuk
pengujian tanpa ponsel fisik.

---

## 2. Arsitektur Sistem

```
        ┌──────────────┐   Protobuf/WS    ┌──────────────────┐
        │  Mobile Node │ ───────────────▶ │                  │
        │  (Flutter)   │ ◀─────────────── │  Master Backend  │
        └──────────────┘   commands       │    (FastAPI)     │
        ┌──────────────┐                  │  WS hub · state  │
        │  Mobile Node │ ───────────────▶ │  machine · audit │
        │  (Flutter)   │ ◀─────────────── │                  │
        └──────────────┘                  └────────┬─────────┘
              ...  (n perangkat, mDNS discovery)    │ WebSocket (JSON)
                                                    ▼
                                          ┌──────────────────┐
                                          │  Master Frontend │
                                          │    (Next.js)     │
                                          │  dashboard live  │
                                          └──────────────────┘
```

Empat *endpoint* WebSocket yang diekspos backend (semua di-*mount* dari `ws_handler.router`):

| Endpoint | Arah | Format | Fungsi |
|---|---|---|---|
| `/ws/control` | dua arah | biner protobuf (`Command`) | registrasi perangkat, PING/PONG, CLOCK_SYNC, START/STOP sesi, SET_LABEL |
| `/ws/telemetry` | perangkat → backend | biner protobuf (`SensorPacket`) | aliran sampel IMU |
| `/ws/frontend` | dua arah | teks JSON | kontrol sesi & status dari dashboard |
| `/ws/live` | backend → dashboard | teks JSON | umpan grafik langsung (~20fps) berisi sampel terbaru per perangkat |

Mesin status sesi (*session state machine*) yang dijalankan backend:

```
IDLE → PREFLIGHT → READY → RECORDING → FINALIZING → VALIDATING → IDLE
                                  (dapat menuju ERROR via abort())
```

---

## 3. Kontrak Data Bersama (`shared_contracts/`)

Direktori ini berisi definisi skema `.proto` yang menjadi **sumber kebenaran tunggal** (*single
source of truth*) untuk format data yang dipertukarkan backend ↔ frontend ↔ mobile. File
`Makefile` menyediakan target `make proto` untuk meregenerasi binding Dart & Python dari file ini
via `protoc`.

### `sensor_packet.proto`

**`message SensorPacket`** — satu sampel data IMU.

| Field | Tipe | Keterangan |
|---|---|---|
| `acc_x`, `acc_y`, `acc_z` | float | Percepatan, satuan **g** |
| `gyro_x`, `gyro_y`, `gyro_z` | float | Kecepatan sudut, satuan **deg/s** |
| `timestamp_ms` | int64 | Unix epoch ms, **sudah dikoreksi** *clock offset* |
| `sequence_number` | uint64 | Nomor urut monoton per `(device_id, session_id)` |
| `device_id` | string | UUID stabil, disimpan di `SharedPreferences` klien |
| `schema_version` | uint32 | Versi skema saat ini = 1 |
| `raw_timestamp_ms` | int64 | Waktu jam ponsel **sebelum** dikoreksi (untuk audit) |

**`message DeviceRegister`** — dikirim sekali oleh klien saat membuka kanal kontrol.

| Field | Tipe | Keterangan |
|---|---|---|
| `device_id` | string | UUID perangkat |
| `device_role` | string | Lokasi pemasangan: `"chest"`, `"waist"`, `"thigh_left"`, dst. |
| `device_model`, `android_version`, `app_version` | string | Metadata perangkat |
| `schema_version` | uint32 | Versi skema |

### `commands.proto`

**`enum CommandType`**: `PING=0`, `PONG=1`, `START_SESSION=2`, `STOP_SESSION=3`, `SET_LABEL=4`,
`ACK=5`, `CLOCK_SYNC=6`, `ERROR_ALERT=7`.

**`message Command`** — amplop perintah generik lintas kanal kontrol.

| Field | Tipe | Keterangan |
|---|---|---|
| `type` | CommandType | Jenis perintah |
| `payload` | string | **String JSON**, skemanya tergantung `type` |
| `issued_at_ms` | int64 | Waktu perintah dibuat (Unix epoch ms) |
| `command_id` | string | UUID untuk mencocokkan permintaan dengan `ACK` |

### `CHANGELOG.md` — Aturan Perubahan Skema

- `schema_version = 1` (2026-04-19): definisi awal.
- **Aturan bump versi**: menambah *field* opsional → tidak perlu bump; mengubah makna *field* →
  bump minor + dokumentasikan; menghapus/mengganti nama *field* → bump major + sediakan alat
  migrasi.

> **Catatan implementasi:** `master_backend/proto/*.py` dan `mobile_node/lib/models/proto/*.pb.dart`
> **bukan** hasil generate `protoc`, melainkan implementasi manual format *wire* protobuf yang
> ditulis tangan agar identik dengan skema di atas. Keduanya diberi komentar eksplisit di kode
> sebagai pengganti sementara sampai `make proto` benar-benar dijalankan.

---

## 4. Backend — `master_backend/`

Aplikasi **FastAPI** yang bertindak sebagai *hub* pusat. Harus dijalankan dari **root
repositori** karena menggunakan *absolute import* `master_backend.*` (lihat
`pyproject.toml`: root paket adalah satu level di atas `master_backend/`).

### 4.1 `app/main.py` — Entry Point Aplikasi

Titik masuk FastAPI. Memuat `.env` via `load_dotenv()` saat modul di-*import*.

- **`lifespan(app)`** *(async context manager)*
  Menangani siklus hidup aplikasi.
  - **Saat startup**: memanggil `_ensure_dirs()`, `_start_audit()`, `_start_mdns()`,
    `_check_interrupted_sessions()`; menjalankan *background task* `_live_broadcaster_loop()`
    dan `session_manager.run_idle_reaper()`.
  - **Saat shutdown**: `audit.close()`, `_stop_mdns()`.
- **`app = FastAPI(title="IMU Telemetry Backend", version="2.0.0", lifespan=lifespan)`**
  CORS diatur `allow_origins=["*"]` (komentar kode: "LAN yang sama, perketat via `LAN_SUBNET`
  bila perlu" — namun `LAN_SUBNET` dari `.env` **belum benar-benar dipakai** di sini).
- **`GET /health`** — Mengembalikan `{status, version, session_state, session_id, online_devices}`.
- **`GET /session`** — Mengembalikan status sesi & daftar perangkat online saat ini.
- **`_ensure_dirs()`** — Membuat direktori `SSD_PATH` & `RESCUE_PATH` jika belum ada.
- **`_start_audit()`** *(async)* — Membuka audit log di `SSD_PATH/backend_audit.jsonl`.
- **`_check_interrupted_sessions()`** — Memeriksa file status sesi (`.sessions/*.state.json`)
  yang tertinggal dalam state `RECORDING`/`FINALIZING` akibat *crash*, lalu memberi peringatan
  di log untuk pemulihan manual via `/session`.
- **`_start_mdns()` / `_stop_mdns()`** — Mendaftarkan/mencabut layanan **mDNS**
  (`_imu-telemetry._tcp.local.`, nama `IMU Backend`) di jaringan lokal agar aplikasi Flutter
  bisa menemukan IP backend secara otomatis. Dibungkus `try/except` agar kegagalan mDNS tidak
  menggagalkan startup.
- **`_get_local_ip()`** — Menemukan IP LAN lokal lewat trik *socket* UDP (`connect` ke
  `8.8.8.8:80`, baca `getsockname()`).

### 4.2 `app/session_manager.py` — Mesin Status Sesi

Modul inti yang mengelola *state machine* sesi perekaman dan status tiap perangkat.

**Konstanta**: `_DEVICE_OFFLINE_SEC = 8.0` (batas waktu tanpa PING sebelum perangkat dianggap
offline — cocok dengan `_pongTimeoutSec` di sisi Flutter), `_COORDINATED_START_LEAD_MS = 500`
(jeda ms sebelum waktu mulai rekaman terkoordinasi).

- **`class SessionState(str, Enum)`**: `IDLE, PREFLIGHT, READY, RECORDING, FINALIZING,
  VALIDATING, ERROR`.
- **`class DeviceSubstate(str, Enum)`**: `CONNECTED, RECORDING, FINALIZED, DISCONNECTED`.
- **`@dataclass DeviceInfo`** — Catatan *runtime* per perangkat: `device_id, device_role,
  device_model, app_version, control_ws, last_ping_ms, is_online, packets_received, substate,
  first_packet_ts, offline_intervals`.
  - **`is_alive`** *(property)* — `True` jika `is_online` dan `last_ping_ms` kurang dari 8 detik
    yang lalu.

**`class SessionManager`** — kelas utama, satu *singleton* per proses (`session_manager`).

| Metode | Deskripsi |
|---|---|
| `register_device(device_id, role, model, app_version, ws)` | Mendaftarkan perangkat baru; **menolak** peran (*role*) yang bentrok dengan perangkat lain yang sedang terhubung (kecuali diawali `"custom:"`). Saat *reconnect*, mempertahankan `offline_intervals`/`first_packet_ts`/`packets_received` lama. |
| `unregister_device(device_id)` | Menandai perangkat offline, menutup `control_ws`; jika sesi sedang `RECORDING`, mencatat interval offline baru. |
| `note_telemetry_disconnect(device_id)` | Mencatat putusnya **kanal telemetri saja** (tanpa menyentuh siklus hidup kanal kontrol) — penting agar laporan integritas tetap menangkap celah data walau kanal kontrol tetap hidup. |
| `mark_ping(device_id)` | Dipanggil saat menerima `PING`; menutup interval offline terbuka & memperbarui `last_ping_ms`. |
| `mark_first_packet(device_id, ts)` | Mencatat *timestamp* paket pertama (dipakai untuk cek selisih waktu mulai antar-perangkat) dan mengubah *substate* menjadi `RECORDING`. |
| `quorum_ok()` | Mengembalikan `(bool, alasan)` — `True` jika minimal 1 perangkat terhubung. |
| `start_recording(payload)` *(async)* | Memulai sesi: hanya valid dari `PREFLIGHT/READY/IDLE`; memerlukan kuorum; membuat `session_id` (epoch ms dalam string); menetapkan `scheduled_start_ms = now + 500ms` untuk sinkronisasi mulai rekaman; memanggil `io_manager.open_session(...)`; membersihkan `dedup`; berpindah state ke `RECORDING`; menyimpan status untuk pemulihan *crash*. |
| `stop_recording(reason)` *(async)* | Menghentikan sesi: menyiarkan `STOP_SESSION` ke semua perangkat **sebelum** menutup berkas → `FINALIZING` → menutup semua *writer* CSV via `io_manager.close_session()` → `VALIDATING` → menjalankan `IntegrityValidator` → kembali ke `IDLE`. Mengembalikan laporan integritas. |
| `abort(reason)` *(async)* | Menghentikan paksa akibat kesalahan; berpindah ke state `ERROR`. |
| `broadcast_control(data)` | Mengirim data biner ke semua perangkat online melalui `control_ws` masing-masing. |
| `send_to_device(device_id, data)` | Mengirim data biner ke satu perangkat tertentu. |
| `_save_state()` / `_clear_state()` | Menyimpan/menghapus *snapshot* sesi di `SSD_PATH/.sessions/{session_id}.state.json` untuk pemulihan pasca-*crash*. |
| `get_interrupted_sessions()` | Memindai berkas status sesi yang tertinggal dalam `RECORDING`/`FINALIZING`. |
| `_monitor_offline()` | *Background loop* selama `RECORDING`: tiap 1 detik memeriksa `is_alive` tiap perangkat; menandai offline & mencatat interval bila tidak ada PING &gt;8 detik, lalu menyiarkan status baru ke dashboard. |
| `run_idle_reaper()` | *Background loop* permanen (dijalankan sekali dari `main.py`): selama state `IDLE`, tiap 1 detik membersihkan perangkat yang `control_ws`-nya `None` atau berhenti PING, agar kartu perangkat basi hilang dari dashboard tanpa perlu *restart* backend. |

### 4.3 `app/ws_handler.py` — Endpoint WebSocket

Mendefinisikan `router = APIRouter()` yang di-*mount* di `main.py`, serta 4 *endpoint* WebSocket.

- **`_frontend_connections`**, **`_latest_samples`** — status modul: set koneksi dashboard dan
  *cache* sampel terbaru per perangkat (untuk `/ws/live`).
- **`drop_latest_sample(device_id)`** — Menghapus sampel *cache* perangkat yang dibersihkan
  *idle reaper*.
- **`broadcast_to_frontends(msg)`** *(async)* — Serialisasi JSON & kirim ke semua dashboard yang
  terhubung; membersihkan koneksi mati.

**`telemetry_ws(websocket)`** — endpoint `/ws/telemetry`. Kanal satu arah (perangkat → backend).
Alur: terima koneksi tanpa perlu registrasi → baca *frame* biner → *parse* sebagai `SensorPacket`
(frame rusak dilewati, tidak fatal) → jika `schema_version != 1` dilewati dengan peringatan → jika
state bukan `RECORDING`, paket dibuang → cek duplikat via `dedup.is_duplicate(...)` → jika bukan
duplikat: `dedup.add(...)`, `session_manager.increment_packets(...)`,
`session_manager.mark_first_packet(...)`, lalu `io_manager.write_packet(pkt)`. Saat terputus:
memanggil `note_telemetry_disconnect` (**bukan** `unregister_device`, agar dashboard tidak
salah menampilkan 0 perangkat setelah rekaman berhenti).

**`control_ws(websocket)`** — endpoint `/ws/control`. Kanal dua arah. *Frame* biner pertama
**wajib** `DeviceRegister`; jika tidak valid → tutup koneksi kode `4001`. Jika peran bentrok →
kirim `ERROR_ALERT` lalu tutup kode `4002`. Selanjutnya membaca `Command` biner secara berulang
dan mendelegasikan ke `_handle_command`.

**`_handle_command(cmd, device_id, ws)`** — dispatcher berdasarkan `cmd.type`:

| Tipe Perintah | Perilaku |
|---|---|
| `PING` | `session_manager.mark_ping(...)`, balas `PONG`. |
| `CLOCK_SYNC` | Pertukaran 3-*timestamp* gaya NTP: baca `t0_ms` dari klien, hitung `t1_ms`/`t2_ms` di server, balas ketiganya untuk koreksi *clock offset* klien. |
| `START_SESSION` | Memvalidasi & memulai sesi via `session_manager.start_recording`; jika sukses, menyiarkan `START_SESSION` ke semua perangkat lain. |
| `STOP_SESSION` | Hanya valid saat `RECORDING`; memanggil `session_manager.stop_recording(reason)`. |
| `SET_LABEL` | Mengubah label aktif via `io_manager.set_label(...)` (berlaku untuk baris berikutnya, **bukan** retroaktif). |
| `ACK` | (ditangani terpisah untuk pencocokan `command_id`.) |
| lainnya | Dicatat di log level *debug*. |

**`frontend_ws(websocket)`** — endpoint `/ws/frontend`. Protokol JSON (bukan biner) untuk
dashboard. Saat terhubung, langsung mengirim `_state_snapshot()`. Dispatch pesan via
`_handle_frontend_msg`: mendukung `START_SESSION`, `STOP_SESSION` (menyiarkan `STATE_UPDATE`
berisi `integrity_report` lengkap setelah selesai), `SET_LABEL`, `GET_STATE`.

**`_state_snapshot()`** — Membangun payload status kanonik untuk dashboard: `state, session_id,
subject, session_tag, operator, devices[], quorum`.

**`live_ws(websocket)`** — endpoint `/ws/live`. Satu arah (backend → dashboard), hanya menunggu
koneksi tetap terbuka.

**`_live_broadcaster_loop()`** — *Background task*, setiap 50ms (~20fps) menyiarkan
`_latest_samples` (dengan `NaN`/`Inf` disanitasi via `_safe_float`) ke semua koneksi `/ws/live`.

### 4.4 `app/io_manager.py` — Penulisan CSV & Fallback Penyimpanan

*Writer* CSV asinkron dengan mekanisme *fallback* SSD → *rescue path*.

**Konstanta**: `_FSYNC_INTERVAL = FSYNC_INTERVAL_SEC` (default 5 detik), `_CSV_HEADER` =
`timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,label_id,label_name,
sequence_number,device_id`.

- **`class DeviceWriter`** — mengelola satu berkas CSV terbuka untuk satu perangkat.
  - `open()` — membuat direktori induk, menulis baris metadata + header CSV.
  - `write_row(row)` — menulis baris; melakukan `fsync` (di *executor* terpisah agar tidak
    memblokir *event loop*) setiap `FSYNC_INTERVAL_SEC` detik.
  - `close()` — *flush*, `fsync`, tutup berkas, hitung **SHA-256**; mengembalikan
    `{"path", "rows", "sha256"}`.
- **`class IoManager`** — manajer tingkat atas (*singleton* `io_manager`).
  - `set_label(label_id, label_name)` — mengatur label aktif untuk baris berikutnya.
  - `open_session(session_id, subject_name, session_tag, operator, device_roles)` *(async)* —
    membuat folder `SSD_PATH/Data_Riset_IMU/{subject}_{tag}`, membuka `DeviceWriter` per
    perangkat dengan nama berkas `{session_id}_{role}_sensor_data.csv`. Bila SSD gagal
    (`OSError`), otomatis beralih ke `RESCUE_PATH` dengan sufiks `_rescue.csv`.
  - `write_packet(pkt)` *(async)* — menulis satu baris CSV untuk paket sensor tertentu; jika
    gagal menulis ke *writer* utama, mencoba ke *writer rescue*.
  - `close_session()` *(async)* — menutup semua *writer* (utama & *rescue*), mengembalikan
    `{device_id: {"path","rows","sha256"}}`.
  - `_sha256(path)` — utilitas hash SHA-256 secara *streaming* (blok 64KB).

### 4.5 `app/dedup_store.py` — Deduplikasi Paket

**`class DedupStore`** — deduplikasi berbasis nomor urut, kunci `(device_id, session_id,
sequence_number)`.

| Metode | Deskripsi |
|---|---|
| `is_duplicate(device_id, session_id, seq)` | Mengecek apakah kombinasi kunci sudah pernah dilihat. |
| `add(device_id, session_id, seq)` | Menandai kombinasi kunci sebagai sudah dilihat. |
| `clear()` | Mengosongkan set (dipanggil saat sesi mulai/berhenti/dibatalkan). |
| `size` *(property)* | Jumlah entri yang tersimpan. |

*Singleton* modul: `dedup = DedupStore()`.

### 4.6 `app/integrity_validator.py` — Validasi Pasca-Sesi

**`class IntegrityValidator`**

- **`run(session_id, file_results, devices, scheduled_start_ms=0)`** *(async)* — Menjalankan
  serangkaian pemeriksaan setelah sesi selesai:
  - **Per perangkat**: status `FAIL` jika `rows == 0`; status `PARTIAL` jika ada
    `offline_intervals` tercatat.
  - **Lintas perangkat** (bila &gt;1 perangkat & `scheduled_start_ms` tersedia):
    - `max_start_drift_ms` — selisih maksimum `first_packet_ts` antar perangkat.
    - `start_drift_ok` — `True` bila selisih ≤ **100ms** (ambang batas wajib sesuai spesifikasi).
    - `all_devices_completed` — `True` bila semua perangkat punya baris data.
    - Pemeriksaan **keunikan peran** (`role_uniqueness`) — selalu dijalankan.
  - Menulis laporan ke `{session_id}_integrity_report.json` di direktori CSV yang sama;
    mengembalikan `dict` laporan.

### 4.7 `app/audit_logger.py` — Audit Log Terstruktur

**`class AuditLogger`** — log JSONL per proses, "tidak pernah menelan kesalahan — gagal secara
nyaring bila penulisan gagal".

| Metode | Deskripsi |
|---|---|
| `open(path)` *(async)* | Membuka berkas log (mode *append*), memulai *task* *flush* berkala. |
| `log(level, event, detail=None)` *(async)* | Menambahkan satu baris JSON `{ts_ms, level, event, detail}` ke *buffer*; level `ERROR` langsung di-*flush*. |
| `_flush()` *(async)* | Menulis *buffer* ke disk + `flush()` berkas. |
| `_periodic_flush()` *(async)* | Loop tiap 1 detik memanggil `_flush()`. |
| `close()` *(async)* | Membatalkan *task flush*, *flush* sisa *buffer*, tutup berkas. |

*Singleton* modul: `audit = AuditLogger()`. Log path: `SSD_PATH/backend_audit.jsonl`.

### 4.8 `proto/commands.py` & `proto/sensor_packet.py`

Implementasi manual format *wire* protobuf (varint, *wire type* 0/2/5) yang mencerminkan
`shared_contracts/*.proto` — **bukan** hasil `protoc`, secara eksplisit ditandai sebagai
pengganti sementara di komentar kode.

- **`class CommandType(IntEnum)`** — mencerminkan `enum CommandType` di proto.
- **`@dataclass Command`** — `from_bytes()`/`to_bytes()` untuk *encode/decode* biner.
- Fungsi pembangun perintah: `make_pong(command_id)`, `make_ack(command_id, status, detail)`,
  `make_error_alert(code, detail)`, `make_clock_sync_response(command_id, t0_ms, t1_ms, t2_ms)`.
- **`@dataclass SensorPacket`** — `from_bytes()` untuk *parsing* paket biner masuk (backend tidak
  pernah mengirim `SensorPacket`, hanya menerima).
- **`@dataclass DeviceRegister`** — `from_bytes()`; properti `is_valid` = `bool(device_id and
  device_role)`.

### 4.9 `run.py`, `pyproject.toml`, `requirements.txt`, `.env.example`

- **`run.py`** — *runner* pengembangan yang bekerja dari direktori manapun: menghitung
  `REPO_ROOT`, menambahkannya ke `sys.path` **dan** *environment variable* `PYTHONPATH` (penting
  karena `uvicorn --reload` memunculkan *interpreter* baru tiap *reload* yang hanya mewarisi
  *environment*, bukan `sys.path` proses induk). Menjalankan
  `uvicorn.run("master_backend.app.main:app", host="0.0.0.0", port=8000, reload=True)`.
- **`pyproject.toml`** — nama paket `imu-telemetry-backend` v2.0.0, `requires-python >= 3.11`;
  root paket satu level di atas `master_backend/` (root repo) — sebab wajib dijalankan dari root.
- **`requirements.txt`** — `fastapi`, `uvicorn[standard]`, `websockets`, `protobuf` (disiapkan
  untuk *codegen* masa depan), `aiofiles`, `python-dotenv`, `zeroconf` (mDNS), `paramiko` (SSH,
  untuk fitur "FAMS" tahap mendatang, belum dipakai), `scipy` (belum dirujuk di kode saat ini).
- **`.env.example`** — variabel konfigurasi:

| Variabel | Contoh | Keterangan |
|---|---|---|
| `SSD_PATH` | `D:/IMU_Data_SSD` | Direktori utama penyimpanan CSV & audit log |
| `RESCUE_PATH` | `D:/IMU_Data_Rescue` | Direktori cadangan bila SSD gagal ditulis |
| `BIND_HOST` | `0.0.0.0` | Alamat *bind* jaringan |
| `PORT` | `8000` | Port server |
| `LAN_SUBNET` | `192.168.1.0/24` | Didokumentasikan untuk pembatasan CORS, **belum benar-benar dipakai** |
| `FSYNC_INTERVAL_SEC` | `5` | Interval `fsync` per `DeviceWriter` |
| `MAX_CONCURRENT_DEVICES` | `8` | Didokumentasikan, **belum ditegakkan** di logika kuorum |
| `FAMS_HOST/PORT/USER/KEY_PATH/REMOTE_DIR` | *(kosong)* | Disiapkan untuk fitur sinkronisasi jarak jauh via SSH (tahap mendatang), belum dipakai |

---

## 5. Frontend — `master_frontend/`

Aplikasi **Next.js 14 (App Router) + React 18 + TypeScript**, dashboard operator *client-heavy*
(hampir semua berkas berawalan `"use client"`). Hanya berkomunikasi dengan backend lewat **dua
koneksi WebSocket mentah** (tidak ada *REST API*). Hanya terdapat **satu rute**: `src/app/page.tsx`
sebagai halaman utama (`/`). Tidak ada manajemen status global (Redux/Zustand) — seluruh status
disimpan lokal via `useState`/`useRef` di `page.tsx` dan diturunkan sebagai *props*.

### 5.1 `src/app/layout.tsx` & `globals.css`

- **`layout.tsx`** — *root layout*: metadata halaman (`title: "IMU Telemetry Dashboard"`), font
  `GeistSans`, impor `globals.css`. Tidak ada *provider* global.
- **`globals.css`** — mendefinisikan bahasa visual "*glassmorphism*" lewat variabel CSS
  (`--glass-*`, `--accent*`) dan kelas komponen (`glass-panel`, `glass-rail`, `glass-card`,
  `glass-input`, `btn-primary/success/danger/glass`) yang dipakai hampir semua komponen.

### 5.2 `src/app/page.tsx` — Komponen `Home()` (Seluruh Aplikasi)

Satu komponen *default export* ini adalah keseluruhan aplikasi dashboard.

**Status lokal utama**: `view` (`"connect"|"dashboard"`), `backendIp`, `isWsConnected`,
`sessionState`, `sessionId`, `devices`, `quorum`, `integrityReport`, field formulir sesi
(`subject`, `sessionTag`, `operator`), *buffer* grafik langsung (`liveSamples`), `activeLabel`,
status kamera (`camStatus`, `camRef`), dan `isStopping` (*guard* anti klik ganda).

**Nilai turunan**: `isRecording = sessionState === "RECORDING"`; `onlineCount` dihitung langsung
dari `devices` (bukan `quorum.connected`, dijadikan *"single source of truth"*);
`prefightAllPass` — gerbang gabungan (koneksi WS + ≥1 perangkat online + formulir terisi +
kamera siap) yang mengaktifkan tombol MULAI.

**Fungsi/handler utama**:

| Fungsi | Deskripsi |
|---|---|
| `useEffect` (langganan WS) | Berlangganan `wsClient.onMessage/onLive/onConnectionChange`. Pada `STATE_UPDATE`, selalu menimpa `devices` (bahkan dengan array kosong) agar kartu perangkat basi/backend-*restart* langsung bersih. Mengimplementasikan **mulai rekaman terkoordinasi**: jika status baru `RECORDING` dan pesan membawa `scheduled_start_ms`, menghitung `delay` lalu memanggil `camRef.current.startRecording(sessionId)` via `setTimeout` — menyinkronkan mulai rekam video di semua browser dengan jam yang ditentukan backend. |
| `useEffect` (auto-reconnect) | Saat *mount*, membaca `backendIp` dari `localStorage`, otomatis `wsClient.connect`, memoll status koneksi hingga 5 detik untuk langsung lompat ke dashboard. |
| `handleConnect()` | Menyambungkan ke `backendIp`, menyimpan IP ke `localStorage` bila sukses, pindah ke `view="dashboard"`; *timeout* 5 detik. |
| `handleStart()` | Mereset `integrityReport`/`activeLabel`/`labelError`, memanggil `wsClient.startSession(...)`. **Tidak** langsung memulai kamera — ditunda ke mekanisme `scheduled_start_ms`. |
| `handleStop()` | Dijaga oleh `isStopping`. Urutan: (1) `camRef.current.stopRecording()` mengumpulkan video dari semua kamera; (2) `wsClient.stopSession(...)` dipanggil **sebelum** proses unduh (agar *hang* unduhan browser tidak membuat sesi backend macet di `RECORDING`); (3) mengunduh tiap video (jeda 350ms antar-unduhan agar tidak diblokir browser); (4) membuat & mengunduh manifest `{sessionId}_cameras.json`; (5) memperingatkan operator bila ada kamera yang tidak menghasilkan rekaman. |
| `handleLabel(id)` | Memanggil `wsClient.setLabel(id)`. |
| `_downloadBlob(blob, name)` | Fungsi bantu tingkat modul — membuat *object URL*, memicu unduhan via elemen `<a download>`, lalu mencabut URL. |

**Tata letak render**: layar koneksi (input IP + tombol Connect) vs. dashboard 3 kolom —
sisi kiri (`SessionForm`, `DevicePanel`, `PreflightPanel`, tombol MULAI/BERHENTI, tombol
Disconnect), tengah (`RealtimeChart`, `LabelingPanel`, `IntegrityReport`), kanan
(`MultiCameraRecorder`).

> **Catatan implementasi**: `MultiCameraRecorder` sengaja diimpor langsung (bukan lewat
> `next/dynamic`) karena `dynamic()` merusak `forwardRef` sehingga `camRef.current` menjadi
> `null`.

### 5.3 `src/lib/ws_client.ts` — Klien WebSocket (Satu-satunya Titik Integrasi Backend)

Mengekspor *singleton* `export const wsClient = new WsClient()`.

**Dua koneksi** (port `8000` pada `backendIp` yang dimasukkan operator):
- `ws://{ip}:8000/ws/frontend` — kanal kontrol: protokol perintah/ACK JSON + pesan `STATE_UPDATE`.
- `ws://{ip}:8000/ws/live` — kanal telemetri langsung: bingkai `{samples: {...}}`, tanpa ACK.

**Tipe utama**: `SessionState`, `DeviceInfo`, `StateUpdate`, `AckMsg`, `FrontendMsg`.

| Metode Publik | Deskripsi |
|---|---|
| `connect(ip)` / `disconnect()` | Membuka/menutup kedua *socket*. |
| `isConnected` *(getter)* | `True` bila `controlWs.readyState === OPEN`. |
| `onMessage(cb)` / `onLive(cb)` / `onConnectionChange(cb)` | Pola *pub/sub*; masing-masing mengembalikan fungsi *unsubscribe*. |
| `startSession(subject, tag, operator)` | Mengirim perintah `START_SESSION` dengan ACK. |
| `stopSession(reason)` | Mengirim `STOP_SESSION`. |
| `setLabel(labelId)` | Mengirim `SET_LABEL`. |
| `getState()` | Mengirim `GET_STATE` tanpa menunggu ACK; dipanggil setiap kali (re)connect untuk memaksa *snapshot* segar. |

**Mekanisme keandalan**:
- `_sendWithAck` — membungkus perintah `{type, payload, command_id}`, *timeout* 2 detik
  (`ACK_TIMEOUT_MS`), mengulang hingga `ACK_MAX_RETRIES = 3` kali dengan `command_id` yang sama.
- `_resolveAck` — mencocokkan pesan `ACK` masuk dengan `pendingAcks` berdasarkan `command_id`.
- Kedua *socket* otomatis *reconnect* 3 detik setelah tertutup; `onopen` langsung memanggil
  `getState()` untuk menyinkronkan ulang status setelah putus/*restart* backend.

### 5.4 `src/lib/video_backup.ts` — Penyimpanan Cadangan Video via IndexedDB

Lapisan ketahanan terhadap *crash* browser untuk fitur rekaman video. Menggunakan database
IndexedDB `imu-video-backup` / *store* `chunks`, kunci komposit
`{sessionId}__{camId}__{index}` (via `chunkKey`) agar potongan dari hingga 5 kamera tidak
bertabrakan.

| Fungsi | Deskripsi |
|---|---|
| `saveChunk(sessionId, camId, index, blob)` | Menyimpan satu potongan (*chunk*) video. |
| `loadChunks(sessionId, camId)` | Membaca & mengurutkan semua potongan berdasarkan `index`, mengembalikan `Blob[]` untuk disatukan menjadi video akhir. |
| `clearChunks(sessionId, camId?)` | Menghapus rekaman yang cocok; tanpa `camId` menghapus semua kamera sesi tsb. |
| `clearAllChunks()` | Mengosongkan seluruh *store*; dipanggil di **awal** sesi baru (bukan saat berhenti) agar rekaman sesi *sebelumnya* tetap bisa dipulihkan sampai sesi baru dimulai. |
| `listPendingCameras(sessionId)` / `hasPendingChunks(sessionId)` | Bantuan pemulihan pasca-*crash* — mendeteksi potongan tersisa dari sesi yang terinterupsi (belum dihubungkan ke UI mana pun). |

### 5.5 `src/lib/roleColors.ts`

`ROLE_HEX: Record<string,string>` — memetakan peran/lokasi sensor (`chest`, `waist`,
`thigh_left/right`, `ankle_left/right`, `wrist_left/right`) ke warna aksen heksadesimal, plus
`ROLE_HEX_FALLBACK` dan fungsi bantu `roleHex(role)`. Dipakai `RealtimeChart.tsx` untuk mewarnai
kartu/legenda tiap perangkat.

### 5.6 Komponen (`src/components/`)

| Komponen | Deskripsi |
|---|---|
| **`StatusBanner`** | Panel atas: status sesi berwarna (merah berkedip untuk `RECORDING`), ID sesi terpotong, indikator koneksi WS, jumlah perangkat online, total paket langsung, daftar chip status per perangkat. |
| **`SessionForm`** | Tiga input teks terkontrol (`subject`, `sessionTag`, `operator`), terkunci saat `isRecording`. |
| **`DevicePanel`** | Daftar perangkat di sisi kiri: lencana kuorum, kartu per perangkat (warna sesuai peran) berisi status online, `MiniSparkline`, jumlah paket, peringatan celah offline. |
| **`MiniSparkline`** *(internal `DevicePanel`)* | Grafik mini berbasis `<canvas>` — menghitung magnitudo vektor akselerometer (`√(x²+y²+z²)`), menyimpan 60 sampel terakhir, digambar ulang tiap perubahan `samples`. |
| **`PreflightPanel`** + fungsi murni `buildChecks(...)` | Menghasilkan 6 pemeriksaan kesiapan: backend terhubung, ≥1 perangkat online, formulir terisi, kamera siap. Diekspor terpisah agar mudah diuji unit. |
| **`LabelingPanel`** | Grid 51 tombol label (`0`–`50`, label `0` = *baseline*); aktif hanya saat `isRecording`. |
| **`IntegrityReport`** | Menampilkan hasil validasi pasca-sesi dari backend: status keseluruhan, waktu validasi, rincian per perangkat (jumlah baris, hash SHA-256 CSV terpotong, catatan masalah). |
| **`AmbientBackdrop`** | Latar dekoratif dengan gradien radial yang warnanya berubah sesuai status sesi; menghormati preferensi `prefers-reduced-motion`. |
| **`MultiCameraRecorder`** | Komponen paling kompleks — lihat detail di bawah. |
| **`RealtimeChart`** | Grafik ECharts langsung — lihat detail di bawah. |

#### `MultiCameraRecorder.tsx` — Rekaman Multi-Kamera Tersinkron

Tipe yang diekspor: `CameraResult {camId, deviceId, label, blob, mime}`, `CameraStatus {ready,
total, ok}`, `StopOutcome {results, missed}`, `MultiCameraRecorderHandle {startRecording(sessionId),
stopRecording()}`.

Konstanta: `MAX_CAMERAS = 5`, `TIMESLICE_MS = 1000` (interval potongan `MediaRecorder`),
`CODEC_PRIORITY` (prioritas: `video/mp4;codecs=avc1.42E01E` → `video/mp4` → WebM VP9/VP8).

- **`CameraTile`** — mengelola **satu** kamera fisik: `acquire()` memanggil `getUserMedia(...)`,
  mendeteksi pencabutan fisik lewat *event* `ended` (kebijakan "*fail loud*"); `startFn`/`stopFn`
  membuat `MediaRecorder`, menyimpan tiap potongan via `saveChunk`, dan menyusun ulang `Blob`
  akhir saat berhenti via `loadChunks`.
- **`MultiCameraRecorder`** *(default export, `forwardRef`)* — manajer utama.
  - `ensureReady()` — meminta izin kamera, membedakan "tidak ada kamera" dari "izin ditolak",
    otomatis memilih kamera pertama sebagai `cam1`.
  - Pendengar `devicechange` — menangani pasang/cabut kamera *hot-plug*; saat merekam, slot
    kamera yang terputus tetap dipertahankan (dilaporkan `missed` saat berhenti) alih-alih
    langsung dihapus.
  - `useImperativeHandle` — mengekspos dua metode ke `page.tsx` via `camRef`:
    - `startRecording(sessionId)` — memanggil `clearAllChunks()` lalu memulai rekam di semua
      kamera aktif secara paralel (`Promise.all`).
    - `stopRecording()` — menghentikan semua kamera paralel, memisahkan hasil sukses (`results`)
      dan kamera yang gagal (`missed`).

#### `RealtimeChart.tsx` — Visualisasi Sinyal IMU Langsung (ECharts)

Dimuat via `dynamic(..., {ssr:false})` karena ECharts membutuhkan `window`.

- **`NodeChartCard`** — satu instance ECharts per perangkat terhubung: grid ganda (ACC di atas,
  GYR di bawah), masing-masing 3 seri garis (X/Y/Z, warna tetap). *Effect* inisialisasi
  membuat `echarts.init(el, "dark")` sekali per kartu dan memasang `ResizeObserver`. *Effect*
  dorong-data melakukan *dedup* berdasarkan *timestamp*, mendorong nilai ke *buffer* cincin
  (maks `maxPoints`), lalu memanggil `chart.setOption` secara parsial (efisien, tanpa animasi).
- **`RealtimeChart`** *(default export)* — kontainer yang menata satu `NodeChartCard` per
  perangkat dalam grid responsif.

### 5.7 Alur Data Frontend (Ringkasan)

1. **Sambung**: operator memasukkan IP LAN backend → `wsClient.connect(ip)` membuka
   `/ws/frontend` (kontrol) dan `/ws/live` (telemetri).
2. **Sinkronisasi status**: backend mendorong `STATE_UPDATE` (mesin status sesi, daftar
   perangkat, kuorum, laporan integritas, waktu mulai terkoordinasi) → dicerminkan ke status
   React lokal → diteruskan sebagai *props* ke `StatusBanner`, `DevicePanel`, `PreflightPanel`.
3. **Telemetri langsung**: backend menyiarkan sampel akselerometer/giroskop per perangkat di
   `/ws/live` → ditampilkan di `RealtimeChart` dan `MiniSparkline`.
4. **Perintah**: `START_SESSION`/`STOP_SESSION`/`SET_LABEL`/`GET_STATE` dikirim sebagai JSON
   `{type, payload, command_id}` melalui kanal kontrol dengan protokol ACK (3× coba ulang,
   *timeout* 2 detik).
5. **Rekaman kamera**: sepenuhnya di sisi klien — `getUserMedia`/`MediaRecorder`, potongan
   disimpan ke IndexedDB, video dirakit & diunduh ke mesin operator saat sesi berhenti
   (**tidak** diunggah ke backend lewat HTTP).
6. **Pasca-sesi**: backend mendorong `integrity_report` (jumlah baris CSV / hash SHA-256 /
   status LULUS-GAGAL per perangkat) di dalam `STATE_UPDATE`, ditampilkan `IntegrityReport`.

---

## 6. Aplikasi Mobile — `mobile_node/`

Aplikasi **Flutter** (paket `sensors_app`, v2.0.0) yang berjalan di ponsel, membaca sensor IMU
bawaan (akselerometer + giroskop) pada ~100Hz, dan mengalirkan data ke backend Python via
WebSocket (`/ws/telemetry` + `/ws/control`, port 8000). Dirancang untuk kondisi lapangan yang
sulit: sinkronisasi jam, *reconnect* otomatis, *buffering* offline ke disk, *foreground service*
agar tetap hidup di latar belakang, dan pemulihan sesi yang terinterupsi.

**Alur navigasi layar**: `ConnectionScreen` → `PreflightScreen` → `DashboardScreen` (kembali ke
`ConnectionScreen` saat *disconnect*). `main.dart` dapat langsung melompat ke `DashboardScreen`
saat aplikasi dibuka bila terdeteksi sesi `RECORDING` yang terinterupsi dan *reconnect* berhasil.

**Pola manajemen status**: *singleton service* (pola `factory` + `_instance` statis) yang
masing-masing mengekspos `Stream` *broadcast*; layar (`StatefulWidget`) berlangganan lewat
`StreamSubscription` di `initState`/`dispose` dan mencerminkannya ke `setState` lokal. Tidak ada
paket manajemen status eksternal (Provider/Riverpod/Bloc).

### 6.1 `main.dart`

- **`main()`** — memanggil `WidgetsFlutterBinding.ensureInitialized()`,
  `ForegroundServiceHandler.initOptions()`, lalu `runApp(ImuTelemetryApp())`.
- **`ImuTelemetryApp`** / **`_ImuTelemetryAppState`** — pada `initState`, memanggil
  `_checkResumeSession()`: memuat `SessionPersistence().loadInterrupted()`; bila ada sesi
  `RECORDING` terinterupsi dengan `server_ip` tersimpan, mencoba
  `WebSocketClient().connect(serverIp)` dan, bila sukses, langsung masuk ke `DashboardScreen`
  (bukan `ConnectionScreen`). Menampilkan *splash* `CircularProgressIndicator` selama
  pengecekan. Tema aplikasi: gelap dengan aksen *deep-purple*.

### 6.2 Model Data (`models/`)

- **`SensorPacket`** (`models/sensor_packet.dart`) — sampel IMU internal aplikasi: `accX/Y/Z`,
  `gyroX/Y/Z` (double), `timestamp` (`DateTime`). Memiliki `toCsvRow(location, label)`. Ini
  adalah tipe yang mengalir di *stream* internal (`InternalSensorManager` →
  `WebSocketClient`/`GraphWidget`).
- **`SensorData`/`SensorBatch`** (`models/sensor.dart`) — pembungkus JSON generik. **Tidak
  dipakai di tempat lain** (kode usang, lihat [§10](#10-kode-usang--tidak-terpakai)).

#### `models/proto/commands.pb.dart` & `models/proto/sensor_packet.pb.dart`

Codec protobuf biner ditulis tangan (mencerminkan `shared_contracts/*.proto` field demi field),
**bukan** hasil `protoc` walau bernama `*.pb.dart` — ditandai eksplisit di komentar kode sebagai
pengganti sementara.

- **`abstract class CommandType`** — konstanta int mencerminkan `enum CommandType` di proto.
- **`class CommandProto`** — field `type`, `payload` (String JSON), `issuedAtMs`, `commandId`.
  `toBytes()` / `fromBytes()` (statis) untuk *encode/decode*.
- **`class SensorPacketProto`** — field `accX/Y/Z`, `gyroX/Y/Z`, `timestampMs`,
  `sequenceNumber`, `deviceId`, `schemaVersion`, `rawTimestampMs`. `toBytes()` untuk
  serialisasi.
- **`class DeviceRegisterProto`** — field `deviceId`, `deviceRole`, `deviceModel`,
  `androidVersion`, `appVersion`, `schemaVersion`. `toBytes()`.
- **`class _ProtoWriter`** *(pada tiap berkas)* — penulis format *wire* protobuf tingkat rendah
  (`writeVarint`, `writeInt64`, `writeString`, `writeFloat`).

### 6.3 Layar (`screens/`)

#### `ConnectionScreen` (`connection_screen.dart`) — **aktif**

Layar pertama aplikasi. Saat `initState`, memuat IP server & peran perangkat terakhir dari
`DeviceIdService`. UI: *dropdown* peran (`chest, waist, thigh_left, thigh_right, ankle_left,
ankle_right, wrist_left, wrist_right`), input teks IP, tombol Connect, tautan lisensi pihak
ketiga.

- **`_connect()`** — menyimpan peran terpilih via `DeviceIdService().setDeviceRole()`, memanggil
  `WebSocketClient().connect(ip)`; sukses → navigasi ke `PreflightScreen`; gagal → tampilkan
  pesan error.

#### `PreflightScreen` (`preflight_screen.dart`) — Gerbang Go/No-Go

Menjalankan **6 pemeriksaan** berurutan sebelum mengizinkan rekaman:

| Pemeriksaan | Fungsi | Kriteria Lulus |
|---|---|---|
| Baterai | `_checkBattery()` | ≥ 30% (via `battery_plus`) |
| Penyimpanan | `_checkStorage()` | Uji tulis berkas 1MB berhasil (**bukan** pengecekan ruang kosong sesungguhnya) |
| Sinkronisasi Jam | `_checkClockSync()` | `ClockSyncService().isSynced` dalam 3 detik, selisih offset ≤ 30000ms |
| RTT WebSocket | `_checkRtt()` | `ClockSyncService().lastRttMs` &lt; 500ms |
| Sanitas Akselerometer | `_checkSensorSanity()` | Rerata magnitudo 0.5–1.5g saat diam |
| Sanitas Giroskop | `_checkSensorSanity()` | Berdasarkan **jumlah event** (bukan nilai magnitudo) — memperbaiki bug lama di mana giroskop mati tetap lulus karena melaporkan nol datar |

Tombol "START RECORDING SESSION" hanya aktif bila keenam pemeriksaan **lulus**; navigasi ke
`DashboardScreen`.

#### `DashboardScreen` (`dashboard_screen.dart`) — UI Rekaman Utama

- `initState()` — memulai `InternalSensorManager().start(frequency: 100)`, menyambungkan
  *stream*-nya ke `WebSocketClient().attachSensorStream()`, berlangganan `stateStream` (status
  koneksi) dan `eventStream` (kejadian dari kanal kontrol).
- `_onEvent()` — menangani `start_session` (mengaktifkan `_isRecording`), `stop_session`
  (mereset), `set_label` (mem-*parsing* `label_id` dari payload JSON untuk ditampilkan).
- UI: bilah status koneksi (`_WsStatusDot` — hijau LIVE / kuning CONNECTING / oranye OFFLINE /
  merah DISCONNECTED), `_StatusBar` (peran, indikator rekaman, ID sesi, jumlah paket
  terkirim/tertunda, label aktif), 6 `GraphWidget` (akselerometer X/Y/Z, giroskop X/Y/Z).
- `_disconnect()` — memanggil `WebSocketClient().disconnect()`, kembali ke `ConnectionScreen`.

### 6.4 Layanan (`services/`)

#### `WebSocketClient` (`services/websocket_client.dart`) — Orkestrator Jaringan Inti

**`enum WsState`**: `disconnected, connecting, connected, offline`.

| Metode | Deskripsi |
|---|---|
| `connect(serverIp)` | Membuka dua `WebSocketChannel` (`/ws/control` & `/ws/telemetry`), menunggu `.ready.timeout(6s)` masing-masing agar tidak salah lapor "terhubung" pada jaringan mati. Mengirim `DeviceRegisterProto` setelah kanal kontrol terbuka. Membatalkan langganan lama sebelum *reconnect* (perbaikan bug "Defect D"). Memulai *timer* ping, sinkronisasi jam, dan *foreground service* Android bila sukses. |
| `attachSensorStream()` / `detachSensorStream()` | Menyambungkan/memutus *stream* dari `InternalSensorManager` ke `_onSensorPacket`. |
| `_onSensorPacket(pkt)` | Membangun `SensorPacketProto` dengan *timestamp* terkoreksi (`ClockSyncService().nowMs`), *timestamp* mentah, nomor urut bertambah otomatis, `deviceId`, `schemaVersion=1`; mengirim via *socket* telemetri jika terhubung, **atau menyimpan ke disk via `FallbackBufferManager`** jika tidak. Menyimpan nomor urut ke `SessionPersistence` tiap 500 paket. |
| `_handleControlMessage(bytes)` | Mendekode `CommandProto` masuk dan menangani per jenis: `PONG` (perbarui `_lastPong`), `CLOCK_SYNC` (selesaikan putaran sinkronisasi, setelah 5 sampel terapkan `ClockSyncService().applyOffsets()`), `START_SESSION` (baca `session_id`/`scheduled_start_ms`, tunggu hingga waktu terjadwal untuk mulai terkoordinasi), `STOP_SESSION` (bersihkan sesi aktif), `SET_LABEL`, `ACK`, `ERROR_ALERT`. |
| `_onControlDisconnect()` / `_scheduleReconnect()` | Saat koneksi putus, status → `offline`, mencoba `connect()` lagi setelah 3 detik; setelah *reconnect* sukses, memanggil `_flushFallbackBuffer()` (mengandalkan deduplikasi backend berbasis `(device_id, session_id, sequence_number)` untuk pengiriman ulang yang aman). |
| `sendCommand(cmd)` | Menulis `CommandProto` ke *socket* kontrol jika terhubung. |
| `_startPingTimer()` | Mengirim `PING` tiap 1 detik; menyatakan offline bila tidak ada `PONG` selama **8 detik** (`_pongTimeoutSec` — sengaja toleran terhadap degradasi Wi-Fi singkat saat subjek bergerak). |
| `_startClockSync()` | Mengirim 5 perintah `CLOCK_SYNC` berjarak 200ms saat *connect*, diulang tiap 5 menit. |
| `disconnect()` | *Disconnect* eksplisit oleh pengguna — membatalkan *timer*/langganan, menutup kedua *socket*, menonaktifkan *buffer fallback*, menghentikan *foreground service*. |

*Stream* yang diekspos: `stateStream` (`Stream<WsState>`), `eventStream`
(`Stream<Map<String,dynamic>>`); *getter*: `packetsSent`, `packetsBuffered`, `activeSessionId`,
`deviceRole`.

#### `ClockSyncService` (`services/clock_sync_service.dart`) — Sinkronisasi Jam Gaya NTP

| Metode | Deskripsi |
|---|---|
| `processResponse({t0Ms, t1Ms, t2Ms, t3Ms})` | Menghitung `rtt = (t3-t0) - (t2-t1)` (menolak RTT negatif); `offset = ((t1-t0)+(t2-t3)) / 2`. |
| `applyOffsets(offsets)` | Mengambil median dari daftar sampel offset, menetapkan `_clockOffsetMs`, `_synced = true`. |
| `nowMs` *(getter)* | `DateTime.now() + clockOffsetMs` — *timestamp* terkoreksi yang dipakai untuk telemetri. |
| `buildPayload(t0Ms)` / `parsePayload(payload)` *(statis)* | Encode/decode JSON `{t0_ms}` (permintaan) / `{t0_ms,t1_ms,t2_ms}` (balasan) untuk payload perintah `CLOCK_SYNC`. |

#### `DeviceIdService` (`services/device_id_service.dart`)

Pembungkus `SharedPreferences`.

| Metode | Deskripsi |
|---|---|
| `getDeviceId()` | Membuat & menyimpan UUID v4 (`device_id`) secara *lazy* sebagai identitas perangkat stabil. |
| `getDeviceRole()` / `setDeviceRole()` | Menyimpan/membaca peran perangkat terpilih (`device_role`, *default* `'chest'`). |
| `getLastServerIp()` / `saveServerIp()` | Menyimpan/membaca IP backend terakhir (`last_server_ip`) untuk mengisi otomatis layar koneksi. |

#### `FallbackBufferManager` (`services/fallback_buffer_manager.dart`) — Buffer Offline

Menulis ke berkas biner (`fallback_buffer.bin`, format `[panjang 4-byte BE][bytes proto]`
berulang) saat jaringan terputus. Batas ukuran `500MB` per berkas, maks `3` rotasi (setelahnya
menimpa berkas tertua secara siklis). *Fsync* tiap 1 detik agar data selamat dari *crash*.

| Metode | Deskripsi |
|---|---|
| `activate()` / `deactivate()` | Membuka/menutup berkas & memulai/menghentikan *timer flush* berkala. |
| `write(packetBytes)` | Menambahkan bytes berpanjang-awalan; melakukan rotasi bila batas ukuran tercapai. |
| `flushStream()` | Generator `async*` yang membaca semua berkas rotasi berurutan dan menghasilkan tiap paket — dikonsumsi `WebSocketClient._flushFallbackBuffer()` saat *reconnect*. |
| `clearAfterFlush()` | Memotong semua berkas rotasi & mereset status setelah *flush* berhasil. |

#### `ForegroundServiceHandler` (`services/foreground_service_handler.dart`)

Menjaga proses tetap hidup selama rekaman di latar belakang, membungkus
`flutter_foreground_task`. **Catatan penting**: OEM tertentu (Xiaomi/OPPO/Samsung) dapat tetap
mematikan *service* akibat optimasi baterai — harus dinonaktifkan manual per ponsel sebagai SOP
operator.

| Metode | Deskripsi |
|---|---|
| `initOptions()` *(statis)* | Mengonfigurasi kanal notifikasi Android `imu_telemetry` (prioritas TINGGI), *event* berkala 5 detik, `autoRunOnBoot: true`, izin *wake-lock* + *Wi-Fi-lock*. |
| `start()` | Memulai *service* bila belum berjalan. |
| `updateNotification(sent, buffered)` | Memperbarui teks notifikasi dengan jumlah paket terkirim/tertunda langsung. |
| `stop()` | Menghentikan *service*. |

#### `InternalSensorManager` (`services/internal_sensor_manager.dart`)

Membungkus *stream* akselerometer/giroskop dari paket `sensors_plus`.

| Metode | Deskripsi |
|---|---|
| `start({frequency: 100})` | Berlangganan `accelerometerEventStream`/`gyroscopeEventStream` dengan `samplingPeriod` eksplisit — mendaftar tepat pada frekuensi target (bukan `SensorInterval.fastestInterval`) agar tetap di bawah gerbang izin *high-rate* Android 12+ (200Hz), menghindari kegagalan senyap pengiriman *event* di sejumlah perangkat OEM. Mencatat penghitung *event* mentah (`_accEventCount`, `_gyroEventCount`) — dipakai `PreflightScreen` untuk membedakan "sensor hidup tapi bacaan nol" dari "sensor mati". *Timer* berkala memancarkan `SensorPacket` pada interval yang dikonfigurasi terlepas dari *event* perangkat keras baru, mengonversi akselerasi m/s² → g dan giroskop rad/s → deg/s. |
| `stop()` | Membatalkan kedua langganan perangkat keras & *ticker*. |

*Stream* yang diekspos: `dataStream` (`Stream<SensorPacket>` *broadcast*); *getter*:
`currentAccMagnitude`, `currentGyroMagnitude`, penghitung *event*.

#### `SessionPersistence` (`services/session_persistence.dart`) — Ketahanan Kematian Proses

Menyimpan status sesi ke berkas JSON `session_state.json` di direktori dokumen aplikasi.

| Metode | Deskripsi |
|---|---|
| `save({sessionId, deviceId, serverIp, clockOffsetMs, lastSequenceNumber, deviceRole})` | Menulis status dengan `state: 'RECORDING'` + `saved_at_ms`; dipanggil `WebSocketClient` tiap 500 paket selama sesi aktif. |
| `loadInterrupted()` | Membaca berkas dan mengembalikan data hanya bila `state == 'RECORDING'` — dikonsumsi `main.dart` saat *startup* untuk mencoba pemulihan otomatis. |
| `clear()` | Mengubah `state` menjadi `'IDLE'` (dipanggil saat `STOP_SESSION`). |

### 6.5 Widget (`widgets/`)

#### `GraphWidget` (`widgets/graph_widget.dart`)

Menerima `Stream<SensorPacket>`, `sensorType` (`'accel'`|`'gyro'`), `axis` (`'x'|'y'|'z'`),
`maxPoints`. Menampung nilai ke daftar `_data` (maks `maxPoints`) **tanpa** memanggil
`setState` tiap paket; `Timer.periodic(50ms)` (20fps) terpisah yang memicu gambar ulang,
memisahkan laju *frame* UI dari laju sensor 100Hz demi performa.

**`NodeGraphPainter`** *(`CustomPainter`)* — menggambar grafik garis sederhana, dinormalisasi
antara `minVal`/`maxVal` (akselerometer: ±12g, giroskop: ±200°/s).

### 6.6 Komunikasi dengan Backend

- **Protokol**: WebSocket mentah (`web_socket_channel`), bukan HTTP/REST, `ws://` polos (bukan
  `wss://`). Port `8000` dan skema `1` merupakan konstanta tertanam (*hard-coded*) di sisi Dart,
  bukan konfigurasi.
- **Dua *socket* terpisah, satu siklus koneksi**: kanal kontrol dibuka & dipastikan `ready`
  lebih dulu (agar `DeviceRegister` dikirim lewat kanal yang sudah pasti hidup), baru kemudian
  kanal telemetri dibuka.
- **Format *wire***: protobuf biner (bukan JSON) untuk `SensorPacket`/`DeviceRegister`/`Command`,
  di-*encode/decode* oleh codec tulisan tangan di `models/proto/*.pb.dart`.
- **Payload bersarang**: `Command.payload` sendiri adalah string JSON, skemanya tergantung
  `CommandType`.
- **Lapisan keandalan di atas *socket* mentah**: *heartbeat* aplikasi (`PING`/`PONG`, batas 8
  detik), *reconnect* otomatis tiap 3 detik, nomor urut per `(device_id, session_id)` untuk
  deduplikasi sisi backend, *buffering* offline ke disk (`FallbackBufferManager`), sinkronisasi
  jam gaya NTP (median dari 5 sampel, tiap 5 menit), dan mulai rekaman terkoordinasi
  (`scheduled_start_ms`).

### 6.7 Izin & Konfigurasi (Android)

Dideklarasikan di `mobile_node/android/app/src/main/AndroidManifest.xml`:

| Izin | Keterangan |
|---|---|
| `INTERNET`, `ACCESS_NETWORK_STATE` | Jaringan |
| `HIGH_SAMPLING_RATE_SENSORS` | Akses sensor &gt;200Hz — dicatat di manifes sebagai **saat ini tidak diperlukan** karena aplikasi mengambil sampel di ~100Hz (sengaja di bawah gerbang Android 12+) |
| `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION` | Historis, diperlukan untuk pemindaian Bluetooth/sensor di Android lama |
| `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `WAKE_LOCK`, `RECEIVE_BOOT_COMPLETED` | *Foreground service* rekaman latar belakang, termasuk `BootReceiver` untuk *restart* otomatis setelah reboot |
| `READ_EXTERNAL_STORAGE` / `WRITE_EXTERNAL_STORAGE` | Ditandai "*legacy*, untuk fallback `CsvLogger`" — namun kelas `CsvLogger` **tidak ada** di `lib/` saat ini (sisa dari fitur yang sudah dihapus/diganti nama) |

Tidak ada berkas konfigurasi/*env* terpisah di sisi mobile — IP server dimasukkan pengguna di
`ConnectionScreen` (disimpan via `SharedPreferences`), peran perangkat dipilih dari daftar tetap
8 opsi, UUID perangkat dibuat otomatis sekali dan di-*cache*.

---

## 7. Alat Bantu — `tools/device_simulator.py`

Skrip mandiri yang mensimulasikan **satu** node mobile IMU untuk menguji backend+frontend tanpa
ponsel fisik. Dijalankan dari root repo: `python tools/device_simulator.py` (membutuhkan paket
`websockets`).

- Konfigurasi: `BACKEND = "ws://localhost:8000"`, `DEVICE_ID = "sim-" + uuid4()[:8]`,
  `DEVICE_ROLE = "chest"`.
- Mengimplementasikan ulang *encoding wire* protobuf yang sama secara independen (tidak
  mengimpor dari `master_backend.proto`) agar bisa berjalan sebagai klien uji yang sepenuhnya
  mandiri.
- **`build_device_register()`** — membangun payload biner `DeviceRegister` (model
  `"Simulator"`, versi Android `"14"`, versi aplikasi `"2.0.0"`).
- **`build_ping(command_id)`** — membangun `Command` bertipe `PING`.
- **`build_sensor_packet(seq)`** — membangun `SensorPacket` dengan data akselerasi/giroskop
  sinusoidal sintetis (`acc_z ≈ 1g` mensimulasikan gravitasi).
- **`control_loop(ws)`** *(async)* — mengirim `PING` tiap 1 detik lewat kanal kontrol.
- **`telemetry_loop()`** *(async)* — membuka koneksi sendiri ke `/ws/telemetry`, mengirim
  `SensorPacket` pada 10Hz dengan nomor urut bertambah otomatis.
- **`run()`** *(async)* — terhubung ke `/ws/control`, mengirim `DeviceRegister` terlebih dahulu
  (sesuai protokol), lalu menjalankan `control_loop` dan `telemetry_loop` secara bersamaan.

> **Catatan**: simulator tidak pernah mengirim `START_SESSION`/`STOP_SESSION` — hanya
> registrasi & streaming. Operator tetap harus memicu `START_SESSION` dari dashboard agar
> backend benar-benar berada di state `RECORDING` dan mulai menulis paket simulator ke CSV
> (paket yang dikirim di luar state `RECORDING` dibuang secara diam-diam).

---

## 8. Alur Protokol End-to-End

Urutan siklus hidup sesi perekaman yang khas:

1. Backend memulai (`main.py` lifespan) → mendaftarkan mDNS, membuka audit log, memulai
   *background loop* (`_live_broadcaster_loop`, `run_idle_reaper`).
2. Perangkat mobile terhubung ke `/ws/control`, mengirim `DeviceRegister` sebagai *frame* biner
   pertama. Backend memvalidasi & mengecek keunikan peran via
   `session_manager.register_device`; menolak bila bentrok (kode tutup `4002`) atau tidak valid
   (kode tutup `4001`). Setelah sukses, perangkat langsung muncul *online* di dashboard.
3. Perangkat mengirim `PING` berkala; backend membalas `PONG` dan memperbarui
   `last_ping_ms`/`is_online` — ini adalah detak jantung (*heartbeat*) utama (batas 8 detik).
4. Perangkat dapat melakukan `CLOCK_SYNC` (pertukaran 3-*timestamp* gaya NTP) untuk menghitung
   selisih jamnya terhadap server, menghasilkan `timestamp_ms` (terkoreksi) vs
   `raw_timestamp_ms` (mentah) pada `SensorPacket` berikutnya.
5. Dashboard terhubung ke `/ws/frontend`, langsung menerima `_state_snapshot()`, dapat meminta
   `GET_STATE` kapan saja.
6. Operator memicu `START_SESSION` (dari dashboard). `session_manager.start_recording()`
   memvalidasi kuorum (≥1 perangkat), membuat `session_id`, menghitung
   `scheduled_start_ms = now + 500ms` untuk mulai serentak, membuka *writer* CSV per perangkat,
   membersihkan *dedup store*, berpindah ke `RECORDING`, menyimpan status pemulihan *crash*.
   Backend menyiarkan `Command` biner `START_SESSION` (berisi `session_id` dan
   `scheduled_start_ms`) ke semua perangkat, serta `STATE_UPDATE` ke semua dashboard.
7. Perangkat mulai mengalirkan `SensorPacket` di `/ws/telemetry`. Tiap paket: versi skema
   diperiksa (harus 1), dibuang jika state bukan `RECORDING`, dideduplikasi berdasarkan
   `(device_id, session_id, sequence_number)`, *timestamp* paket pertama tiap perangkat dicatat
   untuk pengecekan selisih waktu mulai antar-perangkat nanti. Paket valid & bukan duplikat
   ditambahkan ke CSV perangkat tsb (dengan kolom label aktif) dan di-*cache* untuk penyiar
   `/ws/live`.
8. Operator dapat mengirim `SET_LABEL` kapan saja selama rekaman untuk mengubah label yang
   disisipkan ke baris-baris berikutnya (**tidak retroaktif**).
9. Bila kanal kontrol perangkat terputus di tengah rekaman, `unregister_device` mencatat
   interval offline terbuka; bila hanya kanal telemetri yang putus (kontrol tetap hidup),
   `note_telemetry_disconnect` mencatatnya terpisah tanpa memengaruhi status "online" perangkat.
   *Background loop* `_monitor_offline` juga mendeteksi kematian *heartbeat* senyap (tanpa PING
   &gt;8 detik).
10. Operator memicu `STOP_SESSION`. Backend menyiarkan `STOP_SESSION` ke semua perangkat lebih
    dulu (agar perangkat bisa keluar dari mode rekam dengan anggun selagi *socket* masih
    terbuka) → `FINALIZING`: menutup interval offline terbuka, menutup semua *writer* CSV
    (menghitung jumlah baris + SHA-256 per berkas) → `VALIDATING`: menjalankan
    `IntegrityValidator.run()` (pemeriksaan baris/hash per perangkat, selisih waktu mulai
    lintas-perangkat terhadap ambang 100ms, keunikan peran), ditulis ke
    `{session_id}_integrity_report.json` → `IDLE`: membersihkan status sesi tersimpan &
    *dedup store*. Laporan integritas dikembalikan ke dashboard sebagai bagian dari
    respons/`STATE_UPDATE` `STOP_SESSION`.
11. Selama `IDLE`, *idle reaper loop* terus-menerus membersihkan perangkat basi/terputus dari
    registri di memori agar dashboard tidak menampilkan perangkat hantu, tanpa perlu *restart*
    backend.
12. Saat backend *crash*/*restart*, `_check_interrupted_sessions()` memindai
    `.sessions/*.state.json` untuk sesi yang tertinggal dalam `RECORDING`/`FINALIZING` dan
    mencatat peringatan untuk pemulihan manual/operator via `/session`.

---

## 9. Menjalankan Proyek

Ringkasan dari `STARTUP.md`. Urutan yang disarankan: **backend → frontend → ponsel**.

### Kebutuhan

- Python 3.10+ (backend butuh 3.11+ menurut `pyproject.toml`)
- Node.js 18+
- Flutter 3.3+ / Dart 3.3+ (bila membangun/menjalankan klien mobile)
- `protoc` (opsional, hanya untuk regenerasi binding protobuf)

### 9.1 Backend

```powershell
cd master_backend
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Isi SSD_PATH, RESCUE_PATH, dll., lalu buat direktorinya

# Jalankan dari ROOT repositori:
master_backend\venv\Scripts\activate
python master_backend/run.py
```

Backend siap saat log menunjukkan uvicorn berjalan di port 8000. Cek kesehatan:
`GET http://<host>:8000/health`.

### 9.2 Frontend

```powershell
cd master_frontend
npm install
npm run dev   # buka http://localhost:3000
```

### 9.3 Mobile

Buka aplikasi Flutter di tiap ponsel. Aplikasi dapat menemukan backend otomatis via mDNS bila
satu jaringan Wi-Fi, atau IP dapat dimasukkan manual. Selesaikan *checklist* preflight di
dashboard sebelum memulai sesi rekaman.

### 9.4 Perintah `Makefile` (dari root repo)

| Perintah | Fungsi |
|---|---|
| `make proto` | Regenerasi binding protobuf Dart & Python dari `shared_contracts/*.proto` |
| `make run-backend` | Menjalankan `python master_backend/run.py` |
| `make run-frontend` | Menjalankan `npm run dev` di `master_frontend/` |
| `make install-backend` | `pip install -r requirements.txt` |
| `make install-frontend` | `npm install` |

### 9.5 Menguji Tanpa Ponsel Fisik

```bash
pip install websockets
python tools/device_simulator.py
```

### 9.6 Masalah Umum

| Masalah | Solusi |
|---|---|
| `ModuleNotFoundError` saat menjalankan backend | Jalankan dari root repositori atau aktifkan *virtual environment* backend terlebih dahulu |
| Port 8000 sudah dipakai | Hentikan proses yang memegang port tsb sebelum *restart* |
| Frontend tidak bisa terhubung ke backend | Pastikan backend berjalan, IP benar, dan *firewall* mengizinkan trafik LAN di port 8000 |
| Flutter tidak menemukan backend via mDNS | Gunakan input IP manual, pastikan kedua perangkat satu jaringan Wi-Fi |
| Galat tulis di `SSD_PATH` | Buat direktorinya atau ubah path ke lokasi yang dapat ditulis |

---

## 10. Kode Usang / Tidak Terpakai

Beberapa berkas di `mobile_node/lib/` merupakan **sisa desain lama** (prototipe sensor Bluetooth
eksternal, mis. modul IMU via HC-05) yang telah digantikan oleh sensor bawaan ponsel + transport
WebSocket, namun belum dihapus dari repositori:

| Berkas | Status | Catatan |
|---|---|---|
| `screens/connetion_screen.dart` (typo nama berkas) | **Tidak terpakai** | Duplikat `ConnectionScreen` berbasis Bluetooth; tidak diimpor di manapun; bergantung pada paket `flutter_bluetooth_serial` yang **tidak dideklarasikan** di `pubspec.yaml` — akan gagal kompilasi bila diikutsertakan |
| `services/bluetooth_manager.dart` | **Tidak terpakai** | Hanya dipakai berkas di atas; masalah dependensi sama |
| `models/sensor.dart` (`SensorData`/`SensorBatch`) | **Tidak terpakai** | Model JSON generik, kemungkinan sisa desain transport JSON sebelum beralih ke protobuf biner |
| `widgets/custom_form.dart` | **Tidak terpakai** | Seluruh isi berkas dikomentari (`MyCustomForm`) |
| `widgets/label_button.dart` | **Kosong** | Berkas 0 byte |
| `widgets/stream_transform.dart` (`IntervalTransformer`) | **Tidak terpakai** | Didefinisikan tapi tidak direferensikan di berkas lain — mungkin tergantikan oleh pendekatan *timer* manual di `GraphWidget` |
| Izin `READ_EXTERNAL_STORAGE`/`WRITE_EXTERNAL_STORAGE` di manifes Android | **Sisa lama** | Merujuk kelas `CsvLogger` yang tidak lagi ada di `lib/` |

Di sisi frontend, beberapa dependensi di `package.json` juga tampak tidak terpakai secara
langsung: `idb` (kode memakai `indexedDB` mentah, bukan pembungkus `idb`) dan `uuid` (kode
memakai `crypto.randomUUID()` bawaan, bukan paket `uuid`).

Di sisi backend, `master_backend/proto/*.py` dan `mobile_node/lib/models/proto/*.pb.dart` adalah
implementasi manual sementara yang seharusnya digantikan hasil `make proto` (`protoc`) — saat
ini keduanya harus disinkronkan manual dengan `shared_contracts/*.proto` setiap kali skema
berubah.
