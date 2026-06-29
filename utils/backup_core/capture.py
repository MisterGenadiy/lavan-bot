"""Снимок текущей структуры сервера — то, что выполняется по /save.

Отдельно от restore/diff, чтобы (а) можно было переиспользовать для
emergency-бэкапа перед restore и (б) не смешивать "что сохраняем" с
"как сравниваем/применяем", это разные ответственности."""

from __future__ import annotations

import base64

import discord

from . import storage
from .models import (
    SCHEMA_VERSION,
    AutoModActionData,
    AutoModRuleData,
    BackupData,
    BackupMetadata,
    CategoryData,
    ChannelData,
    EmojiData,
    GuildSettingsData,
    OverwriteData,
    RoleData,
    StickerData,
    WebhookData,
)

# Discord ограничивает размер кастомных эмодзи/стикеров (256 КБ), так что
# инлайнить картинки base64 в JSON безопасно по размеру даже для сервера
# с максимальным количеством слотов эмодзи.
_ASSET_FETCH_LIMIT = 256 * 1024


def _overwrites_to_data(overwrites: dict) -> list[OverwriteData]:
    result = []
    for target, overwrite in overwrites.items():
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


async def _read_asset_b64(asset: discord.Asset | None) -> str | None:
    if asset is None:
        return None
    try:
        raw = await asset.read()
    except (discord.HTTPException, discord.NotFound):
        return None
    if len(raw) > _ASSET_FETCH_LIMIT:
        return None
    return base64.b64encode(raw).decode("ascii")


def _capture_roles(guild: discord.Guild) -> list[RoleData]:
    roles = []
    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue
        roles.append(
            RoleData(
                name=role.name,
                color=role.color.value,
                hoist=role.hoist,
                mentionable=role.mentionable,
                permissions=role.permissions.value,
                position=role.position,
                is_managed=role.managed,
            )
        )
    return roles


def _capture_categories(guild: discord.Guild) -> list[CategoryData]:
    return [
        CategoryData(name=cat.name, position=cat.position, overwrites=_overwrites_to_data(cat.overwrites))
        for cat in sorted(guild.categories, key=lambda c: c.position)
    ]


def _channel_type_str(channel) -> str:
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.TextChannel) and channel.is_news():
        return "announcement"
    return "text"


def _capture_channels(guild: discord.Guild) -> list[ChannelData]:
    channels = []
    sortable = [c for c in guild.channels if not isinstance(c, discord.CategoryChannel)]
    for channel in sorted(sortable, key=lambda c: c.position):
        entry = ChannelData(
            name=channel.name,
            type=_channel_type_str(channel),
            position=channel.position,
            category_name=channel.category.name if channel.category else None,
            topic=getattr(channel, "topic", None),
            nsfw=getattr(channel, "nsfw", False),
            slowmode_delay=getattr(channel, "slowmode_delay", 0) or 0,
            bitrate=getattr(channel, "bitrate", None),
            user_limit=getattr(channel, "user_limit", None),
            overwrites=_overwrites_to_data(channel.overwrites),
        )
        channels.append(entry)
    return channels


async def _capture_emojis(guild: discord.Guild) -> list[EmojiData]:
    emojis = []
    for emoji in guild.emojis:
        image_b64 = await _read_asset_b64(emoji.url if hasattr(emoji, "url") else None)
        emojis.append(EmojiData(name=emoji.name, animated=emoji.animated, image_b64=image_b64))
    return emojis


async def _capture_stickers(guild: discord.Guild) -> list[StickerData]:
    stickers = []
    for sticker in guild.stickers:
        image_b64 = await _read_asset_b64(sticker.url if hasattr(sticker, "url") else None)
        stickers.append(
            StickerData(
                name=sticker.name,
                description=sticker.description or "",
                emoji=sticker.emoji or "❓",
                image_b64=image_b64,
            )
        )
    return stickers


def _capture_guild_settings(guild: discord.Guild) -> GuildSettingsData:
    return GuildSettingsData(
        name=guild.name,
        verification_level=getattr(guild.verification_level, "name", None),
        explicit_content_filter=getattr(guild.explicit_content_filter, "name", None),
        default_notifications=getattr(guild.default_notifications, "name", None),
        afk_channel_name=guild.afk_channel.name if guild.afk_channel else None,
        afk_timeout=guild.afk_timeout,
        system_channel_name=guild.system_channel.name if guild.system_channel else None,
    )


