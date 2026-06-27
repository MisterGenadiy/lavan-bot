"""Префиксные команды L.backup save|restore|info — обёртка над utils/backup_core.py."""

import discord
from discord.ext import commands

from utils import backup_core
from utils.moderation_utils import is_mod_or_admin


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="backup")
    @is_mod_or_admin()
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def backup_cmd(self, ctx: commands.Context, action: str = "save"):
        """L.backup save | L.backup restore | L.backup info

        Кулдаун 30 сек на сервер — save/restore создают десятки последовательных
        запросов к Discord API, повторный запуск раньше срока только мешает."""
        action = action.lower()
        if action == "save":
            await self._save(ctx)
        elif action == "restore":
            await self._restore(ctx)
        elif action == "info":
            await self._info(ctx)
        else:
            await ctx.send("Использование: `L.backup save|restore|info`")

    async def _save(self, ctx: commands.Context):
        counts = await backup_core.save_backup(ctx.guild)
        await ctx.send(f"💾 Бэкап сохранён: {counts['roles']} ролей, {counts['channels']} каналов/категорий.")

    async def _info(self, ctx: commands.Context):
        info = backup_core.get_backup_info(ctx.guild.id)
        if not info:
            return await ctx.send("Бэкап для этого сервера не найден.")
        await ctx.send(
            f"📦 Последний бэкап сервера «{info['guild_name']}»: "
            f"{info['roles']} ролей, {info['channels']} каналов."
        )

    async def _restore(self, ctx: commands.Context):
        if not backup_core.has_backup(ctx.guild.id):
            return await ctx.send("⚠️ Бэкап не найден. Сначала выполните `L.backup save`.")

        await ctx.send(
            "⚠️ Восстановление **удалит текущие роли и каналы** и создаст их заново из бэкапа.\n"
            "Напишите `да` в течение 30 секунд для подтверждения."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ("да", "yes")

        try:
            await self.bot.wait_for("message", check=check, timeout=30)
        except Exception:
            return await ctx.send("⏱️ Время ожидания истекло. Восстановление отменено.")

        status = await ctx.send("⏳ Восстановление начато...")
        try:
            counts = await backup_core.restore_backup(ctx.guild)
            content = f"✅ Восстановление завершено: {counts['roles']} ролей, {counts['channels']} каналов."
        except discord.HTTPException as e:
            content = f"⚠️ Восстановление завершилось с ошибкой Discord API: {e}"

        # Канал, где была вызвана команда, мог быть удалён в процессе восстановления —
        # пробуем отредактировать исходное сообщение, а если не вышло — шлём в любой доступный канал/ЛС.
        async def try_edit(text):
            await status.edit(content=text)

        await backup_core.notify_guild_or_dm(ctx.guild, ctx.author, content, preferred=try_edit)


async def setup(bot):
    await bot.add_cog(Backup(bot))
