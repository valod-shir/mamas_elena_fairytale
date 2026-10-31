import os
import asyncio
import logging
import traceback
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
import google.generativeai as genai
import edge_tts

logging.basicConfig(level=logging.INFO)

# 1. Считываем ключи (проверяем все возможные варианты названий)
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

if not BOT_TOKEN:
    logging.error("ОШИБКА: Токен бота не найден в Environment!")
if not GEMINI_KEY:
    logging.error("ОШИБКА: Ключ Gemini (GEMINI_API_KEY) не найден в Environment!")
else:
    genai.configure(api_key=GEMINI_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 2. Команда /start
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

# Функция генерации текста через Gemini
def get_gemini_story(user_prompt: str) -> str:
    prompt = (
        "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
        "успокаивающую сказку на ночь на основе следующего запроса:\n\n"
        f"{user_prompt}\n\n"
        "Требования: ласковый и понятный язык для ребенка, 130-180 слов, "
        "строго без символов markdown-разметки (*, #, _), чтобы озвучка читала ровно."
    )
    # Используем стабильную быструю модель
    model = genai.GenerativeModel("gemini-1.5-flash")
    res = model.generate_content(prompt)
    return res.text.strip()

# 3. Обработчик сообщений
@dp.message(F.text)
async def handle_text(message: Message):
    status_msg = await message.answer("✨ Сочиняю сказку...")
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Генерация сказки
        story_text = await asyncio.to_thread(get_gemini_story, message.text)
        await message.answer(story_text)
        await status_msg.edit_text("🎙 Записываю озвучку...")

        # Генерация аудио
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
                os.remove(audio_path)

    except Exception as e:
        logging.error(f"Ошибка при работе Gemini: {e}")
        traceback.print_exc()
        await message.answer("Произошла ошибка при создании сказки. Попробуйте еще раз.")

# 4. Веб-сервер для прохождения проверки порта на Render
async def handle_ping(request):
    return web.Response(text="Bot is alive")

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
