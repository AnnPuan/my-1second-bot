import asyncio
import logging
import os
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Настройки ===
TOKEN = "8263273605:AAEPrWUFdp0QnXXKVDR8mgFFX-4g0ihJH94"  # твой токен
TIMEZONE = "Europe/Berlin"  # GMT+1 / GMT+2
VIDEO_FOLDER = Path("videos")
VIDEO_FOLDER.mkdir(exist_ok=True)

bot = Bot(token=TOKEN, parse_mode="HTML")
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# === FSM ===
class States(StatesGroup):
    wait_today = State()
    wait_replace = State()
    wait_missed = State()

# === Вспомогательные функции ===
def user_path(user_id: int) -> Path:
    path = VIDEO_FOLDER / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path

def video_path(user_id: int, dt: date) -> Path:
    return user_path(user_id) / f"{dt.isoformat()}.mp4"

async def has_video_today(user_id: int) -> bool:
    return video_path(user_id, date.today()).exists()

async def missed_days_this_month(user_id: int):
    today = date.today()
    first = today.replace(day=1)
    missed = []
    cur = first
    while cur <= today:
        if not video_path(user_id, cur).exists():
            missed.append(cur)
        cur += cur.replace(day=1) + timedelta(days=32)
        cur = cur.replace(day=1) - timedelta(days=1)  # следующий месяц - 1 день
        cur = cur.replace(day=1)
    return [d for d in missed if d <= today]

async def main_menu(user_id: int):
    builder = InlineKeyboardBuilder()
    today_has = await has_video_today(user_id)

    if not today_has:
        builder.button(text="Отправить сегодняшнее видео", callback_data="upload_today")
    else:
        builder.button(text="Заменить сегодняшнее видео", callback_data="replace_today")

    if await missed_days_this_month(user_id):
        builder.button(text="Заполнить пропуски", callback_data="fill_misses")

    builder.button(text="Мой прогресс", callback_data="progress")
    builder.adjust(1)
    return builder.as_markup()

# === Хендлеры ===
@dp.message(CommandStart())
async def start(message: Message):
    text = (
        "<b>Привет!</b>\n\n"
        "Я бот «1 секунда в день»\n"
        "Каждый день присылай мне 1-секундное видео своей жизни — "
        "в конце месяца я соберу из них крутой монтаж и пришлю тебе!\n\n"
        "Готов начать?"
    )
    await message.answer(text, reply_markup=await main_menu(message.from_user.id))

@dp.callback_query(F.data == "upload_today")
async def upload_today(cb: CallbackQuery, state: FSMContext):
    await state.set_state(States.wait_today)
    await cb.message.edit_text(f"Пришли видео за <b>{date.today():%d.%m.%Y}</b>", reply_markup=None)

