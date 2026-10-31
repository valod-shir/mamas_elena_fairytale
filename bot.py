import os
import asyncio
import logging
import time
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
        ai_client = genai.Client(api_key=GEMINI_KEY)
    except Exception as e:
        logging.error(f"Ошибка Gemini: {e}")

# 2. Состояния FSM
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()
    waiting_for_voice = State()

# 3. Клавиатуры
theme_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="😴 На крепкий сон"), KeyboardButton(text="🥣 Хороший аппетит")],
        [KeyboardButton(text="🧸 Делиться игрушками"), KeyboardButton(text="🦷 Чистить зубки")],
        [KeyboardButton(text="✍️ Своя тема")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

voice_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👩 Голос мамы (Светлана)")],
        [KeyboardButton(text="👨 Голос папы (Дмитрий)")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# 4. Команда /start
@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 **Добро пожаловать в мир добрых сказок!**\n\n"
        "Я помогу создать персональную терапевтическую аудиосказку для вашего малыша.\n\n"
        "👶 **Шаг 1 из 4:** Напишите имя и возраст ребенка:\n"
        "*(Например: Влада, полтора года)*"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 5. Шаг 1: Имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 **Шаг 2 из 4:** Кто любимый персонаж или игрушка малыша?\n"
        "*(Например: котенок Мау, зайчик, машинка)*",
        parse_mode="Markdown"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 6. Шаг 2: Персонаж
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 **Шаг 3 из 4:** Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Шаг 3: Тема
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    await state.update_data(theme=theme_text)
    await message.answer(
        "🎙 **Шаг 4 из 4:** Каким голосом озвучить сказку?",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# Надежная генерация с моделью gemini-3.8-flash и автоповтором при 503
def generate_story_sync(child: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("GEMINI_API_KEY не задан")

    prompt = (
        f"Ты детский сказочник. Сочини короткую, нежную терапевтическую сказку на ночь.\n"
        f"Ребенок: {child}. Герой: {hero}. Тема: {theme}.\n"
        f"Объем СТРОГО 70-90 слов. Простые короткие предложения. "
        f"Пиши чистым текстом БЕЗ спецсимволов (*, #, _), чтобы диктор читал ровно."
    )

    last_error = None
    # Делаем до 4 попыток с паузой на случай временной пиковой нагрузки
    for attempt in range(1, 5):
        try:
            response = ai_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=prompt
            )
            if response and response.text:
                return response.text.replace("*", "").replace("#", "").strip()
        except Exception as e:
            last_error = e
            logging.warning(f"Попытка {attempt} завершилась ошибкой: {e}. Ожидание...")
            time.sleep(attempt * 1.5)  # 1.5с, затем 3.0с, затем 4.5с

    raise RuntimeError(f"Сервер Gemini перегружен: {last_error}")

# Озвучка Edge-TTS с переключением на Дмитрий / Светлана
async def generate_audio_fast(text: str, voice_str: str, file_path: str):
    is_male = ("Дмитрий" in voice_str or "папы" in voice_str)
    voice_name = "ru-RU-DmitryNeural" if is_male else "ru-RU-SvetlanaNeural"

    try:
        communicate = edge_tts.Communicate(text, voice_name)
        await asyncio.wait_for(communicate.save(file_path), timeout=12.0)
    except Exception as e:
        logging.warning(f"Edge TTS сбой ({e}), переключаюсь на gTTS...")
        def run_fallback():
            tts = gTTS(text=text, lang='ru', slow=False)
            tts.save(file_path)
        await asyncio.to_thread(run_fallback)

# 8. Шаг 4: Генерация и отправка
@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_finish(message: Message, state: FSMContext):
    voice_choice = message.text.strip()
    data = await state.get_data()
    await state.clear()

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")
    theme = data.get("theme", "сон")

    status_msg = await message.answer("✨ Волшебство начинается! Сочиняю сказку...", reply_markup=ReplyKeyboardRemove())
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Генерация текста
        story_text = await asyncio.to_thread(generate_story_sync, child, hero, theme)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")

        try:
            await status_msg.edit_text("🎙 Записываю сказку голосом...")
        except Exception:
            pass

        # Генерация аудио
        await generate_audio_fast(story_text, voice_choice, audio_path)

        voice_file = FSInputFile(audio_path)
        is_male = ("Дмитрий" in voice_choice or "папы" in voice_choice)
        voice_label = "папы Дмитрия" if is_male else "мамы Светланы"

        await message.answer_voice(
            voice=voice_file, 
            caption=f"🎧 Сказка про {hero}\nОзвучено голосом {voice_label} ✨"
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        next_btn = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/start 🪄 Создать новую сказку")]],
            resize_keyboard=True
        )
        await message.answer("Хотите сочинить еще одну сказку?", reply_markup=next_btn)

    except Exception as e:
        logging.error(f"Ошибка создания сказки: {e}")
        await message.answer(f"⚠️ Ошибка создания сказки: {str(e)[:200]}")
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# 9. Проверенный веб-сервер Render (AppRunner)
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
    await start_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
