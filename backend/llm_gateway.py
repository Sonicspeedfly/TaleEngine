"""
Единый шлюз к LLM через LiteLLM.

По умолчанию запросы идут в УЖЕ ЗАПУЩЕННЫЙ LiteLLM-прокси (например, на :4000):
именно прокси хранит ключи провайдеров и список моделей. Адрес прокси и ключ
настраиваются в интерфейсе (вкладка «Подключение») и передаются сюда как `connection`.

Если proxy выключить (use_proxy=False), LiteLLM маршрутизирует напрямую по имени
модели ('gpt-4o' -> OpenAI, 'gemini/...' -> Google и т.д.) — ключи берутся из .env.

Вся обработка — на сервере: браузер только шлёт текст и слушает токены.
"""
import logging
from typing import AsyncGenerator, Optional

import litellm

from backend import debug_log
from backend.config import settings
from backend.schemas import AttachmentIn, GenerationParams

# Не роняем запрос, если провайдер не поддерживает какой-то параметр (например top_k
# у OpenAI). LiteLLM просто отбросит лишнее.
litellm.drop_params = True

# LiteLLM по умолчанию печатает предупреждения/инфо прямо в stdout. На сервере под
# tmux/nohup поток вывода может «отвалиться», и тогда запись лога падает с
# OSError [Errno 5] Input/output error — ПРЯМО во время запроса, обрывая генерацию
# (в чате это «Ошибка генерации: [Errno 5]» и пропавший текст). Глушим болтливость
# LiteLLM, чтобы её логи не могли уронить запрос через сломанный stdout.
litellm.suppress_debug_info = True
try:
    litellm.set_verbose = False  # deprecated в новых версиях — не критично
except Exception:  # noqa: BLE001
    pass
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)


# Полное снятие настраиваемых фильтров для Gemini / Vertex AI.
# Порог "OFF" (а не "BLOCK_NONE") — САМЫЙ пермиссивный: полностью выключает фильтр,
# тогда как BLOCK_NONE лишь «не блокировать, но оценивать». Для Gemini 2.5/3 "OFF"
# и так дефолт. Ставим явно на ВСЕ настраиваемые категории (в т.ч. CIVIC_INTEGRITY).
# Останутся только неотключаемые фильтры Google (например CSAM) — их обойти нельзя.
GEMINI_SAFETY_OFF = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "OFF"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "OFF"},
]


def _attachment_data_uri(att: AttachmentIn, default_mime: str) -> str:
    """data:URI вложения (данные могут прийти и голым base64, и готовым data:URI)."""
    data = (att.data or "").strip()
    if data.startswith("data:"):
        return data
    return f"data:{att.mime or default_mime};base64,{data}"


