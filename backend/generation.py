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

Жизненный цикл хода (2.13.0):
  waiting (ждём первый чанк) → thinking (идут только размышления) → streaming
  (идёт текст) → saving → done.
  * Стрим идёт во ВНУТРЕННЕЙ задаче job.stream_task — Stop отменяет только её.
    Раньше Stop отменял всю задачу хода, и вторая отмена (кнопка шлёт и HTTP, и
    WS) прилетала уже в сохранение: CancelledError пролетал мимо `except
    Exception`, 'done' не уходил, job навсегда оставался «идущим», а соединение
    SQLite с незакоммиченным INSERT запирало базу до перезапуска.
  * Сохранение защищено asyncio.shield, а 'done' рассылается в finally без
    единого await — ход заканчивается ВСЕГДА.
  * Каждое событие несёт job_id и seq; поздний подписчик получает статус, текст,
    размышления, пояснения и ошибку — не только голый текст.
  * Пока модель молчит, раз в STATUS_PULSE секунд уходит status с прошедшим
    временем: клиент отличает «модель думает» от «сервер умер».
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from backend import turn_events
from backend.config import settings
from backend.llm_gateway import effective_model, stream_completion
from backend.schemas import GenerationParams

logger = logging.getLogger("aichat.generation")

# Стадии, на которых модель ещё работает: на них идёт пульс status.
LIVE_STAGES = ("waiting", "thinking", "streaming")
# События, которые повторяются позднему подписчику (в порядке появления):
# без них переподключившийся клиент получал 'done' без ошибки и молча терял ход.
STICKY_TYPES = ("notice", "fallback", "error")
# Пульс (секунды): держит WS/SSE живыми и даёт клиенту «жду ответ модели · 1:20».
STATUS_PULSE = 10.0
# Через сколько секунд после конца job убирается из памяти (реконнект успевает
# забрать кэш).
CLEANUP_DELAY = 300


def _fmt_duration(seconds) -> str:
    seconds = int(seconds)
    if seconds >= 60:
        return f"{round(seconds / 60)} мин"
    return f"{seconds} с"


@dataclass
class GenerationJob:
    """Состояние одной идущей генерации."""
    job_id: str
    session_id: int
    # send | regenerate | continue | retry | group — что за ход (для клиента и уведомлений).
    kind: str = "send"
    buffer: str = ""        # весь накопленный текст — кэш для реконнекта
    done: bool = False
    error: Optional[str] = None        # ошибка генерации (модель, таймаут)
    save_error: Optional[str] = None   # ответ написан, но не сохранился
    # Если ответ дала ЗАПАСНАЯ модель (fallback) — её имя, для записи в model_used.
    model_used: Optional[str] = None
    # Задача всего хода (генерация + сохранение). Снаружи её НЕ отменяют.
    task: Optional["asyncio.Task"] = None
    # Задача самого стрима модели — её и только её отменяет Stop.
    stream_task: Optional["asyncio.Task"] = None
    # Групповой ход: сценарий runner(job) в своей задаче (см. start_runner).
    runner_task: Optional["asyncio.Task"] = None
    group: bool = False
    # Очереди подписчиков: у каждого подключённого клиента — своя очередь событий.
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    stage: str = "waiting"
    started_at: float = field(default_factory=time.time)
    # Монотонные часы для дедлайнов (time.time может прыгать при синхронизации).
    started_mono: float = field(default_factory=time.monotonic)
    first_chunk_at: Optional[float] = None
    thought: str = ""                  # накопленные размышления (для повтора)
    sticky: list[dict] = field(default_factory=list)
    speaker: Optional[dict] = None     # группа: кто сейчас отвечает (для повтора)
    cancel_requested: bool = False
    # Ответ не нужен вовсе (удалили чат или реплику, на которую он шёл): не сохранять.
    discard: bool = False
    timed_out: bool = False
    seq: int = 0
    # id сохранённого ответа (если on_complete его вернул) — для уведомлений.
    message_id: Optional[int] = None
    done_event: Optional[dict] = None
    finished: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def stopping(self) -> bool:
        """Ход больше не должен ждать модель: Stop или истёк потолок времени."""
        return self.cancel_requested or self.timed_out

    def elapsed(self) -> int:
        return int(time.monotonic() - self.started_mono)

    def info(self) -> dict:
        """Краткое состояние для /api/sessions/{id}/active_job, /api/jobs/active и snapshot."""
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "stage": self.stage,
            "started_at": self.started_at,
            "elapsed": self.elapsed(),
            "chars": len(self.buffer),
        }

    def status_event(self) -> dict:
        return {"type": "status", "stage": self.stage, "elapsed": self.elapsed(), "kind": self.kind}

    def subscribe(self) -> asyncio.Queue:
        """
        Подписаться на события генерации. Новому подписчику сразу отдаём кэш:
        статус, пояснения/fallback, текущего говорящего (группа), размышления,
        весь текст одним token, ошибку и 'done' (если ход уже кончился).

        Повторённые события помечены replay=True; token повтора — это ВЕСЬ текст
        (клиент заменяет им показанное, а не дописывает). Порядок отличается от
        живого в одном: fallback идёт ДО текста — живьём он велит клиенту
        очистить текст основной модели, и после текста стёр бы ответ запасной.
        """
        q: asyncio.Queue = asyncio.Queue()
        snap = {"job_id": self.job_id, "seq": self.seq, "replay": True}
        q.put_nowait({**self.status_event(), **snap})
        for ev in self.sticky:
            if ev["type"] != "error":
                q.put_nowait({**ev, "replay": True})
        if self.speaker:
            q.put_nowait({**self.speaker, "replay": True})
        if self.thought:
            q.put_nowait({"type": "thought", "content": self.thought, **snap})
        # Поддержка реконнекта: отдаём уже накопленный буфер одним событием.
        if self.buffer:
            q.put_nowait({"type": "token", "content": self.buffer, **snap})
        for ev in self.sticky:
            if ev["type"] == "error":
                q.put_nowait({**ev, "replay": True})
        if self.done:
            q.put_nowait({**(self.done_event or {"type": "done", "content": "", **snap}), "replay": True})
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def broadcast(self, event: dict) -> dict:
        """
        Разослать событие всем подписчикам (без ожидания — очереди безразмерны).
        Событие получает job_id и порядковый seq: по ним клиент отличает события
        своего хода от чужих и отбрасывает дубли после переподключения.
        """
        self.seq += 1
        ev = {**event, "job_id": self.job_id, "seq": self.seq}
        if ev.get("type") in STICKY_TYPES:
            self.sticky.append(ev)
        elif ev.get("type") == "speaker":
            self.speaker = ev
        for q in list(self.subscribers):
            q.put_nowait(ev)
        return ev


