"""
Bot Telegram: Sistem Kehadiran Harian (Status + Lokasi GPS + Laporan)
=======================================================================

RINGKASAN ALIRAN
----------------
1. Setiap hari BEKERJA sahaja (Isnin-Jumaat, tak termasuk cuti umum Selangor
   dalam holidays_selangor.json), pada waktu CHECKIN_TIME (default 08:00),
   bot:
     a. Umumkan dalam GROUP bahawa sesi kehadiran hari ini dibuka.
     b. DM setiap ahli berdaftar (roster) dengan 9 pilihan status (butang):
        Luar Ofis, Kursus, Emergency Leave, Cuti, WFH A, WFH B, WFH C,
        Meeting, Lain-lain.
2. Ahli klik satu status (dalam DM peribadi dengan bot):
     - Kalau "Lain-lain", bot minta taip sebab dahulu.
     - Bot kemudian minta ahli kongsi LOKASI GPS (wajib untuk semua status).
     - Selepas lokasi diterima, rekod disimpan (status+lokasi+masa) dan
       "status board" dalam group dikemaskini.
3. Dari CHECKIN_TIME sehingga CUTOFF_TIME (default 10:00), setiap
   REMINDER_INTERVAL_MINUTES minit, bot hantar reminder (tag) dalam group
   kepada ahli yang belum respon, dan kemaskini status board.
4. Bila SEMUA ahli berdaftar dah respon (lebih awal), ATAU bila CUTOFF_TIME
   sampai (mana dahulu), bot muktamadkan kehadiran hari itu:
     - Papar status board akhir dalam group.
     - Kalau ada yang tak respon, DM admin untuk tindakan manual.
     - Jana laporan PDF + Excel dan hantar kepada admin melalui DM Telegram
       DAN emel.

HAD TEKNIKAL PENTING (baca README.md untuk penjelasan penuh):
  - Telegram Bot API tidak boleh senaraikan ahli group secara automatik -
    setiap ahli MESTI /daftar dahulu (dalam DM peribadi bot) supaya bot
    tahu siapa nak di-track.
  - Butang "kongsi lokasi" hanya boleh berfungsi dengan baik dalam DM
    peribadi (bukan dalam group) - sebab itu seluruh interaksi status+lokasi
    berlaku dalam DM, manakala GROUP cuma papar ringkasan/status board.
  - Bot tak boleh DM sesiapa yang belum pernah mulakan chat dengannya -
    /daftar dalam DM automatik selesaikan isu ini untuk ahli & admin.
  - Geolocation API cell tower/WiFi Google TIDAK digunakan (diputuskan) -
    lokasi yang dikongsi Telegram (request_location) sudah cukup tepat.
  - DM peribadi bot HANYA untuk urusan kehadiran rasmi - command/mesej lain
    daripada ahli (bukan admin) akan diarah hubungi admin terus, bukan
    dilayan sebagai chatbot umum.

STORAN: roster + rekod kehadiran disimpan dalam PostgreSQL (env DATABASE_URL),
bukan fail tempatan - lihat README bahagian setup Railway Postgres.
"""

import json
import logging
import os
import smtplib
from datetime import date, datetime, time as dtime, timedelta
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------------------------------------------------------
# Konfigurasi
# ----------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")  # Telegram numeric user id, guna /adminid dalam DM
# Teks hubungan admin (cth. "@helmi_username") dipaparkan kepada ahli yang cuba
# "berbual" dengan bot di luar aliran rasmi. Kosongkan untuk bot cuba kesan
# @username admin secara automatik (perlu ADMIN_USER_ID & admin dah /start bot).
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "")
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Kuala_Lumpur")

CHECKIN_TIME_RAW = os.environ.get("CHECKIN_TIME", "08:00")
CUTOFF_TIME_RAW = os.environ.get("CUTOFF_TIME", "10:00")
REMINDER_INTERVAL_MINUTES = int(os.environ.get("REMINDER_INTERVAL_MINUTES", "5"))

HOLIDAYS_FILE = Path(os.environ.get("HOLIDAYS_FILE", "holidays_selangor.json"))
# Connection string PostgreSQL, cth. postgresql://user:pass@host:5432/dbname
# Railway auto-sediakan ini bila anda tambah database Postgres dalam project.
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- Emel laporan ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

TZ = ZoneInfo(TIMEZONE_NAME)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 9 kategori status (kod -> label paparan)
STATUSES = [
    ("luar_ofis", "🚗 Luar Ofis"),
    ("kursus", "📚 Kursus"),
    ("emergency_leave", "🚨 Emergency Leave"),
    ("cuti", "🏖️ Cuti"),
    ("wfh_a", "🏠 WFH A"),
    ("wfh_b", "🏠 WFH B"),
    ("wfh_c", "🏠 WFH C"),
    ("meeting", "🤝 Meeting"),
    ("lain_lain", "✏️ Lain-lain"),
]
STATUS_LABELS = dict(STATUSES)

