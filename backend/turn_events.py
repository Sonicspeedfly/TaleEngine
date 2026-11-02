"""
Шина уведомлений о ходах (in-process pub/sub).

Зачем: ход живёт в фоне (backend/generation.py), а вкладка, которая его начала,
могла уйти в другой чат, перезагрузиться или вовсе закрыться. Чтобы «Ответ
готов» доходил и туда, менеджер генерации публикует сюда начало и конец
каждого хода, а `GET /sse/events` (backend/main.py) раздаёт эти события всем
открытым вкладкам пользователя — с фильтром по доступу к чату.

События:
  {"type": "turn_started", "session_id", "job_id", "kind"}
  {"type": "turn_done", "session_id", "job_id", "kind", "ok", "error",
   "stopped", "message_id", "preview"}

Очереди подписчиков ограничены: вкладка, которая не читает (зависший браузер,
мёртвое соединение до таймаута), не должна копить события без конца. При
переполнении выбрасывается САМОЕ СТАРОЕ — свежий «готово» важнее давнего «начат».
"""
import asyncio
import logging

logger = logging.getLogger("aichat.turn_events")

QUEUE_MAXSIZE = 200

_subscribers: set[asyncio.Queue] = set()


def subscribe(maxsize: int = QUEUE_MAXSIZE) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)


def publish(event: dict) -> None:
    """
    Разослать событие всем подписчикам. Синхронно и без исключений наружу:
    зовётся из finally хода, где await и падения недопустимы.
    """
    for q in list(_subscribers):
        try:
            if q.full():
                q.get_nowait()
            q.put_nowait(dict(event))
        except Exception:  # noqa: BLE001 — один подписчик не ломает остальных
            logger.exception("Не удалось доставить событие хода подписчику")
