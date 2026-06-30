#!/usr/bin/env bash
# ============================================================
#  AiChat SSF — запуск на Linux-сервере БЕЗ Docker.
#
#  Использование:
#     chmod +x start.sh          # один раз
#     ./start.sh                 # хост 0.0.0.0, порт 8000
#     HOST=0.0.0.0 PORT=8080 ./start.sh   # свой адрес/порт
#
#  Сервер слушает 0.0.0.0 -> доступен снаружи (не забудьте открыть порт в фаерволе).
#  Для продакшена удобнее systemd-сервис или Docker (см. README).
# ============================================================
set -e
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

# 1) .env из шаблона при первом запуске.
[ -f .env ] || { echo "[setup] Создаю .env из .env.example"; cp .env.example .env; }

# 2) Виртуальное окружение.
if [ ! -x .venv/bin/python ]; then
  echo "[setup] Создаю окружение .venv ..."
  python3 -m venv .venv
fi

# 3) Зависимости — один раз (маркер .venv/.installed).
if [ ! -f .venv/.installed ]; then
  echo "[setup] Ставлю зависимости ..."
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r backend/requirements.txt
  touch .venv/.installed
fi

# 4) Telegram-бот — в фоне, если задан токен.
if grep -Eq '^TELEGRAM_BOT_TOKEN=.' .env; then
  echo "[run] Запускаю Telegram-бота (в фоне)"
  .venv/bin/python -m telegram_bot.bot &
fi

# 5) Сервер (в основном процессе). exec -> сигналы (Ctrl+C) доходят до uvicorn.
echo "[run] Сервер: http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn backend.main:app --host "${HOST}" --port "${PORT}"
