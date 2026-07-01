"""Аудит-лог вотчер: пересылает ВСЕ записи аудит-лога сервера в указанный
канал — не только то, на что реагируют анти-рейд/анти-краш (там это лимиты
+ автонаказание), а вообще каждое действие модератора/админа/бота:
создание/удаление/изменение каналов, ролей, прав, баны/кики, изменения
настроек сервера и т.п.

Использует discord.py-событие on_audit_log_entry_create — отдельное событие
именно под это (а не периодический опрос guild.audit_logs()), интент
`moderation` для него уже включён в bot.py."""

import discord
from discord.ext import commands

# Человекочитаемые подписи для самых частых типов событий. Для всего, что
# не попало в словарь, используется автоматически очеловеченное имя enum'а
# (например AuditLogAction.sticker_create -> "Sticker create") — так список
# не нужно поддерживать исчерпывающим вручную при выходе новых типов событий.
_ACTION_LABELS = {
    discord.AuditLogAction.guild_update: "⚙️ Изменены настройки сервера",
    discord.AuditLogAction.channel_create: "➕ Канал создан",
    discord.AuditLogAction.channel_update: "✏️ Канал изменён",
    discord.AuditLogAction.channel_delete: "🗑️ Канал удалён",
    discord.AuditLogAction.overwrite_create: "🔐 Право доступа создано",
    discord.AuditLogAction.overwrite_update: "🔐 Право доступа изменено",
    discord.AuditLogAction.overwrite_delete: "🔐 Право доступа удалено",
    discord.AuditLogAction.kick: "👋 Кик",
    discord.AuditLogAction.member_prune: "🧹 Чистка неактивных участников",
    discord.AuditLogAction.ban: "🔨 Бан",
    discord.AuditLogAction.unban: "🔓 Разбан",
    discord.AuditLogAction.member_update: "✏️ Изменён участник",
    discord.AuditLogAction.member_role_update: "🎭 Изменены роли участника",
    discord.AuditLogAction.member_move: "🔊 Участник перемещён в другой голосовой канал",
    discord.AuditLogAction.member_disconnect: "🔇 Участник отключён от голосового канала",
    discord.AuditLogAction.bot_add: "🤖 Бот добавлен на сервер",
    discord.AuditLogAction.role_create: "➕ Роль создана",
    discord.AuditLogAction.role_update: "✏️ Роль изменена",
    discord.AuditLogAction.role_delete: "🗑️ Роль удалена",
    discord.AuditLogAction.invite_create: "✉️ Приглашение создано",
    discord.AuditLogAction.invite_delete: "✉️ Приглашение удалено",
    discord.AuditLogAction.webhook_create: "🔗 Вебхук создан",
    discord.AuditLogAction.webhook_update: "🔗 Вебхук изменён",
    discord.AuditLogAction.webhook_delete: "🔗 Вебхук удалён",
    discord.AuditLogAction.emoji_create: "😀 Эмодзи добавлен",
    discord.AuditLogAction.emoji_update: "😀 Эмодзи изменён",
    discord.AuditLogAction.emoji_delete: "😀 Эмодзи удалён",
    discord.AuditLogAction.message_delete: "🗑️ Сообщение удалено",
    discord.AuditLogAction.message_bulk_delete: "🗑️ Массовое удаление сообщений",
    discord.AuditLogAction.message_pin: "📌 Сообщение закреплено",
    discord.AuditLogAction.message_unpin: "📌 Сообщение откреплено",
    discord.AuditLogAction.integration_create: "🔌 Интеграция подключена",
    discord.AuditLogAction.integration_update: "🔌 Интеграция изменена",
    discord.AuditLogAction.integration_delete: "🔌 Интеграция отключена",
    discord.AuditLogAction.sticker_create: "🏷️ Стикер добавлен",
    discord.AuditLogAction.sticker_update: "🏷️ Стикер изменён",
    discord.AuditLogAction.sticker_delete: "🏷️ Стикер удалён",
    discord.AuditLogAction.automod_rule_create: "🛡️ Правило AutoMod создано",
    discord.AuditLogAction.automod_rule_update: "🛡️ Правило AutoMod изменено",
    discord.AuditLogAction.automod_rule_delete: "🛡️ Правило AutoMod удалено",
    discord.AuditLogAction.automod_block_message: "🛡️ AutoMod заблокировал сообщение",
}

