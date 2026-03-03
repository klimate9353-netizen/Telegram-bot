import os
import re
import json
import shutil
import zipfile
import logging
import tempfile
import subprocess
import asyncio
import ssl
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import certifi
except Exception:
    certifi = None

from typing import Dict, List, Optional
from urllib.parse import urlparse

try:
    import asyncpg
except Exception:
    asyncpg = None

from dotenv import load_dotenv
from PIL import Image
import img2pdf
import pypdfium2 as pdfium

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_SSL = (os.getenv("DB_SSL") or "1").strip().lower()
DB_SSL_INSECURE = (os.getenv("DB_SSL_INSECURE") or "0").strip().lower()
DB_MAX_POOL_RAW = (os.getenv("DB_MAX_POOL") or "5").strip()
try:
    DB_MAX_POOL = max(1, int(DB_MAX_POOL_RAW))
except Exception:
    DB_MAX_POOL = 5

# Footer shown under each converted file
BOT_FOOTER = "🤖 @photos_converter_bot"

ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for part in ADMIN_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")

PENDING_IMAGES: Dict[int, List[str]] = {}

# Pending single-file conversions (per-user)
# Word (DOC/DOCX) -> PDF
PENDING_WORD: Dict[int, Dict[str, str]] = {}
# Image (JPG/PNG) -> other format
PENDING_IMG_CONV: Dict[int, Dict[str, str]] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("converter-bot")

# ---------------------- Database (Supabase Postgres) ----------------------
# If DATABASE_URL is set, the bot will store user ids in Postgres instead of local data/users.json.
DB_POOL: Optional["asyncpg.pool.Pool"] = None
_DB_READY = False
_DB_LOCK = asyncio.Lock()

def _db_enabled() -> bool:
    return bool(DATABASE_URL)

def _db_use_ssl() -> bool:
    # Supabase requires SSL for external connections.
    return DB_SSL not in ("0", "false", "no", "off", "disable")


def _db_insecure() -> bool:
    # If set, disables certificate verification (NOT recommended). Useful only for debugging.
    return DB_SSL_INSECURE in ("1", "true", "yes", "on")

def _is_transaction_pooler_url(dsn: str) -> bool:
    # Supabase transaction pooler commonly uses port 6543.
    return ":6543" in (dsn or "")


def _log_db_target(dsn: str) -> None:
    """Log DB host/port/user safely (without password)."""
    try:
        u = urlparse(dsn)
        user = u.username or ""
        host = u.hostname or ""
        port = u.port or 0
        dbname = (u.path or "").lstrip("/")
        if host:
            log.info("DB target: user=%s host=%s port=%s db=%s", user, host, port, dbname)
    except Exception:
        pass


def _start_health_server_if_port_set() -> None:
    """Render Web Service needs an open port. If PORT is set, bind a tiny HTTP server."""
    port_s = (os.getenv("PORT") or "").strip()
    if not port_s:
        return
    try:
        port = int(port_s)
    except Exception:
        return

    class _Handler(BaseHTTPRequestHandler):
        def _reply(self, body: bool) -> None:
            # UptimeRobot can use HEAD; some monitors also hit /healthz
            if self.path not in ("/", "/health", "/healthz"):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                if body:
                    self.wfile.write(b"not found")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if body:
                self.wfile.write(b"ok")

        def do_GET(self):
            self._reply(body=True)

        def do_HEAD(self):
            self._reply(body=False)

        # silence default request logs
        def log_message(self, format, *args):
            return

    def _serve():
        try:
            httpd = HTTPServer(("0.0.0.0", port), _Handler)
            log.info("Health server listening on 0.0.0.0:%s", port)
            httpd.serve_forever()
        except Exception as e:
            log.warning("Health server failed: %s", e)

    threading.Thread(target=_serve, daemon=True).start()


