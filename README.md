# IMU Telemetry System

> Platform pengumpulan data multi-perangkat untuk merekam aliran sensor inersia (IMU) dari
> beberapa ponsel sekaligus — disinkronkan dengan video dari beberapa kamera USB — dan
> dikoordinasikan dari satu dashboard operator di laptop.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=white">
  <img alt="Kotlin" src="https://img.shields.io/badge/Kotlin-Android-7F52FF?logo=kotlin&logoColor=white">
  <img alt="Protobuf" src="https://img.shields.io/badge/Protobuf-contracts-4285F4?logo=googl&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-Proprietary-red">
</p>

---

## 📖 Ringkasan

Satu subjek memakai 3–8 ponsel (sensor IMU di beberapa titik tubuh) sambil 1–5 kamera USB
merekam gerakannya. Semua data harus memiliki jam yang sama, harus selamat meskipun Wi-Fi
putus, dan harus bisa dianalisis dengan skrip yang sama seperti 17 sesi sebelumnya.

Sistem berjalan sebagai **satu proses di satu laptop**: satu port (`8000`), satu unit yang
di-deploy, satu APK di ponsel.

| Komponen | Teknologi | Peran |
|----------|-----------|-------|
| [`master_backend/`](master_backend/) | Python 3.12 + FastAPI | Satu-satunya server: state machine sesi, penulisan CSV (frozen contract), kamera (ffmpeg), ZIP sesi, mDNS, **dan** dashboard. |
| [`dashboard/`](dashboard/) | Vite + React 18 + TypeScript | Dashboard operator (Setup → Record → Done). Build statisnya ikut di-commit di `master_backend/app/static/` dan disajikan backend di `/`. |
| [`mobile_node/`](mobile_node/) | Kotlin + Compose (native) | Aplikasi ponsel: foreground service, sensor, rescue CSV lokal, buffer offline, upload, discovery mDNS. |
| [`shared_contracts/`](shared_contracts/) | Protobuf | Kontrak biner ponsel ↔ backend. |
| [`tools/`](tools/) | Skrip bantu | Device simulator, analisa sesi, perbaikan WebM, decode buffer. |

## 🏗️ Arsitektur

```
                        Windows laptop — satu proses (python master_backend/run.py)
   ┌──────────────────────────────────────────────────────────────────────────────────┐
   │  FastAPI di :8000                                                                │
   │                                                                                  │
   │   /ws/telemetry  ◀── protobuf ──┐        ┌───────────────┐    ┌────────────────┐  │
   │   /ws/control    ─── commands ──┤        │ session core  │    │ kamera: 1      │  │
   │   /upload/csv    ◀── rescue ────┤        │ CSV + finalize│    │ ffmpeg per     │  │
   │                                 │        │ + ZIP sesi    │    │ kamera, MJPEG  │  │
   │   /ws/ui  + /api/*  ◀── browser ─┐      └───────────────┘    └───────┬────────┘  │
   │   /  (dashboard statis) ─────────┘                                   │           │
   └──────────────────────────────────────────────────────────────────────┼───────────┘
        ▲  Wi-Fi + mDNS `_imu-telemetry._tcp`                              │ USB
        │                                                                  ▼
   Android: IMU Node (Kotlin, foreground service)                    kamera USB 1–5

   Data di laptop : <SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/<sid>_…  (+ ZIP satu file)
   Data di ponsel : /sdcard/Android/data/com.example.sensors_app/files/imu_sessions/
```

Prinsip yang mengikat desain ini (lihat [`docs/rebuild/SPEC.md`](docs/rebuild/SPEC.md)):

- **Tidak ada state penting di browser.** Sesi, video, ZIP, dan notice semuanya di backend;
  tab bisa ditutup, di-reload, atau dibuka dari perangkat lain kapan saja.
- **Dashboard berasal dari commit yang sama dengan backend.** Bundle statis ikut repo,
  jadi tidak ada lagi kejadian "browser memakai JavaScript lama" (2026-08-11).
