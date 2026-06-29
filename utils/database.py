"""
Простая обёртка над SQLite для хранения настроек серверов,
варнов, авто-наказаний и т.д.

Архитектурные улучшения:
- WAL-режим + busy_timeout — меньше блокировок при параллельных запросах.
- Кэш настроек в памяти: get_all_settings() вызывается практически на КАЖДОЕ
  сообщение на сервере (антиспам/антилинк), а раньше это означало SQLite-запрос
  + json.loads на каждое сообщение. Теперь читаем из кэша, а в БД идём только
  при первом обращении к серверу или после изменения настроек (set_setting
  инвалидирует кэш для этого guild_id).
- Индекс на warns(guild_id, user_id) — ускоряет get_warns/add_warn на серверах
  с большой историей предупреждений.
- Убрана неиспользуемая таблица join_log (нигде не читалась/не писалась —
  антирейд использует только in-memory очередь вступлений).
"""

import json
import sqlite3
import threading
import time
from typing import Any, Optional


class Database:
    def __init__(self, path: str = "data.sqlite3"):
        self._path = path
        self._lock = threading.RLock()  # RLock, т.к. set_setting вызывает get_all_settings рекурсивно
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()
        # Кэш настроек по guild_id. Инвалидируется в set_setting()/ensure_guild().
        self._settings_cache: dict[int, dict] = {}

    def _init_schema(self):
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                settings_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER,
                reason TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_warns_guild_user ON warns(guild_id, user_id);

            CREATE TABLE IF NOT EXISTS warn_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                warn_count INTEGER NOT NULL,
                action TEXT NOT NULL,
                duration_seconds INTEGER,
                UNIQUE(guild_id, warn_count)
            );
            """
        )
        # Старая таблица join_log больше не используется — удаляем, если осталась
        # от предыдущих версий бота (DROP TABLE IF EXISTS безопасен и идемпотентен).
        cur.execute("DROP TABLE IF EXISTS join_log")
        self._conn.commit()

    # ---------- Настройки сервера ----------

    DEFAULT_SETTINGS = {
        "prefix": "L.",
        "log_channel_id": None,
        "antispam_enabled": True,
        "antispam_msg_limit": 6,        # сообщений
        "antispam_interval": 6,         # за N секунд
        "antispam_action": "mute",      # warn|mute|kick|ban
        "antispam_mute_seconds": 300,
        "antiraid_enabled": True,
        "antiraid_join_limit": 8,       # вступлений
        "antiraid_interval": 10,        # за N секунд
        "antiraid_action": "lockdown",  # lockdown|kick|ban
        "antiraid_min_account_age_hours": 0,  # 0 = выключено
        "anticrash_enabled": True,      # защита от мульти-удаления каналов/ролей
        "anticrash_threshold": 3,       # удалений за интервал -> бан виновника
        "anticrash_interval": 10,
        "antispam_sensitivity": "medium",   # low|medium|high (пресет лимит/интервал)
        "antilink_enabled": False,
        "antilink_action": "warn",      # warn|mute|kick|ban|delete
        "antilink_mute_seconds": 300,    # длительность мута, если antilink_action == mute
        "ignore_spam_channels": [],     # список ID каналов, игнорируемых антиспамом/антилинком
        "ban_new_users_enabled": False,
        "ban_new_users_min_age_hours": 24,
        "ban_new_users_description": "Аккаунт слишком новый",
        "isolate_new_bots_enabled": False,  # снимать роли у новых ботов при добавлении
        "mod_log_channel_id": None,     # отдельный канал для логов модераторских действий
        # ---- Анти-mention-спам ----
        "antimention_enabled": False,
        "antimention_limit": 5,         # уникальных упоминаний пользователей в ОДНОМ сообщении
        "antimention_action": "mute",   # warn|mute|kick|ban
        "antimention_mute_seconds": 600,
        # ---- Верификация новых пользователей ----
        "verification_enabled": False,
        "verification_channel_id": None,     # канал, где появляется кнопка подтверждения
        "verification_unverified_role_id": None,  # роль, выдаваемая при вступлении (ограничивает доступ)
        "verification_verified_role_id": None,    # роль, выдаваемая после подтверждения (опционально)
        "verification_timeout_minutes": 0,   # 0 = бессрочно; >0 — кикнуть, если не подтвердил за N минут
        # ---- Аудит-лог вотчер ----
        "auditwatch_enabled": False,
        "auditwatch_channel_id": None,  # канал, куда дублируется ВЕСЬ аудит-лог сервера
    }

    def ensure_guild(self, guild_id: int):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO guild_settings (guild_id, settings_json) VALUES (?, ?)",
                (guild_id, json.dumps(self.DEFAULT_SETTINGS)),
            )
            self._conn.commit()

    def get_all_settings(self, guild_id: int) -> dict:
        cached = self._settings_cache.get(guild_id)
        if cached is not None:
            return cached

        with self._lock:
            # Повторная проверка кэша внутри лока — на случай, если другой поток
            # успел заполнить его, пока мы ждали блокировку.
            cached = self._settings_cache.get(guild_id)
            if cached is not None:
                return cached

            self.ensure_guild(guild_id)
            cur = self._conn.cursor()
            cur.execute("SELECT settings_json FROM guild_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
            data = json.loads(row["settings_json"]) if row else {}
            merged = {**self.DEFAULT_SETTINGS, **data}
            self._settings_cache[guild_id] = merged
            return merged

    def get_setting(self, guild_id: int, key: str, default: Any = None) -> Any:
        settings = self.get_all_settings(guild_id)
        return settings.get(key, default)

    def set_setting(self, guild_id: int, key: str, value: Any):
        with self._lock:
            settings = dict(self.get_all_settings(guild_id))
            settings[key] = value
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE guild_settings SET settings_json = ? WHERE guild_id = ?",
                (json.dumps(settings), guild_id),
            )
            self._conn.commit()
            # Кэш обновляем напрямую (а не просто инвалидируем), чтобы следующее
            # чтение сразу получило свежее значение без лишнего запроса к БД.
            self._settings_cache[guild_id] = settings

    # ---------- Варны ----------

    def add_warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO warns (guild_id, user_id, moderator_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, time.time()),
            )
            self._conn.commit()
            cur.execute(
                "SELECT COUNT(*) AS c FROM warns WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return cur.fetchone()["c"]

    def get_warns(self, guild_id: int, user_id: int) -> list:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM warns WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (guild_id, user_id),
        )
        return [dict(r) for r in cur.fetchall()]

    def clear_warns(self, guild_id: int, user_id: int):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM warns WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
            )
            self._conn.commit()

    def remove_last_warn(self, guild_id: int, user_id: int) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id FROM warns WHERE guild_id = ? AND user_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (guild_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute("DELETE FROM warns WHERE id = ?", (row["id"],))
            self._conn.commit()
            return True

    # ---------- Авто-наказания за варны ----------

    def set_warn_action(self, guild_id: int, warn_count: int, action: str, duration_seconds: Optional[int]):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO warn_actions (guild_id, warn_count, action, duration_seconds) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, warn_count) DO UPDATE SET action = ?, duration_seconds = ?",
                (guild_id, warn_count, action, duration_seconds, action, duration_seconds),
            )
            self._conn.commit()

    def remove_warn_action(self, guild_id: int, warn_count: int) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "DELETE FROM warn_actions WHERE guild_id = ? AND warn_count = ?",
                (guild_id, warn_count),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_warn_action(self, guild_id: int, warn_count: int) -> Optional[dict]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM warn_actions WHERE guild_id = ? AND warn_count = ?",
            (guild_id, warn_count),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_all_warn_actions(self, guild_id: int) -> list:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM warn_actions WHERE guild_id = ? ORDER BY warn_count ASC", (guild_id,)
        )
        return [dict(r) for r in cur.fetchall()]
