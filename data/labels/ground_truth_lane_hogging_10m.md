# Ground Truth — Lane Hogging (10 menit)

Label manual untuk tuning output model pada `output/source_video_annotated_10m.zip`
(sumber: `output/Direktorat Jenderal Bina Marga 2026-11-08 07_00 (4) - 10 menit pertama.mp4`).

**Konvensi**
- Track ID mengikuti output model pada video anotasi 10 menit.
- ID dengan tanda `/` (mis. `#578/557`, `#1004/1043`, `#542/565`) berarti **kendaraan yang sama** yang terpecah menjadi dua track ID.
- `lane hogger` = kendaraan cruising di lajur kanan (lane 2/3) tanpa alasan yang sah.
- `bukan lane hogger` = kendaraan sedang mendahului (overtake) kendaraan lain, jadi keberadaannya di lajur kanan wajar.

---

## Southbound

| Track | Kelas | Lane | Hogger? | Alasan |
|---|---|---|---|---|
| #31 | truck | 2 | bukan | Ada kendaraan lambat di lane 1 |
| #119 | truck | 2 | ya | Cruising di lane 2 |
| #154 | truck | 2 | ya | Cruising di lane 2 |
| #170 | car | 3 | ya | Cruising di lane 3 |
| #222 | truck | 2 | ya | Cruising di lane 2 |
| #252 | truck | 2 | ya | Cruising di lane 2 |
| #369 | truck | 2 | ya | Cruising di lane 2 |
| #401 | car | 3 | ya | Cruising di lane 3 |
| #414 | truck | 2 | ya | Cruising di lane 2 |
| #578/557 | truck | 2 | ya | Cruising di lane 2 (satu kendaraan, 2 track ID) |
| #588 | car | 3 | ya | Cruising di lane 3 |
| #605 | truck | 2 | ya | Cruising di lane 2 |
| #638 | car | 3 | bukan | Sedang overtake #636 |
| #902 | car | 3 | bukan | Sedang overtake #903 |
| #978 | car | 3 | ya | Cruising di lane 3 |
| #998 | car | 3 | bukan | Sedang overtake #996 |
| #1020 | truck | 2 | ya | Cruising di lane 2 |
| #1004/1043 | truck | 2 | ya | Cruising di lane 2 (satu kendaraan, 2 track ID) |
| #1014 | car | 3 | bukan | Sedang overtake #1020 |
| #1059 | car | 3 | bukan | Sedang overtake #1063 |
| #1063 | bus | 2 | ya | Cruising di lane 2 |
| #1115 | car | 3 | bukan | Sedang overtake #1117 |
| #1117 | bus | 2 | ya | Cruising di lane 2 |
| #1132 | car | 3 | bukan | Sedang overtake #1117 |
| #1222 | car | 3 | ya | Cruising di lane 3 |
| #1302 | car | 3 | ya | Cruising di lane 3 |
| #1304 | car | 3 | ya | Cruising di lane 3 |

Ringkasan: 27 kendaraan — 19 lane hogger, 8 bukan.

---

## Northbound

| Track | Kelas | Lane | Hogger? | Alasan |
|---|---|---|---|---|
| #22 | truck | 2 | ya | Cruising di lane 2 |
| #319 | car | 3 | ya | Cruising di lane 3 |
| #349 | car | 3 | ya | Cruising di lane 3 |
| #365 | truck | 2 | ya | Cruising di lane 2 |
| #378 | car | 3 | bukan | Sedang overtake #365 |
| #389 | car | 3 | ya | Cruising di lane 3 |
| #438 | truck | 2 | ya | Cruising di lane 2 |
| #490 | car | 3 | ya | Cruising di lane 3 |
| #495 | truck | 2 | ya | Cruising di lane 2 |
| #542/565 | car | 3 | ya | Cruising di lane 3 (satu kendaraan, 2 track ID) |
| #582 | truck | 2 | ya | Cruising di lane 2 |
| #609 | car | 3 | bukan | Sedang overtake #582 |
| #717 | car | 3 | ya | Cruising di lane 3 |
| #786 | car | 3 | ya | Cruising di lane 3 |
| #841 | truck | 2 | ya | Cruising di lane 2 |
| #873 | truck | 2 | ya | Cruising di lane 2 |
| #989 | truck | 2 | ya | Cruising di lane 2 |
| #1015 | car | 3 | ya | Cruising di lane 3 |
| #1073 | car | 3 | bukan | Sedang overtake #1075 |
| #1075 | truck | 2 | ya | Cruising di lane 2 |
| #1080 | truck | 2 | ya | Cruising di lane 2 |
| #1101 | truck | 2 | ya | Cruising di lane 2 |
| #1140 | car | 3 | ya | Cruising di lane 3 |
| #1150 | car | 3 | ya | Cruising di lane 3 |
| #1159 | car | 3 | ya | Cruising di lane 3 |
| #1174 | truck | 2 | bukan | Sedang overtake #1175 |
| #1179 | car | 3 | bukan | Sedang overtake #1074 |
| #1294 | car | 3 | ya | Cruising di lane 3 |
| #1311 | bus | 2 | ya | Cruising di lane 2 |

Ringkasan: 29 kendaraan — 24 lane hogger, 5 bukan.

---

## Catatan tuning

1. **Overtake bukan pelanggaran.** Semua kasus `bukan lane hogger` di lane 2/3 punya kendaraan yang sedang didahului — model harus membedakan "cruising" vs "mendahului".
2. **Kendaraan lambat di lane 1** membuat kendaraan di lane 2 sah berada di sana (#31) — perlu konteks lajur lain, bukan hanya lajur kendaraan itu sendiri.
3. **ID split** (`#578/557`, `#1004/1043`, `#542/565`) harus digabung sebelum klasifikasi; kalau tidak, dwell per ID terpotong dan hogging tidak terdeteksi.
4. **Bus dan truck** mendominasi lane 2, **car** mendominasi lane 3 — threshold dwell sebaiknya tidak dibedakan per kelas kecuali ada bukti tambahan.
5. Total: **56 kendaraan** — 43 hogger, 13 bukan.
