"""
Картинки интерфейса: аватарки и фоны чатов — сжатыми копиями по ссылке (2.17.0).

Раньше аватарка персонажа, персоны, профиля и фон чата лежали в базе как
data:URI исходного файла (фото с телефона — 1–5 МБ) и ЦЕЛИКОМ уходили в
списках: «персонажи» весили десятки мегабайт, «чаты» — по фону на каждый чат,
и на мобильном интернете боковая панель грузилась минуту ради кружков в 40
пикселей.

Теперь:
  * исходник остаётся в своей колонке как был — его по-прежнему видят модель
    (аватар в запросе), экспорт, Telegram-бот, референсы артов; скачать его
    можно по ссылке …/orig;
  * сервер делает сжатую копию (WebP: аватар — до 256 px, фон — до 1920 px) и
    хранит её в image_variants;
  * в списках вместо картинки — ссылка /api/img/<вид>/<id>/<вариант>?v=<версия>;
    по версии браузер кэширует копию навсегда и не качает её второй раз;
  * версия считается по длине исходника и его краям (их отдаёт SQL, сам
    исходник в списках из базы не читается). Сменили картинку любым путём —
    сменилась версия, копия пересоздаётся.

Без Pillow (не установлен) — всё как раньше: в списках исходники.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import logging
import urllib.parse
from dataclasses import dataclass

from sqlalchemy import case, delete as sql_delete, func, select

from backend import models

log = logging.getLogger("aichat.images")

URL_PREFIX = "/api/img/"
_EDGE = 96   # сколько символов с каждого края исходника идёт в версию


@dataclass(frozen=True)
class Kind:
    model: type
    column: str
    variant: str     # какую копию показывают в списках
    max_side: int


KINDS: dict[str, Kind] = {
    "character": Kind(models.Character, "avatar_path", "thumb", 256),
    "persona": Kind(models.Persona, "avatar_path", "thumb", 256),
    "user": Kind(models.User, "avatar_path", "thumb", 256),
    "session_bg": Kind(models.ChatSession, "background", "view", 1920),
}


def is_data_image(value) -> bool:
    # Без учёта регистра — как LIKE в SQLite (по нему списки отбирают картинки).
    return isinstance(value, str) and value[:11].lower() == "data:image/"


def is_our_url(value) -> bool:
    """Ссылка на копию, пришедшая обратно от клиента (сохранение карточки) — «не менялось»."""
    return isinstance(value, str) and value.startswith(URL_PREFIX)


def version_of(length: int, head: str, tail: str) -> str:
    return hashlib.sha1(f"{length}|{head}|{tail}".encode()).hexdigest()[:16]


def version_of_value(value: str) -> str:
    return version_of(len(value), value[:_EDGE], value[-_EDGE:])


def url(kind: str, oid: int, variant: str, ver: str) -> str:
    return f"{URL_PREFIX}{kind}/{oid}/{variant}?v={ver}"


# ---------------------------------------------------------------------------
# Сжатие
# ---------------------------------------------------------------------------
try:  # Pillow — необязательная зависимость: без неё копий нет, всё как раньше
    from PIL import Image, ImageOps
    _HAVE_PIL = True
except Exception:  # noqa: BLE001
    _HAVE_PIL = False


def decode_data_uri(value: str) -> tuple[bytes, str]:
    """Байты и MIME из data:URI (base64 или %-кодированного). ValueError — не разобрать."""
    head, sep, payload = value.partition(",")
    if not sep or not head.lower().startswith("data:"):
        raise ValueError("не data:URI")
    mime = head[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" in head:
        payload = payload.strip()
        try:
            return base64.b64decode(payload + "=" * (-len(payload) % 4)), mime
        except (binascii.Error, ValueError) as exc:
            raise ValueError("битый base64") from exc
    return urllib.parse.unquote_to_bytes(payload), mime   # data:image/svg+xml,<svg …>


# Больше — не сжимаем (исходник как есть): распаковка такого кадра одна заняла
# бы сотни МБ памяти на 4 ГБ сервере.
MAX_PIXELS = 50_000_000
_ANIMATED = ("GIF", "WEBP", "PNG")


def render(value: str, max_side: int) -> tuple[bytes, str, int, int] | None:
    """Сжатая копия (WebP) исходника; None — копию сделать нельзя (нет Pillow, битый файл)."""
    if not _HAVE_PIL:
        return None
    try:
        raw, mime = decode_data_uri(value)
    except ValueError:
        log.warning("Картинка не разбирается как data:URI (%d символов)", len(value))
        return None
    try:
        im = Image.open(io.BytesIO(raw))   # заголовок; пиксели ещё не прочитаны
        if im.width * im.height > MAX_PIXELS:
            log.warning("Картинка %dx%d слишком велика для сжатия", im.width, im.height)
            return None
        if getattr(im, "is_animated", False):
            if im.format in _ANIMATED:
                # Настоящая анимация (GIF/WebP/APNG): первый кадр в копии убил
                # бы её — показываем исходник как есть.
                return raw, mime, im.width, im.height
            im.seek(0)   # MPO/TIFF с фото телефона: второй «кадр» — карта глубины/превью
        if im.format in ("JPEG", "MPO"):
            # Фото с телефона (12–50 Мп): JPEG сразу декодируется в уменьшенном
            # масштабе — в разы меньше памяти и процессора на слабом сервере.
            im.draft("RGB", (max_side, max_side))
        im.thumbnail((max_side, max_side), Image.LANCZOS)
        im = ImageOps.exif_transpose(im)   # поворот из EXIF — уже на маленьком кадре
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() or "transparency" in im.info else "RGB")
        out = io.BytesIO()
        im.save(out, "WEBP", quality=82 if max_side <= 512 else 80, method=4)
        data = out.getvalue()
    except Exception:  # noqa: BLE001 — не картинка / неизвестный формат
        log.warning("Не удалось сжать картинку (%s, %d байт)", mime, len(raw))
        return None
    if len(data) >= len(raw):
        return raw, mime, im.width, im.height   # исходник и так маленький
    return data, "image/webp", im.width, im.height


# ---------------------------------------------------------------------------
# Копии в базе
# ---------------------------------------------------------------------------
_inflight: set = set()
# Сжатия — по одному: каждое держит распакованный кадр, а пул потоков общий
# со сборкой контекста ходов (первый запуск после обновления — десятки картинок).
_render_gate: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _render_gate
    if _render_gate is None:
        _render_gate = asyncio.Semaphore(1)
    return _render_gate


async def ensure(db, kind: str, oid: int) -> str | None:
    """
    Сделать (или проверить) копию картинки kind/oid. Возвращает версию готовой
    копии или None (исходника нет, это не data:URI, Pillow недоступен).
    """
    spec = KINDS[kind]
    value = (await db.execute(
        select(getattr(spec.model, spec.column)).where(spec.model.id == oid)
    )).scalar_one_or_none()
    if not is_data_image(value):
        await db.execute(sql_delete(models.ImageVariant).where(
            models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid))
        await db.commit()
        return None
    ver = version_of_value(value)
    row = (await db.execute(select(models.ImageVariant.src_ver, models.ImageVariant.mime).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid,
        models.ImageVariant.variant == spec.variant))).first()
    if row is not None and row[0] == ver:
        return ver if row[1] else None   # пустой mime — «уже пробовали, не сжимается»
    async with _gate():
        got = await asyncio.to_thread(render, value, spec.max_side)
    del value
    await db.execute(sql_delete(models.ImageVariant).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid))
    if got is None:
        if not _HAVE_PIL:
            await db.commit()
            return None
        # Не сжимается (SVG, HEIC, битый файл): помечаем эту версию, чтобы
        # списки не пробовали заново на каждом обновлении — уходит исходник.
        db.add(models.ImageVariant(kind=kind, owner_id=oid, variant=spec.variant, src_ver=ver,
                                   mime="", data=b"", width=0, height=0))
        await db.commit()
        return None
    data, mime, w, h = got
    db.add(models.ImageVariant(kind=kind, owner_id=oid, variant=spec.variant, src_ver=ver,
                               mime=mime, data=data, width=w, height=h))
    await db.commit()
    return ver


def _spawn_ensure(kind: str, oid: int) -> None:
    """Сделать копию в фоне (список не ждёт: в этот раз уйдёт исходник)."""
    key = (kind, oid)
    if key in _inflight or not _HAVE_PIL:
        return
    _inflight.add(key)

    async def run():
        from backend.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                await ensure(db, kind, oid)
        except Exception:  # noqa: BLE001 — копия догонит на следующем списке
            log.exception("Копия картинки %s/%s не сделана", kind, oid)
        finally:
            _inflight.discard(key)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        _inflight.discard(key)


async def public_values(db, kind: str, ids) -> dict[int, str | None]:
    """
    Что показать клиенту вместо картинки для каждого id: ссылку на свежую копию,
    маленькое значение как есть (URL, CSS-градиент), а пока копии нет — исходник
    (и копия заказывается в фоне). Исходники из базы читаются только для них.
    """
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    spec = KINDS[kind]
    col = getattr(spec.model, spec.column)
    is_data = col.like("data:image/%")
    out: dict[int, str | None] = {}
    sigs: dict[int, str] = {}
    for part in range(0, len(ids), 500):
        chunk = ids[part:part + 500]
        rows = (await db.execute(select(
            spec.model.id, is_data, case((is_data, None), else_=col),
            func.length(col), func.substr(col, 1, _EDGE), func.substr(col, -_EDGE),
        ).where(spec.model.id.in_(chunk)))).all()
        for oid, data_flag, small, length, head, tail in rows:
            if data_flag:
                sigs[oid] = version_of(int(length or 0), head or "", tail or "")
            else:
                out[oid] = small
    if not sigs:
        return out
    have = {}
    keys = list(sigs)
    for part in range(0, len(keys), 500):
        for oid, ver, mime in (await db.execute(select(
            models.ImageVariant.owner_id, models.ImageVariant.src_ver, models.ImageVariant.mime,
        ).where(models.ImageVariant.kind == kind, models.ImageVariant.variant == spec.variant,
                models.ImageVariant.owner_id.in_(keys[part:part + 500])))).all():
            have[oid] = (ver, mime)
    stale, unsupported = [], []
    for oid, ver in sigs.items():
        got = have.get(oid)
        if got and got[0] == ver and got[1]:
            out[oid] = url(kind, oid, spec.variant, ver)
        elif got and got[0] == ver:
            unsupported.append(oid)   # эта версия не сжимается — исходник, без новых попыток
        else:
            stale.append(oid)
    if stale or unsupported:
        originals = dict((await db.execute(
            select(spec.model.id, col).where(spec.model.id.in_(stale + unsupported)))).all())
        for oid in stale + unsupported:
            out[oid] = originals.get(oid)
        for oid in stale:
            _spawn_ensure(kind, oid)
    return out


async def current_version(db, kind: str, oid: int) -> str | None:
    """Версия картинки в колонке сейчас (None — там не data:image) — без чтения исходника."""
    spec = KINDS[kind]
    col = getattr(spec.model, spec.column)
    row = (await db.execute(select(
        col.like("data:image/%"), func.length(col), func.substr(col, 1, _EDGE), func.substr(col, -_EDGE),
    ).where(spec.model.id == oid))).first()
    if row is None or not row[0]:
        return None
    return version_of(int(row[1] or 0), row[2] or "", row[3] or "")


async def public_value(db, kind: str, oid) -> str | None:
    if oid is None:
        return None
    return (await public_values(db, kind, [oid])).get(oid)


async def backfill() -> int:
    """Копии для всех картинок без свежей копии (старт сервера, фоном, по одной)."""
    if not _HAVE_PIL:
        log.info("Pillow не установлен — картинки уходят исходниками (pip install Pillow)")
        return 0
    from backend.database import AsyncSessionLocal

    made = 0
    for kind, spec in KINDS.items():
        col = getattr(spec.model, spec.column)
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(select(spec.model.id).where(col.like("data:image/%")))).scalars().all()
        for oid in ids:
            if (kind, oid) in _inflight:
                continue
            _inflight.add((kind, oid))
            try:
                async with AsyncSessionLocal() as db:
                    if await ensure(db, kind, oid):
                        made += 1
            except Exception:  # noqa: BLE001
                log.exception("Копия картинки %s/%s не сделана", kind, oid)
            finally:
                _inflight.discard((kind, oid))
            await asyncio.sleep(0)
    return made


async def drop(db, kind: str, oid: int) -> None:
    """Владелец картинки удалён — копии тоже (коммит — у вызывающего)."""
    await db.execute(sql_delete(models.ImageVariant).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid))
