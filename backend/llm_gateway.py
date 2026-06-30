"""
Единый шлюз к LLM через LiteLLM.

По умолчанию запросы идут в УЖЕ ЗАПУЩЕННЫЙ LiteLLM-прокси (например, на :4000):
именно прокси хранит ключи провайдеров и список моделей. Адрес прокси и ключ
настраиваются в интерфейсе (вкладка «Подключение») и передаются сюда как `connection`.

Если proxy выключить (use_proxy=False), LiteLLM маршрутизирует напрямую по имени
модели ('gpt-4o' -> OpenAI, 'gemini/...' -> Google и т.д.) — ключи берутся из .env.

Вся обработка — на сервере: браузер только шлёт текст и слушает токены.
"""
from typing import AsyncGenerator, Optional

import litellm

from backend import debug_log
from backend.config import settings
from backend.schemas import AttachmentIn, GenerationParams

# Не роняем запрос, если провайдер не поддерживает какой-то параметр (например top_k
# у OpenAI). LiteLLM просто отбросит лишнее.
litellm.drop_params = True


# Полное снятие фильтров для Gemini / Vertex AI (режим Zero-Censorship).
GEMINI_SAFETY_OFF = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]


def _content_from_attachment(att: AttachmentIn) -> dict:
    """Превращает вложение в content-блок формата OpenAI/LiteLLM."""
    if att.type == "image":
        return {"type": "image_url", "image_url": {"url": att.data}}
    if att.type == "audio":
        # Gemini 1.5 Pro принимает аудио НАТИВНО — Whisper не нужен.
        b64 = att.data.split(",")[-1]  # отрезаем 'data:audio/...;base64,' если он есть
        fmt = (att.mime or "audio/wav").split("/")[-1]
        return {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}
    if att.type == "document":
        # Word/PDF/текст: конвертируем в PDF или извлекаем текст (см. document_service).
        from backend.document_service import prepare_document

        return prepare_document(att.data, att.mime, att.name)
    raise ValueError(f"Неизвестный тип вложения: {att.type}")


def build_user_content(text: str, attachments: list[AttachmentIn]):
    """Собирает контент сообщения: строка без вложений, иначе список блоков."""
    if not attachments:
        return text
    blocks: list = []
    if text:
        blocks.append({"type": "text", "text": text})
    for att in attachments:
        blocks.append(_content_from_attachment(att))
    return blocks


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
            # model/disable_safety/web_access/send_avatars обрабатываются отдельно,
            # не как сэмплинг-параметры litellm.
            if key in ("model", "disable_safety", "web_access", "send_avatars"):
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
) -> AsyncGenerator[str, None]:
    """
    Стримит ответ модели по токенам (async generator).
    Используется и веб-сервером (WS/SSE), и Telegram-ботом — логика единая.
    """
    call_kwargs: dict = {
        "messages": messages,
        "stream": True,
        "timeout": settings.REQUEST_TIMEOUT,
        **_merge_params(params),
    }
    _apply_connection(call_kwargs, params, connection)

    # Доступ в интернет: подключаем инструмент веб-поиска (Google Search grounding
    # у Gemini). Неподдерживающие провайдеры отбросят его через drop_params.
    if params and params.web_access:
        call_kwargs["tools"] = [{"googleSearch": {}}]

    # Zero-Censorship: снимаем фильтры там, где это применимо (для остальных — drop).
    if params and params.disable_safety:
        call_kwargs["safety_settings"] = GEMINI_SAFETY_OFF

    # Запись в отладочный лог: что именно уходит в прокси.
    entry = debug_log.log_request(
        "chat", call_kwargs["model"], call_kwargs.get("api_base"),
        {
            "messages": debug_log.summarize_messages(messages),
            "params": {k: call_kwargs.get(k) for k in ("temperature", "top_p", "max_tokens")},
            "safety_off": bool(params and params.disable_safety),
        },
    )
    try:
        response = await litellm.acompletion(**call_kwargs)
        text = ""
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta:
                text += delta
                yield delta
        debug_log.finish(entry, "ok", preview=text[:400])
    except Exception as exc:  # noqa: BLE001
        debug_log.finish(entry, "error", error=str(exc))
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
        "timeout": settings.REQUEST_TIMEOUT,
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
