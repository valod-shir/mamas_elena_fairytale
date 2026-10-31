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
        "👋 **Добро пожаловать в мир добрых сказок!**\n\n"
        "Я помогу создать персональную терапевтическую аудиосказку для вашего малыша.\n\n"
        "👶 **Шаг 1 из 4:** Напишите **имя и возраст** ребенка.\n"
        "*(Например: Влада, полтора года)*"
    )
    await message.answer(welcome_text, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    await state.set_state(StoryCreation.waiting_for_child_info)

# 5. Шаг 1: Получаем имя и возраст
@dp.message(StoryCreation.waiting_for_child_info, F.text)
async def process_child_info(message: Message, state: FSMContext):
    await state.update_data(child_info=message.text.strip())
    
    await message.answer(
        "🧸 **Шаг 2 из 4:** Кто любимый герой, персонаж или игрушка малыша?\n"
        "*(Например: плюшевый котенок Мау, машинка Молния, зайчик)*",
        parse_mode="Markdown"
    )
    await state.set_state(StoryCreation.waiting_for_hero)

# 6. Шаг 2: Получаем любимого героя
@dp.message(StoryCreation.waiting_for_hero, F.text)
async def process_hero(message: Message, state: FSMContext):
    await state.update_data(hero=message.text.strip())
    
    await message.answer(
        "🎯 **Шаг 3 из 4:** Выберите тему сказки или напишите свою:",
        reply_markup=theme_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_theme)

# 7. Шаг 3: Получаем тему сказки
@dp.message(StoryCreation.waiting_for_theme, F.text)
async def process_theme(message: Message, state: FSMContext):
    theme_text = message.text.strip()
    if theme_text == "✍️ Написать свою тему":
        await message.answer("Напишите кратко, о чем должна быть сказка или какую привычку нужно привить:")
        return  # Оставляем в том же состоянии, чтобы пользователь ввел текст

    await state.update_data(theme=theme_text)
    
    await message.answer(
        "🎙 **Шаг 4 из 4:** Каким голосом озвучить сказку?",
        reply_markup=voice_keyboard
    )
    await state.set_state(StoryCreation.waiting_for_voice)

# Вспомогательная синхронная генерация через Gemini
def generate_story_sync(child_info: str, hero: str, theme: str) -> str:
    system_instruction = (
        "Ты профессиональный детский писатель и психолог. "
        "Сочини короткую, нежную, терапевтическую сказку для малыша. "
        "Сказка должна учить полезному навыку или мягко настраивать на сон. "
        "Язык должен быть ласковым, предложения понятными для ребенка, объем 140-190 слов. "
        "Пиши чистым текстом БЕЗ спецсимволов markdown (*, #, _), чтобы диктор читал ровно."
    )
    user_prompt = (
        f"Ребенок: {child_info}\n"
        f"Любимый персонаж/игрушка: {hero}\n"
        f"Тема сказки / ситуация: {theme}"
    )
    
    response = ai_client.models.generate_content(
        model="gemini-3.8-flash",
        contents=f"{system_instruction}\n\n{user_prompt}"
    )
    return response.text.strip()

# 8. Шаг 4: Выбор голоса и генерация
@dp.message(StoryCreation.waiting_for_voice, F.text)
async def process_voice_and_generate(message: Message, state: FSMContext):
    voice_choice = message.text.strip()
    # По умолчанию Светлана, если выбран Дмитрий — ставим Дмитрия
    selected_voice = "ru-RU-DmitryNeural" if "Дмитрий" in voice_choice else "ru-RU-SvetlanaNeural"

    data = await state.get_data()
    await state.clear()

    status_msg = await message.answer(
        "✨ Волшебство начинается! Сочиняю сказку...", 
        reply_markup=ReplyKeyboardRemove()
    )
    audio_path = f"story_{message.chat.id}_{message.message_id}.mp3"

    try:
        # Генерация текста через Gemini
        story_text = await asyncio.to_thread(
            generate_story_sync, 
            data["child_info"], 
            data["hero"], 
            data["theme"]
        