LOCATION_BUTTON = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Kongsi Lokasi Sekarang", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

STATUS_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton(label, callback_data=code)] for code, label in STATUSES]
)


# ----------------------------------------------------------------------------
# Cuti umum / hari bekerja
# ----------------------------------------------------------------------------

def load_holidays() -> set[str]:
    """Baca senarai cuti umum (format [{"date": "YYYY-MM-DD", "name": ...}, ...])."""
    if not HOLIDAYS_FILE.exists():
        logger.warning("Fail cuti umum %s tidak dijumpai - anggap tiada cuti umum.", HOLIDAYS_FILE)
        return set()
    try:
        data = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
        return {item["date"] for item in data}
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        logger.error("Gagal baca fail cuti umum: %s", exc)
        return set()


def is_working_day(d: date) -> bool:
    """Isnin-Jumaat dan bukan cuti umum yang disenaraikan."""
    if d.weekday() >= 5:  # 5=Sabtu, 6=Ahad
        return False
    holidays = load_holidays()
    return d.isoformat() not in holidays


def is_admin(user_id: int) -> bool:
    return bool(ADMIN_USER_ID) and str(user_id) == str(ADMIN_USER_ID)


async def get_admin_contact_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Teks untuk arahkan ahli menghubungi admin (bukan bot) untuk urusan lain."""
    if ADMIN_CONTACT:
        return ADMIN_CONTACT

    cached = context.bot_data.get("admin_contact")
    if cached:
        return cached

    if ADMIN_USER_ID:
        try:
            chat = await context.bot.get_chat(ADMIN_USER_ID)
            contact = f"@{chat.username}" if chat.username else (chat.full_name or "admin group")
            context.bot_data["admin_contact"] = contact
            return contact
        except TelegramError:
            pass

    return "admin group"


# ----------------------------------------------------------------------------
# Storan (PostgreSQL - cth. Railway Postgres): roster + rekod kehadiran harian
#
# Nota: fungsi di bawah guna psycopg2 (synchronous) untuk kesederhanaan -
# memandangkan bot ini kegunaan satu pejabat/group kecil dengan bilangan
# operasi DB yang sangat rendah (beberapa kali sejam), overhead blocking
# call psycopg2 dalam handler async boleh diterima. Kalau nak scale lebih
# besar nanti, pertimbangkan tukar ke asyncpg / connection pool.
# ----------------------------------------------------------------------------

def get_db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL belum ditetapkan. Sila tambah database PostgreSQL "
            "(cth. dalam Railway) dan isikan connection string dalam .env / Variables."
        )
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS roster (
                    user_id BIGINT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    username TEXT,
                    registered_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    date TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    full_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    place_name TEXT,
                    timestamp_local TEXT NOT NULL,
                    PRIMARY KEY (date, user_id)
                )
                """
            )
    finally:
        conn.close()


def register_member(user_id: int, full_name: str, username: str | None) -> None:
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO roster (user_id, full_name, username, registered_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    username = EXCLUDED.username
                """,
                (user_id, full_name, username, datetime.now(TZ).isoformat()),
            )
    finally:
        conn.close()


def get_roster() -> list[dict]:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roster ORDER BY full_name")
            return cur.fetchall()
    finally:
        conn.close()


def get_member(user_id: int) -> dict | None:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM roster WHERE user_id = %s", (user_id,))
            return cur.fetchone()
    finally:
        conn.close()


def record_attendance(
    date_str: str,
    user_id: int,
    full_name: str,
    status: str,
    note: str | None,
    lat: float,
    lng: float,
    place_name: str,
    timestamp_local: str,
) -> None:
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO attendance
                    (date, user_id, full_name, status, note, latitude, longitude, place_name, timestamp_local)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date, user_id) DO UPDATE SET
                    full_name = EXCLUDED.full_name,
                    status = EXCLUDED.status,
                    note = EXCLUDED.note,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    place_name = EXCLUDED.place_name,
                    timestamp_local = EXCLUDED.timestamp_local
                """,
                (date_str, user_id, full_name, status, note, lat, lng, place_name, timestamp_local),
            )
    finally:
        conn.close()


def get_attendance_for_date(date_str: str) -> list[dict]:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM attendance WHERE date = %s", (date_str,))
            return cur.fetchall()
    finally:
        conn.close()


