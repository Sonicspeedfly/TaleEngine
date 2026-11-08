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
import hashlib
import io
import logging
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
    return isinstance(value, str) and value.startswith("data:image/")


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
    head, _, b64 = value.partition(",")
    mime = head[5:].split(";", 1)[0] or "application/octet-stream"
    return base64.b64decode(b64), mime


def render(value: str, max_side: int) -> tuple[bytes, str, int, int] | None:
    """Сжатая копия (WebP) исходника; None — копию сделать нельзя (нет Pillow, битый файл)."""
    if not _HAVE_PIL:
        return None
    raw, mime = decode_data_uri(value)
    try:
        im = Image.open(io.BytesIO(raw))
        if getattr(im, "is_animated", False):
            # Анимированный GIF/WebP: первый кадр в копии убил бы анимацию —
            # показываем исходник как есть.
            return raw, mime, im.width, im.height
        if im.format == "JPEG":
            # Фото с телефона (12–50 Мп): JPEG сразу декодируется в уменьшенном
            # масштабе — в разы меньше памяти и процессора на слабом сервере.
            im.draft("RGB", (max_side * 2, max_side * 2))
        im = ImageOps.exif_transpose(im)   # фото с телефона: поворот из EXIF
        im.thumbnail((max_side, max_side), Image.LANCZOS)
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
    row = (await db.execute(select(models.ImageVariant.src_ver).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid,
        models.ImageVariant.variant == spec.variant))).scalar_one_or_none()
    if row == ver:
        return ver
    got = await asyncio.to_thread(render, value, spec.max_side)
    del value
    if got is None:
        return None
    data, mime, w, h = got
    await db.execute(sql_delete(models.ImageVariant).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid))
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
        have.update(dict((await db.execute(select(
            models.ImageVariant.owner_id, models.ImageVariant.src_ver,
        ).where(models.ImageVariant.kind == kind, models.ImageVariant.variant == spec.variant,
                models.ImageVariant.owner_id.in_(keys[part:part + 500])))).all()))
    stale = []
    for oid, ver in sigs.items():
        if have.get(oid) == ver:
            out[oid] = url(kind, oid, spec.variant, ver)
        else:
            stale.append(oid)
    if stale:
        originals = dict((await db.execute(
            select(spec.model.id, col).where(spec.model.id.in_(stale)))).all())
        for oid in stale:
            out[oid] = originals.get(oid)
            _spawn_ensure(kind, oid)
    return out


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
            try:
                async with AsyncSessionLocal() as db:
                    if await ensure(db, kind, oid):
                        made += 1
            except Exception:  # noqa: BLE001
                log.exception("Копия картинки %s/%s не сделана", kind, oid)
            await asyncio.sleep(0)
    return made


async def drop(db, kind: str, oid: int) -> None:
    """Владелец картинки удалён — копии тоже (коммит — у вызывающего)."""
    await db.execute(sql_delete(models.ImageVariant).where(
        models.ImageVariant.kind == kind, models.ImageVariant.owner_id == oid))
