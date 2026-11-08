# Kirim hasil analisis ke pipeline produksi (Cloudflare Tunnel)

Dokumen ini menjelaskan sisi **pengirim** di repository ini. Receiver, Kafka, dan
dashboard berada di proyek lain; kontrak lengkapnya ada di
[`HANDOFF_DATA_PIPELINE_DASHBOARD.md`](HANDOFF_DATA_PIPELINE_DASHBOARD.md#45-kontrak-webhook-transport-handoff)
dan `docs/schema/lane_event.schema.json`.

## Alamat

| Item | Nilai |
| --- | --- |
| Receiver | `https://lane-api.syaifudin-fam.my.id` |
| Dashboard | `https://lane.syaifudin-fam.my.id` |
| Health (publik) | `GET /health` |
| Stats (publik) | `GET /stats` |
| Auth | `Authorization: Bearer <token>` |
| Token | secret `PIPELINE_WEBHOOK_TOKEN` |

Tidak perlu `cloudflared`/`ngrok` di mesin pengirim. Cukup HTTPS biasa; selalu
`https://`, jangan `http://`. Hostname ini tidak memakai Cloudflare Access.

## Variabel lingkungan

| Variabel | Wajib | Fungsi |
| --- | --- | --- |
| `PIPELINE_WEBHOOK_TOKEN` | ya (saat kirim) | bearer token dari operator |
| `PIPELINE_BASE_URL` | tidak | override hostname, default produksi |
| `PIPELINE_SOURCE` | tidak | label `source` di envelope, default `kaggle-replay` |
| `WEBHOOK_TOKEN` / `WEBHOOK_URL` | tidak | nama lama, masih dibaca sebagai fallback |

Jangan menulis token di kode, notebook yang dibagikan, atau repo. Di Colab/Kaggle
pakai Secrets; di server/CI pakai environment variable.

## Urutan kirim (wajib)

```text
events (urut video_time_s)
  -> POST /webhook          (banyak batch, <= 200 event per request)
  -> POST /webhook/tracks   (sekali, setelah semua event)
  -> POST /webhook/run      (terakhir, complete=true)
```

`/webhook/run` adalah pemicu agregasi. Tanpa `complete: true`, dashboard menganggap
run belum selesai.

## CLI

```powershell
$env:PIPELINE_WEBHOOK_TOKEN = "<token>"

lane-analytics replay `
  --events output/events/km58_162550_final.jsonl `
  --tracks output/tracks/km58_162550_final.jsonl `
  --summary output/reports/km58_162550_final/summary.json `
  --run-config output/reports/km58_162550_final/run_config.json `
  --speed 1 `
  --window 5 `
  --stats
```

Cek jalur Cloudflare -> receiver -> Kafka tanpa mengirim data:

```powershell
lane-analytics pipeline-status
```

Uji payload tanpa HTTP:

```powershell
lane-analytics replay --events <file.jsonl> --tracks <file.jsonl> --dry-run
```

Hasil dry-run ditulis ke `output/replay/<run_id>/` (events, tracks, run, manifest).

## Perilaku sender

- **Validasi lokal sebelum kirim.**
  - `timestamp` harus WIB naive: `2026-11-08T06:58:12`. `...Z` dan `...+07:00`
    ditolak karena receiver membalas `422`.
  - `camera_id` setiap event harus sama dengan envelope.
  - `event_type` harus salah satu dari `lane_enter`, `lane_exit`, `lane_change`,
    `lane_hogging_indication`.
  - `window_index < window_count` bila keduanya ada.
- **Idempotensi.** `event_id` dari `analyze` dipakai ulang. Bila kosong, sender
  menurunkan UUID5 stabil dari identitas event, jadi retry tidak menghasilkan ID baru.
- **Retry.** `408`, `425`, `429`, `5xx`, `52x` (Cloudflare), dan error jaringan
  di-retry 5x dengan backoff 1-2-4-8-16 detik + jitter, timeout 30 detik.
  `400/401/413/422` tidak di-retry: payload atau token harus diperbaiki.
- **Partial accept.** `202` dengan `rejected > 0` dianggap gagal. Perbaiki data lalu
  kirim ulang; event yang sudah masuk terdeteksi sebagai duplikat.
- **Health gate.** Sebelum batch pertama, sender memanggil `/health` sampai
  `healthy: true` dan `sink.status: "ok"` (default 6 percobaan, 10 detik).
- **Batas.** Maksimum 2 MB dan 2.000 objek per request; default 200.

## Verifikasi setelah kirim

```powershell
curl -s https://lane-api.syaifudin-fam.my.id/stats
```

| Cek | Harapan |
| --- | --- |
| `accepted_events` | bertambah sejumlah event baru |
| `duplicate_events` | 0 pada kiriman pertama |
| `rejected_events` | **0** |
| `accepted_tracks` | sejumlah track |
| `accepted_runs` | +1 |
| Dashboard | run muncul setelah export berikutnya (+-60 detik) |

## Troubleshooting

| Gejala | Penyebab | Tindakan |
| --- | --- | --- |
| `401` | token salah/kosong/ada spasi | baca ulang secret, `strip()` |
| `422` timestamp | ada `Z` atau offset | kirim WIB naive |
| `422` camera_id | event beda dengan envelope | samakan |
| `422` event_type | tipe tidak dikenal | pakai 4 tipe resmi |
| `413` | batch terlalu besar | turunkan `--batch-size` |
| `502/520-524` | origin/tunnel bermasalah | sender retry otomatis; kalau > 10 menit hubungi operator |
| `202` tapi dashboard kosong | `/webhook/run` belum terkirim atau export belum jalan | kirim run terakhir dengan `complete=true`, tunggu 60 detik |

## Notebook

- `notebooks/colab_lane_occupancy.ipynb` - `activate()` membaca
  `PIPELINE_WEBHOOK_TOKEN`; URL default produksi.
- `notebooks/kaggle_production.ipynb` - mode live/drive per window, replay ke
  pipeline, dan ringkasan `/stats` di bundle akhir.

## Media clips

Klip bukti hogging tidak dikirim lewat webhook. Upload ke Nextcloud lewat WebDAV,
lalu sertakan `video_path` di event. Lihat
[`NEXTCLOUD_MEDIA.md`](NEXTCLOUD_MEDIA.md).
