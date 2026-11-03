import os
import sys
import re
import math
import wave
import time
import struct
import random
import shutil
import hashlib
import sqlite3
import asyncio
import logging
import traceback
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    ErrorEvent
)
from aiogram.utils.chat_action import ChatActionSender
import edge_tts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

# ==========================================
# 1. КОНФИГУРАЦИЯ И ТАРИФЫ (РУБЛИ -> ЗВЁЗДЫ)
# ==========================================
raw_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
BOT_TOKEN = raw_token.strip().strip("'\"")

if not BOT_TOKEN:
    logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

raw_admin = os.environ.get("ADMIN_ID", "0").strip().strip("'\"")
ADMIN_ID = int(raw_admin) if raw_admin.isdigit() else 0

WEBHOOK_BASE_URL = (
    os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
).strip().rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

DB_PATH = os.environ.get("DATABASE_PATH") or (
    "/tmp/users.db" if os.path.exists("/tmp") else "users.db"
)
LULLABY_WAV_PATH = (
    "/tmp/lullaby_loop_bg.wav" if os.path.exists("/tmp") else "lullaby_loop_bg.wav"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

tts_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts_pool")
tts_semaphore = asyncio.Semaphore(2)

story_task_queue = None
NUM_QUEUE_WORKERS = 3
active_generations = set()
user_cooldowns = {}
COOLDOWN_SECONDS = 8.0

current_key_index = 0
cached_models_by_key = {}
http_session = None

# Единый добрый промокод на 5 сказок (принимает и кириллицу, и латиницу)
VALID_PROMO_ALIASES = {"DOBRO", "ДОБРО"}
PROMO_BONUS_STORIES = 5

FREE_START_STORIES = 3
REFERRAL_BONUS_STORIES = 2
MAX_CHILDREN_PER_FAMILY = 3

# Тарифная сетка: 99 ₽ = 50 ⭐ | 199 ₽ = 100 ⭐ | 499 ₽ (Месяц) = 250 ⭐
TARIFFS = {
    "pack_5": {
        "stories": 5,
        "days": 0,
        "stars": 50,
        "rub": 99,
        "title": "Пакет: 5 добрых сказок",
        "btn_text": "✨ 5 сказок — 99 ₽ (50 ⭐)"
    },
    "pack_15": {
        "stories": 15,
        "days": 0,
        "stars": 100,
        "rub": 199,
        "title": "Пакет: 15 добрых сказок (Выгодно)",
        "btn_text": "🔥 15 сказок — 199 ₽ (100 ⭐)"
    },
    "sub_month": {
        "stories": 0,
        "days": 30,
        "stars": 250,
        "rub": 499,
        "title": "Подписка на месяц (30 дней безлимита)",
        "btn_text": "👑 Подписка на месяц — 499 ₽ (250 ⭐)"
    }
}

DEFAULT_VOICE = "ru-RU-SvetlanaNeural"

AVAILABLE_VOICES = {
    "ru-RU-SvetlanaNeural": "👩 Нежная Светлана",
    "ru-RU-DariyaNeural": "👩‍🦰 Ласковая Дарья",
    "ru-RU-DmitryNeural": "👨 Добрый сказочник Дмитрий"
}

VOICE_PREVIEW_TEXTS = {
    "ru-RU-SvetlanaNeural": (
        "Здравствуй, малыш. Меня зовут Светлана. Сегодня вечером я расскажу тебе самую тёплую "
        "и добрую сказку. Устраивайся поудобнее на мягкой подушечке, закрывай глазки и слушай."
    ),
    "ru-RU-DariyaNeural": (
        "Привет, мой хороший. Я сказочница Дарья. Давай вместе отправимся в уютную волшебную страну, "
        "где живут твои любимые игрушки и тихонько светят ласковые звёздочки."
    ),
    "ru-RU-DmitryNeural": (
        "Добрый вечер, дружок. Меня зовут Дмитрий. Пришло время отложить все игрушки, "
        "укрыться тёплым одеяльцем и послушать спокойную вечернюю историю."
    )
}

TIMEZONE_OFFSETS = {
    "3": ("Москва (UTC+3)", 3),
    "5": ("Екатеринбург (UTC+5)", 5),
    "7": ("Новосибирск (UTC+7)", 7),
    "8": ("Иркутск (UTC+8)", 8),
    "10": ("Владивосток (UTC+10)", 10),
}

def get_all_gemini_keys():
    keys = []
    candidates = [
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3",
        "GEMINI_API_KEY_4", "GEMINI_API_KEY_5"
    ]
    for var_name in candidates:
        val = os.environ.get(var_name, "")
        if val:
            for part in val.split(","):
                clean_k = part.strip().strip("'\"")
                if clean_k and clean_k not in keys:
                    keys.append(clean_k)
    return keys

async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        timeout_cfg = aiohttp.ClientTimeout(total=35.0, connect=8.0)
        connector = aiohttp.TCPConnector(limit=30, ttl_dns_cache=300)
        http_session = aiohttp.ClientSession(timeout=timeout_cfg, connector=connector)
    return http_session

def sanitize_input(text, max_len=70):
    if not text:
        return ""
    cleaned = re.sub(r'[<>{}\[\]\\*$#^~`|]', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len]

def extract_clean_name(child_info):
    """Извлекает чистое имя из строки вида 'Миша, 4 года' -> 'Миша' для красивых заголовков."""
    if not child_info:
        return "Малыш"
    first_part = re.split(r'[,(\d]', child_info)[0].strip()
    return first_part if len(first_part) >= 2 else child_info.strip()

def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID

def check_rate_limit(user_id):
    now = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - (now - last_time)) + 1
    user_cooldowns[user_id] = now
    return 0

# ==========================================
# 2. МОНИТОРИНГ ОШИБОК И RETRY
# ==========================================
async def report_error_to_admin(context_title, exc):
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logging.error(f"[{context_title}] {exc}\n{tb_str}")
    if ADMIN_ID != 0:
        try:
            short_tb = tb_str[-1200:] if len(tb_str) > 1200 else tb_str
            await bot.send_message(
                ADMIN_ID,
                f"🚨 Ошибка ({context_title}):\n"
                f"{type(exc).__name__}: {str(exc)[:200]}\n\n"
                f"Лог:\n{short_tb}"
            )
        except Exception:
            pass

@dp.error()
async def global_error_handler(event: ErrorEvent):
    await report_error_to_admin("Dispatcher Error", event.exception)

async def retry_with_backoff(coro_factory, retries=3, base_delay=0.7, max_delay=5.0):
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_factory(attempt)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                sleep_time = random.uniform(0.3, min(max_delay, base_delay * (2 ** attempt)))
                logging.warning(f"Повтор #{attempt+1} через {sleep_time:.2f}с: {e}")
                await asyncio.sleep(sleep_time)
    raise last_exc

