"""Persistent conversation state and Open WebUI chat synchronization."""

import asyncio
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx


log = logging.getLogger("meshcore-openwebui")


class OpenWebUIChatClient:
    """Small client for the Open WebUI v0.11.x chat and folder APIs."""

    def __init__(self, base_url, api_key, timeout, folder_name="meshcore"):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        self.folder_name = folder_name.strip()
        self._folder_id = None
        self._folder_lock = asyncio.Lock()

    async def _request(self, method, path, *, json_body=None, allow_missing=False):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                self.base_url + path,
                headers=self.headers,
                json=json_body,
            )
        if allow_missing and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json() if response.content else None

    async def get_chat(self, chat_id):
        return await self._request(
            "GET", f"/api/v1/chats/{chat_id}", allow_missing=True
        )

    async def create_chat(self, title, model):
        chat = self._chat_document(title, model, [])
        result = await self._request(
            "POST", "/api/v1/chats/new", json_body={"chat": chat}
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise RuntimeError("Open WebUI create-chat response did not contain an id")
        await self._put_in_folder(result["id"])
        return result["id"]

    async def update_chat(self, chat_id, title, model, messages):
        chat = self._chat_document(title, model, messages)
        return await self._request(
            "POST", f"/api/v1/chats/{chat_id}", json_body={"chat": chat}
        )

    async def _ensure_folder(self):
        if not self.folder_name:
            return None
        if self._folder_id:
            return self._folder_id

        async with self._folder_lock:
            if self._folder_id:
                return self._folder_id
            return await self._ensure_folder_unlocked()

    async def _ensure_folder_unlocked(self):

        result = await self._request("GET", "/api/v1/folders/")
        folders = result if isinstance(result, list) else []
        if isinstance(result, dict):
            folders = result.get("folders") or result.get("data") or []
        for folder in folders:
            if str(folder.get("name", "")).casefold() == self.folder_name.casefold():
                self._folder_id = folder.get("id")
                return self._folder_id

        created = await self._request(
            "POST", "/api/v1/folders/", json_body={"name": self.folder_name}
        )
        if isinstance(created, dict):
            self._folder_id = created.get("id")
        if not self._folder_id:
            raise RuntimeError(
                "Open WebUI create-folder response did not contain an id"
            )
        return self._folder_id

    async def _put_in_folder(self, chat_id):
        if not self.folder_name:
            return
        try:
            folder_id = await self._ensure_folder()
            await self._request(
                "POST",
                f"/api/v1/chats/{chat_id}/folder",
                json_body={"folder_id": folder_id},
            )
        except Exception:
            # Folder organization must never make radio chat unavailable. The chat
            # remains owned by the service account and can be moved manually.
            log.exception(
                "Could not place Open WebUI chat %s in folder %r",
                chat_id,
                self.folder_name,
            )

    @staticmethod
    def _chat_document(title, model, messages):
        """Build the message graph expected by Open WebUI's chat editor."""
        graph = {}
        flat = []
        parent_id = None
        now_ms = int(time.time() * 1000)
        for index, item in enumerate(messages):
            message_id = str(uuid.uuid4())
            message = {
                "id": message_id,
                "parentId": parent_id,
                "childrenIds": [],
                "role": item["role"],
                "content": item["content"],
                "timestamp": now_ms + index,
            }
            if item.get("model"):
                message["model"] = item["model"]
            if item.get("delivery") and item["delivery"] != "acked":
                message["delivery"] = item["delivery"]
            if parent_id:
                graph[parent_id]["childrenIds"].append(message_id)
            graph[message_id] = message
            flat.append(message)
            parent_id = message_id

        return {
            "title": title,
            "models": [model],
            "params": {},
            "history": {"messages": graph, "currentId": parent_id},
            "messages": flat,
            "tags": [],
            "timestamp": int(time.time()),
        }


class ConversationStore:
    """SQLite-backed node/chat mapping and authoritative recent transcript."""

    def __init__(self, path):
        self.path = Path(path)

    async def initialize(self):
        await asyncio.to_thread(self._initialize)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self):
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    node_key TEXT PRIMARY KEY,
                    chat_id TEXT,
                    title TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_key TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    model TEXT,
                    delivery TEXT NOT NULL DEFAULT 'received',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(node_key) REFERENCES conversations(node_key)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_node_id
                    ON messages(node_key, id);
                """
            )

    async def get_conversation(self, node_key):
        return await asyncio.to_thread(self._get_conversation, node_key)

    def _get_conversation(self, node_key):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE node_key = ?", (node_key,)
            ).fetchone()
            return dict(row) if row else None

    async def save_conversation(self, node_key, chat_id, title):
        await asyncio.to_thread(self._save_conversation, node_key, chat_id, title)

    def _save_conversation(self, node_key, chat_id, title):
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO conversations(node_key, chat_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_key) DO UPDATE SET
                    chat_id=excluded.chat_id, title=excluded.title,
                    updated_at=excluded.updated_at
                """,
                (node_key, chat_id, title, now, now),
            )

    async def replace_conversation(self, node_key, chat_id, title):
        await asyncio.to_thread(self._replace_conversation, node_key, chat_id, title)

    def _replace_conversation(self, node_key, chat_id, title):
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM conversations WHERE node_key = ?", (node_key,))
            db.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                (node_key, chat_id, title, now, now),
            )

    async def append_exchange(self, node_key, question, answer, model, delivery):
        await asyncio.to_thread(
            self._append_exchange, node_key, question, answer, model, delivery
        )

    def _append_exchange(self, node_key, question, answer, model, delivery):
        now = int(time.time())
        with self._connect() as db:
            db.executemany(
                """INSERT INTO messages
                   (node_key, role, content, model, delivery, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (node_key, "user", question, None, "received", now),
                    (node_key, "assistant", answer, model, delivery, now),
                ],
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE node_key = ?",
                (now, node_key),
            )

    async def messages(self, node_key):
        return await asyncio.to_thread(self._messages, node_key)

    def _messages(self, node_key):
        with self._connect() as db:
            rows = db.execute(
                "SELECT role, content, model, delivery, created_at "
                "FROM messages WHERE node_key = ? ORDER BY id",
                (node_key,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def context(self, node_key, max_turns, max_chars):
        messages = await self.messages(node_key)
        # Exchanges are stored atomically. Exclude both halves when the node did
        # not receive the answer so the model cannot refer to a phantom turn.
        exchanges = []
        pending_user = None
        for item in messages:
            if item["role"] == "user":
                pending_user = item
            elif pending_user and item["delivery"] in {"acked", "local-only"}:
                exchanges.append((pending_user, item))
                pending_user = None
        selected = []
        chars = 0
        for user_message, assistant_message in reversed(exchanges):
            if len(selected) >= max_turns:
                break
            size = len(user_message["content"]) + len(assistant_message["content"])
            if chars + size > max_chars:
                break
            selected.append((user_message, assistant_message))
            chars += size
        context = []
        for exchange in reversed(selected):
            context.extend(
                {"role": item["role"], "content": item["content"]} for item in exchange
            )
        return context

    async def count(self, node_key):
        return await asyncio.to_thread(self._count, node_key)

    def _count(self, node_key):
        with self._connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM messages WHERE node_key = ? AND role = 'user'",
                (node_key,),
            ).fetchone()[0]


class ConversationService:
    def __init__(self, store, client, max_turns=8, max_chars=8000):
        self.store = store
        self.client = client
        self.max_turns = max(1, max_turns)
        self.max_chars = max(256, max_chars)
        self._locks = {}
        self._locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def lock(self, node_key):
        async with self._locks_guard:
            lock = self._locks.setdefault(node_key, asyncio.Lock())
        async with lock:
            yield

    async def ensure_chat(self, node_key, title, model):
        conversation = await self.store.get_conversation(node_key)
        if conversation and conversation.get("chat_id"):
            remote = await self.client.get_chat(conversation["chat_id"])
            if remote is not None:
                return conversation

        # A definite missing chat is recreated. Other HTTP failures propagate.
        chat_id = await self.client.create_chat(title, model)
        await self.store.save_conversation(node_key, chat_id, title)
        return await self.store.get_conversation(node_key)

    async def new_chat(self, node_key, title, model):
        chat_id = await self.client.create_chat(title, model)
        await self.store.replace_conversation(node_key, chat_id, title)
        return chat_id

    async def context(self, node_key):
        return await self.store.context(node_key, self.max_turns, self.max_chars)

    async def record_and_sync(self, node_key, question, answer, model, delivery):
        await self.store.append_exchange(node_key, question, answer, model, delivery)
        conversation = await self.store.get_conversation(node_key)
        messages = await self.store.messages(node_key)
        await self.client.update_chat(
            conversation["chat_id"], conversation["title"], model, messages
        )

    async def status(self, node_key):
        conversation = await self.store.get_conversation(node_key)
        if not conversation:
            return None
        return {
            "chat_id": conversation["chat_id"],
            "title": conversation["title"],
            "turns": await self.store.count(node_key),
        }
