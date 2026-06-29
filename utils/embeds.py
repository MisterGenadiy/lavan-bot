"""Общие embed-builders, чтобы не дублировать код между префиксными и слэш-командами."""

import discord

from utils.backup_core.models import RestorePlan

_KIND_LABELS = {
    "role": "Роль",
    "category": "Категория",
    "channel": "Канал",
    "emoji": "Эмодзи",
    "sticker": "Стикер",
    "guild_settings": "Настройки сервера",
}

_MAX_LINES_PER_FIELD = 12
_FIELD_VALUE_LIMIT = 1024  # жёсткий лимит Discord на длину value у поля embed'а


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_items(items, *, with_details: bool = False) -> str:
    """Список пунктов плана в одно поле embed'а, гарантированно укладывающийся
    в лимит Discord (1024 символа на value) — раньше это не проверялось,
    и /load падал с `Invalid Form Body` на серверах с длинными описаниями
    конфликтов или большим количеством каналов/ролей."""
    if not items:
        return "—"

    # Резервируем место под завершающую строку "… и ещё N." — она добавляется
    # либо потому что строк больше, чем _MAX_LINES_PER_FIELD, либо потому что
    # мы упёрлись в лимит символов раньше.
    budget = _FIELD_VALUE_LIMIT - 24

    lines: list[str] = []
    used = 0
    for item in items[:_MAX_LINES_PER_FIELD]:
        label = _KIND_LABELS.get(item.kind, item.kind)
        line = f"**{label}** «{item.name}»"
        if with_details and item.details:
            line += f" — {item.details}"
        line = _truncate(line, 200)  # одна длинная строка не должна съедать всё поле

        added_len = len(line) + (1 if lines else 0)  # +1 за "\n" перед строкой, кроме первой
        if used + added_len > budget:
            break
        lines.append(line)
        used += added_len

    remaining = len(items) - len(lines)
    if remaining > 0:
        lines.append(f"… и ещё {remaining}.")

    return _truncate("\n".join(lines), _FIELD_VALUE_LIMIT)


def build_restore_plan_embed(plan: RestorePlan, guild_name: str) -> discord.Embed:
    """План восстановления (/load, L.backup restore) перед подтверждением —
    что именно изменится, чтобы пользователь не восстанавливал сервер наугад."""
    color = discord.Color.orange() if (plan.conflicts or plan.removes) else discord.Color.blurple()
    embed = discord.Embed(
        title="📋 План восстановления",
        description=f"Сервер: **{guild_name}**\nБэкап: `{plan.backup_id}`",
        color=color,
    )
    embed.add_field(name=f"🟢 Создать ({len(plan.creates)})", value=_format_items(plan.creates), inline=False)
    embed.add_field(
        name=f"🟡 Обновить ({len(plan.updates)})", value=_format_items(plan.updates, with_details=True), inline=False
    )
    if plan.remove_extra:
        embed.add_field(
            name=f"🔴 Удалить ({len(plan.removes)})", value=_format_items(plan.removes, with_details=True), inline=False
        )
    elif plan.removes:
        embed.add_field(
            name=f"⚪ Не в бэкапе, но останется ({len(plan.removes)})",
            value="Удаление лишнего отключено — эти элементы не будут тронуты.",
            inline=False,
        )
    if plan.conflicts:
        embed.add_field(
            name=f"⚠️ Конфликты, требуют ручного решения ({len(plan.conflicts)})",
            value=_format_items(plan.conflicts, with_details=True),
            inline=False,
        )
    if plan.is_empty:
        embed.description += "\n\n✅ Текущее состояние сервера уже совпадает с бэкапом — изменений не требуется."
    return embed


def format_missing_permissions_warning(missing: list[str]) -> str:
    """Текст-предупреждение, если у бота не хватает прав для выбранной области
    восстановления — показывается ДО подтверждения, а не выясняется по ходу
    restore через малопонятные 403 от Discord API."""
    if not missing:
        return ""
    return (
        f"⚠️ У бота не хватает прав: **{', '.join(missing)}**. "
        "Соответствующая часть плана не применится, пока права не выданы роли бота.\n\n"
    )


def build_backup_action_log_embed(action: str, user, description: str) -> discord.Embed:
    """Запись для mod-log-канала о действии с бэкапом (/save, /load, /rollback
    и их префиксные аналоги) — кто и когда трогал структуру сервера через
    бэкап, важно для аудита независимо от того, как прошла сама операция."""
    icons = {"save": "💾", "load": "♻️", "rollback": "⏪", "clone": "🧬"}
    embed = discord.Embed(
        title=f"{icons.get(action, '💾')} Бэкап: {action}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Инициатор: {user}")
    return embed


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
