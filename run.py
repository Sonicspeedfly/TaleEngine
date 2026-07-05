"""
Запуск сервера TaleEngine БЕЗ лимита на размер WebSocket-сообщения.

Обычный `uvicorn backend.main:app` ставит `ws_max_size = 16 МБ`: большое вложение
(например аудио 14 МБ → base64 ~19 МБ) в один WS-кадр не влезает и молча обрывается
(close 1009). Здесь лимит снят полностью (`ws_max_size=None` — без ограничения),
поэтому запускать сервер нужно ИМЕННО так:

    python run.py                      # 0.0.0.0:8000
    python run.py --host 0.0.0.0 --port 8042
    HOST=127.0.0.1 PORT=9000 python run.py

(Вложения и без того уходят по HTTP — см. /api/sessions/{id}/send, — но так лимит
не мешает и любым другим большим сообщениям по WebSocket.)
"""
import argparse
import os

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="TaleEngine server (no WebSocket size limit)")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        ws_max_size=None,  # без ЛИМИТА на размер кадра WebSocket
    )


if __name__ == "__main__":
    main()
