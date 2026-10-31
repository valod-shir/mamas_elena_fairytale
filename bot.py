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
from google.genai import types
import edge_tts
from gtts import gTTS

logging.basicConfig(level=logging.INFO)

# 1. Читаем ключи окружения
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'\"")
GEMINI_KEY = raw_gemini_key.strip().strip("'\"")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализируем клиент без долгих внутренних зависаний
ai_client = None
if GEMINI_KEY:
    try:
        ai_client = genai.Client(
            api_key=GEMINI_KEY,
            http_options=types.HttpOptions(timeout=10000) # 10 секунд максимум на запрос
        )
    except Exception as e:
        logging.error(f"Ошибка инициализации Gemini: {e}")

# 2. Состояния FSM
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
        [KeyboardButton(text="✍️ Своя тема")]
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

# 4. Команда /start (и кнопка создания новой сказки)
@dp.message(CommandStart())
@dp.message(F.text.startswith("/start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 Добро пожаловать в мир добрых сказок!\n\n"
        "Я помогу создать персональную терапевтическую аудиосказку для вашего малыша.\n\n"
        "👶 Шаг 1 из 4: Напишите имя и возраст ребенка:\n"
        "(Например: Влада, полтора года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 5. Шаг 1: Имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 4: Кто любимый персонаж или игрушка малыша?\n"
        "(Например: котенок Мау, зайчик, машинка)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 6. Шаг 2: Персонаж
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 4: Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Шаг 3: Тема
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    await state.update_data(theme=theme_text)
    await message.answer(
        "🎙 Шаг 4 из 4: Каким голосом озвучить сказку?",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# 8. Быстрая генерация текста с автоматическим обходом 503
async def generate_story_fast(child: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("Ключ GEMINI_API_KEY не задан")

    prompt = (
        f"Сочини короткую, ласковую терапевтическую сказку на ночь для малыша.\n"
        f"Ребенок: {child}. Любимый герой: {hero}. Тема: {theme}.\n"
        f"Объем строго 65-85 слов. Простые предложения, чистый текст без звездочек и спецсимволов."
    )

    # Сначала пробуем актуальные модели напрямую без долгих ожиданий
    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.8-pro"
    ]

    last_err = None
    for model_name in candidate_models:
        for attempt in range(2):
            try:
                res = await asyncio.to_thread(
                    ai_client.models.generate_content,
                    model=model_name,
                    contents=prompt
                )
                if res and res.text:
                    return res.text.replace("*", "").replace("#", "").strip()
            except Exception as e:
                last_err = e
                err_str = str(e)
                # Если модели нет (404), сразу переходим к следующей без повтора
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                await asyncio.sleep(1.0)

    # Если все стандартные имена дали сбой, автоматически ищем любую доступную flash модель в аккаунте
    try:
        available = await asyncio.to_thread(ai_client.models.list)
        for m in available:
            m_name = m.name.replace("models/", "")
            if "gemini" in m_name and ("flash" in m_name or "pro" in m_name):
                try:
                    res = await asyncio.to_thread(
                        ai_client.models.generate_content,
                        model=m_name,
                        contents=prompt
                    )
                    if res and res.text:
                        return res.text.replace("*", "").replace("#", "").strip()
                except Exception:
                    continue
    except Exception:
        pass

    raise RuntimeError(f"Сервер Google временно перегружен (503). Пожалуйста, попробуйте через минуту.")

# 9. Быстрая озвучка (Дмитрий / Светлана) без 12-секундных зависаний
async def generate_audio_fast(text: str, voice_str: str, file_path: str):
    is_male = ("Дмитрий" in voice_str or "папы" in voice_str)
    voice_name = "ru-RU-DmitryNeural" if is_male else "ru-RU-SvetlanaNeural"

    try:
        # Таймаут всего 4.5 секунды: если IP Render заблокирован Microsoft, не ждем зря
        communicate = edge_tts.Communicate(text, voice_name, rate="-5%")
        await asyncio.wait_for(communicate.save(file_path), timeout=4.5)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 500:
            return "edge"
    except Exception as e:
        logging.warning(f"Быстрое переключение с Edge-TTS на резервный синтезатор: {e}")

    # Мгновенный резерв через Google TTS
    def run_gtts():
        # Для папы включаем более медленный и размеренный темп чтения
        tts = gTTS(text=text, lang='ru', slow=is_male)
        tts.save(file_path)

    await asyncio.to_thread(run_gtts)
    return "gtts"

# 10. Шаг 4: Финальная сборка и отправка
@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_finish(message: Message, state: FSMContext):
    voice_choice = message.text.strip()
    data = await state.get_data()
    await state.clear()

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")
    theme = data.get("theme", "добрый сон")

    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю сказку (это займет ~10 секунд)...", 
        reply_markup=ReplyKeyboardRemove()
    )
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # 1. Генерируем текст
        story_text = await generate_story_fast(child, hero, theme)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")

        try:
            await status_msg.edit_text("🎙 Записываю голос...")
        except Exception:
            pass

        # 2. Генерируем звук
        engine_used = await generate_audio_fast(story_text, voice_choice, audio_path)

        voice_file = FSInputFile(audio_path)
        is_male = ("Дмитрий" in voice_choice or "папы" in voice_choice)
        
        if engine_used == "edge":
            voice_label = "голос папы Дмитрия" if is_male else "голос мамы Светланы"
        else:
            voice_label = "спокойный темп" if is_male else "нежный голос"

        await message.answer_voice(
            voice=voice_file, 
            caption=f"🎧 Сказка про {hero} ({voice_label})\nСладких снов малышу ✨"
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        next_btn = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/start 🪄 Создать новую сказку")]],
            resize_keyboard=True
        )
        await message.answer("Хотите сочинить еще одну сказку?", reply_markup=next_btn)

    except Exception as e:
        logging.error(f"Ошибка создания сказки: {e}")
        retry_btn = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/start 🔄 Попробовать снова")]],
            resize_keyboard=True
        )
        await message.answer(f"⚠️ {str(e)[:200]}", reply_markup=retry_btn)
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# 11. Веб-сервер Render для мгновенного открытия порта
async def handle_ping(request):
    return web.Response(text="OK")

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
    # Сначала мгновенно открываем порт для Render
    await start_server()
    # Даем 2 секунды старому контейнеру завершиться и сбрасываем зависшие запросы
    await asyncio.sleep(2)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
