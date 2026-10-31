import os
import sys
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
import edge_tts
from gtts import gTTS

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# 1. Читаем ключи окружения
raw_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
raw_gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""

BOT_TOKEN = raw_bot_token.strip().strip("'\"")
GEMINI_KEY = raw_gemini_key.strip().strip("'\"")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 2. База данных SQLite в надежной директории
DB_PATH = "/tmp/users.db" if os.path.exists("/tmp") else "users.db"
FREE_START_STORIES = 2

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

# 5. Профиль и оплата Stars
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
        f"Выберите пакет для мгновенной оплаты Звёздами Telegram (⭐️):"
    )
    await message.answer(text, reply_markup=get_tariffs_inline_kb())

@dp.message(Command("add_free"))
async def add_free_cmd(message: Message):
    new_bal = add_stories_to_user(message.from_user.id, 5)
    await message.answer(f"🎁 Бонус начислен! Ваш баланс: {new_bal} сказок.", reply_markup=main_menu_keyboard)

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
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars_price)]
    )

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

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

# 6. Старт создания сказки
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
            "Чтобы продолжить создавать персональные аудиосказки для малыша, "
            "выберите пакет пополнения ниже:"
        )
        await message.answer(no_balance_text, reply_markup=get_tariffs_inline_kb())
        return

    welcome_text = (
        f"👋 Добро пожаловать в мир добрых сказок!\n"
        f"🎁 Доступно сказок на балансе: {balance} шт.\n\n"
        f"👶 Шаг 1 из 3: Напишите имя и возраст ребенка:\n"
        f"(Например: Влада, полтора года)"
    )
    await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    await message.answer(
        "🧸 Шаг 2 из 3: Кто любимый персонаж или игрушка малыша?\n"
        "(Например: котенок Мау, зайчик, машинка)"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    await message.answer(
        "🎯 Шаг 3 из 3: Выберите тему сказки:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Безотказная генерация без конфликта HttpOptions
async def generate_story_text(child: str, hero: str, theme: str) -> str:
    prompt = (
        f"Сочини очень мягкую, успокаивающую сказку на ночь для малыша.\n"
        f"Ребенок: {child}. Любимый герой: {hero}. Тема: {theme}.\n"
        f"Используй плавные переходы, тире и точки, чтобы текст звучал мелодично и неторопливо.\n"
        f"Объем строго 65-85 слов. Только чистый текст без звездочек и спецсимволов."
    )

    # Собираем все ключи (если добавите запасные в Render)
    keys = []
    for k_name in ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"]:
        val = os.environ.get(k_name, "").strip().strip("'\"")
        if val:
            keys.append(val)
    if not keys and GEMINI_KEY:
        keys = [GEMINI_KEY]

    # Каскад моделей: от самых быстрых и стабильных к резервным
    models = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-3.8-flash"]

    for current_key in keys:
        # Инициализируем клиент БЕЗ http_options, которые вызывали 504 DEADLINE_EXCEEDED
        client = genai.Client(api_key=current_key)
        
        for model_name in models:
            try:
                # Ограничиваем ожидание 15 секундами средствами самого Python
                res = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt
                    ),
                    timeout=15.0
                )
                if res and res.text:
                    return res.text.replace("*", "").replace("#", "").strip()
            except Exception as e:
                logging.warning(f"Модель {model_name} пропустила ход: {e}")
                await asyncio.sleep(1.0)

    raise RuntimeError("Все бесплатные линии сейчас заняты. Пожалуйста, нажмите кнопку создания сказки еще раз через минуту.")

# 8. Живая озвучка
async def generate_audio_natural(text: str, file_path: str):
    try:
        communicate = edge_tts.Communicate(text, "ru-RU-SvetlanaNeural", rate="-10%")
        await asyncio.wait_for(communicate.save(file_path), timeout=6.0)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 500:
            return
    except Exception as e:
        logging.warning(f"edge-tts таймаут, используем плавный gTTS: {e}")

    def run_gtts():
        tts = gTTS(text=text, lang='ru', slow=True)
        tts.save(file_path)

    await asyncio.to_thread(run_gtts)

# 9. Шаг 3: Финал, списание и отправка
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
        "✨ Волшебство начинается! Рассказываю сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    msg_id = message.message_id.real if hasattr(message.message_id, 'real') else message.message_id
    audio_path = f"/tmp/story_{message.chat.id}_{msg_id}.mp3" if os.path.exists("/tmp") else f"story_{message.chat.id}_{msg_id}.mp3"

    try:
        story_text = await generate_story_text(child, hero, theme_text)
        await message.answer(f"📖 Сказка для {child}\n\n{story_text}")

        try:
            await status_msg.edit_text("🎙 Записываю мягкий голос...")
        except Exception:
            pass

        await generate_audio_natural(story_text, audio_path)

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
        await message.answer(
            f"⚠️ {str(e)[:200]}\n(Попытка не списана с вашего баланса)", 
            reply_markup=main_menu_keyboard
        )
    finally:
        if os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except Exception:
                pass

# 10. Веб-сервер Render
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
    print(f"WEB SERVER STARTED ON PORT {port}", flush=True)

async def main():
    print("STARTING BOT SERVICE...", flush=True)
    await start_server()
    init_db()
    await asyncio.sleep(2)
    await bot.delete_webhook(drop_pending_updates=True)
    print("STARTING TELEGRAM POLLING...", flush=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