def clear_attendance_for_date(date_str: str) -> int:
    """Padam semua rekod kehadiran bagi satu tarikh. Pulangkan bilangan baris dipadam.

    Untuk testing/pembetulan sahaja (cth. /resetkehadiran) - membolehkan admin
    "reset" hari semasa tanpa perlu tunggu esok, memandangkan rekod kehadiran
    disimpan per (tarikh, user_id) dan kekal sepanjang hari itu merentasi
    berapa kali /mula dijalankan semula.
    """
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM attendance WHERE date = %s", (date_str,))
            return cur.rowcount
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Reverse geocoding (Google Maps)
# ----------------------------------------------------------------------------

def reverse_geocode(lat: float, lng: float) -> str:
    if not GOOGLE_MAPS_API_KEY:
        return "(GOOGLE_MAPS_API_KEY belum ditetapkan)"

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"latlng": f"{lat},{lng}", "key": GOOGLE_MAPS_API_KEY}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.error("Ralat memanggil Google Maps API: %s", exc)
        return "(gagal hubungi Google Maps API)"

    status = data.get("status")
    if status != "OK":
        logger.warning("Google Maps API status: %s - %s", status, data.get("error_message"))
        return f"(tiada alamat dijumpai - status: {status})"

    results = data.get("results", [])
    if not results:
        return "(tiada alamat dijumpai)"
    return results[0].get("formatted_address", "(alamat tidak diketahui)")


# ----------------------------------------------------------------------------
# Status board (mesej dalam group yang dikemaskini)
# ----------------------------------------------------------------------------

def build_status_board_text(date_str: str) -> str:
    roster = get_roster()
    records = {r["user_id"]: r for r in get_attendance_for_date(date_str)}

    lines = [f"📋 *Status Kehadiran - {date_str}*\n"]
    if not roster:
        lines.append("_Tiada ahli berdaftar. Guna /daftar Nama Penuh dalam DM peribadi bot._")
        return "\n".join(lines)

    responded = 0
    for member in roster:
        rec = records.get(member["user_id"])
        if rec:
            responded += 1
            label = STATUS_LABELS.get(rec["status"], rec["status"])
            masa = datetime.fromisoformat(rec["timestamp_local"]).strftime("%H:%M")
            lines.append(f"✅ {member['full_name']} — {label} ({masa})")
        else:
            lines.append(f"⏳ {member['full_name']} — belum respon")

    lines.append(f"\n*{responded}/{len(roster)}* ahli telah respon.")
    return "\n".join(lines)


