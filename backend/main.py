"""
Главный модуль веб-сервера (FastAPI).

Отвечает за:
  * REST API (персонажи, сессии, сообщения, память Horae, персоны, пресеты,
    настройки подключения к LiteLLM);
  * импорт карточек персонажей (PNG / JSON в формате SillyTavern);
  * стриминг ответов LLM по WebSocket (основной канал) и SSE (резерв/реконнект);
  * раздачу самого веб-интерфейса (статические файлы из ../frontend).

Архитектура «тонкого клиента»: браузер только отправляет текст и слушает токены.
Все запросы к LLM идут с сервера в LiteLLM-прокси (адрес настраивается в UI).

Запуск:  uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import base64
import hmac
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete as sql_delete
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from fastapi import Header, Request

from backend import accounts, admin_service, debug_log, group_chat, models, native_io, telegram_runtime
from backend.attachments import (
    attachment_data,
    delete_message_blobs,
    hydrate_export_attachments,
    message_attachments_in,
    store_attachments,
)
from backend.characters import (
    build_character_book,
    decode_png_card,
    extract_horae_entries,
    parse_character_json,
    parse_character_png,
)
from backend.chat_import import parse_sillytavern_chat
from backend.config import settings
from backend.database import AsyncSessionLocal, get_session, init_db
from backend.generation import generation_manager
from backend.horae_memory import build_context_from_db, messages_to_history_db
from backend.llm_gateway import (
    build_user_content,
    complete,
    generate_image,
    generate_image_chat,
    stream_completion,
)
from backend.horae_memory import _is_image
from backend.schemas import (
    AttachmentIn,
    CharacterCreate,
    CharacterRead,
    CharacterUpdate,
    ConnectionSettings,
    GenerationParams,
    HoraeEntryCreate,
    HoraeEntryRead,
    GroupCreate,
    HoraeEntryUpdate,
    ImagePrompt,
    MessageEdit,
    PersonaBase,
    PersonaRead,
    PresetBase,
    PresetRead,
    SessionUpdate,
    WSContinue,
    WSRegenerate,
    WSUserMessage,
)
from backend.settings_service import (
    fetch_proxy_models,
    get_connection,
    set_connection,
)


# Фоновые задачи процесса (авто-сводка Horae и т.п.): регистрируем, чтобы при
# остановке приложения корректно их погасить — брошенная на полпути задача
# оставляет незакрытую транзакцию SQLite («database is locked»).
_bg_tasks: set = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()  # создаём/мигрируем таблицы при старте
    async with AsyncSessionLocal() as db:
        await admin_service.load_caches(db)  # код доступа, пароль админа, настройки TG
    # Автозапуск Telegram-бота, если он включён в админке, задан токен И не
    # запрещён через TELEGRAM_AUTOSTART (защита от двойного polling — Conflict 409,
    # когда второй/тестовый инстанс делит БД с боевым сервером).
    tg = admin_service.telegram_cache()
    if settings.TELEGRAM_AUTOSTART and tg.get("enabled") and tg.get("token"):
        try:
            await telegram_runtime.start()
        except Exception:  # noqa: BLE001 — не мешаем старту веб-сервера
            pass
    yield
    await telegram_runtime.stop()
    # Даём фоновым задачам дозавершиться (обычно это быстрые чтения БД);
    # зависших — отменяем. Резкая отмена посреди SQL-запроса опасна, поэтому
    # сначала мягкое ожидание.
    if _bg_tasks:
        _done, pending = await asyncio.wait(list(_bg_tasks), timeout=8)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# Открытые пути (не требуют авторизации): здоровье, вход, регистрация, статус.
_OPEN_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/admin",
    "/api/auth/register",
    "/api/auth/login_user",
}


class AccessMiddleware(BaseHTTPMiddleware):
    """
    Два режима доступа:
      * РЕЖИМ АККАУНТОВ (security.accounts_enabled): /api/* требуют X-User-Token
        (валидного пользователя); /api/admin/* — роль admin (или X-Admin-Password).
      * РЕЖИМ КОНТРОЛЯ ДОСТУПА (по умолчанию): /api/* требуют X-Access-Code,
        /api/admin/* — X-Admin-Password (если заданы; пусто = открыто).
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if not (path.startswith("/api/") and path not in _OPEN_PATHS):
            return await call_next(request)

        sec = admin_service.security_cache()
        is_admin_path = path.startswith("/api/admin/")

        if sec.get("accounts_enabled"):
            # Токен из заголовка ИЛИ из query (?token=) — второе нужно для <img>/<audio>,
            # которые не умеют слать заголовки (лениво подгружаемые вложения).
            token = request.headers.get("X-User-Token", "") or request.query_params.get("token", "")
            async with AsyncSessionLocal() as db:
                user = await accounts.user_from_token(db, token)
            if user is None:
                return JSONResponse({"detail": "Требуется вход"}, status_code=401)
            if is_admin_path and user.role != "admin":
                ap = sec.get("admin_password") or ""
                if not (ap and request.headers.get("X-Admin-Password", "") == ap):
                    return JSONResponse({"detail": "Только администратор"}, status_code=403)
            return await call_next(request)

        # Режим контроля доступа (как раньше).
        if is_admin_path:
            ap = sec.get("admin_password") or ""
            if ap and request.headers.get("X-Admin-Password", "") != ap:
                return JSONResponse({"detail": "Требуется пароль администратора"}, status_code=403)
        else:
            code = sec.get("access_code") or ""
            if code:
                ap = sec.get("admin_password") or ""
                adm = request.headers.get("X-Admin-Password", "")
                # Код из заголовка ИЛИ из query (?access_code=) — для <img>/<audio>.
                given = request.headers.get("X-Access-Code", "") or request.query_params.get("access_code", "")
                if given != code and not (ap and adm == ap):
                    return JSONResponse({"detail": "Требуется код доступа"}, status_code=401)
        return await call_next(request)


async def current_user(
    x_user_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
):
    """Текущий пользователь (или None, если режим аккаунтов выключен / не вошёл)."""
    if not admin_service.security_cache().get("accounts_enabled"):
        return None
    return await accounts.user_from_token(db, x_user_token)


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """
    Запрещает кэшировать веб-интерфейс (app.js/styles.css/index.html), чтобы
    обновления применялись сразу, без ручного хард-рефреша у пользователя.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/api") and not path.startswith("/ws"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    HTTP Basic Auth — самый внешний барьер: браузер показывает системное окно
    «Войдите в систему» ещё ДО загрузки приложения. Включается в админке
    (security.basic_auth.enabled + логин/пароль). Это защита поверх внутреннего
    входа (код доступа / аккаунты), а не вместо него.

    WebSocket (/ws) исключаем: браузерный WebSocket не умеет проходить Basic-Auth
    запрос, а у /ws своя авторизация (?token=/?code=). /api/health тоже открыт
    (его опрашивает start.bat при запуске).
    """

    _EXEMPT_PREFIXES = ("/ws",)
    _EXEMPT_PATHS = {"/api/health"}

    async def dispatch(self, request, call_next):
        ba = admin_service.security_cache().get("basic_auth") or {}
        if ba.get("enabled") and ba.get("username"):
            path = request.url.path
            exempt = path in self._EXEMPT_PATHS or path.startswith(self._EXEMPT_PREFIXES)
            if not exempt and not _check_basic_auth(request, ba):
                return JSONResponse(
                    {"detail": "Требуется авторизация"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="AiChat SSF"'},
                )
        return await call_next(request)


def _check_basic_auth(request, ba: dict) -> bool:
    """Проверка заголовка Authorization: Basic <base64(user:pass)> (constant-time)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:  # noqa: BLE001 — любой битый заголовок = не авторизован
        return False
    user, _, pwd = decoded.partition(":")
    ok_user = hmac.compare_digest(user, str(ba.get("username", "")))
    ok_pass = hmac.compare_digest(pwd, str(ba.get("password", "")))
    return ok_user and ok_pass


app.add_middleware(NoCacheStaticMiddleware)
app.add_middleware(AccessMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Добавляем ПОСЛЕДНИМ -> Starlette делает его самым внешним (срабатывает первым).
app.add_middleware(BasicAuthMiddleware)


def _effective_model(params, connection: dict) -> str:
    """Какая модель реально пойдёт в запрос (для записи в model_used)."""
    if params and params.model:
        return params.model
    return connection.get("default_model") or settings.DEFAULT_MODEL


def _ctx_budget(params) -> int:
    """
    Окно контекста («память» диалога): значение из UI (params.context_tokens)
    важнее дефолта из .env. Управляет тем, сколько истории видит модель.
    """
    if params and params.context_tokens:
        return max(1000, int(params.context_tokens))
    return settings.CONTEXT_TOKEN_BUDGET


def _hist_files_limit(params) -> int | None:
    """
    Лимит ФАЙЛОВ истории в символах base64 (то, сколько прежних вложений модель
    заново «видит» на каждом ходу). None — БЕЗ лимита: полная память по файлам.
    UI (params.history_files_mb) важнее дефолта из .env; 0 — без лимита.
    """
    mb = params.history_files_mb if params and params.history_files_mb is not None \
        else settings.HISTORY_FILES_MB
    if not mb or mb <= 0:
        return None
    return int(mb * 1024 * 1024 * 4 / 3)  # size хранится «сырым», base64 длиннее


# ==================== АВТО-СВОДКА СЮЖЕТА (память Horae) ====================
# Каждые ~N новых сообщений фоновая задача сжимает их в запись Horae
# «Сводка сюжета (авто)» (always_on): даже когда старая история выпадает из окна
# контекста, её суть остаётся видимой модели. Это и есть «долгая память» чата.
_AUTO_SUMMARY_EVERY = 12          # сообщений между обновлениями сводки
_AUTO_SUMMARY_MARK = "__auto__"   # метка авто-записи в keywords
_AUTO_SUMMARY_TITLE = "📜 Сводка сюжета (авто)"

logger = logging.getLogger("aichat.summary")


async def _maybe_update_summary(session_id: int) -> None:
    """Фоновое обновление авто-сводки чата. Любая ошибка здесь не роняет ход."""
    try:
        async with AsyncSessionLocal() as db:
            # Выключатель (вкладка «Память»): settings/ui -> auto_summary=false.
            ui = await db.get(models.AppSetting, "ui")
            if ui and isinstance(ui.value, dict) and ui.value.get("auto_summary") is False:
                return
            entry = (await db.execute(
                select(models.HoraeEntry).where(
                    models.HoraeEntry.session_id == session_id,
                    models.HoraeEntry.category == "summary",
                )
            )).scalars().first()
            last_id = 0
            for kw in ((entry.keywords if entry else None) or []):
                if isinstance(kw, str) and kw.startswith("last:"):
                    try:
                        last_id = int(kw[5:])
                    except ValueError:
                        pass
            fresh = (await db.execute(
                select(models.Message).where(
                    models.Message.session_id == session_id,
                    models.Message.id > last_id,
                ).order_by(models.Message.id)
            )).scalars().all()
            fresh = [m for m in fresh if (m.content or "").strip()]
            if len(fresh) < _AUTO_SUMMARY_EVERY:
                return  # ещё рано — копим события
            transcript = "\n".join(
                f"{m.speaker_name or ('Пользователь' if m.role == 'user' else 'Персонаж')}: "
                + (m.content or "")[:1500]
                for m in fresh
            )[:24000]
            prev = (entry.content if entry else "") or "(пока пусто)"
            connection = await get_connection(db)
            newest_id = fresh[-1].id

        # LLM-вызов ВНЕ сессии БД (может занять десятки секунд).
        summary = (await complete(
            [
                {"role": "system", "content": (
                    "Ты ведёшь сжатую память ролевого чата. Объедини старую сводку и новые "
                    "события в ЕДИНЫЙ связный конспект до 1500 символов: ключевые факты, "
                    "имена, отношения, решения, состояние персонажей, незакрытые сюжетные "
                    "линии. Пиши в прошедшем времени, без вступлений и пояснений — только "
                    "текст сводки."
                )},
                {"role": "user", "content": f"[Старая сводка]\n{prev}\n\n[Новые события]\n{transcript}"},
            ],
            None, connection,
        )).strip()
        if not summary:
            return

        async with AsyncSessionLocal() as db:
            entry = (await db.execute(
                select(models.HoraeEntry).where(
                    models.HoraeEntry.session_id == session_id,
                    models.HoraeEntry.category == "summary",
                )
            )).scalars().first()
            if entry is None:
                entry = models.HoraeEntry(session_id=session_id, category="summary")
                db.add(entry)
            entry.title = _AUTO_SUMMARY_TITLE
            entry.content = summary[:4000]
            entry.keywords = [_AUTO_SUMMARY_MARK, f"last:{newest_id}"]
            entry.always_on = True
            entry.enabled = True
            entry.priority = 50  # сводка важнее рядовых записей, но ниже ручных «100+»
            await db.commit()
    except Exception:  # noqa: BLE001 — фоновая задача не должна ничего ронять
        logger.exception("Авто-сводка чата %s не обновилась", session_id)


# ---- Колбэки сохранения ответа ассистента (после завершения генерации) ----
# model_override приходит от менеджера генерации, если ответ дала ЗАПАСНАЯ модель.
def _make_persist_new(model_used: str):
    """Создаёт НОВОЕ сообщение ассистента (первый свайп = сам ответ)."""

    async def cb(session_id: int, content: str, model_override: str | None = None) -> None:
        async with AsyncSessionLocal() as db:
            db.add(
                models.Message(
                    session_id=session_id,
                    role="assistant",
                    content=content,
                    swipes=[content],
                    active_swipe=0,
                    model_used=model_override or model_used,
                )
            )
            await db.commit()
        # Ход завершён — возможно, пора освежить авто-сводку (фоном, не ждём).
        _spawn_bg(_maybe_update_summary(session_id))

    return cb


def _make_persist_swipe(message_id: int, model_used: str):
    """Добавляет новый вариант (свайп) к существующему ответу ассистента."""

    async def cb(session_id: int, content: str, model_override: str | None = None) -> None:
        async with AsyncSessionLocal() as db:
            msg = await db.get(models.Message, message_id)
            if not msg:
                return
            swipes = list(msg.swipes or [])
            swipes.append(content)
            msg.swipes = swipes
            msg.active_swipe = len(swipes) - 1
            msg.content = content
            msg.model_used = model_override or model_used
            await db.commit()

    return cb


def _make_persist_continue(message_id: int, model_used: str):
    """Дописывает сгенерированный текст В КОНЕЦ существующего ответа (функция «Продолжить»)."""

    async def cb(session_id: int, content: str, model_override: str | None = None) -> None:
        async with AsyncSessionLocal() as db:
            msg = await db.get(models.Message, message_id)
            if not msg:
                return
            new_full = (msg.content or "") + content
            msg.content = new_full
            swipes = list(msg.swipes or [])
            if swipes:
                swipes[msg.active_swipe] = new_full
            else:
                swipes = [new_full]
            msg.swipes = swipes
            msg.model_used = model_override or model_used
            await db.commit()

    return cb


# ============================ ПЕРСОНАЖИ ============================
@app.get("/api/characters", response_model=list[CharacterRead])
async def list_characters(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    q = accounts.scope_query(select(models.Character), models.Character, user)
    return (await db.execute(q)).scalars().all()


@app.post("/api/characters", response_model=CharacterRead)
async def create_character(
    payload: CharacterCreate, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    char = models.Character(**payload.model_dump())
    if user:
        char.owner_id = user.id
    db.add(char)
    await db.commit()
    await db.refresh(char)
    return char


@app.patch("/api/characters/{character_id}", response_model=CharacterRead)
async def update_character(
    character_id: int,
    payload: CharacterUpdate,
    db: AsyncSession = Depends(get_session),
):
    char = await db.get(models.Character, character_id)
    if not char:
        raise HTTPException(404, "Персонаж не найден")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(char, key, value)
    await db.commit()
    await db.refresh(char)
    return char


@app.delete("/api/characters/{character_id}")
async def delete_character(character_id: int, db: AsyncSession = Depends(get_session)):
    char = await db.get(models.Character, character_id)
    if char:
        await db.delete(char)
        await db.commit()
    return {"ok": True}


@app.post("/api/characters/import", response_model=CharacterRead)
async def import_character(
    file: UploadFile = File(...),
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Импорт карточки: .json или .png (SillyTavern). Лорбук (World Info) → память Horae."""
    raw = await file.read()
    if (file.filename or "").lower().endswith(".png"):
        card = decode_png_card(raw)
    else:
        card = json.loads(raw)
    payload = parse_character_json(card)

    char = models.Character(**payload.model_dump())
    if user:
        char.owner_id = user.id
    db.add(char)
    await db.commit()
    await db.refresh(char)

    # Детали Horae: записи лорбука из карточки привязываем к этому персонажу.
    for e in extract_horae_entries(card):
        db.add(
            models.HoraeEntry(
                character_id=char.id,
                category=e["category"], title=e["title"], content=e["content"],
                keywords=e["keywords"], always_on=e["always_on"],
                enabled=e["enabled"], priority=e["priority"],
            )
        )
    await db.commit()
    return char


@app.get("/api/characters/{character_id}/export")
async def export_character(character_id: int, db: AsyncSession = Depends(get_session)):
    """Экспорт персонажа в формат карточки SillyTavern V2 (с лорбуком из памяти Horae)."""
    char = await db.get(models.Character, character_id)
    if not char:
        raise HTTPException(404, "Персонаж не найден")
    entries = (
        await db.execute(
            select(models.HoraeEntry).where(models.HoraeEntry.character_id == character_id)
        )
    ).scalars().all()
    book_entries = [
        {
            "title": e.title, "content": e.content, "keywords": e.keywords or [],
            "always_on": e.always_on, "enabled": e.enabled, "priority": e.priority,
        }
        for e in entries
    ]
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": char.name,
            "description": char.description,
            "personality": char.personality,
            "scenario": char.scenario,
            "first_mes": char.first_message,
            "system_prompt": char.system_prompt,
            "character_book": build_character_book(book_entries) if book_entries else None,
        },
    }


# ============================ СЕССИИ ============================
@app.get("/api/sessions/shared")
async def list_shared_sessions(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Чаты, которыми со мной поделились друзья (доступны на чтение/участие)."""
    if not user:
        return []
    ids = await _shared_session_ids(db, user.id)
    if not ids:
        return []
    rows = (
        await db.execute(
            select(models.ChatSession)
            .where(models.ChatSession.id.in_(ids))
            .order_by(models.ChatSession.id.desc())
        )
    ).scalars().all()
    out = []
    for s in rows:
        ch = await db.get(models.Character, s.character_id)
        owner = await db.get(models.User, s.owner_id) if s.owner_id else None
        out.append({
            "id": s.id, "title": s.title, "is_group": s.is_group,
            "character_id": s.character_id,
            "character_name": ch.name if ch else "",
            "character_avatar": ch.avatar_path if ch else None,
            "owner": owner.username if owner else "",
            "timezone": s.timezone or "",
        })
    return out


@app.get("/api/sessions")
async def list_sessions(
    character_id: int | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    q = (
        select(models.ChatSession)
        .where(models.ChatSession.is_group == False)  # noqa: E712 — группы отдельно
        .order_by(models.ChatSession.id.desc())
    )
    if character_id is not None:
        q = q.where(models.ChatSession.character_id == character_id)
    q = accounts.scope_query(q, models.ChatSession, user)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": s.id,
            "title": s.title,
            "character_id": s.character_id,
            "author_note": s.author_note,
            "persona_id": s.persona_id,
            "background": s.background,
            "is_group": s.is_group,
            "director": s.director,
            "timezone": s.timezone or "",
        }
        for s in rows
    ]


@app.post("/api/sessions")
async def create_session(
    character_id: int,
    user_key: str = "web:anon",
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    sess = models.ChatSession(character_id=character_id, user_key=user_key)
    if user:
        sess.owner_id = user.id
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    # Сразу показываем приветствие персонажа (first message), как в SillyTavern.
    character = await db.get(models.Character, character_id)
    if character and character.first_message:
        db.add(
            models.Message(
                session_id=sess.id,
                role="assistant",
                content=character.first_message,
                swipes=[character.first_message],
                active_swipe=0,
            )
        )
        await db.commit()
    return {"session_id": sess.id}


@app.patch("/api/sessions/{session_id}")
async def update_session(
    session_id: int, payload: SessionUpdate, db: AsyncSession = Depends(get_session)
):
    sess = await db.get(models.ChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(sess, key, value)
    await db.commit()
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """
    Удаляет чат СО ВСЕМИ привязанными к нему строками. Раньше чистились только
    сообщения — а group_members/canvases/shares/session-horae оставались «сиротами».
    В SQLite id удалённой строки ПЕРЕИСПОЛЬЗУЕТСЯ, и новый чат наследовал чужих
    осиротевших участников (группа «пухла» с каждым пересозданием). Каскад это чинит.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    # Данные вложений сообщений этого чата (blob-таблица) — до удаления сообщений.
    await delete_message_blobs(
        db, select(models.Message.id).where(models.Message.session_id == session_id)
    )
    # Все дочерние таблицы, ссылающиеся на session_id (глобальные Horae с session_id
    # IS NULL не затрагиваются — они не привязаны к этому чату).
    for model in (
        models.Message, models.GroupMember, models.SessionShare,
        models.Canvas, models.HoraeEntry,
    ):
        await db.execute(sql_delete(model).where(model.session_id == session_id))
    if sess:
        await db.delete(sess)
    await db.commit()
    return {"ok": True}


def _att_meta(a: dict) -> dict:
    """Мета вложения для списка сообщений (без тяжёлого base64 `data`)."""
    data = a.get("data") or ""
    size = int(len(data) * 0.75) if data else int(a.get("size") or 0)  # ~сырой размер
    return {"type": a.get("type"), "mime": a.get("mime"), "name": a.get("name"), "size": size}


def _iso_utc(dt) -> str | None:
    """created_at -> ISO-строка. SQLite func.now() пишет UTC без tzinfo — помечаем 'Z',
    чтобы браузер корректно перевёл в часовой пояс пользователя/чата."""
    if not dt:
        return None
    return dt.isoformat() + ("" if dt.tzinfo else "Z")


@app.get("/api/messages/{message_id}/att/{idx}")
async def get_attachment(
    message_id: int, idx: int,
    token: str = "", x_user_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
):
    """
    Отдаёт байты одного вложения сообщения (лениво подгружается тегами <img>/<audio>).
    Так список сообщений остаётся лёгким, а картинки/аудио грузятся по мере показа и
    кэшируются браузером. Авторизация — по токену (заголовок или ?token=, т.к. <img>
    не шлёт заголовки); доступ к чату проверяется как обычно.
    """
    msg = await db.get(models.Message, message_id)
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    sess = await db.get(models.ChatSession, msg.session_id)
    user = None
    if admin_service.security_cache().get("accounts_enabled"):
        user = await accounts.user_from_token(db, x_user_token or token)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    atts = msg.attachments or []
    if not (0 <= idx < len(atts)) or not isinstance(atts[idx], dict):
        raise HTTPException(404, "Вложение не найдено")
    # data: инлайн (легаси) или из blob-таблицы (тяжёлый base64 хранится отдельно).
    data = await attachment_data(db, atts[idx])
    if not data:
        raise HTTPException(404, "Данные вложения не найдены")
    mime = atts[idx].get("mime") or "application/octet-stream"
    b64 = data.split(",", 1)[1] if data.startswith("data:") and "," in data else data
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Не удалось декодировать вложение")
    # Вложения неизменяемы — можно смело кэшировать в браузере.
    return Response(content=raw, media_type=mime, headers={"Cache-Control": "private, max-age=604800"})


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(
    session_id: int, before: int | None = None, limit: int | None = None,
    user=Depends(current_user), db: AsyncSession = Depends(get_session),
):
    """
    Сообщения чата. Пагинация для ленивой подгрузки:
      * `limit=N` (без before) — ПОСЛЕДНИЕ N сообщений;
      * `before=<id>&limit=N` — N сообщений СТАРШЕ указанного id (скролл вверх);
      * без параметров — вся история (совместимость).
    В любом случае возвращаются в хронологическом порядке (по возрастанию id).
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    q = select(models.Message).where(models.Message.session_id == session_id)
    if before is not None:
        q = q.where(models.Message.id < before)
    if limit is not None:
        # Берём последние N (по убыванию) и разворачиваем в хронологический порядок.
        q = q.order_by(models.Message.id.desc()).limit(max(1, min(limit, 500)))
        rows = list(reversed((await db.execute(q)).scalars().all()))
    else:
        rows = (await db.execute(q.order_by(models.Message.id))).scalars().all()
    # Заголовки/типы канвасов для «плашек документов» (одним запросом).
    canvas_ids = [m.canvas_id for m in rows if m.canvas_id]
    canvas_meta: dict = {}
    if canvas_ids:
        cvs = (
            await db.execute(select(models.Canvas).where(models.Canvas.id.in_(canvas_ids)))
        ).scalars().all()
        canvas_meta = {c.id: {"title": c.title, "kind": c.kind} for c in cvs}
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            # Только МЕТА вложений (без base64 data) — иначе чат с фото/аудио весит
            # десятки МБ и грузится медленно. Сами данные отдаёт /messages/{id}/att/{i}.
            "attachments": [_att_meta(a) for a in (m.attachments or []) if isinstance(a, dict)],
            "swipes": m.swipes or [m.content],
            "active_swipe": m.active_swipe,
            "model_used": m.model_used,
            "speaker_name": m.speaker_name,
            "reply_to_id": m.reply_to_id,
            "canvas_id": m.canvas_id,
            "canvas_title": canvas_meta.get(m.canvas_id, {}).get("title") if m.canvas_id else None,
            "canvas_kind": canvas_meta.get(m.canvas_id, {}).get("kind") if m.canvas_id else None,
            # Время сообщения: у user — момент отправки, у assistant — момент ответа.
            "created_at": _iso_utc(m.created_at),
        }
        for m in rows
    ]


# ============================ ГРУППОВЫЕ ЧАТЫ ============================
@app.get("/api/groups")
async def list_groups(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Список групповых чатов с именами участников."""
    q = (
        select(models.ChatSession)
        .where(models.ChatSession.is_group == True)  # noqa: E712
        .order_by(models.ChatSession.id.desc())
    )
    q = accounts.scope_query(q, models.ChatSession, user)
    rows = (await db.execute(q)).scalars().all()
    result = []
    for s in rows:
        members = await group_chat.load_members(db, s.id)
        result.append(
            {
                "id": s.id,
                "title": s.title,
                "director": s.director,
                "scenario": s.scenario,
                "timezone": s.timezone or "",
                # avatar_path нужен для аватарок реплик в групповом чате.
                "members": [
                    {"id": c.id, "name": c.name, "avatar_path": c.avatar_path}
                    for c in members
                ],
            }
        )
    return result


@app.post("/api/groups")
async def create_group(
    payload: GroupCreate, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Создаёт групповой чат из нескольких персонажей с общей «сценой»."""
    # Убираем дубли, сохраняя порядок — один персонаж = один участник.
    char_ids = list(dict.fromkeys(payload.character_ids))
    if len(char_ids) < 1:
        raise HTTPException(400, "Нужен хотя бы один персонаж")
    sess = models.ChatSession(
        character_id=char_ids[0],  # «ведущий» — первый
        user_key="web:anon",
        title=payload.name or "Групповой чат",
        is_group=True,
        director=payload.director,
        scenario=payload.scenario,
    )
    if user:
        sess.owner_id = user.id
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    for cid in char_ids:
        db.add(models.GroupMember(session_id=sess.id, character_id=cid))
    # Приветствия участников (first_message) как первые реплики.
    for cid in char_ids:
        ch = await db.get(models.Character, cid)
        if ch and ch.first_message:
            db.add(
                models.Message(
                    session_id=sess.id, role="assistant", content=ch.first_message,
                    swipes=[ch.first_message], active_swipe=0, speaker_name=ch.name,
                )
            )
    await db.commit()
    return {"session_id": sess.id}


# ============================ СООБЩЕНИЯ ============================
@app.patch("/api/messages/{message_id}")
async def edit_message(
    message_id: int, payload: MessageEdit, db: AsyncSession = Depends(get_session)
):
    """Редактирование текста или переключение активного свайпа (варианта ответа)."""
    msg = await db.get(models.Message, message_id)
    if not msg:
        raise HTTPException(404, "Сообщение не найдено")
    if payload.active_swipe is not None:
        swipes = msg.swipes or [msg.content]
        idx = max(0, min(payload.active_swipe, len(swipes) - 1))
        msg.active_swipe = idx
        msg.content = swipes[idx]
    if payload.content is not None:
        msg.content = payload.content
        # Синхронизируем активный свайп с отредактированным текстом.
        swipes = list(msg.swipes or [])
        if swipes:
            swipes[msg.active_swipe] = payload.content
        else:
            swipes = [payload.content]
        msg.swipes = swipes
    await db.commit()
    return {"ok": True, "content": msg.content, "active_swipe": msg.active_swipe}


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int, db: AsyncSession = Depends(get_session)):
    msg = await db.get(models.Message, message_id)
    if msg:
        await delete_message_blobs(db, [message_id])  # данные вложений — тоже
        await db.delete(msg)
        await db.commit()
    return {"ok": True}


# ============================ ПАМЯТЬ HORAE ============================
@app.get("/api/horae", response_model=list[HoraeEntryRead])
async def list_horae(
    session_id: int | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    q = select(models.HoraeEntry)
    if session_id is not None:
        q = q.where(models.HoraeEntry.session_id == session_id)
    rows = (await db.execute(q)).scalars().all()
    # В режиме аккаунтов прячем чужую память (см. _can_access_horae). Глобальный
    # лор (без привязки к сессии/персонажу) виден всем; user is None / админ — всё.
    return [r for r in rows if await _can_access_horae(db, r, user)]


@app.post("/api/horae", response_model=HoraeEntryRead)
async def create_horae(
    payload: HoraeEntryCreate,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    entry = models.HoraeEntry(**payload.model_dump())
    # Нельзя привязать запись к чужой сессии или чужому персонажу.
    if not await _can_access_horae(db, entry, user):
        raise HTTPException(403, "Нет доступа к этой сессии или персонажу")
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


@app.patch("/api/horae/{entry_id}", response_model=HoraeEntryRead)
async def update_horae(
    entry_id: int,
    payload: HoraeEntryUpdate,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    entry = await db.get(models.HoraeEntry, entry_id)
    if not entry:
        raise HTTPException(404, "Запись памяти не найдена")
    if not await _can_access_horae(db, entry, user):
        raise HTTPException(403, "Нет доступа к этой записи памяти")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(entry, key, value)
    await db.commit()
    await db.refresh(entry)
    return entry


@app.delete("/api/horae/{entry_id}")
async def delete_horae(
    entry_id: int,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    entry = await db.get(models.HoraeEntry, entry_id)
    if entry:
        if not await _can_access_horae(db, entry, user):
            raise HTTPException(403, "Нет доступа к этой записи памяти")
        await db.delete(entry)
        await db.commit()
    return {"ok": True}


# ============================ ПЕРСОНЫ ============================
@app.get("/api/personas", response_model=list[PersonaRead])
async def list_personas(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    q = accounts.scope_query(select(models.Persona), models.Persona, user)
    return (await db.execute(q)).scalars().all()


@app.post("/api/personas", response_model=PersonaRead)
async def create_persona(
    payload: PersonaBase, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    persona = models.Persona(**payload.model_dump())
    if user:
        persona.owner_id = user.id
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: int, db: AsyncSession = Depends(get_session)):
    persona = await db.get(models.Persona, persona_id)
    if persona:
        await db.delete(persona)
        await db.commit()
    return {"ok": True}


# ============================ ПРЕСЕТЫ ГЕНЕРАЦИИ ============================
@app.get("/api/presets", response_model=list[PresetRead])
async def list_presets(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    q = accounts.scope_query(select(models.SamplingPreset), models.SamplingPreset, user)
    return (await db.execute(q)).scalars().all()


@app.post("/api/presets", response_model=PresetRead)
async def create_preset(payload: PresetBase, db: AsyncSession = Depends(get_session)):
    # upsert по имени, чтобы «Сохранить» перезаписывал одноимённый пресет.
    existing = (
        await db.execute(
            select(models.SamplingPreset).where(
                models.SamplingPreset.name == payload.name
            )
        )
    ).scalars().first()
    if existing:
        existing.params = payload.params
        await db.commit()
        await db.refresh(existing)
        return existing
    preset = models.SamplingPreset(**payload.model_dump())
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    return preset


@app.delete("/api/presets/{preset_id}")
async def delete_preset(preset_id: int, db: AsyncSession = Depends(get_session)):
    preset = await db.get(models.SamplingPreset, preset_id)
    if preset:
        await db.delete(preset)
        await db.commit()
    return {"ok": True}


@app.post("/api/presets/{preset_id}/default")
async def set_default_preset(preset_id: int, db: AsyncSession = Depends(get_session)):
    """Делает пресет дефолтным (применяется автоматически при загрузке UI)."""
    presets = (await db.execute(select(models.SamplingPreset))).scalars().all()
    for p in presets:
        p.is_default = p.id == preset_id
    await db.commit()
    return {"ok": True}


# ============================ ПОДКЛЮЧЕНИЕ К LITELLM ============================
@app.get("/api/settings/connection")
async def read_connection(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Текущие настройки подключения. Не-админам прячем API-ключ прокси (защита Gemini)."""
    conn = await get_connection(db)
    if user is not None and user.role != "admin":
        conn = {**conn, "api_key": ("***" if conn.get("api_key") else "")}
    return conn


@app.put("/api/settings/connection")
async def write_connection(
    payload: ConnectionSettings,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    # В режиме аккаунтов менять подключение (доступ к Gemini) может только админ.
    if user is not None and user.role != "admin":
        raise HTTPException(403, "Только администратор")
    return await set_connection(db, payload.model_dump())


@app.get("/api/models")
async def list_models(db: AsyncSession = Depends(get_session)):
    """
    Список моделей у LiteLLM-прокси (для выпадающего списка и проверки связи).
    Запрос делает СЕРВЕР — браузер в прокси не ходит.
    """
    conn = await get_connection(db)
    try:
        return {"ok": True, "models": await fetch_proxy_models(conn)}
    except Exception as exc:  # noqa: BLE001 — показываем ошибку в UI, не роняем сервер
        return {"ok": False, "error": str(exc), "models": []}


# ============================ UI-НАСТРОЙКИ (сохраняются в БД) ============================
@app.get("/api/settings/ui")
async def read_ui_settings(db: AsyncSession = Depends(get_session)):
    """Произвольные настройки интерфейса (параметры генерации и т.п.)."""
    row = await db.get(models.AppSetting, "ui")
    return row.value if row else {}


@app.put("/api/settings/ui")
async def write_ui_settings(payload: dict, db: AsyncSession = Depends(get_session)):
    row = await db.get(models.AppSetting, "ui")
    if row is None:
        db.add(models.AppSetting(key="ui", value=payload))
    else:
        row.value = payload
    await db.commit()
    return {"ok": True}


# ============================ ИМПОРТ/ЭКСПОРТ ЧАТА ============================
@app.get("/api/sessions/{session_id}/export")
async def export_session(
    session_id: int,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Нативный экспорт чата AiChat (JSON): персонаж, персона, сообщения, память."""
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    character = await db.get(models.Character, sess.character_id)
    persona = await db.get(models.Persona, sess.persona_id) if sess.persona_id else None
    messages = (
        await db.execute(
            select(models.Message)
            .where(models.Message.session_id == sess.id)
            .order_by(models.Message.id)
        )
    ).scalars().all()
    # Память: записи этого чата + лорбук персонажа.
    horae = (
        await db.execute(
            select(models.HoraeEntry).where(
                or_(
                    models.HoraeEntry.session_id == sess.id,
                    models.HoraeEntry.character_id == sess.character_id,
                )
            )
        )
    ).scalars().all()
    members: list = []
    if sess.is_group:
        gms = (
            await db.execute(
                select(models.GroupMember).where(models.GroupMember.session_id == sess.id)
            )
        ).scalars().all()
        for gm in gms:
            c = await db.get(models.Character, gm.character_id)
            if c:
                members.append(c)
    data = native_io.build_chat_export(sess, character, persona, messages, horae, members)
    # Полный экспорт: данные вложений подтягиваем из blob-таблицы в файл.
    await hydrate_export_attachments(db, data, messages)
    return data


async def _import_native_chat(db, data: dict, owner_id, user) -> dict:
    """Импорт чата из нативного формата AiChat (полная точность)."""
    ch_data = data.get("character") or {}
    name = (ch_data.get("name") or "Импортированный персонаж").strip()

    def _new_character(d: dict) -> models.Character:
        return models.Character(
            name=(d.get("name") or "Персонаж").strip(),
            description=d.get("description", ""),
            personality=d.get("personality", ""),
            scenario=d.get("scenario", ""),
            first_message=d.get("first_message", ""),
            system_prompt=d.get("system_prompt", ""),
            avatar_path=d.get("avatar_path"),
            generation_params=d.get("generation_params") or {},
            model=d.get("model"),
            owner_id=owner_id,
        )

    # Персонаж: переиспользуем по имени в области пользователя, иначе создаём.
    q = accounts.scope_query(
        select(models.Character).where(models.Character.name == name), models.Character, user
    )
    character = (await db.execute(q)).scalars().first()
    character_created = character is None
    if character is None:
        character = _new_character(ch_data)
        db.add(character)
        await db.commit()
        await db.refresh(character)

    # Персона (опционально).
    persona_id = None
    p_data = data.get("persona")
    if p_data and (p_data.get("name") or "").strip():
        pq = accounts.scope_query(
            select(models.Persona).where(models.Persona.name == p_data["name"].strip()),
            models.Persona, user,
        )
        persona = (await db.execute(pq)).scalars().first()
        if persona is None:
            persona = models.Persona(
                name=p_data["name"].strip(),
                description=p_data.get("description", ""),
                avatar_path=p_data.get("avatar_path"),
                owner_id=owner_id,
            )
            db.add(persona)
            await db.commit()
            await db.refresh(persona)
        persona_id = persona.id

    s_data = data.get("session") or {}
    sess = models.ChatSession(
        character_id=character.id,
        user_key="web:anon",
        title=s_data.get("title") or ("Импорт: " + name),
        scenario=s_data.get("scenario", ""),
        author_note=s_data.get("author_note", ""),
        background=s_data.get("background", ""),
        is_group=bool(s_data.get("is_group")),
        director=bool(s_data.get("director")),
        persona_id=persona_id,
        owner_id=owner_id,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    # Участники группы (создаём недостающих персонажей). Дедупим — один раз каждого.
    if sess.is_group:
        added_ids: set[int] = set()
        for cd in data.get("group_members") or []:
            cname = (cd.get("name") or "").strip()
            if not cname:
                continue
            cq = accounts.scope_query(
                select(models.Character).where(models.Character.name == cname),
                models.Character, user,
            )
            member = (await db.execute(cq)).scalars().first()
            if member is None:
                member = _new_character(cd)
                db.add(member)
                await db.commit()
                await db.refresh(member)
            if member.id not in added_ids:
                added_ids.add(member.id)
                db.add(models.GroupMember(session_id=sess.id, character_id=member.id))

    # Сообщения: создаём, запоминаем idx→id, затем проставляем ответы-на-сообщение.
    idx_to_id: dict = {}
    pending_replies: list = []
    for m in data.get("messages") or []:
        msg = models.Message(
            session_id=sess.id,
            role=m.get("role", "assistant"),
            content=m.get("content", ""),
            swipes=m.get("swipes") or [m.get("content", "")],
            active_swipe=m.get("active_swipe") or 0,
            speaker_name=m.get("speaker_name"),
            attachments=[],
            model_used=m.get("model_used"),
        )
        db.add(msg)
        await db.flush()  # получаем msg.id, не закрывая транзакцию
        # Данные вложений из файла экспорта — в blob-таблицу, в строке — мета.
        msg.attachments = await store_attachments(db, msg.id, m.get("attachments") or [])
        idx_to_id[m.get("idx")] = msg.id
        if m.get("reply_to_idx") is not None:
            pending_replies.append((msg, m["reply_to_idx"]))
    for msg, ridx in pending_replies:
        msg.reply_to_id = idx_to_id.get(ridx)

    # Память Horae: память чата — всегда; лорбук персонажа — только если персонаж
    # новый (у существующего лорбук уже есть, не плодим дубли).
    horae_count = 0
    for h in data.get("horae") or []:
        scope = h.get("scope")
        if scope == "character" and not character_created:
            continue
        db.add(
            models.HoraeEntry(
                session_id=sess.id if scope != "character" else None,
                character_id=character.id if scope == "character" else None,
                category=h.get("category", "lore"),
                title=h.get("title", ""),
                content=h.get("content", ""),
                keywords=h.get("keywords") or [],
                always_on=bool(h.get("always_on")),
                enabled=bool(h.get("enabled", True)),
                priority=h.get("priority") or 0,
            )
        )
        horae_count += 1
    await db.commit()

    return {
        "session_id": sess.id,
        "character_id": character.id,
        "count": len(data.get("messages") or []),
        "native": True,
        "horae_saved": horae_count,
    }


@app.post("/api/sessions/import")
async def import_chat(
    file: UploadFile = File(...),
    character_id: int | None = None,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """
    Импорт чата. Сам определяет формат:
      * нативный AiChat (JSON с "format":"aichat.chat") — полная точность;
      * иначе — SillyTavern (.jsonl): реплики, кто говорил, снимок Horae.
    """
    raw = (await file.read()).decode("utf-8", errors="ignore")
    owner_id = user.id if user else None

    # Нативный формат AiChat?
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if native_io.is_native_chat(obj):
        return await _import_native_chat(db, obj, owner_id, user)

    parsed = parse_sillytavern_chat(raw)

    character = None
    if character_id:
        character = await db.get(models.Character, character_id)
    if character is None:
        q = select(models.Character).where(models.Character.name == parsed["character_name"])
        q = accounts.scope_query(q, models.Character, user)
        character = (await db.execute(q)).scalars().first()
    if character is None:
        character = models.Character(name=parsed["character_name"], owner_id=owner_id)
        db.add(character)
        await db.commit()
        await db.refresh(character)

    sess = models.ChatSession(
        character_id=character.id,
        user_key="web:anon",
        title="Импорт: " + parsed["character_name"],
        owner_id=owner_id,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    for m in parsed["messages"]:
        db.add(
            models.Message(
                session_id=sess.id,
                role=m["role"],
                content=m["content"],
                swipes=m["swipes"],
                active_swipe=m["active_swipe"],
                speaker_name=m.get("speaker"),
            )
        )

    # Детали Horae: текущее состояние (always_on) и хронология событий (отдельной
    # записью без always_on — чтобы не раздувать каждый запрос; включается в UI).
    horae_saved = bool(parsed.get("horae_state"))
    if horae_saved:
        db.add(
            models.HoraeEntry(
                session_id=sess.id,
                category="state",
                title="Состояние сюжета (импорт SillyTavern)",
                content=parsed["horae_state"],
                always_on=True,
                enabled=True,
                priority=10,
            )
        )
    events_saved = bool(parsed.get("horae_events"))
    if events_saved:
        db.add(
            models.HoraeEntry(
                session_id=sess.id,
                category="lore",
                title="Хронология событий (импорт SillyTavern)",
                content=parsed["horae_events"],
                always_on=False,
                enabled=True,
                priority=5,
            )
        )
    await db.commit()
    return {
        "session_id": sess.id,
        "character_id": character.id,
        "count": len(parsed["messages"]),
        "horae_saved": horae_saved,
        "horae_events_saved": events_saved,
        "horae_preview": (parsed.get("horae_state") or "")[:400],
    }


# ============================ КАНВАС (документ/код рядом с чатом) ============================
def _canvas_dict(c: models.Canvas) -> dict:
    return {
        "id": c.id, "session_id": c.session_id, "source_message_id": c.source_message_id,
        "title": c.title, "kind": c.kind, "language": c.language, "content": c.content,
        "can_undo": bool(c.history),
    }


_CANVAS_HISTORY_LIMIT = 30


def _push_history(canvas: models.Canvas) -> None:
    """Сохранить текущее содержимое в историю (для отката), ограничив длину."""
    hist = list(canvas.history or [])
    hist.append(canvas.content)
    canvas.history = hist[-_CANVAS_HISTORY_LIMIT:]


async def _canvas_or_403(db, canvas_id: int, user):
    """Канвас + проверка доступа через его сессию (владелец/админ/соавтор шары)."""
    canvas = await db.get(models.Canvas, canvas_id)
    if canvas is None:
        raise HTTPException(404, "Канвас не найден")
    sess = await db.get(models.ChatSession, canvas.session_id) if canvas.session_id else None
    if canvas.session_id and not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому канвасу")
    return canvas


@app.get("/api/canvas")
async def list_canvases(
    session_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Канвасы сессии (с проверкой доступа к ней)."""
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    rows = (
        await db.execute(
            select(models.Canvas)
            .where(models.Canvas.session_id == session_id)
            .order_by(models.Canvas.updated_at.desc())
        )
    ).scalars().all()
    return [_canvas_dict(c) for c in rows]


@app.post("/api/canvas")
async def create_canvas(
    payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Создать канвас (обычно из сообщения). Если для сообщения он уже есть — вернуть его."""
    session_id = payload.get("session_id")
    sess = await db.get(models.ChatSession, session_id) if session_id else None
    if session_id and not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    src = payload.get("source_message_id")
    if src:
        existing = (
            await db.execute(
                select(models.Canvas).where(
                    models.Canvas.session_id == session_id,
                    models.Canvas.source_message_id == src,
                )
            )
        ).scalars().first()
        if existing:
            return _canvas_dict(existing)
    canvas = models.Canvas(
        owner_id=(user.id if user else None),
        session_id=session_id,
        source_message_id=src,
        title=(payload.get("title") or "Без названия")[:300],
        kind=payload.get("kind") or "document",
        language=payload.get("language"),
        content=payload.get("content") or "",
    )
    db.add(canvas)
    await db.commit()
    await db.refresh(canvas)
    return _canvas_dict(canvas)


@app.get("/api/canvas/{canvas_id}")
async def get_canvas(
    canvas_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Один канвас по id (открытие по клику на плашку документа в чате)."""
    canvas = await _canvas_or_403(db, canvas_id, user)
    return _canvas_dict(canvas)


@app.patch("/api/canvas/{canvas_id}")
async def update_canvas(
    canvas_id: int, payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Ручное редактирование канваса (заголовок/тип/язык/содержимое)."""
    canvas = await _canvas_or_403(db, canvas_id, user)
    for field in ("title", "kind", "language", "content"):
        if field in payload and payload[field] is not None:
            setattr(canvas, field, payload[field])
    await db.commit()
    return _canvas_dict(canvas)


def _strip_fence(text: str) -> str:
    """Снять обрамляющий ```lang ... ``` если модель его добавила."""
    cleaned = text.strip()
    fence = re.match(r"^```[\w-]*\n(.*)\n```$", cleaned, re.DOTALL)
    return fence.group(1) if fence else cleaned


@app.post("/api/canvas/{canvas_id}/revise")
async def revise_canvas(
    canvas_id: int, payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """
    Доработка канваса нейросетью. Если переданы selection_start/selection_end —
    модель переписывает ТОЛЬКО выделенный фрагмент (контекстное редактирование, как
    в Gemini Canvas), остальной документ не трогаем. Иначе — весь материал целиком.
    Перед изменением кладём текущую версию в историю (для отката).
    """
    canvas = await _canvas_or_403(db, canvas_id, user)
    instruction = (payload.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "Пустая инструкция")
    connection = await get_connection(db)
    kind_word = "код" if canvas.kind == "code" else "документ"
    lang = f" (язык: {canvas.language})" if canvas.kind == "code" and canvas.language else ""

    start = payload.get("selection_start")
    end = payload.get("selection_end")
    content = canvas.content or ""
    selection_mode = (
        isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(content)
    )

    if selection_mode:
        fragment = content[start:end]
        system = (
            f"Ты — редактор ({kind_word}{lang}). Пользователь выделил ФРАГМЕНТ и просит его "
            "изменить. Перепиши ТОЛЬКО этот фрагмент и верни ТОЛЬКО его — без остального "
            "текста, без пояснений и без обрамляющих ```. Сохраняй стиль и согласованность "
            "с окружающим текстом."
        )
        user_msg = (
            f"Весь материал (для контекста):\n\n{content}\n\n---\n"
            f"Выделенный фрагмент для правки:\n\n{fragment}\n\n---\nЧто сделать: {instruction}"
        )
        new_fragment = _strip_fence(await complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            None, connection,
        ))
        _push_history(canvas)
        canvas.content = content[:start] + new_fragment + content[end:]
    else:
        system = (
            f"Ты — редактор. Пользователь работает над материалом ({kind_word}{lang}) и просит "
            "его изменить. Верни ПОЛНУЮ обновлённую версию целиком, БЕЗ пояснений, без "
            "приветствий и без обрамляющих ``` — только готовое содержимое."
        )
        user_msg = f"Текущее содержимое:\n\n{content}\n\n---\nЧто сделать: {instruction}"
        new_content = _strip_fence(await complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
            None, connection,
        ))
        _push_history(canvas)
        canvas.content = new_content
    await db.commit()
    return _canvas_dict(canvas)


@app.post("/api/canvas/{canvas_id}/undo")
async def undo_canvas(
    canvas_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Откатить канвас к предыдущей версии из истории."""
    canvas = await _canvas_or_403(db, canvas_id, user)
    hist = list(canvas.history or [])
    if hist:
        canvas.content = hist.pop()
        canvas.history = hist
        await db.commit()
    return _canvas_dict(canvas)


@app.delete("/api/canvas/{canvas_id}")
async def delete_canvas(
    canvas_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    canvas = await _canvas_or_403(db, canvas_id, user)
    await db.delete(canvas)
    await db.commit()
    return {"ok": True}


@app.get("/api/canvas/{canvas_id}/export")
async def export_canvas(
    canvas_id: int, fmt: str = "docx", user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """Экспорт канваса в Docx или PDF."""
    canvas = await _canvas_or_403(db, canvas_id, user)
    from urllib.parse import quote

    from backend import document_service

    name = re.sub(r"[^\w\-. а-яёА-ЯЁ]+", "_", canvas.title or "document").strip() or "document"
    if fmt == "pdf":
        data = document_service.markdown_to_pdf(canvas.content, canvas.title)
        media = "application/pdf"
        ext = "pdf"
    else:
        data = document_service.markdown_to_docx(canvas.content, canvas.title)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
    # Имя файла может быть кириллическим — HTTP-заголовки только latin-1, поэтому даём
    # ASCII-фолбэк + RFC 5987 (filename*) с процент-кодированием UTF-8.
    ascii_name = re.sub(r"[^A-Za-z0-9_.\-]+", "_", name) or "document"
    disposition = (
        f"attachment; filename=\"{ascii_name}.{ext}\"; "
        f"filename*=UTF-8''{quote(name)}.{ext}"
    )
    return Response(content=data, media_type=media, headers={"Content-Disposition": disposition})


def _detect_canvas(text: str):
    """Определить тип сгенерированной сущности: код-блок -> code, иначе document."""
    body = (text or "").strip()
    m = re.match(r"^```([\w+-]*)\n(.*)\n```$", body, re.DOTALL)
    if m:
        return "code", (m.group(1) or None), m.group(2).strip()
    return "document", None, body


def _looks_web(content: str) -> bool:
    """Похоже ли на веб-приложение (HTML) — тогда в Canvas доступен live-предпросмотр."""
    low = (content or "").lower()
    return (
        "<!doctype html" in low or "<html" in low or "<body" in low
        or ("<div" in low and ("<script" in low or "<style" in low))
    )


def _canvas_title(content: str, kind: str) -> str:
    for line in (content or "").splitlines():
        t = re.sub(r"^#+\s*", "", line.strip())
        if t:
            return t[:80]
    return "Код" if kind == "code" else "Документ"


@app.post("/api/sessions/{session_id}/canvas_generate")
async def canvas_generate(
    session_id: int, payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """
    Сгенерировать документ/код в Канвас по запросу. Сообщение пользователя уходит в
    чат как обычно; ОТВЕТ ИИ сохраняется как Канвас, а в чат кладётся «плашка
    документа» (assistant-сообщение с canvas_id) — оно открывает Канвас по клику.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    prompt = (payload.get("prompt") or "").strip()
    raw_atts = payload.get("attachments") or []
    attachments = [AttachmentIn(**a) for a in raw_atts]
    if not prompt and not attachments:
        raise HTTPException(400, "Пустой запрос")

    character = await db.get(models.Character, sess.character_id)
    connection = await get_connection(db)
    params = GenerationParams(**(payload.get("params") or {}))

    # 1. Сообщение пользователя — в чат как обычно (данные вложений — в blobs).
    user_msg = models.Message(session_id=session_id, role="user", content=prompt, attachments=[])
    db.add(user_msg)
    await db.flush()
    user_msg.attachments = await store_attachments(db, user_msg.id, attachments)
    await db.commit()

    # 2. Генерация (нестриминговая): просим ПОЛНЫЙ документ/код.
    user_content = build_user_content(prompt, attachments)
    messages = await build_context_from_db(
        db, sess, character, prompt, user_content, _ctx_budget(params)
    )
    messages.append({"role": "system", "content": (
        "Сгенерируй по запросу пользователя ПОЛНЫЙ, законченный материал (документ, "
        "статью, план или код). Верни ТОЛЬКО готовый материал в Markdown — без "
        "приветствий и разговорных вставок. Если это код — оберни его в один блок ```."
    )})
    result = await complete(messages, params, connection)

    kind, language, content = _detect_canvas(result)
    title = _canvas_title(content, kind)

    # 3. Канвас с результатом.
    canvas = models.Canvas(
        owner_id=(user.id if user else None), session_id=session_id,
        title=title, kind=kind, language=language, content=content,
    )
    db.add(canvas)
    await db.commit()
    await db.refresh(canvas)

    # 4. Сообщение от ИИ + «плашка документа» (assistant-сообщение со ссылкой на канвас).
    # content — это нормальный ответ в чат, плашка рендерится из canvas_id отдельно.
    kind_word = "веб-приложение" if (kind == "code" and _looks_web(content)) else ("код" if kind == "code" else "документ")
    intro = (
        f"Готово! Подготовил {kind_word} «{title}» — он открыт в Canvas справа. "
        f"Нажмите карточку ниже, чтобы посмотреть и при необходимости доработать."
    )
    card = models.Message(
        session_id=session_id, role="assistant", content=intro,
        canvas_id=canvas.id, model_used=_effective_model(params, connection),
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    canvas.source_message_id = card.id
    await db.commit()

    return {"message_id": card.id, "canvas_id": canvas.id, "title": title, "kind": kind}


@app.post("/api/sessions/{session_id}/canvas_edit")
async def canvas_edit(
    session_id: int, payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)
):
    """
    Правка УЖЕ ОТКРЫТОГО канваса: текущее содержимое + запрос уходят в модель, ответ
    ПАТЧИТ тот же канвас (новый файл/плашка НЕ создаётся). В чат — запрос пользователя
    и короткое подтверждение со ссылкой на тот же документ.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    canvas = await db.get(models.Canvas, payload.get("canvas_id"))
    if canvas is None or canvas.session_id != session_id:
        raise HTTPException(404, "Канвас не найден")
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "Пустой запрос")
    connection = await get_connection(db)
    params = GenerationParams(**(payload.get("params") or {}))

    db.add(models.Message(session_id=session_id, role="user", content=prompt))
    await db.commit()

    kind_word = "код" if canvas.kind == "code" else "документ"
    system = (
        f"Ты редактируешь {kind_word}. Внеси правку по запросу пользователя и верни "
        "ПОЛНУЮ обновлённую версию целиком — без пояснений, без приветствий и без "
        "обрамляющих ```."
    )
    user_msg = f"Текущее содержимое:\n\n{canvas.content}\n\n---\nЧто сделать: {prompt}"
    new_content = _strip_fence(await complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
        params, connection,
    ))
    _push_history(canvas)
    canvas.content = new_content
    # Тип мог уточниться (например стал кодом) — пересчитаем по содержимому.
    new_kind, new_lang, _ = _detect_canvas(new_content)
    if new_kind == "code":
        canvas.kind = "code"
        if new_lang:
            canvas.language = new_lang

    db.add(models.Message(
        session_id=session_id, role="assistant",
        content=f"✏️ Обновил «{canvas.title}» — изменения видны в Canvas справа.",
        model_used=_effective_model(params, connection),
    ))
    await db.commit()
    await db.refresh(canvas)
    return {"canvas": _canvas_dict(canvas)}


# ============================ ГЕНЕРАЦИЯ АРТА ============================
async def _collect_reference_images(db, session_id, character, msgs) -> list[str]:
    """Референс-картинки для генерации: аватар персонажа, аватар персоны, фото из чата."""
    refs: list[str] = []
    if character.avatar_path and _is_image(character.avatar_path):
        refs.append(character.avatar_path)
    sess = await db.get(models.ChatSession, session_id)
    if sess and sess.persona_id:
        p = await db.get(models.Persona, sess.persona_id)
        if p and p.avatar_path and _is_image(p.avatar_path):
            refs.append(p.avatar_path)
    # Свежие картинки из переписки (последние сообщения с вложениями-изображениями).
    # Данные тянем точечно (blob-таблица); тяжёлые не-картинки не читаются вовсе.
    for m in msgs[-8:]:
        for att in (m.attachments or []):
            if isinstance(att, dict) and att.get("type") == "image":
                data = await attachment_data(db, att)
                if _is_image(data):
                    refs.append(data)
    # Без дублей, не больше 4.
    seen, uniq = set(), []
    for r in refs:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq[:4]


async def _build_art_context(db, session_id: int, payload: ImagePrompt, conn: dict):
    """
    Умная сборка данных для арта: возвращает (текст промпта, референс-картинки).
    Учитывает контекст чата, фокус-сообщение и САМУЮ СВЕЖУЮ внешность персонажей.
    """
    sess = await db.get(models.ChatSession, session_id)
    character = await db.get(models.Character, sess.character_id)
    msgs = (
        await db.execute(
            select(models.Message)
            .where(models.Message.session_id == session_id)
            .order_by(models.Message.id)
        )
    ).scalars().all()

    # Фото, прикреплённые к описанию арта — приоритетные референсы.
    attach_refs = [
        a.data for a in payload.attachments if a.type == "image" and _is_image(a.data)
    ]
    chat_refs = await _collect_reference_images(db, session_id, character, msgs)
    seen, refs = set(), []
    for r in attach_refs + chat_refs:
        if r not in seen:
            seen.add(r)
            refs.append(r)
    refs = refs[:4]

    focus_text = ""
    if payload.from_message_id:
        fm = await db.get(models.Message, payload.from_message_id)
        if fm:
            focus_text = fm.content or ""

    # Режим «по описанию»: текст пользователя — основа. Если приложены фото-референсы,
    # просим модель собрать яркий промпт по описанию + фото; иначе берём текст как есть.
    if payload.mode == "prompt" and not focus_text:
        if not attach_refs:
            return (payload.prompt or character.name), refs
        instruction = (
            "Ты пишешь яркий детальный английский промпт для генерации изображения "
            "(одним абзацем, без вступлений). Опиши картинку по описанию пользователя, "
            "опираясь на приложенные фото-референсы (внешность, детали)."
        )
        uc: list = [{"type": "text", "text": "Описание: " + (payload.prompt or "")}]
        for r in refs:
            uc.append({"type": "image_url", "image_url": {"url": r}})
        crafted = (await complete(
            [{"role": "system", "content": instruction}, {"role": "user", "content": uc}],
            None, conn,
        )).strip()
        return (crafted or payload.prompt or character.name), refs

    n = 6 if payload.mode == "scene" else 24
    recent = [m for m in msgs if m.content][-n:]
    transcript = "\n".join(f"{m.speaker_name or m.role}: {m.content}" for m in recent)

    target = "последнюю сцену" if payload.mode == "scene" else "общую картину происходящего"
    instruction = (
        "Ты пишешь яркий детальный промпт для генерации изображения (на английском, "
        "одним абзацем, без вступлений и пояснений). Опиши " + target + ". "
        "КРАЙНЕ ВАЖНО: используй САМУЮ СВЕЖУЮ внешность персонажей — если за время "
        "диалога их вид менялся (одежда, причёска, раны, поза, обстановка), отрази "
        "ПОСЛЕДНЕЕ состояние. Приложенные картинки — базовая внешность персонажа/ролевика "
        "и/или сцены из чата, опирайся на них."
    )
    if payload.prompt:
        instruction += " Пожелание пользователя: " + payload.prompt + "."

    text = ""
    if focus_text:
        text += "Особый акцент на этом моменте: " + focus_text + "\n\n"
    text += f"Персонаж: {character.name}\n\nДиалог:\n{transcript}"

    user_content: list = [{"type": "text", "text": text}]
    for r in refs:
        user_content.append({"type": "image_url", "image_url": {"url": r}})

    crafted = (await complete(
        [{"role": "system", "content": instruction}, {"role": "user", "content": user_content}],
        None,
        conn,
    )).strip()
    return (crafted or payload.prompt or character.name), refs


@app.post("/api/sessions/{session_id}/image")
async def make_image(
    session_id: int, payload: ImagePrompt, db: AsyncSession = Depends(get_session)
):
    """
    Умная генерация арта из чата: собирает промпт по контексту/сообщению + референс-
    картинки (аватары и фото из чата, с учётом последней внешности). Генерирует через
    chat-модель (если включено «через чат») или через image_generation.
    """
    conn = await get_connection(db)
    try:
        prompt, refs = await _build_art_context(db, session_id, payload, conn)
        if conn.get("image_via_chat"):
            url = await generate_image_chat(prompt, refs, conn)
        else:
            url = await generate_image(prompt, conn, size=payload.size)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Ошибка генерации изображения: {exc}")
    if not url:
        raise HTTPException(502, "Провайдер не вернул изображение")

    content = f"![{prompt[:80]}]({url})"
    db.add(
        models.Message(
            session_id=session_id,
            role="assistant",
            content=content,
            swipes=[content],
            active_swipe=0,
        )
    )
    await db.commit()
    return {"ok": True}


# ==================== КОНВЕЙЕР ГЕНЕРАЦИИ ====================
async def _start_group_turn(session_id, content, attachments, params, db, reply_to_message_id=None, save_user=True) -> str:
    """
    Ход в групповом чате: сохраняем реплику пользователя и запускаем сценарий, где
    последовательно отвечают выбранные персонажи (по упоминанию / режиссёр / по кругу).
    save_user=False — повтор хода (retry): реплика уже в БД, второй раз не сохраняем.
    """
    sess = await db.get(models.ChatSession, session_id)
    members = await group_chat.load_members(db, session_id)
    connection = await get_connection(db)
    director = sess.director

    if save_user:
        msg = models.Message(
            session_id=session_id, role="user", content=content,
            attachments=[], reply_to_id=reply_to_message_id,
        )
        db.add(msg)
        await db.flush()
        msg.attachments = await store_attachments(db, msg.id, attachments)
        await db.commit()

    model_used = _effective_model(params, connection)

    async def runner(job):
        # Решаем, кто отвечает (читаем актуальную историю в свежей сессии).
        async with AsyncSessionLocal() as rdb:
            rmembers = await group_chat.load_members(rdb, session_id)
            msgs = (
                await rdb.execute(
                    select(models.Message)
                    .where(models.Message.session_id == session_id)
                    .order_by(models.Message.id)
                )
            ).scalars().all()
        last_assistant = next((m for m in reversed(msgs) if m.role == "assistant"), None)
        last_speaker = last_assistant.speaker_name if last_assistant else None
        transcript = "\n".join(
            (f"{m.speaker_name or 'Персонаж'}: {m.content}" if m.role == "assistant"
             else f"Пользователь: {m.content}")
            for m in msgs
        )

        chosen = group_chat.mentioned_responders(content, rmembers)
        if not chosen and director:
            chosen = await group_chat.director_pick(
                rmembers, transcript, connection, params=params, last_user=content
            )
        if not chosen:
            # Никто не упомянут и режиссёр выключен/промолчал — отвечает следующий
            # по кругу. Так на реплику пользователя ВСЕГДА кто-то реагирует.
            chosen = group_chat.round_robin_next(rmembers, last_speaker)

        for character in chosen:
            job.broadcast({"type": "speaker", "name": character.name, "character_id": character.id})
            async with AsyncSessionLocal() as rdb:
                rsess = await rdb.get(models.ChatSession, session_id)
                messages = await group_chat.build_group_messages(
                    rdb, rsess, character, _ctx_budget(params),
                    send_avatars=bool(params and params.send_avatars),
                )
            text = ""
            _thought = lambda t: job.broadcast({"type": "thought", "content": t})  # noqa: E731
            async for tok in stream_completion(messages, params, connection, on_thought=_thought):
                text += tok
                job.broadcast({"type": "token", "content": tok})
            async with AsyncSessionLocal() as rdb:
                rdb.add(
                    models.Message(
                        session_id=session_id, role="assistant", content=text,
                        swipes=[text], active_swipe=0,
                        speaker_name=character.name, model_used=model_used,
                    )
                )
                await rdb.commit()
            job.broadcast({"type": "speaker_done", "name": character.name})
        # Ход группы завершён — освежаем авто-сводку сюжета (фоном).
        _spawn_bg(_maybe_update_summary(session_id))

    job_id = uuid.uuid4().hex
    await generation_manager.start_runner(job_id, session_id, runner)
    return job_id


async def _reply_prefix(db, reply_to_message_id) -> str:
    """Текст-приставка «(В ответ на …)» — чтобы модель поняла, к чему обращаются."""
    if not reply_to_message_id:
        return ""
    rep = await db.get(models.Message, reply_to_message_id)
    if not rep or not rep.content:
        return ""
    who = rep.speaker_name or ("твоё сообщение" if rep.role == "assistant" else "сообщение пользователя")
    return f"(В ответ на {who}: «{rep.content[:300]}»)\n"


async def _start_user_turn(session_id, content, attachments, params, db, reply_to_message_id=None) -> str:
    """Сохранить сообщение пользователя -> собрать контекст -> запустить генерацию."""
    sess = await db.get(models.ChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    if sess.is_group:
        return await _start_group_turn(session_id, content, attachments, params, db, reply_to_message_id)
    character = await db.get(models.Character, sess.character_id)
    connection = await get_connection(db)

    # Для модели добавляем ссылку на сообщение, на которое отвечает пользователь.
    model_text = (await _reply_prefix(db, reply_to_message_id)) + content
    user_content = build_user_content(model_text, attachments)
    # Контекст строим ДО сохранения нового сообщения (иначе оно задвоится).
    messages = await build_context_from_db(
        db, sess, character, model_text, user_content, _ctx_budget(params),
        send_avatars=bool(params and params.send_avatars),
        history_files_limit=_hist_files_limit(params),
    )
    msg = models.Message(
        session_id=session_id,
        role="user",
        content=content,
        attachments=[],
        reply_to_id=reply_to_message_id,
    )
    db.add(msg)
    await db.flush()
    # Тяжёлый base64 — в blob-таблицу, в сообщении остаётся лёгкая мета.
    msg.attachments = await store_attachments(db, msg.id, attachments)
    await db.commit()

    job_id = uuid.uuid4().hex
    await generation_manager.start(
        job_id,
        session_id,
        messages,
        params,
        on_complete=_make_persist_new(_effective_model(params, connection)),
        connection=connection,
    )
    return job_id


async def _start_regenerate(session_id, params, db) -> str:
    """
    Перегенерация: создаёт НОВЫЙ свайп для последнего ответа ассистента.
    Контекст собирается БЕЗ этого ответа (модель заново отвечает на ту же реплику).
    """
    sess = await db.get(models.ChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    character = await db.get(models.Character, sess.character_id)
    connection = await get_connection(db)

    msgs = (
        await db.execute(
            select(models.Message)
            .where(models.Message.session_id == session_id)
            .order_by(models.Message.id)
        )
    ).scalars().all()

    # Последний ответ ассистента — его и будем дополнять новым свайпом.
    target = next((m for m in reversed(msgs) if m.role == "assistant"), None)
    if not target:
        raise HTTPException(400, "Нет ответа ассистента для перегенерации")
    # Последняя реплика пользователя перед этим ответом.
    last_user = next(
        (m for m in reversed(msgs) if m.role == "user" and m.id < target.id), None
    )
    user_text = last_user.content if last_user else ""
    boundary_id = last_user.id if last_user else target.id
    # История сохраняет вложения (модель «видит» прежние файлы); данные — из blobs.
    history = await messages_to_history_db(
        db, [m for m in msgs if m.id < boundary_id], _hist_files_limit(params)
    )
    user_content = build_user_content(
        user_text,
        [],  # вложения прошлой реплики при перегенерации не пересобираем
    )
    messages = await build_context_from_db(
        db,
        sess,
        character,
        user_text,
        user_content,
        _ctx_budget(params),
        history=history,
    )

    job_id = uuid.uuid4().hex
    await generation_manager.start(
        job_id,
        session_id,
        messages,
        params,
        on_complete=_make_persist_swipe(target.id, _effective_model(params, connection)),
        connection=connection,
    )
    return job_id


async def _start_continue(session_id, params, db) -> str:
    """
    «Продолжить»: дописывает последний ответ ассистента дальше. Контекст — диалог
    до этого ответа + сам ответ + просьба продолжить с того места, где оборвалось.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    character = await db.get(models.Character, sess.character_id)
    connection = await get_connection(db)

    msgs = (
        await db.execute(
            select(models.Message)
            .where(models.Message.session_id == session_id)
            .order_by(models.Message.id)
        )
    ).scalars().all()
    target = next((m for m in reversed(msgs) if m.role == "assistant"), None)
    if not target:
        raise HTTPException(400, "Нет ответа ассистента для продолжения")
    last_user = next(
        (m for m in reversed(msgs) if m.role == "user" and m.id < target.id), None
    )
    user_text = last_user.content if last_user else ""
    boundary_id = last_user.id if last_user else target.id
    # История сохраняет вложения (модель «видит» прежние файлы); данные — из blobs.
    history = await messages_to_history_db(
        db, [m for m in msgs if m.id < boundary_id], _hist_files_limit(params)
    )
    user_content = build_user_content(user_text, [])
    messages = await build_context_from_db(
        db, sess, character, user_text, user_content, _ctx_budget(params),
        history=history,
    )
    # Уже написанный ответ + явная просьба продолжить именно его.
    messages.append({"role": "assistant", "content": target.content})
    messages.append({
        "role": "user",
        "content": "Продолжи свой предыдущий ответ ровно с того места, где он оборвался. "
                   "Не повторяй уже написанное и не начинай заново.",
    })

    job_id = uuid.uuid4().hex
    await generation_manager.start(
        job_id,
        session_id,
        messages,
        params,
        on_complete=_make_persist_continue(target.id, _effective_model(params, connection)),
        connection=connection,
    )
    return job_id


async def _start_retry(session_id, params, db) -> str:
    """
    Повторная генерация после сбоя/обрыва/ручной остановки. Если диалог кончается
    репликой ПОЛЬЗОВАТЕЛЯ (ответ так и не родился) — отвечаем на неё заново, НЕ
    сохраняя её второй раз; иначе — новый свайп последнего ответа (= regenerate).
    """
    sess = await db.get(models.ChatSession, session_id)
    if not sess:
        raise HTTPException(404, "Сессия не найдена")
    msgs = (
        await db.execute(
            select(models.Message)
            .where(models.Message.session_id == session_id)
            .order_by(models.Message.id)
        )
    ).scalars().all()
    last = msgs[-1] if msgs else None
    if not last or last.role != "user":
        return await _start_regenerate(session_id, params, db)

    if sess.is_group:
        # Реплика уже в БД (runner группы читает историю из неё) — не дублируем.
        return await _start_group_turn(session_id, last.content, [], params, db, save_user=False)

    character = await db.get(models.Character, sess.character_id)
    connection = await get_connection(db)
    # ПОЛНЫЕ вложения повторяемой реплики (данные — из blob-таблицы).
    atts = await message_attachments_in(db, last)
    user_content = build_user_content(last.content, atts)
    # Контекст: история ДО последней реплики + сама реплика как текущее сообщение —
    # ровно то же, что видел бы _start_user_turn, но без повторного сохранения.
    history = await messages_to_history_db(
        db, [m for m in msgs if m.id < last.id], _hist_files_limit(params)
    )
    messages = await build_context_from_db(
        db, sess, character, last.content, user_content, _ctx_budget(params),
        history=history, send_avatars=bool(params and params.send_avatars),
    )
    job_id = uuid.uuid4().hex
    await generation_manager.start(
        job_id,
        session_id,
        messages,
        params,
        on_complete=_make_persist_new(_effective_model(params, connection)),
        connection=connection,
    )
    return job_id


# ==================== WEBSOCKET (основной стриминг) ====================
@app.websocket("/ws/chat/{session_id}")
async def ws_chat(websocket: WebSocket, session_id: int):
    """
    Входящие сообщения от клиента:
      {"type":"user_message","content":"...","attachments":[...],"params":{...}}
      {"type":"regenerate","params":{...}}      — новый вариант последнего ответа
      {"type":"retry","params":{...}}            — повторить ход после сбоя/остановки
      {"type":"stop"}                            — остановить текущую генерацию

    Исходящие: {"type":"job","job_id":...}, {"type":"token",...}, {"type":"done"|"error"}.

    Чтение входящих и пересылка токенов идут ПАРАЛЛЕЛЬНО, поэтому кнопка Stop
    срабатывает прямо во время генерации.
    """
    # Контроль доступа для WebSocket (код/токен передаются в query).
    sec = admin_service.security_cache()
    if sec.get("accounts_enabled"):
        async with AsyncSessionLocal() as db:
            ws_user = await accounts.user_from_token(db, websocket.query_params.get("token", ""))
            if ws_user is None:
                await websocket.close(code=4401)
                return
            # Доступ к чату: владелец, тот, с кем поделились, или админ.
            wsess = await db.get(models.ChatSession, session_id)
            if not await _can_access_session(db, wsess, ws_user):
                await websocket.close(code=4403)
                return
    else:
        code = sec.get("access_code") or ""
        if code:
            q = websocket.query_params.get("code", "")
            ap = sec.get("admin_password") or ""
            if q != code and not (ap and q == ap):
                await websocket.close(code=4401)
                return

    await websocket.accept()
    current_job_id: str | None = None
    forward_task: asyncio.Task | None = None

    async def forward(job):
        """Пересылает события генерации клиенту, не блокируя приём входящих."""
        queue = job.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
                if event["type"] in ("done", "error"):
                    break
        except Exception:  # noqa: BLE001 — клиент мог отключиться; генерация живёт в фоне
            pass
        finally:
            job.unsubscribe(queue)

    try:
        while True:
            raw = await websocket.receive_json()
            mtype = raw.get("type")

            if mtype == "stop":
                if current_job_id:
                    generation_manager.cancel(current_job_id)
                continue

            # Останавливаем предыдущую пересылку (если вдруг ещё идёт).
            if forward_task and not forward_task.done():
                forward_task.cancel()

            async with AsyncSessionLocal() as db:
                if mtype == "regenerate":
                    msg = WSRegenerate(**raw)
                    current_job_id = await _start_regenerate(session_id, msg.params, db)
                elif mtype == "continue":
                    msg = WSContinue(**raw)
                    current_job_id = await _start_continue(session_id, msg.params, db)
                elif mtype == "retry":
                    # Повтор хода: ответ на «повисшую» реплику юзера или новый свайп.
                    rparams = GenerationParams(**(raw.get("params") or {}))
                    current_job_id = await _start_retry(session_id, rparams, db)
                else:
                    msg = WSUserMessage(**raw)
                    current_job_id = await _start_user_turn(
                        session_id, msg.content, msg.attachments, msg.params, db,
                        reply_to_message_id=msg.reply_to_message_id,
                    )

            await websocket.send_json({"type": "job", "job_id": current_job_id})
            job = generation_manager.get(current_job_id)
            forward_task = asyncio.create_task(forward(job))
    except WebSocketDisconnect:
        # Клиент отвалился — норма: генерация доживёт в фоне и сохранится в БД.
        return


# ==================== SSE (резерв / реконнект) ====================
@app.get("/sse/job/{job_id}")
async def sse_job(job_id: str):
    job = generation_manager.get(job_id)
    if not job:
        raise HTTPException(404, "Задача генерации не найдена или уже очищена")

    async def event_stream():
        queue = job.subscribe()
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/sessions/{session_id}/send")
async def http_send(
    session_id: int, msg: WSUserMessage,
    user=Depends(current_user), db: AsyncSession = Depends(get_session),
):
    """
    Отправить ход по HTTP (а не по WebSocket) и слушать ответ через SSE
    (`/sse/job/{job_id}`). Нужен для сообщений с БОЛЬШИМИ вложениями: в WebSocket
    один кадр ограничен (uvicorn ws_max_size ≈ 16 МБ), а 14-МБ аудио в base64 —
    это ~19 МБ, и такое сообщение молча обрывалось. У HTTP-тела такого лимита нет.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    job_id = await _start_user_turn(
        session_id, msg.content, msg.attachments, msg.params, db,
        reply_to_message_id=msg.reply_to_message_id,
    )
    return {"job_id": job_id}


@app.post("/api/sessions/{session_id}/send_form")
async def http_send_form(
    session_id: int,
    payload: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    user=Depends(current_user), db: AsyncSession = Depends(get_session),
):
    """
    Отправка хода MULTIPART'ом — для БОЛЬШИХ файлов. Браузер шлёт файл бинарно,
    прямо с диска: без FileReader и base64 на клиенте (то есть без троекратного
    расхода памяти на телефоне) и на треть меньше трафика, чем JSON-путь /send.
    `payload` — JSON {content, params, reply_to_message_id, attachments}, где
    вложение либо инлайновое (есть `data`), либо ссылается на файл формы
    по `file_index`. В base64 файл переводит уже СЕРВЕР.
    """
    sess = await db.get(models.ChatSession, session_id)
    if not await _can_access_session(db, sess, user):
        raise HTTPException(403, "Нет доступа к этому чату")
    try:
        data = json.loads(payload)
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        raise HTTPException(422, "Некорректный payload")

    attachments: list[AttachmentIn] = []
    for meta in (data.get("attachments") or []):
        if not isinstance(meta, dict):
            continue
        idx = meta.get("file_index")
        if idx is None:
            attachments.append(AttachmentIn(**meta))  # маленькое инлайн-вложение
            continue
        idx = int(idx)
        if not (0 <= idx < len(files)):
            raise HTTPException(422, f"file_index {idx} вне диапазона")
        upload = files[idx]
        raw = await upload.read()
        mime = meta.get("mime") or upload.content_type or "application/octet-stream"
        attachments.append(AttachmentIn(
            type=meta.get("type") or "document",
            data=f"data:{mime};base64," + base64.b64encode(raw).decode(),
            mime=mime,
            name=meta.get("name") or upload.filename or "файл",
        ))

    params = GenerationParams(**(data.get("params") or {})) if data.get("params") else None
    job_id = await _start_user_turn(
        session_id, (data.get("content") or ""), attachments, params, db,
        reply_to_message_id=data.get("reply_to_message_id"),
    )
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Остановить генерацию по id задачи (работает и для WS-, и для HTTP-хода)."""
    generation_manager.cancel(job_id)
    return {"ok": True}


# ============================ АУТЕНТИФИКАЦИЯ / ДОСТУП ============================
@app.get("/api/auth/status")
async def auth_status(db: AsyncSession = Depends(get_session)):
    """Открытый эндпоинт: режим доступа и нужно ли что-то вводить на входе."""
    sec = admin_service.security_cache()
    return {
        "access_required": bool(sec.get("access_code")),
        "admin_set": bool(sec.get("admin_password")),
        "accounts_enabled": bool(sec.get("accounts_enabled")),
        "users_exist": (await accounts.count_users(db)) > 0,
    }


def _user_public(user) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role, "avatar_path": user.avatar_path}


@app.post("/api/auth/register")
async def auth_register(payload: dict, db: AsyncSession = Depends(get_session)):
    """Регистрация. Первый зарегистрированный становится администратором."""
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "Нужны логин и пароль")
    if await accounts.username_taken(db, username):
        raise HTTPException(409, "Такой логин уже занят")
    user = await accounts.register(db, username, password)
    token = await accounts.create_token(db, user.id)
    return {"ok": True, "token": token, "user": _user_public(user)}


@app.post("/api/auth/login_user")
async def auth_login_user(payload: dict, db: AsyncSession = Depends(get_session)):
    """Вход по логину/паролю (режим аккаунтов)."""
    user = await accounts.authenticate(db, payload.get("username", ""), payload.get("password", ""))
    if not user:
        raise HTTPException(401, "Неверный логин или пароль")
    token = await accounts.create_token(db, user.id)
    return {"ok": True, "token": token, "user": _user_public(user)}


@app.get("/api/auth/me")
async def auth_me(user=Depends(current_user)):
    if not user:
        return None
    data = _user_public(user)
    data["telegram_id"] = user.telegram_id
    return data


@app.post("/api/auth/link/telegram")
async def auth_link_telegram(user=Depends(current_user)):
    """Выдаёт одноразовый код. В боте: /link <код> — привяжет Telegram к аккаунту."""
    if not user:
        raise HTTPException(401, "Требуется вход")
    return {"code": accounts.make_link_code(user.id)}


@app.patch("/api/auth/me")
async def auth_update_me(payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Обновление профиля (например, внешность ролевика — avatar_path)."""
    if not user:
        raise HTTPException(401, "Требуется вход")
    db_user = await db.get(models.User, user.id)
    if "avatar_path" in payload:
        db_user.avatar_path = payload["avatar_path"]
    await db.commit()
    return _user_public(db_user)


# ----- Друзья (доступно в режиме аккаунтов) -----
@app.get("/api/friends")
async def list_friends(user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    if not user:
        return {"friends": [], "incoming": []}
    # Принятые дружбы (в обе стороны) + входящие заявки.
    rows = (
        await db.execute(
            select(models.Friendship).where(
                (models.Friendship.user_id == user.id) | (models.Friendship.friend_id == user.id)
            )
        )
    ).scalars().all()
    friends, incoming = [], []
    for f in rows:
        other_id = f.friend_id if f.user_id == user.id else f.user_id
        other = await db.get(models.User, other_id)
        if not other:
            continue
        if f.status == "accepted":
            friends.append({"friendship_id": f.id, "id": other.id, "username": other.username})
        elif f.friend_id == user.id and f.status == "pending":
            incoming.append({"friendship_id": f.id, "id": other.id, "username": other.username})
    return {"friends": friends, "incoming": incoming}


async def _existing_friendship(db, a: int, b: int):
    from sqlalchemy import and_, or_

    return (
        await db.execute(
            select(models.Friendship).where(
                or_(
                    and_(models.Friendship.user_id == a, models.Friendship.friend_id == b),
                    and_(models.Friendship.user_id == b, models.Friendship.friend_id == a),
                )
            )
        )
    ).scalars().first()


@app.post("/api/friends/add")
async def add_friend(payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    if not user:
        raise HTTPException(401, "Требуется вход")
    other = (
        await db.execute(select(models.User).where(models.User.username == payload.get("username", "")))
    ).scalars().first()
    if not other or other.id == user.id:
        raise HTTPException(404, "Пользователь не найден")
    # Не плодим повторные заявки — если связь уже есть, ничего не создаём.
    if await _existing_friendship(db, user.id, other.id):
        return {"ok": True, "duplicate": True}
    f = models.Friendship(user_id=user.id, friend_id=other.id, status="pending")
    db.add(f)
    await db.commit()
    await db.refresh(f)
    # Уведомление в Telegram, если у получателя привязан аккаунт.
    if other.telegram_id:
        try:
            await telegram_runtime.notify_friend_request(other.telegram_id, user.username, f.id)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}


@app.post("/api/friends/{friendship_id}/accept")
async def accept_friend(friendship_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    if not user:
        raise HTTPException(401, "Требуется вход")
    f = await db.get(models.Friendship, friendship_id)
    if f and f.friend_id == user.id:
        f.status = "accepted"
        await db.commit()
    return {"ok": True}


@app.post("/api/friends/{friendship_id}/decline")
async def decline_friend(friendship_id: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Отклонить заявку или удалить из друзей (в любую сторону)."""
    if not user:
        raise HTTPException(401, "Требуется вход")
    f = await db.get(models.Friendship, friendship_id)
    if f and (f.friend_id == user.id or f.user_id == user.id):
        await db.delete(f)
        await db.commit()
    return {"ok": True}


# ----- Доступ к чату / шаринг с друзьями -----
async def _are_friends(db, a: int, b: int) -> bool:
    f = await _existing_friendship(db, a, b)
    return bool(f and f.status == "accepted")


async def _shared_session_ids(db, user_id: int) -> list[int]:
    rows = (
        await db.execute(select(models.SessionShare).where(models.SessionShare.user_id == user_id))
    ).scalars().all()
    return [r.session_id for r in rows]


async def _can_access_session(db, sess, user) -> bool:
    if sess is None:
        return False
    if user is None or user.role == "admin" or sess.owner_id == user.id:
        return True
    sh = (
        await db.execute(
            select(models.SessionShare).where(
                models.SessionShare.session_id == sess.id,
                models.SessionShare.user_id == user.id,
            )
        )
    ).scalars().first()
    return bool(sh)


async def _can_access_horae(db, entry, user) -> bool:
    """
    Доступ к записи памяти Horae в режиме аккаунтов. Владелец у записи не хранится
    напрямую — он определяется по привязке (как и при сборке контекста):
      * user is None (режим выключен) либо админ — полный доступ;
      * глобальная запись (session_id и character_id оба NULL) — общий лор мира,
        видна всем;
      * привязка к сессии — доступ, если есть доступ к этой сессии
        (владелец / шара / админ, см. _can_access_session);
      * привязка к персонажу — доступ, если персонаж общий (owner_id NULL) или
        принадлежит пользователю.
    Запись с несколькими привязками доступна, если доступна хотя бы одна из них
    (так же, как такая запись попадает в контекст в _load_horae_records).
    """
    if user is None or user.role == "admin":
        return True
    if entry.session_id is None and entry.character_id is None:
        return True
    if entry.session_id is not None:
        sess = await db.get(models.ChatSession, entry.session_id)
        if await _can_access_session(db, sess, user):
            return True
    if entry.character_id is not None:
        char = await db.get(models.Character, entry.character_id)
        if char is not None and (char.owner_id is None or char.owner_id == user.id):
            return True
    return False


@app.get("/api/sessions/{session_id}/shares")
async def list_shares(session_id: int, db: AsyncSession = Depends(get_session)):
    rows = (
        await db.execute(select(models.SessionShare).where(models.SessionShare.session_id == session_id))
    ).scalars().all()
    out = []
    for r in rows:
        u = await db.get(models.User, r.user_id)
        if u:
            out.append({"user_id": u.id, "username": u.username})
    return out


@app.post("/api/sessions/{session_id}/share")
async def share_session(session_id: int, payload: dict, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    """Поделиться чатом с другом (он сможет читать и участвовать)."""
    if not user:
        raise HTTPException(401, "Требуется вход")
    sess = await db.get(models.ChatSession, session_id)
    if not sess or (sess.owner_id != user.id and user.role != "admin"):
        raise HTTPException(403, "Это не ваш чат")
    other = (
        await db.execute(select(models.User).where(models.User.username == payload.get("username", "")))
    ).scalars().first()
    if not other:
        raise HTTPException(404, "Пользователь не найден")
    if not await _are_friends(db, user.id, other.id):
        raise HTTPException(400, "Сначала добавьте пользователя в друзья")
    exists = (
        await db.execute(
            select(models.SessionShare).where(
                models.SessionShare.session_id == session_id,
                models.SessionShare.user_id == other.id,
            )
        )
    ).scalars().first()
    if not exists:
        db.add(models.SessionShare(session_id=session_id, user_id=other.id))
        await db.commit()
    return {"ok": True}


@app.delete("/api/sessions/{session_id}/share/{uid}")
async def unshare_session(session_id: int, uid: int, user=Depends(current_user), db: AsyncSession = Depends(get_session)):
    if not user:
        raise HTTPException(401, "Требуется вход")
    sess = await db.get(models.ChatSession, session_id)
    if not sess or (sess.owner_id != user.id and user.role != "admin"):
        raise HTTPException(403, "Это не ваш чат")
    sh = (
        await db.execute(
            select(models.SessionShare).where(
                models.SessionShare.session_id == session_id,
                models.SessionShare.user_id == uid,
            )
        )
    ).scalars().first()
    if sh:
        await db.delete(sh)
        await db.commit()
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(payload: dict):
    """Проверка кода доступа к приложению."""
    code = admin_service.security_cache().get("access_code") or ""
    return {"ok": (not code) or payload.get("code", "") == code}


@app.post("/api/auth/admin")
async def auth_admin(payload: dict):
    """Проверка пароля администратора."""
    ap = admin_service.security_cache().get("admin_password") or ""
    return {"ok": (not ap) or payload.get("password", "") == ap}


# ============================ АДМИНКА (требует X-Admin-Password) ============================
@app.get("/api/admin/security")
async def admin_get_security(db: AsyncSession = Depends(get_session)):
    return await admin_service.get_security(db)


@app.put("/api/admin/security")
async def admin_set_security(payload: dict, db: AsyncSession = Depends(get_session)):
    return await admin_service.set_security(db, payload)


@app.get("/api/admin/telegram")
async def admin_get_telegram(db: AsyncSession = Depends(get_session)):
    data = await admin_service.get_telegram(db)
    data["bot_state"] = telegram_runtime.status()
    return data


@app.put("/api/admin/telegram")
async def admin_set_telegram(payload: dict, db: AsyncSession = Depends(get_session)):
    return await admin_service.set_telegram(db, payload)


@app.post("/api/admin/telegram/whitelist/{tg_id}")
async def admin_whitelist_add(tg_id: int, db: AsyncSession = Depends(get_session)):
    return await admin_service.add_whitelist(db, tg_id)


@app.delete("/api/admin/telegram/whitelist/{tg_id}")
async def admin_whitelist_remove(tg_id: int, db: AsyncSession = Depends(get_session)):
    return await admin_service.remove_whitelist(db, tg_id)


@app.post("/api/admin/telegram/start")
async def admin_telegram_start(db: AsyncSession = Depends(get_session)):
    # Перечитываем кэш (вдруг токен только что сохранили) и стартуем бота.
    await admin_service.load_caches(db)
    try:
        await telegram_runtime.start()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return telegram_runtime.status()


@app.post("/api/admin/telegram/stop")
async def admin_telegram_stop():
    await telegram_runtime.stop()
    return telegram_runtime.status()


# ----- Управление пользователями (только админ) -----
@app.get("/api/admin/users")
async def admin_list_users(db: AsyncSession = Depends(get_session)):
    rows = (await db.execute(select(models.User))).scalars().all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "telegram_id": u.telegram_id}
        for u in rows
    ]


async def _admin_count(db) -> int:
    rows = (await db.execute(select(models.User).where(models.User.role == "admin"))).scalars().all()
    return len(rows)


@app.post("/api/admin/users/{user_id}/role")
async def admin_set_role(user_id: int, payload: dict, db: AsyncSession = Depends(get_session)):
    role = payload.get("role")
    if role not in ("admin", "user"):
        raise HTTPException(400, "Неверная роль")
    u = await db.get(models.User, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if u.role == "admin" and role == "user" and await _admin_count(db) <= 1:
        raise HTTPException(400, "Нельзя снять роль у последнего администратора")
    u.role = role
    await db.commit()
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, db: AsyncSession = Depends(get_session)):
    u = await db.get(models.User, user_id)
    if u:
        if u.role == "admin" and await _admin_count(db) <= 1:
            raise HTTPException(400, "Нельзя удалить последнего администратора")
        for t in (
            await db.execute(select(models.UserToken).where(models.UserToken.user_id == user_id))
        ).scalars().all():
            await db.delete(t)
        await db.delete(u)
        await db.commit()
    return {"ok": True}


@app.get("/api/debug/log")
async def debug_log_get():
    """Последние обращения к LLM (что отправлено и что вернулось)."""
    return debug_log.entries()


@app.delete("/api/debug/log")
async def debug_log_clear():
    debug_log.clear()
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME}


# ==================== РАЗДАЧА ВЕБ-ИНТЕРФЕЙСА ====================
# Монтируем ПОСЛЕ всех API/WS-маршрутов, чтобы они имели приоритет.
# html=True -> отдаёт index.html для корня и неизвестных путей (SPA).
_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
