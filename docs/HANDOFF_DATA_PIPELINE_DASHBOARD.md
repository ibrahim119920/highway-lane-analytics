# Handoff — Data Pipeline & Dashboard

**Proyek sumber:** `highway-lane-occupancy` (Intelligent Highway Lane Occupancy & Hogging Detection)
**Repo:** <https://github.com/ibrahim119920/highway-lane-analytics>
**Commit acuan:** `c289de1` (2026-11-08)
**Status:** tahap computer vision selesai dan tervalidasi. Handoff ke proyek berikutnya memakai
**replay hasil batch melalui webhook HTTP → Kafka lokal**, bukan pemrosesan live.
**Tujuan dokumen:** konteks mandiri + kontrak integrasi untuk proyek pipeline/dashboard.
**Batas scope:** dokumen ini hanya menjelaskan apa yang dihasilkan proyek video analytics dan
kontrak yang harus dipenuhi proyek pipeline. Implementasi receiver, Kafka, pipeline, dan
dashboard **bukan** bagian dari proyek ini.

---

## 1. Ringkasan

Sistem yang sudah jadi memproses rekaman CCTV tol, mendeteksi kendaraan (YOLO11n), melacaknya
(ByteTrack), memetakan posisinya ke 6 lajur (3 northbound + 3 southbound), lalu menghasilkan
**event stream JSONL** tentang masuk/keluar lajur, perpindahan lajur, dan indikasi lane hogging.

Karena ini proyek skala kecil dan videonya sudah tersedia sejak awal, **pemrosesan dilakukan
sekali secara batch (offline)**. Hasil batch itulah yang kemudian **di-replay** sebagai
micro-batch HTTP untuk mensimulasikan feed near-real-time ke pipeline.

```
┌──────────────────────────────────────────────┐
│  Sumber video (sudah tersedia)               │  MP4 1024x768 @ 18 fps, +/-5 menit/klip
└───────────────────┬──────────────────────────┘
                    │  batch / offline (Colab atau local)
                    ▼
┌──────────────────────────────────────────────┐
│  Vision pipeline (SELESAI — proyek ini)      │  YOLO11n + ByteTrack + lane polygons
│  `lane-analytics analyze --no-video`         │  frame stride 2 -> ~9 fps diproses
└───────────────────┬──────────────────────────┘
                    │
                    ▼
┌──────────────────────────────────────────────┐
│  Artefak batch (source of truth)             │  events/<run_id>.jsonl
│  `output/`                                   │  tracks/<run_id>.jsonl
│                                              │  reports/<run_id>/summary.json + run_config.json
└───────────────────┬──────────────────────────┘
                    │  replay sender (Colab, `requests`) — proyek ini
                    │  POST micro-batch per N detik event-time
                    ▼
┌──────────────────────────────────────────────┐
│  ngrok tunnel                                │  https://<acak>.ngrok-free.app
└───────────────────┬──────────────────────────┘
                    │  HTTPS POST /webhook
                    ▼
┌──────────────────────────────────────────────┐
│  ★ PROYEK PIPELINE ★                         │
│  Webhook receiver lokal (FastAPI :5000)      │  validasi + auth + dedupe
│  -> Kafka localhost:9092                     │  lane.events / lane.tracks / lane.runs
│  -> consumer -> Parquet/DuckDB               │  bronze / silver / gold
│  -> dashboard                                │  Streamlit / Next.js
└──────────────────────────────────────────────┘
```

### Pembagian tanggung jawab

| Komponen | Pemilik | Status |
| --- | --- | --- |
| Batch processing video -> events/tracks/reports | **proyek ini** | selesai |
| Replay sender dari Colab (`requests.post`) | **proyek ini** | selesai |
| ngrok tunnel (`ngrok http 5000`) | operator | manual |
| Webhook receiver FastAPI :5000 | proyek pipeline | **selesai** (receiver + auth + dedupe + `/health`, `/stats`) |
| Kafka broker + topic | proyek pipeline | **selesai** (3 topic, key `camera_id`, retensi 7 hari) |
| Consumer, validasi, dedupe, Parquet/DuckDB | proyek pipeline | **selesai** (silver/gold, karantina, export Parquet) |
| Dashboard | proyek pipeline | **selesai** (4 halaman, filter run/kamera, indikator lag) |

> Implementasi: <https://github.com/ibrahim119920/highway-lane-analytics-data-pipeline>
> (commit `e2ff01f`; 20 tes lulus, CI Ubuntu + Windows). Jalur Kafka sudah diverifikasi
> end-to-end dengan data demo dan run asli `source_video` (418 event / 328 track).

> **Aturan handoff:** proyek ini hanya **mengirim** data. Tidak ada receiver, Kafka client,
> consumer, atau dashboard di repo video analytics.

---

## 2. Sumber data & asumsi

| Item | Nilai |
| --- | --- |
| Kamera | `tj_priok_km58_400b` — Tol Tj. Priok, KM 58+400 (B), malam |
| Resolusi | 1024 × 768 (4:3) |
| Frame rate | 18 fps (17.999) |
| Durasi per klip | ± 5 menit (5.394 frame) |
| Jumlah klip | 3 (satu sesi, berurutan) |
| Zona waktu overlay | WIB (UTC+7), **waktu lokal naive** tanpa offset |
| Kelas kendaraan | `car`, `truck`, `bus` (COCO id 2, 5, 7; motor dikecualikan) |
| Arah | `northbound` = lajur kiri, kendaraan menjauh; `southbound` = lajur kanan, mendekat |

Jadwal klip (tanggal 2026-11-07):

| Klip | File | `base_timestamp` (overlay frame 0) | Status |
| --- | --- | --- | --- |
| Video 1 | `km58_400B_tj_priok_20261107_161545.mp4` | `2026-11-07T23:15:38` | diproses (baseline) |
| Video 2 | `km58_400B_tj_priok_20261107_162049.mp4` | `2026-11-07T23:20:41` | **belum diproses** |
| Video 3 | `km58_400B_tj_priok_20261107_162550.mp4` | `2026-11-07T23:25:41` | diproses (final) |

> **Penting:** `base_timestamp` harus dibaca dari overlay video. Kalau salah, seluruh
> timestamp event bergeser. Pipeline sebaiknya menyimpan `base_timestamp` sebagai metadata run
> dan memvalidasi bahwa `timestamp` event berada di dalam rentang `base_timestamp + durasi`.

### Keputusan arsitektur (disepakati)

1. **Tidak ada pemrosesan live.** Video diproses sekali secara batch; hasilnya menjadi
   *source of truth*.
2. **Replay, bukan stream ulang.** Hasil batch di-replay sebagai micro-batch HTTP untuk
   mensimulasikan kedatangan data near-real-time.
