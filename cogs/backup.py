"""Префиксные команды L.backup save|restore|info — обёртка над utils/backup_core.

L.backup restore теперь не удаляет всё подряд: сначала строится план
(что создастся/обновится/удалится/какие конфликты) и показывается пользователю,
и только после подтверждения он применяется. Перед самим применением
автоматически создаётся emergency-бэкап текущего состояния — на случай,
если результат восстановления окажется не тем, что ожидалось."""

import discord
from discord.ext import commands

from utils import backup_core
from utils.backup_core.models import RestoreScope
from utils.embeds import build_restore_plan_embed
from utils.moderation_utils import is_mod_or_admin

VALID_SCOPES = ("all", "roles", "channels", "categories", "permissions")


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="backup")
    @is_mod_or_admin()
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def backup_cmd(self, ctx: commands.Context, action: str = "save", scope: str = "all", mode: str = "safe"):
        """L.backup save | L.backup info | L.backup restore [all|roles|channels|categories|permissions] [safe|strict]

        scope — какую часть бэкапа восстанавливать (по умолчанию всё).
        mode — 'safe' (по умолчанию) ничего не удаляет, только создаёт и обновляет;
        'strict' дополнительно удаляет с сервера то, чего нет в бэкапе (старое поведение).

        Кулдаун 30 сек на сервер — save/restore создают десятки последовательных
        запросов к Discord API, повторный запуск раньше срока только мешает."""
        action = action.lower()
        if action == "save":
            await self._save(ctx)
        elif action == "info":
            await self._info(ctx)
        elif action == "restore":
            await self._restore(ctx, scope.lower(), mode.lower())
        else:
            await ctx.send("Использование: `L.backup save|restore|info`")

    async def _save(self, ctx: commands.Context):
        counts = await backup_core.save_backup(ctx.guild)
        await ctx.send(
            f"💾 Бэкап сохранён (`{counts.get('backup_id', '?')}`): "
            f"{counts['roles']} ролей, {counts.get('categories', 0)} категорий, "
            f"{counts['channels']} каналов, {counts.get('emojis', 0)} эмодзи, {counts.get('stickers', 0)} стикеров."
        )

    async def _info(self, ctx: commands.Context):
        info = backup_core.get_backup_info(ctx.guild.id)
        if not info:
            return await ctx.send("Бэкап для этого сервера не найден.")
        backups = backup_core.list_backups(ctx.guild.id)
        await ctx.send(
            f"📦 Последний бэкап сервера «{info['guild_name']}» (`{info['backup_id']}`, {info['created_at']}):\n"
            f"{info['roles']} ролей, {info['channels']} каналов. Всего сохранённых бэкапов: {len(backups)}."
        )

    async def _restore(self, ctx: commands.Context, scope_keyword: str, mode: str):
        if not backup_core.has_backup(ctx.guild.id):
            return await ctx.send("⚠️ Бэкап не найден. Сначала выполните `L.backup save`.")
        if scope_keyword not in VALID_SCOPES:
            return await ctx.send(f"⚠️ Неизвестная область восстановления. Доступно: {', '.join(VALID_SCOPES)}.")
        if mode not in ("safe", "strict"):
            return await ctx.send("⚠️ Режим должен быть `safe` или `strict`.")

        scope = RestoreScope.from_keyword(scope_keyword)
        remove_extra = mode == "strict"

        try:
            plan = backup_core.build_plan(ctx.guild, scope=scope, remove_extra=remove_extra)
        except FileNotFoundError:
            return await ctx.send("⚠️ Бэкап не найден.")

        await ctx.send(embed=build_restore_plan_embed(plan, ctx.guild.name))
        if plan.is_empty:
            return await ctx.send("Восстанавливать ничего не нужно.")

        await ctx.send(
            "⚠️ Перед восстановлением будет автоматически создан резервный бэкап текущего состояния.\n"
            "Напишите `да` в течение 30 секунд для подтверждения."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ("да", "yes")

        try:
            await self.bot.wait_for("message", check=check, timeout=30)
        except Exception:
            return await ctx.send("⏱️ Время ожидания истекло. Восстановление отменено.")

        status = await ctx.send("⏳ Создаётся резервный бэкап и применяется восстановление...")
        try:
            _plan, result, emergency_id = await backup_core.restore_with_safety(
                ctx.guild, scope=scope, remove_extra=remove_extra
            )
            content = (
                f"✅ Восстановление завершено: создано {result.total_created()}, "
                f"обновлено {result.total_updated()}, удалено {result.total_removed()}.\n"
                f"Пропущено конфликтов: {result.skipped_conflicts}."
            )
            if result.errors:
                content += f"\n⚠️ Ошибок: {len(result.errors)} (первая: {result.errors[0]})"
            if emergency_id:
                content += f"\n🛟 Резервный бэкап на случай отката: `{emergency_id}`."
        except discord.HTTPException as e:
            content = f"⚠️ Восстановление завершилось с ошибкой Discord API: {e}"

        # Канал, где была вызвана команда, мог быть удалён в процессе восстановления —
        # пробуем отредактировать исходное сообщение, а если не вышло — шлём в любой доступный канал/ЛС.
        async def try_edit(text):
            await status.edit(content=text)

        await backup_core.notify_guild_or_dm(ctx.guild, ctx.author, content, preferred=try_edit)


async def setup(bot):
    await bot.add_cog(Backup(bot))