async def _ensure_db() -> None:
    """Lazy-init DB pool inside the running event loop."""
    global DB_POOL, _DB_READY
    if not _db_enabled():
        return
    if asyncpg is None:
        raise RuntimeError("asyncpg paketi o‘rnatilmagan. requirements.txt ga asyncpg==0.30.0 qo‘shing.")
    async with _DB_LOCK:
        if DB_POOL is None:
            ssl_param = None
            if _db_use_ssl():
                # Build an SSL context that trusts BOTH the system CAs (if present) and certifi's bundle.
                # Some hosts inject a custom root CA; using only certifi can break verification.
                ssl_ctx = ssl.create_default_context()
                if certifi is not None:
                    try:
                        ssl_ctx.load_verify_locations(cafile=certifi.where())
                    except Exception:
                        pass
                if _db_insecure():
                    ssl_ctx.check_hostname = False
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                ssl_param = ssl_ctx

            # If using transaction pooler, prepared statements may break. Turn off statement cache in that case.
            stmt_cache = 0 if _is_transaction_pooler_url(DATABASE_URL) else 100
            _log_db_target(DATABASE_URL)
            try:
                DB_POOL = await asyncpg.create_pool(
                    dsn=DATABASE_URL,
                    min_size=1,
                    max_size=DB_MAX_POOL,
                    ssl=ssl_param,
                    command_timeout=60,
                    statement_cache_size=stmt_cache,
                )
            except ssl.SSLCertVerificationError as e:
                # Some hosts (or proxies) present a cert chain that can't be validated with the available CA store.
                # As a pragmatic fallback (still encrypted, but without verification), retry with CERT_NONE.
                log.warning(
                    "DB SSL sertifikat tekshiruvi muvaffaqiyatsiz: %s. Insecure TLS (verify o‘chirilgan) bilan qayta urinayapman. "
                    "Xavfsiz variant uchun CA muammosini hal qiling yoki DB_SSL_INSECURE=1 ni o‘zingiz boshqaring.",
                    e,
                )
                ssl_param2 = None
                if _db_use_ssl():
                    ssl_ctx2 = ssl.create_default_context()
                    if certifi is not None:
                        try:
                            ssl_ctx2.load_verify_locations(cafile=certifi.where())
                        except Exception:
                            pass
                    ssl_ctx2.check_hostname = False
                    ssl_ctx2.verify_mode = ssl.CERT_NONE
                    ssl_param2 = ssl_ctx2
            
                DB_POOL = await asyncpg.create_pool(
                    dsn=DATABASE_URL,
                    min_size=1,
                    max_size=DB_MAX_POOL,
                    ssl=ssl_param2,
                    command_timeout=60,
                    statement_cache_size=stmt_cache,
                )

        if not _DB_READY:
            async with DB_POOL.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.users (
                        user_id BIGINT PRIMARY KEY,
                        lang TEXT NOT NULL DEFAULT 'uz',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    );
                    """
                )
            _DB_READY = True


async def load_users() -> List[int]:
    """Return list of user_ids who pressed /start (from DB if enabled, else from users.json)."""
    if _db_enabled():
        await _ensure_db()
        assert DB_POOL is not None
        async with DB_POOL.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM public.users ORDER BY user_id;")
        return [int(r["user_id"]) for r in rows]
    # Fallback: local JSON
    return load_users_file()


async def add_user(user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """Upsert user into DB (or users.json fallback)."""
    if _db_enabled():
        await _ensure_db()
        assert DB_POOL is not None
        lang = None
        try:
            if context is not None:
                lang = (context.user_data or {}).get("lang")
        except Exception:
            lang = None
        if lang not in ("uz", "ru"):
            lang = None
        async with DB_POOL.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.users (user_id, lang)
                VALUES ($1, COALESCE($2, 'uz'))
                ON CONFLICT (user_id)
                DO UPDATE SET
                    lang = COALESCE(EXCLUDED.lang, public.users.lang),
                    updated_at = now();
                """,
                int(user_id),
                lang,
            )
        return

    # Fallback: local JSON
    add_user_file(user_id)


# ---------------------- Language helpers ----------------------
def get_lang(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return user's chosen language: 'uz' (default) or 'ru'."""
    try:
        lang = (context.user_data or {}).get("lang", "uz")
        return "ru" if lang == "ru" else "uz"
    except Exception:
        return "uz"


def tr(context: ContextTypes.DEFAULT_TYPE, uz: str, ru: str) -> str:
    """Translate by context language."""
    return ru if get_lang(context) == "ru" else uz


def tr_lang(lang: str, uz: str, ru: str) -> str:
    """Translate by explicit language code."""
    return ru if (lang or "uz") == "ru" else uz


def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("O'zbekcha", callback_data="lang:uz"),
         InlineKeyboardButton("Русский", callback_data="lang:ru")]
    ])


def _greeting_uz() -> str:
    return (
        '👋🏻 <b>Salom</b>\n'
        'Rasmlarni <b>PDF</b> qiluvchi yoki <b>PDF</b> ni rasmga hamda <b>Word.doc</b> dokumentni <b>PDF</b> ga aylantiruvchu '
        '<b>@photos_converter_bot</b> ga xush kelibsiz.\n\n'
        '✅ <b>Botning imkoniyatlari:</b>\n'
        '🔁 <b>JPG</b> yoki <b>PNG</b> rasmlarni <b>PDF</b> qiladi yoki aksincha '
        '<b>PDF</b> ni <b>JPG</b> yoki <b>PNG</b> rasm qilib beradi;\n\n'
        '🔁 <b>PDF</b> dan rasmga aylantirishda <b>rasm tiniqligini oshirish</b> '
        'imkoniyati bor — <b>150 DPI</b> / <b>300 DPI</b> va boshqa\n\n'
        '🔁 <b>Word.doc</b> yoki <b>Word.docx</b> dokumentlarni <b>PDF</b> ga aylantiradi\n\n'
        'ℹ️ Biror bir xatolikga duch kelsangiz bizni botlar kanaliga o’ting va '
        'u yerdagi adminlarga habar bering.\n'
        'Bizning foydali botlar kanali 👉 '
        '<b>https://t.me/+skp5TgimYIJjYzIy</b>\n\n'
        '🔗 <b>BOSHLASH UCHUN</b> MENGA <b>JPG</b>, <b>PNG</b> rasm yoki <b>PDF</b> fayl '
        'YUBORING ⤵️'
    )