3. **Colab hanya mengirim.** Colab memakai `requests.post`; tidak ada Kafka client di Colab.
4. **Kafka tetap lokal.** Webhook lokal di `localhost:5000` yang memproduksi ke
   `localhost:9092`.
5. **Event time dipertahankan.** `timestamp` pada event tetap waktu kejadian asli; waktu
   kedatangan dicatat terpisah (`ingested_at`).
6. **Delivery bersifat at-least-once.** Pengiriman bisa retry; dedupe wajib di receiver dan
   di pipeline.
7. **Hasil batch tetap bisa di-ingest langsung** dari file (mode fallback), sehingga pipeline
   tidak bergantung pada webhook saat backfill.

---

## 3. Layout lajur

`lane_name` adalah kunci unik. `lane_index` **tidak unik global** (1–3 dipakai di kedua arah),
jadi dashboard/pipeline wajib memakai `lane_name` atau pasangan `(direction, lane_index)`.

| `lane_name` | `lane_index` | `direction` | `lane_role` | Posisi |
| --- | --- | --- | --- | --- |
| `nb_lane_1` | 1 | northbound | normal | paling kiri / luar |
| `nb_lane_2` | 2 | northbound | normal | tengah |
| `nb_lane_3` | 3 | northbound | **overtaking** | sisi median |
| `sb_lane_1` | 1 | southbound | normal | paling kanan / luar |
| `sb_lane_2` | 2 | southbound | normal | tengah |
| `sb_lane_3` | 3 | southbound | **overtaking** | sisi median |

- Di kedua arah, **lane 3 = lajur cepat / median** dan dipakai aturan hogging.
- Poligon dinormalisasi (0–1) di `config/config.yaml`; gambar kalibrasi:
  `docs/sample_output/km58_lane_mapping.jpg`.
- Catatan geometri: `sb_lane_1` berakhir di `y ≈ 0.829` karena garis `lane1_right` berhenti di
  `y ≈ 0.686`. Kendaraan sangat dekat di pojok kanan-bawah frame akan jatuh ke `sb_lane_2`.
  Ini disengaja, bukan bug.

---

## 4. Kontrak data

Tiga artefak yang perlu di-ingest:

1. `output/events/<run_id>.jsonl` — event stream (append-friendly, satu objek per baris).
2. `output/tracks/<run_id>.jsonl` — ringkasan per kendaraan + riwayat lajur (stint).
3. `output/reports/<run_id>/` — CSV/JSON/Markdown siap pakai (turunan, boleh diabaikan pipeline
   dan dibangun ulang).

Skema mesin: `docs/schema/lane_event.schema.json`, versi kontrak `1.0`.

### 4.1 Event JSONL

Empat `event_type`:

| `event_type` | Kapan | Field kunci |
| --- | --- | --- |
| `lane_enter` | assignment lajur stabil dimulai | `lane_name`, `lane_index`, `lane_role` |
| `lane_exit` | stint lajur ditutup | `lane_name`, `dwell_seconds` |
| `lane_change` | kendaraan pindah lajur | `previous_lane`, `lane_name`, `dwell_seconds` |
| `lane_hogging_indication` | ambang aturan terpenuhi | `rule_name`, `severity`, `dwell_seconds`, `message` |

Urutan saat lane change, **pada frame/timestamp yang sama**: `lane_exit` → `lane_change` → `lane_enter`.

Contoh nyata (`km58_162550_final`):

```json
{"camera_id":"tj_priok_km58_400b","timestamp":"2026-11-07T23:25:54","track_id":26,"object_class":"truck","confidence":0.4704,"event_type":"lane_exit","lane_name":"sb_lane_1","lane_index":1,"lane_role":"normal","previous_lane":"","dwell_seconds":1.778,"class_id":7,"frame_index":250,"video_time_s":13.889,"severity":"","rule_name":"","message":"","schema_version":"1.0","event_id":"c3eec5fbaa7b495794542d2b74bbb140"}
```

| Field | Tipe | Wajib | Catatan |
| --- | --- | --- | --- |
| `camera_id` | string | ya | dari config; kunci partisi |
| `timestamp` | string | ya | ISO-8601 lokal naive `YYYY-MM-DDTHH:MM:SS`; **tanpa zona** |
| `track_id` | int | ya | **run-scoped**, bukan identitas kendaraan global |
| `object_class` | string | ya | `car` / `truck` / `bus` |
| `confidence` | float | ya | 0–1 pada frame event |
| `event_type` | string | ya | 4 nilai di atas |
| `lane_name` | string | ya | lajur baru (enter/change/hogging) atau lajur lama (exit) |
| `lane_index` | int/null | tidak | 1–3, **scoped per arah** |
| `lane_role` | string | tidak | `normal` / `overtaking` |
| `previous_lane` | string | tidak | diisi untuk `lane_change` (dan `lane_enter` setelah change) |
| `dwell_seconds` | float | tidak | waktu kontinu di lajur; untuk exit/change = durasi stint lama |
| `class_id` | int | tidak | COCO id |
| `frame_index` | int | tidak | indeks frame sumber |
| `video_time_s` | float | tidak | `frame_index / source_fps` |
| `severity` | string | tidak | `low` / `medium` / `high` / kosong |
| `rule_name` | string | tidak | nama aturan dari config |
| `message` | string | tidak | deskripsi manusiawi (bahasa Indonesia) |
| `schema_version` | string | tidak | saat ini `1.0` |
| `event_id` | string | tidak | UUID4 hex 32 char; **kunci dedupe** |

Kontrak stabil = tujuh field pertama + `lane_name`. Field baru bisa ditambahkan; konsumen harus
mengabaikan key tak dikenal dan memeriksa `schema_version` bila perlu strict.

### 4.2 Track JSONL (satu objek per track selesai)

```json
{
  "camera_id": "tj_priok_km58_400b",
  "track_id": 3,
  "object_class": "truck",
  "class_id": 7,
  "first_frame": 0,
  "last_frame": 111,
  "first_timestamp": "2026-11-07T23:25:41",
  "last_timestamp": "2026-11-07T23:25:52",
  "first_video_time_s": 0.0,
  "last_video_time_s": 6.2,
  "lane_change_count": 0,
  "hogging_event_count": 1,
  "dominant_lane": "sb_lane_2",
  "total_lane_dwell_seconds": 5.4,
  "lane_stints": [
    {
      "stint_id": 1,
      "lane_name": "sb_lane_2",
      "lane_index": 2,
      "lane_role": "normal",
      "enter_frame": 2,
      "enter_time_s": 0.111,
      "enter_timestamp": "2026-11-07T23:25:41",
      "exit_frame": 110,
      "exit_time_s": 6.111,
      "exit_timestamp": "2026-11-07T23:25:47",
      "dwell_seconds": 6.0
    }
  ]
}
```

