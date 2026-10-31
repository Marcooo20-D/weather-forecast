# LANGIT Weather Forecast

Prakiraan multi-sumber untuk Dago, Jatinangor, dan Arjawinangun. Mesin prakiraan
dan evaluasi ada di `weather_ensemble_multi_location.py`; tampilan publik
dibangun oleh `langit_v65_cinematic_rebuild.py`.

**Status 31 Oktober 2026:** v65.3.1 sudah diterbitkan.
[Bukti verifikasi rilis](docs/RELEASE_VERIFICATION_2026-10-31.md) mencatat
CI, publikasi, pemeriksaan resource HTTP, dan peta langsung.

## Pemeriksaan lokal

Python 3.12 dan Node.js diperlukan untuk menjalankan seluruh pemeriksaan.
Mesin memakai pustaka standar Python; `certifi` dapat dipasang untuk CA HTTPS.

```bash
python -m unittest discover -s tests -v
node --test tests/public_data.test.js
python -m compileall -q -x 'outputs|_site|\.git' .
```

Pengujian tidak mengambil data cuaca langsung dan tidak mengubah `outputs/`.

## Membuat website

Gunakan data `outputs/` yang ada atau jalankan mesin prakiraan terlebih dahulu.

```bash
python langit_v65_cinematic_rebuild.py --root outputs
python check_fixes.py --root outputs
python -m site_tools.public_site --source outputs --destination _site
python -m http.server 8000 --directory _site
```

`_site` harus berupa direktori baru. Pembuatan paket tidak menghapus atau
memindahkan data sumber. `_site/` berisi halaman, aset, API ringkas dan CSV
terkini; arsip anggota ensemble, ledger SQLite, log, serta payload mentah
tidak masuk ke paket publik.

## Workflow

- `Offline checks`: memeriksa perubahan PR dengan izin baca dan checkout kode
  saja, tanpa mengunduh arsip `outputs/` yang besar.
- `Browser release checks`: membangun kandidat dari snapshot API publik di
  repositori dan menguji interaksi Chromium pada 1440×900 dan 390×844.
  Screenshot, trace, dan laporan tersedia sebagai artifact run. Pustaka
  Leaflet asli dipasang dari npm; font eksternal dan tile sengaja diblokir
  agar pemeriksaan interaksi tidak bergantung pada layanan tersebut.
- `LANGIT Forecast and Pages`: berjalan pada jadwal yang ada, pemicu manual,
  atau perubahan kode produksi di `main`. Input lokasi: `all`, `dago`,
  `jatinangor`, `arjawinangun`, atau kombinasi dipisahkan koma.
- Workflow produksi menjalankan tes, forecast, sinkronisasi referensi,
  evaluasi, rebuild, validasi paket, penyimpanan hasil ke `main`, dan publikasi
  **hanya `_site/`** ke `gh-pages`.
- Jika `main` berubah ketika forecast berlangsung, push dapat ditolak. Workflow
  berhenti sebelum publikasi; jalankan ulang dari `main` terbaru.

Data historis tetap tersedia untuk audit dan evaluasi di `main/outputs/`.
Arsip forecast baru memakai `.json.gz` lossless; pembaca mendukung arsip lama
`.json` dan arsip baru. CSV terbaru menyimpan nilai skalar; distribusi lengkap
tetap tersimpan dalam JSON dan arsip immutable.

Lihat [audit terbaru](docs/AUDIT_LATEST.md) dan
[arsitektur/kontrak data](docs/ARCHITECTURE.md) untuk temuan, reproduksi dan
batas pemeriksaan. [Catatan v65.3](docs/RELEASE_NOTES.md) menjelaskan
pemeriksaan browser dan perbaikan tambahan. Status publikasi aktual
ditentukan dari workflow produksi/Pages dan manifest website.

[Audit 24 Oktober 2026](docs/AUDIT_2026-10-24.md) tetap disimpan sebagai
riwayat. Metrik akurasi membutuhkan observasi lokal
yang sesuai; sinkronisasi referensi model saja bukan bukti akurasi lapangan.