def _greeting_ru() -> str:
    return (
        '👋🏻 <b>Привет</b>\n'
        'Добро пожаловать в <b>@photos_converter_bot</b> — бот, который конвертирует изображения и файлы без лишних шагов.\n\n'
        '✅ <b>Возможности бота:</b>\n'
        '🔁 Конвертация <b>JPG</b>/<b>PNG</b> → <b>PDF</b> и наоборот <b>PDF</b> → <b>JPG</b>/<b>PNG</b>;\n\n'
        '🔁 При конвертации <b>PDF</b> → изображение можно повысить качество: <b>150 DPI</b> / <b>300 DPI</b> и т.д.\n\n'
        '🔁 Конвертация <b>Word.doc</b>/<b>Word.docx</b> → <b>PDF</b>\n\n'
        'ℹ️ Если возникнут ошибки — напишите админам в нашем канале полезных ботов:\n'
        '<b>https://t.me/+skp5TgimYIJjYzIy</b>\n\n'
        '🔗 <b>ДЛЯ НАЧАЛА</b> отправьте мне <b>JPG</b>, <b>PNG</b>, <b>PDF</b> или <b>DOC/DOCX</b> ⤵️'
    )


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_users_file() -> List[int]:
    ensure_data_dir()
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [int(x) for x in data if str(x).isdigit()]
    except Exception:
        log.exception("Failed to load users.json")
    return []


def save_users_file(users: List[int]) -> None:
    ensure_data_dir()
    users = sorted(set(int(x) for x in users if str(x).isdigit()))
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)


def add_user_file(user_id: int) -> None:
    users = load_users_file()
    if user_id not in users:
        users.append(user_id)
        save_users_file(users)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def kb_pdf_to_images(lang: str = "uz") -> InlineKeyboardMarkup:
    # Buttons are self-explanatory; keep labels identical in both languages.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("PDF → JPG (150 DPI)", callback_data="pdf2jpg:150"),
         InlineKeyboardButton("PDF → PNG (150 DPI)", callback_data="pdf2png:150")],
        [InlineKeyboardButton("PDF → JPG (300 DPI)", callback_data="pdf2jpg:300"),
         InlineKeyboardButton("PDF → PNG (300 DPI)", callback_data="pdf2png:300")],
    ])

def kb_finish_images_to_pdf(lang: str = "uz") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr_lang(lang, "✅ Finish PDF", "✅ Завершить PDF"), callback_data="finish_img2pdf")],
        [InlineKeyboardButton(tr_lang(lang, "🗑 Clear images", "🗑 Очистить изображения"), callback_data="clear_img2pdf")]
    ])

def kb_image_actions(ext: str, lang: str = "uz") -> InlineKeyboardMarkup:
    """When a user sends a JPG/PNG image (photo or image-document), offer exactly two actions:
    1) Convert format (JPG<->PNG)
    2) Convert accumulated images to PDF (reuses existing Finish PDF flow)
    """
    ext = (ext or "jpg").lower().lstrip(".")
    if ext == "png":
        btn1 = InlineKeyboardButton("🖼 PNG → JPG", callback_data="lastimg:jpg")
        btn2 = InlineKeyboardButton("📄 PNG → PDF", callback_data="finish_img2pdf")
    else:
        # default to JPG
        btn1 = InlineKeyboardButton("🖼 JPG → PNG", callback_data="lastimg:png")
        btn2 = InlineKeyboardButton("📄 JPG → PDF", callback_data="finish_img2pdf")
    return InlineKeyboardMarkup([[btn1, btn2]])

def kb_word_to_pdf(lang: str = "uz") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tr_lang(lang, "📄 PDF qilish", "📄 В PDF"), callback_data="word2pdf")]
    ])

def kb_image_convert(src_ext: str, lang: str = "uz") -> InlineKeyboardMarkup:
    src_ext = (src_ext or "").lower().lstrip(".")
    if src_ext in ("jpg", "jpeg"):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 JPG → PNG", callback_data="imgconv:png")]
        ])
    if src_ext == "png":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🖼 PNG → JPG", callback_data="imgconv:jpg")]
        ])
    # fallback
    return InlineKeyboardMarkup([])

def _which(cmd: str) -> str | None:
    try:
        import shutil as _sh
        return _sh.which(cmd)
    except Exception:
        return None