`lane_stints` adalah **grain terbaik untuk occupancy/dwell**. Satu kendaraan bisa punya beberapa
stint (masuk lajur, pindah, keluar).

### 4.3 CSV di `reports/<run_id>/`

| File | Grain | Kolom penting |
| --- | --- | --- |
| `events.csv` | event | semua field event |
| `vehicles.csv` | track | metadata, `dominant_lane`, total dwell, jumlah change/hogging |
| `lane_stints.csv` | stint | `camera_id, track_id, object_class, stint_id, lane_name, lane_index, lane_role, enter_frame, enter_time_s, enter_timestamp, exit_frame, exit_time_s, exit_timestamp, dwell_seconds, hogging_indication` |
| `lane_usage.csv` | lajur × kelas | `vehicle_count, stint_count, total_dwell_s, avg_dwell_s, max_dwell_s, vehicle_share_pct, dwell_share_pct` |
| `lane_changes.csv` | `lane_change` | lajur lama/baru, kelas, dwell |
| `hogging_events.csv` | hogging | rule, severity, dwell, message |
| `event_intervals.csv` | bucket waktu | jumlah event + breakdown |
| `summary.json` | run | ringkasan lengkap (lihat 4.4) |
| `summary.md` | run | laporan siap baca |
| `run_config.json` | run | snapshot konfigurasi + metadata eksekusi |

### 4.4 `summary.json`

Key level atas: `schema_version, camera_id, generated_at, time_range{start,end}, interval_seconds,
total_vehicles, total_events, vehicles_by_class, events_by_type, events_by_lane, lane_usage[],
lane_changes{total,by_class,pairs[]}, hogging{total,by_class,by_severity,by_rule,by_lane,events[]},
intervals[], peak_interval, run{}`.

`run{}` berisi: `run_id, source_video, detector, start_frame, frames_read, frames_processed,
frame_stride, source_fps, output_fps, duration_s, processing_seconds, processing_fps,
base_timestamp, lanes[], hogging_rules[]`.

### 4.5 Kontrak webhook (transport handoff)

Bagian ini adalah **kontrak**, bukan implementasi. Receiver boleh dibuat dengan Flask, FastAPI,
atau framework lain; yang penting perilakunya sesuai.

#### Alamat dan autentikasi

| Item | Nilai |
| --- | --- |
| Base URL lokal | `http://127.0.0.1:5000` |
| Base URL publik | `https://<subdomain>.ngrok-free.app` |
| Header auth | `Authorization: Bearer <WEBHOOK_TOKEN>` |
| Alternatif | `X-Webhook-Token: <WEBHOOK_TOKEN>` |
| Content type | `application/json` |
| Token | disimpan di Colab Secrets / env, **jangan** di kode atau repo |

Receiver **wajib** menolak request tanpa token yang valid (`401`). URL ngrok bersifat publik.

#### Endpoint

| Method | Path | Isi | Topic Kafka |
| --- | --- | --- | --- |
| `POST` | `/webhook` | batch event | `lane.events` |
| `POST` | `/webhook/tracks` | batch track summary | `lane.tracks` |
| `POST` | `/webhook/run` | metadata run (summary + config) | `lane.runs` |
| `GET` | `/health` | status receiver + Kafka | - |
| `GET` | `/stats` | counter request/event (opsional) | - |

#### Payload event

```json
{
  "run_id": "km58_162550_final",
  "camera_id": "tj_priok_km58_400b",
  "source": "colab-replay",
  "sent_at": "2026-11-08T00:10:00Z",
  "events": [
    { "event_id": "...", "event_type": "lane_enter", "lane_name": "sb_lane_2", "...": "..." }
  ]
}
```

Ketentuan:

- `run_id` dan `camera_id` wajib ada di envelope.
- `events` berisi 1–2.000 objek; sender default 200 per request.
- Setiap event wajib punya `event_type`, `camera_id`, `timestamp`.
- `event_id` sangat dianjurkan; kalau tidak ada, receiver dapat memakai hash
  `(camera_id, track_id, event_type, frame_index)` sebagai kunci dedupe.
- Receiver **tidak boleh** mengubah nilai field asli.
- Receiver menambahkan metadata transport: `ingested_at` (UTC ISO-8601 dengan `Z`) dan
  `ingest_source` (mis. `colab-replay`).

#### Payload track

```json
{
  "run_id": "km58_162550_final",
  "camera_id": "tj_priok_km58_400b",
  "source": "colab-replay",
  "sent_at": "2026-11-08T00:14:30Z",
  "tracks": [ { "track_id": 15, "lane_stints": [ ... ], "...": "..." } ]
}
```

Dikirim **sekali di akhir run** (setelah semua event). Isi objek track sama dengan
`tracks/<run_id>.jsonl` (lihat 4.2).

#### Payload run

```json
{
  "run_id": "km58_162550_final",
  "camera_id": "tj_priok_km58_400b",
  "source": "colab-replay",
  "complete": true,
  "run": { "run_id": "...", "duration_s": 299.676, "...": "..." },
  "summary": { "total_vehicles": 158, "total_events": 111, "...": "..." },
  "config": { "occupancy": { "...": "..." } }
}
```

`run` = isi `summary.json["run"]`; `summary` = `summary.json` tanpa key `run` (boleh juga
dikirim utuh); `config` = isi `run_config.json`. `complete: true` menandai run selesai dan
menjadi pemicu pembangunan agregat di pipeline.

#### Respons

Sukses (partial accepted tetap `202`):

```json
{
  "accepted": 200,
  "duplicates": 0,
  "rejected": 0,
  "topic": "lane.events",
  "request_id": "6f2a1c9e4b7d4f0a"
}
```

| Status | Arti | Tindakan sender |
| --- | --- | --- |
| `202` | diterima (sebagian/seluruh) | lanjut |
| `400` | JSON rusak | berhenti, perbaiki payload |
| `401` | token salah/hilang | berhenti, perbaiki konfigurasi |
| `413` | body > 2 MB atau > 2.000 event | perkecil batch |
| `422` | tidak ada event valid | berhenti, periksa data |
| `429` | rate limit | retry dengan backoff |
| `503` | Kafka belum siap | retry dengan backoff |

#### Pemetaan Kafka

| Topic | Key | Partisi | Jaminan urutan |
| --- | --- | --- | --- |
| `lane.events` | `camera_id` | 3 | urut per kamera |
| `lane.tracks` | `camera_id` | 3 | urut per kamera |
| `lane.runs` | `camera_id` | 3 | urut per kamera |

