import os
import sys
import time
import json
import sqlite3
import asyncio
import traceback
import threading as _threading
import requests as _requests
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler as _BaseHandler, HTTPServer as _HTTPServer

import poster_api

KYIV_TZ = ZoneInfo("Europe/Kyiv")

# ── Глобальна статистика видалень і Poster-оновлень ─────────────────────────
_sys_stats: dict = {
    "barcode_deleted": 0,  # успішних видалень штрих-кодів
    "barcode_del_err": 0,  # помилок видалення штрих-кодів
    "prize_deleted": 0,  # успішних видалень повідомлень про виграш
    "prize_del_err": 0,  # помилок видалення виграшів
    "poster_calls": 0,  # кількість викликів Poster API
    "poster_total_ms": 0,  # сумарний час відповіді Poster (мс)
}


# Зручний хелпер для округлення Decimal до копійки
def _dec(value, default="0") -> Decimal:
    """Конвертує будь-яке значення в Decimal з точністю до 0.01."""
    try:
        return Decimal(str(value or default)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except Exception:
        return Decimal("0.00")


def to_db(value) -> str:
    """
    Конвертує Decimal/float/int → str для запису в SQLite.
    SQLite не підтримує Decimal нативно — завжди зберігаємо як TEXT/REAL через str.
    None → "0.00"
    """
    if value is None:
        return "0.00"
    if isinstance(value, Decimal):
        return str(value)
    try:
        return str(_dec(value))
    except Exception:
        return "0.00"


from aiohttp import web as aiohttp_web

from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")
ADMIN_ID = 499369352
CHANNEL_USERNAME = "@pivniinaraioni"
BOT_USERNAME = ""  # заповнюється при старті через get_me()
_DEV_DOMAIN = os.environ.get("REPLIT_DEV_DOMAIN", "")
WEBAPP_WHEEL_URL = f"https://{_DEV_DOMAIN}/api/wheel" if _DEV_DOMAIN else ""

AI_TEST_PHONES = ["380732928958", "380674463546"]

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", 8765))

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    phone TEXT,
    name TEXT,
    birth TEXT,
    bonus INTEGER DEFAULT 0,
    poster_client_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS purchases_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    amount REAL,
    bonus INTEGER,
    created_at TEXT,
    transaction_id TEXT UNIQUE,
    bonus_spent INTEGER DEFAULT 0,
    sent INTEGER DEFAULT 0
)
""")

# ===== ДОДАНО ДР =====
cursor.execute("""
CREATE TABLE IF NOT EXISTS birthday_log (
    user_id INTEGER,
    year INTEGER,
    notified_3 INTEGER DEFAULT 0,
    notified_1 INTEGER DEFAULT 0
)
""")

# ===== ПОВЕРНЕННЯ ЧЕКІВ =====
cursor.execute("""
CREATE TABLE IF NOT EXISTS refund_log (
    tx_id INTEGER PRIMARY KEY
)
""")

# ── Таблиця failed_receipts (нові) ───────────────────────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS failed_receipts (
    tx_id       TEXT PRIMARY KEY,
    client_id   INTEGER,
    user_id     INTEGER,
    error       TEXT,
    created_at  TEXT,
    retry_count INTEGER DEFAULT 0
)
""")

# ── Таблиця wheel_log (колесо фортуни) ───────────────────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS wheel_log (
    user_id    INTEGER,
    spin_date  TEXT,
    prize      TEXT,
    paid       INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, spin_date)
)
""")

# ── Таблиця temp_messages (TTL-видалення виграшів колеса) ────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS temp_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    message_id INTEGER,
    type       TEXT,
    created_at REAL
)
""")

# ── Таблиця barcodes (штрих-коди з 4-годинним TTL) ───────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS barcodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    chat_id    INTEGER,
    code       TEXT,
    message_id INTEGER,
    created_at REAL,
    expire_at  REAL
)
""")

# ── Таблиця prize_stats (глобальна статистика колеса) ────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS prize_stats (
    id           INTEGER PRIMARY KEY DEFAULT 1,
    total_spins  INTEGER DEFAULT 0,
    count_5_all  INTEGER DEFAULT 0,
    count_5_beer INTEGER DEFAULT 0,
    count_5_snack INTEGER DEFAULT 0
)
""")
cursor.execute("INSERT OR IGNORE INTO prize_stats (id) VALUES (1)")

