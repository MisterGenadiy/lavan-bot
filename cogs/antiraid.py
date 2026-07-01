"""Анти-рейд: следит за всплесками вступлений новых пользователей.
Анти-краш: следит за журналом аудита на массовое удаление каналов/ролей/
вебхуков (типичный признак "нюка" сервера) и блокирует виновника."""

import asyncio
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from utils.moderation_utils import send_log

# Записи об активности старше этого порога точно не нужны ни для подсчёта
# всплесков (антирейд/антикраш работают в окне интервалов в секундах-минутах),
# ни для антидубликата — используются только периодической чисткой ниже,
# чтобы join_log/delete_log не росли бесконечно на долгоживущем процессе.
_STALE_ACTIVITY_SECONDS = 3600


class AntiRaid(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.join_log: dict[int, deque] = defaultdict(lambda: deque(maxlen=100))
        # счётчики удалений по (guild_id, actor_id)
        self.delete_log: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))
        self.lockdown_active: set[int] = set()
        self._cleanup_stale_activity.start()

    def cog_unload(self):
        self._cleanup_stale_activity.cancel()

    @tasks.loop(minutes=15)
    async def _cleanup_stale_activity(self):
        """Убирает записи неактивных серверов/пользователей из join_log и
        delete_log — без этого словари растут бесконечно, т.к. ключи
        никогда не удалялись сами по себе (только добавлялись при событиях)."""
        cutoff = time.time() - _STALE_ACTIVITY_SECONDS
        for guild_id in [gid for gid, log in self.join_log.items() if not log or log[-1] < cutoff]:
            del self.join_log[guild_id]
        for key in [k for k, log in self.delete_log.items() if not log or log[-1] < cutoff]:
            del self.delete_log[key]

    @_cleanup_stale_activity.before_loop
    async def _before_cleanup(self):
        await self.bot.wait_until_ready()

    # ---------------- Анти-рейд (массовые вступления) ----------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = self.bot.db.get_all_settings(member.guild.id)

        # Изоляция новых ботов: снимаем все назначенные роли/права при добавлении
        if member.bot and settings.get("isolate_new_bots_enabled"):
            try:
                await member.edit(roles=[], reason="Изоляция нового бота")
                await send_log(
                    self.bot,
                    member.guild,
                    discord.Embed(
                        description=f"🤖 Бот {member.mention} добавлен — роли временно сняты для изоляции.",
                        color=discord.Color.orange(),
                    ),
                )
            except discord.Forbidden:
                pass

        # Бан новых аккаунтов (независимо от рейд-детектора, как /ban-new-users в оригинале)
        if not member.bot and settings.get("ban_new_users_enabled"):
            min_age_hours = settings.get("ban_new_users_min_age_hours", 24)
            account_age_hours = (discord.utils.utcnow() - member.created_at).total_seconds() / 3600
            if account_age_hours < min_age_hours:
                reason = settings.get("ban_new_users_description") or "Аккаунт слишком новый"
                try:
                    await member.ban(reason=f"Автобан новых аккаунтов: {reason}", delete_message_seconds=0)
                    await send_log(
                        self.bot,
                        member.guild,
                        discord.Embed(
                            description=f"🚫 {member} автоматически забанен: {reason} "
                            f"(возраст аккаунта {account_age_hours:.1f} ч.)",
                            color=discord.Color.dark_red(),
                        ),
                    )
                except discord.Forbidden:
                    pass
                return  # забаненного дальше по антирейду не обрабатываем

        if not settings["antiraid_enabled"]:
            return

        now = time.time()
        log = self.join_log[member.guild.id]
        log.append(now)
        interval = settings["antiraid_interval"]
        recent = [t for t in log if now - t <= interval]
        self.join_log[member.guild.id] = deque(recent, maxlen=100)

        if len(recent) < settings["antiraid_join_limit"]:
            return
        if member.guild.id in self.lockdown_active:
            return

        await self._trigger_raid_response(member.guild, settings)

    async def _trigger_raid_response(self, guild: discord.Guild, settings: dict):
        action = settings["antiraid_action"]
        embed = discord.Embed(
            title="🚨 Обнаружена рейд-атака!",
            description=f"Зафиксирован всплеск вступлений. Применяется действие: `{action}`",
            color=discord.Color.dark_red(),
        )
        await send_log(self.bot, guild, embed)

        if action == "lockdown":
            self.lockdown_active.add(guild.id)
            everyone = guild.default_role
            locked = 0
            for channel in guild.text_channels:
                try:
                    overwrite = channel.overwrites_for(everyone)
                    overwrite.send_messages = False
                    await channel.set_permissions(everyone, overwrite=overwrite, reason="Анти-рейд: lockdown")
                    locked += 1
                except discord.Forbidden:
                    continue
            await send_log(
                self.bot,
                guild,
                discord.Embed(
                    description=f"🔒 Сервер заблокирован (lockdown). Каналов закрыто: {locked}.\n"
                    f"Используйте `{settings['prefix']}unlock`, чтобы снять блокировку.",
                    color=discord.Color.dark_red(),
                ),
            )
        elif action in ("kick", "ban"):
            cutoff = time.time() - settings["antiraid_interval"]
            recent_members = [
                m for m in guild.members
                if m.joined_at and m.joined_at.timestamp() >= cutoff and not m.bot
            ]
            for i, m in enumerate(recent_members):
                try:
                    if action == "kick":
                        await m.kick(reason="Анти-рейд: массовое вступление")
                    else:
                        await m.ban(reason="Анти-рейд: массовое вступление", delete_message_seconds=0)
                except discord.Forbidden:
                    continue
                # Небольшая пауза каждые 5 действий — на реальном крупном рейде
                # (десятки-сотни вступлений разом) без неё легко упереться
                # в rate limit Discord и часть кик/банов просто не применится.
                if i % 5 == 4:
                    await asyncio.sleep(1)

    @commands.command(name="unlock")
    async def unlock(self, ctx: commands.Context):
        """Снимает lockdown с сервера после ложного срабатывания антирейда."""
        if not (ctx.author.guild_permissions.manage_guild or ctx.author.guild_permissions.administrator):
            return await ctx.send("⛔ Недостаточно прав.")
        everyone = ctx.guild.default_role
        unlocked = 0
        for channel in ctx.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(everyone)
                overwrite.send_messages = None
                await channel.set_permissions(everyone, overwrite=overwrite, reason="Снятие lockdown")
                unlocked += 1
            except discord.Forbidden:
                continue
        self.lockdown_active.discard(ctx.guild.id)
        await ctx.send(f"🔓 Lockdown снят. Каналов разблокировано: {unlocked}.")

    # ---------------- Анти-краш (защита от "нюка" сервера) ----------------

    async def _get_actor_from_audit(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int
    ) -> discord.Member | discord.User | None:
        try:
            # Таймаут 5 сек: если Discord отвечает медленно или недоступен,
            # не хотим блокировать event loop на неопределённое время — лучше
            # просто не найти актора, чем зависнуть в ожидании.
            async with asyncio.timeout(5.0):
                async for entry in guild.audit_logs(limit=5, action=action):
                    if entry.target and entry.target.id == target_id:
                        return entry.user
        except (discord.Forbidden, asyncio.TimeoutError):
            return None
        return None

    async def _register_deletion(self, guild: discord.Guild, actor: discord.Member | discord.User | None):
        # ВАЖНО: entry.user из audit_logs() не гарантированно Member — если виновник
        # не в кэше участников (например, уже вышел с сервера сразу после "нюка"),
        # discord.py возвращает обычный discord.User. У User нет .ban() и нет
        # .edit(roles=...), поэтому ниже нельзя полагаться на то, что actor — Member.
        if actor is None or actor.bot:
            return
        settings = self.bot.db.get_all_settings(guild.id)
        if not settings["anticrash_enabled"]:
            return
        # Владельца и саму себя не наказываем
        if actor.id == guild.owner_id:
            return

        key = (guild.id, actor.id)
        now = time.time()
        log = self.delete_log[key]
        log.append(now)
        interval = settings["anticrash_interval"]
        recent = [t for t in log if now - t <= interval]
        self.delete_log[key] = deque(recent, maxlen=50)

        if len(recent) < settings["anticrash_threshold"]:
            return

        # Виновник найден — снимаем опасные права и/или банним.
        # guild.ban() (а не actor.ban()) принимает любой discord.abc.Snowflake,
        # поэтому работает одинаково и для Member, и для "оторвавшегося" User.
        try:
            await guild.ban(
                actor,
                reason="Анти-краш: массовое удаление каналов/ролей (подозрение на нюк)",
                delete_message_seconds=0,
            )
            result = "забанен"
        except discord.Forbidden:
            # Снять роли можно только если это реально Member на сервере —
            # пробуем получить его из кэша, если actor пришёл как plain User.
            member = actor if isinstance(actor, discord.Member) else guild.get_member(actor.id)
            if member is not None:
                try:
                    await member.edit(roles=[], reason="Анти-краш: подозрение на нюк сервера")
                    result = "роли сняты (бан невозможен из-за прав)"
                except discord.Forbidden:
                    result = "не удалось наказать (недостаточно прав бота)"
            else:
                result = "не удалось наказать (пользователь не на сервере, а бан невозможен из-за прав)"

        await send_log(
            self.bot,
            guild,
            discord.Embed(
                title="🛑 Анти-краш сработал",
                description=f"Пользователь {actor.mention} массово удалял каналы/роли.\nРезультат: {result}",
                color=discord.Color.dark_red(),
            ),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        actor = await self._get_actor_from_audit(
            channel.guild, discord.AuditLogAction.channel_delete, channel.id
        )
        await self._register_deletion(channel.guild, actor)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        actor = await self._get_actor_from_audit(role.guild, discord.AuditLogAction.role_delete, role.id)
        await self._register_deletion(role.guild, actor)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        # Массовое создание вебхуков часто используется для спам-рейдов
        try:
            async with asyncio.timeout(5.0):
                async for entry in channel.guild.audit_logs(limit=3, action=discord.AuditLogAction.webhook_create):
                    await self._register_deletion(channel.guild, entry.user)
                    break
        except (discord.Forbidden, asyncio.TimeoutError):
            pass


async def setup(bot):
    await bot.add_cog(AntiRaid(bot))