Key `camera_id` dipilih agar semua event satu kamera masuk partisi yang sama, sehingga urutan
terjaga tanpa perlu partisi tunggal. Nilai pesan = objek JSON event/track/run yang sudah
ditambah `ingested_at` dan `ingest_source`.

#### Dedupe dan idempotensi

- Receiver: dedupe berdasarkan `event_id` (LRU bounded atau SQLite).
- Pipeline: dedupe berdasarkan `(run_id, event_id)` untuk event dan
  `(run_id, camera_id, track_id)` untuk track.
- `run_id` bersifat idempotent (upsert); reprocessing video yang sama menghasilkan `run_id`
  baru dan harus menandai run lama sebagai `superseded`, bukan menjumlahkan keduanya.
- Pengiriman bersifat **at-least-once**: duplikat mungkin terjadi saat retry.

#### Event time vs ingest time

| Field | Arti | Zona |
| --- | --- | --- |
| `timestamp` | waktu kejadian di video | lokal naive (WIB) |
| `sent_at` | waktu sender mengirim batch | UTC (`Z`) |
| `ingested_at` | waktu receiver menerima | UTC (`Z`) |

Dashboard sebaiknya menampilkan `timestamp` sebagai waktu kejadian, dan
`ingested_at - timestamp` sebagai *data lag*. Replay 1× menghasilkan lag ± durasi window;
replay N× memperkecil lag simulasi tanpa mengubah `timestamp`.

#### Perilaku sender (proyek ini)

- Membaca `events/<run_id>.jsonl`, urut `video_time_s`.
- Memvalidasi `timestamp` (WIB naive, tanpa `Z`/offset), `camera_id` per event, dan
  `event_type` sebelum mengirim; pelanggaran dihentikan dengan pesan jelas.
- Mengelompokkan ke window event-time (default 5 detik) atau maksimum 200 event.
- `POST` per window; tidur sampai window berikutnya sesuai faktor kecepatan (`--speed`).
- Retry 5× dengan exponential backoff + jitter untuk `408`, `425`, `429`, `5xx`,
  `52x` (Cloudflare), dan error jaringan. `400/401/413/422` tidak di-retry.
- Preflight `GET /health` (poll sampai `healthy` dan `sink.status == "ok"`) sebelum
  mulai; berhenti dengan pesan jelas bila Kafka belum siap.
- `202` dengan `rejected > 0` dianggap gagal; perbaiki payload lalu kirim ulang.
- Setelah window terakhir: kirim `/webhook/tracks`, lalu `/webhook/run` dengan `complete: true`.
- `event_id` dipakai ulang saat retry; bila kosong, sender menurunkan UUID5 stabil
  dari identitas event.
- Target default `https://lane-api.syaifudin-fam.my.id` (Cloudflare Tunnel); header
  `ngrok-skip-browser-warning: true` tetap dikirim agar kompatibel dengan tunnel lain.

---

## 5. Volume data (untuk sizing)

Diukur dari run 5 menit (`km58_162550_final`):

| Artefak | Ukuran | Ekstrapolasi per kamera/hari (24 jam) |
| --- | --- | --- |
| Events JSONL | 51 KB / 111 event | ± 15 MB |
| Tracks JSONL | 78 KB / 158 track | ± 22 MB |
| Annotated MP4 | 44 MB | ± 12,7 GB (**jangan disimpan jangka panjang**) |
| Reports (CSV/JSON/MD) | ± 50 KB | ± 15 MB |

Implikasi:

- Event + track sangat ringan (± 37 MB/hari/kamera). 100 kamera ≈ 3,7 GB/hari — aman di Parquet.
- Video anotasi mahal. Simpan hanya: video sumber (retensi pendek), klip sekitar event hogging,
  dan thumbnail. Untuk dashboard, simpan path/URL, bukan file di database.
- **Pemrosesan batch berada di luar jalur kritis.** CPU lokal ± 8,6 fps (1 klip 5 menit ≈
  5 menit 12 detik); di GPU Colab jauh lebih cepat. Karena dashboard memakai hasil yang sudah
  dihitung, latency dashboard ditentukan oleh replay, bukan inferensi.
- **Beban replay sangat kecil.** 111 event / 300 detik ≈ 0,37 event/detik. Dengan window 5 detik
  ≈ 60 request per run 5 menit, total ± 51 KB data. ngrok dan Kafka tidak akan menjadi bottleneck.
- Retensi Kafka 7 hari cukup (≈ 100 MB untuk satu kamera); arsip jangka panjang ada di Parquet.

---

## 6. Baseline hasil (untuk validasi dashboard)

### Video 3 — run final `km58_162550_final` (dengan adaptive anchor)

| Metrik | Nilai |
| --- | --- |
| Periode | 2026-11-07T23:25:52 → 23:30:29 |
| Kendaraan unik | **158** (car 126, truck 29, bus 3) |
| Event | **111** |
| `lane_enter` / `lane_exit` | 52 / 52 |
| `lane_change` | **2** (semua car) |
| Hogging | **5** (semua `medium`, rule `prolonged_overtaking_lane`, lajur `nb_lane_3`) |
| Peak interval | 23:27:00–23:28:00, 32 event |
| Frame | 2.697 diproses dari 5.394 (stride 2) |
| Waktu proses | 311,6 s (8,65 fps) |
| Event per lajur | `sb_lane_1` 28, `sb_lane_2` 25, `nb_lane_2` 24, `nb_lane_3` 19, `nb_lane_1` 9, `sb_lane_3` 6 |

Stint per lajur (n, median dwell, max):

| Lajur | n | median | max |
| --- | --- | --- | --- |
| `nb_lane_1` | 4 | 1,05 s | 7,11 s |
| `nb_lane_2` | 12 | 1,61 s | 4,33 s |
| `nb_lane_3` | 7 | 2,22 s | 3,44 s |
| `sb_lane_1` | 14 | 1,22 s | 4,67 s |
| `sb_lane_2` | 12 | 0,94 s | 4,33 s |
| `sb_lane_3` | 3 | 0,67 s | 0,89 s |

Hogging yang terdeteksi:

| Waktu | Track | Kelas | Lajur | Dwell | Severity |
| --- | --- | --- | --- | --- | --- |
| 23:26:59 | 203 | car | `nb_lane_3` | 2,11 s | medium |
| 23:27:44 | 378 | car | `nb_lane_3` | 2,11 s | medium |
| 23:27:47 | 409 | car | `nb_lane_3` | 2,11 s | medium |
| 23:29:37 | 803 | car | `nb_lane_3` | 2,00 s | medium |
| 23:30:11 | 874 | car | `nb_lane_3` | 2,00 s | medium |

