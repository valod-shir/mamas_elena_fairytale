import asyncio
import os
import aiohttp
from aiohttp import web
import edge_tts
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Мягкий и глубокий голос сказочника с убаюкивающим темпом
VOICE = "ru-RU-DmitryNeural"
VOICE_RATE = "-12%"

class StoryForm(StatesGroup):
    waiting_for_details = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await message.answer(
        "👋 Привет! Я создаю персональные аудиосказки для детей.\n\n"
        "Напиши мне:\n"
        "1. Имя и возраст ребенка\n"
        "2. Любимые герои или игрушки\n"
        "3. Какую проблему решить или тему сказки\n\n"
        "✍️ Отправь всё одним сообщением:"
    )
    await state.set_state(StoryForm.waiting_for_details)

@dp.message(StoryForm.waiting_for_details)
async def generate_story(message: types.Message, state: FSMContext):
    user_prompt = message.text
    status_msg = await message.answer("✨ Сочиняю сказку...")

    if not GEMINI_API_KEY:
        await status_msg.edit_text("Ошибка: в настройках Render не задан GEMINI_API_KEY!")
        await state.clear()
        return

    key = GEMINI_API_KEY.strip()
    headers = {"Content-Type": "application/json"}
    
    prompt = (
        "Ты детский писатель-сказкотерапевт. Напиши добрую, нежную и короткую терапевтическую сказку "
        "для малыша на русском языке на основе пожелания: "
        f"{user_prompt}\n\n"
        "Требования: объем около 130-160 слов. Пиши сплошным текстом без звездочек (*), "
        "решеток (#), кавычек, дефисов в начале строк и списков, так как текст пойдет прямо в аудио-озвучку."
    )

    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    story_text = None
    debug_info = ""

    async with aiohttp.ClientSession() as session:
        # Автоматический опрос доступных моделей для данного ключа
        list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
        try:
            async with session.get(list_url) as resp:
                models_data = await resp.json()
                if "models" in models_data:
                    available_models = [
                        m["name"] for m in models_data["models"] 
                        if "generateContent" in m.get("supportedGenerationMethods", [])
                    ]
                else:
                    available_models = []
                    debug_info = f"Ошибка списка моделей: {models_data}"
        except Exception as e:
            available_models = []
            debug_info = f"Сетевая ошибка при запросе моделей: {e}"

        if available_models:
            available_models.sort(key=lambda x: ("flash" not in x, x))
            for full_model_name in available_models:
                gen_url = f"https://generativelanguage.googleapis.com/v1beta/{full_model_name}:generateContent?key={key}"
                try:
                    async with session.post(gen_url, headers=headers, json=payload) as resp:
                        data = await resp.json()
                        if "candidates" in data and len(data["candidates"]) > 0:
                            story_text = data["candidates"][0]["content"]["parts"][0]["text"]
                            break
                        else:
                            debug_info = f"Ошибка {full_model_name}: {str(data.get('error', data))[:120]}"
                except Exception as e:
                    debug_info = str(e)
        elif not debug_info:
            debug_info = "Google не вернул доступных моделей для текста."

    if not story_text:
        await status_msg.edit_text(f"Не удалось получить текст от Gemini:\n{debug_info[:300]}")
        await state.clear()
        return

    await status_msg.edit_text(story_text)
    audio_status = await message.answer("🎙 Записываю аудиосказку...")

    audio_path = f"story_{message.from_user.id}.mp3"
    try:
        communicate = edge_tts.Communicate(story_text, VOICE, rate=VOICE_RATE)
        await communicate.save(audio_path)

        audio_file = FSInputFile(audio_path)
        await message.answer_audio(audio_file, caption="🎧 Персональная сказка")
        await audio_status.delete()
    except Exception as e:
        await audio_status.edit_text(f"Ошибка озвучки: {e}")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        await state.clear()

async def handle_ping(request):
    return web.Response(text="Bot is running")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