# ==========================================
# 3. БАЗА ДАННЫХ SQLITE
# ==========================================
@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                stories_left INTEGER DEFAULT 3,
                total_generated INTEGER DEFAULT 0,
                last_child TEXT DEFAULT '',
                last_hero TEXT DEFAULT '',
                last_story_summary TEXT DEFAULT '',
                voice TEXT DEFAULT 'ru-RU-SvetlanaNeural',
                referred_by INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0,
                remind_time TEXT DEFAULT '',
                tz_offset INTEGER DEFAULT 3,
                last_reminded_date TEXT DEFAULT '',
                tomorrow_hook TEXT DEFAULT '',
                bg_music INTEGER DEFAULT 1,
                sub_until INTEGER DEFAULT 0,
                promo_used INTEGER DEFAULT 0,
                utm_source TEXT DEFAULT ''
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS child_profiles (
                profile_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                child_name TEXT,
                hero_name TEXT,
                last_summary TEXT DEFAULT '',
                tomorrow_hook TEXT DEFAULT '',
                stories_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY,
                user_id INTEGER,
                stories_added INTEGER,
                stars_amount INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS voice_samples (
                sample_key TEXT PRIMARY KEY,
                file_id TEXT
            )
        ''')

        existing_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(users);").fetchall()
        ]
        migrations = {
            "last_child": "TEXT DEFAULT ''",
            "last_hero": "TEXT DEFAULT ''",
            "last_story_summary": "TEXT DEFAULT ''",
            "voice": "TEXT DEFAULT 'ru-RU-SvetlanaNeural'",
            "referred_by": "INTEGER DEFAULT 0",
            "referrals_count": "INTEGER DEFAULT 0",
            "remind_time": "TEXT DEFAULT ''",
            "tz_offset": "INTEGER DEFAULT 3",
            "last_reminded_date": "TEXT DEFAULT ''",
            "tomorrow_hook": "TEXT DEFAULT ''",
            "bg_music": "INTEGER DEFAULT 1",
            "sub_until": "INTEGER DEFAULT 0",
            "promo_used": "INTEGER DEFAULT 0",
            "utm_source": "TEXT DEFAULT ''"
        }
        for col_name, col_def in migrations.items():
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
                except Exception as e:
                    logging.warning(f"Миграция users.{col_name}: {e}")

        conn.execute(
            "UPDATE users SET voice = ? WHERE voice NOT IN ('ru-RU-SvetlanaNeural', 'ru-RU-DariyaNeural', 'ru-RU-DmitryNeural')",
            (DEFAULT_VOICE,)
        )

        legacy_rows = conn.execute(
            "SELECT user_id, last_child, last_hero, last_story_summary, tomorrow_hook, total_generated "
            "FROM users WHERE last_child != '' AND last_hero != ''"
        ).fetchall()
        for uid, l_child, l_hero, l_sum, t_hook, tot_gen in legacy_rows:
            exists = conn.execute(
                "SELECT 1 FROM child_profiles WHERE user_id = ?", (uid,)
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO child_profiles 
                       (user_id, child_name, hero_name, last_summary, tomorrow_hook, stories_count, is_active) 
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (uid, l_child, l_hero, l_sum or "", t_hook or "", tot_gen or 1)
                )
        conn.commit()

def get_cached_voice_sample(voice_id, bg_music):
    key = f"{voice_id}::bg_{bg_music}"
    with get_db_connection() as conn:
        row = conn.execute("SELECT file_id FROM voice_samples WHERE sample_key = ?", (key,)).fetchone()
        return row[0] if row else ""

def save_cached_voice_sample(voice_id, bg_music, file_id):
    if not file_id:
        return
    key = f"{voice_id}::bg_{bg_music}"
    with get_db_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO voice_samples (sample_key, file_id) VALUES (?, ?)",
            (key, file_id)
        )
        conn.commit()

def get_subscription_status(user_id):
    with get_db_connection() as conn:
        row = conn.execute("SELECT sub_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row or not row[0]:
            return False, 0
        now_ts = int(time.time())
        if row[0] > now_ts:
            days_left = max(1, int(math.ceil((row[0] - now_ts) / 86400)))
            return True, days_left
        return False, 0

def has_available_stories(user_id):
    has_sub, _ = get_subscription_status(user_id)
    if has_sub:
        return True
    balance = get_user_data(user_id)[0]
    return balance > 0

def get_family_profiles(user_id):
    with get_db_connection() as conn:
        return conn.execute(
            """SELECT profile_id, child_name, hero_name, last_summary, tomorrow_hook, stories_count, is_active 
               FROM child_profiles WHERE user_id = ? ORDER BY is_active DESC, profile_id ASC""",
            (user_id,)
        ).fetchall()

def get_active_child_profile(user_id):
    profiles = get_family_profiles(user_id)
    if not profiles:
        return None
    for p in profiles:
        if p[6] == 1:
            return p
    return profiles[0]

def save_or_activate_child_profile(user_id, child_name, hero_name):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE child_profiles SET is_active = 0 WHERE user_id = ?", (user_id,))
        existing = c.execute(
            "SELECT profile_id FROM child_profiles WHERE user_id = ? AND lower(child_name) = lower(?)",
            (user_id, child_name.strip())
        ).fetchone()

        if existing:
            pid = existing[0]
            c.execute(
                "UPDATE child_profiles SET child_name = ?, hero_name = ?, is_active = 1 WHERE profile_id = ?",
                (child_name.strip(), hero_name.strip(), pid)
            )
        else:
            count = c.execute(
                "SELECT COUNT(*) FROM child_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if count >= MAX_CHILDREN_PER_FAMILY:
                oldest = c.execute(
                    "SELECT profile_id FROM child_profiles WHERE user_id = ? ORDER BY profile_id ASC LIMIT 1",
                    (user_id,)
                ).fetchone()
                if oldest:
                    c.execute(
                        """UPDATE child_profiles 
                           SET child_name = ?, hero_name = ?, last_summary = '', tomorrow_hook = '', is_active = 1 
                           WHERE profile_id = ?""",
                        (child_name.strip(), hero_name.strip(), oldest[0])
                    )
            else:
                c.execute(
                    """INSERT INTO child_profiles 
                       (user_id, child_name, hero_name, last_summary, tomorrow_hook, stories_count, is_active) 
                       VALUES (?, ?, ?, '', '', 0, 1)""",
                    (user_id, child_name.strip(), hero_name.strip())
                )

        c.execute(
            "UPDATE users SET last_child = ?, last_hero = ? WHERE user_id = ?",
            (child_name.strip(), hero_name.strip(), user_id)
        )
        conn.commit()

def switch_active_child_profile(user_id, profile_id):
    with get_db_connection() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT child_name, hero_name, last_summary, tomorrow_hook FROM child_profiles WHERE profile_id = ? AND user_id = ?",
            (profile_id, user_id)
        ).fetchone()
        if not row:
            return None
        c.execute("UPDATE child_profiles SET is_active = 0 WHERE user_id = ?", (user_id,))
        c.execute("UPDATE child_profiles SET is_active = 1 WHERE profile_id = ?", (profile_id,))
        c.execute(
            "UPDATE users SET last_child = ?, last_hero = ?, last_story_summary = ?, tomorrow_hook = ? WHERE user_id = ?",
            (row[0], row[1], row[2] or "", row[3] or "", user_id)
        )
        conn.commit()
        return row

def delete_child_profile_db(user_id, profile_id):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM child_profiles WHERE profile_id = ? AND user_id = ?", (profile_id, user_id))
        remaining = c.execute(
            "SELECT profile_id, child_name, hero_name, last_summary, tomorrow_hook FROM child_profiles WHERE user_id = ? ORDER BY profile_id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        if remaining:
            c.execute("UPDATE child_profiles SET is_active = 1 WHERE profile_id = ?", (remaining[0],))
            c.execute(
                "UPDATE users SET last_child = ?, last_hero = ?, last_story_summary = ?, tomorrow_hook = ? WHERE user_id = ?",
                (remaining[1], remaining[2], remaining[3] or "", remaining[4] or "", user_id)
            )
        else:
            c.execute(
                "UPDATE users SET last_child = '', last_hero = '', last_story_summary = '', tomorrow_hook = '' WHERE user_id = ?",
                (user_id,)
            )
        conn.commit()

def register_or_get_user(user_id, referrer_id=0, utm_source=""):
    ref_rewarded = False
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT stories_left, total_generated, last_child, last_hero, 
                      last_story_summary, voice, referrals_count, remind_time, tz_offset,
                      tomorrow_hook, bg_music
               FROM users WHERE user_id = ?""",
            (user_id,)
        )
        row = c.fetchone()
        if row is None:
            valid_ref = referrer_id if (referrer_id > 0 and referrer_id != user_id) else 0
            if valid_ref > 0:
                c.execute("SELECT user_id FROM users WHERE user_id = ?", (valid_ref,))
                if c.fetchone() is not None:
                    c.execute(
                        "UPDATE users SET stories_left = stories_left + ?, "
                        "referrals_count = referrals_count + 1 WHERE user_id = ?",
                        (REFERRAL_BONUS_STORIES, valid_ref)
                    )
                    ref_rewarded = True
                else:
                    valid_ref = 0

            c.execute(
                """INSERT INTO users 
                   (user_id, stories_left, total_generated, last_child, last_hero, 
                    last_story_summary, voice, referred_by, referrals_count, 
                    remind_time, tz_offset, tomorrow_hook, bg_music, utm_source) 
                   VALUES (?, ?, 0, '', '', '', ?, ?, 0, '', 3, '', 1, ?)""",
                (user_id, FREE_START_STORIES, DEFAULT_VOICE, valid_ref, utm_source[:40])
            )
            conn.commit()
            return (
                FREE_START_STORIES, 0, "", "", "", DEFAULT_VOICE,
                0, "", 3, "", 1, ref_rewarded
            )

        voice_val = row[5] if (row[5] in AVAILABLE_VOICES) else DEFAULT_VOICE
        return (
            row[0], row[1], row[2] or "", row[3] or "",
            row[4] or "", voice_val, row[6] or 0,
            row[7] or "", row[8] if row[8] is not None else 3,
            row[9] or "", 1 if (row[10] is None or row[10] == 1) else 0,
            False
        )

def get_user_data(user_id):
    """Возвращает: (balance, total, child, hero, summary, voice, refs, remind_time, tz_offset, tomorrow_hook, bg_music)"""
    data = register_or_get_user(user_id)
    active_prof = get_active_child_profile(user_id)
    if active_prof:
        _, c_name, h_name, l_sum, t_hook, _, _ = active_prof
        return (
            data[0], data[1], c_name, h_name, l_sum,
            data[5], data[6], data[7], data[8], t_hook, data[10]
        )
    return data[:11]

def set_user_voice(user_id, voice_id):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET voice = ? WHERE user_id = ?", (voice_id, user_id))
        conn.commit()

def toggle_user_bg_music(user_id):
    data = get_user_data(user_id)
    new_val = 0 if data[10] == 1 else 1
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET bg_music = ? WHERE user_id = ?", (new_val, user_id))
        conn.commit()
    return new_val

def set_user_reminder(user_id, remind_time, tz_offset=None):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        if tz_offset is not None:
            conn.execute(
                "UPDATE users SET remind_time = ?, tz_offset = ? WHERE user_id = ?",
                (remind_time, tz_offset, user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET remind_time = ? WHERE user_id = ?",
                (remind_time, user_id)
            )
        conn.commit()

def update_user_after_story(user_id, child, hero, summary, tomorrow_hook):
    save_or_activate_child_profile(user_id, child, hero)
    has_sub, _ = get_subscription_status(user_id)

    with get_db_connection() as conn:
        c = conn.cursor()
        if has_sub:
            c.execute(
                """UPDATE users 
                   SET total_generated = total_generated + 1, 
                       last_child = ?, 
                       last_hero = ?, 
                       last_story_summary = ?,
                       tomorrow_hook = ?
                   WHERE user_id = ?""",
                (child, hero, summary, tomorrow_hook, user_id)
            )
        else:
            c.execute(
                """UPDATE users 
                   SET stories_left = MAX(0, stories_left - 1), 
                       total_generated = total_generated + 1, 
                       last_child = ?, 
                       last_hero = ?, 
                       last_story_summary = ?,
                       tomorrow_hook = ?
                   WHERE user_id = ?""",
                (child, hero, summary, tomorrow_hook, user_id)
            )
        c.execute(
            """UPDATE child_profiles 
               SET hero_name = ?, last_summary = ?, tomorrow_hook = ?, stories_count = stories_count + 1 
               WHERE user_id = ? AND is_active = 1""",
            (hero, summary, tomorrow_hook, user_id)
        )
        conn.commit()
        row = c.execute(
            "SELECT stories_left, total_generated FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
        rem = row[0] if row else 0
        tot = row[1] if row else 1
    return rem, tot

def add_stories_to_user(user_id, count):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?",
            (count, user_id)
        )
        conn.commit()
        row = c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else count

def activate_single_promocode_db(user_id, raw_code):
    _ = get_user_data(user_id)
    code = sanitize_input(raw_code, max_len=30).upper()
    if code not in VALID_PROMO_ALIASES:
        return False, "Такой промокод не найден. Введите добрый промокод: ДОБРО ✨"

    with get_db_connection() as conn:
        c = conn.cursor()
        row = c.execute("SELECT promo_used, stories_left FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0] == 1:
            return False, "Вы уже активировали подарочный промокод ранее!"

        c.execute(
            "UPDATE users SET stories_left = stories_left + ?, promo_used = 1 WHERE user_id = ?",
            (PROMO_BONUS_STORIES, user_id)
        )
        conn.commit()
        new_bal = (row[1] if row else 0) + PROMO_BONUS_STORIES
        return True, f"🎉 Промокод ДОБРО активирован! Вам начислено +{PROMO_BONUS_STORIES} бесплатных сказок.\n🎧 Доступно на балансе: {new_bal} шт."

def record_payment_idempotent(charge_id, user_id, stories_added, sub_days, stars_amount):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT charge_id FROM payments WHERE charge_id = ?", (charge_id,))
        if c.fetchone() is not None:
            r = c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return False, (r[0] if r else 0)

        c.execute(
            "INSERT INTO payments (charge_id, user_id, stories_added, stars_amount) "
            "VALUES (?, ?, ?, ?)",
            (charge_id, user_id, stories_added if stories_added > 0 else 30, stars_amount)
        )

        if sub_days > 0:
            now_ts = int(time.time())
            cur_sub = c.execute("SELECT sub_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
            base_ts = max(now_ts, cur_sub[0]) if (cur_sub and cur_sub[0]) else now_ts
            new_sub_until = base_ts + (sub_days * 86400)
            c.execute(
                "UPDATE users SET sub_until = ? WHERE user_id = ?",
                (new_sub_until, user_id)
            )
        else:
            c.execute(
                "UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?",
                (stories_added, user_id)
            )
        conn.commit()
        row = c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return True, (row[0] if row else stories_added)

def get_global_stats():
    with get_db_connection() as conn:
        c = conn.cursor()
        users_cnt = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        profiles_cnt = c.execute("SELECT COUNT(*) FROM child_profiles").fetchone()[0]
        stories_cnt = c.execute("SELECT COALESCE(SUM(total_generated), 0) FROM users").fetchone()[0]
        pay_cnt = c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        stars_sum = c.execute("SELECT COALESCE(SUM(stars_amount), 0) FROM payments").fetchone()[0]
        utm_rows = c.execute(
            "SELECT utm_source, COUNT(*) FROM users WHERE utm_source != '' GROUP BY utm_source ORDER BY COUNT(*) DESC LIMIT 5"
        ).fetchall()
        return users_cnt, profiles_cnt, stories_cnt, pay_cnt, stars_sum, utm_rows

async def send_db_backup_to_admin(reason="Плановый бэкап"):
    if ADMIN_ID == 0 or not os.path.exists(DB_PATH):
        return
    try:
        db_file = FSInputFile(DB_PATH, filename="users_backup.db")
        await bot.send_document(
            chat_id=ADMIN_ID,
            document=db_file,
            caption=f"🛡 Авто-бэкап БД ({reason}).\nДля восстановления отправьте файл с подписью /restore"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить бэкап админу: {e}")

# ==========================================
# 4. РУССКАЯ ОРФОЭПИЯ (РЕШЕНИЕ ПРОБЛЕМЫ УДАРЕНИЙ)
# ==========================================
# 1. Полный словарь ёфикации для детских сказок
YO_WORDS_DICT = {
    "котенок": "котёнок", "котенка": "котёнка", "котенку": "котёнку", "котенком": "котёнком", "котенке": "котёнке",
    "лисенок": "лисёнок", "лисенка": "лисёнка", "лисенку": "лисёнку", "лисенком": "лисёнком",
    "слоненок": "слонёнок", "слоненка": "слонёнка", "слоненку": "слонёнку", "слоненком": "слонёнком",
    "тигренок": "тигрёнок", "тигренка": "тигрёнка", "львенок": "львёнок", "львенка": "львёнка",
    "жеребенок": "жеребёнок", "жеребенка": "жеребёнка", "теленок": "телёнок", "теленка": "телёнка",
    "поросенок": "поросёнок", "поросенка": "поросёнка", "цыпленок": "цыплёнок", "цыпленка": "цыплёнка",
    "утенок": "утёнок", "утенка": "утёнка", "гусенок": "гусёнок", "гусенка": "гусёнка",
    "козленок": "козлёнок", "козленка": "козлёнка", "осленок": "ослёнок", "осленка": "ослёнка",
    "ежик": "ёжик", "ежика": "ёжика", "ежику": "ёжику", "ежиком": "ёжиком", "ежике": "ёжике", "ежики": "ёжики",
    "елка": "ёлка", "елки": "ёлки", "елке": "ёлке", "елку": "ёлку", "елкой": "ёлкой",
    "елочка": "ёлочка", "елочки": "ёлочки", "елочке": "ёлочке", "елочку": "ёлочку", "елочкой": "ёлочкой",
    "звездочка": "звёздочка", "звездочки": "звёздочки", "звездочек": "звёздочек", "звездочке": "звёздочке",
    "звездочку": "звёздочку", "звездочкой": "звёздочкой", "звездочками": "звёздочками", "звездочках": "звёздочках",
    "звездный": "звёздный", "звездное": "звёздное", "звездная": "звёздная", "звездные": "звёздные",
    "звездным": "звёздным", "звездном": "звёздном", "звездной": "звёздной", "звезд": "звёзд", "звездам": "звёздам",
    "теплый": "тёплый", "теплая": "тёплая", "теплое": "тёплое", "теплые": "тёплые",
    "теплым": "тёплым", "теплой": "тёплой", "теплом": "тёплом", "теплую": "тёплую", "теплых": "тёплых", "теплыми": "тёплыми",
    "темный": "тёмный", "темная": "тёмная", "темное": "тёмное", "темные": "тёмные",
    "темным": "тёмным", "темной": "тёмной", "темном": "тёмном", "темную": "тёмную",
    "тяжелый": "тяжёлый", "тяжелые": "тяжёлые", "тяжелыми": "тяжёлыми", "тяжелая": "тяжёлая", "тяжелое": "тяжёлое",
    "легкий": "лёгкий", "легкая": "лёгкая", "легкое": "лёгкое", "легкие": "лёгкие", "легким": "лёгким", "легкой": "лёгкой",
    "далекий": "далёкий", "далекая": "далёкая", "далекое": "далёкое", "далекие": "далёкие", "далеком": "далёком", "далекой": "далёкой",
    "шепотом": "шёпотом", "шепот": "шёпот",
    "щечки": "щёчки", "щечку": "щёчку", "щечкам": "щёчкам", "щечками": "щёчками", "щечках": "щёчках",
    "слезки": "слёзки", "слез": "слёз", "слезы": "слёзы",
    "мед": "мёд", "медом": "мёдом", "меду": "мёду",
    "поет": "поёт", "споет": "споёт", "уснет": "уснёт", "заснет": "заснёт", "проснется": "проснётся",
    "улыбнется": "улыбнётся", "вернется": "вернётся", "коснется": "коснётся", "начнется": "начнётся",
    "обернется": "обернётся", "свернется": "свернётся", "вздохнет": "вздохнёт", "зевнет": "зевнёт", "моргнет": "моргнёт",
    "придет": "придёт", "уйдет": "уйдёт", "найдет": "найдёт", "подойдет": "подойдёт", "зайдет": "зайдёт",
    "обойдет": "обойдёт", "пройдет": "пройдёт", "взойдет": "взойдёт",
    "плывет": "плывёт", "уплывет": "уплывёт", "живет": "живёт", "зовет": "зовёт", "позовет": "позовёт",
    "несет": "несёт", "принесет": "принесёт", "унесет": "унесёт", "ведет": "ведёт", "приведет": "приведёт",
    "растет": "растёт", "подрастет": "подрастёт", "цветет": "цветёт", "расцветет": "расцветёт",
    "узнает": "узнаёт", "встает": "встаёт", "отдает": "отдаёт", "дает": "даёт", "создает": "создаёт",
    "вперед": "вперёд", "полет": "полёт", "самолет": "самолёт", "самолетик": "самолётик", "вертолет": "вертолёт",
    "колеса": "колёса", "колесики": "колёсики", "колесами": "колёсами",
    "пестрый": "пёстрый", "пестрая": "пёстрая", "пестрое": "пёстрое",
    "желтый": "жёлтый", "желтая": "жёлтая", "желтое": "жёлтое", "желтые": "жёлтые", "желтым": "жёлтым", "желтой": "жёлтой",
    "зеленый": "зелёный", "зеленая": "зелёная", "зеленое": "зелёное", "зеленые": "зелёные",
    "зеленым": "зелёным", "зеленой": "зелёной", "зеленом": "зелёном", "зеленую": "зелёную",
    "береза": "берёза", "березка": "берёзка", "березки": "берёзки", "березке": "берёзке",
    "клен": "клён", "клена": "клёна", "черемуха": "черёмуха", "пеленки": "пелёнки",
    "ребенок": "ребёнок", "ребенка": "ребёнка", "ребенку": "ребёнку", "ребенком": "ребёнком",
    "огонек": "огонёк", "огоньки": "огоньки", "еще": "ещё",
    "вдвоем": "вдвоём", "втроем": "втроём", "нипочем": "нипочём", "причем": "причём",
    "мое": "моё", "твое": "твоё", "свое": "своё", "ее": "её", "нее": "неё",
    "пес": "пёс", "песик": "пёсик", "песика": "пёсика", "ковер": "ковёр",
    "шел": "шёл", "ушел": "ушёл", "пришел": "пришёл", "нашел": "нашёл",
    "подошел": "подошёл", "зашел": "зашёл", "прошел": "прошёл", "обошел": "обошёл", "вошел": "вошёл",
    "шелк": "шёлк", "шелковый": "шёлковый", "шелковая": "шёлковая", "шелковое": "шёлковое",
    "расческа": "расчёска", "щетка": "щётка", "щеточка": "щёточка", "щеточкой": "щёточкой",
    "зернышко": "зёрнышко", "зернышки": "зёрнышки", "гнездышко": "гнёздышко", "ведерко": "ведёрко",
    "стеклышко": "стёклышко", "веревочка": "верёвочка", "тетя": "тётя", "тетушка": "тётушка",
    "сестры": "сёстры", "сестренка": "сестрёнка", "сестренки": "сестрёнки",
    "костер": "костёр", "шатер": "шатёр", "бобер": "бобёр", "осел": "осёл"
}

# 2. Словарь замены слов-омографов, в которых TTS ошибается с ударением
HOMOGRAPH_REPLACEMENTS = {
    "чудный": "чудесный", "чудная": "чудесная", "чудное": "чудесное", "чудные": "чудесные", "чудным": "чудесным",
    "белки": "белочки", "белок": "белочек",
    "кружки": "чашечки", "кружек": "чашечек", "кружке": "чашечке", "кружку": "чашечку",
    "стрелки": "стрелочки", "стрелок": "стрелочек",
    "замок": "дворец", "замка": "дворца", "замку": "дворцу", "замке": "дворце", "замком": "дворцом",
    "атлас": "шёлк", "атласный": "шёлковый", "атласная": "шёлковая", "атласное": "шёлковое"
}

YO_REGEX_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in YO_WORDS_DICT.keys()) + r')\b',
    flags=re.IGNORECASE
)

HOMOGRAPH_REGEX_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in HOMOGRAPH_REPLACEMENTS.keys()) + r')\b',
    flags=re.IGNORECASE
)

def _replace_preserving_case(match, lookup_dict):
    word = match.group(0)
    low = word.lower()
    target = lookup_dict.get(low, word)
    if word.istitle():
        return target.capitalize()
    if word.isupper():
        return target.upper()
    return target

YO_CONTEXT_PHRASES = [
    ("во всем теле", "во всём теле"),
    ("во всем мире", "во всём мире"),
    ("во всем доме", "во всём доме"),
    ("во всем лесу", "во всём лесу"),
    ("обо всем", "обо всём"),
    ("при всем", "при всём"),
    ("все хорошо", "всё хорошо"),
    ("все спокойно", "всё спокойно"),
    ("все тише", "всё тише"),
    ("все медленнее", "всё медленнее"),
    ("все вокруг", "всё вокруг"),
    ("все небо", "всё небо"),
    ("все тело", "всё тело"),
    ("все равно", "всё равно"),
    ("все будет", "всё будет"),
    ("все затихло", "всё затихло"),
    ("все уснуло", "всё уснуло"),
    ("все готово", "всё готово"),
    ("это все", "это всё"),
    ("вот и все", "вот и всё"),
    ("о чем", "о чём"),
    ("на чем", "на чём"),
    ("в чем", "в чём"),
    ("ни о чем", "ни о чём"),
    ("все-таки", "всё-таки"),
    ("дверной дворец", "дверной замок")
]

def prepare_russian_text_for_tts(text):
    """Нормализует текст для идеального русского произношения и правильных ударений."""
    # Убираем знаки ударения (Edge-TTS читает их по слогам с искажением)
    result = text.replace("\u0301", "").replace("́", "")

    # Заменяем слова-омографы на однозначные синонимы
    result = HOMOGRAPH_REGEX_PATTERN.sub(
        lambda m: _replace_preserving_case(m, HOMOGRAPH_REPLACEMENTS), result
    )

    # Применяем контекстные фразы с «ё»
    for old_p, new_p in YO_CONTEXT_PHRASES:
        result = re.sub(re.escape(old_p), new_p, result, flags=re.IGNORECASE)

    # Применяем словарь ёфикации
    result = YO_REGEX_PATTERN.sub(
        lambda m: _replace_preserving_case(m, YO_WORDS_DICT), result
    )

    # Очищаем пунктуацию для плавных пауз диктора
    result = re.sub(r'\.{2,}', '.', result)
    result = re.sub(r'\s*[—–-]\s*', ', ', result)
    result = re.sub(r'[«»"“”()]', '', result)
    result = re.sub(r'\n{2,}', '\n', result)
    result = re.sub(r'[ \t]+', ' ', result).strip()
    return result

# ==========================================
# 5. ФОНОВАЯ МУЗЫКАЛЬНАЯ ШКАТУЛКА И FFMPEG
# ==========================================
def synthesize_lullaby_loop_wav(wav_path=LULLABY_WAV_PATH, duration_sec=24):
    if os.path.exists(wav_path) and os.path.getsize(wav_path) > 10000:
        return
    sample_rate = 16000
    notes = [261.63, 329.63, 392.00, 523.25, 392.00, 329.63, 293.66, 392.00, 440.00, 392.00, 329.63, 261.63]
    note_dur = duration_sec / len(notes)
    total_samples = int(sample_rate * duration_sec)

    with wave.open(wav_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(total_samples):
            t = i / sample_rate
            n_idx = min(int(t / note_dur), len(notes) - 1)
            local_t = t - (n_idx * note_dur)
            freq = notes[n_idx]
            env = math.exp(-1.9 * local_t) * min(1.0, local_t / 0.04)
            val = (
                math.sin(2.0 * math.pi * freq * local_t) * 0.75
                + math.sin(2.0 * math.pi * (freq * 2.0) * local_t) * 0.25
            ) * env
            sample_int = int(max(-32767, min(32767, val * 4200)))
            frames.extend(struct.pack("<h", sample_int))
        wf.writeframes(frames)

def finalize_audio_with_lullaby_sync(raw_voice_path, final_audio_path, use_bg_music=1):
    """Склеивает голос с колыбельной шкатулкой в нативный формат OGG Opus."""
    if not shutil.which("ffmpeg"):
        shutil.move(raw_voice_path, final_audio_path)
        return

    try:
        if use_bg_music == 1:
            synthesize_lullaby_loop_wav(LULLABY_WAV_PATH)
            cmd = [
                "ffmpeg", "-y",
                "-i", raw_voice_path,
                "-stream_loop", "-1", "-i", LULLABY_WAV_PATH,
                "-filter_complex", "[1:a]volume=0.07[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[out]",
                "-map", "[out]",
                "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
                final_audio_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-i", raw_voice_path,
                "-c:a", "libopus", "-b:a", "48k", "-vbr", "on",
                final_audio_path
            ]

        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        if res.returncode == 0 and os.path.exists(final_audio_path) and os.path.getsize(final_audio_path) > 3000:
            return
    except Exception as e:
        logging.warning(f"Переход на стандартный файл при микшировании: {e}")

    shutil.move(raw_voice_path, final_audio_path)

async def synthesize_speech_to_file(text_to_read, final_audio_path, voice_id, use_bg_music=1):
    """Генерирует русскую речь через Edge-TTS с проверкой орфоэпии и наложением шкатулки."""
    phonetic_text = prepare_russian_text_for_tts(text_to_read)
    raw_mp3_path = f"{final_audio_path}_raw.mp3"
    chosen_voice = voice_id if voice_id in AVAILABLE_VOICES else DEFAULT_VOICE

    try:
        async with tts_semaphore:
            async def _do_tts(_attempt):
                communicate = edge_tts.Communicate(phonetic_text, chosen_voice, rate="-6%")
                await asyncio.wait_for(communicate.save(raw_mp3_path), timeout=40.0)
                if not os.path.exists(raw_mp3_path) or os.path.getsize(raw_mp3_path) < 2000:
                    raise RuntimeError("Пустой аудиофайл от TTS")

            await retry_with_backoff(_do_tts, retries=3, base_delay=0.8, max_delay=3.0)

        if os.path.exists(raw_mp3_path):
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                tts_thread_pool, finalize_audio_with_lullaby_sync, raw_mp3_path, final_audio_path, use_bg_music
            )
    finally:
        if os.path.exists(raw_mp3_path):
            try:
                os.remove(raw_mp3_path)
            except Exception:
                pass

# ==========================================
# 6. КЛАВИАТУРЫ И ИНТЕРФЕЙС
# ==========================================
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()
    waiting_for_custom_theme = State()
    waiting_for_promo = State()

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪄 Создать новую сказку"), KeyboardButton(text="🔄 Продолжить сказку")],
        [KeyboardButton(text="🧸 Сказкотерапия (страхи/сон)"), KeyboardButton(text="👶 Профили детей")],
        [KeyboardButton(text="🎙 Голос и музыка"), KeyboardButton(text="⏰ Время спать")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="💎 Тарифы и Подписка")]
    ],
    resize_keyboard=True
)

theme_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="😴 На крепкий сон"), KeyboardButton(text="🥣 Хороший аппетит")],
        [KeyboardButton(text="🧸 Делиться игрушками"), KeyboardButton(text="🦷 Чистить зубки")],
        [KeyboardButton(text="✍️ Своя тема"), KeyboardButton(text="🏠 В главное меню")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

therapy_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🌑 Не бояться темноты"), KeyboardButton(text="🏫 С радостью идти в садик")],
        [KeyboardButton(text="🩺 Не бояться врачей"), KeyboardButton(text="🧹 Убирать свои игрушки")],
        [KeyboardButton(text="🛌 Засыпать в своей кроватке"), KeyboardButton(text="🏠 В главное меню")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

def get_tariffs_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=TARIFFS["pack_5"]["btn_text"], callback_data="buy_pack_5")],
            [InlineKeyboardButton(text=TARIFFS["pack_15"]["btn_text"], callback_data="buy_pack_15")],
            [InlineKeyboardButton(text=TARIFFS["sub_month"]["btn_text"], callback_data="buy_sub_month")],
            [InlineKeyboardButton(text="🎟 Ввести промокод на 5 сказок", callback_data="enter_promo")],
            [InlineKeyboardButton(text="🎁 +2 сказки за приглашённого друга", callback_data="ref_program")]
        ]
    )

def get_post_story_inline_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продолжить эту историю", callback_data="action_continue")],
            [InlineKeyboardButton(text="🎲 Новая тема с этим же героем", callback_data="action_same_hero")],
            [InlineKeyboardButton(text="👶 Сменить профиль ребёнка", callback_data="manage_children")],
            [InlineKeyboardButton(text="🎁 +2 сказки за друга", callback_data="ref_program")]
        ]
    )

def get_family_profiles_inline_kb(user_id):
    profiles = get_family_profiles(user_id)
    rows = []
    for pid, c_name, h_name, _, _, s_cnt, is_act in profiles:
        mark = "✅ " if is_act == 1 else "👶 "
        rows.append([
            InlineKeyboardButton(
                text=f"{mark}{c_name} — {h_name} ({s_cnt} ск.)",
                callback_data=f"selectchild_{pid}"
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"delchild_{pid}")
        ])
    if len(profiles) < MAX_CHILDREN_PER_FAMILY:
        rows.append([InlineKeyboardButton(text="➕ Добавить профиль ребёнка", callback_data="add_new_child")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_voice_and_music_kb(current_voice, bg_music=1):
    """Клавиатура выбора голоса с кнопками предварительного прослушивания каждого диктора."""
    rows = []
    for v_id, v_label in AVAILABLE_VOICES.items():
        mark = "✅ " if v_id == current_voice else ""
        rows.append([
            InlineKeyboardButton(text=f"{mark}{v_label}", callback_data=f"setvoice_{v_id}"),
            InlineKeyboardButton(text="▶️ Прослушать", callback_data=f"preview_{v_id}")
        ])
    bg_label = "✅ Вкл (Музыкальная шкатулка)" if bg_music == 1 else "🔕 Выкл (Только голос)"
    rows.append([InlineKeyboardButton(text=f"🎵 Колыбельная фоном: {bg_label}", callback_data="toggle_bg_music")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_reminder_inline_kb(current_time, current_tz):
    times = ["20:00", "20:30", "21:00", "21:30", "22:00"]
    time_btns = []
    for t in times:
        mark = "✅ " if t == current_time else ""
        time_btns.append(InlineKeyboardButton(text=f"{mark}{t}", callback_data=f"remind_time_{t}"))

    tz_btns = []
    for code, (label, offset) in TIMEZONE_OFFSETS.items():
        mark = "✅ " if offset == current_tz else ""
        tz_btns.append([InlineKeyboardButton(text=f"🌍 Пояс: {mark}{label}", callback_data=f"remind_tz_{code}")])

    rows = [
        time_btns[:3],
        time_btns[3:],
        *tz_btns,
        [InlineKeyboardButton(text="🔕 Выключить напоминание", callback_data="remind_off")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ==========================================
# 7. ОБРАБОТЧИКИ МЕНЮ, ПРОФИЛЕЙ И ПРОСЛУШИВАНИЯ ГОЛОСА
# ==========================================
@dp.message(F.text == "🏠 В главное меню")
@dp.message(Command("cancel"))
async def cancel_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard)

@dp.message(F.text == "👶 Профили детей")
async def show_children_profiles_menu(message: Message):
    user_id = message.from_user.id
    profiles = get_family_profiles(user_id)
    if not profiles:
        await message.answer(
            "👶 В вашем семейном профиле пока нет сохранённых карточек детей.\n"
            "Нажмите кнопку ниже, чтобы добавить первого малыша (доступно до 3 профилей):",
            reply_markup=get_family_profiles_inline_kb(user_id)
        )
        return
    await message.answer(
        f"👨‍👩‍👧‍👦 Семейный мультипрофиль ({len(profiles)} из {MAX_CHILDREN_PER_FAMILY}):\n\n"
        f"У каждого ребёнка сохраняется свой любимый герой и своя история серий.\n"
        f"Нажмите на имя ребёнка, чтобы сделать его активным для следующей сказки:",
        reply_markup=get_family_profiles_inline_kb(user_id)
    )

@dp.callback_query(F.data == "manage_children")
async def manage_children_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "👨‍👩‍👧‍👦 Выберите профиль ребёнка для следующей сказки или добавьте нового:",
        reply_markup=get_family_profiles_inline_kb(callback.from_user.id)
    )

@dp.callback_query(F.data.startswith("selectchild_"))
async def select_child_profile_cb(callback: CallbackQuery):
    pid_str = callback.data.replace("selectchild_", "")
    if not pid_str.isdigit():
        await callback.answer()
        return
    row = switch_active_child_profile(callback.from_user.id, int(pid_str))
    if row:
        c_name, h_name, _, _ = row
        await callback.answer(f"Выбран профиль: {c_name}")
        await callback.message.edit_text(
            f"✅ Активный профиль: {c_name} (Любимый герой: {h_name}).\n\n"
            f"Теперь все новые сказки и продолжения серий будут создаваться для этого малыша!",
            reply_markup=get_family_profiles_inline_kb(callback.from_user.id)
        )
    else:
        await callback.answer("Профиль не найден", show_alert=True)

@dp.callback_query(F.data.startswith("delchild_"))
async def delete_child_profile_cb(callback: CallbackQuery):
    pid_str = callback.data.replace("delchild_", "")
    if pid_str.isdigit():
        delete_child_profile_db(callback.from_user.id, int(pid_str))
        await callback.answer("Профиль удалён")
        await callback.message.edit_text(
            "👨‍👩‍👧‍👦 Список профилей детей обновлён:",
            reply_markup=get_family_profiles_inline_kb(callback.from_user.id)
        )

@dp.callback_query(F.data == "add_new_child")
async def add_new_child_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    profiles = get_family_profiles(callback.from_user.id)
    if len(profiles) >= MAX_CHILDREN_PER_FAMILY:
        await callback.message.answer(
            f"Достигнут максимум ({MAX_CHILDREN_PER_FAMILY} профиля). Удалите старый профиль значком 🗑."
        )
        return
    await state.update_data(adding_profile_only=True)
    await callback.message.answer(
        "👶 Напишите имя и возраст ребёнка для нового профиля:\n(Например: Артём, 4 года)",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(Command("promo"))
async def promo_command_handler(message: Message, state: FSMContext, command: CommandObject = None):
    if command and command.args:
        _, msg_text = activate_single_promocode_db(message.from_user.id, command.args)
        await message.answer(msg_text, reply_markup=main_menu_keyboard)
        return
    await message.answer("🎟 Введите подарочный промокод в ответном сообщении:")
    await state.set_state(StoryCreation.waiting_for_promo)

@dp.callback_query(F.data == "enter_promo")
async def enter_promo_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🎟 Напишите ваш промокод в ответном сообщении:")
    await state.set_state(StoryCreation.waiting_for_promo)

@dp.message(StoryCreation.waiting_for_promo, F.text)
async def process_promo_input(message: Message, state: FSMContext):
    await state.clear()
    _, msg_text = activate_single_promocode_db(message.from_user.id, message.text)
    await message.answer(msg_text, reply_markup=main_menu_keyboard)

@dp.message(F.text.in_({"🎙 Голос и музыка", "🎨 Стиль и Голос", "🎙 Голос диктора"}))
async def choose_voice_and_music_cmd(message: Message):
    _, _, _, _, _, current_voice, _, _, _, _, bg_music = get_user_data(message.from_user.id)
    bg_txt = "Включена (Музыкальная шкатулка)" if bg_music == 1 else "Выключена (Только голос)"
    await message.answer(
        f"🎙 Настройка голоса диктора и фоновой мелодии\n\n"
        f"Выбранный диктор: {AVAILABLE_VOICES.get(current_voice, AVAILABLE_VOICES[DEFAULT_VOICE])}\n"
        f"Колыбельная фоном: {bg_txt}\n\n"
        f"Нажмите «▶️ Прослушать», чтобы оценить звучание любого диктора перед созданием сказки:",
        reply_markup=get_voice_and_music_kb(current_voice, bg_music)
    )

@dp.callback_query(F.data.startswith("setvoice_"))
async def set_voice_callback(callback: CallbackQuery):
    voice_id = callback.data.replace("setvoice_", "")
    if voice_id in AVAILABLE_VOICES:
        set_user_voice(callback.from_user.id, voice_id)
        _, _, _, _, _, _, _, _, _, _, bg_music = get_user_data(callback.from_user.id)
        bg_txt = "Включена (Музыкальная шкатулка)" if bg_music == 1 else "Выключена (Только голос)"
        await callback.answer(f"Выбран голос: {AVAILABLE_VOICES[voice_id]}")
        await callback.message.edit_text(
            f"🎙 Настройка голоса диктора и фоновой мелодии\n\n"
            f"✅ Выбранный диктор: {AVAILABLE_VOICES[voice_id]}\n"
            f"🎵 Колыбельная фоном: {bg_txt}\n\n"
            f"Нажмите «▶️ Прослушать», чтобы услышать пример голоса:",
            reply_markup=get_voice_and_music_kb(voice_id, bg_music)
        )

@dp.callback_query(F.data == "toggle_bg_music")
async def toggle_bg_music_callback(callback: CallbackQuery):
    new_bg = toggle_user_bg_music(callback.from_user.id)
    _, _, _, _, _, current_voice, _, _, _, _, _ = get_user_data(callback.from_user.id)
    status_txt = "Включена (Музыкальная шкатулка)" if new_bg == 1 else "Выключена (Только голос)"
    await callback.answer(f"Колыбельная фоном: {status_txt}")
    await callback.message.edit_text(
        f"🎙 Настройка голоса диктора и фоновой мелодии\n\n"
        f"✅ Выбранный диктор: {AVAILABLE_VOICES.get(current_voice, AVAILABLE_VOICES[DEFAULT_VOICE])}\n"
        f"🎵 Колыбельная фоном: {status_txt}\n\n"
        f"Нажмите «▶️ Прослушать», чтобы услышать пример голоса:",
        reply_markup=get_voice_and_music_kb(current_voice, new_bg)
    )

@dp.callback_query(F.data.startswith("preview_"))
async def preview_voice_callback(callback: CallbackQuery):
    voice_id = callback.data.replace("preview_", "")
    if voice_id not in AVAILABLE_VOICES:
        await callback.answer("Голос не найден", show_alert=True)
        return

    user_id = callback.from_user.id
    _, _, _, _, _, _, _, _, _, _, bg_music = get_user_data(user_id)
    voice_title = AVAILABLE_VOICES[voice_id]
    bg_note = "с музыкальной шкатулкой" if bg_music == 1 else "без фоновой музыки"

    cached_fid = get_cached_voice_sample(voice_id, bg_music)
    if cached_fid:
        try:
            await callback.answer(f"Включаю пример: {voice_title}")
            await callback.message.answer_voice(
                voice=cached_fid,
                caption=f"🎧 Пример звучания: {voice_title} ({bg_note})"
            )
            return
        except Exception:
            pass

    await callback.answer("Записываю пример голоса (пару секунд)...")
    sample_path = (
        f"/tmp/sample_{user_id}_{int(time.time())}.ogg"
        if os.path.exists("/tmp")
        else f"sample_{user_id}_{int(time.time())}.ogg"
    )
    try:
        async with ChatActionSender.record_voice(bot=bot, chat_id=callback.message.chat.id):
            sample_text = VOICE_PREVIEW_TEXTS.get(voice_id, VOICE_PREVIEW_TEXTS[DEFAULT_VOICE])
            await synthesize_speech_to_file(sample_text, sample_path, voice_id, bg_music)
            sent_msg = await callback.message.answer_voice(
                voice=FSInputFile(sample_path),
                caption=f"🎧 Пример звучания: {voice_title} ({bg_note})"
            )
            if sent_msg.voice:
                save_cached_voice_sample(voice_id, bg_music, sent_msg.voice.file_id)
    except Exception as e:
        logging.warning(f"Ошибка генерации превью голоса: {e}")
        await callback.message.answer("⚠️ Не удалось создать аудио-пример, попробуйте через пару секунд.")
    finally:
        if os.path.exists(sample_path):
            try:
                os.remove(sample_path)
            except Exception:
                pass

@dp.message(F.text == "⏰ Время спать")
@dp.message(Command("remind"))
async def bedtime_reminder_menu(message: Message):
    _, _, _, _, _, _, _, remind_time, tz_offset, _, _ = get_user_data(message.from_user.id)
    status_str = f"Включено на {remind_time} (UTC+{tz_offset})" if remind_time else "Выключено"
    await message.answer(
        f"⏰ Вечернее напоминание «Время спать»\n\n"
        f"Бот мягко напомнит вам вечером, что пора приглушить свет и включить новую сказку малышу.\n\n"
        f"Текущий статус: {status_str}\n"
        f"Выберите ваш часовой пояс и удобное время:",
        reply_markup=get_reminder_inline_kb(remind_time, tz_offset)
    )

@dp.callback_query(F.data.startswith("remind_time_"))
async def set_remind_time_cb(callback: CallbackQuery):
    chosen_time = callback.data.replace("remind_time_", "")
    _, _, _, _, _, _, _, _, tz_offset, _, _ = get_user_data(callback.from_user.id)
    set_user_reminder(callback.from_user.id, chosen_time)
    await callback.answer(f"Напоминание установлено на {chosen_time}!")
    await callback.message.edit_text(
        f"✅ Вечернее напоминание установлено на {chosen_time} (UTC+{tz_offset})!",
        reply_markup=get_reminder_inline_kb(chosen_time, tz_offset)
    )

@dp.callback_query(F.data.startswith("remind_tz_"))
async def set_remind_tz_cb(callback: CallbackQuery):
    tz_code = callback.data.replace("remind_tz_", "")
    if tz_code in TIMEZONE_OFFSETS:
        _, offset = TIMEZONE_OFFSETS[tz_code]
        _, _, _, _, _, _, _, remind_time, _, _, _ = get_user_data(callback.from_user.id)
        active_t = remind_time if remind_time else "21:00"
        set_user_reminder(callback.from_user.id, active_t, offset)
        await callback.answer(f"Часовой пояс UTC+{offset} сохранён!")
        await callback.message.edit_text(
            f"✅ Часовой пояс изменён на UTC+{offset}. Время напоминания: {active_t}",
            reply_markup=get_reminder_inline_kb(active_t, offset)
        )

@dp.callback_query(F.data == "remind_off")
async def set_remind_off_cb(callback: CallbackQuery):
    _, _, _, _, _, _, _, _, tz_offset, _, _ = get_user_data(callback.from_user.id)
    set_user_reminder(callback.from_user.id, "")
    await callback.answer("Напоминания выключены")
    await callback.message.edit_text(
        "🔕 Вечерние напоминания выключены:",
        reply_markup=get_reminder_inline_kb("", tz_offset)
    )

async def bedtime_reminder_scheduler():
    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                c = conn.cursor()
                rows = c.execute(
                    "SELECT user_id, remind_time, tz_offset, last_reminded_date, "
                    "last_child, last_hero, tomorrow_hook FROM users WHERE remind_time != ''"
                ).fetchall()

                for uid, r_time, tz_off, last_date, l_child, l_hero, t_hook in rows:
                    user_now = utc_now + timedelta(hours=int(tz_off or 3))
                    today_str = user_now.strftime("%Y-%m-%d")
                    current_hm = user_now.strftime("%H:%M")

                    if current_hm == r_time and last_date != today_str:
                        conn.execute(
                            "UPDATE users SET last_reminded_date = ? WHERE user_id = ?",
                            (today_str, uid)
                        )
                        conn.commit()
                        clean_name = extract_clean_name(l_child) if l_child else ""
                        child_label = f" для {clean_name}" if clean_name else ""
                        hook_line = f"\n\n🔮 Анонс новой серии: {t_hook}" if t_hook else ""
                        try:
                            await bot.send_message(
                                uid,
                                f"🌙 Время готовиться ко сну!\n\n"
                                f"Пора проветрить спальню, приглушить свет и включить "
                                f"новую добрую сказку{child_label} ✨{hook_line}",
                                reply_markup=get_post_story_inline_kb() if l_hero else main_menu_keyboard
                            )
                        except Exception:
                            pass
        except Exception as e:
            logging.warning(f"Ошибка планировщика напоминаний: {e}")
        await asyncio.sleep(45)

@dp.message(Command("myid"))
async def show_my_id(message: Message):
    uid = message.from_user.id
    admin_status = "✅ Вы Администратор!" if is_admin(uid) else "ℹ️ Добавьте ADMIN_ID в Render."
    await message.answer(f"🔑 Ваш Telegram ID: {uid}\n\n{admin_status}", reply_markup=main_menu_keyboard)

@dp.message(Command("stats"))
async def admin_stats_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    u_cnt, prof_cnt, s_cnt, p_cnt, stars_sum, utm_rows = get_global_stats()
    q_size = story_task_queue.qsize() if story_task_queue else 0
    utm_text = "\n".join([f"  • {src}: {cnt} чел." for src, cnt in utm_rows]) if utm_rows else "  Пока нет переходов по меткам"
    await message.answer(
        f"📊 Статистика бота и рекламы:\n\n"
        f"📥 Задач в очереди: {q_size}\n"
        f"👥 Семей: {u_cnt} | 👶 Детей: {prof_cnt}\n"
        f"📚 Создано сказок: {s_cnt}\n"
        f"💳 Оплат: {p_cnt} ({stars_sum} ⭐ / ~{stars_sum * 2} ₽)\n\n"
        f"📈 Источники трафика (UTM):\n{utm_text}"
    )

@dp.message(Command("backup"))
async def manual_backup_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await send_db_backup_to_admin("Ручной запрос /backup")

@dp.message(F.document, F.caption == "/restore")
async def restore_db_from_file(message: Message):
    if not is_admin(message.from_user.id):
        return
    doc = message.document
    if not doc.file_name.endswith(".db"):
        await message.answer("⚠️ Отправьте файл с расширением .db")
        return
    temp_restore = f"{DB_PATH}.restore"
    try:
        await bot.download(doc, destination=temp_restore)
        shutil.move(temp_restore, DB_PATH)
        init_db()
        await message.answer("✅ База данных успешно восстановлена!")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка восстановления БД: {e}")

@dp.message(Command("terms"))
async def terms_cmd(message: Message):
    await message.answer(
        "📜 Пользовательское соглашение:\n"
        "1. Бот генерирует персональные аудиосказки на ночь с помощью ИИ.\n"
        "2. Оплата пакетов и подписки производится через официальные Telegram Stars (XTR).\n"
        "3. Поддержка пользователей: /paysupport."
    )

@dp.message(Command("paysupport"))
async def paysupport_cmd(message: Message):
    await message.answer("🛎 Напишите ваш вопрос с пометкой #оплата — он будет сразу передан администратору.")

@dp.message(F.text.contains("#оплата"))
async def forward_support_to_admin(message: Message):
    if ADMIN_ID != 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆘 Обращение от ID {message.from_user.id} (@{message.from_user.username}):\n\n{message.text}"
            )
        except Exception:
            pass
    await message.answer("✅ Ваше сообщение передано администратору!")

@dp.message(F.text.in_({"👤 Мой профиль", "👤 Мой баланс"}))
async def show_balance(message: Message):
    user_id = message.from_user.id
    balance, total, last_child, last_hero, _, voice, refs_cnt, remind_time, tz_off, _, bg_music = get_user_data(user_id)
    has_sub, sub_days = get_subscription_status(user_id)
    profiles = get_family_profiles(user_id)
    voice_name = AVAILABLE_VOICES.get(voice, AVAILABLE_VOICES[DEFAULT_VOICE])

    if has_sub:
        status_line = f"👑 Безлимитная подписка: активна (ещё {sub_days} дн.)\n🎧 Запасных сказок: {balance} шт."
    else:
        status_line = f"🎧 Доступно сказок: {balance} шт."

    if profiles:
        children_lines = "\n".join(
            [f"  {'✅' if p[6] == 1 else '👶'} {p[1]} — герой: {p[2]} ({p[5]} ск.)" for p in profiles]
        )
        family_block = f"\n\n👨‍👩‍👧‍👦 Профили детей:\n{children_lines}"
    else:
        family_block = f"\n🧸 Активный герой: {last_hero} (для: {last_child})" if last_hero else ""

    remind_info = f"\n⏰ Напоминание ко сну: {remind_time} (UTC+{tz_off})" if remind_time else "\n⏰ Напоминание: выкл."
    bg_info = "Вкл (Музыкальная шкатулка)" if bg_music == 1 else "Выкл"
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    profile_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👶 Управление профилями детей", callback_data="manage_children")],
            [InlineKeyboardButton(text="🎟 Ввести промокод на 5 сказок", callback_data="enter_promo")],
            [InlineKeyboardButton(text="🎁 Пригласить друга (+2 сказки)", callback_data="ref_program")]
        ]
    )

    await message.answer(
        f"👤 Семейный профиль:\n\n"
        f"{status_line}\n"
        f"📚 Всего прослушано сказок: {total} шт.{family_block}\n\n"
        f"🎙 Голос диктора: {voice_name}\n"
        f"🎵 Колыбельная фоном: {bg_info}{remind_info}\n"
        f"👥 Приглашено друзей: {refs_cnt}\n\n"
        f"🎁 Ваша ссылка (+{REFERRAL_BONUS_STORIES} сказки за друга):\n{ref_link}",
        reply_markup=profile_kb
    )

@dp.callback_query(F.data == "ref_program")
async def ref_program_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    await callback.answer()
    await callback.message.answer(
        f"🎁 Дарим бесплатные сказки за друзей!\n\n"
        f"Перешлите эту ссылку знакомым родителям:\n{ref_link}\n\n"
        f"За каждого друга вам автоматически начислится +{REFERRAL_BONUS_STORIES} бесплатные сказки!",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text.in_({"💎 Тарифы и Подписка", "💎 Купить сказки (\u2b50 Звёзды)"}))
async def show_tariffs(message: Message):
    user_id = message.from_user.id
    balance, _, _, _, _, _, _, _, _, tomorrow_hook, _ = get_user_data(user_id)
    has_sub, sub_days = get_subscription_status(user_id)
    sub_str = f"👑 Активна безлимитная подписка (ещё {sub_days} дн.)\n" if has_sub else ""
    hook_block = f"\n🔮 В следующей серии:\n«{tomorrow_hook}»\n" if tomorrow_hook else ""

    await message.answer(
        f"💎 Тарифы и Месячная подписка\n\n"
        f"{sub_str}🎧 Доступно сказок на балансе: {balance} шт.{hook_block}\n"
        f"• 5 сказок — 99 ₽ (50 ⭐)\n"
        f"• 15 сказок — 199 ₽ (100 ⭐)\n"
        f"• Подписка на месяц (30 дней безлимита) — 499 ₽ (250 ⭐)\n\n"
        f"Каждая сказка включает индивидуальный сюжет про вашего малыша, вопросы на обсуждение и мягкую озвучку под музыкальную шкатулку:",
        reply_markup=get_tariffs_inline_kb()
    )

@dp.message(Command("add_free"))
async def add_free_cmd(message: Message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.answer("🎁 Используйте промокод ДОБРО или пригласите друга в разделе «👤 Мой профиль»!")
        return
    new_bal = add_stories_to_user(user_id, 5)
    await message.answer(f"🎁 Начислено +5 сказок! Ваш баланс: {new_bal} шт.", reply_markup=main_menu_keyboard)

@dp.callback_query(F.data.startswith("buy_"))
async def send_stars_invoice(callback: CallbackQuery):
    pack_key = callback.data.replace("buy_", "")
    if pack_key not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    t = TARIFFS[pack_key]
    await callback.answer()
    desc = (
        "Безлимитный доступ к созданию персональных аудиосказок на ночь на 30 дней."
        if t["days"] > 0
        else f"Пакет на {t['stories']} персональных аудиосказок с колыбельной шкатулкой."
    )
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"⭐ {t['title']} ({t['rub']} ₽)",
        description=desc,
        payload=f"tariff:{pack_key}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{t['title']} (~{t['rub']} ₽)", amount=t["stars"])]
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payment = message.successful_payment
    charge_id = payment.telegram_payment_charge_id
    payload = payment.invoice_payload
    stars_paid = payment.total_amount

    pack_key = payload.replace("tariff:", "")
    t = TARIFFS.get(pack_key, TARIFFS["pack_5"])

    is_new, new_balance = record_payment_idempotent(
        charge_id, message.from_user.id, t["stories"], t["days"], stars_paid
    )
    if is_new:
        await send_db_backup_to_admin(
            f"Оплата тарифа {pack_key} ({t['rub']} ₽ / {stars_paid} XTR) от ID {message.from_user.id}"
        )

    if t["days"] > 0:
        _, days_left = get_subscription_status(message.from_user.id)
        await message.answer(
            f"🎉 Оплата прошла успешно!\n"
            f"👑 Ваша безлимитная подписка на месяц активирована (доступно {days_left} дн.). Приятных сказок!",
            reply_markup=main_menu_keyboard
        )
    else:
        await message.answer(
            f"🎉 Оплата прошла успешно!\n"
            f"Начислено: +{t['stories']} сказок. Всего доступно на балансе: {new_balance} шт.",
            reply_markup=main_menu_keyboard
        )

# ==========================================
# 8. ДИАЛОГИ ВЫБОРА И ПОСТАНОВКА В ОЧЕРЕДЬ
# ==========================================
@dp.message(CommandStart())
async def start_command_handler(message: Message, state: FSMContext, command: CommandObject = None):
    await state.clear()
    user_id = message.from_user.id
    referrer_id = 0
    utm_source = ""

    if command and command.args:
        arg_val = command.args.strip()
        if arg_val.startswith("ref_"):
            ref_str = arg_val.replace("ref_", "").strip()
            if ref_str.isdigit():
                referrer_id = int(ref_str)
        else:
            utm_source = sanitize_input(arg_val, max_len=40)

    balance, _, _, _, _, _, _, _, _, _, _, ref_rewarded = register_or_get_user(
        user_id, referrer_id, utm_source
    )

    if ref_rewarded and referrer_id > 0:
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 По вашей ссылке присоединился друг! Вам начислено +{REFERRAL_BONUS_STORIES} бесплатные сказки!"
            )
            await send_db_backup_to_admin(f"Новый реферал от ID {referrer_id}")
        except Exception:
            pass

    await message.answer(
        f"👋 Добро пожаловать в мир добрых персональных аудиосказок!\n\n"
        f"✨ Бот сочиняет уютную сказку на ночь про вашего ребёнка и его любимую игрушку "
        f"и озвучивает её живым ласковым голосом под мелодию музыкальной шкатулки.\n\n"
        f"🎙 В меню «Голос и музыка» можно заранее прослушать и выбрать голос диктора.\n"
        f"🎁 На вашем балансе: {balance} бесплатные сказки (а по промокоду ДОБРО можно получить ещё +5 сказок!).",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text == "🪄 Создать новую сказку")
async def create_new_story_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    _, _, _, _, _, _, _, _, _, tomorrow_hook, _ = get_user_data(user_id)

    if not has_available_stories(user_id):
        hook_msg = f"\n\n🔮 В следующей серии: «{tomorrow_hook}»" if tomorrow_hook else ""
        await message.answer(
            f"🌙 Бесплатные сказки закончились!{hook_msg}\nВыберите удобный пакет или активируйте промокод ДОБРО:",
            reply_markup=get_tariffs_inline_kb()
        )
        return

    profiles = get_family_profiles(user_id)
    if profiles:
        kb_rows = []
        for _, c_name, h_name, _, _, _, is_act in profiles:
            star = "⚡ " if is_act == 1 else "👶 "
            kb_rows.append([KeyboardButton(text=f"{star}Для: {c_name} | Герой: {h_name}")])
        kb_rows.append([KeyboardButton(text="➕ Добавить другого ребёнка"), KeyboardButton(text="🏠 В главное меню")])

        quick_kb = ReplyKeyboardMarkup(keyboard=kb_rows, resize_keyboard=True, one_time_keyboard=True)
        await message.answer(
            "👨‍👩‍👧‍👦 Выберите профиль ребёнка кнопкой ниже (или напишите новое имя и возраст):",
            reply_markup=quick_kb
        )
    else:
        await message.answer(
            "👶 Шаг 1 из 3: Напишите имя и возраст ребёнка:\n(Например: Миша, 4 года)",
            reply_markup=ReplyKeyboardRemove()
        )
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(F.text == "🧸 Сказкотерапия (страхи/сон)")
async def start_therapy_mode(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    _, _, last_child, last_hero, _, _, _, _, _, _, _ = get_user_data(user_id)

    if not has_available_stories(user_id):
        await message.answer("🌙 Сказки на балансе закончились! Пополните баланс:", reply_markup=get_tariffs_inline_kb())
        return

    if last_child and last_hero:
        await state.update_data(child_info=last_child, hero=last_hero, is_continuation=False)
        await message.answer(
            f"🧸 Сказкотерапия для профиля: {last_child} (герой: {last_hero}).\n"
            f"Выберите тему для мягкой проработки:",
            reply_markup=therapy_keyboard
        )
        await state.set_state(StoryCreation.waiting_for_theme)
    else:
        await state.update_data(therapy_mode=True)
        await message.answer(
            "🧸 Режим сказкотерапии.\nНапишите имя и возраст ребёнка (например: Аня, 3 года):",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(F.text.in_({"🔄 Продолжение вчерашней сказки", "🔄 Продолжить сказку"}))
async def continue_story_cmd(message: Message, state: FSMContext):
    await state.clear()
    await enqueue_continuation(message, message.from_user.id)

@dp.callback_query(F.data == "action_continue")
async def continue_story_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await enqueue_continuation(callback.message, callback.from_user.id)

@dp.callback_query(F.data == "action_same_hero")
async def same_hero_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    user_id = callback.from_user.id
    _, _, last_child, last_hero, _, _, _, _, _, _, _ = get_user_data(user_id)
    if not has_available_stories(user_id):
        await callback.message.answer("🌙 Сказки закончились! Выберите пакет ниже:", reply_markup=get_tariffs_inline_kb())
        return
    if not last_child or not last_hero:
        await callback.message.answer("Нажмите «🪄 Создать новую сказку», чтобы задать имя и героя.")
        return

    await state.update_data(child_info=last_child, hero=last_hero, is_continuation=False)
    await callback.message.answer(
        f"🎯 Выберите новую тему для приключений ({extract_clean_name(last_child)} и {last_hero}):",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

async def enqueue_continuation(message: Message, user_id):
    _, _, last_child, last_hero, last_summary, _, _, _, _, tomorrow_hook, _ = get_user_data(user_id)
    if not has_available_stories(user_id):
        hook_msg = f"\n\n🔮 В этой серии: «{tomorrow_hook}»" if tomorrow_hook else ""
        await message.answer(f"🌙 Сказки закончились!{hook_msg}\nПополните баланс:", reply_markup=get_tariffs_inline_kb())
        return
    if not last_summary or not last_hero:
        await message.answer(
            f"Для профиля «{last_child or 'малыша'}» пока нет сохранённой первой серии. Нажмите «🪄 Создать новую сказку»!",
            reply_markup=main_menu_keyboard
        )
        return

    next_theme = tomorrow_hook if tomorrow_hook else "Новое доброе вечернее приключение и сладкий сон"
    await enqueue_story_job(
        message=message,
        user_id=user_id,
        child=last_child or "Малыш",
        hero=last_hero,
        theme_text=next_theme,
        is_continuation=True,
        prev_summary=last_summary
    )

@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "➕ Добавить другого ребёнка":
        await message.answer(
            "👶 Напишите имя и возраст ребёнка:\n(Например: Артём, 5 лет)",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if "Для:" in text and "| Герой:" in text:
        try:
            after_for = text.split("Для:", 1)[1]
            c_part, h_part = after_for.split("| Герой:", 1)
            c_clean = sanitize_input(c_part, max_len=60)
            h_clean = sanitize_input(h_part, max_len=60)
            if c_clean and h_clean:
                save_or_activate_child_profile(message.from_user.id, c_clean, h_clean)
                await state.update_data(child_info=c_clean, hero=h_clean, is_continuation=False)
                await message.answer("🎯 Шаг 3 из 3: Выберите тему сказки:", reply_markup=theme_keyboard)
                await state.set_state(StoryCreation.waiting_for_theme)
                return
        except Exception:
            pass

    clean_child = sanitize_input(text, max_len=60)
    if len(clean_child) < 2:
        await message.answer("Пожалуйста, напишите имя и возраст ребёнка (например: София, 4 года):")
        return

    await state.update_data(child_info=clean_child, is_continuation=False)
    await message.answer(
        "🧸 Шаг 2 из 3: Кто любимый персонаж или игрушка малыша?\n(Например: плюшевый мишка Топа, зайчик Снежок, синий трактор)",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryCreation.waiting_for_hero)

@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    clean_hero = sanitize_input(message.text, max_len=60)
    if len(clean_hero) < 2:
        await message.answer("Пожалуйста, напишите имя любимого героя или игрушки:")
        return

    data = await state.get_data()
    child_name = data.get("child_info", "Малыш")
    save_or_activate_child_profile(message.from_user.id, child_name, clean_hero)

    if data.get("adding_profile_only"):
        await state.clear()
        await message.answer(
            f"✅ Профиль «{child_name}» (Герой: {clean_hero}) сохранён и сделан активным!",
            reply_markup=main_menu_keyboard
        )
        return

    await state.update_data(hero=clean_hero)
    kb = therapy_keyboard if data.get("therapy_mode") else theme_keyboard
    await message.answer("🎯 Шаг 3 из 3: Выберите тему сказки или напишите свою:", reply_markup=kb)
    await state.set_state(StoryCreation.waiting_for_theme)

@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme_choice(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("✍️ Напишите кратко, о чём должна быть сказка (до 140 символов):")
        await state.set_state(StoryCreation.waiting_for_custom_theme)
        return

    clean_theme = sanitize_input(theme_text, max_len=140)
    data = await state.get_data()
    await state.clear()
    await enqueue_story_job(
        message=message,
        user_id=message.from_user.id,
        child=data.get("child_info", "Малыш"),
        hero=data.get("hero", "Котёнок"),
        theme_text=clean_theme,
        is_continuation=False,
        prev_summary=""
    )

@dp.message(StoryCreation.waiting_for_custom_theme, F.text)
async def process_custom_theme(message: Message, state: FSMContext):
    clean_theme = sanitize_input(message.text, max_len=140)
    if len(clean_theme) < 3:
        await message.answer("Пожалуйста, опишите тему чуть подробнее:")
        return

    data = await state.get_data()
    await state.clear()
    await enqueue_story_job(
        message=message,
        user_id=message.from_user.id,
        child=data.get("child_info", "Малыш"),
        hero=data.get("hero", "Котёнок"),
        theme_text=clean_theme,
        is_continuation=False,
        prev_summary=""
    )

# ==========================================
# 9. ГЕНЕРАЦИЯ ТЕКСТА СКАЗКИ (GEMINI API)
# ==========================================
def strip_service_tags_and_clean(raw_text, default_hero, default_theme):
    text = raw_text.replace("\u0301", "").replace("́", "").strip()
    summary = f"Добрая история про героя ({default_hero}) на тему: {default_theme}."
    tomorrow_hook = f"Новое волшебное приключение, которое {default_hero} покажет малышу завтра вечером."

    hook_match = re.search(r'TOMORROW[_ ]?HOOK\s*:\s*(.+)', text, flags=re.IGNORECASE)
    if hook_match:
        tomorrow_hook = hook_match.group(1).strip()
        text = re.sub(r'TOMORROW[_ ]?HOOK\s*:\s*.+', '', text, flags=re.IGNORECASE).strip()

    sum_match = re.search(r'SUMMARY\s*:\s*(.+)', text, flags=re.IGNORECASE)
    if sum_match:
        summary = sum_match.group(1).strip()
        text = re.sub(r'SUMMARY\s*:\s*.+', '', text, flags=re.IGNORECASE).strip()

    cleaned = re.sub(r'[*#_~`>|]', '', text)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()

    story_only = cleaned
    for marker in ["💬 Вопросы", "Вопросы для малыша", "ВОПРОСЫ ДЛЯ ОБСУЖДЕНИЯ", "ПЕРЕД СНОМ"]:
        if marker.lower() in cleaned.lower():
            idx = cleaned.lower().find(marker.lower())
            story_only = cleaned[:idx].strip()
            break

    return cleaned, story_only, summary[:250], tomorrow_hook[:200]

async def fetch_available_models(session, api_key):
    if api_key in cached_models_by_key:
        return cached_models_by_key[api_key]

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                valid_models = []
                for m in data.get("models", []):
                    name = m.get("name", "")
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods and "gemini" in name.lower():
                        if not any(x in name.lower() for x in ["vision", "embedding", "imagen", "aqa", "tts", "thinking", "exp"]):
                            valid_models.append(name)

                def model_priority(m_name):
                    m_low = m_name.lower()
                    if "2.0-flash" in m_low and "lite" not in m_low:
                        return 0
                    if "1.5-flash" in m_low and "8b" not in m_low:
                        return 1
                    if "2.5-flash" in m_low:
                        return 2
                    return 3

                valid_models.sort(key=model_priority)
                if valid_models:
                    cached_models_by_key[api_key] = valid_models
                    return valid_models
    except Exception as e:
        logging.warning(f"Ошибка получения списка моделей: {e}")

    return ["models/gemini-2.0-flash", "models/gemini-1.5-flash", "models/gemini-2.5-flash"]

async def generate_story_bundle(child, hero, theme, is_continuation, prev_summary):
    global current_key_index

    keys = get_all_gemini_keys()
    if not keys:
        raise RuntimeError("Не найден GEMINI_API_KEY в настройках Render!")

    clean_name = extract_clean_name(child)

    system_rules = (
        "Ты профессиональный русский детский писатель-сказочник. "
        "Сочиняй грамотные, тёплые, уютные сказки на ночь на безупречном литературном русском языке. "
        "СТРОГИЕ ПРАВИЛА ОРФОЭПИИ ДЛЯ ГОЛОСОВОГО ДИКТОРА: "
        "1. ВСЕГДА пиши букву «ё» во всех словах, где она должна быть (всё, тёплый, звёздочки, котёнок, ёжик, шёпотом, уснёт, поёт, ещё, лёгкий, далёкий). "
        "2. Строго различай слова «все» (множественное число: все игрушки, все зверята) и «всё» (средний род: всё вокруг, всё стихло, всё хорошо). "
        "3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО ставить знаки ударения над буквами и использовать слова-омографы: "
        "не используй слово «замок» (пиши «дворец» или «терем»), не используй «чудный» (пиши «чудесный»), "
        "не используй «кружки» (пиши «чашечки»), не используй «белки» (пиши «белочки»), не используй «стрелки» (пиши «стрелочки»), "
        "не используй «село» и «дорога». "
        "4. Естественно склоняй имя ребёнка по падежам русского языка и никогда не вставляй цифру возраста в сам текст сказки. "
        "5. Пиши плавными, мелодичными предложениями без скобок, кавычек и сложных причастных оборотов. "
        "Не используй дыхательную гимнастику — завершай сказку спокойной картиной уютного засыпания."
    )

    if is_continuation:
        context_block = (
            f"Напиши новую вечернюю серию (продолжение) сказки на ночь.\n"
            f"Имя ребёнка: {clean_name} (анкета: {child}). Любимый персонаж: {hero}. Тема серии: {theme}.\n"
            f"Краткое содержание прошлой серии: {prev_summary}.\n"
        )
    else:
        context_block = (
            f"Сочини уютную, добрую сказку на ночь.\n"
            f"Имя ребёнка: {clean_name} (анкета: {child}). Любимый друг: {hero}. Тема: {theme}.\n"
        )

    prompt = (
        f"{system_rules}\n\n{context_block}\n"
        f"Строгий формат ответа:\n"
        f"1. Сказка объёмом 300-360 слов, разделённая на 5 абзацев. Первые 4 абзаца раскрывают доброе неспешное приключение, "
        f"а в 5-м абзаце наступает тихий вечер: за окном светят звёздочки, {hero} и {clean_name} уютно укрываются тёплым одеяльцем и сладко засыпают.\n"
        f"2. С новой строки заголовок '💬 Вопросы для малыша перед сном:' и 2 коротких ласковых вопроса по сюжету.\n"
        f"3. С новой строки 'SUMMARY: ' и 1 предложение о том, что произошло в сказке.\n"
        f"4. С новой строки 'TOMORROW_HOOK: ' и 1 интригующее предложение-анонс завтрашней серии.\n"
        f"Пиши без символов звёздочек (*) и решёток (#)."
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 4096},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    session = await get_http_session()
    last_error = "Неизвестная ошибка"

    for k_offset in range(len(keys)):
        key_idx = (current_key_index + k_offset) % len(keys)
        api_key = keys[key_idx]
        models = await fetch_available_models(session, api_key)

        for model_path in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={api_key}"
            try:
                async def _do_request(_attempt):
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.json()
                        cands = data.get("candidates", [])
                        if not cands or "content" not in cands[0]:
                            raise RuntimeError("Пустой ответ модели")
                        parts = cands[0]["content"].get("parts", [])
                        return "".join(p.get("text", "") for p in parts)

                full_raw = await retry_with_backoff(_do_request, retries=2, base_delay=0.6, max_delay=2.5)
                display_text, story_for_tts, summary, tomorrow_hook = strip_service_tags_and_clean(full_raw, hero, theme)
                word_count = len(story_for_tts.split())

                if word_count >= 100:
                    current_key_index = key_idx
                    return display_text, story_for_tts, summary, tomorrow_hook
                else:
                    last_error = f"Короткий ответ ({word_count} слов)"
            except Exception as e:
                last_error = f"{model_path}: {str(e)[:60]}"
                logging.warning(f"Переключение модели после: {last_error}")

    raise RuntimeError(f"Серверы нейросети временно перегружены ({last_error}). Попробуйте через минуту.")

# ==========================================
# 10. ФОНОВАЯ ОЧЕРЕДЬ ЗАДАЧ И ВОРКЕРЫ
# ==========================================
async def enqueue_story_job(message, user_id, child, hero, theme_text, is_continuation, prev_summary):
    if user_id in active_generations:
        await message.answer("⏳ Ваша сказка уже создаётся, подождите несколько секунд...")
        return

    wait_sec = check_rate_limit(user_id)
    if wait_sec > 0:
        await message.answer(f"⏳ Пожалуйста, подождите {wait_sec} сек. перед следующим запросом.")
        return

    if not has_available_stories(user_id):
        await message.answer("🌙 Сказки на балансе закончились! Выберите удобный тариф:", reply_markup=get_tariffs_inline_kb())
        return

    active_generations.add(user_id)
    clean_name = extract_clean_name(child)
    q_waiting = story_task_queue.qsize() if story_task_queue else 0
    queue_note = f" (Номер в очереди: {q_waiting + 1})" if q_waiting >= NUM_QUEUE_WORKERS else ""

    status_msg = await message.answer(
        f"✍️ Сочиняю добрую сказку на ночь для: {clean_name} (герой: {hero})...{queue_note}",
        reply_markup=ReplyKeyboardRemove()
    )

    job = {
        "message": message,
        "status_msg": status_msg,
        "user_id": user_id,
        "child": child,
        "hero": hero,
        "theme_text": theme_text,
        "is_continuation": is_continuation,
        "prev_summary": prev_summary
    }
    await story_task_queue.put(job)

async def process_single_story_job(job):
    user_id = job["user_id"]
    msg = job["message"]
    status_msg = job["status_msg"]
    child = job["child"]
    hero = job["hero"]
    theme_text = job["theme_text"]
    is_continuation = job["is_continuation"]
    prev_summary = job["prev_summary"]

    clean_name = extract_clean_name(child)
    unique_tag = f"{user_id}_{int(time.time() * 1000)}"
    audio_path = f"/tmp/story_{unique_tag}.ogg" if os.path.exists("/tmp") else f"story_{unique_tag}.ogg"

    try:
        _, _, _, _, _, preferred_voice, _, _, _, _, bg_music = get_user_data(user_id)

        async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
            display_text, story_for_tts, new_summary, tomorrow_hook = await generate_story_bundle(
                child, hero, theme_text, is_continuation, prev_summary
            )

        try:
            await status_msg.edit_text(f"📖 Сказка для: {clean_name}\n\n{display_text[:4000]}")
        except Exception:
            await msg.answer(f"📖 Сказка для: {clean_name}\n\n{display_text[:4000]}")

        async with ChatActionSender.record_voice(bot=bot, chat_id=msg.chat.id):
            await synthesize_speech_to_file(story_for_tts, audio_path, preferred_voice, bg_music)

        remaining, total_gen = update_user_after_story(user_id, child, hero, new_summary, tomorrow_hook)
        has_sub, sub_days = get_subscription_status(user_id)

        if total_gen % 5 == 0:
            asyncio.create_task(send_db_backup_to_admin(f"Создана {total_gen}-я сказка"))

        hook_teaser = f"\n🔮 В следующей серии: «{tomorrow_hook}»\n" if tomorrow_hook else "\n"
        bal_line = f"👑 Безлимитная подписка активна ({sub_days} дн.)" if has_sub else f"🎁 Осталось сказок на балансе: {remaining} шт."
        voice_caption = (
            f"🎧 Аудиосказка для: {clean_name} (герой: {hero})"
            f"{hook_teaser}"
            f"{bal_line}"
        )

        await msg.answer_voice(
            voice=FSInputFile(audio_path),
            caption=voice_caption,
            reply_markup=get_post_story_inline_kb()
        )

        if not has_sub and remaining <= 1 and tomorrow_hook:
            await msg.answer(
                f"💡 На вашем балансе осталась {remaining} сказка.\n"
                f"Чтобы завтра малыш сразу узнал продолжение («{tomorrow_hook}»), выберите удобный пакет или подписку:",
                reply_markup=get_tariffs_inline_kb()
            )
        else:
            await msg.answer("Главное меню:", reply_markup=main_menu_keyboard)

    except Exception as e:
        await report_error_to_admin("Story Worker Error", e)
        await msg.answer(
            f"⚠️ {str(e)[:350]}\n\n(Попытка сохранена на вашем балансе)",
            reply_markup=main_menu_keyboard
        )
    finally:
        active_generations.discard(user_id)
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

async def story_worker_loop(worker_id):
    logging.info(f"Фоновый воркер очереди #{worker_id} запущен.")
    while True:
        job = await story_task_queue.get()
        try:
            await process_single_story_job(job)
        except Exception as e:
            logging.error(f"Сбой воркера #{worker_id}: {e}")
        finally:
            story_task_queue.task_done()

# ==========================================
# 11. ВЕБ-СЕРВЕР RENDER И СТАРТ
# ==========================================
async def handle_ping(request):
    return web.Response(text="OK - FairyTale Audio Bot is running")

async def handle_telegram_webhook(request):
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret_header != WEBHOOK_SECRET:
        return web.Response(status=403, text="Forbidden")
    try:
        data = await request.json()
        update = Update.model_validate(data, context={"bot": bot})
        asyncio.create_task(dp.feed_update(bot, update))
    except Exception as e:
        logging.error(f"Ошибка обработки Webhook: {e}")
    return web.Response(text="OK")

async def start_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    app.router.add_post(WEBHOOK_PATH, handle_telegram_webhook)

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"WEB SERVER BOUND TO PORT {port}")
    return runner

async def main():
    global story_task_queue
    logging.info("ЗАПУСК АУДИО-СКАЗОЧНИКА...")
    init_db()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(tts_thread_pool, synthesize_lullaby_loop_wav, LULLABY_WAV_PATH)

    story_task_queue = asyncio.Queue()
    server_runner = await start_server()

    worker_tasks = [
        asyncio.create_task(story_worker_loop(i + 1))
        for i in range(NUM_QUEUE_WORKERS)
    ]
    scheduler_task = asyncio.create_task(bedtime_reminder_scheduler())

    if ADMIN_ID != 0:
        u_cnt, prof_cnt, s_cnt, _, _, _ = get_global_stats()
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🚀 Аудио-бот запущен (без картинок, с предпрослушиванием дикторов и контролем ударений)!\n"
                f"👥 Семей: {u_cnt} | 👶 Детей: {prof_cnt} | 📚 Сказок: {s_cnt}"
            )
        except Exception:
            pass

    try:
        if WEBHOOK_BASE_URL:
            full_webhook_url = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"
            await bot.set_webhook(
                url=full_webhook_url,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types()
            )
            logging.info(f"РЕЖИМ WEBHOOK АКТИВИРОВАН: {full_webhook_url}")
            await asyncio.Event().wait()
        else:
            logging.info("Переключение на Long Polling...")
            await asyncio.sleep(5)
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in worker_tasks:
            t.cancel()
        scheduler_task.cancel()
        tts_thread_pool.shutdown(wait=False)
        if http_session and not http_session.closed:
            await http_session.close()
        await bot.session.close()
        await server_runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