### Video 1 — baseline sebelum adaptive anchor (untuk perbandingan, bukan acuan final)

219 kendaraan (car 163, truck 43, bus 13), 192 event, 12 lane change, 8 hogging
(7 medium + 1 high). Angka ini lebih tinggi karena false positive kendaraan besar di dekat kamera.
Setelah adaptive anchor diterapkan, hasil video 3 turun ke 2 lane change dan 0 hogging high.
**Jangan mencampur run lama dan baru dalam satu agregat tanpa flag versi.**

Detail: `docs/sample_output/km58_summary.md`.

---

## 7. Semantik yang harus dipahami sebelum membuat metrik

Ini bagian yang paling mudah salah saat membangun dashboard.

1. **`track_id` hanya unik dalam satu run.** Kendaraan yang sama di klip berbeda punya ID berbeda.
   Jangan pernah `COUNT(DISTINCT track_id)` lintas run. Gunakan `COUNT(DISTINCT run_id || ':' || track_id)`.
2. **`dwell_seconds` = lama terlihat di dalam poligon**, bukan durasi occupancy sebenarnya.
   Kamera hanya mencakup beberapa puluh meter per arah; kendaraan terlihat 0,3–3 detik.
   Aturan hogging diskalakan ke jendela itu ("bertahan di lajur cepat hampir sepanjang lintasan pandang").
3. **Hogging adalah indikasi berbasis aturan, bukan pelanggaran hukum.** Selalu tampilkan disclaimer.
4. **`lane_index` tidak unik**; gunakan `lane_name` sebagai kunci.
5. **Nama lajur, role, dan rule adalah konfigurasi, bukan enum.** Jangan hardcode di dashboard;
   ambil dari data atau tabel dimensi.
6. **`timestamp` naive lokal (WIB).** Simpan `event_ts_utc` + `event_ts_local`, atau paling tidak
   metadata `timezone` per kamera.
7. **Run bisa diulang.** Video yang sama pernah diproses 3× saat tuning
   (`km58_162550`, `km58_162550_anchor`, `km58_162550_final`). `event_id` baru dibuat setiap run,
   jadi dedupe `event_id` **tidak** menghapus duplikat antar-run. Butuh **run registry** dengan
   `is_active`/`superseded_by`, atau filter `run_id` terpilih.
8. **Deteksi tidak sempurna.** Confidence rendah di malam hari, ByteTrack bisa ganti ID saat
   oklusi. Jumlah kendaraan = jumlah track, bukan volume lalu lintas sebenarnya.
9. **`lane_enter`/`lane_exit` berpasangan** untuk tiap stint, kecuali track berakhir di tepi video.
   Saat lane change, tiga event muncul pada timestamp yang sama.
10. **Event `lane_hogging_indication` bisa lebih dari satu per stint** bila
    `retrigger_after_seconds > 0`; konfigurasi saat ini `0` = sekali per (track, stint, rule).
11. **Data yang masuk lewat webhook adalah replay, bukan live.** Urutan antar-event dalam satu
    run terjaga, tetapi kedatangan batch bisa terlambat/retry. Jangan menganggap
    `ingested_at` sama dengan `timestamp`; hitung lag secara eksplisit.
12. **Run dianggap lengkap hanya setelah `/webhook/run` dengan `complete: true`.** Jangan
    membangun agregat final dari event yang masih mengalir; gunakan status run.
13. **Replay bisa dipercepat (`--speed N`).** `timestamp` tidak berubah, jadi grafik berbasis
    event-time tetap benar; hanya lag simulasi yang mengecil.

---

## 8. Arsitektur pipeline yang direkomendasikan

Titik masuk utama adalah **webhook → Kafka**. File batch tetap tersedia sebagai fallback
(backfill atau jika webhook gagal), sehingga pipeline tidak bergantung pada satu jalur.

```
Colab replay ──HTTPS──▶ webhook :5000 ──produce──▶ Kafka :9092
                                                       │
                                                       ▼
                                              consumer group
                                                       │
                        ┌──────────────────────────────┼───────────────────────┐
                        ▼                              ▼                       ▼
                  bronze (raw JSON)            silver (typed)           gold (aggregates)
                  lane.events/tracks/runs      fact_event/stint/run     agg_*
                                                       │
                                                       ▼
                                                   Dashboard
```

### Langkah consumer (proyek pipeline)

1. **Consume** topic `lane.events`, `lane.tracks`, `lane.runs` dengan consumer group
   `lane-pipeline`. Commit offset **setelah** data tertulis (at-least-once).
2. **Validate** tiap pesan dengan `lane_event.schema.json`; pesan gagal → `quarantine/`,
   jangan gagalkan seluruh batch.
3. **Dedupe**: `(run_id, event_id)` untuk event, `(run_id, camera_id, track_id)` untuk track,
   `run_id` untuk run.
4. **Normalize**: parse `timestamp` → `event_ts_local` + `event_ts_utc`; `lane_key`; kelas;
   `severity` kosong → `NULL`; simpan `ingested_at`.
5. **Write bronze** (raw JSON per topic) lalu **silver** (Parquet) partisi
   `dt=YYYY-MM-DD/camera_id=<id>/run_id=<id>/`.
6. **Build gold** saat menerima `lane.runs` dengan `complete: true`; jangan menghitung agregat
   final dari run yang masih mengalir.
7. **Idempotensi**: tulis ulang partisi `(run_id)` dengan overwrite; jangan append ganda.
8. **Fallback file**: bila webhook tidak dipakai, ingest `output/events/*.jsonl`,
   `output/tracks/*.jsonl`, dan `reports/*/summary.json` dengan kontrak yang sama.

### Pengaturan Kafka yang disarankan

| Setting | Nilai | Alasan |
| --- | --- | --- |
| Topic partisi | 3 | ruang untuk paralelisme consumer |
| Key | `camera_id` | urutan per kamera |
| Retention | 7 hari | cukup untuk replay/backfill |
| `enable.auto.commit` | `false` | commit setelah write |
| Consumer `max.poll.records` | 500 | batch kecil, latency rendah |
| Producer `acks` | `all` | tidak kehilangan event |
| Producer idempotence | `true` | aman saat retry |

### Pemeriksaan kualitas data (jalankan per run)