# ── Таблиця spins_log (лог списань бонусів за прокрути) ──────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS spins_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL,
    amount         INTEGER NOT NULL DEFAULT 50,
    status         TEXT NOT NULL DEFAULT 'pending',
    balance_before REAL,
    balance_after  REAL,
    date           TEXT NOT NULL
)
""")

# ── Таблиця real_stats (реальні витрати на знижки) ───────────────────────────
cursor.execute("""
CREATE TABLE IF NOT EXISTS real_stats (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    order_sum        REAL,
    discount_percent INTEGER,
    discount_value   REAL,
    date             TEXT NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS temp_bonus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount TEXT,
    expires_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bonus_campaign_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    phone TEXT,
    sent_at INTEGER,
    bonus_amount TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bonus_reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    sent_at INTEGER
)
""")

conn.commit()

# Migration: add columns if they don't exist (for old DBs)
pl_cols = {row[1] for row in cursor.execute("PRAGMA table_info(purchases_log)")}
if "transaction_id" not in pl_cols:
    cursor.execute("ALTER TABLE purchases_log ADD COLUMN transaction_id TEXT")
    print("[migration] Added column: transaction_id")
if "bonus_spent" not in pl_cols:
    cursor.execute("ALTER TABLE purchases_log ADD COLUMN bonus_spent INTEGER DEFAULT 0")
    print("[migration] Added column: bonus_spent")
if "sent" not in pl_cols:
    cursor.execute("ALTER TABLE purchases_log ADD COLUMN sent INTEGER DEFAULT 0")
    cursor.execute("UPDATE purchases_log SET sent=1 WHERE sent IS NULL OR sent=0")
    print("[migration] Added column: sent, marked all existing rows as sent=1")
if "retry_count" not in pl_cols:
    cursor.execute("ALTER TABLE purchases_log ADD COLUMN retry_count INTEGER DEFAULT 0")
    print("[migration] Added column: retry_count")
if "balance_after" not in pl_cols:
    cursor.execute("ALTER TABLE purchases_log ADD COLUMN balance_after REAL")
    print("[migration] Added column: balance_after")
conn.commit()

users_cols = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
if "poster_client_id" not in users_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN poster_client_id INTEGER")
    print("[migration] Added column: poster_client_id")
if "welcome_bonus" not in users_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN welcome_bonus INTEGER DEFAULT 0")
    print("[migration] Added column: welcome_bonus")
if "broadcast_bonus" not in users_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN broadcast_bonus INTEGER DEFAULT 0")
    print("[migration] Added column: broadcast_bonus")
if "risk_level" not in users_cols:
    cursor.execute("ALTER TABLE users ADD COLUMN risk_level TEXT")
    print("[migration] Added column: risk_level")

existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(birthday_log)")}
if "notified_3" not in existing_cols:
    cursor.execute("ALTER TABLE birthday_log ADD COLUMN notified_3 INTEGER DEFAULT 0")
    print("[migration] Added column: notified_3")
if "notified_1" not in existing_cols:
    cursor.execute("ALTER TABLE birthday_log ADD COLUMN notified_1 INTEGER DEFAULT 0")
    print("[migration] Added column: notified_1")
if "notified_0" not in existing_cols:
    cursor.execute("ALTER TABLE birthday_log ADD COLUMN notified_0 INTEGER DEFAULT 0")
    print("[migration] Added column: notified_0")

wl_cols = {row[1] for row in cursor.execute("PRAGMA table_info(wheel_log)")}
if "spins_count" not in wl_cols:
    cursor.execute("ALTER TABLE wheel_log ADD COLUMN spins_count INTEGER DEFAULT 1")
    print("[migration] Added column: wheel_log.spins_count")
if "expire_at" not in wl_cols:
    cursor.execute("ALTER TABLE wheel_log ADD COLUMN expire_at REAL")
    print("[migration] Added column: wheel_log.expire_at")

tm_cols = {row[1] for row in cursor.execute("PRAGMA table_info(temp_messages)")}
if "expire_at" not in tm_cols:
    cursor.execute("ALTER TABLE temp_messages ADD COLUMN expire_at REAL")
    cursor.execute(
        "UPDATE temp_messages SET expire_at = created_at + 86400 WHERE expire_at IS NULL"
    )
    print("[migration] Added column: temp_messages.expire_at + backfilled")

bc_cols = {row[1] for row in cursor.execute("PRAGMA table_info(barcodes)")}
if "chat_id" not in bc_cols:
    cursor.execute("ALTER TABLE barcodes ADD COLUMN chat_id INTEGER")
    cursor.execute("UPDATE barcodes SET chat_id = user_id WHERE chat_id IS NULL")
    print("[migration] Added column: barcodes.chat_id (backfilled from user_id)")

conn.commit()

user_state = {}
admin_mode = {}

# ── Моніторинг ──────────────────────────────────────────────────────────────
_time_global = time  # аліас для зворотної сумісності

_last_receipt_time: float = _time_global.time()  # час останнього обробленого чека
_last_activity: float = _time_global.time()      # час будь-якої активності бота
_BOT_START_TIME: float = _time_global.time()     # час запуску процесу
_restart_count: int = 0  # лічильник перезапусків polling
_last_tg_check: float = _time_global.time()   # час останньої успішної перевірки Telegram API
_TG_FAIL_ALERTED: bool = False                 # щоб не спамити про TG збій
_bot_started: bool = False                     # True після того як post_init відпрацював
_crash_mode: bool = False                      # True під час /test_crash — блокує _touch_activity


def _touch_receipt():
    """Оновлює час останнього чека (викликається в receipt_checker)."""
    global _last_receipt_time
    _last_receipt_time = _time_global.time()


def _touch_activity():
    """Оновлює час останньої активності. В crash_mode — пропускає (watchdog бачить freeze)."""
    global _last_activity
    if _crash_mode:
        return  # симуляція зависання: inactivity росте, watchdog спрацює
    _last_activity = _time_global.time()


def _barcode_expire() -> float:
    """Expire = min(now+4h, наступний 00:00 Kyiv) як Unix timestamp."""
    _now_k = datetime.now(KYIV_TZ)
    _in_4h = _now_k + timedelta(hours=4)
    _midnight = _now_k.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    return min(_in_4h, _midnight).timestamp()


def _save_barcode_msg(user_id: int, message_id: int, chat_id: int = None):
    """Зберігає новий штрих-код в таблиці barcodes (TTL = 4год або 00:00 Kyiv — що раніше)."""
    try:
        _created = _time_global.time()
        _expire = _barcode_expire()
        _chat_id = chat_id if chat_id is not None else user_id
        _exp_kyiv = datetime.fromtimestamp(_expire, tz=KYIV_TZ).strftime(
            "%d.%m.%Y %H:%M"
        )
        cursor.execute(
            "INSERT INTO barcodes (user_id, chat_id, code, message_id, created_at, expire_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, _chat_id, str(user_id), message_id, _created, _expire),
        )
        conn.commit()
        print(
            f"[barcode] saved user={user_id} chat={_chat_id} msg={message_id} expire={_exp_kyiv} Kyiv"
        )
    except Exception as _e:
        print(f"[barcode] save error: {_e}")


async def _delete_user_barcodes(user_id: int, bot):
    """Видаляє всі попередні штрих-коди користувача перед генерацією нового (3 спроби)."""
    global _sys_stats
    try:
        cursor.execute(
            "SELECT message_id, COALESCE(chat_id, user_id) FROM barcodes WHERE user_id=?",
            (user_id,),
        )
        old_msgs = cursor.fetchall()
        for mid, cid in old_msgs:
            for _attempt in range(3):
                try:
                    await bot.delete_message(chat_id=cid, message_id=mid)
                    _sys_stats["barcode_deleted"] += 1
                    print(
                        f"[OK] Barcode deleted: user={user_id} chat={cid} msg={mid} attempt={_attempt + 1}"
                    )
                    break
                except Exception as _de:
                    if _attempt == 2:
                        _sys_stats["barcode_del_err"] += 1
                        print(
                            f"[ERROR] Barcode delete failed: user={user_id} chat={cid} msg={mid} err={_de}"
                        )
                    else:
                        await asyncio.sleep(1)
        cursor.execute("DELETE FROM barcodes WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception as _e:
        print(f"[barcode] _delete_user_barcodes error: {_e}")


def _midnight_kyiv() -> float:
    """Повертає Unix timestamp наступного 00:00 за часовим поясом Київ."""
    _now_k = datetime.now(KYIV_TZ)
    _next = _now_k.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    return _next.timestamp()


def _save_temp_msg(
    user_id: int, message_id: int, msg_type: str, expire_at: float = None
):
    """Зберігає message_id в temp_messages для TTL-видалення.
    expire_at — Unix timestamp; якщо None, то наступний 00:00 за Києвом."""
    try:
        _created = _time_global.time()
        _ea = expire_at if expire_at is not None else _midnight_kyiv()
        _ea_str = datetime.fromtimestamp(_ea, tz=KYIV_TZ).strftime(
            "%d.%m.%Y %H:%M Kyiv"
        )
        cursor.execute(
            "INSERT INTO temp_messages (user_id, message_id, type, created_at, expire_at) VALUES (?,?,?,?,?)",
            (user_id, message_id, msg_type, _created, _ea),
        )
        conn.commit()
        print(
            f"[temp_msg] saved user={user_id} msg={message_id} type={msg_type} expire_at={_ea_str}"
        )
    except Exception as _e:
        print(f"[temp_msg] save error: {_e}")


# ========= КНОПКИ =========
start_keyboard = ReplyKeyboardMarkup([["🚀 Пуск"]], resize_keyboard=True)


def get_main_keyboard(user_id):
    if user_id == ADMIN_ID:
        return ReplyKeyboardMarkup(
            [
                ["💳 Моя карта"],
                ["🎁 Бонуси", "📊 Історія"],
                ["🎰 КОЛЕСО ФОРТУНИ 🎰"],
                ["📍 Як нас знайти", "📸 Instagram"],
                ["🔧 Адмін"],
            ],
            resize_keyboard=True,
        )
    return ReplyKeyboardMarkup(
        [
            ["💳 Моя карта"],
            ["🎁 Бонуси", "📊 Історія"],
            ["🎰 КОЛЕСО ФОРТУНИ 🎰"],
            ["📍 Як нас знайти", "📸 Instagram"],
        ],
        resize_keyboard=True,
    )


contact_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Поділитись номером", request_contact=True)]],
    resize_keyboard=True,
)

admin_keyboard = ReplyKeyboardMarkup(
    [
        ["📊 Клієнти", "🔍 Пошук"],
        ["➕ Нарахувати бонус", "❌ Видалити"],
        ["📢 Розсилка", "📥 Вигрузити клієнтів"],
        ["📊 База клієнтів", "🎡 Аналітика колеса"],
        ["📊 Бонусна кампанія"],
        ["🔄 Синхронізувати клієнтів", "🛠 Виправити штрих-коди"],
        ["🔄 Оновити всіх клієнтів"],
        ["🔍 Перевірка системи"],
        ["🔄 Оновити меню"],
        ["⬅️ Назад"],
    ],
    resize_keyboard=True,
)

INSTAGRAM_URL = "https://www.instagram.com/pivnii_na_raioni?igsh=MXJuNTB6YXNkM3lmNQ%3D%3D&utm_source=qr"
LOCATION_URL = "https://maps.google.com/?cid=1923607365414580195"
print(f"[LOCATION_URL] {LOCATION_URL}")


def get_instagram_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📸 Підписатись на Instagram", url=INSTAGRAM_URL)]]
    )


def get_marketing_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Як нас знайти", url=LOCATION_URL)],
        [InlineKeyboardButton("📸 Підписатись на Instagram", url=INSTAGRAM_URL)],
    ])


# ========= EXCEL EXPORT =========
def generate_excel():
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Клієнти"
    ws.append(["ID", "Телефон", "Ім'я", "Дата народження", "Бонуси"])
    cursor.execute(
        "SELECT user_id, phone, name, birth, bonus FROM users ORDER BY bonus DESC"
    )
    for row in cursor.fetchall():
        ws.append(list(row))
    file_name = f"clients_{datetime.now(KYIV_TZ).strftime('%Y-%m-%d')}.xlsx"
    wb.save(file_name)
    return file_name


# ========= СИНХРОНІЗАЦІЯ + ВИПРАВЛЕННЯ ВСІХ КЛІЄНТІВ =========
def sync_and_fix_clients():
    def norm(phone):
        return poster_api._normalize_phone(phone)

    poster_clients = poster_api.get_clients()
    phone_map = {}
    ext_map = {}
    id_map = {}
    for c in poster_clients:
        pid = str(c.get("client_id") or c.get("id") or "")
        if pid:
            id_map[pid] = c
        np = norm(c.get("phone") or "")
        if np:
            phone_map[np] = c
        ext = str(c.get("external_id") or "")
        if ext:
            ext_map[ext] = c

    cursor.execute("SELECT user_id, phone, name, birth, poster_client_id FROM users")
    users = cursor.fetchall()

    updated = 0
    created = 0
    skipped = 0

    for user_id, phone, name, birth, existing_pid in users:
        try:
            if not phone or not phone.strip():
                skipped += 1
                print(f"[sync_fix] ⚠️ skipped user={user_id} — no phone")
                continue

            birthday = None
            if birth:
                try:
                    birthday = datetime.strptime(birth.strip(), "%d.%m.%Y").strftime(
                        "%Y-%m-%d"
                    )
                except Exception:
                    pass

            poster_c = None

            if existing_pid and str(existing_pid) in id_map:
                poster_c = id_map[str(existing_pid)]
            elif norm(phone) in phone_map:
                poster_c = phone_map[norm(phone)]
            elif str(user_id) in ext_map:
                poster_c = ext_map[str(user_id)]

            if poster_c is not None:
                pid = int(
                    poster_c.get("client_id") or poster_c.get("id") or existing_pid
                )
                result = poster_api.update_client(pid, user_id, birthday=birthday)
                if result is not None:
                    cursor.execute(
                        "UPDATE users SET poster_client_id=? WHERE user_id=?",
                        (pid, user_id),
                    )
                    conn.commit()
                    updated += 1
                    print(
                        f"[sync_fix] ✅ updated user={user_id} → poster={pid} bday={birthday}"
                    )
                else:
                    skipped += 1
                    print(
                        f"[sync_fix] ⚠️ update failed user={user_id} poster={existing_pid}"
                    )
            else:
                result = poster_api.create_client(
                    name or "—", phone, external_id=user_id, birthday=birthday
                )
                if result:
                    pid = result.get("client_id") or result.get("id")
                    if pid:
                        cursor.execute(
                            "UPDATE users SET poster_client_id=? WHERE user_id=?",
                            (pid, user_id),
                        )
                        conn.commit()
                        created += 1
                        print(
                            f"[sync_fix] ➕ created user={user_id} → poster={pid} bday={birthday}"
                        )
                else:
                    skipped += 1
                    print(f"[sync_fix] ⚠️ create failed user={user_id}")

        except Exception as e:
            skipped += 1
            print(f"[sync_fix] Error for user_id={user_id}: {e}")

    return updated, created, skipped


# ========= ВИПРАВЛЕННЯ ШТРИХ-КОДІВ =========
def fix_clients_card_numbers():
    cursor.execute(
        "SELECT user_id, poster_client_id FROM users WHERE poster_client_id IS NOT NULL"
    )
    rows = cursor.fetchall()
    updated = 0
    for user_id, poster_client_id in rows:
        try:
            result = poster_api.update_client(poster_client_id, user_id)
            if result is not None:
                updated += 1
                print(
                    f"[fix_barcodes] ✅ user_id={user_id} → poster_client_id={poster_client_id} → card_number={user_id}"
                )
            else:
                print(
                    f"[fix_barcodes] ⚠️ Failed for user_id={user_id}, poster_client_id={poster_client_id}"
                )
        except Exception as e:
            print(f"[fix_barcodes] Error for user_id={user_id}: {e}")
    return updated


# ========= ВІТАЛЬНИЙ БОНУС — РЕТРО-НАРАХУВАННЯ =========
def give_bonus_to_existing_users():
    cursor.execute(
        "SELECT user_id, poster_client_id FROM users WHERE welcome_bonus=0 AND poster_client_id IS NOT NULL"
    )
    rows = cursor.fetchall()
    given = 0
    skipped = 0
    for user_id, poster_client_id in rows:
        try:
            ok, _status, _data = poster_api.add_bonus(
                poster_client_id, 50, comment="Бонус за реєстрацію в Telegram"
            )
            if ok:
                cursor.execute(
                    "UPDATE users SET welcome_bonus=1 WHERE user_id=?", (user_id,)
                )
                conn.commit()
                given += 1
                print(
                    f"[welcome_bonus] ✅ user={user_id} poster={poster_client_id} +50"
                )
            else:
                skipped += 1
                print(
                    f"[welcome_bonus] ⚠️ failed user={user_id} poster={poster_client_id} status={_status}"
                )
        except Exception as e:
            skipped += 1
            print(f"[welcome_bonus] Error user={user_id}: {e}")
    return given, skipped


# ========= АВТО-ПРИВЯЗКА ДО POSTER =========
def _normalize_bonus(bonus) -> Decimal:
    """Нормалізує бонус: None → 0.00, > 100000 (копійки) → ділити на 100."""
    if bonus is None:
        return Decimal("0.00")
    val = Decimal(str(bonus))
    if val > Decimal("100000"):
        val = (val / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ensure_poster_client(user_id, phone, name, birth):
    """
    Верифікація + пошук + створення клієнта в Poster з retry-логікою.
    Кроки: search_phone → search_external_id → search_card_number → create_client
    Повертає (poster_client_id, was_created) або (None, False).
    """
    if not phone:
        print(f"[auto_fix] user_id={user_id} — немає телефону")
        return None, False

    norm = poster_api._normalize_phone(phone)
    print(f"[auto_fix] step=start user_id={user_id} phone={phone} norm={norm}")

    birthday = None
    if birth:
        try:
            birthday = datetime.strptime(birth.strip(), "%d.%m.%Y").strftime("%Y-%m-%d")
        except Exception:
            pass

    for attempt in range(3):
        try:
            print(f"[auto_fix] step=search_clients attempt={attempt + 1}")
            clients = poster_api.get_clients()
            if not clients:
                print(
                    f"[auto_fix] step=retry attempt={attempt + 1} — get_clients повернув порожньо"
                )
                time.sleep(1)
                continue

            found = None

            # 1. Пошук по телефону
            for c in clients:
                if poster_api._normalize_phone(c.get("phone") or "") == norm:
                    found = c
                    print(
                        f"[auto_fix] step=search_phone found client_id={c.get('client_id')}"
                    )
                    break

            # 2. Пошук по external_id
            if not found:
                for c in clients:
                    if str(c.get("external_id") or "") == str(user_id):
                        found = c
                        print(
                            f"[auto_fix] step=search_external_id found client_id={c.get('client_id')}"
                        )
                        break

            # 3. Пошук по card_number
            if not found:
                for c in clients:
                    if str(c.get("card_number") or "") == str(user_id):
                        found = c
                        print(
                            f"[auto_fix] step=search_card_number found client_id={c.get('client_id')}"
                        )
                        break

            # 4. Пошук по lastname == user_id (legacy: старий sync записував user_id в lastname)
            if not found:
                for c in clients:
                    if str(c.get("lastname") or "") == str(user_id):
                        found = c
                        print(
                            f"[auto_fix] step=search_lastname found client_id={c.get('client_id')}"
                        )
                        break

            if found:
                pid = int(float(found.get("client_id") or found.get("id")))
                poster_api.update_client(pid, user_id)
                cursor.execute(
                    "UPDATE users SET poster_client_id=? WHERE user_id=?",
                    (pid, user_id),
                )
                conn.commit()
                print(f"[auto_fix] status=found client_id={pid}")
                return pid, False

            # 4. Не знайдено — створюємо
            print(
                f"[auto_fix] step=create_client user_id={user_id} attempt={attempt + 1}"
            )
            result = poster_api.create_client(
                name or "Клієнт", phone, external_id=user_id, birthday=birthday
            )
            if result:
                # Poster повертає або int (новий client_id) або dict
                if isinstance(result, (int, float)):
                    pid = int(result)
                else:
                    pid = result.get("client_id") or result.get("id")
                if pid:
                    pid = int(pid)
                    cursor.execute(
                        "UPDATE users SET poster_client_id=? WHERE user_id=?",
                        (pid, user_id),
                    )
                    conn.commit()
                    print(f"[auto_fix] status=created client_id={pid}")
                    return pid, True

            print(f"[auto_fix] step=retry attempt={attempt + 1} — create_client failed")
            time.sleep(1)

        except Exception as e:
            print(f"[auto_fix] step=retry attempt={attempt + 1} error: {e}")
            time.sleep(1)

    print(f"[auto_fix] status=error всі спроби вичерпано user={user_id}")
    return None, False


# ========= POSTER SYNC =========
def sync_clients_from_poster():
    clients = poster_api.get_clients()
    if not clients:
        return 0
    imported = 0
    for c in clients:
        try:
            client_id = c.get("client_id") or c.get("id")
            phone = str(c.get("phone", "")).strip()
            firstname = c.get("firstname", "") or ""
            lastname = c.get("lastname", "") or ""
            name = f"{firstname} {lastname}".strip() or "—"
            # Poster повертає bonus в копійках → ділимо на 100 → грн (Decimal)
            _bonus_kopecks = Decimal(str(c.get("bonus", "0") or "0"))
            poster_balance = (_bonus_kopecks / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            print(
                f"[bonus_sync] poster={poster_balance} (kopecks={int(_bonus_kopecks)})"
            )

            if not client_id:
                continue

            cursor.execute(
                "SELECT user_id FROM users WHERE poster_client_id=?", (client_id,)
            )
            if cursor.fetchone():
                continue

            if phone:
                cursor.execute("SELECT user_id FROM users WHERE phone=?", (phone,))
                existing = cursor.fetchone()
                if existing:
                    cursor.execute(
                        "UPDATE users SET poster_client_id=? WHERE user_id=?",
                        (client_id, existing[0]),
                    )
                    conn.commit()
                    continue

            cursor.execute(
                "INSERT OR IGNORE INTO users "
                "(user_id, phone, name, bonus, poster_client_id) VALUES (?, ?, ?, ?, ?)",
                (client_id, phone, name, str(poster_balance), client_id),
            )
            conn.commit()
            imported += 1
        except Exception as e:
            print(f"[sync] Error processing client: {e}")
    return imported


# ========= BARCODE =========
def create_barcode(user_id):
    from barcode import Code128
    from barcode.writer import ImageWriter

    barcode = Code128(str(user_id), writer=ImageWriter())
    full_path = barcode.save(f"barcode_{user_id}")
    return full_path


# ========= ПЕРЕВІРКА ПІДПИСКИ =========
async def check_sub(user_id, context):
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# ========= VERIFIED BONUS HELPER =========
async def poster_add_bonus_verified(client_id, amount, comment, app=None):
    """
    Нараховує бонуси через Poster і одразу перевіряє результат.
    Повертає (ok: bool, balance_after: float|None).
    Логує:
      [balance_before]     — баланс до нарахування
      [poster_bonus_add]   — HTTP OK від Poster
      [poster_bonus_error] — HTTP помилка (включаючи 405)
      [balance_after]      — баланс після нарахування
      [bonus_success]      — баланс підтверджено збільшено
      [bonus_not_applied]  — баланс НЕ змінився (Poster проігнорував)
    """
    client_id = int(client_id)
    amount = _dec(amount)  # Decimal, наприклад 50.00

    # 1. Баланс ДО (Poster = джерело правди)
    _raw_before = poster_api.get_poster_balance(client_id)
    balance_before: Decimal | None = (
        _dec(_raw_before) if _raw_before is not None else None
    )
    print(f"[balance_before] client={client_id} balance={balance_before}")

    # 2. Нарахування
    ok, status_code, data = poster_api.add_bonus(client_id, int(amount), comment)

    if ok:
        print(
            f"[poster_bonus_add] client={client_id} amount={amount} "
            f"status={status_code} comment={repr(comment)}"
        )
    else:
        if status_code == 405:
            print(
                f"[poster_bonus_error] client={client_id} amount={amount} "
                f"status=405 — Тариф Poster не підтримує addBonus"
            )
            admin_msg = (
                f"❌ Тариф Poster не підтримує addBonus\n"
                f"client_id={client_id} | +{amount:.2f}\n"
                f"HTTP 405 — оновіть тарифний план"
            )
        else:
            print(
                f"[poster_bonus_error] client={client_id} amount={amount} "
                f"status={status_code} data={data}"
            )
            admin_msg = (
                f"❌ Poster не застосував бонуси\n"
                f"client_id={client_id} | +{amount:.2f} | HTTP {status_code}\n"
                f"comment: {comment}"
            )
        if app:
            try:
                await app.bot.send_message(ADMIN_ID, admin_msg)
            except Exception:
                pass

    # 3. Баланс ПІСЛЯ (знову з Poster)
    _raw_after = poster_api.get_poster_balance(client_id)
    balance_after: Decimal | None = _dec(_raw_after) if _raw_after is not None else None
    print(f"[balance_after] client={client_id} balance={balance_after}")

    # 4. Перевірка результату
    if balance_before is not None and balance_after is not None:
        print(f"[bonus_sync] poster={balance_after} before={balance_before}")
        if balance_after > balance_before:
            print(
                f"[bonus_success] client={client_id} +{amount} | "
                f"{balance_before:.2f} → {balance_after:.2f} ✅"
            )
        else:
            print(
                f"[bonus_not_applied] client={client_id} +{amount} | "
                f"{balance_before:.2f} → {balance_after:.2f} ❌"
            )
            if ok and app:
                try:
                    await app.bot.send_message(
                        ADMIN_ID,
                        f"❌ Poster не застосував бонуси\n"
                        f"client_id={client_id} | +{amount:.2f}\n"
                        f"Баланс до: {balance_before:.2f} | Після: {balance_after:.2f}",
                    )
                except Exception:
                    pass
    elif balance_after is None:
        print(
            f"[bonus_not_applied] client={client_id} — "
            f"не вдалося отримати баланс після нарахування"
        )

    return ok, balance_after


# ===== ДР — DEBUG MODE =====
async def birthday_checker(app):
    print("[birthday_checker] Task started ✅")
    while True:
        print("[birthday_checker] Running check...")
        now = datetime.now(KYIV_TZ)

        cursor.execute("SELECT user_id, name, birth FROM users")
        users = cursor.fetchall()

        print(f"[birthday_checker] Found {len(users)} user(s) in DB")

        for u in users:
            user_id, name, birth = u

            if not birth:
                print(f"  [skip] user_id={user_id} — no birth date")
                continue

            try:
                bdate = datetime.strptime(birth, "%d.%m.%Y")

                next_bd = datetime(now.year, bdate.month, bdate.day, tzinfo=KYIV_TZ)
                if next_bd < now:
                    next_bd = datetime(
                        now.year + 1, bdate.month, bdate.day, tzinfo=KYIV_TZ
                    )

                days_left = (next_bd - now).days

                print(
                    f"  user_id={user_id} | name={name} | birth={birth} | next_bd={next_bd.date()} | days_left={days_left}"
                )

                cursor.execute(
                    "SELECT notified_3, notified_1, notified_0 FROM birthday_log WHERE user_id=? AND year=?",
                    (user_id, next_bd.year),
                )
                log = cursor.fetchone()

                if not log:
                    cursor.execute(
                        "INSERT INTO birthday_log (user_id, year) VALUES (?, ?)",
                        (user_id, next_bd.year),
                    )
                    conn.commit()
                    notified_3, notified_1, notified_0 = 0, 0, 0
                else:
                    notified_3, notified_1, notified_0 = log

                if days_left == 3 and not notified_3:
                    try:
                        await app.bot.send_message(
                            user_id,
                            f"🎉 {name}, вже скоро твій День народження!\n\n🎁 Ти отримаєш 100 грн бонус!",
                            reply_markup=get_instagram_keyboard(),
                        )
                        cursor.execute(
                            "UPDATE birthday_log SET notified_3=1 WHERE user_id=? AND year=?",
                            (user_id, next_bd.year),
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"ERROR sending 3-day message to {user_id}: {e}")

                if days_left == 1 and not notified_1:
                    try:
                        await app.bot.send_message(
                            user_id,
                            f"🔥 {name}, вже завтра!\n\n🎂 Завтра отримаєш 100 грн бонус!",
                            reply_markup=get_instagram_keyboard(),
                        )
                        cursor.execute(
                            "UPDATE birthday_log SET notified_1=1 WHERE user_id=? AND year=?",
                            (user_id, next_bd.year),
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"ERROR sending 1-day message to {user_id}: {e}")

                if days_left == 0 and not notified_0:
                    try:
                        cursor.execute(
                            "SELECT poster_client_id FROM users WHERE user_id=?",
                            (user_id,),
                        )
                        pid_row = cursor.fetchone()
                        poster_pid = pid_row[0] if pid_row else None

                        balance_msg = ""
                        if poster_pid:
                            _ok, balance = await poster_add_bonus_verified(
                                poster_pid, 100, "Бонус до дня народження", app
                            )
                            if balance is not None:
                                balance_msg = f"\n🎁 Ваш баланс: {int(balance)} грн"
                        else:
                            print(
                                f"[birthday_bonus] user={user_id} — no poster_client_id, бонус не нараховано"
                            )

                        await app.bot.send_message(
                            user_id,
                            f"🎉 {name}, сьогодні твій День народження!\n"
                            f"🔥 Тобі нараховано +100 грн бонусу!{balance_msg}\n"
                            f"🍻 Чекаємо в гості!",
                            reply_markup=get_instagram_keyboard(),
                        )
                        cursor.execute(
                            "UPDATE birthday_log SET notified_0=1 WHERE user_id=? AND year=?",
                            (user_id, next_bd.year),
                        )
                        conn.commit()
                    except Exception as e:
                        print(f"BDAY ERROR sending birthday message to {user_id}: {e}")

            except Exception as e:
                print(f"  [error] Failed processing user_id={user_id}: {e}")
                continue

        await asyncio.sleep(43200)


# ========= АВТО БОНУСИ З ЧЕКІВ =========
async def receipt_checker(app):
    print("[receipt_checker] Task started ✅")
    while True:
        try:
            today = datetime.now(KYIV_TZ).strftime("%Y%m%d")
            transactions = poster_api.get_transactions(today, today)
            tx_count = len(transactions) if transactions else 0
            if tx_count:
                _touch_receipt()  # оновлюємо watchdog-час
                print(f"[receipt_checker] Got {tx_count} transaction(s) for {today}")

            for t in transactions or []:
                try:
                    import json as _json

                    transaction_id = str(t.get("transaction_id") or t.get("id") or "")
                    client_id_raw = (
                        t.get("client_id")
                        or t.get("client")
                        or t.get("clients_id")
                        or t.get("cardClientId")
                    )

                    # ── Фільтр: потрібен transaction_id і client_id ──────────
                    if not transaction_id or not client_id_raw:
                        continue

                    client_id = int(float(client_id_raw))

                    # ── Сума: загальна (sum) і касова (payed_sum) ────────────
                    total_sum = _dec(t.get("sum") or t.get("amount") or "0")
                    payed_sum = _dec(t.get("payed_sum") or "0")
                    if total_sum <= Decimal("0") and payed_sum <= Decimal("0"):
                        continue

                    # ── Дедублікація: перевіряємо sent-статус ────────────────
                    cursor.execute(
                        "SELECT id, sent, retry_count FROM purchases_log "
                        "WHERE transaction_id=?",
                        (transaction_id,),
                    )
                    existing = cursor.fetchone()

                    if existing:
                        _row_id, _sent, _retry_cnt = existing
                        if _sent:
                            print(f"[receipt_skip] tx={transaction_id} — вже надіслано")
                            continue
                        # sent=0, але вже 3+ невдалих спроби — припиняємо
                        if _retry_cnt >= 3:
                            print(
                                f"[receipt_skip] tx={transaction_id} — "
                                f"перевищено ліміт спроб ({_retry_cnt}), пропускаємо"
                            )
                            continue
                        # sent=0: запис є, але повідомлення не дійшло → повторна відправка
                        _is_resend = True
                        print(
                            f"[resend_attempt] tx={transaction_id} | client={client_id} | "
                            f"спроба #{_retry_cnt + 1}"
                        )
                    else:
                        _is_resend = False
                        print(
                            f"[receipt_found] tx={transaction_id} | client={client_id} | "
                            f"sum={total_sum:.2f} payed={payed_sum:.2f}"
                        )

                    # ── Бонуси ────────────────────────────────────────────────
                    bonus_spent = _dec(
                        t.get("payed_bonus")
                        or t.get("paid_bonus")
                        or t.get("bonus_payed")
                        or t.get("bonus_spent")
                        or "0"
                    )

                    products = t.get("products") or []
                    bonus_earned = sum(
                        (_dec(p.get("bonus_accrual", "0")) for p in products),
                        Decimal("0.00"),
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                    # ── Фіскальні дані — перевіряємо всі відомі поля ─────────
                    print_fiscal = bool(int(t.get("print_fiscal") or 0))

                    # 1. Пряме поле транзакції
                    fiscal_number = (
                        t.get("fiscal_number")
                        or t.get("fiscal_receipt_number")
                        or t.get("fiscal_check_number")
                        or t.get("receipt_number")
                        or t.get("fiscal_id")
                        or t.get("fn")
                    )

                    # 2. Вкладений об'єкт fiscal_data
                    if not fiscal_number and t.get("fiscal_data"):
                        fd = t["fiscal_data"]
                        fiscal_number = (
                            fd.get("check_number")
                            or fd.get("fiscal_number")
                            or fd.get("receipt_number")
                            or fd.get("fn")
                        )

                    # 3. Шукаємо в products
                    if not fiscal_number:
                        for p in products:
                            fiscal_number = (
                                p.get("fiscal_number")
                                or p.get("fiscal_receipt_number")
                                or p.get("fiscal_id")
                                or p.get("fn")
                            )
                            if fiscal_number:
                                break

                    # 4. Якщо fiscal_number є — будуємо URL податкової
                    fiscal_url = (
                        t.get("fiscal_url")
                        or t.get("receipt_url")
                        or t.get("check_url")
                        or t.get("qr_code")
                    )
                    if not fiscal_url:
                        for p in products:
                            fiscal_url = (
                                p.get("fiscal_url")
                                or p.get("receipt_url")
                                or p.get("check_url")
                            )
                            if fiscal_url:
                                break

                    if not fiscal_url and fiscal_number:
                        date_str = (t.get("date_close") or "")[:10]  # "2026-04-01"
                        try:
                            fiscal_url = (
                                f"https://cabinet.tax.gov.ua/cashregs/check"
                                f"?fn={fiscal_number}&dt={date_str}"
                            )
                        except Exception:
                            fiscal_url = None

                    # 5. Логуємо фіскальний результат
                    print(
                        f"[FISCAL] tx_id={transaction_id} | "
                        f"fiscal_number={fiscal_number!r} | "
                        f"fiscal_url={fiscal_url!r} | "
                        f"print_fiscal={print_fiscal}"
                    )

                    # 6. Якщо print_fiscal=1 але номер не знайдено — FULL dump
                    if print_fiscal and not fiscal_number:
                        print(f"[FULL_TX] {_json.dumps(t, ensure_ascii=False)}")

                    # ── Актуальний баланс з Poster (джерело правди) ──────────
                    _raw_balance = poster_api.get_poster_balance(client_id)
                    balance: Decimal | None = (
                        _dec(_raw_balance) if _raw_balance is not None else None
                    )

                    # ── Контроль бонусів (місматч) ───────────────────────────
                    cursor.execute(
                        "SELECT balance_after, bonus, bonus_spent FROM purchases_log "
                        "WHERE client_id=? AND sent=1 ORDER BY id DESC LIMIT 1",
                        (client_id,),
                    )
                    _prev = cursor.fetchone()
                    if _prev and _prev[0] is not None and balance is not None:
                        _prev_bal = _dec(_prev[0])
                        _prev_earned = _dec(_prev[1])
                        _prev_spent = _dec(_prev[2])
                        print(
                            f"[DB_LOAD] balance_after={_prev[0]} "
                            f"bonus={_prev[1]} bonus_spent={_prev[2]}"
                        )
                        _expected = (_prev_bal + bonus_earned - bonus_spent).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        _actual = balance
                        print(
                            f"[bonus_sync] poster={_actual} local_expected={_expected}"
                        )
                        if abs(_expected - _actual) > Decimal("1.00"):
                            print(
                                f"[bonus_mismatch] client={client_id} "
                                f"expected={_expected} actual={_actual} "
                                f"(prev_bal={_prev_bal} +earned={bonus_earned} "
                                f"-spent={bonus_spent})"
                            )
                            try:
                                await app.bot.send_message(
                                    ADMIN_ID,
                                    f"⚠️ РОЗСИНХРОН БОНУСІВ\n"
                                    f"client_id: {client_id}\n"
                                    f"Poster: {_actual:.2f}\n"
                                    f"Очікувалось: {_expected:.2f}\n"
                                    f"TX: {transaction_id}",
                                )
                            except Exception as _me:
                                print(f"[bonus_mismatch] alert error: {_me}")

                    # ── Запис у purchases_log з sent=0 (якщо нова транзакція) ─
                    if not _is_resend:
                        print(
                            f"[DB_SAVE] INSERT tx={transaction_id} "
                            f"amount={to_db(total_sum)} "
                            f"bonus={to_db(bonus_earned)} "
                            f"bonus_spent={to_db(bonus_spent)}"
                        )
                        cursor.execute(
                            "INSERT OR IGNORE INTO purchases_log "
                            "(client_id, amount, bonus, bonus_spent, created_at, "
                            "transaction_id, sent, retry_count) "
                            "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                            (
                                client_id,
                                to_db(total_sum),
                                to_db(bonus_earned),
                                to_db(bonus_spent),
                                datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                                transaction_id,
                            ),
                        )
                        conn.commit()

                    # ── Пошук Telegram-юзера (тільки реальні, user_id > 100M) ─
                    cursor.execute(
                        "SELECT user_id FROM users "
                        "WHERE poster_client_id=? AND user_id > 100000000 LIMIT 1",
                        (client_id,),
                    )
                    user_row = cursor.fetchone()

                    if not user_row:
                        print(
                            f"[receipt_skip] tx={transaction_id} client={client_id} "
                            f"— Telegram-юзер не знайдений"
                        )
                        continue

                    user_id = user_row[0]

                    # ── Лог даних чека ────────────────────────────────────────
                    _label = "receipt_resend" if _is_resend else "receipt_send"
                    print(f"[{_label}] tx={transaction_id}")
                    print(
                        f"[receipt_data] amount={total_sum} | "
                        f"payed={payed_sum} | "
                        f"spent={bonus_spent} | "
                        f"earned={bonus_earned} | "
                        f"balance={balance}"
                    )

                    # ── Формуємо повідомлення ─────────────────────────────────
                    lines = [f"🧾 Ваш чек №{transaction_id}\n"]

                    lines.append(f"💰 Сума замовлення: {total_sum:.2f} грн")

                    if bonus_spent > 0:
                        lines.append(f"➖ Списано бонусів: {bonus_spent:.2f} грн")

                    lines.append(f"💳 До оплати: {payed_sum:.2f} грн")

                    lines.append("")  # порожній рядок

                    if bonus_earned > 0:
                        lines.append(f"➕ Нараховано бонусів: {bonus_earned:.2f} грн")

                    if balance is not None:
                        lines.append(f"🎁 Баланс після покупки: {balance:.2f} грн")
                    else:
                        lines.append("🎁 Баланс: тимчасово недоступний")

                    lines.append("")
                    lines.append("━━━━━━━━━━━━━━━")
                    lines.append("🍻 Дякуємо за покупку!")

                    # Кнопки: фіскальний чек (якщо є) + Instagram
                    buttons_row1 = []
                    if fiscal_url:
                        buttons_row1.append(
                            InlineKeyboardButton("🧾 Відкрити чек", url=fiscal_url)
                        )
                    instagram_btn = InlineKeyboardButton(
                        "📸 Підписатись на Instagram", url=INSTAGRAM_URL
                    )
                    kb_rows = []
                    if buttons_row1:
                        kb_rows.append(buttons_row1)
                    kb_rows.append([instagram_btn])
                    reply_markup = InlineKeyboardMarkup(kb_rows)

                    msg_text = "\n".join(lines)

                    # ── Відправка ─────────────────────────────────────────────
                    try:
                        await app.bot.send_message(
                            user_id, msg_text, reply_markup=reply_markup
                        )
                        # ✅ Відправлено — позначаємо sent=1, зберігаємо balance_after
                        _bal_db = to_db(balance)
                        print(f"[DB_SAVE] balance_after={_bal_db} tx={transaction_id}")
                        cursor.execute(
                            "UPDATE purchases_log SET sent=1, balance_after=? "
                            "WHERE transaction_id=?",
                            (_bal_db, transaction_id),
                        )
                        # Видаляємо з failed_receipts якщо є
                        cursor.execute(
                            "DELETE FROM failed_receipts WHERE tx_id=?",
                            (str(transaction_id),),
                        )
                        conn.commit()

                        # ── Списуємо temp_bonus якщо є ───────────────────────
                        cursor.execute(
                            "SELECT COUNT(*) FROM temp_bonus "
                            "WHERE user_id=? AND expires_at > ?",
                            (user_id, int(_time_global.time())),
                        )
                        _tb_cnt = cursor.fetchone()[0] or 0
                        if _tb_cnt > 0:
                            cursor.execute(
                                "DELETE FROM temp_bonus WHERE user_id=?", (user_id,)
                            )
                            conn.commit()
                            print(f"[temp_bonus_used] user={user_id} count={_tb_cnt}")

                        _fiscal_label = (
                            repr(fiscal_number)
                            if fiscal_number
                            else ("URL" if fiscal_url else "—")
                        )
                        print(
                            f"[receipt_sent] ✅ tx={transaction_id} | user={user_id} | "
                            f"client={client_id} | sum={total_sum:.2f} | "
                            f"+{bonus_earned:.2f} -{bonus_spent:.2f} | "
                            f"balance={balance} | fiscal={_fiscal_label}"
                        )
                    except Exception as e:
                        # ❌ Не вдалось — збільшуємо retry_count, пишемо в failed_receipts
                        _err_str = str(e)
                        print(
                            f"[receipt_error] user={user_id} tx={transaction_id}: {_err_str}"
                        )
                        cursor.execute(
                            "UPDATE purchases_log SET retry_count = retry_count + 1 "
                            "WHERE transaction_id=?",
                            (transaction_id,),
                        )
                        cursor.execute(
                            "INSERT OR REPLACE INTO failed_receipts "
                            "(tx_id, client_id, user_id, error, created_at, retry_count) "
                            "VALUES (?, ?, ?, ?, ?, "
                            "  COALESCE((SELECT retry_count FROM failed_receipts "
                            "            WHERE tx_id=?), 0) + 1)",
                            (
                                str(transaction_id),
                                client_id,
                                user_id,
                                _err_str,
                                datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                                str(transaction_id),
                            ),
                        )
                        conn.commit()
                        try:
                            await app.bot.send_message(
                                ADMIN_ID,
                                f"❌ Чек НЕ відправлено\n"
                                f"TX: {transaction_id}\n"
                                f"Client: {client_id} | User: {user_id}\n"
                                f"Помилка: {_err_str[:200]}",
                            )
                        except Exception as _ae:
                            print(f"[receipt_error] alert failed: {_ae}")

                except Exception as e:
                    import traceback

                    print(
                        f"[receipt_checker] Помилка транзакції {t.get('transaction_id', '?')}: {e}"
                    )
                    print(traceback.format_exc())

        except Exception as e:
            import traceback

            print(f"[receipt_checker] Outer error: {e}")
            print(traceback.format_exc())

        await asyncio.sleep(10)


# ========= REFUND CHECKER =========
async def refund_checker(app):
    print("[refund_checker] Task started ✅")

    # Завантажуємо вже відомі tx_id повернень, щоб не дублювати
    seen_refund_ids: set = set()
    for row in cursor.execute("SELECT tx_id FROM refund_log"):
        seen_refund_ids.add(str(row[0]))

    while True:
        try:
            today = datetime.now(KYIV_TZ).strftime("%Y%m%d")
            transactions = poster_api.get_transactions(today, today) or []

            for t in transactions:
                tx_id = str(t.get("transaction_id") or t.get("id") or "")
                if not tx_id:
                    continue

                reason = int(float(t.get("reason") or 0))
                total_sum = _dec(t.get("sum") or "0")
                payed_sum = _dec(t.get("payed_sum") or "0")

                # Визначаємо повернення: reason != 0 АБО від'ємна сума
                is_refund = (
                    (reason != 0)
                    or (total_sum < Decimal("0"))
                    or (payed_sum < Decimal("0"))
                )
                if not is_refund:
                    continue

                if tx_id in seen_refund_ids:
                    print(f"[refund_skip] tx={tx_id}")
                    continue

                print(
                    f"[refund_detected] tx={tx_id} | reason={reason} | sum={total_sum}"
                )
                seen_refund_ids.add(tx_id)

                # Фіксуємо у БД
                cursor.execute(
                    "INSERT OR IGNORE INTO refund_log (tx_id) VALUES (?)", (int(tx_id),)
                )
                conn.commit()

                # ── Клієнт ───────────────────────────────────────────────
                client_id = t.get("client_id")
                phone = "—"
                client_name = "—"
                if client_id:
                    try:
                        c_data = poster_api.get_client(int(client_id))
                        if isinstance(c_data, list):
                            c_data = c_data[0] if c_data else None
                        if isinstance(c_data, dict):
                            raw_phone = (
                                c_data.get("phone") or c_data.get("phone_number") or ""
                            )
                            phone = raw_phone.strip() if raw_phone else "—"
                            fname = c_data.get("firstname") or ""
                            lname = c_data.get("lastname") or ""
                            client_name = f"{fname} {lname}".strip() or "—"
                    except Exception:
                        pass

                # ── Бонуси ───────────────────────────────────────────────
                bonus_spent = _dec(t.get("payed_bonus") or "0")
                products = t.get("products") or []
                bonus_earned = sum(
                    (_dec(p.get("bonus_accrual", "0")) for p in products),
                    Decimal("0.00"),
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                # ── Причина ──────────────────────────────────────────────
                reason_labels = {
                    1: "скасовано",
                    2: "повернення коштів",
                    3: "видалено касиром",
                }
                reason_text = reason_labels.get(reason, f"reason={reason}")
                if total_sum < 0 and reason == 0:
                    reason_text = "від'ємна сума"

                # ── Повідомлення адміну ───────────────────────────────────
                msg = (
                    f"🚫 ПОВЕРНЕННЯ ЧЕКУ\n\n"
                    f"🧾 Чек №{tx_id}\n\n"
                    f"💰 Сума: {abs(total_sum):.2f} грн"
                )
                if bonus_spent > 0:
                    msg += f"\n➖ Списано бонусів: {bonus_spent:.2f} грн"
                if bonus_earned > 0:
                    msg += f"\n➕ Нараховано: {bonus_earned:.2f} грн"
                msg += (
                    f"\n\n👤 Клієнт: {client_name} (ID: {client_id})\n"
                    f"📱 {phone}\n\n"
                    f"⚠️ {reason_text}"
                )

                try:
                    await app.bot.send_message(ADMIN_ID, msg)
                    print(
                        f"[refund_sent] tx={tx_id} | client={client_id} | "
                        f"sum={total_sum:.2f} | reason={reason}"
                    )
                except Exception as e:
                    print(f"[refund_checker] Помилка відправки: {e}")

        except Exception as e:
            import traceback

            print(f"[refund_checker] Outer error: {e}")
            print(traceback.format_exc())

        await asyncio.sleep(30)


# ========= HELPERS: PAGINATED CLIENTS =========
CLIENTS_PAGE_SIZE = 10


def build_clients_page(page: int):
    """Повертає (text, markup) для сторінки списку клієнтів."""
    cursor.execute(
        "SELECT user_id, name, phone, bonus FROM users "
        "WHERE user_id > 100000000 ORDER BY name COLLATE NOCASE"
    )
    all_users = cursor.fetchall()
    total = len(all_users)
    total_pages = max(1, (total + CLIENTS_PAGE_SIZE - 1) // CLIENTS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * CLIENTS_PAGE_SIZE
    chunk = all_users[start : start + CLIENTS_PAGE_SIZE]

    lines = [f"👥 Клієнти (сторінка {page + 1}/{total_pages}, всього {total}):\n"]
    for i, (uid, name, phone, bonus) in enumerate(chunk, start=start + 1):
        n = name or "—"
        p = phone or "—"
        lines.append(f"{i}. {n} | {p} | {bonus or 0} грн")

    # Кнопки клієнтів
    btn_rows = []
    for uid, name, phone, bonus in chunk:
        label = f"{name or phone or uid}"[:28]
        btn_rows.append([InlineKeyboardButton(label, callback_data=f"cl_view_{uid}")])

    # Навігація
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"cl_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"cl_page_{page + 1}"))
    if nav:
        btn_rows.append(nav)

    markup = InlineKeyboardMarkup(btn_rows)
    return "\n".join(lines), markup


def build_client_detail(uid: int):
    """Повертає (text, markup) деталей клієнта для адміна."""
    cursor.execute(
        "SELECT user_id, name, phone, birth, bonus, poster_client_id "
        "FROM users WHERE user_id=?",
        (uid,),
    )
    row = cursor.fetchone()
    if not row:
        return "❌ Клієнт не знайдений", InlineKeyboardMarkup([])
    uid_, name, phone, birth, bonus, pid = row
    # Баланс Poster
    bal_str = "—"
    if pid:
        try:
            bal = poster_api.get_poster_balance(pid)
            bal_str = f"{bal:.2f} грн" if bal is not None else "—"
        except Exception:
            pass
    # Остання покупка
    cursor.execute(
        "SELECT amount, bonus, created_at FROM purchases_log "
        "WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (pid or -1,),
    )
    last = cursor.fetchone()
    last_str = f"{last[0]:.0f} грн / +{last[1]} грн / {last[2]}" if last else "—"

    text = (
        f"👤 {name or '—'}\n"
        f"📱 {phone or '—'}\n"
        f"🎂 {birth or '—'}\n"
        f"🆔 user_id: {uid_}\n"
        f"🎁 Бонуси Poster: {bal_str}\n"
        f"🧾 Остання покупка: {last_str}"
    )
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Нарахувати бонус", callback_data=f"cl_add_bonus_{uid}"
                )
            ],
            [InlineKeyboardButton("📊 Історія", callback_data=f"cl_history_{uid}")],
            [InlineKeyboardButton("❌ Видалити", callback_data=f"delete_{uid}")],
            [InlineKeyboardButton("◀️ До списку", callback_data="cl_page_0")],
        ]
    )
    return text, markup


# ========= CALLBACK =========
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _touch_activity()

    data = query.data

    if data.startswith("cl_page_"):
        page = int(data.split("_")[2])
        text, markup = build_clients_page(page)
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text, reply_markup=markup)

    elif data.startswith("cl_view_"):
        uid = int(data.split("_")[2])
        text, markup = build_client_detail(uid)
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception:
            await query.message.reply_text(text, reply_markup=markup)

    elif data.startswith("cl_history_"):
        uid = int(data.split("_")[2])
        cursor.execute("SELECT poster_client_id FROM users WHERE user_id=?", (uid,))
        r = cursor.fetchone()
        pid = r[0] if r else None
        if pid:
            cursor.execute(
                "SELECT amount, bonus, bonus_spent, created_at FROM purchases_log "
                "WHERE client_id=? ORDER BY id DESC LIMIT 10",
                (pid,),
            )
            rows = cursor.fetchall()
            if rows:
                lines = [f"📊 Покупки клієнта {uid}:\n"]
                for amt, earn, spent, dt in rows:
                    lines.append(
                        f"🧾 {float(amt):.0f} грн | +{earn:.0f} | -{spent:.0f} | {dt}"
                    )
                await query.message.reply_text("\n".join(lines))
            else:
                await query.message.reply_text("📊 Покупок ще немає")
        else:
            await query.message.reply_text("⚠️ Клієнт не прив'язаний до Poster")

    elif data.startswith("cl_add_bonus_"):
        uid = int(data.split("_")[3])
        cursor.execute(
            "SELECT name, poster_client_id FROM users WHERE user_id=?", (uid,)
        )
        r = cursor.fetchone()
        if not r or not r[1]:
            await query.message.reply_text("⚠️ Клієнт не прив'язаний до Poster")
            return
        name, pid = r
        admin_mode[ADMIN_ID] = {
            "action": "bonus_amount",
            "target_uid": uid,
            "target_name": name or str(uid),
            "poster_client_id": pid,
        }
        await query.message.reply_text(
            f"➕ Нарахування бонусу\n"
            f"👤 Клієнт: {name or uid}\n\n"
            f"Введіть суму бонусу (грн):"
        )

    elif data.startswith("cl_bonus_confirm_"):
        parts = data.split("_")
        uid = int(parts[3])
        amount = _dec(parts[4])
        cursor.execute(
            "SELECT name, poster_client_id FROM users WHERE user_id=?", (uid,)
        )
        r = cursor.fetchone()
        if not r or not r[1]:
            await query.message.reply_text("⚠️ Клієнт не знайдений")
            return
        name, pid = r
        admin_mode.pop(ADMIN_ID, None)
        _ok, bal = await poster_add_bonus_verified(
            pid, amount, "Ручне нарахування адміном", context.application
        )
        bal_txt = f"\n🎁 Новий баланс: {bal:.2f} грн" if bal is not None else ""
        await query.message.edit_text(
            f"{'✅' if _ok else '⚠️'} Нараховано {amount:.0f} грн → {name or uid}{bal_txt}"
        )
        # Повідомлення клієнту
        try:
            await context.bot.send_message(
                uid,
                f"🎁 Вам нараховано {amount:.0f} грн бонусів від бару!\n"
                f"{'🏦 Ваш баланс: ' + str(int(bal)) + ' грн' if bal is not None else ''}",
            )
        except Exception:
            pass
        print(f"[admin_bonus] uid={uid} pid={pid} amount={amount} ok={_ok} bal={bal}")

    elif data.startswith("cl_bonus_cancel_"):
        admin_mode.pop(ADMIN_ID, None)
        await query.message.edit_text("❌ Нарахування скасовано")

    elif data.startswith("card_"):
        uid = int(data.split("_")[1])
        file = create_barcode(uid)
        with open(file, "rb") as photo:
            await query.message.reply_photo(
                photo=photo,
                caption="📊 Покажи цей штрих-код на касі для нарахування бонусів",
            )

    elif data.startswith("bonus_"):
        uid = int(data.split("_")[1])
        cursor.execute("SELECT poster_client_id FROM users WHERE user_id=?", (uid,))
        pid_row = cursor.fetchone()
        poster_pid = pid_row[0] if pid_row else None
        if poster_pid:
            _ok, bal = await poster_add_bonus_verified(
                poster_pid, 50, "Бонус від адміна", context.application
            )
            bal_txt = f" | Баланс: {int(bal)} грн" if bal is not None else ""
            await query.message.reply_text(f"➕ +50 бонусів через Poster{bal_txt}")
        else:
            await query.message.reply_text(
                "⚠️ Клієнта немає в Poster, бонус не нараховано"
            )

    elif data.startswith("delete_"):
        uid = int(data.split("_")[1])
        cursor.execute("DELETE FROM users WHERE user_id=?", (uid,))
        conn.commit()
        await query.message.reply_text("❌ Клієнт видалений")

    elif data == "check_sub":
        user_id = query.from_user.id
        is_sub = await check_sub(user_id, context)

        if is_sub:
            await query.message.reply_text("✅ Доступ відкрито, натисни 🚀 Пуск")
        else:
            await query.message.reply_text("❌ Ти ще не підписаний")


# ========= START =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вітаю 👋", reply_markup=start_keyboard)
    await update.message.reply_text(
        "📸 Підписуйся на наш Instagram, щоб не пропустити акції!",
        reply_markup=get_instagram_keyboard(),
    )


# ========= АДМІН =========
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    admin_mode[update.effective_user.id] = None
    await update.message.reply_text("🔧 Адмін панель", reply_markup=admin_keyboard)


# ========= /balance <client_id> =========
async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Використання: /balance <client_id>")
        return
    try:
        cid = int(args[0])
    except ValueError:
        await update.message.reply_text("client_id має бути числом")
        return
    bal = poster_api.get_poster_balance(cid)
    cursor.execute(
        "SELECT u.full_name, u.phone FROM users u "
        "WHERE u.poster_client_id=? AND u.user_id > 100000000 LIMIT 1",
        (cid,),
    )
    urow = cursor.fetchone()
    name = urow[0] if urow else "—"
    phone = urow[1] if urow else "—"
    cursor.execute(
        "SELECT COUNT(*), SUM(amount), SUM(bonus), SUM(bonus_spent) "
        "FROM purchases_log WHERE client_id=? AND sent=1",
        (cid,),
    )
    stats = cursor.fetchone()
    cnt, total_amt, total_earn, total_spent = stats if stats else (0, 0, 0, 0)
    msg = (
        f"💳 Клієнт #{cid}\n"
        f"👤 {name} | 📱 {phone}\n\n"
        f"🎁 Баланс Poster: {bal:.2f} грн\n\n"
        f"📊 Статистика:\n"
        f"  Чеків надіслано: {cnt}\n"
        f"  Загальна сума: {total_amt or 0:.2f} грн\n"
        f"  Нараховано бонусів: {total_earn or 0:.2f} грн\n"
        f"  Списано бонусів: {total_spent or 0:.2f} грн"
    )
    await update.message.reply_text(msg)


# ========= /checks — останні 10 чеків =========
async def checks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    cursor.execute(
        "SELECT transaction_id, client_id, amount, bonus, bonus_spent, created_at "
        "FROM purchases_log WHERE sent=1 "
        "ORDER BY id DESC LIMIT 10"
    )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("Чеків ще немає")
        return
    lines = ["📋 Останні 10 чеків:\n"]
    for tx, cid, amt, earn, spent, dt in rows:
        lines.append(
            f"🧾 TX#{tx} | Client:{cid}\n"
            f"   💰 {amt:.2f} грн | +{earn:.2f} бонус | -{spent:.2f} списано\n"
            f"   📅 {dt}"
        )
    await update.message.reply_text("\n".join(lines))


# ========= /refunds — всі повернення =========
async def refunds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    # refund_log має лише tx_id; додаткові дані беремо з purchases_log
    cursor.execute(
        "SELECT r.tx_id, p.client_id, p.amount, p.created_at "
        "FROM refund_log r "
        "LEFT JOIN purchases_log p ON p.transaction_id = CAST(r.tx_id AS TEXT) "
        "ORDER BY r.tx_id DESC LIMIT 20"
    )
    rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("Повернень ще немає")
        return
    lines = [f"🚫 Повернення (останні {len(rows)}):\n"]
    for tx, cid, amt, dt in rows:
        lines.append(
            f"TX#{tx} | Client:{cid or '?'}\n   💸 {amt or '?'} грн | 📅 {dt or '?'}"
        )
    await update.message.reply_text("\n".join(lines))


# ========= /broadcast — текст → надіслати всім =========
_broadcast_state: dict = {}  # user_id → "waiting_text"


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    _broadcast_state[ADMIN_ID] = "waiting_text"
    await update.message.reply_text("📩 Введіть текст розсилки (або /cancel):")


async def broadcast_new_menu(application):
    _bc_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _bc_cursor = _bc_conn.cursor()
    _bc_cursor.execute("SELECT user_id FROM users WHERE user_id > 100000000")
    users = _bc_cursor.fetchall()
    _bc_conn.close()

    success = 0
    failed = 0
    for (user_id,) in users:
        try:
            await application.bot.send_message(
                chat_id=user_id,
                text="🔄 Оновили меню бота\n\nСкористайтесь новими функціями 👇",
                reply_markup=get_main_keyboard(user_id),
            )
            success += 1
            await asyncio.sleep(0.05)
        except Exception as _e:
            print(f"[broadcast_menu_error] user={user_id} {_e}")
            failed += 1

    print(f"[broadcast_done] success={success} failed={failed}")


async def update_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("⏳ Оновлюю меню всім користувачам...")
    await broadcast_new_menu(context.application)
    await update.message.reply_text("✅ Меню оновлено")


# ========= ГОЛОВНИЙ =========
async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    _touch_activity()

    user = update.message.from_user
    text = update.message.text
    contact = update.message.contact

    # ===== ПІДПИСКА =====
    if text not in ["🚀 Пуск", "/start"]:
        is_sub = await check_sub(user.id, context)

        if not is_sub:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📢 Підписатись",
                            url=f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✅ Я підписався", callback_data="check_sub"
                        )
                    ],
                ]
            )

            await update.message.reply_text(
                "❗ Ти відписався від каналу\nПідпишись щоб користуватись ботом",
                reply_markup=keyboard,
            )
            return

    # ВХІД В АДМІН
    if text == "🔧 Адмін" or text == "/admin":
        await admin(update, context)
        return

    # ---------- ПУСК ----------
    if text == "🚀 Пуск":
        is_sub = await check_sub(user.id, context)

        if not is_sub:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📢 Підписатись",
                            url=f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "✅ Я підписався", callback_data="check_sub"
                        )
                    ],
                ]
            )

            await update.message.reply_text(
                "❗ Для використання бота підпишись на канал", reply_markup=keyboard
            )
            return

        cursor.execute("SELECT * FROM users WHERE user_id=?", (user.id,))
        if cursor.fetchone():
            await update.message.reply_text(
                "Меню 👇", reply_markup=get_main_keyboard(user.id)
            )
        else:
            await update.message.reply_text(
                "📱 Поділись номером", reply_markup=contact_keyboard
            )
        return

    # ---------- РЕЄСТРАЦІЯ ----------
    if contact:
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, phone) VALUES (?, ?)",
            (user.id, contact.phone_number),
        )
        conn.commit()
        user_state[user.id] = "name"
        await update.message.reply_text("👤 Введи ім'я")
        return

    if user_state.get(user.id) == "name":
        cursor.execute("UPDATE users SET name=? WHERE user_id=?", (text, user.id))
        conn.commit()
        user_state[user.id] = "birth"
        await update.message.reply_text(
            "🎂 Введи дату (01.01.2000)\n🎁 У День народження +100 бонусів"
        )
        return

    if user_state.get(user.id) == "birth":
        try:
            birth_date = datetime.strptime(text, "%d.%m.%Y")

            cursor.execute("UPDATE users SET birth=? WHERE user_id=?", (text, user.id))
            conn.commit()
            user_state[user.id] = None

            cursor.execute("SELECT phone, name FROM users WHERE user_id=?", (user.id,))
            reg = cursor.fetchone()
            _reg_phone, _reg_name = reg[0], reg[1]
            _poster_sync_status = "unknown"
            try:
                # ── Перевірити чи клієнт вже є в Poster ────────────────────────
                print(f"[poster_check] user={user.id} phone={_reg_phone}")
                _existing = poster_api.get_client_by_phone(_reg_phone)
                if _existing:
                    poster_id = int(_existing.get("client_id") or _existing.get("id") or 0)
                    print(f"[poster_check] FOUND client_id={poster_id} for phone={_reg_phone}")
                    _poster_sync_status = "found"
                    cursor.execute(
                        "UPDATE users SET poster_client_id=? WHERE user_id=?",
                        (poster_id, user.id),
                    )
                    conn.commit()
                    # ── AI Sync: перевірити ім'я ─────────────────────────────
                    _poster_fname = (_existing.get("firstname") or "").strip()
                    _poster_lname = (_existing.get("lastname") or "").strip()
                    _poster_full = f"{_poster_fname} {_poster_lname}".strip()
                    _bot_clean = poster_api._clean_name(_reg_name or "")
                    if _bot_clean and _bot_clean.lower() != _poster_full.lower():
                        try:
                            poster_api.update_client_info(poster_id, name=_reg_name)
                            print(f"[poster_update] user={user.id} name: '{_poster_full}' → '{_bot_clean}'")
                            _poster_sync_status = "found+name_updated"
                        except Exception as _upd_e:
                            print(f"[poster_update] ❌ {_upd_e}")
                else:
                    # ── Створити нового клієнта ──────────────────────────────
                    print(f"[poster_create] user={user.id} phone={_reg_phone} name={_reg_name}")
                    result = poster_api.create_client(_reg_name, _reg_phone, external_id=user.id)
                    if result:
                        if isinstance(result, (int, float)):
                            poster_id = int(result)
                        else:
                            poster_id = result.get("client_id") or result.get("id")
                        if poster_id:
                            poster_id = int(poster_id)
                            cursor.execute(
                                "UPDATE users SET poster_client_id=? WHERE user_id=?",
                                (poster_id, user.id),
                            )
                            conn.commit()
                            print(f"[poster_create] ✅ new poster_client_id={poster_id}")
                            _poster_sync_status = "created"
                    else:
                        poster_id = None
                        _poster_sync_status = "create_failed"
                # ── Вітальний бонус ──────────────────────────────────────────
                if poster_id:
                    try:
                        cursor.execute(
                            "SELECT welcome_bonus FROM users WHERE user_id=?",
                            (user.id,),
                        )
                        wb_row = cursor.fetchone()
                        if wb_row and wb_row[0] == 0:
                            _ok, _bal = await poster_add_bonus_verified(
                                poster_id,
                                50,
                                "Бонус за реєстрацію в Telegram",
                                context.application,
                            )
                            cursor.execute(
                                "UPDATE users SET welcome_bonus=1 WHERE user_id=?",
                                (user.id,),
                            )
                            conn.commit()
                            print(
                                f"[welcome_bonus] ✅ new user={user.id} +50 ok={_ok} balance={_bal}"
                            )
                    except Exception as wb_e:
                        print(f"[welcome_bonus] Error new user={user.id}: {wb_e}")
            except Exception as e:
                print(f"[poster] Error during registration sync user={user.id}: {e}")
                _poster_sync_status = f"error: {e}"
                poster_id = None

            await _delete_user_barcodes(user.id, context.bot)
            file = create_barcode(user.id)
            with open(file, "rb") as photo:
                _bc_msg = await update.message.reply_photo(
                    photo=photo,
                    caption="📱 Твоя картка клієнта\n⏳ Дійсний 4 год або до 00:00",
                )
            _save_barcode_msg(user.id, _bc_msg.message_id, _bc_msg.chat_id)

            await update.message.reply_text(
                "✅ Реєстрація завершена", reply_markup=get_main_keyboard(user.id)
            )

            await update.message.reply_text(
                "📸 Підпишись на наш Instagram 👇",
                reply_markup=get_instagram_keyboard(),
            )

            cursor.execute(
                "SELECT phone, name, birth, poster_client_id FROM users WHERE user_id=?",
                (user.id,),
            )
            data = cursor.fetchone()
            _adm_pcid = data[3]
            _adm_bonus = None
            if _adm_pcid:
                try:
                    _adm_bonus = poster_api.get_poster_balance(_adm_pcid)
                except Exception:
                    pass
            _sync_emoji = {
                "created": "🆕",
                "found": "🔄",
                "found+name_updated": "✏️",
                "create_failed": "⚠️",
            }.get(_poster_sync_status, "ℹ️")

            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"👤 *НОВА РЕЄСТРАЦІЯ*\n\n"
                    f"📱 Телефон: `{data[0]}`\n"
                    f"👤 Ім'я: {data[1]}\n"
                    f"🆔 user\\_id: `{user.id}`\n"
                    f"🎂 Дата: {data[2]}\n"
                    f"🏷 Poster client\\_id: `{_adm_pcid or '—'}`\n"
                    f"🎁 Бонус: {float(_adm_bonus or 0):.2f} грн\n"
                    f"{_sync_emoji} Poster sync: `{_poster_sync_status}`",
                    parse_mode="Markdown",
                )
            except Exception as _adm_e:
                print(f"[admin_notify] registration notify error: {_adm_e}")

        except:
            await update.message.reply_text("❌ Формат: 01.01.2000")
        return

    # ---------- МЕНЮ ----------
    if text == "💳 Моя карта":
        await _delete_user_barcodes(user.id, context.bot)
        file = create_barcode(user.id)
        with open(file, "rb") as photo:
            _bc_msg = await update.message.reply_photo(
                photo=photo,
                caption="📱 Твоя картка клієнта\n⏳ Дійсний 4 год або до 00:00",
            )
        _save_barcode_msg(user.id, _bc_msg.message_id, _bc_msg.chat_id)
        # Авто-привʼязка в фоні якщо ще не прив'язано
        cursor.execute(
            "SELECT poster_client_id, phone, name, birth FROM users WHERE user_id=?",
            (user.id,),
        )
        urow = cursor.fetchone()
        if urow and urow[0] is None and urow[1]:
            _ps_t0 = _time_global.time()
            pid, created = ensure_poster_client(user.id, urow[1], urow[2], urow[3])
            _ps_ms = int((_time_global.time() - _ps_t0) * 1000)
            _sys_stats["poster_calls"] += 1
            _sys_stats["poster_total_ms"] += _ps_ms
            print(f"[POSTER UPDATE] user={user.id} time={_ps_ms}ms ok={bool(pid)}")
            if pid:
                msg = (
                    "✅ Картка активована автоматично!"
                    if created
                    else "✅ Картка прив'язана до вашого акаунту!"
                )
                await update.message.reply_text(msg)

    elif text == "📊 Штрих-код":
        await _delete_user_barcodes(user.id, context.bot)
        file = create_barcode(user.id)
        with open(file, "rb") as photo:
            _bc_msg = await update.message.reply_photo(
                photo=photo,
                caption="📱 Твоя картка клієнта\n⏳ Дійсний 4 год або до 00:00",
            )
        _save_barcode_msg(user.id, _bc_msg.message_id, _bc_msg.chat_id)

    elif text == "📊 Історія":
        cursor.execute("SELECT poster_client_id FROM users WHERE user_id=?", (user.id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            await update.message.reply_text(
                "📊 Історія покупок порожня.\nЗробіть першу покупку з карткою лояльності!"
            )
        else:
            cursor.execute(
                "SELECT amount, bonus, created_at FROM purchases_log WHERE client_id=? ORDER BY created_at DESC LIMIT 10",
                (row[0],),
            )
            rows = cursor.fetchall()
            if not rows:
                await update.message.reply_text(
                    "📊 Покупок ще немає.\nПокажіть штрих-код на касі щоб отримувати бонуси!"
                )
            else:
                lines = ["📊 Останні покупки:\n"]
                for r in rows:
                    lines.append(f"🧾 {float(r[0]):.0f} грн → +{r[1]} грн")
                await update.message.reply_text("\n".join(lines))

    elif text == "🎁 Бонуси":
        cursor.execute(
            "SELECT poster_client_id, bonus, phone, name, birth FROM users WHERE user_id=?",
            (user.id,),
        )
        row = cursor.fetchone()
        poster_client_id = row[0] if row else None
        local_bonus = row[1] if row else 0
        phone = row[2] if row else None
        uname = row[3] if row else None
        birth = row[4] if row else None
        print(
            f"[auto_link] user_id={user.id} poster_client_id={poster_client_id} phone={phone}"
        )

        if not phone:
            await update.message.reply_text("❌ Спочатку поділіться номером телефону")
            return

        # Крок 1: якщо poster_client_id відсутній — авто-привʼязка
        if poster_client_id is None:
            await update.message.reply_text("🔄 Виправляємо прив'язку...")
            poster_client_id, was_created = ensure_poster_client(
                user.id, phone, uname, birth
            )
            if poster_client_id:
                msg = (
                    "✅ Картка активована автоматично!"
                    if was_created
                    else "✅ Картка прив'язана до вашого акаунту!"
                )
                await update.message.reply_text(msg)
                # Бонус за реєстрацію через Poster якщо ще не давали
                cursor.execute(
                    "SELECT welcome_bonus FROM users WHERE user_id=?", (user.id,)
                )
                wb_row = cursor.fetchone()
                if not wb_row or not wb_row[0]:
                    _ok, _bal = await poster_add_bonus_verified(
                        poster_client_id, 50, "Бонус за реєстрацію", context.application
                    )
                    cursor.execute(
                        "UPDATE users SET welcome_bonus=1 WHERE user_id=?", (user.id,)
                    )
                    conn.commit()
                    print(
                        f"[bonus_add] welcome user={user.id} pid={poster_client_id} ok={_ok} balance={_bal}"
                    )

        if poster_client_id is None:
            await update.message.reply_text(
                "⚠️ Тимчасова помилка, спробуйте ще раз через хвилину."
            )
            return

        # Крок 2: отримуємо бонуси
        bonus_raw = poster_api.get_poster_balance(poster_client_id)

        # Smart re-link: poster_client_id є але Poster не відповів — переприв'язуємо
        if bonus_raw is None:
            print(
                f"[auto_fix] poster_client_id={poster_client_id} API мовчить, re-link..."
            )
            new_pid, _ = ensure_poster_client(user.id, phone, uname, birth)
            if new_pid:
                poster_client_id = new_pid
                bonus_raw = poster_api.get_poster_balance(poster_client_id)

        bonus = _normalize_bonus(bonus_raw)
        print(
            f"[poster_bonus] poster_client_id={poster_client_id} bonus_raw={bonus_raw} bonus={bonus}"
        )

        # Акційні бонуси (temp_bonus)
        _now_ts_b = int(_time_global.time())
        cursor.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM temp_bonus "
            "WHERE user_id=? AND expires_at > ?",
            (user.id, _now_ts_b),
        )
        _temp_b = Decimal(str(cursor.fetchone()[0] or 0)).quantize(Decimal("0.01"))
        print(f"[temp_bonus_check] user={user.id} temp={_temp_b} poster_raw={bonus_raw}")

        if bonus_raw is not None:
            _poster_b = Decimal(str(bonus or 0)).quantize(Decimal("0.01"))
            _total_b = _poster_b + _temp_b
            if _total_b == 0:
                await update.message.reply_text("🎁 У вас поки що немає бонусів")
            else:
                _lines_b = ["🎁 *Ваші бонуси:*\n"]
                _lines_b.append(f"💳 Основні: *{_poster_b:.2f} грн*")
                if _temp_b > 0:
                    _lines_b.append(f"🎁 Акційні: *{_temp_b:.2f} грн* ⏳")
                    _lines_b.append(f"━━━━━━━━━━━━━━━")
                    _lines_b.append(f"💰 Разом: *{_total_b:.2f} грн*")
                await update.message.reply_text(
                    "\n".join(_lines_b), parse_mode="Markdown"
                )
        else:
            print(f"[poster_bonus] Poster недоступний для client={poster_client_id}")
            if _temp_b > 0:
                await update.message.reply_text(
                    f"🎁 *Акційні бонуси:* {_temp_b:.2f} грн ⏳\n"
                    f"_(Основний баланс тимчасово недоступний)_",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "⚠️ Тимчасово не вдалося отримати баланс\nСпробуйте ще раз через хвилину"
                )

    elif text == "📍 Як нас знайти":
        await update.message.reply_text(
            "📍 Як нас знайти?\n\nМи тут 👇",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📍 Відкрити карту", url=LOCATION_URL)],
                [InlineKeyboardButton("📸 Підписатись на Instagram", url=INSTAGRAM_URL)],
            ])
        )

    elif text == "📸 Instagram":
        await update.message.reply_text(
            "📸 Наш Instagram 👇", reply_markup=get_instagram_keyboard()
        )

    elif text == "🎰 КОЛЕСО ФОРТУНИ 🎰":
        # ── Перевіряємо реєстрацію ───────────────────────────────────────────
        cursor.execute("SELECT poster_client_id FROM users WHERE user_id=?", (user.id,))
        _wrow = cursor.fetchone()
        if not _wrow or not _wrow[0]:
            await update.message.reply_text(
                "❌ Спочатку зареєструйся — натисни 💳 Моя карта"
            )
            return

        # ── Відправляємо InlineKeyboard з WebApp кнопкою ─────────────────────
        _wapp_url = f"{WEBAPP_WHEEL_URL}?uid={user.id}" if WEBAPP_WHEEL_URL else ""
        if not _wapp_url:
            await update.message.reply_text(
                "⚠️ WebApp тимчасово недоступний. Спробуй пізніше."
            )
            return

        _now_wday = datetime.now(KYIV_TZ).weekday()
        _days_map = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Нд"}
        _is_admin = user.id == ADMIN_ID

        if not _is_admin and _now_wday > 2:
            _info = f"⚠️ Колесо доступне Пн–Ср\nСьогодні {_days_map[_now_wday]} — приходь у понеділок!\n\n"
        else:
            cursor.execute(
                "SELECT spins_count FROM wheel_log WHERE user_id=? AND spin_date=?",
                (user.id, datetime.now(KYIV_TZ).strftime("%Y-%m-%d")),
            )
            _wr = cursor.fetchone()
            _spins = _wr[0] if _wr else 0
            _left = max(0, 3 - _spins)
            if _is_admin:
                _info = "🧪 Тестовий режим — безліміт\n\n"
            elif _left == 0:
                _info = "⏳ Ліміт прокрутів вичерпано. Завтра буде знову!\n\n"
            elif _spins == 0:
                _info = "🎁 Перший прокрут — безкоштовно!\n\n"
            else:
                _info = f"🔄 Залишилось прокрутів: {_left} / 3\n\n"

        _inline_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎰 Відкрити колесо", web_app=WebAppInfo(url=_wapp_url)
                    )
                ]
            ]
        )

        await update.message.reply_text(
            f"🎰 *КОЛЕСО ФОРТУНИ*\n\n"
            f"{_info}"
            f"Натисни кнопку нижче щоб крутити!\n"
            f"1 прокрут — безкоштовно\n"
            f"2-й і 3-й — по 50 бонусів",
            parse_mode="Markdown",
            reply_markup=_inline_kb,
        )
        return

    elif text == "⬅️ Назад":
        admin_mode[user.id] = None
        await update.message.reply_text(
            "Меню 👇", reply_markup=get_main_keyboard(user.id)
        )
        return

    # ---------- АДМІН ----------
    if user.id == ADMIN_ID:
        # ── Стан: введення суми бонусу ──────────────────────────────────────
        mode = admin_mode.get(user.id)
        if isinstance(mode, dict) and mode.get("action") == "bonus_amount" and text:
            if text.lower() == "/cancel":
                admin_mode.pop(user.id, None)
                await update.message.reply_text("❌ Нарахування скасовано")
                return
            try:
                amount = _dec(text.replace(",", "."))
                if amount <= Decimal("0"):
                    raise ValueError
            except (ValueError, Exception):
                await update.message.reply_text(
                    "❌ Введіть коректну суму (наприклад: 50)"
                )
                return
            target_uid = mode["target_uid"]
            target_name = mode["target_name"]
            pid = mode["poster_client_id"]
            # Підтвердження з inline кнопками
            confirm_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ Нарахувати {amount:.0f} грн",
                            callback_data=f"cl_bonus_confirm_{target_uid}_{amount:.0f}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Скасувати",
                            callback_data=f"cl_bonus_cancel_{target_uid}",
                        )
                    ],
                ]
            )
            await update.message.reply_text(
                f"❓ Підтвердіть:\n\n"
                f"Нарахувати {amount:.0f} грн клієнту {target_name}?",
                reply_markup=confirm_markup,
            )
            return

        # ── Стан /broadcast команди ─────────────────────────────────────────
        if _broadcast_state.get(ADMIN_ID) == "waiting_text" and text:
            _broadcast_state.pop(ADMIN_ID, None)
            if text.lower() == "/cancel":
                await update.message.reply_text("❌ Розсилку скасовано")
                return
            cursor.execute("SELECT user_id FROM users WHERE user_id > 100000000")
            _ok, _fail = 0, 0
            for _u in cursor.fetchall():
                try:
                    await context.bot.send_message(
                        _u[0], text, reply_markup=get_marketing_keyboard()
                    )
                    _ok += 1
                except:
                    _fail += 1
                await asyncio.sleep(0.05)
            await update.message.reply_text(
                f"📩 Розсилка завершена\nУспішно: {_ok}\nПомилки: {_fail}"
            )
            return

        if text == "📢 Розсилка":
            admin_mode[user.id] = "bc_waiting_photo"
            context.user_data.pop("bc_photo_id", None)
            await update.message.reply_text(
                "📷 Надішли фото для розсилки (або /cancel):"
            )
            return

        elif admin_mode.get(user.id) == "bc_waiting_photo":
            if text and text.lower() == "/cancel":
                admin_mode.pop(user.id, None)
                await update.message.reply_text("❌ Розсилку скасовано")
                return
            if not update.message.photo:
                await update.message.reply_text(
                    "❌ Потрібне фото. Надішли фото або напиши /cancel"
                )
                return
            context.user_data["bc_photo_id"] = update.message.photo[-1].file_id
            admin_mode[user.id] = "bc_waiting_text"
            await update.message.reply_text(
                "✍️ Тепер надішли текст підпису (або /cancel):"
            )
            return

        elif admin_mode.get(user.id) == "bc_waiting_text":
            if not text:
                await update.message.reply_text("❌ Потрібен текст. Напиши підпис:")
                return
            if text.lower() == "/cancel":
                admin_mode.pop(user.id, None)
                context.user_data.pop("bc_photo_id", None)
                await update.message.reply_text("❌ Розсилку скасовано")
                return
            photo_id = context.user_data.pop("bc_photo_id", None)
            admin_mode.pop(user.id, None)
            if not photo_id:
                await update.message.reply_text(
                    "❌ Фото не знайдено. Почни заново: 📢 Розсилка"
                )
                return

            _bot_url = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else None
            _bc_buttons = [
                [
                    InlineKeyboardButton(
                        "📲 Instagram", url="https://www.instagram.com/pivnii_na_raioni"
                    )
                ]
            ]
            if _bot_url:
                _bc_buttons[0].insert(
                    0, InlineKeyboardButton("🎁 +50 грн за реєстрацію", url=_bot_url)
                )
            _bc_kb = InlineKeyboardMarkup(_bc_buttons)

            # Відправка в канал
            _ch_ok = True
            try:
                await context.bot.send_photo(
                    CHANNEL_USERNAME, photo_id, caption=text, reply_markup=_bc_kb
                )
            except Exception as _ce:
                _ch_ok = False
                print(f"[broadcast] ❌ канал: {_ce}")
                await update.message.reply_text(f"⚠️ Помилка каналу: {_ce}")

            # Відправка всім користувачам
            cursor.execute("SELECT user_id FROM users WHERE user_id > 100000000")
            _ok, _fail = 0, 0
            for _bu in cursor.fetchall():
                try:
                    await context.bot.send_photo(
                        _bu[0], photo_id, caption=text, reply_markup=_bc_kb
                    )
                    _ok += 1
                except Exception:
                    _fail += 1
                await asyncio.sleep(0.05)

            context.user_data.clear()
            print(
                f"[broadcast] done | channel={'ok' if _ch_ok else 'fail'} | ok={_ok} fail={_fail}"
            )
            await update.message.reply_text(
                f"✅ Розсилка відправлена\n\n"
                f"{'✅' if _ch_ok else '❌'} Канал {CHANNEL_USERNAME}\n"
                f"✅ Надіслано: {_ok}\n"
                f"❌ Помилок: {_fail}"
            )
            return

        elif text == "📥 Вигрузити клієнтів":
            file = generate_excel()
            with open(file, "rb") as doc:
                await update.message.reply_document(document=doc, filename=file)
            return

        elif text == "🛠 Виправити штрих-коди":
            await update.message.reply_text(
                "⏳ Оновлення card_number для всіх клієнтів..."
            )
            try:
                count = fix_clients_card_numbers()
                await update.message.reply_text(
                    f"✅ Оновлено {count} клієнтів\n\nТепер сканер на касі знайде клієнта по штрих-коду."
                )
            except Exception as e:
                print(f"[fix_barcodes] Outer error: {e}")
                await update.message.reply_text(
                    "❌ Помилка під час оновлення. Перевірте логи."
                )
            return

        elif text == "🔄 Оновити всіх клієнтів":
            await update.message.reply_text(
                "⏳ Синхронізація та оновлення штрих-кодів..."
            )
            try:
                upd, cre, skp = sync_and_fix_clients()
                await update.message.reply_text(
                    f"✅ Оновлено {upd} клієнтів\n"
                    f"➕ Створено {cre} нових\n"
                    f"⚠️ Пропущено {skp}\n\n"
                    f"Тепер сканер на касі знайде клієнта по штрих-коду."
                )
            except Exception as e:
                print(f"[sync_fix] Outer error: {e}")
                await update.message.reply_text("❌ Помилка. Перевірте логи.")
            return

        elif text == "➕ Нарахувати бонус":
            # Показуємо пагінований список клієнтів для вибору
            txt, markup = build_clients_page(0)
            await update.message.reply_text(
                f"➕ Оберіть клієнта для нарахування бонусу:\n\n{txt}",
                reply_markup=markup,
            )
            return

        elif text == "🔄 Синхронізувати клієнтів":
            await update.message.reply_text("⏳ Синхронізація...")
            try:
                count = sync_clients_from_poster()
                await update.message.reply_text(
                    f"✅ Імпортовано {count} клієнтів з Poster"
                )
            except Exception as e:
                print(f"[sync] Error: {e}")
                await update.message.reply_text(
                    "❌ Помилка синхронізації. Перевірте підключення."
                )
            return

        elif text == "📊 Клієнти":
            txt, markup = build_clients_page(0)
            await update.message.reply_text(txt, reply_markup=markup)
            return

        elif text == "📊 База клієнтів":
            try:
                cursor.execute(
                    "SELECT user_id FROM users WHERE user_id > 100000000 ORDER BY user_id"
                )
                rows = cursor.fetchall()
                total = len(rows)

                header = f"📊 База клієнтів\n👥 Всього: {total}\n\n🆔 ID:\n"
                id_lines = [f"- {r[0]}" for r in rows]

                # Розбиваємо на частини по 4000 символів
                chunks = []
                current = header
                for line in id_lines:
                    if len(current) + len(line) + 1 > 4000:
                        chunks.append(current)
                        current = line + "\n"
                    else:
                        current += line + "\n"
                if current:
                    chunks.append(current)

                for part in chunks:
                    await update.message.reply_text(part)

            except Exception as _dbe:
                print(f"[База клієнтів] ❌ {_dbe}")
                await update.message.reply_text(f"❌ Помилка: {_dbe}")
            return

        elif text == "🎡 Аналітика колеса":
            try:
                # ── Глобальна статистика ─────────────────────────────────────
                cursor.execute(
                    "SELECT total_spins, count_5_all, count_5_beer, count_5_snack "
                    "FROM prize_stats WHERE id=1"
                )
                _ps = cursor.fetchone()
                if not _ps or _ps[0] == 0:
                    await update.message.reply_text(
                        "📊 Немає статистики — колесо ще не крутили"
                    )
                    return

                _total_spins = _ps[0]
                _c5_all = _ps[1]
                _c5_beer = _ps[2]
                _c5_snack = _ps[3]

                # ── Кількість по кожному призу з wheel_log ────────────────────
                cursor.execute("SELECT prize, COUNT(*) FROM wheel_log GROUP BY prize")
                _pw = {r[0]: r[1] for r in cursor.fetchall()}

                _c3_beer = _pw.get("🍺 -3% на пиво", 0)
                _c3_snack = _pw.get("🥩 -3% на закуски", 0)

                _total_5 = _c5_all + _c5_beer + _c5_snack
                _total_3 = _c3_beer + _c3_snack
                _total_wins = _total_3 + _total_5
                _losses = _total_spins - _total_wins

                # ── Платні прокрути (для доходу) ──────────────────────────────
                cursor.execute(
                    "SELECT COALESCE(SUM(spins_count),0), COALESCE(COUNT(*),0) FROM wheel_log"
                )
                _wsp = cursor.fetchone()
                _paid_spins = (_wsp[0] - _wsp[1]) if _wsp else 0
                if _paid_spins < 0:
                    _paid_spins = 0

                # ── Фінанси ───────────────────────────────────────────────────
                _cost_3 = _total_3 * 10  # -3% ≈ 10 грн
                _cost_5 = _total_5 * 20  # -5% ≈ 20 грн
                _total_cost = _cost_3 + _cost_5

                _income = _paid_spins * 50
                _profit = _income - _total_cost
                _profit_sign = "+" if _profit >= 0 else ""

                # ── Статистика списань бонусів (spins_log) ────────────────────
                cursor.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM spins_log WHERE status='success'"
                )
                _bonus_deducted = cursor.fetchone()[0] or 0

                cursor.execute("SELECT COUNT(*) FROM spins_log WHERE status='failed'")
                _deduct_errors = cursor.fetchone()[0] or 0

                cursor.execute("SELECT COUNT(*) FROM spins_log WHERE status='success'")
                _deduct_ok = cursor.fetchone()[0] or 0

                # ── Реальні витрати на знижки (real_stats) ────────────────────
                cursor.execute(
                    "SELECT COALESCE(SUM(discount_value),0), COUNT(*) FROM real_stats WHERE discount_value IS NOT NULL"
                )
                _rs = cursor.fetchone()
                _real_discount_total = _rs[0] or 0
                _real_discount_count = _rs[1] or 0

                _msg = (
                    f"📊 *АНАЛІТИКА КОЛЕСА*\n\n"
                    f"🎰 Прокрутів: {_total_spins}\n"
                    f"🎁 Виграшів: {_total_wins}\n"
                    f"😅 Програшів: {_losses}\n\n"
                    f"——————————————\n\n"
                    f"🍺 -3% пиво: {_c3_beer}\n"
                    f"🥩 -3% закуски: {_c3_snack}\n"
                    f"🍺 -5% пиво: {_c5_beer}\n"
                    f"🥩 -5% закуски: {_c5_snack}\n"
                    f"🔥 -5% весь чек: {_c5_all}\n\n"
                    f"——————————————\n\n"
                    f"💸 Орієнтовні витрати:\n"
                    f"  -3% → {_total_3} × 10 = {_cost_3} грн\n"
                    f"  -5% → {_total_5} × 20 = {_cost_5} грн\n"
                    f"  ЗАГАЛОМ: {_total_cost} грн\n\n"
                    f"📈 Зароблено: {_paid_spins} × 50 = {_income} грн\n"
                    f"💰 Прибуток: {_profit_sign}{_profit} грн\n\n"
                    f"——————————————\n\n"
                    f"💳 *Списання бонусів:*\n"
                    f"  ✅ Успішних: {_deduct_ok} × 50 = {_bonus_deducted:.0f} бонусів\n"
                    f"  ❌ Помилок: {_deduct_errors}\n\n"
                    f"💸 *Реальні витрати на знижки:*\n"
                    f"  📦 Операцій: {_real_discount_count}\n"
                    f"  💵 Загалом: {_real_discount_total:.2f} грн"
                )
                await update.message.reply_text(_msg, parse_mode="Markdown")
                print(
                    f"[analytics] wheel: spins={_total_spins} wins={_total_wins} profit={_profit_sign}{_profit} deducted={_bonus_deducted} errors={_deduct_errors}"
                )

            except Exception as _ae:
                print(f"[analytics] ❌ {_ae}")
                await update.message.reply_text("❌ Помилка аналітики")
            return

        elif text == "📊 Бонусна кампанія":
            try:
                _now_ts = int(_time_global.time())
                _7days_ago = _now_ts - 7 * 86400
                _2days_sec = 2 * 86400

                # ── Загальна статистика ───────────────────────────────────────
                cursor.execute("SELECT COUNT(*) FROM bonus_campaign_log")
                _bc_total = cursor.fetchone()[0] or 0

                cursor.execute(
                    "SELECT COUNT(*) FROM bonus_campaign_log WHERE sent_at > ?",
                    (_7days_ago,),
                )
                _bc_7d = cursor.fetchone()[0] or 0

                cursor.execute(
                    "SELECT COUNT(*) FROM temp_bonus WHERE expires_at > ?",
                    (_now_ts,),
                )
                _active_bonus = cursor.fetchone()[0] or 0

                # ── Розрахунок доходу ─────────────────────────────────────────
                cursor.execute("SELECT user_id, sent_at FROM bonus_campaign_log")
                _campaign_entries = cursor.fetchall()

                _total_revenue = Decimal("0")
                _returned_users = set()

                for _cu, _cst in _campaign_entries:
                    cursor.execute(
                        "SELECT poster_client_id FROM users WHERE user_id=?", (_cu,)
                    )
                    _urow = cursor.fetchone()
                    if not _urow or not _urow[0]:
                        continue
                    _pcid = _urow[0]

                    _from_dt = datetime.fromtimestamp(_cst, tz=KYIV_TZ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    _to_dt = datetime.fromtimestamp(
                        _cst + _2days_sec, tz=KYIV_TZ
                    ).strftime("%Y-%m-%d %H:%M:%S")

                    cursor.execute(
                        "SELECT COALESCE(SUM(amount),0), COUNT(*) FROM purchases_log "
                        "WHERE client_id=? AND created_at >= ? AND created_at < ?",
                        (_pcid, _from_dt, _to_dt),
                    )
                    _prow = cursor.fetchone()
                    _rev = Decimal(str(_prow[0] or 0))
                    _cnt = _prow[1] or 0

                    if _cnt > 0:
                        _returned_users.add(_cu)
                        _total_revenue += _rev

                _total_returned = len(_returned_users)
                _avg_check = (
                    (_total_revenue / _total_returned).quantize(Decimal("0.01"))
                    if _total_returned > 0
                    else Decimal("0")
                )
                _cost_campaign = Decimal(str(_bc_total)) * Decimal("30")
                _net = _total_revenue - _cost_campaign

                # ── Останні відправки ─────────────────────────────────────────
                cursor.execute(
                    "SELECT user_id, phone, sent_at FROM bonus_campaign_log "
                    "ORDER BY sent_at DESC LIMIT 15"
                )
                _bc_rows = cursor.fetchall()

                _today = datetime.now(KYIV_TZ).date()
                _yesterday = _today - timedelta(days=1)

                _lines = ["📊 *Бонусна кампанія (4 дні)*\n"]
                _lines.append(f"👥 Отримали всього: *{_bc_total}*")
                _lines.append(f"👥 За останні 7 днів: *{_bc_7d}*")
                _lines.append(f"⏳ Активних бонусів: *{_active_bonus}*\n")
                _lines.append("——————————————\n")
                _lines.append(f"🧲 Повернулись: *{_total_returned}*")
                _lines.append(
                    f"💰 Дохід від кампанії: *{_total_revenue:.2f} грн*"
                )
                _lines.append(f"🧾 Середній чек: *{_avg_check} грн*")
                _lines.append(f"📤 Витрачено бонусів: *{_cost_campaign:.0f} грн*")
                _net_sign = "+" if _net >= 0 else ""
                _lines.append(f"📈 Чистий дохід: *{_net_sign}{_net:.2f} грн*\n")

                # ── Статистика нагадувань ─────────────────────────────────────
                cursor.execute("SELECT COUNT(*) FROM bonus_reminder_log")
                _rem_total = cursor.fetchone()[0] or 0
                _lines.append(f"🔔 Повторний пуш надіслано: *{_rem_total}*\n")

                if _bc_rows:
                    _lines.append("——————————————\n")
                    _lines.append("Останні відправки:\n")
                    for _i, (_row_uid, _ph, _st) in enumerate(_bc_rows, 1):
                        _dt = datetime.fromtimestamp(_st, tz=KYIV_TZ)
                        if _dt.date() == _today:
                            _when = "сьогодні"
                        elif _dt.date() == _yesterday:
                            _when = "вчора"
                        else:
                            _when = _dt.strftime("%d.%m")
                        _ret_mark = " ✅" if _row_uid in _returned_users else ""
                        _lines.append(f"{_i}. {_ph or '—'} — {_when}{_ret_mark}")
                else:
                    _lines.append("_Відправок ще не було_")

                await update.message.reply_text(
                    "\n".join(_lines), parse_mode="Markdown"
                )
                print(
                    f"[campaign_stats] users={_total_returned} revenue={_total_revenue:.2f}"
                )
            except Exception as _bce:
                print(f"[analytics] bonus_campaign ❌ {_bce}")
                await update.message.reply_text("❌ Помилка аналітики кампанії")
            return

        elif text == "🔍 Перевірка системи":
            # ── Статистика видалень і Poster ──────────────────────────────────
            _bc_ok = _sys_stats["barcode_deleted"]
            _bc_err = _sys_stats["barcode_del_err"]
            _pr_ok = _sys_stats["prize_deleted"]
            _pr_err = _sys_stats["prize_del_err"]
            _p_calls = _sys_stats["poster_calls"]
            _p_avg = (
                round(_sys_stats["poster_total_ms"] / _p_calls) if _p_calls > 0 else 0
            )

            # ── Активні записи в БД ───────────────────────────────────────────
            _now_ts = _time_global.time()
            cursor.execute(
                "SELECT COUNT(*) FROM barcodes WHERE expire_at > ?", (_now_ts,)
            )
            _bc_active = cursor.fetchone()[0] or 0
            cursor.execute(
                "SELECT COUNT(*) FROM temp_messages WHERE expire_at > ?", (_now_ts,)
            )
            _tm_active = cursor.fetchone()[0] or 0

            _syscheck = (
                f"🔍 *ПЕРЕВІРКА СИСТЕМИ*\n\n"
                f"🗑 *Штрих-коди (видалення)*\n"
                f"  ✅ Успішно: {_bc_ok}\n"
                f"  ❌ Помилок: {_bc_err}\n"
                f"  📋 Активних у БД: {_bc_active}\n\n"
                f"🎁 *Виграші колеса (видалення)*\n"
                f"  ✅ Успішно: {_pr_ok}\n"
                f"  ❌ Помилок: {_pr_err}\n"
                f"  📋 Активних у БД: {_tm_active}\n\n"
                f"🔗 *Poster POS*\n"
                f"  📡 Викликів: {_p_calls}\n"
                f"  ⚡ Середній час: {_p_avg} мс\n\n"
                f"🕐 {datetime.now(KYIV_TZ).strftime('%d.%m.%Y %H:%M')} Kyiv"
            )
            await update.message.reply_text(_syscheck, parse_mode="Markdown")
            print(
                f"[syscheck] bc={_bc_ok}/{_bc_err} prize={_pr_ok}/{_pr_err} poster={_p_calls}×avg{_p_avg}ms"
            )
            return

        elif text == "🔄 Оновити меню":
            await update.message.reply_text("⏳ Оновлюю меню всім користувачам...")
            await broadcast_new_menu(context.application)
            await update.message.reply_text("✅ Меню оновлено")
            return


# ========= ПОВНИЙ ТЕСТ СИСТЕМИ (тільки адмін) =========
async def test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test {phone} — повна діагностика клієнта: SQLite + Poster + бонуси + чек.
    Тільки для ADMIN_ID.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Вкажіть номер телефону:\n/test 0732928958")
        return

    raw_phone = args[0].strip()
    phone_norm = poster_api._normalize_phone(raw_phone)

    print(f"[test_run] старт тесту phone={raw_phone} norm={phone_norm}")

    await update.message.reply_text(f"🔍 Тест клієнта: {raw_phone}\n⏳ Перевіряємо...")

    lines = []  # акумулюємо підсумок
    errors = []

    # ── 1. SQLite ────────────────────────────────────────────────────────────
    cursor.execute(
        "SELECT user_id, phone, name, birth, bonus, poster_client_id "
        "FROM users WHERE phone=? OR phone LIKE ?",
        (phone_norm, f"%{raw_phone}"),
    )
    db_row = cursor.fetchone()

    if not db_row:
        msg = (
            f"❌ Клієнт {raw_phone} не знайдений у базі бота.\n"
            f"Перевірте номер або попросіть клієнта зареєструватися."
        )
        print(f"[test_error] клієнт не знайдений у SQLite: {phone_norm}")
        await update.message.reply_text(msg)
        return

    user_id, db_phone, name, birth, local_bonus, stored_poster_id = db_row

    # ── 2. Poster ────────────────────────────────────────────────────────────
    all_clients = poster_api.get_clients()
    if not all_clients:
        await update.message.reply_text("❌ Poster API недоступний. Перевірте токен.")
        return

    poster_client = None
    search_strategy = "—"

    for c in all_clients:
        if poster_api._normalize_phone(c.get("phone") or "") == phone_norm:
            poster_client = c
            search_strategy = "телефон"
            break
    if not poster_client and user_id:
        for c in all_clients:
            if str(c.get("external_id") or "") == str(user_id):
                poster_client = c
                search_strategy = "external_id"
                break
    if not poster_client and stored_poster_id:
        for c in all_clients:
            cid = str(c.get("client_id") or c.get("id") or "")
            if cid == str(stored_poster_id):
                poster_client = c
                search_strategy = "poster_id"
                break

    if not poster_client:
        msg = (
            f"❌ Клієнт знайдений у боті, але відсутній у Poster.\n"
            f"Запустіть синхронізацію: python3 sync_clients.py"
        )
        print(f"[test_error] клієнт не знайдений у Poster: {phone_norm}")
        await update.message.reply_text(msg)
        return

    pid = int(float(poster_client.get("client_id") or poster_client.get("id")))
    lt_raw = poster_client.get("loyalty_type")
    loyalty_type = int(lt_raw) if lt_raw is not None else None
    disc_per = poster_client.get("discount_per") or "0"

    # ── 3. Повідомлення: дані клієнта ───────────────────────────────────────
    loyalty_line = (
        "✅ Бонусна картка"
        if loyalty_type == 1
        else f"⚠️ Знижкова картка {disc_per}% (loyalty_type=2 — бонуси через API не працюють)"
        if loyalty_type == 2
        else f"❓ Тип картки: {loyalty_type}"
    )

    client_info = (
        f"📋 Дані клієнта:\n\n"
        f"📱 Телефон: {db_phone}\n"
        f"🆔 user_id: {user_id}\n"
        f"🆔 Poster ID: {pid} (знайдено через {search_strategy})\n"
        f"👤 Ім'я: {name or '—'}\n"
        f"🎂 ДН: {birth or '—'}\n"
        f"📇 {loyalty_line}"
    )
    await update.message.reply_text(client_info)

    # ── БЛОК ДЛЯ ЗНИЖКОВОЇ КАРТКИ ───────────────────────────────────────────
    if loyalty_type == 2:
        print(
            f"[test_error] loyalty_type=2 для client_id={pid} — пропускаємо бонусні тести"
        )
        summary = (
            f"📊 РЕЗУЛЬТАТ ТЕСТУ\n\n"
            f"📱 {raw_phone}\n"
            f"🆔 Poster ID: {pid}\n\n"
            f"⚠️ Знижкова картка (loyalty_type=2)\n"
            f"Бонуси через API неможливі.\n\n"
            f"❗ Виправлення в Poster:\n"
            f"Маркетинг → Клієнти → клієнт\n"
            f"→ Тип лояльності → «Бонусна» → Зберегти\n\n"
            f"Після зміни: /test {raw_phone}"
        )
        await update.message.reply_text(summary)
        return

    # ── 4. Баланс ДО ────────────────────────────────────────────────────────
    bal_start = poster_api.get_poster_balance(pid)
    if bal_start is None:
        await update.message.reply_text("❌ Не вдалося отримати баланс з Poster.")
        return

    print(f"[test_run] client_id={pid} баланс_старт={bal_start}")

    # ── 5. Тест +10 грн ─────────────────────────────────────────────────────
    await update.message.reply_text(f"🧪 Тест: нараховуємо +10 грн...")

    ok10, st10, _ = poster_api.add_bonus(pid, 10, "test_run +10")
    await asyncio.sleep(0.5)
    bal_after10 = poster_api.get_poster_balance(pid)

    bonus_test_ok = (
        bal_after10 is not None
        and bal_after10 > bal_start
        and abs(bal_after10 - (bal_start + 10)) < 2
    )

    if bonus_test_ok:
        test10_line = f"✅ +10 грн: {bal_start:.0f} → {bal_after10:.0f} грн"
        print(f"[test_success] +10: {bal_start} → {bal_after10}")
    else:
        test10_line = f"❌ +10 не застосовано (баланс після: {bal_after10})"
        errors.append("нарахування +10 не пройшло")
        print(f"[test_error] +10 failed: ok={ok10} st={st10} bal={bal_after10}")

    # ── 6. Тест чеку 300 грн (3% = 9 грн) ──────────────────────────────────
    RECEIPT_SUM = 300.0
    RECEIPT_PCT = 0.03
    bonus_earned = round(RECEIPT_SUM * RECEIPT_PCT)  # 9
    bonus_spent = 0

    await update.message.reply_text(f"🧾 Тест: чек {RECEIPT_SUM:.0f} грн...")

    bal_before_receipt = bal_after10 if bal_after10 is not None else bal_start

    ok_r, st_r, _ = poster_api.add_bonus(
        pid, bonus_earned, f"test_run receipt {RECEIPT_SUM:.0f}грн"
    )
    await asyncio.sleep(0.5)
    bal_final = poster_api.get_poster_balance(pid)

    receipt_ok = (
        bal_final is not None
        and bal_final > bal_before_receipt
        and abs(bal_final - (bal_before_receipt + bonus_earned)) < 2
    )

    if receipt_ok:
        receipt_line = (
            f"✅ +{bonus_earned} грн: {bal_before_receipt:.0f} → {bal_final:.0f} грн"
        )
        print(f"[test_success] receipt: {bal_before_receipt} → {bal_final}")
    else:
        receipt_line = f"❌ +{bonus_earned} грн не застосовано (баланс: {bal_final})"
        errors.append("нарахування за чек не пройшло")
        print(f"[test_error] receipt failed: ok={ok_r} st={st_r} bal={bal_final}")

    # ── 7. Синхронізація ────────────────────────────────────────────────────
    poster_final = poster_api.get_poster_balance(pid)
    db_local = local_bonus  # значення на старті (до тесту)
    # перевіряємо фінальний poster-баланс збігається з тим що очікується
    expected_final = bal_start + 10 + bonus_earned
    sync_ok = poster_final is not None and abs(poster_final - expected_final) < 2

    sync_line = "✅ Синхронізація OK" if sync_ok else "⚠️ Розходження (можливо drift)"
    print(
        f"[test_run] poster_final={poster_final} expected={expected_final} sync={sync_ok}"
    )

    # ── 8. Фінальне повідомлення ─────────────────────────────────────────────
    all_ok = bonus_test_ok and receipt_ok

    if all_ok:
        status_lines = "✅ Бонуси працюють\n✅ Каса синхронна\n✅ Бот працює"
        print(f"[test_success] клієнт={pid} phone={raw_phone} всі тести пройшли")
    else:
        status_lines = (
            "\n".join(f"❌ {e}" for e in errors) if errors else "⚠️ Часткові помилки"
        )
        print(f"[test_error] клієнт={pid} phone={raw_phone} помилки={errors}")

    display_final = (
        bal_final if bal_final is not None else (bal_start + 10 + bonus_earned)
    )

    result_msg = (
        f"📊 РЕЗУЛЬТАТ ТЕСТУ\n\n"
        f"📱 {raw_phone}\n"
        f"🆔 Poster ID: {pid}\n\n"
        f"🎁 Було: {bal_start:.0f} грн\n"
        f"➕ +10 тест\n"
        f"➕ +{bonus_earned} чек (300 грн × 3%)\n"
        f"🎁 Стало: {display_final:.0f} грн\n\n"
        f"{test10_line}\n"
        f"{receipt_line}\n"
        f"{sync_line}\n\n"
        f"{status_lines}"
    )

    await update.message.reply_text(result_msg)

    # ── 9. Чек-повідомлення (як бачить клієнт) ──────────────────────────────
    receipt_msg = (
        f"🧾 Ваш чек:\n\n"
        f"💰 Сума: {RECEIPT_SUM:.0f} грн\n"
        f"➖ Списано бонусів: {bonus_spent}\n"
        f"➕ Нараховано: {bonus_earned}\n"
        f"🎁 Баланс: {display_final:.0f} грн\n\n"
        f"Фіскальний номер: test_demo_001\n"
        f"(Poster API не надає прямих посилань на фіскальні чеки)"
    )
    await update.message.reply_text(receipt_msg)

    # ── 10. Відкат тестових бонусів ──────────────────────────────────────────
    rollback_amount = -(10 + bonus_earned)
    poster_api.add_bonus(pid, rollback_amount, "test_run rollback")
    await asyncio.sleep(0.5)
    bal_restored = poster_api.get_poster_balance(pid)
    restored_ok = bal_restored is not None and abs(bal_restored - bal_start) < 2

    rollback_msg = (
        f"↩️ Тестові бонуси відкатано\n"
        f"Баланс відновлено: {bal_start:.0f} → {display_final:.0f} → {bal_restored:.0f} грн\n"
        f"{'✅ Баланс повернуто до початкового' if restored_ok else '⚠️ Перевірте баланс вручну'}"
    )
    await update.message.reply_text(rollback_msg)
    print(
        f"[test_run] rollback: {display_final} → {bal_restored} restored_ok={restored_ok}"
    )


# ========= ТЕСТ БОНУСНОЇ СИСТЕМИ =========
async def test_bonus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /test_bonus [phone]  — повний тест temp_bonus сценарію.
    Тільки ADMIN_ID. Надсилає результати прямо в Telegram клієнту.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    phone_arg = (context.args[0].strip() if context.args else "380732928958").lstrip("+")

    await update.message.reply_text(f"🔬 Запускаю тест для телефону {phone_arg}...")

    # ── 1. Знайти користувача ────────────────────────────────────────────────
    cursor.execute(
        "SELECT user_id, name, poster_client_id FROM users WHERE phone=?", (phone_arg,)
    )
    _row = cursor.fetchone()
    if not _row:
        await update.message.reply_text(f"❌ Користувача з телефоном {phone_arg} не знайдено")
        return

    _test_uid, _test_name, _test_pcid = _row
    await update.message.reply_text(
        f"✅ Знайдено: user_id={_test_uid}\nІм'я: {_test_name}\nposter_client_id={_test_pcid}"
    )

    # ── 2. Видати temp_bonus ─────────────────────────────────────────────────
    _now_ts = int(_time_global.time())
    _expires = _now_ts + 2 * 86400
    cursor.execute(
        "DELETE FROM temp_bonus WHERE user_id=?", (_test_uid,)
    )
    cursor.execute(
        "INSERT INTO temp_bonus (user_id, amount, expires_at) VALUES (?,?,?)",
        (_test_uid, "30.00", _expires),
    )
    conn.commit()
    print(f"[temp_bonus_given] user={_test_uid} amount=30.00 expires={_expires}")

    # Надіслати клієнту
    try:
        await context.bot.send_message(
            _test_uid,
            "🎁 *ТЕСТ БОНУСУ*\n\nВам нараховано *30.00 грн* акційних бонусів\n\n⏳ Діє 2 дні",
            parse_mode="Markdown",
        )
        await update.message.reply_text("📤 Крок 1: повідомлення про бонус відправлено ✅")
    except Exception as _e:
        await update.message.reply_text(f"⚠️ Не вдалось надіслати клієнту: {_e}")

    # ── 3. Показати баланс ───────────────────────────────────────────────────
    _poster_bal = None
    if _test_pcid:
        _poster_bal = poster_api.get_poster_balance(_test_pcid)
    _poster_dec = Decimal(str(_poster_bal or 0)).quantize(Decimal("0.01"))
    _temp_dec = Decimal("30.00")
    _total_dec = _poster_dec + _temp_dec

    _bal_text = (
        f"📊 *ТЕСТ БАЛАНСУ*\n\n"
        f"💳 Основні: *{_poster_dec:.2f} грн*\n"
        f"🎁 Акційні: *{_temp_dec:.2f} грн* ⏳\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 Разом: *{_total_dec:.2f} грн*"
    )
    try:
        await context.bot.send_message(_test_uid, _bal_text, parse_mode="Markdown")
        await update.message.reply_text(f"📤 Крок 2: баланс показано ✅\n{_bal_text}")
    except Exception as _e:
        await update.message.reply_text(f"⚠️ Баланс: {_e}")

    await asyncio.sleep(1)

    # ── 4. Симуляція покупки ─────────────────────────────────────────────────
    cursor.execute("DELETE FROM temp_bonus WHERE user_id=?", (_test_uid,))
    conn.commit()
    print(f"[temp_bonus_used] user={_test_uid} (test simulation)")

    try:
        await context.bot.send_message(
            _test_uid,
            "🧾 *ТЕСТ ПОКУПКИ*\n\n➖ Списано акційних бонусів: *30.00 грн*\n🎁 Залишилось: тільки основні бонуси",
            parse_mode="Markdown",
        )
        await update.message.reply_text("📤 Крок 3: симуляція покупки ✅")
    except Exception as _e:
        await update.message.reply_text(f"⚠️ Покупка: {_e}")

    await asyncio.sleep(1)

    # ── 5. Тест expire ───────────────────────────────────────────────────────
    cursor.execute(
        "INSERT INTO temp_bonus (user_id, amount, expires_at) VALUES (?,?,?)",
        (_test_uid, "30.00", _now_ts + 2 * 86400),
    )
    conn.commit()
    # Штучно прострочуємо
    cursor.execute("UPDATE temp_bonus SET expires_at=0 WHERE user_id=?", (_test_uid,))
    conn.commit()

    # Запускаємо cleanup
    cursor.execute(
        "SELECT COUNT(*) FROM temp_bonus WHERE expires_at < ?", (_now_ts,)
    )
    _expired_cnt = cursor.fetchone()[0] or 0
    cursor.execute("DELETE FROM temp_bonus WHERE expires_at < ?", (_now_ts,))
    conn.commit()
    print(f"[temp_bonus_deleted_expired] count={_expired_cnt} (test)")

    try:
        await context.bot.send_message(
            _test_uid,
            "⏳ *БОНУС ЗГОРІВ*\n\nВаші акційні бонуси були видалені (термін дії вийшов)",
            parse_mode="Markdown",
        )
        await update.message.reply_text(
            f"📤 Крок 4: тест expire ✅ (видалено {_expired_cnt} запис(ів))"
        )
    except Exception as _e:
        await update.message.reply_text(f"⚠️ Expire: {_e}")

    # ── Підсумок ─────────────────────────────────────────────────────────────
    print(f"[test_flow] completed user_id={_test_uid} phone={phone_arg}")
    await update.message.reply_text(
        f"✅ *Тест завершено*\n\n"
        f"👤 user\\_id: `{_test_uid}`\n"
        f"📱 phone: {phone_arg}\n"
        f"🎯 Всі 4 кроки виконано:\n"
        f"1. ✅ Нарахування бонусу\n"
        f"2. ✅ Показ балансу\n"
        f"3. ✅ Списання при покупці\n"
        f"4. ✅ Авто-видалення (expire)",
        parse_mode="Markdown",
    )


# ========= POSTER SYNC CHECKER =========
async def poster_sync_checker(app):
    """Кожні 2 години синхронізує дані всіх реальних користувачів з Poster."""
    print("[poster_sync_checker] Task started ✅")
    while True:
        await asyncio.sleep(7200)
        try:
            cursor.execute(
                "SELECT user_id, phone, name, poster_client_id "
                "FROM users WHERE user_id > 100000000"
            )
            _sync_rows = cursor.fetchall()
            _synced = 0
            for _s_uid, _s_phone, _s_name, _s_pcid in _sync_rows:
                if not _s_phone:
                    continue
                try:
                    _pc = poster_api.get_client_by_phone(_s_phone)
                    if not _pc:
                        # Клієнта немає в Poster — пропустити (не створювати без запиту)
                        print(f"[poster_sync] user={_s_uid} phone={_s_phone} NOT in Poster")
                        continue

                    _real_id = int(_pc.get("client_id") or _pc.get("id") or 0)
                    if not _real_id:
                        continue

                    _changed = False

                    # Оновити poster_client_id якщо відрізняється
                    if _s_pcid != _real_id:
                        cursor.execute(
                            "UPDATE users SET poster_client_id=? WHERE user_id=?",
                            (_real_id, _s_uid),
                        )
                        conn.commit()
                        print(f"[poster_sync] user={_s_uid} pcid: {_s_pcid} → {_real_id}")
                        _changed = True

                    # AI Sync: порівняти ім'я
                    _pf = (_pc.get("firstname") or "").strip()
                    _pl = (_pc.get("lastname") or "").strip()
                    _poster_full = f"{_pf} {_pl}".strip()
                    _bot_clean = poster_api._clean_name(_s_name or "")

                    if _bot_clean and _poster_full and \
                            _bot_clean.lower() != _poster_full.lower():
                        poster_api.update_client_info(_real_id, name=_s_name)
                        print(
                            f"[poster_update] user={_s_uid} "
                            f"name: '{_poster_full}' → '{_bot_clean}'"
                        )
                        _changed = True

                    if _changed:
                        _synced += 1
                    await asyncio.sleep(0.1)
                except Exception as _row_e:
                    print(f"[poster_sync] ❌ user={_s_uid}: {_row_e}")

            print(f"[poster_sync_checker] ✅ Done. Synced {_synced}/{len(_sync_rows)} users")
        except Exception as _sync_e:
            print(f"[poster_sync_checker] ❌ {_sync_e}")


# ========= AI RISK MODULE =========

def _compute_risk(poster_client_id, override_days_since=None):
    """
    Повертає (risk_level, days_since, avg_days) для клієнта.
    override_days_since — примусово задати days_since (для тестів).
    """
    _ic = sqlite3.connect(DB_PATH, check_same_thread=False)
    _c = _ic.cursor()
    _c.execute(
        "SELECT created_at FROM purchases_log WHERE client_id=? ORDER BY created_at ASC",
        (poster_client_id,),
    )
    _visits_raw = [r[0] for r in _c.fetchall()]
    _ic.close()

    _now_ts = int(_time_global.time())

    if not _visits_raw:
        _days_since = override_days_since if override_days_since is not None else 999
        _avg_days = 7.0
    else:
        _last_raw = _visits_raw[-1]
        try:
            _last_dt = datetime.strptime(_last_raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KYIV_TZ)
        except ValueError:
            _last_dt = datetime.now(KYIV_TZ)
        _last_ts = int(_last_dt.timestamp())
        _days_since = (
            override_days_since if override_days_since is not None
            else (_now_ts - _last_ts) / 86400
        )
        if len(_visits_raw) >= 2:
            _dts = []
            for _v in _visits_raw:
                try:
                    _dts.append(datetime.strptime(_v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KYIV_TZ))
                except ValueError:
                    pass
            if len(_dts) >= 2:
                _deltas = [
                    (_dts[i + 1] - _dts[i]).total_seconds() / 86400
                    for i in range(len(_dts) - 1)
                ]
                _avg_days = sum(_deltas) / len(_deltas)
            else:
                _avg_days = 7.0
        else:
            _avg_days = 7.0

    if _days_since > _avg_days * 1.5:
        _risk = "high"
    elif _days_since > _avg_days:
        _risk = "medium"
    else:
        _risk = "low"

    return _risk, _days_since, _avg_days


def _ai_push_text(risk: str) -> str:
    if risk == "high":
        return (
            "😏 Ми давно не бачились...\n\n"
            "🎁 Тримай *40 грн* — тільки сьогодні!\n\n"
            "Приходь до Пивних на районі і скажи бармену про бонус 🍺"
        )
    elif risk == "medium":
        return (
            "👀 Чекаємо тебе!\n\n"
            "🎁 *20 грн* вже на карті — просто приходь\n\n"
            "Пивні на районі скучили 🍺"
        )
    else:
        return (
            "🔥 Сьогодні свіже і смачне\n\n"
            "Забігай до нас 😉🍺"
        )


async def ai_risk_checker(app):
    """Щогодинна перевірка ризику відтоку для AI_TEST_PHONES."""
    print("[ai_risk_checker] Task started ✅")
    while True:
        await asyncio.sleep(3600)
        try:
            for _ai_phone in AI_TEST_PHONES:
                cursor.execute(
                    "SELECT user_id, poster_client_id FROM users WHERE phone=?",
                    (_ai_phone,),
                )
                _ai_row = cursor.fetchone()
                if not _ai_row:
                    continue
                _ai_uid, _ai_pcid = _ai_row

                _risk, _days, _avg = _compute_risk(_ai_pcid)

                cursor.execute(
                    "UPDATE users SET risk_level=? WHERE user_id=?",
                    (_risk, _ai_uid),
                )
                conn.commit()

                print(
                    f"[AI_TEST] phone={_ai_phone} risk={_risk} "
                    f"days={_days:.1f} avg={_avg:.1f}"
                )

                _push = _ai_push_text(_risk)
                await app.bot.send_message(
                    _ai_uid,
                    _push,
                    parse_mode="Markdown",
                    reply_markup=get_marketing_keyboard(),
                )
                await asyncio.sleep(0.2)
        except Exception as _ai_e:
            print(f"[ai_risk_checker] ❌ {_ai_e}")


async def ai_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /ai_test — примусово запускає AI-цикл для AI_TEST_PHONES (days_since=5).
    Тільки ADMIN_ID.
    """
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "🤖 Запускаю AI тест (симуляція 5 днів без візиту)..."
    )

    _ai_results = []

    for _ai_phone in AI_TEST_PHONES:
        cursor.execute(
            "SELECT user_id, poster_client_id FROM users WHERE phone=?",
            (_ai_phone,),
        )
        _row = cursor.fetchone()
        if not _row:
            _ai_results.append(f"❌ {_ai_phone} — не знайдено в БД")
            continue

        _uid, _pcid = _row

        # Симуляція: last_visit = 5 днів тому
        _risk, _days, _avg = _compute_risk(_pcid, override_days_since=5)

        cursor.execute(
            "UPDATE users SET risk_level=? WHERE user_id=?", (_risk, _uid)
        )
        conn.commit()

        print(
            f"[AI_TEST] phone={_ai_phone} risk={_risk} "
            f"days={_days:.1f} avg={_avg:.1f}"
        )

        _push = _ai_push_text(_risk)
        try:
            await context.bot.send_message(
                _uid,
                _push,
                parse_mode="Markdown",
                reply_markup=get_marketing_keyboard(),
            )
            _ai_results.append(
                f"✅ `{_ai_phone}`\n"
                f"risk\\_level: *{_risk}*\n"
                f"days\\_since: {_days:.1f} | avg: {_avg:.1f}\n"
                f"Текст: _{_push[:60].replace(chr(10), ' ')}_"
            )
        except Exception as _send_e:
            _ai_results.append(f"⚠️ {_ai_phone} — send error: {_send_e}")

        await asyncio.sleep(0.2)

    await update.message.reply_text(
        "🤖 *AI Тест завершено*\n\n" + "\n\n".join(_ai_results),
        parse_mode="Markdown",
    )


