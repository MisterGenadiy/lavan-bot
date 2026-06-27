"""Верификация новых пользователей — простая защита от ботов-рейдеров без
капчи: новый участник получает ограничивающую роль и должен нажать кнопку
"Я не бот" в специальном канале, чтобы получить доступ к серверу.

Кнопка реализована как persistent View (custom_id фиксирован), поэтому
продолжает работать даже после перезапуска бота — её не нужно пересылать."""

import asyncio

import discord
from discord.ext import commands

from utils.moderation_utils import is_mod_or_admin, send_log

VERIFY_BUTTON_CUSTOM_ID = "lavan_verify_button_v1"


class VerifyView(discord.ui.View):
    """Persistent-view с одной кнопкой подтверждения. Регистрируется один раз
    в bot.py через bot.add_view(VerifyView(bot)) — после этого работает на
    любом сообщении с этой view, включая отправленные до перезапуска бота."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Я не бот, подтвердить",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=VERIFY_BUTTON_CUSTOM_ID,
    )
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return
        settings = self.bot.db.get_all_settings(interaction.guild.id)
        if not settings.get("verification_enabled"):
            return await interaction.response.send_message(
                "⚠️ Верификация на этом сервере сейчас выключена.", ephemeral=True
            )

        member = interaction.user
        unverified_role_id = settings.get("verification_unverified_role_id")
        verified_role_id = settings.get("verification_verified_role_id")
        changed = False

        if unverified_role_id:
            role = interaction.guild.get_role(unverified_role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Верификация пройдена")
                    changed = True
                except discord.Forbidden:
                    return await interaction.response.send_message(
                        "⛔ У бота не хватает прав, чтобы снять ограничивающую роль. "
                        "Сообщите об этом администратору сервера.",
                        ephemeral=True,
                    )

        if verified_role_id:
            role = interaction.guild.get_role(verified_role_id)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Верификация пройдена")
                    changed = True
                except discord.Forbidden:
                    pass

        if changed:
            await interaction.response.send_message("✅ Вы успешно подтверждены! Добро пожаловать 🎉", ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ Вы уже подтверждены.", ephemeral=True)


class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        settings = self.bot.db.get_all_settings(member.guild.id)
        if not settings.get("verification_enabled"):
            return

        role_id = settings.get("verification_unverified_role_id")
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Верификация: ограничение доступа до подтверждения")
                except discord.Forbidden:
                    await send_log(
                        self.bot,
                        member.guild,
                        discord.Embed(
                            description=(
                                f"⚠️ Не удалось выдать ограничивающую роль {role.mention} участнику "
                                f"{member.mention} — проверьте права бота и иерархию ролей."
                            ),
                            color=discord.Color.orange(),
                        ),
                    )

        timeout_minutes = settings.get("verification_timeout_minutes", 0)
        if timeout_minutes and timeout_minutes > 0:
            self.bot.loop.create_task(
                self._kick_if_not_verified(member.guild.id, member.id, timeout_minutes, role_id)
            )

    async def _kick_if_not_verified(self, guild_id: int, user_id: int, minutes: int, unverified_role_id):
        await asyncio.sleep(minutes * 60)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None:
            return  # уже вышел сам или был кикнут/забанен
        if unverified_role_id and not any(r.id == unverified_role_id for r in member.roles):
            return  # успел подтвердиться
        try:
            await member.kick(reason=f"Верификация: не подтвердился за {minutes} мин.")
            await send_log(
                self.bot,
                guild,
                discord.Embed(
                    description=f"⏱️ {member} кикнут — не прошёл верификацию за {minutes} мин.",
                    color=discord.Color.orange(),
                ),
            )
        except discord.Forbidden:
            pass

    @commands.command(name="verification-post")
    @is_mod_or_admin()
    async def verification_post(self, ctx: commands.Context):
        """Отправляет (повторно) сообщение с кнопкой верификации в настроенный канал."""
        settings = self.bot.db.get_all_settings(ctx.guild.id)
        channel_id = settings.get("verification_channel_id")
        channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        if channel is None:
            return await ctx.send(
                f"⚠️ Канал верификации не найден. Настройте его: `{settings['prefix']}verification-channel #канал`"
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
        await ctx.send(f"✅ Сообщение с верификацией отправлено в {channel.mention}.")


async def setup(bot):
    await bot.add_cog(Verification(bot))