- `event_id` duplikat dalam run → 0.
- `timestamp` monoton tidak turun (event diurutkan per frame) → boleh sama, tidak boleh mundur.
- Semua `timestamp` di dalam `[base_timestamp, base_timestamp + duration_s]`.
- `video_time_s` konsisten dengan `frame_index / source_fps` (toleransi 0,05 s).
- Setiap `lane_change` punya `lane_exit` tepat sebelum dan `lane_enter` tepat sesudah pada frame sama.
- `dwell_seconds >= 0`; stint `exit >= enter`.
- `lane_name` ada di `dim_lane` untuk kamera tersebut; `rule_name` ada di `dim_rule`.
- Jumlah `lane_enter` ≈ jumlah `lane_exit` (selisih wajar bila track terpotong di tepi video).
- `total_vehicles` di `summary.json` = jumlah baris `tracks` = jumlah track unik di events.

### Pilihan teknologi

| Kebutuhan | Rekomendasi ringan (self-study) | Alternatif produksi |
| --- | --- | --- |
| Penyimpanan | Parquet di disk/S3 + DuckDB | Postgres/BigQuery + dbt |
| Orkestrasi | skrip Python + `cron`/GitHub Actions | Airflow/Dagster/Prefect |
| Validasi | `jsonschema` + Pandas | Great Expectations / dbt tests |
| Dashboard | **Streamlit + Plotly** (paling cepat) | Next.js + Recharts, atau Grafana |
| API | tidak perlu dulu | FastAPI di atas DuckDB/Postgres |
| Transport | **Kafka lokal** (sudah dipilih) | Redpanda (API kompatibel Kafka) |
| Consumer | `confluent-kafka` / `kafka-python` | Flink / Spark Structured Streaming |
| Webhook receiver | FastAPI + Uvicorn `:5000` | Flask + Gunicorn |

Untuk tahap ini, **Kafka single-node + DuckDB/Parquet + Streamlit** sudah cukup dan bisa
berjalan penuh di laptop. Kafka tidak perlu cluster; satu broker KRaft cukup.

---

## 9. Model data analitik (star schema)

DDL ringkas (kompatibel DuckDB/Postgres dengan penyesuaian tipe):

```sql
CREATE TABLE dim_camera (
  camera_id      TEXT PRIMARY KEY,
  name           TEXT,
  road           TEXT,
  km_marker      TEXT,
  timezone       TEXT NOT NULL DEFAULT 'Asia/Jakarta',
  lane_layout    TEXT              -- mis. '3+3 median'
);

CREATE TABLE dim_lane (
  lane_key       TEXT PRIMARY KEY, -- camera_id || ':' || lane_name
  camera_id      TEXT NOT NULL,
  lane_name      TEXT NOT NULL,    -- nb_lane_1 ... sb_lane_3
  lane_index     INTEGER NOT NULL, -- 1..3, scoped per direction
  direction      TEXT NOT NULL,    -- northbound | southbound
  lane_role      TEXT NOT NULL     -- normal | overtaking
);

CREATE TABLE dim_vehicle_class (
  class_id       INTEGER PRIMARY KEY,
  class_name     TEXT NOT NULL     -- car | truck | bus
);

CREATE TABLE dim_rule (
  rule_name      TEXT PRIMARY KEY,
  default_severity TEXT,
  description    TEXT
);

CREATE TABLE fact_run (
  run_id           TEXT PRIMARY KEY,
  camera_id        TEXT NOT NULL,
  source_video     TEXT,
  base_timestamp   TIMESTAMP,
  duration_s       DOUBLE,
  source_fps       DOUBLE,
  output_fps       DOUBLE,
  frames_read      INTEGER,
  frames_processed INTEGER,
  processing_fps   DOUBLE,
  detector         TEXT,
  config_hash      TEXT,
  is_active        BOOLEAN DEFAULT TRUE,
  superseded_by    TEXT,
  ingested_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE fact_event (
  event_id       TEXT PRIMARY KEY,
  run_id         TEXT NOT NULL,
  camera_id      TEXT NOT NULL,
  event_ts_local TIMESTAMP NOT NULL,
  event_ts_utc   TIMESTAMP NOT NULL,
  video_time_s   DOUBLE,
  frame_index    INTEGER,
  track_id       INTEGER NOT NULL,
  object_class   TEXT NOT NULL,
  class_id       INTEGER,
  event_type     TEXT NOT NULL,
  lane_key       TEXT,
  previous_lane_key TEXT,
  lane_role      TEXT,
  dwell_seconds  DOUBLE,
  severity       TEXT,
  rule_name      TEXT,
  confidence     DOUBLE,
  schema_version TEXT
);

-- Grain: satu baris per (run, track, stint). Sumber utama metrik occupancy.
CREATE TABLE fact_lane_stint (
  run_id            TEXT NOT NULL,
  camera_id         TEXT NOT NULL,
  track_id          INTEGER NOT NULL,
  stint_id          INTEGER NOT NULL,
  object_class      TEXT NOT NULL,
  lane_key          TEXT NOT NULL,
  lane_role         TEXT,
  enter_ts_local    TIMESTAMP NOT NULL,
  exit_ts_local     TIMESTAMP NOT NULL,
  enter_video_time_s DOUBLE,
  exit_video_time_s  DOUBLE,
  dwell_seconds     DOUBLE NOT NULL,
  hogging_indication BOOLEAN DEFAULT FALSE,
  PRIMARY KEY (run_id, camera_id, track_id, stint_id)
);
```

Tabel agregat (gold), semuanya bisa dibangun ulang:

| Tabel | Grain | Isi |
| --- | --- | --- |
| `agg_lane_hourly` | tanggal × jam × kamera × lajur | kendaraan, stint, total/avg/p50/p90 dwell |
| `agg_class_hourly` | tanggal × jam × kamera × kelas | kendaraan, lane change, hogging |
| `agg_hogging_daily` | tanggal × kamera × rule × severity × lajur × kelas | jumlah indikasi, rata-rata dwell |
| `agg_lane_transition` | kamera × dari-lajur × ke-lajur × kelas | jumlah lane change |
| `agg_run_quality` | run | jumlah event, track, duplikat, gagal validasi, durasi proses |

---

## 10. Definisi metrik & contoh query

Semua contoh memakai DuckDB dan mengasumsikan Parquet silver sudah ada.

**Jumlah kendaraan unik** (ingat `track_id` run-scoped):

```sql
SELECT run_id, COUNT(DISTINCT run_id || ':' || track_id) AS vehicles
FROM fact_event
GROUP BY run_id;
```

**Pangsa kendaraan per lajur per arah:**

```sql
SELECT d.direction, s.lane_key, COUNT(DISTINCT s.run_id || ':' || s.track_id) AS vehicles,
       ROUND(100.0 * COUNT(DISTINCT s.run_id || ':' || s.track_id)
             / SUM(COUNT(DISTINCT s.run_id || ':' || s.track_id)) OVER (PARTITION BY d.direction), 1)
         AS share_pct
FROM fact_lane_stint s
JOIN dim_lane d USING (lane_key)
JOIN fact_run r USING (run_id)
WHERE r.is_active
GROUP BY d.direction, s.lane_key
ORDER BY d.direction, s.lane_key;
```

