import aiosqlite
import json
from datetime import datetime
from config import DB_PATH

class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    relationship INTEGER DEFAULT 50,
                    trust INTEGER DEFAULT 50,
                    affection INTEGER DEFAULT 50,
                    respect INTEGER DEFAULT 50,
                    irritation INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'standard',
                    notes TEXT DEFAULT '[]',
                    autonomy_allowed INTEGER DEFAULT 0,
                    last_interaction TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_proactive_at TEXT,
                    proactive_day TEXT DEFAULT '',
                    proactive_count INTEGER DEFAULT 0
                );
            """)

            await db.execute("UPDATE users SET status = 'friend' WHERE status IN ('trusted', 'close_friend')")
            await db.execute("UPDATE users SET status = 'admin' WHERE status = 'admin'")
            await db.execute("UPDATE users SET status = 'standard' WHERE status IS NULL OR status IN ('unknown', 'enemy', 'disliked', 'acquaintance')")
            cursor = await db.execute("PRAGMA table_info(users)")
            user_columns = {column[1] for column in await cursor.fetchall()}
            for column, definition in {
                "last_proactive_at": "TEXT",
                "proactive_day": "TEXT DEFAULT ''",
                "proactive_count": "INTEGER DEFAULT 0",
            }.items():
                if column not in user_columns:
                    await db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (

                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                role TEXT,
                    content TEXT,
                    archived INTEGER DEFAULT 0,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Миграция старой базы: раньше контекст хранился только по автору.
            cursor = await db.execute("PRAGMA table_info(messages)")
            columns = await cursor.fetchall()
            if not any(column[1] == "chat_id" for column in columns):
                await db.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS global_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    content TEXT,
                    importance INTEGER DEFAULT 5,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS brain_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    reasoning TEXT,
                    emotions_before TEXT,
                    emotions_after TEXT,
                    actions_taken TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT DEFAULT '',
                    kind TEXT DEFAULT 'unknown',
                    context_summary TEXT DEFAULT '',
                    rules TEXT DEFAULT '',
                    last_activity TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_ada_reply TEXT DEFAULT '',
                    next_group_review_at REAL DEFAULT 0
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    owner_id INTEGER,
                    chat_id INTEGER,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    importance INTEGER DEFAULT 5,
                    confidence REAL DEFAULT 1.0,
                    source TEXT DEFAULT '',
                    expires_at TEXT,
                    last_used TEXT DEFAULT CURRENT_TIMESTAMP,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    chat_id INTEGER,
                    description TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    repeat_rule TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS meeting_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester_id INTEGER NOT NULL,
                    requester_name TEXT DEFAULT '',
                    purpose TEXT NOT NULL,
                    preferred_time TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_events (

                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    chat_id INTEGER,
                    actor_id INTEGER,
                    message_id INTEGER,
                    payload TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id INTEGER,
                    action TEXT NOT NULL,
                    content TEXT NOT NULL,
                    actor TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.commit()
            await self._migrate_legacy_memory()

    async def _migrate_legacy_memory(self):
        """Разовый перенос legacy global_memories в memory_items (scope=global)."""
        migrated = await self.get_kv("legacy_memory_migrated", "false")
        if migrated == "true":
            return
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM global_memories")
            count = (await cursor.fetchone() or (0,))[0]
            if count:
                await db.execute("""
                    INSERT INTO memory_items(scope, owner_id, chat_id, category, content,
                        importance, confidence, source)
                    SELECT 'global', NULL, NULL, category, content, importance, 0.8, 'legacy_migration'
                    FROM global_memories
                """)
                await db.execute("DELETE FROM global_memories")
                await db.commit()
            await self.set_kv("legacy_memory_migrated", "true")

    async def get_kv(self, key: str, default=None):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM system_kv WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else default

    async def set_kv(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO system_kv (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_chat(self, chat_id: int) -> dict | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_or_create_user(self, user_id: int, username: str = "", first_name: str = "", is_admin: bool = False) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            
            if row:
                u_dict = dict(row)
                # Создателю закрепляется только категория. Уважение остаётся тем,
                # что установлено инструментом или командой редактирования.
                if is_admin and u_dict["status"] != "creator":
                    await db.execute("UPDATE users SET status = 'creator' WHERE user_id = ?", (user_id,))
                    await db.commit()
                    u_dict["status"] = "creator"
                return u_dict

            now_str = datetime.now().isoformat()
            if is_admin:
                # Создатель: уважение по умолчанию 85, меняется только вручную.
                await db.execute("""
                    INSERT INTO users (user_id, username, first_name, relationship, trust, affection, respect, irritation, status, autonomy_allowed, last_interaction)
                    VALUES (?, ?, ?, 85, 80, 55, 85, 0, 'creator', 1, ?)
                """, (user_id, username, first_name, now_str))
            else:
                await db.execute("""
                    INSERT INTO users (user_id, username, first_name, relationship, trust, affection, respect, irritation, status, autonomy_allowed, last_interaction)
                    VALUES (?, ?, ?, 50, 40, 40, 40, 0, 'standard', 0, ?)
                """, (user_id, username, first_name, now_str))
            await db.commit()

            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return dict(await cursor.fetchone())

    async def update_user_relationship(self, user_id: int, updates: dict):
        fields, values = [], []
        for k, v in updates.items():
            if k in ["relationship", "trust", "affection", "respect", "irritation", "status", "notes", "autonomy_allowed", "last_interaction", "last_proactive_at", "proactive_day", "proactive_count"]:
                fields.append(f"{k} = ?")
                values.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
        if not fields:
            return
        values.append(user_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", tuple(values))
            await db.commit()

    async def save_message(self, user_id: int, role: str, content: str, chat_id: int | None = None):
        now_str = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO messages (user_id, chat_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                (user_id, chat_id, role, content[:12000], now_str),
            )
            await db.execute("UPDATE users SET last_interaction = ? WHERE user_id = ?", (now_str, user_id))
            await db.commit()

    async def get_active_dialog(self, user_id: int, chat_id: int | None = None, limit: int = 12) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if chat_id is None:
                cursor = await db.execute("""
                SELECT role, content, timestamp FROM messages 
                WHERE user_id = ? AND archived = 0 ORDER BY id DESC LIMIT ?
                """, (user_id, limit))
            else:
                cursor = await db.execute("""
                SELECT role, content, timestamp FROM messages
                WHERE chat_id = ? AND archived = 0 ORDER BY id DESC LIMIT ?
                """, (chat_id, limit))
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def count_unarchived_messages(self, user_id: int, chat_id: int | None = None) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            if chat_id is None:
                cursor = await db.execute("SELECT COUNT(*) FROM messages WHERE user_id = ? AND archived = 0", (user_id,))
            else:
                cursor = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND archived = 0", (chat_id,))
            res = await cursor.fetchone()
            return res[0] if res else 0

    async def get_messages_for_compression(self, user_id: int, chat_id: int | None = None, offset: int = 12) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if chat_id is None:
                cursor = await db.execute("""
                    SELECT id, role, content FROM messages
                    WHERE user_id = ? AND archived = 0 ORDER BY id ASC
                """, (user_id,))
            else:
                cursor = await db.execute("""
                    SELECT id, role, content FROM messages
                    WHERE chat_id = ? AND archived = 0 ORDER BY id ASC
                """, (chat_id,))
            rows = await cursor.fetchall()
            if len(rows) > offset:
                return [dict(r) for r in rows[:-offset]]
            return []

    async def archive_messages(self, message_ids: list):
        if not message_ids:
            return
        async with aiosqlite.connect(self.db_path) as db:
            q = f"UPDATE messages SET archived = 1 WHERE id IN ({','.join(['?']*len(message_ids))})"
            await db.execute(q, tuple(message_ids))
            await db.commit()

    async def add_memory(self, category: str, content: str, importance: int = 5):
        """Глобальная память: пишется сразу в новую таблицу memory_items."""
        await self.add_scoped_memory(
            "global", content, category=category,
            importance=max(0, min(10, int(importance))), confidence=0.9,
            source="legacy_add_memory",
        )

    async def get_all_memories(self) -> list:
        """Глобальные факты из memory_items (после миграции legacy там же)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, category, content, importance FROM memory_items WHERE scope='global' ORDER BY id ASC"
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def rewrite_memories(self, compressed_items: list):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM memory_items WHERE scope='global'")
            now_str = datetime.now().isoformat()
            for item in compressed_items:
                await db.execute("""
                    INSERT INTO memory_items(scope, owner_id, chat_id, category, content,
                        importance, confidence, source, created_at)
                    VALUES ('global', NULL, NULL, ?, ?, ?, 0.9, 'compression', ?)
                """, (item.get("category", "summary"), item.get("content", ""),
                      item.get("importance", 5), now_str))
            await db.commit()

    async def log_brain_action(self, user_id: int, reasoning: str, before: dict, after: dict, actions: list):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO brain_logs (user_id, reasoning, emotions_before, emotions_after, actions_taken)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, reasoning, json.dumps(before), json.dumps(after), json.dumps(actions, ensure_ascii=False)))
            await db.commit()

    async def get_all_users_for_autonomy(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM users
                WHERE autonomy_allowed = 1
                  AND status IN ('standard', 'friend', 'admin')
                  AND relationship >= 20
            """)
            return [dict(r) for r in await cursor.fetchall()]

    async def mark_proactive_attempt(self, user_id: int, timestamp: str, day: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                UPDATE users SET last_proactive_at = ?,
                    proactive_count = CASE WHEN proactive_day = ? THEN proactive_count + 1 ELSE 1 END,
                    proactive_day = ? WHERE user_id = ?
            """, (timestamp, day, day, user_id))
            await db.commit()


    async def set_user_category(self, user_id: int, category: str):
        category = category if category in {"стандарт", "друг", "админ"} else "стандарт"
        status = {"стандарт": "standard", "друг": "friend", "админ": "admin"}[category]
        await self.update_user_relationship(user_id, {"status": status})

    async def get_users_by_category(self, category: str) -> list:
        status = {"стандарт": "standard", "друг": "friend", "админ": "admin"}.get(category, category)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE status = ?", (status,))
            return [dict(row) for row in await cursor.fetchall()]

    async def clear_memory(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM global_memories")
            await db.execute("DELETE FROM memory_items")
            await db.execute("DELETE FROM memory_history")
            await db.execute("DELETE FROM brain_logs")
            await db.execute("DELETE FROM messages")
            await db.execute("DELETE FROM telegram_events")
            await db.execute("UPDATE users SET notes = '[]', last_interaction = CURRENT_TIMESTAMP")
            await db.execute("UPDATE chats SET context_summary = '', rules = '', last_ada_reply = ''")
            await db.commit()


    async def list_scoped_memories(self, limit: int = 100) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, scope, owner_id, chat_id, category, content, importance, confidence, source FROM memory_items ORDER BY importance DESC, id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def list_tasks(self, owner_id: int | None = None, limit: int = 30) -> list:

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if owner_id is None:
                cursor = await db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY due_at LIMIT ?", (limit,))
            else:
                cursor = await db.execute("SELECT * FROM tasks WHERE status='pending' AND owner_id=? ORDER BY due_at LIMIT ?", (owner_id, limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def _log_memory_history(self, db, memory_id: int, action: str, content: str, actor: str = ""):
        await db.execute(
            "INSERT INTO memory_history(memory_id, action, content, actor) VALUES (?, ?, ?, ?)",
            (memory_id, action, content[:4000], actor[:200]),
        )

    async def add_scoped_memory(self, scope: str, content: str, category: str = "fact",
                                owner_id: int | None = None, chat_id: int | None = None,
                                importance: int = 5, confidence: float = 1.0,
                                source: str = "", expires_at: str | None = None):
        async with aiosqlite.connect(self.db_path) as db:
            # Версионирование: находим активный факт в той же области с сильным
            # пересечением токенов. Другой текст => старый помечается superseded.
            similar_id, similar_content = None, None
            cursor = await db.execute("""
                SELECT id, content FROM memory_items
                WHERE scope = ? AND category = ?
                  AND (owner_id IS ? OR owner_id = ?)
                  AND (chat_id IS ? OR chat_id = ?)
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY created_at DESC LIMIT 50
            """, (scope, category, owner_id, owner_id, chat_id, chat_id))
            import re as _re
            new_tokens = {t.lower() for t in _re.findall(r"[\w-]{4,}", content)}
            for row in await cursor.fetchall():
                old_tokens = {t.lower() for t in _re.findall(r"[\w-]{4,}", row[1])}
                if new_tokens and old_tokens:
                    overlap = len(new_tokens & old_tokens) / min(len(new_tokens), len(old_tokens))
                    if overlap >= 0.7:
                        similar_id, similar_content = row[0], row[1]
                        break
            if similar_id is not None and similar_content != content:
                await db.execute(
                    "UPDATE memory_items SET confidence = 0.0, source = source || ' | superseded' WHERE id = ?",
                    (similar_id,),
                )
                await self._log_memory_history(db, similar_id, "superseded", similar_content, source)

            cursor = await db.execute("""
                INSERT INTO memory_items(scope, owner_id, chat_id, category, content,
                    importance, confidence, source, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (scope, owner_id, chat_id, category, content[:4000],
                  max(0, min(10, int(importance))), max(0.0, min(1.0, float(confidence))), source[:200],
                  expires_at))
            memory_id = cursor.lastrowid
            await self._log_memory_history(db, memory_id, "created", content, source)
            await db.commit()

    async def search_scoped_memories(self, query: str, owner_id: int | None = None,
                                     chat_id: int | None = None, limit: int = 20) -> list:
        pattern = f"%{query.lower()}%"
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM memory_items
                WHERE (? = '%%' OR lower(content) LIKE ?)
                  AND (owner_id IS NULL OR owner_id = ?)
                  AND (chat_id IS NULL OR chat_id = ?)
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY importance DESC, last_used DESC LIMIT ?
            """, (pattern, pattern, owner_id, chat_id, limit))
            return [dict(r) for r in await cursor.fetchall()]

    async def get_relevant_memories(self, query: str, owner_id: int, chat_id: int,
                                    is_group: bool, limit: int = 8) -> list:
        import re
        tokens = {token.lower() for token in re.findall(r"[\w-]{4,}", query)}
        stopwords = {"который", "которая", "которые", "этого", "этому", "пожалуйста", "сейчас", "можешь", "нужно", "хочу", "привет", "здравствуйте"}
        tokens -= stopwords
        if not tokens:
            return []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if is_group:
                cursor = await db.execute("""
                    SELECT * FROM memory_items
                    WHERE (scope = 'global' AND owner_id IS NULL AND chat_id IS NULL)
                       OR (scope = 'group' AND chat_id = ?)
                    ORDER BY importance DESC, created_at DESC LIMIT 200
                """, (chat_id,))
            else:
                cursor = await db.execute("""
                    SELECT * FROM memory_items
                    WHERE (scope = 'global' AND owner_id IS NULL AND chat_id IS NULL)
                       OR (scope = 'person' AND owner_id = ?)
                    ORDER BY importance DESC, created_at DESC LIMIT 200
                """, (owner_id,))
            rows = [dict(row) for row in await cursor.fetchall()]
        now = datetime.now().isoformat()
        scored = []
        for row in rows:
            if row.get("expires_at") and row["expires_at"] <= now:
                continue
            content_tokens = {token.lower() for token in re.findall(r"[\w-]{4,}", row["content"])}
            overlap = len(tokens & content_tokens)
            if overlap:
                score = overlap * 3 + int(row["importance"]) + float(row["confidence"])
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]]

    async def delete_scoped_memory(self, memory_id: int):

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT content FROM memory_items WHERE id = ?", (memory_id,))
            row = await cursor.fetchone()
            if row:
                await self._log_memory_history(db, memory_id, "deleted", row[0], "manual_delete")
            await db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
            await db.commit()

    async def get_memory_history(self, memory_id: int | None = None, limit: int = 30) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if memory_id is None:
                cursor = await db.execute(
                    "SELECT * FROM memory_history ORDER BY id DESC LIMIT ?", (limit,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY id DESC LIMIT ?",
                    (memory_id, limit),
                )
            return [dict(row) for row in await cursor.fetchall()]

    async def upsert_chat(self, chat_id: int, title: str, kind: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO chats(chat_id, title, kind, last_activity)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(chat_id) DO UPDATE SET
                    title=excluded.title, kind=excluded.kind, last_activity=CURRENT_TIMESTAMP
            """, (chat_id, title[:300], kind[:40]))
            await db.commit()

    async def update_chat_context(self, chat_id: int, summary: str | None = None,
                                  rules: str | None = None, last_reply: str | None = None):
        fields, values = [], []
        for key, value in (("context_summary", summary), ("rules", rules), ("last_ada_reply", last_reply)):
            if value is not None:
                fields.append(f"{key} = ?")
                values.append(value[:8000])
        if not fields:
            return
        values.append(chat_id)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"UPDATE chats SET {', '.join(fields)} WHERE chat_id = ?", values)
            await db.commit()

    async def add_event(self, event_type: str, payload: str, chat_id: int | None = None,
                        actor_id: int | None = None, message_id: int | None = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO telegram_events(event_type, chat_id, actor_id, message_id, payload)
                VALUES (?, ?, ?, ?, ?)
            """, (event_type, chat_id, actor_id, message_id, payload[:12000]))
            await db.commit()

    async def create_task(self, owner_id: int, description: str, due_at: str,
                          chat_id: int | None = None, repeat_rule: str = "") -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                INSERT INTO tasks(owner_id, chat_id, description, due_at, repeat_rule)
                VALUES (?, ?, ?, ?, ?)
            """, (owner_id, chat_id, description[:2000], due_at, repeat_rule[:200]))
            await db.commit()
            return cursor.lastrowid

    async def get_due_tasks(self, now_iso: str) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM tasks WHERE status = 'pending' AND due_at <= ? ORDER BY due_at ASC
            """, (now_iso,))
            return [dict(r) for r in await cursor.fetchall()]

    async def complete_task(self, task_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE tasks SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            await db.commit()

    async def cancel_task(self, task_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE tasks SET status='cancelled', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (task_id,),
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def reschedule_task(self, task_id: int, next_due: str):
        """Переносит повторяющуюся задачу на следующее время."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE tasks SET due_at = ?, status = 'pending', completed_at = NULL WHERE id = ?",
                (next_due, task_id),
            )
            await db.commit()

    async def delete_memory(self, memory_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM global_memories WHERE id = ?", (memory_id,))
            await db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
            await db.commit()
