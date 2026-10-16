import os
import random
from pathlib import Path
from typing import List, Tuple
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "0").strip()
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip().lstrip("@").lower()

# Парсинг пула API_ID и API_HASH
api_ids_raw = os.getenv("API_IDS", "").strip()
api_hashes_raw = os.getenv("API_HASHES", "").strip()

API_POOLS: List[Tuple[int, str]] = []

if api_ids_raw and api_hashes_raw:
    ids = [i.strip() for i in api_ids_raw.split(",") if i.strip()]
    hashes = [h.strip() for h in api_hashes_raw.split(",") if h.strip()]
    for aid, ahash in zip(ids, hashes):
        try:
            API_POOLS.append((int(aid), ahash))
        except ValueError:
            pass

# Если задан одиночный API_ID/API_HASH в старом формате
single_api_id = os.getenv("API_ID", "").strip()
single_api_hash = os.getenv("API_HASH", "").strip()
if single_api_id and single_api_hash:
    try:
        pair = (int(single_api_id), single_api_hash)
        if pair not in API_POOLS:
            API_POOLS.append(pair)
    except ValueError:
        pass

def get_random_api_credentials() -> Tuple[int, str]:
    """Возвращает случайную пару (api_id, api_hash) из пула."""
    if API_POOLS:
        return random.choice(API_POOLS)
    return 0, ""

DB_PATH = BASE_DIR / os.getenv("DB_PATH", "bot_database.sqlite3")
SESSIONS_DIR = BASE_DIR / os.getenv("SESSIONS_DIR", "sessions")

# Создаем папку для сессий, если её еще нет
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
