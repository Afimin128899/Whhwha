#!/usr/bin/env bash
# Run from the repository: bash install.sh [--start]
set -euo pipefail
case "${1:-}" in
  ""|--start) ;;
  -h|--help)
    echo "Использование: bash install.sh [--start]"
    echo "Установка зависимостей и настройка .env; --start также запускает бота."
    exit 0 ;;
  *) echo "Неизвестный параметр. Используй bash install.sh --help" >&2; exit 2 ;;
esac
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null || {
  echo "Установи Python: sudo apt-get update && sudo apt-get install -y python3 python3-venv" >&2
  exit 1
}
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || {
    echo "Для Ubuntu/Debian: sudo apt-get install -y python3-venv; затем повтори запуск." >&2
    exit 1
  }
fi
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python - <<'PY'
import getpass
import os
from pathlib import Path
import re
import tempfile

from dotenv import dotenv_values, set_key

target = Path(".env")
if target.is_symlink():
    raise SystemExit("Файл .env не должен быть символической ссылкой.")
existing = dotenv_values(target) if target.exists() else {}
print("Настройка .env. Enter сохраняет существующее значение.")
print("Токен получи в @BotFather; API_ID/API_HASH — на my.telegram.org.")
values = {}
def ask(key, prompt, valid, secret=False):
    current = existing.get(key) or ""
    while True:
        suffix = " [сохранить текущее]" if current else ""
        value = (getpass.getpass(prompt + suffix + ": ") if secret
                 else input(prompt + suffix + ": ")).strip() or current
        if valid(value):
            return value
        print("Некорректное значение, попробуй ещё раз.")

try:
    values["BOT_TOKEN"] = ask("BOT_TOKEN", "BOT_TOKEN", lambda v: bool(
        re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", v)), True)
    values["ADMIN_ID"] = ask("ADMIN_ID", "Числовой Telegram ID администратора",
                             lambda v: v.isascii() and v.isdecimal() and int(v) > 0)
    values["API_ID"] = ask("API_ID", "API_ID",
                           lambda v: v.isascii() and v.isdecimal() and int(v) > 0)
    values["API_HASH"] = ask("API_HASH", "API_HASH",
                             lambda v: bool(re.fullmatch(r"[A-Fa-f0-9]{32}", v)), True)
except (EOFError, KeyboardInterrupt):
    raise SystemExit("\nНастройка отменена. Файл .env не изменён.")

# Keep unrelated settings; replace the file atomically with owner-only permissions.
fd, name = tempfile.mkstemp(prefix=".env-install-", dir=".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if target.exists():
            stream.write(target.read_text(encoding="utf-8"))
    for key, value in values.items():
        set_key(name, key, value)
    os.chmod(name, 0o600)
    os.replace(name, target)
finally:
    if os.path.exists(name):
        os.unlink(name)
print(".env сохранён с правами 600.")
print("Формат токена проверен; его действительность проверит Telegram при запуске.")
PY
if [[ "${1:-}" == "--start" ]]; then
  # Explicit .env values take precedence over stale exports in the caller's shell.
  exec env -u BOT_TOKEN -u ADMIN_ID -u API_ID -u API_HASH .venv/bin/python main.py
fi
echo "Готово. Запуск: env -u BOT_TOKEN -u ADMIN_ID -u API_ID -u API_HASH .venv/bin/python main.py"