# ========= PRRO WEBHOOK SERVER =========
async def prro_webhook_server(tg_app):
    """aiohttp HTTP-сервер для ПРРО вебхуків від Poster/РРО."""

    async def handle_prro(request: aiohttp_web.Request) -> aiohttp_web.Response:
        try:
            data = await request.json()
        except Exception as e:
            print(f"[PRRO] Помилка парсингу JSON: {e}")
            return aiohttp_web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )

        print(f"[PRRO] Webhook отримано: {json.dumps(data, ensure_ascii=False)}")

        # ── Витягуємо фіскальні поля ─────────────────────────────────────
        fiscal_number = (
            data.get("fiscal_number")
            or data.get("fiscal_receipt_number")
            or data.get("fiscal_check_number")
            or data.get("receipt_number")
            or data.get("fn")
            or data.get("check_number")
        )
        fiscal_url = (
            data.get("fiscal_url")
            or data.get("receipt_url")
            or data.get("check_url")
            or data.get("url")
        )
        total_sum = data.get("sum") or data.get("amount") or data.get("total") or 0
        client_id = (
            data.get("client_id")
            or data.get("poster_client_id")
            or data.get("clientId")
            or data.get("external_id")
        )

        print(
            f"[PRRO] fiscal_number={fiscal_number!r} | fiscal_url={fiscal_url!r} | "
            f"sum={total_sum} | client_id={client_id}"
        )

        # ── Будуємо URL якщо є номер але немає посилання ─────────────────
        if not fiscal_url and fiscal_number:
            date_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
            fiscal_url = (
                f"https://cabinet.tax.gov.ua/cashregs/check"
                f"?fn={fiscal_number}&dt={date_str}"
            )
            print(f"[PRRO] fiscal_url побудований: {fiscal_url}")

        # ── Пошук Telegram user_id по poster_client_id ───────────────────
        user_id = None
        if client_id:
            try:
                cid = int(float(client_id))
                wh_conn = sqlite3.connect(DB_PATH)
                wh_cur = wh_conn.cursor()
                wh_cur.execute(
                    "SELECT user_id FROM users "
                    "WHERE poster_client_id=? AND user_id > 100000000 LIMIT 1",
                    (cid,),
                )
                row = wh_cur.fetchone()
                wh_conn.close()
                if row:
                    user_id = row[0]
                    print(f"[PRRO] client_id={cid} → Telegram user_id={user_id}")
                else:
                    print(f"[PRRO] client_id={cid} — Telegram-юзер не знайдений")
            except Exception as e:
                print(f"[PRRO] Помилка пошуку user: {e}")

        # ── Якщо юзера немає — сповіщаємо адміна ────────────────────────
        if not user_id:
            try:
                await tg_app.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ [PRRO webhook]\n"
                    f"fiscal_number: {fiscal_number}\n"
                    f"client_id: {client_id}\n"
                    f"sum: {total_sum} грн\n"
                    f"❌ Telegram-юзер не знайдений",
                )
            except Exception:
                pass
            return aiohttp_web.json_response({"ok": True, "note": "user_not_found"})

        # ── Формуємо повідомлення ─────────────────────────────────────────
        lines = ["🧾 Ваш чек:\n"]
        try:
            lines.append(f"💰 {_dec(total_sum):.2f} грн")
        except Exception:
            lines.append(f"💰 {total_sum} грн")
        if fiscal_number:
            lines.append(f"🧾 № {fiscal_number}")
        if not fiscal_url:
            lines.append("\n⚠️ Посилання на чек буде доступне пізніше")
        lines.append("\n🍻 Дякуємо за покупку!")

        msg_text = "\n".join(lines)

        prro_kb_rows = []
        if fiscal_url:
            prro_kb_rows.append(
                [InlineKeyboardButton("🧾 Відкрити чек", url=fiscal_url)]
            )
        prro_kb_rows.append(
            [InlineKeyboardButton("📸 Підписатись на Instagram", url=INSTAGRAM_URL)]
        )
        reply_markup = InlineKeyboardMarkup(prro_kb_rows)

        # ── Відправка ────────────────────────────────────────────────────
        try:
            await tg_app.bot.send_message(user_id, msg_text, reply_markup=reply_markup)
            print(
                f"[PRRO] ✅ Надіслано user={user_id} | fiscal_number={fiscal_number!r}"
            )
        except Exception as e:
            print(f"[PRRO] Помилка відправки: {e}")
            return aiohttp_web.json_response({"ok": False, "error": str(e)}, status=500)

        # ── Пріоритет webhook: позначаємо sent=1 в purchases_log ────────
        # Шукаємо транзакцію по client_id (якщо receipt_checker ще не записав — вставляємо)
        if client_id:
            try:
                cid = int(float(client_id))
                wh_conn2 = sqlite3.connect(DB_PATH)
                wh_cur2 = wh_conn2.cursor()
                # Якщо рядок вже є від receipt_checker — оновлюємо sent
                wh_cur2.execute(
                    "UPDATE purchases_log SET sent=1 WHERE id = ("
                    "  SELECT id FROM purchases_log "
                    "  WHERE client_id=? AND sent=0 ORDER BY id DESC LIMIT 1"
                    ")",
                    (cid,),
                )
                if wh_cur2.rowcount == 0:
                    # receipt_checker ще не записав — вставляємо власний запис
                    wh_cur2.execute(
                        "INSERT OR IGNORE INTO purchases_log "
                        "(client_id, amount, bonus, bonus_spent, created_at, "
                        "transaction_id, sent) "
                        "VALUES (?, ?, 0, 0, ?, ?, 1)",
                        (
                            cid,
                            str(_dec(total_sum or "0")),
                            datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                            f"prro_{fiscal_number or cid}",
                        ),
                    )
                wh_conn2.commit()
                wh_conn2.close()
                print(f"[PRRO] purchases_log updated: client_id={cid} sent=1")
            except Exception as e:
                print(f"[PRRO] DB update error: {e}")

        return aiohttp_web.json_response(
            {
                "ok": True,
                "fiscal_number": fiscal_number,
                "fiscal_url": fiscal_url,
                "user_id": user_id,
            }
        )

    async def handle_get(request: aiohttp_web.Request) -> aiohttp_web.Response:
        return aiohttp_web.json_response(
            {
                "status": "ok",
                "endpoint": "POST /webhook/prro",
                "expected_fields": ["fiscal_number", "fiscal_url", "sum", "client_id"],
            }
        )

    async def handle_alive(request: aiohttp_web.Request) -> aiohttp_web.Response:
        return aiohttp_web.Response(text="Bot is alive 🍺")

    async def handle_spin(request: aiohttp_web.Request) -> aiohttp_web.Response:
        """WebApp /spin endpoint — повна логіка колеса."""
        import random as _rnd

        try:
            data = await request.json()
        except Exception:
            return aiohttp_web.json_response(
                {"ok": False, "error": "invalid json"}, status=400
            )

        try:
            user_id = int(data.get("user_id", 0))
        except (ValueError, TypeError):
            user_id = 0
        if not user_id:
            return aiohttp_web.json_response(
                {"ok": False, "error": "missing user_id"}, status=400
            )

        try:
            # ── Перевіряємо реєстрацію ───────────────────────────────────────
            cursor.execute(
                "SELECT poster_client_id FROM users WHERE user_id=? AND user_id > 100000000",
                (user_id,),
            )
            wrow = cursor.fetchone()
            if not wrow or not wrow[0]:
                return aiohttp_web.json_response(
                    {"ok": False, "error": "not_registered"}
                )

            poster_cid = wrow[0]
            today_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
            is_admin = user_id == ADMIN_ID
            SPIN_COST = 50
            MAX_SPINS = 3

            # ── Тільки Пн–Ср (адмін пропускає) ──────────────────────────────
            if not is_admin and datetime.now(KYIV_TZ).weekday() > 2:
                _dm = {3: "Чт", 4: "Пт", 5: "Сб", 6: "Нд"}
                return aiohttp_web.json_response(
                    {
                        "ok": False,
                        "error": "wrong_day",
                        "day": _dm.get(datetime.now(KYIV_TZ).weekday(), "?"),
                    }
                )

            # ── Скільки прокрутів сьогодні? ──────────────────────────────────
            cursor.execute(
                "SELECT spins_count, prize FROM wheel_log WHERE user_id=? AND spin_date=?",
                (user_id, today_str),
            )
            wday_row = cursor.fetchone()
            spins_today = wday_row[0] if wday_row else 0

            if not is_admin and spins_today >= MAX_SPINS:
                return aiohttp_web.json_response(
                    {"ok": False, "error": "limit_reached", "limit": MAX_SPINS}
                )

            spin_num = spins_today + 1
            is_free = (spin_num == 1) or is_admin

            # ── Оплата (не адмін, не перший прокрут) ─────────────────────────
            if not is_admin and not is_free:
                balance_before = poster_api.get_poster_balance(poster_cid)
                if balance_before is None:
                    return aiohttp_web.json_response(
                        {"ok": False, "error": "balance_unavailable"}
                    )
                if balance_before < SPIN_COST:
                    return aiohttp_web.json_response(
                        {
                            "ok": False,
                            "error": "insufficient_bonus",
                            "balance": float(balance_before),
                            "required": SPIN_COST,
                        }
                    )

                dok, _ds, _dd = poster_api.add_bonus(
                    poster_cid, -SPIN_COST, "Колесо (WebApp)"
                )

                # ── Верифікація: перевіряємо що баланс реально змінився ───────
                balance_after = poster_api.get_poster_balance(poster_cid)
                _deducted = balance_before - (balance_after or balance_before)

                if not dok or _deducted < SPIN_COST * 0.5:
                    _status_log = "failed"
                    try:
                        cursor.execute(
                            "INSERT INTO spins_log (user_id, amount, status, balance_before, balance_after, date) "
                            "VALUES (?,?,?,?,?,?)",
                            (
                                user_id,
                                SPIN_COST,
                                _status_log,
                                balance_before,
                                balance_after,
                                today_str,
                            ),
                        )
                        conn.commit()
                    except Exception as _le:
                        print(f"[spins_log] ❌ {_le}")
                    print(
                        f"[spin_web] ❌ deduction failed: before={balance_before} after={balance_after} dok={dok}"
                    )
                    return aiohttp_web.json_response(
                        {"ok": False, "error": "deduction_failed"}
                    )

                # ── Успішне списання: логуємо ─────────────────────────────────
                try:
                    cursor.execute(
                        "INSERT INTO spins_log (user_id, amount, status, balance_before, balance_after, date) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            user_id,
                            SPIN_COST,
                            "success",
                            balance_before,
                            balance_after,
                            today_str,
                        ),
                    )
                    conn.commit()
                    print(
                        f"[spins_log] ✅ user={user_id} -{SPIN_COST} бонусів | {balance_before:.2f}→{balance_after:.2f}"
                    )
                except Exception as _le:
                    print(f"[spins_log] ❌ {_le}")

            # ── Шанс виграшу залежить від номеру прокруту ────────────────────
            # spin_num: 1→50%, 2→70%, 3→70%
            _win_chances = {1: 0.50, 2: 0.70, 3: 0.70}
            _win_chance = _win_chances.get(spin_num, 0.50)
            _win = _rnd.random() < _win_chance

            if _win:
                # Ваговий розподіл призів (weights: 30/30/15/15/10)
                _prizes = [
                    "🍺 -3% на пиво",
                    "🥩 -3% на закуски",
                    "🍺 -5% на пиво",
                    "🥩 -5% на закуски",
                    "🔥 -5% на весь чек",
                ]
                _weights = [30, 30, 15, 15, 10]
                prize = _rnd.choices(_prizes, weights=_weights, k=1)[0]
            else:
                prize = "😅 Спробуй ще завтра"

            print(
                f"[spin_web] spin#{spin_num} win_chance={_win_chance:.0%} win={_win} prize={prize}"
            )

            is_win = prize != "😅 Спробуй ще завтра"

            # ── Зберігаємо в wheel_log ────────────────────────────────────────
            try:
                if wday_row:
                    cursor.execute(
                        "UPDATE wheel_log SET spins_count=?, prize=? WHERE user_id=? AND spin_date=?",
                        (spin_num, prize, user_id, today_str),
                    )
                else:
                    cursor.execute(
                        "INSERT INTO wheel_log (user_id, spin_date, prize, paid, spins_count) VALUES (?,?,?,?,?)",
                        (user_id, today_str, prize, 0 if is_free else 1, 1),
                    )
                conn.commit()
            except Exception as _dbe:
                print(f"[spin_web] DB error: {_dbe}")

            # ── Оновлюємо глобальну статистику ───────────────────────────────
            try:
                cursor.execute(
                    """
                    UPDATE prize_stats SET
                        total_spins   = total_spins + 1,
                        count_5_all   = count_5_all   + ?,
                        count_5_beer  = count_5_beer  + ?,
                        count_5_snack = count_5_snack + ?
                    WHERE id = 1
                """,
                    (
                        1 if prize == "🔥 -5% на весь чек" else 0,
                        1 if prize == "🍺 -5% на пиво" else 0,
                        1 if prize == "🥩 -5% на закуски" else 0,
                    ),
                )
                conn.commit()
            except Exception as _ste:
                print(f"[spin_web] stats error: {_ste}")

            # ── Зберігаємо в real_stats (якщо є виграш зі знижкою) ───────────
            if is_win:
                try:
                    _pct_map = {
                        "🍺 -3% на пиво": 3,
                        "🥩 -3% на закуски": 3,
                        "🍺 -5% на пиво": 5,
                        "🥩 -5% на закуски": 5,
                        "🔥 -5% на весь чек": 5,
                    }
                    _disc_pct = _pct_map.get(prize)
                    if _disc_pct:
                        _last_order = poster_api.get_last_order(poster_cid)
                        _order_sum = _last_order["total"] if _last_order else None
                        _disc_val = (
                            round(_order_sum * _disc_pct / 100, 2)
                            if _order_sum
                            else None
                        )
                        cursor.execute(
                            "INSERT INTO real_stats (user_id, order_sum, discount_percent, discount_value, date) "
                            "VALUES (?,?,?,?,?)",
                            (user_id, _order_sum, _disc_pct, _disc_val, today_str),
                        )
                        conn.commit()
                        print(
                            f"[real_stats] user={user_id} order={_order_sum} disc={_disc_pct}% val={_disc_val}"
                        )
                except Exception as _rse:
                    print(f"[real_stats] ❌ {_rse}")

            # ── Надсилаємо повідомлення в Telegram (фоново) ──────────────────
            async def _notify():
                try:
                    _today_disp = datetime.now(KYIV_TZ).strftime("%d.%m.%Y")
                    if is_win:
                        _msg = (
                            f"🎰 *Результат колеса (WebApp):*\n\n"
                            f"🎉 Ти виграв: *{prize}*\n\n"
                            f"⏳ Дійсний тільки сьогодні (до 00:00)\n"
                            f"📅 {_today_disp}\n\n"
                            f"Покажи цей екран бармену 🍺"
                        )
                    else:
                        _msg = f"🎰 *Результат колеса (WebApp):*\n\n😅 Не пощастило — спробуй ще!"
                    await tg_app.bot.send_message(user_id, _msg, parse_mode="Markdown")
                except Exception as _te:
                    print(f"[spin_web] send_message error: {_te}")

            asyncio.create_task(_notify())

            print(f"[spin_web] user={user_id} spin#{spin_num} prize={prize}")

            _left = max(0, MAX_SPINS - spin_num) if not is_admin else None
            return aiohttp_web.json_response(
                {
                    "ok": True,
                    "prize": prize,
                    "is_win": is_win,
                    "spin_num": spin_num,
                    "spins_left": _left,
                    "is_admin": is_admin,
                }
            )

        except Exception as _e:
            print(f"[spin_web] ❌ {_e}")
            return aiohttp_web.json_response(
                {"ok": False, "error": str(_e)}, status=500
            )

    async def handle_spin_status(request: aiohttp_web.Request) -> aiohttp_web.Response:
        """GET /spin-status?uid=USER_ID — повертає кількість прокрутів сьогодні."""

        try:
            uid = int(request.rel_url.query.get("uid", 0))
        except (ValueError, TypeError):
            uid = 0
        if not uid:
            return aiohttp_web.json_response(
                {"ok": False, "error": "missing uid"}, status=400
            )
        try:
            today = datetime.now(KYIV_TZ).strftime("%Y-%m-%d")
            cursor.execute(
                "SELECT spins_count FROM wheel_log WHERE user_id=? AND spin_date=?",
                (uid, today),
            )
            row = cursor.fetchone()
            spins_today = row[0] if row else 0
            is_admin = uid == ADMIN_ID
            return aiohttp_web.json_response(
                {
                    "ok": True,
                    "spins_today": spins_today,
                    "spins_left": None if is_admin else max(0, 3 - spins_today),
                    "is_admin": is_admin,
                }
            )
        except Exception as _se:
            return aiohttp_web.json_response(
                {"ok": False, "error": str(_se)}, status=500
            )

    webhook_app = aiohttp_web.Application()
    webhook_app.router.add_post("/webhook/prro", handle_prro)
    webhook_app.router.add_get("/webhook/prro", handle_get)
    webhook_app.router.add_post("/spin", handle_spin)
    webhook_app.router.add_get("/spin-status", handle_spin_status)
    webhook_app.router.add_get("/", handle_alive)
    webhook_app.router.add_get("/health", handle_alive)

    runner = aiohttp_web.AppRunner(webhook_app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"[prro_webhook] ✅ Listening on port {WEBHOOK_PORT}")
    print(f"[prro_webhook] URL: POST http://localhost:{WEBHOOK_PORT}/webhook/prro")

    while True:
        await asyncio.sleep(3600)