# Колбэк, который сохраняет финальный ответ в БД:
# (session_id, text, model_used_override) -> awaitable[int | None]. Третий
# аргумент задан, только если ответ дала запасная модель (иначе None — пишется
# основная). Вернуть можно id сохранённого сообщения — он уйдёт в 'done' и в
# уведомление «Ответ готов».
OnComplete = Callable[..., Awaitable[Optional[int]]]


class GenerationManager:
    """Хранит активные генерации в памяти процесса и управляет их жизненным циклом."""

    def __init__(self) -> None:
        self._jobs: dict[str, GenerationJob] = {}
        # Отложенные уборки: держим ссылки, иначе задачу может съесть сборщик мусора.
        self._cleanups: set = set()

    def get(self, job_id: str) -> Optional[GenerationJob]:
        return self._jobs.get(job_id)

    def active(self) -> int:
        """Сколько генераций идёт сейчас (фоновый загрузчик файлов ждёт, пока их нет)."""
        return sum(1 for j in self._jobs.values() if not j.done)

    def active_for(self, session_id: int) -> Optional[GenerationJob]:
        """
        Идущий ход чата (последний не завершённый). По нему клиент находит ход
        после F5 или смены чата, а сервер не даёт запустить второй параллельно.
        """
        for job in reversed(list(self._jobs.values())):
            if job.session_id == session_id and not job.done:
                return job
        return None

    def active_jobs(self) -> list[GenerationJob]:
        return [j for j in self._jobs.values() if not j.done]

    async def wait_done(self, job: GenerationJob, timeout: float) -> bool:
        """Дождаться конца хода не дольше timeout секунд. True — ход закончился."""
        if job.done:
            return True
        try:
            await asyncio.wait_for(job.finished.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        return job.done

    def _register(self, job: GenerationJob) -> None:
        self._jobs[job.job_id] = job
        turn_events.publish({
            "type": "turn_started", "session_id": job.session_id,
            "job_id": job.job_id, "kind": job.kind,
        })

    async def start(
        self,
        job_id: str,
        session_id: int,
        messages: list[dict],
        params: Optional[GenerationParams],
        on_complete: Optional[OnComplete] = None,
        connection: Optional[dict] = None,
        kind: str = "send",
    ) -> GenerationJob:
        """Запускает фоновую генерацию и СРАЗУ возвращает job (не блокирует клиента)."""
        job = GenerationJob(job_id=job_id, session_id=session_id, kind=kind)
        self._register(job)
        # create_task -> работа идёт в фоне независимо от того, слушает ли её клиент.
        job.task = asyncio.create_task(
            self._run(job, messages, params, on_complete, connection)
        )
        return job

    def cancel(self, job_id: str, discard: bool = False) -> bool:
        """
        Останавливает генерацию (кнопка Stop). Уже сгенерированный текст сохранится
        (discard=True — не сохранится: чат или реплику, на которую шёл ответ, удалили).

        Идемпотентна: повторная отмена, отмена закончившегося хода или хода,
        который уже сохраняет ответ, ничего не делает. Отменяется только ожидание
        модели (stream_task) — сохранение и 'done' прервать нельзя.
        Возвращает True, если именно этот вызов остановил ход.
        """
        job = self._jobs.get(job_id)
        if job is None or job.done:
            return False
        if discard and job.stage != "saving":
            job.discard = True
        if job.cancel_requested:
            return False
        if job.stage == "saving":
            if job.group:
                # Группа сохраняет реплику одного персонажа, а очередь ещё не
                # кончилась: сохранение не трогаем, следующих просто не будет.
                job.cancel_requested = True
                return True
            return False
        job.cancel_requested = True
        self._interrupt(job)
        return True

    def _interrupt(self, job: GenerationJob) -> None:
        """Прервать ожидание модели, не трогая сохранение."""
        st = job.stream_task
        if st is not None:
            # Стрим идёт — режем его. Уже кончился, а сценарий ещё не продолжил
            # (тот же оборот цикла) — сценарий не трогаем: он увидит stopping и
            # сохранит дописанную реплику сам.
            if not st.done():
                st.cancel()
            return
        # Групповой сценарий вне стрима (режиссёр выбирает, пауза между ответами):
        # сценарий целиком. На сохранении реплики (stage saving) — не трогаем.
        rt = job.runner_task
        if rt is not None and not rt.done() and job.stage != "saving":
            rt.cancel()

    def set_stage(self, job: GenerationJob, stage: str) -> None:
        """Сменить стадию и сообщить её подписчикам (status при каждой смене)."""
        if job.stage == stage:
            return
        job.stage = stage
        job.broadcast(job.status_event())

    # ---------------------------------------------------------------- стрим
    def _on_thought(self, job: GenerationJob):
        # Размышления модели транслируем клиентам live (в ответ они не входят).
        def cb(t: str) -> None:
            if job.first_chunk_at is None:
                job.first_chunk_at = time.time()
            job.thought += t
            if job.stage == "waiting":
                self.set_stage(job, "thinking")
            job.broadcast({"type": "thought", "content": t})
        return cb

    def _on_notice(self, job: GenerationJob):
        # Пояснение, если запрос пришлось облегчить (файлы истории, см. stream_completion).
        return lambda t: job.broadcast({"type": "notice", "content": t})

    async def _consume(self, job: GenerationJob, stream) -> None:
        async for token in stream:
            if job.first_chunk_at is None:
                job.first_chunk_at = time.time()
            job.buffer += token
            if job.stage != "streaming":
                self.set_stage(job, "streaming")
            job.broadcast({"type": "token", "content": token})

    async def _watch(self, job: GenerationJob, task: "asyncio.Task") -> None:
        """
        Ждёт задачу работы, попутно рассылая пульс status и следя за потолком
        хода (settings.GENERATION_MAX_SECONDS). Ждём через asyncio.wait с
        таймаутом, а не через sleep в соседней задаче: пульс не живёт дольше хода.
        """
        next_pulse = time.monotonic() + STATUS_PULSE
        while not task.done():
            now = time.monotonic()
            limit = settings.GENERATION_MAX_SECONDS
            timeout = next_pulse - now
            if limit and limit > 0 and not job.timed_out:
                timeout = min(timeout, job.started_mono + limit - now)
            await asyncio.wait({task}, timeout=max(0.0, timeout))
            if task.done():
                break
            now = time.monotonic()
            if limit and limit > 0 and not job.timed_out and now >= job.started_mono + limit:
                job.timed_out = True
                logger.warning("Ход %s упёрся в потолок %s с — останавливаю", job.job_id, limit)
                if not job.error:
                    job.error = f"Модель не закончила ответ за {_fmt_duration(limit)} — остановлено"
                self._interrupt(job)
            if now >= next_pulse:
                if job.stage in LIVE_STAGES:
                    job.broadcast(job.status_event())
                next_pulse = now + STATUS_PULSE

    async def _drive(self, job: GenerationJob, stream) -> Optional[BaseException]:
        """
        Прогнать стрим модели в job.stream_task. Возвращает исключение стрима или
        None (ответ дописан, остановлен кнопкой Stop или потолком времени).
        """
        if job.stopping:
            await _aclose_quiet(stream)
            return None
        task = asyncio.create_task(self._consume(job, stream))
        job.stream_task = task
        try:
            await self._watch(job, task)
        except asyncio.CancelledError:
            # Отменили сам ход (остановка сервера) — стрим за ним.
            task.cancel()
            raise
        finally:
            job.stream_task = None
        if task.cancelled():
            return None
        return task.exception()

    def _stream(self, job, messages, params, connection):
        return stream_completion(messages, params, connection,
                                 on_thought=self._on_thought(job), on_notice=self._on_notice(job))

    async def _run(self, job, messages, params, on_complete, connection=None) -> None:
        try:
            try:
                job.broadcast(job.status_event())
                await self._generate(job, messages, params, connection)
            finally:
                # Сохраняем накопленный ответ в БД ДО события 'done' — чтобы к моменту,
                # когда клиент увидит «готово», сообщение уже точно было записано.
                # Это работает и при обрыве связи, и при нажатии Stop (есть частичный текст).
                await self._save(job, on_complete)
                # Ошибка генерации — ПОСЛЕ сохранения частичного ответа: клиент,
                # увидев её, перечитывает чат, и написанное уже там.
                if job.error:
                    job.broadcast({"type": "error", "content": job.error})
                if job.save_error:
                    job.broadcast({"type": "error", "content": job.save_error})
        finally:
            self._finish(job)

    async def _generate(self, job, messages, params, connection) -> None:
        exc = await self._drive(job, self._stream(job, messages, params, connection))
        if exc is None:
            return
        # Печатаем полный traceback в консоль сервера — видно реальную причину.
        logger.error("Ошибка генерации (job %s): %s", job.job_id, exc, exc_info=exc)
        # Запасная модель: если настроена и включён авто-режим — повторяем ход ею,
        # вместо того чтобы сразу показывать ошибку. После Stop — не повторяем.
        fb = ((connection or {}).get("fallback_model") or "").strip()
        auto = (connection or {}).get("auto_fallback", True)
        if fb and auto and fb != effective_model(params, connection) and not job.stopping:
            await self._run_fallback(job, messages, params, connection, fb, exc)
        else:
            job.error = str(exc)

    async def _run_fallback(self, job, messages, params, connection, fb_model, primary_exc) -> None:
        """
        Повтор генерации ЗАПАСНОЙ моделью после сбоя основной. Частичный текст
        основной модели сбрасывается (событие 'fallback' велит клиенту очистить
        live-текст), ответ пишется с чистого листа.
        """
        job.buffer = ""
        job.thought = ""
        job.broadcast({"type": "fallback", "model": fb_model, "reason": str(primary_exc)})
        # С этого момента любой сохранённый текст — от запасной модели.
        job.model_used = fb_model
        fparams = (
            params.model_copy(update={"model": fb_model})
            if params else GenerationParams(model=fb_model)
        )
        self.set_stage(job, "waiting")
        exc2 = await self._drive(job, self._stream(job, messages, fparams, connection))
        if exc2 is None:
            return  # дописала, или Stop во время запасной генерации — частичный текст сохранится
        logger.error("Запасная модель тоже не ответила (job %s)", job.job_id, exc_info=exc2)
        job.error = f"Основная модель: {primary_exc}\nЗапасная ({fb_model}): {exc2}"

    async def _save(self, job: GenerationJob, on_complete) -> None:
        """
        Сохранение ответа отдельной задачей под asyncio.shield: отмена хода
        (остановка сервера) не рвёт INSERT посреди транзакции — ждём, пока
        сохранение закончится. Любой сбой логируется и уходит клиенту ошибкой:
        раньше `except Exception: pass` молча терял готовый ответ.
        """
        if not (on_complete and job.buffer) or job.discard:
            return
        self.set_stage(job, "saving")
        save = asyncio.create_task(on_complete(job.session_id, job.buffer, job.model_used))
        while True:
            try:
                result = await asyncio.shield(save)
            except asyncio.CancelledError:
                if not save.done():
                    # Отменили НАС, а не сохранение: дожидаемся его.
                    continue
                if save.cancelled():
                    logger.error("Сохранение ответа прервано (job %s)", job.job_id)
                    job.save_error = "Ответ не сохранился: сохранение прервано"
                    return
                continue  # сохранение успело закончиться — заберём результат
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # noqa: BLE001 — сохранение не должно ронять воркер
                logger.exception("Ответ не сохранился (job %s)", job.job_id)
                job.save_error = f"Ответ не сохранился: {exc}"
                return
            if isinstance(result, int) and not isinstance(result, bool):
                job.message_id = result
            return

    def _finish(self, job: GenerationJob) -> None:
        """
        Конец хода — БЕЗ единого await: сюда не долетит отмена, и 'done' уходит
        всегда. Каждый шаг под своим try: сбой одного не отменяет остальные.
        """
        job.stage = "done"
        job.done = True
        try:
            job.done_event = job.broadcast({
                "type": "done", "content": "",
                "ok": not (job.error or job.save_error),
                "stopped": job.cancel_requested,
                "message_id": job.message_id,
            })
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось разослать done (job %s)", job.job_id)
        job.finished.set()
        try:
            turn_events.publish({
                "type": "turn_done", "session_id": job.session_id, "job_id": job.job_id,
                "kind": job.kind, "ok": not (job.error or job.save_error),
                "error": job.error or job.save_error, "stopped": job.cancel_requested,
                "message_id": job.message_id, "preview": job.buffer.strip()[:140],
            })
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось опубликовать конец хода (job %s)", job.job_id)
        # Подчищаем job через некоторое время, чтобы реконнект успел забрать кэш.
        try:
            t = asyncio.get_running_loop().create_task(self._cleanup_later(job.job_id))
            self._cleanups.add(t)
            t.add_done_callback(self._cleanups.discard)
        except RuntimeError:
            self._jobs.pop(job.job_id, None)

    # ---------------------------------------------------------------- группа
    async def start_runner(self, job_id: str, session_id: int, runner, kind: str = "group") -> "GenerationJob":
        """
        Запускает произвольный сценарий генерации (нужно для групповых чатов, где за
        один ход отвечают НЕСКОЛЬКО персонажей). runner(job) сам шлёт события через
        job.broadcast(...) и сохраняет сообщения; стрим каждого персонажа он ведёт
        через stream_part — тогда Stop прерывает только ожидание модели. Мы лишь
        оборачиваем его в job и гарантируем финальное событие 'done'.
        """
        job = GenerationJob(job_id=job_id, session_id=session_id, kind=kind, group=True)
        self._register(job)
        job.task = asyncio.create_task(self._run_runner(job, runner))
        return job

    async def _run_runner(self, job, runner) -> None:
        try:
            job.broadcast(job.status_event())
            work = asyncio.create_task(runner(job))
            job.runner_task = work
            try:
                await self._watch(job, work)
            except asyncio.CancelledError:
                work.cancel()
                raise
            finally:
                job.runner_task = None
            if not work.cancelled() and work.exception() is not None:
                exc = work.exception()
                logger.error("Ошибка группового сценария (job %s)", job.job_id, exc_info=exc)
                job.error = str(exc)
            if job.error:
                job.broadcast({"type": "error", "content": job.error})
        finally:
            self._finish(job)

    async def stream_part(self, job: GenerationJob, make_stream) -> tuple[str, Optional[BaseException]]:
        """
        Стрим ОДНОГО персонажа группового хода внутри job: свой буфер (повтор
        подписчику — только текущая реплика, прежние уже в БД), Stop прерывает
        только его. make_stream(on_thought, on_notice) -> async-генератор токенов.
        Возвращает (текст, исключение | None); после Stop/потолка — (текст, None),
        и сценарий сам решает сохранить частичное и закончить.
        """
        job.buffer = ""
        job.thought = ""
        stream = make_stream(self._on_thought(job), self._on_notice(job))
        if job.stopping:
            await _aclose_quiet(stream)
            return "", None
        self.set_stage(job, "waiting")
        task = asyncio.create_task(self._consume(job, stream))
        job.stream_task = task
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            job.stream_task = None
        if task.cancelled():
            return job.buffer, None
        return job.buffer, task.exception()

    async def _cleanup_later(self, job_id: str, delay: int = CLEANUP_DELAY) -> None:
        await asyncio.sleep(delay)
        self._jobs.pop(job_id, None)


async def _aclose_quiet(stream) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001
        pass


# Глобальный singleton-менеджер на процесс.
generation_manager = GenerationManager()
