"""Анти-спам: следит за частотой сообщений каждого пользователя
и применяет настроенное наказание при превышении лимита.

Также здесь живёт анти-mention-спам — защита от сообщений с массовым
упоминанием пользователей (классический приём для "пинг-рейдов").

Примечание: проверка ссылок (антилинк) вынесена в отдельный модуль
cogs/antilink.py, чтобы не дублировать одну и ту же логику в двух местах."""

import time
from collections import defaultdict, deque

import discord
from discord.ext import commands

from utils.moderation_utils import add_warn_and_escalate, apply_punishment, send_log


class AntiSpam(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # {(guild_id, user_id): deque[timestamps]}
        self.message_log: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=50))
        # Небольшой кулдаун, чтобы не наказывать дважды подряд за один и тот же всплеск
        self.recent_actions: dict[tuple[int, int], float] = {}
        self.recent_mention_actions: dict[tuple[int, int], float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if isinstance(message.author, discord.Member) and (
            message.author.guild_permissions.manage_messages
        ):
            return  # модераторов не трогаем

        settings = self.bot.db.get_all_settings(message.guild.id)
        ignore_channels = set(settings.get("ignore_spam_channels", []))
        if message.channel.id in ignore_channels:
            return

        # Антиmention-спам проверяем первым: одно сообщение с кучей упоминаний
        # опаснее для сервера, чем просто частые сообщения, и должно сработать
        # даже если лимит обычного антиспама ещё не превышен.
        handled = await self._check_antimention(message, settings)
        if not handled:
            await self._check_antispam(message, settings)

    async def _punish(
        self,
        message: discord.Message,
        settings: dict,
        action: str,
        reason: str,
        mute_seconds: int,
        public_notice: str,
        log_title: str,
    ):
        """Общая логика применения наказания + уведомления + лог, используется
        и антиспамом, и анти-mention-спамом, чтобы не дублировать код дважды."""
        member = message.author
        action_row = None
        count = None

        if action == "warn":
            count, action_row = await add_warn_and_escalate(
                self.bot, message.guild, member, self.bot.user.id, reason
            )
            public_notice += f" (предупреждение №{count})"
        else:
            await apply_punishment(message.guild, member, action, reason, duration_seconds=mute_seconds)

        try:
            await message.channel.send(public_notice, delete_after=10)
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title=log_title,
            description=f"Пользователь: {member.mention}\nДействие: `{action}`\nПричина: {reason}",
            color=discord.Color.red(),
        )
        await send_log(self.bot, message.guild, embed)

        if action_row:
            await send_log(
                self.bot,
                message.guild,
                discord.Embed(
                    description=f"🔧 {member.mention} получил `{action_row['action']}` "
                    f"за {count} варн(а/ов) (эскалация).",
                    color=discord.Color.dark_red(),
                ),
            )

    async def _check_antimention(self, message: discord.Message, settings: dict) -> bool:
        """Возвращает True, если сработало наказание (чтобы не дублировать с антиспамом)."""
        if not settings.get("antimention_enabled", False):
            return False

        # Считаем уникальных упомянутых пользователей, а не общее число <@id> —
        # иначе можно было бы обойти лимит, упомянув одного и того же человека N раз.
        unique_mentions = {u.id for u in message.mentions if not u.bot}
        limit = settings.get("antimention_limit", 5)
        if len(unique_mentions) < limit:
            return False

        key = (message.guild.id, message.author.id)
        now = time.time()
        last_action = self.recent_mention_actions.get(key, 0)
        if now - last_action < 10:  # антидубликат: не чаще раза в 10 сек
            return False
        self.recent_mention_actions[key] = now

        try:
            await message.delete()
        except discord.Forbidden:
            pass

        await self._punish(
            message,
            settings,
            action=settings.get("antimention_action", "mute"),
            reason=f"Антиmention-спам: {len(unique_mentions)} упоминаний в одном сообщении",
            mute_seconds=settings.get("antimention_mute_seconds", 600),
            public_notice=f"🚨 {message.author.mention} получил наказание за массовое упоминание пользователей.",
            log_title="Анти-mention-спам сработал",
        )
        return True

    async def _check_antispam(self, message: discord.Message, settings: dict):
        if not settings["antispam_enabled"]:
            return

        key = (message.guild.id, message.author.id)
        now = time.time()
        log = self.message_log[key]
        log.append(now)

        interval = settings["antispam_interval"]
        limit = settings["antispam_msg_limit"]

        # считаем сколько сообщений за последние `interval` секунд
        recent = [t for t in log if now - t <= interval]
        self.message_log[key] = deque(recent, maxlen=50)

        if len(recent) < limit:
            return

        # антидубликат: не наказывать чаще раза в interval секунд
        last_action = self.recent_actions.get(key, 0)
        if now - last_action < interval:
            return
        self.recent_actions[key] = now

        action = settings["antispam_action"]
        await self._punish(
            message,
            settings,
            action=action,
            reason=f"Антиспам: {len(recent)} сообщений за {interval} сек",
            mute_seconds=settings["antispam_mute_seconds"],
            public_notice=f"🚨 {message.author.mention} нарушил антиспам-лимит и получил наказание `{action}`.",
            log_title="Антиспам сработал",
        )

        # Чистим лог за этого пользователя, чтобы избежать повторного срабатывания подряд
        self.message_log[key].clear()


async def setup(bot):
    await bot.add_cog(AntiSpam(bot))
