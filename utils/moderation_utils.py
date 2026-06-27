"""Вспомогательные функции: применение наказаний, проверка прав, тайм-аут."""

from datetime import timedelta

import discord

# Discord ограничивает timeout максимум 28 днями.
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60


async def apply_punishment(
    guild: discord.Guild,
    member: discord.Member,
    action: str,
    reason: str,
    duration_seconds: int | None = None,
):
    """Применяет наказание: warn (ничего не делает, варн уже записан отдельно),
    mute (timeout), kick, ban."""
    action = (action or "").lower()

    if action == "mute":
        seconds = duration_seconds or 300
        # Защита от некорректных/слишком больших значений — иначе Discord API
        # вернёт ошибку и наказание не применится вообще.
        seconds = max(1, min(seconds, MAX_TIMEOUT_SECONDS))
        try:
            await member.timeout(timedelta(seconds=seconds), reason=reason)
        except discord.Forbidden:
            pass
    elif action == "kick":
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            pass
    elif action == "ban":
        try:
            await member.ban(reason=reason, delete_message_seconds=0)
        except discord.Forbidden:
            pass
    # action == "warn" -> ничего дополнительно делать не нужно


async def send_log(bot, guild: discord.Guild, embed: discord.Embed):
    """Отправляет embed в настроенный лог-канал безопасности (антиспам/антирейд/анти-краш)."""
    channel_id = bot.db.get_setting(guild.id, "log_channel_id")
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


async def send_mod_log(bot, guild: discord.Guild, embed: discord.Embed):
    """Отправляет embed в канал логов модераторских действий (kick/ban/mute/warn).
    Если отдельный канал не настроен — падает обратно на общий лог-канал."""
    channel_id = bot.db.get_setting(guild.id, "mod_log_channel_id") or bot.db.get_setting(
        guild.id, "log_channel_id"
    )
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        pass


_PERMISSION_LABELS = {
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "moderate_members": "Moderate Members (timeout)",
}


def bot_can_moderate(guild: discord.Guild, member: discord.Member, permission: str = None) -> str | None:
    """Проверяет ДО вызова Discord API, может ли бот в принципе применить
    наказание к этому участнику. Возвращает текст ошибки, если нет, либо None,
    если всё в порядке.

    Без этой проверки команды вроде /kick просто получали бы 403 Forbidden
    от Discord без понятного объяснения причины (роль бота ниже роли цели,
    у бота нет нужного права, цель — владелец сервера и т.п.).

    permission — имя атрибута discord.Permissions, которое требуется для действия:
    'kick_members', 'ban_members' или 'moderate_members'. Если не передано,
    проверяется только иерархия ролей."""
    me = guild.me
    if member.id == guild.owner_id:
        return "⛔ Невозможно применить наказание к владельцу сервера."
    if member.id == me.id:
        return "⛔ Бот не может применить наказание к самому себе."
    if permission and not getattr(me.guild_permissions, permission, False):
        label = _PERMISSION_LABELS.get(permission, permission)
        return (
            f"⛔ У бота нет права **{label}** на этом сервере. "
            "Выдайте его роли бота в настройках сервера и повторите попытку."
        )
    if member.top_role >= me.top_role:
        return (
            "⛔ Роль бота должна быть **выше** роли участника, иначе Discord не позволит "
            "применить наказание. Поднимите роль бота в настройках сервера и повторите попытку."
        )
    return None


def is_mod_or_admin():
    """Декоратор-проверка: пользователь должен иметь права модератора."""
    from discord.ext import commands

    async def predicate(ctx):
        if ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator:
            return True
        return False

    return commands.check(predicate)


async def add_warn_and_escalate(
    bot,
    guild: discord.Guild,
    member: discord.Member,
    moderator_id: int,
    reason: str,
):
    """Добавляет предупреждение пользователю и, если для нового количества варнов
    настроено авто-наказание (L.add-warn-action), сразу его применяет.

    Используется ВСЕМИ источниками варнов (ручная команда L.warn/​/warn, а также
    автоматические варны от антиспама и антилинка), чтобы эскалация наказаний
    срабатывала одинаково независимо от того, кто или что выдало предупреждение.

    Возвращает (count, action_row) — count — порядковый номер варна,
    action_row — словарь с применённым авто-наказанием либо None, если оно не настроено.
    """
    count = bot.db.add_warn(guild.id, member.id, moderator_id, reason)
    action_row = bot.db.get_warn_action(guild.id, count)
    if action_row:
        await apply_punishment(
            guild,
            member,
            action_row["action"],
            reason=f"Авто-наказание: достигнуто {count} варн(а/ов)",
            duration_seconds=action_row["duration_seconds"],
        )
    return count, action_row