async def _capture_automod_rules(guild: discord.Guild) -> list[AutoModRuleData]:
    """AutoMod недоступен или бот может не иметь прав Manage Server — в этом
    случае Discord вернёт HTTPException, и мы просто пропускаем секцию,
    а не валим весь /save из-за одной необязательной части бэкапа."""
    try:
        fetched = await guild.fetch_automod_rules()
    except discord.HTTPException:
        return []

    rules = []
    for rule in fetched:
        trigger = rule.trigger
        actions = []
        for act in rule.actions:
            channel = guild.get_channel(act.channel_id) if act.channel_id else None
            actions.append(
                AutoModActionData(
                    type=act.type.name,
                    channel_name=channel.name if channel else None,
                    duration_seconds=int(act.duration.total_seconds()) if act.duration else None,
                    custom_message=act.custom_message,
                )
            )
        rules.append(
            AutoModRuleData(
                name=rule.name,
                event_type=rule.event_type.name,
                trigger_type=trigger.type.name,
                keyword_filter=list(trigger.keyword_filter or []),
                regex_patterns=list(trigger.regex_patterns or []),
                presets=list(trigger.presets.to_array()) if trigger.presets else [],
                allow_list=list(trigger.allow_list or []),
                mention_limit=trigger.mention_limit,
                mention_raid_protection=bool(trigger.mention_raid_protection),
                enabled=rule.enabled,
                exempt_role_names=[r.name for r in rule.exempt_roles if r is not None],
                exempt_channel_names=[c.name for c in rule.exempt_channels if c is not None],
                actions=actions,
            )
        )
    return rules


async def _capture_webhooks(guild: discord.Guild) -> list[WebhookData]:
    """Только структура (имя, канал, аватар) — токен вебхука не сохраняется:
    Discord выдаёт новый при создании, старый восстановить нельзя в принципе."""
    try:
        fetched = await guild.webhooks()
    except discord.HTTPException:
        return []

    webhooks = []
    for wh in fetched:
        if wh.channel is None or not wh.name:
            continue
        avatar_b64 = await _read_asset_b64(wh.avatar) if wh.avatar else None
        webhooks.append(WebhookData(name=wh.name, channel_name=wh.channel.name, avatar_b64=avatar_b64))
    return webhooks


async def capture_guild(guild: discord.Guild, *, is_emergency: bool = False, note: str | None = None) -> BackupData:
    """Делает полный снимок структуры сервера. Ничего не пишет на диск —
    за это отвечает storage.save(), вызывающий код решает, сохранять ли результат."""
    roles = _capture_roles(guild)
    categories = _capture_categories(guild)
    channels = _capture_channels(guild)
    emojis = await _capture_emojis(guild)
    stickers = await _capture_stickers(guild)
    guild_settings = _capture_guild_settings(guild)
    automod_rules = await _capture_automod_rules(guild)
    webhooks = await _capture_webhooks(guild)

    counts = {
        "roles": len(roles),
        "categories": len(categories),
        "channels": len(channels),
        "text_channels": sum(1 for c in channels if c.type in ("text", "announcement")),
        "voice_channels": sum(1 for c in channels if c.type in ("voice", "stage")),
        "forum_channels": sum(1 for c in channels if c.type == "forum"),
        "emojis": len(emojis),
        "stickers": len(stickers),
        "automod_rules": len(automod_rules),
        "webhooks": len(webhooks),
    }

    metadata = BackupMetadata(
        backup_id=storage.generate_backup_id(),
        created_at=discord.utils.utcnow().isoformat(),
        schema_version=SCHEMA_VERSION,
        guild_id=guild.id,
        guild_name=guild.name,
        counts=counts,
        is_emergency=is_emergency,
        note=note,
    )

    return BackupData(
        metadata=metadata,
        roles=roles,
        categories=categories,
        channels=channels,
        emojis=emojis,
        stickers=stickers,
        guild_settings=guild_settings,
        automod_rules=automod_rules,
        webhooks=webhooks,
    )
