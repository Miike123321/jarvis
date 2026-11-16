import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def get_required_env(name: str, *, cast_type=str):
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Не задана переменная окружения: {name}")
    if cast_type is int:
        return int(value)
    return value


TELEGRAM_TOKEN = get_required_env("TELEGRAM_TOKEN")
SHEET_NAME = get_required_env("SHEET_NAME")
SHEETS_API_KEY = get_required_env("SHEETS_API_KEY")
SOURCE_GROUP_ID = get_required_env("SOURCE_GROUP_ID", cast_type=int)

PARTNER_CHAT_MAP = {
    "ФОП БОГОМОЛОВА": get_required_env("PARTNER_A_GROUP_ID", cast_type=int),
    "ФОП ТОЛПАЗАН": get_required_env("PARTNER_B_GROUP_ID", cast_type=int),
}

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Локальный кеш базы данных аквабоксов: { "BOX-123": "ФОП БОГОМОЛОВА" }
box_cache = {}


def update_cache() -> bool:
    """Загрузка данных из Google Sheets в память"""
    global box_cache

    try:
        gc = gspread.api_key(SHEETS_API_KEY)
        sheet = gc.open_by_url(SHEET_NAME).sheet1
        data = sheet.get_all_records()

        new_cache = {}
        for row in data:
            box_id = str(row.get("ID DMS", "")).strip()
            partner = str(row.get("Partner", "")).strip()
            if box_id and partner:
                new_cache[box_id] = partner
        box_cache = new_cache
        logger.info(f"Кеш обновлен. Загружено боксов: {len(box_cache)}")
        return True
    except Exception as e:
        logger.error(f"Ошибка при обновлении кеша из Google Sheets: {e}")
        return False


async def periodic_cache_update():
    """Фоновое обновление кеша каждый час"""
    while True:
        await asyncio.sleep(3600)
        update_cache()


@dp.message(F.chat.id == SOURCE_GROUP_ID, Command("cash_update"))
async def handle_cash_update(message: Message):
    if update_cache():
        await message.reply(f"✅ Кеш обновлён. Загружено записей: {len(box_cache)}.")
    else:
        await message.reply(
            "⚠️ Не удалось обновить кеш из Google Таблицы. Используются предыдущие данные."
        )


@dp.message(F.chat.id == SOURCE_GROUP_ID)
async def handle_complaint(message: Message):
    text = message.text or message.caption or ""

    if not text:
        return

    # Поиск ID в первой строке сообщения
    match = re.search(
        r"^\s*(?:id\s*[:#]?\s*)?([A-Za-z0-9][A-Za-z0-9-]*)",
        text.split("\n")[0],
        re.IGNORECASE,
    )
    if not match:
        await message.reply("⚠️ Ошибка: не удалось распознать ID аквабокса в первой строке.")
        return

    box_id = match.group(1)
    partner = box_cache.get(box_id)

    if not partner or partner not in PARTNER_CHAT_MAP:
        await message.reply(f"⚠️ Ошибка: Аквабокс {box_id} не найден в базе данных партнеров.")
        return

    target_chat_id = PARTNER_CHAT_MAP[partner]

    try:
        await bot.forward_message(
            chat_id=target_chat_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        logger.info(
            f"Сообщение по боксу {box_id} успешно переслано партнеру {partner} (chat_id: {target_chat_id})"
        )
        await message.reply(
            f"✅ Принято. Заявка по аквабоксу {box_id} передана партнёру {partner}."
        )
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения: {e}")
        await message.reply(f"❌ Ошибка при пересылке сообщения: {e}")


async def main():
    update_cache()
    asyncio.create_task(periodic_cache_update())
    logger.info("Бот запущен и готов к работе...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as exc:
        logger.error(f"Ошибка запуска: {exc}")
        raise