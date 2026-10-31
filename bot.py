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

# 1. Получение ключей
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if not BOT_TOKEN:
    logging.error("ВНИМАНИЕ: Не найден токен бота в переменных окружения!")
if not GEMINI_KEY:
    logging.error("ВНИМАНИЕ: Не найден GEMINI_API_KEY в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_KEY)

# 2. Обработчик команды /start
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

# 3. Обработчик текста для сказки
@dp.message(F.text)
async def generate_story_handler(message: Message):
    status_msg = await message.answer("✨ Сочиняю сказку...")
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Промпт для Gemini
        prompt = (
            "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
            "успокаивающую сказку на ночь на основе запроса:\n\n"
            f"{message.text}\n\n"
            "Требования: простой и ласковый язык для ребенка, объем 150-200 слов, "
            "без markdown-разметки (без звездочек *, решеток # и прочих знаков), чтобы озвучка читала гладко."
        )

        # Генерация текста через Gemini
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        story_text = response.text.strip()

        # Отправляем текст сказки в чат
        await message.answer(story_text)
        await status_msg.edit_text("🎙 Записываю озвучку...")

        # Генерация аудио через Edge TTS (голос Светланы - мягкий женский)
        try:
            communicate = edge_tts.Communicate(story_text, "ru-RU-SvetlanaNeural")
            await communicate.save(audio_path)

            voice_file = FSInputFile(audio_path)
            await message.answer_voice(voice=voice_file, caption="🎧 Приятного прослушивания!")
            await status_msg.delete()

        except Exception as e:
            logging.error(f"Ошибка Edge TTS: {e}")
            await message.answer("Сказка готова текстом выше, но файл озвучки сохранить не удалось.")
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)

    except Exception as e:
        logging.error(f"Ошибка Gemini: {e}")
        await message.answer("Произошла ошибка при создании сказки. Попробуйте еще раз.")

# 4. Веб-сервер для прохождения проверки порта Render
async def handle_ping(request):
    return web.Response(text="Bot is running")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    
    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}")

# 5. Главная функция одновременного запуска
async def main():
    # Запускаем одновременно веб-сервер для Render и polling бота
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
