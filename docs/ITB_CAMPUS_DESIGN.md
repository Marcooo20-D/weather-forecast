# LANGIT Campus — pembaruan visual v66

## Arah desain

Fotografi arsitektur asli Ganesha, Jatinangor dan Cirebon menjadi identitas
halaman; data dibaca pada bidang putih dengan aksen biru kobalt. Tipografi
sistem, jarak yang konsisten, angka tabular, navigasi kampus dan tombol yang
jelas menggantikan latar partikel serta panel neon.

Referensi yang ditelaah:

- [Apple iPhone](https://www.apple.com/id/iphone/): hierarki gambar, judul dan tindakan yang terarah.
- [Apple HIG Layout](https://developer.apple.com/design/human-interface-guidelines/layout): pengelompokan, kesejajaran dan ruang baca.
- [Met Office Bandung](https://weather.metoffice.gov.uk/forecast/qqu88vxyw): ringkasan hari, rincian jam, satuan dan peluang yang terpisah.
- [ITB Multikampus](https://itb.ac.id/multikampus): identitas dan lokasi tiga kampus.

Warna web adalah pilihan desain kobalt, bukan klaim kode hex resmi ITB.
Simbol atap LANGIT merupakan markah produk sendiri, bukan lambang resmi
institusi. Footer menjelaskan proyek independen mahasiswa.

## Foto dan makna lokasi

Foto berbeda pada setiap halaman lokasi. Ganesha dipakai sebagai identitas
kampus untuk Dago; label **Titik prakiraan: Dago, Bandung** tetap terlihat.
Koordinat model tidak dipindahkan ke posisi bangunan pada foto. Foto
merupakan dokumentasi, bukan webcam atau bukti cuaca saat ini.

[Metadata foto](../site_tools/campus_assets/credits.json) mencatat sumber,
penulis, ukuran, pengolahan dan status hak masing-masing. Ganesha oleh
A2613 memakai CC BY-SA 4.0, termasuk turunannya. Foto Jatinangor dan
Arjawinangun adalah dokumentasi publik ITB; sumber tidak menyatakan lisensi
terbuka, dan tidak dilabeli sebagai Creative Commons. Kredit sumber serta
hak asal dipertahankan. Tidak ada gambar kampus yang dibuat generatif.

Gambar responsif memakai WebP, ukuran eksplisit, prioritas hero dan lazy
loading kartu. Semua varian picture/srcset harus masuk paket publik.

## Preservasi fitur dan data

Mesin prakiraan dan `site_tools/public_data.js` tidak diubah. Halaman hari
ini, tiga hari, aktivitas, detail per jam, empat variabel grafik, enam
lapisan peta, timeline, putar/jeda, planner, sumber/akurasi, CSV, JSON,
GeoJSON dan seluruh alias URL tetap tersedia. Pilihan kampus memakai link
sesungguhnya; ringkasan memiliki tombol salin dengan fallback pemilihan teks.

Renderer aktif yang dipanggil workflow tetap
`langit_v65_cinematic_rebuild.py`. Komponen dan aset visual dipisahkan ke
`site_tools/campus_components.py`, `campus_ui.css`, `campus_ui.js`, dan
`campus_map.css`. Data ilmiah tidak menjadi bagian modul desain tersebut.

Perbandingan awal dengan input dan waktu yang sama memeriksa 53 produk
JSON/GeoJSON/CSV/teks: angka dan isinya sama setelah mengabaikan identitas
versi. Semua 71 URL HTML dipertahankan. Pengujian paket juga memeriksa
gambar responsif dan tujuan navigasi yang benar-benar tersedia.

## Pemeriksaan rilis

Jalankan seluruh regresi Python/Node serta rebuild/validasi paket. Chromium
menguji 1440×900, 768×1024, 390×844 dan 320×780: navigasi, data, grafik,
dialog, peta, pilihan kampus, foto yang terdekode, dan umpan balik tombol
salin. Screenshot ditelaah sebelum merge. Fitur yang belum dapat diperiksa
tetap harus diberi batas, bukan dianggap lulus. Hasil visual tidak
membuktikan peningkatan akurasi prakiraan atau kalibrasi peluang.
