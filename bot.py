import os
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from google import genai
import edge_tts

logging.basicConfig(level=logging.INFO)

# Получение переменных окружения
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'").strip('"')
GEMINI_KEY = raw_gemini_key.strip().strip("'").strip('"')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_KEY)

@dp.message(CommandStart())
async def start_cmd(message: Message):
    welcome_text = (
        "👋 Привет! Я создаю персональные аудиосказки для детей.\n\n"
        "Напиши мне:\n"
        "1. Имя и возраст ребенка\n"
        "2. Любимые герои или игрушки\n"
        "3. Какую проблему решить или тему сказки\n\n"
        "✍️ Отправь всё одним сообщением:"
    )
    await message.answer(welcome_text)

def generate_story_sync(prompt_text: str) -> str:
    system_instruction = (
        "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
        "успокаивающую сказку на ночь на основе запроса пользователя. "
        "Язык должен быть простым и ласковым, объем 130-180 слов. "
        "Пиши чистым текстом БЕЗ спецсимволов markdown (*, #, _), чтобы озвучка читала ровно."
    )
    
    response = ai_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=f"{system_instruction}\n\nЗапрос:\n{prompt_text}"
    )
    return response.text.strip()

@dp.message(F.text)
async def handle_user_text(message: Message):
    status_msg = await message.answer("✨ Сочиняю сказку...")
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # 1. Генерация текста сказки
        story_text = await asyncio.to_thread(generate_story_sync, message.text)
        await message.answer(story_text)
        await status_msg.edit_text("🎙 Записываю озвучку...")

        # 2. Озвучка через Edge TTS
        try:
            communicate = edge_tts.Communicate(story_text, "ru-RU-SvetlanaNeural")
            await communicate.save(audio_path)

            voice = FSInputFile(audio_path)
            await message.answer_voice(voice=voice, caption="🎧 Приятного прослушивания!")
            await status_msg.delete()

        except Exception as tts_err:
            logging.error(f"Ошибка озвучки: {tts_err}")
            await message.answer("Сказка готова текстом выше, но файл озвучки сохранить не удалось.")
        finally:
            if os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    except Exception as e:
        error_details = str(e)
        logging.error(f"Ошибка генерации: {error_details}")
        await message.answer(f"⚠️ Ошибка генерации:\n{error_details[:300]}")

# Фоновый веб-сервер для прохождения проверки порта Render
async def handle_ping(request):
    return web.Response(text="Bot is running")

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
