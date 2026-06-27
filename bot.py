"""
Lavan-clone — мультифункциональный Discord-бот для защиты сервера.
Анти-спам, анти-mention-спам, анти-рейд/анти-краш, верификация новых
участников, модерация, бэкап сервера, настройки.

Запуск:
    1. pip install -r requirements.txt
    2. Заполнить .env (см. .env.example) — DISCORD_TOKEN
    3. python bot.py
"""

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands
from dotenv import load_dotenv

from cogs.verification import VerifyView
from utils.database import Database

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "L.")

# ---------------- Логирование ----------------
# Пишем и в консоль, и в файл с ротацией (5 МБ x 3 файла) — иначе при падении
# бота на ночном запуске трейсбек просто исчезает вместе с закрытым терминалом.
os.makedirs("logs", exist_ok=True)
_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s")

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

_file_handler = RotatingFileHandler(
    "logs/lavan-bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
log = logging.getLogger("lavan")

db = Database("data.sqlite3")


async def get_prefix(bot_: "LavanBot", message: discord.Message):
    if message.guild is None:
        return DEFAULT_PREFIX
    prefix = bot_.db.get_setting(message.guild.id, "prefix", DEFAULT_PREFIX)
    return prefix


class LavanBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        intents.guilds = True
        intents.moderation = True

        super().__init__(command_prefix=get_prefix, intents=intents, help_command=None)
        self.db = db
        # Глобальный обработчик ошибок слэш-команд — без него Forbidden/HTTPException
        # из любой команды просто "проглатывается" в лог, а пользователь в Discord
        # не получает никакого ответа (интеракция выглядит как зависшая/проваленная).
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self):
        for ext in (
            "cogs.settings",
            "cogs.moderation",
            "cogs.antispam",
            "cogs.antilink",
            "cogs.antiraid",
            "cogs.verification",
            "cogs.backup",
            "cogs.slash",
            "cogs.help",
        ):
            await self.load_extension(ext)
            log.info("Загружен модуль: %s", ext)

        # Регистрируем persistent-view кнопки верификации ОДИН раз при старте.
        # Благодаря фиксированному custom_id кнопка продолжит работать на старых
        # сообщениях даже после перезапуска бота — их не нужно пересылать.
        self.add_view(VerifyView(self))
        log.info("Persistent-view верификации зарегистрирована")

        # Синхронизация слэш-команд
        await self.tree.sync()
        log.info("Слэш-команды синхронизированы")

    async def on_ready(self):
        log.info("Бот запущен как %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="за безопасностью сервера"
            )
        )

    async def on_resumed(self):
        # discord.py сам переподключается при разрывах гейтвея; здесь только
        # фиксируем это в логе, чтобы было видно в файле логов, что происходило,
        # если сервер "странно" вёл себя в какой-то момент (например, не сработал антирейд).
        log.warning("Соединение с Discord Gateway восстановлено после разрыва (resume)")

    async def on_disconnect(self):
        log.warning("Соединение с Discord Gateway потеряно, ожидаю переподключения...")

    async def on_guild_join(self, guild: discord.Guild):
        self.db.ensure_guild(guild.id)
        log.info("Добавлен на сервер: %s (%s)", guild.name, guild.id)

    # ---------------- Глобальная обработка ошибок ----------------

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """Обрабатывает ошибки префиксных команд (L.kick, L.ban и т.д.).
        Без этого: CheckFailure (нет прав) и Forbidden (боту не хватает прав
        на сервере) молча уходят в лог, а пользователь ничего не видит в Discord."""
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CheckFailure):
            return await ctx.send("⛔ У вас недостаточно прав для этой команды.")
        if isinstance(error, commands.CommandOnCooldown):
            return await ctx.send(
                f"⏱️ Команда недавно уже использовалась. Попробуйте снова через {error.retry_after:.1f} сек."
            )
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(f"⚠️ Не указан обязательный аргумент: `{error.param.name}`.")
        if isinstance(error, (commands.BadArgument, commands.MemberNotFound, commands.ChannelNotFound)):
            return await ctx.send(f"⚠️ Некорректный аргумент команды: {error}")

        original = getattr(error, "original", error)
        if isinstance(original, discord.Forbidden):
            return await ctx.send(
                "⛔ У бота не хватает прав для этого действия. Проверьте, что роль бота "
                "выше роли участника и что у бота включено нужное право (Kick/Ban/Moderate Members)."
            )
        if isinstance(original, discord.HTTPException):
            return await ctx.send(f"⚠️ Ошибка Discord API: {original}")

        log.exception("Необработанная ошибка команды %s", ctx.command, exc_info=original)
        await ctx.send("⚠️ Произошла непредвиденная ошибка при выполнении команды.")

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        """Обрабатывает ошибки слэш-команд (/kick, /ban и т.д.) — аналог on_command_error
        выше, но для interaction-based команд."""
        if isinstance(error, discord.app_commands.CheckFailure) and not isinstance(
            error, discord.app_commands.CommandOnCooldown
        ):
            # mod_check() уже отправил пользователю "⛔ Недостаточно прав." сам — здесь нечего добавлять.
            return

        if isinstance(error, discord.app_commands.CommandOnCooldown):
            message = f"⏱️ Команда недавно уже использовалась. Попробуйте снова через {error.retry_after:.1f} сек."
        elif isinstance(error, discord.app_commands.TransformerError):
            # Частая ситуация: пользователь ввёл текст руками вместо выбора участника/канала
            # из автодополнения Discord, либо указал ID/имя несуществующего объекта.
            # Это ошибка ввода, а не баг бота — не шумим в лог полным трейсбеком.
            message = (
                f"⚠️ Не удалось распознать значение `{error.value}`. "
                "Выберите участника или канал из списка автодополнения Discord, "
                "а не вводите его вручную текстом."
            )
        else:
            original = getattr(error, "original", error)
            if isinstance(original, discord.Forbidden):
                message = (
                    "⛔ У бота не хватает прав для этого действия. Проверьте, что роль бота "
                    "выше роли участника и что у бота включено нужное право (Kick/Ban/Moderate Members)."
                )
            elif isinstance(original, discord.HTTPException):
                message = f"⚠️ Ошибка Discord API: {original}"
            else:
                log.exception(
                    "Необработанная ошибка слэш-команды %s",
                    interaction.command.name if interaction.command else "?",
                    exc_info=original,
                )
                message = "⚠️ Произошла непредвиденная ошибка при выполнении команды."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


bot = LavanBot()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Не найден DISCORD_TOKEN. Заполните файл .env (см. .env.example).")
    asyncio.run(bot.start(TOKEN))

