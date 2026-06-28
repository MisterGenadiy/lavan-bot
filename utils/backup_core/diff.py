"""Сравнение бэкапа с текущим состоянием сервера.

Самое важное отличие от старой реализации: раньше /load просто удалял ВСЁ
и создавал заново. Здесь вместо этого строится план (RestorePlan) —
какие сущности создать, какие обновить, какие удалить (и только если явно
попросили) и что выглядит подозрительно (конфликты, требующие внимания
человека). Сам план ни на что не влияет до вызова restore.apply_plan().

Сопоставление (matching) сущностей идёт по ИМЕНИ, а не по Discord ID — после
recreate ID всё равно меняются, так что сопоставление по ID было бы бессмысленно
сразу после первого restore. Это, естественно, не идеально (две роли с
одинаковым именем неотличимы), поэтому такие случаи помечаются конфликтом,
а не "угадываются"."""

from __future__ import annotations

import discord

from .models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_EMOJI,
    KIND_GUILD_SETTINGS,
    KIND_ROLE,
    KIND_STICKER,
    BackupData,
    ChannelData,
    OverwriteData,
    PlanItem,
    RestorePlan,
    RestoreScope,
    RoleData,
)


def _group_by_name(items, key):
    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    return groups


def _overwrites_equal(a: list[OverwriteData], b: list[OverwriteData]) -> bool:
    norm = lambda lst: sorted((o.target_name, o.target_type, o.allow, o.deny) for o in lst)
    return norm(a) == norm(b)


