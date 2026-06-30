"""Слэш-команды (/), дублирующие набор команд оригинального Lavan.
Логика максимально переиспользует utils/database.py, utils/moderation_utils.py
и utils/backup_core.py, чтобы не дублировать код с префиксными командами."""

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils import backup_core
from utils.backup_core.models import RestoreScope
from utils.embeds import (
    build_backup_action_log_embed,
    build_restore_plan_embed,
    build_settings_embed,
    format_missing_permissions_warning,
)
from utils.moderation_utils import add_warn_and_escalate, bot_can_moderate, send_mod_log


def _is_mod(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator


def mod_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not _is_mod(interaction):
            await interaction.response.send_message("⛔ Недостаточно прав.", ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


def owner_check():
    """Для /clone-template: применение бэкапа с ДРУГОГО сервера читает его
    метаданные (имена ролей/каналов) с диска бота независимо от того, состоит
    ли вызывающий в том сервере-источнике — поэтому доступ только владельцу
    бота, а не любому модератору текущего сервера."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if not await interaction.client.is_owner(interaction.user):
            await interaction.response.send_message("⛔ Команда доступна только владельцу бота.", ephemeral=True)
            return False
        return True

    return app_commands.check(predicate)


ACTION_CHOICES = [
    app_commands.Choice(name="warn", value="warn"),
    app_commands.Choice(name="mute", value="mute"),
    app_commands.Choice(name="kick", value="kick"),
    app_commands.Choice(name="ban", value="ban"),
]

LINK_ACTION_CHOICES = ACTION_CHOICES + [app_commands.Choice(name="delete", value="delete")]

SENSITIVITY_PRESETS = {
    "low": (10, 8),     # msg_limit, interval
    "medium": (6, 6),
    "high": (4, 5),
}

# Пресеты длительности мута для автодополнения — пользователь видит готовые
# варианты вместо того, чтобы высчитывать секунды в голове ("1 час" вместо 3600).
DURATION_PRESETS = [
    ("1 минута", 60),
    ("5 минут", 300),
    ("10 минут", 600),
    ("30 минут", 1800),
    ("1 час", 3600),
    ("6 часов", 21600),
    ("12 часов", 43200),
    ("1 день", 86400),
    ("3 дня", 259200),
    ("1 неделя", 604800),
    ("28 дней (максимум)", 2419200),
]


async def duration_autocomplete(interaction: discord.Interaction, current: str):
    """Подсказки длительности для полей мута — Discord всё равно позволяет ввести
    произвольное число, это просто готовые варианты сверху списка."""
    return [
        app_commands.Choice(name=label, value=seconds)
        for label, seconds in DURATION_PRESETS
        if current in label or current in str(seconds)
    ][:25]


async def backup_id_autocomplete(interaction: discord.Interaction, current: str):
    """Подсказки ID бэкапов сервера — от новых к старым, с датой создания
    и пометкой [авто], если это emergency-бэкап перед прошлым restore.
    Без этого пользователю нужно было бы помнить/копировать ID вручную."""
    ids = list(reversed(backup_core.list_backups(interaction.guild.id)))
    choices = []
    for backup_id in ids:
        if current and current.lower() not in backup_id.lower():
            continue
        info = backup_core.get_backup_info(interaction.guild.id, backup_id)
        if info is None:
            continue
        label = f"{backup_id} — {info['created_at']}"
        if info.get("is_emergency"):
            label += " [авто перед restore]"
        choices.append(app_commands.Choice(name=label[:100], value=backup_id))
        if len(choices) >= 25:  # лимит Discord на количество вариантов автодополнения
            break
    return choices


async def clone_backup_id_autocomplete(interaction: discord.Interaction, current: str):
    """Как backup_id_autocomplete, но читает бэкапы СЕРВЕРА-ИСТОЧНИКА, а не
    текущего — id_сервера_источника берём из уже введённого пользователем
    значения соседнего поля (interaction.namespace), доступного на лету,
    пока он заполняет форму слэш-команды.

    ВАЖНО: Discord вызывает функцию автодополнения независимо от того,
    пройдёт ли пользователь app_commands.check() самой команды — проверка
    @owner_check() на /clone-template не защищает автодополнение само по
    себе. Без явной проверки здесь любой пользователь, начавший вводить
    /clone-template и подставив произвольный ID сервера, увидел бы в
    подсказках имена и даты бэкапов ЧУЖИХ серверов, где работает бот —
    утечка приватности. Поэтому owner-проверка дублируется и тут."""
    if not await interaction.client.is_owner(interaction.user):
        return []

    raw_guild_id = getattr(interaction.namespace, "id_сервера_источника", None)
    if not raw_guild_id:
        return []
    try:
        source_guild_id = int(raw_guild_id)
    except ValueError:
        return []

    ids = list(reversed(backup_core.list_backups(source_guild_id)))
    choices = []
    for backup_id in ids:
        if current and current.lower() not in backup_id.lower():
            continue
        info = backup_core.get_backup_info(source_guild_id, backup_id)
        if info is None:
            continue
        label = f"{backup_id} — {info['created_at']} ({info['guild_name']})"
        choices.append(app_commands.Choice(name=label[:100], value=backup_id))
        if len(choices) >= 25:
            break
    return choices


class IgnoreSpamGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="ignore-spam", description="Каналы, игнорируемые антиспамом/антилинком")
        self.bot = bot

    @app_commands.command(name="add", description="Добавить канал игнорирования спама")
    @app_commands.describe(channel="Канал, который нужно игнорировать")
    @mod_check()
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        ids = self.bot.db.get_setting(interaction.guild.id, "ignore_spam_channels", [])
        if channel.id not in ids:
            ids.append(channel.id)
            self.bot.db.set_setting(interaction.guild.id, "ignore_spam_channels", ids)
        await interaction.response.send_message(
            f"✅ Канал {channel.mention} добавлен в игнор антиспама.", ephemeral=True
        )

    @app_commands.command(name="remove", description="Убрать канал игнорирования спама")
    @app_commands.describe(channel="Канал, который нужно убрать из игнора")
    @mod_check()
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        ids = self.bot.db.get_setting(interaction.guild.id, "ignore_spam_channels", [])
        if channel.id in ids:
            ids.remove(channel.id)
            self.bot.db.set_setting(interaction.guild.id, "ignore_spam_channels", ids)
            msg = f"✅ Канал {channel.mention} убран из игнора."
        else:
            msg = "⚠️ Этот канал не в списке игнорируемых."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="reset", description="Забыть настроенные каналы игнорирования спама")
    @mod_check()
    async def reset(self, interaction: discord.Interaction):
        self.bot.db.set_setting(interaction.guild.id, "ignore_spam_channels", [])
        await interaction.response.send_message("✅ Список каналов игнорирования сброшен.", ephemeral=True)


class WarnActionsGroup(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="warn-actions", description="Авто-наказания за количество предупреждений")
        self.bot = bot

    @app_commands.command(name="add", description="Добавить наказание за определённое количество предупреждений")
    @app_commands.describe(
        предупреждений="Количество варнов, при котором срабатывает наказание",
        наказание="warn, mute, kick или ban",
        длительность="Длительность мута в секундах (необязательно)",
    )
    @app_commands.choices(наказание=ACTION_CHOICES)
    @mod_check()
    async def add(
        self,
        interaction: discord.Interaction,
        предупреждений: int,
        наказание: app_commands.Choice[str],
        длительность: int = None,
    ):
        self.bot.db.set_warn_action(interaction.guild.id, предупреждений, наказание.value, длительность)
        await interaction.response.send_message(
            f"✅ За {предупреждений} варн(а/ов) → `{наказание.value}`"
            + (f" на {длительность} сек" if длительность else ""),
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="Удалить наказание за некоторое количество предупреждений")
    @app_commands.describe(предупреждений="Количество варнов, для которого нужно удалить правило")
    @mod_check()
    async def remove(self, interaction: discord.Interaction, предупреждений: int):
        removed = self.bot.db.remove_warn_action(interaction.guild.id, предупреждений)
        await interaction.response.send_message(
            "✅ Удалено." if removed else "⚠️ Такого правила не найдено.", ephemeral=True
        )

    @app_commands.command(name="show", description="Посмотреть все настроенные наказания за предупреждения")
    @mod_check()
    async def show(self, interaction: discord.Interaction):
        rows = self.bot.db.get_all_warn_actions(interaction.guild.id)
        if not rows:
            return await interaction.response.send_message("Список авто-наказаний пуст.", ephemeral=True)
        lines = [
            f"**{r['warn_count']}** варн(а/ов) → `{r['action']}`"
            + (f" ({r['duration_seconds']} сек)" if r["duration_seconds"] else "")
            for r in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class RestoreConfirmView(discord.ui.View):
    """Кнопки подтверждения для /load, чтобы не восстанавливать сервер случайно.
    К моменту показа кнопок план уже построен и показан пользователю (см. /load) —
    кнопка "Подтвердить" применяет именно его область восстановления."""

    def __init__(
        self,
        bot,
        guild: discord.Guild,
        author_id: int,
        scope: RestoreScope,
        remove_extra: bool,
        backup_id: str | None = None,
        kind: str = "load",
        source_guild_id: int | None = None,
    ):
        super().__init__(timeout=30)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.scope = scope
        self.remove_extra = remove_extra
        self.backup_id = backup_id
        self.kind = kind  # "load" | "rollback" | "clone" — только для подписи в mod-log
        self.source_guild_id = source_guild_id  # для клонирования шаблона с другого сервера
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Это подтверждение не для вас.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Подтвердить восстановление", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        try:
            await interaction.response.edit_message(
                content="⏳ Создаётся резервный бэкап и применяется восстановление...", embed=None, view=None
            )
        except discord.HTTPException:
            pass

        try:
            progress_cb = backup_core.make_throttled_progress_callback(
                lambda text: interaction.edit_original_response(content=text)
            )
            _plan, result, emergency_id = await backup_core.restore_with_safety(
                self.guild, backup_id=self.backup_id, scope=self.scope, remove_extra=self.remove_extra,
                progress_cb=progress_cb, source_guild_id=self.source_guild_id,
            )
            content = (
                f"✅ Восстановление завершено: создано {result.total_created()}, "
                f"обновлено {result.total_updated()}, удалено {result.total_removed()}.\n"
                f"Пропущено конфликтов: {result.skipped_conflicts}."
            )
            if result.errors:
                content += f"\n⚠️ Ошибок: {len(result.errors)} (первая: {result.errors[0]})"
            if emergency_id:
                content += f"\n🛟 Резервный бэкап на случай отката: `{emergency_id}` (его можно загрузить через /load)."

            log_description = (
                f"Бэкап: `{self.backup_id or 'последний'}` · Область: `{self.scope}` · "
                f"Удаление лишнего: {'да' if self.remove_extra else 'нет'}\n"
                f"Создано: {result.total_created()}, обновлено: {result.total_updated()}, "
                f"удалено: {result.total_removed()}, конфликтов: {result.skipped_conflicts}, "
                f"ошибок: {len(result.errors)}."
            )
            await send_mod_log(
                self.bot, self.guild, build_backup_action_log_embed(self.kind, interaction.user, log_description)
            )
        except FileNotFoundError:
            content = "⚠️ Бэкап не найден."
        except discord.HTTPException as e:
            content = f"⚠️ Восстановление завершилось с ошибкой Discord API: {e}"

        # Канал, где была вызвана команда, мог быть удалён в процессе восстановления —
        # поэтому используем устойчивую к этому функцию с фолбэками.
        await backup_core.notify_guild_or_dm(self.guild, interaction.user, content, preferred=interaction.followup.send)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Восстановление отменено.", embed=None, view=None)


class SlashCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ignore_spam_group = IgnoreSpamGroup(bot)
        self.warn_actions_group = WarnActionsGroup(bot)
        bot.tree.add_command(self.ignore_spam_group)
        bot.tree.add_command(self.warn_actions_group)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ignore_spam_group.name, type=discord.AppCommandType.chat_input)
        self.bot.tree.remove_command(self.warn_actions_group.name, type=discord.AppCommandType.chat_input)

    # ---------------- Информация ----------------

    @app_commands.command(name="info", description="Посмотреть информацию о боте")
    async def info(self, interaction: discord.Interaction):
        embed = discord.Embed(title="ℹ️ О боте", color=discord.Color.blurple())
        embed.add_field(name="Серверов", value=str(len(self.bot.guilds)))
        embed.add_field(name="Задержка", value=f"{round(self.bot.latency * 1000)} мс")
        embed.add_field(name="Библиотека", value="discord.py")
        embed.set_footer(text="Защита сервера: антиспам, антилинк, антирейд, анти-краш, бэкап")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="server-info", description="Посмотреть информацию об этом сервере")
    async def server_info(self, interaction: discord.Interaction):
        guild = interaction.guild
        embed = discord.Embed(title=f"📊 {guild.name}", color=discord.Color.blurple())
        embed.add_field(name="Участников", value=str(guild.member_count))
        embed.add_field(name="Создан", value=discord.utils.format_dt(guild.created_at, "D"))
        embed.add_field(name="Владелец", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Каналов", value=str(len(guild.channels)))
        embed.add_field(name="Ролей", value=str(len(guild.roles)))
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="settings", description="Посмотреть настройки этого сервера")
    @mod_check()
    async def settings_slash(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_settings_embed(self.bot, interaction.guild))

    # ---------------- Антиспам / антилинк ----------------

    @app_commands.command(name="antispam", description="Настроить наказание за спам")
    @app_commands.describe(включено="Включить антиспам?", действие="Какое наказание применять")
    @app_commands.choices(действие=ACTION_CHOICES)
    @mod_check()
    async def antispam(self, interaction: discord.Interaction, включено: bool, действие: app_commands.Choice[str]):
        self.bot.db.set_setting(interaction.guild.id, "antispam_enabled", включено)
        self.bot.db.set_setting(interaction.guild.id, "antispam_action", действие.value)
        await interaction.response.send_message(
            f"✅ Антиспам: {'включён' if включено else 'выключен'} → `{действие.value}`", ephemeral=True
        )

    @app_commands.command(name="antispam-sensitivity", description="Настроить чувствительность антиспама")
    @app_commands.choices(
        уровень=[
            app_commands.Choice(name="low (мягко)", value="low"),
            app_commands.Choice(name="medium (стандарт)", value="medium"),
            app_commands.Choice(name="high (жёстко)", value="high"),
        ]
    )
    @mod_check()
    async def antispam_sensitivity(self, interaction: discord.Interaction, уровень: app_commands.Choice[str]):
        msg_limit, interval = SENSITIVITY_PRESETS[уровень.value]
        self.bot.db.set_setting(interaction.guild.id, "antispam_sensitivity", уровень.value)
        self.bot.db.set_setting(interaction.guild.id, "antispam_msg_limit", msg_limit)
        self.bot.db.set_setting(interaction.guild.id, "antispam_interval", interval)
        await interaction.response.send_message(
            f"✅ Чувствительность антиспама: `{уровень.value}` ({msg_limit} сообщ. / {interval} сек)",
            ephemeral=True,
        )

    @app_commands.command(name="antilink", description="Настроить наказание за размещение ссылок")
    @app_commands.describe(
        включено="Включить антилинк?",
        действие="Какое наказание применять",
        длительность_мута="Длительность мута в секундах (если действие — mute)",
    )
    @app_commands.choices(действие=LINK_ACTION_CHOICES)
    @app_commands.autocomplete(длительность_мута=duration_autocomplete)
    @mod_check()
    async def antilink(
        self,
        interaction: discord.Interaction,
        включено: bool,
        действие: app_commands.Choice[str],
        длительность_мута: int = None,
    ):
        self.bot.db.set_setting(interaction.guild.id, "antilink_enabled", включено)
        self.bot.db.set_setting(interaction.guild.id, "antilink_action", действие.value)
        if длительность_мута is not None:
            self.bot.db.set_setting(interaction.guild.id, "antilink_mute_seconds", длительность_мута)
        await interaction.response.send_message(
            f"✅ Антилинк: {'включён' if включено else 'выключен'} → `{действие.value}`", ephemeral=True
        )

    @app_commands.command(name="antimention", description="Настроить защиту от массового упоминания пользователей")
    @app_commands.describe(
        включено="Включить анти-mention-спам?",
        лимит="От скольки уникальных упоминаний в одном сообщении срабатывает наказание",
        действие="Какое наказание применять",
        длительность_мута="Длительность мута в секундах (если действие — mute)",
    )
    @app_commands.choices(действие=ACTION_CHOICES)
    @app_commands.autocomplete(длительность_мута=duration_autocomplete)
    @mod_check()
    async def antimention(
        self,
        interaction: discord.Interaction,
        включено: bool,
        лимит: int = 5,
        действие: app_commands.Choice[str] = None,
        длительность_мута: int = None,
    ):
        action_value = действие.value if действие else "mute"
        self.bot.db.set_setting(interaction.guild.id, "antimention_enabled", включено)
        self.bot.db.set_setting(interaction.guild.id, "antimention_limit", max(1, лимит))
        self.bot.db.set_setting(interaction.guild.id, "antimention_action", action_value)
        if длительность_мута is not None:
            self.bot.db.set_setting(interaction.guild.id, "antimention_mute_seconds", длительность_мута)
        await interaction.response.send_message(
            f"✅ Анти-mention-спам: {'включён' if включено else 'выключен'} — "
            f"от {лимит} упоминаний → `{action_value}`",
            ephemeral=True,
        )

    # ---------------- Верификация ----------------

    @app_commands.command(name="verification", description="Настроить верификацию новых участников")
    @app_commands.describe(
        включено="Включить верификацию?",
        роль_до_подтверждения="Роль, ограничивающая доступ до подтверждения",
        роль_после_подтверждения="Роль, выдаваемая после подтверждения (необязательно)",
        таймаут_минут="Кикать, если не подтвердился за N минут (0 = без ограничения)",
    )
    @mod_check()
    async def verification(
        self,
        interaction: discord.Interaction,
        включено: bool,
        роль_до_подтверждения: discord.Role = None,
        роль_после_подтверждения: discord.Role = None,
        таймаут_минут: int = 0,
    ):
        self.bot.db.set_setting(interaction.guild.id, "verification_enabled", включено)
        if роль_до_подтверждения:
            self.bot.db.set_setting(interaction.guild.id, "verification_unverified_role_id", роль_до_подтверждения.id)
        if роль_после_подтверждения:
            self.bot.db.set_setting(interaction.guild.id, "verification_verified_role_id", роль_после_подтверждения.id)
        if таймаут_минут:
            self.bot.db.set_setting(interaction.guild.id, "verification_timeout_minutes", таймаут_минут)
        await interaction.response.send_message(
            f"✅ Верификация: {'включена' if включено else 'выключена'}.\n"
            "Не забудьте `/verification-channel` и `/verification-post`, чтобы отправить кнопку подтверждения.",
            ephemeral=True,
        )

    @app_commands.command(name="verification-channel", description="Назначить канал для кнопки верификации")
    @app_commands.describe(канал="Канал, куда отправится сообщение с кнопкой")
    @mod_check()
    async def verification_channel(self, interaction: discord.Interaction, канал: discord.TextChannel):
        self.bot.db.set_setting(interaction.guild.id, "verification_channel_id", канал.id)
        await interaction.response.send_message(f"✅ Канал верификации установлен: {канал.mention}", ephemeral=True)

    @app_commands.command(name="verification-post", description="Отправить (повторно) сообщение с кнопкой верификации")
    @mod_check()
    async def verification_post(self, interaction: discord.Interaction):
        from cogs.verification import VerifyView

        settings = self.bot.db.get_all_settings(interaction.guild.id)
        channel_id = settings.get("verification_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel
        if channel is None:
            return await interaction.response.send_message(
                "⚠️ Канал верификации не найден. Настройте его через /verification-channel.", ephemeral=True
            )
        embed = discord.Embed(
            title="🔐 Верификация",
            description=(
                "Нажмите кнопку ниже, чтобы подтвердить, что вы не бот, "
                "и получить полный доступ к серверу."
            ),
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=VerifyView(self.bot))
        await interaction.response.send_message(f"✅ Сообщение с верификацией отправлено в {channel.mention}.", ephemeral=True)

    # ---------------- Защита от новых аккаунтов/ботов ----------------

    @app_commands.command(
        name="ban-new-users", description="Настроить/выключить автоматический бан новых аккаунтов"
    )
    @app_commands.describe(включено="Включить автобан?", мин_возраст_часов="Минимальный возраст аккаунта в часах")
    @mod_check()
    async def ban_new_users(self, interaction: discord.Interaction, включено: bool, мин_возраст_часов: int = 24):
        self.bot.db.set_setting(interaction.guild.id, "ban_new_users_enabled", включено)
        self.bot.db.set_setting(interaction.guild.id, "ban_new_users_min_age_hours", мин_возраст_часов)
        await interaction.response.send_message(
            f"✅ Автобан новых аккаунтов: {'включён' if включено else 'выключен'} "
            f"(мин. возраст {мин_возраст_часов} ч.)",
            ephemeral=True,
        )

    @app_commands.command(
        name="ban-new-users-desc", description="Задать описание/причину автобана новых аккаунтов"
    )
    @app_commands.describe(описание="Текст, который будет указан как причина бана")
    @mod_check()
    async def ban_new_users_desc(self, interaction: discord.Interaction, описание: str):
        self.bot.db.set_setting(interaction.guild.id, "ban_new_users_description", описание)
        await interaction.response.send_message(f"✅ Описание автобана обновлено: {описание}", ephemeral=True)

    @app_commands.command(
        name="isolate-new-bots", description="Включить/выключить временное обнуление прав приглашаемых ботов"
    )
    @app_commands.describe(включено="Включить изоляцию новых ботов?")
    @mod_check()
    async def isolate_new_bots(self, interaction: discord.Interaction, включено: bool):
        self.bot.db.set_setting(interaction.guild.id, "isolate_new_bots_enabled", включено)
        await interaction.response.send_message(
            f"✅ Изоляция новых ботов: {'включена' if включено else 'выключена'}", ephemeral=True
        )

    @app_commands.command(name="mod-log-channel", description="Настроить канал для логирования модераторских действий")
    @app_commands.describe(канал="Канал, куда будут приходить логи kick/ban/mute/warn")
    @mod_check()
    async def mod_log_channel(self, interaction: discord.Interaction, канал: discord.TextChannel):
        self.bot.db.set_setting(interaction.guild.id, "mod_log_channel_id", канал.id)
        await interaction.response.send_message(f"✅ Канал мод-логов установлен: {канал.mention}", ephemeral=True)

    # ---------------- Модерация ----------------

    @app_commands.command(name="ban", description="Забанить пользователя")
    @app_commands.describe(пользователь="Кого забанить", причина="Причина бана")
    @app_commands.checks.cooldown(1, 3)
    @mod_check()
    async def ban(self, interaction: discord.Interaction, пользователь: discord.Member, причина: str = "Не указана"):
        error = bot_can_moderate(interaction.guild, пользователь, "ban_members")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        try:
            await пользователь.ban(reason=причина, delete_message_seconds=0)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⛔ Discord отклонил действие (недостаточно прав у бота).", ephemeral=True
            )
        embed = discord.Embed(
            description=f"🔨 {пользователь} забанен.\n**Причина:** {причина}", color=discord.Color.dark_red()
        )
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_mod_log(self.bot, interaction.guild, embed)

    @app_commands.command(name="unban", description="Разбанить пользователя")
    @app_commands.describe(id_пользователя="Discord ID пользователя для разбана")
    @mod_check()
    async def unban(self, interaction: discord.Interaction, id_пользователя: str):
        try:
            user_id = int(id_пользователя)
        except ValueError:
            return await interaction.response.send_message("⚠️ Укажите числовой ID пользователя.", ephemeral=True)
        try:
            await interaction.guild.unban(discord.Object(id=user_id))
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⛔ У бота нет права Ban Members, необходимого для разбана.", ephemeral=True
            )
        except discord.NotFound:
            return await interaction.response.send_message(
                "⚠️ Пользователь с таким ID не найден в списке банов.", ephemeral=True
            )
        await interaction.response.send_message(f"✅ Пользователь с ID {user_id} разбанен.")

    @app_commands.command(name="kick", description="Кикнуть (исключить) пользователя")
    @app_commands.describe(пользователь="Кого исключить", причина="Причина исключения")
    @app_commands.checks.cooldown(1, 3)
    @mod_check()
    async def kick(self, interaction: discord.Interaction, пользователь: discord.Member, причина: str = "Не указана"):
        error = bot_can_moderate(interaction.guild, пользователь, "kick_members")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        try:
            await пользователь.kick(reason=причина)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⛔ Discord отклонил действие (недостаточно прав у бота).", ephemeral=True
            )
        embed = discord.Embed(
            description=f"👢 {пользователь} исключён.\n**Причина:** {причина}", color=discord.Color.red()
        )
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_mod_log(self.bot, interaction.guild, embed)

    @app_commands.command(name="mute", description="Замьютить (заглушить) пользователя")
    @app_commands.describe(пользователь="Кого замьютить", секунды="Длительность мута в секундах", причина="Причина")
    @app_commands.autocomplete(секунды=duration_autocomplete)
    @app_commands.checks.cooldown(1, 3)
    @mod_check()
    async def mute(
        self,
        interaction: discord.Interaction,
        пользователь: discord.Member,
        секунды: int = 300,
        причина: str = "Не указана",
    ):
        error = bot_can_moderate(interaction.guild, пользователь, "moderate_members")
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        секунды = max(1, min(секунды, 28 * 24 * 60 * 60))
        try:
            await пользователь.timeout(timedelta(seconds=секунды), reason=причина)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⛔ Discord отклонил действие (недостаточно прав у бота).", ephemeral=True
            )
        embed = discord.Embed(
            description=f"🔇 {пользователь.mention} получил тайм-аут на {секунды} сек.\n**Причина:** {причина}",
            color=discord.Color.yellow(),
        )
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_mod_log(self.bot, interaction.guild, embed)

    @app_commands.command(name="unmute", description="Размьютить пользователя")
    @app_commands.describe(пользователь="С кого снять тайм-аут")
    @mod_check()
    async def unmute(self, interaction: discord.Interaction, пользователь: discord.Member):
        try:
            await пользователь.timeout(None)
        except discord.Forbidden:
            return await interaction.response.send_message(
                "⛔ Discord отклонил действие (недостаточно прав у бота).", ephemeral=True
            )
        await interaction.response.send_message(f"✅ Тайм-аут снят с {пользователь.mention}.")

    @app_commands.command(name="purge", description="Удалить несколько сообщений сразу")
    @app_commands.describe(количество="Сколько сообщений удалить (1-200)")
    @mod_check()
    async def purge(self, interaction: discord.Interaction, количество: int = 10):
        количество = max(1, min(количество, 200))
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(limit=количество)
        except discord.Forbidden:
            return await interaction.followup.send("⛔ У бота нет права Manage Messages в этом канале.", ephemeral=True)
        await interaction.followup.send(f"🧹 Удалено сообщений: {len(deleted)}", ephemeral=True)

    # ---------------- Варны ----------------

    @app_commands.command(name="warn", description="Предупредить пользователя")
    @app_commands.describe(пользователь="Кого предупредить", причина="Причина предупреждения")
    @mod_check()
    async def warn(self, interaction: discord.Interaction, пользователь: discord.Member, причина: str = "Не указана"):
        count, action_row = await add_warn_and_escalate(self.bot, interaction.guild, пользователь, interaction.user.id, причина)
        embed = discord.Embed(
            description=f"⚠️ {пользователь.mention} получил предупреждение ({count}-е).\n**Причина:** {причина}",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Модератор: {interaction.user}")
        await interaction.response.send_message(embed=embed)
        await send_mod_log(self.bot, interaction.guild, embed)

        if action_row:
            await interaction.followup.send(
                f"🔧 {пользователь.mention} получил `{action_row['action']}` за {count} варн(а/ов)."
            )

    @app_commands.command(name="warns", description="Посмотреть свои или чужие предупреждения")
    @app_commands.describe(пользователь="Чьи предупреждения посмотреть (по умолчанию — свои)")
    async def warns(self, interaction: discord.Interaction, пользователь: discord.Member = None):
        member = пользователь or interaction.user
        if member != interaction.user and not _is_mod(interaction):
            return await interaction.response.send_message(
                "⛔ Чтобы смотреть чужие предупреждения, нужны права модератора.", ephemeral=True
            )
        rows = self.bot.db.get_warns(interaction.guild.id, member.id)
        if not rows:
            return await interaction.response.send_message(f"У {member.mention} нет предупреждений.", ephemeral=True)
        lines = [f"**#{i+1}** — {r['reason']}" for i, r in enumerate(rows)]
        embed = discord.Embed(
            title=f"Предупреждения {member}", description="\n".join(lines), color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=(member != interaction.user))

    @app_commands.command(name="unwarn", description="Убрать одно предупреждение у пользователя, либо сразу все")
    @app_commands.describe(пользователь="С кого снять предупреждение", все="Снять сразу все предупреждения?")
    @mod_check()
    async def unwarn(self, interaction: discord.Interaction, пользователь: discord.Member, все: bool = False):
        if все:
            self.bot.db.clear_warns(interaction.guild.id, пользователь.id)
            await interaction.response.send_message(f"✅ Все предупреждения {пользователь.mention} очищены.")
        else:
            ok = self.bot.db.remove_last_warn(interaction.guild.id, пользователь.id)
            await interaction.response.send_message(
                "✅ Снято одно предупреждение." if ok else "У пользователя нет предупреждений."
            )

    # ---------------- Бэкап ----------------

    SCOPE_CHOICES = [
        app_commands.Choice(name="всё", value="all"),
        app_commands.Choice(name="только роли", value="roles"),
        app_commands.Choice(name="только каналы", value="channels"),
        app_commands.Choice(name="только категории", value="categories"),
        app_commands.Choice(name="только права доступа", value="permissions"),
    ]

    @app_commands.command(name="save", description="Сохранить структуру сервера в файл")
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    @mod_check()
    async def save(self, interaction: discord.Interaction):
        await interaction.response.defer()
        counts = await backup_core.save_backup(interaction.guild)
        await interaction.followup.send(
            f"💾 Бэкап сохранён (`{counts.get('backup_id', '?')}`): "
            f"{counts['roles']} ролей, {counts.get('categories', 0)} категорий, "
            f"{counts['channels']} каналов, {counts.get('emojis', 0)} эмодзи, {counts.get('stickers', 0)} стикеров, "
            f"{counts.get('automod_rules', 0)} правил AutoMod, {counts.get('webhooks', 0)} вебхуков."
        )
        await send_mod_log(
            self.bot,
            interaction.guild,
            build_backup_action_log_embed(
                "save", interaction.user, f"Создан бэкап `{counts.get('backup_id', '?')}` ({counts['roles']} ролей, {counts['channels']} каналов)."
            ),
        )

    @app_commands.command(name="load", description="Восстановить сервер из файла бэкапа")
    @app_commands.describe(
        область="Что восстанавливать — по умолчанию всё",
        удалять_лишнее="Удалять то, чего нет в бэкапе (роли/каналы) — по умолчанию выключено",
        бэкап="Какой бэкап использовать — по умолчанию последний",
    )
    @app_commands.choices(область=SCOPE_CHOICES)
    @app_commands.autocomplete(бэкап=backup_id_autocomplete)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    @mod_check()
    async def load(
        self,
        interaction: discord.Interaction,
        область: app_commands.Choice[str] = None,
        удалять_лишнее: bool = False,
        бэкап: str = None,
    ):
        if not backup_core.has_backup(interaction.guild.id):
            return await interaction.response.send_message(
                "⚠️ Бэкап не найден. Сначала выполните `/save`.", ephemeral=True
            )

        scope_keyword = область.value if область else "all"
        scope = RestoreScope.from_keyword(scope_keyword)

        await interaction.response.defer()
        try:
            plan = await backup_core.build_plan(interaction.guild, бэкап, scope=scope, remove_extra=удалять_лишнее)
        except FileNotFoundError:
            return await interaction.followup.send("⚠️ Бэкап не найден.")

        embed = build_restore_plan_embed(plan, interaction.guild.name)
        if plan.is_empty:
            return await interaction.followup.send(
                "Текущее состояние сервера уже совпадает с бэкапом — изменений не требуется.", embed=embed
            )

        missing_perms = backup_core.missing_permissions_for_scope(interaction.guild, scope)
        warning = format_missing_permissions_warning(missing_perms)

        view = RestoreConfirmView(
            self.bot, interaction.guild, interaction.user.id, scope, удалять_лишнее, backup_id=бэкап, kind="load"
        )
        await interaction.followup.send(
            warning
            + "⚠️ Перед восстановлением будет автоматически создан резервный бэкап текущего состояния. "
            "Подтвердите применение плана выше:",
            embed=embed,
            view=view,
        )

    @app_commands.command(name="backups", description="Посмотреть список сохранённых бэкапов этого сервера")
    @mod_check()
    async def backups_list(self, interaction: discord.Interaction):
        ids = list(reversed(backup_core.list_backups(interaction.guild.id)))
        if not ids:
            return await interaction.response.send_message("Бэкапов для этого сервера пока нет.", ephemeral=True)

        lines = []
        for backup_id in ids[:25]:  # лимит на длину embed-поля — see build_restore_plan_embed для аналогичной логики
            info = backup_core.get_backup_info(interaction.guild.id, backup_id)
            if info is None:
                continue
            mark = " 🛟 (авто перед restore)" if info.get("is_emergency") else ""
            lines.append(f"`{backup_id}` — {info['created_at']}{mark}\n{info['roles']} ролей, {info['channels']} каналов")
        embed = discord.Embed(
            title=f"📦 Бэкапы сервера «{interaction.guild.name}»",
            description="\n\n".join(lines) or "—",
            color=discord.Color.blurple(),
        )
        if len(ids) > 25:
            embed.set_footer(text=f"Показаны последние 25 из {len(ids)}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="resume", description="Продолжить восстановление, прерванное перезапуском бота"
    )
    @app_commands.describe(
        чекпоинт="ID чекпоинта — оставьте пустым, чтобы выбрать единственный или последний",
        отменить="Отменить чекпоинт БЕЗ применения оставшихся пунктов (если проблему уже решили вручную)",
    )
    @mod_check()
    async def resume_cmd(self, interaction: discord.Interaction, чекпоинт: str = None, отменить: bool = False):
        checkpoints = backup_core.list_checkpoints(interaction.guild.id)
        if not checkpoints:
            return await interaction.response.send_message(
                "✅ Незавершённых восстановлений нет — всё завершилось штатно или чекпоинты ещё не создавались.",
                ephemeral=True,
            )

        cp_id = чекпоинт
        if cp_id is None:
            if len(checkpoints) == 1:
                cp_id = checkpoints[0]["checkpoint_id"]
            else:
                lines = [
                    f"`{c['checkpoint_id']}` — прогресс {c['progress']}, бэкап `{c['backup_id']}`"
                    for c in checkpoints
                ]
                return await interaction.response.send_message(
                    "Найдено несколько незавершённых чекпоинтов — укажите один явно:\n" + "\n".join(lines),
                    ephemeral=True,
                )

        if отменить:
            backup_core.discard_checkpoint(interaction.guild.id, cp_id)
            return await interaction.response.send_message(
                f"🗑️ Чекпоинт `{cp_id}` отменён — оставшиеся пункты плана применены не будут "
                "(уже сделанное до сбоя НЕ откатывается).",
                ephemeral=True,
            )

        await interaction.response.defer()
        try:
            status = await interaction.followup.send(f"⏳ Возобновляю восстановление с чекпоинта `{cp_id}`...")

            async def progress_edit(text):
                await status.edit(content=text)

            progress_cb = backup_core.make_throttled_progress_callback(progress_edit)
            _plan, result, emergency_id = await backup_core.resume_from_checkpoint(
                interaction.guild, cp_id, progress_cb=progress_cb
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
                self.bot, interaction.guild,
                build_backup_action_log_embed("load", interaction.user, f"Возобновлено с чекпоинта `{cp_id}`. {content}"),
            )
        except FileNotFoundError:
            content = f"⚠️ Чекпоинт `{cp_id}` не найден."
        except discord.HTTPException as e:
            content = f"⚠️ Ошибка Discord API при возобновлении: {e}"

        await backup_core.notify_guild_or_dm(interaction.guild, interaction.user, content, preferred=status.edit)

    @app_commands.command(
        name="rollback", description="Откатиться к авто-бэкапу, сделанному перед последним восстановлением"
    )
    @app_commands.describe(бэкап="Конкретный бэкап для отката — по умолчанию последний emergency-бэкап")
    @app_commands.autocomplete(бэкап=backup_id_autocomplete)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    @mod_check()
    async def rollback(self, interaction: discord.Interaction, бэкап: str = None):
        target_id = бэкап or backup_core.latest_emergency_backup_id(interaction.guild.id)
        if target_id is None:
            return await interaction.response.send_message(
                "⚠️ Не найдено ни одного авто-бэкапа перед restore — откатываться не к чему. "
                "Используйте `/load` с конкретным `бэкап`, если хотите восстановиться к произвольному снимку.",
                ephemeral=True,
            )

        scope = RestoreScope.all()
        await interaction.response.defer()
        try:
            # remove_extra=True — смысл rollback именно в полном возврате к прежнему состоянию,
            # а не в частичном "доливании" отсутствующего, как при обычном /load.
            plan = await backup_core.build_plan(interaction.guild, target_id, scope=scope, remove_extra=True)
        except FileNotFoundError:
            return await interaction.followup.send("⚠️ Указанный бэкап не найден.")

        embed = build_restore_plan_embed(plan, interaction.guild.name)
        if plan.is_empty:
            return await interaction.followup.send(
                f"Текущее состояние сервера уже совпадает с бэкапом `{target_id}` — откатывать нечего.", embed=embed
            )

        missing_perms = backup_core.missing_permissions_for_scope(interaction.guild, scope)
        warning = format_missing_permissions_warning(missing_perms)

        view = RestoreConfirmView(self.bot, interaction.guild, interaction.user.id, scope, True, backup_id=target_id, kind="rollback")
        await interaction.followup.send(
            warning
            + f"⚠️ Откат к бэкапу `{target_id}`. Перед применением будет создан ещё один резервный бэкап "
            "текущего состояния (на случай, если и откат окажется ошибкой). Подтвердите применение плана выше:",
            embed=embed,
            view=view,
        )

    @app_commands.command(
        name="backup-diff", description="Сравнить два бэкапа между собой — что изменилось между ними"
    )
    @app_commands.describe(бэкап1="Более старый бэкап", бэкап2="Более новый бэкап")
    @app_commands.autocomplete(бэкап1=backup_id_autocomplete, бэкап2=backup_id_autocomplete)
    @mod_check()
    async def backup_diff(self, interaction: discord.Interaction, бэкап1: str, бэкап2: str):
        try:
            plan = backup_core.diff_backups(interaction.guild.id, бэкап1, бэкап2)
        except FileNotFoundError:
            return await interaction.response.send_message("⚠️ Один из указанных бэкапов не найден.", ephemeral=True)

        embed = build_restore_plan_embed(plan, interaction.guild.name)
        embed.title = "📋 Сравнение бэкапов"
        embed.description = f"`{бэкап1}` → `{бэкап2}`"
        if plan.is_empty:
            embed.description += "\n\n✅ Между этими бэкапами нет различий."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="clone-template",
        description="[Только владелец бота] Применить бэкап с другого сервера как шаблон на этот сервер",
    )
    @app_commands.describe(
        id_сервера_источника="ID сервера, чей бэкап используется как шаблон",
        область="Что переносить — по умолчанию всё",
        удалять_лишнее="Удалять то, чего нет в шаблоне — по умолчанию выключено",
        бэкап="Какой бэкап сервера-источника использовать — по умолчанию последний",
    )
    @app_commands.choices(область=SCOPE_CHOICES)
    @app_commands.autocomplete(бэкап=clone_backup_id_autocomplete)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    @owner_check()
    async def clone_template(
        self,
        interaction: discord.Interaction,
        id_сервера_источника: str,
        область: app_commands.Choice[str] = None,
        удалять_лишнее: bool = False,
        бэкап: str = None,
    ):
        try:
            source_guild_id = int(id_сервера_источника)
        except ValueError:
            return await interaction.response.send_message("⚠️ ID сервера должен быть числом.", ephemeral=True)

        if not backup_core.has_backup(source_guild_id):
            return await interaction.response.send_message(
                "⚠️ У указанного сервера нет сохранённых бэкапов (или бот никогда не делал /save там).",
                ephemeral=True,
            )

        scope_keyword = область.value if область else "all"
        scope = RestoreScope.from_keyword(scope_keyword)

        await interaction.response.defer()
        try:
            plan = await backup_core.build_plan(
                interaction.guild, бэкап, scope=scope, remove_extra=удалять_лишнее, source_guild_id=source_guild_id
            )
        except FileNotFoundError:
            return await interaction.followup.send("⚠️ Указанный бэкап не найден.")

        embed = build_restore_plan_embed(plan, interaction.guild.name)
        embed.description = f"Источник (шаблон): сервер `{source_guild_id}`\n" + embed.description
        if plan.is_empty:
            return await interaction.followup.send(
                "Текущее состояние сервера уже совпадает с шаблоном — изменений не требуется.", embed=embed
            )

        missing_perms = backup_core.missing_permissions_for_scope(interaction.guild, scope)
        warning = format_missing_permissions_warning(missing_perms)

        view = RestoreConfirmView(
            self.bot, interaction.guild, interaction.user.id, scope, удалять_лишнее,
            backup_id=бэкап, kind="clone", source_guild_id=source_guild_id,
        )
        await interaction.followup.send(
            warning
            + "⚠️ Применяется ШАБЛОН с другого сервера. Перед применением будет автоматически создан "
            "резервный бэкап текущего состояния ЭТОГО сервера. Подтвердите план выше:",
            embed=embed,
            view=view,
        )

    @app_commands.command(
        name="auditwatch",
        description="Настроить дублирование аудит-лога сервера в канал"
    )
    @app_commands.describe(
        включить="Включить или выключить аудит-лог вотчер",
        канал="Канал для дублирования — обязателен при включении",
    )
    @mod_check()
    async def auditwatch(
        self,
        interaction: discord.Interaction,
        включить: bool,
        канал: discord.TextChannel = None,
    ):
        if включить and канал is None:
            existing_channel_id = self.bot.db.get_setting(interaction.guild.id, "auditwatch_channel_id")
            if not existing_channel_id:
                return await interaction.response.send_message(
                    "⚠️ Укажите канал: `/auditwatch включить:True канал:#канал`.", ephemeral=True
                )

        self.bot.db.set_setting(interaction.guild.id, "auditwatch_enabled", включить)
        if канал is not None:
            self.bot.db.set_setting(interaction.guild.id, "auditwatch_channel_id", канал.id)

        target = канал.mention if канал else (
            f"<#{self.bot.db.get_setting(interaction.guild.id, 'auditwatch_channel_id')}>"
            if self.bot.db.get_setting(interaction.guild.id, "auditwatch_channel_id")
            else "ранее указанный канал"
        )
        status = "включён" if включить else "выключен"
        await interaction.response.send_message(
            f"✅ Аудит-лог вотчер {status}" + (f" → {target}" if включить else "."),
            ephemeral=True,
        )

    @app_commands.command(
        name="find-duplicates", description="Найти роли/каналы/категории с одинаковыми именами на сервере"
    )
    @mod_check()
    async def find_duplicates(self, interaction: discord.Interaction):
        groups = backup_core.find_duplicates(interaction.guild)
        if not groups:
            return await interaction.response.send_message(
                "✅ Дубликатов по именам не найдено — ролям и каналам можно безопасно сопоставляться с бэкапом.",
                ephemeral=True,
            )

        labels = {"role": "Роль", "category": "Категория", "channel": "Канал"}
        lines = [f"**{labels.get(g.kind, g.kind)}** «{g.name}» — {len(g.ids)} шт. ({', '.join(str(i) for i in g.ids)})" for g in groups]
        embed = discord.Embed(
            title="⚠️ Найдены дубликаты имён",
            description=(
                "Восстановление из бэкапа сопоставляет сущности по имени, а не по Discord ID — "
                "из-за этих дубликатов соответствующие элементы будут помечены конфликтом и пропущены "
                "при `/load`. Рекомендуется переименовать или удалить лишние вручную.\n\n" + "\n".join(lines[:25])
            ),
            color=discord.Color.orange(),
        )
        if len(lines) > 25:
            embed.set_footer(text=f"Показаны первые 25 из {len(lines)} групп.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SlashCommands(bot))
