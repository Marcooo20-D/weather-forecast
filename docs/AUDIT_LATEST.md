# Audit LANGIT — 30 Oktober 2026

> **Status rilis 31 Oktober: v65.3.1 sudah tayang dan diverifikasi.**
> Lihat [verifikasi rilis](RELEASE_VERIFICATION_2026-10-31.md) untuk bukti final.

> Bagian A–L berikut merekam hasil audit awal pada 30 Oktober. Pemeriksaan
> rilis 31 Oktober menambahkan workflow Chromium dan perbaikan menu/panel
> ponsel. Lihat [catatan rilis](RELEASE_NOTES.md), pemeriksaan akhir PR #3,
> serta manifest/workflow publikasi untuk status rilis terkini. Status
> “belum merge/deploy” di bawah adalah status saat audit awal tersebut.

## A. Ringkasan operasional

**Kode diperbaiki dan CI lulus; kandidat belum dinyatakan siap produksi.**
Pengujian browser untuk hasil perubahan masih terblokir. Perubahan tersedia
sebagai [draft PR #3](https://github.com/Marcooo20-D/weather-forecast/pull/3).

| Status | Bukti |
| --- | --- |
| Baseline sumber | `main`: `7224aed537f83614f64d9933da63004a4a2aee18` |
| Baseline publik | `gh-pages`: `2befb601aca07fa9bdaa872b481e6d0b261369c4`; v65.2 |
| Kandidat kode | `6754eba6ef5e00c27432ad4335fda4008f95acb4`; v65.3 |
| Cabang | `codex/langit-audit-20261030` |
| Temuan | 26 kelompok temuan terkonfirmasi, seluruhnya mendapat implementasi perbaikan |
| Tes | 55/55 tes Python lulus, termasuk enam subtes Node dan eksekusi skrip pada fixture DOM |
| CI | [Offline checks berhasil](https://github.com/Marcooo20-D/weather-forecast/actions/runs/41039157627); tes dan sintaks berhasil |
| Penerapan | Belum merge dan belum deploy; produksi yang diperiksa tetap v65.2 |

Masalah utama menyangkut makna dan integritas data: peluang hujan dapat
disalahartikan sebagai intensitas/bahaya, data kosong masih menjadi nol,
sebagian waktu UTC berpindah tanggal secara keliru, dan CSV berbeda dari API
akhir. Ada pula cacat grafik, pemilihan status sumber, ekspor sebagian
lokasi, serta pembersihan staging.

Tidak ditemukan P0 dalam cakupan ini. Ini bukan jaminan seluruh repositori
bebas cacat. “Diperbaiki” tidak berarti persetujuan visual atau rilis.

## B. Arsitektur aktual

Workflow menjalankan tes, forecast, sinkronisasi referensi, evaluasi, rebuild
v65, pemeriksaan keluaran, staging, penyimpanan hasil ke `main`, lalu
publikasi `_site/` ke `gh-pages`. Pages menerbitkan branch tersebut.

| Komponen | Peran |
| --- | --- |
| `weather_ensemble_multi_location.py` | Provider deterministik/anggota ensemble, penyelarasan, perhitungan, arsip dan evaluasi |
| `langit_v65_cinematic_rebuild.py` | Sumber akhir API, GeoJSON, CSV tampilan, HTML, peta dan aset |
| `site_tools/public_data.js` | Kontrak numerik grafik dan peta, tanpa dependensi jaringan/DOM |
| `site_tools/public_site.py` | Paket publik terbatas dan gate integritas |
| `site_tools/workflow_inputs.py` | Validasi input sebelum digunakan shell |
| `main/outputs/` | Data terkini dan historis, bukan seluruh isi publikasi |
| `gh-pages` | Hasil generator, bukan sumber perbaikan permanen |

Inventaris AST mesin baseline menemukan 608 definisi fungsi/kelas dan 501
nama unik. Wrapper akhir memanggil versi sebelumnya melalui alias. Builder
lama dan `build_utils/modify_map_page.py` tidak dipanggil workflow aktif.
Tidak dilakukan konsolidasi besar yang berisiko mengubah perhitungan.

Lihat [arsitektur dan kontrak](ARCHITECTURE.md).

## C. Cakupan

| Area | Diperiksa | Batas |
| --- | --- | --- |
| Kedua branch | Semua path, ukuran dan SHA; 6.615 blob `main` dan 128 berkas Pages | Inventaris bukan validasi ilmiah semua isi |
| Sumber | Seluruh 32 berkas di luar `outputs` diambil; simbol, sintaks, dependensi dan pola kredensial diperiksa | Tidak mengklaim telaah semantik setiap baris dari 608 definisi |
| Mesin aktif | Parser, satuan, waktu, keluarga, peluang, kuantil, CRPS, arsip dan bagian penting evaluasi | Pengambilan penuh semua provider tidak dijalankan ulang |
| Tiga lokasi | Sembilan Sentinel JSON/CSV, status bertanggal, API, GeoJSON, CSV dan halaman publik | Snapshot 30 Oktober–1 November 2026 |
| Arsip | Seluruh 585 arsip diinventarisasi; enam sampel nyata JSON/gzip dibaca, dua per lokasi | Integritas sampel, bukan akurasi atau integritas semua arsip |
| Browser produksi | Portal, tiga lokasi, pergantian hari, produk tiga hari/aktivitas, sumber/akurasi dan enam lapisan peta Dago | Chromium; bukan seluruh kontrol di seluruh browser |
| URL produksi | 127 resource HTTP cocok dengan blob baseline Pages | `.nojekyll` tidak diminta sebagai URL |
| Kandidat | Rebuild tiga lokasi, paket, seluruh skrip hasil build, fixture DOM | Belum dirender dalam browser |
| Trading dashboard | Inventaris, sintaks JS dan pencarian pola kredensial | Strategi, hasil, pasar dan UI operasional tidak diubah/diklaim teruji |
| Keamanan | Origin pesan, escaping JSON, input workflow, izin token, SHA action, pola kredensial | Bukan pentest, scan advisori menyeluruh atau scan semua riwayat Git |

Baseline `main` memuat 6.186.912.400 byte isi blob, termasuk 4.562.921.425 byte
pada `forecast_archive/`. Ini bukan ukuran transfer Git. Arsip tidak
dipangkas untuk membuat pengujian lulus.

## D. Register temuan

Rincian lokasi, reproduksi, ekspektasi dan bukti terstruktur terdapat pada
[audit-evidence-20261030.json](audit-evidence-20261030.json).

| ID | Prioritas | Sebelum: akar masalah/bukti | Perbaikan |
| --- | --- | --- | --- |
| A01 | P1 | PoP tinggi menghasilkan “Hujan kuat”; indeks tampak sebagai bahaya | Pisahkan peluang, intensitas dan indeks gangguan; hilangkan klaim intensitas/cahaya tanpa data |
| A02 | P1 | `prob(140)` menjadi 100; parser menerima nilai fisik salah/boolean | Tolak nilai tidak sah, pertahankan null dan flag |
| A03 | P1 | `2026-10-30T23:00Z` dapat tetap tanggal 30 | Konversi alias valid time ke 06.00 WIB tanggal 31 |
| A04 | P1 | Jam rusak/hilang menjadi 00.00 yang valid | Jangan membuat tengah malam; catat penolakan |
| A05 | P1 | Duplikat menimpa jam UI atau menggandakan anggota variabel ensemble | Tolak timestamp duplikat/tidak berurutan |
| A06 | P1 | Portal masih memberi nol untuk awan/arah/angin kosong | Gunakan serializer lokasi yang sama |
| A07 | P1 | Persen awan dihitung dari kategori ketika datanya kosong | Hapus persen awan buatan |
| A08 | P2 | Bearing tetap ada pada angin tenang | Arah/panah tenang atau tidak sah tetap kosong |
| A09 | P1 | Hari tanpa data diberi indeks 35; panas terasa disalin dari suhu | Data turunan kosong tetap kosong dan tampil “—” |
| A10 | P1 | CSV Dago 00.00 `watch`, API akhir `limited` | Tiga CSV tampilan berasal dari API akhir |
| A11 | P2 | Halaman 30 Oktober memakai status provider 1 November | Pilih status untuk tanggal berlaku yang sama |
| A12 | P1 | Run satu lokasi menghilangkan lokasi lain dari gabungan/dimensi | Pertahankan baris lokasi lain yang masih berlaku |
| A13 | P1 | Collision mkdir dapat diikuti penghapusan staging proses lain | Hapus hanya staging yang dibuat sendiri |
| A14 | P1 | HTML wajib kosong dan data yang tidak konsisten bisa lolos gate | Periksa isi wajib, versi, tanggal, jam, rentang dan kontrak silang |
| A15 | P1 | Pemeriksa kata kunci keluar normal saat “MISSING” | `check_fixes.py` memakai gate paket sebenarnya |
| A16 | P1 | Grafik null menjadi nol; gap tersambung; pixel memakai viewBox tetap | Gap tetap terbuka, skala per variabel, viewBox mengikuti ukuran |
| A17 | P2 | Timeline peta hanya memakai lokasi pertama | Gabungkan seluruh tanggal/jam yang unik |
| A18 | P2 | HUD selalu memilih lokasi pertama | Selector lokasi dan klik marker memperbarui HUD; slot kosong tetap kosong |
| A19 | P2 | Kegagalan Leaflet menutup data; play kosong tetap aktif | Nilai/timeline terpisah dari Leaflet, fallback bertautan, play kosong nonaktif |
| A20 | P2 | Loading statis mengaku menghubungi satelit/orbit dan memakai elevasi hardcoded | Hapus telemetri simulasi/elevasi itu; tampilkan konten langsung |
| A21 | P2 | Jam aktivitas yang sudah lewat masih disarankan | Keluarkan jam lewat untuk hari berjalan |
| A22 | P2 | Hero memilih hari ini tetapi panel awal tetap indeks 0 | Tab/panel mengikuti tanggal `today`, tanggal absolut dicantumkan |
| A23 | P2 | Revisi sumber hilang; build dan issue tidak jelas berbeda | Manifest SHA, build time, issue per hari dan indikator pembaruan |
| A24 | P2 | Nama iframe/form, fokus, modal mobile dan gerak kurang memadai | Nama/state aksesibel, fokus, dialog native, pengumuman dan reduced motion |
| A25 | P2 | Semua aset lama dan setiap HTML lokasi ikut disalin | Hanya aset direferensikan dan daftar halaman publik |
| A26 | P2 | Halaman offline masih ANEMOS dan bukan keluaran builder akhir | Generate fallback LANGIT; seluruh URL halaman lama dipertahankan |

Seluruh kelompok sudah mendapat implementasi. Bagian visual A24 dan
perubahan interaksi lainnya masih memerlukan gate browser kandidat.

## E. Implementasi dan preservasi

Definisi peluang anggota dan penyeimbangan keluarga tidak diganti. Mesin
mendapat validasi waktu, penanganan awan/angin kosong, urutan anggota yang
deterministik dan preservasi ekspor parsial.

Builder menjadi sumber bersama tampilan/CSV. Grafik hanya membawa kolom
yang dipakai, sementara metadata lengkap tetap ada pada API/GeoJSON.
Paket staging terpisah dari sumber.

Pada commit kode, tree `outputs` tetap
`b43ec824a48111a0d79ae50055e9c0f3833ee4fd` dan tree trading tetap
`4a89ba19f15e47edf6a7d901848f37249d5e5f9d`, identik dengan baseline.
Dengan demikian seluruh subtree data dan trading tetap utuh di GitHub.

## F. Validasi ilmiah

- 216 jam dan 1.080 nilai suhu, RH, peluang, angin dan awan cocok dengan
  sembilan JSON Sentinel sumber.
- 864 distribusi diperiksa untuk normalisasi bobot, anggota unik dan urutan
  kuantil. Jumlah kategori dan urutan peluang pelampauan juga diperiksa.
- Boundary 0%, <1%, 100%, anggota tunggal, hujan ekstrem, data kosong, semua
  provider gagal, CRPS manual dan keseimbangan keluarga diuji offline.
- Dago 62 jam usable/10 limited; Jatinangor 62/10; Arjawinangun 60/12.
- Enam sampel arsip cocok dengan SHA blob dan dapat dibaca kembali; ini
  membuktikan integritas sampel, bukan kebenaran atmosfernya.

Contoh: Dago pukul 16.00 memiliki PoP 86,0130718954%, median akumulasi
1,7 mm untuk satu jam, dan proporsi anggota untuk ≥10 mm sebesar 0% pada
snapshot tersebut. Angka terakhir bukan jaminan hujan lebat mustahil.

Seluruh ringkasan verifikasi baseline memiliki **0 pasangan pengamatan yang
memenuhi syarat**. Tidak ada klaim peningkatan MAE, kalibrasi peluang atau
skill lapangan. Tes CRPS memeriksa rumus, bukan akurasi prakiraan.

[Dokumentasi Ensemble API Open-Meteo](https://open-meteo.com/en/docs/ensemble-api)
digunakan untuk satuan/interval. Grid hourly API tidak otomatis dianggap
sebagai resolusi native hourly model.

## G. UI/UX dan batas browser

Produksi benar-benar dibuka dan screenshot viewport diamati. Galat ekstensi
browser dipisahkan dari galat aplikasi. Navigasi/lapisan yang diuji
dijelaskan pada cakupan, bukan digeneralisasi ke semua kontrol.

Skrip hasil generator dieksekusi pada fixture DOM untuk kegagalan Leaflet,
jam/lokasi kosong, origin pesan, play/pause, nilai nol, gap, parameter dan
rescaling. Koordinat grafik diuji pada lebar 320, 360, 375, 390, 430, 768,
1024, 1440 dan 1920 pixel. **Ini bukan sembilan pengujian viewport browser.**

Binary Chromium lokal tidak tersedia; unduhan menghasilkan ZIP tidak valid.
Browser cloud menolak navigasi file lokal melalui kebijakan protokol.
Tidak dilakukan workaround atas pembatasan itu.

Responsivitas kandidat, screenshot sesudah perubahan, dialog mobile,
pembaca layar, audit WCAG 2.2 AA penuh, Firefox dan WebKit:
**NOT VERIFIED**.

## H. Hasil tes dan reproduksi

```bash
python -m unittest discover -s tests -v
node --test tests/public_data.test.js
python -m compileall -q -x 'outputs|_site|\.git' .
python langit_v65_cinematic_rebuild.py --root outputs --verify-only
python check_fixes.py --root outputs
git diff --check
```

Hasil: **55/55 tes Python lulus**, tanpa gagal/skip; enam subtes Node
dijalankan di dalam suite. Seluruh 29 blok JS inline (11 unik) dan dua aset
JS bersama hasil build lolos `node --check`. CI commit kode juga lulus.

```bash
LANGIT_SOURCE_SHA=6754eba6ef5e00c27432ad4335fda4008f95acb4 \
  python langit_v65_cinematic_rebuild.py --root outputs \
  --public-base-url https://marcooo20-d.github.io/weather-forecast
python -m site_tools.public_site --source outputs --destination _site
python -m http.server 8000 --directory _site
```

Tujuan staging harus baru. `check_fixes.py` membuat salinan sementara yang
dimilikinya sendiri dan tidak meregenerasi sumber.

## I. Performa terukur

| Ukuran isi tanpa kompresi | Rebuild baseline | Kandidat, input repo lengkap |
| --- | ---: | ---: |
| Berkas | 128 | 129 |
| Paket | 3.571.470 byte | 3.671.507 byte |
| JS bersama | 31.930 byte | 26.306 byte |
| Seluruh HTML | 1.865.230 byte | 1.553.981 byte |
| Rebuild, satu pengukuran | 0,894 detik | 0,921 detik |
| Staging/validasi, satu pengukuran | 0,208 detik | 0,353 detik |

Total bertambah sekitar 2,8% karena provenance/data konsisten bertambah.
HTML/JS mengecil, sementara gate memeriksa lebih banyak hal. Pengukuran
tunggal bukan benchmark statistik atau bukti perbaikan Core Web Vitals.

Paket produksi yang diambil lewat HTTP berukuran 3.575.224 byte. Angka
rebuild lokal dan publikasi sebenarnya tidak disamakan. Log produksi
menunjukkan checkout sekitar empat menit, forecast sekitar 12,5 menit dan
publikasi branch kecil sekitar dua detik. Checkout historis tetap mahal.

LCP, INP, CLS, Lighthouse dan durasi deploy kandidat: **NOT VERIFIED**.

## J. GitHub dan deployment

Kode/tes berada pada commit di bagian A; dokumentasi ditambahkan sebagai
commit berikutnya. PR #3 clean/tanpa konflik saat diperiksa, tetap draft.
Workflow hanya mendapat provenance HEAD checkout; izin/jadwal/pola paket
kecil yang sudah bekerja dipertahankan.

**Belum merge/deploy.** Tidak ada force-push atau perubahan konfigurasi Pages
yang dijalankan audit ini. Setelah browser kandidat lolos dan merge
diotorisasi, workflow normal serta verifikasi manifest, hash publik,
browser dan unduhan harus dijalankan.

## K. Hal yang belum selesai

Tidak ada temuan dalam register yang sengaja dibiarkan tanpa implementasi.
Gate browser kandidat dan verifikasi publikasi tetap terbuka. Observasi
lokal diperlukan untuk pembuktian skill. Monolit berlapis override, biaya
checkout, ketergantungan CDN dan audit keamanan/riwayat yang lebih luas
merupakan batas atau pekerjaan lanjutan, bukan bagian yang diklaim selesai.

## L. Putusan

**NOT READY FOR PRODUCTION RELEASE — DRAFT REVIEW.**

Kode, tes, data hasil build dan CI menunjukkan perbaikan nyata. Kandidat
belum mendapat validasi browser yang diperlukan untuk menyatakan perubahan
antarmukanya siap terbit. Produksi tetap pada baseline terverifikasi dan
arsip dipertahankan.
