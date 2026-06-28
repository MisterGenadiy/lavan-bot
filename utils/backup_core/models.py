"""Типизированная схема бэкапа сервера.

Все секции бэкапа описаны датаклассами с явными `to_dict`/`from_dict`,
вместо того чтобы гонять "сырые" словари по всему коду — это даёт автодополнение,
проверку типов и единое место, где описан формат файла.

SCHEMA_VERSION нужен для будущей совместимости: если формат поменяется,
старые бэкапы (включая совсем старые "плоские" файлы без версии вообще,
сохранённые предыдущей версией бота) подхватываются и приводятся к актуальному
виду в storage.migrate_legacy(), а не ломают загрузку.

Любые новые секции бэкапа (вебхуки, баны, AutoMod-правила и т.п.) должно
добавляться сюда новым датаклассом + полем в BackupData, а старые бэкапы
просто не будут иметь этого поля (см. BackupData.from_dict — пропущенные
секции восстанавливаются как пустые, а не ломают загрузку).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Flag, auto
from typing import Any

SCHEMA_VERSION = 2


# ---------------------------------------------------------------------------
# Секции бэкапа
# ---------------------------------------------------------------------------


@dataclass
class OverwriteData:
    target_name: str
    target_type: str  # "role" | "member"
    allow: int
    deny: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OverwriteData":
        return cls(
            target_name=d["target_name"],
            target_type=d.get("target_type", "role"),
            allow=int(d.get("allow", 0)),
            deny=int(d.get("deny", 0)),
        )


@dataclass
class RoleData:
    name: str
    color: int
    hoist: bool
    mentionable: bool
    permissions: int
    position: int
    is_managed: bool = False  # роли ботов/интеграций — не пересоздаются при restore

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RoleData":
        return cls(
            name=d["name"],
            color=int(d.get("color", 0)),
            hoist=bool(d.get("hoist", False)),
            mentionable=bool(d.get("mentionable", False)),
            permissions=int(d.get("permissions", 0)),
            position=int(d.get("position", 0)),
            is_managed=bool(d.get("is_managed", False)),
        )


@dataclass
class CategoryData:
    name: str
    position: int
    overwrites: list[OverwriteData] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "position": self.position, "overwrites": [o.to_dict() for o in self.overwrites]}

    @classmethod
    def from_dict(cls, d: dict) -> "CategoryData":
        return cls(
            name=d["name"],
            position=int(d.get("position", 0)),
            overwrites=[OverwriteData.from_dict(o) for o in d.get("overwrites", [])],
        )


@dataclass
class ChannelData:
    """Текстовый/голосовой/форум/новостной канал.

    Поля, специфичные не для всех типов (bitrate, user_limit, topic, ...)
    хранятся всегда, но имеют смысл только для подходящего `type` — это проще
    и расширяемее, чем несколько датаклассов-наследников, и не мешает
    добавить новый тип канала позже."""

    name: str
    type: str  # "text" | "voice" | "forum" | "announcement" | "stage"
    position: int
    category_name: str | None
    topic: str | None = None
    nsfw: bool = False
    slowmode_delay: int = 0
    bitrate: int | None = None
    user_limit: int | None = None
    overwrites: list[OverwriteData] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "position": self.position,
            "category_name": self.category_name,
            "topic": self.topic,
            "nsfw": self.nsfw,
            "slowmode_delay": self.slowmode_delay,
            "bitrate": self.bitrate,
            "user_limit": self.user_limit,
            "overwrites": [o.to_dict() for o in self.overwrites],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelData":
        return cls(
            name=d["name"],
            type=d.get("type", "text"),
            position=int(d.get("position", 0)),
            category_name=d.get("category_name") or d.get("category"),  # "category" — имя поля в старом формате
            topic=d.get("topic"),
            nsfw=bool(d.get("nsfw", False)),
            slowmode_delay=int(d.get("slowmode_delay", 0)),
            bitrate=d.get("bitrate"),
            user_limit=d.get("user_limit"),
            overwrites=[OverwriteData.from_dict(o) for o in d.get("overwrites", [])],
        )


@dataclass
class EmojiData:
    name: str
    animated: bool
    image_b64: str | None  # картинка прямо в бэкапе — восстановление работает,
    # даже если оригинальный эмодзи к моменту restore уже удалён с серверов Discord CDN.

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EmojiData":
        return cls(name=d["name"], animated=bool(d.get("animated", False)), image_b64=d.get("image_b64"))


@dataclass
class StickerData:
    name: str
    description: str
    emoji: str  # related unicode-эмодзи (обязателен для discord.Guild.create_sticker)
    image_b64: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StickerData":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            emoji=d.get("emoji", "❓"),
            image_b64=d.get("image_b64"),
        )


@dataclass
class GuildSettingsData:
    """Настройки сервера, доступные через Discord API.
    Не включает иконку/баннер (бинарные данные — намеренно не тащим
    в JSON-бэкап, чтобы не раздувать размер файла на больших серверах)."""

    name: str
    verification_level: str | None = None
    explicit_content_filter: str | None = None
    default_notifications: str | None = None
    afk_channel_name: str | None = None
    afk_timeout: int = 300
    system_channel_name: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GuildSettingsData":
        return cls(
            name=d.get("name", ""),
            verification_level=d.get("verification_level"),
            explicit_content_filter=d.get("explicit_content_filter"),
            default_notifications=d.get("default_notifications"),
            afk_channel_name=d.get("afk_channel_name"),
            afk_timeout=int(d.get("afk_timeout", 300)),
            system_channel_name=d.get("system_channel_name"),
        )


@dataclass
class BackupMetadata:
    backup_id: str
    created_at: str  # ISO-8601, UTC
    schema_version: int
    guild_id: int
    guild_name: str
    counts: dict[str, int] = field(default_factory=dict)
    is_emergency: bool = False
    note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BackupMetadata":
        return cls(
            backup_id=d["backup_id"],
            created_at=d["created_at"],
            schema_version=int(d.get("schema_version", 1)),
            guild_id=int(d["guild_id"]),
            guild_name=d.get("guild_name", ""),
            counts=dict(d.get("counts", {})),
            is_emergency=bool(d.get("is_emergency", False)),
            note=d.get("note"),
        )


@dataclass
class BackupData:
    """Полный снимок структуры сервера. Это и есть содержимое файла бэкапа."""

    metadata: BackupMetadata
    roles: list[RoleData] = field(default_factory=list)
    categories: list[CategoryData] = field(default_factory=list)
    channels: list[ChannelData] = field(default_factory=list)
    emojis: list[EmojiData] = field(default_factory=list)
    stickers: list[StickerData] = field(default_factory=list)
    guild_settings: GuildSettingsData | None = None
    # Зарезервировано под будущие секции (вебхуки, баны, AutoMod и т.п.),
    # которые не хотим заводить отдельной миграцией формата прямо сейчас.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "metadata": self.metadata.to_dict(),
            "roles": [r.to_dict() for r in self.roles],
            "categories": [c.to_dict() for c in self.categories],
            "channels": [c.to_dict() for c in self.channels],
            "emojis": [e.to_dict() for e in self.emojis],
            "stickers": [s.to_dict() for s in self.stickers],
            "guild_settings": self.guild_settings.to_dict() if self.guild_settings else None,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BackupData":
        gs = d.get("guild_settings")
        return cls(
            metadata=BackupMetadata.from_dict(d["metadata"]),
            roles=[RoleData.from_dict(r) for r in d.get("roles", [])],
            categories=[CategoryData.from_dict(c) for c in d.get("categories", [])],
            channels=[ChannelData.from_dict(c) for c in d.get("channels", [])],
            emojis=[EmojiData.from_dict(e) for e in d.get("emojis", [])],
            stickers=[StickerData.from_dict(s) for s in d.get("stickers", [])],
            guild_settings=GuildSettingsData.from_dict(gs) if gs else None,
            extra=dict(d.get("extra", {})),
        )


# ---------------------------------------------------------------------------
# Область восстановления (partial restore) и план изменений
# ---------------------------------------------------------------------------


class RestoreScope(Flag):
    """Что именно восстанавливать. Флаги комбинируются — например
    ROLES | CHANNELS восстановит роли и каналы, но не тронет эмодзи."""

    ROLES = auto()
    CATEGORIES = auto()
    CHANNELS = auto()
    PERMISSIONS = auto()  # перезаписать overwrites на УЖЕ существующих каналах/категориях
    EMOJIS = auto()
    STICKERS = auto()
    GUILD_SETTINGS = auto()

    @classmethod
    def all(cls) -> "RestoreScope":
        return cls.ROLES | cls.CATEGORIES | cls.CHANNELS | cls.PERMISSIONS | cls.EMOJIS | cls.STICKERS | cls.GUILD_SETTINGS

    @classmethod
    def from_keyword(cls, keyword: str) -> "RestoreScope":
        """Парсит выбор пользователя (slash-команда /load принимает choice строкой)."""
        mapping = {
            "all": cls.all(),
            "roles": cls.ROLES,
            "categories": cls.CATEGORIES | cls.PERMISSIONS,
            "channels": cls.CHANNELS | cls.PERMISSIONS,
            "permissions": cls.PERMISSIONS,
        }
        try:
            return mapping[keyword]
        except KeyError:
            raise ValueError(f"Неизвестная область восстановления: {keyword}") from None


# kind-литералы, используемые в PlanItem.kind — единое место для всех "типов сущностей"
KIND_ROLE = "role"
KIND_CATEGORY = "category"
KIND_CHANNEL = "channel"
KIND_EMOJI = "emoji"
KIND_STICKER = "sticker"
KIND_GUILD_SETTINGS = "guild_settings"

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_REMOVE = "remove"
ACTION_CONFLICT = "conflict"


@dataclass
class PlanItem:
    """Одна строка плана восстановления: что и как изменится."""

    kind: str
    action: str  # create | update | remove | conflict
    name: str
    details: str = ""  # человекочитаемое описание (что именно изменится / в чём конфликт)
    backup_obj: Any = None  # датакласс из бэкапа (для create/update)
    current_id: int | None = None  # ID существующего объекта на сервере (для update/remove/conflict)


@dataclass
class RestorePlan:
    """Результат сравнения бэкапа с текущим состоянием сервера —
    то, что показывается пользователю перед подтверждением /load."""

    backup_id: str
    scope: RestoreScope
    remove_extra: bool
    items: list[PlanItem] = field(default_factory=list)

    def by_action(self, action: str) -> list[PlanItem]:
        return [i for i in self.items if i.action == action]

    @property
    def creates(self) -> list[PlanItem]:
        return self.by_action(ACTION_CREATE)

    @property
    def updates(self) -> list[PlanItem]:
        return self.by_action(ACTION_UPDATE)

    @property
    def removes(self) -> list[PlanItem]:
        return self.by_action(ACTION_REMOVE)

    @property
    def conflicts(self) -> list[PlanItem]:
        return self.by_action(ACTION_CONFLICT)

    @property
    def is_empty(self) -> bool:
        return not self.items
