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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

VOICE = "ru-RU-SvetlanaNeural"

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

    url = "https://models.inference.ai.azure.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GITHUB_TOKEN}"
    }
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты профессиональный детский писатель-сказкотерапевт. "
                    "Напиши короткую добрую сказку на ночь на основе запроса (200-250 слов). "
                    "Не используй спецсимволы, звездочки или разметку, так как текст идет в озвучку."
                )
            },
            {"role": "user", "content": user_prompt}
        ],
        "model": "gpt-4o-mini",
        "temperature": 0.7
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                story_text = data["choices"][0]["message"]["content"]
    except Exception as e:
        await status_msg.edit_text("Не удалось получить текст от ИИ. Проверь GITHUB_TOKEN.")
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
        await audio_status.edit_text("Сказка готова текстом выше, но озвучка не удалась.")
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)
        await state.clear()

# Микро-сервер для удовлетворения проверок Render
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

