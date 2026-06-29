"""Хранение файлов бэкапа на диске.

Layout: backups/{guild_id}/{backup_id}.json — один сервер может иметь
несколько сохранённых бэкапов (а не один файл, перезаписываемый каждый раз,
как было раньше). Это и нужно для "истории" бэкапов, и пригодится для
будущего rollback (см. restore.py).

Совместимость со старым форматом: до этого обновления бэкап лежал прямо
в backups/{guild_id}.json и не имел ни ID, ни версии схемы, ни metadata —
просто {"guild_name", "roles", "channels"}. Такие файлы по-прежнему читаются:
migrate_legacy() оборачивает их в актуальную схему на лету. Сам legacy-файл
не трогаем (на случай, если что-то пошло не так при миграции) — новый бэкап
в актуальном формате появится рядом при следующем /save."""

from __future__ import annotations

import gzip
import json
import os
import time
import uuid

from .models import BackupData, BackupMetadata, RoleData, ChannelData, OverwriteData

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

# Новые бэкапы сжимаются gzip'ом — на сервере с сотней кастомных эмодзи/стикеров
# (картинки лежат в JSON как base64) файл легко вырастает до нескольких МБ,
# а текстовый JSON с повторяющимися ключами сжимается очень хорошо (обычно в 5-10 раз).
# Бэкапы, сохранённые ДО этого изменения (расширение .json, без сжатия), по-прежнему
# читаются как обычные JSON-файлы — см. _existing_backup_path()/_open_for_read().
_COMPRESSED_SUFFIX = ".json.gz"
_PLAIN_SUFFIX = ".json"


def _guild_dir(guild_id: int) -> str:
    path = os.path.join(BACKUP_DIR, str(guild_id))
    os.makedirs(path, exist_ok=True)
    return path


def _legacy_path(guild_id: int) -> str:
    return os.path.join(BACKUP_DIR, f"{guild_id}.json")


def generate_backup_id() -> str:
    """Короткий, но уникальный и сортируемый по времени ID: <unix-время>-<4 hex>.
    Сортируемость по имени файла удобна для list_backups (последний бэкап —
    последний по алфавиту/времени без отдельного индекса)."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _backup_path(guild_id: int, backup_id: str) -> str:
    """Путь для ЗАПИСИ нового бэкапа — всегда сжатый .json.gz."""
    return os.path.join(_guild_dir(guild_id), f"{backup_id}{_COMPRESSED_SUFFIX}")


def _existing_backup_path(guild_id: int, backup_id: str) -> str | None:
    """Путь для ЧТЕНИЯ — бэкап может быть как новым (сжатым), так и старым
    (несжатым, сохранённым до этого изменения). Возвращает None, если файла
    с таким backup_id вообще нет ни в каком виде."""
    compressed = os.path.join(_guild_dir(guild_id), f"{backup_id}{_COMPRESSED_SUFFIX}")
    if os.path.exists(compressed):
        return compressed
    plain = os.path.join(_guild_dir(guild_id), f"{backup_id}{_PLAIN_SUFFIX}")
    if os.path.exists(plain):
        return plain
    return None


def _open_for_read(path: str):
    """gzip.open сам определяет, что делать, по магическим байтам было бы
    надёжнее, но проще и достаточно ориентироваться на расширение файла —
    мы сами контролируем, как и с каким именем что записывается."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def migrate_legacy(raw: dict, guild_id: int) -> BackupData:
    """Превращает самый старый формат бэкапа (без metadata/version) в BackupData."""
    roles = [
        RoleData(
            name=r["name"],
            color=int(r.get("color", 0)),
            hoist=bool(r.get("hoist", False)),
            mentionable=bool(r.get("mentionable", False)),
            permissions=int(r.get("permissions", 0)),
            position=int(r.get("position", 0)),
        )
        for r in raw.get("roles", [])
    ]

    categories = []
    channels = []
    for c in raw.get("channels", []):
        overwrites = [OverwriteData.from_dict(o) for o in c.get("overwrites", [])]
        if c.get("type") == "category":
            from .models import CategoryData

            categories.append(CategoryData(name=c["name"], position=c.get("position", 0), overwrites=overwrites))
        else:
            channels.append(
                ChannelData(
                    name=c["name"],
                    type=c.get("type", "text"),
                    position=c.get("position", 0),
                    category_name=c.get("category"),
                    overwrites=overwrites,
                )
            )

    metadata = BackupMetadata(
        backup_id=f"legacy-{guild_id}",
        created_at="unknown",
        schema_version=1,
        guild_id=guild_id,
        guild_name=raw.get("guild_name", ""),
        counts={"roles": len(roles), "categories": len(categories), "channels": len(channels)},
        note="Импортирован из старого формата бэкапа (до версии с metadata).",
    )
    return BackupData(metadata=metadata, roles=roles, categories=categories, channels=channels)


