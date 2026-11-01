from __future__ import annotations

import os
import sys
import re
import json
import time
import random
import shutil
import hashlib
import sqlite3
import asyncio
import logging
import traceback
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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
    URLInputFile,
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
from gtts import gTTS

SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.5)
    except ImportError:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

# ==========================================
# 1. КОНФИГУРАЦИЯ, ПУЛЫ ПОТОКОВ И СЕТЕВОЙ СЛОЙ
# ==========================================
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
BOT_TOKEN = raw_bot_token.strip().strip("'\"")

if not BOT_TOKEN:
    logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

raw_admin_id = os.environ.get("ADMIN_ID", "0").strip().strip("'\"")
ADMIN_ID = int(raw_admin_id) if raw_admin_id.isdigit() else 0

WEBHOOK_BASE_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

UPSTASH_REDIS_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
UPSTASH_REDIS_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
TURSO_DB_URL = os.environ.get("TURSO_DATABASE_URL", "").strip().replace("libsql://", "https://").rstrip("/")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

tts_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts_cpu_pool")
tts_semaphore = asyncio.Semaphore(2)

story_task_queue: asyncio.Queue | None = None
NUM_QUEUE_WORKERS = 3
active_generations = set()
user_cooldowns = {}
COOLDOWN_SECONDS = 10.0

current_key_index = 0
cached_models_by_key = {}
http_session: aiohttp.ClientSession | None = None

AVAILABLE_VOICES = {
    "ru-RU-SvetlanaNeural": "👩 Нежная Светлана",
    "ru-RU-DariyaNeural": "👩‍🦰 Ласковая Дарья",
    "ru-RU-DmitryNeural": "👨 Добрый сказочник Дмитрий"
}

ART_STYLES = {
    "pixar": (
        "🎬 Мультфильм Pixar 3D",
        "Masterpiece 3D animation style like Pixar and Disney DreamWorks, cute adorable character, soft volumetric warm bedtime lantern lighting, magical starry night bedroom background, lush fluffy textures, 8k octane render, heartwarming"
    ),
    "watercolor": (
        "🎨 Акварельная книжка",
        "Masterpiece classic children story book illustration, soft pastel watercolor and colored pencils, Beatrix Potter cozy style, warm golden evening glow, gentle dreamy atmosphere, high detail"
    ),
    "ghibli": (
        "🏮 Уютный стиль Миядзаки",
        "Masterpiece Studio Ghibli Hayao Miyazaki anime art style, cozy warm bedroom at night, magical soft glowing stars outside window, gentle pastel colors, peaceful sleepy atmosphere, ultra detailed"
    )
}

TIMEZONE_OFFSETS = {
    "3": ("МСК (UTC+3)", 3),
    "5": ("Екатеринбург (UTC+5)", 5),
    "7": ("Новосибирск (UTC+7)", 7),
    "8": ("Иркутск (UTC+8)", 8),
    "10": ("Владивосток (UTC+10)", 10),
}

def get_child_rank(total_stories: int) -> str:
    if total_stories >= 15:
        return "\U0001f451 Магистр Сказочного Королевства"
    if total_stories >= 5:
        return "\u2b50 Хранитель Добрых Снов"
    return "\U0001f331 Юный Мечтатель"

def get_all_gemini_keys() -> list[str]:
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

async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        timeout_cfg = aiohttp.ClientTimeout(total=35.0, connect=8.0)
        connector = aiohttp.TCPConnector(limit=40, ttl_dns_cache=300)
        http_session = aiohttp.ClientSession(timeout=timeout_cfg, connector=connector)
    return http_session

def sanitize_input(text: str, max_len: int = 70) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'[<>{}\[\]\\*$#^~`|]', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned[:max_len]

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

# ==========================================
# 2. МОНИТОРИНГ ОШИБОК И RETRY С EXPONENTIAL BACKOFF + JITTER
# ==========================================
async def report_error_to_admin(context_title: str, exc: Exception):
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logging.error(f"[{context_title}] {exc}\n{tb_str}")
    if ADMIN_ID != 0:
        try:
            short_tb = tb_str[-1200:] if len(tb_str) > 1200 else tb_str
            await bot.send_message(
                ADMIN_ID,
                f"🚨 Мониторинг ошибок ({context_title}):\n"
                f"Тип: {type(exc).__name__}: {str(exc)[:200]}\n\n"
                f"Лог:\n{short_tb}"
            )
        except Exception:
            pass

@dp.error()
async def global_error_handler(event: ErrorEvent):
    await report_error_to_admin("Global Dispatcher Error", event.exception)

async def retry_with_backoff(coro_factory, retries: int = 3, base_delay: float = 0.7, max_delay: float = 6.0):
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_factory(attempt)
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                exp_cap = min(max_delay, base_delay * (2 ** attempt))
                sleep_time = random.uniform(0.2, exp_cap)
                logging.warning(f"Повтор #{attempt+1} через {sleep_time:.2f}с: {e}")
                await asyncio.sleep(sleep_time)
    raise last_exc

