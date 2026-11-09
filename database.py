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
                    status TEXT DEFAULT 'unknown',
                    notes TEXT DEFAULT '[]',
                    autonomy_allowed INTEGER DEFAULT 0,
                    last_interaction TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)

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
            await db.commit()

    async def get_kv(self, key: str, default=None):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT value FROM system_kv WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else default

    async def set_kv(self, key: str, value: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT OR REPLACE INTO system_kv (key, value) VALUES (?, ?)", (key, value))
            await db.commit()

    async def get_or_create_user(self, user_id: int, username: str = "", first_name: str = "", is_admin: bool = False) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            
            if row:
                u_dict = dict(row)
                if is_admin and (u_dict["respect"] < 100 or u_dict["status"] != "creator"):
                    await db.execute("""
                        UPDATE users SET respect = 100, trust = 100, affection = 100, 
                        irritation = 0, status = 'creator' WHERE user_id = ?
                    """, (user_id,))
                    await db.commit()
                    cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                    return dict(await cursor.fetchone())
                return u_dict

            now_str = datetime.now().isoformat()
            if is_admin:
                await db.execute("""
                    INSERT INTO users (user_id, username, first_name, relationship, trust, affection, respect, irritation, status, autonomy_allowed, last_interaction)
                    VALUES (?, ?, ?, 100, 100, 100, 100, 0, 'creator', 1, ?)
                """, (user_id, username, first_name, now_str))
            else:
                await db.execute("""
                    INSERT INTO users (user_id, username, first_name, relationship, trust, affection, respect, irritation, status, autonomy_allowed, last_interaction)
                    VALUES (?, ?, ?, 50, 40, 40, 40, 0, 'unknown', 0, ?)
                """, (user_id, username, first_name, now_str))
            await db.commit()

            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return dict(await cursor.fetchone())

    async def update_user_relationship(self, user_id: int, updates: dict):
        fields, values = [], []
        for k, v in updates.items():
            if k in ["relationship", "trust", "affection", "respect", "irritation", "status", "notes", "autonomy_allowed", "last_interaction"]:
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
        now_str = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO global_memories (category, content, importance, timestamp) VALUES (?, ?, ?, ?)",
                (category[:80], content[:4000], max(0, min(10, int(importance))), now_str),
            )
            await db.commit()

    async def get_all_memories(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id, category, content, importance FROM global_memories ORDER BY id ASC")
            return [dict(r) for r in await cursor.fetchall()]

    async def rewrite_memories(self, compressed_items: list):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM global_memories")
            now_str = datetime.now().isoformat()
            for item in compressed_items:
                await db.execute("""
                    INSERT INTO global_memories (category, content, importance, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (item.get("category", "summary"), item.get("content", ""), item.get("importance", 5), now_str))
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
                  AND status NOT IN ('enemy', 'disliked')
            """)
            return [dict(r) for r in await cursor.fetchall()]

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
            await db.execute("DELETE FROM brain_logs")
            await db.execute("DELETE FROM messages")
            await db.commit()

    async def list_tasks(self, owner_id: int | None = None, limit: int = 30) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if owner_id is None:
                cursor = await db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY due_at LIMIT ?", (limit,))
            else:
                cursor = await db.execute("SELECT * FROM tasks WHERE status='pending' AND owner_id=? ORDER BY due_at LIMIT ?", (owner_id, limit))
            return [dict(row) for row in await cursor.fetchall()]

    async def add_scoped_memory(self, scope: str, content: str, category: str = "fact",
                                owner_id: int | None = None, chat_id: int | None = None,
                                importance: int = 5, confidence: float = 1.0,
                                source: str = ""):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO memory_items(scope, owner_id, chat_id, category, content,
                    importance, confidence, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (scope, owner_id, chat_id, category, content[:4000],
                   max(0, min(10, int(importance))), max(0.0, min(1.0, float(confidence))), source[:200]))
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

    async def delete_scoped_memory(self, memory_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
            await db.commit()

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

    async def delete_memory(self, memory_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM global_memories WHERE id = ?", (memory_id,))
            await db.commit()