- **Data identik byte-per-byte dengan sesi lama.** Format output dibekukan di
  [`docs/OUTPUT_CONTRACT.md`](docs/OUTPUT_CONTRACT.md) dan dijaga golden test.

## ✨ Fitur Utama

- 📡 Perekaman IMU tersinkron dari banyak ponsel sekaligus (100 Hz, timestamp per-sampel).
- 🎥 1–5 kamera USB direkam backend dengan ffmpeg (WebM VP9), preview MJPEG di dashboard,
  dan otomatis lanjut ke file `_partN` bila kamera terlepas lalu tersambung lagi.
- 🔁 Tahan gangguan: ponsel menyimpan rescue CSV lokal + buffer offline dan meng-upload
  ulang setelah jaringan kembali; backend menerima data terlambat selama jendela
  `LATE_ACCEPT_SEC` dan menyusun ulang ZIP-nya sendiri.
- 🧭 Operator flow terpandu: **Setup → Record → Done**, dengan readiness yang dihitung
  backend (blocker vs warning) dan riwayat sesi di halaman Sessions.
- 📐 Kalibrasi kamera: reference stills serentak dari semua kamera + overlay ghost
  (Blend / Difference / Blink / Side by side) dengan verdict OK / CHECK / MOVED.
- 🗂️ ZIP sesi (`<subject>_<tag>_<sid>.zip`) ditulis di laptop saat STOP — tidak perlu
  diunduh, tidak ada dialog yang memblokir operator.
- 🔍 Discovery otomatis lewat mDNS; kalau gagal, IP laptop bisa diketik manual.

## 📋 Kebutuhan

**Laptop lapangan (Windows):**

- Python 3.12 (64-bit) — https://www.python.org/downloads/windows/
- `ffmpeg.exe` + `ffprobe.exe` — di `vendor\ffmpeg\bin\`, di `PATH`, atau lewat
  `IMU_FFMPEG`/`IMU_FFPROBE` (lihat [`STARTUP.md`](STARTUP.md))
- Tidak perlu Node, tidak perlu Flutter, tidak perlu Android SDK.

**Hanya untuk development / build:**

- Node.js 18+ (membangun dashboard: `make dashboard`)
- Android SDK + JDK 17 (membangun APK: `make apk`)
- `protoc` (opsional, hanya untuk regenerasi binding protobuf)

## 🚀 Quick Start

Panduan lengkap ada di **[STARTUP.md](STARTUP.md)**. Versi singkat:

1. Di laptop: salin `master_backend\.env.example` → `master_backend\.env`, isi `SSD_PATH`
   dan `RESCUE_PATH`, lalu jalankan **`start.bat`** (klik dua kali) atau `.\start_all.ps1`.
2. Script mencetak **LAN IP** dan membuka dashboard di `http://localhost:8000/`
   (dari perangkat lain: `http://<laptop-ip>:8000/`).
3. Di ponsel: pasang APK sekali (`./ops/install_phones.sh` dari Linux, atau
   `adb install -r <apk>`), lalu buka aplikasi **IMU Node**. Aplikasi menemukan laptop
   lewat mDNS; kalau tidak, ketik IP yang tercetak tadi.
4. Isi Subject/Tag/Operator, pilih kamera, pastikan tidak ada blocker, tekan **Start**.

Hentikan semuanya dengan Ctrl+C di jendela backend, atau `.\ops\stop.ps1` (ikut
mematikan ffmpeg yang dijalankan backend).

> **Kunci rilis:** buat sekali `mobile_node/keystore.properties` dari
> `keystore.properties.example` (atau set `IMU_KEYSTORE_*`), lalu **backup** file `.jks`-nya.
> Tanpa kunci, Gradle memakai debug key, memberi peringatan keras, dan versi berakhiran
> `-debugsigned` (jangan dipasang di ponsel lapangan). Ponsel yang masih memakai build lama
> perlu **satu kali** uninstall/reinstall: unggah/ambil CSV rescue dulu (uninstall
> menghapusnya), `adb uninstall com.example.sensors_app`, lalu install; `device_id` akan
> dibuat ulang. Detail: [`STARTUP.md`](STARTUP.md) §3.