def _live_overwrites(channel_or_category) -> list[OverwriteData]:
    result = []
    for target, overwrite in channel_or_category.overwrites.items():
        allow, deny = overwrite.pair()
        result.append(
            OverwriteData(
                target_name=target.name,
                target_type="role" if isinstance(target, discord.Role) else "member",
                allow=allow.value,
                deny=deny.value,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Роли
# ---------------------------------------------------------------------------


def _diff_roles(guild: discord.Guild, backup_roles: list[RoleData]) -> list[PlanItem]:
    current_by_name = _group_by_name([r for r in guild.roles if not r.is_default()], key=lambda r: r.name)
    items: list[PlanItem] = []
    backup_names = set()

    for br in backup_roles:
        if br.is_managed:
            continue  # роль бота/интеграции — пересоздать через API нельзя, и не нужно
        backup_names.add(br.name)
        candidates = [r for r in current_by_name.get(br.name, []) if not r.managed]

        if not candidates:
            items.append(PlanItem(kind=KIND_ROLE, action=ACTION_CREATE, name=br.name, backup_obj=br))
            continue

        if len(candidates) > 1:
            items.append(
                PlanItem(
                    kind=KIND_ROLE,
                    action=ACTION_CONFLICT,
                    name=br.name,
                    details=f"На сервере {len(candidates)} роли с именем «{br.name}» — невозможно однозначно "
                    "определить, какую из них обновлять.",
                    backup_obj=br,
                    current_id=candidates[0].id,
                )
            )
            continue

        current = candidates[0]
        changed = (
            current.permissions.value != br.permissions
            or current.color.value != br.color
            or current.hoist != br.hoist
            or current.mentionable != br.mentionable
        )
        if changed:
            items.append(
                PlanItem(
                    kind=KIND_ROLE,
                    action=ACTION_UPDATE,
                    name=br.name,
                    details="Отличаются права доступа и/или оформление роли.",
                    backup_obj=br,
                    current_id=current.id,
                )
            )

    for name, roles in current_by_name.items():
        if name in backup_names:
            continue
        for r in roles:
            if r.managed:
                continue
            items.append(
                PlanItem(kind=KIND_ROLE, action=ACTION_REMOVE, name=name, details="Роли нет в бэкапе.", current_id=r.id)
            )

    return items


# ---------------------------------------------------------------------------
# Категории
# ---------------------------------------------------------------------------


def _diff_categories(
    guild: discord.Guild, backup_categories, *, check_permissions: bool, check_existence: bool
) -> list[PlanItem]:
    current_by_name = _group_by_name(guild.categories, key=lambda c: c.name)
    items: list[PlanItem] = []
    backup_names = set()

    for bc in backup_categories:
        backup_names.add(bc.name)
        candidates = current_by_name.get(bc.name, [])

        if not candidates:
            if check_existence:
                items.append(PlanItem(kind=KIND_CATEGORY, action=ACTION_CREATE, name=bc.name, backup_obj=bc))
            continue

        if len(candidates) > 1:
            items.append(
                PlanItem(
                    kind=KIND_CATEGORY,
                    action=ACTION_CONFLICT,
                    name=bc.name,
                    details=f"На сервере {len(candidates)} категории с именем «{bc.name}».",
                    backup_obj=bc,
                    current_id=candidates[0].id,
                )
            )
            continue

        current = candidates[0]
        if check_permissions and not _overwrites_equal(_live_overwrites(current), bc.overwrites):
            items.append(
                PlanItem(
                    kind=KIND_CATEGORY,
                    action=ACTION_UPDATE,
                    name=bc.name,
                    details="Отличаются права доступа (permission overwrites).",
                    backup_obj=bc,
                    current_id=current.id,
                )
            )

    if check_existence:
        for name, cats in current_by_name.items():
            if name in backup_names:
                continue
            for c in cats:
                items.append(
                    PlanItem(
                        kind=KIND_CATEGORY, action=ACTION_REMOVE, name=name, details="Категории нет в бэкапе.", current_id=c.id
                    )
                )

    return items


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------


def _channel_key(name: str, category_name: str | None) -> tuple:
    return (category_name or "", name)


def _diff_channels(
    guild: discord.Guild, backup_channels: list[ChannelData], *, check_permissions: bool, check_existence: bool
) -> list[PlanItem]:
    live_channels = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]
    current_by_key = _group_by_name(
        live_channels, key=lambda c: _channel_key(c.name, c.category.name if c.category else None)
    )
    items: list[PlanItem] = []
    backup_keys = set()

    for bch in backup_channels:
        key = _channel_key(bch.name, bch.category_name)
        backup_keys.add(key)
        candidates = current_by_key.get(key, [])

        if not candidates:
            if check_existence:
                items.append(PlanItem(kind=KIND_CHANNEL, action=ACTION_CREATE, name=bch.name, backup_obj=bch))
            continue

        if len(candidates) > 1:
            items.append(
                PlanItem(
                    kind=KIND_CHANNEL,
                    action=ACTION_CONFLICT,
                    name=bch.name,
                    details=f"{len(candidates)} канала с именем «{bch.name}» в одной категории.",
                    backup_obj=bch,
                    current_id=candidates[0].id,
                )
            )
            continue

        current = candidates[0]
        from .capture import _channel_type_str  # локальный импорт — избегаем циклической зависимости

        current_type = _channel_type_str(current)
        if current_type != bch.type:
            items.append(
                PlanItem(
                    kind=KIND_CHANNEL,
                    action=ACTION_CONFLICT,
                    name=bch.name,
                    details=f"В бэкапе канал типа «{bch.type}», на сервере — «{current_type}». "
                    "Автоматическая замена типа канала не выполняется.",
                    backup_obj=bch,
                    current_id=current.id,
                )
            )
            continue

        diffs = []
        if check_existence:
            if getattr(current, "topic", None) != bch.topic:
                diffs.append("тема")
            if getattr(current, "nsfw", False) != bch.nsfw:
                diffs.append("NSFW")
            if (getattr(current, "slowmode_delay", 0) or 0) != bch.slowmode_delay:
                diffs.append("замедление")
        if check_permissions and not _overwrites_equal(_live_overwrites(current), bch.overwrites):
            diffs.append("права доступа")

        if diffs:
            items.append(
                PlanItem(
                    kind=KIND_CHANNEL,
                    action=ACTION_UPDATE,
                    name=bch.name,
                    details="Отличается: " + ", ".join(diffs) + ".",
                    backup_obj=bch,
                    current_id=current.id,
                )
            )

    if check_existence:
        for key, chans in current_by_key.items():
            if key in backup_keys:
                continue
            for c in chans:
                items.append(
                    PlanItem(kind=KIND_CHANNEL, action=ACTION_REMOVE, name=c.name, details="Канала нет в бэкапе.", current_id=c.id)
                )

    return items


# ---------------------------------------------------------------------------
# Эмодзи и стикеры — Discord не разрешит создать дубль по имени, поэтому здесь
# только "create", если такого имени ещё нет; "update" для них не имеет
# смысла (картинку эмодзи нельзя заменить через API, только пересоздать).
# ---------------------------------------------------------------------------


def _diff_emojis(guild: discord.Guild, backup_emojis) -> list[PlanItem]:
    current_names = {e.name for e in guild.emojis}
    return [
        PlanItem(kind=KIND_EMOJI, action=ACTION_CREATE, name=e.name, backup_obj=e)
        for e in backup_emojis
        if e.name not in current_names
    ]


def _diff_stickers(guild: discord.Guild, backup_stickers) -> list[PlanItem]:
    current_names = {s.name for s in guild.stickers}
    return [
        PlanItem(kind=KIND_STICKER, action=ACTION_CREATE, name=s.name, backup_obj=s)
        for s in backup_stickers
        if s.name not in current_names
    ]


# ---------------------------------------------------------------------------
# Настройки сервера
# ---------------------------------------------------------------------------


def _diff_guild_settings(guild: discord.Guild, backup_settings) -> list[PlanItem]:
    if backup_settings is None:
        return []
    current_repr = (
        getattr(guild.verification_level, "name", None),
        getattr(guild.explicit_content_filter, "name", None),
        getattr(guild.default_notifications, "name", None),
    )
    backup_repr = (
        backup_settings.verification_level,
        backup_settings.explicit_content_filter,
        backup_settings.default_notifications,
    )
    if current_repr == backup_repr:
        return []
    return [
        PlanItem(
            kind=KIND_GUILD_SETTINGS,
            action=ACTION_UPDATE,
            name=guild.name,
            details="Отличаются уровень верификации / фильтр контента / уведомления по умолчанию.",
            backup_obj=backup_settings,
        )
    ]


# ---------------------------------------------------------------------------
# Сборка плана
# ---------------------------------------------------------------------------


def build_plan(guild: discord.Guild, backup: BackupData, *, scope: RestoreScope, remove_extra: bool) -> RestorePlan:
    items: list[PlanItem] = []

    if RestoreScope.ROLES in scope:
        items += _diff_roles(guild, backup.roles)

    check_perms = RestoreScope.PERMISSIONS in scope
    if RestoreScope.CATEGORIES in scope or check_perms:
        items += _diff_categories(
            guild, backup.categories, check_permissions=check_perms, check_existence=RestoreScope.CATEGORIES in scope
        )
    if RestoreScope.CHANNELS in scope or check_perms:
        items += _diff_channels(
            guild, backup.channels, check_permissions=check_perms, check_existence=RestoreScope.CHANNELS in scope
        )

    if RestoreScope.EMOJIS in scope:
        items += _diff_emojis(guild, backup.emojis)
    if RestoreScope.STICKERS in scope:
        items += _diff_stickers(guild, backup.stickers)
    if RestoreScope.GUILD_SETTINGS in scope:
        items += _diff_guild_settings(guild, backup.guild_settings)

    if not remove_extra:
        items = [i for i in items if i.action != ACTION_REMOVE]

    return RestorePlan(backup_id=backup.metadata.backup_id, scope=scope, remove_extra=remove_extra, items=items)
