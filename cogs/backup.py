"""Префиксные команды L.backup — обёртка над utils/backup_core.

L.backup restore не удаляет всё подряд: сначала строится план (что создастся/
обновится/удалится/какие конфликты) и показывается пользователю, и только после
подтверждения он применяется. Перед самим применением автоматически создаётся
emergency-бэкап текущего состояния — на случай, если результат восстановления
окажется не тем, что ожидалось. Этим же emergency-бэкапом пользуется
`L.backup rollback`, если нужно откатиться обратно."""

import discord
from discord.ext import commands

from utils import backup_core
from utils.backup_core.models import RestoreScope
from utils.embeds import build_backup_action_log_embed, build_restore_plan_embed, format_missing_permissions_warning
from utils.moderation_utils import is_mod_or_admin, send_mod_log

VALID_SCOPES = ("all", "roles", "channels", "categories", "permissions")
DUPLICATE_KIND_LABELS = {"role": "Роль", "category": "Категория", "channel": "Канал"}


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="backup")
    @is_mod_or_admin()
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def backup_cmd(self, ctx: commands.Context, action: str = "save", *args: str):
        """L.backup save | info | list | duplicates
        L.backup restore [all|roles|channels|categories|permissions] [safe|strict]
        L.backup rollback [id_бэкапа]

        restore: scope — какую часть бэкапа восстанавливать (по умолчанию всё);
        mode — 'safe' (по умолчанию) ничего не удаляет, только создаёт и обновляет,
        'strict' дополнительно удаляет с сервера то, чего нет в бэкапе.

        rollback без аргумента откатывается к последнему авто-бэкапу,
        созданному перед прошлым restore (если он есть).

        Кулдаун 30 сек на сервер — save/restore создают десятки последовательных
        запросов к Discord API, повторный запуск раньше срока только мешает."""
        action = action.lower()
        if action == "save":
            await self._save(ctx)
        elif action == "info":
            await self._info(ctx)
        elif action == "list":
            await self._list(ctx)
        elif action == "duplicates":
            await self._duplicates(ctx)
        elif action == "restore":
            scope_kw = args[0].lower() if args else "all"
            mode = args[1].lower() if len(args) > 1 else "safe"
            await self._restore(ctx, scope_kw, mode)
        elif action == "rollback":
            backup_id = args[0] if args else None
            await self._rollback(ctx, backup_id)
        elif action == "diff":
            if len(args) < 2:
                return await ctx.send("Использование: `L.backup diff <id_бэкапа_1> <id_бэкапа_2>`")
            await self._diff(ctx, args[0], args[1])
        elif action == "resume":
            cp_id = args[0] if args else None
            await self._resume(ctx, cp_id)
        else:
            await ctx.send("Использование: `L.backup save|restore|rollback|info|list|duplicates|diff|resume`")

    async def _save(self, ctx: commands.Context):
        counts = await backup_core.save_backup(ctx.guild)
        await ctx.send(
            f"💾 Бэкап сохранён (`{counts.get('backup_id', '?')}`): "
            f"{counts['roles']} ролей, {counts.get('categories', 0)} категорий, "
            f"{counts['channels']} каналов, {counts.get('emojis', 0)} эмодзи, {counts.get('stickers', 0)} стикеров, "
            f"{counts.get('automod_rules', 0)} правил AutoMod, {counts.get('webhooks', 0)} вебхуков."
        )
        await send_mod_log(
            self.bot,
            ctx.guild,
            build_backup_action_log_embed(
                "save", ctx.author, f"Создан бэкап `{counts.get('backup_id', '?')}` ({counts['roles']} ролей, {counts['channels']} каналов)."
            ),
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

    async def _list(self, ctx: commands.Context):
        ids = list(reversed(backup_core.list_backups(ctx.guild.id)))
        if not ids:
            return await ctx.send("Бэкапов для этого сервера пока нет.")
        lines = []
        for backup_id in ids[:25]:
            info = backup_core.get_backup_info(ctx.guild.id, backup_id)
            if info is None:
                continue
            mark = " 🛟 (авто перед restore)" if info.get("is_emergency") else ""
            lines.append(f"`{backup_id}` — {info['created_at']}{mark}: {info['roles']} ролей, {info['channels']} каналов")
        text = "\n".join(lines)
        if len(ids) > 25:
            text += f"\n… показаны последние 25 из {len(ids)}."
        await ctx.send(f"📦 Бэкапы сервера «{ctx.guild.name}»:\n{text}")

    async def _duplicates(self, ctx: commands.Context):
        groups = backup_core.find_duplicates(ctx.guild)
        if not groups:
            return await ctx.send("✅ Дубликатов по именам не найдено.")
        lines = [
            f"**{DUPLICATE_KIND_LABELS.get(g.kind, g.kind)}** «{g.name}» — {len(g.ids)} шт."
            for g in groups[:25]
        ]
        text = "\n".join(lines)
        if len(groups) > 25:
            text += f"\n… показаны первые 25 из {len(groups)} групп."
        await ctx.send(
            "⚠️ Найдены дубликаты имён — такие элементы будут помечены конфликтом и пропущены при restore "
            f"(сопоставление идёт по имени, не по ID):\n{text}"
        )

    async def _resume(self, ctx: commands.Context, checkpoint_id: str | None):
        checkpoints = backup_core.list_checkpoints(ctx.guild.id)
        if not checkpoints:
            return await ctx.send("✅ Незавершённых восстановлений нет.")

        cp_id = checkpoint_id
        if cp_id is None:
            if len(checkpoints) == 1:
                cp_id = checkpoints[0]["checkpoint_id"]
            else:
                lines = [f"`{c['checkpoint_id']}` — прогресс {c['progress']}, бэкап `{c['backup_id']}`" for c in checkpoints]
                return await ctx.send("Найдено несколько чекпоинтов — укажите ID:\n" + "\n".join(lines))

        status = await ctx.send(f"⏳ Возобновляю восстановление с чекпоинта `{cp_id}`...")
        try:
            progress_cb = backup_core.make_throttled_progress_callback(lambda text: status.edit(content=text))
            _plan, result, emergency_id = await backup_core.resume_from_checkpoint(
                ctx.guild, cp_id, progress_cb=progress_cb
            )
            content = (
                f"✅ Восстановление завершено (resume): создано {result.total_created()}, "
                f"обновлено {result.total_updated()}, удалено {result.total_removed()}.\n"
                f"Пропущено конфликтов: {result.skipped_conflicts}."
            )
            if result.errors:
                content += f"\n⚠️ Ошибок: {len(result.errors)} (первая: {result.errors[0]})"
            if emergency_id:
                content += f"\n🛟 Резервный бэкап для отката: `{emergency_id}`."
            await send_mod_log(
                self.bot, ctx.guild,
                build_backup_action_log_embed("load", ctx.author, f"Возобновлено с чекпоинта `{cp_id}`. {content}"),
            )
        except FileNotFoundError:
            content = f"⚠️ Чекпоинт `{cp_id}` не найден."
        except discord.HTTPException as e:
            content = f"⚠️ Ошибка Discord API при возобновлении: {e}"

        async def try_edit(text):
            await status.edit(content=text)

        await backup_core.notify_guild_or_dm(ctx.guild, ctx.author, content, preferred=try_edit)

    async def _diff(self, ctx: commands.Context, id1: str, id2: str):
        try:
            plan = backup_core.diff_backups(ctx.guild.id, id1, id2)
        except FileNotFoundError:
            return await ctx.send("⚠️ Один из указанных бэкапов не найден.")

        embed = build_restore_plan_embed(plan, ctx.guild.name)
        embed.title = "📋 Сравнение бэкапов"
        embed.description = f"`{id1}` → `{id2}`"
        if plan.is_empty:
            embed.description += "\n\n✅ Между этими бэкапами нет различий."
        await ctx.send(embed=embed)

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
            plan = await backup_core.build_plan(ctx.guild, scope=scope, remove_extra=remove_extra)
        except FileNotFoundError:
            return await ctx.send("⚠️ Бэкап не найден.")

        await self._confirm_and_apply(ctx, plan, scope, remove_extra, backup_id=None)

    async def _rollback(self, ctx: commands.Context, backup_id: str | None):
        target_id = backup_id or backup_core.latest_emergency_backup_id(ctx.guild.id)
        if target_id is None:
            return await ctx.send(
                "⚠️ Не найдено ни одного авто-бэкапа перед restore — откатываться не к чему. "
                "Используйте `L.backup restore`, указав конкретный ID бэкапа, если такой есть (`L.backup list`)."
            )

        scope = RestoreScope.all()
        try:
            # remove_extra=True — смысл rollback в полном возврате к прежнему состоянию.
            plan = await backup_core.build_plan(ctx.guild, target_id, scope=scope, remove_extra=True)
        except FileNotFoundError:
            return await ctx.send("⚠️ Указанный бэкап не найден.")

        await self._confirm_and_apply(ctx, plan, scope, True, backup_id=target_id, is_rollback=True)

    async def _confirm_and_apply(
        self,
        ctx: commands.Context,
        plan,
        scope: RestoreScope,
        remove_extra: bool,
        *,
        backup_id: str | None,
        is_rollback: bool = False,
    ):
        await ctx.send(embed=build_restore_plan_embed(plan, ctx.guild.name))
        if plan.is_empty:
            return await ctx.send("Восстанавливать ничего не нужно.")

        missing_perms = backup_core.missing_permissions_for_scope(ctx.guild, scope)
        warning = format_missing_permissions_warning(missing_perms)
        if warning:
            await ctx.send(warning)

        verb = "откат" if is_rollback else "восстановление"
        await ctx.send(
            f"⚠️ Перед {verb}ом будет автоматически создан резервный бэкап текущего состояния.\n"
            "Напишите `да` в течение 30 секунд для подтверждения."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel and m.content.lower() in ("да", "yes")

        try:
            await self.bot.wait_for("message", check=check, timeout=30)
        except Exception:
            return await ctx.send(f"⏱️ Время ожидания истекло. {verb.capitalize()} отменён.")

        status = await ctx.send(f"⏳ Создаётся резервный бэкап и применяется {verb}...")
        try:
            progress_cb = backup_core.make_throttled_progress_callback(lambda text: status.edit(content=text))
            _plan, result, emergency_id = await backup_core.restore_with_safety(
                ctx.guild, backup_id=backup_id, scope=scope, remove_extra=remove_extra, progress_cb=progress_cb
            )
            content = (
                f"✅ {verb.capitalize()} завершён: создано {result.total_created()}, "
                f"обновлено {result.total_updated()}, удалено {result.total_removed()}.\n"
                f"Пропущено конфликтов: {result.skipped_conflicts}."
            )
            if result.errors:
                content += f"\n⚠️ Ошибок: {len(result.errors)} (первая: {result.errors[0]})"
            if emergency_id:
                content += f"\n🛟 Резервный бэкап на случай отката: `{emergency_id}`."

            log_description = (
                f"Бэкап: `{backup_id or 'последний'}` · Область: `{scope}` · "
                f"Удаление лишнего: {'да' if remove_extra else 'нет'}\n"
                f"Создано: {result.total_created()}, обновлено: {result.total_updated()}, "
                f"удалено: {result.total_removed()}, конфликтов: {result.skipped_conflicts}, "
                f"ошибок: {len(result.errors)}."
            )
            await send_mod_log(
                self.bot,
                ctx.guild,
                build_backup_action_log_embed("rollback" if is_rollback else "load", ctx.author, log_description),
            )
        except discord.HTTPException as e:
            content = f"⚠️ {verb.capitalize()} завершился с ошибкой Discord API: {e}"

        # Канал, где была вызвана команда, мог быть удалён в процессе восстановления —
        # пробуем отредактировать исходное сообщение, а если не вышло — шлём в любой доступный канал/ЛС.
        async def try_edit(text):
            await status.edit(content=text)

        await backup_core.notify_guild_or_dm(ctx.guild, ctx.author, content, preferred=try_edit)


async def setup(bot):
    await bot.add_cog(Backup(bot))