# ==========================================
# 3. REDIS АНТИ-ФЛУД И БАЗА ДАННЫХ (TURSO / SQLITE WAL)
# ==========================================
async def check_distributed_rate_limit(user_id: int) -> int:
    if UPSTASH_REDIS_URL and UPSTASH_REDIS_TOKEN:
        try:
            session = await get_http_session()
            key = f"ratelimit:user:{user_id}"
            headers = {"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"}
            async with session.post(
                f"{UPSTASH_REDIS_URL}/pipeline",
                headers=headers,
                json=[["SET", key, "1", "EX", str(int(COOLDOWN_SECONDS)), "NX"], ["TTL", key]],
                timeout=aiohttp.ClientTimeout(total=3.0)
            ) as resp:
                if resp.status == 200:
                    res = await resp.json()
                    set_result = res[0].get("result")
                    ttl_result = res[1].get("result", 5)
                    if set_result is None:
                        return max(1, int(ttl_result))
                    return 0
        except Exception as e:
            logging.warning(f"Redis RateLimit fallback: {e}")

    now = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return int(COOLDOWN_SECONDS - (now - last_time)) + 1
    user_cooldowns[user_id] = now
    return 0

async def sync_query_to_turso_cloud(sql: str, args: list):
    if not TURSO_DB_URL or not TURSO_AUTH_TOKEN:
        return
    try:
        session = await get_http_session()
        headers = {"Authorization": f"Bearer {TURSO_AUTH_TOKEN}", "Content-Type": "application/json"}
        formatted_args = []
        for a in args:
            if isinstance(a, int):
                formatted_args.append({"type": "integer", "value": str(a)})
            else:
                formatted_args.append({"type": "text", "value": str(a)})
        payload = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": formatted_args}}, {"type": "close"}]}
        async with session.post(f"{TURSO_DB_URL}/v2/pipeline", headers=headers, json=payload, timeout=5.0) as resp:
            if resp.status != 200:
                logging.warning(f"Turso Cloud sync status: {resp.status}")
    except Exception as e:
        logging.warning(f"Turso Cloud sync warning: {e}")

DB_PATH = os.environ.get("DATABASE_PATH") or ("/tmp/users.db" if os.path.exists("/tmp") else "users.db")
FREE_START_STORIES = 3
REFERRAL_BONUS_STORIES = 2