async def update_board(context: ContextTypes.DEFAULT_TYPE) -> None:
    board_chat_id = context.bot_data.get("board_chat_id")
    board_message_id = context.bot_data.get("board_message_id")
    date_str = context.bot_data.get("session_date")
    if not (board_chat_id and board_message_id and date_str):
        return
    try:
        await context.bot.edit_message_text(
            chat_id=board_chat_id,
            message_id=board_message_id,
            text=build_status_board_text(date_str),
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        # Selalunya "message is not modified" - selamat diabaikan
        logger.debug("Gagal kemaskini status board: %s", exc)


# ----------------------------------------------------------------------------
# Laporan (PDF + Excel) & penghantaran (DM Telegram + emel)
# ----------------------------------------------------------------------------

def build_report_rows(date_str: str) -> list[dict]:
    """Gabungkan roster + rekod kehadiran -> satu baris setiap ahli (termasuk yang tak respon)."""
    roster = get_roster()
    records = {r["user_id"]: r for r in get_attendance_for_date(date_str)}
    rows = []
    for member in roster:
        rec = records.get(member["user_id"])
        if rec:
            rows.append(
                {
                    "full_name": member["full_name"],
                    "status": STATUS_LABELS.get(rec["status"], rec["status"]),
                    "note": rec["note"] or "",
                    "time": datetime.fromisoformat(rec["timestamp_local"]).strftime("%H:%M:%S"),
                    "latitude": rec["latitude"],
                    "longitude": rec["longitude"],
                    "place_name": rec["place_name"],
                }
            )
        else:
            rows.append(
                {
                    "full_name": member["full_name"],
                    "status": "Tiada Respon",
                    "note": "",
                    "time": "-",
                    "latitude": None,
                    "longitude": None,
                    "place_name": "-",
                }
            )
    return rows


def build_pdf_report(rows: list[dict], target_date: date) -> Path:
    tmp_path = Path(TemporaryDirectory().name)
    tmp_path.mkdir(parents=True, exist_ok=True)
    pdf_path = tmp_path / f"laporan_kehadiran_{target_date.isoformat()}.pdf"

    doc = SimpleDocTemplate(str(pdf_path), pagesize=landscape(A4))
    styles = getSampleStyleSheet()
    elements = [
        Paragraph(f"Laporan Kehadiran - {target_date.strftime('%d/%m/%Y')}", styles["Title"]),
        Spacer(1, 12),
    ]

    header = ["Nama", "Status", "Catatan", "Masa", "Latitud", "Longitud", "Lokasi"]
    data = [header]
    for r in rows:
        data.append(
            [
                r["full_name"],
                r["status"],
                r["note"],
                r["time"],
                f"{r['latitude']:.6f}" if r["latitude"] is not None else "-",
                f"{r['longitude']:.6f}" if r["longitude"] is not None else "-",
                r["place_name"] or "-",
            ]
        )

    if len(data) == 1:
        elements.append(Paragraph("Tiada ahli berdaftar.", styles["Normal"]))
    else:
        table = Table(data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        elements.append(table)

    doc.build(elements)
    return pdf_path


def build_excel_report(rows: list[dict], target_date: date) -> Path:
    tmp_path = Path(TemporaryDirectory().name)
    tmp_path.mkdir(parents=True, exist_ok=True)
    xlsx_path = tmp_path / f"laporan_kehadiran_{target_date.isoformat()}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "Kehadiran"

    header = ["Nama", "Status", "Catatan", "Tarikh", "Masa", "Latitud", "Longitud", "Lokasi"]
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for r in rows:
        ws.append(
            [
                r["full_name"],
                r["status"],
                r["note"],
                target_date.strftime("%d/%m/%Y"),
                r["time"],
                r["latitude"],
                r["longitude"],
                r["place_name"],
            ]
        )

    for col_cells in ws.columns:
        length = max(len(str(c.value)) if c.value is not None else 0 for c in col_cells)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 50)

    wb.save(xlsx_path)
    return xlsx_path


def send_report_email(rows: list[dict], target_date: date, pdf_path: Path, xlsx_path: Path) -> None:
    if not (SMTP_USER and SMTP_PASSWORD and ADMIN_EMAIL):
        logger.warning("SMTP_USER/SMTP_PASSWORD/ADMIN_EMAIL belum lengkap - emel laporan dilangkau.")
        return

    responded = sum(1 for r in rows if r["status"] != "Tiada Respon")
    msg = EmailMessage()
    msg["Subject"] = f"Laporan Kehadiran {target_date.strftime('%d/%m/%Y')}"
    msg["From"] = SMTP_USER
    msg["To"] = ADMIN_EMAIL
    msg.set_content(
        f"Salam,\n\nLaporan kehadiran bertarikh {target_date.strftime('%d/%m/%Y')}: "
        f"{responded}/{len(rows)} ahli telah respon.\n\n"
        f"Emel ini dijana secara automatik oleh bot kehadiran."
    )

    for path, mime_subtype in (
        (pdf_path, "pdf"),
        (xlsx_path, "vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        with path.open("rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype=mime_subtype, filename=path.name)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Emel laporan %s dihantar ke %s.", target_date, ADMIN_EMAIL)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Gagal hantar emel laporan: %s", exc)


async def send_report_dm(context: ContextTypes.DEFAULT_TYPE, rows: list[dict], target_date: date,
                          pdf_path: Path, xlsx_path: Path) -> None:
    if not ADMIN_USER_ID:
        logger.warning("ADMIN_USER_ID belum ditetapkan - laporan DM Telegram dilangkau.")
        return

    responded = sum(1 for r in rows if r["status"] != "Tiada Respon")
    caption = (
        f"📊 Laporan Kehadiran {target_date.strftime('%d/%m/%Y')}\n"
        f"{responded}/{len(rows)} ahli telah respon."
    )
    try:
        await context.bot.send_message(chat_id=ADMIN_USER_ID, text=caption)
        with pdf_path.open("rb") as f:
            await context.bot.send_document(chat_id=ADMIN_USER_ID, document=f, filename=pdf_path.name)
        with xlsx_path.open("rb") as f:
            await context.bot.send_document(chat_id=ADMIN_USER_ID, document=f, filename=xlsx_path.name)
    except TelegramError as exc:
        logger.error(
            "Gagal DM laporan kepada admin (%s). Pastikan admin dah /start bot secara peribadi dahulu. Ralat: %s",
            ADMIN_USER_ID, exc,
        )


# ----------------------------------------------------------------------------
# Sesi kehadiran harian: mula, reminder, finalize
# ----------------------------------------------------------------------------

async def start_attendance_session(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> None:
    today = datetime.now(TZ).date()
    date_str = today.isoformat()

    context.bot_data["attendance_open"] = True
    context.bot_data["session_date"] = date_str
    context.bot_data["pending"] = {}
    context.bot_data["awaiting_note"] = set()

    roster = get_roster()
    if not roster:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Sesi kehadiran hari ini tidak dapat dimulakan - tiada ahli berdaftar. "
                 "Setiap ahli perlu hantar /daftar Nama Penuh dalam DM peribadi dengan bot dahulu.",
        )
        context.bot_data["attendance_open"] = False
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔔 Sesi kehadiran hari ini ({today.strftime('%d/%m/%Y')}) telah dibuka!\n"
            f"Sila semak DM peribadi anda dengan bot ini untuk pilih status & kongsi lokasi.\n"
            f"Tarikh akhir: {CUTOFF_TIME_RAW}."
        ),
    )

    board_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=build_status_board_text(date_str),
        parse_mode=ParseMode.MARKDOWN,
    )
    context.bot_data["board_chat_id"] = board_msg.chat_id
    context.bot_data["board_message_id"] = board_msg.message_id

    for member in roster:
        try:
            await context.bot.send_message(
                chat_id=member["user_id"],
                text=f"Salam {member['full_name']}, sila pilih status kehadiran anda hari ini:",
                reply_markup=STATUS_KEYBOARD,
            )
        except TelegramError as exc:
            logger.warning(
                "Tak dapat DM ahli %s (%s) - mungkin belum /start bot secara peribadi. Ralat: %s",
                member["full_name"], member["user_id"], exc,
            )

    # Jadualkan reminder berkala & cutoff paksa
    context.job_queue.run_repeating(
        reminder_job,
        interval=REMINDER_INTERVAL_MINUTES * 60,
        first=REMINDER_INTERVAL_MINUTES * 60,
        chat_id=chat_id,
        name="reminder_job",
    )

    cutoff_time = parse_single_time(CUTOFF_TIME_RAW) or dtime(hour=10, minute=0, tzinfo=TZ)
    cutoff_dt = datetime.combine(today, cutoff_time.replace(tzinfo=None), tzinfo=TZ)
    if cutoff_dt <= datetime.now(TZ):
        cutoff_dt = cutoff_dt + timedelta(days=1)  # elak run_once di masa lalu jika /mula lepas cutoff
    context.job_queue.run_once(cutoff_job, when=cutoff_dt, chat_id=chat_id, name="cutoff_job")


