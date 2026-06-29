"""Проверка прав бота ДО восстановления.

Без этого узнать о нехватке прав можно было только по ходу restore — часть
ролей/каналов создаётся, а потом, например, на эмодзи Discord возвращает
403, и в RestoreResult.errors появляется малопонятная техническая ошибка.
Здесь — то же самое, но заранее и человеческим языком, прямо в плане
восстановления (см. cogs/backup.py, cogs/slash.py)."""

from __future__ import annotations

import discord

from .models import RestoreScope

# Какие права Discord нужны боту для каждой области восстановления.
# Permission overwrites (PERMISSIONS) трогают и роли, и сами каналы, поэтому
# требуют оба права сразу — частая причина, почему overwrites не применяются,
# хотя сами каналы создаются.
_REQUIRED_PERMISSIONS: dict[RestoreScope, tuple[str, ...]] = {
    RestoreScope.ROLES: ("manage_roles",),
    RestoreScope.CATEGORIES: ("manage_channels",),
    RestoreScope.CHANNELS: ("manage_channels",),
    RestoreScope.PERMISSIONS: ("manage_roles", "manage_channels"),
    RestoreScope.EMOJIS: ("manage_emojis_and_stickers",),
    RestoreScope.STICKERS: ("manage_emojis_and_stickers",),
    RestoreScope.GUILD_SETTINGS: ("manage_guild",),
}

_PERMISSION_LABELS = {
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_emojis_and_stickers": "Manage Emojis and Stickers",
    "manage_guild": "Manage Server",
    "manage_webhooks": "Manage Webhooks",
}


def missing_permissions_for_scope(guild: discord.Guild, scope: RestoreScope) -> list[str]:
    """Возвращает читаемые названия прав, которых не хватает боту для
    восстановления выбранной области. Пустой список — всё в порядке."""
    me = guild.me
    if me is None:
        return ["бот не найден на сервере"]

    needed: set[str] = set()
    for flag, perms in _REQUIRED_PERMISSIONS.items():
        if flag in scope:
            needed.update(perms)

    missing = [perm for perm in needed if not getattr(me.guild_permissions, perm, False)]
    return [_PERMISSION_LABELS.get(p, p) for p in sorted(missing)]
