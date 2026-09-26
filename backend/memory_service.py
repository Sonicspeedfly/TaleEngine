"""
Сервис иерархической памяти: адаптер чистого ядра hierarchical_memory к БД.

Ядро умеет свёртку State_N = merge(State_{N-1}, Block_N), паузы, повторы и
проверку снимка, но не знает ни про SQLAlchemy, ни про main. Здесь — всё, что
связывает его с TaleEngine:

  * запись «📜 Память чата (авто)» (HoraeEntry, category="summary"): указатель
    «учтено до», буфер пересборки meta["rebuild"], прижатие указателя после
    удаления сообщений;
  * DbBatchSource — источник пакетов для ядра: что уже учтено, что старше
    активного окна, сверка куска с чатом перед записью, факты после записи;
  * ежеходный инкремент run_incremental (раньше — main._summary_pass);
  * задания «Пересобрать»/«Догнать» (start_job/cancel_job), статус, сброс и
    экспорт снимка для вкладки «Память».

main сюда не импортируется (цикл). complete и get_connection приходят через
MemoryDeps с поздним связыванием: main передаёт лямбды, которые берут свои
глобалы в момент вызова, поэтому patch("backend.main.complete") в тестах
действует и на этот код.
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend import hierarchical_memory as hm
from backend import horae_recall, llm_gateway, models
from backend.config import settings
from backend.database import AsyncSessionLocal
from backend.horae_memory import chat_tzinfo, count_tokens

logger = logging.getLogger("aichat.summary")

AUTO_SUMMARY_MARK = "__auto__"   # метка авто-записи в keywords
AUTO_SUMMARY_TITLE = "📜 Память чата (авто)"

# Пробельные символы, которые не делают сообщение «с текстом» (см. _has_content).
_BLANK = " \t\r\n"


@dataclass
class MemoryDeps:
    """Зависимости от main, связанные поздно (см. docstring модуля)."""
    complete: Callable[..., Awaitable[str]]
    get_connection: Callable[[AsyncSession], Awaitable[dict]]


# ============================================================================
# Указатель «учтено до» и запись сводки
# ============================================================================
def summary_last_id(entry) -> int:
    """
    До какого сообщения авто-сводка уже учла события.

    Читаем из служебного поля meta, но поддерживаем и СТАРЫЙ формат — метку
    "last:123" внутри keywords. Старый формат был хрупким: keywords пользователь
    правит руками в интерфейсе, и достаточно было тронуть ключевые слова записи
    «Память чата (авто)», чтобы указатель исчез и сводка пересобиралась заново.
    """
    if entry is None:
        return 0
    meta = getattr(entry, "meta", None)
    if isinstance(meta, dict):
        try:
            value = int(meta.get("last_message_id") or 0)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    for kw in (entry.keywords or []):  # легаси-метка
        if isinstance(kw, str) and kw.startswith("last:"):
            try:
                return int(kw[5:])
            except ValueError:
                pass
    return 0


def int_or_zero(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def chat_compression(db, session_id: int) -> dict:
    """
    Кто сжимает историю чата: мастер-снимок, свёртки Хроники или никто
    (horae_engine.chat_compression). Сбой — как до Хроники: снимок по «ui».
    """
    try:
        from backend import horae_engine

        session = await db.get(models.ChatSession, session_id)
        if session is not None:
            info = await horae_engine.chat_compression(db, session)
            return {"engine": info["engine"], "summary_layer": info["summary_layer"]}
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось узнать, кто сжимает историю чата %s", session_id)
    ui, _ = await _ui_setting(db)
    return {"engine": "snapshot" if ui_flag(ui, "auto_summary") else "off",
            "summary_layer": "default"}


async def _summary_entry(db, session_id: int):
    return (await db.execute(
        select(models.HoraeEntry).where(
            models.HoraeEntry.session_id == session_id,
            models.HoraeEntry.category == "summary",
        )
    )).scalars().first()


async def clamp_summary_pointer(db, session_id: int) -> int | None:
    """
    Не даёт указателю авто-сводки стоять дальше последнего сообщения чата.
    Возвращает новый указатель, если его пришлось сдвинуть; коммит — за вызывающим.

    id сообщений в SQLite без AUTOINCREMENT: после удаления самых свежих
    сообщений следующие получают ТЕ ЖЕ id. Раньше указатель при этом стоял на
    месте, и переписанный заново ответ с «чужим» id считался уже учтённым: в
    сводку и факты он не попадал никогда, а активное окно потом выбрасывало его
    из контекста как «пересказанный». В памяти же жил удалённый текст.

    Указатель ставится на последнее ОСТАВШЕЕСЯ сообщение, а не в ноль. Всё, что
    осталось в чате, лежит до него и в сводке уже учтено. Сброс в ноль заставлял
    модель заново «дописывать» в старую сводку весь чат с самого начала как
    новые события — сюжет в хронике откатывался к первой сцене на десятки ходов,
    а факты дублировались. Факты из удалённых сообщений уходят вместе с ними.

    То же для указателя буфера пересборки (meta["rebuild"], см. DbBatchSource):
    он живёт по тем же id. Прижатие НЕ повышает версию сводки — указатель
    старого формата так и остаётся недоверенным для окна.
    """
    entry = await _summary_entry(db, session_id)
    if entry is None:
        return None
    max_id = (await db.execute(
        select(func.max(models.Message.id)).where(models.Message.session_id == session_id)
    )).scalar() or 0
    meta = dict(entry.meta or {})
    moved = False
    if summary_last_id(entry) > max_id:
        meta["last_message_id"] = max_id
        # Легаси-метку «last:N» тоже убираем: при указателе 0 в meta она бы снова
        # вернула старое значение (см. summary_last_id).
        entry.keywords = [
            k for k in (entry.keywords or [])
            if not (isinstance(k, str) and k.startswith("last:"))
        ] or [AUTO_SUMMARY_MARK]
        moved = True
    rebuild = meta.get("rebuild")
    if isinstance(rebuild, dict) and int_or_zero(rebuild.get("last_message_id")) > max_id:
        meta["rebuild"] = {**rebuild, "last_message_id": max_id}
        moved = True
    if int_or_zero(meta.get("facts_upto")) > max_id:
        # Отметка «факты разобраны до» (см. DbBatchSource.commit) — по тем же id.
        meta["facts_upto"] = max_id
        moved = True
    if not moved:
        return None
    entry.meta = meta
    await db.execute(sql_delete(models.HoraeFact).where(
        models.HoraeFact.session_id == session_id,
        models.HoraeFact.source_message_id > max_id,
    ))
    return max_id


def snapshot_tokens(content) -> int:
    """
    Размер снимка в токенах — одна оценка на meta.tokens, статус и экспорт.

    Тем же horae_memory.count_tokens, что у менеджера ядра (build_manager):
    раньше число считали в трёх местах, и одно из них брало внедрённый в
    источник оценщик, а другое — модульный, то есть две оценки на одно поле.
    Пустой снимок (или одни пробелы) весит ноль, как пустой блок в инспекторе.

    Без кэша, в отличие от estimate_tokens: каждый снимок уникален (новый на
    каждый пакет), повторно его не считают, а кэш держал бы вместе с числом и
    сам текст в десятки килобайт — после большой пересборки сотни мегабайт
    (финальное ревью, сервис M5). Порога длинного кэша estimate_tokens тут
    мало: снимки короче 20 000 символов копились бы всё равно.
    """
    text = str(content or "")
    return count_tokens(text) if text.strip() else 0


def adopt_summary(entry, content: str, last_id: int, *, tokens: int | None = None,
                  updated_by: str = "incremental", warnings=None) -> None:
    """
    Записывает снимок текущего формата: текст, указатель «учтено до» и версию,
    которой окно верит (horae_recall.SUMMARY_FORMAT), плюс схему снимка, его
    размер, кто записал и последние предупреждения для вкладки «Память».
    Незаконченная пересборка при этом уходит — этот снимок её и заменяет.
    Коммит — за вызывающим.

    Текст НЕ режется. Раньше здесь стоял слайс [:6000] символов, и хвостовые
    разделы снимка (списки, реестр) пропадали молча, а модель «забывала»
    договорённости из начала чата. От мусора защищает проверка ядра (снимок
    длиннее max(hm.MAX_SNAPSHOT_CHARS, 8 × бюджет) символов не принимается),
    от раздувания — бюджет снимка и сжатие хроники в арки.

    tokens — уже посчитанный размер: передан — не пересчитывается, иначе
    snapshot_tokens(content).

    Ошибка прошлого ежеходного прохода (meta.last_error) уходит вместе с
    буфером: снимок записан — пауза после сбоя и строка «Память не
    обновилась» во вкладке больше не нужны.
    """
    content = content or ""
    meta = dict(entry.meta or {})
    meta.pop("rebuild", None)
    meta.pop("last_error", None)
    # Указатель — в служебное meta, а не в keywords: keywords пользователь
    # редактирует руками, и правка ключевых слов ломала сводку.
    meta.update(
        last_message_id=last_id,
        v=horae_recall.SUMMARY_FORMAT,
        schema=hm.SNAPSHOT_SCHEMA,
        tokens=int(tokens) if tokens is not None else snapshot_tokens(content),
        updated_by=updated_by,
        warnings=list(warnings or [])[-5:],
    )
    entry.meta = meta
    entry.title = AUTO_SUMMARY_TITLE
    entry.content = content
    entry.keywords = [AUTO_SUMMARY_MARK]
    entry.always_on = True
    # Пустой снимок (самый первый пакет чата нечитаем — одни <think>) не
    # включается: указатель его проходит, но в контекст ушёл бы пустой блок
    # «Что было в истории». Первый непустой снимок запись включает.
    entry.enabled = bool(content.strip())
    entry.priority = 50  # сводка важнее рядовых записей, но ниже ручных «100+»


# ============================================================================
# Настройки из «ui»
# ============================================================================
async def _ui_setting(db) -> tuple[object | None, dict]:
    """
    Строка настроек «ui» и её значение-словарь ({} — нет строки или мусор).
    Один помощник на весь сервис: раньше то же выражение стояло в трёх
    местах, и однажды одно из них прочло бы настройку иначе.
    """
    ui = await db.get(models.AppSetting, "ui")
    return ui, (ui.value if ui is not None and isinstance(ui.value, dict) else {})


def ui_flag(ui, key: str, default: bool = True) -> bool:
    """Булев флаг из настроек «ui»: выключен только явным false."""
    if ui and isinstance(ui.value, dict) and key in ui.value:
        return ui.value.get(key) is not False
    return default


def _ui_int(ui_value: dict, key: str, default: int, low: int, high: int) -> int:
    """Целое из «ui» в пределах поля вкладки «Память»; нет значения или мусор — default."""
    raw = ui_value.get(key)
    if raw is None or raw == "" or isinstance(raw, bool):
        return default
    try:
        return min(high, max(low, int(raw)))
    except (TypeError, ValueError):
        return default


def _memory_window(ui_value: dict) -> int:
    """
    Активное окно (Tier 3). Читается так же, как в horae_memory._long_memory:
    разойдись они — и сводка сжимала бы реплики, которые ещё лежат в окне и
    которые ещё могут перегенерировать или поправить.
    """
    try:
        return int(ui_value.get("memory_window", horae_recall.DEFAULT_WINDOW))
    except (TypeError, ValueError):
        return horae_recall.DEFAULT_WINDOW


def _summary_every(ui_value: dict) -> int:
    """
    Порог ежеходного инкремента. Настраивается в UI (вкладка «Память»): реже =
    дешевле, ведь каждое обновление снимка — ОТДЕЛЬНЫЙ платный запрос к модели.
    """
    every = settings.AUTO_SUMMARY_EVERY
    if ui_value.get("summary_every"):
        try:
            every = max(2, int(ui_value["summary_every"]))
        except (TypeError, ValueError):
            pass
    return every


def memory_config(ui_value, **overrides) -> hm.MemoryConfig:
    """
    Параметры ядра: размер пакета, пауза и бюджет снимка — из «ui» (пределы
    те же, что у полей вкладки «Память»), остальное — из настроек сервера.
    overrides (значения задания, None — не задано) важнее «ui».
    """
    ui_value = ui_value if isinstance(ui_value, dict) else {}
    values = {
        "batch_size": _ui_int(ui_value, "memory_batch", settings.MEMORY_BATCH_SIZE, 1, 200),
        "delay_ms": _ui_int(ui_value, "memory_delay_ms", settings.MEMORY_DELAY_MS, 0, 60_000),
        "snapshot_tokens": _ui_int(ui_value, "memory_snapshot_tokens",
                                   settings.MEMORY_SNAPSHOT_TOKENS, 1000, 200_000),
    }
    values.update({k: v for k, v in overrides.items() if v is not None})
    return hm.MemoryConfig(batch_max_chars=settings.MEMORY_BATCH_CHARS,
                           max_retries=settings.MEMORY_MAX_RETRIES, **values)


def _background_connection(connection) -> dict:
    """
    Подключение фоновых вызовов памяти: быстрая модель (summary_model), если
    задана, подставлена в default_model — см. build_manager, почему не в params.
    """
    connection = connection or {}
    fast = (connection.get("summary_model") or "").strip()
    return {**connection, "default_model": fast} if fast else connection


# Длина вывода памяти от бюджета снимка: модель переписывает снимок целиком
# (плюс события пакета) — 1,4 × бюджет, и ещё 1024 на обёртку и случайное
# рассуждение вслух. 12 000 → 17 824.
_OUT_PER_BUDGET = 1.4
_OUT_EXTRA = 1024


def memory_output_limit(connection) -> int | None:
    """Лимит вывода модели памяти по карте LiteLLM; None — модель неизвестна."""
    model = llm_gateway.effective_model(None, _background_connection(connection))
    return llm_gateway.model_max_output_tokens(model)


def max_snapshot_tokens(max_output: int | None) -> int | None:
    """
    Наибольший бюджет снимка, при котором 1,4 × бюджет + 1024 влезает в лимит
    вывода модели памяти; None — лимит неизвестен.
    """
    if not max_output:
        return None
    return max(0, int((max_output - _OUT_EXTRA) / _OUT_PER_BUDGET))


# Отказ провайдера именно из-за длины вывода: «max_tokens is too large»
# (OpenAI), «maxOutputTokens … supported range» (Vertex), max_output_tokens,
# max_completion_tokens.
_MAX_TOKENS_REJECTED_RE = re.compile(r"max[\s_-]?(?:output[\s_-]?|completion[\s_-]?)?tokens",
                                     re.IGNORECASE)


def _rejects_max_tokens(exc: BaseException) -> bool:
    """Провайдер отклонил запрос (400) из-за max_tokens, а не по сути."""
    return (hm.classify_error(exc).kind == "bad_request"
            and bool(_MAX_TOKENS_REJECTED_RE.search(str(exc))))


def build_manager(connection, ui_value, deps: MemoryDeps,
                  **overrides) -> tuple[hm.HierarchicalMemoryManager, dict]:
    """
    Менеджер ядра на один прогон → (менеджер, подключение фоновых вызовов).
    overrides — размер пакета и пауза задания (см. memory_config).

    Фоновая работа идёт на быстрой модели, если она задана в подключении:
    сводке и фактам дорогая модель чата не нужна. Модель подменяется в
    ПОДКЛЮЧЕНИИ, а params остаются None. Через GenerationParams(model=…) вызов
    переставал быть служебным: у params фильтры безопасности по умолчанию
    включены (disable_safety=False), и на жёстком ролевом эпизоде провайдер
    блокировал сводку. Указатель тогда не двигался, каждый ход повторял
    платную попытку, а память чата молча замирала именно там, где нужна.

    Свои температура и длина вывода — через llm_gateway.sampling_overrides:
    снимок переписывается целиком, и стандартного max_tokens ответа чата на
    него не хватает (обрезанный снимок ядро не примет).

    Длина вывода прижата к лимиту модели памяти, если его знает LiteLLM, а
    рабочий бюджет снимка — к наибольшему, который модель успевает написать
    (max_snapshot_tokens): иначе при модели по умолчанию gpt-4o (лимит вывода
    16 384) каждый запрос с 17 824 получал 400, и снимок не собирался никогда.
    Провайдер всё же отверг max_tokens (псевдоним прокси, неизвестный
    LiteLLM) — один повтор с DEFAULT_MAX_TOKENS и предупреждение, дальше в
    прогоне — сразу с ним.
    """
    bg_conn = _background_connection(connection)
    config = memory_config(ui_value, **overrides)
    out_tokens = max(settings.DEFAULT_MAX_TOKENS,
                     round(config.snapshot_tokens * _OUT_PER_BUDGET) + _OUT_EXTRA)
    max_out = memory_output_limit(bg_conn)
    if max_out:
        out_tokens = min(out_tokens, max_out)
        fits = max_snapshot_tokens(max_out)
        if fits and config.snapshot_tokens > fits:
            config.snapshot_tokens = fits
    config.output_tokens = out_tokens
    limits = {"max_tokens": out_tokens}

    async def ask(messages: list[dict], max_tokens: int) -> str:
        with llm_gateway.sampling_overrides(max_tokens=max_tokens,
                                            temperature=settings.MEMORY_TEMPERATURE), \
                llm_gateway.finish_reason_probe() as probe:
            text = await deps.complete(messages, None, bg_conn, kind="summary")
        # Непустой ответ, оборванный лимитом вывода, шлюз отдаёт как успех.
        # Для снимка это length: ядро сожмёт хронику и повторит слияние, а
        # не будет слать корректирующие ходы с тем же лимитом (финальное
        # ревью, I1). Факты — не снимок: обрывок списка фактов полезен как
        # есть, и отказ от него лишь терял бы их.
        if (llm_gateway.is_length_finish(probe["finish_reason"])
                and messages and messages[0].get("content") != horae_recall.FACTS_PROMPT):
            raise hm.MemoryLLMError(
                "length", "Ответ модели упёрся в лимит длины вывода: снимок оборван на "
                          f"{max_tokens} токенах (finish_reason={probe['finish_reason']})",
                retryable=False)
        return text

    async def llm(messages: list[dict]) -> str:
        try:
            return await ask(messages, limits["max_tokens"])
        except Exception as exc:  # noqa: BLE001 — разбираем только отказ из-за max_tokens
            if (limits["max_tokens"] <= settings.DEFAULT_MAX_TOKENS
                    or not _rejects_max_tokens(exc)):
                raise
            rejected = limits["max_tokens"]
            limits["max_tokens"] = manager.config.output_tokens = settings.DEFAULT_MAX_TOKENS
            manager.warn(
                f"модель памяти не приняла max_tokens={rejected} — запросы идут с "
                f"{settings.DEFAULT_MAX_TOKENS}; большой снимок оборвётся: уменьшите "
                "бюджет снимка или выберите модель памяти с длинным выводом")
            return await ask(messages, limits["max_tokens"])

    # Оценщик без кэша — как у snapshot_tokens: ядро считает снимки и их разделы,
    # каждый уникален, и кэш держал бы их тексты (см. snapshot_tokens).
    manager = hm.HierarchicalMemoryManager(llm, config, estimate_tokens=count_tokens)
    return manager, bg_conn


# ============================================================================
# Сообщения чата для ядра
# ============================================================================
def _has_content():
    """
    SQL-условие «сообщение не пустое»: есть текст (не одни пробелы) или вложения.

    Единственное определение пустоты в сервисе: по нему считается, сколько
    сообщений ждут сжатия, какие уходят в пакет и какие сверяются перед
    записью. Правило в двух местах (SQL здесь, strip() в Python там) однажды
    разошлось бы — и сверка куска не сошлась бы никогда: пакет с картинкой без
    подписи не записался бы ни на одном ходу. Считать в SQL, а не грузить
    тексты, нужно ради pending(): его зовут после каждого пакета, а бэклог
    импортированного чата — тысячи сообщений.

    Сообщение, в котором после чистки ядра ничего не останется (одни <think>),
    тоже «непустое»: оно идёт в пакет без строки, и указатель его проходит.
    """
    return or_(
        func.length(func.trim(func.coalesce(models.Message.content, ""), _BLANK)) > 0,
        func.coalesce(cast(models.Message.attachments, String), "[]").not_in(("[]", "null", "")),
    )


def _local_time(stamp, tz):
    """
    created_at в поясе чата. В БД время naive UTC (server_default=now()).
    Пояс не разобрался (tz is None) — время остаётся в UTC: опечатка в
    настройке чата не должна останавливать память (см. chat_tzinfo).
    """
    if not isinstance(stamp, datetime) or tz is None:
        return stamp
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(tz)


def _attachments_meta(attachments) -> tuple[dict, ...]:
    """Лёгкая мета вложений для строки «📎 …»: содержимое модели сжатия не нужно."""
    return tuple(
        {"type": a.get("type"), "name": a.get("name"), "mime": a.get("mime")}
        for a in (attachments or []) if isinstance(a, dict)
    )


async def load_memory_messages(db, session, after_id: int, before_id: int | None = None, *,
                               limit: int | None = None) -> list[hm.MemoryMessage]:
    """
    Непустые сообщения чата с after_id < id < before_id по порядку, в виде ядра.

    Имена — настоящие, а не «Пользователь»/«Персонаж»: снимок переносит автора
    из шапки строки, и хроника с безликими ролями не отличала бы, кто кому что
    пообещал. user — персона чата; assistant — speaker_name (групповой чат),
    иначе персонаж чата; system — «Система».

    :param limit: сколько сообщений взять (пакету нужно не больше batch_size —
        грузить ради него весь бэклог большого чата незачем).
    """
    persona = await db.get(models.Persona, session.persona_id) if session.persona_id else None
    character = await db.get(models.Character, session.character_id)
    user_name = ((persona.name if persona else "") or "").strip() or "Пользователь"
    char_name = ((character.name if character else "") or "").strip() or "Персонаж"
    tz = chat_tzinfo(session.timezone)

    q = select(models.Message).where(
        models.Message.session_id == session.id,
        models.Message.id > after_id,
        _has_content(),
    )
    if before_id is not None:
        q = q.where(models.Message.id < before_id)
    q = q.order_by(models.Message.id)
    if limit:
        q = q.limit(limit)
    out = []
    for m in (await db.execute(q)).scalars().all():
        if m.role == "user":
            speaker = user_name
        elif m.role == "assistant":
            speaker = (m.speaker_name or "").strip() or char_name
        else:
            speaker = "Система"
        out.append(hm.MemoryMessage(
            id=m.id, role=m.role, speaker=speaker, text=m.content or "",
            created_at=_local_time(m.created_at, tz),
            attachments=_attachments_meta(m.attachments),
        ))
    return out


# ============================================================================
# Источник пакетов (§6.3)
# ============================================================================
@dataclass
class _Target:
    """Куда пишет прогон: живой снимок записи или буфер пересборки."""
    entry: object | None
    rebuilding: bool
    pointer: int     # «учтено до» цели
    state: str       # снимок цели
    # Остановленный буфер ручной пересборки: прогон пишет в живой снимок, а
    # буфер ждёт «Продолжить» (см. _read_target). None — такого буфера нет.
    paused: dict | None = None


def _read_target(entry) -> _Target:
    """
    Цель записи. Пересборка идёт, если сводка старого формата (до 2.4.0, без
    meta.v = SUMMARY_FORMAT) или в meta лежит буфер «rebuild».

    Сводка старого формата пересобирается с нуля: она резала реплики до 1500
    символов, а бэклог — до 24 000, и её указателю окно не верит (см.
    horae_recall.SUMMARY_FORMAT). Новая копится в meta["rebuild"], а в контекст
    до конца пересборки идёт СТАРАЯ: иначе на время догонки середина чата не
    была бы ни в пересказе, ни дословно. Тот же буфер держит ручная пересборка
    (задание «Пересобрать»); пока он есть, ежеходные проходы продолжают его —
    так пересборка переживает перезапуск сервера.

    Кроме буфера, который пользователь ОСТАНОВИЛ (meta.rebuild.paused, ставит
    задание по «Остановить»): его продолжает только «Продолжить», а цель
    остальных прогонов — живой снимок. Раньше «Остановить» было лишь паузой
    за чужой счёт: ежеходные проходы продолжали буфер (до шести платных
    слияний за ход), живой снимок не обновлялся, и в конце буфер сам
    подменял его (финальное ревью, M2). У сводки старого формата буфер
    остановленным не бывает: её указателю окно не верит, писать в неё нельзя.
    """
    meta = dict(entry.meta or {}) if entry is not None else {}
    buffer = meta.get("rebuild") if isinstance(meta.get("rebuild"), dict) else None
    legacy = entry is not None and meta.get("v") != horae_recall.SUMMARY_FORMAT
    if entry is not None and buffer is not None and buffer.get("paused") and not legacy:
        return _Target(entry, False, summary_last_id(entry), entry.content or "", paused=buffer)
    if entry is not None and (legacy or buffer is not None):
        buffer = buffer or {}
        return _Target(entry, True, int_or_zero(buffer.get("last_message_id")),
                       str(buffer.get("content") or ""))
    return _Target(entry, False, summary_last_id(entry), (entry.content or "") if entry else "")


async def _window_start_id(db, session_id: int, window: int) -> int | None:
    """
    Граница сжатия: id самого старого сообщения окна — всё, что с ним и
    новее, ждёт. None — окна нет (window ≤ 0, сжимается весь чат); 0 — чат
    короче окна: модель и так видит его целиком, сжимать нечего.
    """
    if window <= 0:
        return None
    return (await db.execute(
        select(models.Message.id)
        .where(models.Message.session_id == session_id)
        .order_by(models.Message.id.desc())
        .offset(window - 1).limit(1)
    )).scalar() or 0


async def _count_between(db, session_id: int, after_id: int, before_id: int | None) -> int:
    """Сколько непустых сообщений лежит между указателем и окном."""
    q = select(func.count(models.Message.id)).where(
        models.Message.session_id == session_id,
        models.Message.id > after_id,
        _has_content(),
    )
    if before_id is not None:
        q = q.where(models.Message.id < before_id)
    return (await db.execute(q)).scalar() or 0


async def _pending_count(db, session_id: int, window: int) -> int:
    """
    Сколько сообщений старше окна ещё не учтены целью записи (буфером, если
    идёт пересборка). Одно правило на прогресс ядра и на статус вкладки
    «Память»: посчитай они по-разному — «ждут сжатия N» в интерфейсе не
    сходилось бы с тем, сколько сообщений задание на самом деле свернёт.
    """
    before = await _window_start_id(db, session_id, window)
    if before == 0:
        return 0
    target = _read_target(await _summary_entry(db, session_id))
    return await _count_between(db, session_id, target.pointer, before)


_MODES = ("incremental", "rebuild", "catchup")
# Сколько пакетов неразобранных фактами сообщений до пакета берёт запрос
# фактов (см. DbBatchSource._facts_gap): пропуск после одного-двух сбоев.
_FACTS_GAP_BATCHES = 2


class DbBatchSource:
    """
    Чат из БД как hm.BatchSource: ядро берёт отсюда пакеты и отдаёт сюда
    готовый снимок каждого пакета.

    Сжимается только то, что СТАРШЕ активного окна. Раньше в сводку и факты
    уходил и ответ, сохранённый миллисекунды назад. Пользователь потом
    перегенерировал его, жал «Продолжить» или правил текст, но указатель уже
    стоял за ним: новая версия в память не попадала никогда, а когда сообщение
    выходило из окна, модели оставался только пересказ ОТВЕРГНУТОГО свайпа.
    Пока сообщение в окне, модель видит его дословно; сжимается оно, уже
    устоявшись.

    mode: "incremental" — ежеходный проход (порог summary_every); "catchup" —
    задание «Догнать»; "rebuild" — задание «Пересобрать»: буфер создан заранее,
    каждый пакет пишется в него, подмена снимка — в конце задания.

    alive: False — чат, для которого источник создан, удалён (задание забыто,
    см. forget_job). Чат ищется по id, а SQLite отдаёт id удалённого чата
    следующему новому: без этой проверки задание брало бы пакеты нового чата
    и писало бы в его память.
    """

    def __init__(self, session_id: int, *, mode: str, threshold: int,
                 manager: hm.HierarchicalMemoryManager, want_facts: bool, connection: dict,
                 window: int, alive: Callable[[], bool] | None = None):
        # Своего оценщика токенов у источника нет: размер снимка в meta.tokens
        # считает adopt_summary (snapshot_tokens) — одна оценка на поле.
        if mode not in _MODES:
            raise ValueError(f"неизвестный режим памяти: {mode!r}")
        self.session_id = session_id
        self.mode = mode
        self.threshold = max(1, int(threshold or 1))
        self.manager = manager
        self.want_facts = want_facts
        self.connection = connection
        self.window = window
        self._alive = alive or (lambda: True)

    # ------------------------------------------------------ протокол ядра
    async def pending(self) -> int:
        async with AsyncSessionLocal() as db:
            return await _pending_count(db, self.session_id, self.window)

    async def current_state(self, state: str) -> str:
        """
        Свежий снимок цели перед пакетом: ручная правка снимка во вкладке
        «Память» между пакетами не затирается.

        Сначала — прижатие указателя. Он мог «уехать в будущее»: пользователь
        удалил последние сообщения, а чат старше этой защиты в delete_message.
        Тогда условие id > указателя не выполнялось бы НИКОГДА — сводка молча
        умирала навсегда. Указатель прижимается к последнему оставшемуся
        сообщению (не в ноль — см. clamp_summary_pointer).
        """
        async with AsyncSessionLocal() as db:
            entry = await _summary_entry(db, self.session_id)
            old_id = summary_last_id(entry)
            clamped = await clamp_summary_pointer(db, self.session_id)
            if clamped is not None:
                logger.info(
                    "Указатель авто-сводки чата %s указывал на #%s, а последнее "
                    "сообщение — #%s (сообщения удаляли). Указатель прижат.",
                    self.session_id, old_id, clamped,
                )
                await db.commit()
            return _read_target(entry).state

    async def next_messages(self, limit: int) -> list[hm.MemoryMessage]:
        if not self._alive():
            return []  # чат удалён: с тем же id может жить уже другой чат
        async with AsyncSessionLocal() as db:
            session = await db.get(models.ChatSession, self.session_id)
            if session is None:
                return []
            before = await _window_start_id(db, self.session_id, self.window)
            if before == 0:
                return []  # чат короче окна — модель и так видит его целиком
            target = _read_target(await _summary_entry(db, self.session_id))
            if await _count_between(db, self.session_id, target.pointer, before) < self.threshold:
                # Пересборка догнала бэклог настолько, насколько его догнал бы
                # обычный сводчик: остаток меньше порога ждёт и у него. Подменяем
                # сейчас, без вызова модели, — иначе старая сводка жила бы до тех
                # пор, пока в чате не наберётся ещё summary_every сообщений.
                # Задание «Пересобрать» подменяет снимок само, в конце.
                if (self.mode != "rebuild" and target.rebuilding
                        and target.state and target.pointer):
                    adopt_summary(target.entry, target.state, target.pointer,
                                  updated_by=self.mode)
                    await db.commit()
                return []  # ещё рано — копим события
            return await load_memory_messages(db, session, target.pointer, before, limit=limit)

    async def commit(self, batch: hm.Batch, new_state: str) -> bool:
        """
        Записать снимок пакета: в буфер пересборки или в живой снимок. False —
        кусок изменился, пока модель считала: не пишется ни снимок, ни факты.
        """
        async with AsyncSessionLocal() as db:
            # Пока модель считала, сообщения куска могли удалить или переписать,
            # а освободившийся id — занять новый ответ (SQLite отдаёт id удалённых
            # последних строк заново). Прижатие указателя этого не ловит: чат
            # снова доходит до того же id. Тогда указатель лёг бы на текст,
            # которого снимок не видел, и окно выбросило бы его из контекста
            # навсегда. Кусок сверяется целиком; не совпал — пакет не пишется.
            now = {
                mid: hash(content or "")
                for mid, content in (await db.execute(
                    select(models.Message.id, models.Message.content).where(
                        models.Message.session_id == self.session_id,
                        models.Message.id >= batch.first_id,
                        models.Message.id <= batch.last_id,
                        _has_content(),
                    )
                )).all()
            }
            if now != {m.id: hash(m.text or "") for m in batch.messages}:
                logger.info("Кусок памяти чата %s (#%s–#%s) изменился, пока модель считала: "
                            "пакет не записан", self.session_id, batch.first_id, batch.last_id)
                return False
            entry = await _summary_entry(db, self.session_id)
            created = entry is None
            if created:
                entry = models.HoraeEntry(session_id=self.session_id, category="summary")
                db.add(entry)
            target = _read_target(None if created else entry)
            # Пересборка продолжается, пока в бэклоге есть сообщения; догнала —
            # подмена целиком. Не раньше: у сводки из импорта указатель
            # неизвестен, и подмена «по указателю» случалась после первого же
            # куска — хроника всего чата менялась на пересказ его первых
            # сообщений. Если старую запись удалили, пока шёл пакет, пересобранный
            # снимок валиден сам по себе (покрывает всё до last_id) и
            # записывается сразу.
            if target.rebuilding and not created and (
                    self.mode == "rebuild" or await self._has_more(db, batch.last_id)):
                meta = dict(entry.meta or {})
                buffer = meta.get("rebuild") if isinstance(meta.get("rebuild"), dict) else {}
                meta["rebuild"] = {**buffer, "content": new_state, "last_message_id": batch.last_id}
                meta.pop("last_error", None)  # запись удалась — пауза после сбоя не нужна
                entry.meta = meta
            else:
                adopt_summary(entry, new_state, batch.last_id,
                              updated_by=self.mode, warnings=self.manager.warnings)
                if target.paused is not None:
                    # Остановленная пересборка ждёт «Продолжить»: живой снимок
                    # обновлён, а её буфер остаётся как был (adopt_summary
                    # убирает буфер — он считает, что снимок его заменил).
                    entry.meta = {**entry.meta, "rebuild": target.paused}
            if not self._alive():
                # Чат удалили, пока модель считала, и его id мог уже достаться
                # новому чату — с той же перепиской под теми же id, если чат
                # загрузили заново: сверка куска выше это пропускает. Выход без
                # commit откатывает всё, что сессия успела сбросить в БД.
                return False
            await db.commit()
            _turn_errors.pop(self.session_id, None)
            # Факты — только из сообщений новее уже разобранных. Снимок можно
            # начать заново (удалили запись «Память чата (авто)», пересборка), а
            # факты при этом остаются: повторный разбор того же куска давал
            # пересказанные другими словами дубли, и они занимали места в отборе.
            # «Разобраны до» — наибольшее из отметки meta.facts_upto (её ставит
            # удачный разбор, даже без единого факта) и источника фактов.
            facts_upto, gap = 0, []
            if self.want_facts:
                facts_upto = max(int_or_zero((entry.meta or {}).get("facts_upto")), (
                    await db.execute(
                        select(func.max(models.HoraeFact.source_message_id))
                        .where(models.HoraeFact.session_id == self.session_id)
                    )).scalar() or 0)
                gap = await self._facts_gap(db, batch, facts_upto)

        # Вызов модели — вне сессии БД (может занять десятки секунд).
        facts = await self._extract_facts(gap + list(batch.messages), facts_upto) \
            if self.want_facts else None
        if not self._alive():
            # Чат удалили, пока модель искала факты: снимок уже записан (и
            # удалён вместе с чатом), а факты под его id унаследовал бы новый чат.
            return True
        async with AsyncSessionLocal() as db:
            # Факты — после коммита снимка и не бросают исключений: их сбой не
            # должен откатить уже посчитанный (и оплаченный) снимок.
            if facts:
                await horae_recall.store_facts(db, self.session_id, facts, batch.last_id,
                                               self.connection)
            if facts is not None:
                # Разбор удался (пусть и без фактов): отметка идёт вперёд, и
                # следующий пакет не пошлёт эти сообщения модели фактов снова.
                # Только вперёд: пакеты пересборки идут с начала чата, и их
                # «разобрано до» меньше давно разобранного.
                entry = await _summary_entry(db, self.session_id)
                meta = dict(entry.meta or {}) if entry is not None else {}
                if entry is not None and int_or_zero(meta.get("facts_upto")) < batch.last_id:
                    entry.meta = {**meta, "facts_upto": batch.last_id}
                    await db.commit()
            # Пока модель считала, сообщения куска могли удалить: тогда указатель
            # (и факты из них) прижимаются так же, как при удалении.
            if await clamp_summary_pointer(db, self.session_id) is not None:
                await db.commit()
        return True

    async def _facts_gap(self, db, batch: hm.Batch, facts_upto: int) -> list[hm.MemoryMessage]:
        """
        Сообщения до пакета, которые ещё не разобраны фактами: между отметкой
        «разобраны до» и началом пакета, не больше _FACTS_GAP_BATCHES пакетов.

        ПОЧЕМУ: снимок коммитится ДО запроса фактов, и если ответ модели фактов
        не пришёл (сбой провайдера, «Остановить», остановка сервера посреди
        запроса), факты этого пакета раньше не извлекались уже никогда:
        следующий пакет брал только свои сообщения новее отметки (финальное
        ревью, сервис M1). Теперь пропуск доизвлекает следующий пакет. Предел —
        чтобы включение фактов в давно идущем чате не отправило модели
        фактов весь его бэклог разом (старая переписка, как и раньше, без
        фактов).
        """
        if facts_upto >= batch.first_id - 1:
            return []
        session = await db.get(models.ChatSession, self.session_id)
        if session is None:
            return []
        cap = max(1, self.manager.config.batch_size) * _FACTS_GAP_BATCHES
        lowest = (await db.execute(
            select(models.Message.id).where(
                models.Message.session_id == self.session_id,
                models.Message.id > facts_upto,
                models.Message.id < batch.first_id,
                _has_content(),
            ).order_by(models.Message.id.desc()).offset(cap - 1).limit(1)
        )).scalar()
        after = max(facts_upto, (lowest or 0) - 1)
        return await load_memory_messages(db, session, after, batch.first_id)

    async def _has_more(self, db, after_id: int) -> bool:
        """Остались ли после пакета сообщения старше окна."""
        before = await _window_start_id(db, self.session_id, self.window)
        return before != 0 and await _count_between(db, self.session_id, after_id, before) > 0

    async def _extract_facts(self, messages: list[hm.MemoryMessage],
                             facts_upto: int) -> list[str] | None:
        """
        Атомарные факты (слой 3 Horae) из сообщений новее facts_upto: пропуск
        до пакета (_facts_gap) и сам пакет. → факты; None — разбор не удался
        (сбой модели, отмена прогона), и эти сообщения доизвлечёт следующий
        пакет. Запрос идёт через manager.call: пауза между запросами и повторы
        при сбоях провайдера — общие со слиянием, лимит частоты не различает
        виды запросов.

        В запрос — самые свежие строки в пределах бюджета пакета (batch_max_chars):
        пропуск вместе с пакетом бывает вдвое длиннее пакета, а пакет важнее.
        """
        max_chars = self.manager.config.batch_max_chars
        lines = [line for m in messages
                 if m.id > facts_upto and (line := hm.normalize_message(m, max_chars))]
        kept, size = [], 0
        for line in reversed(lines):
            size += len(line) + 2
            if kept and size > max_chars:
                break
            kept.append(line)
        if not kept:
            return []
        try:
            return horae_recall.parse_facts(await self.manager.call([
                {"role": "system", "content": horae_recall.FACTS_PROMPT},
                {"role": "user", "content": "[Новые события]\n" + "\n\n".join(reversed(kept))},
            ]))
        except hm.MemoryCancelled:
            return None  # «Остановить»/сброс: без запроса, факты — со следующим пакетом
        except Exception:  # noqa: BLE001 — без фактов снимок всё равно полезен
            logger.exception("Факты чата %s не извлеклись", self.session_id)
            return None


# ============================================================================
# Занятость чата и ежеходный инкремент (§6.4)
# ============================================================================
# Один прогон памяти на чат за раз: второй посчитал бы те же сообщения и
# записал бы снимок поверх первого. Замок общий у ежеходного инкремента,
# заданий пересборки и сброса; _busy — чаты, где прогон идёт прямо сейчас
# (main видит его как _summary_running).
_locks: dict[int, asyncio.Lock] = {}
# Сколько вызывающих держат замок чата или ждут его через chat_lock. По нулю
# замок убирается из реестра (см. chat_lock).
_lock_users: dict[int, int] = {}
_busy: set[int] = set()


def session_lock(session_id: int) -> asyncio.Lock:
    """
    Замок чата из реестра (создаётся при первом обращении). Сервис берёт его
    только через chat_lock — она и убирает свободный замок; прямой доступ —
    для тестов, которым нужно «занять» чат снаружи.
    """
    lock = _locks.get(session_id)
    if lock is None:
        lock = _locks[session_id] = asyncio.Lock()
    return lock


@asynccontextmanager
async def chat_lock(session_id: int):
    """
    Держать замок чата на время блока; на выходе свободный замок без
    ожидающих убирается из реестра.

    ПОЧЕМУ убирать. asyncio.Lock привязывается к циклу событий при первом
    ожидании, а реестр жил вечно: id чатов повторяются (SQLite отдаёт id
    удалённого чата новому), и замок, однажды ожидавшийся в другом цикле
    (тесты гоняют каждый в своём), дал бы «RuntimeError: … is bound to a
    different event loop». Да и копить замки всех когда-либо тронутых чатов
    незачем. ПОЧЕМУ считать вызывающих, а не смотреть на сам замок: пока
    кто-то ждёт, lock.locked() в момент выхода бывает False, а удалить замок
    из-под ожидающего нельзя — следующий вызывающий получил бы новый замок,
    и два прогона пошли бы над одним чатом разом.
    """
    lock = session_lock(session_id)
    _lock_users[session_id] = _lock_users.get(session_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        left = _lock_users.get(session_id, 1) - 1
        if left > 0:
            _lock_users[session_id] = left
        else:
            _lock_users.pop(session_id, None)
            if not lock.locked() and _locks.get(session_id) is lock:
                del _locks[session_id]


def is_busy(session_id: int) -> bool:
    lock = _locks.get(session_id)
    return session_id in _busy or (lock is not None and lock.locked())


def _make_run(session_id: int, deps: MemoryDeps, *, mode: str, threshold: int, ui,
              ui_value: dict, connection: dict, alive: Callable[[], bool] | None = None,
              **overrides) -> tuple[hm.HierarchicalMemoryManager, dict, DbBatchSource]:
    """
    Всё для одного прогона → (менеджер, подключение фоновых вызовов, источник).
    Один помощник на ежеходный проход и на задание: сборка была продублирована
    почти дословно, и правка одной копии (окно, флаг фактов) разошлась бы с
    другой. overrides — размер пакета и пауза задания (см. build_manager).
    """
    manager, bg_conn = build_manager(connection, ui_value, deps, **overrides)
    source = DbBatchSource(
        session_id, mode=mode, threshold=threshold, manager=manager,
        want_facts=ui_flag(ui, "horae_facts"), connection=connection,
        window=_memory_window(ui_value), alive=alive,
    )
    return manager, bg_conn, source


# ---------------------------------------------------------------------------
# Ошибка ежеходного прохода и пауза после неё
# ---------------------------------------------------------------------------
# Пауза после сбоя ежеходного прохода: 10 мин · 2^(failures−1), не больше 6 ч.
_TURN_RETRY_BASE = timedelta(minutes=10)
_TURN_RETRY_MAX = timedelta(hours=6)
# Ошибки ежеходного прохода чатов, у которых записи снимка ещё нет (упал самый
# первый проход): meta хранить негде, а заводить пустую запись ради ошибки —
# значит показать во вкладке «снимок есть» там, где его нет. Живёт в процессе:
# после перезапуска будет одна попытка, и ошибка запишется заново.
_turn_errors: dict[int, dict] = {}
# Событие отмены идущего ежеходного прохода чата: сброс памяти (purge) его
# ставит, и проход останавливается после текущего запроса к модели, а не
# доделывает до шести пакетов (см. purge).
_turn_cancels: dict[int, asyncio.Event] = {}


def describe_error(err: BaseException) -> tuple[str, str]:
    """
    Ошибка прогона памяти → (вид, русский текст для вкладки «Память»). Одни
    тексты у статуса задания (job.error) и у ошибки ежеходного прохода
    (snapshot.last_error).
    """
    if isinstance(err, hm.MemoryLLMError):
        return err.kind, err.message
    if isinstance(err, hm.SnapshotValidationError):
        return "validation", "модель вернула снимок не по схеме: " + "; ".join(err.problems)
    if isinstance(err, hm.SourceConflictError):
        return "conflict", "переписка менялась во время сжатия, повторите"
    return "internal", f"внутренняя ошибка: {err}"


def _parse_utc(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def turn_retry_after(error) -> datetime | None:
    """Когда ежеходный проход попробует снова после ошибки error (None — ошибки нет)."""
    if not isinstance(error, dict):
        return None
    at = _parse_utc(error.get("at"))
    if at is None:
        return None
    failures = max(1, int_or_zero(error.get("failures")))
    # Степень ограничена: 2^(failures−1) при сотнях сбоев переполнил бы timedelta.
    return at + min(_TURN_RETRY_MAX, _TURN_RETRY_BASE * 2 ** min(failures - 1, 16))


def _turn_error(entry, session_id: int) -> dict | None:
    """Последняя ошибка ежеходного прохода: из meta записи, а без записи — из процесса."""
    if entry is None:
        return _turn_errors.get(session_id)
    error = (entry.meta or {}).get("last_error")
    return error if isinstance(error, dict) else None


async def _record_turn_error(session_id: int, err: BaseException) -> None:
    """
    Запомнить сбой ежеходного прохода: meta.last_error = {message, kind, at,
    failures}. ПОЧЕМУ: указатель при сбое стоит, бэклог по-прежнему выше
    порога, и КАЖДЫЙ следующий ход повторял тот же платный провал (обрыв
    лимитом — три полноразмерных вызова за ход), а видна ошибка была только
    в логе: вкладка показывала лишь растущее «ждут сжатия» (финальное ревью).
    Теперь следующий проход ждёт паузу (turn_retry_after), а статус отдаёт
    ошибку и время следующей попытки.
    """
    kind, message = describe_error(err)
    async with AsyncSessionLocal() as db:
        if await db.get(models.ChatSession, session_id) is None:
            return  # чат удалили посреди прохода: его id может достаться новому
        entry = await _summary_entry(db, session_id)
        previous = _turn_error(entry, session_id) or {}
        error = {"message": message, "kind": kind, "at": _utc_iso(),
                 "failures": int_or_zero(previous.get("failures")) + 1}
        if entry is None:
            _turn_errors[session_id] = error
            return
        entry.meta = {**(entry.meta or {}), "last_error": error}
        await db.commit()


async def _clear_turn_error(session_id: int) -> None:
    """Проход (или задание) прошёл без ошибки — старая ошибка и пауза больше не нужны."""
    _turn_errors.pop(session_id, None)
    async with AsyncSessionLocal() as db:
        entry = await _summary_entry(db, session_id)
        if entry is not None and "last_error" in (entry.meta or {}):
            entry.meta = {k: v for k, v in entry.meta.items() if k != "last_error"}
            await db.commit()


async def run_incremental(session_id: int, deps: MemoryDeps, *, max_batches: int,
                          on_progress: Callable[[hm.ScanProgress], None] | None = None,
                          ) -> hm.ScanResult | None:
    """
    Ежеходный проход: свернуть в снимок то, что вышло из окна, не больше
    max_batches пакетов. None — проход не делался: чат занят (идёт задание или
    другой проход), авто-сводка выключена или после ошибки прошлого прохода
    ещё не вышла пауза (turn_retry_after; ручные «Догнать»/«Пересобрать» её
    не ждут).

    Кусок, изменившийся под моделью, не пишется и не пересобирается сразу
    (retry_conflicts=False): следующий ход начнёт его заново. Исключения ядра
    (ошибка API, снимок не по схеме) записываются в meta.last_error и уходят
    вызывающему — main их логирует; указатель стоит, и пакет повторит проход
    после паузы.
    """
    if is_busy(session_id):
        return None
    async with chat_lock(session_id):
        _busy.add(session_id)
        cancel = _turn_cancels[session_id] = asyncio.Event()
        try:
            async with AsyncSessionLocal() as db:
                # Выключатель (вкладка «Память»): settings/ui -> auto_summary=false.
                ui, ui_value = await _ui_setting(db)
                if not ui_flag(ui, "auto_summary"):
                    return None
                # Историю этого чата сжимают свёртки Хроники: снимок стоит, чтобы
                # не платить за второе сжатие той же переписки.
                if (await chat_compression(db, session_id))["engine"] != "snapshot":
                    return None
                failed = _turn_error(await _summary_entry(db, session_id), session_id)
                retry_at = turn_retry_after(failed)
                if retry_at is not None and datetime.now(timezone.utc) < retry_at:
                    return None
                connection = await deps.get_connection(db)
            manager, _, source = _make_run(
                session_id, deps, mode="incremental", threshold=_summary_every(ui_value),
                ui=ui, ui_value=ui_value, connection=connection)
            try:
                result = await manager.scan_and_compress_history(
                    source, on_progress=on_progress, max_batches=max_batches,
                    retry_conflicts=False, cancel=cancel)
            except Exception as err:
                try:
                    await _record_turn_error(session_id, err)
                except Exception:  # noqa: BLE001 — не заслонять исходную ошибку
                    logger.exception("Ошибка памяти чата %s не записалась", session_id)
                raise
            if failed is not None:
                await _clear_turn_error(session_id)
            return result
        finally:
            _busy.discard(session_id)
            if _turn_cancels.get(session_id) is cancel:
                del _turn_cancels[session_id]


# ============================================================================
# Задания «Пересобрать» / «Догнать» (§6.5)
# ============================================================================
JOB_MODES = ("rebuild", "catchup")
_ACTIVE = ("queued", "running")


def _utc_iso(stamp: datetime | None = None) -> str:
    """ISO UTC с «Z», как даты в остальном API: в пояс пользователя переводит браузер."""
    stamp = stamp or datetime.now(timezone.utc)
    if stamp.tzinfo is None:  # SQLite func.now() пишет naive UTC
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class JobConflict(Exception):
    """У чата уже есть задание памяти в очереди или в работе."""


class _ChatGone(Exception):
    """Чат удалили до или во время задания: памяти больше некуда писать."""


@dataclass
class MemoryJob:
    """
    Задание памяти чата. Живёт в памяти процесса (реестр _jobs): прогресс —
    для вкладки «Память», всё сделанное — уже в БД (пакеты пишутся по одному),
    так что перезапуск сервера теряет только строку статуса.
    """
    session_id: int
    mode: str                                  # rebuild | catchup
    status: str = "queued"                     # queued | running | done | error | cancelled
    processed: int = 0
    total: int = 0
    batches: int = 0
    state_tokens: int = 0
    phase: str = "merge"
    retry_in_s: float | None = None
    line: str = ""
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Размер снимка, который работает ПОСЛЕ задания (для тоста «… → N токенов»).
    # state_tokens — это цель прогона: у пересборки буфер, у пустой — ноль, и
    # тост «пересобрана: 0 сообщений → 0 токенов» при живом снимке читался как
    # «память стёрта». None — задание ещё идёт или упало.
    snapshot_tokens: int | None = None
    # queued_at — постановка в очередь; started_at — переход в running. Раньше
    # «начато» ставилось при постановке, и задание, минуты ждавшее ежеходный
    # проход, показывало во вкладке неверное время старта.
    queued_at: str = field(default_factory=_utc_iso)
    started_at: str | None = None
    finished_at: str | None = None
    # Что попросил пользователь (None — из «ui»); после старта — действующие значения.
    batch_size: int | None = None
    delay_ms: int | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    def progress(self, p: hm.ScanProgress) -> None:
        """Колбэк прогресса ядра: поля статуса и строка «[Обработано N/M сообщений | …]»."""
        self.processed, self.total, self.batches = p.processed, p.total, p.batches
        self.state_tokens, self.phase, self.retry_in_s = p.state_tokens, p.phase, p.retry_in_s
        self.line = p.line()

    def finish(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.finished_at = _utc_iso()

    def to_dict(self) -> dict:
        return {
            "mode": self.mode, "status": self.status, "processed": self.processed,
            "total": self.total, "batches": self.batches, "state_tokens": self.state_tokens,
            "phase": self.phase, "retry_in_s": self.retry_in_s, "line": self.line,
            "error": self.error, "warnings": list(self.warnings),
            "snapshot_tokens": self.snapshot_tokens,
            "queued_at": self.queued_at, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# Последнее задание каждого чата. Завершённое остаётся здесь до следующего
# старта: вкладка «Память» показывает, чем кончилось (готово, ошибка, отмена).
# Удаление чата убирает его задание сразу (forget_job): id достанется новому чату.
_jobs: dict[int, MemoryJob] = {}


def get_job(session_id: int) -> MemoryJob | None:
    return _jobs.get(session_id)


def _owns_chat(job: MemoryJob) -> bool:
    """
    Задание всё ещё отвечает за свой чат. Проверка «чат с таким id есть» тут
    не годится: SQLite отдаёт id удалённого последнего чата следующему новому.
    Сравнивается сам объект в реестре — его убирает forget_job при удалении чата.
    """
    return _jobs.get(job.session_id) is job


async def start_job(session_id: int, deps: MemoryDeps, *, mode: str, resume: bool = False,
                    batch_size: int | None = None, delay_ms: int | None = None) -> MemoryJob:
    """
    Поставить задание в очередь чата → MemoryJob в статусе queued.

    rebuild — снимок собирается с нуля в буфер meta["rebuild"], старый снимок
    работает до атомарной подмены в конце (resume=True — продолжить буфер
    прерванной пересборки); catchup — весь бэклог сворачивается в живой
    снимок. JobConflict — у чата уже есть задание в очереди или в работе.

    Между проверкой занятости и записью в реестр нет ни одного await: иначе
    два одновременных запроса оба прошли бы проверку и запустили два задания
    над одним чатом.
    """
    if mode not in JOB_MODES:
        raise ValueError(f"неизвестный режим задания памяти: {mode!r}")
    current = _jobs.get(session_id)
    if current is not None and current.status in _ACTIVE:
        raise JobConflict(session_id)
    job = MemoryJob(session_id=session_id, mode=mode, batch_size=batch_size, delay_ms=delay_ms)
    _jobs[session_id] = job
    job.task = asyncio.create_task(_run_job(job, deps, resume=resume))
    return job


def cancel_job(session_id: int) -> MemoryJob | None:
    """
    Остановить задание чата → это задание; None — останавливать нечего.

    Идущее задание доводит начатый запрос к модели и останавливается:
    оплаченный пакет не пропадает (у пересборки он остаётся в буфере, её
    можно продолжить), а паузы между запросами и перед повторами прерываются
    сразу (ядро, _pause). Остановленную пересборку ежеходные проходы не
    продолжают — только «Продолжить» (см. _read_target). Задание в очереди
    снимается сразу — ждать замка ему незачем, а ежеходный проход может
    держать его минуты.
    """
    job = _jobs.get(session_id)
    if job is None or job.status not in _ACTIVE:
        return None
    job.cancel.set()
    if job.status == "queued":
        # Статус ставится здесь, а не в задаче: задача, отменённая до своего
        # первого шага, не выполняет ни строчки.
        job.finish("cancelled")
        if job.task is not None:
            job.task.cancel()
    return job


def forget_job(session_id: int) -> MemoryJob | None:
    """
    Чат удаляют: остановить его задание и убрать из реестра → это задание
    (None — заданий у чата не было).

    SQLite отдаёт id удалённого последнего чата следующему новому. Останься
    задание в реестре, новый чат с тем же id показывал бы во вкладке «Память»
    чужое «чат удалён» или «готово», а пока старое задание в очереди или в
    работе — получал бы 409 на «Пересобрать». Идущее задание доводит начатый
    пакет на своём объекте MemoryJob, но записать его уже не может (источник
    сверяется с реестром через _owns_chat) и завершается ошибкой «чат удалён».
    Ошибка ежеходного прохода чата без записи снимка (_turn_errors) уходит
    туда же — по той же причине.
    """
    cancel_job(session_id)
    _turn_errors.pop(session_id, None)
    return _jobs.pop(session_id, None)


async def wait_job(session_id: int) -> None:
    """Дождаться конца задания чата (тесты, сброс памяти). Исключений задачи не бросает."""
    job = _jobs.get(session_id)
    if job is not None and job.task is not None and not job.task.done():
        await asyncio.wait({job.task})


# Одно задание памяти на процесс: остальные ждут в очереди (queued). ПОЧЕМУ:
# пауза между запросами (delay_ms) действует внутри ОДНОГО прогона, а задания
# разных чатов шли разом — пять «Пересобрать» подряд давали около трёх
# запросов в секунду на общий ключ провайдера: 429 у заданий и у ходов всех
# пользователей (финальное ревью, API M1). Замок заводится на цикл событий:
# asyncio-примитив привязывается к циклу при первом ожидании, а тесты гоняют
# каждый в своём цикле (см. chat_lock).
_job_gate: tuple[object, asyncio.Lock] | None = None


def _jobs_gate() -> asyncio.Lock:
    global _job_gate
    loop = asyncio.get_running_loop()
    if _job_gate is None or _job_gate[0] is not loop:
        _job_gate = (loop, asyncio.Lock())
    return _job_gate[1]


async def _run_job(job: MemoryJob, deps: MemoryDeps, *, resume: bool) -> None:
    """Задача задания: дождаться очереди и замка чата, выполнить, перевести ошибку в текст для UI."""
    try:
        # Сначала общая очередь заданий, потом замок чата: задание, ждущее
        # очереди, не держит свой чат, и ежеходные проходы в нём идут как шли.
        async with _jobs_gate():
            # Замок общий с ежеходным инкрементом: пока идёт задание, проходы после
            # ходов пропускаются, а задание ждёт конца уже начатого прохода (queued).
            async with chat_lock(job.session_id):
                job.status = "running"
                job.started_at = _utc_iso()
                _busy.add(job.session_id)
                try:
                    await _execute_job(job, deps, resume=resume)
                finally:
                    _busy.discard(job.session_id)
    except asyncio.CancelledError:
        # Снято из очереди (cancel_job) или сервер останавливается. Готовые
        # пакеты уже в БД; отмену не глотаем — её ждёт тот, кто отменял.
        if job.status in _ACTIVE:
            job.finish("cancelled")
        raise
    except _ChatGone:
        job.finish("error", "чат удалён — памяти больше некуда записываться")
    except (hm.MemoryLLMError, hm.SnapshotValidationError, hm.SourceConflictError) as err:
        job.finish("error", describe_error(err)[1])
    except Exception as err:  # noqa: BLE001 — задание не должно падать молча
        logger.exception("Задание памяти чата %s (%s) упало", job.session_id, job.mode)
        job.finish("error", describe_error(err)[1])


async def _execute_job(job: MemoryJob, deps: MemoryDeps, *, resume: bool) -> None:
    """
    Тело задания (замок чата уже взят). Порог — 1 сообщение: задание — ручная
    команда «сверни всё», копить summary_every тут незачем. Выключатель
    «Авто-сводка сюжета» заданий не касается — он про ежеходный проход.
    """
    sid = job.session_id
    async with AsyncSessionLocal() as db:
        if await db.get(models.ChatSession, sid) is None or not _owns_chat(job):
            raise _ChatGone
        ui, ui_value = await _ui_setting(db)
        connection = await deps.get_connection(db)
        manager, _, source = _make_run(
            sid, deps, mode=job.mode, threshold=1, ui=ui, ui_value=ui_value,
            connection=connection, alive=lambda: _owns_chat(job),
            batch_size=job.batch_size, delay_ms=job.delay_ms)
        job.batch_size, job.delay_ms = manager.config.batch_size, manager.config.delay_ms
        if job.mode == "rebuild":
            await _open_rebuild_buffer(db, sid, job, resume=resume)
    try:
        result = await manager.scan_and_compress_history(
            source, cancel=job.cancel, on_progress=job.progress, retry_conflicts=True)
    finally:
        job.warnings = list(manager.warnings)
    job.processed, job.batches, job.state_tokens = (
        result.processed, result.batches, result.state_tokens)
    async with AsyncSessionLocal() as db:
        # Чат удалили посреди задания: источник просто перестал отдавать
        # пакеты, и без этой проверки задание отчиталось бы «готово». Одного
        # «чата с таким id нет» мало — id мог уже достаться новому чату, и
        # тогда пересборка подменила бы ЕГО снимок. Удаление важнее отмены:
        # удаление чата само останавливает задание (forget_job).
        if await db.get(models.ChatSession, sid) is None or not _owns_chat(job):
            raise _ChatGone
        if result.status == "cancelled":
            # Буфер пересборки остаётся — её можно продолжить, но только
            # кнопкой: остановленный буфер ежеходные проходы не трогают.
            if job.mode == "rebuild":
                await _pause_rebuild_buffer(db, sid)
            job.snapshot_tokens = await _live_snapshot_tokens(db, sid)
            job.finish("cancelled")
            return
        if job.mode == "rebuild" and not await _adopt_rebuild_buffer(db, sid, manager.warnings):
            # Без строки UI показал бы «готово» при нетронутом снимке, и
            # нажатие «Пересобрать» выглядело бы так, будто ничего не сделало.
            job.warnings.append(EMPTY_REBUILD_WARNING)
        elif result.processed == 0:
            # То же для задания без записи памяти: «готово: 0 сообщений → 0
            # токенов» читалось как «память стёрта». Объяснение — по причине.
            whole_chat = await _window_start_id(db, sid, _memory_window(ui_value)) == 0
            job.warnings.append(NOTHING_TO_COMPRESS_WARNING if whole_chat or job.mode == "rebuild"
                                else NOTHING_TO_CATCH_UP_WARNING)
        job.snapshot_tokens = await _live_snapshot_tokens(db, sid)
    # Задание прошло без ошибки — ошибка ежеходного прохода и пауза после неё
    # больше не нужны («Догнать» — ручной повтор, см. статус last_error).
    await _clear_turn_error(sid)
    job.finish("done")


async def _live_snapshot_tokens(db, session_id: int) -> int:
    """Размер снимка, который сейчас идёт в контекст (0 — снимка нет)."""
    entry = await _summary_entry(db, session_id)
    return snapshot_tokens(entry.content if entry is not None else "")


async def _pause_rebuild_buffer(db, session_id: int) -> None:
    """Пометить буфер пересборки остановленным (meta.rebuild.paused) — см. _read_target."""
    entry = await _summary_entry(db, session_id)
    meta = dict(entry.meta or {}) if entry is not None else {}
    if isinstance(meta.get("rebuild"), dict):
        meta["rebuild"] = {**meta["rebuild"], "paused": True}
        entry.meta = meta
        await db.commit()


async def _open_rebuild_buffer(db, session_id: int, job: MemoryJob, *, resume: bool) -> None:
    """
    Пустой буфер пересборки в meta["rebuild"]; resume — оставить начатый
    (и снять с него пометку «остановлен»: «Продолжить» — тот самый случай,
    когда буфер снова продолжают).

    Старый снимок при этом продолжает работать: пока буфер копится, в
    контекст идёт он, а окно верит его указателю. Иначе на время пересборки
    середина чата не была бы ни в пересказе, ни дословно. Записи нет — беречь
    нечего: пакеты пишутся сразу в живой снимок (запись создаст первый commit).
    """
    entry = await _summary_entry(db, session_id)
    if entry is None:
        return
    meta = dict(entry.meta or {})
    if resume and isinstance(meta.get("rebuild"), dict):
        if meta["rebuild"].get("paused"):
            meta["rebuild"] = {k: v for k, v in meta["rebuild"].items() if k != "paused"}
            entry.meta = meta
            await db.commit()
        return
    meta["rebuild"] = {"content": "", "last_message_id": 0, "manual": True,
                       "started_at": _utc_iso(), "batch_size": job.batch_size}
    entry.meta = meta
    await db.commit()


# Предупреждение задания «Пересобрать», когда буфер так и остался пустым.
EMPTY_REBUILD_WARNING = "пересборка не нашла сообщений старше окна — прежний снимок оставлен"
# Задание без записи памяти: весь чат в окне (или пересобирать без снимка нечего)…
NOTHING_TO_COMPRESS_WARNING = "сжимать нечего — весь чат в окне"
# …или «Догнать», когда всё старше окна уже в снимке.
NOTHING_TO_CATCH_UP_WARNING = "догонять нечего — всё, что старше окна, уже в снимке"


async def _adopt_rebuild_buffer(db, session_id: int, warnings) -> bool:
    """
    Атомарная подмена: снимок из буфера становится живым одним коммитом —
    даже если хвост короче summary_every (ежеходный проход ждал бы его).
    → False, если подменять было нечем (буфер пуст), иначе True.

    Буфер пуст (указатель 0) — сжимать было нечего: весь чат в окне. Тогда
    прежний снимок остаётся, а буфер убирается, иначе ежеходные проходы так
    и писали бы в него. Затирать память пустым снимком молча нельзя — для
    этого есть явный «Сбросить память»; что снимок оставлен, задание пишет в
    свои предупреждения (EMPTY_REBUILD_WARNING).
    """
    entry = await _summary_entry(db, session_id)
    meta = dict(entry.meta or {}) if entry is not None else {}
    buffer = meta.get("rebuild")
    if not isinstance(buffer, dict):
        return True  # записи не было: пакеты уже легли в живой снимок
    pointer = int_or_zero(buffer.get("last_message_id"))
    if pointer:
        adopt_summary(entry, str(buffer.get("content") or ""), pointer,
                      updated_by="rebuild", warnings=warnings)
    else:
        meta.pop("rebuild")
        entry.meta = meta
    await db.commit()
    return bool(pointer)


# ============================================================================
# Статус, сброс, экспорт (§6.6)
# ============================================================================
async def _count_rows(db, model, session_id: int) -> int:
    return (await db.execute(
        select(func.count(model.id)).where(model.session_id == session_id)
    )).scalar() or 0


async def status(db, session_id: int, connection: dict | None = None) -> dict:
    """
    Всё, что показывает блок «Мастер-память»: снимок, буфер, бэклог, факты,
    задание, ошибку ежеходного прохода.

    :param connection: подключение (main передаёт настройки «Подключения»):
        по модели памяти считается settings.max_snapshot_tokens — наибольший
        бюджет снимка, который она успевает написать за ответ. None — не
        считается (max_snapshot_tokens: null, как у неизвестной LiteLLM модели).
    """
    _, ui_value = await _ui_setting(db)
    config = memory_config(ui_value)
    window = _memory_window(ui_value)
    entry = await _summary_entry(db, session_id)
    meta = dict(entry.meta or {}) if entry is not None else {}
    content = (entry.content or "") if entry is not None else ""
    # Размер — по тексту, а не из meta.tokens: снимок правят руками во
    # вкладке «Память», и записанное при сжатии число тогда устаревает.
    tokens = snapshot_tokens(content)
    # Буфер — та же цель, что у источника пакетов (_read_target), а не только
    # явный meta.rebuild. Сводка старого формата пересобирается неявно, с нуля:
    # бэклог (_pending_count) считается от нуля, и без буфера в статусе
    # вкладка показала бы «учтено до #400» и «ждут сжатия 850» без объяснения.
    # Остановленный буфер (paused) показывается тоже — он ждёт «Продолжить»,
    # хотя бэклог уже считается от живого снимка.
    target = _read_target(entry)
    buffer = meta.get("rebuild") if isinstance(meta.get("rebuild"), dict) else {}
    staged = target.paused if target.paused is not None else (
        {"last_message_id": target.pointer, "content": target.state} if target.rebuilding else None)
    warnings = list(meta.get("warnings") or [])
    max_budget = (max_snapshot_tokens(memory_output_limit(connection))
                  if connection is not None else None)
    if max_budget is not None and config.snapshot_tokens > max_budget:
        # Снимок, которого модель памяти не успевает написать за ответ,
        # обрывается на каждом обновлении. Прогоны и так работают с этим
        # потолком (build_manager), но бюджет в настройках его не показывал.
        warnings.append(
            f"модель памяти успевает написать снимок не больше ≈{hm._fmt_int(max_budget)} "
            f"токенов, а бюджет — {hm._fmt_int(config.snapshot_tokens)}: память работает "
            "с меньшим бюджетом — уменьшите его или выберите модель с длинным выводом")
    last_error = _turn_error(entry, session_id)
    retry_after = turn_retry_after(last_error)
    updated_at = entry.updated_at if entry is not None else None
    job = _jobs.get(session_id)
    return {
        "job": job.to_dict() if job else None,
        # Кто сжимает историю этого чата: snapshot / horae / off (см. chat_compression).
        "compression": await chat_compression(db, session_id),
        "snapshot": {
            "exists": entry is not None,
            "tokens": tokens,
            "budget": config.snapshot_tokens,
            "covered_upto": summary_last_id(entry),
            "schema": meta.get("schema"),
            "structured": hm.is_structured(content),
            "updated_at": _utc_iso(updated_at) if updated_at else None,
            "over_budget": tokens > config.snapshot_tokens,
            "warnings": warnings,
            # Сбой ежеходного прохода и когда он попробует снова; ручные
            # «Догнать»/«Пересобрать» паузу не ждут.
            "last_error": None if last_error is None else {
                k: last_error.get(k) for k in ("message", "kind", "at", "failures")},
            "retry_after": _utc_iso(retry_after) if retry_after else None,
        },
        "staging": None if staged is None else {
            "last_message_id": int_or_zero(staged.get("last_message_id")),
            "tokens": snapshot_tokens(staged.get("content")),
            "manual": bool(buffer.get("manual")),
            "started_at": buffer.get("started_at"),
            "paused": target.paused is not None,
        },
        "backlog": {
            "pending": await _pending_count(db, session_id, window),
            "window": window,
            "messages_total": await _count_rows(db, models.Message, session_id),
        },
        "facts": {"count": await _count_rows(db, models.HoraeFact, session_id)},
        "settings": {"batch_size": config.batch_size, "delay_ms": config.delay_ms,
                     "snapshot_tokens": config.snapshot_tokens,
                     "max_snapshot_tokens": max_budget},
    }


async def purge(db, session_id: int) -> dict:
    """
    Сбросить память чата: снимок (с буфером пересборки) и все атомарные факты.
    Сообщения не трогаются. После сброса указатель = 0, и окно ничего не
    выбрасывает из контекста, пока память не соберётся заново.

    Сначала задание останавливается и дожидается: иначе его следующий пакет
    воскресил бы только что стёртую память. Удаление — под замком чата по той
    же причине: ежеходный проход, начатый до сброса, записал бы снимок со
    старым содержимым поверх пустоты.

    Ждать приходится недолго: паузы между запросами и перед повторами отмена
    прерывает сразу (ядро, _pause), а идущий ежеходный проход получает ту же
    отмену (_turn_cancels) и останавливается после текущего запроса к модели.
    Раньше сброс ждал минуты бэкоффа по Retry-After и все пакеты прохода — до
    шести платных слияний, результат которых тут же стирался.
    """
    cancel_job(session_id)
    turn = _turn_cancels.get(session_id)
    if turn is not None:
        turn.set()
    await wait_job(session_id)
    async with chat_lock(session_id):
        snapshot = await db.execute(sql_delete(models.HoraeEntry).where(
            models.HoraeEntry.session_id == session_id,
            models.HoraeEntry.category == "summary",
        ))
        facts = await db.execute(sql_delete(models.HoraeFact).where(
            models.HoraeFact.session_id == session_id))
        await db.commit()
        _turn_errors.pop(session_id, None)
    return {"snapshot_deleted": bool(snapshot.rowcount), "facts_deleted": int(facts.rowcount or 0)}


# Всё, кроме букв и цифр латиницы и кириллицы, в имени файла — «_»: имя уходит
# в заголовок Content-Disposition и в файловую систему пользователя, где «/»,
# «:» или «?» либо ломают сохранение, либо молча меняются браузером.
_FILENAME_JUNK_RE = re.compile(r"[^0-9A-Za-zА-Яа-яЁё]+")
# Потолок названия в имени файла. Название чата вмещает до 300 символов, а
# имя длиннее 255 — предел Windows и большинства файловых систем — браузер
# обрежет или не сохранит вовсе.
_FILENAME_SLUG_MAX = 80


def export_filename(title, session_id: int) -> str:
    """memory-<очищенное название, ≤ 80 символов>-<id>.md; без названия — memory-<id>.md."""
    # Срез — до strip: иначе «_» на месте разреза остался бы в конце имени.
    slug = _FILENAME_JUNK_RE.sub("_", title or "")[:_FILENAME_SLUG_MAX].strip("_")
    return f"memory-{slug}-{session_id}.md" if slug else f"memory-{session_id}.md"


async def export_markdown(db, session_id: int,
                          include_facts: bool = False) -> tuple[str, str] | None:
    """Снимок как .md-файл → (имя файла, текст); None — снимка нет."""
    entry = await _summary_entry(db, session_id)
    if entry is None:
        return None
    session = await db.get(models.ChatSession, session_id)
    character = (await db.get(models.Character, session.character_id)
                 if session is not None and session.character_id else None)
    content = entry.content or ""
    meta = dict(entry.meta or {})
    facts = None
    if include_facts:
        facts = list((await db.execute(
            select(models.HoraeFact.content)
            .where(models.HoraeFact.session_id == session_id)
            .order_by(models.HoraeFact.id)
        )).scalars().all())
    title = session.title if session is not None else ""
    text = hm.render_export_markdown(
        title=title,
        character=character.name if character is not None else None,
        snapshot=content,
        covered_upto=summary_last_id(entry),
        messages_total=await _count_rows(db, models.Message, session_id),
        tokens=snapshot_tokens(content),
        budget=memory_config((await _ui_setting(db))[1]).snapshot_tokens,
        schema=meta.get("schema"),
        exported_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        facts=facts,
    )
    return export_filename(title, session_id), text
