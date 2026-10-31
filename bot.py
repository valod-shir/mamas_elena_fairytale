import os
import asyncio
import logging
import time
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
from gtts import gTTS

logging.basicConfig(level=logging.INFO)

# 1. Читаем ключи окружения
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'").strip('"')
GEMINI_KEY = raw_gemini_key.strip().strip("'").strip('"')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализируем клиент Gemini
ai_client = None
if GEMINI_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_KEY)
    except Exception as e:
        logging.error(f"Ошибка создания клиента Gemini: {e}")

# 2. Состояния диалога (FSM)
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
        [KeyboardButton(text="👩 Голос мамы")],
        [KeyboardButton(text="👨 Голос папы")]
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
        "Я помогу создать персональную аудиосказку для вашего малыша.\n\n"
        "👶 Шаг 1 из 4: Напишите имя и возраст ребенка.\n"
        "(Например: Влада, полтора года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 5. Шаг 1: Имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 4: Кто любимый герой или игрушка малыша?\n"
        "(Например: плюшевый котенок Мау, машинка, зайчик)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 6. Шаг 2: Любимый герой
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 4: Выберите тему сказки или напишите свою:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Шаг 3: Тема сказки
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Написать свою тему":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    await state.update_data(theme=theme_text)
    await message.answer(
        "🎙 Шаг 4 из 4: Каким голосом озвучить сказку?",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# Синхронная генерация текста Gemini
def generate_story_sync(child_info: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("GEMINI_API_KEY не настроен")

    system_instruction = (
        "Ты добрый сказочник для малышей. Сочини короткую, терапевтическую, "
        "успокаивающую сказку на ночь на основе запроса пользователя. "
        "Язык ласковый и понятный, объем 110-150 слов. "
        "Пиши чистым текстом БЕЗ спецсимволов markdown (*, #, _), чтобы озвучка читала плавно."
    )
    user_prompt = f"Ребенок: {child_info}\nГерой: {hero}\nТема: {theme}"
    full_prompt = f"{system_instruction}\n\nЗапрос:\n{user_prompt}"

    fallback_models = ["gemini-3.8-flash", "gemini-2.0-flash", "gemini-2.5-flash"]
    for model_name in fallback_models:
        try:
            res = ai_client.models.generate_content(model=model_name, contents=full_prompt)
            if res and res.text:
                return res.text.strip()
        except Exception:
            time.sleep(1)
            continue
    raise RuntimeError("Все модели Gemini сейчас перегружены, попробуйте еще раз.")

# Озвучка gTTS
def generate_audio_sync(text: str, filename: str):
    tts = gTTS(text=text, lang='ru', slow=False)
    tts.save(filename)

# 8. Шаг 4: Генерация и отправка аудио
@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_generate(message: Message, state: FSMContext):
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
        # 1. Текст
        story_text = await asyncio.to_thread(generate_story_sync, child, hero, theme)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")
        
        try:
            await status_msg.edit_text("🎙 Записываю сказку голосом...")
        except Exception:
            pass

        # 2. Озвучка
        try:
            await asyncio.to_thread(generate_audio_sync, story_text, audio_path)

            voice_file = FSInputFile(audio_path)
            await message.answer_voice(
                voice=voice_file, 
                caption=f"🎧 Сказка про {hero} готова!\nСладких снов малышу ✨"
            )
            
            try:
                await status_msg.delete()
            except Exception:
                pass

            new_btn = ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="/start 🪄 Создать новую сказку")]],
                resize_keyboard=True
            )
            await message.answer("Хотите сочинить еще одну сказку?", reply_markup=new_btn)

        except Exception as tts_err:
            logging.error(f"Ошибка озвучки gTTS: {tts_err}")
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

# 9. Сервер для Render
async def handle_ping(request):
    return web.Response(text="OK")

async def start_background_tasks(app):
    # Запускаем поллинг бота как фоновую задачу сервера
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