**Dwell p50/p90 per lajur & kelas:**

```sql
SELECT lane_key, object_class,
       COUNT(*) AS stints,
       ROUND(quantile_cont(dwell_seconds, 0.5), 2) AS p50_s,
       ROUND(quantile_cont(dwell_seconds, 0.9), 2) AS p90_s,
       ROUND(MAX(dwell_seconds), 2) AS max_s
FROM fact_lane_stint
GROUP BY lane_key, object_class
ORDER BY lane_key, object_class;
```

**Hogging rate per 1.000 kendaraan** (metrik utama dashboard):

```sql
WITH base AS (
  SELECT r.camera_id,
         COUNT(DISTINCT r.run_id || ':' || s.track_id) AS vehicles
  FROM fact_run r
  JOIN fact_lane_stint s USING (run_id)
  WHERE r.is_active
  GROUP BY r.camera_id
), hog AS (
  SELECT e.camera_id, COUNT(*) AS indications
  FROM fact_event e
  JOIN fact_run r USING (run_id)
  WHERE e.event_type = 'lane_hogging_indication' AND r.is_active
  GROUP BY e.camera_id
)
SELECT b.camera_id, b.vehicles, COALESCE(h.indications, 0) AS indications,
       ROUND(1000.0 * COALESCE(h.indications, 0) / NULLIF(b.vehicles, 0), 2) AS per_1000
FROM base b LEFT JOIN hog h USING (camera_id);
```

**Lane change per 100 kendaraan:**

```sql
SELECT r.camera_id,
       COUNT(*) FILTER (WHERE e.event_type = 'lane_change') AS lane_changes,
       COUNT(DISTINCT r.run_id || ':' || e.track_id) AS vehicles,
       ROUND(100.0 * COUNT(*) FILTER (WHERE e.event_type = 'lane_change')
             / NULLIF(COUNT(DISTINCT r.run_id || ':' || e.track_id), 0), 2) AS per_100
FROM fact_event e JOIN fact_run r USING (run_id)
WHERE r.is_active
GROUP BY r.camera_id;
```

**Heatmap lajur × jam:**

```sql
SELECT EXTRACT(hour FROM event_ts_local) AS hour,
       lane_key,
       COUNT(*) AS events
FROM fact_event
WHERE event_type = 'lane_hogging_indication'
GROUP BY 1, 2
ORDER BY 1, 2;
```

Catatan: `FILTER` dan `EXTRACT` didukung DuckDB dan Postgres modern.

---

## 11. Rancangan dashboard

### Filter global
Rentang tanggal, kamera, arah (northbound/southbound), lajur, kelas, severity, rule, run aktif.

### Halaman 1 — Overview
- KPI cards: kendaraan, kendaraan/jam, lane change, indikasi hogging, **hogging per 1.000 kendaraan**,
  rata-rata dwell di lajur overtaking.
- Time series per menit: jumlah event (stacked by type).
- Bar: pangsa kendaraan per lajur, dipisah arah.
- Line/area: tren hogging harian.
- Banner disclaimer: "Indikasi berbasis aturan, bukan penentuan pelanggaran."

### Halaman 2 — Lane analysis
- Heatmap lajur × jam (kendaraan atau dwell).
- Box/violin dwell per lajur dan kelas.
- Tabel `lane_usage`: kendaraan, stint, total/avg/max dwell, share.
- Lane change matrix (from → to).

### Halaman 3 — Hogging explorer
- Tabel event: timestamp, track, kelas, lajur, dwell, severity, rule, confidence, link ke klip/video.
- Breakdown severity & rule.
- Distribusi dwell untuk event hogging vs non-hogging.
- (Opsional) thumbnail crop bbox pada frame event.

### Halaman 4 — Run & data quality
- Daftar run: kamera, video sumber, base timestamp, frame, fps proses, durasi, status aktif/superseded.
- Cek kualitas: duplikat `event_id`, baris karantina, event di luar rentang waktu,
  `lane_enter` vs `lane_exit`, jumlah track.
- Tombol/tautan unduh CSV & Parquet.

### Catatan UX
- Default ke run aktif terbaru; sembunyikan run superseded kecuali di halaman kualitas.
- Jangan agregasi lintas run tanpa dedupe kendaraan.
- Tampilkan satuan dengan jelas: `dwell_seconds` = detik, bukan menit.
- Sertakan tooltip yang menjelaskan keterbatasan kamera (short baseline).

---

## 12. Roadmap implementasi

| Milestone | Deliverable | Kriteria selesai |
| --- | --- | --- |
| **M0 — Webhook + Kafka** | FastAPI `:5000` dengan `/webhook`, `/webhook/tracks`, `/webhook/run`, auth token, dedupe, health; Kafka + 3 topic | POST batch dari curl/Colab masuk ke Kafka; `/health` hijau |
| **M1 — Consumer & validasi** | Consumer `lane-pipeline`: schema check, dedupe `(run_id, event_id)`, bronze/silver Parquet, `fact_run` | Replay run final menghasilkan 158 kendaraan, 111 event, 2 change, 5 hogging |
| **M2 — Model & metrik** | `fact_lane_stint`, dimensi, tabel `agg_*`, SQL metrik | Angka cocok dengan `summary.json` run final |
| **M3 — Dashboard MVP** | Streamlit: Overview + Hogging explorer + filter, indikator lag | Refresh mengikuti batch masuk; lag `ingested_at - timestamp` tampil |
| **M4 — Kualitas & operasi** | Karantina, halaman run, status `complete`, penanganan `superseded`, retensi Kafka | Replay ulang tidak menggandakan angka |
| **M5 — Skala & fallback** | Multi-kamera, ingest langsung dari file, partisi tanggal | 3 kamera × 24 jam teragregasi < 5 menit |

---

## 13. Batasan & jebakan (ringkas)

- **Short baseline**: dwell 0,3–3 detik; arti "hogging" berbeda dari sistem berbasis lintasan panjang.
- **Tanpa estimasi kecepatan**; aturan hanya memakai dwell, kelas, dan lajur.
- **Lane assignment = point test + hysteresis**, bukan segmentasi. Kendaraan yang straddling garis
  bisa berubah lajur karena noise; adaptive anchor mengurangi kasus ini.
