# Bot Telegram: Sistem Kehadiran Harian (Status + Lokasi GPS + Laporan)

Setiap hari **bekerja sahaja** (Isnin–Jumaat, tak termasuk cuti umum Selangor),
pada jam `CHECKIN_TIME` (default 8 pagi), bot buka satu sesi kehadiran:
setiap ahli berdaftar akan di-DM oleh bot untuk pilih **status** (Luar Ofis,
Kursus, Emergency Leave, Cuti, WFH A/B/C, Meeting, atau Lain-lain) dan
**kongsi lokasi GPS**. Bot ping ahli yang belum respon setiap beberapa minit,
papar status board dalam group, dan pada cutoff (default 10 pagi) —
atau lebih awal jika semua dah respon — bot muktamadkan kehadiran hari itu
dan hantar laporan **PDF + Excel** kepada admin melalui **DM Telegram DAN emel**.

## ⚠️ Had teknikal penting (baca dahulu sebelum setup)

1. **Telegram tak benarkan bot senaraikan ahli group secara automatik.**
   Sebab itu setiap ahli MESTI daftar sendiri (`/daftar Nama Penuh`) supaya
   bot tahu siapa yang perlu di-track. Ahli yang tak daftar tak akan
   di-DM dan tak akan muncul dalam status board / laporan.

2. **Pilih status + kongsi lokasi berlaku dalam DM peribadi dengan bot,
   BUKAN dalam group.** Ini sebab butang "kongsi lokasi" Telegram
   (`request_location`) tidak boleh disasarkan kepada seorang ahli sahaja
   dalam group (semua ahli nampak butang yang sama, mengelirukan). Dengan
   buat semuanya dalam DM, setiap ahli ada ruang sendiri yang jelas.
   **Group hanya papar ringkasan (status board) dan reminder.**

3. **Bot tak boleh mulakan DM dengan sesiapa yang belum pernah mesej bot
   dahulu** (had rasmi Telegram). Proses `/daftar` (dalam DM) automatik
   selesaikan ini — sebab itu setiap ahli (dan admin) WAJIB DM bot dan
   `/start` dahulu sebelum boleh terima sebarang mesej daripadanya.

4. **Geolocation API (cell tower/WiFi) tidak praktikal untuk bot Telegram.**
   Google ada API berasingan untuk anggarkan lokasi guna cell tower/WiFi
   mentah, tapi ini perlukan akses radio-level peranti yang Telegram Bot
   API tidak dedahkan kepada bot — bila ahli tekan "Share Location",
   peranti mereka sendiri dah gabungkan GPS/WiFi/cell tower ikut tetapan
   OS dan cuma hantar koordinat akhir. Jadi versi ini guna sepenuhnya
   lokasi yang dikongsi oleh Telegram (`request_location`), yang sudah
   paling tepat yang boleh didapati.

## Fail dalam pakej ini

| Fail | Kegunaan |
|---|---|
| `bot.py` | Kod utama bot |
| `requirements.txt` | Senarai library Python yang diperlukan |
| `.env.example` | Templat environment variables |
| `Procfile` | Arahan untuk deploy ke Railway (worker process) |
| `holidays_selangor.json` | Senarai cuti umum Selangor 2026 (**sahkan/kemaskini setiap tahun**) |

`attendance.db` (SQLite - roster + rekod kehadiran) dicipta secara automatik
bila bot mula-mula jalan.

---

## 1. Cipta Bot Telegram (BotFather)

1. Dalam Telegram, cari **@BotFather** dan mulakan chat.
2. Hantar `/newbot`, ikut arahan (nama bot & username, contoh `HelmiCheckinBot`).
3. BotFather akan bagi satu **token** (bentuk `123456:ABC-DEF...`). Simpan token ini.
4. **Penting - disable privacy mode** supaya bot boleh baca mesej lokasi dan
   command dalam group:
   - Hantar `/mybots` kepada BotFather → pilih bot anda → **Bot Settings**
     → **Group Privacy** → **Turn off**.