def _cancel_jobs(context: ContextTypes.DEFAULT_TYPE, name: str) -> None:
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


async def finalize_attendance(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> None:
    if not context.bot_data.get("attendance_open"):
        return  # dah finalize (elak double-run bila early-finish & cutoff berlaku serentak)

    context.bot_data["attendance_open"] = False
    _cancel_jobs(context, "reminder_job")
    _cancel_jobs(context, "cutoff_job")

    date_str = context.bot_data.get("session_date", datetime.now(TZ).date().isoformat())
    target_date = date.fromisoformat(date_str)

    await update_board(context)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Sesi kehadiran {target_date.strftime('%d/%m/%Y')} telah dimuktamadkan.",
    )

    rows = build_report_rows(date_str)
    missing = [r["full_name"] for r in rows if r["status"] == "Tiada Respon"]

    if missing and ADMIN_USER_ID:
        senarai = "\n".join(f"- {name}" for name in missing)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    f"⚠️ Ahli berikut TIDAK respon kehadiran pada {target_date.strftime('%d/%m/%Y')}:\n"
                    f"{senarai}\n\nSila buat susulan manual."
                ),
            )
        except TelegramError as exc:
            logger.error("Gagal DM admin pasal ahli tak respon: %s", exc)

    pdf_path = build_pdf_report(rows, target_date)
    xlsx_path = build_excel_report(rows, target_date)
    await send_report_dm(context, rows, target_date, pdf_path, xlsx_path)
    send_report_email(rows, target_date, pdf_path, xlsx_path)


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get("attendance_open"):
        context.job.schedule_removal()
        return

    date_str = context.bot_data.get("session_date")
    roster = get_roster()
    responded_ids = {r["user_id"] for r in get_attendance_for_date(date_str)}
    missing = [m for m in roster if m["user_id"] not in responded_ids]

    await update_board(context)

    if not missing:
        return  # early-finish sepatutnya dah handle, tapi jaga-jaga

    mentions = []
    for m in missing:
        if m["username"]:
            mentions.append(f"@{m['username']}")
        else:
            mentions.append(f"[{m['full_name']}](tg://user?id={m['user_id']})")

    chat_id = context.job.chat_id
    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ Peringatan: sila update status kehadiran anda dalam DM peribadi bot -\n" + ", ".join(mentions),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cutoff_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    await finalize_attendance(context, chat_id)


