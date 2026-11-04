# Tata letak ponsel

Perubahan untuk lebar di bawah 768 px berada di `site_tools/mobile.css`.
Tampilan desktop tetap memakai stylesheet kampus dan editorial yang sama.

- Navigasi utama selalu terlihat. Pilihan kampus tetap tersedia di header.
- Tombol utama memiliki tinggi minimal 44 px. Label formulir tidak memicu
  pembesaran otomatis pada ponsel.
- Ringkasan dan teks pendukung menggunakan ukuran yang lebih terbaca.
  Pilihan variabel grafik dapat digulir mendatar.
- Ketukan pada grafik menampilkan nilai untuk jam terdekat. Data kosong
  tetap kosong; pembacaan tidak mengambil nilai dari jam lain.
- Dialog per jam memakai dua kolom nilai dan menahan gulir halaman di
  belakangnya. Tombol tutup tetap berada di bagian atas dialog.
- Peta tertanam diaktifkan melalui tombol Jelajahi peta. Tombol Kembali ke
  halaman memulihkan gulir halaman. Peta penuh tetap dapat dibuka.

Perhitungan prakiraan dan grid model tidak diubah. Pengujian browser mencakup
320, 360, 390, 430, 768, dan 1440 px; viewport ponsel menggunakan emulasi sentuh.
