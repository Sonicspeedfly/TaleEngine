# Общий образ для backend и Telegram-бота: у них общий код (пакет backend) и общая БД.
# В docker-compose из этого образа поднимаются ДВА сервиса с разными командами.
FROM python:3.11-slim

WORKDIR /app

# Минимальные системные зависимости (задел на сборку аудио/медиа-библиотек).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — так слой с pip-кэшем переиспользуется между сборками.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Затем сам код обоих сервисов и статический веб-интерфейс.
COPY backend /app/backend
COPY telegram_bot /app/telegram_bot
COPY frontend /app/frontend

# Чтобы импорты `backend.*` и `telegram_bot.*` работали из корня.
ENV PYTHONPATH=/app

EXPOSE 8000

# Команда по умолчанию — веб-сервер. Бот переопределяет её в docker-compose.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
