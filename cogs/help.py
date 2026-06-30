"""Команда L.help со списком всех доступных команд."""

import discord
from discord.ext import commands


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help", aliases=["помощь"])
    async def help_cmd(self, ctx: commands.Context):
        prefix = self.bot.db.get_setting(ctx.guild.id, "prefix", "L.") if ctx.guild else "L."
        embed = discord.Embed(
            title="📖 Команды бота",
            description=f"Текущий префикс: `{prefix}`",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="⚙️ Настройки",
            value=(
                f"`{prefix}settings` — показать настройки\n"
                f"`{prefix}prefix <новый>` — сменить префикс\n"
                f"`{prefix}setlogchannel <#канал>` — канал для логов безопасности\n"
                f"`{prefix}mod-log-channel <#канал>` — канал для мод-логов\n"
                f"`{prefix}antispam on|off [лимит] [интервал] [действие]`\n"
                f"`{prefix}antilink on|off [действие] [длительность_мута_сек]`\n"
                f"`{prefix}antimention on|off [лимит] [действие] [мут_сек]`\n"
                f"`{prefix}antiraid on|off [лимит] [интервал] [действие]`\n"
                f"`{prefix}auditwatch on|off [#канал]` — дублировать аудит-лог сервера в канал\n"
                f"`{prefix}verification on|off [@роль_до] [@роль_после] [таймаут_мин]`\n"
                f"`{prefix}verification-channel <#канал>`\n"
                f"`{prefix}verification-post` — отправить кнопку подтверждения\n"
                f"`{prefix}ban-new-users on|off [мин_часов] [описание]`\n"
                f"`{prefix}isolate-new-bots on|off`\n"
                f"`{prefix}ignore-spam add|remove|reset [#канал]`\n"
                f"`{prefix}add-warn-action <N> <действие> [сек]`\n"
                f"`{prefix}remove-warn-action <N>`\n"
                f"`{prefix}warn-actions` — список авто-наказаний"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Модерация",
            value=(
                f"`{prefix}warn @user [причина]`\n"
                f"`{prefix}warnings @user`\n"
                f"`{prefix}unwarn @user` / `{prefix}clearwarns @user`\n"
                f"`{prefix}kick @user [причина]`\n"
                f"`{prefix}ban @user [причина]` / `{prefix}unban <id>`\n"
                f"`{prefix}mute @user [сек] [причина]` / `{prefix}unmute @user`\n"
                f"`{prefix}clear [кол-во]`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🚨 Защита сервера",
            value=(
                f"`{prefix}unlock` — снять lockdown после ложного антирейда\n"
                "Анти-спам, анти-рейд и анти-краш работают автоматически согласно настройкам."
            ),
            inline=False,
        )
        embed.add_field(
            name="💾 Бэкап",
            value=(
                f"`{prefix}backup save` — сохранить структуру сервера\n"
                f"`{prefix}backup restore [область] [safe|strict]` — восстановить из бэкапа "
                "(показывает план изменений и требует подтверждения; область: all/roles/channels/categories/permissions)\n"
                f"`{prefix}backup rollback [id]` — откатиться к авто-бэкапу перед последним restore\n"
                f"`{prefix}backup resume [id]` — продолжить восстановление, прерванное перезапуском бота\n"
                f"`{prefix}backup diff <id1> <id2>` — сравнить два бэкапа между собой\n"
                f"`{prefix}backup info` — информация о последнем бэкапе\n"
                f"`{prefix}backup list` — список всех сохранённых бэкапов\n"
                f"`{prefix}backup duplicates` — найти роли/каналы с одинаковыми именами\n"
                "Также доступно как слэш-команды: `/save`, `/load`, `/rollback`, `/resume`, "
                "`/backups`, `/backup-diff`, `/find-duplicates`, `/clone-template` (только владелец бота)."
            ),
            inline=False,
        )
        embed.set_footer(text="Если забыли префикс — просто @упомяните бота. Также доступны слэш-команды (/)")
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if self.bot.user in message.mentions and len(message.content.split()) <= 2:
            prefix = self.bot.db.get_setting(message.guild.id, "prefix", "L.")
            await message.channel.send(f"Мой текущий префикс на этом сервере: `{prefix}`")


async def setup(bot):
    await bot.add_cog(Help(bot))
