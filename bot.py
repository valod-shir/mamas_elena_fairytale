import os
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
import google.generativeai as genai
import edge_tts

logging.basicConfig(level=logging.INFO)

# Получение переменных окружения (с удалением случайных пробелов и кавычек)
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'").strip('"')
GEMINI_KEY = raw_gemini_key.strip().strip("'").strip('"')

if not BOT_TOKEN:
    logging.error("ВНИМАНИЕ: TELEGRAM_BOT_TOKEN не задан!")
if not GEMINI_KEY:
    logging.error("ВНИМАНИЕ: GEMINI_API_KEY не задан!")
else:
    genai.configure(api_key=GEMINI_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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

def generate_story_with_fallback(prompt_text: str) -> str:
    system_prompt = (
        "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
        "успокаивающую сказку на ночь на основе запроса пользователя. "
        "Язык должен быть простым и ласковым, объем 130-180 слов. "
        "Пиши чистым текстом БЕЗ спецсимволов markdown (*, #, _), чтобы озвучка читала плавно."
    )
    full_prompt = f"{system_prompt}\n\nЗапрос:\n{prompt_text}"
    
    # Список моделей для автоматического перебора при недоступности
    candidate_models = [
        "gemini-1.5-flash-latest",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro"
    ]
    
    last_err = None
    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(full_prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as err:
            last_err = err
            continue
            
    raise RuntimeError(f"Сбой всех моделей Gemini: {last_err}")

@dp.message(F.text)
async def handle_user_text(message: Message):
    status_msg = await message.answer("✨ Сочиняю сказку...")
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        if not GEMINI_KEY:
            raise ValueError("Переменная GEMINI_API_KEY отсутствует в настройках Render Environment")

        # 1. Генерация текста
        story_text = await asyncio.to_thread(generate_story_with_fallback, message.text)
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
        logging.error(f"Ошибка Gemini: {error_details}")
        # Выводим точную техническую причину прямо пользователю для быстрой диагностики
        await message.answer(f"⚠️ Ошибка генерации:\n{error_details[:300]}")

# 3. Сервер для Render
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