def find_soffice() -> List[str] | None:
    """Return command list to run LibreOffice headless conversion, or None if not found."""
    # 1) PATH: linux/mac/windows
    p = _which("soffice") or _which("libreoffice") or _which("soffice.exe")
    if p:
        return [p]

    # 2) Common Linux install paths
    candidates = [
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/usr/lib/libreoffice/program/soffice",
        "/usr/lib/libreoffice/program/soffice.bin",
    ]
    for c in candidates:
        if os.path.exists(c):
            return [c]

    # 3) Common Windows paths
    win_candidates = [
        r"C:\\Program Files\\LibreOffice\\program\\soffice.exe",
        r"C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe",
    ]
    for c in win_candidates:
        if os.path.exists(c):
            return [c]

    return None

    # 3) Common Windows paths
    win_candidates = [
        r"C:\\Program Files\\LibreOffice\\program\\soffice.exe",
        r"C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe",
    ]
    for c in win_candidates:
        if os.path.exists(c):
            return [c]

    return None
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await add_user(user.id, context)

    # Ask for language on every /start (user can reselect anytime)
    await update.message.reply_text(
        "Tilni tanlang / Выбрать язык",
        reply_markup=kb_language()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        lang = get_lang(context)

        uz = (
            "Buyruqlar:\n"
            "/start - botni boshlash\n"
            "/help - yordam\n\n"
            "Admin:\n"
            "/broadcast_post <matn> - start bosganlarning barchasiga xabar yuborish"
        )
        ru = (
            "Команды:\n"
            "/start - начать\n"
            "/help - помощь\n\n"
            "Админ:\n"
            "/broadcast_post <текст> - отправить сообщение всем, кто нажал start"
        )

        await update.message.reply_text(ru if lang == "ru" else uz)

async def cmd_broadcast_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    lang = get_lang(context)

    if not is_admin(user.id):
        await update.message.reply_text("⛔ Эта команда только для админов." if lang == "ru" else "⛔ Bu buyruq faqat adminlar uchun.")
        return

    text = update.message.text or ""
    m = re.match(r"^/broadcast_post(?:@\w+)?\s+(.+)$", text, flags=re.S)
    if not m:
        await update.message.reply_text("Использование: /broadcast_post Ваше сообщение" if lang == "ru" else "Foydalanish: /broadcast_post Xabaringiz")
        return

    msg = m.group(1).strip()
    users = await load_users()
    if not users:
        await update.message.reply_text("Пока нет пользователей, которые нажали /start." if lang == "ru" else "Hali start bosgan foydalanuvchilar yo‘q.")
        return

    await update.message.reply_text((f"Отправляю: {len(users)} пользователям..." if lang == "ru" else f"Yuborilyapti: {len(users)} ta foydalanuvchiga..."))

    sent = 0
    failed = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        (f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}" if lang == "ru" else f"✅ Yuborildi: {sent}\n❌ Xato: {failed}")
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    lang = get_lang(context)

    if not is_admin(user.id):
        await msg.reply_text("⛔ Эта команда только для админов." if lang == "ru" else "⛔ Bu buyruq faqat adminlar uchun.")
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Использование: /broadcast текст" if lang == "ru" else "Foydalanish: /broadcast faqat tekst xabar")
        return
    text_msg = parts[1]
    users = await load_users()
    if not users:
        await msg.reply_text("Пока нет пользователей, которые нажали /start." if lang == "ru" else "Hali start bosgan foydalanuvchilar yo‘q.")
        return
    await msg.reply_text((f"Отправляю: {len(users)} пользователям..." if lang == "ru" else f"Yuborilyapti: {len(users)} ta foydalanuvchiga..."))
    sent = failed = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text_msg)
            sent += 1
        except Exception:
            failed += 1
    await msg.reply_text((f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}" if lang == "ru" else f"✅ Yuborildi: {sent}\n❌ Xato: {failed}"))


async def cmd_broadcastpost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    lang = get_lang(context)

    if not is_admin(user.id):
        await msg.reply_text("⛔ Эта команда только для админов." if lang == "ru" else "⛔ Bu buyruq faqat adminlar uchun.")
        return
    if not msg.reply_to_message:
        await msg.reply_text("Использование: ответьте на пост и отправьте /broadcastpost." if lang == "ru" else "Foydalanish: postga reply qilib /broadcastpost yozing.")
        return
    users = await load_users()
    if not users:
        await msg.reply_text("Пока нет пользователей, которые нажали /start." if lang == "ru" else "Hali start bosgan foydalanuvchilar yo‘q.")
        return
    await msg.reply_text((f"Отправляю пост: {len(users)} пользователям..." if lang == "ru" else f"Post yuborilyapti: {len(users)} ta foydalanuvchiga..."))
    sent = failed = 0
    for uid in users:
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=msg.chat_id,
                message_id=msg.reply_to_message.message_id
            )
            sent += 1
        except Exception:
            failed += 1
    await msg.reply_text((f"✅ Отправлено: {sent}\n❌ Ошибок: {failed}" if lang == "ru" else f"✅ Yuborildi: {sent}\n❌ Xato: {failed}"))

async def on_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg or not msg.document:
        return

    await add_user(user.id, context)
    lang = get_lang(context)

    doc = msg.document
    if not (doc.file_name or "").lower().endswith(".pdf"):
        return

    context.user_data["pending_pdf_file_id"] = doc.file_id
    context.user_data["pending_pdf_name"] = doc.file_name or "document.pdf"

    await msg.reply_text(
        "PDF получен. В какой формат конвертировать?" if lang == "ru" else "PDF qabul qilindi. Qaysi formatga o‘tkazamiz?",
        reply_markup=kb_pdf_to_images(lang)
    )

async def on_word(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Accept DOC/DOCX and offer Word -> PDF conversion.
    user = update.effective_user
    msg = update.message
    if not user or not msg or not msg.document:
        return

    await add_user(user.id, context)
    lang = get_lang(context)

    doc = msg.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".doc") or fname.endswith(".docx")):
        return

    PENDING_WORD[user.id] = {
        "file_id": doc.file_id,
        "name": doc.file_name or "document.docx",
    }

    await msg.reply_text(
        "📄 Файл Word получен. Конвертировать в PDF?" if lang == "ru" else "📄 Word fayl qabul qilindi. PDF ga o‘tkazamizmi?",
        reply_markup=kb_word_to_pdf(lang)
    )

