import os
import sqlite3
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, 
    CallbackQuery,
    FSInputFile, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
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

ai_client = None
if GEMINI_KEY:
    try:
        ai_client = genai.Client(
            api_key=GEMINI_KEY,
            http_options=types.HttpOptions(timeout=10000)
        )
    except Exception as e:
        logging.error(f"Ошибка инициализации Gemini: {e}")

# 2. База данных SQLite для учета пользователей и баланса
DB_PATH = "users.db"
FREE_START_STORIES = 2

# Пакеты сказок: код_тарифа -> (количество_сказок, цена_в_звездах, название)
TARIFFS = {
    "pack_5": (5, 75, "Пакет: 5 добрых сказок"),
    "pack_15": (15, 150, "Пакет: 15 добрых сказок"),
    "pack_30": (30, 250, "Пакет: 30 сказок на месяц")
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            stories_left INTEGER DEFAULT 2,
            total_generated INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def get_user_balance(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO users (user_id, stories_left, total_generated) VALUES (?, ?, 0)",
            (user_id, FREE_START_STORIES)
        )
        conn.commit()
        balance = FREE_START_STORIES
    else:
        balance = row[0]
    conn.close()
    return balance

def add_stories_to_user(user_id: int, count: int) -> int:
    # Убеждаемся, что пользователь есть в базе
    _ = get_user_balance(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET stories_left = stories_left + ? WHERE user_id = ?",
        (count, user_id)
    )
    conn.commit()
    c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else count

def use_one_story(user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "UPDATE users SET stories_left = MAX(0, stories_left - 1), total_generated = total_generated + 1 WHERE user_id = ?",
        (user_id,)
    )
    conn.commit()
    c.execute("SELECT stories_left FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_user_stats(user_id: int):
    balance = get_user_balance(user_id)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT total_generated FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    total = row[0] if row else 0
    return balance, total

# 3. Состояния FSM
class StoryCreation(StatesGroup):
    waiting_for_child_info = State()
    waiting_for_hero = State()
    waiting_for_theme = State()

# 4. Клавиатуры
main_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🪄 Создать новую сказку")],
        [KeyboardButton(text="👤 Мой баланс"), KeyboardButton(text="💎 Купить сказки")]
    ],
    resize_keyboard=True
)

theme_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="😴 На крепкий сон"), KeyboardButton(text="🥣 Хороший аппетит")],
        [KeyboardButton(text="🧸 Делиться игрушками"), KeyboardButton(text="🦷 Чистить зубки")],
        [KeyboardButton(text="✍️ Своя тема")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

def get_tariffs_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌟 5 сказок — 75 ⭐️", callback_data="buy_pack_5")],
            [InlineKeyboardButton(text="🌟 15 сказок — 150 ⭐️ (Выгодно)", callback_data="buy_pack_15")],
            [InlineKeyboardButton(text="🌟 30 сказок — 250 ⭐️ (На месяц)", callback_data="buy_pack_30")]
        ]
    )

# 5. Профиль, тарифы и оплата Telegram Stars
@dp.message(F.text == "👤 Мой баланс")
async def show_balance(message: Message):
    balance, total = get_user_stats(message.from_user.id)
    text = (
        f"👤 Ваш профиль:\n\n"
        f"🎧 Доступно сказок: {balance} шт.\n"
        f"📚 Всего создано сказок: {total} шт.\n\n"
        f"Выберите действие в меню ниже:"
    )
    await message.answer(text, reply_markup=main_menu_keyboard)

@dp.message(F.text == "💎 Купить сказки")
async def show_tariffs(message: Message):
    balance = get_user_balance(message.from_user.id)
    text = (
        f"💎 Пополнение баланса сказок\n"
        f"Сейчас у вас доступно: {balance} шт.\n\n"
        f"Выберите подходящий пакет для мгновенной оплаты Звёздами Telegram (⭐️):"
    )
    await message.answer(text, reply_markup=get_tariffs_inline_kb())

# Секретная команда для администратора/тестирования (начисляет 5 бесплатных сказок)
@dp.message(Command("add_free"))
async def add_free_cmd(message: Message):
    new_bal = add_stories_to_user(message.from_user.id, 5)
    await message.answer(f"🎁 Тестовый бонус начислен! Ваш баланс: {new_bal} сказок.", reply_markup=main_menu_keyboard)

# Нажатие на кнопку покупки пакета -> отправка счета в Stars
@dp.callback_query(F.data.startswith("buy_"))
async def send_stars_invoice(callback: CallbackQuery):
    pack_key = callback.data.replace("buy_", "")
    if pack_key not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    stories_count, stars_price, title = TARIFFS[pack_key]
    await callback.answer()

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=f"Пополнение баланса на {stories_count} персональных аудиосказок для малыша.",
        payload=f"stars_{stories_count}",
        provider_token="",  # Для Telegram Stars токен всегда пустой!
        currency="XTR",     # XTR — официальный код Telegram Stars
        prices=[LabeledPrice(label=title, amount=stars_price)]
    )

