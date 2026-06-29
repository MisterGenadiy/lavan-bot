"""L.settings, L.prefix, L.setlogchannel и связанные настройки."""

import discord
from discord.ext import commands

from utils.embeds import build_settings_embed
from utils.moderation_utils import is_mod_or_admin


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="settings", aliases=["настройки"])
    @is_mod_or_admin()
    async def settings_cmd(self, ctx: commands.Context):
        """Показывает текущие настройки бота на сервере."""
        await ctx.send(embed=build_settings_embed(self.bot, ctx.guild))

    @commands.command(name="prefix")
    @is_mod_or_admin()
    async def prefix_cmd(self, ctx: commands.Context, new_prefix: str):
        """Меняет префикс команд бота на этом сервере."""
        if len(new_prefix) > 5:
            return await ctx.send("⚠️ Префикс не может быть длиннее 5 символов.")
        self.bot.db.set_setting(ctx.guild.id, "prefix", new_prefix)
        await ctx.send(f"✅ Новый префикс: `{new_prefix}`")

    @commands.command(name="setlogchannel", aliases=["логканал"])
    @is_mod_or_admin()
    async def set_log_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Назначает канал для логов модерации/безопасности."""
        self.bot.db.set_setting(ctx.guild.id, "log_channel_id", channel.id)
        await ctx.send(f"✅ Канал логов установлен: {channel.mention}")

    @commands.command(name="antispam")
    @is_mod_or_admin()
    async def antispam_cmd(
        self,
        ctx: commands.Context,
        enabled: str,
        msg_limit: int = 6,
        interval: int = 6,
        action: str = "mute",
    ):
        """L.antispam on/off [лимит_сообщений] [интервал_сек] [warn|mute|kick|ban]"""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        if action.lower() not in ("warn", "mute", "kick", "ban"):
            return await ctx.send("⚠️ Действие должно быть одним из: warn, mute, kick, ban")

        self.bot.db.set_setting(ctx.guild.id, "antispam_enabled", on)
        self.bot.db.set_setting(ctx.guild.id, "antispam_msg_limit", msg_limit)
        self.bot.db.set_setting(ctx.guild.id, "antispam_interval", interval)
        self.bot.db.set_setting(ctx.guild.id, "antispam_action", action.lower())
        await ctx.send(
            f"✅ Антиспам: {'включён' if on else 'выключен'} — "
            f"{msg_limit} сообщ. / {interval} сек → `{action.lower()}`"
        )

    @commands.command(name="antiraid")
    @is_mod_or_admin()
    async def antiraid_cmd(
        self,
        ctx: commands.Context,
        enabled: str,
        join_limit: int = 8,
        interval: int = 10,
        action: str = "lockdown",
    ):
        """L.antiraid on/off [лимит_вступлений] [интервал_сек] [lockdown|kick|ban]"""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        if action.lower() not in ("lockdown", "kick", "ban"):
            return await ctx.send("⚠️ Действие должно быть одним из: lockdown, kick, ban")

        self.bot.db.set_setting(ctx.guild.id, "antiraid_enabled", on)
        self.bot.db.set_setting(ctx.guild.id, "antiraid_join_limit", join_limit)
        self.bot.db.set_setting(ctx.guild.id, "antiraid_interval", interval)
        self.bot.db.set_setting(ctx.guild.id, "antiraid_action", action.lower())
        await ctx.send(
            f"✅ Антирейд: {'включён' if on else 'выключен'} — "
            f"{join_limit} вступлений / {interval} сек → `{action.lower()}`"
        )

    @commands.command(name="mod-log-channel", aliases=["modlog"])
    @is_mod_or_admin()
    async def mod_log_channel_cmd(self, ctx: commands.Context, channel: discord.TextChannel):
        """Назначает отдельный канал для логов модераторских действий (kick/ban/mute/warn)."""
        self.bot.db.set_setting(ctx.guild.id, "mod_log_channel_id", channel.id)
        await ctx.send(f"✅ Канал мод-логов установлен: {channel.mention}")

    @commands.command(name="antilink")
    @is_mod_or_admin()
    async def antilink_cmd(
        self, ctx: commands.Context, enabled: str, action: str = "warn", duration_seconds: int = None
    ):
        """L.antilink on/off [warn|mute|kick|ban|delete] [длительность_мута_сек]"""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        if action.lower() not in ("warn", "mute", "kick", "ban", "delete"):
            return await ctx.send("⚠️ Действие должно быть одним из: warn, mute, kick, ban, delete")
        self.bot.db.set_setting(ctx.guild.id, "antilink_enabled", on)
        self.bot.db.set_setting(ctx.guild.id, "antilink_action", action.lower())
        if duration_seconds is not None:
            self.bot.db.set_setting(ctx.guild.id, "antilink_mute_seconds", duration_seconds)
        await ctx.send(f"✅ Антилинк: {'включён' if on else 'выключен'} → `{action.lower()}`")

    @commands.command(name="ban-new-users")
    @is_mod_or_admin()
    async def ban_new_users_cmd(
        self, ctx: commands.Context, enabled: str, min_age_hours: int = 24, *, description: str = "Аккаунт слишком новый"
    ):
        """L.ban-new-users on/off [мин_возраст_часов] [описание]"""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        self.bot.db.set_setting(ctx.guild.id, "ban_new_users_enabled", on)
        self.bot.db.set_setting(ctx.guild.id, "ban_new_users_min_age_hours", min_age_hours)
        self.bot.db.set_setting(ctx.guild.id, "ban_new_users_description", description)
        await ctx.send(
            f"✅ Автобан новых аккаунтов: {'включён' if on else 'выключен'} "
            f"(мин. возраст {min_age_hours} ч.)"
        )

    @commands.command(name="isolate-new-bots")
    @is_mod_or_admin()
    async def isolate_new_bots_cmd(self, ctx: commands.Context, enabled: str):
        """L.isolate-new-bots on/off — снимать роли у новых ботов при добавлении."""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        self.bot.db.set_setting(ctx.guild.id, "isolate_new_bots_enabled", on)
        await ctx.send(f"✅ Изоляция новых ботов: {'включена' if on else 'выключена'}")

    @commands.command(name="ignore-spam")
    @is_mod_or_admin()
    async def ignore_spam_cmd(self, ctx: commands.Context, action: str, channel: discord.TextChannel = None):
        """L.ignore-spam add|remove|reset [#канал]"""
        action = action.lower()
        ids = self.bot.db.get_setting(ctx.guild.id, "ignore_spam_channels", [])
        if action == "add" and channel:
            if channel.id not in ids:
                ids.append(channel.id)
            self.bot.db.set_setting(ctx.guild.id, "ignore_spam_channels", ids)
            await ctx.send(f"✅ Канал {channel.mention} добавлен в игнор антиспама/антилинка.")
        elif action == "remove" and channel:
            if channel.id in ids:
                ids.remove(channel.id)
            self.bot.db.set_setting(ctx.guild.id, "ignore_spam_channels", ids)
            await ctx.send(f"✅ Канал {channel.mention} убран из игнора.")
        elif action == "reset":
            self.bot.db.set_setting(ctx.guild.id, "ignore_spam_channels", [])
            await ctx.send("✅ Список каналов игнорирования сброшен.")
        else:
            await ctx.send("Использование: `L.ignore-spam add|remove #канал` или `L.ignore-spam reset`")

    @commands.command(name="antimention")
    @is_mod_or_admin()
    async def antimention_cmd(
        self, ctx: commands.Context, enabled: str, limit: int = 5, action: str = "mute", mute_seconds: int = 600
    ):
        """L.antimention on/off [лимит_упоминаний] [warn|mute|kick|ban] [мут_сек]"""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        if action.lower() not in ("warn", "mute", "kick", "ban"):
            return await ctx.send("⚠️ Действие должно быть одним из: warn, mute, kick, ban")
        self.bot.db.set_setting(ctx.guild.id, "antimention_enabled", on)
        self.bot.db.set_setting(ctx.guild.id, "antimention_limit", max(1, limit))
        self.bot.db.set_setting(ctx.guild.id, "antimention_action", action.lower())
        self.bot.db.set_setting(ctx.guild.id, "antimention_mute_seconds", mute_seconds)
        await ctx.send(
            f"✅ Анти-mention-спам: {'включён' if on else 'выключен'} — "
            f"от {limit} упоминаний в одном сообщении → `{action.lower()}`"
        )

    @commands.command(name="verification")
    @is_mod_or_admin()
    async def verification_cmd(
        self,
        ctx: commands.Context,
        enabled: str,
        unverified_role: discord.Role = None,
        verified_role: discord.Role = None,
        timeout_minutes: int = 0,
    ):
        """L.verification on/off [@роль_до_подтверждения] [@роль_после] [таймаут_минут]

        Роль "до подтверждения" должна ограничивать доступ к каналам через
        настройки прав каналов — бот только назначает/снимает её."""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        self.bot.db.set_setting(ctx.guild.id, "verification_enabled", on)
        if unverified_role:
            self.bot.db.set_setting(ctx.guild.id, "verification_unverified_role_id", unverified_role.id)
        if verified_role:
            self.bot.db.set_setting(ctx.guild.id, "verification_verified_role_id", verified_role.id)
        if timeout_minutes:
            self.bot.db.set_setting(ctx.guild.id, "verification_timeout_minutes", timeout_minutes)
        await ctx.send(
            f"✅ Верификация: {'включена' if on else 'выключена'}.\n"
            f"Не забудьте: `{ctx.prefix}verification-channel #канал` и `{ctx.prefix}verification-post`, "
            "чтобы отправить кнопку подтверждения."
        )

    @commands.command(name="verification-channel")
    @is_mod_or_admin()
    async def verification_channel_cmd(self, ctx: commands.Context, channel: discord.TextChannel):
        """Назначает канал, куда отправляется кнопка верификации."""
        self.bot.db.set_setting(ctx.guild.id, "verification_channel_id", channel.id)
        await ctx.send(f"✅ Канал верификации установлен: {channel.mention}")

    @commands.command(name="add-warn-action", aliases=["awa"])
    @is_mod_or_admin()
    async def add_warn_action(
        self, ctx: commands.Context, warn_count: int, action: str, duration_seconds: int = None
    ):
        """L.add-warn-action <кол-во_варнов> <warn|mute|kick|ban> [длительность_сек]"""
        if action.lower() not in ("warn", "mute", "kick", "ban"):
            return await ctx.send("⚠️ Действие должно быть одним из: warn, mute, kick, ban")
        self.bot.db.set_warn_action(ctx.guild.id, warn_count, action.lower(), duration_seconds)
        await ctx.send(
            f"✅ За {warn_count} варн(а/ов) теперь будет применяться: `{action.lower()}`"
            + (f" на {duration_seconds} сек" if duration_seconds else "")
        )

    @commands.command(name="remove-warn-action", aliases=["rwa"])
    @is_mod_or_admin()
    async def remove_warn_action(self, ctx: commands.Context, warn_count: int):
        """Удаляет авто-наказание за указанное количество варнов."""
        removed = self.bot.db.remove_warn_action(ctx.guild.id, warn_count)
        await ctx.send("✅ Удалено." if removed else "⚠️ Такого правила не найдено.")

    @commands.command(name="auditwatch")
    @is_mod_or_admin()
    async def auditwatch_cmd(self, ctx: commands.Context, enabled: str, channel: discord.TextChannel = None):
        """L.auditwatch on/off [#канал] — дублировать ВЕСЬ аудит-лог сервера
        (создание/удаление/изменение каналов, ролей, прав, баны, настройки
        сервера и т.п.) в указанный канал, независимо от лимитов антирейда/анти-краша."""
        on = enabled.lower() in ("on", "вкл", "true", "1")
        if on and channel is None and not self.bot.db.get_setting(ctx.guild.id, "auditwatch_channel_id"):
            return await ctx.send("⚠️ Укажите канал: `L.auditwatch on #канал`.")
        self.bot.db.set_setting(ctx.guild.id, "auditwatch_enabled", on)
        if channel is not None:
            self.bot.db.set_setting(ctx.guild.id, "auditwatch_channel_id", channel.id)
        target = channel.mention if channel else "ранее указанный канал"
        await ctx.send(f"✅ Аудит-лог вотчер: {'включён' if on else 'выключен'}" + (f" → {target}" if on else "."))

    @commands.command(name="warn-actions", aliases=["wal"])
    @is_mod_or_admin()
    async def list_warn_actions(self, ctx: commands.Context):
        """Показывает список всех авто-наказаний за варны."""
        rows = self.bot.db.get_all_warn_actions(ctx.guild.id)
        if not rows:
            return await ctx.send("Список авто-наказаний пуст.")
        lines = [
            f"**{r['warn_count']}** варн(а/ов) → `{r['action']}`"
            + (f" ({r['duration_seconds']} сек)" if r["duration_seconds"] else "")
            for r in rows
        ]
        await ctx.send("\n".join(lines))


async def setup(bot):
    await bot.add_cog(Settings(bot))