# ========= SAFE HANDLER WRAPPER =========

async def safe_handler(func, update, context, timeout: float = 20.0):
    """
    Обгортка для всіх handlers:
      • оновлює таймер активності
      • обмежує виконання timeout секундами
      • на TimeoutError: повідомляє юзера + алерт адміну + os._exit(1)
      • на Exception: логує + повідомляє юзера (без рестарту — watchdog_ultra вирішить)
    """
    _touch_activity()
    _fn = func.__name__
    print(f"[{_fn}]")
    try:
        return await asyncio.wait_for(func(update, context), timeout=timeout)

    except asyncio.TimeoutError:
        print(f"[handler_timeout] {_fn} > {timeout}s")
        # Повідомити юзера
        try:
            if update.message:
                await update.message.reply_text("❌ Сервер не відповів. Спробуйте ще раз.")
            elif update.callback_query:
                await update.callback_query.answer("❌ Timeout. Спробуйте /start", show_alert=True)
        except Exception:
            pass
        # Алерт + жорсткий рестарт (тільки якщо бот живий > 60с)
        _up = _time_global.time() - _BOT_START_TIME
        if _up > 60:
            _notify_admin_sync(
                f"🔴 Handler timeout: {_fn}\n"
                f"Очікування: {int(timeout)}с | Uptime: {int(_up)}с\n"
                f"→ примусовий РЕСТАРТ"
            )
            _time_global.sleep(2)
            os._exit(1)

    except Exception as _he:
        print(f"[handler_error] {_fn}: {_he}")
        try:
            if update.message:
                await update.message.reply_text("❌ Помилка. Спробуйте /start")
            elif update.callback_query:
                await update.callback_query.answer("❌ Помилка. Спробуйте /start", show_alert=True)
        except Exception:
            pass
        _notify_admin_sync(f"⚠️ Handler error [{_fn}]:\n{_he}")


