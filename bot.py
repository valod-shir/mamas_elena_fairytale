import os
import sys
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from telebot.async_telebot import AsyncTeleBot
from google import genai
from gtts import gTTS

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# 1. Запуск мини веб-сервера для Render (устраняет ошибку "No open ports detected")
class RenderHealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # Не засоряем логи фоновыми проверками Render

def run_background_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckHandler)
    server.serve_forever()

threading.Thread(target=run_background_web_server, daemon=True).start()

# 2. Инициализация токенов и клиентов
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    logging.error("ВНИМАНИЕ: Не заданы переменные TELEGRAM_BOT_TOKEN или GEMINI_API_KEY в Render Environment!")

bot = AsyncTeleBot(TELEGRAM_BOT_TOKEN)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 3. Обработчик команды /start
@bot.message_handler(commands=['start'])
async def send_welcome(message):
    welcome_text = (
        "👋 Привет! Я создаю персональные аудиосказки для детей.\n\n"
        "Напиши мне:\n"
        "1. Имя и возраст ребенка\n"
        "2. Любимые герои или игрушки\n"
        "3. Какую проблему решить или тему сказки\n\n"
        "✍️ Отправь всё одним сообщением:"
    )
    await bot.reply_to(message, welcome_text)

# Вспомогательная функция для синхронного сохранения аудио через gTTS
def generate_audio_file(text: str, filename: str):
    tts = gTTS(text=text, lang='ru', slow=False)
    tts.save(filename)

# 4. Основной обработчик создания сказки
@bot.message_handler(func=lambda message: True)
async def generate_fairytale(message):
    status_msg = await bot.reply_to(message, "✨ Сочиняю сказку...")
    
    try:
        # Промпт для Gemini
        prompt = (
            "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
            "успокаивающую и добрую сказку на ночь на основе следующего запроса:\n\n"
            f"{message.text}\n\n"
            "Требования к сказке:\n"
            "- Простой, плавный и мелодичный язык для маленького ребенка.\n"
            "- Длина примерно 150-200 слов.\n"
            "- Без markdown-разметки (без звездочек, решеток и спецсимволов), чтобы текст идеально читался озвучкой."
        )

        # Генерация текста сказки через Gemini
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt
        )
        story_text = response.text.strip()
        
        # Отправляем готовый текст в чат
        await bot.send_message(message.chat.id, story_text)
        await bot.edit_message_text("🎙 Записываю озвучку...", chat_id=message.chat.id, message_id=status_msg.message_id)

        # Имя временного аудиофайла
        audio_file_path = f"story_{message.chat.id}_{message.message_id}.mp3"
        
        try:
            # Озвучиваем текст в отдельном потоке, чтобы не блокировать бота
            await asyncio.to_thread(generate_audio_file, story_text, audio_file_path)
            
            # Отправляем аудиофайл в Telegram
            with open(audio_file_path, 'rb') as audio:
                await bot.send_audio(
                    message.chat.id, 
                    audio, 
                    title="Добрая сказка на ночь", 
                    performer="Сказки для малышей",
                    caption="🎧 Приятного прослушивания!"
                )
                
            await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)

        except Exception as e:
            logging.error(f"Ошибка при создании аудио: {e}")
            await bot.send_message(
                message.chat.id, 
                "Сказка готова текстом выше, но файл озвучки сохранить не удалось."
            )
        finally:
            # Удаляем временный файл с диска
            if os.path.exists(audio_file_path):
                os.remove(audio_file_path)

    except Exception as e:
        logging.error(f"Общая ошибка генератора: {e}")
        await bot.send_message(message.chat.id, "Произошла ошибка при сочинении сказки. Попробуйте еще раз.")

# 5. Точка входа
if __name__ == "__main__":
    asyncio.run(bot.polling(non_stop=True))
