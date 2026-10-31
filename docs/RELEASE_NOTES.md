# LANGIT v65.3 — catatan rilis

## v65.3.1 — peta dasar

Pemeriksaan visual pada publikasi v65.3 menemukan basemap CARTO mengirim
gambar bertuliskan “API key required”. PNG tersebut berhasil dimuat, sehingga
jumlah tile yang selesai dimuat bukan bukti bahwa peta geografis tampil.
[CARTO mengharuskan kunci milik pengguna](https://www.carto.com/basemaps/apikey/index.html).

Basemap diganti dengan tile standar OpenStreetMap melalui URL HTTPS resmi.
Atribusi lisensi terlihat dan tertaut pada peta. Browser memakai cache dan
Referer normal; aplikasi tidak menyediakan unduhan/prefetch/offline tile.
CI tetap memblokir permintaan tile, sesuai batas pengujian di bawah.
Lihat [kebijakan tile OSM](https://operations.osmfoundation.org/policies/tiles/).
Pemeriksaan rilis harus mengamati nama wilayah/jalan pada screenshot peta
langsung, selain marker, kontrol, dan status HTTP.

## v65.3 — integritas data dan antarmuka

Data kosong, waktu berlaku, dan peluang hujan kini dipertahankan secara
konsisten antara API, CSV, grafik, serta peta. Peluang hujan dipisahkan dari
intensitas dan indeks gangguan aktivitas. Rincian 26 kelompok perbaikan
awal tercatat dalam [audit 30 Oktober](AUDIT_LATEST.md).

Pemeriksaan rilis 31 Oktober menambahkan dua perbaikan antarmuka:

- Panel peta dan timeline memakai `border-box`, sehingga padding/border
  masuk ke batas lebar layar ponsel.
- Menu navigasi ponsel yang tertutup memakai `visibility: hidden` dan
  `pointer-events: none`. Sebelumnya transform ke atas menempatkan panel
  tersebut di atas tombol Menu; Chromium menolak klik karena terhalang.

## Pemeriksaan sebelum penggabungan

PR harus memiliki `Offline checks` dan `Browser release checks` yang
berhasil untuk revisi akhirnya. Browser memeriksa tiga lokasi pada desktop
1440×900 dan ponsel 390×844: navigasi, tiga hari, empat variabel grafik,
detail per jam/dialog ponsel, produk aktivitas/data/akurasi, planner, peta
enam lapisan, pemilihan lokasi/jam, putar/jeda, serta fallback Leaflet.

Tes memakai hasil rebuild dari snapshot API publik yang tersimpan di repo.
Leaflet 1.9.4 asli dipasang dari npm untuk tes. Font dan tile eksternal
sengaja diblokir; pengujian ini memvalidasi interaksi dan marker, bukan
ketersediaan layanan basemap/font. Artifact `langit-browser-evidence`
menyimpan screenshot, trace, revisi sumber, versi Chromium, dan `report.json`.
Kegagalan tes menghentikan proses persiapan penggabungan.

Reproduksi pada mesin dengan Node/Python dan Chromium yang tersedia:

```bash
npm install --no-save playwright@1.62.1 leaflet@1.9.4
npx playwright install --with-deps --only-shell chromium
python langit_v65_cinematic_rebuild.py --root outputs
python -m site_tools.public_site --source outputs --destination _site
node tests/browser_smoke.cjs _site browser-evidence
```

## Publikasi dan batas hasil

Merge normal ke `main` memicu `LANGIT Forecast and Pages`. Workflow
mengambil prakiraan baru, menjalankan validasi, menyimpan data historis,
dan menerbitkan paket terbatas ke `gh-pages`. Tidak ada penggabungan isi
`main` langsung ke branch publikasi. Keberhasilan rilis harus dipastikan
dari hasil workflow, Pages, manifest v65.3/revisi sumber, dan website langsung.

Chromium pada dua ukuran layar tidak membuktikan pengujian Firefox,
WebKit, seluruh perangkat, pembaca layar, WCAG penuh, atau Core Web Vitals.
Kecocokan forecast dengan observasi lokal tetap memerlukan data verifikasi;
rilis ini tidak membuktikan kalibrasi peluang atau peningkatan akurasi cuaca.