# Обязательное подтверждение готовности принять платеж (Pre-Checkout)
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

# Успешная оплата -> начисление сказок в БД
@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        stories_added = int(payload.replace("stars_", ""))
    except Exception:
        stories_added = 5

    new_balance = add_stories_to_user(message.from_user.id, stories_added)
    await message.answer(
        f"🎉 Оплата прошла успешно!\n\n"
        f"На ваш баланс зачислено: +{stories_added} сказок.\n"
        f"🎧 Всего доступно: {new_balance} шт.\n\n"
        f"Нажмите «🪄 Создать новую сказку», чтобы начать!",
        reply_markup=main_menu_keyboard
    )

# 6. Старт и начало создания сказки
@dp.message(CommandStart())
@dp.message(F.text.startswith("/start"))
@dp.message(F.text == "🪄 Создать новую сказку")
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    balance = get_user_balance(user_id)

    if balance <= 0:
        no_balance_text = (
            "🌙 Ваши сказки на балансе закончились!\n\n"
            "Чтобы продолжить создавать персональные терапевтические аудиосказки для малыша, "
            "выберите пакет пополнения ниже:"
        )
        await message.answer(no_balance_text, reply_markup=get_tariffs_inline_kb())
        return

    welcome_text = (
        f"👋 Добро пожаловать в мир добрых сказок!\n"
        f"🎁 Доступно сказок на балансе: {balance} шт.\n\n"
        f"👶 Шаг 1 из 3: Напишите имя и возраст ребенка:\n"
        f"(Например: Влада, 2 года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 7. Шаг 1: Имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 3: Кто любимый персонаж или игрушка малыша?\n"
        "(Например: котенок , зайчик, машинка)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 8. Шаг 2: Персонаж
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 3: Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 9. Быстрая генерация текста Gemini
async def generate_story_fast(child: str, hero: str, theme: str) -> str:
    if not ai_client:
        raise ValueError("Ключ GEMINI_API_KEY не задан")

    prompt = (
        f"Сочини короткую, ласковую терапевтическую сказку на ночь для малыша.\n"
        f"Ребенок: {child}. Любимый герой: {hero}. Тема: {theme}.\n"
        f"Объем строго 65-85 слов. Простые предложения, чистый текст без звездочек и спецсимволов."
    )

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.8-pro"
    ]

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
                err_str = str(e)
                if "404" in err_str or "NOT_FOUND" in err_str:
                    break
                await asyncio.sleep(1.0)

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

    raise RuntimeError("Сервер нейросети сейчас перегружен. Пожалуйста, попробуйте через минуту.")

# 10. Озвучка
async def generate_audio_fast(text: str, file_path: str):
    try:
        communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural")
        await asyncio.wait_for(communicate.save(file_path), timeout=3.5)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 500:
            return
    except Exception as e:
        logging.warning(f"Переключение на gTTS: {e}")

    def run_gtts():
        tts = gTTS(text=text, lang='ru', slow=False)
        tts.save(file_path)

    await asyncio.to_thread(run_gtts)

# 11. Шаг 3: Генерация, отправка и списание с баланса
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme_and_finish(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Своя тема":
        await message.answer("Напишите кратко, о чем должна быть сказка:")
        return

    data = await state.get_data()
    await state.clear()

    user_id = message.from_user.id
    if get_user_balance(user_id) <= 0:
        await message.answer(
            "🌙 На вашем балансе закончились сказки! Пополните баланс:", 
            reply_markup=get_tariffs_inline_kb()
        )
        return

    child = data.get("child_info", "малыша")
    hero = data.get("hero", "героя")

    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю и озвучиваю сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # 1. Текст
        story_text = await generate_story_fast(child, hero, theme_text)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")

        try:
            await status_msg.edit_text("🎙 Записываю аудиосказку...")
        except Exception:
            pass

        # 2. Звук
        await generate_audio_fast(story_text, audio_path)

        voice_file = FSInputFile(audio_path)
        remaining = use_one_story(user_id)

        await message.answer_voice(
            voice=voice_file, 
            caption=f"🎧 Аудиосказка про {hero} готова!\nСладких снов малышу ✨\n\n🎁 Осталось сказок на балансе: {remaining} шт."
        )

        try:
            await status_msg.delete()
        except Exception:
            pass

        await message.answer("Выберите действие в меню ниже:", reply_markup=main_menu_keyboard)

    except Exception as e:
        logging.error(f"Ошибка создания сказки: {e}")