## 🛠️ Perintah Berguna

Dari root repository (`make` dipakai di Linux; di Windows pakai `.ps1`):

| Perintah | Fungsi |
|----------|--------|
| `make backend` | Jalankan backend + dashboard + kamera di `:8000` |
| `make dev` | Sama, tapi reload saat kode berubah (`IMU_DEV_RELOAD=1`) — **jangan** saat merekam |
| `make test` | Fast suite backend + unit test dashboard |
| `make test-slow` | Test kamera/video/soak (beberapa menit, butuh ffmpeg) |
| `make dashboard` | `npm ci && npm run build` → `master_backend/app/static/` (commit hasilnya) |
| `make apk` | Build APK rilis: `cd mobile_node && ./gradlew assembleRelease` |
| `make install-phones` | Pasang APK ke semua ponsel yang terhubung (`ops/install_phones.sh`) |
| `make proto` | Regenerasi binding protobuf Python + test vector Kotlin |
| `make ip` / `make stop` | Diagnosa IP + `/health` / matikan backend dan ffmpeg-nya |

## 📁 Struktur Repository

- `master_backend/` — server: `app/api/` (REST + `/ws/ui`), `app/camera/` (capture,
  recording, kalibrasi), `app/session_zip.py` (ZIP sesi), `app/static/` (dashboard build),
  `tests/` (golden contract + unit + slow).
- `dashboard/src/` — dashboard operator (Vite/React; murni klien dari kontrak backend).
- `mobile_node/app/src/main/kotlin/` — aplikasi Android native.
- `shared_contracts/` — file `.proto` bersama backend dan ponsel.
- `docs/` — `OUTPUT_CONTRACT.md` (format data, frozen), `DEPLOY.md`, `FIELD_CHECKLIST.md`,
  `REDMI_NOTE_12_SETUP.md`, dan `rebuild/` (spesifikasi + dokumen arsitektur per komponen).
- `ops/` — `stop.ps1`, `ip.ps1`, `install_phones.sh`.

## 📚 Dokumentasi

| Dokumen | Isi |
|---------|-----|
| [`STARTUP.md`](STARTUP.md) | Setup pertama kali, start harian, env var, setup Linux, troubleshooting |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | Checklist sebelum hari perekaman (commit sama, versi app sama, ffmpeg ada) |
| [`docs/FIELD_CHECKLIST.md`](docs/FIELD_CHECKLIST.md) | Checklist di lapangan: sebelum, selama, sesudah |
| [`docs/REDMI_NOTE_12_SETUP.md`](docs/REDMI_NOTE_12_SETUP.md) | Setelan per-ponsel (MIUI/HyperOS) agar tidak mematikan aplikasi |
| [`master_backend/ARCHITECTURE.md`](master_backend/ARCHITECTURE.md) | Alur START → ZIP, modul backend, endpoint legacy |
| [`master_backend/app/camera/README.md`](master_backend/app/camera/README.md) | Proses kamera, dshow/v4l2, env var, benchmark codec, kalibrasi |
| [`dashboard/README.md`](dashboard/README.md) | Struktur dashboard, aturan "tanpa state di browser", mock scenario |
| [`mobile_node/ARCHITECTURE.md`](mobile_node/ARCHITECTURE.md) | Thread, state machine, format file di ponsel |
| [`docs/rebuild/SPEC.md`](docs/rebuild/SPEC.md) | Spesifikasi rebuild (masalah lama, arsitektur target, acceptance test) |

## 📄 Lisensi

Perangkat lunak ini bersifat **proprietary / closed-source**. Seluruh hak cipta dimiliki
oleh tim pengembang.

Aplikasi memanfaatkan pustaka pihak ketiga berlisensi **permissive** (MIT & BSD-3-Clause).
Rincian audit kepatuhan lisensi tersedia di
[`mobile_node/LICENSE_AUDIT.md`](mobile_node/LICENSE_AUDIT.md) (audit itu ditulis untuk
build Flutter; daftar dependensi aplikasi Kotlin perlu diperbarui sebelum dipakai lagi
untuk pendaftaran hak cipta).