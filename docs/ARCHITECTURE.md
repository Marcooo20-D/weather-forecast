# Arsitektur dan kontrak LANGIT

## Jalur produksi

`.github/workflows/main.yml` berjalan pada `main`, jadwal atau pemicu manual.
`site_tools.workflow_inputs` memeriksa lokasi/debug sebelum shell. Tes dan
sintaks mendahului forecast.

`weather_ensemble_multi_location.py` menangani sumber deterministik,
anggota ensemble, waktu, arsip dan evaluasi. Sinkronisasi referensi model
tidak setara dengan pengamatan lokal untuk pembuktian skill.

Mesin masih menghasilkan beberapa tampilan lama. Builder akhir
`build_weather_site.py` kemudian membangun publikasi dari produk
Sentinel bertanggal. Perbaikan tampilan publik dilakukan pada builder akhir
tersebut, bukan hanya pada HTML yang akan ditimpa run berikutnya.

`site_tools.public_site` menyalin berkas publik ke tujuan baru dan memeriksa
batas 50 MiB/1.000 berkas, tautan, JSON dan kontrak data. Hanya salinan itu
diterbitkan. `main/outputs` mempertahankan bukti operasional/historis;
`gh-pages` berisi hasil publikasi.

`.github/workflows/presentation.yml` menerbitkan perubahan antarmuka dari
snapshot publik terakhir. Checkout hanya memuat kode, aset, dan input publik;
arsip anggota tidak diambil. Builder mempertahankan waktu terbit dan status
ensemble per hari dari API tersimpan saat payload Sentinel tidak tersedia.
Payload Sentinel yang tersedia tetap mendapat prioritas. Jalur ini tidak
menjalankan model atau menulis ulang arsip prakiraan di `main`.

Workflow prakiraan, antarmuka, dan berita memakai grup konkurensi yang sama
agar publikasi berjalan bergantian.

## Sumber kebenaran

| Produk | Sumber |
| --- | --- |
| Angka per jam | Sentinel JSON bertanggal; CSV/API lama sebagai fallback format yang diketahui |
| Status provider | CSV/status payload dengan tanggal berlaku yang sama |
| API, HTML dan CSV tampilan | Normalisasi akhir `build_weather_site.py` |
| GeoJSON portal | Gabungan serializer GeoJSON lokasi yang sama |
| Grafik | Kolom minimum dari API, melalui `site_tools/public_data.js` |
| Skill | Forecast sebelum kejadian dan observasi yang lolos QC/pencocokan |
| Provenance build | HEAD checkout workflow; build time dipisahkan dari issue |

## Kontrak publik

| Field | Makna/satuan |
| --- | --- |
| `temp_c`, `heat_index_c` | °C; indeks panas kosong tidak diganti suhu udara |
| `humidity_pct`, `cloud_pct` | Persen 0–100 |
| `rain_probability` | Persen 0–100; 0,5 berarti 0,5%, bukan 50% |
| `rain_amount_p50_mm` | Median mm untuk interval yang dicatat |
| `rain_interval_start/end/minutes` | Batas akumulasi; bukan inisialisasi model |
| `wind_kmh` | km/jam |
| `wind_dir` | Derajat meteorologis dari arah datang; tenang/tidak diketahui kosong |
| `risk_score` | Indeks heuristik gangguan aktivitas, bukan peluang cuaca ekstrem |
| `date_iso` + `hour` | Waktu berlaku Asia/Jakarta |
| `issued_at` | Issue run, tidak otomatis waktu inisialisasi model provider |
| `generated_at_iso` | Alias issue terbaru dalam snapshot; bukan waktu berlaku |
| `built_at` | Waktu pembuatan produk, tidak memperbarui umur forecast |
| `valid`, `data_quality` | Kelayakan/cakupan; nilai terbatas dapat tetap dicantumkan |
| `probability_basis`, `calibration_status` | Asal perhitungan/status kalibrasi, tanpa menjanjikan skill |

Nol berbeda dari kosong. JSON memakai `null`, CSV sel kosong. Jam hilang
tidak berubah menjadi 00.00. Duplikat timestamp dan JSON sumber rusak
menghentikan build. Batas fisik salah tidak disamarkan dengan clipping.

Peta menggabungkan titik prakiraan kampus dengan grid regional GFS 13 km
yang terpisah. Medan warna memakai interpolasi bilinear; jejak angin memakai
komponen u/v pada 10 m. Nilai di luar grid atau tanpa data tidak diisi.
Hujan grid adalah akumulasi satu jam dalam mm, sedangkan peluang hujan
lokasi dalam persen. [Konvensi peta dan sumber berita](REGIONAL_MAP_AND_NEWS.md)
memuat satuan serta batas visualisasi. Kegagalan tile/Leaflet tetap
menyediakan angka teks dan tautan data/prakiraan.

## Pengembangan

Perintah reproduksi ada pada README dan [audit terakhir](AUDIT_LATEST.md).
`check_fixes.py` adalah gate paket yang tidak mengubah sumber. Tes Node
memakai pustaka bawaan.

`Browser release checks` memakai Playwright 1.62.1 dan Chromium headless
serta Leaflet 1.9.4 asli. Checkout hanya mengambil kode dan snapshot publik
ringkas; arsip anggota dan pengambilan ulang provider tidak dibutuhkan.
Tes memeriksa navigasi tiga lokasi, tab hari, grafik, detail jam, planner,
timeline/pemilihan lokasi/lapisan peta, dan fallback kegagalan Leaflet.
Browser memakai ukuran 1440×900, 768×1024, 430×932, 390×844, 360×800 dan 320×780.
Empat ukuran ponsel menggunakan emulasi sentuh.
Pemeriksaan mencakup animasi/jeda angin, slider waktu, panel ponsel,
filter berita, dan kegagalan grid regional. Screenshot dan trace adalah
artifact CI, bukan isi publikasi. Font/tile eksternal diuji terpisah pada
website langsung; hasil tes ini tidak mengklaim kompatibilitas lintas browser.

Arsip tidak ditulis ulang agar metrik tampak lebih baik. Kalibrasi/skill
memerlukan verifikasi terpisah. Builder warisan dan strategi trading berada
di luar jalur publikasi aktif yang dijelaskan di sini.
