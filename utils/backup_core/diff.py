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

from dataclasses import dataclass

import discord

from .models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    KIND_AUTOMOD,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_EMOJI,
    KIND_GUILD_SETTINGS,
    KIND_ROLE,
    KIND_STICKER,
    KIND_WEBHOOK,
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


def _diff_automod_rules(guild: discord.Guild, backup_rules, *, current_rules) -> list[PlanItem]:
    """Как с эмодзи/стикерами: содержимое правила не диффится по полям —
    только «есть правило с таким именем или нет». Полное сравнение всех
    условий/действий правила добавляет сложности непропорционально пользе:
    отсутствующее правило достаточно просто пересоздать."""
    current_names = {r.name for r in current_rules}
    return [
        PlanItem(kind=KIND_AUTOMOD, action=ACTION_CREATE, name=r.name, backup_obj=r)
        for r in backup_rules
        if r.name not in current_names
    ]


def _diff_webhooks(guild: discord.Guild, backup_webhooks, *, current_webhooks) -> list[PlanItem]:
    current_keys = {(w.name, w.channel.name if w.channel else None) for w in current_webhooks}
    return [
        PlanItem(kind=KIND_WEBHOOK, action=ACTION_CREATE, name=w.name, backup_obj=w)
        for w in backup_webhooks
        if (w.name, w.channel_name) not in current_keys
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


async def build_plan(guild: discord.Guild, backup: BackupData, *, scope: RestoreScope, remove_extra: bool) -> RestorePlan:
    """ВАЖНО: async — в отличие от остальных _diff_* выше, AutoMod-правила и
    вебхуки нельзя прочитать из обычных атрибутов guild (как guild.roles),
    их нужно отдельно запрашивать у Discord API (fetch_automod_rules/webhooks),
    поэтому сборка плана в целом стала корутиной."""
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

    if RestoreScope.AUTOMOD in scope:
        try:
            current_rules = await guild.fetch_automod_rules()
        except discord.HTTPException:
            current_rules = []  # нет прав / фича недоступна — считаем, что на сервере правил нет
        items += _diff_automod_rules(guild, backup.automod_rules, current_rules=current_rules)

    if RestoreScope.WEBHOOKS in scope:
        try:
            current_webhooks = await guild.webhooks()
        except discord.HTTPException:
            current_webhooks = []
        items += _diff_webhooks(guild, backup.webhooks, current_webhooks=current_webhooks)

    if not remove_extra:
        items = [i for i in items if i.action != ACTION_REMOVE]

    return RestorePlan(backup_id=backup.metadata.backup_id, scope=scope, remove_extra=remove_extra, items=items)


def compare_backups(old: BackupData, new: BackupData) -> RestorePlan:
    """Сравнивает ДВА бэкапа между собой (а не бэкап с текущим сервером,
    как build_plan выше) — «что изменилось на сервере между датой A и датой B».
    Чисто информационно, ничего не применяется и не может быть применено:
    current_id у всех пунктов всегда None, conflict-пунктов не бывает
    (обе стороны — статичные снимки, а не живой Discord-сервер с реальными
    дублями имён). remove_extra=True у итогового плана нужен только чтобы
    build_restore_plan_embed() показал секцию «удалено» — это не означает
    «удалить что-то», тут просто нет такого действия в принципе."""
    items: list[PlanItem] = []

    items += _compare_named_list(old.roles, new.roles, kind=KIND_ROLE, key=lambda r: r.name, equal=_roles_equal)
    items += _compare_named_list(
        old.categories, new.categories, kind=KIND_CATEGORY, key=lambda c: c.name, equal=_categories_equal
    )
    items += _compare_named_list(
        old.channels, new.channels, kind=KIND_CHANNEL,
        key=lambda c: _channel_key(c.name, c.category_name), equal=_channels_equal,
        name_from_key=lambda k: f"{k[1]} (в «{k[0]}»)" if k[0] else k[1],
    )
    items += _compare_named_list(old.emojis, new.emojis, kind=KIND_EMOJI, key=lambda e: e.name, equal=lambda a, b: True)
    items += _compare_named_list(old.stickers, new.stickers, kind=KIND_STICKER, key=lambda s: s.name, equal=lambda a, b: True)

    return RestorePlan(backup_id=new.metadata.backup_id, scope=RestoreScope.all(), remove_extra=True, items=items)


def _roles_equal(a: RoleData, b: RoleData) -> bool:
    return (a.permissions, a.color, a.hoist, a.mentionable) == (b.permissions, b.color, b.hoist, b.mentionable)


def _categories_equal(a, b) -> bool:
    return _overwrites_equal(a.overwrites, b.overwrites)


def _channels_equal(a: ChannelData, b: ChannelData) -> bool:
    return (a.type, a.topic, a.nsfw, a.slowmode_delay) == (b.type, b.topic, b.nsfw, b.slowmode_delay) and _overwrites_equal(
        a.overwrites, b.overwrites
    )


def _compare_named_list(old_items, new_items, *, kind: str, key, equal, name_from_key=lambda k: k):
    """Обобщённое сравнение двух списков именованных сущностей бэкапа по
    ключу — общая основа для роль/категория/канал/эмодзи/стикер веток
    compare_backups(), чтобы не повторять одну и ту же группировку 5 раз."""
    old_by_key = _group_by_name(old_items, key=key)
    new_by_key = _group_by_name(new_items, key=key)
    items: list[PlanItem] = []

    for k, new_group in new_by_key.items():
        old_group = old_by_key.get(k, [])
        name = name_from_key(k)
        if not old_group:
            for obj in new_group:
                items.append(PlanItem(kind=kind, action=ACTION_CREATE, name=name, backup_obj=obj))
        elif not equal(old_group[0], new_group[0]):
            items.append(PlanItem(kind=kind, action=ACTION_UPDATE, name=name, backup_obj=new_group[0]))

    for k, old_group in old_by_key.items():
        if k not in new_by_key:
            name = name_from_key(k)
            for obj in old_group:
                items.append(PlanItem(kind=kind, action=ACTION_REMOVE, name=name, backup_obj=obj))

    return items


@dataclass
class DuplicateGroup:
    """Несколько сущностей сервера с одинаковым именем — то, что мешает
    сопоставлению по имени при restore (см. модуль docstring выше) и обычно
    само по себе является мусором, накопившимся за время жизни сервера."""

    kind: str
    name: str
    ids: list[int]


def find_duplicate_entities(guild: discord.Guild) -> list[DuplicateGroup]:
    """Сканирует сервер (без какого-либо бэкапа) и находит роли/категории/каналы
    с повторяющимися именами — те самые группы, которые build_plan() выше
    помечает как ACTION_CONFLICT и пропускает. Отдельная команда удобнее:
    не нужно иметь бэкап, чтобы просто посмотреть, что на сервере задвоено."""
    groups: list[DuplicateGroup] = []

    roles_by_name = _group_by_name([r for r in guild.roles if not r.is_default() and not r.managed], key=lambda r: r.name)
    for name, roles in roles_by_name.items():
        if len(roles) > 1:
            groups.append(DuplicateGroup(kind=KIND_ROLE, name=name, ids=[r.id for r in roles]))

    categories_by_name = _group_by_name(guild.categories, key=lambda c: c.name)
    for name, cats in categories_by_name.items():
        if len(cats) > 1:
            groups.append(DuplicateGroup(kind=KIND_CATEGORY, name=name, ids=[c.id for c in cats]))

    live_channels = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]
    channels_by_key = _group_by_name(
        live_channels, key=lambda c: _channel_key(c.name, c.category.name if c.category else None)
    )
    for (category_name, name), chans in channels_by_key.items():
        if len(chans) > 1:
            label = f"{name} (в категории «{category_name}»)" if category_name else name
            groups.append(DuplicateGroup(kind=KIND_CHANNEL, name=label, ids=[c.id for c in chans]))

    return groups
