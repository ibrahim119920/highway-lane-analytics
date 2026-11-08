# Demo untuk recruiter / reviewer

Dua hal yang bisa dicoba orang lain tanpa menyentuh server:

1. **Dashboard** (publik, tanpa login): <https://lane.syaifudin-fam.my.id>
2. **Notebook** (Colab/Kaggle): `notebooks/demo_recruiter.ipynb` menjalankan pipeline
   penuh pada klip 55 detik dari kamera KM 58+400, lalu menampilkan video anotasi dan
   tabel hasil.

---

## 0. Untuk pemilik proyek - persiapan sekali saja

### 0.1 Pastikan dashboard sudah berisi data

Dashboard membaca Parquet silver/gold. Kalau tampilannya masih

> "The data export is not ready yet. Run ingest / rebuild-gold first."

maka belum ada run yang diekspor. Kirim satu run dulu:

```powershell
$env:PIPELINE_WEBHOOK_TOKEN = "<token dari server>"

lane-analytics replay `
  --events  output/events/<run_id>.jsonl `
  --tracks  output/tracks/<run_id>.jsonl `
  --summary output/reports/<run_id>/summary.json `
  --run-config output/reports/<run_id>/run_config.json `
  --stats
```

Tunggu +-60 detik (export berjalan tiap 500 pesan atau 60 detik). Kalau dashboard
tetap kosong, di server jalankan consumer/export:

```bash
sudo systemctl status lane-consumer
sudo journalctl -u lane-consumer -n 100 --no-pager
# atau, sesuai README pipeline:
lane-pipeline ingest ...     # backfill dari file
lane-pipeline rebuild-gold   # bangun ulang tabel agregat
```

Jangan menganggap "tidak muncul" berarti gagal: cek `GET /stats` dulu.

### 0.2 Publikasikan notebook Kaggle

1. Buka Kaggle -> **Create -> Notebook -> File -> Import Notebook -> GitHub**.
2. Masukkan URL:
   `https://github.com/ibrahim119920/highway-lane-analytics/blob/master/notebooks/demo_recruiter.ipynb`
3. Di cell **2. Settings**, isi:
   - `PIPELINE_TOKEN = "<token>"` (lihat catatan keamanan di bawah),
   - `REPLAY = True` bila ingin hasil recruiter muncul di dashboard,
   - biarkan `REPLAY = False` bila hanya ingin demo lokal.
4. **Save Version -> Save & Run All**, lalu **Share -> Public**.
5. Salin link notebook, mis. `https://www.kaggle.com/code/<user>/<slug>`, dan tempel
   di CV/LinkedIn/portfolio.

> Kaggle Secrets tidak bisa dipakai recruiter karena secret terikat ke akun pemilik.
> Karena itu token ditulis di cell notebook. Ini **sengaja** mengorbankan keamanan:
> siapa pun yang membuka notebook bisa mengirim run. Ganti token di server bila
> disalahgunakan.

### 0.3 Bagikan link Colab (opsional)

Tambahkan badge di README/portfolio:

```markdown
[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/ibrahim119920/highway-lane-analytics/blob/master/notebooks/demo_recruiter.ipynb)
```

Colab membuka notebook langsung dari GitHub. Token tidak ikut terkirim, jadi
recruiter hanya bisa menjalankan demo lokal (`REPLAY=False`). Untuk upload dari
Colab, mereka harus menyalin notebook dan mengisi token sendiri.

---

## 1. Untuk recruiter - mencoba dashboard

1. Buka <https://lane.syaifudin-fam.my.id>. Tidak perlu login.
2. Halaman yang bisa dilihat:
   - **Overview** - jumlah kendaraan, kendaraan masuk lajur, lane change, indikasi
     hogging, lag ingest, timeline session/window.
   - **Lane analysis** - heatmap lajur x jam, distribusi dwell per lajur/kelas,
     tabel lane usage, matriks lane change.
   - **Hogging explorer** - daftar event hogging, breakdown severity/rule, unduh CSV.
   - **Run & data quality** - registry run, status complete/session/window, cek
     duplikat/rentang/enter-exit, unduh CSV.
3. Gunakan filter **sesi**, **run/window**, kamera, arah, lajur, kelas, severity,
   dan rentang tanggal. Tombol **Refresh data** membersihkan cache.
4. Kalau dashboard kosong, lihat bagian troubleshooting di bawah.

## 2. Untuk recruiter - menjalankan Kaggle sendiri

1. Buka link notebook Kaggle yang diberikan pemilik proyek.
2. Klik **Copy & Edit** (perlu akun Kaggle gratis).
3. Buka **Settings**:
   - **Internet: On** (wajib untuk clone repo dan install),
   - **Accelerator: GPU T4 x2** (opsional; CPU juga jalan, lebih lambat).
