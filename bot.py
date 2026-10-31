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

logging.basicConfig(level=logging.INFO)

# 1. Считываем ключи
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'").strip('"')
GEMINI_KEY = raw_gemini_key.strip().strip("'").strip('"')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_KEY)

# 2. Определяем шаги сценария (FSM)
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
        [KeyboardButton(text="✍️ Написать свою тему")]
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
        "👋 Добро пожаловать в мир добрых сказок!\n\n"
        "Я помогу создать персональную терапевтическую аудиосказку для вашего малыша.\n\n"
        "👶 Шаг 1 из 4: Напишите имя и возраст ребенка.\n"
        "(Например: Влада, полтора года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 5. Шаг 1: Получаем имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 4: Кто любимый герой, персонаж или игрушка малыша?\n"
        "(Например: плюшевый котенок Мау, машинка Молния, зайчик)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 6. Шаг 2: Получаем любимого героя
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 4: Выберите тему сказки или напишите свою:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Шаг 3: Получаем тему сказки
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Написать свою тему":
        await message.answer("Напишите кратко, о чем должна быть сказка или какую привычку нужно привить:")
        return

    await state.update_data(theme=theme_text)
    await message.answer(
        "🎙 Шаг 4 из 4: Каким голосом озвучить сказку?",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# Синхронная генерация через актуальную Gemini
def generate_story_sync(child_info: str, hero: str, theme: str) -> str:
    system_instruction = (
        "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
        "успокаивающую сказку на ночь на основе запроса пользователя. "
        "Язык должен быть простым и ласковым, объем 130-180 слов. "
        "Пиши чистым текстом БЕЗ спецсимволов markdown (*, #, _), чтобы озвучка читала ровно."
    )
    user_prompt = f"Ребенок: {child_info}\nЛюбимый герой: {hero}\nТема: {theme}"
    full_prompt = f"{system_instruction}\n\nЗапрос:\n{user_prompt}"

    response = ai_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=full_prompt
    )
    return response.text.strip()

# 8. Шаг 4: Выбор голоса и отправка готовой сказки
@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_generate(message: Message, state: FSMContext):
    voice_choice = message.text.strip()
    selected_voice = "ru-RU-DmitryNeural" if "Дмитрий" in voice_choice else "ru-RU-SvetlanaNeural"

    data = await state.get_data()
    await state.clear()

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")
    theme = data.get("theme", "добрую тему")

    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Вызов генерации без переносов строк в аргументах
        story_text = await asyncio.to_thread(generate_story_sync, child, hero, theme)
        
        # Отправляем текст сказки
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")
        await status_msg.edit_text("🎙 Записываю сказку голосом...")

        # Озвучка через Edge TTS
        try:
            communicate = edge_tts.Communicate(story_text, selected_voice)
            await communicate.save(audio_path)

            voice_file = FSInputFile(audio_path)
            await message.answer_voice(
                voice=voice_file, 
                caption=f"🎧 Сказка про {hero} готова!\nСладких снов малышу ✨"
            )
            await status_msg.delete()

            new_btn = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start 🪄 Создать новую сказку")]],
                resize_keyboard=True
            )
            await message.answer("Хотите сочинить еще одну сказку?", reply_markup=new_btn)

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
        await message.answer(f"⚠️ Ошибка создания сказки: {error_details[:200]}")

# 9. Фоновый веб-сервер для прохождения проверки порта Render
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
