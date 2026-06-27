"""Базовые команды модерации + система варнов с авто-наказаниями."""

from datetime import timedelta

import discord
from discord.ext import commands

from utils.moderation_utils import add_warn_and_escalate, bot_can_moderate, is_mod_or_admin, send_mod_log


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="warn")
    @is_mod_or_admin()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Не указана"):
        """Выдаёт предупреждение пользователю."""
        count, action_row = await add_warn_and_escalate(self.bot, ctx.guild, member, ctx.author.id, reason)
        embed = discord.Embed(
            description=f"⚠️ {member.mention} получил предупреждение ({count}-е).\n**Причина:** {reason}",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Модератор: {ctx.author}")
        await ctx.send(embed=embed)
        await send_mod_log(self.bot, ctx.guild, embed)
        if action_row:
            await ctx.send(
                f"🔧 {member.mention} получил `{action_row['action']}` за {count} варн(а/ов)."
            )

    @commands.command(name="warnings", aliases=["warns"])
    @is_mod_or_admin()
    async def warnings_cmd(self, ctx: commands.Context, member: discord.Member):
        """Показывает список предупреждений пользователя."""
        rows = self.bot.db.get_warns(ctx.guild.id, member.id)
        if not rows:
            return await ctx.send(f"У {member.mention} нет предупреждений.")
        lines = [f"**#{i+1}** — {r['reason']} (модератор: <@{r['moderator_id']}>)" for i, r in enumerate(rows)]
        embed = discord.Embed(
            title=f"Предупреждения {member}", description="\n".join(lines), color=discord.Color.orange()
        )
        await ctx.send(embed=embed)

    @commands.command(name="unwarn")
    @is_mod_or_admin()
    async def unwarn(self, ctx: commands.Context, member: discord.Member):
        """Снимает последнее предупреждение."""
        ok = self.bot.db.remove_last_warn(ctx.guild.id, member.id)
        await ctx.send("✅ Снято одно предупреждение." if ok else "У пользователя нет предупреждений.")

    @commands.command(name="clearwarns")
    @is_mod_or_admin()
    async def clear_warns(self, ctx: commands.Context, member: discord.Member):
        """Полностью очищает предупреждения пользователя."""
        self.bot.db.clear_warns(ctx.guild.id, member.id)
        await ctx.send(f"✅ Все предупреждения {member.mention} очищены.")

    @commands.command(name="kick")
    @is_mod_or_admin()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Не указана"):
        """Исключает пользователя с сервера."""
        error = bot_can_moderate(ctx.guild, member, "kick_members")
        if error:
            return await ctx.send(error)
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            return await ctx.send("⛔ Discord отклонил действие (недостаточно прав у бота).")
        embed = discord.Embed(description=f"👢 {member} исключён.\n**Причина:** {reason}", color=discord.Color.red())
        embed.set_footer(text=f"Модератор: {ctx.author}")
        await ctx.send(embed=embed)
        await send_mod_log(self.bot, ctx.guild, embed)

    @commands.command(name="ban")
    @is_mod_or_admin()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "Не указана"):
        """Банит пользователя на сервере."""
        error = bot_can_moderate(ctx.guild, member, "ban_members")
        if error:
            return await ctx.send(error)
        try:
            await member.ban(reason=reason, delete_message_seconds=0)
        except discord.Forbidden:
            return await ctx.send("⛔ Discord отклонил действие (недостаточно прав у бота).")
        embed = discord.Embed(description=f"🔨 {member} забанен.\n**Причина:** {reason}", color=discord.Color.dark_red())
        embed.set_footer(text=f"Модератор: {ctx.author}")
        await ctx.send(embed=embed)
        await send_mod_log(self.bot, ctx.guild, embed)

    @commands.command(name="unban")
    @is_mod_or_admin()
    async def unban(self, ctx: commands.Context, user_id: int):
        """Разбанивает пользователя по ID."""
        user = discord.Object(id=user_id)
        try:
            await ctx.guild.unban(user)
        except discord.Forbidden:
            return await ctx.send("⛔ У бота нет права Ban Members, необходимого для разбана.")
        except discord.NotFound:
            return await ctx.send("⚠️ Пользователь с таким ID не найден в списке банов.")
        await ctx.send(f"✅ Пользователь с ID {user_id} разбанен.")

    @commands.command(name="mute")
    @is_mod_or_admin()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def mute(self, ctx: commands.Context, member: discord.Member, seconds: int = 300, *, reason: str = "Не указана"):
        """Выдаёт тайм-аут (мут) пользователю на N секунд (по умолчанию 300)."""
        error = bot_can_moderate(ctx.guild, member, "moderate_members")
        if error:
            return await ctx.send(error)
        seconds = max(1, min(seconds, 28 * 24 * 60 * 60))
        try:
            await member.timeout(timedelta(seconds=seconds), reason=reason)
        except discord.Forbidden:
            return await ctx.send("⛔ Discord отклонил действие (недостаточно прав у бота).")
        embed = discord.Embed(
            description=f"🔇 {member.mention} получил тайм-аут на {seconds} сек.\n**Причина:** {reason}",
            color=discord.Color.yellow(),
        )
        embed.set_footer(text=f"Модератор: {ctx.author}")
        await ctx.send(embed=embed)
        await send_mod_log(self.bot, ctx.guild, embed)

    @commands.command(name="unmute")
    @is_mod_or_admin()
    async def unmute(self, ctx: commands.Context, member: discord.Member):
        """Снимает тайм-аут с пользователя."""
        try:
            await member.timeout(None)
        except discord.Forbidden:
            return await ctx.send("⛔ Discord отклонил действие (недостаточно прав у бота).")
        await ctx.send(f"✅ Тайм-аут снят с {member.mention}.")

    @commands.command(name="clear", aliases=["purge"])
    @is_mod_or_admin()
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def clear(self, ctx: commands.Context, amount: int = 10):
        """Удаляет последние N сообщений в канале (по умолчанию 10)."""
        amount = max(1, min(amount, 200))
        try:
            deleted = await ctx.channel.purge(limit=amount + 1)
        except discord.Forbidden:
            return await ctx.send("⛔ У бота нет права Manage Messages в этом канале.")
        await ctx.send(f"🧹 Удалено сообщений: {len(deleted) - 1}", delete_after=5)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