@dp.callback_query(F.data == "replace_today")
async def replace_today(cb: CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, хочу", callback_data="confirm_replace")
    builder.button(text="Нет, не хочу", callback_data="cancel")
    await cb.message.edit_text(
        "Кажется ты загрузил(-а) не то видео или есть момент поярче.\nХочешь заменить сегодняшнее видео?",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "confirm_replace")
async def confirm_replace(cb: CallbackQuery, state: FSMContext):
    await state.set_state(States.wait_replace)
    video_path(cb.from_user.id, date.today()).unlink(missing_ok=True)
    await cb.message.edit_text(f"Пришли новое видео за <b>{date.today():%d.%m.%Y}</b>", reply_markup=None)

@dp.callback_query(F.data == "cancel")
async def cancel(cb: CallbackQuery):
    await cb.message.edit_text("Хорошо, оставляем как есть!", reply_markup=await main_menu(cb.from_user.id))

@dp.callback_query(F.data == "fill_misses")
async def fill_misses(cb: CallbackQuery, state: FSMContext):
    missed = await missed_days_this_month(cb.from_user.id)
    if not missed:
        await cb.answer("Пропусков нет!", show_alert=True)
        return

    day = missed[0]  # самый старый пропуск
    await state.set_state(States.wait_missed)
    await state.set_data({"missed_date": day.isoformat()})

    builder = InlineKeyboardBuilder()
    builder.button(text="Да", callback_data="yes_missed")
    builder.button(text="Нет", callback_data="no_missed")
    await cb.message.edit_text(
        f"У тебя есть пропущенный день <b>{day:%d.%m.%Y}</b>\nГотов загрузить видео прямо сейчас?",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "yes_missed")
async def yes_missed(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    day = date.fromisoformat(data["missed_date"])
    await cb.message.edit_text(f"Отлично! Пришли видео за <b>{day:%d.%m.%Y}</b>", reply_markup=None)

@dp.callback_query(F.data == "no_missed")
async def no_missed(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.delete()
    await bot.send_message(
        cb.from_user.id,
        "Что делать, если ты пропустил(-а) день?\n\n"
        "Не переживай — это нормально!\n"
        "Можно снять видео задним числом или выбрать любой яркий момент за этот день из галереи.\n"
        "Главное — сохранить привычку!\n\n"
        "Я напомню тебе через 5 минут"
    )
    # напоминание через 5 минут
    asyncio.create_task(remind_later(cb.from_user.id))

async def remind_later(user_id: int):
    await asyncio.sleep(300)
    missed = await missed_days_this_month(user_id)
    if missed:
        day = missed[0]
        await bot.send_message(
            user_id,
            f"Загрузи пропущенную 1 секунду за <b>{day:%d.%m.%Y}</b>, когда видео будет готово",
            reply_markup=await main_menu(user_id)
        )

# === Сохранение видео ===
@dp.message((F.video | F.video_note) & (States.wait_today | States.wait_replace | States.wait_missed))
async def save_video(message: Message, state: FSMContext):
    video = message.video or message.video_note
    file = await bot.get_file(video.file_id)
    user_id = message.from_user.id

    if await state.get_state() == States.wait_missed.state:
        data = await state.get_data()
        save_date = date.fromisoformat(data["missed_date"])
    else:
        save_date = date.today()

    save_path = video_path(user_id, save_date)
    await bot.download_file(file.file_path, save_path)

    await message.answer(
        f"Отлично! Видео за <b>{save_date:%d.%m.%Y}</b> сохранено! До завтра!",
        reply_markup=await main_menu(user_id)
    )
    await state.clear()

@dp.callback_query(F.data == "progress")
async def progress(cb: CallbackQuery):
    missed = await missed_days_this_month(cb.from_user.id)
    total = date.today().day
    done = total - len(missed)
    text = f"<b>Прогресс за {date.today():%B %Y}</b>\n\n{done} из {total} дней ✓\n"
    if missed:
        text += f"Пропущено дней: {len(missed)}"
    else:
        text += "Пропусков нет — ты молодец!"
    await cb.message.edit_text(text, reply_markup=await main_menu(cb.from_user.id))

# === Напоминания ===
async def reminder_12():
    for user_dir in VIDEO_FOLDER.iterdir():
        if user_dir.is_dir():
            user_id = int(user_dir.name)
            if not await has_video_today(user_id):
                await bot.send_message(user_id, "Жду твою 1 секунду и желаю тебе хорошего дня! Отправь его, как только видео будет снято")

async def reminder_18():
    for user_dir in VIDEO_FOLDER.iterdir():
        if user_dir.is_dir():
            user_id = int(user_dir.name)
            if not await has_video_today(user_id):
                await bot.send_message(user_id, "Жду твою 1 секунду. Отправь его, как только видео будет снято")

async def reminder_23():
    for user_dir in VIDEO_FOLDER.iterdir():
        if user_dir.is_dir():
            user_id = int(user_dir.name)
            if not await has_video_today(user_id):
                await bot.send_message(user_id, "У тебя остался час, чтобы отправить 1 секунду. Отправь его, как только видео будет снято")

async def new_day():
    for user_dir in VIDEO_FOLDER.iterdir():
        if user_dir.is_dir():
            user_id = int(user_dir.name)
            await bot.send_message(user_id, "Новый день — новая секунда!", reply_markup=await main_menu(user_id))

# === Запуск ===
async def main():
    scheduler.add_job(reminder_12, "cron", hour=12, minute=0)
    scheduler.add_job(reminder_18, "cron", hour=18, minute=0)
    scheduler.add_job(reminder_23, "cron", hour=23, minute=0)
    scheduler.add_job(new_day, "cron", hour=0, minute=5)
    scheduler.start()

    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