def save(data: BackupData) -> str:
    """Пишет бэкап на диск (сжатым gzip'ом), возвращает его backup_id."""
    path = _backup_path(data.metadata.guild_id, data.metadata.backup_id)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data.to_dict(), f, ensure_ascii=False, indent=2)
    return data.metadata.backup_id


def list_backups(guild_id: int) -> list[str]:
    """ID всех бэкапов сервера, от старых к новым (по имени файла, оно сортируемо по времени).
    Распознаёт и новые сжатые файлы (.json.gz), и старые несжатые (.json)."""
    directory = os.path.join(BACKUP_DIR, str(guild_id))
    if not os.path.isdir(directory):
        return []
    ids = set()
    for fn in os.listdir(directory):
        if fn.endswith(_COMPRESSED_SUFFIX):
            ids.add(fn[: -len(_COMPRESSED_SUFFIX)])
        elif fn.endswith(_PLAIN_SUFFIX):
            ids.add(fn[: -len(_PLAIN_SUFFIX)])
    return sorted(ids)


def has_any_backup(guild_id: int) -> bool:
    return bool(list_backups(guild_id)) or os.path.exists(_legacy_path(guild_id))


def latest_backup_id(guild_id: int) -> str | None:
    ids = list_backups(guild_id)
    if ids:
        return ids[-1]
    if os.path.exists(_legacy_path(guild_id)):
        return f"legacy-{guild_id}"
    return None


def latest_emergency_backup_id(guild_id: int) -> str | None:
    """Последний по времени emergency-бэкап (автоматически созданный перед
    restore) — то, что используется командой rollback по умолчанию, если
    backup_id не указан явно."""
    for backup_id in reversed(list_backups(guild_id)):
        try:
            data = load(guild_id, backup_id)
        except (OSError, ValueError, KeyError):
            continue
        if data.metadata.is_emergency:
            return backup_id
    return None


def load(guild_id: int, backup_id: str) -> BackupData:
    if backup_id == f"legacy-{guild_id}" and _existing_backup_path(guild_id, backup_id) is None:
        with open(_legacy_path(guild_id), encoding="utf-8") as f:
            raw = json.load(f)
        return migrate_legacy(raw, guild_id)

    path = _existing_backup_path(guild_id, backup_id)
    if path is None:
        raise FileNotFoundError(f"Бэкап {backup_id} не найден для сервера {guild_id}")
    with _open_for_read(path) as f:
        raw = json.load(f)
    if "metadata" not in raw:
        return migrate_legacy(raw, guild_id)
    return BackupData.from_dict(raw)


def load_latest(guild_id: int) -> BackupData | None:
    backup_id = latest_backup_id(guild_id)
    if backup_id is None:
        return None
    return load(guild_id, backup_id)


# Сколько бэкапов хранить по умолчанию. Обычные и emergency считаются
# раздельно — иначе серия restore-операций (каждая создаёт emergency-бэкап)
# могла бы вымыть единственный "настоящий" бэкап сервера, сделанный руками.
MAX_REGULAR_BACKUPS = 10
MAX_EMERGENCY_BACKUPS = 5


def prune_old_backups(
    guild_id: int, *, keep_regular: int = MAX_REGULAR_BACKUPS, keep_emergency: int = MAX_EMERGENCY_BACKUPS
) -> int:
    """Удаляет старые бэкапы сверх лимита (legacy-файл не трогает — он не
    участвует в нумерации и его всего один). Возвращает количество удалённых файлов.

    Вызывается автоматически после каждого save() — то есть после обычного
    /save и после каждого emergency-бэкапа перед restore — так что лимит
    держится сам по себе, без отдельной команды очистки."""
    ids = list_backups(guild_id)  # от старых к новым
    regular, emergency = [], []
    for backup_id in ids:
        try:
            data = load(guild_id, backup_id)
        except (OSError, ValueError, KeyError):
            continue  # повреждённый/нечитаемый файл — не учитываем в ретеншене, не удаляем сам по себе
        (emergency if data.metadata.is_emergency else regular).append(backup_id)

    removed = 0
    for bucket, keep in ((regular, keep_regular), (emergency, keep_emergency)):
        excess = bucket[: max(0, len(bucket) - keep)]
        for backup_id in excess:
            path = _existing_backup_path(guild_id, backup_id)
            if path is None:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    return removed