async def on_image_doc_convert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle JPG/PNG sent as document ("Отправить как файл").

    We route to the unified `on_image()` flow so the user always gets TWO actions:
      1) Convert format (JPG <-> PNG)
      2) Convert to PDF (image(s) -> PDF)
    """
    await on_image(update, context)

async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    await add_user(user.id, context)
    lang = get_lang(context)

    src_ext = "jpg"
    file_id = None
    is_compressed_photo = False

    if msg.photo:
        is_compressed_photo = True
        file_id = msg.photo[-1].file_id
        src_ext = "jpg"  # Telegram photo odatda JPEG bo‘ladi
    elif msg.document and (((msg.document.mime_type or "").startswith("image/")) or ((msg.document.file_name or "").lower().endswith((".png", ".jpg", ".jpeg")))):
        file_id = msg.document.file_id

        # PNG/JPG ni mime_type orqali ham aniqlaymiz (file_name bo‘lmasligi mumkin)
        mime = (msg.document.mime_type or "").lower()
        fname = (msg.document.file_name or "").lower()

        if ("image/png" in mime) or fname.endswith(".png"):
            src_ext = "png"
        elif ("image/jpeg" in mime) or ("image/jpg" in mime) or fname.endswith(".jpeg") or fname.endswith(".jpg"):
            src_ext = "jpg"
        else:
            src_ext = "jpg"
    else:
        return

    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    tmpdir = context.user_data.get("img2pdf_tmpdir")
    if not tmpdir or not os.path.isdir(tmpdir):
        tmpdir = tempfile.mkdtemp(prefix=f"img2pdf_{user.id}_")
        context.user_data["img2pdf_tmpdir"] = tmpdir

    tfile = await context.bot.get_file(file_id)
    idx = len(PENDING_IMAGES.get(user.id, [])) + 1

    # 1) Save original (for format conversion button)
    orig_path = os.path.join(tmpdir, f"img_{idx}.{src_ext}")
    await tfile.download_to_drive(custom_path=orig_path)
    context.user_data["last_img_path"] = orig_path
    context.user_data["last_img_ext"] = src_ext

    # 2) Prepare PDF-ready JPEG (for multi-image PDF flow)
    pdf_path = orig_path
    if src_ext != "jpg":
        pdf_path = os.path.join(tmpdir, f"img_{idx}.jpg")
        try:
            im = Image.open(orig_path)
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(pdf_path, format="JPEG", quality=95)
        except Exception:
            # fallback: if conversion fails, still try to use original
            pdf_path = orig_path
    else:
        # normalize JPEG quality a bit for PDFs, but keep file as JPEG
        try:
            im = Image.open(orig_path)
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            im.save(orig_path, format="JPEG", quality=95)
        except Exception:
            pass

    PENDING_IMAGES.setdefault(user.id, []).append(pdf_path)

    txt = (
        f"✅ Изображений получено: {len(PENDING_IMAGES[user.id])}.\nВыберите действие:"
        if lang == "ru"
        else f"✅ Rasm qabul qilindi: {len(PENDING_IMAGES[user.id])} ta.\nKerakli amalni tanlang:"
    )

    # Only for compressed photos ("Фото/Сжат"): Telegram may convert PNG to JPG.
    if is_compressed_photo:
        txt += (
            "\n\nℹ️ Eslatma: rasmni oddiy “Фото/Сжат” qilib yuborganda Telegram ko‘pincha uni JPG ga aylantiradi. "
            "PNG asl holatda qolishi uchun rasmni “Отправить как файл” qilib yuboring."
            "\nℹ️ Примечание: если отправить изображение как обычное «Фото/Сжат», Telegram часто конвертирует его в JPG. "
            "Чтобы PNG сохранился в исходном виде, отправьте «Отправить как файл»."
        )

    await msg.reply_text(txt, reply_markup=kb_image_actions(src_ext, lang))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    user = update.effective_user
    if not user:
        return

    data = query.data

    # Language selection
    if data.startswith("lang:"):
        lang = data.split(":", 1)[1].strip().lower()
        if lang not in ("uz", "ru"):
            lang = "uz"
        context.user_data["lang"] = lang
        await add_user(user.id, context)

        greeting = _greeting_ru() if lang == "ru" else _greeting_uz()
        try:
            await query.message.edit_text(
                greeting,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception:
            # fallback: send as a new message
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=greeting,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        return

    if data.startswith("pdf2jpg:") or data.startswith("pdf2png:"):
        await handle_pdf_to_images(query, context, user.id, data)
        return

    if data == "finish_img2pdf":
        await handle_images_to_pdf(query, context, user.id)
        return

    if data == "clear_img2pdf":
        await handle_clear_images(query, context, user.id)
        return

    if data == "word2pdf":
        await handle_word_to_pdf(query, context, user.id)
        return

    if data.startswith("imgconv:"):
        await handle_image_convert(query, context, user.id, data)
        return

    if data.startswith("lastimg:"):
        await handle_last_image_convert(query, context, user.id, data)
        return

async def handle_pdf_to_images(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str) -> None:
    kind, dpi_str = data.split(":")
    dpi = int(dpi_str)

    file_id = context.user_data.get("pending_pdf_file_id")
    pdf_name = context.user_data.get("pending_pdf_name", "document.pdf")
    if not file_id:
        await query.edit_message_text(tr(context, "⚠️ PDF topilmadi. Qaytadan PDF yuboring.", "⚠️ PDF не найден. Отправьте PDF заново."))
        return

    fmt = "jpg" if kind == "pdf2jpg" else "png"
    await query.edit_message_text((f"⏳ Конвертация: PDF → {fmt.upper()} ({dpi} DPI) ..." if get_lang(context) == "ru" else f"⏳ Konvert qilinyapti: PDF → {fmt.upper()} ({dpi} DPI) ..."))

    tmpdir = tempfile.mkdtemp(prefix=f"pdf2img_{user_id}_")
    try:
        tfile = await context.bot.get_file(file_id)
        local_pdf = os.path.join(tmpdir, "input.pdf")
        await tfile.download_to_drive(custom_path=local_pdf)

        pdf = pdfium.PdfDocument(local_pdf)
        out_paths: List[str] = []
        scale = dpi / 72.0

        for i in range(len(pdf)):
            page = pdf[i]
            pil_image = page.render(scale=scale).to_pil()
            out_path = os.path.join(tmpdir, f"page_{i+1:03d}.{fmt}")

            if fmt == "jpg":
                if pil_image.mode in ("RGBA", "P"):
                    pil_image = pil_image.convert("RGB")
                pil_image.save(out_path, format="JPEG", quality=92)
            else:
                pil_image.save(out_path, format="PNG")

            out_paths.append(out_path)

        if not out_paths:
            await query.edit_message_text(tr(context, "⚠️ PDF sahifalari topilmadi.", "⚠️ Страницы в PDF не найдены."))
            return

        base_title = os.path.splitext(os.path.basename(pdf_name))[0][:40] or "pdf"
        if len(out_paths) <= 3:
            await query.edit_message_text((f"✅ Готово. Отправляю страниц: {len(out_paths)}..." if get_lang(context) == "ru" else f"✅ Tayyor. {len(out_paths)} ta sahifa yuborilyapti..."))
            for p in out_paths:
                with open(p, "rb") as f:
                    await context.bot.send_document(chat_id=query.message.chat_id, document=f, filename=os.path.basename(p), caption=BOT_FOOTER)
            await context.bot.send_message(chat_id=query.message.chat_id, text=("✅ Готово." if get_lang(context) == "ru" else ("✅ Готово." if get_lang(context) == "ru" else "✅ Yakunlandi.")))
        else:
            zip_path = os.path.join(tmpdir, f"{base_title}_{fmt.upper()}_{dpi}DPI.zip")
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for p in out_paths:
                    z.write(p, arcname=os.path.basename(p))
            await query.edit_message_text((f"✅ ZIP готов. Страниц: {len(out_paths)}." if get_lang(context) == "ru" else f"✅ ZIP tayyor. {len(out_paths)} ta sahifa."))
            with open(zip_path, "rb") as f:
                await context.bot.send_document(chat_id=query.message.chat_id, document=f, filename=os.path.basename(zip_path), caption=BOT_FOOTER)
            await context.bot.send_message(chat_id=query.message.chat_id, text=("✅ Готово." if get_lang(context) == "ru" else ("✅ Готово." if get_lang(context) == "ru" else "✅ Yakunlandi.")))
    except Exception:
        logging.exception("PDF->Image failed")
        await query.edit_message_text(tr(context, "❌ Xatolik. PDF ni qaytadan yuborib ko‘ring.", "❌ Ошибка. Попробуйте отправить PDF заново."))
    finally:
        context.user_data.pop("pending_pdf_file_id", None)
        context.user_data.pop("pending_pdf_name", None)
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_word_to_pdf(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    pending = PENDING_WORD.get(user_id)
    if not pending:
        await query.edit_message_text(tr(context, "⚠️ Word fayl topilmadi. Qaytadan DOC/DOCX yuboring.", "⚠️ Файл Word не найден. Отправьте DOC/DOCX заново."))
        return

    soffice_cmd = find_soffice()
    if not soffice_cmd:
        await query.edit_message_text(
            tr(context, "❌ LibreOffice topilmadi (soffice/libreoffice). Render build’da LibreOffice o‘rnatilganini tekshiring.", "❌ LibreOffice не найден (soffice/libreoffice). Проверьте, что LibreOffice установлен на сервере.")
        )
        return

    file_id = pending["file_id"]
    fname = pending.get("name", "document.docx")
    base_title = os.path.splitext(os.path.basename(fname))[0][:50] or "document"

    await query.edit_message_text(tr(context, "⏳ Konvert qilinyapti: Word → PDF ...", "⏳ Конвертация: Word → PDF ..."))

    tmpdir = tempfile.mkdtemp(prefix=f"word2pdf_{user_id}_")
    try:
        tfile = await context.bot.get_file(file_id)
        in_path = os.path.join(tmpdir, os.path.basename(fname))
        await tfile.download_to_drive(custom_path=in_path)

        # LibreOffice headless conversion
        # soffice --headless --nologo --nofirststartwizard --convert-to pdf --outdir <tmpdir> <in_path>
        # Use a unique LibreOffice profile per conversion to avoid "UserInstallation is locked"
        profile_dir = os.path.join(tmpdir, f"lo_profile_{uuid.uuid4().hex}")
        os.makedirs(profile_dir, exist_ok=True)

        cmd = list(soffice_cmd) + [
            "--headless",
            "--nologo",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            "pdf",
            "--outdir",
            tmpdir,
            in_path,
        ]

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )

        # LibreOffice returns 0 on success; still verify output exists
        out_pdf = os.path.join(tmpdir, f"{os.path.splitext(os.path.basename(in_path))[0]}.pdf")
        if proc.returncode != 0 or not os.path.exists(out_pdf):
            log.error("Word->PDF failed. rc=%s stdout=%s stderr=%s", proc.returncode, proc.stdout, proc.stderr)
            await query.edit_message_text(tr(context, "❌ Xatolik. Word faylni PDF ga o‘tkazib bo‘lmadi.", "❌ Ошибка. Не удалось конвертировать Word в PDF."))
            return

        await query.edit_message_text(tr(context, "✅ PDF tayyor. Yuborilyapti...", "✅ PDF готов. Отправляю..."))
        with open(out_pdf, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=f"{base_title}.pdf",
            caption=BOT_FOOTER,
            )
        await context.bot.send_message(chat_id=query.message.chat_id, text=("✅ Готово." if get_lang(context) == "ru" else ("✅ Готово." if get_lang(context) == "ru" else "✅ Yakunlandi.")))
    except subprocess.TimeoutExpired:
        await query.edit_message_text(tr(context, "❌ Timeout. Word fayl juda katta bo‘lishi mumkin.", "❌ Таймаут. Возможно, файл Word слишком большой."))
    except Exception:
        log.exception("Word->PDF failed")
        await query.edit_message_text(tr(context, "❌ Xatolik. Qaytadan urinib ko‘ring.", "❌ Ошибка. Попробуйте ещё раз."))
    finally:
        PENDING_WORD.pop(user_id, None)
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_image_convert(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str) -> None:
    pending = PENDING_IMG_CONV.get(user_id)
    if not pending:
        await query.edit_message_text(tr(context, "⚠️ Rasm topilmadi. Qaytadan JPG/PNG yuboring.", "⚠️ Изображение не найдено. Отправьте JPG/PNG заново."))
        return

    target = data.split(":", 1)[1].strip().lower()
    if target not in ("jpg", "png"):
        await query.edit_message_text(tr(context, "⚠️ Noto‘g‘ri format tanlandi.", "⚠️ Неверный формат."))
        return

    src_ext = (pending.get("ext") or "").lower()
    if src_ext == "jpeg":
        src_ext = "jpg"
    if src_ext == target:
        await query.edit_message_text(tr(context, "⚠️ Bu rasm allaqachon shu formatda.", "⚠️ Это изображение уже в этом формате."))
        return

    await query.edit_message_text((f"⏳ Конвертация: {src_ext.upper()} → {target.upper()} ..." if get_lang(context) == "ru" else f"⏳ Konvert qilinyapti: {src_ext.upper()} → {target.upper()} ..."))

    tmpdir = tempfile.mkdtemp(prefix=f"imgconv_{user_id}_")
    try:
        tfile = await context.bot.get_file(pending["file_id"])
        in_path = os.path.join(tmpdir, pending.get("name", f"image.{src_ext}"))
        await tfile.download_to_drive(custom_path=in_path)

        im = Image.open(in_path)

        out_name = f"{os.path.splitext(os.path.basename(in_path))[0]}.{target}"
        out_path = os.path.join(tmpdir, out_name)

        if target == "jpg":
            # Handle alpha by compositing onto white
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(out_path, format="JPEG", quality=92)
        else:
            # PNG
            im.save(out_path, format="PNG")

        await query.edit_message_text(tr(context, "✅ Tayyor. Yuborilyapti...", "✅ Готово. Отправляю..."))
        with open(out_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=out_name,
            caption=BOT_FOOTER,
            )
        await context.bot.send_message(chat_id=query.message.chat_id, text=("✅ Готово." if get_lang(context) == "ru" else ("✅ Готово." if get_lang(context) == "ru" else "✅ Yakunlandi.")))
    except Exception:
        log.exception("Image convert failed")
        await query.edit_message_text(tr(context, "❌ Xatolik. Rasmni o‘zgartirib bo‘lmadi.", "❌ Ошибка. Не удалось конвертировать изображение."))
    finally:
        PENDING_IMG_CONV.pop(user_id, None)
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_last_image_convert(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str) -> None:
    """Convert the *last received* image (sent as photo or image-document) between JPG and PNG.

    This is intentionally separate from handle_image_convert() (which handles images sent as document)
    to avoid touching existing flows.
    """
    target = data.split(":", 1)[1].strip().lower()
    if target not in ("jpg", "png"):
        await query.edit_message_text(tr(context, "⚠️ Noto‘g‘ri format tanlandi.", "⚠️ Неверный формат."))
        return

    in_path = context.user_data.get("last_img_path")
    src_ext = (context.user_data.get("last_img_ext") or "jpg").lower()
    if not in_path or not os.path.exists(in_path):
        await query.edit_message_text(tr(context, "⚠️ Rasm topilmadi. Qaytadan rasm yuboring.", "⚠️ Изображение не найдено. Отправьте изображение заново."))
        return

    if src_ext == "jpeg":
        src_ext = "jpg"
    if src_ext == target:
        await query.edit_message_text(tr(context, "⚠️ Bu rasm allaqachon shu formatda.", "⚠️ Это изображение уже в этом формате."))
        return

    await query.edit_message_text((f"⏳ Конвертация: {src_ext.upper()} → {target.upper()} ..." if get_lang(context) == "ru" else f"⏳ Konvert qilinyapti: {src_ext.upper()} → {target.upper()} ..."))

    tmpdir = tempfile.mkdtemp(prefix=f"lastimg_{user_id}_")
    try:
        im = Image.open(in_path)
        out_name = f"converted.{target}"
        out_path = os.path.join(tmpdir, out_name)

        if target == "jpg":
            # Handle alpha by compositing onto white
            if im.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.save(out_path, format="JPEG", quality=92)
        else:
            # PNG
            im.save(out_path, format="PNG")

        await query.edit_message_text(tr(context, "✅ Tayyor. Yuborilyapti...", "✅ Готово. Отправляю..."))
        with open(out_path, "rb") as f:
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=f,
                filename=out_name,
            caption=BOT_FOOTER,
            )
        await context.bot.send_message(chat_id=query.message.chat_id, text=("✅ Готово." if get_lang(context) == "ru" else ("✅ Готово." if get_lang(context) == "ru" else "✅ Yakunlandi.")))
    except Exception:
        log.exception("Last image convert failed")
        await query.edit_message_text(tr(context, "❌ Xatolik. Rasmni o‘zgartirib bo‘lmadi.", "❌ Ошибка. Не удалось конвертировать изображение."))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_images_to_pdf(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    paths = PENDING_IMAGES.get(user_id, [])
    if not paths:
        await query.edit_message_text(tr(context, "⚠️ Rasmlar yo‘q. Avval rasm yuboring.", "⚠️ Нет изображений. Сначала отправьте изображения."))
        return

    await query.edit_message_text((f"⏳ Создаю PDF ({len(paths)} изображ.) ..." if get_lang(context) == "ru" else f"⏳ PDF tayyorlanyapti ({len(paths)} ta rasm) ..."))

    tmpdir = context.user_data.get("img2pdf_tmpdir")
    if not tmpdir or not os.path.isdir(tmpdir):
        tmpdir = tempfile.mkdtemp(prefix=f"img2pdf_{user_id}_")
        context.user_data["img2pdf_tmpdir"] = tmpdir

    out_pdf = os.path.join(tmpdir, "images.pdf")
    try:
        with open(out_pdf, "wb") as f:
            f.write(img2pdf.convert(list(paths)))

        with open(out_pdf, "rb") as f:
            await context.bot.send_document(chat_id=query.message.chat_id, document=f, filename="images.pdf", caption=BOT_FOOTER)
        await context.bot.send_message(chat_id=query.message.chat_id, text=tr(context, "✅ PDF tayyor.", "✅ PDF готов."))
    except Exception:
        logging.exception("Image->PDF failed")
        await query.edit_message_text(tr(context, "❌ Xatolik. Rasmlarni qayta yuborib ko‘ring.", "❌ Ошибка. Попробуйте отправить изображения заново."))
    finally:
        PENDING_IMAGES.pop(user_id, None)
        try:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        context.user_data.pop("img2pdf_tmpdir", None)


async def handle_clear_images(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    PENDING_IMAGES.pop(user_id, None)

    tmpdir = context.user_data.get("img2pdf_tmpdir")
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    context.user_data.pop("img2pdf_tmpdir", None)

    await query.edit_message_text(tr(context, "🗑 Rasmlar tozalandi. Yangi rasm yuborishingiz mumkin.", "🗑 Изображения очищены. Можете отправлять новые."))


async def on_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(("Отправьте файл: PDF или изображение (JPG/PNG). /help" if get_lang(context) == "ru" else "Fayl yuboring: PDF yoki rasm (JPG/PNG). /help"))


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set. Create .env from .env.example and set BOT_TOKEN.")

    _start_health_server_if_port_set()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("broadcastpost", cmd_broadcastpost))

    app.add_handler(MessageHandler(filters.Document.PDF, on_pdf))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("doc") | filters.Document.FileExtension("docx"),
        on_word
    ))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("jpg")
        | filters.Document.FileExtension("jpeg")
        | filters.Document.FileExtension("png"),
        on_image_doc_convert
    ))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, on_image))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL, on_unknown))

    log.info("Bot started (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
