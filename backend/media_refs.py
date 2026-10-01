"""
Файлы для модели ССЫЛКОЙ: каждый файл один раз загружается в хранилище
провайдера, дальше модель получает короткую ссылку, а не мегабайты base64.

Зачем. Модель должна видеть файлы всего чата, который она видит. Но пересылать
их заново на каждом ходу нельзя: чат с парой голосовых и видео — это десятки и
сотни мегабайт base64 в КАЖДОМ запросе. Сервер, прокси и провайдер давились
(«Base64 decoding failed», обрывы связи, таймауты), и раньше спасали только
ручные лимиты «Файлы в памяти диалога» — ценой того, что модель переставала
видеть старые файлы.

Как. LiteLLM-прокси умеет загружать файлы в хранилище провайдера
(POST /files с заголовком x-litellm-model — маршрут по модели):
  * Vertex AI → Google Cloud Storage, ссылка gs://… — не истекает;
  * Gemini API (AI Studio) → Files API, ссылка https://generativelanguage…/files/…
    — живёт 48 часов.
В запрос уходит {"type": "image_url", "image_url": {"url": <ссылка>, "format": <mime>}},
прокси превращает это в file_data Gemini. Оригиналы остаются в attachment_blobs
нетронутыми — пользователь видит и скачивает их как раньше.

Встроено так:
  * сборщики истории (horae_memory.messages_to_history_db, группы, база знаний)
    НЕ читают данные файлов — ставят заготовку (placeholder) с метой файла;
  * stream_completion перед запросом зовёт materialize(): заготовка → ссылка,
    если файл уже загружен; иначе исходный файл целиком, пока файлы истории
    укладываются в INLINE_FILES_MB (свежие первыми); иначе короткая пометка.
    Файлы без ссылки встают в очередь загрузки;
  * фоновый загрузчик (_Uploader) грузит по ОДНОМУ файлу и только в паузах
    между ответами модели — старые чаты с гигабайтом видео догружаются
    постепенно, сервер не давится;
  * хранилище проверяется для КАЖДОЙ модели прокси отдельно (probe): модель
    должна быть Gemini (Vertex AI или Gemini API), крошечная картинка должна
    загрузиться и прочитаться моделью по ссылке. Не вышло — всё работает как
    раньше (файлы целиком в пределах INLINE_FILES_MB), а в «Генерации»
    видно, почему и что настроить у прокси.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from backend.config import settings

log = logging.getLogger("aichat.media")

# Приватный ключ заготовок в content-блоках. В модель не уходит: materialize()
# возвращает чистые копии.
MARK = "_te"

GCS_PREFIX = "gs://"
FILES_API_PREFIX = "https://generativelanguage.googleapis.com/v1beta/files/"

_SETTINGS_KEY = "media_refs"
# Files API хранит файл 48 ч; берём с запасом, чтобы ссылка не истекла посреди ответа.
_FILES_API_TTL = timedelta(hours=46)
_EXPIRY_MARGIN = timedelta(hours=1)
# Повторная проверка хранилища, которое не работает: оператор мог его настроить.
_RECHECK_FAILED = timedelta(hours=6)
# Бэкофф временных сбоев загрузки: 2^n минут (до 6 ч); после _MAX_ATTEMPTS — раз в сутки.
_MAX_ATTEMPTS = 6
# Files API: файл больше этого идёт в очередь сразу после отправки; меньше —
# только если не влез целиком (ссылка Files API живёт двое суток, мелочь
# дешевле отправить целиком, чем перезагружать).
_FILES_API_MIN_BYTES = 4 * 1024 * 1024

# Пометки вместо файла, который в этот запрос не вошёл целиком.
NOTE_PENDING = ("[содержимое этого файла в запрос не вошло: он ещё загружается в хранилище "
                "модели; не описывай его по догадке]")
NOTE_OVER = ("[содержимое этого файла в запрос не вошло: лимит размера запроса; не описывай "
             "его по догадке]")


def _now() -> datetime:
    """Текущее время UTC без tzinfo — так DateTime хранится в SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _short_error(error) -> str:
    text = str(error or "").strip().replace("\n", " ")
    return text[:300] + ("…" if len(text) > 300 else "")


# ============================ вид файла и оценка ============================

def att_kind(meta: dict) -> str:
    """
    Вид вложения по мете: image | video | audio | pdf | document.
    Повторяет разбор llm_gateway._content_from_attachment: легаси-видео и
    аудио, сохранённые с типом document, — это видео и аудио.
    """
    t = (meta.get("type") or "document").lower()
    mime = (meta.get("mime") or "").lower().split(";")[0].strip()
    if t == "image":
        return "image"
    if t == "video" or (t == "document" and mime.startswith("video/")):
        return "video"
    if t == "audio" or (t == "document" and mime.startswith("audio/")):
        return "audio"
    name = (meta.get("name") or "").lower()
    if mime == "application/pdf" or (mime in ("", "application/octet-stream") and name.endswith(".pdf")):
        return "pdf"
    return "document"


# Синонимы mime, которые Gemini не узнаёт в «неканоническом» виде.
_MIME_ALIASES = {
    "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-m4a": "audio/m4a", "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg",
    "audio/x-flac": "audio/flac", "audio/x-aac": "audio/aac",
    "video/x-m4v": "video/mp4", "application/x-pdf": "application/pdf",
}


def norm_mime(kind: str, mime: str | None) -> str:
    """
    Один нормализованный mime на файл: с ним файл загружается в хранилище, и
    он же уходит в блок-ссылку как format (без параметров, в нижнем регистре,
    синонимы приведены).
    """
    m = (mime or "").lower().split(";")[0].strip()
    m = _MIME_ALIASES.get(m, m)
    if kind == "image":
        return m if m.startswith("image/") else "image/jpeg"
    if kind == "video":
        return m if m.startswith("video/") else "video/mp4"
    if kind == "audio":
        return m if m.startswith("audio/") else "audio/wav"
    if kind == "pdf":
        return "application/pdf"
    return m or "application/octet-stream"


def can_ref(kind: str) -> bool:
    """Можно ли дать модели этот файл ссылкой (Gemini читает такие файлы по URI)."""
    return kind in ("image", "video", "audio", "pdf")


# Сколько байт в секунду весит аудио разных форматов — для оценки его длины.
_AUDIO_BPS = {
    "ogg": 4_000, "opus": 4_000, "webm": 6_000, "amr": 1_600,
    "wav": 88_000, "x-wav": 88_000, "wave": 88_000, "aiff": 88_000, "x-aiff": 88_000,
    "flac": 60_000, "x-flac": 60_000,
}
_VIDEO_BPS = 400_000        # ~3 Мбит/с: типичное видео с телефона после мессенджера
_PDF_PAGE_BYTES = 60_000    # средний вес страницы PDF