async def maybe_early_finish(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get("attendance_open"):
        return
    date_str = context.bot_data.get("session_date")
    roster = get_roster()
    if not roster:
        return
    responded_ids = {r["user_id"] for r in get_attendance_for_date(date_str)}
    if all(m["user_id"] in responded_ids for m in roster):
        chat_id = context.bot_data.get("board_chat_id") or GROUP_CHAT_ID
        await finalize_attendance(context, chat_id)


# ----------------------------------------------------------------------------
# Handlers - command
# ----------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Salam! Bot Sistem Kehadiran.\n\n"
            "Langkah pertama: daftar guna\n"
            "/daftar Nama Penuh Anda\n\n"
            "Selepas berdaftar, setiap hari bekerja jam " + CHECKIN_TIME_RAW + " bot akan hantar "
            "pilihan status kehadiran kepada anda di sini."
        )
    else:
        await update.effective_message.reply_text(
            "Bot Sistem Kehadiran sedia digunakan dalam group ini.\n\n"
            "Setiap ahli perlu DM bot ini secara peribadi dan hantar /daftar Nama Penuh untuk berdaftar.\n\n"
            "Command admin: /mula, /laporan, /resetkehadiran, /senarai, /jadual, /chatid"
        )


async def cmd_daftar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Sila hantar /daftar dalam DM peribadi dengan bot ini (bukan dalam group)."
        )
        return

    full_name = " ".join(context.args).strip()
    if not full_name:
        await update.effective_message.reply_text("Format: /daftar Nama Penuh Anda")
        return

    user = update.effective_user
    register_member(user.id, full_name, user.username)
    await update.effective_message.reply_text(
        f"Pendaftaran berjaya: {full_name}.\n"
        f"Anda akan terima soalan status kehadiran di sini setiap hari bekerja jam {CHECKIN_TIME_RAW}."
    )


async def cmd_senarai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    roster = get_roster()
    if not roster:
        await update.effective_message.reply_text("Tiada ahli berdaftar lagi.")
        return
    senarai = "\n".join(f"- {m['full_name']} (@{m['username']})" if m["username"]
                         else f"- {m['full_name']}" for m in roster)
    await update.effective_message.reply_text(f"Ahli berdaftar ({len(roster)}):\n{senarai}")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Chat ID ini ialah:\n`{chat.id}`\n\n"
        "Kalau ini group, salin ke GROUP_CHAT_ID dalam .env / Railway Variables.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_adminid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text("Sila hantar /adminid dalam DM peribadi dengan bot ini.")
        return
    await update.effective_message.reply_text(
        f"User ID Telegram anda ialah:\n`{update.effective_user.id}`\n\n"
        "Salin ke ADMIN_USER_ID dalam .env / Railway Variables supaya bot boleh DM laporan kepada anda.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_jadual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Waktu mula sesi (CHECKIN_TIME): {CHECKIN_TIME_RAW}\n"
        f"Waktu cutoff (CUTOFF_TIME): {CUTOFF_TIME_RAW}\n"
        f"Selang reminder: setiap {REMINDER_INTERVAL_MINUTES} minit\n"
        f"Zon masa: {TIMEZONE_NAME}\n"
        f"Hari bekerja: Isnin-Jumaat, tak termasuk cuti umum dalam {HOLIDAYS_FILE.name}"
    )


