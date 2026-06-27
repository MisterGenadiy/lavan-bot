"""Антилинк: удаляет сообщения со ссылками и применяет настроенное наказание.
Активируется через /antilink или L.setlogchannel (логируется в лог-канал).
"""

import re

import discord
from discord.ext import commands

from utils.moderation_utils import add_warn_and_escalate, apply_punishment, send_log

# Паттерн: http(s)://, www., discord.gg/, discord.com/invite/
_URL_RE = re.compile(
    r"(?:https?://|www\.|discord\.gg/|discord\.com/invite/)[^\s<>\"'`]*",
    re.IGNORECASE,
)


class AntiLink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        # Модераторов и администраторов не трогаем
        if isinstance(message.author, discord.Member) and (
            message.author.guild_permissions.manage_messages
            or message.author.guild_permissions.administrator
        ):
            return

        settings = self.bot.db.get_all_settings(message.guild.id)
        if not settings.get("antilink_enabled", False):
            return

        ignore_channels = set(settings.get("ignore_spam_channels", []))
        if message.channel.id in ignore_channels:
            return

        if not _URL_RE.search(message.content):
            return

        # Удаляем сообщение
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        member = message.author
        action = settings.get("antilink_action", "warn")
        mute_seconds = settings.get("antilink_mute_seconds", 300)
        reason = "Антилинк: размещение ссылок запрещено"

        action_row = None
        if action == "warn":
            count, action_row = await add_warn_and_escalate(self.bot, message.guild, member, self.bot.user.id, reason)
            notify = f"🔗 {member.mention}, ссылки запрещены на этом сервере! (предупреждение #{count})"
        else:
            await apply_punishment(message.guild, member, action, reason, duration_seconds=mute_seconds)
            notify = f"🔗 {member.mention} получил `{action}` за размещение ссылки."

        try:
            await message.channel.send(notify, delete_after=10)
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title="🔗 Антилинк сработал",
            description=f"Пользователь: {member.mention}\nДействие: `{action}`\nСообщение удалено.",
            color=discord.Color.red(),
        )
        await send_log(self.bot, message.guild, embed)

        if action_row:
            await send_log(
                self.bot,
                message.guild,
                discord.Embed(
                    description=f"🔧 {member.mention} получил `{action_row['action']}` "
                    f"за {count} варн(а/ов) (эскалация после антилинка).",
                    color=discord.Color.dark_red(),
                ),
            )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Проверяем отредактированные сообщения на наличие ссылок."""
        await self.on_message(after)


async def setup(bot):
    await bot.add_cog(AntiLink(bot))
