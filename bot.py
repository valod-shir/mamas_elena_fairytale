import os
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
import edge_tts
from gtts import gTTS

logging.basicConfig(level=logging.INFO)

# Чтение переменных окружения
BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or "").strip().strip("'\"")
GEMINI_KEY = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip().strip("'\"")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализируем клиент
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()
    waiting_for_voice = State()

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

@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 **Добро пожаловать в мир добрых сказок!**\n\n"
        "Я создаю персональные аудиосказки для детей за 10 секунд.\n\n"
        "👶 **Шаг 1 из 4:** Напишите имя и возраст ребенка:\n"
        "*(Например: Влада, полтора года)*"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 **Шаг 2 из 4:** Кто любимый персонаж или игрушка малыша?\n"
        "*(Например: котенок Мау, зайчик, синий трактор)*",
        parse_mode="Markdown"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 **Шаг 3 из 4:** Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "✍️ Своя тема":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    await state.update_data(theme=text)
    await message.answer(
        "🎙 **Шаг 4 из 4:** Выберите голос для озвучки:",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# 1. Сверхбыстрая асинхронная генерация Gemini с таймаутом
async def generate_story_async(child: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("GEMINI_API_KEY не задан")

    prompt = (
        f"Ты детский сказочник. Сочини короткую, нежную терапевтическую сказку на ночь.\n"
        f"Ребенок: {child}. Герой: {hero}. Тема: {theme}.\n"
        f"ОБЪЕМ: строго 70-90 слов. Предложения короткие и простые. "
        f"БЕЗ символов форматирования (*, #, _), только чистый текст для диктора."
    )

    # Используем асинхронный клиент ai_client.aio с лимитом 12 секунд
    response = await asyncio.wait_for(
        ai_client.aio.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        ),
        timeout=12.0
    )
    return response.text.strip()

# 2. Быстрая озвучка с мужским/женским голосом
async def generate_speech_async(text: str, voice_str: str, file_path: str):
    is_male = ("Дмитрий" in voice_str or "папы" in voice_str)
    voice_name = "ru-RU-DmitryNeural" if is_male else "ru-RU-SvetlanaNeural"

    try:
        # Пробуем Edge-TTS с таймаутом 10 секунд
        communicate = edge_tts.Communicate(text, voice_name)
        await asyncio.wait_for(communicate.save(file_path), timeout=10.0)
    except Exception as e:
        logging.warning(f"Edge TTS не ответил вовремя ({e}), быстрый резерв gTTS...")
        # Резерв gTTS через отдельный поток
        def run_gtts():
            tts = gTTS(text=text, lang='ru', slow=False)
            tts.save(file_path)
        await asyncio.to_thread(run_gtts)

@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_finish(message: Message, state: FSMContext):
    voice_choice = message.text.strip()
    data = await state.get_data()
    await state.clear()

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")
    theme = data.get("theme", "сон")

    status_msg = await message.answer("✨ Сочиняю сказку...", reply_markup=ReplyKeyboardRemove())
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Шаг 1: Текст (2-4 секунды)
        story_text = await generate_story_async(child, hero, theme)
        await message.answer(f"📖 **Сказка для {child}**\n\n{story_text}", parse_mode="Markdown")
        
        try:
            await status_msg.edit_text("🎙 Озвучиваю сказку...")
        except Exception:
            pass

        # Шаг 2: Аудио (2-5 секунд)
        await generate_speech_async(story_text, voice_choice, audio_path)

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

        next_kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/start 🪄 Создать новую сказку")]],
            resize_keyboard=True
        )
        await message.answer("Хотите сочинить еще одну сказку?", reply_markup=next_kb)

    except asyncio.TimeoutError:
        await message.answer("⚠️ Сервер нейросети отвечал слишком долго. Пожалуйста, нажмите /start и попробуйте еще раз.")
    except Exception as e:
        logging.error(f"Ошибка создания сказки: {e}")
        await message.answer(f"⚠️ Ошибка создания: {str(e)[:150]}")
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# Веб-сервер Render
async def handle_ping(request):
    return web.Response(text="OK")

async def start_background_tasks(app):
    asyncio.create_task(dp.start_polling(bot))

def make_app():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    app.on_startup.append(start_background_tasks)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = make_app()
    web.run_app(app, host='0.0.0.0', port=port)
