"""Применение плана восстановления (restore.apply_plan) и безопасная обёртка
вокруг всего процесса /load (restore.restore_with_safety).

Ключевые отличия от старой версии:
- По умолчанию ничего не удаляется (remove_extra=False) — план может
  содержать "remove"-пункты, но apply_plan их применяет только если это
  явно разрешено, что и есть защита от случайных деструктивных операций.
- Перед любым restore автоматически создаётся emergency-бэкап текущего
  состояния сервера (create_emergency_backup) — id этого бэкапа возвращается
  вызывающему коду, чтобы при необходимости откатиться (rollback) — то есть
  выполнить ещё один restore с этим backup_id и scope=ALL.
- Ошибки Discord API по каждой сущности не прерывают весь процесс — одна
  неудачная роль/канал не должна обрушивать восстановление остальных сотен.
- На больших серверах (сотни ролей/каналов) применение плана может занимать
  заметное время — apply_plan поддерживает progress_cb(done, total) для
  показа прогресса пользователю, не дожидаясь полного завершения."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import discord

from . import capture, storage
from .models import (
    ACTION_CONFLICT,
    ACTION_CREATE,
    ACTION_REMOVE,
    ACTION_UPDATE,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_EMOJI,
    KIND_GUILD_SETTINGS,
    KIND_ROLE,
    KIND_STICKER,
    BackupData,
    PlanItem,
    RestorePlan,
    RestoreScope,
)
from .retry import with_retry

ProgressCallback = Callable[[int, int], Awaitable[None]]

_VERIFICATION_LEVELS = {lvl.name: lvl for lvl in discord.VerificationLevel}
_CONTENT_FILTERS = {f.name: f for f in discord.ContentFilter}
_NOTIFICATION_LEVELS = {n.name: n for n in discord.NotificationLevel}


@dataclass
class RestoreResult:
    """Итог применения плана — то, что показывается пользователю после /load."""

    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)
    skipped_conflicts: int = 0
    errors: list[str] = field(default_factory=list)

    def _bump(self, bucket: dict[str, int], kind: str):
        bucket[kind] = bucket.get(kind, 0) + 1

    def total_created(self) -> int:
        return sum(self.created.values())

    def total_updated(self) -> int:
        return sum(self.updated.values())

    def total_removed(self) -> int:
        return sum(self.removed.values())


def _overwrites_to_discord(backup_overwrites, guild: discord.Guild) -> dict:
    result = {}
    for ow in backup_overwrites:
        target = None
        if ow.target_type == "role":
            target = discord.utils.find(lambda r: r.name == ow.target_name, guild.roles)
        if target is None:
            continue  # роль ещё не создана / переименована — пропускаем это конкретное правило, не всё восстановление
        result[target] = discord.PermissionOverwrite.from_pair(discord.Permissions(ow.allow), discord.Permissions(ow.deny))
    return result


async def _apply_role(guild: discord.Guild, item: PlanItem, result: RestoreResult):
    br = item.backup_obj
    if item.action == ACTION_CREATE:
        try:
            await with_retry(
                guild.create_role,
                name=br.name,
                permissions=discord.Permissions(br.permissions),
                colour=discord.Colour(br.color),
                hoist=br.hoist,
                mentionable=br.mentionable,
                reason="Восстановление из бэкапа",
            )
            result._bump(result.created, KIND_ROLE)
        except discord.HTTPException as e:
            result.errors.append(f"Роль «{br.name}»: {e}")
    elif item.action == ACTION_UPDATE:
        role = guild.get_role(item.current_id)
        if role is None:
            return
        try:
            await with_retry(
                role.edit,
                permissions=discord.Permissions(br.permissions),
                colour=discord.Colour(br.color),
                hoist=br.hoist,
                mentionable=br.mentionable,
                reason="Восстановление из бэкапа (обновление)",
            )
            result._bump(result.updated, KIND_ROLE)
        except discord.HTTPException as e:
            result.errors.append(f"Роль «{br.name}»: {e}")
    elif item.action == ACTION_REMOVE:
        role = guild.get_role(item.current_id)
        if role is None:
            return
        try:
            await with_retry(role.delete, reason="Восстановление из бэкапа (роли нет в бэкапе)")
            result._bump(result.removed, KIND_ROLE)
        except discord.HTTPException as e:
            result.errors.append(f"Роль «{br.name}»: {e}")


async def _apply_category(guild: discord.Guild, item: PlanItem, result: RestoreResult, name_to_category: dict):
    bc = item.backup_obj
    if item.action == ACTION_CREATE:
        try:
            overwrites = _overwrites_to_discord(bc.overwrites, guild)
            cat = await with_retry(guild.create_category, bc.name, overwrites=overwrites, reason="Восстановление из бэкапа")
            name_to_category[bc.name] = cat
            result._bump(result.created, KIND_CATEGORY)
        except discord.HTTPException as e:
            result.errors.append(f"Категория «{bc.name}»: {e}")
    elif item.action == ACTION_UPDATE:
        cat = guild.get_channel(item.current_id)
        if cat is None:
            return
        try:
            overwrites = _overwrites_to_discord(bc.overwrites, guild)
            await with_retry(cat.edit, overwrites=overwrites, reason="Восстановление из бэкапа (обновление прав)")
            result._bump(result.updated, KIND_CATEGORY)
        except discord.HTTPException as e:
            result.errors.append(f"Категория «{bc.name}»: {e}")
    elif item.action == ACTION_REMOVE:
        cat = guild.get_channel(item.current_id)
        if cat is None:
            return
        try:
            await with_retry(cat.delete, reason="Восстановление из бэкапа (категории нет в бэкапе)")
            result._bump(result.removed, KIND_CATEGORY)
        except discord.HTTPException as e:
            result.errors.append(f"Категория «{bc.name}»: {e}")


_CREATORS = {
    "text": "create_text_channel",
    "announcement": "create_text_channel",  # is_news выставляется отдельным edit ниже
    "voice": "create_voice_channel",
    "stage": "create_stage_channel",
    "forum": "create_forum",
}


async def _create_channel(guild: discord.Guild, bch, category, overwrites: dict):
    creator_name = _CREATORS.get(bch.type, "create_text_channel")
    creator = getattr(guild, creator_name)
    kwargs = {"category": category, "overwrites": overwrites, "reason": "Восстановление из бэкапа"}
    if bch.type in ("text", "announcement", "forum"):
        kwargs["topic"] = bch.topic
        kwargs["nsfw"] = bch.nsfw
        if bch.type != "forum":
            kwargs["slowmode_delay"] = bch.slowmode_delay
    if bch.type == "announcement":
        kwargs["news"] = True
    if bch.type == "voice" and bch.bitrate:
        kwargs["bitrate"] = bch.bitrate
    if bch.type in ("voice", "stage") and bch.user_limit:
        kwargs["user_limit"] = bch.user_limit

    return await with_retry(creator, bch.name, **kwargs)


async def _apply_channel(guild: discord.Guild, item: PlanItem, result: RestoreResult, name_to_category: dict):
    bch = item.backup_obj
    if item.action == ACTION_CREATE:
        category = name_to_category.get(bch.category_name) if bch.category_name else None
        if category is None and bch.category_name:
            category = discord.utils.find(lambda c: c.name == bch.category_name, guild.categories)
        overwrites = _overwrites_to_discord(bch.overwrites, guild)
        try:
            await _create_channel(guild, bch, category, overwrites)
            result._bump(result.created, KIND_CHANNEL)
        except discord.HTTPException as e:
            result.errors.append(f"Канал «{bch.name}»: {e}")
    elif item.action == ACTION_UPDATE:
        channel = guild.get_channel(item.current_id)
        if channel is None:
            return
        try:
            edit_kwargs = {"overwrites": _overwrites_to_discord(bch.overwrites, guild)}
            if hasattr(channel, "topic"):
                edit_kwargs["topic"] = bch.topic
            if hasattr(channel, "nsfw"):
                edit_kwargs["nsfw"] = bch.nsfw
            if hasattr(channel, "slowmode_delay"):
                edit_kwargs["slowmode_delay"] = bch.slowmode_delay
            await with_retry(channel.edit, reason="Восстановление из бэкапа (обновление)", **edit_kwargs)
            result._bump(result.updated, KIND_CHANNEL)
        except discord.HTTPException as e:
            result.errors.append(f"Канал «{bch.name}»: {e}")
    elif item.action == ACTION_REMOVE:
        channel = guild.get_channel(item.current_id)
        if channel is None:
            return
        try:
            await with_retry(channel.delete, reason="Восстановление из бэкапа (канала нет в бэкапе)")
            result._bump(result.removed, KIND_CHANNEL)
        except discord.HTTPException as e:
            result.errors.append(f"Канал «{bch.name}»: {e}")


async def _apply_emoji(guild: discord.Guild, item: PlanItem, result: RestoreResult):
    e = item.backup_obj
    if e.image_b64 is None:
        result.errors.append(f"Эмодзи «{e.name}»: в бэкапе нет изображения (не удалось скачать на момент сохранения).")
        return
    try:
        image_bytes = base64.b64decode(e.image_b64)
        await with_retry(guild.create_custom_emoji, name=e.name, image=image_bytes, reason="Восстановление из бэкапа")
        result._bump(result.created, KIND_EMOJI)
    except discord.HTTPException as e_:
        result.errors.append(f"Эмодзи «{e.name}»: {e_}")


async def _apply_sticker(guild: discord.Guild, item: PlanItem, result: RestoreResult):
    s = item.backup_obj
    if s.image_b64 is None:
        result.errors.append(f"Стикер «{s.name}»: в бэкапе нет изображения (не удалось скачать на момент сохранения).")
        return
    try:
        image_bytes = base64.b64decode(s.image_b64)
        await with_retry(
            guild.create_sticker,
            name=s.name,
            description=s.description,
            emoji=s.emoji,
            file=discord.File(fp=__import__("io").BytesIO(image_bytes), filename=f"{s.name}.png"),
            reason="Восстановление из бэкапа",
        )
        result._bump(result.created, KIND_STICKER)
    except discord.HTTPException as e:
        result.errors.append(f"Стикер «{s.name}»: {e}")


async def _apply_guild_settings(guild: discord.Guild, item: PlanItem, result: RestoreResult):
    gs = item.backup_obj
    kwargs = {}
    if gs.verification_level and gs.verification_level in _VERIFICATION_LEVELS:
        kwargs["verification_level"] = _VERIFICATION_LEVELS[gs.verification_level]
    if gs.explicit_content_filter and gs.explicit_content_filter in _CONTENT_FILTERS:
        kwargs["explicit_content_filter"] = _CONTENT_FILTERS[gs.explicit_content_filter]
    if gs.default_notifications and gs.default_notifications in _NOTIFICATION_LEVELS:
        kwargs["default_notifications"] = _NOTIFICATION_LEVELS[gs.default_notifications]
    if not kwargs:
        return
    try:
        await with_retry(guild.edit, reason="Восстановление из бэкапа (настройки сервера)", **kwargs)
        result._bump(result.updated, KIND_GUILD_SETTINGS)
    except discord.HTTPException as e:
        result.errors.append(f"Настройки сервера: {e}")


async def apply_plan(guild: discord.Guild, plan: RestorePlan, *, progress_cb: ProgressCallback | None = None) -> RestoreResult:
    """Применяет план. Порядок важен: роли -> категории -> каналы -> эмодзи/стикеры
    -> настройки сервера, иначе permission overwrites не на что будет ссылаться
    (роль для overwrite должна существовать до создания канала).

    progress_cb(done, total), если передан, вызывается после каждого пункта плана —
    на больших серверах (сотни ролей/каналов) восстановление может идти минуты,
    и пользователю важно видеть, что процесс не "завис". Ошибка внутри
    progress_cb (например, Discord отверг слишком частый edit сообщения)
    не должна прерывать само восстановление — поэтому она проглатывается."""
    result = RestoreResult()
    name_to_category: dict[str, discord.CategoryChannel] = {c.name: c for c in guild.categories}
    total = len(plan.items)

    for done, item in enumerate(plan.items, start=1):
        if item.action == ACTION_CONFLICT:
            result.skipped_conflicts += 1
        elif item.kind == KIND_ROLE:
            await _apply_role(guild, item, result)
        elif item.kind == KIND_CATEGORY:
            await _apply_category(guild, item, result, name_to_category)
        elif item.kind == KIND_CHANNEL:
            await _apply_channel(guild, item, result, name_to_category)
        elif item.kind == KIND_EMOJI:
            await _apply_emoji(guild, item, result)
        elif item.kind == KIND_STICKER:
            await _apply_sticker(guild, item, result)
        elif item.kind == KIND_GUILD_SETTINGS:
            await _apply_guild_settings(guild, item, result)

        if progress_cb is not None:
            try:
                await progress_cb(done, total)
            except Exception:
                pass

    return result


def make_throttled_progress_callback(edit_func: Callable[[str], Awaitable[None]], *, min_interval: float = 4.0):
    """Оборачивает edit_func (например, status_message.edit или
    interaction.edit_original_response) в progress_cb, который реально шлёт
    запрос в Discord не чаще, чем раз в min_interval секунд — иначе на каждую
    из сотен ролей/каналов улетал бы отдельный edit и упёрся бы в rate limit
    Discord. Последний пункт плана (done == total) обновляется всегда,
    независимо от таймера, чтобы финальное сообщение не зависло на 80%."""
    state = {"last": float("-inf")}  # -inf гарантирует, что первый вызов всегда пройдёт,
    # независимо от абсолютного значения time.monotonic() в конкретной системе/контейнере

    async def progress_cb(done: int, total: int):
        now = time.monotonic()
        if done < total and now - state["last"] < min_interval:
            return
        state["last"] = now
        percent = int(done / total * 100) if total else 100
        await edit_func(f"⏳ Применяется план восстановления: {done}/{total} ({percent}%)...")

    return progress_cb


async def create_emergency_backup(guild: discord.Guild) -> str:
    """Снимок текущего состояния сервера ПЕРЕД восстановлением — подготовка
    к rollback: если /load что-то испортит, этим backup_id можно воспользоваться
    как обычным бэкапом и восстановиться обратно тем же механизмом."""
    data = await capture.capture_guild(guild, is_emergency=True, note="Авто-бэкап перед восстановлением")
    backup_id = storage.save(data)
    storage.prune_old_backups(guild.id)
    return backup_id


async def restore_with_safety(
    guild: discord.Guild,
    *,
    backup_id: str | None = None,
    scope: RestoreScope = RestoreScope.all(),
    remove_extra: bool = False,
    skip_emergency_backup: bool = False,
    progress_cb: ProgressCallback | None = None,
) -> tuple[RestorePlan, RestoreResult, str | None]:
    """Полный безопасный цикл восстановления:
    1) emergency-бэкап текущего состояния (если не отключён явно);
    2) построение плана (что изменится);
    3) применение плана (с опциональным progress_cb для прогресса на больших серверах).

    Возвращает (план, результат, id emergency-бэкапа или None)."""
    target_id = backup_id or storage.latest_backup_id(guild.id)
    if target_id is None:
        raise FileNotFoundError("Бэкап не найден")
    backup = storage.load(guild.id, target_id)

    emergency_id = None if skip_emergency_backup else await create_emergency_backup(guild)

    plan = diff_build_plan(guild, backup, scope=scope, remove_extra=remove_extra)
    result = await apply_plan(guild, plan, progress_cb=progress_cb)
    return plan, result, emergency_id


def diff_build_plan(guild: discord.Guild, backup: BackupData, *, scope: RestoreScope, remove_extra: bool) -> RestorePlan:
    """Тонкая обёртка над diff.build_plan — импорт лениво, чтобы не плодить
    циклические зависимости между restore.py и diff.py (diff.py импортирует
    capture._channel_type_str, а capture.py не зависит от restore.py)."""
    from . import diff

    return diff.build_plan(guild, backup, scope=scope, remove_extra=remove_extra)
