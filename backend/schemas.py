"""
Pydantic-схемы (DTO) для валидации входящих/исходящих данных API.

Отделяем их от ORM-моделей (backend/models.py), чтобы менять формат API
независимо от схемы БД.
"""
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class GenerationParams(BaseModel):
    """
    Параметры генерации, которыми управляет UI.

    ВАЖНО (см. Шаг 5 в README): чтобы добавить новый параметр генерации, достаточно
    дописать поле сюда — и оно автоматически прокинется в LiteLLM (backend/llm_gateway.py),
    без правок самой логики вызова.
    """
    model: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    top_p: Optional[float] = Field(default=None, ge=0, le=1)
    top_k: Optional[int] = Field(default=None, ge=0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    repetition_penalty: Optional[float] = Field(default=None, ge=0)
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    # Главный тумблер "Zero-Censorship": снимает настраиваемые фильтры провайдера.
    # По умолчанию ВКЛючён (True) — проекту нужна полная свобода; при желании
    # пользователь может вернуть фильтры, сняв галку в настройках генерации.
    disable_safety: bool = True
    # Показывать нейросети аватары (внешность персонажа и ролевика) как картинки.
    send_avatars: bool = False
    # Доступ в интернет: подключает инструмент веб-поиска (Google Search grounding
    # у Gemini) на КАЖДЫЙ запрос — модель сможет искать актуальную информацию.
    web_access: bool = False
    # Рассуждения (thinking) модели: "" / "auto" — решает провайдер,
    # "disable" — выключить, "low"/"medium"/"high" — бюджет размышлений.
    # LiteLLM транслирует reasoning_effort в thinkingBudget у Gemini.
    reasoning_effort: Optional[str] = None
    # Включать рассуждения при работе с ФАЙЛАМИ: если reasoning_effort = авто,
    # а в запросе есть вложения (видео/фото/аудио/документ) — форсируем "medium".
    # Gemini местами «ленится» думать над файлами без явного бюджета.
    file_reasoning: bool = True


class AttachmentIn(BaseModel):
    """Вложение к сообщению (картинка, аудио, видео или документ Word/PDF/текст)."""
    type: Literal["image", "audio", "video", "document"]
    # base64 (можно с префиксом data URI) или внешний URL.
    data: str
    mime: Optional[str] = None
    # Имя файла (для документов: по нему определяем формат и показываем в чипе).
    name: Optional[str] = None


class CharacterBase(BaseModel):
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_message: str = ""
    system_prompt: str = ""
    model: Optional[str] = None
    generation_params: dict[str, Any] = Field(default_factory=dict)


class CharacterCreate(CharacterBase):
    pass


class CharacterRead(CharacterBase):
    # from_attributes=True позволяет создавать схему прямо из ORM-объекта.
    model_config = ConfigDict(from_attributes=True)

    id: int


class HoraeEntryBase(BaseModel):
    category: str = "lore"
    title: str = ""
    content: str = ""
    keywords: list[str] = Field(default_factory=list)
    always_on: bool = False
    enabled: bool = True
    priority: int = 0
    session_id: Optional[int] = None
    character_id: Optional[int] = None


class HoraeEntryCreate(HoraeEntryBase):
    pass


class HoraeEntryRead(HoraeEntryBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class CharacterUpdate(BaseModel):
    """Частичное обновление персонажа (все поля опциональны)."""
    name: Optional[str] = None
    description: Optional[str] = None
    personality: Optional[str] = None
    scenario: Optional[str] = None
    first_message: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    avatar_path: Optional[str] = None
    generation_params: Optional[dict[str, Any]] = None


class HoraeEntryUpdate(BaseModel):
    category: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[list[str]] = None
    always_on: Optional[bool] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


# ----- Настройки подключения к LiteLLM (редактируются в UI) -----
class ConnectionSettings(BaseModel):
    use_proxy: bool = True
    base_url: str = "http://localhost:4000"
    api_key: str = ""
    default_model: str = "gpt-4o"
    image_model: str = ""  # модель генерации артов в прокси (необязательно)
    # image_via_chat=True -> генерируем картинку через чат (nano-banana/*-image),
    # передавая модели РЕФЕРЕНС-картинки (аватары, фото из чата). Иначе — image_generation.
    image_via_chat: bool = False
    # Запасная модель: если основная не ответила (ошибка провайдера, пустой ответ),
    # генерация повторяется ею — автоматически (auto_fallback) или вручную из баннера.
    fallback_model: str = ""
    auto_fallback: bool = True


# ----- Персоны пользователя -----
class PersonaBase(BaseModel):
    name: str
    description: str = ""
    avatar_path: Optional[str] = None


class PersonaRead(PersonaBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


# ----- Пресеты параметров генерации -----
class PresetBase(BaseModel):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class PresetRead(PresetBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    is_default: bool = False


# ----- Обновление сессии / сообщения -----
class SessionUpdate(BaseModel):
    title: Optional[str] = None
    author_note: Optional[str] = None
    persona_id: Optional[int] = None
    background: Optional[str] = None
    director: Optional[bool] = None
    scenario: Optional[str] = None
    # Часовой пояс пользователя ДЛЯ ЭТОГО чата (IANA-имя вида Europe/Moscow или
    # смещение "+03:00"): нейросеть видит текущее время пользователя.
    timezone: Optional[str] = None


class GroupCreate(BaseModel):
    """Создание группового чата из нескольких персонажей."""
    name: str = "Групповой чат"
    character_ids: list[int] = Field(default_factory=list)
    director: bool = False
    # Общая «сцена»/сеттинг ролевой, влияет на всех персонажей.
    scenario: str = ""


class MessageEdit(BaseModel):
    content: Optional[str] = None
    # Переключение активного свайпа (варианта ответа) у ассистента.
    active_swipe: Optional[int] = None


class ImagePrompt(BaseModel):
    """Запрос на генерацию арта (умная генерация из чата)."""
    prompt: str = ""
    # prompt — по описанию; scene — последняя сцена; overview — общая картина по контексту.
    mode: Literal["prompt", "scene", "overview"] = "prompt"
    # Сгенерировать по КОНКРЕТНОМУ сообщению чата (кнопка 🎨 на сообщении).
    from_message_id: Optional[int] = None
    # Прикреплённые к описанию фото/файлы (референсы для генерации).
    attachments: list[AttachmentIn] = Field(default_factory=list)
    size: str = "1024x1024"


# ----- Протокол WebSocket -----
class WSUserMessage(BaseModel):
    """Сообщение, которое клиент шлёт серверу по WebSocket."""
    type: Literal["user_message"] = "user_message"
    content: str = ""
    attachments: list[AttachmentIn] = Field(default_factory=list)
    params: Optional[GenerationParams] = None
    # Ответ на конкретное сообщение (его id) — модель поймёт, к чему обращаются.
    reply_to_message_id: Optional[int] = None


class WSRegenerate(BaseModel):
    """Просьба перегенерировать последний ответ ассистента (создать новый свайп)."""
    type: Literal["regenerate"] = "regenerate"
    params: Optional[GenerationParams] = None


class WSContinue(BaseModel):
    """Просьба продолжить последний ответ ассистента (дописать текст)."""
    type: Literal["continue"] = "continue"
    params: Optional[GenerationParams] = None
