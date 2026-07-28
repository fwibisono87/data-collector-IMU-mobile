# Laporan Audit Kepatuhan Lisensi Pihak Ketiga

**Aplikasi:** IMU Telemetry Mobile Node (`sensors_app`) v2.0.0
**Tujuan:** Bukti kepatuhan lisensi (*proof of license compliance*) untuk pendaftaran Hak Cipta DJKI sebagai perangkat lunak **proprietary / closed-source**.
**Tanggal audit:** 15 Juli 2026
**Sumber data:** `mobile_node/pubspec.yaml` + halaman lisensi resmi masing-masing paket di [pub.dev](https://pub.dev).

---

## Ringkasan Eksekutif

Seluruh dependency runtime aplikasi menggunakan lisensi **permissive** (MIT & BSD-3-Clause).
**Tidak ada** dependency berlisensi copyleft (GPL / LGPL / AGPL / MPL).

| Indikator | Jumlah |
|-----------|--------|
| 🟢 AMAN (permissive: MIT / BSD) | 13 runtime + 2 dev |
| 🟡 Perlu atribusi khusus (MPL/LGPL) | 0 |
| 🔴 Berbahaya (GPL/AGPL) | 0 |

**Kesimpulan: AMAN untuk didaftarkan HKI sebagai proprietary.** Satu-satunya kewajiban
adalah atribusi (menampilkan teks lisensi pihak ketiga) — sudah dipenuhi lewat halaman
"Lisensi Pihak Ketiga" di dalam aplikasi (`showLicensePage`, lihat `lib/screens/connection_screen.dart`).

---

## Tabel Rincian Dependency

| Library / Dependency | Versi (pubspec) | Lisensi | Risiko HKI | Tindakan |
|----------------------|-----------------|---------|------------|----------|
| flutter (SDK) | sdk | BSD-3-Clause | 🟢 AMAN | Atribusi (auto di license page) |
| cupertino_icons | ^1.0.6 | MIT | 🟢 AMAN | Atribusi |
| sensors_plus | ^5.0.1 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| permission_handler | ^11.3.0 | MIT | 🟢 AMAN | Atribusi |
| path_provider | ^2.1.2 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| web_socket_channel | ^2.4.5 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| protobuf | ^3.1.0 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| uuid | ^4.4.0 | MIT | 🟢 AMAN | Atribusi |
| shared_preferences | ^2.2.3 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| flutter_foreground_task | ^8.0.0 | MIT | 🟢 AMAN | Atribusi |
| battery_plus | ^5.0.3 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| intl | ^0.19.0 | BSD-3-Clause | 🟢 AMAN | Atribusi |
| flutter_lints *(dev)* | ^3.0.0 | BSD-3-Clause | 🟢 AMAN | Dev-only, tidak dibundel ke APK |
| flutter_test *(dev)* | sdk | BSD-3-Clause | 🟢 AMAN | Dev-only, tidak dibundel ke APK |

> Catatan: dependency `csv` yang sebelumnya tercatat sebagai *legacy* telah **dihapus**
> pada audit ini (beserta dead-code `lib/services/csv_logger.dart` yang menjadi satu-satunya
> pemakainya) untuk memperkecil permukaan audit dan ukuran build.

---

## Verifikasi Lisensi (Item 3)

Untuk finalisasi berkas DJKI, verifikasi ulang teks & versi lisensi terhadap versi yang
benar-benar dibundel:

```bash
cd mobile_node
flutter pub get
flutter pub deps --style=compact   # daftar lengkap dependency + transitif
```

Halaman lisensi resmi tiap paket: `https://pub.dev/packages/<nama_paket>/license`
(mis. https://pub.dev/packages/sensors_plus/license).

Di dalam aplikasi, seluruh teks lisensi paket + transitifnya dapat dibaca pengguna via
tombol **"Lisensi Pihak Ketiga"** pada layar koneksi (Flutter mengumpulkannya otomatis
dari file `LICENSE` tiap paket).

---

## Review Kode Sendiri (Item 5)

Pemindaian heuristik terhadap source pertama (`mobile_node/lib`, `master_backend`,
`master_frontend/src`) untuk marker copyleft (`GNU`, `GPL`, `AGPL`, `copyleft`):

**Hasil: BERSIH** — tidak ditemukan header lisensi asing maupun jejak GPL/AGPL.

> ⚠️ Pemindaian ini bersifat heuristik dan **tidak** menggantikan review manual.
> Pastikan tidak ada potongan kode hasil *copy-paste* dari repo berlisensi copyleft
> yang di-strip header lisensinya.

---

## Disclaimer

Laporan ini bersifat teknis-informasional untuk tim developer, **bukan** nasihat hukum
formal. Konfirmasikan dengan konsultan HKI / advokat kekayaan intelektual sebelum
pendaftaran resmi DJKI.