_IGNORED_CHANGE_KEYS = {"id", "type"}  # шумные технические поля, не несущие пользователю смысла
_MAX_CHANGED_FIELDS = 4


def _label_for(action: discord.AuditLogAction) -> str:
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    return action.name.replace("_", " ").capitalize()


def _format_target(target) -> str:
    if target is None:
        return "—"
    name = getattr(target, "name", None) or getattr(target, "mention", None) or str(target)
    target_id = getattr(target, "id", None)
    return f"{name} (`{target_id}`)" if target_id else str(name)


def _format_changes(entry: discord.AuditLogEntry) -> str:
    """До 4 изменённых полей в формате `поле: было → стало` — иначе для
    guild_update/role_update и т.п. придётся читать длинный дамп объекта."""
    try:
        before_attrs = {k: v for k, v in vars(entry.before).items() if k not in _IGNORED_CHANGE_KEYS}
    except (AttributeError, TypeError):
        before_attrs = {}
    try:
        after_attrs = {k: v for k, v in vars(entry.after).items() if k not in _IGNORED_CHANGE_KEYS}
    except (AttributeError, TypeError):
        after_attrs = {}

    keys = list(dict.fromkeys([*before_attrs.keys(), *after_attrs.keys()]))[:_MAX_CHANGED_FIELDS]
    if not keys:
        return ""

    lines = []
    for key in keys:
        before_val = before_attrs.get(key, "—")
        after_val = after_attrs.get(key, "—")
        lines.append(f"`{key}`: {before_val} → {after_val}")
    return "\n".join(lines)


def _actor_icon_url(actor) -> str | None:
    """Безопасно получает URL аватара актора аудит-лога.
    actor может быть: discord.Member / discord.User (есть display_avatar),
    discord.Object (заглушка для пользователей вне кэша — нет display_avatar),
    или None (Discord не прислал актора вовсе)."""
    avatar = getattr(actor, "display_avatar", None)
    if avatar is None:
        return None
    return getattr(avatar, "url", None)


def _build_entry_embed(entry: discord.AuditLogEntry) -> discord.Embed:
    embed = discord.Embed(
        title=_label_for(entry.action),
        color=discord.Color.blurple(),
        timestamp=entry.created_at,
    )
    actor = entry.user
    embed.set_author(
        name=str(actor) if actor else "Неизвестно",
        icon_url=_actor_icon_url(actor),
    )
    embed.add_field(name="Цель", value=_format_target(entry.target), inline=False)
    changes = _format_changes(entry)
    if changes:
        embed.add_field(name="Изменения", value=changes[:1024], inline=False)
    if entry.reason:
        embed.add_field(name="Причина", value=entry.reason[:1024], inline=False)
    return embed


class AuditWatch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        settings = self.bot.db.get_all_settings(entry.guild.id)
        if not settings.get("auditwatch_enabled"):
            return
        channel_id = settings.get("auditwatch_channel_id")
        if not channel_id:
            return
        channel = entry.guild.get_channel(channel_id)
        if channel is None:
            return

        try:
            embed = _build_entry_embed(entry)
            await channel.send(embed=embed)
        except discord.HTTPException:
            # Аудит-лог может сыпать события заметно чаще, чем антиспам/антирейд,
            # поэтому отдельная ошибка отправки не должна валить остальной бот —
            # просто пропускаем эту конкретную запись.
            pass


async def setup(bot):
    await bot.add_cog(AuditWatch(bot))
