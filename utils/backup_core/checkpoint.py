"""Чекпоинты восстановления.

Раньше падение бота (краш, перезапуск при деплое) посередине /load на
большом сервере означало, что нужно начинать восстановление заново —
никакого состояния о том, что уже было создано/обновлено, не сохранялось.

Теперь restore_with_safety() сам, по ходу применения плана, периодически
пишет чекпоинт на диск: какие пункты плана уже применены и какой
промежуточный результат накопился. Если бот упал и перезапустился,
`L.backup resume [id]` / `/resume` подхватывает чекпоинт и продолжает
с того места, не повторяя уже сделанное и не теряя накопленную статистику.

Чекпоинт хранится как обычный (несжатый) JSON — он временный, небольшой и
живёт недолго (удаляется сразу после успешного завершения), в отличие от
самих бэкапов: backups/{guild_id}/_checkpoints/{checkpoint_id}.json"""

from __future__ import annotations

import json
import os
import time
import uuid

from . import storage
from .models import (
    KIND_AUTOMOD,
    KIND_CATEGORY,
    KIND_CHANNEL,
    KIND_EMOJI,
    KIND_GUILD_SETTINGS,
    KIND_ROLE,
    KIND_STICKER,
    KIND_WEBHOOK,
    AutoModRuleData,
    CategoryData,
    ChannelData,
    EmojiData,
    GuildSettingsData,
    PlanItem,
    RestorePlan,
    RestoreScope,
    RoleData,
    StickerData,
    WebhookData,
)

# kind -> датакласс, в который десериализуется PlanItem.backup_obj при
# восстановлении плана из чекпоинта (тот же принцип, что у BackupData.from_dict).
_KIND_TO_DATACLASS = {
    KIND_ROLE: RoleData,
    KIND_CATEGORY: CategoryData,
    KIND_CHANNEL: ChannelData,
    KIND_EMOJI: EmojiData,
    KIND_STICKER: StickerData,
    KIND_GUILD_SETTINGS: GuildSettingsData,
    KIND_AUTOMOD: AutoModRuleData,
    KIND_WEBHOOK: WebhookData,
}


def _checkpoints_dir(guild_id: int) -> str:
    path = os.path.join(storage.BACKUP_DIR, str(guild_id), "_checkpoints")
    os.makedirs(path, exist_ok=True)
    return path


def generate_checkpoint_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _path(guild_id: int, checkpoint_id: str) -> str:
    return os.path.join(_checkpoints_dir(guild_id), f"{checkpoint_id}.json")


def _serialize_item(item: PlanItem) -> dict:
    return {
        "kind": item.kind,
        "action": item.action,
        "name": item.name,
        "details": item.details,
        "current_id": item.current_id,
        "backup_obj": item.backup_obj.to_dict() if item.backup_obj is not None else None,
    }


def _deserialize_item(d: dict) -> PlanItem:
    backup_obj = None
    if d.get("backup_obj") is not None:
        cls = _KIND_TO_DATACLASS.get(d["kind"])
        if cls is not None:
            backup_obj = cls.from_dict(d["backup_obj"])
    return PlanItem(
        kind=d["kind"],
        action=d["action"],
        name=d["name"],
        details=d.get("details", ""),
        current_id=d.get("current_id"),
        backup_obj=backup_obj,
    )


