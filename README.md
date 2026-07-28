# IMU Telemetry System

> Platform pengumpulan data multi-perangkat untuk merekam aliran sensor inersia (IMU) dari beberapa ponsel sekaligus, dikoordinasikan dari satu dashboard operator secara real-time.

<p>
  <img alt="Flutter" src="https://img.shields.io/badge/Flutter-3.3%2B-02569B?logo=flutter&logoColor=white">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Next.js" src="https://img.shields.io/badge/Next.js-14-000000?logo=nextdotjs&logoColor=white">
  <img alt="Protobuf" src="https://img.shields.io/badge/Protobuf-contracts-4285F4?logo=googl&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-Proprietary-red">
</p>

---

## 📖 Ringkasan

IMU Telemetry System mengumpulkan telemetri sensor inersia dari satu atau lebih perangkat
mobile, menyiarkan status sesi & perangkat ke dashboard secara langsung, dan menyimpan
artefak sesi untuk validasi. Sistem terbagi menjadi tiga komponen utama plus kontrak
data bersama.

| Komponen | Teknologi | Peran |
|----------|-----------|-------|
| [`master_backend/`](master_backend/) | FastAPI + WebSocket | Hub WebSocket, state machine sesi, audit log, mDNS discovery |
| [`master_frontend/`](master_frontend/) | Next.js | Dashboard operator: koneksi, preflight, live chart, labeling, integrity report, multi-camera recording |
| [`mobile_node/`](mobile_node/) | Flutter | Klien mobile: baca sensor perangkat, koneksi ke backend, persistensi sesi untuk recovery |
| [`shared_contracts/`](shared_contracts/) | Protobuf | Kontrak `.proto` bersama lintas backend/frontend/mobile |
| [`tools/`](tools/) | Skrip bantu | Device simulator & utilitas |

## 🏗️ Arsitektur

```
        ┌──────────────┐   Protobuf/WS    ┌──────────────────┐
        │  Mobile Node │ ───────────────▶ │                  │
        │  (Flutter)   │ ◀─────────────── │  Master Backend  │
        └──────────────┘   commands       │    (FastAPI)     │
        ┌──────────────┐                  │  WS hub · state  │
        │  Mobile Node │ ───────────────▶ │  machine · audit │
        │  (Flutter)   │ ◀─────────────── │                  │
        └──────────────┘                  └────────┬─────────┘
              ...  (n perangkat, mDNS discovery)    │ WebSocket
                                                    ▼
                                          ┌──────────────────┐
                                          │  Master Frontend │
                                          │    (Next.js)     │
                                          │  dashboard live  │
                                          └──────────────────┘
```

## ✨ Fitur Utama

- 📡 Mengumpulkan telemetri IMU dari **satu atau banyak** perangkat mobile secara serentak.
- ⚡ Menyiarkan status sesi & perangkat ke dashboard secara **real-time**.
- 🔁 State machine sesi: `IDLE → PREFLIGHT → READY → RECORDING → FINALIZING → VALIDATING → IDLE`.
- 🗂️ Menyimpan audit log backend & artefak sesi untuk peninjauan.
- 🔍 **mDNS discovery** — klien Flutter menemukan backend otomatis di jaringan lokal.
- 🎥 Perekaman multi-kamera & integrity report dari sisi dashboard.
- 💾 Persistensi & **auto-resume** sesi pada mobile node bila koneksi terputus.

## 📋 Kebutuhan

- Python 3.10+
- Node.js 18+
- Flutter 3.3+ / Dart 3.3+
- `protoc` (opsional, hanya bila ingin meregenerasi binding protobuf)

## 🚀 Quick Start

Setup lengkap & urutan runtime didokumentasikan di **[STARTUP.md](STARTUP.md)**. Versi singkat:

1. Jalankan backend dari root repository.
2. Jalankan dashboard Next.js di `master_frontend/`.
3. Buka aplikasi Flutter di tiap ponsel, sambungkan ke IP backend atau nama mDNS.
4. Selesaikan checklist preflight di dashboard sebelum memulai sesi rekaman.

## 🛠️ Perintah Berguna

Dari root repository, `Makefile` menyediakan beberapa tugas umum:

| Perintah | Fungsi |
|----------|--------|
| `make proto` | Regenerasi binding protobuf Dart & Python |
| `make run-backend` | Jalankan backend FastAPI |
| `make run-frontend` | Jalankan dashboard operator |
| `make install-backend` | Install dependency Python backend |
| `make install-frontend` | Install dependency Node frontend |

## 📁 Struktur Repository

- `master_backend/app/` — service backend, handler WebSocket, session manager, logika validasi.
- `master_frontend/src/` — komponen UI dashboard & klien WebSocket.
- `mobile_node/lib/` — screen, service, widget, dan model Flutter.
- `shared_contracts/` — file `.proto` bersama lintas backend, frontend, dan klien mobile.

## 📄 Lisensi

Perangkat lunak ini bersifat **proprietary / closed-source**. Seluruh hak cipta dimiliki
oleh tim pengembang.

Aplikasi memanfaatkan pustaka pihak ketiga berlisensi **permissive** (MIT & BSD-3-Clause).
Rincian audit kepatuhan lisensi tersedia di
[`mobile_node/LICENSE_AUDIT.md`](mobile_node/LICENSE_AUDIT.md), dan atribusi lengkapnya
dapat dibaca langsung di dalam aplikasi melalui tombol **"Lisensi Pihak Ketiga"** pada
layar koneksi.

## 📚 Butuh Setup Lengkap?

Lihat **[STARTUP.md](STARTUP.md)** untuk setup environment, perintah launch, catatan
jaringan, dan troubleshooting.