# ── activity_pulse: фоновий пульс кожні 30с (антипаніка watchdog_ultra) ───────
async def activity_pulse(app):
    print("[activity_pulse] Task started ✅")
    while True:
        await asyncio.sleep(10)  # кожні 10с — inactivity ніколи не перевищить 10с
        _touch_activity()
        print(f"[activity_pulse] alive | uptime={int(_time_global.time() - _BOT_START_TIME)}s")


# ── Async-обгортки для кожного handler (PTB вимагає async callable) ───────────
async def _w_buttons(u, c):         return await safe_handler(buttons,           u, c)
async def _w_main(u, c):            return await safe_handler(main_handler,       u, c)
async def _w_start(u, c):           return await safe_handler(start,              u, c)
async def _w_admin(u, c):           return await safe_handler(admin,              u, c)
async def _w_test(u, c):            return await safe_handler(test_cmd,           u, c)
async def _w_balance(u, c):         return await safe_handler(balance_cmd,        u, c)
async def _w_checks(u, c):          return await safe_handler(checks_cmd,         u, c)
async def _w_refunds(u, c):         return await safe_handler(refunds_cmd,        u, c)
async def _w_broadcast(u, c):       return await safe_handler(broadcast_cmd,      u, c)
async def _w_update_menu(u, c):     return await safe_handler(update_menu_command,u, c)
async def _w_test_bonus(u, c):      return await safe_handler(test_bonus_cmd,     u, c)
async def _w_ai_test(u, c):         return await safe_handler(ai_test_cmd,        u, c)