- **ByteTrack ID switch** setelah oklusi panjang → satu kendaraan bisa jadi beberapa track.
- **Threshold heuristik**, perlu ditinjau terhadap aturan jalan sebenarnya.
- **Deteksi malam** bisa miss kendaraan gelap → angka adalah lower bound.
- **Video anotasi mahal**; jangan jadikan artefak wajib di pipeline produksi.
- **Reprocessing** menghasilkan `event_id` baru; dedupe harus berbasis run yang aktif.
- **URL ngrok berubah** setiap restart; simpan token/URL sebagai secret dan perbarui saat ganti
  sesi. Untuk demo yang lebih stabil, gunakan domain statis atau Cloudflare Tunnel.
- **Webhook bersifat at-least-once**: retry bisa menduplikasi event; dedupe wajib.
- **Laptop harus tetap hidup** selama replay; sleep/hibernate memutus receiver dan tunnel.
- **Kafka harus siap sebelum replay**; sender melakukan preflight `/health`.
- **Replay bukan pengganti real-time**: yang disimulasikan adalah kedatangan data, bukan
  inference pada video live.

---

## 14. Cara mereproduksi data (referensi)

```bash
# Proses satu video (contoh video 3, run final)
lane-analytics analyze \
  --video data/raw/km58_400B_tj_priok_20261107_162550.mp4 \
  --base-timestamp "2026-11-07T23:25:41" \
  --run-id km58_162550_final

# Hanya event + report, tanpa video anotasi (lebih cepat untuk pipeline)
lane-analytics analyze --video ... --no-video --run-id <run_id>

# Bangun ulang report dari JSONL yang sudah ada
lane-analytics summarize output/events/<run_id>.jsonl --out output/reports/<run_id>

# Pratinjau poligon lajur
lane-analytics preview-lanes --video <video> --frame 0

# Cek lingkungan
lane-analytics doctor
```

### Replay ke webhook (sender proyek ini)

Dijalankan setelah receiver `:5000` dan tunnel siap. Kontraknya ada di bagian 4.5.

```bash
lane-analytics replay \
  --events output/events/<run_id>.jsonl \
  --tracks output/tracks/<run_id>.jsonl \
  --summary output/reports/<run_id>/summary.json \
  --run-config output/reports/<run_id>/run_config.json \
  --url https://<subdomain>.ngrok-free.app \
  --token "$WEBHOOK_TOKEN" \
  --speed 1 \
  --window 5
```

Urutan yang benar:

1. `GET /health` → pastikan Kafka siap.
2. Kirim batch `/webhook` per window event-time.
3. Kirim `/webhook/tracks` sekali.
4. Kirim `/webhook/run` dengan `complete: true` paling akhir.

`--speed 1` = replay real-time (5 menit klip = 5 menit); `--speed 10` = demo 30 detik.
`timestamp` event tidak berubah pada kecepatan apa pun.

Video 2 (`km58_400B_tj_priok_20261107_162049.mp4`, `base_timestamp 2026-11-07T23:20:41`)
belum diproses — jalankan dengan perintah yang sama bila diperlukan untuk menambah data historis.

---

## 15. File & referensi

| Lokasi | Isi |
| --- | --- |
| `docs/DATA_SCHEMA.md` | kontrak event/track/report lengkap |
| `docs/schema/lane_event.schema.json` | JSON Schema event `1.0` |
| `docs/ARCHITECTURE.md` | alur internal vision pipeline |
| `docs/LANE_RULES.md` | semantik aturan & distribusi dwell |
| `docs/RUNBOOK.md` | troubleshooting operasional |
| `config/config.yaml` | kamera, poligon 6 lajur, anchor, aturan hogging |
| `docs/sample_output/km58_lane_mapping.jpg` | gambar mapping lajur |
| `docs/sample_output/km58_summary.md` | laporan run video 1 |
| `output/events/*.jsonl` | 9 file event historis (data uji ingestion) |
| `output/reports/km58_162550_final/` | contoh lengkap semua artefak report |
| `notebooks/colab_lane_occupancy.ipynb` | alur Colab (CPU/GPU) + replay webhook |

> Repo ini **tidak** berisi receiver webhook, Kafka producer/consumer, pipeline, atau dashboard.
> Kontrak transport ada di bagian 4.5; implementasinya milik proyek pipeline.

**Data uji yang bisa langsung dipakai pipeline:**

- `output/events/km58_162550_final.jsonl` — 111 event, run bersih.
- `output/events/km58_final.jsonl` — 192 event, video 1 (baseline lama).
- `output/events/km58_162550.jsonl`, `km58_162550_anchor.jsonl` — contoh **run duplikat** untuk
  menguji logika supersede/dedupe.
- `output/tracks/km58_162550_final.jsonl` — 158 track dengan `lane_stints`.
- `output/reports/km58_162550_final/summary.json` — angka acuan validasi agregasi.

---

*Dokumen ini ditulis 2026-11-08 dari kondisi repo pada commit `c289de1`.*

---

## 16. Pembaruan — evidence snapshot + klip 8 detik

Setiap `analyze` kini menghasilkan bukti visual per event secara otomatis:

```text
output/evidence/<run_id>/
  index.json                 # event -> snapshot + clip
  snapshots/snap_*.jpg       # frame tepat di event (dari video anotasi)
  clips/clip_*.mp4           # 8 detik: 4 s sebelum + 4 s sesudah
```

Event yang berbagi frame (`lane_exit` → `lane_change` → `lane_enter`) berbagi satu snapshot
dan satu klip. Index menyimpan `event_id`, `event_type`, `track_id`, `object_class`,
`lane_name`, `timestamp`, `video_time_s`, `frame_index`, `severity`, `rule_name`,
`snapshot`, `clip`, `clip_start_s`, `clip_end_s`, dan `clip_duration_s`. Path media relatif
terhadap `output/evidence/<run_id>/`.

Konfigurasi:

```yaml
evidence:
  enabled: true
  event_types: []          # [] = semua event
  before_seconds: 4.0
  after_seconds: 4.0
  snapshot_format: jpg
  snapshot_quality: 92
  clip_codec: libx264
  clip_preset: veryfast
  clip_crf: 23
  max_clips: null
  output_dir: null         # default output/evidence/<run_id>
```

Perintah:

- `lane-analytics analyze --no-evidence` untuk mematikan pada satu run.
- `lane-analytics evidence --run-id <run_id>` untuk backfill run lama.
- Offset `--start-frame` dibaca otomatis dari `reports/<run_id>/summary.json`; override
  dengan `--time-offset`.

Catatan integrasi: bukti belum dikirim lewat webhook. Pipeline yang berjalan di mesin yang
sama dapat membaca `output/evidence/<run_id>/` langsung. Jika dibuat di Colab, salin folder
itu ke Drive sebelum sesi berakhir. Dashboard sebaiknya menampilkan snapshot inline,
memutar klip saat diklik, dan menautkan video penuh pada `t=video_time_s`.