async def cmd_mula(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Command ini untuk admin sahaja.")
        return
    chat_id = GROUP_CHAT_ID or update.effective_chat.id
    await update.effective_message.reply_text("Memulakan sesi kehadiran...")
    await start_attendance_session(context, chat_id)


async def cmd_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin sahaja: jana & hantar laporan (PDF+Excel) secara adhoc, DM atau group.

    Tanpa argumen: laporan hari ini (atau sesi semasa jika ada).
    Dengan argumen tarikh: /laporan YYYY-MM-DD - laporan bagi tarikh lampau.
    Nota: roster (nama/username ahli) diambil ikut keadaan SEKARANG, jadi ahli
    yang didaftar/dibuang selepas tarikh yang diminta tidak mencerminkan roster
    pada tarikh sebenar tersebut.
    """
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Command ini untuk admin sahaja.")
        return

    if context.args:
        date_arg = context.args[0].strip()
        try:
            target_date = date.fromisoformat(date_arg)
        except ValueError:
            await update.effective_message.reply_text(
                "Format tarikh tidak sah.\n"
                "Guna: /laporan YYYY-MM-DD (cth. /laporan 2026-09-05)\n"
                "Atau /laporan sahaja (tanpa tarikh) untuk laporan hari ini."
            )
            return
    else:
        date_str_default = context.bot_data.get("session_date") or datetime.now(TZ).date().isoformat()
        target_date = date.fromisoformat(date_str_default)

    date_str = target_date.isoformat()
    rows = build_report_rows(date_str)
    await update.effective_message.reply_text(f"Menjana & menghantar laporan {target_date.strftime('%d/%m/%Y')}...")

    pdf_path = build_pdf_report(rows, target_date)
    xlsx_path = build_excel_report(rows, target_date)
    await send_report_dm(context, rows, target_date, pdf_path, xlsx_path)
    send_report_email(rows, target_date, pdf_path, xlsx_path)

    await update.effective_message.reply_text("Laporan telah dihantar (DM Telegram & emel, jika konfigurasi lengkap).")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin sahaja: padam rekod kehadiran HARI INI (untuk testing/pembetulan).

    Rekod kehadiran disimpan per (tarikh, user_id), jadi respons awal seorang
    ahli pada hari yang sama akan kekal dikira "sudah respon" walaupun /mula
    dijalankan semula berkali-kali - ini memang sengaja untuk kegunaan produksi
    (admin re-trigger /mula tak patut hapuskan check-in yang sah). Command ini
    wujud khas untuk keadaan admin nak "reset" hari semasa secara manual, cth.
    semasa testing senario reminder/cutoff.
    """
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Command ini untuk admin sahaja.")
        return

    date_str = context.bot_data.get("session_date") or datetime.now(TZ).date().isoformat()
    deleted = clear_attendance_for_date(date_str)

    # Reset juga state dalam-memori berkaitan supaya tak tersangkut separuh jalan
    context.bot_data["pending"] = {}
    context.bot_data["awaiting_note"] = set()

    await update.effective_message.reply_text(
        f"🗑️ Rekod kehadiran bertarikh {date_str} telah dipadam ({deleted} rekod).\n"
        f"Anda boleh /mula semula untuk testing bersih."
    )


# ----------------------------------------------------------------------------
# Handlers - aliran status + lokasi (DM sahaja)
# ----------------------------------------------------------------------------

async def handle_status_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    member = get_member(user.id)
    if member is None:
        await query.answer("Sila /daftar Nama Penuh dahulu.", show_alert=True)
        return

    if not context.bot_data.get("attendance_open"):
        await query.answer("Sesi kehadiran hari ini belum dibuka / sudah tamat.", show_alert=True)
        return

    await query.answer()
    status_code = query.data
    label = STATUS_LABELS.get(status_code, status_code)
    context.bot_data.setdefault("pending", {})[user.id] = {"status": status_code, "note": None}

    if status_code == "lain_lain":
        context.bot_data.setdefault("awaiting_note", set()).add(user.id)
        await query.edit_message_text(f"Anda pilih: {label}\n\nSila taip sebab/keterangan:")
    else:
        await query.edit_message_text(f"Anda pilih: {label}\n\nSila kongsi lokasi anda sekarang:")
        await context.bot.send_message(
            chat_id=user.id,
            text="Tekan butang di bawah untuk kongsi lokasi:",
            reply_markup=LOCATION_BUTTON,
        )


async def handle_note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    awaiting = context.bot_data.get("awaiting_note", set())
    if user_id not in awaiting:
        # Bukan sedang menunggu catatan "Lain-lain" - ini mesej bebas/chit-chat.
        # Bot ini bukan chatbot umum; arahkan ahli (bukan admin) hubungi admin terus.
        if not is_admin(user_id):
            contact = await get_admin_contact_text(context)
            await update.effective_message.reply_text(
                f"Bot ini hanya untuk urusan kehadiran (pilih status & kongsi lokasi). "
                f"Untuk sebarang pertanyaan lain, sila hubungi {contact} terus."
            )
        return

    note = update.effective_message.text.strip()
    context.bot_data.setdefault("pending", {}).setdefault(user_id, {"status": "lain_lain", "note": None})
    context.bot_data["pending"][user_id]["note"] = note
    awaiting.discard(user_id)

    await update.effective_message.reply_text(
        "Catatan direkodkan. Sila kongsi lokasi anda sekarang:",
        reply_markup=LOCATION_BUTTON,
    )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    location = message.location
    if location is None:
        return

    user_id = update.effective_user.id
    pending = context.bot_data.get("pending", {}).get(user_id)
    if not pending:
        await message.reply_text(
            "Sila pilih status kehadiran dahulu (tunggu bot hantar pilihan pada jam mula sesi, "
            "atau minta admin jalankan /mula).",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if not context.bot_data.get("attendance_open"):
        await message.reply_text(
            "Sesi kehadiran hari ini sudah tamat/dimuktamadkan.",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.bot_data.get("pending", {}).pop(user_id, None)
        return

    lat, lng = location.latitude, location.longitude
    local_time = message.date.astimezone(TZ)
    place_name = reverse_geocode(lat, lng)

    member = get_member(user_id)
    full_name = member["full_name"] if member else (update.effective_user.full_name or "Tidak diketahui")

    date_str = context.bot_data.get("session_date", local_time.date().isoformat())
    record_attendance(
        date_str=date_str,
        user_id=user_id,
        full_name=full_name,
        status=pending["status"],
        note=pending.get("note"),
        lat=lat,
        lng=lng,
        place_name=place_name,
        timestamp_local=local_time.isoformat(),
    )
    context.bot_data.get("pending", {}).pop(user_id, None)

    label = STATUS_LABELS.get(pending["status"], pending["status"])
    await message.reply_text(
        f"✅ Kehadiran direkodkan!\n\n"
        f"Status: {label}\n"
        + (f"Catatan: {pending['note']}\n" if pending.get("note") else "")
        + f"Masa: {local_time.strftime('%H:%M:%S')} ({TIMEZONE_NAME})\n"
        f"Lokasi: {place_name}",
        reply_markup=ReplyKeyboardRemove(),
    )

    await update_board(context)
    await maybe_early_finish(context)


async def handle_unknown_private_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fallback untuk sebarang /command yang tak dikenali dalam DM peribadi.
    Diletakkan SELEPAS semua CommandHandler khusus semasa didaftarkan, jadi ia
    hanya tercetus bila command tu bukan salah satu daripada senarai rasmi bot."""
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.effective_message.reply_text("Command tidak dikenali.")
        return
    contact = await get_admin_contact_text(context)
    await update.effective_message.reply_text(
        f"Command tidak dikenali. Bot ini hanya untuk urusan kehadiran (/daftar, pilih status, "
        f"kongsi lokasi). Untuk sebarang pertanyaan lain, sila hubungi {contact} terus."
    )


# ----------------------------------------------------------------------------
# Job berjadual harian: buka sesi kehadiran (hari bekerja sahaja)
# ----------------------------------------------------------------------------

def parse_single_time(raw: str) -> dtime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        hour, minute = raw.split(":")
        return dtime(hour=int(hour), minute=int(minute), tzinfo=TZ)
    except ValueError:
        logger.warning("Format masa tidak sah: %r", raw)
        return None


async def daily_trigger_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    today = datetime.now(TZ).date()
    if not is_working_day(today):
        logger.info("%s bukan hari bekerja - sesi kehadiran dilangkau.", today)
        return
    if not GROUP_CHAT_ID:
        logger.warning("GROUP_CHAT_ID belum ditetapkan - sesi kehadiran dilangkau.")
        return
    await start_attendance_session(context, GROUP_CHAT_ID)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN belum ditetapkan. Salin .env.example ke .env dan isikan token daripada BotFather."
        )

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.bot_data["attendance_open"] = False
    application.bot_data["pending"] = {}
    application.bot_data["awaiting_note"] = set()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("daftar", cmd_daftar))
    application.add_handler(CommandHandler("senarai", cmd_senarai))
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("adminid", cmd_adminid))
    application.add_handler(CommandHandler("jadual", cmd_jadual))
    application.add_handler(CommandHandler("mula", cmd_mula))
    application.add_handler(CommandHandler("laporan", cmd_laporan))
    application.add_handler(CommandHandler("resetkehadiran", cmd_reset))

    # Fallback (MESTI selepas semua CommandHandler khusus di atas) - command
    # tak dikenali dalam DM peribadi diarahkan hubungi admin, bukan dilayan bot.
    application.add_handler(MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, handle_unknown_private_command))

    application.add_handler(CallbackQueryHandler(handle_status_click))
    application.add_handler(MessageHandler(filters.LOCATION & filters.ChatType.PRIVATE, handle_location))
    application.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND, handle_note_text)
    )

    checkin_time = parse_single_time(CHECKIN_TIME_RAW)
    if checkin_time is not None:
        application.job_queue.run_daily(daily_trigger_job, time=checkin_time)
        logger.info("Trigger harian didaftarkan pada %s (%s, hari bekerja sahaja)", CHECKIN_TIME_RAW, TIMEZONE_NAME)
    else:
        logger.warning("CHECKIN_TIME tidak sah/kosong - trigger automatik harian dimatikan (guna /mula secara manual).")

    logger.info("Bot bermula (polling)...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
