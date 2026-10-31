import os
import sqlite3
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, 
    FSInputFile, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove
)
from google import genai
from google.genai import types
import edge_tts
from gtts import gTTS

logging.basicConfig(level=logging.INFO)

# 1. Читаем ключи окружения
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'\"")
GEMINI_KEY = raw_gemini_key.strip().strip("'\"")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ai_client = None
if GEMINI_KEY:
    try:
        ai_client = genai.Client(
            api_key=GEMINI_KEY,
            http_options=types.HttpOptions(timeout=10000)
        )
    except Exception as e:
        logging.error(f"Ошибка инициализации Gemini: {e}")

# 2. База данных SQLite для учета пользователей и баланса
DB_PATH = "users.db"
FREE_START_STORIES = 2  # Сколько бесплатных сказок дается новичку

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            stories_left INTEGER DEFAULT 2,
            total_generated INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def get_user_balance(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO users (user_id, stories_left, total_generated) VALUES (?, ?, 0)",
            (user_id, FREE_START_STORIES)
        )
        conn.commit()
        balance = FREE_START_STORIES
    else:
        balance = row[0]
    conn.close()
    return balance

def use_one_story(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET stories_left = MAX(0, stories_left - 1), total_generated = total_generated + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_stats(user_id: int):
    balance = get_user_balance(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_generated FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    total = row[0] if row else 0
    return balance, total

# 3. Состояния FSM
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()

# 4. Клавиатуры
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
        [KeyboardButton(text="✍️ Своя тема")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# 5. Обработчики кнопок баланса и покупки
@dp.message(F.text == "👤 Мой баланс")
async def show_balance(message: Message):
    balance, total = get_user_stats(message.from_user.id)
    text = (
        f"👤 Ваш профиль:\n\n"
        f"🎧 Доступно сказок: {balance} шт.\n"
        f"📚 Всего создано сказок: {total} шт.\n\n"
        f"Хотите сочинить новую сказку или пополнить баланс?"
    )
    await message.answer(text, reply_markup=main_menu_keyboard)

@dp.message(F.text == "💎 Купить сказки")
async def show_tariffs(message: Message):
    balance = get_user_balance(message.from_user.id)
    text = (
        f"💎 Пополнение баланса сказок\n"
        f"(Сейчас у вас доступно: {balance} шт.)\n\n"
        f"Наши пакеты:\n"
        f"🌟 5 сказок — 149 ₽ (или 75 ⭐️)\n"
        f"🌟 15 сказок — 299 ₽ (или 150 ⭐️)\n"
        f"🌟 30 сказок на месяц — 490 ₽ (или 250 ⭐️)\n\n"
        f"Автоматическая оплата подключается на следующем этапе!"
    )
    await message.answer(text, reply_markup=main_menu_keyboard)

# 6. Старт и начало создания сказки
@dp.message(CommandStart())
@dp.message(F.text.startswith("/start"))
@dp.message(F.text == "🪄 Создать новую сказку")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    balance = get_user_balance(user_id)

    if balance <= 0:
        no_balance_text = (
            "🌙 Ваши бесплатные сказки закончились!\n\n"
            "Надеемся, малышу понравились наши добрые истории. "
            "Чтобы продолжить создавать персональные аудиосказки на каждую ночь, пополните баланс:\n\n"
            "🌟 5 сказок — 149 ₽\n"
            "🌟 15 сказок — 299 ₽\n"
            "🌟 30 сказок — 490 ₽"
        )
        await message.answer(no_balance_text, reply_markup=main_menu_keyboard)
        return

    welcome_text = (
        f"👋 Добро пожаловать в мир добрых сказок!\n"
        f"🎁 Доступно сказок на балансе: {balance} шт.\n\n"
        f"👶 Шаг 1 из 3: Напишите имя и возраст ребенка:\n"
        f"(Например: Влада, полтора года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 7. Шаг 1: Имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 3: Кто любимый персонаж или игрушка малыша?\n"
        "(Например: котенок Мау, зайчик, машинка)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 8. Шаг 2: Персонаж
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 3: Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 9. Быстрая генерация текста Gemini
async def generate_story_fast(child: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("Ключ GEMINI_API_KEY не задан")

    prompt = (
        f"Сочини короткую, ласковую терапевтическую сказку на ночь для малыша.\n"
        f"Ребенок: {child}. Любимый герой: {hero}. Тема: {theme}.\n"
        f"Объем строго 65-85 слов. Простые предложения, чистый текст без звездочек и спецсимволов."
    )

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.8-pro"
    ]

    for model_name in candidate_models:
        for attempt in range(2):
            try:
                res = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=model_name,
                    contents=prompt
                )
                if res and res.text:
                    return res.text.replace("*", "").replace("#", "").strip()
            except Exception as e:
                err_str = str(e)
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                await asyncio.sleep(1.0)

    try:
        available = await asyncio.to_thread(ai_client.models.list)
        for m in available:
            m_name = m.name.replace("models/", "")
            if "gemini" in m_name and ("flash" in m_name or "pro" in m_name):
                try:
                    res = await asyncio.to_thread(
                        ai_client.models.generate_content,
                        model=m_name,
                        contents=prompt
                    )
                    if res and res.text:
                        return res.text.replace("*", "").replace("#", "").strip()
                except Exception:
                    continue
    except Exception:
        pass

    raise RuntimeError("Сервер нейросети сейчас перегружен. Пожалуйста, попробуйте через минуту.")

# 10. Озвучка
async def generate_audio_fast(text: str, file_path: str):
    try:
        communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
        await asyncio.wait_for(communicate.save(file_path), timeout=3.5)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 500:
            return
    except Exception as e:
        logging.warning(f"Переключение на gTTS: {e}")

    def run_gtts():
        tts = gTTS(text=text, lang='ru', slow=False)
        tts.save(file_path)

    await asyncio.to_thread(run_gtts)

# 11. Шаг 3: Генерация, отправка и списание с баланса
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme_and_finish(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    data = await state.get_data()
    await state.clear()

    user_id = message.from_user.id
    if get_user_balance(user_id) <= 0:
        await message.answer("🌙 На вашем балансе закончились сказки!", reply_markup=main_menu_keyboard)
        return

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")

    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю и озвучиваю сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # 1. Текст
        story_text = await generate_story_fast(child, hero, theme_text)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")

        try:
            await status_msg.edit_text("🎙 Записываю аудиосказку...")
        except Exception:
            pass

        # 2. Звук
        await generate_audio_fast(story_text, audio_path)

        voice_file = FSInputFile(audio_path)
        
        # Списываем 1 сказку с баланса только после успешной генерации
        remaining = use_one_story(user_id)

        await message.answer_voice(
            voice=voice_file, 
            caption=f"🎧 Аудиосказка про {hero} готова!\nСладких снов малышу ✨\n\n🎁 Осталось сказок на балансе: {remaining} шт."
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.answer("Выберите действие в меню ниже:", reply_markup=main_menu_keyboard)

    except Exception as e:
        logging.error(f"Ошибка создания сказки: {e}")
        await message.answer(
            f"⚠️ {str(e)[:200]}\n(Попытка не списана с вашего баланса)", 
            reply_markup=main_menu_keyboard
        )
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# 12. Веб-сервер Render
async def handle_ping(request):
    return web.Response(text="OK")

async def start_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

async def main():
    init_db()  # Создаем базу при запуске
    await start_server()
    await asyncio.sleep(2)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