TARIFFS = {
    "pack_5": (5, 75, "Пакет: 5 добрых сказок"),
    "pack_15": (15, 150, "Пакет: 15 добрых сказок (Выгодно)"),
    "pack_30": (30, 250, "Пакет: 30 сказок на месяц (Хит)")
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

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
                tz_offset INTEGER DEFAULT 8,
                last_reminded_date TEXT DEFAULT '',
                art_style TEXT DEFAULT 'pixar',
                tomorrow_hook TEXT DEFAULT ''
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
            CREATE TABLE IF NOT EXISTS story_cache (
                cache_key TEXT PRIMARY KEY,
                display_template TEXT,
                tts_template TEXT,
                summary TEXT,
                img_prompt TEXT,
                tomorrow_hook TEXT DEFAULT '',
                created_at INTEGER
            )
        ''')
        existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(users);").fetchall()]
        migrations = {
            "last_child": "TEXT DEFAULT ''",
            "last_hero": "TEXT DEFAULT ''",
            "last_story_summary": "TEXT DEFAULT ''",
            "voice": "TEXT DEFAULT 'ru-RU-SvetlanaNeural'",
            "referred_by": "INTEGER DEFAULT 0",
            "referrals_count": "INTEGER DEFAULT 0",
            "remind_time": "TEXT DEFAULT ''",
            "tz_offset": "INTEGER DEFAULT 8",
            "last_reminded_date": "TEXT DEFAULT ''",
            "art_style": "TEXT DEFAULT 'pixar'",
            "tomorrow_hook": "TEXT DEFAULT ''"
        }
        for col_name, col_def in migrations.items():
            if col_name not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def};")
                except Exception as e:
                    logging.warning(f"Миграция {col_name}: {e}")

        cache_cols = [row[1] for row in conn.execute("PRAGMA table_info(story_cache);").fetchall()]
        if "tomorrow_hook" not in cache_cols:
            try:
                conn.execute("ALTER TABLE story_cache ADD COLUMN tomorrow_hook TEXT DEFAULT '';")
            except Exception:
                pass
        conn.commit()

def register_or_get_user(user_id: int, referrer_id: int = 0):
    ref_rewarded = False
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT stories_left, total_generated, last_child, last_hero, 
                      last_story_summary, voice, referrals_count, remind_time, tz_offset,
                      art_style, tomorrow_hook
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
                        "UPDATE users SET stories_left = stories_left + ?, referrals_count = referrals_count + 1 WHERE user_id = ?",
                        (REFERRAL_BONUS_STORIES, valid_ref)
                    )
                    ref_rewarded = True
                else:
                    valid_ref = 0

            c.execute(
                """INSERT INTO users 
                   (user_id, stories_left, total_generated, last_child, last_hero, last_story_summary, voice, referred_by, referrals_count, remind_time, tz_offset, art_style, tomorrow_hook) 
                   VALUES (?, ?, 0, '', '', '', 'ru-RU-SvetlanaNeural', ?, 0, '', 8, 'pixar', '')""",
                (user_id, FREE_START_STORIES, valid_ref)
            )
            conn.commit()
            return FREE_START_STORIES, 0, "", "", "", "ru-RU-SvetlanaNeural", 0, "", 8, "pixar", "", ref_rewarded

        return (
            row[0], row[1], row[2] or "", row[3] or "",
            row[4] or "", row[5] or "ru-RU-SvetlanaNeural", row[6] or 0,
            row[7] or "", row[8] if row[8] is not None else 8,
            row[9] or "pixar", row[10] or "", False
        )

def get_user_data(user_id: int):
    data = register_or_get_user(user_id)
    return data[:11]

def set_user_voice(user_id: int, voice_id: str):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET voice = ? WHERE user_id = ?", (voice_id, user_id))
        conn.commit()

def set_user_art_style(user_id: int, style_id: str):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET art_style = ? WHERE user_id = ?", (style_id, user_id))
        conn.commit()

def set_user_reminder(user_id: int, remind_time: str, tz_offset: int | None = None):
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        if tz_offset is not None:
            conn.execute("UPDATE users SET remind_time = ?, tz_offset = ? WHERE user_id = ?", (remind_time, tz_offset, user_id))
        else:
            conn.execute("UPDATE users SET remind_time = ? WHERE user_id = ?", (remind_time, user_id))
        conn.commit()

def update_user_after_story(user_id: int, child: str, hero: str, summary: str, tomorrow_hook: str) -> tuple[int, int]:
    with get_db_connection() as conn:
        c = conn.cursor()
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
        conn.commit()
        c.execute("SELECT stories_left, total_generated FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        rem = row[0] if row else 0
        tot = row[1] if row else 1
    asyncio.create_task(sync_query_to_turso_cloud(
        "UPDATE users SET stories_left = ?, last_child = ?, last_hero = ?, last_story_summary = ? WHERE user_id = ?",
        [rem, child, hero, summary, user_id]
    ))
    return rem, tot

def add_stories_to_user(user_id: int, count: int) -> int:
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?", (count, user_id))
        conn.commit()
        c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return row[0] if row else count

def record_payment_idempotent(charge_id: str, user_id: int, stories_added: int, stars_amount: int) -> tuple[bool, int]:
    _ = get_user_data(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT charge_id FROM payments WHERE charge_id = ?", (charge_id,))
        if c.fetchone() is not None:
            c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
            r = c.fetchone()
            return False, (r[0] if r else 0)

        c.execute(
            "INSERT INTO payments (charge_id, user_id, stories_added, stars_amount) VALUES (?, ?, ?, ?)",
            (charge_id, user_id, stories_added, stars_amount)
        )
        c.execute("UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?", (stories_added, user_id))
        conn.commit()
        c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return True, (row[0] if row else stories_added)

# ==========================================
# 4. КЭШИРОВАНИЕ ПОПУЛЯРНЫХ ТЕМ
# ==========================================
def make_cache_key(hero: str, theme: str) -> str:
    norm = f"{hero.strip().lower()}::{theme.strip().lower()}"
    return hashlib.md5(norm.encode("utf-8")).hexdigest()

def get_cached_story(hero: str, theme: str, child: str) -> tuple[str, str, str, str, str] | None:
    key = make_cache_key(hero, theme)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT display_template, tts_template, summary, img_prompt, tomorrow_hook FROM story_cache WHERE cache_key = ?",
            (key,)
        )
        row = c.fetchone()
        if row and random.random() < 0.30:
            disp = row[0].replace("{{CHILD}}", child)
            tts_t = row[1].replace("{{CHILD}}", child)
            hook = (row[4] or "").replace("{{CHILD}}", child)
            return disp, tts_t, row[2], row[3], hook
    return None

def save_story_to_cache(hero: str, theme: str, child: str, display_text: str, tts_text: str, summary: str, img_prompt: str, tomorrow_hook: str):
    if not child or len(child) < 2:
        return
    key = make_cache_key(hero, theme)
    disp_tmpl = display_text.replace(child, "{{CHILD}}")
    tts_tmpl = tts_text.replace(child, "{{CHILD}}")
    hook_tmpl = tomorrow_hook.replace(child, "{{CHILD}}")
    with get_db_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO story_cache 
               (cache_key, display_template, tts_template, summary, img_prompt, tomorrow_hook, created_at) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (key, disp_tmpl, tts_tmpl, summary, img_prompt, hook_tmpl, int(time.time()))
        )
        conn.commit()

def get_global_stats() -> tuple[int, int, int, int, int]:
    with get_db_connection() as conn:
        c = conn.cursor()
        users_cnt = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        stories_cnt = c.execute("SELECT COALESCE(SUM(total_generated), 0) FROM users").fetchone()[0]
        pay_cnt = c.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        stars_sum = c.execute("SELECT COALESCE(SUM(stars_amount), 0) FROM payments").fetchone()[0]
        cache_cnt = c.execute("SELECT COUNT(*) FROM story_cache").fetchone()[0]
        return users_cnt, stories_cnt, pay_cnt, stars_sum, cache_cnt

async def send_db_backup_to_admin(reason: str = "Плановый бэкап"):
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
# 5. ТОЧНЫЙ ОРФОЭПИЧЕСКИЙ ПРОЦЕССОР ДЛЯ ДИКТОРА (БЕЗ ОШИБКИ «ВСЕ -> ВСЁ»)
# ==========================================
# Только однозначные слова с «ё» (без опасного глобального 'все' -> 'всё'!)
YO_WORDS_DICT = {
    "котенок": "котёнок", "Котенок": "Котёнок", "котенка": "котёнка", "котенку": "котёнку", "котенком": "котёнком",
    "лисенок": "лисёнок", "лисенка": "лисёнка", "слоненок": "слонёнок", "тигренок": "тигрёнок", "медвежонок": "медвежонок",
    "ежик": "ёжик", "Ежик": "Ёжик", "ежика": "ёжика", "ежику": "ёжику", "ежиком": "ёжиком",
    "елка": "ёлка", "елочка": "ёлочка", "елочки": "ёлочки", "елке": "ёлке",
    "звездочка": "звёздочка", "Звездочка": "Звёздочка", "звездочки": "звёздочки", "звездочек": "звёздочек",
    "звездный": "звёздный", "звездное": "звёздное", "звездным": "звёздным", "звездном": "звёздном", "звезд": "звёзд",
    "теплый": "тёплый", "Теплый": "Тёплый", "теплая": "тёплая", "теплое": "тёплое", "теплые": "тёплые",
    "теплым": "тёплым", "теплой": "тёплой", "теплом": "тёплом", "теплую": "тёплую",
    "темный": "тёмный", "темная": "тёмная", "темное": "тёмное", "темной": "тёмной", "темную": "тёмную",
    "тяжелый": "тяжёлый", "тяжелые": "тяжёлые", "тяжелыми": "тяжёлыми", "тяжелая": "тяжёлая",
    "шепотом": "шёпотом", "шепот": "шёпот", "щечки": "щёчки", "щечку": "щёчку", "щечкам": "щёчкам",
    "слезки": "слёзки", "слез": "слёз", "мед": "мёд", "медом": "мёдом",
    "поет": "поёт", "споет": "споёт", "уснет": "уснёт", "заснет": "заснёт", "проснется": "проснётся",
    "придет": "придёт", "уйдет": "уйдёт", "найдет": "найдёт", "плывет": "плывёт",
    "живет": "живёт", "зовет": "зовёт", "несет": "несёт", "ведет": "ведёт",
    "растет": "растёт", "цветет": "цветёт", "узнает": "узнаёт", "встает": "встаёт",
    "вперед": "вперёд", "полет": "полёт", "самолет": "самолёт", "вертолет": "вертолёт",
    "колеса": "колёса", "колесики": "колёсики", "пестрый": "пёстрый",
    "желтый": "жёлтый", "желтая": "жёлтая", "желтое": "жёлтое", "желтые": "жёлтые",
    "зеленый": "зелёный", "зеленая": "зелёная", "зеленое": "зелёное", "зеленые": "зелёные",
    "березка": "берёзка", "клен": "клён", "черемуха": "черёмуха", "пеленки": "пелёнки",
    "ребенок": "ребёнок", "ребенка": "ребёнка", "ребенку": "ребёнку", "ребенком": "ребёнком",
    "свернулся": "свернулся", "одеялко": "одеялко", "одеяльце": "одеяльце", "еще": "ещё", "Еще": "Ещё"
}

# Контекстные фразы со словом «всё/всём», чтобы никогда не ломать «все зверята / все засыпают»
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
    ("это все", "это всё"),
    ("вот и все", "вот и всё"),
    ("поет свою", "поёт свою"),
    ("тихонько поет", "тихонько поёт"),
]

def prepare_russian_text_for_tts(text: str) -> str:
    """Готовит безупречный русский текст для нейро-диктора: контекстная буква 'ё' и плавные паузы."""
    # 1. Убираем любые знаки ударения Юникода \u0301, чтобы не рвать слова
    result = text.replace("\u0301", "").replace("́", "")

    # 2. Заменяем контекстные словосочетания
    for old_p, new_p in YO_CONTEXT_PHRASES:
        result = re.sub(re.escape(old_p), new_p, result, flags=re.IGNORECASE)

    # 3. Точечно подставляем «ё» в однозначных словах
    for wrong, correct in YO_WORDS_DICT.items():
        result = re.sub(rf'\b{re.escape(wrong)}\b', correct, result)

    # 4. Заменяем тире на запятую для плавной паузы без обрыва интонации
    result = re.sub(r'\s*[—–-]\s*', ', ', result)
    result = re.sub(r'[«»"“”]', '', result)
    # Сохраняем чистые точки между абзацами, чтобы диктор мягко понижал голос в конце мыслей
    result = re.sub(r'\n+', ' ', result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result

# ==========================================
# 6. ИНТЕРФЕЙС И КЛАВИАТУРЫ (С ГАРАНТИРОВАННЫМ ОТОБРАЖЕНИЕМ ЗВЁЗД \u2b50)
# ==========================================
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()
    waiting_for_custom_theme = State()

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪄 Создать новую сказку"), KeyboardButton(text="🔄 Продолжить сказку")],
        [KeyboardButton(text="🧸 Сказкотерапия (страхи/сон)"), KeyboardButton(text="🎨 Стиль и Голос")],
        [KeyboardButton(text="⏰ Время спать"), KeyboardButton(text="👤 Профиль и Грамота")],
        [KeyboardButton(text="💎 Купить сказки (\u2b50 Звёзды)")]
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

def get_tariffs_inline_kb() -> InlineKeyboardMarkup:
    # Используем чистый символ \u2b50 (без ломающегося селектора \ufe0f) + явное слово «Звёзд»!
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="\u2b50 5 сказок — 75 Звёзд", callback_data="buy_pack_5")],
            [InlineKeyboardButton(text="\u2b50 15 сказок — 150 Звёзд (Выгодно)", callback_data="buy_pack_15")],
            [InlineKeyboardButton(text="\u2b50 30 сказок — 250 Звёзд (Хит на месяц)", callback_data="buy_pack_30")],
            [InlineKeyboardButton(text="🎁 Бесплатные сказки за друзей (+2 шт.)", callback_data="ref_program")]
        ]
    )

def get_post_story_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продолжить эту историю", callback_data="action_continue")],
            [InlineKeyboardButton(text="🎲 Новая тема с этим же героем", callback_data="action_same_hero")],
            [InlineKeyboardButton(text="🏅 Сонная грамота малыша", callback_data="award_diploma")],
            [InlineKeyboardButton(text="🎁 +2 сказки за друга", callback_data="ref_program")]
        ]
    )

def get_style_and_voice_kb(current_voice: str, current_style: str) -> InlineKeyboardMarkup:
    rows = []
    for v_id, v_label in AVAILABLE_VOICES.items():
        mark = "✅ " if v_id == current_voice else ""
        rows.append([InlineKeyboardButton(text=f"🎙 {mark}{v_label}", callback_data=f"setvoice_{v_id}")])
    for s_id, (s_label, _) in ART_STYLES.items():
        mark = "✅ " if s_id == current_style else ""
        rows.append([InlineKeyboardButton(text=f"🖼 Стиль: {mark}{s_label}", callback_data=f"setstyle_{s_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_reminder_inline_kb(current_time: str, current_tz: int) -> InlineKeyboardMarkup:
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
# 7. ЭКСКЛЮЗИВНЫЕ ФУНКЦИИ, НАПОМИНАНИЯ, ПРОФИЛЬ И ОПЛАТА
# ==========================================
@dp.message(F.text == "🏠 В главное меню")
@dp.message(Command("cancel"))
async def cancel_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Вы вернулись в главное меню:", reply_markup=main_menu_keyboard)

@dp.message(F.text == "⏰ Время спать")
@dp.message(Command("remind"))
async def bedtime_reminder_menu(message: Message):
    _, _, _, _, _, _, _, remind_time, tz_offset, _, _ = get_user_data(message.from_user.id)
    status_str = f"Включено на {remind_time} (UTC+{tz_offset})" if remind_time else "Выключено"
    await message.answer(
        f"⏰ Родительский контроль: Время спать\n\n"
        f"Бот каждый вечер мягко напомнит вам, что пора готовиться ко сну и включать новую сказку малышу.\n\n"
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
        f"✅ Вечернее напоминание «Время спать» установлено на {chosen_time} (UTC+{tz_offset})!",
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
        await callback.answer(f"Часовой пояс UTC+{offset} сохранен!")
        await callback.message.edit_text(
            f"✅ Часовой пояс изменен на UTC+{offset}. Время напоминания: {active_t}",
            reply_markup=get_reminder_inline_kb(active_t, offset)
        )

@dp.callback_query(F.data == "remind_off")
async def set_remind_off_cb(callback: CallbackQuery):
    _, _, _, _, _, _, _, _, tz_offset, _, _ = get_user_data(callback.from_user.id)
    set_user_reminder(callback.from_user.id, "")
    await callback.answer("Напоминания выключены")
    await callback.message.edit_text(
        "🔕 Вечерние напоминания выключены. Вы всегда можете включить их снова:",
        reply_markup=get_reminder_inline_kb("", tz_offset)
    )

async def bedtime_reminder_scheduler():
    while True:
        try:
            utc_now = datetime.now(timezone.utc)
            with get_db_connection() as conn:
                c = conn.cursor()
                rows = c.execute(
                    "SELECT user_id, remind_time, tz_offset, last_reminded_date, last_child, last_hero, tomorrow_hook FROM users WHERE remind_time != ''"
                ).fetchall()

                for uid, r_time, tz_off, last_date, l_child, l_hero, t_hook in rows:
                    user_now = utc_now + timedelta(hours=int(tz_off or 8))
                    today_str = user_now.strftime("%Y-%m-%d")
                    current_hm = user_now.strftime("%H:%M")

                    if current_hm == r_time and last_date != today_str:
                        conn.execute("UPDATE users SET last_reminded_date = ? WHERE user_id = ?", (today_str, uid))
                        conn.commit()
                        child_label = f" для {l_child}" if l_child else ""
                        hook_line = f"\n\n🔮 Анонс на сегодня: {t_hook}" if t_hook else ""
                        try:
                            await bot.send_message(
                                uid,
                                f"🌙 Время готовиться ко сну!\n\n"
                                f"Пора проветрить спальню, приглушить свет и включить новую добрую сказку{child_label} ✨{hook_line}",
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
    admin_status = "✅ Вы Администратор!" if is_admin(uid) else "ℹ️ Добавьте переменную ADMIN_ID с этим номером в Render -> Environment."
    await message.answer(f"🔑 Ваш Telegram ID: {uid}\n\n{admin_status}", reply_markup=main_menu_keyboard)

@dp.message(Command("stats"))
async def admin_stats_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    u_cnt, s_cnt, p_cnt, stars_sum, cache_cnt = get_global_stats()
    q_size = story_task_queue.qsize() if story_task_queue else 0
    mode_str = f"Webhook ({WEBHOOK_BASE_URL})" if WEBHOOK_BASE_URL else "Long Polling"
    await message.answer(
        f"📊 Инженерная статистика бота:\n\n"
        f"⚙️ Режим связи: {mode_str}\n"
        f"📥 Задач в очереди: {q_size}\n"
        f"🗄 Сюжетов в кэше: {cache_cnt}\n"
        f"👥 Пользователей: {u_cnt}\n"
        f"📚 Создано сказок: {s_cnt}\n"
        f"💳 Оплат: {p_cnt} ({stars_sum} \u2b50 Звёзд)"
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
        "1. Бот генерирует персональные детские сказки, иллюстрации и озвучку с помощью ИИ.\n"
        "2. Пополнение баланса осуществляется через Telegram Stars (XTR).\n"
        "3. Попытка списывается только после успешной доставки аудиосказки.\n"
        "4. Поддержка по платежам: /paysupport."
    )

@dp.message(Command("paysupport"))
async def paysupport_cmd(message: Message):
    await message.answer(
        "🛎 Поддержка по платежам:\n"
        "Напишите ваш вопрос в этот чат с пометкой #оплата — сообщение будет сразу передано администратору."
    )

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

@dp.message(F.text.in_({"👤 Мой баланс", "👤 Профиль и Бонусы", "👤 Профиль и Грамота"}))
async def show_balance(message: Message):
    user_id = message.from_user.id
    balance, total, last_child, last_hero, _, voice, refs_cnt, remind_time, tz_off, art_style, _ = get_user_data(user_id)
    voice_name = AVAILABLE_VOICES.get(voice, "Нежная Светлана")
    style_name = ART_STYLES.get(art_style, ART_STYLES["pixar"])[0]
    rank_title = get_child_rank(total)
    history_info = f"\n🧸 Герой: {last_hero} (для: {last_child})" if last_hero else ""
    remind_info = f"\n⏰ Напоминание ко сну: {remind_time} (UTC+{tz_off})" if remind_time else "\n⏰ Напоминание ко сну: выкл."
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    profile_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏅 Получить Сонную Грамоту малыша", callback_data="award_diploma")],
            [InlineKeyboardButton(text="🎁 Пригласить друга (+2 сказки)", callback_data="ref_program")]
        ]
    )

    await message.answer(
        f"👤 Семейный профиль и Книга Памяти:\n\n"
        f"🏅 Титул малыша: {rank_title}\n"
        f"🎧 Доступно сказок: {balance} шт.\n"
        f"📚 Прослушано сказок: {total} шт.\n"
        f"🎙 Голос диктора: {voice_name}\n"
        f"🖼 Стиль иллюстраций: {style_name}{history_info}{remind_info}\n"
        f"👥 Приглашено друзей: {refs_cnt}\n\n"
        f"🎁 Ваша ссылка (+{REFERRAL_BONUS_STORIES} сказки за друга):\n{ref_link}",
        reply_markup=profile_kb
    )

@dp.callback_query(F.data == "award_diploma")
async def award_diploma_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    _, total, last_child, last_hero, _, _, _, _, _, _, _ = get_user_data(user_id)
    child_name = last_child if last_child else "Влада"
    hero_name = last_hero if last_hero else "Сказочный Друг"
    rank_title = get_child_rank(total)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"

    await callback.answer("Создаю именную Сонную Грамоту!")
    diploma_text = (
        f"📜 ✨ ИМЕННАЯ СОННАЯ ГРАМОТА ✨ 📜\n"
        f"══════════════════════\n"
        f"Награждается: {child_name}\n"
        f"Почётный титул: {rank_title}\n"
        f"Любимый хранитель снов: {hero_name}\n"
        f"Прослушано добрых сказок: {max(1, total)}\n"
        f"══════════════════════\n"
        f"За крепкий сон, добрые сновидения и самую тёплую улыбку перед сном!\n\n"
        f"🪄 Создайте персональную сказку для своего малыша: {ref_link}"
    )
    await callback.message.answer(
        diploma_text,
        reply_markup=main_menu_keyboard
    )

@dp.callback_query(F.data == "ref_program")
async def ref_program_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    await callback.answer()
    await callback.message.answer(
        f"🎁 Дарим сказки за друзей!\n\n"
        f"Перешлите эту ссылку знакомым родителям:\n{ref_link}\n\n"
        f"За каждого друга вам автоматически начислится +{REFERRAL_BONUS_STORIES} бесплатные сказки!",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text.in_({"🎙 Голос диктора", "🎨 Стиль и Голос"}))
async def choose_voice_and_style_cmd(message: Message):
    _, _, _, _, _, current_voice, _, _, _, current_style, _ = get_user_data(message.from_user.id)
    await message.answer(
        "🎨 Режиссёр атмосферы сказки\n\n"
        "Выберите любимый тембр диктора и стиль художника для иллюстраций:",
        reply_markup=get_style_and_voice_kb(current_voice, current_style)
    )

@dp.callback_query(F.data.startswith("setvoice_"))
async def set_voice_callback(callback: CallbackQuery):
    voice_id = callback.data.replace("setvoice_", "")
    if voice_id in AVAILABLE_VOICES:
        set_user_voice(callback.from_user.id, voice_id)
        _, _, _, _, _, _, _, _, _, current_style, _ = get_user_data(callback.from_user.id)
        await callback.answer(f"Выбран голос: {AVAILABLE_VOICES[voice_id]}")
        await callback.message.edit_text(
            f"✅ Голос диктора: {AVAILABLE_VOICES[voice_id]}\n"
            f"🖼 Стиль иллюстраций: {ART_STYLES.get(current_style, ART_STYLES['pixar'])[0]}",
            reply_markup=get_style_and_voice_kb(voice_id, current_style)
        )

@dp.callback_query(F.data.startswith("setstyle_"))
async def set_style_callback(callback: CallbackQuery):
    style_id = callback.data.replace("setstyle_", "")
    if style_id in ART_STYLES:
        set_user_art_style(callback.from_user.id, style_id)
        _, _, _, _, _, current_voice, _, _, _, _, _ = get_user_data(callback.from_user.id)
        await callback.answer(f"Выбран стиль: {ART_STYLES[style_id][0]}")
        await callback.message.edit_text(
            f"🎙 Голос диктора: {AVAILABLE_VOICES.get(current_voice, 'Нежная Светлана')}\n"
            f"✅ Стиль иллюстраций: {ART_STYLES[style_id][0]}",
            reply_markup=get_style_and_voice_kb(current_voice, style_id)
        )

@dp.message(F.text.startswith("💎 Купить сказки"))
async def show_tariffs(message: Message):
    balance, _, _, _, _, _, _, _, _, _, tomorrow_hook = get_user_data(message.from_user.id)
    hook_block = f"\n🔮 Что ждёт в следующей серии:\n«{tomorrow_hook}»\n" if tomorrow_hook else ""
    await message.answer(
        f"💎 Пополнение баланса сказок\n"
        f"Сейчас доступно: {balance} шт.{hook_block}\n"
        f"Каждая сказка включает потоковый текст, иллюстрацию в стиле Pixar/Акварель, сонный дыхательный ритуал и живую озвучку.\n\n"
        f"Выберите пакет ниже (оплата официальными \u2b50 Звёздами Telegram):",
        reply_markup=get_tariffs_inline_kb()
    )

@dp.message(Command("add_free"))
async def add_free_cmd(message: Message):
    user_id = message.from_user.id
    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await message.answer("🎁 Пригласите друга в разделе «👤 Профиль и Грамота», чтобы получить бесплатные сказки!")
        return
    balance, _, _, _, _, _, _, _, _, _, _ = get_user_data(user_id)
    if ADMIN_ID == 0 and balance >= 5:
        await message.answer("У вас уже достаточно тестовых сказок на балансе.")
        return

    new_bal = add_stories_to_user(user_id, 5)
    await message.answer(f"🎁 Начислено +5 сказок! Ваш баланс: {new_bal} шт.", reply_markup=main_menu_keyboard)

@dp.callback_query(F.data.startswith("buy_"))
async def send_stars_invoice(callback: CallbackQuery):
    pack_key = callback.data.replace("buy_", "")
    if pack_key not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    stories_count, stars_price, title = TARIFFS[pack_key]
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"\u2b50 {title}",
        description=f"Пакет на {stories_count} персональных аудиосказок с 3D-иллюстрациями, сонным дыхательным ритуалом и сказкотерапией.",
        payload=f"stars_{stories_count}_{stars_price}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{stories_count} сказок", amount=stars_price)]
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

    try:
        stories_added = int(payload.split("_")[1])
    except Exception:
        stories_added = 5

    is_new, new_balance = record_payment_idempotent(charge_id, message.from_user.id, stories_added, stars_paid)
    if is_new:
        await send_db_backup_to_admin(f"Оплата +{stories_added} сказок ({stars_paid} XTR) от ID {message.from_user.id}")

    await message.answer(
        f"🎉 Оплата прошла успешно!\n"
        f"Начислено: +{stories_added} сказок. Всего доступно: {new_balance} шт.",
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

    if command and command.args and command.args.startswith("ref_"):
        ref_str = command.args.replace("ref_", "").strip()
        if ref_str.isdigit():
            referrer_id = int(ref_str)

    balance, _, _, _, _, _, _, _, _, _, _, ref_rewarded = register_or_get_user(user_id, referrer_id)

    if ref_rewarded and referrer_id > 0:
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 По вашей ссылке присоединился друг! Вам начислено +{REFERRAL_BONUS_STORIES} бесплатные сказки!"
            )
        except Exception:
            pass

    await message.answer(
        f"👋 Добро пожаловать в мир добрых персональных аудиосказок!\n\n"
        f"✨ Мы в реальном времени сочиняем убаюкивающие истории про вашего малыша, рисуем иллюстрацию в стиле Pixar/Акварель и проводим мягкий сонный ритуал живым голосом.\n"
        f"🎁 Доступно сказок на балансе: {balance} шт.",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text == "🪄 Создать новую сказку")
async def create_new_story_btn(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    balance, _, last_child, last_hero, _, _, _, _, _, _, tomorrow_hook = get_user_data(user_id)

    if balance <= 0:
        hook_msg = f"\n\n🔮 В следующей серии: «{tomorrow_hook}»" if tomorrow_hook else ""
        await message.answer(
            f"🌙 Сказки на балансе закончились!{hook_msg}\nПополните баланс \u2b50 Звёздами или пригласите друга:",
            reply_markup=get_tariffs_inline_kb()
        )
        return

    if last_child and last_hero:
        quick_kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=f"⚡ Для: {last_child} и героя {last_hero}")],
                [KeyboardButton(text="🏠 В главное меню")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await message.answer(
            "👶 Шаг 1 из 3: Напишите имя и возраст ребенка (или выберите сохраненных героев кнопкой ниже):",
            reply_markup=quick_kb
        )
    else:
        await message.answer(
            "👶 Шаг 1 из 3: Напишите имя и возраст ребенка:\n(Например: Влада, 3 года)",
            reply_markup=ReplyKeyboardRemove()
        )
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(F.text == "🧸 Сказкотерапия (страхи/сон)")
async def start_therapy_mode(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    balance, _, last_child, last_hero, _, _, _, _, _, _, _ = get_user_data(user_id)

    if balance <= 0:
        await message.answer("🌙 Сказки закончились! Пополните баланс:", reply_markup=get_tariffs_inline_kb())
        return

    if last_child and last_hero:
        await state.update_data(child_info=last_child, hero=last_hero, is_continuation=False)
        await message.answer(
            f"🧸 Сказкотерапия для: {last_child} (герой: {last_hero}).\nВыберите тему для мягкой проработки:",
            reply_markup=therapy_keyboard
        )
        await state.set_state(StoryCreation.waiting_for_theme)
    else:
        await state.update_data(therapy_mode=True)
        await message.answer(
            "🧸 Режим сказкотерапии.\nНапишите имя и возраст ребенка (например: Влада, 3 года):",
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
    balance, _, last_child, last_hero, _, _, _, _, _, _, _ = get_user_data(user_id)
    if balance <= 0:
        await callback.message.answer("🌙 Сказки закончились! Пополните баланс:", reply_markup=get_tariffs_inline_kb())
        return
    if not last_child or not last_hero:
        await callback.message.answer("Нажмите «🪄 Создать новую сказку», чтобы задать имя и героя.")
        return

    await state.update_data(child_info=last_child, hero=last_hero, is_continuation=False)
    await callback.message.answer(
        f"🎯 Выберите новую тему для приключений ({last_child} и {last_hero}):",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

async def enqueue_continuation(message: Message, user_id: int):
    balance, _, last_child, last_hero, last_summary, _, _, _, _, _, tomorrow_hook = get_user_data(user_id)
    if balance <= 0:
        hook_msg = f"\n\n🔮 В этой серии: «{tomorrow_hook}»" if tomorrow_hook else ""
        await message.answer(f"🌙 Сказки закончились!{hook_msg}\nПополните баланс:", reply_markup=get_tariffs_inline_kb())
        return
    if not last_summary or not last_hero:
        await message.answer("У вас пока нет сохраненной сказки. Давайте создадим первую!", reply_markup=main_menu_keyboard)
        return

    next_theme = tomorrow_hook if tomorrow_hook else "Новое доброе вечернее приключение и сладкий сон"
    await enqueue_story_job(
        message=message