def _write(guild_id: int, checkpoint_id: str, data: dict):
    with open(_path(guild_id, checkpoint_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def create(guild_id: int, plan: RestorePlan, *, emergency_backup_id: str | None) -> str:
    """Новый чекпоинт для плана, который только начинает применяться —
    все пункты отмечены как невыполненные."""
    checkpoint_id = generate_checkpoint_id()
    data = {
        "checkpoint_id": checkpoint_id,
        "created_at": time.time(),
        "guild_id": guild_id,
        "backup_id": plan.backup_id,
        "scope": plan.scope.value,
        "remove_extra": plan.remove_extra,
        "emergency_backup_id": emergency_backup_id,
        "items": [_serialize_item(i) for i in plan.items],
        "done": [False] * len(plan.items),
        "result": None,
    }
    _write(guild_id, checkpoint_id, data)
    return checkpoint_id


def load(guild_id: int, checkpoint_id: str) -> dict:
    with open(_path(guild_id, checkpoint_id), encoding="utf-8") as f:
        return json.load(f)


def delete(guild_id: int, checkpoint_id: str):
    try:
        os.remove(_path(guild_id, checkpoint_id))
    except OSError:
        pass


def list_checkpoints(guild_id: int) -> list[dict]:
    """Метаданные незавершённых чекпоинтов сервера (без полного списка items —
    он может быть большим, а для выбора пользователю это не нужно)."""
    directory = _checkpoints_dir(guild_id)
    if not os.path.isdir(directory):
        return []
    result = []
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        done_count = sum(1 for d in data.get("done", []) if d)
        total = len(data.get("items", []))
        result.append(
            {
                "checkpoint_id": data["checkpoint_id"],
                "created_at": data["created_at"],
                "backup_id": data.get("backup_id"),
                "progress": f"{done_count}/{total}",
            }
        )
    return result


def to_plan(data: dict) -> RestorePlan:
    items = [_deserialize_item(d) for d in data["items"]]
    return RestorePlan(
        backup_id=data["backup_id"],
        scope=RestoreScope(data["scope"]),
        remove_extra=data["remove_extra"],
        items=items,
    )


def done_indices(data: dict) -> set[int]:
    return {i for i, flag in enumerate(data.get("done", [])) if flag}


class CheckpointWriter:
    """Записывает прогресс по ходу apply_plan(). Каждый отдельный пункт плана
    отмечается выполненным в памяти немедленно, но реальная запись на диск
    троттлится по времени (как progress_cb в restore.py) — иначе на сервере
    с сотнями ролей/каналов мы писали бы файл на каждый отдельный пункт,
    что и медленно, и избыточно: если бот упадёт между двумя записями,
    максимум что теряется — пункты за последние min_interval секунд,
    которые при resume просто будут переприменены (см. apply_plan: повторное
    применение create/update идемпотентно в худшем случае создаёт дубль,
    который найдётся через /find-duplicates)."""

    def __init__(self, guild_id: int, checkpoint_id: str, data: dict, *, min_interval: float = 2.0):
        self.guild_id = guild_id
        self.checkpoint_id = checkpoint_id
        self.data = data
        self.min_interval = min_interval
        self._last_write = float("-inf")

    def record(self, index: int, result) -> None:
        self.data["done"][index] = True
        self.data["result"] = {
            "created": dict(result.created),
            "updated": dict(result.updated),
            "removed": dict(result.removed),
            "skipped_conflicts": result.skipped_conflicts,
            "errors": list(result.errors),
        }
        now = time.monotonic()
        if now - self._last_write >= self.min_interval:
            self._last_write = now
            _write(self.guild_id, self.checkpoint_id, self.data)

    def flush(self) -> None:
        """Принудительная запись на диск независимо от троттлинга — вызывается
        в конце восстановления (успешном или с ошибкой), чтобы финальное
        состояние точно попало на диск, а не осталось только в памяти."""
        _write(self.guild_id, self.checkpoint_id, self.data)

    def delete(self) -> None:
        delete(self.guild_id, self.checkpoint_id)


def result_from_checkpoint(data: dict):
    """Восстанавливает RestoreResult с накопленными за все попытки счётчиками —
    чтобы после resume пользователь видел ОБЩИЙ итог, а не только то, что
    применилось именно в последнем запуске."""
    from .restore import RestoreResult  # локальный импорт — избегаем циклической зависимости

    raw = data.get("result") or {}
    result = RestoreResult()
    result.created = dict(raw.get("created", {}))
    result.updated = dict(raw.get("updated", {}))
    result.removed = dict(raw.get("removed", {}))
    result.skipped_conflicts = raw.get("skipped_conflicts", 0)
    result.errors = list(raw.get("errors", []))
    return result