4. Klik **Run All**. Notebook akan:
   - clone/pull repo `highway-lane-analytics`,
   - install package,
   - unduh klip 55 detik dari repo,
   - menjalankan YOLO11n + ByteTrack + lane tracker,
   - menampilkan video anotasi dan tabel lane usage/hogging.
5. Kalau `REPLAY=True`, notebook mengirim events/tracks/run ke pipeline dan
   mencetak `/stats`. Buka dashboard +-60 detik setelah selesai.
6. Untuk mencoba video sendiri: taruh video di `/kaggle/input/...`, ubah `VIDEO_PATH`
   dan `BASE_TIMESTAMP` (waktu di overlay, WIB naive), ganti `RUN_ID`, lalu jalankan
   ulang bagian 3-6.

## 3. Untuk recruiter - Colab

1. Buka badge/link Colab.
2. **Runtime -> Run all**.
3. Notebook berjalan lokal (`REPLAY=False`) dan menampilkan video + tabel.
4. Ingin upload? **File -> Save a copy in Drive**, isi `PIPELINE_TOKEN` di cell 2,
   set `REPLAY=True`, lalu jalankan ulang.

---

## 4. Troubleshooting

| Gejala | Penyebab | Tindakan |
| --- | --- | --- |
| Dashboard: "data export is not ready" | belum ada silver/gold Parquet | kirim satu run, tunggu 60 s, atau jalankan `rebuild-gold` di server |
| Dashboard kosong setelah kirim | export belum jalan / run belum `complete` | cek `/stats`, pastikan `/webhook/run` terkirim |
| `401` | token salah/kosong | isi `PIPELINE_TOKEN` dengan benar |
| `422` timestamp | ada `Z`/offset | pakai WIB naive, mis. `2026-11-08T06:58:06` |
| `422` camera_id | event beda dengan envelope | pakai `camera_id` yang sama |
| `413` | batch terlalu besar | turunkan `--batch-size` |
| `502/520-524` | origin/tunnel bermasalah | sender retry otomatis; coba lagi beberapa menit |
| Kaggle lambat/kehabisan disk | GPU/CPU kecil, video besar | pakai `IMGSZ=640`, `FRAME_STRIDE=2`, atau klip 55 detik |

---

## 5. Catatan keamanan (trade-off yang dipilih)

- Token ditulis di notebook Kaggle publik agar recruiter bisa langsung upload.
  Siapa pun yang melihat notebook bisa mengirim data ke receiver.
- Dampak dibatasi oleh receiver: body maksimum 2 MB, 2.000 objek per request, dan
  dedupe `event_id`/`run_id`. Tetap bisa spam run baru.
- Bila disalahgunakan: ganti `PIPELINE_WEBHOOK_TOKEN` di server, restart
  `lane-receiver`, lalu perbarui notebook dan link yang dibagikan.
- Jangan pernah membuka port receiver/Kafka ke publik. Akses tetap lewat Cloudflare
  Tunnel; dashboard memang publik.
- Jangan taruh kredensial server, SSH key, atau file `.env` di notebook/repo.

---

## 6. Angka yang bisa dibicarakan

Dari evaluasi 56 kendaraan berlabel (klip 10 menit KM 58+400):

| Aspek | Hasil |
| --- | --- |
| Lane index (2 vs 3) | 100% (TP 26/FP 0/FN 0/TN 30) |
| Posisi carriageway | 100% |
| Nama lajur lengkap | 56/56 |
| Deteksi lane hogger | TP 41 / FP 6 / FN 2; P 0.872, R 0.953, F1 0.911 |
| Lane change | TP 0 / FN 2 pada run ini; 7 false change dari artefak keluar zona |

Batas yang jujur: dua ground-truth lane change (`#309`, `#1132`) tidak terdeteksi
karena track-nya hanya tercatat di lane 3 (masalah entry/deteksi, bukan assignment
bbox). False change berasal dari transisi 2->3 di akhir stint dengan dwell 0,17-0,83
detik; filter stabilitas >=1 detik menghilangkan semuanya.

---

## 7. File terkait

| File | Isi |
| --- | --- |
| `notebooks/demo_recruiter.ipynb` | Notebook demo Colab/Kaggle. |
| `docs/sample_output/km58_sample_55s.mp4` | Klip 55 detik yang dipakai notebook. |
| `docs/PIPELINE_CLOUDFLARE.md` | Kontrak pengiriman, token, retry, verifikasi. |
| `docs/HANDOFF_DATA_PIPELINE_DASHBOARD.md` | Kontrak data dan dashboard. |
