"""Хранение файлов бэкапа на диске.

Layout: backups/{guild_id}/{backup_id}.json — один сервер может иметь
несколько сохранённых бэкапов (а не один файл, перезаписываемый каждый раз,
как было раньше). Это и нужно для "истории" бэкапов, и пригодится для
будущего rollback (см. restore.py).

Совместимость со старым форматом: до этого обновления бэкап лежал прямо
в backups/{guild_id}.json и не имел ни ID, ни версии схемы, ни metadata —
просто {"guild_name", "roles", "channels"}. Такие файлы по-прежнему читаются:
migrate_legacy() оборачивает их в актуальную схему на лету. Сам legacy-файл
не трогаем (на случай, если что-то пошло не так при миграции) — новый бэкап
в актуальном формате появится рядом при следующем /save."""

from __future__ import annotations

import json
import os
import time
import uuid

from .models import BackupData, BackupMetadata, RoleData, ChannelData, OverwriteData

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)


def _guild_dir(guild_id: int) -> str:
    path = os.path.join(BACKUP_DIR, str(guild_id))
    os.makedirs(path, exist_ok=True)
    return path


def _legacy_path(guild_id: int) -> str:
    return os.path.join(BACKUP_DIR, f"{guild_id}.json")


def generate_backup_id() -> str:
    """Короткий, но уникальный и сортируемый по времени ID: <unix-время>-<4 hex>.
    Сортируемость по имени файла удобна для list_backups (последний бэкап —
    последний по алфавиту/времени без отдельного индекса)."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _backup_path(guild_id: int, backup_id: str) -> str:
    return os.path.join(_guild_dir(guild_id), f"{backup_id}.json")


def migrate_legacy(raw: dict, guild_id: int) -> BackupData:
    """Превращает самый старый формат бэкапа (без metadata/version) в BackupData."""
    roles = [
        RoleData(
            name=r["name"],
            color=int(r.get("color", 0)),
            hoist=bool(r.get("hoist", False)),
            mentionable=bool(r.get("mentionable", False)),
            permissions=int(r.get("permissions", 0)),
            position=int(r.get("position", 0)),
        )
        for r in raw.get("roles", [])
    ]

    categories = []
    channels = []
    for c in raw.get("channels", []):
        overwrites = [OverwriteData.from_dict(o) for o in c.get("overwrites", [])]
        if c.get("type") == "category":
            from .models import CategoryData

            categories.append(CategoryData(name=c["name"], position=c.get("position", 0), overwrites=overwrites))
        else:
            channels.append(
                ChannelData(
                    name=c["name"],
                    type=c.get("type", "text"),
                    position=c.get("position", 0),
                    category_name=c.get("category"),
                    overwrites=overwrites,
                )
            )

    metadata = BackupMetadata(
        backup_id=f"legacy-{guild_id}",
        created_at="unknown",
        schema_version=1,
        guild_id=guild_id,
        guild_name=raw.get("guild_name", ""),
        counts={"roles": len(roles), "categories": len(categories), "channels": len(channels)},
        note="Импортирован из старого формата бэкапа (до версии с metadata).",
    )
    return BackupData(metadata=metadata, roles=roles, categories=categories, channels=channels)


def save(data: BackupData) -> str:
    """Пишет бэкап на диск, возвращает его backup_id."""
    path = _backup_path(data.metadata.guild_id, data.metadata.backup_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)
    return data.metadata.backup_id


def list_backups(guild_id: int) -> list[str]:
    """ID всех бэкапов сервера, от старых к новым (по имени файла, оно сортируемо по времени)."""
    directory = os.path.join(BACKUP_DIR, str(guild_id))
    if not os.path.isdir(directory):
        return []
    ids = [fn[:-5] for fn in os.listdir(directory) if fn.endswith(".json")]
    return sorted(ids)


def has_any_backup(guild_id: int) -> bool:
    return bool(list_backups(guild_id)) or os.path.exists(_legacy_path(guild_id))


def latest_backup_id(guild_id: int) -> str | None:
    ids = list_backups(guild_id)
    if ids:
        return ids[-1]
    if os.path.exists(_legacy_path(guild_id)):
        return f"legacy-{guild_id}"
    return None


def load(guild_id: int, backup_id: str) -> BackupData:
    if backup_id == f"legacy-{guild_id}" and not os.path.exists(_backup_path(guild_id, backup_id)):
        with open(_legacy_path(guild_id), encoding="utf-8") as f:
            raw = json.load(f)
        return migrate_legacy(raw, guild_id)

    path = _backup_path(guild_id, backup_id)
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if "metadata" not in raw:
        return migrate_legacy(raw, guild_id)
    return BackupData.from_dict(raw)


def load_latest(guild_id: int) -> BackupData | None:
    backup_id = latest_backup_id(guild_id)
    if backup_id is None:
        return None
    return load(guild_id, backup_id)
