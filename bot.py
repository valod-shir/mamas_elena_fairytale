import os
import sys
import re
import sqlite3
import asyncio
import logging
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, 
    CallbackQuery,
    FSInputFile, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
)
from aiogram.utils.chat_action import ChatActionSender
import edge_tts
from gtts import gTTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

# ==========================================
# 1. НАСТРОЙКИ, КЛЮЧИ И ЗАЩИТА ОТ ДУБЛЕЙ
# ==========================================
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
BOT_TOKEN = raw_bot_token.strip().strip("'\"")

if not BOT_TOKEN:
    logging.critical("КРИТИЧЕСКАЯ ОШИБКА: Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

active_generations = set()
current_key_index = 0
cached_models_by_key = {}

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

# ==========================================
# 2. НАДЕЖНАЯ БАЗА ДАННЫХ SQLite (WAL-режим)
# ==========================================
DB_PATH = "/tmp/users.db" if os.path.exists("/tmp") else "users.db"
FREE_START_STORIES = 3

TARIFFS = {
    "pack_5": (5, 75, "Пакет: 5 добрых сказок"),
    "pack_15": (15, 150, "Пакет: 15 добрых сказок"),
    "pack_30": (30, 250, "Пакет: 30 сказок на месяц")
}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                stories_left INTEGER DEFAULT 3,
                total_generated INTEGER DEFAULT 0
            )
        ''')
        conn.commit()

def get_user_balance(user_id: int) -> int:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row is None:
            c.execute(
                "INSERT INTO users (user_id, stories_left, total_generated) VALUES (?, ?, 0)",
                (user_id, FREE_START_STORIES)
            )
            conn.commit()
            return FREE_START_STORIES
        return row[0]

def add_stories_to_user(user_id: int, count: int) -> int:
    _ = get_user_balance(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?", (count, user_id))
        conn.commit()
        c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return row[0] if row else count

def use_one_story(user_id: int) -> int:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET stories_left = MAX(0, stories_left - 1), total_generated = total_generated + 1 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()
        c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        return row[0] if row else 0

def get_user_stats(user_id: int):
    balance = get_user_balance(user_id)
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT total_generated FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        total = row[0] if row else 0
    return balance, total

# ==========================================
# 3. ИНТЕРФЕЙС И КЛАВИАТУРЫ
# ==========================================
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()

main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪄 Создать новую сказку")],
        [KeyboardButton(text="👤 Мой баланс"), KeyboardButton(text="💎 Купить сказки")]
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

def get_tariffs_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌟 5 сказок — 75 ⭐️", callback_data="buy_pack_5")],
            [InlineKeyboardButton(text="🌟 15 сказок — 150 ⭐️ (Выгодно)", callback_data="buy_pack_15")],
            [InlineKeyboardButton(text="🌟 30 сказок — 250 ⭐️ (На месяц)", callback_data="buy_pack_30")]
        ]
    )

# ==========================================
# 4. ОБРАБОТЧИКИ ПРОФИЛЯ И ОПЛАТЫ
# ==========================================
@dp.message(F.text == "🏠 В главное меню")
@dp.message(Command("cancel"))
async def cancel_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Вы вернулись в главное меню:", reply_markup=main_menu_keyboard)

@dp.message(F.text == "👤 Мой баланс")
async def show_balance(message: Message):
    balance, total = get_user_stats(message.from_user.id)
    await message.answer(
        f"👤 Ваш профиль:\n\n"
        f"🎧 Доступно сказок: {balance} шт.\n"
        f"📚 Всего создано сказок: {total} шт.\n\n"
        f"Выберите действие в меню ниже:",
        reply_markup=main_menu_keyboard
    )

@dp.message(F.text == "💎 Купить сказки")
async def show_tariffs(message: Message):
    balance = get_user_balance(message.from_user.id)
    await message.answer(
        f"💎 Пополнение баланса сказок\n"
        f"Сейчас у вас доступно: {balance} шт.\n\n"
        f"Выберите пакет для мгновенной оплаты Звёздами (⭐️):",
        reply_markup=get_tariffs_inline_kb()
    )

@dp.message(Command("add_free"))
async def add_free_cmd(message: Message):
    new_bal = add_stories_to_user(message.from_user.id, 5)
    await message.answer(f"🎁 Бонус начислен! Ваш баланс: {new_bal} сказок.", reply_markup=main_menu_keyboard)

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
        title=title,
        description=f"Пополнение баланса на {stories_count} персональных аудиосказок для малыша.",
        payload=f"stars_{stories_count}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars_price)]
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        stories_added = int(payload.replace("stars_", ""))
    except Exception:
        stories_added = 5

    new_balance = add_stories_to_user(message.from_user.id, stories_added)
    await message.answer(
        f"🎉 Оплата прошла успешно!\n\n"
        f"На ваш баланс зачислено: +{stories_added} сказок.\n"
        f"🎧 Всего доступно: {new_balance} шт.\n\n"
        f"Нажмите «🪄 Создать новую сказку», чтобы начать!",
        reply_markup=main_menu_keyboard
    )

# ==========================================
# 5. ШАГИ СОЗДАНИЯ СКАЗКИ
# ==========================================
@dp.message(CommandStart())
@dp.message(F.text.startswith("/start"))
@dp.message(F.text == "🪄 Создать новую сказку")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    balance = get_user_balance(user_id)

    if balance <= 0:
        await message.answer(
            "🌙 Ваши сказки на балансе закончились!\n\n"
            "Чтобы продолжить создавать персональные аудиосказки для малыша, выберите пакет пополнения ниже (или введите /add_free для теста):",
            reply_markup=get_tariffs_inline_kb()
        )
        return

    await message.answer(
        f"👋 Добро пожаловать в мир добрых сказок!\n"
        f"🎁 Доступно сказок на балансе: {balance} шт.\n\n"
        f"👶 Шаг 1 из 3: Напишите имя и возраст ребенка:\n"
        f"(Например: Руслан, 3 года)",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 3: Кто любимый персонаж или игрушка малыша?\n"
        "(Например: попугай Кеша, зайчик Снежок, машинка)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 3: Выберите тему сказки или напишите свою:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# ==========================================
# 6. ПОЛНОЦЕННАЯ ГЕНЕРАЦИЯ БЕЗ ОБРЫВОВ ТЕКСТА
# ==========================================
def clean_text_for_tts(text: str) -> str:
    text = re.sub(r'[*#_~`>|]', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

async def fetch_available_models(session: aiohttp.ClientSession, api_key: str) -> list[str]:
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
                
                def model_priority(m_name: str):
                    m_low = m_name.lower()
                    if "2.0-flash" in m_low and "lite" not in m_low:
                        return 0
                    if "1.5-flash" in m_low and "8b" not in m_low:
                        return 1
                    if "2.5-flash" in m_low:
                        return 2
                    if "flash" in m_low:
                        return 3
                    return 4

                valid_models.sort(key=model_priority)
                if valid_models:
                    cached_models_by_key[api_key] = valid_models
                    return valid_models
    except Exception as e:
        logging.warning(f"Ошибка при запросе списка моделей: {e}")

    return [
        "models/gemini-2.0-flash",
        "models/gemini-1.5-flash",
        "models/gemini-2.5-flash"
    ]

async def generate_story_text(child: str, hero: str, theme: str) -> str:
    global current_key_index
    keys = get_all_gemini_keys()
    if not keys:
        raise RuntimeError("В настройках Render (Environment) не найден GEMINI_API_KEY!")

    prompt = (
        f"Сочини добрую, уютную, убаюкивающую сказку на ночь для малыша.\n"
        f"Главные герои: ребенок ({child}) и его любимый друг ({hero}).\n"
        f"Тема сказки: {theme}.\n"
        f"Требования к тексту:\n"
        f"1. Объем сказки должен быть от 150 до 220 слов (3-4 полноценных абзаца), чтобы при неторопливом чтении она длилась около двух минут.\n"
        f"2. Сюжет должен быть полностью завершенным: мягкое вступление, доброе событие по теме и спокойное, сонное завершение с пожеланием сладких снов.\n"
        f"3. Используй ласковые слова, плавные переходы и спокойный ритм. Пиши только чистый текст самой сказки на русском языке без заголовков и символов форматирования."
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ]
    }

    last_error = "Неизвестная ошибка"
    timeout_cfg = aiohttp.ClientTimeout(total=28.0, connect=8.0)

    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        num_keys = len(keys)
        for k_offset in range(num_keys):
            key_idx = (current_key_index + k_offset) % num_keys
            api_key = keys[key_idx]

            available_models = await fetch_available_models(session, api_key)

            for model_path in available_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={api_key}"
                try:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates and "content" in candidates[0]:
                                finish_reason = candidates[0].get("finishReason", "")
                                parts = candidates[0]["content"].get("parts", [])
                                full_raw_text = "".join(p.get("text", "") for p in parts if "text" in p)
                                clean = clean_text_for_tts(full_raw_text)
                                word_count = len(clean.split())

                                if word_count >= 50 and finish_reason != "MAX_TOKENS":
                                    current_key_index = key_idx
                                    logging.info(f"Сказка создана ({word_count} слов): Ключ #{key_idx+1}, {model_path}")
                                    return clean
                                else:
                                    last_error = f"Короткий ответ ({word_count} слов, причина: {finish_reason})"
                            else:
                                last_error = f"Пустой ответ от {model_path}"
                        else:
                            err_body = await resp.text()
                            last_error = f"Код {resp.status} ({model_path})"
                            if resp.status in (429, 503):
                                await asyncio.sleep(1.0)
                except Exception as e:
                    last_error = f"Ошибка связи ({model_path}): {str(e)[:80]}"

    raise RuntimeError(f"Не удалось сгенерировать полную сказку ({last_error}). Пожалуйста, нажмите кнопку еще раз.")

async def generate_audio_natural(text: str, file_path: str):
    voices = ["ru-RU-SvetlanaNeural", "ru-RU-DariyaNeural"]
    tts_text = text.replace("\n\n", " ... ").replace("\n", " ")

    for voice in voices:
        try:
            communicate = edge_tts.Communicate(tts_text, voice, rate="-10%")
            await asyncio.wait_for(communicate.save(file_path), timeout=25.0)
            if os.path.exists(file_path) and os.path.getsize(file_path) > 5000:
                logging.info(f"Аудио успешно записано голосом {voice}")
                return
        except Exception as e:
            logging.warning(f"Голос {voice} не ответил вовремя: {e}")

    logging.info("Переход на резервную озвучку gTTS...")
    def run_gtts():
        tts = gTTS(text=tts_text, lang='ru', slow=False)
        tts.save(file_path)
    await asyncio.to_thread(run_gtts)

# ==========================================
# 7. ФИНАЛЬНЫЙ ШАГ И ОТПРАВКА СКАЗКИ
# ==========================================
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme_and_finish(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("✍️ Напишите кратко, о чем должна быть сказка (например: как полюбить купаться):")
        return

    user_id = message.from_user.id
    if user_id in active_generations:
        await message.answer("⏳ Ваша сказка уже создается, подождите пару секунд...")
        return

    data = await state.get_data()
    await state.clear()

    if get_user_balance(user_id) <= 0:
        await message.answer("🌙 Сказки на балансе закончились! Пополните баланс (или нажмите /add_free):", reply_markup=get_tariffs_inline_kb())
        return

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")

    active_generations.add(user_id)
    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю длинную добрую сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    
    msg_id = message.message_id.real if hasattr(message.message_id, 'real') else message.message_id
    audio_path = f"/tmp/story_{user_id}_{msg_id}.mp3" if os.path.exists("/tmp") else f"story_{user_id}_{msg_id}.mp3"

    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            story_text = await generate_story_text(child, hero, theme_text)
        
        await message.answer(f"📖 Сказка для: {child}\n\n{story_text}")
        
        try:
            await status_msg.edit_text("🎙 Записываю мягкий убаюкивающий голос (это займет около 10 секунд)...")
        except Exception:
            pass

        async with ChatActionSender.record_voice(bot=bot, chat_id=message.chat.id):
            await generate_audio_natural(story_text, audio_path)
            
        voice_file = FSInputFile(audio_path)
        remaining = use_one_story(user_id)

        await message.answer_voice(
            voice=voice_file, 
            caption=(
                f"🎧 Аудиосказка про героя ({hero}) готова!\n"
                f"Сладких снов малышу ✨\n\n"
                f"🎁 Осталось сказок на балансе: {remaining} шт."
            )
        )
        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.answer("Выберите действие в меню ниже:", reply_markup=main_menu_keyboard)

    except Exception as e:
        logging.error(f"Ошибка при создании сказки: {e}")
        await message.answer(
            f"⚠️ {str(e)[:380]}\n\n(Попытка не списана с вашего баланса)", 
            reply_markup=main_menu_keyboard
        )
    finally:
        active_generations.discard(user_id)
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# ==========================================
# 8. ВЕБ-СЕРВЕР RENDER И БЕЗОПАСНЫЙ ЗАПУСК
# ==========================================
async def handle_ping(request):
    return web.Response(text="OK - FairyTale Bot is running")

async def start_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"WEB SERVER SUCCESSFULLY BOUND TO PORT {port}")
    return runner

async def main():
    logging.info("STARTING BOT SERVICE...")
    server_runner = await start_server()
    init_db()
    
    keys_count = len(get_all_gemini_keys())
    logging.info(f"Загружено ключей Gemini API: {keys_count} шт.")

    await asyncio.sleep(8)
    
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("STARTING TELEGRAM POLLING...")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logging.info("SHUTTING DOWN GRACEFULLY...")
        await bot.session.close()
        await server_runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
