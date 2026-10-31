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

VOICE = "ru-RU-SvetlanaNeural"

GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash-latest",
    "gemini-pro"
]

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

    headers = {"Content-Type": "application/json"}
    prompt = (
        "Ты детский писатель-сказкотерапевт. Напиши добрую, нежную и короткую терапевтическую сказку "
        "для малыша на русском языке на основе пожелания: "
        f"{user_prompt}\n\n"
        "Требования: объем около 150-200 слов. Используй только простой связный текст без звездочек (*), "
        "решеток (#), кавычек и форматирования, так как текст пойдет прямо в аудио-озвучку."
    )

    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ]
    }

    story_text = None
    last_err = ""

    async with aiohttp.ClientSession() as session:
        for model in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY.strip()}"
            try:
                async with session.post(url, headers=headers, json=payload) as resp:
                    data = await resp.json()
                    if "candidates" in data and len(data["candidates"]) > 0:
                        story_text = data["candidates"][0]["content"]["parts"][0]["text"]
                        break
                    else:
                        last_err = str(data.get("error", data))
            except Exception as e:
                last_err = str(e)

    if not story_text:
        await status_msg.edit_text(f"Ответ Gemini: {last_err[:300]}")
        await state.clear()
        return

    await status_msg.edit_text(story_text)
    audio_status = await message.answer("🎙 Записываю аудиосказку...")

    audio_path = f"story_{message.from_user.id}.mp3"
    try:
        communicate = edge_tts.Communicate(story_text, VOICE)
        await communicate.save(audio_path)

        audio_file = FSInputFile(audio_path)
        await message.answer_audio(audio_file, caption="🎧 Персональная сказка")
        await audio_status.delete()
    except Exception as e:
        await audio_status.edit_text("Сказка готова текстом выше, но файл озвучки сохранить не удалось.")
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
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
