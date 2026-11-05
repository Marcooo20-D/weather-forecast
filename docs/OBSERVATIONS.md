# Pengamatan dan evaluasi prakiraan

Mesin utama adalah `weather_ensemble_multi_location.py`. Website menyajikan
keluarannya; data pembanding tidak diambil dari tampilan website.

## Dua jalur yang berbeda

1. Pengukuran stasiun: suhu, kelembapan, dan angin hanya diterima jika sumber
   per variabel menyatakan pengukuran. Data Meteostat berlabel `metno_forecast`
   atau `dwd_mosmix` tidak dipromosikan menjadi observasi. Data hujan stasiun
   memerlukan batas interval yang eksplisit. Pemeriksaan fisik, waktu, lokasi,
   duplikat dan perubahan temporal tetap dilakukan oleh importer mesin.
2. Referensi hujan: NOAA CPC CMORPH ICDR ADJ adalah estimasi berbasis satelit
   dengan koreksi pengamatan, pada grid 0,25° dan interval satu jam. Ini bukan
   pengukuran penakar hujan kampus. Arsip menyimpan nilai sel native, batas sel,
   waktu akumulasi, URL, versi, waktu unduh dan SHA-256 sumber.

Pada pengambilan awal November 2026, sumber stasiun terdekat yang diperiksa
berisi isian model. Isian tersebut ditolak. Akses pengukuran lokal BMKG/ITB
masih dibutuhkan untuk membuktikan akurasi suhu, kelembapan, angin serta hujan
pada skala kampus. Evaluasi satelit tidak menghapus keterbatasan ini.

## Reproduksi

```sh
python -m pip install -r requirements.txt
python weather_ensemble_multi_location.py --mode sync-observations --locations all --lookback-days 14
python -m forecast_tools.verification --root outputs --locations all
python -m unittest discover -s tests -v
```

`sync-references` mempertahankan pengambilan referensi model historis lama.
Mode itu tidak menghitung data model sebagai pengamatan stasiun.

Untuk pengukuran resmi yang diperoleh kemudian, gunakan mode
`import-observations` dengan satu lokasi, CSV pengukuran dan metadata stasiun.
Kolom pengukuran: `observed_datetime` dengan zona waktu, `station_id`,
`latitude`, `longitude`, `elevation_m`, `timezone`, `source_id`, `available_at`,
`temp_c`, `temp_unit`, `rh_pct`, `rh_unit`, `wind_kmh`, `wind_unit`.
Hujan menambahkan `rain_mm`, `rain_unit`, `rain_interval_start` dan
`rain_interval_end`. Nilai kosong tetap kosong; jangan menggantinya dengan nol.

## Aturan evaluasi satelit

- Arsip prakiraan harus terbit setidaknya satu jam sebelum waktu berlaku.
- Hanya distribusi anggota nyata dengan sedikitnya dua keluarga dan lima
  anggota per model yang diterima. Identitas anggota tidak boleh duplikat.
- Interval distribusi dan referensi harus identik. Timestamp CMORPH menyatakan
  awal interval, sedangkan batas terakhirnya inklusif sampai detik 59; parser
  menormalisasinya menjadi batas akhir eksklusif satu jam.
- Angka NetCDF dibaca dengan skala dan mask bawaannya. `mm/hr` dikalikan durasi
  satu jam untuk memperoleh jumlah hujan; data hilang tidak menjadi nol.
- Pilih isu tertua yang layak untuk setiap jam berlaku dan kelompok lead time.
  Revisi prakiraan tidak menambah sampel untuk jam/kelompok yang sama.
- Brier Score memakai peluang dalam fraksi dan kejadian hujan >0,1 mm/jam.
  MAE/RMSE/bias memakai mean seluruh anggota, CRPS memakai distribusi berbobot.
- Kelompok lead dapat berbagi jam referensi. Jumlah kasus tidak sama dengan
  jumlah pengamatan independen. Laporan mencantumkan jam dan tanggal unik.
- Tidak ada pembobotan atau kalibrasi yang dilatih dengan laporan satelit ini.
  Hasilnya adalah kesesuaian terhadap referensi areal, bukan sertifikasi skill
  lokal atau bukti peningkatan dibandingkan layanan cuaca lain.

## Penyimpanan dan publikasi

`outputs/observations/cmorph/` memuat subset sel dari sumber global, bukan
seluruh NetCDF. `outputs/<lokasi>/satellite_verification.json` dan CSV pasangan
memuat ringkasan yang dapat diperiksa. Arsip anggota prakiraan tetap di main.
Hanya ringkasan dan pasangan yang masuk paket publik. Kegagalan pengambilan
tercatat sebagai data tidak tersedia dan tidak mematikan pembacaan prakiraan.

## Sumber

- https://ftp.cpc.ncep.noaa.gov/precip/CMORPH_RT/ICDR/0.25deg-HLY/
- https://www.ncei.noaa.gov/products/climate-data-records/precipitation-cmorph
- https://dev.meteostat.net/data/timeseries/hourly.html
- https://dev.meteostat.net/providers
- https://dev.meteostat.net/quality.html
- https://dataonline.bmkg.go.id/