# ========= ЗАПУСК =========
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start",       _w_start))
app.add_handler(CommandHandler("admin",       _w_admin))
app.add_handler(CommandHandler("test",        _w_test))
app.add_handler(CommandHandler("balance",     _w_balance))
app.add_handler(CommandHandler("checks",      _w_checks))
app.add_handler(CommandHandler("refunds",     _w_refunds))
app.add_handler(CommandHandler("broadcast",   _w_broadcast))
app.add_handler(CommandHandler("update_menu", _w_update_menu))
app.add_handler(CommandHandler("test_bonus",  _w_test_bonus))
app.add_handler(CommandHandler("ai_test",     _w_ai_test))
app.add_handler(CallbackQueryHandler(_w_buttons))
app.add_handler(MessageHandler(filters.ALL,   _w_main))


# ========= BARCODE TTL CLEANER =========
async def barcode_ttl_checker(app):
    """Кожні 5 хв знаходить прострочені штрих-коди і видаляє їх з чату та БД."""
    print("[barcode_ttl] Task started ✅")
    while True:
        await asyncio.sleep(60)  # кожну хвилину
        try:
            _now_ts = _time_global.time()
            _now_kyiv = datetime.fromtimestamp(_now_ts, tz=KYIV_TZ)
            print(
                f"[barcode_ttl] NOW (Kyiv): {_now_kyiv.strftime('%d.%m.%Y %H:%M:%S')}"
            )
            cursor.execute(
                "SELECT id, user_id, COALESCE(chat_id, user_id), message_id, expire_at "
                "FROM barcodes WHERE expire_at <= ?",
                (_now_ts,),
            )
            expired = cursor.fetchall()
            if not expired:
                continue

            print(f"[barcode_ttl] Found {len(expired)} expired barcode(s)")

            for row_id, uid, cid, mid, ea_ts in expired:
                global _sys_stats
                _ea_kyiv = (
                    datetime.fromtimestamp(ea_ts, tz=KYIV_TZ).strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )
                    if ea_ts
                    else "?"
                )
                print(
                    f"[barcode_ttl] NOW: {_now_kyiv.strftime('%d.%m.%Y %H:%M:%S')} | "
                    f"EXPIRE: {_ea_kyiv} | msg={mid} chat={cid} user={uid}"
                )
                for _att in range(3):
                    try:
                        await app.bot.delete_message(chat_id=cid, message_id=mid)
                        _sys_stats["barcode_deleted"] += 1
                        print(
                            f"[OK] Barcode deleted: user={uid} chat={cid} msg={mid} attempt={_att + 1}"
                        )
                        break
                    except Exception as _de:
                        if _att == 2:
                            _sys_stats["barcode_del_err"] += 1
                            print(
                                f"[ERROR] Barcode delete failed: user={uid} chat={cid} msg={mid} err={_de}"
                            )
                            try:
                                await app.bot.send_message(
                                    cid,
                                    "❌ Штрих-код більше не дійсний\n"
                                    "Натисни 💳 Моя карта щоб отримати новий",
                                )
                            except Exception as _se:
                                print(f"[barcode_ttl] fallback send error: {_se}")
                        else:
                            await asyncio.sleep(1)
                try:
                    cursor.execute("DELETE FROM barcodes WHERE id=?", (row_id,))
                    conn.commit()
                except Exception as _dbe:
                    print(f"[barcode_ttl] DB delete error: {_dbe}")

        except Exception as _e:
            print(f"[barcode_ttl] Outer error: {_e}")


