"""
Менеджер генерации. Решает ключевое требование ТЗ:
«при обрыве связи клиента обработка должна кэшироваться, а не крашить клиент».

Идея:
  * Генерация запускается как ФОНОВАЯ asyncio-задача и пишет токены в общий буфер
    + рассылает их подписчикам (WebSocket / SSE клиентам).
  * Если клиент отвалился — задача НЕ останавливается: она дописывает ответ и
    сохраняет его в БД. Клиент может переподключиться и забрать уже накопленный текст
    (через тот же WS или резервный SSE-эндпоинт по job_id).

Так «тонкий клиент» никогда не блокирует UI и не теряет ответ при сетевых сбоях.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from backend.llm_gateway import stream_completion
from backend.schemas import GenerationParams

logger = logging.getLogger("aichat.generation")


@dataclass
class GenerationJob:
    """Состояние одной идущей генерации."""
    job_id: str
    session_id: int
    buffer: str = ""        # весь накопленный текст — кэш для реконнекта
    done: bool = False
    error: Optional[str] = None
    # Ссылка на фоновую задачу — нужна, чтобы её можно было остановить (кнопка Stop).
    task: Optional["asyncio.Task"] = None
    # Очереди подписчиков: у каждого подключённого клиента — своя очередь событий.
    subscribers: list[asyncio.Queue] = field(default_factory=list)

    def subscribe(self) -> asyncio.Queue:
        """Подписаться на события генерации. Новому подписчику сразу отдаём кэш."""
        q: asyncio.Queue = asyncio.Queue()
        # Поддержка реконнекта: отдаём уже накопленный буфер одним событием.
        if self.buffer:
            q.put_nowait({"type": "token", "content": self.buffer})
        if self.done:
            q.put_nowait({"type": "done", "content": ""})
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        """Разослать событие всем подписчикам (без ожидания — очереди безразмерны)."""
        for q in self.subscribers:
            q.put_nowait(event)


# Колбэк, который сохраняет финальный ответ в БД: (session_id, text) -> awaitable.
OnComplete = Callable[[int, str], Awaitable[None]]


class GenerationManager:
    """Хранит активные генерации в памяти процесса и управляет их жизненным циклом."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}

    def get(self, job_id: str) -> Optional[GenerationJob]:
        return self._jobs.get(job_id)

    async def start(
        self,
        job_id: str,
        session_id: int,
        messages: list[dict],
        params: Optional[GenerationParams],
        on_complete: Optional[OnComplete] = None,
        connection: Optional[dict] = None,
    ) -> GenerationJob:
        """Запускает фоновую генерацию и СРАЗУ возвращает job (не блокирует клиента)."""
        job = GenerationJob(job_id=job_id, session_id=session_id)
        self._jobs[job_id] = job
        # create_task -> работа идёт в фоне независимо от того, слушает ли её клиент.
        job.task = asyncio.create_task(
            self._run(job, messages, params, on_complete, connection)
        )
        return job

    def cancel(self, job_id: str) -> None:
        """Останавливает генерацию (кнопка Stop). Уже сгенерированный текст сохранится."""
        job = self._jobs.get(job_id)
        if job and job.task and not job.task.done():
            job.task.cancel()

    async def start_runner(self, job_id: str, session_id: int, runner) -> "GenerationJob":
        """
        Запускает произвольный сценарий генерации (нужно для групповых чатов, где за
        один ход отвечают НЕСКОЛЬКО персонажей). runner(job) сам шлёт события через
        job.broadcast(...) и сохраняет сообщения. Мы лишь оборачиваем его в job и
        гарантируем финальное событие 'done'.
        """
        job = GenerationJob(job_id=job_id, session_id=session_id)
        self._jobs[job_id] = job
        job.task = asyncio.create_task(self._run_runner(job, runner))
        return job

    async def _run_runner(self, job, runner) -> None:
        try:
            await runner(job)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка группового сценария (job %s)", job.job_id)
            job.error = str(exc)
            job.broadcast({"type": "error", "content": job.error})
        finally:
            job.done = True
            job.broadcast({"type": "done", "content": ""})
            asyncio.create_task(self._cleanup_later(job.job_id))

    async def _run(self, job, messages, params, on_complete, connection=None) -> None:
        try:
            async for token in stream_completion(messages, params, connection):
                job.buffer += token
                job.broadcast({"type": "token", "content": token})
        except asyncio.CancelledError:
            # Пользователь нажал Stop. Молча выходим: накопленный текст сохраним в finally.
            pass
        except Exception as exc:  # noqa: BLE001 — сеть/провайдер упали, но сервер живёт
            # Печатаем полный traceback в консоль сервера — видно реальную причину.
            logger.exception("Ошибка генерации (job %s): %s", job.job_id, exc)
            job.error = str(exc)
            job.broadcast({"type": "error", "content": job.error})
        finally:
            # Сохраняем накопленный ответ в БД ДО события 'done' — чтобы к моменту,
            # когда клиент увидит «готово», сообщение уже точно было записано.
            # Это работает и при обрыве связи, и при нажатии Stop (есть частичный текст).
            if on_complete and job.buffer:
                try:
                    await on_complete(job.session_id, job.buffer)
                except Exception:  # noqa: BLE001 — сохранение не должно ронять воркер
                    pass
            job.done = True
            job.broadcast({"type": "done", "content": ""})
            # Подчищаем job через некоторое время, чтобы реконнект успел забрать кэш.
            asyncio.create_task(self._cleanup_later(job.job_id))

    async def _cleanup_later(self, job_id: str, delay: int = 300) -> None:
        await asyncio.sleep(delay)
        self._jobs.pop(job_id, None)


# Глобальный singleton-менеджер на процесс.
generation_manager = GenerationManager()
