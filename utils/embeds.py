"""Общие embed-builders, чтобы не дублировать код между префиксными и слэш-командами."""

import discord


def build_settings_embed(bot, guild: discord.Guild) -> discord.Embed:
    s = bot.db.get_all_settings(guild.id)
    log_ch = guild.get_channel(s["log_channel_id"]) if s["log_channel_id"] else None
    mod_log_ch = guild.get_channel(s["mod_log_channel_id"]) if s["mod_log_channel_id"] else None
    ignore_channels = [guild.get_channel(cid) for cid in s.get("ignore_spam_channels", [])]
    ignore_channels = [c.mention for c in ignore_channels if c is not None]

    embed = discord.Embed(title="⚙️ Текущие настройки", color=discord.Color.blurple())
    embed.add_field(name="Префикс", value=f"`{s['prefix']}`", inline=True)
    embed.add_field(name="Лог-канал", value=log_ch.mention if log_ch else "не настроен", inline=True)
    embed.add_field(
        name="Канал мод-логов", value=mod_log_ch.mention if mod_log_ch else "не настроен", inline=True
    )
    embed.add_field(
        name="Антиспам",
        value=(
            f"{'✅ включён' if s['antispam_enabled'] else '❌ выключен'} "
            f"(чувствительность: `{s['antispam_sensitivity']}`)\n"
            f"Лимит: {s['antispam_msg_limit']} сообщений / {s['antispam_interval']} сек\n"
            f"Наказание: `{s['antispam_action']}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Антилинк",
        value=(
            f"{'✅ включён' if s['antilink_enabled'] else '❌ выключен'}\n"
            f"Наказание: `{s['antilink_action']}`"
            + (f" ({s['antilink_mute_seconds']} сек)" if s['antilink_action'] == "mute" else "")
        ),
        inline=False,
    )
    embed.add_field(
        name="Игнор-каналы антиспама/антилинка",
        value=", ".join(ignore_channels) if ignore_channels else "нет",
        inline=False,
    )
    embed.add_field(
        name="Антирейд",
        value=(
            f"{'✅ включён' if s['antiraid_enabled'] else '❌ выключен'}\n"
            f"Лимит: {s['antiraid_join_limit']} вступлений / {s['antiraid_interval']} сек\n"
            f"Наказание: `{s['antiraid_action']}`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Бан новых аккаунтов",
        value=(
            f"{'✅ включён' if s['ban_new_users_enabled'] else '❌ выключен'}\n"
            f"Мин. возраст аккаунта: {s['ban_new_users_min_age_hours']} ч.\n"
            f"Описание: {s['ban_new_users_description']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Изоляция новых ботов",
        value="✅ включена" if s["isolate_new_bots_enabled"] else "❌ выключена",
        inline=False,
    )
    embed.add_field(
        name="Анти-краш (защита от нюка)",
        value=(
            f"{'✅ включён' if s['anticrash_enabled'] else '❌ выключен'}\n"
            f"Порог: {s['anticrash_threshold']} удалений / {s['anticrash_interval']} сек"
        ),
        inline=False,
    )
    embed.add_field(
        name="Анти-mention-спам",
        value=(
            f"{'✅ включён' if s.get('antimention_enabled') else '❌ выключен'}\n"
            f"Лимит: {s.get('antimention_limit', 5)} упоминаний в сообщении\n"
            f"Наказание: `{s.get('antimention_action', 'mute')}`"
        ),
        inline=False,
    )
    unverified_role = guild.get_role(s.get("verification_unverified_role_id")) if s.get("verification_unverified_role_id") else None
    verified_role = guild.get_role(s.get("verification_verified_role_id")) if s.get("verification_verified_role_id") else None
    verification_channel = guild.get_channel(s.get("verification_channel_id")) if s.get("verification_channel_id") else None
    embed.add_field(
        name="Верификация новых участников",
        value=(
            f"{'✅ включена' if s.get('verification_enabled') else '❌ выключена'}\n"
            f"Канал: {verification_channel.mention if verification_channel else 'не настроен'}\n"
            f"Роль до подтверждения: {unverified_role.mention if unverified_role else 'не настроена'}\n"
            f"Роль после подтверждения: {verified_role.mention if verified_role else '—'}\n"
            f"Таймаут на кик: {s.get('verification_timeout_minutes', 0) or 'без ограничения'}"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Используйте {s['prefix']}help или /info для списка команд")
    return embed