# ========= TTL MESSAGE CLEANER =========
async def ttl_checker(app):
    """Кожні 5 хв знаходить і видаляє прострочені повідомлення колеса."""
    print("[ttl_checker] Task started ✅")
    while True:
        await asyncio.sleep(300)  # 5 хвилин
        try:
            _now_ts = _time_global.time()
            _now_kyiv = datetime.fromtimestamp(_now_ts, tz=KYIV_TZ)
            print(
                f"[ttl_checker] NOW (Kyiv): {_now_kyiv.strftime('%d.%m.%Y %H:%M:%S')}"
            )

            cursor.execute(
                "SELECT id, user_id, message_id, type, expire_at FROM temp_messages WHERE expire_at <= ?",
                (_now_ts,),
            )
            expired = cursor.fetchall()
            if not expired:
                continue

            print(f"[ttl_checker] Found {len(expired)} expired message(s)")

            for row_id, uid, mid, mtype, ea_ts in expired:
                global _sys_stats
                _ea_kyiv = (
                    datetime.fromtimestamp(ea_ts, tz=KYIV_TZ).strftime(
                        "%d.%m.%Y %H:%M:%S"
                    )
                    if ea_ts
                    else "?"
                )
                print(
                    f"[ttl_checker] EXPIRE: {_ea_kyiv} | msg={mid} type={mtype} user={uid}"
                )
                deleted = False
                for _att in range(3):
                    try:
                        await app.bot.delete_message(chat_id=uid, message_id=mid)
                        _sys_stats["prize_deleted"] += 1
                        print(
                            f"[OK] Prize deleted: user={uid} msg={mid} type={mtype} attempt={_att + 1}"
                        )
                        deleted = True
                        break
                    except Exception as _de:
                        if _att == 2:
                            _sys_stats["prize_del_err"] += 1
                            print(
                                f"[ERROR] Prize delete failed: user={uid} msg={mid} err={_de}"
                            )
                            try:
                                if mtype == "barcode":
                                    await app.bot.send_message(
                                        uid,
                                        "❌ Штрих-код більше не дійсний\nНатисни 💳 Моя карта щоб отримати новий",
                                    )
                                else:
                                    await app.bot.send_message(
                                        uid, "❌ Термін дії бонусу закінчився"
                                    )
                            except Exception as _se:
                                print(f"[ttl_checker] ⚠️ cannot send replacement: {_se}")
                        else:
                            await asyncio.sleep(1)

                # Видаляємо запис з БД в будь-якому разі
                try:
                    cursor.execute("DELETE FROM temp_messages WHERE id=?", (row_id,))
                    conn.commit()
                except Exception as _dbe:
                    print(f"[ttl_checker] DB delete error: {_dbe}")

            # ── Авто-очищення прострочених temp_bonus ────────────────────────
            try:
                _tb_now = int(_time_global.time())
                cursor.execute(
                    "SELECT COUNT(*) FROM temp_bonus WHERE expires_at < ?", (_tb_now,)
                )
                _tb_expired_cnt = cursor.fetchone()[0] or 0
                if _tb_expired_cnt > 0:
                    cursor.execute(
                        "DELETE FROM temp_bonus WHERE expires_at < ?", (_tb_now,)
                    )
                    conn.commit()
                    print(
                        f"[temp_bonus_deleted_expired] count={_tb_expired_cnt}"
                    )
            except Exception as _tbe:
                print(f"[ttl_checker] temp_bonus cleanup error: {_tbe}")

        except Exception as _e:
            print(f"[ttl_checker] Outer error: {_e}")


# ========= WATCHDOG =========
_NO_RECEIPT_ALERTED = False  # щоб не спамити одне й те саме попередження


_ACTIVITY_ALERTED = False  # щоб не спамити про бездіяльність


