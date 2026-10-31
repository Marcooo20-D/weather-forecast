# Verifikasi rilis LANGIT — 31 Oktober 2026

**LANGIT v65.3.1 sudah diterbitkan dan diverifikasi dalam cakupan berikut.**
Website: [LANGIT](https://marcooo20-d.github.io/weather-forecast/).

| Bukti | Hasil |
| --- | --- |
| Penggabungan | [PR #3](https://github.com/Marcooo20-D/weather-forecast/pull/3) dan [PR #4](https://github.com/Marcooo20-D/weather-forecast/pull/4) merged |
| Revisi sumber | `3a97819cd584b12e1b13a6a9a07b933a5ce9c55a` |
| gh-pages | `9a07a8e6fb30ff3ccdc2d6be8f4d61929ab67c29` |
| Offline | [55 tes lulus](https://github.com/Marcooo20-D/weather-forecast/actions/runs/41181203704) |
| Browser kandidat | [48 pemeriksaan Chromium lulus](https://github.com/Marcooo20-D/weather-forecast/actions/runs/41181203633), 1440×900 dan 390×844 |
| Produksi | [Forecast, build, validasi dan publikasi berhasil](https://github.com/Marcooo20-D/weather-forecast/actions/runs/41181325643) |
| Pages | [Deployment berhasil](https://github.com/Marcooo20-D/weather-forecast/actions/runs/41182602315) |
| HTTP | 128/128 resource cocok dengan SHA publikasi; manifest v65.3.1 dan kontrak data lulus |

Paket publik berisi 129 berkas termasuk `.nojekyll`. Salinan HTTP memuat
128 berkas / 3,670,222 byte, diperiksa pada 31 Oktober 2026 14:59:33 WIB.
CSV, API dan GeoJSON termasuk dalam pemeriksaan hash serta kontrak.

## Browser dan perbaikan susulan

Portal, halaman utama/tiga hari/aktivitas ketiga lokasi, pergantian hari dan
grafik Dago, sumber/akurasi Dago, serta enam lapisan/timeline/putar-jeda peta
diperiksa pada publikasi v65.3. Menu tertutup yang menghalangi tombol Menu
dan ukuran panel peta ponsel diperbaiki dan diuji melalui Chromium kandidat.

Pemeriksaan visual publikasi menemukan basemap CARTO mengirim PNG dengan
watermark “API key required”. Karena gambar berhasil dimuat, jumlah tile
termuat saja bukan bukti peta geografis tampil. Hotfix v65.3.1 memakai OSM
standar, dengan atribusi terlihat dan tertaut. Pada website final, nama
wilayah/jalan Bandung, Sumedang, Jatinangor dan Arjawinangun benar-benar
diamati; terdapat 35 tile dan tiga marker pada viewport portal. Selector
ketiga lokasi memperbarui nama serta nilai HUD dengan benar. Portal
menampilkan v65.3.1 dan waktu terbit prakiraan baru. Galat ekstensi browser
dipisahkan dari sampel log aplikasi yang diamati.

## Batas hasil

CI memakai Leaflet asli dari npm dan memblokir font/tile eksternal; basemap
geografis diperiksa pada website langsung. Chromium dan dua ukuran layar
tidak membuktikan kompatibilitas Firefox/WebKit, semua perangkat, pembaca
layar, WCAG penuh atau Core Web Vitals. Observasi lokal belum cukup untuk
klaim kalibrasi peluang atau skill prakiraan.

[Catatan rilis](RELEASE_NOTES.md) menjelaskan perubahan.
[Bukti terstruktur](release-evidence-20261031.json) memuat manifest dan
hash seluruh resource. [Audit awal](AUDIT_LATEST.md) tetap disimpan sebagai
riwayat pemeriksaan 30 Oktober.