def media_tokens(kind: str, mime: str | None, size_bytes: int) -> int:
    """
    Сколько токенов входа стоит файл у Gemini (примерно).

    Раньше любой файл считался в 400 токенов, аудио — в 1500, и окно контекста
    «не видело» реальный вес: часовая запись весит ~115 тыс. токенов. Теперь
    оценка идёт от размера и вида файла, и обрезка истории под бюджет учитывает
    файлы честно — ссылкой файл идёт или целиком, стоит он для модели одинаково.
    """
    size = max(0, int(size_bytes or 0))
    if kind == "image":
        return 1100
    if kind == "audio":
        sub = (mime or "").lower().split(";")[0].split("/")[-1].strip()
        seconds = size / _AUDIO_BPS.get(sub, 16_000)
        return max(64, int(seconds * 32))
    if kind == "video":
        seconds = size / _VIDEO_BPS
        return max(300, int(seconds * 300))
    if kind == "pdf":
        return max(258, int(size / _PDF_PAGE_BYTES + 1) * 258)
    m = (mime or "").lower()
    if any(t in m for t in ("word", "officedocument", "opendocument", "rtf", "msword")):
        # Офисный документ уходит PDF-ом или текстом: оценка — страницами, а не
        # байтами сжатого файла (docx на 2 МБ с одной фотографией — пара страниц).
        return max(258, min(int(size / _PDF_PAGE_BYTES + 1) * 258, 20_000))
    if m.startswith("text/") or any(t in m for t in ("json", "csv", "xml", "markdown", "yaml")):
        return max(64, min(size // 4, 200_000))       # текст уходит текстом
    return 64   # прочий двоичный файл модели не шлётся — только пометка


def meta_bytes(meta: dict) -> int:
    """Размер файла в байтах по мете (легаси-мета с inline data — по её длине)."""
    try:
        size = int(meta.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if size > 0:
        return size
    return int(len(meta.get("data") or "") * 3 / 4)


def block_tokens(block: dict) -> int | None:
    """
    Оценка токенов content-блока с файлом; None — блок не файл (текст считает
    вызывающий). Понимает заготовки, ссылки, data:URI и input_audio.
    """
    if not isinstance(block, dict):
        return None
    te = block.get(MARK)
    if isinstance(te, dict):
        mime = te.get("mime") or ""
        if not mime and te.get("kind") == "document":
            import mimetypes

            mime = mimetypes.guess_type(te.get("name") or "")[0] or ""
        return media_tokens(te.get("kind") or "", mime, int(te.get("bytes") or 0))
    t = block.get("type")
    if t == "image_url":
        iu = block.get("image_url") or {}
        url = iu.get("url") or ""
        fmt = (iu.get("format") or "").lower()
        if url[:5].lower() == "data:":
            semi = url.find(";", 0, 120)
            mime = url[5:semi] if semi > 0 else fmt
            payload = len(url) - (url.find(",") + 1)
        else:
            mime, payload = fmt, 0
        mime = (mime or fmt or "").lower()
        kind = ("video" if mime.startswith("video/") else "audio" if mime.startswith("audio/")
                else "pdf" if "pdf" in mime else "image")
        return media_tokens(kind, mime, int(payload * 3 / 4))
    if t == "input_audio":
        ia = block.get("input_audio") or {}
        return media_tokens("audio", "audio/" + (ia.get("format") or ""),
                            int(len(ia.get("data") or "") * 3 / 4))
    return None


# ============================ заготовки в истории ============================

_KIND_RU = {"image": "изображение", "video": "видео", "audio": "аудио", "pdf": "документ"}


def placeholder(meta: dict, *, priority: int = 0) -> dict | None:
    """
    Заготовка файла для контекста: текстовый блок-пометка с метой файла.

    materialize() заменит её ссылкой или самим файлом. Данные файла тут НЕ
    читаются — история чата с гигабайтом вложений собирается мгновенно.

    :param priority: 2 — файлы реплики, на которую идёт ответ (регенерация,
        «Продолжить»); 1 — база знаний; 0 — история. Целиком в запрос файлы
        попадают в порядке приоритета, внутри — свежие первыми.
    """
    if not isinstance(meta, dict):
        return None
    blob_id = meta.get("blob_id")
    data = meta.get("data") or ""
    if not blob_id and not data:
        return None
    try:
        blob_id = int(blob_id) if blob_id else None
    except (TypeError, ValueError):
        return None
    kind = att_kind(meta)
    te = {
        "blob_id": blob_id,
        "kind": kind,
        "type": meta.get("type") or "document",
        "mime": meta.get("mime") or "",
        "name": meta.get("name") or "",
        "bytes": meta_bytes(meta),
        "priority": priority,
    }
    if data and not blob_id:
        te["data"] = data   # легаси: данные лежат прямо в мете сообщения
    return {"type": "text", "text": NOTE_PENDING, MARK: te}


def history_content(text: str, metas, *, current: bool = False, priority: int = 0):
    """
    Контент реплики пользователя из истории: текст, подписи файлов и заготовки.
    Та же разметка, что у llm_gateway.build_user_content (подпись с именем
    перед каждым медиа-файлом), только без данных. Без файлов — строка.
    """
    blocks: list = []
    items = [ph for ph in (placeholder(meta, priority=priority) for meta in metas or []) if ph]
    media = [ph[MARK]["kind"] for ph in items if ph[MARK]["kind"] in ("image", "video", "audio")]
    if current and media:
        # Та же пометка, что у build_user_content(current=True): перегенерация
        # видит реплику так же, как исходный ход.
        kinds = ", ".join(sorted({_KIND_RU.get(k, "файл") for k in media}))
        blocks.append({"type": "text", "text": (
            f"[⬇ ВНИМАНИЕ: ниже — {kinds} из ЭТОГО, самого свежего сообщения. Речь идёт "
            "именно об этих файлах. Проанализируй КАЖДЫЙ из них напрямую и целиком "
            "(видео — просмотри по кадрам, кто/что в кадре; аудио — прослушай полностью). "
            "НЕ путай их с файлами из более ранних сообщений и не переноси выводы оттуда.]"
        )})
    for ph in items:
        te = ph[MARK]
        where = "в этом сообщении" if current else "ранее присланный"
        label = f"[Файл {where}"
        if te["name"]:
            label += f": «{te['name']}»"
        # У документов подпись тоже есть: не уйдёт документ целиком — модель
        # всё равно знает его имя.
        label += f" — {_KIND_RU.get(te['kind'], 'документ')}]"
        blocks.append({"type": "text", "text": label})
        blocks.append(ph)
    if not blocks:
        return text
    return ([{"type": "text", "text": text}] if text else []) + blocks


def has_markers(messages) -> bool:
    """Есть ли в запросе заготовки файлов (нужен ли materialize)."""
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and MARK in b:
                    return True
    return False


def ref_block(uri: str, mime: str) -> dict:
    """Блок-ссылка на файл в хранилище (у прокси он станет file_data Gemini)."""
    return {"type": "image_url", "image_url": {"url": uri, "format": mime}}


# ============================ маршрут и хранилище ============================

@dataclass(frozen=True)
class Route:
    """Куда уходит запрос: прокси (адрес, ключ) и псевдоним модели на нём."""
    base_url: str
    api_key: str
    alias: str

    @property
    def proxy_key(self) -> str:
        return hashlib.sha1(self.base_url.encode()).hexdigest()[:12]

    @property
    def cap_key(self) -> str:
        return f"{self.proxy_key}|{self.alias}"


def route_for(params=None, connection: dict | None = None, model: str | None = None) -> Route | None:
    """
    Маршрут для ссылок; None — прямой режим без прокси или не задан адрес
    прокси: тогда с файлами не делаем ничего сверх обычного (иначе LiteLLM
    отправил бы загрузку на api.openai.com по умолчанию).
    """
    from backend.llm_gateway import DUMMY_PROXY_KEY, effective_model

    conn = connection or {}
    use_proxy = conn.get("use_proxy", settings.LITELLM_USE_PROXY)
    base = (conn.get("base_url", settings.LITELLM_BASE_URL) or "").strip().rstrip("/")
    alias = (model or effective_model(params, connection) or "").strip()
    if alias.startswith("litellm_proxy/"):
        alias = alias[len("litellm_proxy/"):]
    if not (use_proxy and base and alias):
        return None
    key = (conn.get("api_key", settings.LITELLM_API_KEY) or "").strip() or DUMMY_PROXY_KEY
    return Route(base, key, alias)


def family_of(uri: str) -> str:
    if (uri or "").startswith(GCS_PREFIX):
        return "gcs"
    if (uri or "").startswith(FILES_API_PREFIX):
        return "files_api"
    return ""


def gcs_bucket(uri: str) -> str:
    return (uri or "")[len(GCS_PREFIX):].split("/", 1)[0] if (uri or "").startswith(GCS_PREFIX) else ""


def decode_file_id(file_id: str) -> str:
    """
    Исходная ссылка провайдера из id, который вернул прокси. Новый прокси при
    загрузке по модели заворачивает её: file-<urlsafe b64("litellm:<ссылка>;model,<модель>")>;
    старый может вернуть ссылку как есть — её и возвращаем.
    """
    fid = (file_id or "").strip()
    if family_of(fid):
        return fid
    body = fid[5:] if fid.startswith("file-") else fid
    try:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    except Exception:  # noqa: BLE001
        return fid
    if decoded.startswith("litellm:") and ";model," in decoded:
        return decoded[len("litellm:"):].rpartition(";model,")[0]
    return fid


def encode_file_id(uri: str, alias: str) -> str:
    """Обратное к decode_file_id — id, по которому прокси найдёт файл через модель alias."""
    raw = f"litellm:{uri};model,{alias}".encode()
    return "file-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def scope_for(route: Route, cap: dict) -> str:
    """
    Где ссылка действует. GCS: копии в одном бакете общие для всех моделей
    прокси, прошедших проверку (каждая модель проверяет чтение сама). Files API:
    файл привязан к ключу Gemini API — у каждой модели свой набор ссылок.
    """
    if cap.get("family") == "gcs":
        return f"gcs:{route.proxy_key}:{cap.get('bucket') or ''}"
    return f"files_api:{route.proxy_key}:{route.alias}"


# Кэш проверок хранилища по моделям: {cap_key: {family, ok, bucket, error,
# alias, proxy, checked_at, until}}. Лежит в AppSetting «media_refs», чтобы не
# проверять прокси после каждого рестарта.
_caps: dict[str, dict] = {}


async def load_caps(db) -> None:
    from backend.models import AppSetting

    row = await db.get(AppSetting, _SETTINGS_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    _caps.clear()
    _caps.update({k: v for k, v in (value.get("routes") or {}).items() if isinstance(v, dict)})


async def _save_caps() -> None:
    from backend.database import AsyncSessionLocal
    from backend.models import AppSetting

    async with AsyncSessionLocal() as db:
        row = await db.get(AppSetting, _SETTINGS_KEY)
        value = {"routes": dict(_caps)}
        if row is None:
            db.add(AppSetting(key=_SETTINGS_KEY, value=value))
        else:
            row.value = value
        await db.commit()


def capability(route: Route | None) -> dict | None:
    """Результат последней проверки хранилища для модели (None — не проверялась)."""
    if route is None:
        return None
    return _caps.get(route.cap_key)


_caps_read_at = 0.0


async def refresh_caps() -> None:
    """
    Процесс без своего загрузчика (отдельный Telegram-бот) проверок не делает —
    перечитывает их результаты из базы раз в 5 минут, чтобы тоже слать ссылки.
    """
    global _caps_read_at
    if uploader.running or time.monotonic() - _caps_read_at < 300:
        return
    _caps_read_at = time.monotonic()
    try:
        from backend.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await load_caps(db)
    except Exception:  # noqa: BLE001
        log.debug("Проверки хранилища не прочитались", exc_info=True)


def usable(route: Route | None) -> str:
    """
    Семейство хранилища, если ссылки для модели работают, иначе ''.
    MEDIA_REFS=false — ссылок нет совсем (как будто хранилища у прокси нет).
    """
    if not settings.MEDIA_REFS:
        return ""
    cap = capability(route)
    if not cap or not cap.get("ok"):
        return ""
    return cap.get("family") or ""


# Рабочее хранилище перепроверяется раз в сутки (лениво, при использовании):
# бакет могли удалить, права — отозвать.
_RECHECK_OK = 24 * 3600


def _cap_stale(cap: dict | None) -> bool:
    if not cap:
        return True
    if cap.get("ok"):
        return time.time() - float(cap.get("checked_at") or 0) >= _RECHECK_OK
    return time.time() >= float(cap.get("until") or 0)


async def _set_cap(route: Route, family: str, *, ok: bool, error: str = "", bucket: str = "") -> dict:
    cap = {
        "family": family, "ok": ok, "error": error, "bucket": bucket, "alias": route.alias,
        "proxy": route.base_url, "checked_at": time.time(),
        "until": 0 if ok else time.time() + _RECHECK_FAILED.total_seconds(),
    }
    _caps.pop(route.cap_key, None)
    _caps[route.cap_key] = cap
    while len(_caps) > 50:   # не копим проверки всех когда-либо введённых моделей
        _caps.pop(next(iter(_caps)))
    try:
        await _save_caps()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сохранить результат проверки хранилища")
    return cap


# ============================ сборка запроса ============================

@dataclass
class _Marker:
    mi: int          # индекс сообщения
    bi: int          # индекс блока
    te: dict


def _markers(messages) -> list[_Marker]:
    out = []
    for mi, m in enumerate(messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for bi, b in enumerate(c):
            if isinstance(b, dict) and isinstance(b.get(MARK), dict):
                out.append(_Marker(mi, bi, b[MARK]))
    return out


async def _lookup(db, scope: str, blob_ids: list[int]) -> dict[int, dict]:
    """
    Состояние ссылок для blob_ids в хранилище scope:
    {blob_id: {"uri", "mime", "bytes"} | {"state": waiting|failed|rejected, ...}}.
    Данные блобов не читаются.
    """
    from sqlalchemy import select

    from backend.models import AttachmentBlob, MediaRef

    now = _now()
    out: dict[int, dict] = {}
    ids = sorted(set(blob_ids))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        # message_id блоба — без колонки data: SQLite не трогает её страниц.
        owners = dict((await db.execute(
            select(AttachmentBlob.id, AttachmentBlob.message_id).where(AttachmentBlob.id.in_(chunk))
        )).all())
        rows = (await db.execute(
            select(MediaRef).where(MediaRef.scope == scope, MediaRef.blob_id.in_(chunk))
        )).scalars().all()
        for r in rows:
            if r.blob_id not in owners or owners[r.blob_id] != r.blob_owner:
                continue   # блоб удалён или его id уже занят другим файлом
            if r.status == "rejected" and not (r.next_try_at and r.next_try_at <= now):
                out[r.blob_id] = {"state": "rejected"}
            elif r.status == "rejected":
                out[r.blob_id] = {"state": "failed", "next_try_at": None}   # отказ истёк — ещё раз
            elif r.status == "uploading":
                # Резерв идущей загрузки; просроченный (сервер упал посреди
                # загрузки) — как сбой, который пора повторить.
                out[r.blob_id] = ({"state": "waiting"} if r.next_try_at and r.next_try_at > now
                                  else {"state": "failed", "next_try_at": None})
            elif r.status != "ready" or not r.uri:
                out[r.blob_id] = {"state": "failed", "next_try_at": r.next_try_at}
            elif r.expires_at is not None and r.expires_at - _EXPIRY_MARGIN <= now:
                continue   # Files API: истекает — загрузим заново
            elif r.usable_after is not None and r.usable_after > now:
                out[r.blob_id] = {"state": "waiting"}
            else:
                out[r.blob_id] = {"uri": r.uri, "mime": r.mime, "bytes": r.bytes or 0}
    return out


def _inline_chars(te: dict) -> int:
    if te.get("data"):
        return len(te["data"])
    return int((te.get("bytes") or 0) * 4 / 3) + 64


@dataclass
class Plan:
    """Что станет с каждым файлом запроса: ссылка, целиком или пометка."""
    refs: dict = field(default_factory=dict)      # индекс маркера -> {uri, mime, bytes}
    inline: set = field(default_factory=set)      # индексы маркеров, которые уйдут целиком
    enqueue: list = field(default_factory=list)   # te файлов, которые нужно загрузить
    pending: set = field(default_factory=set)     # индексы: ссылка скоро будет
    inline_chars: int = 0
    family: str = ""
    route: Route | None = None
    total: int = 0

    def summary(self) -> dict:
        # «Ждут загрузки» — только то, что правда встанет ссылкой; документы,
        # файлы сверх MEDIA_UPLOAD_MAX_MB и отвергнутые — «пометкой».
        waiting = len(self.pending - self.inline - set(self.refs))
        return {
            "total": self.total,
            "refs": len(self.refs),
            "inline": len(self.inline),
            "inline_mb": round(self.inline_chars * 3 / 4 / (1024 * 1024), 1),
            "pending": waiting,
            "notes": self.total - len(self.refs) - len(self.inline) - waiting,
            "family": self.family,
        }


async def plan(messages, params=None, connection=None, *, exclude_refs: set | None = None,
               db=None) -> tuple[Plan, list[_Marker]]:
    """
    Решает судьбу файлов запроса, не читая их данных. Порядок «целиком»:
    файлы отвечаемой реплики, база знаний, затем история от свежих к старым.
    """
    marks = _markers(messages)
    route = route_for(params, connection)
    if marks and route is not None:
        await refresh_caps()
    cap = capability(route) or {}
    family = usable(route)
    p = Plan(family=family, route=route, total=len(marks))
    if not marks:
        return p, marks
    scope = scope_for(route, cap) if family else ""
    refs: dict[int, dict] = {}
    if family:
        blob_ids = [m.te["blob_id"] for m in marks if m.te.get("blob_id") and can_ref(m.te.get("kind"))]
        if blob_ids:
            if db is None:
                from backend.database import AsyncSessionLocal

                async with AsyncSessionLocal() as own:
                    refs = await _lookup(own, scope, blob_ids)
            else:
                refs = await _lookup(db, scope, blob_ids)
    # Лимит «целиком» — для файлов истории и базы знаний, и он не зависит от
    # того, есть ли файлы у текущей реплики: иначе набор файлов истории менялся
    # бы от хода к ходу, и провайдер не находил бы начало запроса в своём кэше.
    # Файлы отвечаемой реплики (перегенерация, «Продолжить», ретрай) идут
    # всегда, как в исходном ходе, — сверх лимита.
    budget = max(0, int(settings.INLINE_FILES_MB)) * 1024 * 1024
    max_upload = max(0, int(settings.MEDIA_UPLOAD_MAX_MB)) * 1024 * 1024
    order = sorted(range(len(marks)), key=lambda i: (-int(marks[i].te.get("priority") or 0), -i))
    now = _now()
    for i in order:
        te = marks[i].te
        bid = te.get("blob_id")
        ref = refs.get(bid) if bid else None
        if ref and ref.get("uri") and not (exclude_refs and ref["uri"] in exclude_refs):
            p.refs[i] = ref
            continue
        need = _inline_chars(te)
        answered = int(te.get("priority") or 0) >= 2
        inline = answered or p.inline_chars + need <= budget
        if inline:
            p.inline.add(i)
            if not answered:
                p.inline_chars += need
        if not (family and bid and can_ref(te.get("kind"))):
            continue
        state = (ref or {}).get("state")
        if state == "rejected" or int(te.get("bytes") or 0) > max_upload:
            continue   # ссылкой этот файл не пойдёт никогда — и в очередь не ставим
        p.pending.add(i)
        if state == "waiting" or (ref and ref.get("uri")):
            continue   # уже загружен: ждём обработки или ссылку сейчас обходим
        if state == "failed" and ref.get("next_try_at") and ref["next_try_at"] > now:
            continue
        # Files API: мелкий файл, который и так уходит целиком, не перезагружаем
        # каждые двое суток — только то, что целиком не влезает.
        if family == "files_api" and inline:
            continue
        p.enqueue.append((0 if inline else 1, i, te))
    # Очередь: сначала то, что модель сейчас НЕ видит (пометки), и от старых к
    # новым. Тогда начало запроса со временем только растёт ссылками и не
    # перетасовывается — кэш провайдера продолжает попадать.
    p.enqueue = [te for _, _, te in sorted(p.enqueue, key=lambda x: (-x[0], x[1]))]
    return p, marks


def prune_unsendable(messages: list[dict], route: Route | None) -> list[dict]:
    """
    Хранилище для маршрута не работает (или прямой режим): файлы, которые всё
    равно уйдут пометкой (не влезут в INLINE_FILES_MB), заранее становятся
    пометкой — чтобы их вес в токенах не занимал окно контекста впустую.
    Тот же отбор, что в plan(): приоритет, затем свежие первыми.
    """
    if usable(route):
        return messages
    marks = _markers(messages)
    if not marks:
        return messages
    budget = max(0, int(settings.INLINE_FILES_MB)) * 1024 * 1024
    used = 0
    drop: set[tuple[int, int]] = set()
    for i in sorted(range(len(marks)), key=lambda i: (-int(marks[i].te.get("priority") or 0), -i)):
        te = marks[i].te
        if int(te.get("priority") or 0) >= 2:
            continue
        need = _inline_chars(te)
        if used + need <= budget:
            used += need
        else:
            drop.add((marks[i].mi, marks[i].bi))
    if not drop:
        return messages
    out = []
    for mi, m in enumerate(messages):
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any((mi, bi) in drop for bi in range(len(c))):
            m = {**m, "content": [{"type": "text", "text": NOTE_OVER} if (mi, bi) in drop else b
                                  for bi, b in enumerate(c)]}
        out.append(m)
    return out


def inline_payload(messages) -> int:
    """Сколько символов данных (data:URI, input_audio) уже лежит в запросе."""
    total = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for b in c:
            if not isinstance(b, dict) or MARK in b:
                continue
            if b.get("type") == "image_url":
                url = (b.get("image_url") or {}).get("url") or ""
                if url[:5].lower() == "data:":
                    total += len(url)
            elif b.get("type") == "input_audio":
                total += len((b.get("input_audio") or {}).get("data") or "")
    return total


async def _load_blobs(ids: list[int]) -> dict[int, str]:
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models import AttachmentBlob

    if not ids:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(AttachmentBlob.id, AttachmentBlob.data).where(AttachmentBlob.id.in_(sorted(set(ids))))
        )).all()
    return {i: d or "" for i, d in rows}


# Документы истории (Word, ODT, RTF…) ссылкой не идут и конвертируются в PDF
# или текст. Конвертация — LibreOffice, секунды процессора: в event loop она
# заморозила бы весь сервер, а без окна «12 последних сообщений» документов в
# запросе стало больше. Поэтому — в отдельном потоке по одному, и результат
# помнится (документ в чате не меняется).
_DOC_CACHE: dict[tuple, tuple[int, list[dict]]] = {}
_DOC_CACHE_BYTES = 64 * 1024 * 1024
_doc_lock: asyncio.Lock | None = None


def _doc_key(te: dict, data: str) -> tuple:
    # По СОДЕРЖИМОМУ, а не по blob_id: id удалённого файла SQLite отдаёт
    # новому, и кэш выдал бы старый документ вместо исправленного. Хэш дёшев
    # рядом с конвертацией LibreOffice.
    digest = hashlib.blake2b(data.encode("ascii", "ignore"), digest_size=16).hexdigest()
    return (digest, te.get("mime") or "", te.get("name") or "")


def _convert(te: dict, data: str) -> list[dict]:
    """Сам файл целиком — тем же конвертером, что файлы текущей реплики."""
    from backend.llm_gateway import _content_from_attachment
    from backend.schemas import AttachmentIn

    kind = te.get("type") if te.get("type") in ("image", "audio", "video", "document") else "document"
    try:
        att = AttachmentIn(type=kind, data=data, mime=te.get("mime") or None,
                           name=te.get("name") or None)
        return [_content_from_attachment(att)]
    except Exception:  # noqa: BLE001 — битый файл не роняет ход
        return [{"type": "text", "text": "[файл не удалось прочитать]"}]


async def _inline_blocks(te: dict, data: str) -> list[dict]:
    global _doc_lock
    if te.get("kind") != "document":
        return _convert(te, data)
    key = _doc_key(te, data) if data else None
    if key in _DOC_CACHE:
        weight, blocks = _DOC_CACHE.pop(key)
        _DOC_CACHE[key] = (weight, blocks)   # LRU: недавний — в конец
        return blocks
    if _doc_lock is None:
        _doc_lock = asyncio.Lock()
    async with _doc_lock:
        blocks = await asyncio.to_thread(_convert, te, data)
    weight = sum(len((b.get("image_url") or {}).get("url") or b.get("text") or "") for b in blocks)
    if key is not None and weight <= _DOC_CACHE_BYTES // 4:
        _DOC_CACHE[key] = (weight, blocks)
        while sum(w for w, _ in _DOC_CACHE.values()) > _DOC_CACHE_BYTES:
            _DOC_CACHE.pop(next(iter(_DOC_CACHE)))
    return blocks


@dataclass
class Materialized:
    messages: list
    used: list = field(default_factory=list)     # [{"uri", "bytes"}] — ссылки в запросе
    summary: dict = field(default_factory=dict)
    # id() блоков-файлов ИСТОРИИ, ушедших целиком: если прокси не переварит
    # запрос, повтор снимет именно их (в группе они приклеены к последней
    # реплике, и «всё до последней реплики» их бы не задело).
    history_inline: set = field(default_factory=set)
    # id() ссылок на файлы истории (для повтора при превышении лимита токенов).
    history_refs: set = field(default_factory=set)
    # id() блоков файлов ОТВЕЧАЕМОЙ реплики (приоритет 2): их не снимают повторы,
    # и по ним решается авто-режим рассуждений — даже если после них в запросе
    # есть ещё реплика («Продолжи ответ»).
    answered: set = field(default_factory=set)

    @property
    def ref_mb(self) -> float:
        return sum(int(u.get("bytes") or 0) for u in self.used) / (1024 * 1024)


async def materialize(messages, params=None, connection=None, *, exclude_refs: set | None = None,
                      background: bool = True) -> Materialized:
    """
    Чистые копии messages для провайдера: заготовки → ссылка / файл целиком /
    пометка; метки MARK сняты.

    :param exclude_refs: ссылки, которые модель не смогла прочитать, — вместо них
        файл целиком (в пределах лимита) или пометка.
    :param background: ставить недостающие файлы в очередь загрузки.
    """
    p, marks = await plan(messages, params, connection, exclude_refs=exclude_refs)
    if not marks:
        return Materialized(messages)
    if background:
        _schedule(p)
    data = await _load_blobs([marks[i].te["blob_id"] for i in p.inline if marks[i].te.get("blob_id")])
    replace: dict[tuple[int, int], list[dict]] = {}
    used: list[dict] = []
    history_inline: set = set()
    history_refs: set = set()
    answered: set = set()
    for i, mk in enumerate(marks):
        te = mk.te
        is_answered = int(te.get("priority") or 0) >= 2
        if i in p.refs:
            ref = p.refs[i]
            block = ref_block(ref["uri"], ref.get("mime") or norm_mime(te["kind"], te["mime"]))
            replace[(mk.mi, mk.bi)] = [block]
            used.append({"uri": ref["uri"], "bytes": ref.get("bytes") or te.get("bytes") or 0})
            (answered if is_answered else history_refs).add(id(block))
        elif i in p.inline:
            payload = te.get("data") or data.get(te.get("blob_id") or -1, "")
            blocks = (await _inline_blocks(te, payload) if payload
                      else [{"type": "text", "text": "[файл недоступен]"}])
            (answered if is_answered else history_inline).update(id(b) for b in blocks)
            replace[(mk.mi, mk.bi)] = blocks
        else:
            replace[(mk.mi, mk.bi)] = [{"type": "text", "text": NOTE_PENDING if i in p.pending else NOTE_OVER}]
    del data
    out: list[dict] = []
    for mi, m in enumerate(messages):
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            out.append(m)
            continue
        blocks: list = []
        for bi, b in enumerate(c):
            if (mi, bi) in replace:
                blocks.extend(replace[(mi, bi)])
            elif isinstance(b, dict) and MARK in b:
                blocks.append({k: v for k, v in b.items() if k != MARK})
            else:
                blocks.append(b)
        out.append({**m, "content": blocks})
    return Materialized(out, used, p.summary(), history_inline, history_refs, answered)


async def preview(messages, params=None, connection=None, db=None) -> dict:
    """Сводка для инспектора: сколько файлов пойдёт ссылкой, целиком и ждёт загрузки."""
    try:
        p, _ = await plan(messages, params, connection, db=db)
    except Exception:  # noqa: BLE001 — инспектор не падает из-за сводки
        return {}
    out = p.summary()
    cap = capability(p.route)
    out["storage"] = ("disabled" if not settings.MEDIA_REFS else "direct" if p.route is None
                      else "ok" if p.family else "unchecked" if cap is None else "off")
    return out


# ============================ сбой чтения ссылок ============================

_REF_ERROR_RE = re.compile(
    r"gs://|/v1beta/files/|file_?uri|file_?data|no such object|storage\.objects|"
    r"not in an active state|failed_precondition|"
    r"file .{0,60}(not found|expired|does not exist|not exist|deleted|may not exist)|"
    r"(cannot|can't|unable to|failed to) (fetch|read|access|retrieve) .{0,60}(file|uri|url|object)",
    re.IGNORECASE,
)
_INACTIVE_RE = re.compile(r"not in an active state|failed_precondition|is processing|state.{0,20}processing",
                          re.IGNORECASE)
_MIME_RE = re.compile(r"unsupported mime|mime type .{0,40}not supported|file type not supported|"
                      r"unsupported (file|media) type", re.IGNORECASE)
_MISSING_RE = re.compile(r"not found|no such object|may not exist|does not exist|expired|deleted|404",
                         re.IGNORECASE)
_PERMISSION_RE = re.compile(r"permission|access denied|forbidden|403", re.IGNORECASE)
# Сколько раз ссылка была «под подозрением» (ошибка без явного виновника) у
# модели. Успешный ответ с ней обнуляет счёт; _SUSPECT_LIMIT подряд — модель её
# не читает, ссылкой этот файл для неё больше не идёт.
_suspects: dict[tuple[str, str], int] = {}
_SUSPECT_LIMIT = 3


def _ref_forms(uri: str) -> list[str]:
    """Как ссылка может выглядеть в тексте ошибки провайдера."""
    forms = {uri, quote(uri, safe=""), quote(uri, safe="/:")}
    if uri.startswith(GCS_PREFIX):
        path = uri[len(GCS_PREFIX):]
        forms |= {path, quote(path, safe="/"), path.rsplit("/", 1)[-1]}
    elif uri.startswith(FILES_API_PREFIX):
        fid = uri[len(FILES_API_PREFIX):]
        forms |= {"files/" + fid, fid}
    return [f for f in forms if len(f) >= 8]


async def handle_ref_error(used: list[dict], error, params=None, connection=None) -> set[str] | None:
    """
    Запрос со ссылками упал до первого токена. Разбираем, при чём тут ссылки,
    и возвращаем ссылки, которые на повторе надо заменить файлами целиком
    (None — ошибка не про ссылки, повтор не нужен).

      * файл ещё обрабатывается (Files API, not in an ACTIVE state) — ссылку
        не трогаем, только откладываем её использование;
      * формат файла модель не принимает — ссылкой этот файл больше не пойдёт;
      * названная в ошибке ссылка не найдена — она сбойная, файл загрузится
        заново (старая копия будет удалена);
      * нет прав — перезагрузка не поможет: модель перепроверяется (probe), и
        только провал проверки выключает ссылки для неё;
      * по тексту не понять, какая ссылка виновата, — ничего не портим: этот
        ход идёт с файлами целиком, модель перепроверяется, а ссылки запроса
        получают «подозрение» (успешный ответ с ними его снимает).

    Всё меняется только в хранилище ЭТОЙ модели: ссылки GCS общие для моделей
    одного бакета, и сбой одной модели не должен ломать их другим.
    """
    text = str(error or "")
    uris = [u["uri"] for u in used if u.get("uri")]
    if not uris or not _REF_ERROR_RE.search(text) and not _MIME_RE.search(text):
        return None
    named = {u for u in uris if any(f in text for f in _ref_forms(u))}
    route = route_for(params, connection)
    cap = capability(route) or {}
    scope = scope_for(route, cap) if (route is not None and cap.get("family")) else ""
    if _INACTIVE_RE.search(text):
        targets = named or {u for u in uris if u.startswith(FILES_API_PREFIX)} or set(uris)
        await _update_refs(scope, targets, usable_after=_now() + timedelta(seconds=90))
        return targets
    if _MIME_RE.search(text):
        if named:
            await _update_refs(scope, named, status="rejected", error=_short_error(text))
        return named or set(uris)
    if named and _MISSING_RE.search(text) and not _PERMISSION_RE.search(text):
        await _update_refs(scope, named, status="failed", error=_short_error(text),
                           next_try_at=_now() + timedelta(minutes=1))
        return named
    if route is not None:
        uploader.enqueue_probe(route, force=True)
        # «Подозрение» — только когда виновник однозначен: в запросе была одна
        # ссылка. Иначе браковали бы и здоровые ссылки, ехавшие с ней вместе.
        if len(uris) == 1 and not _PERMISSION_RE.search(text):
            key = (route.cap_key, uris[0])
            _suspects[key] = _suspects.get(key, 0) + 1
            if _suspects[key] >= _SUSPECT_LIMIT:
                _suspects.pop(key, None)
                await _update_refs(scope, set(uris), status="failed",
                                   error="Модель несколько раз не смогла прочитать файл по ссылке",
                                   next_try_at=_now() + timedelta(hours=6))
    return set(uris)


def note_success(used: list[dict], params=None, connection=None) -> None:
    """Ответ с этими ссылками пришёл — значит, модель их читает: снять подозрения."""
    if not _suspects or not used:
        return
    route = route_for(params, connection)
    if route is None:
        return
    for u in used:
        _suspects.pop((route.cap_key, u.get("uri") or ""), None)


async def _update_refs(scope: str, uris: set[str], **values) -> None:
    from sqlalchemy import update

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    if not uris or not scope:
        return

    async def _write():
        async with AsyncSessionLocal() as db:
            await db.execute(update(MediaRef).where(
                MediaRef.scope == scope, MediaRef.uri.in_(sorted(uris))).values(**values))
            await db.commit()

    try:
        # shield: отмена хода (кнопка «Стоп») не обрывает запись посреди SQL.
        await asyncio.shield(_write())
    except Exception:  # noqa: BLE001
        log.exception("Не удалось обновить состояние ссылок на файлы")


# ============================ проверка хранилища ============================

def explain(error: str) -> str:
    """Понятная причина, почему хранилище у прокси не работает."""
    low = (error or "").lower()
    if "bucket" in low:
        return ("У прокси не задан бакет Google Cloud Storage для загрузки файлов "
                "(переменная окружения GCS_BUCKET_NAME у процесса прокси).")
    if "files_settings" in low or "model_list" in low or "model not found" in low:
        return ("Прокси не понял, куда загружать файл для этой модели — вероятно, его версия "
                "не умеет загрузку по модели (нужен LiteLLM новее) или модели нет в model_list.")
    if "managed" in low:
        return "Прокси требует managed files (enterprise) — загрузка по модели выключена."
    if "401" in low or "unauthorized" in low or "api key" in low:
        return "Прокси не принял ключ доступа."
    if "403" in low or "permission" in low or "forbidden" in low:
        return "Нет прав: у сервисного аккаунта прокси нет доступа к бакету (нужна роль Storage Object User)."
    return "Прокси не принял загрузку файла."


# Картинка 1×1 PNG: двоичные байты (0x89…) проверяют, что прокси не портит
# файлы как текст, а модель читает по ссылке настоящий image/png.
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


async def _alias_deployments(route: Route) -> list[str] | None:
    """
    Модели провайдера за псевдонимом (litellm_params.model из /model/info).
    None — прокси не ответил (старый, нет прав) — тогда решает сама проверка.
    """
    import httpx

    headers = {"Authorization": f"Bearer {route.api_key}"}
    for path in ("/model/info", "/v1/model/info"):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(route.base_url + path, headers=headers)
            if resp.status_code != 200:
                continue
            data = resp.json().get("data") or []
        except Exception:  # noqa: BLE001
            continue
        found = [((d.get("litellm_params") or {}).get("model") or "")
                 for d in data if isinstance(d, dict) and d.get("model_name") == route.alias]
        # Нет точного совпадения (шаблон «gemini/*», псевдоним группы) — решает
        # сама проверка загрузкой и чтением.
        return found or None
    return None


def _files_client(route: Route, timeout: float):
    """
    Клиент OpenAI SDK к /files прокси. Не через litellm.acreate_file: клиент
    LiteLLM многих версий не умеет загружать файлы через litellm_proxy («LiteLLM
    doesn't support litellm_proxy for 'create_file'»), а прокси говорит на
    OpenAI-совместимом /files — пакет openai стоит вместе с LiteLLM всегда.
    max_retries=0: повтор SDK заново гонит файл целиком через прокси.
    """
    from openai import AsyncOpenAI

    headers = {"x-litellm-model": route.alias} if route.alias.isascii() else {}
    return AsyncOpenAI(base_url=route.base_url, api_key=route.api_key, max_retries=0,
                       timeout=timeout, default_headers=headers)


async def _create_file(route: Route, file, timeout: int):
    """Загрузка файла через прокси по модели (заголовок x-litellm-model и поле model)."""
    client = _files_client(route, timeout)
    try:
        last = None
        for purpose in ("user_data", "assistants"):
            try:
                return await asyncio.wait_for(client.files.create(
                    file=file, purpose=purpose, extra_body={"model": route.alias}), timeout + 30)
            except Exception as exc:  # noqa: BLE001
                last = exc
                if "purpose" not in str(exc).lower():
                    raise
                if hasattr(file[1], "seek"):
                    file[1].seek(0)
        raise last
    finally:
        await client.close()


async def _delete_remote(route: Route, file_id: str) -> bool:
    """
    Удаляет копию файла в хранилище. True — дело сделано (удалена, уже не
    существует или удалить её нельзя в принципе), False — временный сбой,
    повторить позже.
    """
    client = _files_client(route, 60)
    try:
        await asyncio.wait_for(client.files.delete(file_id), 75)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_transient(exc):
            log.info("Копию файла в хранилище удалить пока не удалось: %s", _short_error(_error_text(exc)))
            return False
        log.info("Копию файла в хранилище удалить нельзя: %s", _short_error(_error_text(exc)))
        return True
    finally:
        await client.close()


_TRANSIENT_RE = re.compile(
    r"\b(429|500|502|503|504)\b|rate.?limit|resource.?exhausted|overloaded|unavailable|"
    r"timed? ?out|timeout|connection|temporarily|try again", re.IGNORECASE)


def is_transient(exc) -> bool:
    """Временный сбой (лимит, перегрузка, сеть, таймаут) — не повод выключать хранилище."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and (code == 429 or code >= 500):
        return True
    return bool(_TRANSIENT_RE.search(f"{type(exc).__name__}: {exc}"))


def _error_text(exc) -> str:
    """Текст ошибки для людей: у таймаута asyncio он пустой."""
    text = str(exc).strip()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and not text:
        return "таймаут"
    return text or type(exc).__name__


async def _probe_retry_soon(route: Route, exc) -> dict:
    """
    Проверка упала из-за временного сбоя: прежний итог не трогаем (рабочее
    хранилище не выключается на 6 часов из-за одного 429), но перепроверяем
    минут через 15.
    """
    prev = capability(route)
    if prev and prev.get("ok"):
        prev = dict(prev, checked_at=time.time() - _RECHECK_OK + 900)
        _caps[route.cap_key] = prev
        return prev
    cap = await _set_cap(route, (prev or {}).get("family") or "", ok=False, error=(
        "Проверка не прошла из-за временного сбоя прокси или провайдера — повторю через "
        "15 минут. Ответ: " + _short_error(_error_text(exc))), bucket=(prev or {}).get("bucket") or "")
    cap["until"] = time.time() + 900
    return cap


async def probe(route: Route) -> dict:
    """
    Проверяет хранилище прокси для модели: модель должна быть Gemini, крошечная
    картинка — загрузиться и прочитаться моделью по ссылке. Итог запоминается.
    Временный сбой (лимит, перегрузка, сеть) итог не портит — см. _probe_retry_soon.
    """
    import litellm

    deployments = await _alias_deployments(route)
    if deployments:
        kinds = {"gcs" if d.startswith("vertex_ai") and "gemini" in d.lower()
                 else "files_api" if d.startswith("gemini/") else "other" for d in deployments}
        if "other" in kinds or len(kinds) > 1:
            return await _set_cap(route, "", ok=False, error=(
                f"Модель «{route.alias}» на прокси — не только Gemini ({', '.join(deployments)}). "
                "Ссылки на файлы понимают модели Gemini через Vertex AI или Gemini API."))
        if kinds == {"files_api"} and len(deployments) > 1:
            return await _set_cap(route, "", ok=False, error=(
                f"У модели «{route.alias}» несколько развёртываний Gemini API: файл Files API "
                "виден только своему ключу. Оставьте одно развёртывание."))
    uri = ""
    file_id = ""
    try:
        resp = await _create_file(route, ("taleengine-probe.png", _PROBE_PNG, "image/png"), 60)
        file_id = getattr(resp, "id", "") or ""
        uri = decode_file_id(file_id)
        family = family_of(uri)
        if not family:
            return await _set_cap(route, "", ok=False, error=(
                "Прокси загрузил файл не в хранилище Google (ответ: " + _short_error(file_id)
                + "). Ссылки работают для моделей Vertex AI (GCS) и Gemini API (Files API)."))
        try:
            await asyncio.wait_for(litellm.acompletion(
                model=f"litellm_proxy/{route.alias}", api_base=route.base_url, api_key=route.api_key,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": "Ответь одним словом: ок."},
                    ref_block(uri, "image/png"),
                ]}],
                max_tokens=64, num_retries=0, timeout=90), 100)
        except Exception as exc:  # noqa: BLE001
            if is_transient(exc):
                return await _probe_retry_soon(route, exc)
            return await _set_cap(route, family, ok=False, error=(
                "Файл загрузился, но модель не смогла прочитать его по ссылке: "
                + _short_error(_error_text(exc))
                + (" Если бакет в другом проекте Google Cloud, дайте сервисному агенту Vertex AI "
                   "право чтения бакета." if family == "gcs" else "")))
        return await _set_cap(route, family, ok=True, bucket=gcs_bucket(uri))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_transient(exc) and not _CAPABILITY_RE.search(str(exc)):
            return await _probe_retry_soon(route, exc)
        return await _set_cap(route, "", ok=False, error=explain(str(exc)) + " Ответ прокси: "
                              + _short_error(_error_text(exc)))
    finally:
        if file_id and family_of(uri) == "gcs":
            # В фоне: при остановке сервера проверка не должна ждать удаления.
            if uploader.running:
                uploader.enqueue_delete(route, file_id)
            else:
                try:
                    await asyncio.wait_for(_delete_remote(route, file_id), 10)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass


# ============================ фоновый загрузчик ============================

_LANE_PROBE, _LANE_TURN, _LANE_BACKFILL, _LANE_DELETE = 0, 1, 2, 3
# Постоянные отказы прокси по самому файлу (размер, формат): повтор не поможет.
_REJECT_RE = re.compile(r"\b(413|415)\b|too large|unsupported|not supported|"
                        r"invalid (file|mime)|max_file_size", re.IGNORECASE)
# Отказ по самому файлу не вечен: через неделю (или после смены прокси) — ещё раз.
_REJECT_TTL = timedelta(days=7)
_CAPABILITY_RE = re.compile(r"bucket|files_settings|model_list|model not found|managed files|"
                            r"\b401\b|unauthorized|invalid api key|authentication", re.IGNORECASE)


@dataclass
class _Job:
    lane: int
    seq: int
    kind: str                 # probe | upload | delete
    route: Route
    te: dict | None = None
    file_id: str = ""
    trash_id: int | None = None   # строка корзины: убрать, когда копия удалена


class _Uploader:
    """
    Очередь загрузок в хранилище провайдера. Один файл за раз, только когда
    модель никому не отвечает (и ещё 2 секунды тишины), секунда между
    загрузками. Очередь в памяти: после рестарта недостающие файлы встанут в
    неё снова при первой же сборке контекста — ничего не теряется.
    """

    def __init__(self) -> None:
        self._jobs: dict[tuple, _Job] = {}
        self._seq = 0
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        # Когда убрать ссылки на удалённые файлы (monotonic; 0 — по расписанию).
        self._sweep_at = 0.0
        self.current: dict | None = None
        self.waiting = False   # ждёт паузы в ответах модели
        self._fails = 0         # подряд сбоев связи с прокси
        self._pause_until = 0.0
        self.done = 0
        self.failed = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def queued(self) -> int:
        return sum(1 for j in self._jobs.values() if j.kind == "upload")

    _MAX_JOBS = 2000

    def _put(self, key: tuple, job: _Job) -> None:
        old = self._jobs.get(key)
        if old is not None and old.lane <= job.lane:
            return
        if old is None and len(self._jobs) >= self._MAX_JOBS:
            # Очередь не растёт без конца: вытесняем самую старую фоновую
            # загрузку — она встанет снова, когда файл опять попадёт в запрос.
            victim = min((k for k, j in self._jobs.items() if j.lane == _LANE_BACKFILL),
                         key=lambda k: self._jobs[k].seq, default=None)
            if victim is None or job.lane >= _LANE_BACKFILL:
                return
            self._jobs.pop(victim, None)
        self._seq += 1
        job.seq = self._seq
        self._jobs[key] = job
        if self._wake is not None:
            self._wake.set()

    def enqueue_upload(self, route: Route, te: dict, lane: int = _LANE_BACKFILL) -> None:
        cap = capability(route) or {}
        if (not self.running or not cap.get("ok") or not te.get("blob_id")
                or not can_ref(te.get("kind"))):
            return
        self._put(("up", scope_for(route, cap), int(te["blob_id"])),
                  _Job(lane, 0, "upload", route, te=dict(te)))

    def enqueue_probe(self, route: Route, force: bool = False) -> None:
        if not self.running:
            return
        if not force and not _cap_stale(capability(route)):
            return
        self._put(("probe", route.cap_key), _Job(_LANE_PROBE, 0, "probe", route))

    def enqueue_delete(self, route: Route, file_id: str, trash_id: int | None = None) -> None:
        if self.running and file_id:
            self._put(("del", file_id), _Job(_LANE_DELETE, 0, "delete", route, file_id=file_id,
                                             trash_id=trash_id))

    def drop_scope(self, scope: str) -> None:
        for key in [k for k in self._jobs if k[0] == "up" and k[1] == scope]:
            self._jobs.pop(key, None)

    def pause(self) -> None:
        """Прокси не отвечает: очередь ждёт 1, 2, 4… минуты (до 30)."""
        self._fails += 1
        self._pause_until = time.monotonic() + min(1800, 60 * 2 ** (self._fails - 1))

    def sweep_soon(self) -> None:
        """Файлы удалены — уборка через несколько секунд (после commit удаления)."""
        self._sweep_at = time.monotonic() + 5
        if self._wake is not None:
            self._wake.set()

    def start(self) -> None:
        if self.running or not settings.MEDIA_REFS:
            return
        self._stopping = False
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="media-uploader")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        if self._wake is not None:
            self._wake.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._jobs.clear()

    def _next(self) -> tuple | None:
        if not self._jobs:
            return None
        return min(self._jobs, key=lambda k: (self._jobs[k].lane, self._jobs[k].seq))

    async def _quiet(self, job: _Job) -> None:
        """
        Ждём, пока никто не ждёт ответа модели (ни генерации, ни стрима), и
        ещё немного тишины: 2 секунды, для файлов больше 8 МБ — 20. Сервер,
        который не замолкает, не должен вечно держать очередь: файл реплики до
        32 МБ идёт после 10 минут ожидания, фоновые — после часа.
        """
        from backend import llm_gateway
        from backend.generation import generation_manager

        size = int((job.te or {}).get("bytes") or 0)
        quiet = 20 if size > 8 * 1024 * 1024 else 2
        if job.kind != "upload" or job.lane == _LANE_TURN:
            deadline = 600 if size <= 32 * 1024 * 1024 else 3600
        else:
            deadline = 3600
        started = time.monotonic()
        self.waiting = True
        try:
            while not self._stopping:
                busy = llm_gateway.user_streams() or generation_manager.active()
                if not busy and llm_gateway.idle_for() >= quiet:
                    return
                if time.monotonic() - started >= deadline:
                    return
                await asyncio.sleep(1)
        finally:
            self.waiting = False

    async def _run(self) -> None:
        try:
            await asyncio.sleep(max(0, settings.MEDIA_UPLOAD_START_DELAY))
            last_sweep = 0.0
            while not self._stopping:
                now = time.monotonic()
                if (self._sweep_at and now >= self._sweep_at) or now - last_sweep > 1800:
                    self._sweep_at = 0.0
                    last_sweep = now
                    await self._sweep()
                key = self._next()
                if key is None:
                    self._wake.clear()
                    wait = 1800.0
                    if self._sweep_at:
                        wait = max(0.5, min(wait, self._sweep_at - time.monotonic()))
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                    continue
                wait = self._pause_until - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(min(wait, 60))
                    continue
                job = self._jobs.get(key)
                if job is None:
                    continue
                await self._quiet(job)
                # Пока ждали тишины, могла прийти задача важнее (проверка,
                # файл свежей реплики) — берём лучшую на этот момент.
                if self._next() != key:
                    continue
                job = self._jobs.pop(key, None)
                if job is None:
                    continue
                try:
                    if job.kind == "probe":
                        await probe(job.route)
                    elif job.kind == "upload":
                        await self._upload(job)
                    elif job.kind == "delete":
                        if await _delete_remote(job.route, job.file_id) and job.trash_id:
                            await _drop_trash([job.trash_id])
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — загрузчик не падает никогда
                    log.exception("Фоновая задача хранилища файлов упала")
                finally:
                    self.current = None
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _sweep(self) -> None:
        """
        Корзина копий удалённых файлов → задачи удаления в хранилище. Строка
        корзины уходит, только когда хранилище подтвердило удаление (или его
        нельзя сделать в принципе): рестарт и сбои прокси копии не теряют.
        """
        try:
            trash = await sweep_orphans()
        except Exception:  # noqa: BLE001
            log.exception("Уборка ссылок на удалённые файлы не удалась")
            return
        done: list[int] = []
        for tid, scope, uri in trash:
            if not uri.startswith(GCS_PREFIX):
                done.append(tid)   # Files API удалит сам через двое суток
                continue
            # Удаляет любая рабочая модель этого прокси с тем же бакетом: модель,
            # через которую файл загружали, могла исчезнуть из конфигурации.
            for cap in list(_caps.values()):
                route = _route_from_cap(cap)
                if route is not None and cap.get("ok") and scope_for(route, cap) == scope:
                    self.enqueue_delete(route, encode_file_id(uri, route.alias), trash_id=tid)
                    break
        if done:
            await _drop_trash(done)

    async def _upload(self, job: _Job) -> None:
        import secrets

        from sqlalchemy import select

        from backend.database import AsyncSessionLocal
        from backend.models import AttachmentBlob, MediaRef

        route, te = job.route, job.te or {}
        cap = capability(route) or {}
        family = usable(route)
        if not family:
            return
        scope = scope_for(route, cap)
        blob_id = int(te["blob_id"])
        size = int(te.get("bytes") or 0)
        mime = norm_mime(te.get("kind") or "", te.get("mime"))
        token = secrets.token_hex(16)
        # Резерв: в ОДНОЙ транзакции — блоб ещё есть, ссылки ещё нет, строка
        # «uploading» с токеном. Удалят файл посреди загрузки — триггер снесёт
        # резерв, и результат не запишется (см. _write_ref).
        async with AsyncSessionLocal() as db:
            owner = (await db.execute(select(AttachmentBlob.message_id).where(
                AttachmentBlob.id == blob_id))).first()
            if owner is None:
                return   # файл удалили, пока он ждал очереди
            owner = owner[0]
            row = (await db.execute(select(MediaRef).where(
                MediaRef.scope == scope, MediaRef.blob_id == blob_id))).scalar_one_or_none()
            same = row is not None and row.blob_owner == owner
            if same:
                if row.status == "rejected":
                    return
                fresh = row.expires_at is None or row.expires_at - _EXPIRY_MARGIN > _now()
                if row.status == "ready" and row.uri and fresh:
                    return
                if row.status in ("failed", "uploading") and row.next_try_at and row.next_try_at > _now():
                    return
            attempts = row.attempts if (same and row.status in ("failed", "uploading")) else 0
            if row is None:
                row = MediaRef(scope=scope, blob_id=blob_id, uri="", file_id="")
                db.add(row)
            elif not same:
                row.uri, row.file_id = "", ""   # чужая ссылка прежнего владельца id
            row.blob_owner = owner
            row.family = family
            row.status = "uploading"
            row.upload_token = token
            row.attempts = attempts
            row.next_try_at = _now() + timedelta(seconds=_upload_timeout(size) + 120)
            await db.commit()
            ref_id = row.id

        async def _fail(**kw):
            await _save_ref(ref_id, token, route, **kw)

        self.current = {"name": te.get("name") or f"файл #{blob_id}",
                        "mb": round(size / 1048576, 1), "since": time.time()}
        if size > settings.MEDIA_UPLOAD_MAX_MB * 1024 * 1024:
            await _fail(status="rejected", mime=mime, size=size, next_try_at=_now() + _REJECT_TTL,
                        error=f"Файл больше {settings.MEDIA_UPLOAD_MAX_MB} МБ — ссылкой не отправляется")
            return
        short = _admit(size)
        if short:
            # Не сбой, а «не сейчас»: попытка не засчитывается, файл встанет в
            # очередь снова через 10 минут.
            log.info("Загрузка файла #%s отложена: %s", blob_id, short)
            await _fail(status="failed", mime=mime, size=size, attempts=attempts, error=short,
                        next_try_at=_now() + timedelta(minutes=10))
            return
        try:
            got = await self._read_blob(blob_id)
            if got is None:
                return
            spool, size, digest = got
            if size <= 0:
                spool.close()
                return
            if size > settings.MEDIA_UPLOAD_MAX_MB * 1024 * 1024:
                spool.close()
                await _fail(status="rejected", mime=mime, size=size, next_try_at=_now() + _REJECT_TTL,
                            error=f"Файл больше {settings.MEDIA_UPLOAD_MAX_MB} МБ — ссылкой не отправляется")
                return
            name = _filename(blob_id, digest, mime)
            uri, file_id = await self._send(route, spool, name, mime, size, family)
        except Exception as exc:  # noqa: BLE001
            self.failed += 1
            text = f"{type(exc).__name__}: {_error_text(exc)}"
            timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or bool(
                re.search(r"timed? ?out|timeout", text, re.IGNORECASE))
            log.warning("Загрузка файла #%s в хранилище не удалась: %s", blob_id, _short_error(text))
            if is_transient(exc) and re.search(r"connect|refused|reset|unreachable|name or service",
                                                text, re.IGNORECASE):
                # Прокси недоступен: не гоняем по кругу чтение и декодирование
                # каждого файла из очереди — пауза всей очереди.
                self.pause()
            if _CAPABILITY_RE.search(text):
                # Сломалось само хранилище — не файл: перепроверим модель позже,
                # очередь этого хранилища не гоняем впустую.
                await _set_cap(route, family, ok=False, error=explain(text) + " " + _short_error(text),
                               bucket=cap.get("bucket") or "")
                self.drop_scope(scope)
                await _fail(status="failed", mime=mime, size=size, attempts=attempts,
                            error=_short_error(text), next_try_at=_now() + _RECHECK_FAILED)
                return
            code = getattr(exc, "status_code", None)
            if (code in (413, 415) or (_REJECT_RE.search(text) and not is_transient(exc))):
                await _fail(status="rejected", mime=mime, size=size, error=_short_error(text),
                            next_try_at=_now() + _REJECT_TTL)
                return
            attempts += 1
            delay = (timedelta(hours=24) if attempts >= _MAX_ATTEMPTS
                     else min(timedelta(hours=6), timedelta(minutes=2 ** (attempts - 1))))
            if timed_out:
                # Файл мог дойти, а ответ — нет: частые повторы плодили бы
                # копии-сироты в бакете. Не раньше чем через час.
                delay = max(delay, timedelta(hours=1))
                log.info("Файл #%s мог остаться в хранилище без ссылки (обрыв после отправки)", blob_id)
            await _fail(status="failed", mime=mime, size=size, attempts=attempts,
                        error=_short_error(text), next_try_at=_now() + delay)
            return
        stored = await _save_ref(
            ref_id, token, route, status="ready", mime=mime, size=size, uri=uri, file_id=file_id,
            usable_after=_usable_after(family, te.get("kind") or "", size),
            expires_at=(_now() + _FILES_API_TTL) if family == "files_api" else None)
        self._fails = 0
        if stored:
            self.done += 1
        elif family == "gcs":
            # Файл удалили, пока он грузился: свежая копия никому не нужна.
            self.enqueue_delete(route, file_id)

    async def _read_blob(self, blob_id: int):
        """
        Данные файла → временный файл (до 64 МБ — в памяти, крупнее — на диске)
        и его sha256. base64 декодируется кусками прямо из строки из базы, без
        копий целиком, в отдельном потоке: сотня мегабайт не подвешивает event
        loop сервера, пока другие пользователи общаются.
        """
        from sqlalchemy import select

        from backend.database import AsyncSessionLocal
        from backend.models import AttachmentBlob

        async with AsyncSessionLocal() as db:
            data = (await db.execute(select(AttachmentBlob.data).where(
                AttachmentBlob.id == blob_id))).scalar_one_or_none()
        if not data:
            return None
        try:
            return await asyncio.to_thread(_decode_to_spool, data)
        finally:
            del data

    async def _send(self, route: Route, spool, name: str, mime: str, size: int, family: str):
        timeout = _upload_timeout(size)
        try:
            resp = await _create_file(route, (name, spool, mime), timeout)
        finally:
            spool.close()
        file_id = getattr(resp, "id", "") or ""
        uri = decode_file_id(file_id)
        if family_of(uri) != family:
            raise RuntimeError(f"Прокси вернул неожиданную ссылку: {_short_error(file_id)}")
        if family == "gcs" and name not in uri:
            # Старый прокси складывает файл под нашим именем — оно уникально
            # (id + хэш), иначе файлы с одинаковым именем затёрли бы друг друга.
            log.debug("Хранилище переименовало файл: %s", uri)
        got = int(getattr(resp, "bytes", 0) or 0)
        if got and got != size:
            raise RuntimeError(f"Прокси принял {got} байт из {size}")
        return uri, file_id


uploader = _Uploader()


def _mem_available() -> int | None:
    """Свободная память (MemAvailable из /proc/meminfo), байт; None — не Linux."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _admit(size: int) -> str:
    """
    Хватит ли серверу ресурсов на загрузку файла: '' — да, иначе причина.
    Строка base64 из базы (~1,33 размера) + до 64 МБ декодированного в памяти;
    крупнее — во временном файле на диске, и диску нужен запас: SQLite без
    места перестаёт писать совсем.
    """
    import shutil

    mem = _mem_available()
    if mem is not None and mem < int(size * 4 / 3) + min(size, 64 * 1024 * 1024) + 256 * 1024 * 1024:
        return "мало свободной памяти на сервере"
    if size > 64 * 1024 * 1024:
        try:
            usage = shutil.disk_usage(tempfile.gettempdir())
        except OSError:
            return ""
        if usage.free < size + max(1024 ** 3, int(usage.total * 0.05)):
            return "мало места на диске сервера"
    return ""


def _decode_to_spool(raw: str):
    """
    base64 (или data:URI) → SpooledTemporaryFile, sha256, размер. Идёт кусками
    по 4 МБ прямо по строке: пробелы и URL-safe алфавит чистятся в куске,
    хвост, не кратный 4, переносится в следующий — копии всей строки нет.
    """
    start = 0
    if raw[:5].lower() == "data:":
        # «video/webm;codecs=vp8,opus;base64,…» — запятая бывает и внутри mime.
        j = raw.find(";base64,", 0, 512)
        start = j + 8 if j >= 0 else raw.find(",") + 1
    import io

    # До 64 МБ — в памяти (BytesIO: httpx берёт размер через seek/tell), крупнее
    # — сразу на диск: SpooledTemporaryFile переливался бы на диск уже внутри
    # httpx, в event loop сервера.
    spool = io.BytesIO() if (len(raw) - start) * 3 // 4 <= 64 * 1024 * 1024 else tempfile.TemporaryFile()
    sha = hashlib.sha256()
    size = 0
    carry = ""
    step = 4 * 1024 * 1024
    table = str.maketrans("-_", "+/", " \n\r\t")
    for i in range(start, len(raw), step):
        piece = carry + raw[i:i + step].translate(table)
        usable = len(piece) - len(piece) % 4
        carry = piece[usable:]
        if usable:
            chunk = base64.b64decode(piece[:usable])
            sha.update(chunk)
            spool.write(chunk)
            size += len(chunk)
    carry = carry.rstrip("=")
    if carry:
        chunk = base64.b64decode(carry + "=" * (-len(carry) % 4))
        sha.update(chunk)
        spool.write(chunk)
        size += len(chunk)
    spool.seek(0)
    return spool, size, sha.hexdigest()


def _usable_after(family: str, kind: str, size: int) -> datetime | None:
    """
    Files API обрабатывает загруженный файл не мгновенно (PROCESSING → ACTIVE),
    а через прокси состояние файла не узнать. Даём время с запасом: ссылка до
    него не используется, а если модель всё же ответит «not in an ACTIVE
    state», ссылка просто отложится ещё (handle_ref_error).
    """
    if family != "files_api" or kind == "image":
        return None
    if kind == "video":
        return _now() + timedelta(seconds=30 + size / (5 * 1024 * 1024))
    return _now() + timedelta(seconds=15)


def _route_from_cap(cap: dict) -> Route | None:
    """Маршрут модели из записи проверки (ключ прокси — из текущего подключения)."""
    conn = _conn_cache.get("conn")
    if not conn or not cap.get("alias"):
        return None
    route = route_for(None, conn, model=cap["alias"])
    if route is None or route.base_url != cap.get("proxy"):
        return None
    return route


# Последние настройки подключения (для удаления копий, когда хода нет).
_conn_cache: dict = {}


def remember_connection(connection: dict | None) -> None:
    if connection:
        _conn_cache["conn"] = dict(connection)


def _schedule(p: Plan) -> None:
    """Ставит в очередь недостающие файлы запроса и проверку хранилища."""
    route = p.route
    if route is None or not uploader.running:
        return
    if _cap_stale(capability(route)):
        uploader.enqueue_probe(route)
    if not usable(route):
        return
    for te in p.enqueue:
        uploader.enqueue_upload(route, te, _LANE_TURN if int(te.get("priority") or 0) >= 2
                                else _LANE_BACKFILL)


def enqueue_message(metas, params=None, connection=None) -> None:
    """
    Файлы только что сохранённой реплики — первыми в очередь: к следующему ходу
    модель, скорее всего, получит их уже ссылкой. (Files API — только крупные:
    мелочь дешевле отправлять целиком, чем перезагружать каждые двое суток.)
    """
    remember_connection(connection)
    route = route_for(params, connection)
    if route is None or not uploader.running:
        return
    if _cap_stale(capability(route)):
        uploader.enqueue_probe(route)
    family = usable(route)
    if not family:
        return
    for meta in metas or []:
        ph = placeholder(meta)
        if ph is None:
            continue
        te = ph[MARK]
        if family == "files_api" and int(te.get("bytes") or 0) < _FILES_API_MIN_BYTES:
            continue
        uploader.enqueue_upload(route, te, _LANE_TURN)


_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/heic": ".heic", "image/heif": ".heif",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/webm": ".webm",
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/flac": ".flac",
    "audio/opus": ".opus", "audio/aiff": ".aiff",
    "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/3gpp": ".3gp", "video/mpeg": ".mpeg", "application/pdf": ".pdf",
}


def _filename(blob_id: int, digest: str, mime: str) -> str:
    """
    Уникальное имя из латиницы: te-<blob>-<sha256>.<ext>. Старый прокси кладёт
    файл в бакет ПОД ЭТИМ именем — «voice.ogg» из разных сообщений затирали бы
    друг друга, а кириллица и «&#?» ломали бы ссылку. Человеческое имя остаётся
    только в наших подписях.
    """
    ext = _EXT.get(mime) or ("." + re.sub(r"[^a-z0-9]", "", mime.split("/")[-1])[:8] if "/" in mime else "")
    return f"te-{blob_id}-{digest[:12]}{ext}"


def _upload_timeout(size: int) -> int:
    """Таймаут загрузки: минута + 10 с на МБ, не дольше получаса."""
    return min(1800, 60 + int(size / (1024 * 1024)) * 10)


async def _save_ref(ref_id: int, token: str, route: Route | None, **kw) -> bool:
    """
    Итог загрузки в зарезервированную строку; shield — остановка сервера не
    рвёт запись посреди SQL. False — резерва больше нет (файл удалили) или его
    перехватила другая загрузка: итог выброшен.
    """
    return await asyncio.shield(_write_ref(ref_id, token, route, **kw))


async def _write_ref(ref_id: int, token: str, route: Route | None, *, status: str, mime: str,
                     size: int, attempts: int = 0, error: str = "", uri: str = "", file_id: str = "",
                     next_try_at=None, expires_at=None, usable_after=None) -> bool:
    from backend.database import AsyncSessionLocal
    from backend.models import MediaRef

    async with AsyncSessionLocal() as db:
        row = await db.get(MediaRef, ref_id)
        if row is None or row.upload_token != token:
            return False
        if (status == "ready" and route is not None and row.uri and row.uri != uri
                and row.uri.startswith(GCS_PREFIX)):
            # Файл загружен заново (старая копия не читалась) — старую убираем.
            uploader.enqueue_delete(route, encode_file_id(row.uri, route.alias))
        row.status = status
        row.upload_token = ""
        row.mime = mime
        row.bytes = int(size or 0)
        row.attempts = attempts
        row.error = error
        row.next_try_at = next_try_at
        row.expires_at = expires_at
        row.usable_after = usable_after
        if status == "ready":
            row.uri = uri
            row.file_id = file_id
        await db.commit()
        return True


async def sweep_orphans(limit: int = 200) -> list[tuple[int, str, str]]:
    """
    Ссылки на файлы, которых больше нет, и ссылки, чей id блоба уже занят
    другим файлом (базы, где триггера ещё не было), — в корзину. Возвращает
    до limit строк корзины [(id, scope, uri)]: их копии надо стереть в
    хранилище. Сами строки корзины здесь НЕ удаляются — только после
    подтверждённого удаления копии (_drop_trash).
    """
    from sqlalchemy import delete, select

    from backend.database import AsyncSessionLocal
    from backend.models import AttachmentBlob, MediaRef, MediaRefTrash

    async with AsyncSessionLocal() as db:
        missing = (await db.execute(
            select(MediaRef.id, MediaRef.scope, MediaRef.uri).where(
                ~MediaRef.blob_id.in_(select(AttachmentBlob.id)))
        )).all()
        moved = (await db.execute(
            select(MediaRef.id, MediaRef.scope, MediaRef.uri)
            .join(AttachmentBlob, AttachmentBlob.id == MediaRef.blob_id)
            .where(MediaRef.blob_owner.is_distinct_from(AttachmentBlob.message_id))
        )).all()
        refs = list(missing) + list(moved)
        if refs:
            for _rid, scope, uri in refs:
                if uri:
                    db.add(MediaRefTrash(scope=scope, uri=uri))
            ids = [r[0] for r in refs]
            for i in range(0, len(ids), 500):
                await db.execute(delete(MediaRef).where(MediaRef.id.in_(ids[i:i + 500])))
            await db.commit()
        trash = (await db.execute(
            select(MediaRefTrash.id, MediaRefTrash.scope, MediaRefTrash.uri)
            .order_by(MediaRefTrash.id).limit(limit))).all()
    return [(t[0], t[1], t[2]) for t in trash]


async def _drop_trash(ids: list[int]) -> None:
    from sqlalchemy import delete

    from backend.database import AsyncSessionLocal
    from backend.models import MediaRefTrash

    async with AsyncSessionLocal() as db:
        await db.execute(delete(MediaRefTrash).where(MediaRefTrash.id.in_(ids)))
        await db.commit()


# ============================ статус для интерфейса ============================

async def status(db, params=None, connection=None, private: bool = False) -> dict:
    """
    Состояние хранилища для текущей модели: проверка, счётчики, очередь.
    :param private: не показывать имя загружаемого файла (он может быть чужим).
    """
    from sqlalchemy import func, select

    from backend.models import MediaRef

    remember_connection(connection)
    route = route_for(params, connection)
    cap = capability(route)
    family = usable(route)
    counts = {"ready": 0, "failed": 0, "rejected": 0, "ready_mb": 0.0}
    if route is not None and cap and cap.get("family"):
        rows = (await db.execute(
            select(MediaRef.status, func.count(), func.coalesce(func.sum(MediaRef.bytes), 0))
            .where(MediaRef.scope == scope_for(route, cap)).group_by(MediaRef.status)
        )).all()
        for st, n, size in rows:
            counts[st] = int(n)
            if st == "ready":
                counts["ready_mb"] = round(int(size or 0) / 1048576, 1)
    return {
        "enabled": bool(settings.MEDIA_REFS),
        "direct": route is None,
        "model": route.alias if route else "",
        "checked": cap is not None,
        "ok": bool(family),
        "family": (cap or {}).get("family") or "",
        "bucket": (cap or {}).get("bucket") or "",
        "error": (cap or {}).get("error") or "",
        "checked_at": (cap or {}).get("checked_at"),
        "counts": counts,
        "queued": uploader.queued(),
        "uploading": (dict(uploader.current, name="файл") if private and uploader.current
                      else uploader.current),
        "waiting": uploader.waiting,
        "worker": uploader.running,
        "inline_mb": settings.INLINE_FILES_MB,
    }