5. Masukkan bot ke dalam group Telegram yang anda mahu guna.

## 2. Dapatkan Google Maps API Key (untuk nama tempat)

1. Pergi ke [Google Cloud Console](https://console.cloud.google.com/).
2. Cipta project baharu (atau guna sedia ada) dan **aktifkan billing**.
3. **APIs & Services → Library** → cari & **Enable** "**Geocoding API**".
4. **APIs & Services → Credentials** → cipta **API key** baharu.
5. (Disyorkan) Sekat API key supaya hanya boleh guna Geocoding API.

## 3. Dapatkan Gmail App Password (untuk emel laporan)

1. Aktifkan **2-Step Verification** pada akaun Gmail: [myaccount.google.com/security](https://myaccount.google.com/security).
2. Pergi ke [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Cipta App Password baharu → dapat kod 16-aksara.
4. `SMTP_USER` = alamat Gmail penuh; `SMTP_PASSWORD` = kod 16-aksara tadi
   (bukan kata laluan Gmail biasa); `ADMIN_EMAIL` = alamat yang terima laporan.

> Guna penyedia emel lain? Tukar `SMTP_HOST`/`SMTP_PORT` — logik bot guna SMTP generik.

## 4. Setup tempatan (untuk uji dahulu)

```bash
cd telegram-location-bot
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Isikan sekurang-kurangnya TELEGRAM_BOT_TOKEN dan GOOGLE_MAPS_API_KEY dahulu

python bot.py
```

### Langkah pendaftaran (WAJIB untuk setiap ahli, termasuk admin)

1. Setiap ahli (dan admin) **DM bot secara peribadi** (cari username bot,
   klik **Start**).
2. Hantar `/daftar Nama Penuh Anda` — contoh: `/daftar Ahmad bin Ali`.
3. Admin juga hantar `/adminid` dalam DM tersebut, salin User ID yang
   dipaparkan ke `ADMIN_USER_ID` dalam `.env` (perlu untuk bot boleh
   DM laporan/makluman kepada admin).

### Dapatkan GROUP_CHAT_ID

Dalam group Telegram (dengan bot sudah dimasukkan), hantar `/chatid` —
salin nombor yang dipaparkan ke `GROUP_CHAT_ID` dalam `.env`.

### Uji sesi kehadiran tanpa tunggu jam 8 pagi

Selepas sekurang-kurangnya seorang ahli `/daftar`, admin boleh hantar
`/mula` dalam group untuk mulakan sesi kehadiran serta-merta (berguna untuk
testing). Ikuti arahan yang bot DM kepada anda: pilih status → (kalau
Lain-lain, taip sebab) → tekan butang kongsi lokasi.

## 5. Jadual & cutoff

Dalam `.env`:

```
CHECKIN_TIME=08:00
CUTOFF_TIME=10:00
REMINDER_INTERVAL_MINUTES=5
```

- `CHECKIN_TIME`: bot buka sesi kehadiran secara automatik (hari bekerja sahaja).
- Setiap `REMINDER_INTERVAL_MINUTES` minit selepas itu, bot ping ahli yang
  belum respon dan kemaskini status board dalam group.
- `CUTOFF_TIME`: bot muktamadkan kehadiran secara automatik — ahli yang
  masih belum respon disenaraikan dan admin di-DM untuk susulan manual.
- Kalau **semua ahli** dah respon sebelum cutoff, bot muktamadkan awal
  (tak perlu tunggu sampai `CUTOFF_TIME`).

Command `/jadual` boleh digunakan bila-bila masa untuk semak jadual semasa.

### Cuti umum (hari bukan bekerja)

`holidays_selangor.json` mengandungi senarai cuti umum Selangor 2026 yang
disusun daripada carian web (bukan sumber rasmi terus) — **sila sahkan
dengan Warta/Pekeliling rasmi Kerajaan Negeri Selangor**
([selangor.gov.my](https://www.selangor.gov.my/index.php/pages/view/23?mid=64))
sebelum guna sebenar, terutamanya tarikh Hari Raya (ikut takwim Hijrah,
disahkan lewat) dan cuti gantian. Edit fail ini setiap tahun — format:

```json
[{"date": "2026-01-01", "name": "Tahun Baharu"}, ...]
```

## 6. Deploy ke Railway (supaya bot jalan 24/7)

1. Push kod ini ke satu repo GitHub (**jangan** sertakan `.env` sebenar
   atau `attendance.db` sebenar — `.env.example` sahaja yang perlu dalam repo).
2. Di [Railway](https://railway.app), cipta **New Project** → **Deploy from GitHub repo**.
3. Railway detect `requirements.txt` + `Procfile` dan run sebagai **worker**
   (bot guna polling, bukan webhook — tak perlu port/HTTP).
4. Dalam tab **Variables**, tambah SEMUA env var dalam `.env.example`:
   `TELEGRAM_BOT_TOKEN`, `GOOGLE_MAPS_API_KEY`, `GROUP_CHAT_ID`,
   `ADMIN_USER_ID`, `TIMEZONE`, `CHECKIN_TIME`, `CUTOFF_TIME`,
   `REMINDER_INTERVAL_MINUTES`, `HOLIDAYS_FILE`, `DB_FILE`, `SMTP_HOST`,
   `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ADMIN_EMAIL`.
5. Deploy. Semak **Deploy Logs** — patut nampak `Bot bermula (polling)...`.

> **PENTING — storan berkekalan:** `attendance.db` (roster + rekod
> kehadiran) disimpan sebagai fail tempatan. Pada Railway, storan fail
> biasa adalah *ephemeral* — **boleh hilang bila redeploy/restart**,
> bermakna roster & sejarah kehadiran hilang juga. Untuk elak ini,
> attach satu **Railway Volume** (Settings → Volumes) dan mount pada
> folder tempat `attendance.db`/`holidays_selangor.json` berada (tetapkan
> `DB_FILE=/data/attendance.db` dan letak volume di `/data`), supaya data
> kekal walaupun bot redeploy. Beritahu saya kalau nak saya bantu setkan ini.

## Command yang tersedia

| Command | Konteks | Fungsi |
|---|---|---|
| `/start` | DM / group | Mesej bantuan |
| `/daftar Nama Penuh` | DM sahaja | Daftar sebagai ahli yang di-track |
| `/senarai` | DM / group | Papar senarai ahli berdaftar |
| `/adminid` | DM sahaja | Papar User ID anda (untuk `ADMIN_USER_ID`) |
| `/chatid` | DM / group | Papar Chat ID (untuk `GROUP_CHAT_ID`) |
| `/jadual` | DM / group | Papar jadual & cutoff semasa |
| `/mula` | DM / group (admin sahaja) | Mulakan sesi kehadiran serta-merta |
| `/laporan` | DM / group (admin sahaja) | Jana & hantar laporan hari ini serta-merta |

## Susun atur aliran (ringkasan visual)

```
08:00 (hari bekerja) ─▶ Bot umum dalam GROUP + DM setiap ahli (9 pilihan status)
                            │
                 ahli pilih status ─▶ (Lain-lain? taip sebab) ─▶ kongsi lokasi
                            │
                 rekod disimpan ─▶ status board GROUP dikemaskini
                            │
   setiap 5 minit ──────────┼──▶ ping ahli belum respon + kemaskini board
                            │
        semua respon? ──ya──┴──▶ muktamad AWAL
             │tidak
             ▼
        10:00 CUTOFF ──▶ muktamad ──▶ DM admin (senarai tak respon)
                                   ──▶ jana PDF+Excel ──▶ DM admin + emel admin
```

## Kemungkinan penambahbaikan akan datang

- Railway Volume / database luaran (Postgres) untuk storan kekal — disyorkan sebelum guna production.
- Laporan mingguan/bulanan tambahan (bukan setakat harian).
- Butang "Hadir di Pejabat" jika nanti perlukan status kehadiran biasa juga.
- Auto-kemaskini kalendar cuti umum tahun hadapan.