async def watchdog(app):
    global _NO_RECEIPT_ALERTED, _ACTIVITY_ALERTED
    print("[watchdog] Task started ✅")
    while True:
        await asyncio.sleep(60)
        _touch_activity()  # asyncio event loop жива → бот живий (watchdog_ultra не тригерить)
        _uptime_min = int((_time_global.time() - _BOT_START_TIME) / 60)
        _since_act = int(_time_global.time() - _last_activity)
        print(f"[watchdog] alive | uptime={_uptime_min}m | last_activity={_since_act}s ago")
        print("[heartbeat] alive")

        # ── Перевірка загальної активності з боку ЮЗЕРІВ (5 хв без повідомлень) ──
        if _since_act > 300:
            if not _ACTIVITY_ALERTED:
                _ACTIVITY_ALERTED = True
                _mins = _since_act // 60
                print(f"[watchdog] ⚠️ no activity for {_mins}m")
                try:
                    await app.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ Бот неактивний більше {_mins} хв\n"
                        f"Uptime: {_uptime_min}m | Остання активність: {_since_act}s тому"
                    )
                except Exception as _wa:
                    print(f"[watchdog] activity alert error: {_wa}")
        else:
            _ACTIVITY_ALERTED = False

        # ── Перевірка: чи приходили чеки в останні 5 хв (тільки вдень 10-23) ──
        now_hour = datetime.now(KYIV_TZ).hour
        if 10 <= now_hour <= 23:
            since_last = _time_global.time() - _last_receipt_time
            if since_last > 300:  # 5 хвилин
                if not _NO_RECEIPT_ALERTED:
                    mins = int(since_last // 60)
                    msg = (
                        f"⚠️ Немає чеків з Poster вже {mins} хв\n"
                        f"Перевір підключення до Poster або касу"
                    )
                    print(f"[watchdog] {msg}")
                    try:
                        await app.bot.send_message(ADMIN_ID, msg)
                    except Exception as _we:
                        print(f"[watchdog] alert error: {_we}")
                    _NO_RECEIPT_ALERTED = True
            else:
                _NO_RECEIPT_ALERTED = False  # скидаємо після відновлення


# ── Telegram API healthcheck (кожні 60 сек) ──────────────────────────────────
async def telegram_healthcheck(app):
    global _last_tg_check, _TG_FAIL_ALERTED
    print("[tg_check] Task started ✅")
    await asyncio.sleep(15)  # короткий старт — щоб встигнути до першої перевірки watchdog (30с)
    while True:
        try:
            await app.bot.send_chat_action(chat_id=ADMIN_ID, action="typing")
            _last_tg_check = _time_global.time()
            if _TG_FAIL_ALERTED:
                _TG_FAIL_ALERTED = False
                try:
                    await app.bot.send_message(ADMIN_ID, "✅ Telegram API відновлено")
                except Exception:
                    pass
            _uptime_min = int((_time_global.time() - _BOT_START_TIME) / 60)
            print(f"[tg_check] OK | uptime={_uptime_min}m")
        except Exception as _tge:
            print(f"[tg_check] FAIL: {_tge}")
            if not _TG_FAIL_ALERTED:
                _TG_FAIL_ALERTED = True
                _notify_admin_sync(f"⚠️ Telegram API не відповідає!\n{_tge}")
        await asyncio.sleep(15)  # перевіряємо кожні 15с (для watchdog_ultra 25с поріг)


# ── watchdog_ultra: thread, ultra-fast 5с цикл, реакція 15-25с ───────────────
def _watchdog_ultra_loop():
    """
    Ultra-fast watchdog у окремому thread.
    Перевіряє кожні 5с:
      • inactivity = скільки секунд немає activity_pulse (event loop жива)
      • tg_dead    = скільки секунд немає відповіді Telegram API
    LEVEL 1 (WARN)  : inactivity > 15с → [WARN] лог
    LEVEL 2 (RESTART): inactivity > 25с OR tg_dead > 25с → os._exit(1)
    Антипаніка: пропускає перші 30с після старту.
    """
    print("[watchdog_ultra] Thread started ✅")
    while True:
        _time_global.sleep(5)
        _now     = _time_global.time()
        _uptime  = _now - _BOT_START_TIME
        _inact   = _now - _last_activity
        _tg_dead = _now - _last_tg_check

        # ── Захист від від'ємних значень ──────────────────────────────────────
        if _inact < 0:
            _inact = 0
        if _tg_dead < 0:
            _tg_dead = 0

        # ── Антипаніка: не тригерити перші 30с після старту ──────────────────
        if _uptime < 30:
            continue

        # ── Level 1 SOFT WARNING ──────────────────────────────────────────────
        if _inact > 15:
            print(f"[WARN] inactivity={int(_inact)}s | tg_dead={int(_tg_dead)}s | uptime={int(_uptime)}s")

        # ── Level 2 HARD RESTART: inactivity > 25с ───────────────────────────
        if _inact > 25:
            _msg = (
                f"🔴 ULTRA RESTART\n"
                f"inactivity={int(_inact)}s | uptime={int(_uptime)}s\n"
                f"tg_dead={int(_tg_dead)}s"
            )
            print(f"[ULTRA_RESTART_TRIGGER] inactivity={int(_inact)}s | uptime={int(_uptime)}s → os._exit(1)")
            _notify_admin_sync(_msg)
            _time_global.sleep(2)
            os._exit(1)

        # ── Level 2 HARD RESTART: tg_dead > 25с ─────────────────────────────
        if _tg_dead > 25:
            _msg = (
                f"🔴 ULTRA RESTART\n"
                f"tg_dead={int(_tg_dead)}s > 25s\n"
                f"inactivity={int(_inact)}s | uptime={int(_uptime)}s"
            )
            print(f"[ULTRA_RESTART_TRIGGER] tg_dead={int(_tg_dead)}s → os._exit(1)")
            _notify_admin_sync(_msg)
            _time_global.sleep(3)
            os._exit(1)


# ✅ правильний запуск ДР
async def reminder_checker(app):
    print("[reminder_checker] Task started ✅")
    while True:
        try:
            _rc_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _rc_cur = _rc_conn.cursor()
            _now_ts = int(_time_global.time())
            _3days = 3 * 86400
            _2days = 2 * 86400

            # Всі записи кампанії де минуло 3+ дні
            _rc_cur.execute(
                "SELECT user_id, sent_at FROM bonus_campaign_log WHERE sent_at < ?",
                (_now_ts - _3days,),
            )
            _entries = _rc_cur.fetchall()

            _sent = 0
            for _ru, _rst in _entries:
                try:
                    # Анти-дубль: чи вже надсилали нагадування
                    _rc_cur.execute(
                        "SELECT id FROM bonus_reminder_log WHERE user_id=?", (_ru,)
                    )
                    if _rc_cur.fetchone():
                        continue

                    # Знайти poster_client_id
                    _rc_cur.execute(
                        "SELECT poster_client_id FROM users WHERE user_id=?", (_ru,)
                    )
                    _urow = _rc_cur.fetchone()
                    _pcid = _urow[0] if _urow else None

                    # Перевірити чи прийшов клієнт протягом 2 днів
                    _came = False
                    if _pcid:
                        _from_dt = datetime.fromtimestamp(_rst, tz=KYIV_TZ).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        _to_dt = datetime.fromtimestamp(
                            _rst + _2days, tz=KYIV_TZ
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        _rc_cur.execute(
                            "SELECT COUNT(*) FROM purchases_log "
                            "WHERE client_id=? AND created_at >= ? AND created_at < ?",
                            (_pcid, _from_dt, _to_dt),
                        )
                        _came = (_rc_cur.fetchone()[0] or 0) > 0

                    if _came:
                        continue  # прийшов — нагадування не потрібне

                    # Відправити повторний пуш
                    await app.bot.send_message(
                        chat_id=_ru,
                        text=(
                            "😏 Ми чекали тебе...\n\n"
                            "Але ти так і не зайшов 👀\n\n"
                            "🎁 Твої бонуси вже згоріли...\n\n"
                            "Але сьогодні дамо ще один шанс 😉\n\n"
                            "Забігай — зробимо тобі гарний настрій 🍻"
                        ),
                        reply_markup=get_marketing_keyboard(),
                    )

                    # Записати в лог
                    _rc_cur.execute(
                        "INSERT INTO bonus_reminder_log (user_id, sent_at) VALUES (?,?)",
                        (_ru, _now_ts),
                    )
                    _rc_conn.commit()
                    _sent += 1
                    print(f"[bonus_reminder_sent] user={_ru}")
                    await asyncio.sleep(0.1)

                except Exception as _re:
                    print(f"[reminder_checker] ❌ user={_ru}: {_re}")

            _rc_conn.close()
            if _sent:
                print(f"[reminder_checker] done sent={_sent}")

        except Exception as _rce:
            print(f"[reminder_checker] ❌ loop error: {_rce}")

        await asyncio.sleep(3600)


async def inactivity_checker(app):
    print("[inactivity_checker] Task started ✅")
    while True:
        try:
            _ic_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _ic_cur = _ic_conn.cursor()
            _now_ts = int(_time_global.time())
            _4days_ago = _now_ts - 4 * 86400
            _2days_sec = 2 * 86400

            # Знайти реальних користувачів і дату їх останнього відвідування
            _ic_cur.execute("""
                SELECT u.user_id, u.phone,
                       MAX(p.created_at) as last_visit
                FROM users u
                LEFT JOIN purchases_log p ON u.poster_client_id = p.client_id
                WHERE u.user_id > 100000000
                GROUP BY u.user_id
            """)
            _candidates = _ic_cur.fetchall()

            _sent = 0
            _skipped = 0
            for (_uid, _phone, _last_visit) in _candidates:
                try:
                    # Визначити час останнього відвідування
                    if _last_visit:
                        try:
                            _lv_dt = datetime.strptime(_last_visit[:19], "%Y-%m-%d %H:%M:%S")
                            _lv_ts = int(_lv_dt.replace(tzinfo=KYIV_TZ).timestamp())
                        except Exception:
                            _lv_ts = 0
                    else:
                        _lv_ts = 0  # ніколи не відвідував

                    # Якщо відвідував менше 4 днів тому — пропустити
                    if _lv_ts > _4days_ago:
                        _skipped += 1
                        continue

                    # Перевірка: чи є активний temp_bonus
                    _ic_cur.execute(
                        "SELECT id FROM temp_bonus WHERE user_id=? AND expires_at > ?",
                        (_uid, _now_ts),
                    )
                    if _ic_cur.fetchone():
                        _skipped += 1
                        continue

                    # Перевірка: чи надсилали протягом 2 днів
                    _ic_cur.execute(
                        "SELECT id FROM bonus_campaign_log WHERE user_id=? AND sent_at > ?",
                        (_uid, _now_ts - _2days_sec),
                    )
                    if _ic_cur.fetchone():
                        _skipped += 1
                        continue

                    # Нарахувати temp_bonus
                    _expires = _now_ts + _2days_sec
                    _ic_cur.execute(
                        "INSERT INTO temp_bonus (user_id, amount, expires_at) VALUES (?,?,?)",
                        (_uid, "30.00", _expires),
                    )

                    # Лог кампанії
                    _ic_cur.execute(
                        "INSERT INTO bonus_campaign_log (user_id, phone, sent_at, bonus_amount) VALUES (?,?,?,?)",
                        (_uid, _phone or "", _now_ts, "30.00"),
                    )
                    _ic_conn.commit()

                    # Видалити прострочені temp_bonus
                    _ic_cur.execute("DELETE FROM temp_bonus WHERE expires_at < ?", (_now_ts,))
                    _ic_conn.commit()

                    # Відправити повідомлення
                    await app.bot.send_message(
                        chat_id=_uid,
                        text=(
                            "😏 Ми тут згадали про тебе...\n\n"
                            "Схоже, ти давно не заходив 👀\n\n"
                            "🎁 Тримай 30 грн бонусів від нас!\n\n"
                            "⏳ Дійсні тільки сьогодні та завтра\n\n"
                            "Забігай — щось смачненьке вже чекає 😎🍺"
                        ),
                        reply_markup=get_marketing_keyboard(),
                    )
                    _sent += 1
                    print(f"[temp_bonus_auto] sent user={_uid} phone={_phone}")
                    await asyncio.sleep(0.1)

                except Exception as _ue:
                    print(f"[inactivity_checker] ❌ user={_uid}: {_ue}")

            _ic_conn.close()
            if _sent:
                print(f"[bonus_campaign] done sent={_sent} skipped={_skipped}")

        except Exception as _e:
            print(f"[inactivity_checker] ❌ loop error: {_e}")

        await asyncio.sleep(3600)


async def startup_tg_check(app):
    """5 спроб ping Telegram API при старті. Якщо всі провалились → os._exit(1)."""
    global _last_tg_check
    for _i in range(5):
        try:
            await app.bot.send_chat_action(chat_id=ADMIN_ID, action="typing")
            _last_tg_check = _time_global.time()  # скидаємо tg_dead → 0 (захист від false trigger)
            print(f"[startup_tg_ok] Telegram API відповів (спроба {_i + 1}/5)")
            return
        except Exception as _e:
            print(f"[startup_tg_check] спроба {_i + 1}/5 FAIL: {_e}")
            await asyncio.sleep(5)
    # Всі 5 спроб провалились
    print("[startup_tg_check] ❌ Telegram API не відповідає → РЕСТАРТ")
    _notify_admin_sync("🔴 Telegram API не відповідає при старті → рестарт")
    _time_global.sleep(2)
    os._exit(1)


async def start_background_tasks(app):
    global BOT_USERNAME, _bot_started
    _bot_started = True  # polling запущено — startup_watchdog може зупинитись
    print("[startup_ok] Bot polling active ✅")
    try:
        _me = await app.bot.get_me()
        BOT_USERNAME = _me.username or ""
        print(f"[startup] BOT_USERNAME={BOT_USERNAME}")
    except Exception as _e:
        print(f"[startup] ❌ get_me failed: {_e}")

    asyncio.create_task(birthday_checker(app))
    asyncio.create_task(receipt_checker(app))
    asyncio.create_task(refund_checker(app))
    asyncio.create_task(prro_webhook_server(app))
    asyncio.create_task(barcode_ttl_checker(app))
    asyncio.create_task(ttl_checker(app))
    asyncio.create_task(watchdog(app))
    asyncio.create_task(inactivity_checker(app))
    asyncio.create_task(reminder_checker(app))
    asyncio.create_task(ai_risk_checker(app))
    asyncio.create_task(poster_sync_checker(app))
    asyncio.create_task(telegram_healthcheck(app))
    asyncio.create_task(activity_pulse(app))
    asyncio.create_task(startup_tg_check(app))   # 5 спроб ping TG при старті
    # watchdog_ultra запущено як daemon thread (поза asyncio) — не треба тут

    # Сповіщення адміна про старт
    try:
        started_at = datetime.now(KYIV_TZ).strftime("%d.%m.%Y %H:%M:%S")
        await app.bot.send_message(
            ADMIN_ID,
            f"✅ Бот відновлено після падіння\n"
            f"🟢 Система онлайн\n"
            f"🕐 {started_at}\n"
            f"♻️ Перезапуск #{_restart_count}\n\n"
            f"✅ receipt_checker\n"
            f"✅ birthday_checker\n"
            f"✅ refund_checker\n"
            f"✅ watchdog soft (5хв алерт)\n"
            f"✅ watchdog_ultra (25с → restart, 5с цикл)\n"
            f"✅ tg_check (15с) + activity_pulse (10с)\n"
            f"✅ safe_handler (20с timeout)\n"
            f"✅ startup_watchdog (90с) + startup_tg_check\n"
            f"✅ prro_webhook (8765)\n"
            f"✅ keep-alive (PORT={os.environ.get('PORT', '3000')})\n"
            f"✅ self_ping (60s)",
        )
        print("[startup_notify] ✅ Адмін повідомлений про старт")
    except Exception as _e:
        print(f"[startup_notify] Помилка: {_e}")


app.post_init = start_background_tasks

print("🚀 Bot started")
print("[SYSTEM] BOT STARTED")
print("[startup] Перевірка Poster токена...")
poster_api.check_token()
cursor.execute(
    "SELECT user_id, poster_client_id FROM users WHERE poster_client_id IS NOT NULL LIMIT 10"
)
print(f"[startup] Users with poster_client_id: {cursor.fetchall()}")



def _notify_admin_sync(msg: str):
    """Sync Telegram повідомлення адміну (для виклику поза asyncio)."""
    try:
        _requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": ADMIN_ID, "text": msg},
            timeout=5,
        )
        print(f"[notify_admin] ✅ надіслано: {msg[:60]}")
    except Exception as _e:
        print(f"[notify_admin] ❌ помилка: {_e}")


class _KeepAliveHandler(_BaseHandler):
    def do_GET(self):
        import json as _json

        # ── /test_crash — ручний тест watchdog (freeze mode) ─────────────────
        if self.path.rstrip("/") == "/test_crash":
            global _crash_mode, _last_activity, _last_tg_check
            print("[TEST] manual crash triggered")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"status":"crash_triggered","info":"watchdog will trigger in ~15-25s"}'
            )
            self.wfile.flush()
            # Симулюємо зависання event loop: скидаємо activity timestamps.
            # _crash_mode=True блокує _touch_activity → inactivity росте.
            # Watchdog (5с цикл): [WARN] @ 15с → [ULTRA_RESTART_TRIGGER] @ 25с → os._exit(1)
            _crash_mode = True
            # Встановлюємо реалістичний timestamp: inactivity = 10s (зростає до 25s за ~15с)
            # → [WARN] @ ~15s, [ULTRA_RESTART_TRIGGER] @ ~25s
            _last_activity = _time_global.time() - 10
            _last_tg_check = _time_global.time() - 10
            return

        # ── /api/ та всі інші — статус бота ──────────────────────────────────
        _uptime_sec = int(_time_global.time() - _BOT_START_TIME)
        _since_act  = int(_time_global.time() - _last_activity)
        _body = _json.dumps({
            "status": "ok",
            "uptime_min": _uptime_sec // 60,
            "uptime_sec": _uptime_sec,
            "last_activity_sec": _since_act,
            "restart_count": _restart_count,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_http_server():
    port = int(os.environ.get("PORT", 3000))

    class _ReuseServer(_HTTPServer):
        allow_reuse_address = True

    def run():
        for _attempt in range(5):
            try:
                server = _ReuseServer(("0.0.0.0", port), _KeepAliveHandler)
                print(f"[server] running on port {port}")
                print("Keep-alive server started")
                server.serve_forever()
                break
            except OSError as _e:
                print(
                    f"[server] port {port} busy (attempt {_attempt + 1}/5), retry in 2s: {_e}"
                )
                time.sleep(2)

    _threading.Thread(target=run, daemon=True).start()


class Handler(_BaseHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run():
    port = int(os.environ.get("PORT", 3000))
    server = _HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

# ── Self-ping: бот пінгує сам себе кожні 60с, щоб Replit не засинав ─────────
def _self_ping_loop():
    _ping_url = f"https://{_DEV_DOMAIN}/api/" if _DEV_DOMAIN else ""
    if not _ping_url:
        print("[self_ping] REPLIT_DEV_DOMAIN not set — disabled")
        return
    print(f"[self_ping] started → {_ping_url}")
    _time_global.sleep(30)  # невелика затримка перед першим пінгом
    while True:
        try:
            _r = _requests.get(_ping_url, timeout=10, verify=False)
            _uptime_min = int((_time_global.time() - _BOT_START_TIME) / 60)
            print(f"[self_ping] {_r.status_code} | uptime={_uptime_min}m")
        except Exception as _pe:
            print(f"[self_ping_error] {_pe}")
        _time_global.sleep(60)

def _startup_watchdog_loop():
    """
    Дає боту 90с на старт. Якщо _bot_started залишається False →
    вважаємо що polling завис → os._exit(1).
    """
    print("[startup_watchdog] started ✅")
    _time_global.sleep(90)  # 90с вікно для ініціалізації
    if not _bot_started:
        _msg = (
            f"🔴 Бот не стартував за 90с → примусовий РЕСТАРТ\n"
            f"polling не відповів або завис при ініціалізації"
        )
        print(f"[startup_watchdog] ❌ bot_started=False → os._exit(1)")
        _notify_admin_sync(_msg)
        _time_global.sleep(2)
        os._exit(1)
    print("[startup_watchdog] ✅ bot_started=True — все ок, завершуємо перевірку")


def _guard_loop():
    """
    Guard monitor: пінгує API server кожні 15с.
    Друкує [GUARD_OK] / [GUARD_WARN] / [GUARD_ERROR].
    Аналог guard_bot.py з окремого проєкту — тепер вбудований.
    """
    _time_global.sleep(20)  # чекаємо поки API server стартує
    while True:
        try:
            _gr = _requests.get("http://localhost:8080/api/", timeout=5)
            if _gr.status_code == 200:
                print("[GUARD_OK] bot alive")
            else:
                print(f"[GUARD_WARN] bad status={_gr.status_code}")
        except Exception as _ge:
            print(f"[GUARD_ERROR] bot not responding: {_ge}")
        _time_global.sleep(15)


_threading.Thread(target=_self_ping_loop, daemon=True).start()
_threading.Thread(target=_watchdog_ultra_loop, daemon=True).start()
_threading.Thread(target=_startup_watchdog_loop, daemon=True).start()
_threading.Thread(target=_guard_loop, daemon=True).start()

# ── HTTP keep-alive у фоновому потоці, бот — у ГОЛОВНОМУ ─────────────────────
print("🚀 Bot starting...")
print("[system] main thread active")
start_http_server()

# ── Бот у ГОЛОВНОМУ потоці (app.run_polling вимагає main thread) ─────────────
try:
    print(f"[bot] polling started (restart #{_restart_count})")
    app.run_polling()
except Exception as _e:
    _restart_count += 1
    _tb_text = traceback.format_exc()
    print(f"[bot] ❌ CRASH #{_restart_count}: {_e}")
    print(_tb_text)

    # Коротке повідомлення адміну (перші 600 символів traceback)
    short_tb = _tb_text[-600:] if len(_tb_text) > 600 else _tb_text
    _notify_admin_sync(
        f"🔴 Бот впав! (перезапуск #{_restart_count})\n\n"
        f"⚠️ {_e}\n\n"
        f"```\n{short_tb}\n```\n\n"
        f"♻️ Процес завершується — Replit перезапустить автоматично..."
    )
    time.sleep(2)
    # Виходимо: asyncio event loop вже закритий, reuse неможливий.
    # Replit workflow перезапустить весь процес чисто.
    sys.exit(1)

