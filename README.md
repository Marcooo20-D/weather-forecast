# LANGIT

Prakiraan cuaca untuk Dago, Jatinangor, dan Arjawinangun, dengan identitas kampus ITB.

**[Buka website](https://marcooo20-d.github.io/weather-forecast/)**

## Fitur

- Prakiraan per jam dan tiga hari, peluang hujan, serta panduan aktivitas.
- Peta GFS dengan aliran angin, suhu, kelembapan, awan, dan hujan satu jam.
- Berita meteorologi Bandung/Jawa Barat dan Indonesia dari BMKG serta ANTARA.
- Status sumber, evaluasi, dan unduhan CSV, JSON, serta GeoJSON.

## Data

Prakiraan lokasi menggabungkan beberapa sumber melalui
`weather_ensemble_multi_location.py`. Lapisan regional memakai grid permukaan
**NOAA GFS sekitar 13 km** melalui [Open-Meteo](https://open-meteo.com/en/docs/gfs-api).
Angin ditampilkan pada 10 m, suhu dan kelembapan pada 2 m. Hujan peta adalah
akumulasi satu jam dalam mm; peluang hujan lokasi tetap dalam persen.

Aliran angin menunjukkan arah dan kecepatan relatif hasil interpolasi komponen
u/v. Area tanpa data tidak diekstrapolasi. Waktu pengambilan data dibedakan dari
waktu berlaku prakiraan. Cache grid dipertahankan maksimal 18 jam saat sumber gagal.

Berita menyertakan judul, tanggal, sumber, dan tautan artikel asli. Pembaruan
berjalan setiap jam; waktu terbit artikel tetap mengikuti penerbit.

Data Open-Meteo: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
[Sumber dan hak foto](site_tools/campus_assets/credits.json) dicatat per gambar.
Proyek independen mahasiswa Meteorologi ITB; bukan layanan resmi ITB atau BMKG.

## Pengembangan

Python 3.12 dan Node.js diperlukan untuk seluruh pemeriksaan. Dengan data lokasi
tersedia di `outputs/`, jalankan:

```bash
python -m site_tools.weather_field --root outputs
python -m site_tools.weather_news --root outputs
python build_weather_site.py --root outputs
python -m site_tools.public_site --source outputs --destination _site
python -m http.server 8000 --directory _site
```

Gunakan direktori baru untuk `_site`. Mesin prakiraan dan evaluasi dijelaskan di
[panduan arsitektur](docs/ARCHITECTURE.md).

## Struktur

| Bagian | Lokasi |
| --- | --- |
| Mesin prakiraan | `weather_ensemble_multi_location.py` |
| Pembangun website | `build_weather_site.py` |
| Antarmuka | `site_tools/campus_*`, `editorial.css` |
| Peta dan grid model | `site_tools/forecast_map.js`, `weather_field.py`, `weather_field.js` |
| Berita | `site_tools/weather_news.py` |
| Pengujian | `tests/` |
| Pembangun terdahulu | `legacy/` |

## Pengujian dan publikasi

```bash
python -m unittest discover -s tests -v
node --test tests/public_data.test.js tests/weather_field.test.js
```

GitHub Actions memeriksa kode serta interaksi Chromium pada lebar 1440, 768,
430, 390, 360, dan 320 piksel, termasuk emulasi sentuh pada ponsel.
Workflow produksi memperbarui prakiraan dan grid, mengevaluasi
data, lalu memvalidasi dan menerbitkan website ke `gh-pages`.

Perubahan antarmuka memakai prakiraan tersimpan melalui workflow publikasi
terpisah. Waktu terbit dan status model tetap dipertahankan. Pembaruan prakiraan,
antarmuka, dan berita memakai antrean publikasi yang sama.

Arsip prakiraan, observasi, dan evaluasi tetap tersimpan di `main/outputs/`.
Hanya halaman, aset, dan data publik terkini yang diterbitkan. Catatan audit dan
verifikasi rilis tersimpan di `docs/`.

## Observasi dan verifikasi

Mesin prakiraan kini memiliki jalur pengukuran stasiun yang menyaring isian model serta evaluasi hujan terpisah terhadap referensi NOAA CPC CMORPH. Metode, batas penggunaan, dan perintah reproduksi tersedia di [dokumentasi observasi](docs/OBSERVATIONS.md). Referensi satelit tidak dinyatakan sebagai pengamatan stasiun kampus.
