"""Уведомление пользователя об итоге save/restore — устойчиво к тому, что
исходный канал/сообщение могли быть удалены во время восстановления сервера.
Логика идентична предыдущей версии backup_core.py."""

import discord


async def notify_guild_or_dm(guild: discord.Guild, user: discord.abc.User, content: str, preferred=None):
    """Порядок попыток: preferred (например interaction.followup.send) ->
    system_channel сервера -> первый доступный текстовый канал -> ЛС пользователю."""
    if preferred is not None:
        try:
            await preferred(content)
            return
        except discord.HTTPException:
            pass

    target = guild.system_channel
    if target is None:
        for c in guild.text_channels:
            target = c
            break

    if target is not None:
        try:
            await target.send(content)
            return
        except discord.HTTPException:
            pass

    try:
        await user.send(content)
    except discord.Forbidden:
        pass
