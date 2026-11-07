# Migrasi LANGIT: sumber privat, hasil prakiraan publik

## Status dan prinsip

Repo sumber `main` saat ini mengandung mesin prakiraan, builder HTML/CSS/JS, kode historis, tests, workflow dan arsip. Menghapus file dari commit terbaru atau menambahkan `.gitignore` **tidak** membuat sejarah repo publik menjadi rahasia.

Deployment sudah memisahkan salinan hasil dalam `_site` via `site_tools.public_site`; branch `gh-pages` seharusnya hanya menampung produk browser yang memang publik. HTML/CSS/JS yang diunduh browser, serta JSON/CSV yang sengaja dipublikasikan, akan selalu dapat dilihat pengguna. Jangan menaruh kunci, token, algoritma rahasia, atau data internal di salinan `_site`.

## Opsi 1 — GitHub Pro (periksa paket akun terlebih dahulu)

Jika GitHub Pages tersedia untuk repository privat pada paket akun pemilik, simpan repository ini dengan nama lama dan ubah visibility menjadi **Private** via Settings → General → Danger Zone. Konfirmasi GitHub Pages tetap dipublikasikan secara public dan pemicu terjadwal tetap berjalan. Jangan mengaktifkan pengaturan akses privat untuk *website* jika pengunjung harus tetap bisa membukanya.

Opsi ini tidak memerlukan perubahan `PUBLIC_PAGES_REPOSITORY` dan tidak memerlukan token tambahan. Jangan memilih opsi ini jika perubahan visibility akan membuat GitHub Pages unpublish pada paket akun yang dipakai.

## Opsi 2 — GitHub Free (direkomendasikan bila ingin URL tetap sama)

Target akhir:
- `Marcooo20-D/weather-forecast-source`: **PRIVATE**. Semua kode Python, backend model, builder website, workflow, tests, dan sejarah repo lama.
- `Marcooo20-D/weather-forecast`: **PUBLIC**. Repository publik *baru*, berisi hasil `gh-pages` saja (HTML/CSS/JS browser, aset, produk publik terkini), tanpa source code Python, tests, atau sejarah repo sumber.
- Website tetap `https://marcooo20-d.github.io/weather-forecast/`.

### Persiapan aman

1. **Cadangkan** repository lama secara lengkap dan validasi cadangan (misalnya `git clone --mirror` ke lokasi aman). Jangan mengunggah cadangan ke repository publik.
2. Merge PR migration ini **setelah diperiksa**. Selama variable `PUBLIC_PAGES_REPOSITORY` belum diisi, workflow tetap menayangkan ke `gh-pages` di repo lama seperti sebelumnya.
3. Buat repo publik sementara baru bernama `weather-forecast-site` berisi README sederhana saja. **Jangan** fork, template-copy, atau push riwayat source repo lama ke repo publik ini.
4. Buat GitHub fine-grained PAT dengan akses **hanya** ke repo publik tujuan, permission **Contents: Read and Write**; masa berlaku sesingkat yang praktis. Tambahkan sebagai Actions repository secret `PUBLIC_PAGES_DEPLOY_TOKEN` di repo sumber. Jangan menaruh token di file, URL, commit, atau log.
5. Tambahkan Actions repository variable `PUBLIC_PAGES_REPOSITORY=Marcooo20-D/weather-forecast-site` pada repo sumber. Jalankan workflow **Publish interface** secara manual, verifikasi branch `gh-pages` baru hanya berisi produk publik, dan arahkan Settings → Pages → Deploy from branch `gh-pages`, folder `/(root)` di repo publik sementara. Uji laman dan file produk secara langsung.
6. Sesudah siap cutover: rename repo sumber lama menjadi `weather-forecast-source`, lalu ubah visibility-nya menjadi **Private**. Rename repo publik baru `weather-forecast-site` menjadi **weather-forecast**. Pastikan tidak ada repo publik lain yang menyimpan copy source. Akan ada kemungkinan jeda singkat propagasi GitHub Pages.
7. Ubah repo variable pada repo sumber privat menjadi `PUBLIC_PAGES_REPOSITORY=Marcooo20-D/weather-forecast`. Jalankan **Publish interface** atau workflow utama secara manual, pastikan branch `gh-pages` public terbarui, Pages source masih `gh-pages /(root)`, dan URL website lama berfungsi. Cek 3 lokasi, peta, berita, JSON/CSV publik, serta pembaruan otomatis berikutnya.
8. Uji semua workflow: `LANGIT Forecast and Pages` (pembaruan berkala), `Meteorology headlines` (per jam), `Publish interface`, `Browser release checks`, `Offline checks`. Jangan pindahkan token dari repository secret ke environment yang tidak tersedia untuk workflow ini.
9. Periksa Actions billing untuk repository **private** (pemakaian runner dapat dihitung menurut kuota akun). Buat prosedur rotasi PAT; jika history publik pernah berisi kredensial asli, rotasi/revoke kredensial tersebut sekalipun repo sudah private.

### Mode konfigurasi workflow

- Variabel `PUBLIC_PAGES_REPOSITORY` kosong: deploy ke `gh-pages` dalam repo sendiri memakai `GITHUB_TOKEN` (perilaku lama).
- Variabel terisi: deploy ke `gh-pages` repo publik tujuan memakai secret `PUBLIC_PAGES_DEPLOY_TOKEN`.
- `main.yml` dan `presentation.yml` menggunakan `force_orphan: true` agar hanya produk deployment terkini yang ada di riwayat branch deploy. `news.yml` memakai `keep_files: true` untuk memperbarui berita tanpa menghapus situs.
- Token `GITHUB_TOKEN` yang dibuat GitHub Actions **tidak** boleh diandalkan untuk menulis repository publik lain; karena itu perlu token yang aksesnya terbatas pada tujuan.

### Pemeriksaan keamanan tambahan

- Verifikasi `_site` tidak menyertakan `.py`, `.github/workflows`, `tests`, `legacy`, `docs`, notebook, file rahasia, dump mentah, atau artefak tracing/debug.
- Periksa dan minimalkan JSON/CSV/GeoJSON yang bisa diunduh bila ada data yang tidak ingin diekspos.
- Hindari memberi akses tulis dari pengguna website ke repository sumber. Jangan mengirim secrets lewat JS browser.
- Repo source pernah public; orang yang sudah melakukan clone, cache, atau fork mungkin tetap memiliki versi lamanya. Perubahan visibility tidak mencabut salinan yang sudah tersebar.
- Repository publik **baru** harus dibuat tanpa commit history dari repo lama. Menghapus file pada commit terbaru saja tidak cukup untuk menghapus source dari sejarah repo publik.

Dokumentasi: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility dan https://github.com/peaceiris/actions-gh-pages#-deploy-to-external-repository-external_repository .
