import asyncio
import os
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.utils.token import TokenValidationError
import gspread_asyncio
from google.oauth2.service_account import Credentials

# --- Конфигурация ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

def load_dotenv():
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "7434377872:AAG1YqKrucZyIHPerV_SNJ4gnpn_YvU46Mo")
SHEET_ID = os.getenv("SHEET_ID", "1Ld2PxCMxCgDTJcSsqv9-xhoqaJbOCgSXu53jg0MkF7g")
CREDS_FILE = os.getenv("CREDS_FILE", "credentials.json")

def load_config():
    token = BOT_TOKEN.strip() if isinstance(BOT_TOKEN, str) else ""
    sheet_id = SHEET_ID.strip() if isinstance(SHEET_ID, str) else ""
    creds_file = CREDS_FILE.strip() if isinstance(CREDS_FILE, str) else ""

    if not re.fullmatch(r"\d+:\w+", token):
        raise RuntimeError("BOT_TOKEN looks invalid. It should look like 123456789:ABCdefGhIJKlmNoPqrStUvWxYz123456789.")
    if len(sheet_id) < 10:
        raise RuntimeError("SHEET_ID looks invalid.")
    if not os.path.exists(creds_file):
        creds_hint = os.path.join(BASE_DIR, creds_file) if not os.path.isabs(creds_file) else creds_file
        raise RuntimeError(f"Google credentials file not found: {creds_hint}")

    return token, sheet_id, creds_file

try:
    BOT_TOKEN, SHEET_ID, CREDS_FILE = load_config()
except RuntimeError as exc:
    print(f"Configuration error: {exc}")
    raise SystemExit(1) from exc

try:
    bot = Bot(token=BOT_TOKEN)
except TokenValidationError as exc:
    print(f"Telegram token validation failed: {exc}")
    raise SystemExit(1) from exc

dp = Dispatcher()
message_queue = []  # Буфер для батчинга

# --- Настройка Google Sheets ---
def get_creds():
    creds = Credentials.from_service_account_file(CREDS_FILE)
    return creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ])

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

# --- Перехват сообщений ---
@dp.message()
async def catch_all(message: types.Message):
    text = message.text or message.caption or "[МЕДИА]"
    thread_id = message.message_thread_id or 0
    
    message_queue.append([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        message.chat.id,
        thread_id,
        message.from_user.id,
        message.from_user.full_name,
        text
    ])

# --- Фоновая задача: выгрузка в таблицу ---
async def flush_to_sheets():
    while True:
        await asyncio.sleep(60) 
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if not message_queue:
            print(f"[{current_time}] Статус: 0 сообщений. Бот работает.")
            continue
        
        batch = message_queue[:]
        message_queue.clear()
        
        try:
            agc = await agcm.authorize()
            sh = await agc.open_by_key(SHEET_ID)
            ws = await sh.worksheet("Logs")
            await ws.append_rows(batch)
            print(f"[{current_time}] Статус: успешно записано {len(batch)} строк(и) в таблицу.")
        except Exception as e:
            message_queue.extend(batch)
            print(f"[{current_time}] Ошибка выгрузки: {repr(e)}")

# --- Запуск ---
async def main():
    try:
        asyncio.create_task(flush_to_sheets())
        print("Бот запущен. Начат сбор сообщений...")
        await dp.start_polling(bot)
    except Exception as exc:
        print(f"Failed to start bot: {exc}")
        raise

if __name__ == "__main__":
    asyncio.run(main())