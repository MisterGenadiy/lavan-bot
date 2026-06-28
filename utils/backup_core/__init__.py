"""Публичный API системы бэкапов сервера.

Используется и префиксными командами (cogs/backup.py — L.backup),
и слэш-командами (cogs/slash.py — /save, /load), чтобы не дублировать логику.

Эта точка входа сохраняет старые имена функций (save_backup, has_backup,
get_backup_info, notify_guild_or_dm), которые были в предыдущей версии
backup_core.py — внешний код, который их вызывает, продолжает работать
без изменений. restore_backup (полная деструктивная замена всего сервера)
тоже сохранён для обратной совместимости, но новый код должен использовать
restore_with_safety + build_plan, дающие предпросмотр и менее разрушительное
поведение по умолчанию."""

from __future__ import annotations

import discord

from . import capture, diff, storage
from .models import BackupData, PlanItem, RestorePlan, RestoreScope
from .notify import notify_guild_or_dm
from .restore import RestoreResult, apply_plan, create_emergency_backup, restore_with_safety

__all__ = [
    "save_backup",
    "has_backup",
    "get_backup_info",
    "list_backups",
    "load_backup",
    "build_plan",
    "apply_plan",
    "create_emergency_backup",
    "restore_with_safety",
    "restore_backup",
    "notify_guild_or_dm",
    "RestoreScope",
    "RestorePlan",
    "RestoreResult",
    "PlanItem",
    "BackupData",
]


async def save_backup(guild: discord.Guild) -> dict:
    """Сохраняет полный снимок структуры сервера. Возвращает счётчики
    (роли/категории/каналы/эмодзи/стикеры и т.п.) — формат словаря расширен
    по сравнению со старой версией, но старые ключи ('roles', 'channels')
    сохранены, так что код, читающий только их, продолжает работать."""
    data = await capture.capture_guild(guild)
    storage.save(data)
    counts = dict(data.metadata.counts)
    counts.setdefault("roles", len(data.roles))
    counts.setdefault("channels", len(data.channels) + len(data.categories))
    counts["backup_id"] = data.metadata.backup_id
    return counts


def has_backup(guild_id: int) -> bool:
    return storage.has_any_backup(guild_id)


def list_backups(guild_id: int) -> list[str]:
    """ID всех бэкапов сервера от старых к новым (включая legacy, если он есть)."""
    ids = storage.list_backups(guild_id)
    if not ids and storage.has_any_backup(guild_id):
        return [storage.latest_backup_id(guild_id)]
    return ids


def load_backup(guild_id: int, backup_id: str | None = None) -> BackupData:
    """Загружает конкретный бэкап (или последний, если backup_id не передан)."""
    target = backup_id or storage.latest_backup_id(guild_id)
    if target is None:
        raise FileNotFoundError("Бэкап не найден")
    return storage.load(guild_id, target)


def get_backup_info(guild_id: int, backup_id: str | None = None) -> dict | None:
    """Метаданные бэкапа для отображения пользователю. Старые ключи
    ('guild_name', 'roles', 'channels') сохранены для обратной совместимости
    с cogs/backup.py, плюс новые: backup_id, created_at, counts, is_emergency."""
    if not storage.has_any_backup(guild_id):
        return None
    try:
        data = load_backup(guild_id, backup_id)
    except (FileNotFoundError, OSError, ValueError, KeyError):
        return None
    return {
        "guild_name": data.metadata.guild_name,
        "roles": len(data.roles),
        "channels": len(data.channels),
        "backup_id": data.metadata.backup_id,
        "created_at": data.metadata.created_at,
        "schema_version": data.metadata.schema_version,
        "counts": data.metadata.counts,
        "is_emergency": data.metadata.is_emergency,
    }


def build_plan(
    guild: discord.Guild,
    backup_id: str | None = None,
    *,
    scope: RestoreScope = None,
    remove_extra: bool = False,
) -> RestorePlan:
    """Строит план восстановления без применения — то, что показывается
    пользователю в /load до нажатия кнопки подтверждения."""
    target_id = backup_id or storage.latest_backup_id(guild.id)
    if target_id is None:
        raise FileNotFoundError("Бэкап не найден")
    backup = storage.load(guild.id, target_id)
    return diff.build_plan(guild, backup, scope=scope or RestoreScope.all(), remove_extra=remove_extra)


async def restore_backup(guild: discord.Guild) -> dict:
    """Старое поведение restore: полная деструктивная замена всего сервера
    бэкапом (роли/категории/каналы создаются заново, лишнее удаляется).
    Сохранено для обратной совместимости с уже написанным внешним кодом.

    Новые команды (/load, L.backup restore) используют restore_with_safety
    напрямую — это даёт предпросмотр плана и emergency-бэкап перед стартом."""
    plan, result, _emergency_id = await restore_with_safety(guild, scope=RestoreScope.all(), remove_extra=True)
    return {"roles": result.total_created() + result.total_updated(), "channels": result.total_created()}