def _content_from_attachment(att: AttachmentIn) -> dict:
    """Превращает вложение в content-блок формата OpenAI/LiteLLM."""
    kind = att.type
    mime = (att.mime or "").lower().split(";")[0].strip()
    # Старые записи: до появления типа "video" фронтенд помечал видео (и любые
    # не-картинки/не-аудио) как document — тогда бинарник декодировался как текст
    # и в контекст уходили мегабайты мусора. Перенаправляем по mime.
    if kind == "document" and mime.startswith("video/"):
        kind = "video"
    elif kind == "document" and mime.startswith("audio/"):
        kind = "audio"
    if kind == "image":
        img_mime = mime if mime.startswith("image/") else "image/jpeg"
        # format — ЯВНАЯ подсказка mime: старый конвертер LiteLLM на прокси иначе
        # может ошибиться с типом. Для картинок это тоже страховка.
        return {"type": "image_url", "image_url": {"url": att.data, "format": img_mime}}
    if kind == "video":
        # data:URI внутри image_url + ЯВНЫЙ format=video/… — LiteLLM создаёт для
        # Gemini inline_data именно как ВИДЕО (а не один кадр image/jpeg, чем грешит
        # старый конвертер на прокси). Отсюда и «слабый анализ видео».
        vmime = mime if mime.startswith("video/") else "video/mp4"
        return {"type": "image_url", "image_url": {"url": _attachment_data_uri(att, vmime), "format": vmime}}
    if kind == "audio":
        # Gemini 1.5 Pro принимает аудио НАТИВНО — Whisper не нужен.
        b64 = att.data.split(",")[-1]  # отрезаем 'data:audio/...;base64,' если он есть
        fmt = (att.mime or "audio/wav").split("/")[-1]
        return {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
    if kind == "document":
        # Word/PDF/текст: конвертируем в PDF или извлекаем текст (см. document_service).
        from backend.document_service import prepare_document

        return prepare_document(att.data, att.mime, att.name)
    raise ValueError(f"Неизвестный тип вложения: {att.type}")


def _is_media_att(att: AttachmentIn) -> bool:
    """Медиа-вложение (фото/видео/аудио), в т.ч. легаси-видео/аудио под типом document."""
    mime = (att.mime or "").lower()
    return att.type in ("image", "audio", "video") or (
        att.type == "document"
        and (mime.startswith("video/") or mime.startswith("audio/") or mime.startswith("image/"))
    )


def _media_kind_ru(att: AttachmentIn) -> str:
    mime = (att.mime or "").lower()
    if att.type == "video" or mime.startswith("video/"):
        return "видео"
    if att.type == "audio" or mime.startswith("audio/"):
        return "аудио"
    if att.type == "image" or mime.startswith("image/"):
        return "изображение"
    return "файл"


def build_user_content(text: str, attachments: list[AttachmentIn], current: bool = False):
    """Собирает контент сообщения: строка без вложений, иначе список блоков.

    Перед каждым медиа-вложением (фото/видео/аудио) добавляем текстовую пометку
    с ИМЕНЕМ файла — иначе модель видит содержимое, но не знает, как файл назван.

    :param current: это файлы ТЕКУЩЕГО (самого свежего) сообщения. Тогда явно их
        выделяем — при «полной памяти» в контексте висят десятки старых файлов, и
        модель путает свежий файл с ранее присланными (узнаёт «не того»).
    """
    if not attachments:
        return text
    blocks: list = []
    if text:
        blocks.append({"type": "text", "text": text})
    media = [a for a in attachments if _is_media_att(a)]
    if current and media:
        kinds = ", ".join(sorted({_media_kind_ru(a) for a in media}))
        blocks.append({"type": "text", "text": (
            f"[⬇ ВНИМАНИЕ: ниже — {kinds} из ЭТОГО, самого свежего сообщения. Речь идёт "
            "именно об этих файлах. Проанализируй КАЖДЫЙ из них напрямую и целиком "
            "(видео — просмотри по кадрам, кто/что в кадре; аудио — прослушай полностью). "
            "НЕ путай их с файлами из более ранних сообщений и не переноси выводы оттуда.]"
        )})
    for att in attachments:
        name = (att.name or "").strip()
        if _is_media_att(att):
            where = "в этом сообщении" if current else "ранее присланный"
            label = f"[Файл {where}"
            if name:
                label += f": «{name}»"
            label += f" — {_media_kind_ru(att)}]"
            blocks.append({"type": "text", "text": label})
        blocks.append(_content_from_attachment(att))
    return blocks


# С какого объёма payload считаем запрос «большим» (base64-видео/аудио и т.п.).
_LARGE_PAYLOAD_BYTES = 8 * 1024 * 1024


def _payload_bytes(messages: list[dict]) -> int:
    """Приблизительный объём запроса в байтах (текст + inline base64-данные)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for b in c:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    total += len(b.get("text") or "")
                elif t == "image_url":
                    total += len(((b.get("image_url") or {}).get("url")) or "")
                elif t == "input_audio":
                    total += len(((b.get("input_audio") or {}).get("data")) or "")
    return total


def _request_timeout(messages: list[dict]) -> int:
    """
    Таймаут под размер запроса: большие мультимодальные payload'ы (видео, аудио)
    добираются до Vertex и обрабатываются моделью значительно дольше 120 секунд —
    иначе крупный файл стабильно падал бы по таймауту, хотя провайдер его принимает.

    ВАЖНО: отсчёт идёт с момента запроса к LLM — файл к этому времени УЖЕ на нашем
    сервере (загрузка с устройства в таймаут не входит), но заливка payload'а
    «сервер → прокси → Vertex» входит. Поэтому даём время пропорционально размеру:
    ~10 секунд на МБ, минимум LARGE_REQUEST_TIMEOUT, потолок — полчаса.
    """
    size = _payload_bytes(messages)
    if size <= _LARGE_PAYLOAD_BYTES:
        return settings.REQUEST_TIMEOUT
    mb = size // (1024 * 1024)
    return max(settings.LARGE_REQUEST_TIMEOUT, min(1800, mb * 10))


def _has_media_blocks(messages: list[dict]) -> bool:
    """Есть ли в запросе вложения (image_url/input_audio — фото, видео, PDF, аудио)."""
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(
            isinstance(b, dict) and b.get("type") in ("image_url", "input_audio")
            for b in c
        ):
            return True
    return False


def effective_reasoning(params: Optional[GenerationParams], messages: list[dict]) -> str:
    """
    Итоговый уровень рассуждений (reasoning_effort для LiteLLM):
      * явный выбор пользователя ("disable"/"low"/"medium"/"high") — как есть;
      * "auto"/пусто + в запросе есть файлы + включён file_reasoning — "medium":
        Gemini местами не думает над файлами без явного бюджета размышлений;
      * иначе "" — параметр не передаём, решает провайдер.

    ВАЖНО: авто-форс thinking НЕ применяем при включённом Zero-Censorship
    (disable_safety). У Gemini режим рассуждений добавляет СОБСТВЕННУЮ модерацию
    во время reasoning, которая душит контент даже при safety_settings=OFF —
    то есть авто-thinking незаметно ломал обход цензуры. Явный выбор уровня
    пользователем уважаем всегда: он сам решает баланс «рассуждения ↔ свобода».
    """
    if params is None:
        return ""
    effort = (params.reasoning_effort or "").strip().lower()
    if effort == "auto":
        effort = ""
    if effort:
        return effort  # явный выбор пользователя — всегда как есть
    # Автовключение — только когда фильтры НЕ сняты (иначе thinking их вернёт).
    if not params.disable_safety and params.file_reasoning and _has_media_blocks(messages):
        return "medium"
    return ""


def _merge_params(params: Optional[GenerationParams]) -> dict:
    """Сливает дефолты из .env с тем, что пришло из UI (UI имеет приоритет)."""
    merged = {
        "temperature": settings.DEFAULT_TEMPERATURE,
        "top_p": settings.DEFAULT_TOP_P,
        "top_k": settings.DEFAULT_TOP_K,
        "max_tokens": settings.DEFAULT_MAX_TOKENS,
        "repetition_penalty": settings.DEFAULT_REPETITION_PENALTY,
    }
    if params:
        for key, value in params.model_dump(exclude_none=True).items():
            # model/disable_safety/web_access/send_avatars/reasoning_*/context_tokens/
            # history_files_mb обрабатываются отдельно, не как сэмплинг-параметры litellm.
            if key in ("model", "disable_safety", "web_access", "send_avatars",
                       "reasoning_effort", "file_reasoning", "context_tokens",
                       "history_files_mb"):
                continue
            merged[key] = value
    return merged


# Заглушка-ключ: LiteLLM требует api_key даже если прокси работает БЕЗ авторизации.
# Без него вызов падает ещё до отправки запроса (AuthenticationError).
DUMMY_PROXY_KEY = "sk-no-key-required"


def _route_kwargs(connection: Optional[dict], model_name: str) -> dict:
    """
    Готовит ключи маршрутизации (model / api_base / api_key):
      * через LiteLLM-прокси — префикс 'litellm_proxy/' + api_base;
      * напрямую — LiteLLM сам выберет провайдера по имени модели.
    """
    conn = connection or {}
    use_proxy = conn.get("use_proxy", settings.LITELLM_USE_PROXY)
    base_url = (conn.get("base_url", settings.LITELLM_BASE_URL) or "").strip()
    api_key = conn.get("api_key", settings.LITELLM_API_KEY)
    # Чистим имя модели от случайных пробелов/табов/переводов строк (копипаст).
    model_name = (model_name or "").strip()

    if use_proxy and base_url:
        return {
            "model": f"litellm_proxy/{model_name}",
            "api_base": base_url,
            # Если ключ не задан — подставляем заглушку, иначе LiteLLM не отправит запрос.
            "api_key": api_key or DUMMY_PROXY_KEY,
        }
    return {"model": model_name}


def effective_model(params: Optional[GenerationParams], connection: Optional[dict]) -> str:
    """Какая модель реально пойдёт в запрос: UI > настройки подключения > .env."""
    if params and params.model:
        return params.model.strip()
    return ((connection or {}).get("default_model") or settings.DEFAULT_MODEL).strip()


def _apply_connection(call_kwargs: dict, params, connection: Optional[dict]) -> None:
    """
    Настраивает, КУДА уйдёт запрос. Имя модели: модель из UI (params.model) >
    модель по умолчанию из настроек подключения > DEFAULT_MODEL из .env.
    """
    conn = connection or {}
    model_name = (
        (params.model if params and params.model else None)
        or conn.get("default_model")
        or settings.DEFAULT_MODEL
    )
    call_kwargs.update(_route_kwargs(connection, model_name))


async def stream_completion(
    messages: list[dict],
    params: Optional[GenerationParams] = None,
    connection: Optional[dict] = None,
    on_thought=None,
) -> AsyncGenerator[str, None]:
    """
    Стримит ответ модели по токенам (async generator).
    Используется и веб-сервером (WS/SSE), и Telegram-ботом — логика единая.

    :param on_thought: колбэк для «размышлений» модели (reasoning_content) —
        они не входят в ответ, но их можно показать пользователю live.
    """
    call_kwargs: dict = {
        "messages": messages,
        "stream": True,
        # Большой мультимодальный запрос (видео/аудио) получает увеличенный таймаут.
        "timeout": _request_timeout(messages),
        # Сбой/лимит провайдера (429 RESOURCE_EXHAUSTED, обрыв связи) — LiteLLM
        # повторяет с экспоненциальным бэкоффом, прежде чем отдать ошибку наверх.
        "num_retries": settings.LLM_NUM_RETRIES,
        **_merge_params(params),
    }
    _apply_connection(call_kwargs, params, connection)

    # Рассуждения (thinking): уровень пользователя или авто-включение при файлах.
    # LiteLLM транслирует reasoning_effort в thinkingBudget Gemini.
    # ВАЖНО: у нас модель идёт как 'litellm_proxy/…', и внешний LiteLLM с
    # drop_params=True НЕ знает, что прокси-модель поддерживает reasoning_effort —
    # и ТИХО выкидывает его (поэтому «высокие» размышления не доходили до Gemini).
    # allowed_openai_params форсирует проброс параметра в прокси как есть.
    reasoning = effective_reasoning(params, messages)
    allowed_params: list[str] = []
    if reasoning:
        call_kwargs["reasoning_effort"] = reasoning
        allowed_params.append("reasoning_effort")

    # Доступ в интернет: инструмент веб-поиска (Google Search grounding у Gemini).
    # Тоже пробрасываем принудительно, иначе drop_params может его выкинуть.
    if params and params.web_access:
        call_kwargs["tools"] = [{"googleSearch": {}}]
        allowed_params.append("tools")

    if allowed_params:
        call_kwargs["allowed_openai_params"] = allowed_params

    # Полная свобода по умолчанию: снимаем настраиваемые фильтры, если пользователь
    # их не включил явно (disable_safety=True — дефолт) ИЛИ это служебный вызов
    # без params (режиссёр, заголовок канваса и т.п.) — их тоже нельзя блокировать.
    # Для не-Gemini провайдеров LiteLLM отбросит safety_settings (drop_params).
    safety_off = params is None or params.disable_safety
    if safety_off:
        call_kwargs["safety_settings"] = GEMINI_SAFETY_OFF

    # Запись в отладочный лог: что именно уходит в прокси.
    entry = debug_log.log_request(
        "chat", call_kwargs["model"], call_kwargs.get("api_base"),
        {
            "messages": debug_log.summarize_messages(messages),
            "params": {k: call_kwargs.get(k) for k in ("temperature", "top_p", "max_tokens")},
            "safety_off": safety_off,
            "reasoning": reasoning or "auto",
        },
    )
    try:
        response = await litellm.acompletion(**call_kwargs)
        text = ""
        finish_reason = None
        thought_len = 0
        async for chunk in response:
            if not getattr(chunk, "choices", None):
                continue  # служебный чанк без choices (например, usage)
            choice = chunk.choices[0]
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
            # «Думающие» модели (Gemini 3.x и т.п.) стримят рассуждения отдельным
            # полем — в ответ они не идут, но их можно показать пользователю live.
            rc = getattr(choice.delta, "reasoning_content", None)
            if rc:
                thought_len += len(rc)
                if on_thought:
                    try:
                        on_thought(rc)
                    except Exception:  # noqa: BLE001 — показ мыслей не роняет стрим
                        pass
            delta = choice.delta.content
            if delta:
                text += delta
                yield delta
        if finish_reason:
            entry["finish_reason"] = finish_reason
        if not text:
            # Стрим завершился «успешно», но контента НЕТ (фильтры провайдера,
            # обрезка по токенам во время размышлений и т.п.). Молчать нельзя —
            # иначе пользователь видит «ничего» без объяснений. Бросаем ошибку:
            # она уйдёт клиенту событием error и попадёт в отладочный лог.
            extra = f", размышления: {thought_len} симв." if thought_len else ""
            msg = (
                f"Модель вернула ПУСТОЙ ответ (finish_reason={finish_reason or 'нет'}{extra}). "
                "Чаще всего это фильтры контента провайдера (даже при Zero-Censorship) "
                "или исчерпание max_tokens на размышления. Попробуйте переформулировать, "
                "сменить модель или повторить генерацию."
            )
            debug_log.finish(entry, "error", error=msg)
            raise RuntimeError(msg)
        debug_log.finish(entry, "ok", preview=text[:400])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # Обрыв соединения на БОЛЬШОМ запросе — объясняем по-человечески: голое
        # «Connection error» не говорит, что прокси/провайдер оборвали связь
        # именно во время передачи крупного файла (лимит размера или память).
        size = _payload_bytes(messages)
        if size > _LARGE_PAYLOAD_BYTES and "connection" in msg.lower():
            mb = size // (1024 * 1024)
            friendly = (
                f"Соединение оборвалось на большом запросе (~{mb} МБ с учётом base64): "
                "прокси или провайдер закрыли связь во время передачи файла. Обычно это "
                "лимит размера запроса или нехватка памяти у LiteLLM-прокси (смотрите его "
                f"консоль/лог) либо ограничение провайдера. Исходная ошибка: {msg}"
            )
            if entry.get("status") != "error":
                debug_log.finish(entry, "error", error=friendly)
            raise RuntimeError(friendly) from exc
        if entry.get("status") != "error":  # не перетираем детальную запись о пустом ответе
            debug_log.finish(entry, "error", error=msg)
        raise


async def complete(
    messages: list[dict],
    params: Optional[GenerationParams] = None,
    connection: Optional[dict] = None,
) -> str:
    """Разовый (нестриминговый) ответ — собираем целиком из стрима. Удобно для
    служебных задач, например «сочини промпт картинки по контексту чата»."""
    return "".join([chunk async for chunk in stream_completion(messages, params, connection)])


async def generate_image(
    prompt: str, connection: Optional[dict] = None, size: str = "1024x1024"
) -> str:
    """
    Генерация изображения (арта) через LiteLLM. Модель берётся из настроек
    подключения (поле image_model) — в вашем прокси должна быть настроена
    модель генерации картинок. Возвращает URL или data:image base64.
    """
    conn = connection or {}
    model_name = conn.get("image_model") or "dall-e-3"

    kwargs: dict = {
        "prompt": prompt,
        "n": 1,
        "size": size,
        "timeout": settings.REQUEST_TIMEOUT,
        **_route_kwargs(connection, model_name),
    }
    entry = debug_log.log_request(
        "image", kwargs["model"], kwargs.get("api_base"),
        {"prompt": prompt[:200], "size": size},
    )
    try:
        response = await litellm.aimage_generation(**kwargs)
    except Exception as exc:  # noqa: BLE001
        debug_log.finish(entry, "error", error=str(exc))
        raise
    debug_log.finish(entry, "ok", preview="(картинка получена)")

    item = response.data[0]
    # У разных провайдеров результат приходит как b64_json или как url.
    b64 = getattr(item, "b64_json", None) or (
        item.get("b64_json") if isinstance(item, dict) else None
    )
    if b64:
        return "data:image/png;base64," + b64
    url = getattr(item, "url", None) or (
        item.get("url") if isinstance(item, dict) else None
    )
    return url or ""


def _extract_image_from_message(msg) -> str:
    """Достаёт сгенерированную картинку из ответа chat-модели (nano-banana и т.п.)."""
    # 1) Отдельное поле images (litellm для gemini image output).
    images = getattr(msg, "images", None)
    if images:
        for im in images:
            url = (im.get("image_url") or {}).get("url") if isinstance(im, dict) else None
            if url:
                return url
    # 2) content как список блоков с image_url.
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url")
                if url:
                    return url
    # 3) content как data:URI строкой.
    if isinstance(content, str) and content.startswith("data:image"):
        return content
    return ""


async def generate_image_chat(
    prompt: str, reference_images: list, connection: Optional[dict] = None
) -> str:
    """
    Генерация картинки через ЧАТ-модель (nano-banana / *-image): модели передаются
    референс-картинки (аватары, фото из чата) — она «видит» внешность и сцену.
    Картинку достаём из ответа. Работает не со всеми моделями (нужен image-вывод).
    """
    conn = connection or {}
    model_name = conn.get("image_model") or "gemini-2.5-flash-image"

    content: list = [{"type": "text", "text": prompt}]
    for ref in (reference_images or [])[:4]:
        if isinstance(ref, str) and (ref.startswith("data:image") or ref.startswith("http")):
            content.append({"type": "image_url", "image_url": {"url": ref}})

    kwargs: dict = {
        "messages": [{"role": "user", "content": content}],
        # Референсы — data:URI картинок; при большом объёме даём больше времени.
        "timeout": _request_timeout([{"role": "user", "content": content}]),
        **_route_kwargs(connection, model_name),
    }
    entry = debug_log.log_request(
        "image-chat", kwargs["model"], kwargs.get("api_base"),
        {"prompt": prompt[:200], "refs": len(content) - 1},
    )
    try:
        response = await litellm.acompletion(**kwargs)
        url = _extract_image_from_message(response.choices[0].message)
        if not url:
            raise RuntimeError(
                "Модель не вернула картинку в ответе. Возможно, выбранная модель "
                "генерит картинки через image_generation — выключите «через чат»."
            )
        debug_log.finish(entry, "ok", preview="(картинка из чата)")
        return url
    except Exception as exc:  # noqa: BLE001
        debug_log.finish(entry, "error", error=str(exc))
        raise
