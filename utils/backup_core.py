"""Общая логика сохранения/восстановления структуры сервера.
Вынесена отдельно, чтобы её можно было использовать и из префиксных
команд (L.backup), и из слэш-команд (/save, /load)."""

import asyncio
import json
import os

import discord
BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)


async def _with_retry(coro_func, *args, retries: int = 3, base_delay: float = 1.5, **kwargs):
    """Повторяет вызов Discord API при ВРЕМЕННЫХ ошибках (5xx на стороне Discord),
    чтобы восстановление бэкапа не теряло роли/каналы из-за случайного
    кратковременного сбоя. Обычный rate-limit (429) discord.py уже обрабатывает
    сам внутри HTTP-клиента — здесь мы перехватываем только то, что осталось
    дойти до нас как исключение."""
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_func(*args, **kwargs)
        except discord.HTTPException as e:
            last_exc = e
            status = getattr(e, "status", 0)
            if status and status < 500:
                raise  # клиентская ошибка (400/403/404) — повторять бессмысленно
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc


def _backup_path(guild_id: int) -> str:
    return os.path.join(BACKUP_DIR, f"{guild_id}.json")


def has_backup(guild_id: int) -> bool:
    return os.path.exists(_backup_path(guild_id))


def get_backup_info(guild_id: int) -> dict | None:
    path = _backup_path(guild_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {"guild_name": data["guild_name"], "roles": len(data["roles"]), "channels": len(data["channels"])}


async def save_backup(guild: discord.Guild) -> dict:
    """Сохраняет роли/каналы/категории/права в JSON-файл. Возвращает счётчики."""
    data = {"guild_name": guild.name, "roles": [], "channels": []}

    for role in sorted(guild.roles, key=lambda r: r.position):
        if role.is_default():
            continue
        data["roles"].append(
            {
                "name": role.name,
                "permissions": role.permissions.value,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "position": role.position,
            }
        )

    for channel in sorted(guild.channels, key=lambda c: c.position):
        entry = {
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category": channel.category.name if channel.category else None,
            "overwrites": [],
        }
        for target, overwrite in channel.overwrites.items():
            entry["overwrites"].append(
                {
                    "target_name": target.name,
                    "target_type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": overwrite.pair()[0].value,
                    "deny": overwrite.pair()[1].value,
                }
            )
        data["channels"].append(entry)

    with open(_backup_path(guild.id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return {"roles": len(data["roles"]), "channels": len(data["channels"])}


async def restore_backup(guild: discord.Guild) -> dict:
    """Удаляет текущие роли/каналы и создаёт их заново из бэкапа.
    Вызывающий код отвечает за получение подтверждения у пользователя ДО вызова этой функции."""
    path = _backup_path(guild.id)
    if not os.path.exists(path):
        raise FileNotFoundError("Бэкап не найден")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    for channel in list(guild.channels):
        try:
            await channel.delete(reason="Восстановление из бэкапа")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            continue

    for role in list(guild.roles):
        if role.is_default() or role.managed:
            continue
        try:
            await role.delete(reason="Восстановление из бэкапа")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            continue

    name_to_role = {}
    created_roles = 0
    for r in data["roles"]:
        try:
            new_role = await _with_retry(
                guild.create_role,
                name=r["name"],
                permissions=discord.Permissions(r["permissions"]),
                color=discord.Color(r["color"]),
                hoist=r["hoist"],
                mentionable=r["mentionable"],
                reason="Восстановление из бэкапа",
            )
            name_to_role[r["name"]] = new_role
            created_roles += 1
        except discord.HTTPException:
            continue

    name_to_category = {}
    for c in data["channels"]:
        if c["type"] == "category":
            try:
                cat = await _with_retry(guild.create_category, c["name"], reason="Восстановление из бэкапа")
                name_to_category[c["name"]] = cat
            except discord.HTTPException:
                continue

    created_channels = len(name_to_category)
    for c in data["channels"]:
        if c["type"] == "category":
            continue
        category = name_to_category.get(c["category"]) if c["category"] else None
        overwrites = {}
        for ow in c["overwrites"]:
            target = name_to_role.get(ow["target_name"]) if ow["target_type"] == "role" else None
            if target is None:
                continue
            overwrites[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
            )

        try:
            if c["type"] == "voice":
                await _with_retry(
                    guild.create_voice_channel,
                    c["name"], category=category, overwrites=overwrites, reason="Восстановление из бэкапа",
                )
            else:
                await _with_retry(
                    guild.create_text_channel,
                    c["name"], category=category, overwrites=overwrites, reason="Восстановление из бэкапа",
                )
            created_channels += 1
        except discord.HTTPException:
            continue

    return {"roles": created_roles, "channels": created_channels}


async def notify_guild_or_dm(guild: discord.Guild, user: discord.abc.User, content: str, preferred=None):
    """Отправляет уведомление, устойчиво к тому, что исходный канал/сообщение
    могли быть удалены во время восстановления сервера (restore_backup удаляет
    ВСЕ текущие каналы, включая тот, где была вызвана команда).

    Порядок попыток: preferred (например interaction.followup.send) ->
    system_channel сервера -> первый доступный текстовый канал -> ЛС пользователю."""
    if preferred is not None:
        try:
            await preferred(content)
            return
        except discord.HTTPException:
            pass

    target = guild.system_channel
    if target is None:
        for c in guild.text_channels:
            target = c
            break

    if target is not None:
        try:
            await target.send(content)
            return
        except discord.HTTPException:
            pass

    try:
        await user.send(content)
    except discord.Forbidden:
        pass
